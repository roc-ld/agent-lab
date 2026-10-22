#!/usr/bin/env python3
"""task_state.py — 任务档执行状态机（②zhixing 的唯一运行时脚本）。

**主会话是调度器**：主会话读本脚本的输出决定下一步派谁，重活全在一次性 subagent
里。所以主会话上下文自始至终很轻，一个会话能从 D1 跑到 Dn 不炸——不靠 compact
（compact 有损，只是极端兜底），靠"重活不进主会话"。

粒度（与 xuqiu 一致，别混）：
    需求点 REQ  → ticket（功能点）→ **任务 daily**（多个 ticket）→ 项目
    ticket 完工 = 跑自己的 accept（2-3 条轻量断言）
    **任务完工 = 干净 QA subagent 按级分片跑完整 L0-L3 + 返工循环**  ← 完整测试只在这里

═══════════════════════════════════════════════════════════════════════════
为什么是脚本 + 主会话，而不是 workflow 调度器
═══════════════════════════════════════════════════════════════════════════
任务档是**串行**的（任务之间硬前置，任务内 ticket 按 touches 决定能否并行）。
串行流程不需要波次调度器、不需要就绪集计算、不需要轨隔离——那套是多轨并行档的
成本，任务档一分钱都不该付。本脚本只做三件主会话自己算容易错的事：

    next    算下一步该干什么（哪个任务、哪些 ticket 可并行、是否该进 QA 门）
    set     记录 ticket/任务状态（**加文件锁 + 原子写**，并发安全、跑崩可续）
    report  人看的进度表

命令：
    python3 task_state.py [--state <路径>] init  --tickets doc/tickets
    python3 task_state.py [--state <路径>] sync  --tickets doc/tickets   # 增量并入新 ticket
    python3 task_state.py [--state <路径>] next
    python3 task_state.py [--state <路径>] set --ticket T-1 --status done [--note "..."]
    python3 task_state.py [--state <路径>] set --phase D1 --status qa_passed
    python3 task_state.py [--state <路径>] report

⚠️ `--state` 是**全局参数，必须写在子命令之前**（argparse 顶层 parser）。
   写成 `init --state x` 会报 invalid choice。

退出码：0 正常 / 1 参数或状态错 / 2 无事可做（全部完成）/ 3 需要人介入（exception）
纯 stdlib。front-matter 解析与 dag_lint 同规则（优先 pyyaml，缺席走扁平 fallback）。
"""

from __future__ import annotations   # 兼容 Python 3.9（PEP 604 注解）

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import sys
import tempfile

# ticket 状态。
#   stale     蓝图翻案后被 blueprint_refs 反查命中，需重做（会把所在任务回退到 pending）
#   escalated 连撞 2 次转人后挂起——**不阻塞本任务其余 ticket**，但会在 QA 门显式标注
#   （2026-08-16 删掉了 running：全套从无 `set --status running` 的指令，subagent 在
#     主会话内同步返回，没有僵尸概念——它是死代码，留着只会让 ticket 卡在一个
#     无人能置回的状态。老 state 里若有残留 running，会落进 blocked detail 显式暴露。）
TICKET_STATUSES = ("pending", "done", "failed", "blocked", "stale", "escalated")
# accepted = qa_passed 之后的人工抽验完成态。抽验是**异步旁路**：qa_passed 即可继续
# 下一任务（否则「一个会话从 D1 跑到 Dn」破产），负责人 批式抽验完逐个标 accepted，
# all_done 收尾时列出所有未 accepted 的任务一次性清（此前抽验零状态表示，跳没跳过查不出）。
PHASE_STATUSES = ("pending", "impl_done", "qa_running", "qa_passed", "qa_failed", "accepted")

MAX_FIX_ATTEMPTS = 2        # ticket 自修上限，超限自动转 escalated
MAX_QA_ROUNDS = 2           # 单次武装内的连红上限，超限报 exception 叫人

# ★ 一个任务累计能跑的 QA 轮数**硬顶** —— rearm 也突破不了。
# 由来（2026-08-19 T006 D1 的实测账）：MAX_QA_ROUNDS 只管「连红」，rearm 把它归零
# 且不设累计上限，于是 D1 一路跑到**第 7 轮**才收。7 轮共报 75 条问题，其中 S0/S1
# 只有 3 条；第 2、4、6、7 四轮**一条 S0/S1 都没有**，找到的全长在检查工具自己身上。
# 轮数不封顶时，「再验一轮」永远显得比「就此收手」更负责，而它是纯亏的。
# ★ 硬顶按**磁盘上的 audit 文件**数（见 _rounds_on_disk），不看 qa_rounds ——
#   那个数只在有人跑 set 时才 +1，D1 磁盘七轮而 state 记 4 轮、rearms 0 次。
# 撞到硬顶只有两条合法出口：human_override（记名认下残留往前走）或回 ①xuqiu 改口径。
MAX_QA_SEQ = 4

PHASE_RE = re.compile(r"^[A-Za-z]*\d+$")   # 规范任务号：D1 / D10 / 2；D1.5 会告警
QA_LEVELS = ("L0", "L1", "L2", "L3")       # 一轮 QA 必须覆盖满的四级


# ═══════════════════════════════════════════════════════════════════════════
# 工作区根定位 —— 与 xuqiu/scripts/{authority,coverage}_lint.py 的同名函数保持同步
# ═══════════════════════════════════════════════════════════════════════════
# 支持两种真实布局：<root>/doc/{tickets/,waves/,phase-plan.yaml}（标准）
#             与 <root>/{tickets/,waves/,phase-plan.yaml}（拍平，doc-phaseN 多阶段项目）
# 在此之前默认写死 doc/waves/state.json，在拍平布局下会在错误的层级新建 state.json，
# 或把 49 个 ticket 算成 0 个。
WS_ENV = "ZHIXING_WORKDIR"
WS_MARKERS_DIR = ("authority", "tickets")
WS_MARKERS_FILE = ("requirements.yaml", "phase-plan.yaml")


def _is_ws_root(d):
    if not os.path.isdir(d):
        return False
    return (any(os.path.isdir(os.path.join(d, m)) for m in WS_MARKERS_DIR)
            or any(os.path.isfile(os.path.join(d, m)) for m in WS_MARKERS_FILE))


def resolve_ws(explicit):
    """→ 相对 cwd 的工作区根前缀（"." 表示 cwd 本身）。"""
    if explicit is not None:
        return explicit
    env = os.environ.get(WS_ENV)
    if env:
        sys.stderr.write(f"[ws] 工作区根 = {env}（来自 ${WS_ENV}）\n")
        return env
    for cand, label in (("doc", "doc/ 布局"), (".", "拍平布局")):
        if _is_ws_root(cand):
            if cand != "doc":        # doc/ 是老默认，不制造噪音
                sys.stderr.write(f"[ws] 自动探测到工作区根 = {cand}（{label}）；"
                                 f"不对就传 --ws 或设 ${WS_ENV}\n")
            return cand
    return os.path.join("doc")

# 长在**检查工具**上而不是**产品**上的发现（断言写错、闸门有洞、脚手架不严）。
# 这类一律封顶 S2：它们真实存在，但拦不住老师用、也拖不垮下游 ticket，
# 记进台账到 all_done 一起清即可，**不许拿它拉一整轮返工**。
# 由来：一轮验收找到的东西全长在检查工具上，就是该停的信号——继续挖只会挖出
# 更深一层的自指（判据 → 自检 → 自检的测试 → 换台机器还灵不灵，一层比一层空）。
HARNESS_TYPE = "harness"
HARNESS_SEVERITY_CAP = "S2"


def _qa_seq(ph):
    """audit 文件名用的**单调**轮次 —— qa_rounds 会被 rearm 清零，它不会。

    老 state 没有 qa_seq 时回落到 qa_rounds（rearm 出现之前两者恒等，回落即等价）。
    必须分成两个数：rearm 把 qa_rounds 归零是为了重新武装闸门，但文件名若跟着回到
    r1，下一轮 QA 会**覆盖第 1 轮的 audit** —— 那是唯一一份原始证据，抹掉就再也
    对不出「两轮红的是不是同一类东西」。
    """
    return int(ph.get("qa_seq", ph.get("qa_rounds", 0)))


def _qa_dir(state_path):
    """audit JSON 的固定落点：与 state.json 同级的 qa/。"""
    return os.path.join(os.path.dirname(state_path) or ".", "qa")


_AUDIT_RE = re.compile(r"^(?P<phase>.+)-r(?P<round>\d+)(?:-[^-.]+)?\.json$")


def _rounds_on_disk(state_path, phase_id):
    """这个任务**实际落过盘**的 QA 轮数 —— 从 qa/ 目录数，不问 state 记了几轮。

    ★ 为什么必须从磁盘数（2026-08 T006 的实测教训，也是这套闸门最大的一个洞）：
    `qa_rounds` 只在有人跑 `set --phase <D> --status qa_failed` 时才 +1。主会话
    自己派了一轮 QA、看完报告直接派返工、没跑 set —— 这一轮对计数器就**不存在**。
    T006 的 D1 磁盘上躺着 r1..r7 七轮 audit，而 state 里 `qa_rounds=4, rearms=0`：
    `MAX_QA_ROUNDS` 一次都没被触发过，闸门形同虚设。

    「堵住所有跳过上报的路径」是在开放集合上证否定命题，堵不完。所以改成对
    **已经发生过的全体**求证：一轮 QA 必然落 audit 文件（那是 qa_passed 的证据门
    要求的），就数它。
    """
    d = _qa_dir(state_path)
    if not os.path.isdir(d):
        return 0
    seen = set()
    for name in os.listdir(d):
        m = _AUDIT_RE.match(name)
        if m and m.group("phase") == phase_id:
            seen.add(int(m.group("round")))
    return len(seen)


# ═══════════════════════════════════════════════════════════════════════════
# ticket front-matter（与 dag_lint 同规则；只取本脚本需要的字段）
# ═══════════════════════════════════════════════════════════════════════════
def _read_fm(md_path):
    """→ (dict|None, err|None)。**任何解析异常都返回 err，绝不冒泡**
    ——一份坏文件不该让整个 init/next 崩成 traceback、其余上百份一条不报。"""
    try:
        with open(md_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return None, f"读文件失败: {e}"
    if not lines or lines[0].strip() != "---":
        return None, "缺 front-matter（文件未以 --- 开头）"
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, "front-matter 未闭合（缺第二个 ---）"
    block = "\n".join(lines[1:end])
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(block)
        if not isinstance(data, dict):
            return None, "front-matter 不是 mapping"
        return data, None
    except ImportError:
        return _fm_fallback(block), None
    except Exception as e:
        return None, f"YAML 解析失败: {e}"


def _fm_fallback(block):
    """pyyaml 缺席兜底：顶层标量 + 块式 list + 行内 list。"""
    out, key, lst = {}, None, None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] not in " \t":
            k, _, v = raw.partition(":")
            key, v, lst = k.strip(), v.strip(), None
            if v.startswith("[") and v.endswith("]"):
                out[key] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
            elif v:
                out[key] = v.strip("\"'")
            else:
                out[key] = []
                lst = out[key]
        elif lst is not None:
            m = re.match(r"\s*-\s*(.+)", raw)
            if m:
                lst.append(m.group(1).strip().strip("\"'"))
    return out


def load_tickets(tickets_dir):
    """扫 <dir>/**/prompt.md → ({id: {...}}, errors)。

    递归扫：ticket 可能按系统/域分组（doc/tickets/store/<id>/prompt.md）。
    重复 id 记 error（两处 load_tickets 曾经一个首个胜、一个末个胜，
    导致 lint 看到的和调度器派的是**两个不同文件**）。
    """
    out, errors = {}, []
    for md in sorted(glob.glob(os.path.join(tickets_dir, "**", "prompt.md"),
                               recursive=True)):
        fm, err = _read_fm(md)
        dir_id = os.path.basename(os.path.dirname(md))
        if err:
            errors.append({"ticket": dir_id, "msg": f"{err}（{md}）"})
            continue
        tid = str((fm or {}).get("ticket") or dir_id)
        if tid in out:
            errors.append({"ticket": tid,
                           "msg": f"ticket id 重复：{out[tid]['path']} 与 {md}"})
            continue
        deps = fm.get("depends_on") or []
        out[tid] = {
            "phase": str(fm.get("phase") or "").strip(),
            "track": str(fm.get("track") or "").strip(),
            "system": str(fm.get("system") or "").strip(),   # 多系统项目用，可空
            "depends_on": [str(d).strip() for d in deps] if isinstance(deps, list) else [],
            "touches": fm.get("touches") or [],
            "path": md,
        }
    return out, errors


# ═══════════════════════════════════════════════════════════════════════════
# state.json 读写（文件锁 + 原子替换）
# ═══════════════════════════════════════════════════════════════════════════
class StateLock:
    """排他文件锁，包住整个 load-modify-save。

    原子 rename 只保证"不写半个文件"，**挡不住 lost update**：两个并发 set
    各自读到旧 state、各改各的、后写的覆盖先写的。而 SKILL 正鼓励同批并发派
    subagent，主会话把多个 bash 调用放同一条消息里就是并发执行——实测 6 个并发
    set 只落 3-4 个。丢 `done` → ticket 被重复实现；丢 `failed` → fix_attempts
    少计，2 次上限形同虚设。
    """

    def __init__(self, state_path):
        self.lock_path = state_path + ".lock"
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        self.fh = open(self.lock_path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False


def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(path, state):
    """先写临时文件再 rename —— 中途被杀不会留下半截 JSON。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def phase_key(p):
    """任务号排序键：D2 排在 D10 前（字典序会把 D10 排到 D2 前，是真会踩的坑）。

    非规范值（D1.5 / 中文）排序不可靠，由 init/sync 显式告警，这里保证不崩。
    """
    m = re.match(r"^([A-Za-z]*)(\d+)$", p or "")
    return (0, m.group(1), int(m.group(2))) if m else (1, p or "", 0)


# ═══════════════════════════════════════════════════════════════════════════
# 权威基线（authority_lint snapshot 的摘要，蓝图翻案守门用）
# ═══════════════════════════════════════════════════════════════════════════
def _snapshot_path(tickets_dir):
    """约定：<tickets_dir>/../authority/_snapshot.json（与 lint-profile 同惯例）。"""
    return os.path.join(os.path.dirname(os.path.abspath(tickets_dir)),
                        "authority", "_snapshot.json")


def _authority_baseline(snap_path):
    """→ (sha256hex, err)。**只对 hashes 算，不对整文件**——snapshot 里还有 titles
    映射，而 record_hash 刻意排除 title（改标题错别字不算蓝图变更）；对整文件算
    会让无害改动全线误报 blueprint_changed。"""
    try:
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
    except OSError as e:
        return None, f"读快照失败: {e}"
    except json.JSONDecodeError as e:
        return None, f"快照不是合法 JSON: {e}"
    hashes = snap.get("hashes")
    if not isinstance(hashes, dict):
        return None, "快照缺 hashes 字段（authority_lint 版本过旧？重跑 snapshot）"
    blob = json.dumps(sorted(hashes.items()), ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest(), None


def _phase_plan_path(tickets_dir):
    """约定：<tickets_dir>/../phase-plan.yaml（与 lint-profile / _snapshot 同惯例）。"""
    return os.path.join(os.path.dirname(os.path.abspath(tickets_dir)), "phase-plan.yaml")


def load_phase_plan(tickets_dir):
    """→ (plan|None, err|None)。plan = {order/goal/deps/qa_scope: {id:…}, rehearsal: […]}。

    为什么要它（2026-08-17，对抗审计批 1）：此前 phase-plan.md 是「人看的表格 +
    零机器消费方」，三条洞全指向它——
      ① 返工任务名（D9X / D-FIX-N）不匹配名字正则，排序退化（D-FIX-10 排在 D-FIX-2 前）；
      ② C8 跨任务依赖检查对这些名字**静默跳过**；
      ③ QA 范围不进 qa_gate payload，两轮 QA 各自即兴划范围，「连红 2 轮」信号不可比。
    yaml 显式 order 一次解决三条。**没有 yaml 的老项目退回名字排序（warn），不硬卡。**
    ticket→任务 的归属不在这里——权威在 ticket front-matter 的 `phase` 字段（C7 必填），
    这里只登记任务本身（顺序 / QA 范围 / 演练剧本），避免两处真相。
    """
    path = _phase_plan_path(tickets_dir)
    if not os.path.exists(path):
        return None, None
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return None, "phase-plan.yaml 存在但无 pyyaml——pip install pyyaml（不做手解析兜底）"
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return None, f"phase-plan.yaml 解析失败: {e}"
    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        return None, "phase-plan.yaml 缺 phases 列表"
    order, qa, goal, deps = {}, {}, {}, {}
    for i, p in enumerate(phases):
        if not isinstance(p, dict) or not p.get("id"):
            return None, f"phases[{i}] 缺 id"
        pid = str(p["id"]).strip()
        if pid in order:
            return None, f"phase id 重复: {pid}"
        try:
            order[pid] = int(p.get("order", (i + 1) * 10))
        except (TypeError, ValueError):
            return None, f"phase {pid} 的 order 不是整数"
        qs = p.get("qa_scope") or {}
        if qs and not isinstance(qs, dict):
            return None, f"phase {pid} 的 qa_scope 必须是 mapping（键 L0-L3）"
        qa[pid] = qs
        goal[pid] = str(p.get("goal") or "")
        deps[pid] = [str(d).strip() for d in (p.get("depends_on_phases") or [])]
    for pid, ds in deps.items():
        for d in ds:
            if d not in order:
                return None, f"phase {pid} 的 depends_on_phases 指向不存在的 {d}"
            if order[d] >= order[pid]:
                return None, (f"phase {pid}(order {order[pid]}) 依赖 {d}(order {order[d]})"
                              f"——前置任务的 order 必须更小")
    rehearsal = ((data.get("final_rehearsal") or {}).get("scenarios")) or []
    return {"order": order, "qa_scope": qa, "goal": goal, "deps": deps,
            "rehearsal": [str(s) for s in rehearsal]}, None


def _resolve_phases(meta, tickets_dir):
    """磁盘 phase 集 × phase-plan.yaml → (有序 phase 列表, plan|None, err|None)。
    init 与 sync **共调**——只升级 init 的话，补拆 ticket 走 sync 进来，同一事故重演。"""
    phases = _build_records(meta)
    plan, perr = load_phase_plan(tickets_dir)
    if perr:
        return None, None, perr
    if plan:
        unknown = [p for p in phases if p not in plan["order"]]
        if unknown:
            return None, None, (f"这些任务不在 phase-plan.yaml 里: {', '.join(unknown)}。"
                                f"返工任务（D9X/D-FIX-N）也要登记 id + order 才能定序")
        phases.sort(key=lambda p: plan["order"][p])
    else:
        _warn_phases(phases)
        sys.stderr.write("[warn] 无 doc/phase-plan.yaml——任务顺序退回名字排序。"
                         "返工任务名（D9X/D-FIX-N）这类排序不可靠，建议补 yaml 显式定序\n")
    return phases, plan, None


def _issue_summary(state, severities=None):
    """开放 issue 按严重度分组摘要（qa_gate / all_done payload 用）。"""
    out = {}
    for it in state.get("open_issues", []):
        if it.get("status") != "open":
            continue
        sv = it.get("severity", "S2")
        if severities and sv not in severities:
            continue
        out.setdefault(sv, []).append(
            f"#{it['id']} [{it.get('ticket') or '-'}] {it.get('title', '')}")
    return out


def _auto_issue(state, ticket, title, detail=""):
    """escalated 与 open_issues 并轨：转 escalated 时自动记一条 S1（同一把锁一次写盘）。"""
    issues = state.setdefault("open_issues", [])
    if any(it.get("ticket") == ticket and it.get("type") == "escalated"
           and it.get("status") == "open" for it in issues):
        return None
    nid = max((int(it.get("id", 0)) for it in issues), default=0) + 1
    issues.append({"id": nid, "ticket": ticket, "severity": "S1", "type": "escalated",
                   "title": title, "detail": detail, "status": "open", "decision": ""})
    return nid


def _load_meta(state, quiet=False):
    """载入 ticket 元信息，并校验与 state 的一致性。

    `tickets_dir` 在 init 时存成**绝对路径**——本项目的 agent 线程每次 bash 调用
    cwd 会重置，相对路径会导致 load_tickets 扫不到任何文件，于是所有 depends_on
    变空（全部就绪）、touches 变空（互不冲突）、prompt 变 null，而且**一声不吭**。
    """
    tdir = state.get("tickets_dir", "")
    meta, errors = load_tickets(tdir)
    known = set(state["tickets"])
    if not meta:
        sys.stderr.write(
            f"[error] 从 {tdir} 扫不到任何 ticket。\n"
            f"        state.json 记录的是这个目录；若目录被移动/改名，请重跑 "
            f"`sync --tickets <新路径>`。\n"
            f"        （扫不到时依赖边和 touches 会全部失效，本脚本拒绝在这种状态下派单）\n")
        return None, errors
    missing = known - set(meta)
    if missing and not quiet:
        sys.stderr.write(f"[error] state 里有 {len(missing)} 个 ticket 在磁盘上找不到: "
                         f"{', '.join(sorted(missing)[:5])}\n"
                         f"        ticket 被删/改名了？跑 `sync` 对齐。\n")
        return None, errors
    return meta, errors


# ═══════════════════════════════════════════════════════════════════════════
# 命令
# ═══════════════════════════════════════════════════════════════════════════
def _build_records(meta):
    phases = sorted({v["phase"] for v in meta.values()}, key=phase_key)
    return phases


def _warn_phases(phases):
    bad = [p for p in phases if not PHASE_RE.match(p)]
    if bad:
        sys.stderr.write(
            f"[warn] {len(bad)} 个任务号不规范: {', '.join(bad)}\n"
            f"       规范形态 D1/D2/D10（字母+数字）。D1.5、中文名的执行顺序不可靠——\n"
            f"       实测 ['D1','D2','D10','D1.5'] 会排成 D1→D2→D10→D1.5（插的跑最后）。\n")


def cmd_init(a):
    with StateLock(a.state):
        if os.path.exists(a.state) and not a.force:
            sys.stderr.write(
                f"[error] {a.state} 已存在。init 会**重置全部进度**（含 open_issues 台账）。\n"
                f"        只想并入新增 ticket → 用 `sync`；确定要重来 → 加 --force。\n")
            return 1
        meta, errors = load_tickets(a.tickets)
        if errors:
            sys.stderr.write(f"[error] {len(errors)} 个 ticket 解析失败：\n")
            for e in errors:
                sys.stderr.write(f"  [{e['ticket']}] {e['msg']}\n")
            return 1
        if not meta:
            sys.stderr.write(
                f"[error] {a.tickets} 下没找到 ticket（期望 <目录>/**/prompt.md）。\n"
                f"        正文文件必须固定叫 prompt.md。\n")
            return 1
        no_phase = [t for t, v in meta.items() if not v["phase"]]
        if no_phase:
            sys.stderr.write(
                f"[error] {len(no_phase)} 个 ticket 没有 phase，会被静默跳过: "
                f"{', '.join(sorted(no_phase)[:5])}{' …' if len(no_phase) > 5 else ''}\n"
                f"        先跑 dag_lint.py 修完再 init\n")
            return 1

        # 权威基线：没有 snapshot 就拒绝开工。此刻不打基线，之前的蓝图变更永久丢失
        # ——impact 反查无从比较，"蓝图翻案 → 置 stale" 整条链形同虚设。
        snap_path = _snapshot_path(a.tickets)
        if not os.path.exists(snap_path):
            sys.stderr.write(
                f"[error] 缺权威基线快照: {snap_path}\n"
                f"        没有基线，蓝图翻案时 impact 反查不出受影响 ticket，变更会被无声吞掉。\n"
                f"        补救（一条命令，打完重跑 init）：\n"
                f"        python3 <xuqiu>/scripts/authority_lint.py snapshot "
                f"--authority {os.path.dirname(snap_path)} --tickets {a.tickets}\n")
            return 1
        baseline, berr = _authority_baseline(snap_path)
        if berr:
            sys.stderr.write(f"[error] 权威快照不可用（{berr}）: {snap_path}\n"
                             f"        重跑 authority_lint.py snapshot 后再 init\n")
            return 1

        phases, plan, perr = _resolve_phases(meta, a.tickets)
        if perr:
            sys.stderr.write(f"[error] {perr}\n")
            return 1
        state = {
            "plan_mode": "task",
            # 绝对路径：agent 线程 cwd 会重置，相对路径会静默失效
            "tickets_dir": os.path.abspath(a.tickets),
            "authority_baseline": baseline,
            "authority_snapshot_path": snap_path,
            "phases": [{"id": p, "status": "pending", "qa_rounds": 0} for p in phases],
            "tickets": {t: {"status": "pending", "phase": v["phase"], "track": v["track"],
                            "system": v["system"], "fix_attempts": 0, "note": ""}
                        for t, v in meta.items()},
            "open_issues": [],
        }
        if plan:
            state["phase_plan"] = {"qa_scope": plan["qa_scope"], "goal": plan["goal"],
                                   "rehearsal": plan["rehearsal"]}
        save_state(a.state, state)
    print(f"[ok] init: {len(meta)} ticket / {len(phases)} 个任务 → {a.state}")
    print(f"     任务顺序: {' → '.join(phases)}"
          + ("（phase-plan.yaml 显式定序）" if plan else "（名字排序）"))
    print(f"     权威基线: {baseline[:8]}（{snap_path}）")
    return 0


def cmd_sync(a):
    """增量并入新 ticket / 移除已删 ticket，**保留已有进度**。

    没有这个命令时：`coverage_lint` 报漏拆 → 按流程"补 ticket" → 新 ticket 不在
    state 里 → 永远不被派单，且无任何检测。重跑 init 又会清空全部进度。
    """
    with StateLock(a.state):
        state = load_state(a.state)
        if state is None:
            sys.stderr.write(f"[error] 状态文件不存在: {a.state}（先跑 init）\n")
            return 1
        tdir = a.tickets or state.get("tickets_dir")
        meta, errors = load_tickets(tdir)
        if errors:
            sys.stderr.write(f"[error] {len(errors)} 个 ticket 解析失败：\n")
            for e in errors:
                sys.stderr.write(f"  [{e['ticket']}] {e['msg']}\n")
            return 1
        if not meta:
            sys.stderr.write(f"[error] {tdir} 下没找到 ticket\n")
            return 1
        no_phase = [t for t, v in meta.items() if not v["phase"]]
        if no_phase:
            sys.stderr.write(f"[error] {len(no_phase)} 个 ticket 没有 phase: "
                             f"{', '.join(sorted(no_phase)[:5])}\n")
            return 1

        added = [t for t in meta if t not in state["tickets"]]
        removed = [t for t in state["tickets"] if t not in meta]
        rephased = [t for t, v in meta.items()
                    if t in state["tickets"] and state["tickets"][t]["phase"] != v["phase"]]

        for t in added:
            v = meta[t]
            state["tickets"][t] = {"status": "pending", "phase": v["phase"],
                                   "track": v["track"], "system": v["system"],
                                   "fix_attempts": 0, "note": ""}
        for t in removed:
            del state["tickets"][t]
        for t in rephased:
            state["tickets"][t]["phase"] = meta[t]["phase"]

        state["tickets_dir"] = os.path.abspath(tdir)
        phases, plan, perr = _resolve_phases(meta, tdir)
        if perr:
            sys.stderr.write(f"[error] {perr}\n")
            return 1
        if plan:
            state["phase_plan"] = {"qa_scope": plan["qa_scope"], "goal": plan["goal"],
                                   "rehearsal": plan["rehearsal"]}
        old = {p["id"]: p for p in state["phases"]}
        state["phases"] = [old.get(p, {"id": p, "status": "pending", "qa_rounds": 0})
                           for p in phases]
        # 新增/改归属的 ticket 若落进已通过/已抽验的任务 → 该任务必须回退重跑 QA 门
        reopened = []
        for ph in state["phases"]:
            if ph["status"] in ("qa_passed", "accepted", "risk_accepted"):
                undone = [t for t, v in state["tickets"].items()
                          if v["phase"] == ph["id"] and v["status"] != "done"]
                if undone:
                    ph["status"] = "pending"
                    reopened.append(ph["id"])

        # 权威基线 re-arm：sync 是**唯一**的重对出口（next 只读不写盘）。
        # 存量项目升级（state 无 baseline）和消化完蓝图变更后，都走一次 sync。
        snap_path = _snapshot_path(state["tickets_dir"])
        old_b = state.get("authority_baseline")
        new_b, berr = _authority_baseline(snap_path)
        if berr:
            sys.stderr.write(
                f"[warn] 权威基线未更新（{berr}）。先跑 authority_lint.py snapshot 再 sync，"
                f"否则 next 会报 baseline_missing。\n")
        else:
            state["authority_baseline"] = new_b
            state["authority_snapshot_path"] = snap_path
        save_state(a.state, state)

    print(f"[ok] sync: 新增 {len(added)} / 移除 {len(removed)} / 改任务归属 {len(rephased)}")
    if not berr and new_b != old_b:
        # 强制留痕：单跑 sync 可以消音 blueprint_changed（不强制先跑 impact），
        # 靠这一行让"基线被谁在什么时候换掉"事后可审计。
        print(f"     ⚠️ 权威基线已更新: {(old_b or '无')[:8]} → {new_b[:8]}"
              f"（若蓝图确有变更，确认受影响 ticket 已跑 authority_lint impact / set stale）")
    for t in added:
        print(f"     + {t} → {meta[t]['phase']}")
    for t in removed:
        print(f"     - {t}（已从磁盘删除）")
    if reopened:
        print(f"  ⚠️ 以下任务已通过 QA 门但又出现未完成 ticket，已回退待重跑 QA: "
              f"{', '.join(reopened)}")
    return 0


def _touches_conflict(a_globs, b_globs):
    """悲观判定两个 ticket 是否碰同一片文件。

    比 glob 的目录前缀（通配符之前的部分）。**前缀为空（glob 以通配符开头，
    如 `**/i18n/**`）视为与一切冲突**——历史版本在这里把空前缀过滤掉，导致
    两个 ticket 声明**同一份** `**/i18n/**` 被判"不冲突"：照文档正确声明共享文件
    （i18n 词表 / 路由表 / api barrel）恰好得到零保护，两个 subagent 并行改同一份
    文件互相覆盖，事后极难发现。

    宁可误判为冲突（多串行一个 ticket），也不要漏判。
    """
    def prefixes(gs):
        out, wildcard_root = set(), False
        for g in gs or []:
            s = str(g).strip()
            if not s:
                continue
            head = re.split(r"[*?\[]", s)[0]
            # 路径归一：./a/b → a/b；大小写统一（macOS 默认大小写不敏感文件系统）
            head = os.path.normpath(head).lstrip("./").rstrip("/").lower()
            if not head or head == ".":
                wildcard_root = True     # `**/x`、`*` 这类：无法定位到子树 → 视为碰全局
            else:
                out.add(head)
        return out, wildcard_root

    pa, wa = prefixes(a_globs)
    pb, wb = prefixes(b_globs)
    if wa or wb:
        return True                      # 任一侧碰全局 → 悲观判冲突
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/")
               for x in pa for y in pb)


def cmd_next(a):
    """算下一步 —— 主会话读这个 JSON 决定派谁。"""
    state = load_state(a.state)
    if state is None:
        sys.stderr.write(f"[error] 状态文件不存在: {a.state}（先跑 init）\n")
        return 1
    meta, errors = _load_meta(state)
    if meta is None:
        return 1
    if errors:
        sys.stderr.write(f"[warn] {len(errors)} 个 ticket 解析失败（不在 state 里，已跳过）\n")

    # ── 权威基线守门（只读；re-arm 唯一出口是 sync）─────────────────────────
    snap_path = state.get("authority_snapshot_path") or _snapshot_path(state["tickets_dir"])
    if "authority_baseline" not in state:
        print(json.dumps({
            "action": "baseline_missing", "snapshot": snap_path,
            "hint": "state 里没有权威基线（存量项目升级后的预期状态，不是事故）。自愈两步："
                    "① python3 <xuqiu>/scripts/authority_lint.py impact  消化可能积压的蓝图变更"
                    "（有受影响 ticket → 逐个 set --status stale）；"
                    "② python3 task_state.py sync  （末尾自动写入基线）。然后重跑 next，不叫人。",
        }, ensure_ascii=False, indent=2))
        return 3
    cur_baseline, berr = _authority_baseline(snap_path)
    if berr:
        print(json.dumps({
            "action": "baseline_missing", "snapshot": snap_path,
            "hint": f"权威快照读不了（{berr}）。自愈：authority_lint.py snapshot 重建 → "
                    f"task_state.py sync 重对基线 → 重跑 next，不叫人。",
        }, ensure_ascii=False, indent=2))
        return 3
    if cur_baseline != state["authority_baseline"]:
        print(json.dumps({
            "action": "blueprint_changed",
            "baseline": state["authority_baseline"][:8], "current": cur_baseline[:8],
            "hint": "权威快照与开工基线不一致——蓝图/需求在执行期被改了（触点 c，停机型）。"
                    "四步解除：① authority_lint.py diff / impact 看改了什么、波及哪些 ticket；"
                    "② 叫 负责人 确认权威版本；③ 受影响 ticket 逐个 set --status stale；"
                    "④ task_state.py sync 重对基线。然后重跑 next。",
        }, ensure_ascii=False, indent=2))
        return 3
    # 蓝图改了但没重打快照的窗口：hash 比对看不见（快照还是旧的），补一道 mtime 提醒
    try:
        snap_m = os.path.getmtime(snap_path)
        adir = os.path.dirname(snap_path)
        watch = glob.glob(os.path.join(adir, "*.yaml"))
        req = os.path.join(os.path.dirname(adir), "requirements.yaml")
        if os.path.exists(req):
            watch.append(req)
        newer = sorted(os.path.basename(y) for y in watch if os.path.getmtime(y) > snap_m)
        if newer:
            sys.stderr.write(
                f"[warn] {', '.join(newer)} 比权威快照新——蓝图改了没重打 snapshot？"
                f"跑 authority_lint.py diff/impact 消化后再 snapshot，否则变更检测不到。\n")
    except OSError:
        pass

    ts = state["tickets"]
    # 反向对账：磁盘有、state 没有 = 补拆的 ticket 忘了 sync——不拦的话它们永远
    # 不会被派单，且 next 会在跑完存量后堂而皇之报 all_done（静默少干活并报完工）。
    extra = sorted(set(meta) - set(ts))
    if extra:
        sys.stderr.write(f"[warn] 磁盘上有 {len(extra)} 个 ticket 不在 state 里（未 sync）: "
                         f"{', '.join(extra[:5])}{' …' if len(extra) > 5 else ''}\n")
    # 任务选取：**看 ticket 不只看 phase.status** —— 否则蓝图翻案后把 ticket 打回
    # pending，其所在任务已 qa_passed，该 ticket 永远不再被调度，next 直接报完成。
    def phase_done(ph):
        undone = [t for t, v in ts.items()
                  if v["phase"] == ph["id"] and v["status"] not in ("done", "escalated")]
        # accepted（人工抽验完成）≥ qa_passed；抽验是异步旁路，不挡下一任务开工
        return ph["status"] in ("qa_passed", "accepted", "risk_accepted") and not undone

    cur = next((p for p in state["phases"] if not phase_done(p)), None)
    if cur is None:
        if extra:
            print(json.dumps({
                "action": "out_of_sync",
                "extra_tickets": extra,
                "extra_phases": sorted({meta[t]["phase"] for t in extra}, key=phase_key),
                "hint": "存量任务全部过门，但磁盘上还有未入 state 的 ticket——**不能报 all_done**。"
                        "自愈：python3 task_state.py sync 并入后重跑 next，不叫人。"
                        "（历史事故形态：补拆的 ticket 忘 sync → 永不派单 + 完工报告撒谎）",
            }, ensure_ascii=False, indent=2))
            return 3
        # 收尾复核：全量 ticket 状态必须落定，别只信 phase 标记
        leftover = {t: v["status"] for t, v in ts.items()
                    if v["status"] not in ("done", "escalated")}
        if leftover:
            print(json.dumps({
                "action": "inconsistent",
                "hint": "所有任务标了 qa_passed，但仍有 ticket 未落定——state 被手工改过？",
                "tickets": leftover,
            }, ensure_ascii=False, indent=2))
            return 3
        esc = [t for t, v in ts.items() if v["status"] == "escalated"]
        plan = state.get("phase_plan") or {}
        payload = {
            "action": "all_done",
            "summary": "全部任务已通过 QA 门，项目实现完成",
            "escalated": esc,
            # 待抽验：qa_passed 但未 accepted 的任务（抽验异步旁路，收尾一次性清）
            "pending_acceptance": [p["id"] for p in state["phases"]
                                   if p["status"] != "accepted"],
            "pending_issues": _issue_summary(state),
            "hint": "收尾清单（按序，一次做完）："
                    "① 处理本 payload 带出的全部未决——escalated / 各级 issues（issue list "
                    "--undecided 看明细，负责人 逐条 issue decide）/ pending_acceptance 批式抽验"
                    "（验完 set --phase <D> --status accepted）；"
                    "② 合 integration → staging 并 push（首个交付前先跑 部署 skill 建管道）；"
                    "③ 跑 ops/status.sh <env> 当部署 smoke；"
                    "④ 最终验收演练：按 rehearsal 剧本派干净 QA subagent 跑全链路；"
                    "⑤ client 模式：把实耗一次性汇总回 /fangan 的 00-brief.md（估 vs 实）",
        }
        if plan.get("rehearsal"):
            payload["rehearsal"] = plan["rehearsal"]
        else:
            payload["rehearsal_missing"] = ("phase-plan.yaml 未配 final_rehearsal.scenarios——"
                                            "写不出全链路剧本 = 任务计划没想清楚，补上再交付")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    pid = cur["id"]
    in_phase = {t: v for t, v in ts.items() if v["phase"] == pid}

    # 0) 累计轮数撞硬顶 → exception。**以磁盘上的 audit 为准**，不看 qa_rounds。
    # 这一条独立于下面的「连红 2 轮」：主会话派了一轮 QA 却没跑 set，qa_rounds 就
    # 不会 +1，闸门永远不触发（T006 的 D1 磁盘上七轮、state 记 4 轮、rearms 0 次，
    # MAX_QA_ROUNDS 一次都没响过）。轮次不是自报的，是数出来的。
    _ran = _rounds_on_disk(a.state, pid)
    if cur["status"] not in ("qa_passed", "accepted", "risk_accepted") and _ran >= MAX_QA_SEQ:
        print(json.dumps({
            "action": "exception", "kind": "qa_seq_cap", "phase": pid,
            "rounds_ran": _ran, "qa_seq_cap": MAX_QA_SEQ,
            "qa_rounds_recorded": cur["qa_rounds"],
            "rearm_refused": True,
            "hint": f"任务 {pid} 磁盘上已有 {_ran} 轮 QA audit，撞到硬顶 "
                    f"MAX_QA_SEQ={MAX_QA_SEQ} —— 停手，rearm 也会被拒。"
                    f"跑到这个轮数还没收，问题不在实现里，再加轮是纯亏的"
                    f"（实测 D1 跑到第 7 轮，最后三轮一条 S0/S1 都没有，"
                    f"找到的全长在检查工具自己身上）。两条出口："
                    f"a) 人记名认下残留往前走 `set --phase {pid} --status qa_passed "
                    f"--override-by <人名> --note '<接受了哪些残留，≥30 字>'`（"
                    f"state 记 human_override，与 QA 通过永不混淆）；"
                    f"b) 回 ①xuqiu 改蓝图口径 / 重切任务。",
        }, ensure_ascii=False, indent=2))
        return 3

    # 1) QA 门连红超限 → exception，别无限返工
    if cur["status"] == "qa_failed" and cur["qa_rounds"] >= MAX_QA_ROUNDS:
        payload = {
            "action": "exception", "kind": "gate_red_2x", "phase": pid,
            "qa_rounds": cur["qa_rounds"], "qa_seq": _qa_seq(cur),
            "rearm_requires_human": True,
            "hint": f"任务 {pid} 的 QA 门已连红 {cur['qa_rounds']} 轮 —— 停手叫人。"
                    f"连红说明问题不在单个 ticket 的实现，而在蓝图/口径/任务划分。"
                    f"**人拍板后**才重新武装闸门（主会话不许自己调）："
                    f"task_state.py rearm --phase {pid} --reason '<负责人 的决策原话>'"
                    f" —— 它把 qa_rounds 归零、留痕、audit 文件名继续往前排不覆盖旧证据。",
        }
        ran = max(_qa_seq(cur), _rounds_on_disk(a.state, pid))
        payload["qa_seq_cap"] = MAX_QA_SEQ
        payload["rounds_ran"] = ran
        payload["rounds_left_before_cap"] = max(0, MAX_QA_SEQ - ran)
        if payload["rounds_left_before_cap"] == 0:
            payload["rearm_refused"] = True
            payload["hint"] = (
                f"任务 {pid} 累计已跑 {_qa_seq(cur)} 轮 QA，撞到硬顶 MAX_QA_SEQ={MAX_QA_SEQ}，"
                f"rearm 会被拒。跑到这个轮数还没收，问题不在实现里，再加轮是纯亏的。"
                f"两条出口：a) 记名认下残留往前走 "
                f"`set --phase {pid} --status qa_passed --override-by <人名> "
                f"--note '<接受了哪些残留，≥30 字>'`；b) 回 ①xuqiu 改蓝图口径 / 重切任务。")
        if cur.get("rearms"):
            payload["rearms"] = cur["rearms"]
            payload["hint"] += (f" ⚠️ 本任务已经重新武装过 {len(cur['rearms'])} 次——"
                                f"再撞就不是返工能解决的了，回 ①xuqiu 看口径。")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3

    # 2) QA 门跑挂（未超限）→ 先修，别推进
    if cur["status"] == "qa_failed":
        print(json.dumps({
            "action": "qa_rework", "phase": pid, "qa_rounds": cur["qa_rounds"],
            "remaining_rounds": MAX_QA_ROUNDS - cur["qa_rounds"],
            "next_round_label": f"r{_qa_seq(cur) + 1}",
            "hint": "QA 门未过：把 QA 报告的问题派回 impl subagent 修，修完**重跑整个 QA 门**"
                    "（不是只验修的那几条——回归才是重跑的意义）。处理完立即重跑 next，不要请示",
        }, ensure_ascii=False, indent=2))
        return 0

    # 3) 本任务还有 ticket 没完 → 派实现（escalated 的跳过，不阻塞全任务）
    undone = {t: v for t, v in in_phase.items()
              if v["status"] not in ("done", "escalated")}
    if undone:
        ready = []
        for t, v in undone.items():
            if v["status"] not in ("pending", "failed", "stale"):
                continue
            deps = meta.get(t, {}).get("depends_on", [])
            # 依赖被 escalated 视为未满足——不能建在没实现的东西上
            if all(ts.get(d, {}).get("status") == "done" for d in deps):
                ready.append(t)
        if not ready:
            blocked_detail = {}
            for t, v in undone.items():
                deps = meta.get(t, {}).get("depends_on", [])
                bad = [d for d in deps if ts.get(d, {}).get("status") != "done"]
                blocked_detail[t] = {
                    "status": v["status"],
                    "unmet_depends_on": {d: ts.get(d, {}).get("status", "不存在") for d in bad},
                }
            # 阻塞源**全部**是 escalated → 连坐收敛，不算停机：骨架 ticket 挂了不该让
            # 整个任务永远到不了 QA 门边界（qa_gate 会显式标注全部未实现项）
            esc_only = bool(blocked_detail) and all(
                bd["unmet_depends_on"]
                and all(s == "escalated" for s in bd["unmet_depends_on"].values())
                and bd["status"] in ("pending", "failed", "stale")
                for bd in blocked_detail.values())
            if esc_only:
                print(json.dumps({
                    "action": "phase_partial_done", "phase": pid, "detail": blocked_detail,
                    "hint": "这些 pending ticket 的依赖全部 escalated——自愈型：逐个 "
                            "set --ticket <T> --status escalated --note '依赖 <D> escalated 连坐'"
                            "（cmd_set 会自动记 S1 issue），set 完重跑 next 自然进 qa_gate"
                            "（QA 报告显式标注全部未实现项）。不叫人",
                }, ensure_ascii=False, indent=2))
                return 0
            print(json.dumps({
                "action": "blocked", "phase": pid, "detail": blocked_detail,
                "hint": "本任务内无 ticket 可开工。看 unmet_depends_on："
                        "① 依赖在后面的任务 → 任务划分错了，回 ①xuqiu 改任务计划；"
                        "② 依赖是 escalated/blocked → 那条得先转人解决；"
                        "③ 依赖不存在 → depends_on 写错了 ticket id",
            }, ensure_ascii=False, indent=2))
            return 3

        # 并行分组：touches 不冲突的可同一批派
        ready.sort(key=lambda t: (len(meta.get(t, {}).get("depends_on", [])), t))
        batch = []
        for t in ready:
            if all(not _touches_conflict(meta.get(t, {}).get("touches"),
                                         meta.get(b, {}).get("touches")) for b in batch):
                batch.append(t)
        payload = {
            "action": "impl", "phase": pid,
            "spawn": [{"ticket": t, "prompt": meta[t]["path"], "track": meta[t]["track"],
                       "system": meta[t]["system"], "retry": ts[t]["fix_attempts"]}
                      for t in batch],
            "deferred": [t for t in ready if t not in batch],
            "hint": "每个 ticket 派一个全新上下文的 impl subagent；完工后跑该 ticket 的 accept，"
                    "绿了 set --status done。deferred 的与本批 touches 冲突，下一批再派。"
                    "全批 set 完立即重跑 next，不要请示",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # 4) 本任务 ticket 全落定 → 进干净 QA 门（完整测试只在这里）
    esc = [t for t, v in in_phase.items() if v["status"] == "escalated"]
    qa_scope = ((state.get("phase_plan") or {}).get("qa_scope") or {}).get(pid) or None
    pending_s01 = _issue_summary(state, ("S0", "S1"))
    payload = {
        "action": "qa_gate", "phase": pid,
        "tickets": sorted(t for t, v in in_phase.items() if v["status"] == "done"),
        "escalated": esc, "qa_rounds": cur["qa_rounds"],
        "round_label": f"r{_qa_seq(cur) + 1}",
        "audit_path": os.path.join(_qa_dir(a.state), f"{pid}-r{_qa_seq(cur) + 1}.json"),
        "shards": [
            {"levels": ["L0", "L1"],
             "audit_path": os.path.join(_qa_dir(a.state),
                                        f"{pid}-r{_qa_seq(cur) + 1}-L01.json")},
            {"levels": ["L2", "L3"],
             "audit_path": os.path.join(_qa_dir(a.state),
                                        f"{pid}-r{_qa_seq(cur) + 1}-L23.json")},
        ],
        "systems": sorted({v["system"] for v in in_phase.values() if v["system"]}),
        "hint": "按 payload 的 shards 分片派【干净 QA subagent】（各自全新上下文，不读实现方的解释和报告、也不读同伴分片的结论，"
                "照 templates/qa-subagent-prompt.md 拼 prompt）跑本任务完整 L0-L3"
                + ("——范围以 payload 的 qa_scope 为准（每轮 QA 拿同一份，可 diff）"
                   if qa_scope else
                   "：L0 编译+单测/build · L1 真实 seed 跨表对账（禁自造数据自证）· "
                   "L2 端侧截图/真机（反 stale：先验产物版本）· "
                   "L3 独立邪路（跨日期/非超管权限/空数据/并发）"
                   "（phase-plan.yaml 未配 qa_scope，用通用范围）")
                + "。★ 按 payload 的 shards **分级派单**（默认 L0+L1 / L2+L3 两个 agent，"
                  "各写自己的 audit_path）——一个 agent 扛完 L0-L3 上下文会涨到几十万 token，"
                  "中途一断就得整轮重来。分片必须独占端口与运行根（含破坏性场景的那片尤其），"
                  "隔不开就改成串行分片，别让两片互相污染。"
                  "跑完 task_state.py qa merge --phase <id> 由程序算 verdict 合成规范 audit，"
                  "再 set --phase <id> --status qa_passed|qa_failed。"
                  "set 完立即重跑 next，不要请示"
                + (f"。⚠️ escalated 未实现的 ticket: {', '.join(esc)}——QA 报告里必须显式标注"
                   if esc else "")
                + ("。⚠️ 有 S0/S1 未决 issue——这是允许中断向 负责人 集中上报的时点（触点 b）"
                   if pending_s01 else ""),
    }
    if qa_scope:
        payload["qa_scope"] = qa_scope
    if pending_s01:
        payload["pending_issues"] = pending_s01
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_set(a):
    with StateLock(a.state):
        state = load_state(a.state)
        if state is None:
            sys.stderr.write(f"[error] 状态文件不存在: {a.state}\n")
            return 1
        if a.ticket:
            if a.ticket not in state["tickets"]:
                sys.stderr.write(f"[error] 未知 ticket: {a.ticket}"
                                 f"（新增的 ticket 先跑 `sync`）\n")
                return 1
            if a.status not in TICKET_STATUSES:
                sys.stderr.write(f"[error] ticket 状态须是 {TICKET_STATUSES}\n")
                return 1
            # done 必须带「坑与解法」：impl subagent 上下文用完即弃，此刻不落盘就永远丢了
            # ——它是 build-log / 自媒体母稿的唯一来源。真没有就写「无非显然项」（3 个字，
            # 不是墙是门槛：逼一次「有没有值得记的」的判断）。
            if a.status == "done" and not a.note.strip():
                sys.stderr.write(
                    "[error] set done 必须附 --note「坑与解法 3-5 行」（impl subagent 完工报告"
                    "第 2 项；确实没有就写 --note '无非显然项'）\n")
                return 1
            rec = state["tickets"][a.ticket]
            auto_esc = False
            if a.status == "failed":
                rec["fix_attempts"] += 1
                if rec["fix_attempts"] >= MAX_FIX_ATTEMPTS:
                    # 自动转 escalated：历史版本只 print 一句建议，next 照旧无限派单
                    rec["status"] = "escalated"
                    auto_esc = True
            if not auto_esc:
                rec["status"] = a.status
            if a.note:
                rec["note"] = a.note
            # escalated ↔ open_issues 并轨（同一把锁一次写盘）：
            # 挂起自动记 S1；人决策后 set pending 重新入队时自动关闭对应 issue
            if auto_esc or a.status == "escalated":
                _auto_issue(state, a.ticket,
                            (f"{a.ticket} 连撞 {MAX_FIX_ATTEMPTS} 次自动挂起" if auto_esc
                             else f"{a.ticket} 被挂起（escalated）"),
                            a.note or "")
            elif a.status == "pending":
                for it in state.get("open_issues", []):
                    if (it.get("ticket") == a.ticket and it.get("type") == "escalated"
                            and it.get("status") == "open"):
                        it["status"] = "decided"
                        it["decision"] = "重新入队"
            # stale（蓝图翻案重做）：把所在任务回退，否则该 ticket 永不再被调度
            if a.status == "stale":
                rec["fix_attempts"] = 0
                for ph in state["phases"]:
                    if ph["id"] == rec["phase"] and ph["status"] in ("qa_passed", "accepted", "risk_accepted"):
                        ph["status"] = "pending"
                        print(f"  ⚠️ 任务 {ph['id']} 已回退到 pending（含 stale ticket，需重跑 QA 门）")
            save_state(a.state, state)
            if auto_esc:
                print(f"[ok] {a.ticket} → escalated（连续失败 {rec['fix_attempts']} 次，自动转人）")
                print("  连撞 2 次说明是需求/口径问题不是实现问题，再修也是白修。")
                print("  已挂起：**不阻塞本任务其余 ticket**，但 QA 门会显式标注它未实现。")
                print("  人决策后：set --status pending 重新入队，或改需求/删 ticket 后跑 sync。")
            else:
                print(f"[ok] {a.ticket} → {a.status}"
                      + (f"（第 {rec['fix_attempts']} 次失败）" if a.status == "failed" else ""))
            return 0

        if a.phase:
            ph = next((p for p in state["phases"] if p["id"] == a.phase), None)
            if ph is None:
                sys.stderr.write(f"[error] 未知任务: {a.phase}\n")
                return 1
            if a.status not in PHASE_STATUSES:
                sys.stderr.write(f"[error] 任务状态须是 {PHASE_STATUSES}\n")
                return 1
            # qa_passed 一致性校验：历史版本零校验，可在 0 个 ticket 完成时
            # 让 next 返回 all_done「项目实现完成」——而主会话唯一读的就是 next。
            if a.status == "qa_passed":
                undone = {t: v["status"] for t, v in state["tickets"].items()
                          if v["phase"] == a.phase and v["status"] not in ("done", "escalated")}
                if undone:
                    sys.stderr.write(
                        f"[error] 任务 {a.phase} 还有 {len(undone)} 个 ticket 未落定，"
                        f"不能标 qa_passed：\n")
                    for t, s in sorted(undone.items()):
                        sys.stderr.write(f"        {t}: {s}\n")
                    sys.stderr.write("        （QA 门是对本任务**全部** ticket 的验收；"
                                     "确实不做的先 set 成 escalated 并说明）\n")
                    return 1
            # accepted 只能从 qa_passed 转入（先过 QA 门再抽验），不计 qa_rounds
            if a.status == "accepted" and ph["status"] != "qa_passed":
                sys.stderr.write(f"[error] accepted 只能从 qa_passed 转入"
                                 f"（当前 {ph['status']}）——先过 QA 门再人工抽验\n")
                return 1
            # qa_passed 证据门（2026-08-17，批 4）：不再是裸状态翻转。
            # QA subagent 按 qa-subagent-prompt.md 必须把 audit JSON 落固定路径，
            # 这里自动去找（--audit-json 仅覆盖）。**诚实标注：这是自签名证据**——
            # 真实价值是逼 QA subagent 填结构化表单（verdict/auditor/levels 各自
            # evidence），不是防伪；防伪靠「干净上下文 + 禁读实现方解释」的流程约定。
            # ★ 人裁决放行（2026-08-19 补）：技能里写着「触点 b 人决策」，却没有地方
            #   记录「决策人看过残留、明确接受、放行」这件事。缺这条路的实际后果是：
            #   真发生这件事时，唯一走得通的做法是**去改 QA 写的 audit JSON** ——
            #   那是伪造判词，比缺一个功能坏得多。所以给它一条明路，代价是记账要记得
            #   比 QA 通过更重：谁裁的、接受了什么残留、当时 QA 的判词是什么。
            if a.status == "qa_passed" and a.override_by:
                if len((a.note or "").strip()) < 30:
                    sys.stderr.write(
                        "[error] --override-by 必须配 --note 写清**接受了哪些残留**"
                        "（至少 30 字）——人裁决放行是把风险显式接下来，不是跳过\n")
                    return 1
                qa_dir = os.path.join(os.path.dirname(a.state), "qa")
                # 取**轮次最大**的那份，不是 qa_seq 那份：qa_seq 只数「set 过几次」，
                # 而人裁决放行往往发生在 QA 又跑了几轮却没 set 的时候 —— 拿 qa_seq
                # 去找会读到更早、判词更好看的那份，等于给自己挑证据。
                last_verdict, last = None, None
                cands = sorted(glob.glob(os.path.join(qa_dir, f"{a.phase}-r*.json")),
                               key=lambda f: (len(f), f))
                nums = []
                for f in cands:
                    m = re.search(rf"{re.escape(a.phase)}-r(\d+)\.json$", f)
                    if m:
                        nums.append((int(m.group(1)), f))
                if nums:
                    last = max(nums)[1]
                if last and os.path.exists(last):
                    try:
                        with open(last, encoding="utf-8") as f:
                            last_verdict = json.load(f).get("verdict")
                    except (OSError, json.JSONDecodeError):
                        pass
                ph["human_override"] = {
                    "by": a.override_by,
                    "qa_round_at_override": max(nums)[0] if nums else None,
                    "note": a.note,
                    "qa_verdict_at_override": last_verdict,
                    "audit_path": last if (last and os.path.exists(last)) else None,
                }
                print(f"[!] 人裁决放行：{a.override_by} 接受任务 {a.phase} 的残留并放行。"
                      f"**这不是 QA 通过** —— 最后一轮 QA 判词 = {last_verdict!r}。"
                      f"state 里记的是 human_override，别把它当绿灯引用。")
            elif a.status == "qa_passed":
                qa_dir = os.path.join(os.path.dirname(a.state), "qa")
                audit_path = a.audit_json or os.path.join(
                    qa_dir, f"{a.phase}-r{_qa_seq(ph) + 1}.json")
                try:
                    with open(audit_path, encoding="utf-8") as f:
                        audit = json.load(f)
                except OSError:
                    sys.stderr.write(
                        f"[error] 缺 audit JSON: {audit_path}\n"
                        f"        qa_passed 不是裸翻转——QA subagent 收尾必须把结构化"
                        f"结论落这个路径（qa-subagent-prompt.md 有格式），或 --audit-json 指定。\n")
                    return 1
                except json.JSONDecodeError as e:
                    sys.stderr.write(f"[error] audit JSON 不合法（{e}）: {audit_path}\n")
                    return 1
                if audit.get("verdict") != "PASS":
                    sys.stderr.write(f"[error] audit verdict={audit.get('verdict')!r}≠PASS"
                                     f"——QA 没给绿灯就不能标 qa_passed（{audit_path}）\n")
                    return 1
                if audit.get("auditor") != "independent":
                    sys.stderr.write(f"[error] auditor={audit.get('auditor')!r}≠independent"
                                     f"——实现方自评不算 QA（{audit_path}）\n")
                    return 1
                levels = audit.get("levels") or {}
                bad = [lv for lv in ("L0", "L1", "L2", "L3")
                       if (levels.get(lv) or {}).get("status") != "pass"
                       or not (levels.get(lv) or {}).get("evidence")]
                if bad:
                    sys.stderr.write(f"[error] audit 的 {', '.join(bad)} 缺 pass 状态或 "
                                     f"evidence——每级都要有亲自跑出来的证据（{audit_path}）\n")
                    return 1
            if a.status in ("qa_failed", "qa_passed"):
                # qa_seq 单调递增（rearm 不清它），qa_rounds 是闸门计数（rearm 会清零）
                ph["qa_seq"] = _qa_seq(ph) + 1
                ph["qa_rounds"] += 1
            ph["status"] = a.status
            save_state(a.state, state)
            print(f"[ok] 任务 {a.phase} → {a.status}（QA 第 {ph['qa_rounds']} 轮）")
            if a.status == "qa_failed" and ph["qa_rounds"] >= MAX_QA_ROUNDS:
                print(f"  ⚠️ 已连红 {ph['qa_rounds']} 轮 —— next 会报 exception 叫人，别再自动返工。")
            return 0

        sys.stderr.write("[error] 须指定 --ticket 或 --phase\n")
        return 1


# ═══════════════════════════════════════════════════════════════════════════
# open_issues —— 非阻塞问题台账（impl/QA subagent 的 WARN 落点）
# ═══════════════════════════════════════════════════════════════════════════
# 「非阻塞问题最后一起抛」的物理载体：impl subagent 撞上“字段名和蓝图对不上但不影响
# 本 ticket”这类问题时，此前只有两条路——问人（停机）或丢弃（两个月后变成偏离审计
# 大扫除）。现在第三条路：issue add 记下继续跑，qa_gate 拦 S0/S1、all_done 全量清。
# 格式与分级沿用 一个实战项目实战模板（见 templates/_open-issues.md）。

def cmd_issue_add(a):
    sev, capped = a.severity, False
    if a.itype == HARNESS_TYPE and sev in ("S0", "S1"):
        sev, capped = HARNESS_SEVERITY_CAP, True   # 检查工具自己的毛病拦不住任务，见常量注释
    with StateLock(a.state):
        state = load_state(a.state)
        if state is None:
            sys.stderr.write(f"[error] 状态文件不存在: {a.state}\n")
            return 1
        if a.ticket and a.ticket not in state["tickets"]:
            sys.stderr.write(f"[warn] ticket {a.ticket} 不在 state 里（照记，但确认没写错 id）\n")
        issues = state.setdefault("open_issues", [])
        nid = max((int(it.get("id", 0)) for it in issues), default=0) + 1
        rec = {"id": nid, "ticket": a.ticket or "", "severity": sev,
               "type": a.itype, "title": a.title, "detail": a.detail,
               "status": "open", "decision": ""}
        if capped:
            rec["severity_reported"] = a.severity
        issues.append(rec)
        save_state(a.state, state)
    print(f"[ok] issue #{nid} [{sev}/{a.itype}] {a.title}")
    if capped:
        print(f"  ⚖️  type={HARNESS_TYPE} 是「长在检查工具上、不长在产品上」，"
              f"{a.severity}→{HARNESS_SEVERITY_CAP} 封顶：它拦不住老师用、也拖不垮下游 ticket，"
              f"记账到 all_done 一起清，不许拿它拉一轮返工。")
    if sev in ("S0", "S1"):
        print("  S0/S1 会在下一个 qa_gate 的 pending_issues 里被拦（触点 b）；S2/S3 推到 all_done")
    return 0


def cmd_issue_list(a):
    state = load_state(a.state)
    if state is None:
        sys.stderr.write(f"[error] 状态文件不存在: {a.state}\n")
        return 1
    rows = [it for it in state.get("open_issues", [])
            if not a.undecided or it.get("status") == "open"]
    if not rows:
        print("（无" + ("未决 " if a.undecided else "") + "issue）")
        return 0
    for it in sorted(rows, key=lambda x: (x.get("severity", "S2"), x.get("id", 0))):
        mark = "⏳" if it.get("status") == "open" else "✅"
        print(f"{mark} #{it['id']:<3} {it.get('severity','?')}/{it.get('type','?'):<10} "
              f"[{it.get('ticket') or '-'}] {it.get('title','')}")
        if it.get("detail"):
            print(f"      {it['detail']}")
        if it.get("decision"):
            print(f"      决策: {it['decision']}")
    return 0


def cmd_issue_decide(a):
    with StateLock(a.state):
        state = load_state(a.state)
        if state is None:
            sys.stderr.write(f"[error] 状态文件不存在: {a.state}\n")
            return 1
        it = next((x for x in state.get("open_issues", []) if x.get("id") == a.id), None)
        if it is None:
            sys.stderr.write(f"[error] 无 issue #{a.id}（issue list 看全部）\n")
            return 1
        it["status"] = "decided"
        it["decision"] = a.decision + (f"：{a.note}" if a.note else "")
        save_state(a.state, state)
    print(f"[ok] issue #{a.id} 已决策: {a.decision}")
    return 0


def cmd_report(a):
    state = load_state(a.state)
    if state is None:
        sys.stderr.write(f"[error] 状态文件不存在: {a.state}\n")
        return 1
    ts = state["tickets"]
    done = sum(1 for v in ts.values() if v["status"] == "done")
    print(f"任务档进度：ticket {done}/{len(ts)} 完成\n")
    print(f"{'任务':<8}{'状态':<14}{'ticket 进度':<14}QA 轮次")
    for p in state["phases"]:
        inp = [v for v in ts.values() if v["phase"] == p["id"]]
        d = sum(1 for v in inp if v["status"] == "done")
        mark = {"qa_passed": "✅", "qa_failed": "❌", "qa_running": "🔄",
                "impl_done": "🟡", "accepted": "🏁"}.get(p["status"], "⬜")
        rounds = str(p["qa_rounds"])
        if p.get("rearms"):
            rounds += f"（累计 {_qa_seq(p)}，🔁 重新武装 {len(p['rearms'])} 次）"
        print(f"{p['id']:<8}{mark + p['status']:<14}{f'{d}/{len(inp)}':<14}{rounds}")
    open_issues = [it for it in state.get("open_issues", []) if it.get("status") == "open"]
    if open_issues:
        print(f"\n📋 未决 issue ({len(open_issues)})：")
        for it in sorted(open_issues, key=lambda x: (x.get("severity", "S2"), x.get("id", 0))):
            print(f"  #{it['id']} {it.get('severity','?')} [{it.get('ticket') or '-'}] "
                  f"{it.get('title','')}")

    for label, st, icon in (("失败待重试", "failed", "🟠"),
                            ("已转人挂起", "escalated", "🔴"),
                            ("蓝图翻案待重做", "stale", "🔁"),
                            ("卡住", "blocked", "⛔")):
        rows = [(t, v) for t, v in ts.items() if v["status"] == st]
        if rows:
            print(f"\n{icon} {label} ({len(rows)})：")
            for t, v in sorted(rows):
                extra = f"  第 {v['fix_attempts']} 次" if v["fix_attempts"] else ""
                print(f"  {t}{extra}  {v['note']}")
    return 0



# ═══════════════════════════════════════════════════════════════════════════
# rearm —— QA 门连红超限后，人拍板了才重新武装（唯一解除 exception 的口子）
# ═══════════════════════════════════════════════════════════════════════════
# 没有这个口子时，qa_rounds 撞到上限就永久卡死：next 每次都吐 exception，而返工
# 明明做完了，只能手改 state.json —— 手改没有留痕，「连红几轮」这个信号就此作废。
# 有了它，重新武装是一次**记名**的人决策：留 reason、留次数，report 和 next 都看得见。
# ★ 它是给人用的，主会话不许在自动循环里自己调 —— 那等于把 MAX_QA_ROUNDS 拆了。

def cmd_rearm(a):
    reason = (a.reason or "").strip()
    if len(reason) < 10:
        sys.stderr.write("[error] --reason 太短：重新武装是记名的人决策，"
                         "要写清 负责人 拍了什么板（≥10 字）\n")
        return 1
    with StateLock(a.state):
        state = load_state(a.state)
        if state is None:
            sys.stderr.write(f"[error] 状态文件不存在: {a.state}\n")
            return 1
        ph = next((p for p in state["phases"] if p["id"] == a.phase), None)
        if ph is None:
            sys.stderr.write(f"[error] 未知任务: {a.phase}\n")
            return 1
        if ph["status"] != "qa_failed" or ph["qa_rounds"] < MAX_QA_ROUNDS:
            sys.stderr.write(
                f"[error] {a.phase} 现在是 {ph['status']} / qa_rounds={ph['qa_rounds']}，"
                f"不需要重新武装。\n"
                f"        rearm 只用于「连红 {MAX_QA_ROUNDS} 轮转 exception、人已拍板」"
                f"这一种情况 —— 别拿它当常规提速手段，那是把 QA 门拆了。\n")
            return 1
        ph["qa_seq"] = _qa_seq(ph)          # 固化，老 state 补齐字段
        ran = max(ph["qa_seq"], _rounds_on_disk(a.state, a.phase))
        if ran >= MAX_QA_SEQ:
            sys.stderr.write(
                f"[error] {a.phase} 累计已经跑了 {ran} 轮 QA（磁盘上的 audit 为准），撞到硬顶 "
                f"MAX_QA_SEQ={MAX_QA_SEQ} —— 拒绝再武装。\n"
                f"        跑到这个轮数还没收，问题已经不在实现里了。继续加轮是纯亏的：\n"
                f"        实测 D1 跑了 7 轮，第 6、7 两轮一条 S0/S1 都没有，"
                f"找到的全长在检查工具自己身上。\n"
                f"        两条合法出口，挑一条：\n"
                f"        a) 认下残留往前走（记名，与 QA 通过永不混淆）：\n"
                f"           task_state.py set --phase {a.phase} --status qa_passed \\\n"
                f"             --override-by <人名> --note '<接受了哪些残留，≥30 字>'\n"
                f"        b) 回 ①xuqiu 改蓝图口径 / 重切任务 —— 连撞这么多轮通常是那里错了\n")
            return 1
        ph.setdefault("rearms", []).append({
            "reason": reason, "from_rounds": ph["qa_rounds"], "at_seq": ph["qa_seq"]})
        ph["qa_rounds"] = 0
        save_state(a.state, state)
        n, seq = len(ph["rearms"]), ph["qa_seq"]
    print(f"[ok] 任务 {a.phase} 的 QA 门已重新武装（第 {n} 次）：qa_rounds 归零，"
          f"下一轮 audit 落 {a.phase}-r{seq + 1}.json（旧证据不覆盖）")
    print(f"  记名理由：{reason}")
    print(f"  next 现在会返回 qa_rework —— 返工完照常重跑**整个** QA 门。")
    if n >= 2:
        print(f"  ⚠️ 已经是第 {n} 次重新武装。连撞这么多轮基本不是实现问题，"
              f"回 ①xuqiu 看蓝图口径和任务划分。")
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# qa merge —— 把分级 QA 分片合成一份规范 audit，verdict 由程序算
# ═══════════════════════════════════════════════════════════════════════════
# 为什么要分片：一个 QA subagent 扛完 L0-L3，上下文能涨到几十万 token，中途一断
# 就整轮重来（实测连撞三次 529 后颗粒无收，白烧 3 小时）。拆成 L0+L1 / L2+L3 两个
# 全新上下文，45 分钟跑完。
# 为什么合并要用程序：qa_passed 的证据门读的是**一份** canonical audit。让主会话
# 手写这份合并结论，等于把「四级全绿」这句话交给一个没跑过测试的人写 —— 正是
# 「绿着错」的入口。这里只做机械合并：缺级拒绝、重复认领拒绝、无证据拒绝，
# verdict 由 全级 pass ∧ 无 S0/S1 算出来。

def cmd_qa_merge(a):
    state = load_state(a.state)
    if state is None:
        sys.stderr.write(f"[error] 状态文件不存在: {a.state}\n")
        return 1
    ph = next((p for p in state["phases"] if p["id"] == a.phase), None)
    if ph is None:
        sys.stderr.write(f"[error] 未知任务: {a.phase}\n")
        return 1
    seq = a.round if a.round else _qa_seq(ph) + 1
    qa_dir = _qa_dir(a.state)
    canon = os.path.join(qa_dir, f"{a.phase}-r{seq}.json")
    shards = sorted(glob.glob(os.path.join(qa_dir, f"{a.phase}-r{seq}-*.json")))
    if not shards:
        sys.stderr.write(f"[error] 没找到分片: {qa_dir}/{a.phase}-r{seq}-*.json\n"
                         f"        分片文件名照 next 的 shards[].audit_path 落"
                         f"（如 {a.phase}-r{seq}-L01.json / -L23.json）\n")
        return 1
    if os.path.exists(canon):
        try:
            old = json.load(open(canon, encoding="utf-8"))
        except Exception:
            old = {}
        if not a.force and not old.get("merged_from") and \
                old.get("verdict") in ("PASS", "FAIL"):
            sys.stderr.write(f"[error] {canon} 已存在且不是合并产物"
                             f"（verdict={old.get('verdict')}）——不覆盖别人的结论。"
                             f"确要覆盖加 --force\n")
            return 1

    levels, owners, issues, escalated, bad_auditor = {}, {}, [], [], []
    for sp in shards:
        name = os.path.basename(sp)
        try:
            d = json.load(open(sp, encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"[error] 分片读不了 {name}: {e}\n")
            return 1
        if not isinstance(d, dict):
            sys.stderr.write(f"[error] 分片不是 object: {name}\n")
            return 1
        if d.get("auditor") != "independent":
            bad_auditor.append(f"{name}(auditor={d.get('auditor')!r})")
        for lv, v in (d.get("levels") or {}).items():
            if lv not in QA_LEVELS or not isinstance(v, dict):
                continue
            # 只认跑完的级：pending/running 的是占位，不算认领
            if v.get("status") not in ("pass", "fail"):
                continue
            if lv in levels:
                sys.stderr.write(f"[error] {lv} 被两个分片同时认领"
                                 f"（{owners[lv]} 与 {name}）——分工没分清，"
                                 f"合并会让谁的结论算数变成掷骰子\n")
                return 1
            levels[lv] = v
            owners[lv] = name
        issues += [i for i in (d.get("issues") or []) if isinstance(i, dict)]
        escalated += list(d.get("escalated_noted") or [])
    if bad_auditor:
        sys.stderr.write(f"[error] 分片 auditor≠independent: {', '.join(bad_auditor)}"
                         f"——实现方自评不算 QA\n")
        return 1
    missing = [lv for lv in QA_LEVELS if lv not in levels]
    if missing:
        sys.stderr.write(f"[error] 这一轮没人跑 {', '.join(missing)} —— 拒绝合并。\n"
                         f"        分片覆盖：" +
                         "; ".join(f"{lv}←{owners[lv]}" for lv in sorted(owners)) + "\n"
                         f"        缺级的 audit 合出来就是「没跑当跑过」，"
                         f"正是 qa_passed 证据门要挡的东西\n")
        return 1
    noev = [lv for lv in QA_LEVELS if not levels[lv].get("evidence")]
    if noev:
        sys.stderr.write(f"[error] {', '.join(noev)} 没有 evidence"
                         f"——每级都要有亲自跑出来的命令与输出\n")
        return 1

    # ── 检查工具自己的毛病封顶 S2 ──────────────────────────────────────
    # 它是真的，但它不是产品缺陷。放它进 S0/S1 就等于让「我的断言写得不够狠」
    # 这件事去拉一轮返工，而下一轮必然发现「我给断言写的那条自检也不够狠」。
    capped = []
    for i in issues:
        if i.get("type") == HARNESS_TYPE and i.get("severity") in ("S0", "S1"):
            capped.append(f"{i.get('severity')}→{HARNESS_SEVERITY_CAP} {i.get('title', '')[:40]}")
            i["severity_reported"] = i["severity"]
            i["severity"] = HARNESS_SEVERITY_CAP

    blocking = [i for i in issues if i.get("severity") in ("S0", "S1")]

    # ── 级的红绿是**算**出来的，不是分片按感觉写的 ─────────────────────
    # 原先 verdict = 全级 status==pass ∧ 无 S0/S1，两套口径 AND 在一起，而松的
    # 那套（level status）是 agent 自由心证。实测 D1 第 2 轮：L0+L1 片判 fail，
    # 它报的 11 条**全是 S2/S3** —— 按台账口径这些本该推到 all_done，却硬把整个
    # 任务拉回一轮返工。现在要判一级红，只有一条路：把问题定成 S0/S1 并标 level。
    # 那条路上有台账、有 负责人 的 decide，自由心证在那里会被看见。
    for lv in QA_LEVELS:
        levels[lv]["reported"] = levels[lv].get("status")
        lv_blocking = [i for i in blocking if i.get("level") == lv]
        levels[lv]["status"] = "fail" if lv_blocking else "pass"

    downgraded = [lv for lv in QA_LEVELS
                  if levels[lv]["reported"] not in (None, "pass") and levels[lv]["status"] == "pass"]

    failed = [lv for lv in QA_LEVELS if levels[lv]["status"] != "pass"]
    verdict = "PASS" if not failed and not blocking else "FAIL"
    out = {
        "phase": a.phase, "round": seq, "verdict": verdict, "auditor": "independent",
        "levels": {lv: levels[lv] for lv in QA_LEVELS},
        "escalated_noted": sorted(set(escalated)),
        "issues": issues,
        "merged_from": [{"file": os.path.basename(owners[lv]), "levels": lv}
                        for lv in QA_LEVELS],
    }
    os.makedirs(qa_dir, exist_ok=True)
    with open(canon, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[ok] 合成 {canon}")
    for lv in QA_LEVELS:
        print(f"  {lv} {levels[lv]['status']:<5} ← {owners[lv]}")
    print(f"  issues {len(issues)} 条（S0/S1 {len(blocking)} 条）")
    for line in capped:
        print(f"  ⚖️  长在检查工具上，封顶 S2：{line}")
    if downgraded:
        print(f"  ⚖️  {'/'.join(downgraded)} 分片自己判了 fail，但这几级一条带 level 标记的 "
              f"S0/S1 都没有 —— 按台账口径算 pass。")
        print(f"      要让一级真的红，把问题定成 S0/S1 并在 issue 里写 \"level\": \"L?\"；"
              f"S2/S3 是「推到 all_done 一起清」的意思，不是「再来一轮」。")
    print(f"  verdict = {verdict}" +
          ("" if verdict == "PASS" else
           f"（红在 {'/'.join(failed) or '—'}" +
           (f" + {len(blocking)} 条 S0/S1" if blocking else "") + "）"))
    print(f"  → 先把 issues 逐条 issue add，再 "
          f"set --phase {a.phase} --status "
          f"{'qa_passed' if verdict == 'PASS' else 'qa_failed'}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="task_state.py",
        description="任务档执行状态机（主会话调度用）",
        epilog="⚠️ --state 是全局参数，必须写在子命令**之前**："
               "task_state.py --state x.json init --tickets doc/tickets")
    p.add_argument("--ws", default=None,
                   help="工作区根：含 tickets/ waves/ phase-plan.yaml 的那一层。"
                        "省略则按 $ZHIXING_WORKDIR → 自动探测 → 兜底 doc/ 定位")
    p.add_argument("--state", default=None,
                   help="状态文件路径（写在子命令之前；默认 <ws>/waves/state.json）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="从 ticket 目录初始化状态（会重置进度）")
    pi.add_argument("--tickets", default=None,
                    help="ticket 根目录（默认 <ws>/tickets）")
    pi.add_argument("--force", action="store_true", help="覆盖已有 state（重置全部进度）")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("sync", help="增量并入新增/删除的 ticket，保留已有进度")
    ps.add_argument("--tickets", help="ticket 目录（省略则用 state 里记的）")
    ps.set_defaults(func=cmd_sync)

    pn = sub.add_parser("next", help="算下一步该干什么（JSON）")
    pn.set_defaults(func=cmd_next)

    pset = sub.add_parser("set", help="记录 ticket / 任务状态")
    pset.add_argument("--ticket")
    pset.add_argument("--phase")
    pset.add_argument("--status", required=True)
    pset.add_argument("--note", default="")
    pset.add_argument("--audit-json", dest="audit_json", default="",
                      help="qa_passed 的证据文件（默认自动找 <state目录>/qa/<phase>-r<round>.json）")
    pset.add_argument("--override-by", dest="override_by", default="",
                      help="人裁决放行：QA 没给 PASS，但决策人看过残留、明确接受并放行。"
                           "填决策人（如 owner），**必须同时 --note 写清接受了哪些残留**。"
                           "state 里记 human_override，与 QA 通过永不混淆。")
    pset.set_defaults(func=cmd_set)

    pr = sub.add_parser("report", help="人看的进度表")
    pr.set_defaults(func=cmd_report)

    pis = sub.add_parser("issue", help="非阻塞问题台账（impl/QA 的 WARN 落点，格式见 templates/_open-issues.md）")
    isub = pis.add_subparsers(dest="issue_cmd", required=True)
    pia = isub.add_parser("add", help="记一条非阻塞问题，不改 ticket 状态、不计 fix_attempts")
    pia.add_argument("--ticket", default="", help="关联 ticket id（可空）")
    pia.add_argument("--severity", required=True, choices=("S0", "S1", "S2", "S3"),
                     help="S0 阻塞下游 / S1 影响别的 ticket / S2 本 ticket 内 cosmetic / S3 长尾")
    pia.add_argument("--type", dest="itype", default="debt",
                     choices=("arch", "ux", "debt", "clarify", "doc-drift", "escalated",
                              HARNESS_TYPE),
                     help="harness = 长在检查工具上而不是产品上（断言写错 / 闸门有洞 / "
                          "脚手架不严），封顶 S2 不许拉返工")
    pia.add_argument("--title", required=True)
    pia.add_argument("--detail", default="", help="影响范围（file:line / 下游 ticket）+ 建议方案 a/b/c")
    pia.set_defaults(func=cmd_issue_add)
    pil = isub.add_parser("list")
    pil.add_argument("--undecided", action="store_true")
    pil.set_defaults(func=cmd_issue_list)
    pid_ = isub.add_parser("decide", help="负责人 拍板（a/b/c/拒绝/推V2）")
    pid_.add_argument("--id", type=int, required=True)
    pid_.add_argument("--decision", required=True)
    pid_.add_argument("--note", default="")
    pid_.set_defaults(func=cmd_issue_decide)

    prm = sub.add_parser("rearm", help="QA 门连红超限、人已拍板 → 重新武装闸门（主会话不许自调）")
    prm.add_argument("--phase", required=True)
    prm.add_argument("--reason", required=True, help="负责人 拍板的原话（记名，会留痕）")
    prm.set_defaults(func=cmd_rearm)

    pqa = sub.add_parser("qa", help="QA audit 分片工具")
    qsub = pqa.add_subparsers(dest="qa_cmd", required=True)
    pqm = qsub.add_parser("merge", help="把分级分片合成规范 audit（verdict 由程序算）")
    pqm.add_argument("--phase", required=True)
    pqm.add_argument("--round", type=int, default=0, help="默认取当前轮（qa_seq+1）")
    pqm.add_argument("--force", action="store_true", help="覆盖非合并产物的同名 audit")
    pqm.set_defaults(func=cmd_qa_merge)

    a = p.parse_args(argv)

    # 工作区根解析（见文件上方 resolve_ws）。子命令参数在原位解析，故此处补齐默认值。
    ws = resolve_ws(a.ws)
    if a.state is None:
        a.state = os.path.join(ws, "waves", "state.json")
    if a.cmd == "init" and a.tickets is None:
        a.tickets = os.path.join(ws, "tickets")

    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
