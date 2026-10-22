#!/usr/bin/env python3
"""req_extract.py — 从甲方结构化需求文档抽 REQ 清单骨架。

三件套 ①xuqiu 的第一步工具。把甲方给的 xlsx/csv 需求表**逐行**变成带 id 的
REQ 条目，落 doc/requirements.yaml —— 这是「确认每一个需求点」从「人扫一遍」
变成「脚本可查」的起点。

不做的事（刻意）：
  - 不解读、不提炼、不合并相似需求 —— 原文照抄进 text。提炼会丢掉甲方措辞，
    而歧义恰恰藏在措辞里。解读是 AI 读完之后手工补 note，不是抽取阶段干的。
  - 不判优先级 —— priority 留空由人/AI 补，脚本猜不准。
  - 不填 tickets —— 那是 coverage_lint.py 反向算的。

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
  # 1. 先看表长什么样（不落盘，打印每个 sheet 的前几行 + 列号）
  python3 req_extract.py --src 需求.xlsx --inspect

  # 2. 按看到的列布局抽取（列号从 1 起，对齐 Excel 的 A=1）
  python3 req_extract.py --src 需求.xlsx --sheet 门店系统 \
      --module-col 3 --text-col 4 --domain STORE --prefix REQ-STORE \
      --out doc/requirements.yaml

  # 3. 多 sheet 追加到同一份清单（--append 不覆盖已有条目，id 从最大号续）
  python3 req_extract.py --src 需求.xlsx --sheet 官网 \
      --module-col 3 --text-col 4 --domain SITE --prefix REQ-SITE \
      --out doc/requirements.yaml --append

契约：
  - 输出格式见 templates/requirements.schema.yaml（那份是唯一真相源）。
  - **空单元格向下填充**：甲方表常把分组列只写在首行（如「库存管理」占 7 行只填第 1 行），
    脚本对 module-col 做 forward-fill，否则 6 条需求的 module 会是空。
  - source 写成 `<文件名>#<sheet>!<Excel列><行号>`，可直接回原表定位。
  - **幂等**：同一输入重跑产出一致。--append 时按 source 去重，已存在的行不重复加。
  - 纯 stdlib（zipfile + xml.etree 手解析 xlsx），不装 openpyxl/pandas。

退出码：0 成功 / 1 参数或解析错 / 2 抽出 0 条（八成是列号指错了，别静默当成功）。
"""

from __future__ import annotations   # 兼容 Python 3.9（PEP 604 注解）

import argparse
import csv
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ═══════════════════════════════════════════════════════════════════════════
# xlsx 读取（纯 stdlib）
# ═══════════════════════════════════════════════════════════════════════════
def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    """读 sharedStrings.xml。xlsx 把重复文本存这里，单元格只存下标。"""
    try:
        raw = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    # 一个 <si> 可能被拆成多个 <t>（富文本分段），拼起来才是完整字符串。
    return ["".join(t.text or "" for t in si.iter(NS + "t")) for si in root]


def _sheet_map(z: zipfile.ZipFile) -> dict[str, str]:
    """sheet 名 → worksheets/sheetN.xml 路径（按 workbook.xml.rels 的 r:id 精确映射）。"""
    wb = z.read("xl/workbook.xml").decode("utf-8")
    # ⚠️ 必须走 rels 映射：workbook 里 sheet 的出现顺序不保证等于 sheet1/sheet2 的编号。
    #    Excel/WPS/Numbers 删表重存后编号错位很常见，猜顺序会抽出**完整但错域**的清单，
    #    且 source 指向的单元格里根本没有那行字（"甲方质疑时能翻出来"的保证就破了）。
    pairs = re.findall(r'<sheet[^>]*\bname="([^"]+)"[^>]*r:id="([^"]+)"', wb)
    if not pairs:   # 有些导出把 r:id 写在 name 前面
        pairs = [(m[1], m[0]) for m in
                 re.findall(r'<sheet[^>]*r:id="([^"]+)"[^>]*\bname="([^"]+)"', wb)]
    try:
        rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        id2target = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
    except KeyError:
        id2target = {}
    out = {}
    for name, rid in pairs:
        tgt = id2target.get(rid, "")
        if tgt:
            out[name] = "xl/" + tgt.lstrip("/").replace("worksheets/", "worksheets/", 1)
    if out:
        return out
    # rels 缺失才回退到顺序假设，并明确告警
    names = re.findall(r'<sheet[^>]*\bname="([^"]+)"', wb)
    sys.stderr.write("[warn] 读不到 workbook.xml.rels，回退按顺序猜 sheet 编号——"
                     "若抽出的内容与 --sheet 指定的表对不上，就是这里错了\n")
    return {n: f"xl/worksheets/sheet{i}.xml" for i, n in enumerate(names, 1)}


def _col_index(ref: str) -> int:
    """单元格引用 'C12' → 列号 3（1-based，对齐 Excel 的 A=1）。"""
    letters = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n


def read_xlsx(path: Path, sheet: str | None) -> dict[str, list[tuple[int, list[str]]]]:
    """读 xlsx → {sheet名: [(excel行号, [单元格文本...]), ...]}，单元格按列号补齐。"""
    z = zipfile.ZipFile(path)
    ss = _shared_strings(z)
    smap = _sheet_map(z)
    targets = [sheet] if sheet else list(smap)
    out: dict[str, list[tuple[int, list[str]]]] = {}
    for name in targets:
        if name not in smap:
            raise ValueError(f"sheet 不存在: {name!r}，可选: {list(smap)}")
        root = ET.fromstring(z.read(smap[name]))
        rows = []
        for row in root.iter(NS + "row"):
            cells: list[str] = []
            for c in row.iter(NS + "c"):
                idx = _col_index(c.get("r") or "A1")
                while len(cells) < idx - 1:      # 空单元格在 xml 里直接缺席，补位
                    cells.append("")
                v = c.find(NS + "v")
                if v is None or v.text is None:
                    # inlineStr 形态（少见）：文本直接内嵌在 <is><t>
                    txt = "".join(t.text or "" for t in c.iter(NS + "t"))
                else:
                    txt = ss[int(v.text)] if c.get("t") == "s" else v.text
                cells.append((txt or "").strip())
            rows.append((int(row.get("r") or 0), cells))
        out[name] = rows
    return out


def read_csv(path: Path) -> dict[str, list[tuple[int, list[str]]]]:
    """读 csv → 与 read_xlsx 同形状（单 sheet，名字用文件名）。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [(i, [c.strip() for c in r]) for i, r in enumerate(csv.reader(f), 1)]
    return {path.stem: rows}


# ═══════════════════════════════════════════════════════════════════════════
# 抽取
# ═══════════════════════════════════════════════════════════════════════════
def cell(cells: list[str], col: int) -> str:
    """取第 col 列（1-based），越界返回空串。"""
    return cells[col - 1] if 0 < col <= len(cells) else ""


def extract(rows, *, module_col, text_col, name_col, header_rows, forward_fill=True):
    """逐行抽 (excel行号, module, name, text)。

    甲方表常见三层结构：分组列（如「库存管理」）/ 需求名列（如「按份库存核算」）/
    描述列。三层都留住——分组用来给确认单分节，需求名是甲方认得的短标签，
    描述是原文正文。name_col=0 表示表里没有这一层。

    forward_fill：module 列为空时沿用上一行的值 —— 甲方表普遍把分组列只写在
    首行（「库存管理」占 7 行只填第 1 行），不填充会让 6 条需求 module 为空。
    text 列**不填充**：text 为空 = 这行不是需求行（分隔行/小计行），直接跳过。
    """
    items = []
    last_module = ""
    for rno, cells in rows:
        if rno <= header_rows:
            continue
        text = cell(cells, text_col)
        module = cell(cells, module_col)
        name = cell(cells, name_col) if name_col else ""
        if forward_fill:
            if module:
                last_module = module
            else:
                module = last_module
        if not text:
            continue                      # 无描述 = 不是需求行
        items.append((rno, module, name, text))
    return items


def col_letter(n: int) -> str:
    """列号 3 → 'C'（source 里回写 Excel 风格定位用）。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def yaml_block(text: str, indent: str) -> str:
    """把原文渲染成 YAML 块标量（`>` 折叠式），免去引号转义。

    原文含冒号/引号/换行都安全；多行按 indent 缩进。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(indent + ln for ln in lines) if lines else indent + "(空)"


def render(items, *, prefix, domain, src_name, sheet, text_col, start_no):
    """渲染成 requirements.yaml 的 requirements 条目串。"""
    out = []
    for i, (rno, module, name, text) in enumerate(items, start_no):
        rid = f"{prefix}-{i:03d}"
        src = f"{src_name}#{sheet}!{col_letter(text_col)}{rno}"
        # ⚠️ 一律走 JSON 串：客户表里的「POS: 收银与支付」「盘点: 差异处理」直接拼进 YAML
        #    会产出**语法非法**的 requirements.yaml，而抽取还报 [ok]，下游 coverage_lint 才崩。
        #    JSON 字符串是合法的 YAML 标量，冒号/引号/中括号全部安全。
        q = lambda v: json.dumps(v, ensure_ascii=False)
        name_line = f"    name: {q(name)}\n" if name else ""
        out.append(
            f"  - id: {rid}\n"
            f"    source: {q(src)}\n"
            f"    module: {q(module or '(未分组)')}\n"
            f"{name_line}"
            f"    text: >\n{yaml_block(text, '      ')}\n"
            f"    domain: {domain}\n"
            f"    priority: <P0|P1|P2>          # TODO(人补): 脚本不猜优先级\n"
            f"    status: pending\n"
            f"    tickets: []\n"
        )
    return "\n".join(out)


HEADER_TMPL = """# 需求点清单（REQ 清单）—— 由 scripts/req_extract.py 抽取骨架，人工补 priority/note
# 格式权威源：templates/requirements.schema.yaml
# 校验：python3 scripts/coverage_lint.py --req doc/requirements.yaml --tickets doc/tickets
#
# ⚠️ 抽取只保证「一行原文 = 一条 REQ，一条不漏」。以下三件是脚本干不了、必须人/AI 补的：
#    1. priority —— 每条补 P0/P1/P2
#    2. note     —— 解读、与其他 REQ 的关系、实现要点
#    3. clarify  —— 有歧义的改 status: clarify 并写清要问甲方什么

meta:
  project: <项目名>
  baseline:
    - file: {src}
      version: <导出日期或版本号>
      note: <这份文件覆盖哪部分需求>
  domains: [{domain}]

requirements:

"""


def parse_existing(path: Path, prefix: str):
    """读已有 requirements.yaml，返回 (全文, 已有 source 集合, 该 prefix 的最大序号)。

    不引 pyyaml：--append 只需要「已有哪些 source」和「号排到几」，正则够用且
    不会因为 pyyaml 缺席而不可用。
    """
    if not path.exists():
        return "", set(), 0
    text = path.read_text(encoding="utf-8")
    sources = set(re.findall(r"^\s*source:\s*['\"]?([^'\"\s]+)", text, re.M))
    nums = [int(m) for m in re.findall(rf"^\s*-?\s*id:\s*{re.escape(prefix)}-(\d+)", text, re.M)]
    return text, sources, max(nums, default=0)


# ═══════════════════════════════════════════════════════════════════════════
# --merge：甲方送来新版基线时的增量合并
# ═══════════════════════════════════════════════════════════════════════════
def _norm(s):
    """归一化用于比对：去空白、去常见标点，避免"改了个顿号"被当成改需求。"""
    return re.sub(r"[\s，,、。.；;：:（）()【】\[\]\"'`*]+", "", str(s or ""))


def load_existing_reqs(path: Path):
    """读已有清单 → [{id, module, name, text, source, _block}]，_block 是原始文本块。

    用块级解析而非 pyyaml：合并要**原地改一行**并保留人写的 note/clarify/priority，
    整份 YAML 重序列化会把这些格式冲掉（这份清单是人要读、要在上面写决策的）。
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    out = []
    parts = re.split(r"(?m)^(?=  - id:)", text)
    for blk in parts[1:]:
        m = re.match(r"\s*- id:\s*(\S+)", blk)
        if not m:
            continue
        rec = {"id": m.group(1), "_block": blk}
        for k in ("module", "name", "source", "status"):
            mm = re.search(rf"^    {k}:\s*(.+)$", blk, re.M)
            rec[k] = mm.group(1).strip().strip('"') if mm else ""
        mt = re.search(r"^    text: >\s*\n((?:      .*\n)+)", blk, re.M)
        rec["text"] = " ".join(l.strip() for l in mt.group(1).splitlines()) if mt else ""
        out.append(rec)
    return out


def match_existing(item, existing, used):
    """把新抽出的一行匹配到已有 REQ。返回 (rec|None, 匹配依据)。

    匹配优先级（从稳到糙）：
      1. module + name 完全一致  —— 最稳：甲方改描述但不改功能板块名是最常见的
      2. 原文完全一致           —— 名字栏可能被改，但描述没动
      3. 原文相似度 ≥ 0.85      —— 措辞微调
      4. source 单元格一致       —— 最后兜底：**行会挪动**，所以放最后
    匹配不上 = 新需求。

    ⚠️ 故意不用 source 当主键：甲方在表中间插一行，后面所有单元格引用全部位移，
    按 source 匹配会把整批需求判成"全删了 + 全新增"，id 全换 → 所有 req_refs 悬空。
    """
    _, module, name, text = item
    for rec in existing:
        if rec["id"] in used:
            continue
        if name and rec.get("name") and _norm(rec["name"]) == _norm(name) \
                and _norm(rec.get("module")) == _norm(module):
            return rec, "板块+名称"
    for rec in existing:
        if rec["id"] not in used and _norm(rec.get("text")) == _norm(text):
            return rec, "原文一致"
    import difflib
    best, score = None, 0.0
    for rec in existing:
        if rec["id"] in used:
            continue
        r = difflib.SequenceMatcher(None, _norm(rec.get("text")), _norm(text)).ratio()
        if r > score:
            best, score = rec, r
    if best and score >= 0.85:
        return best, f"原文相似 {score:.0%}"
    return None, ""


def do_merge(a, items, sheet_name):
    """把新抽的行合并进已有清单。**只改 text，绝不改 id**。"""
    existing = load_existing_reqs(a.out)
    if not existing:
        sys.stderr.write(f"[error] --merge 需要已有清单，但 {a.out} 里没解析出 REQ。\n"
                         f"        首次抽取用 --out（不加 --merge）。\n")
        return 1

    text = a.out.read_text(encoding="utf-8")
    used, changed, added, unchanged = set(), [], [], 0
    max_no = max([int(m) for m in re.findall(
        rf"^\s*-?\s*id:\s*{re.escape(a.prefix)}-(\d+)", text, re.M)] or [0])

    for it in items:
        rno, module, name, new_text = it
        rec, how = match_existing(it, existing, used)
        if rec is None:
            max_no += 1
            added.append((f"{a.prefix}-{max_no:03d}", module, name, new_text, rno))
            continue
        used.add(rec["id"])
        if _norm(rec["text"]) == _norm(new_text):
            unchanged += 1
            continue
        # 原地替换 text 块，保留人写的 priority/note/clarify/tickets
        new_block = re.sub(
            r"(^    text: >\s*\n)((?:      .*\n)+)",
            lambda m: m.group(1) + yaml_block(new_text, "      ") + "\n",
            rec["_block"], count=1, flags=re.M)
        # source 跟着指向**新版**文件的单元格——source 的用途是"甲方质疑时当场翻出来"，
        # 指着旧文件就翻不到了。原始出处的追溯靠 git 历史，不靠这一行。
        new_src = json.dumps(f"{a.src.name}#{sheet_name}!{col_letter(a.text_col)}{rno}",
                             ensure_ascii=False)
        new_block = re.sub(r"^    source:.*$", f"    source: {new_src}",
                           new_block, count=1, flags=re.M)
        text = text.replace(rec["_block"], new_block, 1)
        changed.append((rec["id"], how, rec["text"][:34], new_text[:34]))

    if added:
        body = render(
            [(rno, module, name, txt) for _, module, name, txt, rno in added],
            prefix=a.prefix, domain=a.domain, src_name=a.src.name,
            sheet=sheet_name, text_col=a.text_col,
            start_no=int(added[0][0].rsplit("-", 1)[1]))
        text = text.rstrip("\n") + "\n\n" + body

    missing = [r for r in existing if r["id"] not in used]

    a.out.write_text(text, encoding="utf-8")

    print(f"[merge] 未变 {unchanged} · 原文改了 {len(changed)} · 新增 {len(added)} · "
          f"本次未出现 {len(missing)}")
    if changed:
        print(f"\n🔁 原文改了（id 保持不变，**下游 ticket 可能要重做**）：")
        for rid, how, old, new in changed:
            print(f"  {rid}  [{how}]")
            print(f"      旧: {old}…")
            print(f"      新: {new}…")
    if added:
        print(f"\n➕ 新增（需要补 priority/note，并拆 ticket）：")
        for rid, module, name, _, _ in added:
            print(f"  {rid}  {module} / {name}")
    if missing:
        print(f"\n❓ 本次文件里没出现的 {len(missing)} 条 —— **没有自动标废弃**：")
        for r in missing[:12]:
            print(f"  {r['id']}  {r.get('module','')} / {r.get('name','')}")
        if len(missing) > 12:
            print(f"  …… 另 {len(missing) - 12} 条")
        print("  甲方可能只是**发了个补充文件**，不代表这些需求取消了。")
        print("  确认真取消 → 手工把它们改成 status: dropped（别删行，删了 req_refs 会悬空）。")

    print(f"\n下一步（改了原文就必须跑，否则已完工 ticket 建在旧口径上还绿着）：")
    print(f"  python3 authority_lint.py diff")
    print(f"  python3 authority_lint.py impact")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="req_extract.py",
        description="从甲方 xlsx/csv 需求表抽 REQ 清单骨架（一行原文 = 一条 REQ，不合并不提炼）",
    )
    p.add_argument("--src", required=True, type=Path, help="需求文档（.xlsx 或 .csv）")
    p.add_argument("--inspect", action="store_true", help="只打印各 sheet 前 8 行 + 列号，不落盘")
    p.add_argument("--sheet", help="sheet 名（xlsx 多表时必填）")
    p.add_argument("--module-col", type=int, default=2, help="分组列号（Excel 列，A=1），默认 2")
    p.add_argument("--name-col", type=int, default=3, help="需求名列号；0=表里没这层，默认 3")
    p.add_argument("--text-col", type=int, default=4, help="需求描述列号，默认 4")
    p.add_argument("--header-rows", type=int, default=1, help="表头占几行，默认 1")
    p.add_argument("--domain", default="<DOMAIN>", help="域名（= ticket.track），如 STORE")
    p.add_argument("--prefix", default="REQ", help="id 前缀，如 REQ-STORE")
    p.add_argument("--out", type=Path, help="输出 requirements.yaml；省略则打到 stdout")
    p.add_argument("--append", action="store_true", help="追加到已有 --out（按 source 去重，id 续号）")
    p.add_argument("--merge", action="store_true",
                   help="甲方送来新版基线时用：按 板块+名称/原文相似 匹配已有 REQ，"
                        "**原地更新原文、id 保持不变**，新行新增、消失的只报不删")
    a = p.parse_args(argv)

    if not a.src.exists():
        sys.stderr.write(f"[error] 文件不存在: {a.src}\n")
        return 1
    try:
        sheets = (read_csv(a.src) if a.src.suffix.lower() == ".csv"
                  else read_xlsx(a.src, None if a.inspect else a.sheet))
    except Exception as e:
        sys.stderr.write(f"[error] 解析失败: {e}\n")
        return 1

    # --- inspect：先看表结构再决定列号，省得瞎猜 ---
    if a.inspect:
        for name, rows in sheets.items():
            print(f"=== sheet: {name}  ({len(rows)} 行) ===")
            for rno, cells in rows[:8]:
                cols = "  ".join(f"[{col_letter(i)}]{c[:28]}" for i, c in enumerate(cells, 1) if c)
                print(f"  r{rno}: {cols}")
            print()
        print("下一步：按上面的列号跑 --module-col / --text-col")
        return 0

    if len(sheets) > 1 and not a.sheet:
        sys.stderr.write(f"[error] 多 sheet 需指定 --sheet，可选: {list(sheets)}\n")
        return 1
    sheet_name = a.sheet or next(iter(sheets))
    rows = sheets[sheet_name]

    items = extract(rows, module_col=a.module_col, text_col=a.text_col,
                    name_col=a.name_col, header_rows=a.header_rows)

    # --- merge：新版基线合并（与 append 互斥；append 只按 source 去重，检测不到原文改动）---
    if a.merge:
        if not a.out:
            sys.stderr.write("[error] --merge 需要 --out 指向已有清单\n")
            return 1
        return do_merge(a, items, sheet_name)

    # --- append：按 source 去重、id 从已有最大号续 ---
    existing_text, existing_srcs, max_no = "", set(), 0
    if a.append and a.out:
        existing_text, existing_srcs, max_no = parse_existing(a.out, a.prefix)
        before = len(items)
        items = [it for it in items
                 if f"{a.src.name}#{sheet_name}!{col_letter(a.text_col)}{it[0]}" not in existing_srcs]
        if before != len(items):
            sys.stderr.write(f"[info] 去重跳过 {before - len(items)} 条已存在\n")

    if not items:
        if a.append and existing_srcs:
            sys.stderr.write(
                f"[ok] 本 sheet 的行已全部在 {a.out} 里（去重后无新增），未改动文件。\n")
            return 0
        sys.stderr.write(
            "[error] 抽出 0 条需求 —— 八成是 --text-col 指错了列。\n"
            "        先跑 --inspect 看真实列布局，别把空结果当成功。\n")
        return 2

    body = render(items, prefix=a.prefix, domain=a.domain, src_name=a.src.name,
                  sheet=sheet_name, text_col=a.text_col, start_no=max_no + 1)

    if a.out and a.append and existing_text:
        # 追加时把本轮 domain 并进 meta.domains（否则第二个 sheet 的域名不会出现在清单头）
        def _merge_domains(m):
            doms = [d.strip() for d in m.group(1).split(",") if d.strip()]
            if a.domain not in doms:
                doms.append(a.domain)
            return f"  domains: [{', '.join(doms)}]"
        existing_text = re.sub(r"^  domains: \[([^\]]*)\]", _merge_domains,
                               existing_text, count=1, flags=re.M)
        content = existing_text.rstrip("\n") + "\n\n" + body
    else:
        content = HEADER_TMPL.format(src=a.src.name, domain=a.domain) + body

    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(content, encoding="utf-8")
        print(f"[ok] {len(items)} 条需求 → {a.out}  (id: {a.prefix}-{max_no+1:03d} .. "
              f"{a.prefix}-{max_no+len(items):03d})", file=sys.stderr)
        print("[next] 人工补 priority / note / clarify，然后拆 ticket 时在 front-matter 写 req_refs",
              file=sys.stderr)
    else:
        print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
