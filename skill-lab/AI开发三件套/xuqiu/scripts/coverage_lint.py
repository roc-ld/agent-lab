#!/usr/bin/env python3
"""coverage_lint.py — 需求覆盖率双向校验（「确认每一个需求点」的机器闸门）。

回答两个 dag_lint 回答不了的问题：
  ① **漏拆**：甲方提的哪几条需求，没有任何 ticket 认领？
  ② **镀金**：哪些 ticket 不对应任何需求（自己加的戏 / req_refs 写错了）？

一期教训：蓝图三件套锚的是「实现结构」，`blueprint_refs` 能回答「蓝图改了影响
哪些 ticket」，回答不了「甲方需求文档某一行到底拆没拆」。66 ticket 那次是靠人
扫一遍 —— 二期需求基线 106 条、跨两个系统，人扫必漏。

═══════════════════════════════════════════════════════════════════════════
契约
═══════════════════════════════════════════════════════════════════════════
输入：
  --req      doc/requirements.yaml   REQ 清单（格式见 templates/requirements.schema.yaml）
  --tickets  doc/tickets             ticket 根目录（doc/tickets/<id>/prompt.md）

输出：
  - stdout 人类可读报告：覆盖率 + 漏拆清单 + 镀金清单 + 每域分布
  - --write-back：把每条 REQ 的 tickets 字段回写成实际认领它的 ticket 列表
    （单向：ticket.req_refs 是真相源，REQ.tickets 是派生视图，永远不手填）
  - --json <path>：机器可读结果，供 _manifest.json 汇总

退出码：0 全覆盖可拆 / 1 有漏拆或镀金（error）/ 2 仅 warning（有 clarify 待澄清）

判定口径（与 requirements.schema.yaml 状态机一致）：
  status=pending  且无 ticket 认领  → ❌ error「漏拆」
  status=clarify                    → 🟡 warning「待澄清」，不计漏拆（进确认单提问区）
  status∈{deferred,dropped}         → ✅ 不计入分母（本期不做）
  ticket.req_refs 为空              → ❌ error「无源 ticket」
  ticket.req_refs 指向不存在的 REQ  → ❌ error「悬空引用」

**为什么 pending 无认领是 error 不是 warning**：漏拆是这一层唯一要拦的事故。
降成 warning 就会像 dag_lint 的 C5 一样被无视，这层校验白建。

依赖：优先 pyyaml；不可用时走内置极简解析（够解析本 schema 的扁平结构）。
"""

import argparse
import glob
import json
import os
import re
import sys


# ═══════════════════════════════════════════════════════════════════════════
# 解析
# ═══════════════════════════════════════════════════════════════════════════
def load_requirements(path):
    """读 requirements.yaml → [{id, status, priority, domain, module, name, tickets}, ...]。

    pyyaml 缺席时走正则分块解析：REQ 条目结构固定（`  - id:` 起一条，同缩进
    `    key: value`），不需要通用 YAML 引擎。text 是块标量，本脚本不消费其内容，
    只在报告里取首行做提示，所以不必完整还原。
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(text) or {}
        reqs = []
        for r in (data.get("requirements") or []):
            if not isinstance(r, dict) or not r.get("id"):
                continue
            reqs.append({
                "id": str(r["id"]).strip(),
                "status": str(r.get("status") or "pending").strip(),
                "priority": str(r.get("priority") or "").strip(),
                "domain": str(r.get("domain") or "").strip(),
                "module": str(r.get("module") or "").strip(),
                "name": str(r.get("name") or "").strip(),
                "text": str(r.get("text") or "").strip(),
                "source": str(r.get("source") or "").strip(),
                "defer_to": str(r.get("defer_to") or "").strip(),
                "decided_by": str(r.get("decided_by") or "").strip(),
                "decided_text_sha": str(r.get("decided_text_sha") or "").strip(),
            })
        return reqs
    except ImportError:
        return _parse_reqs_fallback(text)


def _parse_reqs_fallback(text):
    """pyyaml 缺席兜底：按 `  - id:` 切块，块内取扁平 key。"""
    reqs = []
    blocks = re.split(r"^  - id:", text, flags=re.M)[1:]
    for blk in blocks:
        rid = blk.splitlines()[0].strip()
        if not rid:
            continue
        rec = {"id": rid, "status": "pending", "priority": "", "domain": "",
               "module": "", "name": "", "text": "", "source": "", "defer_to": ""}
        # 注意：decided_text_sha **刻意不进 fallback**——fallback 的 text 是截断的，
        # 用它算 sha 必然误报「口径被改动」。sha 漂移检查只在 pyyaml 全量解析下生效。
        for key in ("status", "priority", "domain", "module", "name", "source", "defer_to",
                    "decided_by"):
            m = re.search(rf"^    {key}:\s*(.+)$", blk, re.M)
            if m:
                rec[key] = m.group(1).strip()
        m = re.search(r"^    text: >\s*\n((?:      .*\n)+)", blk, re.M)
        if m:
            rec["text"] = " ".join(ln.strip() for ln in m.group(1).splitlines())
        reqs.append(rec)
    return reqs


def load_ticket_reqrefs(tickets_dir):
    """扫 doc/tickets/**/prompt.md，返回 (refs_map, parse_errors)。

    refs_map = {ticket_id: [req_id, ...]}；parse_errors = [{"ticket","msg"}]。

    ⚠️ **必须与 dag_lint 用同一套 YAML 方言**。历史版本这里是手写行扫描，只认块式
    `req_refs:\\n  - A`，看不见合法的行内式 `req_refs: [A, B]` —— 于是同一份 ticket
    被 dag_lint 判"req_refs 齐"、被本脚本判"无源 ticket + 那几条 REQ 漏拆"，
    **同时制造假阳性和假阴性**。两个 lint 对同一文件给相反结论，人就会开始
    无视覆盖率闸门的红色输出。所以：优先 pyyaml，缺席才走 fallback。
    """
    out, errors = {}, []
    # 递归扫：ticket 可能按系统/域分组放子目录（doc/tickets/store/<id>/prompt.md）
    for md in sorted(glob.glob(os.path.join(tickets_dir, "**", "prompt.md"),
                               recursive=True)):
        tid = os.path.basename(os.path.dirname(md))
        try:
            with open(md, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            errors.append({"ticket": tid, "msg": f"读文件失败: {e}"})
            continue
        if not lines or lines[0].strip() != "---":
            errors.append({"ticket": tid, "msg": "缺 front-matter（文件未以 --- 开头）"})
            out[tid] = []
            continue
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            errors.append({"ticket": tid, "msg": "front-matter 未闭合（缺第二个 ---）"})
            out[tid] = []
            continue
        block = "\n".join(lines[1:end])
        fm, err = _parse_fm(block)
        if err:
            # 一份坏文件不该毁掉全量报告 —— 记一条 error 继续扫别的
            errors.append({"ticket": tid, "msg": err})
            out[tid] = []
            continue
        tid = str(fm.get("ticket") or tid)
        refs = fm.get("req_refs") or []
        if not isinstance(refs, list):
            errors.append({"ticket": tid, "msg": "req_refs 不是 list"})
            refs = []
        if tid in out:
            errors.append({"ticket": tid, "msg": f"ticket id 重复（{md}）—— "
                                                 f"id 必须全局唯一，否则调度器派错文件"})
        out[tid] = [str(r).strip() for r in refs]
    return out, errors


def _parse_fm(block):
    """解析 front-matter → (dict, err)。优先 pyyaml，缺席走扁平 fallback。"""
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(block)
        if not isinstance(data, dict):
            return {}, "front-matter 不是 mapping"
        return data, None
    except ImportError:
        pass
    except Exception as e:                      # ScannerError 等：别让一份坏文件崩掉全量
        return {}, f"YAML 解析失败: {e}"
    # fallback：顶层标量 + 块式 list + 行内 list（够 front-matter 用）
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
    return out, None


# ═══════════════════════════════════════════════════════════════════════════
# 校验
# ═══════════════════════════════════════════════════════════════════════════
NOT_COUNTED = {"deferred", "dropped"}     # 本期不做，不计入覆盖率分母


VALID_PRIORITIES = {"P0", "P1", "P2"}
MAX_REFS_PER_TICKET = 8      # 单 ticket 认领超过这么多 REQ → 疑似"一个 ticket 吞下全部"
MAX_TICKETS_PER_REQ = 4      # 一条 REQ 被这么多 ticket 认领 → 疑似 req_refs 乱贴


# ═══════════════════════════════════════════════════════════════════════════
# 工作区根定位 —— 与 authority_lint.py 的同名函数保持同步（逻辑只有十几行；
# 三份脚本各自独立可跑优先于抽公共模块，改动时三处一起改）
# ═══════════════════════════════════════════════════════════════════════════
# 支持两种真实布局：<root>/doc/{requirements.yaml,tickets/}（标准）
#             与 <root>/{requirements.yaml,tickets/}（拍平，doc-phaseN 多阶段项目）
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


def analyze(reqs, ticket_refs):
    """返回 result_dict。覆盖关系 = ticket.req_refs 反向索引。"""
    by_id = {}
    dup_ids = []
    for r in reqs:
        if r["id"] in by_id:
            dup_ids.append(r["id"])          # 重复 id 会让分子分母双重计，覆盖率虚高
        by_id[r["id"]] = r
    claimed = {}                                   # req_id -> [ticket_id]
    dangling = []                                  # ticket 引用了不存在的 REQ
    orphan_tickets = []                            # ticket 没写 req_refs

    banned_refs = []                               # ticket 引用了「明确不做」的 REQ
    for tid, refs in ticket_refs.items():
        if not refs:
            orphan_tickets.append(tid)
            continue
        for rid in refs:
            if rid not in by_id:
                dangling.append({"ticket": tid, "req": rid})
                continue
            if by_id[rid]["status"] in NOT_COUNTED:
                # 范围膨胀检测：做了明确不做的事。要做先把 REQ 状态改回并走 change-log
                banned_refs.append({"ticket": tid, "req": rid,
                                    "status": by_id[rid]["status"]})
                continue
            claimed.setdefault(rid, []).append(tid)

    # ── 质量校验：这些不查，闸门就能被"合法地"绕过 ──────────────────────────
    bad_priority = [r for r in reqs
                    if r["status"] not in NOT_COUNTED and r["priority"] not in VALID_PRIORITIES]
    # deferred 必须写 defer_to，否则 status 就是个无门槛的消音开关
    bad_defer = [r for r in reqs if r["status"] == "deferred" and not r.get("defer_to")]
    # 绊线：clarify 是「待拍板」，decided_by 是「拍板 = 永远只属于 负责人」的签名——
    # 两者并存 = AI 默认口径被悄悄写成了终局（solo 模式最容易发生）。
    # 如实标注：这是绊线不是完备防护，真正的保障是「写 decided_* 的只能是 负责人」这条约定。
    clarify_decided = [r for r in reqs
                       if r["status"] == "clarify" and r.get("decided_by")]
    # 决策终局闸（批 3）：已拍板口径（decided_text_sha 记录拍板时的 text 指纹）被改动
    # 而没走 CR（没更新 sha）→ error。防「甲方 W3 改成 B、W6 又想回 A」这类反复打架
    # 无人发现——改可以，但必须显式走 change-log + 重签。
    import hashlib as _hl
    decided_drift = [r for r in reqs if r.get("decided_text_sha")
                     and _hl.sha256(r.get("text", "").strip().encode()).hexdigest()[:8]
                     != r["decided_text_sha"]]
    fat_tickets = [{"ticket": t, "n": len(set(rs))}
                   for t, rs in ticket_refs.items() if len(set(rs)) > MAX_REFS_PER_TICKET]
    spread_reqs = [{"req": rid, "n": len(set(ts))}
                   for rid, ts in claimed.items() if len(set(ts)) > MAX_TICKETS_PER_REQ]

    # 去重后再算分母：重复 id 只算一条
    seen, in_scope = set(), []
    for r in reqs:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        if r["status"] not in NOT_COUNTED:
            in_scope.append(r)
    covered, missing, clarify = [], [], []
    for r in in_scope:
        tids = claimed.get(r["id"], [])
        if tids:
            covered.append(r["id"])
        elif r["status"] == "clarify":
            clarify.append(r)
        else:
            missing.append(r)

    denom = len(in_scope)
    rate = (len(covered) / denom) if denom else 1.0

    # 按域统计，方便看哪个域拆得不够
    per_domain = {}
    for r in in_scope:
        d = r["domain"] or "(未分域)"
        slot = per_domain.setdefault(d, {"total": 0, "covered": 0, "missing": 0, "clarify": 0})
        slot["total"] += 1
        if r["id"] in covered:
            slot["covered"] += 1
        elif r["status"] == "clarify":
            slot["clarify"] += 1
        else:
            slot["missing"] += 1

    return {
        "total_reqs": len(seen),
        "in_scope": denom,
        "out_of_scope": len(seen) - denom,
        "covered": len(covered),
        "coverage_rate": round(rate, 4),
        "missing": missing,
        "clarify": clarify,
        "dangling": dangling,
        "orphan_tickets": sorted(orphan_tickets),
        "per_domain": per_domain,
        "claimed": claimed,
        "tickets_scanned": len(ticket_refs),
        "dup_req_ids": sorted(set(dup_ids)),
        "bad_priority": bad_priority,
        "bad_defer": bad_defer,
        "banned_refs": banned_refs,
        "clarify_decided": clarify_decided,
        "decided_drift": decided_drift,
        "fat_tickets": fat_tickets,
        "spread_reqs": spread_reqs,
    }


def print_report(res):
    print(f"coverage_lint: {res['tickets_scanned']} 个 ticket × {res['total_reqs']} 条需求")
    print(f"  本期在范围: {res['in_scope']}  (本期不做 {res['out_of_scope']} 条已排除)")
    pct = res["coverage_rate"] * 100
    print(f"  已覆盖: {res['covered']}/{res['in_scope']}  = {pct:.1f}%")

    if res["per_domain"]:
        print("\n  按域分布：")
        for d, s in sorted(res["per_domain"].items()):
            flag = "❌" if s["missing"] else ("🟡" if s["clarify"] else "✅")
            print(f"    {flag} {d:<12} 覆盖 {s['covered']}/{s['total']}"
                  f"  漏拆 {s['missing']}  待澄清 {s['clarify']}")

    if res["missing"]:
        print(f"\n❌ 漏拆 ({len(res['missing'])}) — 甲方提了但没有任何 ticket 认领：")
        for r in res["missing"]:
            label = r["name"] or (r["text"][:30] + "…" if len(r["text"]) > 30 else r["text"])
            print(f"  [{r['id']}] {r['module']} / {label}")
            print(f"      原文出处: {r['source']}")

    if res["dangling"]:
        print(f"\n❌ 悬空引用 ({len(res['dangling'])}) — ticket 指向不存在的 REQ：")
        for d in res["dangling"]:
            print(f"  [{d['ticket']}] req_refs → {d['req']}（清单里没有这个 id）")

    if res["orphan_tickets"]:
        print(f"\n❌ 无源 ticket ({len(res['orphan_tickets'])}) — 没写 req_refs，"
              f"说不清在满足甲方哪条需求（镀金嫌疑）：")
        for t in res["orphan_tickets"]:
            print(f"  [{t}]")

    if res["dup_req_ids"]:
        print(f"\n❌ REQ id 重复 ({len(res['dup_req_ids'])}) — 分子分母双重计，覆盖率会虚高：")
        for rid in res["dup_req_ids"]:
            print(f"  [{rid}] 出现多次（id 必须全局唯一且终身不变）")

    if res["bad_priority"]:
        print(f"\n❌ priority 未填或非法 ({len(res['bad_priority'])}) — "
              f"必须是 P0/P1/P2（占位符 <P0|P1|P2> 也算没填）：")
        print(f"  priority 是后面编任务 D1-Dn 的唯一依据，不填等于任务计划没根据。")
        for r in res["bad_priority"][:15]:
            print(f"  [{r['id']}] priority={r['priority']!r}")
        if len(res["bad_priority"]) > 15:
            print(f"  …… 另 {len(res['bad_priority']) - 15} 条")

    if res["bad_defer"]:
        print(f"\n❌ deferred 未写 defer_to ({len(res['bad_defer'])}) — "
              f"不写就是无门槛的消音开关：")
        for r in res["bad_defer"]:
            print(f"  [{r['id']}] {r['module']} / {r['name'] or r['text'][:24]}")

    if res["banned_refs"]:
        print(f"\n❌ 做了明确不做的事 ({len(res['banned_refs'])}) — "
              f"ticket 引用了 deferred/dropped 的 REQ（范围膨胀）：")
        print(f"  规则 4 范围写死：要做先把 REQ 状态改回 pending 并在 change-log 留一行，"
              f"别让范围悄悄长回来。")
        for b in res["banned_refs"]:
            print(f"  [{b['ticket']}] req_refs → {b['req']}（status={b['status']}）")

    if res["clarify_decided"]:
        print(f"\n❌ clarify 与 decided_by 并存 ({len(res['clarify_decided'])}) — "
              f"「待拍板」的口径被写上了拍板签名：")
        print(f"  两种情况都要修：真拍板了 → status 改掉；没拍板 → decided_by 清空"
              f"（AI 默认口径不许署名成终局，拍板永远只属于 负责人）。")
        for r in res["clarify_decided"]:
            print(f"  [{r['id']}] decided_by={r['decided_by']!r}")

    if res.get("decided_drift"):
        print(f"\n❌ 已拍板口径被改动、未走 CR ({len(res['decided_drift'])}) — "
              f"text 与拍板时的 decided_text_sha 对不上：")
        print(f"  改可以，但必须显式走 change-log 重签（更新 decided_* 四字段）——"
              f"否则「W3 改成 B、W6 又想回 A」这类反复打架无人发现。")
        for r in res["decided_drift"]:
            print(f"  [{r['id']}] 拍板 sha={r['decided_text_sha']}（{r['decided_by']} 签）")

    if res["fat_tickets"]:
        print(f"\n🟡 单 ticket 认领过多 REQ ({len(res['fat_tickets'])}) — "
              f"覆盖率会被一个大 ticket 刷成 100% 但没真拆：")
        for f in res["fat_tickets"]:
            print(f"  [{f['ticket']}] 认领 {f['n']} 条（阈值 {MAX_REFS_PER_TICKET}）")

    if res["spread_reqs"]:
        print(f"\n🟡 单条 REQ 被过多 ticket 认领 ({len(res['spread_reqs'])}) — "
              f"疑似 req_refs 乱贴：")
        for s in res["spread_reqs"]:
            print(f"  [{s['req']}] 被 {s['n']} 个 ticket 认领（阈值 {MAX_TICKETS_PER_REQ}）")

    hard = (res["missing"] or res["dangling"] or res["orphan_tickets"]
            or res["dup_req_ids"] or res["bad_priority"] or res["bad_defer"])
    if not hard:
        soft = res["clarify"] or res["fat_tickets"] or res["spread_reqs"]
        if soft:
            print("\n🟡 无漏拆无镀金，但有待澄清 / 可疑项 —— "
                  "**澄清项必须真澄清完**（不是标了 clarify 就算过）才进 ticket 拆解。")
        else:
            print("\n✅ 需求全覆盖、无镀金、priority 齐，可交 ②zhixing 排任务。")


def write_back(req_path, claimed):
    """把 REQ.tickets 回写成实际认领它的 ticket 列表（派生视图，单向）。

    只改 `    tickets: [...]` 那一行，不动其余内容 —— 避免 YAML 重序列化把
    人写的注释/块标量格式冲掉（这份清单是人要读的）。
    """
    with open(req_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    cur = None
    changed = 0
    for i, ln in enumerate(lines):
        m = re.match(r"^  - id:\s*(\S+)", ln)
        if m:
            cur = m.group(1)
            continue
        if cur and re.match(r"^    tickets:", ln):
            tids = sorted(claimed.get(cur, []))
            new = f"    tickets: [{', '.join(tids)}]" if tids else "    tickets: []"
            if new != ln:
                lines[i] = new
                changed += 1
            cur = None
    with open(req_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return changed


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="coverage_lint.py",
        description="需求覆盖率双向校验：查漏拆（REQ 无 ticket）+ 查镀金（ticket 无 REQ）",
    )
    p.add_argument("--ws", default=None,
                   help="工作区根：含 requirements.yaml / tickets/ 的那一层。"
                        "省略则按 $XUQIU_WORKDIR → 自动探测 → 兜底 doc/ 定位")
    p.add_argument("--req", default=None,
                   help="REQ 清单路径（默认 <ws>/requirements.yaml）")
    p.add_argument("--tickets", default=None,
                   help="ticket 根目录（默认 <ws>/tickets）")
    p.add_argument("--write-back", action="store_true",
                   help="把每条 REQ 的 tickets 字段回写成实际认领者（派生视图）")
    p.add_argument("--json", dest="json_out", help="额外输出机器可读结果到此路径")
    a = p.parse_args(argv)

    ws = resolve_ws(a.ws)
    if a.req is None:
        a.req = os.path.join(ws, "requirements.yaml")
    if a.tickets is None:
        a.tickets = os.path.join(ws, "tickets")

    if not os.path.exists(a.req):
        sys.stderr.write(f"[error] REQ 清单不存在: {a.req}\n"
                         f"        先跑 <xuqiu>/scripts/req_extract.py 从甲方文档抽一份；\n"
                         f"        多阶段项目传 --ws doc-phaseN 或设 $XUQIU_WORKDIR\n")
        return 1
    if not os.path.isdir(a.tickets):
        sys.stderr.write(f"[error] tickets 目录不存在: {a.tickets}\n")
        return 1

    try:
        reqs = load_requirements(a.req)
    except Exception as e:
        sys.stderr.write(f"[error] {a.req} 解析失败: {e}\n"
                         f"        （常见原因：module/name 里带冒号等 YAML 特殊字符）\n")
        return 1
    if not reqs:
        sys.stderr.write(f"[error] {a.req} 里没解析出任何 REQ —— 格式对不上？\n")
        return 1
    ticket_refs, parse_errors = load_ticket_reqrefs(a.tickets)
    if not ticket_refs:
        sys.stderr.write(
            f"[error] {a.tickets} 下没扫到任何 ticket —— 期望 <目录>/**/prompt.md。\n"
            f"        正文文件必须固定叫 prompt.md（叫别的名不会被扫到）。\n"
            f"        零 ticket 绝不是「全覆盖」，不给绿灯。\n")
        return 1
    if parse_errors:
        print(f"❌ ticket 解析失败 ({len(parse_errors)}) — 这些 ticket 的 req_refs 读不到：")
        for e in parse_errors:
            print(f"  [{e['ticket']}] {e['msg']}")
        print()
    res = analyze(reqs, ticket_refs)
    res["parse_errors"] = parse_errors
    print_report(res)

    if a.write_back:
        n = write_back(a.req, res["claimed"])
        print(f"\n[write-back] 更新了 {n} 条 REQ 的 tickets 字段")

    if a.json_out:
        payload = {k: v for k, v in res.items() if k != "claimed"}
        payload["missing"] = [r["id"] for r in res["missing"]]
        payload["clarify"] = [r["id"] for r in res["clarify"]]
        payload["status"] = ("fail" if (res["missing"] or res["dangling"]
                                        or res["orphan_tickets"] or res["dup_req_ids"]
                                        or res["bad_priority"] or res["bad_defer"]
                                        or res["banned_refs"] or res["clarify_decided"]
                                        or res.get("decided_drift")
                                        or res["parse_errors"]) else "pass")
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    hard = (res["missing"] or res["dangling"] or res["orphan_tickets"]
            or res["dup_req_ids"] or res["bad_priority"] or res["bad_defer"]
            or res["banned_refs"] or res["clarify_decided"]
            or res.get("decided_drift")
            or res["parse_errors"])
    if hard:
        return 1
    return 2 if (res["clarify"] or res["fat_tickets"] or res["spread_reqs"]) else 0


if __name__ == "__main__":
    sys.exit(main())
