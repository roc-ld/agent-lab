#!/usr/bin/env python3
"""dag_lint.py — ticket 上机前静态体检（②zhixing 的守门闸）。

开跑前扫一遍 doc/tickets/*，把「跑起来才炸」的结构性错误挡在门外：
front-matter 缺字段、depends_on 指向不存在的 ticket、DAG 有环、accept 写不出
或缺 form、touches 空。任何一类失败都让执行不可靠——环会让就绪集永远算不
出来，悬空 depends_on 让 ticket 永远开不了工，无 form 的 accept 是弱断言批发的
温床（见 accept-assertion-forms.md）。

契约
----
输入：
  - argv[1] 可选：tickets 根目录，默认 ./doc/tickets
    目录结构 = doc/tickets/<ticket-id>/*.md，每份 .md 顶部一段 YAML front-matter
    （`---` 包裹），schema 见 xuqiu/templates/ticket.frontmatter.yaml。
    必填字段见 REQUIRED_FIELDS（track/phase/req_refs/depends_on/touches/blueprint_refs/accept）。
  - 编排层 headless 模式：读 workdir/_input/tickets_dir（若存在），产物写
    workdir/_lint.json 供 zhixing 读取（见下 emit_manifest；与主 _manifest.json 分文件，不混词汇）。

输出：
  - stdout：人类可读的分组报告（每类校验一段，errors 明确到 ticket + 字段 + 原因）。
  - 退出码：0 = 全过可开跑；1 = 有 error（结构性，禁止上机）；2 = 仅 warning。
  - _lint.json（headless，独立于 zhixing 主 _manifest.json）：{"skill":"dag-lint",
    "status":"pass|fail","counts":{...},"errors":[...],"warnings":[...],"cycles":[[...]]}。
    zhixing 读到 status=fail → 翻译成主 _manifest 的 status="needs_human"
    + needs_human.why="dag_lint 拦截：<errors 摘要>"（两套 status 词汇不同时丢给弱模型编排层）。

校验清单（一校验一函数，报错清晰可定位）：
  C1 check_frontmatter_present  每 ticket 有可解析 front-matter + 必填字段齐全
  C2 check_depends_exist        depends_on 引用的 ticket 都在集合内（无悬空/无日期语义）
  C3 check_acyclic              depends_on 构成的有向图无环（拓扑；报出具体环路）
  C4 check_accept_parsable      accept 是非空 list，每条 dict 有 name/form/run
  C5 check_accept_form_strength accept 每条 form ∈ 白名单 **且 run 内容满足该 form 的
                                强制形态**（防弱断言批发，见 accept-assertion-forms.md）
  C6 check_touches_nonempty     touches 非空 list（空 = 模块占用锁失效，合流互斥算不出）；
                                以通配符开头的 glob 会被迫全局串行 → warning 提示改精确前缀
  C7 check_phase_and_reqrefs    phase 非空（否则不属于任何任务/daily，被静默跳过）+
                                req_refs 非空（否则说不清满足甲方哪条需求）
  C8 check_phase_order          depends_on 不许指向**后面的任务**（串行档下永远等不到）

非目标（这些归调度器运行时 / 别的工件，dag_lint 不管）：
  - **需求覆盖率**（REQ 有没有被漏拆）→ xuqiu/scripts/coverage_lint.py：它同时持有
    REQ 清单和 ticket 两侧，本 lint 只看得到 ticket 一侧，只能查 req_refs 非空
  - touches glob 与实际 git diff 的对账（那是合流时的 touches 罚则，运行时才有 diff）
  - accept 断言真去跑（那是任务收敛阶段 verify.sh / QA 门的事）
  - 迁移时间戳 / 端口 / 轨隔离（多轨并行才需要，任务串行档不涉及）

依赖：纯 Python stdlib（图/查环/遍历全手写）。**YAML 解析是唯一外部依赖**——
必须 `import yaml`（PyYAML）；不可用时报结构错误，不回退简化解析器。
"""

import sys
import os
import re
import json
import glob

# accept 的 form 白名单，与 accept-assertion-forms.md 强制形态表一一对应。
VALID_ACCEPT_FORMS = {"DDL", "API", "MENU", "DATA", "STATE", "E2E"}

# front-matter 里调度器实际消费的字段（正文不读）。缺任一 = 任务书未就绪。
#   phase    —— 归属任务(daily)。漏写会静默不入任何任务，必须拦。
#   req_refs —— 满足甲方哪几条需求。空 = 无需求来源（镀金）；覆盖率由 coverage_lint 深校。
REQUIRED_FIELDS = ("track", "phase", "req_refs", "depends_on",
                   "touches", "blueprint_refs", "accept")


# ═══════════════════════════════════════════════════════════════════════════
# 工作区根定位 —— 与 task_state.py / xuqiu 的两个 lint 保持同步
# ═══════════════════════════════════════════════════════════════════════════
# 支持 <root>/doc/tickets（标准）与 <root>/tickets（拍平，doc-phaseN 多阶段项目）。
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
                                 f"不对就传 [tickets目录] 或设 ${WS_ENV}\n")
            return cand
    return os.path.join("doc")

# ═══════════════════════════════════════════════════════════════════════════
# C5 断言强度：**启发式提示，不是保证**（这一节的定位是被红队打出来的，别退回去）
# ═══════════════════════════════════════════════════════════════════════════
# 红队实证结论（2026-07-28）：
#   · 五种 form 的强制形态**全部可被绕过**，成本极低——判定建立在子串命中上，
#     而 shell `#` / SQL `--` 注释也是 run 的一部分：注释里写个 `path`、`-- join`、
#     把 6 个公共字段名写进注释，就全绿。
#   · 同时它**硬拦合法写法**：跨系统 API 对账（domain-ownership.md §5 明文要求的形态）、
#     权限 403 断言（正确做法就是丢弃响应体只看状态码）、非 RuoYi 栈建表。
#   · `run: bash x.sh` 是一键逃逸，静态分析完全失明。
#
# 于是采取三条对策，而不是继续加正则：
#   ① **先剥注释再判**——杀掉整类"注释里塞关键词"的绕过。
#   ② **启发式一律 warning**（error 只留给无争议项：run 为空 / form 不在白名单）。
#      理由：每条启发式都有已证实的合法反例，硬拦的代价是逼人走 `bash x.sh` 逃逸，
#      那时 C5 一点作用都没有。诚实标注"这是提示"，比假装"强度已被机器保证"好。
#   ③ **项目常量外置**到 doc/lint-profile.yaml（支持按 system 分组，两个后端各一套）。
#
# 真正能区分强弱断言的手段是**变异测试**（往被测对象注入已知类事故，看断言是否仍绿），
# 见 accept-assertion-forms.md §冻结 gate。静态 grep 永远做不到，别指望本函数。

# 公共字段默认集（RuoYi 系）。项目不同 → 写 doc/lint-profile.yaml 覆盖，别改这里
# （本文件是全局跨项目的，改它会波及所有项目）。
DEFAULT_PUBLIC_FIELDS = ("create_by", "create_time", "update_by",
                         "update_time", "create_dept", "del_flag")

# 只断状态码 / 只断存在性的典型弱写法（正则，大小写不敏感）
WEAK_PATTERNS = {
    "API": [r"code\s*[=:]=?=?\s*['\"]?200", r"status_code\s*==?=?\s*200",
            r"HTTP/[\d.]+\s+200", r"-w\s+['\"]?%\{http_code\}"],
    "MENU": [r"COUNT\(\*\)\s*(?:BETWEEN|>|>=)", r"count\s*>\s*0"],
}

# 「非法转移被拒」的合法表达（中英混排，覆盖靠错误文案断言的写法）
REJECT_WORDS = ("非法", "illegal", "invalid", "reject", "拒", "不允许", "禁止",
                "不可", "不能", "校验失败", "should_fail", "expect_fail",
                "must_fail", "assert_fail", "4[0-9][0-9]")


def _strip_comments(run):
    """剥掉 shell `#` 与 SQL `--` 注释再做形态判定。

    不剥的话，「把 6 个公共字段名写进注释、SQL 里其实是 SELECT 1」这种就全绿——
    红队实测五种 form 全靠注释绕过。粗剥即可：不追求完整 shell 词法分析
    （引号内的 # 会被误剥），宁可少认关键词（偏保守 → 多出 warning）也不要多认。
    """
    out = []
    for line in run.splitlines():
        line = re.sub(r"--\s.*$", "", line)          # SQL 注释（-- 后必须跟空白，避免误伤 --flag）
        line = re.sub(r"(?<!\$)(?<!\\)#(?!\{).*$", "", line)  # shell 注释（避开 ${#x} / #{...}）
        out.append(line)
    return "\n".join(out)


def _has(run, *needles):
    low = run.lower()
    return any(n.lower() in low for n in needles)


def _re_any(run, patterns):
    return any(re.search(p, run, re.I | re.M) for p in patterns)


def load_lint_profile(tickets_dir):
    """读项目级 lint 配置（可选）。找 <tickets_dir>/../lint-profile.yaml。

    格式：
        public_fields: [create_by, create_time, ...]      # 默认集
        systems:                                          # 多系统项目：按 system 覆盖
          store:  { public_fields: [...] }
          site:   { public_fields: [id, created_at, updated_at, deleted_at] }

    没有这个文件就用 DEFAULT_PUBLIC_FIELDS —— 二期是两个后端两套公共字段，
    单一全局常量表达不了，而 dag_lint.py 本身是跨项目共享文件不该改。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(tickets_dir)), "lint-profile.yaml")
    if not os.path.exists(path):
        return {"public_fields": list(DEFAULT_PUBLIC_FIELDS), "systems": {},
                "_explicit": False}
    try:
        import yaml  # noqa: PLC0415
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        sys.stderr.write(f"[warn] lint-profile.yaml 读取失败，用默认公共字段集: {e}\n")
        return {"public_fields": list(DEFAULT_PUBLIC_FIELDS), "systems": {},
                "_explicit": False}
    # 栈包解析（2026-08-17）：lint-profile 写 `stack: <id>`（或 systems.<id>.stack）→
    # 从 ~/.claude/stacks/<id>/profile.yaml 取 public_fields。单一权威，
    # 结束「RuoYi 六字段硬编码在跨项目共享文件里」的状态。显式 public_fields 仍最优先。
    def _stack_fields(stack_id):
        if not stack_id:
            return None
        sp = os.path.expanduser(os.path.join("~", ".claude", "stacks",
                                             str(stack_id), "profile.yaml"))
        if not os.path.exists(sp):
            sys.stderr.write(f"[warn] stack {stack_id!r} 的栈包不存在: {sp}"
                             f"（用 xuqiu 选栈步骤生成最小骨架，或改回显式 public_fields）\n")
            return None
        try:
            with open(sp, encoding="utf-8") as f:
                sdata = yaml.safe_load(f) or {}
            return sdata.get("public_fields")
        except Exception as e:
            sys.stderr.write(f"[warn] 栈包 profile 读取失败 {sp}: {e}\n")
            return None

    top_fields = data.get("public_fields") or _stack_fields(data.get("stack"))
    systems = {}
    for sid, sconf in (data.get("systems") or {}).items():
        sconf = dict(sconf or {})
        if not sconf.get("public_fields"):
            sf = _stack_fields(sconf.get("stack"))
            if sf:
                sconf["public_fields"] = sf
        systems[sid] = sconf
    return {
        "public_fields": top_fields or list(DEFAULT_PUBLIC_FIELDS),
        "systems": systems,
        # 是否有任何显式来源（顶层字段/栈/任一 system）——C5 的 DDL 断言只在
        # 「有 DDL accept 且完全靠默认集」时报 error（收窄判据，防误伤纯前端项目）
        "_explicit": bool(top_fields) or any(
            s.get("public_fields") or s.get("stack") for s in systems.values()),
    }


def _public_fields_for(profile, system):
    sysconf = (profile.get("systems") or {}).get(system or "", {})
    return tuple(sysconf.get("public_fields") or profile["public_fields"])


# ---------------------------------------------------------------------------
# front-matter 载入
# ---------------------------------------------------------------------------

def load_tickets(tickets_dir):
    """扫描 tickets_dir，返回 {ticket_id: {"path": str, "fm": dict|None, "raw_err": str|None}}.

    ticket_id 取自 front-matter 的 `ticket` 字段；缺失则回退用目录名，并记一条
    error（id 不一致会让 depends_on 引用对不上）。同一 id 出现两次也记 error。
    """
    tickets, dups = {}, []
    # 递归扫 <dir>/**/prompt.md —— ticket 可能按系统/域分组放子目录
    # （doc/tickets/store/<id>/prompt.md）。只认固定名 prompt.md。
    for md_path in sorted(glob.glob(os.path.join(tickets_dir, "**", "prompt.md"),
                                    recursive=True)):
        fm, err = _read_frontmatter(md_path)
        # ticket id 优先取 front-matter；回退目录名。
        dir_id = os.path.basename(os.path.dirname(md_path))
        tid = (fm or {}).get("ticket") or dir_id
        if tid in tickets:
            # 重复 id 必须报：dag_lint 曾"首个胜"、task_state "末个胜"，
            # 于是 lint 校验的和调度器派单的是**两个不同文件**（复制粘贴手误的典型后果）。
            dups.append({"ticket": tid, "field": "ticket",
                         "msg": f"ticket id 重复：{tickets[tid]['path']} 与 {md_path}"
                                f"（id 必须全局唯一，否则调度器可能派另一个文件）"})
            continue
        tickets[tid] = {"path": md_path, "fm": fm, "raw_err": err, "dir_id": dir_id}
    return tickets, dups


def _read_frontmatter(md_path):
    """读单个 md 顶部的 `---` YAML 块，返回 (dict|None, err_str|None)。"""
    try:
        with open(md_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return None, f"读文件失败: {e}"
    # 提取首个 --- ... --- 块。
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "缺 front-matter（文件未以 --- 开头）"
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, "front-matter 未闭合（缺第二个 ---）"
    block = "\n".join(lines[1:end])
    try:
        import yaml  # noqa: PLC0415  优先真 YAML 解析器
        data = yaml.safe_load(block)
        if not isinstance(data, dict):
            return None, "front-matter 不是 mapping"
        return data, None
    except ImportError:
        return None, "新版需要 PyYAML；请使用 D0 固定的 Python，不使用简化 YAML 解析器"
    except Exception as e:
        # 绝不冒泡：一份坏文件曾把上百份 ticket 的全量报告变成一段 traceback，
        # 得二分法手工找。常见触发：run 里带未加引号的 ": "（如 Authorization: Bearer）。
        return None, f"YAML 解析失败: {e}（run 里含 ': ' 时要给整个值加引号）"


def _parse_frontmatter_fallback(block):
    """PyYAML 不可用时的极简解析器：够解析 ticket front-matter 的扁平结构。

    支持：`key: scalar` / `key:` 后跟 `  - item` 缩进 list / list-of-dict
    （`  - name: x` 连续 `    form: y`）。**不支持**嵌套 mapping、流式 [a,b]、
    多行标量、锚点。ticket.frontmatter.yaml 的 accept 是 list-of-dict，这里要能吃下。

    TODO(按需): 若项目 front-matter 用了更复杂 YAML（流式/嵌套），装 PyYAML 而非
    在此加解析分支——别把这个 fallback 养成半个 YAML 引擎。
    """
    # NOTE: 手写状态机——逐行判断 缩进层级 + `- ` 前缀 + `key:` 冒号。
    #   顶层 key -> 值；遇 `key:` 空值后续 `  - ` 收集成 list；
    #   `  - name:` 起头则该 list 元素是 dict，吸收后续更深缩进的 `key: val`。
    result = {}
    cur_key = None          # 当前正在填充的顶层 key
    cur_list = None         # 当前顶层 key 对应的 list（若是 list 值）
    cur_dict_item = None    # list-of-dict 时，当前正在填充的 dict 元素
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            # 顶层 key: 收尾上一个 list
            cur_list = None
            cur_dict_item = None
            if ":" not in stripped:
                continue
            k, _, v = stripped.partition(":")
            cur_key = k.strip()
            v = v.strip()
            if v == "":
                # 值待定：可能是 list（下面缩进 - ）——先存空 list 占位
                result[cur_key] = []
                cur_list = result[cur_key]
            elif v.startswith("[") and v.endswith("]"):
                # 行内 list（blueprint_refs: [FLOW:x, FIELD:y]）——与 task_state._fm_fallback 同规则。
                # 不解析的话行内非空 list 会被当成一个字符串标量，C7 的非空检查会误报。
                result[cur_key] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
                cur_list = None
            else:
                result[cur_key] = _coerce_scalar(v)
                cur_list = None
        elif stripped.startswith("- "):
            # list 项
            item = stripped[2:].strip()
            if cur_list is None:
                # key 当行没留空 list 占位（防御）
                if cur_key is not None:
                    result[cur_key] = []
                    cur_list = result[cur_key]
                else:
                    continue
            if ":" in item:
                # list-of-dict 首行：`- name: 表已建`
                k, _, v = item.partition(":")
                cur_dict_item = {k.strip(): _coerce_scalar(v.strip())}
                cur_list.append(cur_dict_item)
            else:
                cur_dict_item = None
                cur_list.append(_coerce_scalar(item))
        elif cur_dict_item is not None and ":" in stripped:
            # list-of-dict 的后续字段（更深缩进）：`    form: DDL`
            k, _, v = stripped.partition(":")
            cur_dict_item[k.strip()] = _coerce_scalar(v.strip())
        else:
            # 其余缩进形态（嵌套 mapping / 流式 [a,b] / 多行标量）不支持 → 显式告警，
            # 不静默吞（防结构错被当解析成功、削弱 lint 守门可信度）。命中就该装 PyYAML。
            sys.stderr.write(f"[warn] fallback 解析器跳过不支持的行 {stripped!r}（建议 pip install pyyaml）\n")
    return result, None


def _coerce_scalar(v):
    """剥引号 + 常见标量类型转换（够 front-matter 用）。"""
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


# ---------------------------------------------------------------------------
# C1 front-matter 完整性
# ---------------------------------------------------------------------------

def check_frontmatter_present(tickets):
    """C1：每 ticket 有可解析 front-matter + 五字段齐全 + ticket id 与目录名一致。

    返回 errors 列表（每条 dict: {"ticket","field","msg"}）。
    """
    errors = []
    for tid, rec in tickets.items():
        fm = rec["fm"]
        if fm is None:
            errors.append({"ticket": tid, "field": "front-matter",
                           "msg": rec["raw_err"] or "front-matter 解析失败"})
            continue
        # id 与目录名一致性（depends_on 用的是 id，对不上会造成悬空引用）
        if fm.get("ticket") and fm["ticket"] != rec["dir_id"]:
            errors.append({"ticket": tid, "field": "ticket",
                           "msg": f"front-matter ticket={fm['ticket']} 与目录名 {rec['dir_id']} 不一致"})
        for field in REQUIRED_FIELDS:
            if field not in fm or fm.get(field) is None:
                errors.append({"ticket": tid, "field": field,
                               "msg": f"缺字段 {field}（front-matter 必填 "
                                      f"{len(REQUIRED_FIELDS)} 字段之一）"})
    return errors


# ---------------------------------------------------------------------------
# C2 depends_on 引用存在
# ---------------------------------------------------------------------------

def check_depends_exist(tickets):
    """C2：depends_on 里的每个 id 都在 tickets 集合内。

    悬空引用 = 该 ticket 永远进不了就绪集（depends_on 那条永远 done 不了）。
    顺带拦「日期语义」：depends_on 只能写 ticket id，不能写 "D8 merge 后" 这类——
    日期语义正是三件套要退役的东西（见 ticket.frontmatter.yaml 注释）。
    """
    errors = []
    ids = set(tickets.keys())
    for tid, rec in tickets.items():
        fm = rec["fm"] or {}
        deps = fm.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append({"ticket": tid, "field": "depends_on",
                           "msg": "depends_on 必须是 list"})
            continue
        for dep in deps:
            dep_s = str(dep).strip()
            # 日期语义嗅探：含 "merge" / "后" / "D<数字>" 且不在 id 集合里。
            if dep_s not in ids:
                hint = ""
                if any(k in dep_s for k in ("merge", "后", "之后")) or _looks_like_day_ref(dep_s):
                    hint = "（疑似日期语义，depends_on 只能写 ticket id）"
                errors.append({"ticket": tid, "field": "depends_on",
                               "msg": f"依赖 {dep_s!r} 不存在{hint}"})
    return errors


def _looks_like_day_ref(s):
    """粗判 'D8' / 'day9' 这类日期锚点。"""
    s = s.lower().lstrip()
    if s.startswith("day") and s[3:].isdigit():
        return True
    if s.startswith("d") and s[1:].isdigit():
        return True
    return False


# ---------------------------------------------------------------------------
# C3 无环（拓扑 / DFS 三色查环）
# ---------------------------------------------------------------------------

def check_acyclic(tickets):
    """C3：depends_on 有向图无环。有环则报出具体环路（ticket id 链）。

    实现：DFS 三色标记（white 未访 / gray 在栈 / black 已完）。gray 边回指 = 环，
    回溯栈拼出环路。stdlib 手写，不引第三方图库。只在依赖都存在的边上建图
    （悬空依赖已由 C2 报，这里跳过避免 KeyError）。
    """
    errors = []
    ids = set(tickets.keys())
    # 建邻接表：ticket -> [它依赖的、且存在的 ticket]
    graph = {}
    for tid, rec in tickets.items():
        fm = rec["fm"] or {}
        deps = fm.get("depends_on") or []
        graph[tid] = [str(d).strip() for d in deps if str(d).strip() in ids]

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in graph}
    stack = []  # 当前 DFS 路径，用于还原环路
    found_cycles = []

    def dfs(node):
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, ()):
            if color[nxt] == GRAY:
                # 回边 -> 环。截取 stack 从 nxt 到当前。
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                found_cycles.append(cycle)
            elif color[nxt] == WHITE:
                dfs(nxt)
        stack.pop()
        color[node] = BLACK

    for tid in graph:
        if color[tid] == WHITE:
            dfs(tid)

    for cyc in found_cycles:
        errors.append({"ticket": cyc[0], "field": "depends_on",
                       "msg": "存在依赖环: " + " -> ".join(cyc)})
    # 去重（同一环可能从多起点被发现）
    seen = set()
    uniq = []
    for e in errors:
        key = e["msg"]
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq, found_cycles


# ---------------------------------------------------------------------------
# C4 accept 可解析
# ---------------------------------------------------------------------------

def check_accept_parsable(tickets):
    """C4：accept 是非空 list，每条为 dict 且含 name / form / run。

    写不出 accept = 任务书未就绪，不进 DAG（ticket.frontmatter.yaml 明规）。
    accept 是 testing-ai/verify.sh 的结构化子集，run 缺失就无从收敛。
    """
    errors = []
    for tid, rec in tickets.items():
        fm = rec["fm"] or {}
        accept = fm.get("accept")
        if not isinstance(accept, list) or len(accept) == 0:
            errors.append({"ticket": tid, "field": "accept",
                           "msg": "accept 缺失或为空（至少 1 条机器可验断言）"})
            continue
        for i, a in enumerate(accept):
            if not isinstance(a, dict):
                errors.append({"ticket": tid, "field": f"accept[{i}]",
                               "msg": "断言项必须是 dict（name/form/run）"})
                continue
            for key in ("name", "form", "run"):
                if not a.get(key):
                    errors.append({"ticket": tid, "field": f"accept[{i}].{key}",
                                   "msg": f"断言缺 {key}"})
    return errors


# ---------------------------------------------------------------------------
# C5 accept form 白名单 + 强度（强度留 TODO）
# ---------------------------------------------------------------------------

def _strength_verdict(form, raw_run, public_fields):
    """按 form 给单条 run 的强度**提示**，返回 (level, msg) 或 None。

    level: "error" 只用于无争议项；其余一律 "warning"（见文件上方 C5 定位说明）。
    形态口径对齐 xuqiu/templates/accept-assertion-forms.md，但**它是提示不是保证**。
    """
    if not raw_run or not raw_run.strip():
        return ("error", "run 为空")

    run = _strip_comments(raw_run)      # ← 先剥注释，杀掉"注释里塞关键词"的绕过
    if not run.strip():
        return ("error", "run 剥掉注释后为空——整条断言都在注释里？")

    if form == "DDL":
        missing = [c for c in public_fields if c not in run]
        if len(missing) == len(public_fields):
            return ("warning",
                    f"DDL 断言（剥注释后）没提到任何公共字段——「建了表但漏公共字段」会骗绿。"
                    f"本项目公共字段集: {', '.join(public_fields)}。"
                    f"若本 ticket 的表/栈不适用这套字段，在 doc/lint-profile.yaml 里"
                    f"按 system 配置该系统的 public_fields")
        if missing:
            return ("warning", f"DDL 断言缺公共字段: {', '.join(missing)}（建议断全集）")
        return None

    if form == "API":
        # 权限类断言（断 4xx/403）**本来就该丢弃响应体**——L3 邪路场景明确要求
        # "非超管权限"每类至少一条，这是合法形态，不是弱断言。
        is_perm = _re_any(run, [r"\b4[0-9][0-9]\b"]) and _re_any(
            run, [r"-o\s+/dev/null", r"-w\s+['\"]?%\{http_code\}", r"grep\s+-qx?\s+['\"]?4"])
        if is_perm:
            return None
        if _re_any(run, [r"-o\s+/dev/null", r"-w\s+['\"]?%\{http_code\}"]):
            return ("warning",
                    "API 断言丢弃了响应体（-o /dev/null / -w %{http_code}），只剩状态码——"
                    "接口漏字段/返回空行/值算错照样 200。除非这是权限类断言（断 4xx），"
                    "否则应断返回行内容")
        has_content = _has(run, "jq", "has(", "json", "grep -q", "python -c", "python3 -c",
                           "diff ", "assert")
        if _re_any(run, WEAK_PATTERNS["API"]) and not has_content:
            return ("warning",
                    "API 断言看起来只断了状态码——必须断返回行内容（关键字段齐全 + 关键值正确）")
        if not has_content:
            return ("warning", "API 断言看不出在校验返回体内容，确认不是只断连通性")
        return None

    if form == "MENU":
        if not _has(run, "path"):
            return ("warning",
                    "MENU 断言（剥注释后）没断 path——「只改 parent 不改 path→404」"
                    "「空 path 崩整个路由」都是真实事故，仅落号段 COUNT>0 拦不住")
        if not _has(run, "getrouters", "router", "route"):
            return ("warning", "MENU 断言断了 path，但没断路由下发可达（getRouters 等价物）")
        return None

    if form == "DATA":
        if _re_any(run, [r"\bINSERT\s+INTO\b", r"\bmock\b", r"\bdevmock\b"]):
            # 这条保持 error：自造数据自证是形态上的确定错误，没有合法反例
            return ("error",
                    "DATA 断言里出现 INSERT/mock——自造数据自证是 B 类事故的温床"
                    "（junction 缺失、resolver 漏关联在自证闭环里永远测不出）。"
                    "断言必须打在实现方没写过的真实 seed 数据上")
        # 「两侧独立算、断差值为 0」的合法形态远不止 SQL JOIN：
        #   · 跨系统对账（两个 API / 两个库）—— domain-ownership.md §5 明文要求
        #   · 同表两侧独立算（流水累计 vs balance_after 快照）
        cross = (_has(run, "join", "union", "except", "diff ")
                 or len(re.findall(r"\bFROM\s+\w+", run, re.I)) >= 2
                 or run.count("curl") >= 2 or run.count("$(") >= 2
                 or _has(run, "abs(", "sum(") )
        if not cross:
            return ("warning",
                    "DATA 断言看不出「两侧独立算、断差值为 0」的对账形态。"
                    "合法形态包括：跨表 JOIN / 跨系统两个 API 对账 / 同表流水累计 vs 快照")
        return None

    if form == "STATE":
        if not _re_any(run, [f"({'|'.join(REJECT_WORDS)})"]) and not _has(run, "!=", "! ", "not "):
            return ("warning",
                    "STATE 断言看不出「非法转移被拒」用例——只断终态存在的话，"
                    "状态机允许了不该允许的跳转照样绿")
        return None

    return None


def check_accept_form_strength(tickets, profile):
    """C5：accept 每条 form ∈ 白名单，且 run 的**内容**满足该 form 的强制形态。

    由来：一个 AI 批量产出几十份 accept，弱断言会系统性成批出现——
    `COUNT(*) > 0` 这种，菜单插错父级 / path 为空照样绿。**只查 accept 存在性
    不够，必须查形态**（见 xuqiu/templates/accept-assertion-forms.md）。

    判定是启发式的，不追求 100% 判准：
      - 命中反模式且无强证据 → error（阻塞）。宁可误伤逼人写强一点，
        也不能像历史版本那样整块留 TODO —— 那等于这层防线不存在。
      - 判不准的边缘情况 → warning（不阻塞）。
    误伤的处理方式是把断言写强，不是放宽这里的规则。

    覆盖不到的（诚实）：run 里调外部脚本（`bash verify.sh`）时本函数看不见内容。
    这类只能靠「冻结后抽 20% accept 做变异测试」补，那是运行时独立 agent 的活。
    """
    errors = []
    warnings = []
    has_ddl = False
    for tid, rec in tickets.items():
        fm = rec["fm"] or {}
        accept = fm.get("accept")
        if not isinstance(accept, list):
            continue  # C4 已报
        for i, a in enumerate(accept):
            if not isinstance(a, dict):
                continue
            form = a.get("form")
            if form == "DDL":
                has_ddl = True
            if form and form not in VALID_ACCEPT_FORMS:
                errors.append({"ticket": tid, "field": f"accept[{i}].form",
                               "msg": f"form={form!r} 不在白名单 {sorted(VALID_ACCEPT_FORMS)}"})
                continue
            if not form:
                continue  # C4 已报缺 form
            run = str(a.get("run") or "")

            # ── counterfeit：一句话「把实现改成什么样，这条会红」 ──────────
            # 静态 lint 判不了断言强不强（`bash verify.sh` 直接失明），但能判
            # **写断言的人有没有想过它能拦住什么**。写不出反例的断言基本都是装饰品。
            # 实测（2026-08 T006）：39 条 accept 里 35 条「实现坏了照样绿」。
            # 在这里补一行的代价是改 yaml，等到 ②zhixing 的 QA 门才发现是整轮返工。
            # 故意留 warning 不留 error：这是提示写作质量，不是可判定的机器性质，
            # 用 error 拦只会逼出一句应付的废话。
            cf = str(a.get("counterfeit") or "").strip()
            if len(cf) < 8:
                warnings.append({"ticket": tid, "field": f"accept[{i}].counterfeit",
                                 "msg": "缺反例（把实现改成什么样，这条会红）"})

            # run 是外部脚本调用 → 本静态 lint 看不到内容。这是已知的一键逃逸路径，
            # 标 warning 并提示：真正的强度保证只能靠变异测试，不是靠这个 lint。
            if re.match(r"^\s*(bash|sh|\./|python3?\s+\S+\.py)\s+\S+", run) and len(run) < 120:
                warnings.append({"ticket": tid, "field": f"accept[{i}].run",
                                 "msg": f"run 调外部脚本，形态无法静态校验——"
                                        f"确保脚本内满足 {form} 形态（本 lint 看不见脚本内容）"})
                continue
            pf = _public_fields_for(profile, str(fm.get("system") or ""))
            verdict = _strength_verdict(form, run, pf)
            if verdict:
                level, msg = verdict
                (errors if level == "error" else warnings).append(
                    {"ticket": tid, "field": f"accept[{i}]({form})", "msg": msg})
    if has_ddl and not (profile or {}).get("_explicit"):
        errors.append({"ticket": "-", "field": "lint-profile",
                       "msg": "存在 DDL form 的 accept，但公共字段集来自内置默认（RuoYi 六字段）"
                              "——写 doc/lint-profile.yaml：`stack: <栈id>`（解析 "
                              "~/.claude/stacks/<id>/profile.yaml）或显式 public_fields。"
                              "断言强度不该取决于碰巧对的默认值"})
    return errors, warnings


# ---------------------------------------------------------------------------
# C6 touches 非空
# ---------------------------------------------------------------------------

def check_touches_nonempty(tickets):
    """C6：touches 非空 list。

    空 touches = 模块占用锁失效，合流互斥算不出来，四轨并行会撞车。悲观声明原则：
    宁可多列（共享文件 api barrel / 路由表 / i18n 词表 / 公共组件必须显式列）。
    这里只校验非空；glob 与实际 diff 的对账是运行时合流的 touches 罚则，不在此。
    """
    errors, warnings = [], []
    for tid, rec in tickets.items():
        fm = rec["fm"] or {}
        touches = fm.get("touches")
        if not isinstance(touches, list) or len(touches) == 0:
            errors.append({"ticket": tid, "field": "touches",
                           "msg": "touches 为空（模块占用锁失效，合流互斥算不出）"})
            continue
        # 以通配符开头的 glob（`**/i18n/**`、`*`）定位不到子树 → 调度器只能悲观
        # 判定「与一切冲突」，该 ticket 会被迫全局串行。安全但**吃掉并发度**。
        # 目标是"能并发就并发"，所以提示改成精确前缀。
        wild = [g for g in touches
                if str(g).strip().lstrip("./").startswith(("*", "?", "["))]
        if wild:
            warnings.append({
                "ticket": tid, "field": "touches",
                "msg": f"{len(wild)} 条 glob 以通配符开头（{', '.join(map(str, wild[:3]))}）——"
                       f"定位不到子树，调度器只能判「与一切冲突」，该 ticket 会被迫全局串行。"
                       f"改成精确前缀（如 `src/locales/**` 而非 `**/i18n/**`）可恢复并发"})
    return errors, warnings


# ---------------------------------------------------------------------------
# C7 任务归属 + 需求来源
# ---------------------------------------------------------------------------

def check_phase_and_reqrefs(tickets):
    """C7：phase 非空标量 + req_refs 非空 list + blueprint_refs 非空 list。

    phase 空 = 该 ticket 不属于任何**任务(daily)**，按任务串行跑时会被静默跳过
    ——比报错更糟，因为看起来一切正常但功能就是没做。

    req_refs 空 = 说不清这个 ticket 在满足甲方哪条需求（镀金嫌疑）。这里只查非空；
    「REQ 是否真存在 / 有没有需求漏拆」由 xuqiu/scripts/coverage_lint.py 深校
    （它同时持有 REQ 清单和 ticket 两侧，本 lint 只看得到 ticket 一侧）。

    blueprint_refs 空 = 该 ticket **永久脱离 authority_lint impact 的反查链**：
    蓝图翻案时它不会被找到、旧 accept 继续绿着错（绿着错比红着错危险）。
    2026-08-16 审计实证：`blueprint_refs: []` 此前三条 lint 全绿——只要同一个锚被
    别的 ticket 正确引用过，连 authority_lint 的兜底警告都不会响。C1 只查字段
    在不在（缺席/None），空数组一直放行，这里补上。写不出锚 = 说不清建在哪份
    权威上，回 ①xuqiu 补，别留空混过去。
    """
    errors = []
    for tid, rec in tickets.items():
        fm = rec["fm"] or {}
        # 字段整个缺席的情况 C1 已报，这里只查「在但为空」，避免同一问题报两条
        if "phase" in fm and fm.get("phase") is not None:
            phase = fm["phase"]
            if (isinstance(phase, str) and not phase.strip()) or phase == []:
                errors.append({"ticket": tid, "field": "phase",
                               "msg": "phase 为空（不属于任何任务/daily，串行跑时会被静默跳过）"})
        if "req_refs" in fm and fm.get("req_refs") is not None:
            req_refs = fm["req_refs"]
            if not isinstance(req_refs, list) or len(req_refs) == 0:
                errors.append({"ticket": tid, "field": "req_refs",
                               "msg": "req_refs 为空（说不清满足甲方哪条需求；"
                                      "覆盖率深校跑 xuqiu/scripts/coverage_lint.py）"})
        if "blueprint_refs" in fm and fm.get("blueprint_refs") is not None:
            bp = fm["blueprint_refs"]
            if not isinstance(bp, list) or len(bp) == 0:
                errors.append({"ticket": tid, "field": "blueprint_refs",
                               "msg": "blueprint_refs 为空——空数组 = 永久脱离 authority_lint "
                                      "impact 反查：蓝图翻案时找不到这个 ticket、旧 accept "
                                      "继续绿着错。写不出锚就回 ①xuqiu 补，别留空"})
    return errors


# ---------------------------------------------------------------------------
# 报告 + manifest + main
# ---------------------------------------------------------------------------

def _load_phase_plan_order(tickets_dir):
    """→ (order|None, err|None)。phase-plan.yaml 的显式定序
    （<tickets_dir>/../phase-plan.yaml，与 lint-profile 同惯例；task_state 同约定）。"""
    if not tickets_dir:
        return None, None
    path = os.path.join(os.path.dirname(os.path.abspath(tickets_dir)), "phase-plan.yaml")
    if not os.path.exists(path):
        return None, None
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return None, "phase-plan.yaml 存在但无 pyyaml——装 pyyaml，别让 C8 静默退化"
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return None, f"phase-plan.yaml 解析失败: {e}"
    order = {}
    for i, p in enumerate(data.get("phases") or []):
        if isinstance(p, dict) and p.get("id"):
            try:
                order[str(p["id"]).strip()] = int(p.get("order", (i + 1) * 10))
            except (TypeError, ValueError):
                return None, f"phase {p.get('id')} 的 order 不是整数"
    return (order or None), None


def check_phase_order(tickets, tickets_dir=None):
    """C8：depends_on 不许指向**后面的任务**。→ (errors, warnings)

    定序来源两档：
    ① doc/phase-plan.yaml 存在 → 用显式 order；phase 不在 yaml 里 = **error**
      （返工任务 D9X/D-FIX-N 也必须登记，否则 init/sync 同样拒收）。
    ② 无 yaml → 退回名字正则排序；**排不了序的 phase 报 warning 而不是静默跳过**
      ——2026-08-16 审计实证：一个实战项目61 个任务里 30 个是 D-FIX-N 这类名字，
      旧版对它们一个字不说，C8 形同虚设且无人知晓（本轮唯一真正无声的洞）。
    """
    errors, warnings = [], []
    plan, perr = _load_phase_plan_order(tickets_dir)
    if perr:
        errors.append({"ticket": "-", "field": "phase-plan.yaml", "msg": perr})
        return errors, warnings

    if plan:
        # 显式定序：phase 必须全部登记
        order = {}
        for tid, rec in tickets.items():
            ph = str((rec["fm"] or {}).get("phase") or "").strip()
            if not ph:
                continue                     # C7 已报
            if ph not in plan:
                errors.append({"ticket": tid, "field": "phase",
                               "msg": f"phase {ph} 不在 phase-plan.yaml 里——"
                                      f"返工任务也要登记 id + order 才能定序"})
                continue
            order[tid] = plan[ph]
        for tid, rec in tickets.items():
            if tid not in order:
                continue
            for dep in ((rec["fm"] or {}).get("depends_on") or []):
                dep = str(dep).strip()
                if dep not in order:
                    continue
                if order[dep] > order[tid]:
                    errors.append({"ticket": tid, "field": "depends_on",
                                   "msg": f"依赖 {dep}(order {order[dep]}) 在**后面的任务**"
                                          f"（本 ticket order {order[tid]}）——任务串行，"
                                          f"这条永远等不到，任务划分要改"})
        return errors, warnings

    # 无 yaml：名字正则档
    order = {}
    unord = set()
    for tid, rec in tickets.items():
        ph = str((rec["fm"] or {}).get("phase") or "").strip()
        m = re.match(r"^([A-Za-z]*)(\d+)$", ph)
        if m:
            order[tid] = (m.group(1), int(m.group(2)))
        elif ph:
            unord.add(ph)
    if unord:
        warnings.append({"ticket": "-", "field": "phase",
                         "msg": f"C8 对这些任务**未生效**（名字排不了序）: "
                                f"{', '.join(sorted(unord))}——建 doc/phase-plan.yaml "
                                f"显式定序（返工任务 D9X/D-FIX-N 全靠它）"})
    for tid, rec in tickets.items():
        if tid not in order:
            continue
        for dep in ((rec["fm"] or {}).get("depends_on") or []):
            dep = str(dep).strip()
            if dep not in order:
                continue
            if order[dep][0] == order[tid][0] and order[dep][1] > order[tid][1]:
                dph = (rec["fm"] or {}).get("phase")
                errors.append({"ticket": tid, "field": "depends_on",
                               "msg": f"依赖 {dep} 在**后面的任务**（{tid}@{dph} ← "
                                      f"{dep}@{order[dep][0]}{order[dep][1]}）——"
                                      f"任务是串行的，这条永远等不到，任务划分要改"})
    return errors, warnings


def run_all(tickets, profile, tickets_dir=None):
    """跑全部校验，返回 (errors, warnings, cycles)。"""
    errors = []
    warnings = []
    errors += check_frontmatter_present(tickets)
    from workflow_contract import ticket_errors
    for tid, item in tickets.items():
        for message in ticket_errors(item.get("fm") or {}):
            errors.append({"ticket": tid, "field": "accept/ui", "msg": message})
    errors += check_depends_exist(tickets)
    cyc_errs, cycles = check_acyclic(tickets)
    errors += cyc_errs
    errors += check_accept_parsable(tickets)
    form_errs, form_warns = check_accept_form_strength(tickets, profile)
    errors += form_errs
    warnings += form_warns
    touch_errs, touch_warns = check_touches_nonempty(tickets)
    errors += touch_errs
    warnings += touch_warns
    errors += check_phase_and_reqrefs(tickets)
    c8_errs, c8_warns = check_phase_order(tickets, tickets_dir)
    errors += c8_errs
    warnings += c8_warns
    return errors, warnings, cycles


def print_report(tickets, errors, warnings):
    """人类可读分组报告到 stdout。"""
    print(f"dag_lint: 扫描 {len(tickets)} 个 ticket")
    if errors:
        print(f"\n❌ ERROR ({len(errors)}) — 结构性问题，禁止开跑：")
        for e in errors:
            print(f"  [{e['ticket']}] {e['field']}: {e['msg']}")
    if warnings:
        ncf = sum(1 for w in warnings if w["field"].endswith(".counterfeit"))
        print(f"\n🟡 WARNING ({len(warnings)}) — 建议修，不阻塞：")
        if ncf:
            print(f"  ★ 其中 {ncf} 条是「缺反例」：每条 accept 要有一句话说清"
                  f"**把实现改成什么样，这条会红**。写不出的基本都是装饰品。")
            print(f"    实测（2026-08 T006）：39 条 accept 有 35 条「实现坏了照样绿」，"
                  f"D1 的 QA 门为此跑了 7 轮。")
            print(f"    在这里补一行的代价是改 yaml；等到 ②zhixing 的 QA 门才发现，"
                  f"代价是**整轮返工**。")
            print(f"    八种见过的假绿形态：xuqiu/templates/accept-assertion-forms.md")
        for w in warnings:
            print(f"  [{w['ticket']}] {w['field']}: {w['msg']}")
    if not errors and not warnings:
        print("\n✅ 结构全过。别忘了还要跑 coverage_lint 查需求漏拆。")
    elif not errors:
        print("\n✅ 无结构性错误（上面是断言强度提示，不阻塞）。"
              "\n   ⚠️ C5 是**启发式提示不是保证**：静态分析拦不住所有弱断言"
              "（`run: bash x.sh` 就看不见）。真正的强度检验是变异测试——"
              "\n   蓝图冻结后抽 20% accept，往被测对象注入已知类事故，看断言是否仍绿。"
              "\n   别忘了还要跑 coverage_lint 查需求漏拆。")


def emit_manifest(workdir, tickets, errors, warnings, cycles):
    """headless 模式：写 _lint.json（独立文件，不撞 zhixing 主 _manifest.json）。"""
    manifest = {
        "skill": "dag-lint",
        "status": "fail" if errors else "pass",
        "counts": {"tickets": len(tickets), "errors": len(errors), "warnings": len(warnings)},
        "errors": errors,
        "warnings": warnings,
        "cycles": cycles,
    }
    out = os.path.join(workdir, "_lint.json")  # 独立文件；zhixing 读它再翻译进主 _manifest.json
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def main(argv):
    if len(argv) > 1 and argv[1] in ("-h", "--help"):
        print(__doc__)
        print("用法: dag_lint.py [tickets目录]   （省略则自动探测工作区根，兜底 doc/tickets）\n"
              "退出码: 0 全过 / 1 有 error（禁止开跑）/ 2 仅 warning\n"
              "配套: 需求覆盖率查漏拆用 xuqiu/scripts/coverage_lint.py（本 lint 不管）")
        return 0
    # standalone: argv[1] = tickets 目录；headless: 读 workdir/_input/tickets_dir。
    workdir = os.environ.get("DAG_WORKDIR")  # 编排层 传；standalone 为 None
    tickets_dir = argv[1] if len(argv) > 1 else None
    if tickets_dir is None and workdir:
        # TODO(headless): 从 workdir/_input 读实际 tickets 目录约定
        tickets_dir = os.path.join(workdir, "_input", "tickets")
    if tickets_dir is None:
        # 工作区根自动探测：显式位置参数 > $ZHIXING_WORKDIR > 拍平/doc 探测 > 老默认
        tickets_dir = os.path.join(resolve_ws(None), "tickets")

    if not os.path.isdir(tickets_dir):
        print(f"dag_lint: tickets 目录不存在: {tickets_dir}", file=sys.stderr)
        return 1

    tickets, dups = load_tickets(tickets_dir)
    if not tickets:
        print(f"dag_lint: 扫描 0 个 ticket", file=sys.stderr)
        print(f"[error] {tickets_dir} 下没扫到任何 ticket —— 期望 <目录>/**/prompt.md。\n"
              f"        正文文件必须固定叫 prompt.md（叫别的名不会被扫到，"
              f"这曾让目录组织错误拿到绿灯）。\n"
              f"        零 ticket 绝不是「全过」，不给绿灯。", file=sys.stderr)
        return 1
    profile = load_lint_profile(tickets_dir)
    errors, warnings, cycles = run_all(tickets, profile, tickets_dir)
    errors = dups + errors
    print_report(tickets, errors, warnings)

    if workdir:
        emit_manifest(workdir, tickets, errors, warnings, cycles)

    # 退出码：0 全过 / 1 有 error / 2 仅 warning。
    if errors:
        return 1
    if warnings:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
