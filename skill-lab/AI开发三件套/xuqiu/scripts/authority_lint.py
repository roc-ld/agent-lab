#!/usr/bin/env python3
"""authority_lint.py — 权威变更传播链（把 `blueprint_refs` 从摆设变成机制）。

═══════════════════════════════════════════════════════════════════════════
为什么必须有这个脚本
═══════════════════════════════════════════════════════════════════════════
`blueprint_refs` 字段一直都在，但在本脚本出现前**零个脚本读它的内容**
（dag_lint C1 只查字段存在；空数组 `[]` 直到 2026-08-16 才被 C7 拦成 error——
此前的注释声称"dag_lint 一直强制它非空"是假前提）。于是这条链是断的：

    蓝图改了 ──?──▶ 哪些 ticket 受影响 ──?──▶ 置 stale ──▶ 重做
               ↑断                       ↑断

后果（3-4 个月、甲方持续调整的项目里必然发生）：
  1. 改一个字段口径 → 已完工的 N 个 ticket 里哪几个建在错图上，**没人知道**
  2. 那些 ticket 的 accept 还是绿的（期望值写死在旧口径上）→ **绿着错**，比红着错危险
  3. 验收期归因不到 doc / impl / misread

本脚本把链补上：给每条权威记录算 hash → diff 出变了哪些锚 id → 反查 blueprint_refs
→ 输出受影响 ticket 集（可直接喂 task_state 置 stale）。

═══════════════════════════════════════════════════════════════════════════
命令
═══════════════════════════════════════════════════════════════════════════
  authority_lint.py snapshot            给当前权威打快照（首次 / 消化完变更后）
  authority_lint.py diff                当前 vs 快照 → 变了哪些锚 id
  authority_lint.py impact              变更锚 id 反查 → 受影响 ticket（--apply 直接置 stale）
  authority_lint.py check               体检：悬空引用 / 漏锚 / 锚到废弃记录
  authority_lint.py show <锚id>         精确取一条权威（给 AI 用，免全文读）
  authority_lint.py render              从 YAML 单向渲染人看的 markdown

  公共参数：--authority doc/authority  --tickets doc/tickets  --req doc/requirements.yaml

退出码：0 无事 / 1 有 error（悬空引用、漏锚等）/ 2 有变更待消化（diff/impact 非空）

依赖：**需要 pyyaml**。这里不做手解析兜底——权威数据被静默错解析的后果，
比"装个包"严重得多（半对的解析会让反查漏掉 ticket，而漏掉是无声的）。
"""

from __future__ import annotations   # 兼容 Python 3.9

import argparse
import glob
import hashlib
import json
import os
import re
import sys

SNAPSHOT_NAME = "_snapshot.json"
# ADR-NNNN 是第五种锚（2026-08-17）：ADR 翻案与蓝图翻案是同一类事（ADR-0011 耳号
# 翻案就是实例），必须同样进 snapshot/diff/impact 的传播链。
ANCHOR_RE = re.compile(r"^(REQ|FIELD|FLOW|UI|OWN):|^ADR-\d{3,4}$")


# ═══════════════════════════════════════════════════════════════════════════
# 工作区根定位（workspace root）
# ═══════════════════════════════════════════════════════════════════════════
# 为什么需要：同一套脚本要服务两种真实存在的目录布局——
#   · 标准布局  <root>/doc/{requirements.yaml,authority/,tickets/,_adr/}
#   · 拍平布局  <root>/{requirements.yaml,authority/,tickets/,_adr/}
#     （一个实战项目的多阶段项目就是拍平形态：doc-phase2/、doc-phase3/ 各是一个 root）
#
# 在此之前路径默认值写死 `doc/…`：在拍平布局下一个文件都读不到，**却照样打绿灯**。
# 2026-09-13 实测：`cd doc-phase2 && authority_lint.py check` 报
# 「0 条权威 × 0 个 ticket」并 ✅ 退出 0；加显式路径后真实数据是 556 条。
# 这正是本 skill 自己最警惕的「绿着错」，所以：
#   ① 自动探测（不静默，往 stderr 打一行）；
#   ② check 对零输入直接报 error（见 cmd_check）。
#
# 定位顺序：显式 --ws > $XUQIU_WORKDIR > 自动探测 > 老默认 doc/。
WS_ENV = "XUQIU_WORKDIR"
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


def _need_yaml():
    try:
        import yaml  # noqa: PLC0415
        return yaml
    except ImportError:
        sys.stderr.write(
            "[error] 需要 pyyaml：pip install pyyaml\n"
            "        本脚本刻意不做手解析兜底——权威数据被半对地解析，会让反查\n"
            "        **无声地漏掉** ticket，比装个包严重得多。\n")
        raise SystemExit(1)


# ═══════════════════════════════════════════════════════════════════════════
# 载入
# ═══════════════════════════════════════════════════════════════════════════
def load_requirements(req_path):
    """把 requirements.yaml 的每条需求也载入成 `REQ:<id>` 锚。

    为什么需求也要进传播链：**甲方改需求原文和改蓝图是同一类事**——
    都会让已完工的 ticket 建在过时的依据上，而且都是无声的。
    ticket 用 `req_refs` 指需求、用 `blueprint_refs` 指蓝图，两者都是"权威引用"，
    走同一套 snapshot/diff/impact 才不会漏。

    甲方后续补新版需求文件是常态（`req_extract.py --merge`），改了措辞的那几条
    会在这里 hash 变化 → impact 反查出受影响 ticket。
    """
    yaml = _need_yaml()
    out, errors = {}, []
    if not os.path.exists(req_path):
        return out, errors            # 没有需求清单不算错（可能项目还没抽）
    try:
        with open(req_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        errors.append(f"{req_path}: YAML 解析失败: {e}")
        return out, errors
    for r in (data.get("requirements") or []):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        rid = str(r["id"]).strip()
        aid = rid if rid.startswith("REQ:") else f"REQ:{rid}"
        rec = {
            "id": aid,
            "title": r.get("name") or (str(r.get("text", ""))[:40]),
            # 只 hash 会影响实现的：原文 + 状态 + 优先级。
            # 改 note/tickets 不该让 ticket 失效（那是派生信息）。
            "body": str(r.get("text", "")).strip(),
            "status": {"pending": "active", "covered": "active",
                       "clarify": "active"}.get(str(r.get("status", "")),
                                                 str(r.get("status") or "active")),
            "owner_system": str(r.get("domain") or ""),
            "priority": str(r.get("priority") or ""),
            "source": str(r.get("source") or ""),
        }
        _put(out, errors, aid, rec, req_path)
    return out, errors


def load_authority(authority_dir):
    """读 doc/authority/*.yaml → {anchor_id: record}。

    记录来源有二：
      · 顶层 records[]（flows / ui-index / ownership）
      · flow 记录的 steps[]（粒度到步——改一步不该让锚了整个流程的 ticket 全失效）
    另外 field-ssot.yaml 的每个字段自动派生 `FIELD:<table>.<column>` 锚。
    """
    yaml = _need_yaml()
    out, errors = {}, []
    for path in sorted(glob.glob(os.path.join(authority_dir, "*.yaml"))):
        if os.path.basename(path).startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            errors.append(f"{path}: YAML 解析失败: {e}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path}: 顶层不是 mapping")
            continue

        # field-ssot.yaml：从 tables[].fields[] 派生 FIELD: 锚
        if data.get("tables"):
            for t in data["tables"]:
                tname = t.get("table") or t.get("name")
                if not tname:
                    continue
                for fld in (t.get("fields") or []):
                    fname = fld.get("name")
                    if not fname:
                        continue
                    aid = f"FIELD:{tname}.{fname}"
                    _put(out, errors, aid, {
                        "id": aid, "title": f"{tname}.{fname}",
                        "status": "active",
                        "owner_system": t.get("system", ""),
                        "body": json.dumps(fld, ensure_ascii=False, sort_keys=True),
                    }, path)
            continue

        for rec in (data.get("records") or []):
            if not isinstance(rec, dict) or not rec.get("id"):
                errors.append(f"{path}: 有记录缺 id")
                continue
            _put(out, errors, str(rec["id"]).strip(), rec, path)
            for step in (rec.get("steps") or []):
                if isinstance(step, dict) and step.get("id"):
                    s = dict(step)
                    s.setdefault("title", f"{rec.get('title','')} · {step.get('action','')}")
                    s.setdefault("status", rec.get("status", "active"))
                    s["_parent"] = rec["id"]
                    _put(out, errors, str(step["id"]).strip(), s, path)
    return out, errors


def _put(out, errors, aid, rec, path):
    if not ANCHOR_RE.match(aid):
        errors.append(f"{path}: 锚 id {aid!r} 前缀不合法"
                      f"（必须是 REQ:/FIELD:/FLOW:/UI:/OWN: 之一，或 ADR-NNNN）")
        return
    if aid in out:
        errors.append(f"{path}: 锚 id 重复 {aid}（id 必须全局唯一且终身不变）")
        return
    rec = dict(rec)
    rec["_src"] = path
    out[aid] = rec


def record_hash(rec):
    """按**语义字段**算 hash，忽略来源路径等元信息。

    只 hash 会影响实现的字段——改 title 的错别字不该让 20 个 ticket 失效，
    但改 body / 主权归属 / 业务键必须触发。
    """
    sig = {k: rec.get(k) for k in
           ("id", "body", "status", "superseded_by", "owner_system",
            "authority_system", "reader_systems", "sync", "conflict_rule",
            "business_key", "writes", "produces", "actor", "action",
            "form_factor", "prototype", "priority")
           if rec.get(k) is not None}
    return hashlib.sha256(
        json.dumps(sig, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def load_ticket_anchors(tickets_dir):
    """扫 <dir>/**/prompt.md → ({ticket: [anchor...]}, errors)。"""
    yaml = _need_yaml()
    out, errors = {}, []
    for md in sorted(glob.glob(os.path.join(tickets_dir, "**", "prompt.md"),
                               recursive=True)):
        tid = os.path.basename(os.path.dirname(md))
        try:
            with open(md, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            errors.append(f"{tid}: 读文件失败 {e}")
            continue
        if not lines or lines[0].strip() != "---":
            errors.append(f"{tid}: 缺 front-matter")
            continue
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            errors.append(f"{tid}: front-matter 未闭合")
            continue
        try:
            fm = yaml.safe_load("\n".join(lines[1:end])) or {}
        except Exception as e:
            errors.append(f"{tid}: YAML 解析失败 {e}")
            continue
        tid = str(fm.get("ticket") or tid)
        # (field, anchor) 二元组：cmd_check 报错时要能说清悬空的是 blueprint_refs
        # 还是 req_refs——合成一个列表曾导致 req_refs 悬空被报成"blueprint_refs 悬空"。
        refs = []
        for field in ("blueprint_refs", "req_refs", "adr_refs"):
            v = fm.get(field) or []
            if not isinstance(v, list):
                errors.append(f"{tid}: {field} 不是 list")
                continue
            for r in v:
                r = str(r).strip()
                # req_refs 里写的是裸 REQ id（REQ-STORE-011），统一加 REQ: 前缀成锚
                if field == "req_refs" and not r.startswith("REQ:"):
                    r = f"REQ:{r}"
                refs.append((field, r))
        out[tid] = refs
    return out, errors


def load_adrs(adr_dir):
    """doc/_adr/*.md frontmatter → ADR 记录。

    只 hash **frontmatter 的语义字段**（decision / implementation_status / anchor /
    supersedes 链），正文措辞不触发 impact——最终口径必须写在 frontmatter
    （templates/adr.md 的约定），正文只是理由；改理由的措辞不该让 20 个 ticket 失效。
    """
    yaml = _need_yaml()
    out, errors = {}, []
    if not os.path.isdir(adr_dir):
        return out, errors                      # 没有 ADR 目录的项目不强制
    for md in sorted(glob.glob(os.path.join(adr_dir, "*.md"))):
        base = os.path.basename(md)
        if base.upper().startswith(("README", "_")):
            continue
        try:
            with open(md, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            errors.append(f"{base}: 读文件失败 {e}")
            continue
        if not lines or lines[0].strip() != "---":
            errors.append(f"{base}: 缺 YAML frontmatter（双轨 status 全靠它，见 templates/adr.md）")
            continue
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            errors.append(f"{base}: frontmatter 未闭合")
            continue
        try:
            fm = yaml.safe_load("\n".join(lines[1:end])) or {}
        except Exception as e:
            errors.append(f"{base}: frontmatter 解析失败 {e}")
            continue
        aid = str(fm.get("id") or "").strip()
        if not re.fullmatch(r"ADR-\d{3,4}", aid):
            errors.append(f"{base}: frontmatter 缺合法 id（形如 ADR-0011）")
            continue
        if aid in out:
            errors.append(f"{base}: ADR id 重复 {aid}")
            continue
        title = next((ln.lstrip("# ").strip() for ln in lines[end + 1:]
                      if ln.startswith("#")), "")
        status = str(fm.get("status") or "proposed").strip()
        sb = fm.get("superseded_by") or ""
        if isinstance(sb, list):
            sb = sb[0] if sb else ""
        out[aid] = {
            "id": aid, "title": title,
            # deprecated 归一成 dropped：锚到不再适用的 ADR 与锚到废弃权威同罪
            "status": {"deprecated": "dropped"}.get(status, status),
            "adr_status": status,
            "superseded_by": str(sb).strip(),
            # default=str：yaml 会把 decided_at/verified 解析成 date 对象，裸 dumps 会崩
            "body": json.dumps({
                "decision": fm.get("decision"),
                "implementation_status": fm.get("implementation_status"),
                "anchor": fm.get("anchor"),
                "supersedes": fm.get("supersedes"), "amends": fm.get("amends"),
            }, ensure_ascii=False, sort_keys=True, default=str),
            "_src": md,
        }
    return out, errors


def load_all(a):
    """权威 + 需求 + ADR = 完整的「ticket 可以引用的东西」全集。"""
    recs, errs = load_authority(a.authority)
    r2, e2 = load_requirements(a.req)
    for k, v in r2.items():
        if k in recs:
            errs.append(f"锚 id 冲突：{k} 同时出现在权威和需求清单里")
        recs[k] = v
    adr_dir = getattr(a, "adr", None) or os.path.join(
        os.path.dirname(os.path.abspath(a.authority)), "_adr")
    r3, e3 = load_adrs(adr_dir)
    for k, v in r3.items():
        if k in recs:
            errs.append(f"锚 id 冲突：{k} 同时出现在 ADR 和其他权威里")
        recs[k] = v
    return recs, errs + e2 + e3


def load_snapshot(authority_dir):
    p = os.path.join(authority_dir, SNAPSHOT_NAME)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(authority_dir, records):
    p = os.path.join(authority_dir, SNAPSHOT_NAME)
    payload = {"hashes": {aid: record_hash(r) for aid, r in records.items()},
               "titles": {aid: r.get("title", "") for aid, r in records.items()}}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, p)
    return p


def compute_diff(records, snap):
    """→ {changed:[], added:[], removed:[]}"""
    cur = {aid: record_hash(r) for aid, r in records.items()}
    old = (snap or {}).get("hashes", {})
    return {
        "changed": sorted(a for a in cur if a in old and cur[a] != old[a]),
        "added": sorted(a for a in cur if a not in old),
        "removed": sorted(a for a in old if a not in cur),
    }


def expand(anchors, records):
    """锚 id 展开成"它 + 它的所有子锚"。

    改了 `FLOW:F-WMS-03` 本体（比如整体口径变了），锚在它某一步上的 ticket 也受影响；
    反之改某一步**不**牵连锚了别的步的 ticket —— 这正是细粒度锚的意义。
    """
    out = set(anchors)
    for aid, rec in records.items():
        if rec.get("_parent") in anchors:
            out.add(aid)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 命令
# ═══════════════════════════════════════════════════════════════════════════
def cmd_snapshot(a):
    records, errors = load_all(a)
    if errors:
        for e in errors:
            sys.stderr.write(f"[error] {e}\n")
        return 1
    if not records:
        sys.stderr.write(f"[error] {a.authority} 下没读到任何权威记录\n")
        return 1
    snap = load_snapshot(a.authority)
    if snap:
        d = compute_diff(records, snap)
        n = len(d["changed"]) + len(d["added"]) + len(d["removed"])
        if n and not a.force:
            sys.stderr.write(
                f"[error] 检测到 {n} 处权威变更尚未消化。\n"
                f"        直接打快照会把变更**悄悄吞掉**——已完工的 ticket 建在错图上、\n"
                f"        accept 还绿着，而且再也查不出来。\n"
                f"        先跑 `impact` 处理受影响 ticket，确认无误后再 `snapshot --force`。\n")
            return 2
    p = save_snapshot(a.authority, records)
    print(f"[ok] 快照已更新：{len(records)} 条权威记录 → {p}")
    return 0


def cmd_diff(a):
    records, errors = load_all(a)
    for e in errors:
        sys.stderr.write(f"[error] {e}\n")
    snap = load_snapshot(a.authority)
    if snap is None:
        print(f"[info] 还没有快照（{a.authority}/{SNAPSHOT_NAME}）——先跑 snapshot 建立基线")
        return 0
    d = compute_diff(records, snap)
    total = sum(len(v) for v in d.values())
    if not total:
        print("✅ 权威无变更")
        return 0
    print(f"权威变更 {total} 处：")
    for kind, label in (("changed", "🔁 内容改了"), ("added", "➕ 新增"), ("removed", "➖ 删除")):
        if d[kind]:
            print(f"\n{label} ({len(d[kind])})：")
            for aid in d[kind]:
                title = records.get(aid, {}).get("title") or snap["titles"].get(aid, "")
                print(f"  {aid}  {title}")
    if d["removed"]:
        print("\n⚠️ 有记录被**删除**。权威记录不该删——废弃标 status: dropped、"
              "被取代标 superseded + superseded_by。\n"
              "   删了会让历史 ticket 的 blueprint_refs 悬空，反查链断掉。")
    print("\n下一步：authority_lint.py impact   （看波及哪些 ticket）")
    return 2


def cmd_impact(a):
    records, aerr = load_all(a)
    tickets, terr = load_ticket_anchors(a.tickets)
    for e in aerr + terr:
        sys.stderr.write(f"[error] {e}\n")
    snap = load_snapshot(a.authority)
    if snap is None:
        sys.stderr.write("[error] 还没有快照，无从比较。先跑 snapshot 建立基线。\n")
        return 1
    d = compute_diff(records, snap)
    touched = expand(set(d["changed"]) | set(d["removed"]), records)
    if not touched:
        print("✅ 权威无变更（或变更不涉及任何已有锚），无 ticket 受影响")
        return 0

    affected = {}
    for tid, refs in tickets.items():
        hit = sorted({r for _, r in refs} & touched)
        if hit:
            affected[tid] = hit

    print(f"权威变更涉及 {len(touched)} 个锚，波及 {len(affected)} 个 ticket\n")
    if affected:
        for tid, hit in sorted(affected.items()):
            print(f"  [{tid}]  ← {', '.join(hit)}")
        print("\n这些 ticket 需要：")
        print("  ① 重做（已完工的置 stale 重新入队）")
        print("  ② **accept 断言一并更新** —— 期望值写死在旧口径上，不改就是绿着错")
        print("\n置 stale：")
        for tid in sorted(affected):
            print(f"  python3 <zhixing>/scripts/task_state.py set --ticket {tid} "
                  f"--status stale --note '权威变更: {affected[tid][0]}'")

    # 闭合校验：蓝图确实改了，却没有任何 ticket 锚它 → 疑有漏锚
    unanchored = sorted(a_ for a_ in touched
                        if not any(a_ in {r for _, r in refs}
                                   for refs in tickets.values()))
    if unanchored:
        print(f"\n⚠️ 有 {len(unanchored)} 个变更锚**没有任何 ticket 引用**：")
        for aid in unanchored:
            print(f"  {aid}  {records.get(aid, {}).get('title', '')}")
        print("  两种可能：① 这条权威还没被实现（正常，将来拆 ticket 时锚上）")
        print("            ② **有 ticket 漏写 blueprint_refs** —— 那它就建在错图上而无人知晓")
        print("  逐条确认，别默认是 ①。")

    if a.apply:
        print("\n[--apply] 本脚本不直接改 zhixing 的 state.json（跨 skill 写状态"
              "会让两边的真相源纠缠）。请复制上面的命令执行。")
    return 2 if affected or unanchored else 0


def cmd_check(a):
    records, aerr = load_all(a)
    tickets, terr = load_ticket_anchors(a.tickets)
    errors = list(aerr) + list(terr)
    warnings = []

    # ── 零输入守卫（2026-09-13 体检测试补）─────────────────────────────
    # 为什么必须有：errors 只在"读到了东西但引用不对"时才产生；路径不对时
    # 读到 0 条 → 0 个 error → 打印 ✅ 并退出 0。这就是「绿着错」：
    # 2026-09-13 实测 实战项目/doc-phase2 报 0 条权威 × 0 ticket 却全绿，
    # 而真实数据是 556 条权威 × 49 个 ticket。闸门形同虚设却没人知道。
    # 措辞照抄 dag_lint 的同类守卫，保持三件套一致。
    if not records:
        sys.stderr.write(
            f"[error] 权威目录里一条记录都没读到：{a.authority}\n"
            f"        多半是路径不对——多阶段项目要传 --ws doc-phaseN，"
            f"或设 $XUQIU_WORKDIR。\n"
            f"        零条权威绝不是「全过」，不给绿灯。\n")
        return 1
    if not tickets:
        sys.stderr.write(
            f"[error] ticket 目录里一个 ticket 都没读到：{a.tickets}\n"
            f"        期望 <目录>/**/prompt.md，正文文件必须固定叫 prompt.md。\n"
            f"        零 ticket 绝不是「全过」，不给绿灯——check 是交 ②zhixing 前的闸门，\n"
            f"        还没拆 ticket 就还没到能过闸的时候。\n")
        return 1

    known = set(records)
    for tid, refs in tickets.items():
        bp = [r for f, r in refs if f == "blueprint_refs"]
        if not bp:
            warnings.append(f"[{tid}] blueprint_refs 为空——建在哪份权威上说不清"
                            f"（dag_lint C7 现已把它报成 error）")
        for field, r in refs:
            if not ANCHOR_RE.match(r):
                warnings.append(
                    f"[{tid}] {field} {r!r} 不是锚 id 形态。"
                    f"章节号/路径这类锚**会漂移**：蓝图插一节，所有后面的锚静默指向别的内容。"
                    f"改成 FLOW:/FIELD:/UI:/OWN:（或 ADR-NNNN）形态")
                continue
            if r not in known:
                errors.append(f"[{tid}] {field} {r} **悬空**——权威里没有这个锚 id")
                continue
            st = records[r].get("status", "active")
            if st == "superseded":
                errors.append(f"[{tid}] {field} 锚到已被取代的 {r}"
                              f"（→ {records[r].get('superseded_by', '?')}），需重新锚")
            elif st == "dropped":
                errors.append(f"[{tid}] {field} 锚到已废弃的 {r}——该 ticket 还该做吗？")

    # 权威内部：superseded_by 指向存在吗
    for aid, rec in records.items():
        sb = rec.get("superseded_by")
        if sb and sb not in known:
            errors.append(f"权威 {aid} 的 superseded_by={sb} 不存在")

    print(f"authority_check: {len(records)} 条权威 × {len(tickets)} 个 ticket")
    if errors:
        print(f"\n❌ ERROR ({len(errors)})：")
        for e in errors:
            print(f"  {e}")
    if warnings:
        print(f"\n🟡 WARNING ({len(warnings)})：")
        for w in warnings:
            print(f"  {w}")
    if not errors and not warnings:
        print("\n✅ 锚点全部有效、无悬空、无锚到废弃记录")
    return 1 if errors else (2 if warnings else 0)


def cmd_show(a):
    """精确取一条权威 —— 给 AI 用，免得为了一个字段把整份蓝图读进上下文。"""
    records, _ = load_all(a)
    rec = records.get(a.anchor)
    if rec is None:
        sys.stderr.write(f"[error] 锚 id 不存在: {a.anchor}\n")
        # 模糊匹配：子串 + 编辑距离。AI 打错锚 id 时必须给出路，
        # 否则它会退回「全文读蓝图」——那正是这套机制要消灭的行为。
        import difflib
        near = [k for k in records if a.anchor.lower() in k.lower()]
        near += [k for k in difflib.get_close_matches(a.anchor, list(records), n=8, cutoff=0.5)
                 if k not in near]
        if not near:                      # 同前缀的也列出来（至少告诉它这类有哪些）
            pref = a.anchor.split(":", 1)[0] + ":"
            near = [k for k in records if k.startswith(pref)][:8]
        if near:
            sys.stderr.write("        形似的有：\n")
            for k in near[:8]:
                sys.stderr.write(f"          {k}  {records[k].get('title','')}\n")
        else:
            sys.stderr.write(f"        （权威里共 {len(records)} 条，"
                             f"跑 `authority_lint.py check` 看全部锚）\n")
        return 1
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_render(a):
    """从 YAML 单向渲染人看的 markdown。产物别手改，下次 render 会覆盖。"""
    records, errors = load_all(a)
    if errors:
        for e in errors:
            sys.stderr.write(f"[error] {e}\n")
        return 1
    by_kind = {k: v for k, v in records.items()}
    records = {k: v for k, v in by_kind.items() if not k.startswith('REQ:')}   # REQ 有自己的清单文件，不重复渲染
    by_kind = {}
    for aid, rec in records.items():
        if rec.get("_parent"):
            continue                       # 子锚跟着父记录渲染
        by_kind.setdefault(aid.split(":", 1)[0], []).append((aid, rec))

    os.makedirs(a.out, exist_ok=True)
    titles = {"FLOW": "业务流权威图", "UI": "UI 权威索引",
              "OWN": "领域主权表", "FIELD": "字段表"}
    written = []
    for kind, items in sorted(by_kind.items()):
        lines = [f"# {titles.get(kind, kind)}", "",
                 "> 本文件由 `authority_lint.py render` 从 "
                 "`doc/authority/*.yaml` 自动生成。**别手改**——改权威改 YAML，"
                 "手改这里下次 render 就被覆盖。", ""]
        for aid, rec in sorted(items):
            st = rec.get("status", "active")
            flag = {"superseded": " ⚠️（已被取代）", "dropped": " ⛔（已废弃）"}.get(st, "")
            lines += [f"## {rec.get('title', aid)}{flag}", "",
                      f"**锚 id**：`{aid}`（ticket 的 blueprint_refs 写这个）"]
            if rec.get("superseded_by"):
                lines.append(f"**已被取代**：→ `{rec['superseded_by']}`")
            for k, label in (("owner_system", "归属系统"), ("authority_system", "唯一写入方"),
                             ("reader_systems", "只读方"), ("sync", "同步方向"),
                             ("business_key", "业务键"), ("form_factor", "组件形态"),
                             ("prototype", "原型锚点")):
                if rec.get(k):
                    v = rec[k]
                    lines.append(f"**{label}**：{', '.join(v) if isinstance(v, list) else v}")
            if rec.get("related_reqs"):
                lines.append(f"**对应需求**：{', '.join(map(str, rec['related_reqs']))}")
            lines += ["", str(rec.get("body", "")).rstrip(), ""]
            if rec.get("steps"):
                lines += ["| 步 | 谁 | 做什么 | 写什么 | 产出 |", "|---|---|---|---|---|"]
                for s in rec["steps"]:
                    w = s.get("writes") or []
                    lines.append(
                        f"| `{s.get('id','')}` | {s.get('actor','')} | {s.get('action','')} "
                        f"| {', '.join(w) if isinstance(w, list) else w} | {s.get('produces','')} |")
                lines.append("")
        dest = os.path.join(a.out, f"{kind.lower()}.md")
        with open(dest, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        written.append(dest)
    for p in written:
        print(p)
    print(f"[ok] 渲染 {len(records)} 条权威 → {len(written)} 份 markdown", file=sys.stderr)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="authority_lint.py",
        description="权威变更传播链：snapshot / diff / impact / check / show / render")
    p.add_argument("--ws", default=None,
                   help="工作区根：含 authority/ tickets/ requirements.yaml 的那一层。"
                        "省略则按 $XUQIU_WORKDIR → 自动探测（cwd 拍平布局 / cwd/doc）"
                        "→ 兜底 doc/ 的顺序定位")
    p.add_argument("--authority", default=None,
                   help="权威目录（默认 <ws>/authority）")
    p.add_argument("--adr", default=None,
                   help="ADR 目录（默认 <authority>/../_adr；不存在则跳过）")
    p.add_argument("--tickets", default=None,
                   help="ticket 根目录（默认 <ws>/tickets）")
    p.add_argument("--req", default=None,
                   help="需求清单（默认 <ws>/requirements.yaml；每条也是一个 REQ: 锚）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="给当前权威打快照")
    s.add_argument("--force", action="store_true", help="有未消化变更时仍打快照")
    s.set_defaults(func=cmd_snapshot)

    sub.add_parser("diff", help="当前 vs 快照 → 变了哪些锚").set_defaults(func=cmd_diff)

    i = sub.add_parser("impact", help="变更锚反查 → 受影响 ticket")
    i.add_argument("--apply", action="store_true")
    i.set_defaults(func=cmd_impact)

    sub.add_parser("check", help="悬空引用 / 漏锚 / 锚到废弃记录").set_defaults(func=cmd_check)

    sh = sub.add_parser("show", help="精确取一条权威（给 AI 用）")
    sh.add_argument("anchor")
    sh.set_defaults(func=cmd_show)

    r = sub.add_parser("render", help="从 YAML 单向渲染人看的 markdown")
    r.add_argument("--out", default=None,
                   help="输出目录（默认 <authority>/_rendered）")
    r.set_defaults(func=cmd_render)

    a = p.parse_args(argv)

    # ── 工作区根解析：显式参数优先，其余从根推导（见文件上方 resolve_ws）──
    ws = resolve_ws(a.ws)
    if a.authority is None:
        a.authority = os.path.join(ws, "authority")
    if a.tickets is None:
        a.tickets = os.path.join(ws, "tickets")
    if a.req is None:
        a.req = os.path.join(ws, "requirements.yaml")
    if getattr(a, "out", None) is None and hasattr(a, "out"):
        a.out = os.path.join(a.authority, "_rendered")

    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
