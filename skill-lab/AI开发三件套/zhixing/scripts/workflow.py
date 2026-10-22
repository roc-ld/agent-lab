#!/usr/bin/env python3
"""D0, artifact provenance, executable ticket acceptance and resource locking."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import workflow_contract as c


def preflight(a):
    cfg, root = c.config(a.ws)
    checks = cfg["preflight"].get(a.stage)
    c.require(isinstance(checks, list) and checks, f"preflight.{a.stage} 必须配置实际检查")
    ids = [x.get("id") for x in checks]
    c.require(all(ids) and len(set(ids)) == len(ids), "D0 检查 id 必须非空且唯一")
    c.require(any(x.get("expect_exit", 0) == 0 for x in checks), "D0 必须包含一条预期成功的正例，验证执行器确实可用")
    c.require(any(x.get("expect_exit", 0) != 0 for x in checks), "D0 必须包含一条预期失败的反例，验证错误不会被吞")
    before = c.hashes(a.ws)
    rows = []
    for check in checks:
        c.require(isinstance(check.get("run"), str) and check["run"].strip(), "D0 缺 run")
        expected = check.get("expect_exit", 0)
        c.require(type(expected) is int and expected >= 0, "expect_exit 必须是非负整数")
        rc, evidence = c.command(a.ws, check["run"], f"D0:{a.stage}:{check['id']}", check.get("timeout", 300), shell=True)
        rows.append({"id": check["id"], "status": "pass" if rc == expected else "fail",
                     "exit_code": rc, "expected_exit": expected, "evidence": [evidence]})
    after = c.hashes(a.ws)
    stable = all(before[k] == after[k] for k in ("config_hash", "harness_hash"))
    report = {"stage": a.stage, "status": "pass" if stable and all(x["status"] == "pass" for x in rows) else "fail",
              "python": str(Path(sys.executable).resolve()), "at": c.now(), **after, "checks": rows}
    path = Path(a.ws) / f"waves/preflight-{a.stage}.json"
    c.write(path, report)
    print(json.dumps({"status": report["status"], "report": str(path)}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


def stamp(a):
    before = c.hashes(a.ws)
    artifacts = [c.asset(p) for p in a.artifact]
    rc, evidence = c.command(a.ws, a.probe, "artifact_probe", a.timeout, shell=True)
    c.require(rc == 0, "运行产物版本探测失败，未签发产物清单")
    c.require(before == c.hashes(a.ws), "探测期间源码/规格/工具发生变化")
    c.validate_assets(artifacts)
    c.write(a.out, {"environment": a.environment, "source_hash": before["source_hash"],
                   "at": c.now(), "artifacts": artifacts, "probe_exit": rc, "probe_evidence": [evidence]})
    print(a.out)
    return 0


def acceptance(a):
    cfg, root = c.config(a.ws)
    c.preflight_valid(a.ws, "tools")
    paths = list((Path(a.ws) / "tickets").rglob("prompt.md"))
    candidates = [(p, c.ticket_fm(p)) for p in paths]
    matches = [(p, fm) for p, fm in candidates if str(fm.get("ticket") or p.parent.name) == a.ticket]
    c.require(len(matches) == 1, "ticket 不存在或重复")
    path, fm = matches[0]
    c.require(not c.ticket_errors(fm), "; ".join(c.ticket_errors(fm)))
    accepts = fm.get("accept")
    c.require(isinstance(accepts, list) and accepts, "accept 为空")
    before = c.identity(a.ws, a.environment, a.artifact_manifest)
    binding = {**before, "ticket": a.ticket, "ticket_hash": c.file_hash(path), "run_id": uuid.uuid4().hex}
    rows = []
    for index, accept in enumerate(accepts):
        c.require(isinstance(accept.get("run"), str) and accept["run"].strip(), "accept.run 为空")
        result = c.inside(root, accept["result"]) if accept.get("form") == "E2E" else None
        if result:
            c.require(not result.exists(), f"E2E 输出必须是本次新文件，先清理旧结果: {result}")
        rc, evidence = c.command(a.ws, accept["run"], f"accept:{a.ticket}:{index}", accept.get("timeout", a.timeout), shell=True)
        row = {"index": index, "name": accept.get("name"), "form": accept.get("form"),
               "status": "pass" if rc == 0 else "fail", "exit_code": rc, "evidence": [evidence]}
        if result and rc == 0:
            ui = c.read(result)
            c.require(ui.get("status") == "pass" and type(ui.get("assertions")) is int and ui["assertions"] > 0,
                      "E2E 必须有实际成功断言，零用例不算通过")
            c.require(ui.get("entry") == accept["entry"] and ui.get("action") == accept["action"] and ui.get("outcome") == accept["outcome"],
                      "E2E 结果未覆盖票面入口/操作/业务结果")
            c.validate_assets(ui.get("evidence"))
            archive = Path(a.ws) / "waves/evidence" / binding["run_id"] / f"accept-{index}"
            archive.mkdir(parents=True)
            for number, source in enumerate([c.asset(result), *ui["evidence"]]):
                target = archive / f"{number}-{Path(source['path']).name}"
                shutil.copyfile(source["path"], target)
                saved = c.asset(target)
                c.require(saved["sha256"] == source["sha256"], "E2E 证据归档期间发生变化")
                row["evidence"].append(saved)
        rows.append(row)
    c.require(before == c.identity(a.ws, a.environment, a.artifact_manifest), "验收期间源码/规格/工具/产物变化，请重新运行")
    report = {"binding": binding, "status": "pass" if all(x["status"] == "pass" for x in rows) else "fail",
              "at": c.now(), "checks": rows}
    out = Path(a.ws) / "waves/accept" / f"{a.ticket}-{binding['run_id']}.json"
    c.write(out, report)
    c.event(a.ws, "accept_complete", ticket=a.ticket, status=report["status"], report=str(out))
    print(json.dumps({"status": report["status"], "accept_json": str(out)}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


def check(a):
    cfg, root = c.config(a.ws)
    skills = Path(__file__).resolve().parents[1].parent   # <skills>/zhixing/scripts/workflow.py → <skills>
    xuqiu = skills / "xuqiu" / "scripts"
    zhixing = Path(__file__).resolve().parent
    # Installed and staging layouts deliberately have identical sibling structure.
    jobs = [([sys.executable, str(zhixing / "dag_lint.py"), str(Path(a.ws) / "tickets")], {0, 2}),
            ([sys.executable, str(xuqiu / "coverage_lint.py"), "--ws", a.ws], {0}),
            ([sys.executable, str(xuqiu / "authority_lint.py"), "--ws", a.ws, "check"], {0}),
            ([sys.executable, str(xuqiu / "adr_check.py"), "--ws", a.ws, "--repo-root", str(root)], {0, 2})]
    for argv, allowed in jobs:
        completed = subprocess.run(argv, cwd=root)
        if completed.returncode not in allowed:
            return 1
    print("四项静态检查通过；warning 单独评估，不把 exit 2 当结构失败。")
    return 0


def run_command(a):
    argv = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
    c.require(argv, "run 缺命令")
    rc, evidence = c.command(a.ws, argv, a.label, a.timeout)
    print(json.dumps({"exit_code": rc, "evidence": evidence}, ensure_ascii=False))
    return rc


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ws", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    pf = sub.add_parser("preflight")
    pf.add_argument("--stage", choices=("tools", "integration"), required=True)
    pf.set_defaults(func=preflight)
    st = sub.add_parser("stamp")
    st.add_argument("--environment", required=True)
    st.add_argument("--artifact", action="append", required=True)
    st.add_argument("--probe", required=True, help="实际查询运行实例版本的命令；不是 echo 一个本地期望值")
    st.add_argument("--out", required=True)
    st.add_argument("--timeout", type=int, default=60)
    st.set_defaults(func=stamp)
    ac = sub.add_parser("accept")
    ac.add_argument("--ticket", required=True)
    ac.add_argument("--environment", required=True)
    ac.add_argument("--artifact-manifest", required=True)
    ac.add_argument("--timeout", type=int, default=300)
    ac.set_defaults(func=acceptance)
    ch = sub.add_parser("check")
    ch.set_defaults(func=check)
    fp = sub.add_parser("fingerprint")
    fp.set_defaults(func=lambda a: print(json.dumps(c.hashes(a.ws), ensure_ascii=False)) or 0)
    run = sub.add_parser("run", help="在共享验证锁内跑一次命令；用于返工复验与回归 runner")
    run.add_argument("--label", required=True)
    run.add_argument("--timeout", type=int, default=300)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    run.set_defaults(func=run_command)
    a = p.parse_args()
    a.ws = str(Path(a.ws).resolve())
    try:
        return a.func(a)
    except (c.ContractError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
