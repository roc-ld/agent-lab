#!/usr/bin/env python3
"""adr_check.py — ADR 与 OQ 台账体检（收编 一个实战项目doc/_audit/adr-anchor-check.sh）。

检查项：
  A1  frontmatter 齐全：id 合法（ADR-NNNN）且与文件名序号一致、status/implementation_status
      在枚举内、verified 是日期
  A2  supersedes 链闭合：superseded 必填 superseded_by 且指向存在的 ADR；
      supersedes/amends 指向的 ADR 存在
  A3  anchor 可验：非空；形如路径的（含 / 且不含空格）在仓库里真实存在
      （file:line 只验 file 部分）。「以代码为准」这类没锚点的话等于没写
  A4  口径自相矛盾（抓 ADR-0011:65 那类）：正文行出现 decision.rejected_values 里的
      裸值、而该行没有「不采纳/不取/弃用/已废弃/rejected」标记 → error
  A5  OQ 台账闭环：doc/_oq*.md 里落地状态为 ✅ 的行，closed_by 列必须非空
      （取值限 ADR-NNNN 或 code anchor）。表头没有 closed_by 列 → warning 提示迁移

用法：adr_check.py [--ws <工作区根>] [--adr <目录>] [--oq-glob '<glob>'] [--repo-root .]
     省略时自动探测工作区根（<root>/doc/… 或 <root>/…，见下方 resolve_ws）
退出码：0 全过 / 1 有 error / 2 仅 warning
依赖 pyyaml（与 authority_lint 同一立场：权威数据不做手解析兜底）。
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

VALID_STATUS = ("proposed", "accepted", "amended", "deprecated", "superseded")
VALID_IMPL = ("implemented", "partially-implemented", "not-yet-implemented",
              "superseded-by-code", "needs-verification")
REJECT_MARKS = ("不采纳", "不取", "弃用", "已废弃", "不用", "rejected", "否决", "不选")


# ── 工作区根定位（与 authority_lint.py / coverage_lint.py 的同名函数保持同步）──
# 2026-09-13 体检测出：本脚本原来写死 doc/_adr，在 doc-phase2 这类**拍平布局**下
# A1-A4 全部静默跳过（只打一句「无 ADR 目录」），最后照样说「✅ 全部合规」——
# 与 authority_lint 是同一个「零输入给绿灯」的病。
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


def _load(md):
    with open(md, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None, "缺 YAML frontmatter"
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, None, "frontmatter 未闭合"
    import yaml  # noqa: PLC0415
    try:
        fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception as e:
        return None, None, f"frontmatter 解析失败: {e}"
    return fm, lines[end + 1:], None


def _glob_any(pattern, roots):
    """在若干根下找 pattern，返回去重后的文件列表（pattern 本身也当 cwd 相对试一次）。"""
    out = []
    for pat in [pattern] + [os.path.join(r, pattern) for r in roots]:
        for f in glob.glob(pat):
            if f not in out:
                out.append(f)
    return out


def check_adrs(adr_dir, repo_roots):
    """repo_roots：anchor 允许相对哪几个根解析（见 main 里的说明）。"""
    errors, warnings = [], []
    known = set()
    parsed = []
    files = [f for f in sorted(glob.glob(os.path.join(adr_dir, "*.md")))
             if not os.path.basename(f).upper().startswith(("README", "_"))]
    for md in files:
        base = os.path.basename(md)
        fm, body, err = _load(md)
        if err:
            errors.append(f"[{base}] {err}")
            continue
        aid = str(fm.get("id") or "").strip()
        if not re.fullmatch(r"ADR-\d{3,4}", aid):
            errors.append(f"[{base}] A1: id 缺失或不合法（{aid!r}，须 ADR-NNNN）")
            continue
        m = re.match(r"(\d{3,4})-", base)
        if m and int(m.group(1)) != int(aid.split("-")[1]):
            errors.append(f"[{base}] A1: 文件名序号与 id {aid} 不一致")
        known.add(aid)
        parsed.append((base, aid, fm, body))

    for base, aid, fm, body in parsed:
        st = str(fm.get("status") or "").strip()
        impl = str(fm.get("implementation_status") or "").strip()
        if st not in VALID_STATUS:
            errors.append(f"[{aid}] A1: status={st!r} 不在枚举 {VALID_STATUS}")
        if impl and impl not in VALID_IMPL:
            errors.append(f"[{aid}] A1: implementation_status={impl!r} 不在枚举")
        if not impl:
            warnings.append(f"[{aid}] A1: 无 implementation_status——双轨 status 的第二轨缺失，"
                            f"读的人无从知道代码现状（先标 needs-verification）")

        # A2 链闭合
        sb = fm.get("superseded_by") or []
        sb = sb if isinstance(sb, list) else [sb]
        sb = [str(x).strip() for x in sb if str(x).strip()]
        if st == "superseded" and not sb:
            errors.append(f"[{aid}] A2: status=superseded 但 superseded_by 为空——"
                          f"被谁推翻的必须留痕")
        for field in ("supersedes", "superseded_by", "amends", "amended_by"):
            v = fm.get(field) or []
            v = v if isinstance(v, list) else [v]
            for ref in (str(x).strip() for x in v if str(x).strip()):
                if re.fullmatch(r"ADR-\d{3,4}", ref) and ref not in known:
                    errors.append(f"[{aid}] A2: {field} 指向不存在的 {ref}")

        # A3 anchor
        anchor = str(fm.get("anchor") or "").strip()
        if not anchor:
            errors.append(f"[{aid}] A3: anchor 为空——写「以代码为准」等于没写，"
                          f"给一个能 grep 到的 file:line / 表名 / 类名")
        elif "/" in anchor and " " not in anchor:
            path = anchor.split(":")[0]
            if not any(os.path.exists(os.path.join(r, path)) for r in repo_roots):
                errors.append(f"[{aid}] A3: anchor 路径不存在: {path}"
                              f"（试过 {' , '.join(repo_roots)}；"
                              f"代码挪了？改 anchor 并重核 implementation_status）")

        # A4 口径自相矛盾
        rejected = ((fm.get("decision") or {}).get("rejected_values")) or []
        for rv in (str(x).strip() for x in rejected if str(x).strip()):
            # 取被否口径里最有辨识度的片段（数字+单位优先，否则前 6 字）
            key = re.search(r"\d+\s*位|\d+\s*段|\d+\s*字符", rv)
            needle = key.group(0).replace(" ", "") if key else rv[:6]
            for i, ln in enumerate(body):
                bare = ln.replace(" ", "")
                if needle in bare and not any(mk in ln for mk in REJECT_MARKS):
                    errors.append(f"[{aid}] A4: 正文第 {i + 1} 行出现被否口径「{needle}」"
                                  f"却无「不采纳」标记——口径自相矛盾，读的人只能猜哪个算数")
                    break
    return errors, warnings


def check_oq(oq_glob, repo_roots):
    errors, warnings = [], []
    for md in sorted(_glob_any(oq_glob, repo_roots)):
        base = os.path.basename(md)
        with open(md, encoding="utf-8") as f:
            lines = f.read().splitlines()
        header_cols = None
        for ln in lines:
            if ln.strip().startswith("|") and "ID" in ln and ("落地" in ln or "状态" in ln):
                header_cols = [c.strip() for c in ln.strip().strip("|").split("|")]
                break
        if header_cols is None:
            continue
        if "closed_by" not in [c.lower() for c in header_cols]:
            warnings.append(f"[{base}] A5: OQ 表头无 closed_by 列——✅Closed 的依据无处落，"
                            f"迁移到 templates/_oq.md 的表头（加一列即可）")
            continue
        ci = [c.lower() for c in header_cols].index("closed_by")
        for i, ln in enumerate(lines):
            if not ln.strip().startswith("|") or "✅" not in ln:
                continue
            cols = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cols) <= ci or not cols[ci] or cols[ci] in ("-", "⬜"):
                errors.append(f"[{base}] A5: 第 {i + 1} 行标了 ✅ 但 closed_by 为空——"
                              f"凭什么 Closed 的？填 ADR-NNNN 或 code anchor")
    return errors, warnings


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ws", default=None,
                   help="工作区根（含 _adr/ 与 requirements.yaml 的那一层）；省略则自动探测")
    p.add_argument("--adr", default=None, help="ADR 目录（默认 <ws>/_adr）")
    p.add_argument("--oq-glob", default=None,
                   help="OQ 台账 glob（默认 _oq*.md，在下面几个根下各找一遍）")
    p.add_argument("--repo-root", default=None,
                   help="anchor / OQ 相对哪个根解析。默认按 [<ws>, <ws>的父目录, cwd] 依次试——"
                        "真实项目两种写法都有：一个实战项目写 authority/xxx.yaml（ws 相对），"
                        "一个实战项目写 doc/lint-profile.yaml（仓库根相对）")
    a = p.parse_args(argv)

    ws = resolve_ws(a.ws)
    if a.adr is None:
        a.adr = os.path.join(ws, "_adr")
    if a.oq_glob is None:
        a.oq_glob = "_oq*.md"
    if a.repo_root is not None:
        roots = [a.repo_root]
    else:
        # 只影响「找不找得到」，不放松「必须真实存在」这条判据。
        roots = [ws, os.path.dirname(os.path.abspath(ws)), os.getcwd()]
        roots = list(dict.fromkeys(roots))          # 去重、保序

    try:
        import yaml  # noqa: F401, PLC0415
    except ImportError:
        sys.stderr.write("[error] 需要 pyyaml：pip install pyyaml\n")
        return 1

    errors, warnings = [], []
    if os.path.isdir(a.adr):
        e, w = check_adrs(a.adr, roots)
        errors += e
        warnings += w
    else:
        print(f"（无 ADR 目录 {a.adr} —— A1-A4 **跳过，不是通过**；"
              f"若这里本该有 ADR，多半是工作区根没探对：传 --ws 或设 $XUQIU_WORKDIR）")
        # 「跳过」只有在**确实站在一个工作区里**才合法。两条线索都没有 = 目录跑错了，
        # 这时候打 ✅ 就是 authority_lint 那个「零输入给绿灯」的同款错误。
        if not _glob_any(a.oq_glob, roots) and not _is_ws_root(ws):
            sys.stderr.write(
                f"[error] ADR 与 OQ 都没找到，而且 {ws} 也不像工作区根"
                f"（没有 authority/ tickets/ requirements.yaml 任何一个）。\n"
                f"        几乎可以肯定是目录不对——先 cd 到工作区根，或传 --ws。\n"
                f"        零输入绝不是「全部合规」，不给绿灯。\n")
            return 1
    e, w = check_oq(a.oq_glob, roots)
    errors += e
    warnings += w

    if errors:
        print(f"❌ ERROR ({len(errors)})：")
        for x in errors:
            print(f"  {x}")
    if warnings:
        print(f"🟡 WARNING ({len(warnings)})：")
        for x in warnings:
            print(f"  {x}")
    if not errors and not warnings:
        print("✅ ADR / OQ 台账全部合规")
    return 1 if errors else (2 if warnings else 0)


if __name__ == "__main__":
    sys.exit(main())
