#!/usr/bin/env python3
"""Versioned task scheduler: completed execution + current evidence + resolved issues."""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import uuid

import _legacy_state as base
import workflow_contract as c

# Scheduling primitives are reused from the frozen baseline. QA policy is owned below.
base.MAX_QA_ROUNDS = 10**9
base.MAX_QA_SEQ = 10**9
MAX_ROUNDS = 4
MAX_SAME_ROOT_REPAIRS = 2


def load(a):
    state = c.read(a.state)
    c.require(state.get("workflow_version") == 2 and state.get("release") == c.RELEASE,
              "这份 state 不是本版本创建的（缺 workflow_version/release），本版不接管；请用 task_state.py init 新建")
    c.require(Path(state["workspace"]).resolve() == Path(a.ws).resolve(), "--ws 与状态中的工作区不一致")
    c.config(a.ws)
    return state


def phase(state, pid):
    value = next((p for p in state["phases"] if p["id"] == pid), None)
    c.require(value is not None, f"未知阶段: {pid}")
    return value


def unresolved(state, pid):
    return [i for i in state.get("open_issues", []) if i.get("status") == "open"
            and i.get("severity") in ("S0", "S1")
            and (not i.get("phase") or i.get("phase") == pid)]


def required_checks(state, pid):
    scope = state.get("phase_plan", {}).get("qa_scope", {}).get(pid, {})
    result = {}
    for lv in c.LEVELS:
        rows = scope.get(lv)
        c.require(isinstance(rows, list) and rows, f"phase-plan 缺 {lv} 的具体范围")
        result[lv] = {f"{lv}-{i+1:03d}": value for i, value in enumerate(rows)}
    return result


def output(payload, rc=0):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


def init(a):
    c.require(not Path(a.state).exists(), "状态已存在；新版 init 不覆盖历史，不自动迁移")
    c.preflight_valid(a.ws, "tools")
    cfg, _ = c.config(a.ws)
    meta, errors = base.load_tickets(a.tickets)
    c.require(meta and not errors, f"ticket 读取失败: {errors}")
    for t in meta.values():
        errors += c.ticket_errors(c.ticket_fm(t["path"]))
    c.require(not errors, f"ticket 新版契约未通过: {errors}")
    c.require(set(cfg.get("integration_after", [])) <= set(meta), "integration_after 引用不存在的 ticket")
    # Run the same four lints as the user-facing check command before creating state.
    import workflow
    c.require(workflow.check(a) == 0, "四项静态检查未通过")
    with base.StateLock(a.state):
        c.require(not Path(a.state).exists(), "另一个执行器已经初始化状态")
        with tempfile.TemporaryDirectory() as temporary:
            shadow = argparse.Namespace(state=str(Path(temporary) / "state.json"), force=False, tickets=a.tickets)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = base.cmd_init(shadow)
            c.require(rc == 0, "基线/任务计划初始化失败")
            state = c.read(shadow.state)
        state.update(workflow_version=2, release=c.RELEASE, workspace=a.ws,
                     spec_hash=c.hashes(a.ws)["spec_hash"], created_at=c.now())
        base.save_state(a.state, state)
    c.event(a.ws, "initialized", tickets=len(meta), release=c.RELEASE)
    return output({"status": "initialized", "workflow_version": 2, "release": c.RELEASE, "tickets": len(meta)})


def sync(a):
    # This is explicit scope revision, never an automatic way to dismiss a change warning.
    c.require(a.reason and len(a.reason.strip()) >= 10, "sync 需 --reason 记录已确认的规格变更与影响处置")
    with base.StateLock(a.state):
        state = load(a)
        c.require(not any(p.get("active_run") for p in state["phases"]), "QA 正在运行，先完成/中止本轮再同步规格")
        import workflow
        c.require(workflow.check(a) == 0, "规格检查未通过")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            c.write(path, state)
            args = argparse.Namespace(state=str(path), tickets=a.tickets)
            rc = base.cmd_sync(args)
            c.require(rc == 0, "sync 失败")
            new = c.read(path)
        if state["spec_hash"] != c.hashes(a.ws)["spec_hash"]:
            completed = [t for t, value in new["tickets"].items() if value["status"] == "done"]
            c.require(not completed or a.impact_json, "有已完成 ticket 的规格变更必须提供 authority impact 处置清单 --impact-json")
            if a.impact_json:
                impact = c.read(a.impact_json)
                c.require(impact.get("from_spec_hash") == state["spec_hash"] and impact.get("to_spec_hash") == c.hashes(a.ws)["spec_hash"], "impact 清单基线不匹配")
                affected = impact.get("tickets")
                c.require(isinstance(affected, list) and affected and set(affected) <= set(new["tickets"]), "impact.tickets 必须列出受影响 ticket")
                for tid in affected:
                    new["tickets"][tid]["status"] = "stale"
                    new["tickets"][tid]["fix_attempts"] = 0
                    phase(new, new["tickets"][tid]["phase"])["status"] = "pending"
        new["spec_hash"] = c.hashes(a.ws)["spec_hash"]
        new.setdefault("scope_changes", []).append({"at": c.now(), "reason": a.reason, "spec_hash": new["spec_hash"]})
        base.save_state(a.state, new)
    c.event(a.ws, "spec_synced", reason=a.reason)
    return 0


def budget_reason(ph):
    if ph.get("qa_rounds", 0) >= MAX_ROUNDS:
        return "qa_budget_exhausted"
    if max(ph.get("repair_failures", {}).values(), default=0) >= MAX_SAME_ROOT_REPAIRS:
        return "same_root_repair_failed"
    return None


def next_action(a):
    state = load(a)
    current = c.hashes(a.ws)
    if current["spec_hash"] != state["spec_hash"]:
        return output({"action": "blueprint_changed", "hint": "规格/accept/QA 范围发生变化；先 impact、确认口径并 sync，不在旧证据上续跑"}, 3)
    try:
        c.preflight_valid(a.ws, "tools")
        cfg, _ = c.config(a.ws)
        foundation = cfg.get("integration_after", [])
        if all(state["tickets"][tid]["status"] == "done" for tid in foundation):
            c.preflight_valid(a.ws, "integration")
    except c.ContractError as exc:
        return output({"action": "preflight_required", "hint": str(exc)}, 3)
    for ph in state["phases"]:
        if ph.get("active_run"):
            if ph.get("execution_errors", 0) >= 2:
                return output({"action": "exception", "kind": "harness_unavailable", "phase": ph["id"],
                               "hint": "执行器连续两次不能完成必需检查；保留当前 run，诊断环境后再续跑，不计产品失败轮"}, 3)
            return output({"action": "qa_resume", "phase": ph["id"], "run": ph["active_run"],
                           "hint": "续跑同一 run 未完成分片，完成后 merge；不要创建新一轮"})
        if ph["status"] not in ("qa_passed", "accepted", "risk_accepted"):
            reason = budget_reason(ph)
            if reason:
                return output({"action": "exception", "kind": reason, "phase": ph["id"],
                               "hint": "需要具体诊断/决策；预算耗尽不代表必然是需求错误，不自动放行"}, 3)
            break
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = base.cmd_next(a)
    data = json.loads(buffer.getvalue())
    if data.get("action") == "impl":
        batch = data["spawn"]
        data["spawn"] = batch[:1]
        data["deferred"] = [x["ticket"] for x in batch[1:]] + data.get("deferred", [])
        data["hint"] = "共享验证栈容量 1：本批只派一票。按新版 impl 模板运行 accept，带 --accept-json 设置 done。"
    if data.get("action") in ("qa_gate", "qa_rework"):
        data["pending_issues"] = unresolved(state, data["phase"])
        data["hint"] = "先逐问题修复并复验，可用 workflow.py run 执行较小回归集；放行须 qa start → 完整独立 L0–L3 → qa merge → set。新问题不触发两轮停机，同根两次修复失败或总计四轮才停。"
        data["requires_qa_start"] = True
    return output(data, rc)


def start(a):
    with base.StateLock(a.state):
        state = load(a)
        ph = phase(state, a.phase)
        c.require(not ph.get("active_run"), "已有 active run；使用 next 续跑或 qa abort，不能覆盖")
        c.require(ph["status"] not in ("qa_passed", "accepted", "risk_accepted"), "已通过阶段不重复启动 QA；规格变更先 stale/sync")
        c.require(not budget_reason(ph), "QA 预算已达上限或同根修复反复失败，需要人决策")
        c.require(all(v["status"] in ("done", "escalated") for v in state["tickets"].values() if v["phase"] == a.phase), "本阶段 ticket 尚未落定")
        if ph.get("qa_rounds", 0):
            for item in unresolved(state, a.phase):
                repair = item.get("repair", {})
                c.require(repair.get("id") and repair["id"] != item.get("consumed_repair"),
                          f"issue #{item['id']} 尚无本次修复/局部复验记录；先 issue repair 再启动下一轮")
                c.validate_assets([repair.get("evidence", {})])
        c.preflight_valid(a.ws, "tools")
        c.preflight_valid(a.ws, "integration")
        binding = {**c.identity(a.ws, a.environment, a.artifact_manifest), "run_id": uuid.uuid4().hex,
                   "phase": a.phase, "round": ph.get("qa_seq", 0) + 1, "mode": "full",
                   "required_checks": required_checks(state, a.phase)}
        c.require(binding["spec_hash"] == state["spec_hash"], "规格已改变，先 impact/sync")
        ph["active_run"] = binding
        ph["execution_errors"] = 0
        ph["status"] = "qa_running"
        base.save_state(a.state, state)
    c.event(a.ws, "qa_start", **binding)
    return output({"binding": binding, "pending_issues": unresolved(state, a.phase),
                   "shards": [{"levels": ["L0", "L1"], "path": str(Path(a.state).parent / "qa" / f"{a.phase}-r{binding['round']}-L01.json")},
                              {"levels": ["L2", "L3"], "path": str(Path(a.state).parent / "qa" / f"{a.phase}-r{binding['round']}-L23.json")}],
                   "resource_capacity": 1})


def validate_level(value):
    c.require(isinstance(value, dict), "level 必须是 object")
    c.require(value.get("execution") in ("completed", "error", "blocked"), "缺 execution: completed/error/blocked")
    c.validate_assets(value.get("evidence"))
    checks = value.get("checks")
    c.require(isinstance(checks, list) and checks, "每级需逐项检查结果；不能只写一段自评")
    ids = [i.get("id") for i in checks if isinstance(i, dict)]
    c.require(len(ids) == len(checks) and all(ids) and len(set(ids)) == len(ids), "检查 id 缺失/重复")
    for item in checks:
        c.require(item.get("status") in ("pass", "fail", "error", "blocked"), "检查状态无效")
        c.require(type(item.get("assertions")) is int and item["assertions"] >= 0, "需填写实际 assertions 数")
        if item["status"] in ("pass", "fail"):
            c.require(item["assertions"] > 0, "零断言不能算执行完成")
    if value["execution"] == "completed":
        c.require(all(i["status"] in ("pass", "fail") for i in checks), "含 error/blocked 不能声明 completed")


def normalize_issue(item, pid):
    c.require(isinstance(item, dict), "issue 必须是 object")
    c.require(item.get("severity") in ("S0", "S1", "S2", "S3"), "issue 缺有效严重度")
    c.require(item.get("level") in c.LEVELS and item.get("title") and item.get("root_cause"), "issue 需 level/title/root_cause 稳定指纹")
    result = dict(item, phase=pid)
    if result.get("type") == "harness" and result["severity"] in ("S0", "S1"):
        result["severity_reported"] = result["severity"]
        result["severity"] = "S2"
    return result


def merge(a):
    with base.StateLock(a.state):
        state = load(a)
        ph = phase(state, a.phase)
        binding = ph.get("active_run")
        c.require(binding, "先 qa start 绑定本轮版本与环境")
        c.validate_binding(a.ws, binding, binding)
        directory = Path(a.state).parent / "qa"
        paths = sorted(directory.glob(f"{a.phase}-r{binding['round']}-*.json"))
        c.require(paths, "没有分片报告")
        levels, issues, retests = {}, {}, {}
        for path in paths:
            data = c.read(path)
            c.validate_binding(a.ws, data.get("binding"), binding)
            c.require(data.get("auditor") == "independent", "分片不是独立 QA")
            for lv, value in data.get("levels", {}).items():
                c.require(lv in c.LEVELS and lv not in levels, f"未知或重复认领的级: {lv}")
                validate_level(value)
                c.require(set(binding["required_checks"][lv]) <= {x["id"] for x in value["checks"]},
                          f"{lv} 尚未覆盖 phase-plan 要求的全部检查")
                levels[lv] = value
            for issue in data.get("issues", []):
                item = normalize_issue(issue, a.phase)
                key = item["root_cause"]
                observations = issues.get(key, {}).get("observations", []) + [{"level": item["level"], "checks": item.get("checks", [])}]
                if key not in issues or item["severity"] < issues[key]["severity"]:
                    issues[key] = item
                issues[key]["observations"] = observations
            for retest in data.get("retests", []):
                c.require(retest.get("status") in ("pass", "fail") and retest.get("root_cause"), "复验需 root_cause/status")
                c.validate_assets(retest.get("evidence"))
                key = retest["root_cause"]
                c.require(key not in retests or retests[key]["status"] == retest["status"], "分片复验结论冲突")
                retests[key] = retest
        c.require(set(levels) == set(c.LEVELS), "缺少 L0–L3 分片，不合并")
        # Every failed check must be explicitly associated with a finding; no silent fail->pass.
        for lv, value in levels.items():
            failed = {x["id"] for x in value["checks"] if x["status"] == "fail"}
            linked = {check for i in issues.values() for o in i.get("observations", []) if o["level"] == lv for check in o["checks"]}
            c.require(failed <= linked, f"{lv} 的失败检查缺问题关联: {sorted(failed-linked)}")
        for item in unresolved(state, a.phase):
            key = item.get("root_cause")
            c.require(key in retests or key in issues, f"未复验台账阻塞项 #{item['id']}: {item['title']}")
            if key in retests and retests[key]["status"] == "fail" and key not in issues:
                issues[key] = normalize_issue(item, a.phase)
        failed_retests = {k for k, v in retests.items() if v["status"] == "fail"}
        c.require(failed_retests <= set(issues), "失败复验必须列出对应问题")
        for key, item in issues.items():
            c.require(not (key in retests and retests[key]["status"] == "pass"), "同一根因同时报已修与仍有问题")
        complete = all(v["execution"] == "completed" for v in levels.values())
        blocking = [i for i in issues.values() if i["severity"] in ("S0", "S1")]
        verdict = "ERROR" if not complete else "FAIL" if blocking else "PASS"
        report = {"binding": binding, "auditor": "independent", "verdict": verdict,
                  "levels": levels, "issues": list(issues.values()), "retests": list(retests.values()),
                  "merged_from": [c.asset(p) for p in paths]}
        canonical = directory / f"{a.phase}-r{binding['round']}.json"
        c.write(canonical, report)
        ph["merged_audit"] = c.asset(canonical)
        if verdict == "ERROR":
            ph["execution_errors"] = ph.get("execution_errors", 0) + 1
        else:
            ph["execution_errors"] = 0
        base.save_state(a.state, state)
    return output({"verdict": verdict, "audit": str(canonical), "hint": "ERROR: 修复执行器后续跑本 run；PASS/FAIL: set --phase --status qa_passed/qa_failed"})


def validate_accept(a, state):
    c.require(a.accept_json, "done 必须提供 workflow.py accept 产出的 --accept-json")
    report = c.read(a.accept_json)
    binding = report.get("binding", {})
    meta, errors = base.load_tickets(state["tickets_dir"])
    c.require(not errors and a.ticket in meta, "ticket 读取失败")
    fm = c.ticket_fm(meta[a.ticket]["path"])
    c.require(report.get("status") == "pass" and binding.get("ticket") == a.ticket, "accept 未通过或归属错误")
    c.require(binding.get("ticket_hash") == c.file_hash(meta[a.ticket]["path"]), "票面已变化")
    current = c.identity(a.ws, binding.get("environment"), binding.get("artifact_manifest", {}).get("path", ""))
    c.require(current["spec_hash"] == state["spec_hash"], "规格已改变，先确认并 sync，再记录 ticket 完工")
    c.require(all(binding.get(k) == v for k, v in current.items()), "accept 证据已过期")
    checks = report.get("checks", [])
    c.require([x.get("index") for x in checks] == list(range(len(fm["accept"]))), "accept 未全部执行")
    for item in checks:
        c.require(item.get("status") == "pass" and item.get("exit_code") == 0, "accept 不全绿")
        c.validate_assets(item.get("evidence"))


def set_status(a):
    with base.StateLock(a.state):
        state = load(a)
        if a.ticket:
            c.require(a.ticket in state["tickets"], "未知 ticket")
            c.require(a.status in base.TICKET_STATUSES, "ticket 状态无效")
            item = state["tickets"][a.ticket]
            c.require(not phase(state, item["phase"]).get("active_run"), "QA 运行中不改变 ticket 状态")
            if a.status == "done":
                validate_accept(a, state)
                c.require(a.note.strip(), "done 需记录非显然项/下游注意事项")
                item["accept_evidence"] = c.asset(a.accept_json)
            if a.status == "failed":
                item["fix_attempts"] += 1
                if item["fix_attempts"] >= 2:
                    a.status = "escalated"
                    upsert_issue(state, {"phase": item["phase"], "ticket": a.ticket, "severity": "S1", "type": "escalated",
                                        "level": "L1", "root_cause": "escalated:" + a.ticket, "title": "ticket 两次自修失败，需要诊断", "detail": a.note})
            if a.status in ("stale", "pending"):
                item["fix_attempts"] = 0
                phase(state, item["phase"])["status"] = "pending"
            item.update(status=a.status, note=a.note or item.get("note", ""), updated_at=c.now())
        else:
            ph = phase(state, a.phase)
            c.require(a.status in ("accepted", "qa_passed", "qa_failed", "risk_accepted"), "QA 只能由 qa start 启动；阶段状态无效")
            if a.status == "accepted":
                c.require(ph["status"] in ("qa_passed", "risk_accepted"), "须先完成 QA/记名风险接受，再记录人工抽验")
                ph["status"] = "accepted"
            elif a.status == "risk_accepted":
                c.require(not ph.get("active_run") and ph.get("last_audit"), "先完整 QA 并记录 qa_failed，再决定接受哪些残留")
                c.require(a.by and len(a.note.strip()) >= 30, "记名风险接受需 --by 与至少 30 字具体残留说明；不能自动调用")
                c.validate_assets([ph["last_audit"]])
                report = c.read(ph["last_audit"]["path"])
                c.validate_binding(a.ws, report["binding"], report["binding"])
                c.require(report["verdict"] == "FAIL", "风险接受只适用于已经完成的产品 FAIL，不可覆盖工具错误或缺失检查")
                ph.update(status="risk_accepted", human_override={"by": a.by, "note": a.note,
                          "at": c.now(), "qa_verdict": "FAIL", "audit": ph["last_audit"]})
                accepted = unresolved(state, a.phase)
                ph["human_override"]["issue_ids"] = [i["id"] for i in accepted]
                for item in accepted:
                    item.update(status="accepted", decision={"by": a.by, "note": a.note, "at": c.now()})
            else:
                binding = ph.get("active_run")
                c.require(binding and ph.get("merged_audit"), "缺当前 run 的 merge 结果")
                c.validate_assets([ph["merged_audit"]])
                report = c.read(ph["merged_audit"]["path"])
                c.validate_binding(a.ws, report.get("binding"), binding)
                c.validate_assets(report["merged_from"])
                c.require(report["verdict"] == ("PASS" if a.status == "qa_passed" else "FAIL"), "执行错误不计产品失败轮；set 必须与本轮 verdict 一致")
                c.require(all(v["execution"] == "completed" for v in report["levels"].values()), "测试未完成")
                for value in report["levels"].values():
                    validate_level(value)
                old = {i.get("root_cause"): i for i in unresolved(state, a.phase)}
                retests = {r["root_cause"]: r for r in report["retests"]}
                roots = {i["root_cause"] for i in report["issues"] if i["severity"] in ("S0", "S1")}
                for root, item in old.items():
                    c.require(root in retests or root in roots, f"merge 后新增/遗漏阻塞项: {root}")
                    if retests.get(root, {}).get("status") == "pass":
                        c.validate_assets(retests[root]["evidence"])
                        item.update(status="resolved", resolved_at=c.now(), resolution=ph["merged_audit"])
                        ph.setdefault("repair_failures", {})[root] = 0
                # A repeated finding after a recorded repair counts; discovering a new root does not.
                for root in (roots & set(old) if ph.get("qa_rounds", 0) else set()):
                    repair = old[root].get("repair")
                    c.require(repair and repair.get("id") != old[root].get("consumed_repair"), "同根问题再次进入 QA 前，须 issue repair 记录本次修复与局部复验")
                    c.validate_assets([repair["evidence"]])
                    old[root]["consumed_repair"] = repair["id"]
                    ph.setdefault("repair_failures", {})[root] = ph.get("repair_failures", {}).get(root, 0) + 1
                for finding in report["issues"]:
                    upsert_issue(state, finding)
                if a.status == "qa_passed":
                    c.require(not unresolved(state, a.phase), "仍有未解决 S0/S1，不能通过")
                    c.require(all(v["status"] == "done" for v in state["tickets"].values() if v["phase"] == a.phase), "存在未实施 ticket，不能算 QA 通过")
                ph.update(status=a.status, qa_seq=binding["round"], qa_rounds=ph.get("qa_rounds", 0) + 1,
                          last_audit=ph.pop("merged_audit"), updated_at=c.now())
                ph.pop("active_run")
        base.save_state(a.state, state)
    c.event(a.ws, "state_changed", ticket=a.ticket, phase=a.phase, status=a.status)
    return output({"status": a.status, "ticket": a.ticket, "phase": a.phase})


def upsert_issue(state, finding):
    rows = state.setdefault("open_issues", [])
    existing = next((i for i in rows if i.get("root_cause") == finding["root_cause"] and i.get("phase") == finding["phase"]), None)
    if existing:
        if existing.get("status") != "open":
            existing["reopened"] = existing.get("reopened", 0) + 1
        existing.update(finding, status="open", updated_at=c.now())
    else:
        rows.append({**finding, "id": max((i["id"] for i in rows), default=0) + 1,
                     "status": "open", "created_at": c.now()})


def issue(a):
    if a.issue_cmd == "list":
        return base.cmd_issue_list(a)
    with base.StateLock(a.state):
        state = load(a)
        if a.issue_cmd == "add":
            finding = normalize_issue({"severity": a.severity, "level": a.level, "type": a.itype,
                                       "root_cause": a.root_cause, "title": a.title, "detail": a.detail, "ticket": a.ticket}, a.phase)
            phase(state, a.phase)
            upsert_issue(state, finding)
        else:
            item = next((i for i in state["open_issues"] if i["id"] == a.id), None)
            c.require(item, "未知 issue")
            c.require(a.note and len(a.note.strip()) >= 10, "记录具体修复或决策依据")
            if a.issue_cmd == "repair":
                ph = phase(state, item["phase"])
                c.require(not ph.get("active_run"), "不能在 QA 运行中补写修复记录")
                c.require(item["status"] == "open", "只能修复 open issue")
                c.require(a.evidence, "修复需局部复验日志 --evidence")
                item["repair"] = {"id": uuid.uuid4().hex, "at": c.now(), "source_hash": c.hashes(a.ws)["source_hash"],
                                  "note": a.note, "evidence": c.asset(a.evidence)}
            else:
                c.require(a.by, "接受已知边界必须记录授权人 --by，不用于自动关闭已修问题")
                c.require(item["severity"] in ("S2", "S3"), "S0/S1 必须在完整 QA FAIL 后以阶段 risk_accepted 记录，不能逐条隐藏后变成 PASS")
                item.update(status="accepted", decision={"by": a.by, "note": a.note, "at": c.now()})
        base.save_state(a.state, state)
    c.event(a.ws, "issue_" + a.issue_cmd, issue_id=getattr(a, "id", None))
    return 0


def abort(a):
    c.require(a.reason and len(a.reason.strip()) >= 10, "中止需记录原因")
    with base.StateLock(a.state):
        state = load(a)
        ph = phase(state, a.phase)
        binding = ph.get("active_run")
        c.require(binding, "没有 active run")
        if ph.get("merged_audit"):
            c.require(c.read(ph["merged_audit"]["path"]).get("verdict") == "ERROR",
                      "已完成的产品 QA 不能 abort 抹掉轮次；必须据 verdict 设置状态")
        ph.setdefault("aborted_runs", []).append({"binding": binding, "reason": a.reason, "at": c.now()})
        ph["qa_seq"] = binding["round"]
        ph.pop("active_run")
        ph.pop("merged_audit", None)
        ph["status"] = "impl_done"
        base.save_state(a.state, state)
    c.event(a.ws, "qa_aborted", run_id=binding["run_id"], reason=a.reason)
    return 0


def rearm(a):
    c.require(a.by and a.reason and len(a.reason.strip()) >= 10, "rearm 需人授权 --by 与具体 --reason")
    with base.StateLock(a.state):
        state = load(a)
        ph = phase(state, a.phase)
        c.require(ph.get("qa_rounds", 0) < MAX_ROUNDS, "累计四轮产品 QA 已耗尽，不能 rearm 突破")
        c.require(budget_reason(ph) == "same_root_repair_failed", "只为同根修复反复失败提供有授权的重启")
        ph.setdefault("rearms", []).append({"by": a.by, "reason": a.reason, "at": c.now(), "repair_failures": ph.get("repair_failures", {})})
        ph["repair_failures"] = {}
        base.save_state(a.state, state)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ws")
    p.add_argument("--state")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("init", init), ("sync", sync)):
        sp = sub.add_parser(name); sp.add_argument("--tickets")
        if name == "sync":
            sp.add_argument("--reason", required=True); sp.add_argument("--impact-json")
        sp.set_defaults(func=fn)
    sub.add_parser("next").set_defaults(func=next_action)
    sub.add_parser("report").set_defaults(func=base.cmd_report)
    ss = sub.add_parser("set")
    ss.add_argument("--ticket"); ss.add_argument("--phase"); ss.add_argument("--status", required=True)
    ss.add_argument("--note", default=""); ss.add_argument("--accept-json"); ss.add_argument("--by")
    ss.set_defaults(func=set_status)
    qa = sub.add_parser("qa").add_subparsers(dest="qa_cmd", required=True)
    st = qa.add_parser("start"); st.add_argument("--phase", required=True)
    st.add_argument("--environment", required=True); st.add_argument("--artifact-manifest", required=True); st.set_defaults(func=start)
    mg = qa.add_parser("merge"); mg.add_argument("--phase", required=True); mg.set_defaults(func=merge)
    ab = qa.add_parser("abort"); ab.add_argument("--phase", required=True); ab.add_argument("--reason", required=True); ab.set_defaults(func=abort)
    iss = sub.add_parser("issue").add_subparsers(dest="issue_cmd", required=True)
    il = iss.add_parser("list"); il.add_argument("--undecided", action="store_true"); il.set_defaults(func=issue)
    ia = iss.add_parser("add"); ia.add_argument("--phase", required=True); ia.add_argument("--ticket", default="")
    ia.add_argument("--severity", required=True); ia.add_argument("--level", required=True); ia.add_argument("--type", dest="itype", default="debt")
    ia.add_argument("--root-cause", required=True); ia.add_argument("--title", required=True); ia.add_argument("--detail", default=""); ia.set_defaults(func=issue)
    for name in ("repair", "accept-risk"):
        sp = iss.add_parser(name); sp.add_argument("--id", type=int, required=True); sp.add_argument("--note", required=True)
        sp.add_argument("--by"); sp.add_argument("--evidence"); sp.set_defaults(func=issue)
    re = sub.add_parser("rearm"); re.add_argument("--phase", required=True); re.add_argument("--reason", required=True); re.add_argument("--by", required=True); re.set_defaults(func=rearm)
    a = p.parse_args()
    a.ws = str(Path(base.resolve_ws(a.ws)).resolve())
    a.state = str(Path(a.state).resolve()) if a.state else str(Path(a.ws) / "waves/state.json")
    if hasattr(a, "tickets"):
        a.tickets = str(Path(a.tickets).resolve()) if a.tickets else str(Path(a.ws) / "tickets")
    try:
        return a.func(a)
    except (c.ContractError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
