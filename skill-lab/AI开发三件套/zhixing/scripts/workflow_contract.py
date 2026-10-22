"""Version 2 evidence primitives. No live workspace is modified on import."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import tempfile
import time
import uuid

RELEASE = "2026-09-22"
LEVELS = ("L0", "L1", "L2", "L3")
IGNORED_PARTS = {".git", "node_modules", "target", "dist", "__pycache__", ".venv", ".m2repo", ".pnpm-store"}


class ContractError(ValueError):
    pass


def require(value, message):
    if not value:
        raise ContractError(message)


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"不能读取 JSON {path}: {exc}") from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def lock(path, timeout=30):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, f"资源仍被占用: {path}；等待超时，没有执行命令")
                time.sleep(0.05)
        try:
            yield stream
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def event(ws, kind, **fields):
    path = Path(ws) / "waves/events.jsonl"
    with lock(str(path) + ".lock"):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": now(), "kind": kind, **fields}, ensure_ascii=False) + "\n")


def inside(root, relative):
    require(isinstance(relative, str) and relative.strip(), "路径不能为空")
    path = (root / relative).resolve()
    require(path == root or root in path.parents, f"路径越出项目根: {relative}")
    return path


def config(ws):
    ws = Path(ws).resolve()
    data = read(ws / "workflow.json")
    require(data.get("schema_version") == 2 and data.get("release") == RELEASE,
            "需要本版本的 workflow.json（schema_version=2, release=2026-09-22），不自动升级已有状态")
    root = (ws / data.get("project_root", "..")).resolve()
    # project_root is intentionally allowed to be a parent of doc/.
    require(root.is_dir() and (ws == root or root in ws.parents), "project_root 必须包含工作区")
    for key in ("source_paths", "harness_paths"):
        require(isinstance(data.get(key), list) and data[key], f"workflow.json 缺 {key}")
        for item in data[key]:
            p = inside(root, item)
            require(p != root, f"{key} 不能是整个项目根；避免把 QA 产物纳入被测源码")
            require(not (ws / "waves" == p or ws / "waves" in p.parents), "waves/ 不能作为源码或执行器输入")
    require(isinstance(data.get("preflight"), dict), "缺 preflight 配置")
    require(data.get("runtime", {}).get("capacity", 1) == 1,
            "本版本默认仅支持容量 1 的共享验证环境；完整多槽隔离需独立验证后再启用")
    return data, root


def tree_hash(root, paths, ignores=()):
    rows = []
    for item in sorted(set(paths)):
        path = inside(root, item)
        if not path.exists():
            rows.append((item, "missing"))
            continue
        candidates = []
        if path.is_file():
            candidates.append(path)
        else:
            for directory, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in IGNORED_PARTS]
                candidates.extend(Path(directory) / name for name in files)
        for f in candidates:
            relative = str(f.relative_to(root))
            if any(part in IGNORED_PARTS for part in f.parts) or any(fnmatch.fnmatch(relative, p) for p in ignores):
                continue
            require(not f.is_symlink(), f"指纹输入不接受符号链接: {f}")
            if f.is_file():
                rows.append((relative, file_hash(f)))
    return digest(sorted(set(rows)))


def hashes(ws):
    cfg, root = config(ws)
    ws = Path(ws).resolve()
    spec = [str(p.relative_to(root)) for p in (ws / "tickets", ws / "authority", ws / "_adr",
            ws / "requirements.yaml", ws / "phase-plan.yaml", ws / "api-contract.md")]
    return {"config_hash": digest(cfg), "source_hash": tree_hash(root, cfg["source_paths"], cfg.get("ignore", [])),
            "harness_hash": tree_hash(root, cfg["harness_paths"], cfg.get("ignore", [])),
            "spec_hash": tree_hash(root, spec)}


def asset(path):
    path = Path(path).resolve()
    require(path.is_file(), f"证据文件不存在: {path}")
    return {"path": str(path), "sha256": file_hash(path)}


def validate_assets(items):
    require(isinstance(items, list) and items, "缺少实际证据文件")
    for item in items:
        require(isinstance(item, dict) and item.get("path") and item.get("sha256"), "证据需含 path/sha256")
        require(asset(item["path"]) == item, f"证据已改变: {item.get('path')}")


def identity(ws, environment, manifest):
    current = hashes(ws)
    deployment = read(manifest)
    require(deployment.get("environment") == environment, "产物环境不匹配")
    require(deployment.get("source_hash") == current["source_hash"], "运行产物绑定的源码已过期，重新构建/探测并 stamp")
    require(deployment.get("probe_exit") == 0, "产物缺少成功的实际版本探测")
    validate_assets(deployment.get("artifacts"))
    validate_assets(deployment.get("probe_evidence"))
    return {**current, "environment": environment, "artifact_manifest": asset(manifest)}


def validate_binding(ws, binding, expected):
    require(isinstance(binding, dict) and binding == expected, "报告不是当前 run 的证据（阶段/轮次/版本/环境不匹配）")
    current = identity(ws, binding["environment"], binding["artifact_manifest"]["path"])
    for key, value in current.items():
        require(binding.get(key) == value, f"被测输入发生变化: {key}")


def ticket_fm(path):
    try:
        import yaml
    except ImportError as exc:
        raise ContractError("需要 PyYAML；请使用 D0 固定的 Python，不使用简化 YAML 解析器") from exc
    text = Path(path).read_text(encoding="utf-8")
    require(text.startswith("---\n"), f"缺 frontmatter: {path}")
    try:
        fm = yaml.safe_load(text.split("---", 2)[1])
    except Exception as exc:
        raise ContractError(f"frontmatter 无效: {path}: {exc}") from exc
    require(isinstance(fm, dict), f"frontmatter 不是 mapping: {path}")
    return fm


def ui_required(fm):
    directories = {"pages", "views", "components", "router", "routes"}
    parts = [part for path in fm.get("touches", [])
             for part in str(path).replace("\\", "/").lower().split("/")]
    anchors = fm.get("blueprint_refs", [])
    inferred = bool(directories.intersection(parts)) or any(str(x).startswith("UI:") for x in anchors)
    return inferred or (fm.get("ui") or {}).get("required") is True


def ticket_errors(fm):
    errors = []
    if ui_required(fm):
        ui = fm.get("ui") or {}
        if ui.get("required") is not True:
            errors.append("UI 路径/权威锚命中：必须声明 ui.required: true")
        accepts = [a for a in fm.get("accept", []) if a.get("form") == "E2E"]
        if not accepts:
            errors.append("UI ticket 缺 E2E accept")
        for a in accepts:
            for key in ("entry", "action", "outcome", "result", "counterfeit"):
                if not isinstance(a.get(key), str) or not a[key].strip():
                    errors.append(f"E2E accept 缺 {key}")
    for a in fm.get("accept", []):
        if not a.get("counterfeit"):
            errors.append("每条 accept 必须声明 counterfeit；存在性检查不等于断言强度保证")
    return errors


def preflight_valid(ws, stage):
    cfg, _ = config(ws)
    report = read(Path(ws) / f"waves/preflight-{stage}.json")
    require(report.get("status") == "pass" and report.get("stage") == stage, f"D0 {stage} 未通过")
    current = hashes(ws)
    for key in ("config_hash", "harness_hash"):
        require(report.get(key) == current[key], f"D0 {stage} 已过期: {key}")
    require(report.get("python") == str(Path(sys.executable).resolve()), "D0 与执行器 Python 不一致")
    declared = cfg["preflight"].get(stage, [])
    require(declared and {c["id"] for c in declared} == {c["id"] for c in report.get("checks", [])}, "D0 检查集不完整")
    for item in report["checks"]:
        require(item.get("status") == "pass", "D0 检查未通过")
        validate_assets(item.get("evidence"))


def command(ws, argv, label, timeout=300, shell=False):
    """Keep verification capacity locked for the entire subprocess tree lifetime."""
    cfg, root = config(ws)
    run_id = uuid.uuid4().hex
    out = Path(ws) / "waves/evidence" / run_id
    out.mkdir(parents=True)
    logfile = out / "output.log"
    started = time.monotonic()
    event(ws, "command_wait", run_id=run_id, label=label)
    with lock(Path(ws) / "waves/locks/verification.lock", timeout=30) as lease:
        waited = time.monotonic() - started
        event(ws, "command_start", run_id=run_id, label=label, wait_seconds=round(waited, 3))
        with logfile.open("w") as stream:
            process = subprocess.Popen(["/bin/bash", "-euo", "pipefail", "-c", argv] if shell else argv,
                                       cwd=root, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=(lease.fileno(),))
            previous = signal.getsignal(signal.SIGTERM)
            def terminated(signum, frame):
                raise KeyboardInterrupt("verification runner terminated")
            signal.signal(signal.SIGTERM, terminated)
            try:
                rc = process.wait(timeout=timeout)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
            finally:
                signal.signal(signal.SIGTERM, previous)
                # Detached descendants belong to this invocation, never to other runners.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        event(ws, "command_end", run_id=run_id, label=label, exit_code=rc,
              duration_seconds=round(time.monotonic() - started - waited, 3))
    return rc, asset(logfile)
