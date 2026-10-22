#!/usr/bin/env python3
"""ssot_gen.py — 字段表 SSOT 单向生成器（骨架 · renderer 按栈实现）

三件套 ①xuqiu 的工具。**字段表 SSOT 是每字段的唯一真相源**，一处改字段 →
脚本重生成产物，消灭"字段名三层对不上"这一整类跨层契约事故。

═══════════════════════════════════════════════════════════════════════════
⚠️ 未实现的 target 会**报错退出**，绝不产出 TODO 占位文件
═══════════════════════════════════════════════════════════════════════════
历史版本的 renderer 全是 TODO，却仍 `exit 0` 并写出一个内容是注释、文件名叫
`V<版本时间戳>__<TICKET-ID>-xxx.sql` 的"产物"——**静默假绿，比直接失败危险得多**：
调用方（人或流水线）会以为生成成功了。

现在的契约：`IMPLEMENTED_TARGETS` 声明本项目实现了哪些 target；未声明的直接
exit 3 并指出该改哪个函数。**要么真生成，要么明确失败。**

═══════════════════════════════════════════════════════════════════════════
换项目时哪些能复用（ssot_gen 的复用边界）
═══════════════════════════════════════════════════════════════════════════
  ✅ 100% 复用（换栈不用动）：
       SSOT schema（templates/field-ssot.schema.yaml）· 本文件的 CLI / 解析 /
       公共字段自动附加 / 校验 / 幂等与不覆盖策略 —— 约 80% 的代码量
  🔁 同类栈复用（如都是 SQL 库）：
       render_ddl —— 换方言改类型映射即可（MySQL↔PG 改几行）
  ❌ 每栈重写（各约 20-30 行）：
       render_entity（绑后端框架）· render_ts（绑前端类型系统）

  所以**推荐只实现 render_ddl**：DDL 最机械、最容易漏（漏 create_dept 会在
  INSERT 时才炸）、且是迁移工具下不可回改的一次性产物，值得机器保证。
  entity / TS 让 AI 直接照 SSOT 写更省事——它们本来就要人为加业务注解，
  生成后再手改会破坏"单向生成、产物可重生成"的契约（改了下次重跑被覆盖）。
  entity/TS 的一致性靠 --check 校验（见下），不靠生成。

═══════════════════════════════════════════════════════════════════════════
契约（CONTRACT）
═══════════════════════════════════════════════════════════════════════════

输入：field-ssot.yaml —— **权威格式见 templates/field-ssot.schema.yaml（那份是唯一真相源，
    本脚本按它解析）**。下面是最小样例，key 名与 schema 一致：
    type_map: { str: {...}, int: {...}, dict: {...}, ... }   # 逻辑类型 → DDL/entity/TS 类型映射（全表复用）
    common_field_blocks:            # 公共字段块（审计/租户），表按名引用，不逐表重写
      audit:  [ {name: create_by, ...}, {name: create_dept, ...}, {name: del_flag, ...} ]
      tenant: [ {name: tenant_id, ...} ]
    tables:
      - table: t_warehouse_demand   # ★ 物理表名（key 是 `table`，不是 `name`；兼容旧 `name`）
        entity: WarehouseDemand     # entity 类名（缺省由表名 PascalCase 派生）
        comment: 仓库需求单
        common_fields: [audit, tenant]   # 引用上面的块名 → normalize 展开成真实公共字段列
        fields:
          - { name: demand_no, type: str, length: 32, nullable: false,
              unique: uk_demand_no, comment: 需求单号 }   # unique 组名；多租户下 render 时自动前置 tenant_id
          - { name: status, type: dict, length: 20, dict: <字典key>,
              nullable: false, default: draft, comment: 单据状态 }

输出：--out 目录下三份（按 --targets 选择生成哪些）
    ddl     → 迁移DDL 文件（迁移工具的版本化脚本，文件名带时间戳/版本前缀 + ticket id）
    entity  → 后端 entity 骨架（ORM 注解 + 字段声明，业务逻辑不生成）
    ts      → 前端 TS 类型（interface / type，字典字段可带联合类型或注释）

契约保证：
  1. **单向**：只 SSOT → 三份产物，绝不反向回写 SSOT。产物是可重生成的，改字段改 SSOT。
  2. **公共字段自动附加**：每表三份产物都自动带 PUBLIC_FIELDS 全集，SSOT 里不用重复写，
     也防漏（漏 create_dept 是真实高频事故：INSERT 报 Unknown column 'create_dept'）。
  3. **幂等**：同一 SSOT 重跑产出内容一致（迁移DDL 除文件名时间戳外）；已存在同名迁移
     文件默认不覆盖（迁移工具铁律：已应用的迁移不可改 checksum）——见 write 阶段 TODO。
  4. **纯 stdlib**：解析优先 pyyaml，未装则走极简手解析兜底（见 load_ssot）。不引其他依赖。

退出码：0 全部 target 成功；非 0 = 解析失败 / 未知 target / 写盘失败（消息进 stderr）。

═══════════════════════════════════════════════════════════════════════════
双模式（standalone / 编排层 headless）—— 本脚本两模式共用，无差异
═══════════════════════════════════════════════════════════════════════════
  standalone：负责人 手动 `python3 ssot_gen.py --ssot field-ssot.yaml --out gen/`。
  编排层 headless：SKILL 流程里被调，--ssot 指 <workdir>/_input 下的 SSOT，--out 指
    产物目录；本脚本不吐 _manifest.json（那是 SKILL 编排层的活，工人脚本只管生成）。
"""

from __future__ import annotations   # 兼容 Python 3.9（PEP 604 注解）

import argparse
import sys
from pathlib import Path

# ── 公共字段全集（每表自动附加，SSOT 里不写）────────────────────────────────
# 6 公共字段 + 租户列。accept 的 DDL 断言会逐个断这些列存在（防漏 create_dept）。
# 逻辑类型栈无关，render_* 里映射到各自方言/类型系统。
# TODO(按栈): 若本项目公共字段集不同（如无多租户、或 del_flag 命名不同），改这里一处即可。
PUBLIC_FIELDS = [
    {"name": "tenant_id",   "type": "varchar(20)", "nullable": False, "comment": "租户号"},
    {"name": "create_dept", "type": "bigint",      "nullable": True,  "comment": "创建部门"},
    {"name": "create_by",   "type": "bigint",      "nullable": True,  "comment": "创建者"},
    {"name": "create_time", "type": "datetime",    "nullable": True,  "comment": "创建时间"},
    {"name": "update_by",   "type": "bigint",      "nullable": True,  "comment": "更新者"},
    {"name": "update_time", "type": "datetime",    "nullable": True,  "comment": "更新时间"},
    {"name": "del_flag",    "type": "char(1)",     "nullable": False, "default": "0", "comment": "删除标记"},
]

VALID_TARGETS = ("ddl", "entity", "ts")

# ── 本项目实现了哪些 target ──────────────────────────────────────────────────
# 实现一个 renderer（把它的 NotImplementedError 换成真模板）就把名字加进来。
# 不在这个集合里的 target：main() 直接 exit 3 报错，**不产出任何文件**。
# 空集合 = 本项目还没实例化任何 renderer，跑什么都会明确失败（这是对的）。
#
# 推荐只开 "ddl"（理由见文件头「复用边界」）：entity/TS 让 AI 照 SSOT 直接写，
# 一致性靠 `--check` 校验，别用生成器——生成的 entity 迟早要手加业务注解，
# 一手改就破坏了"产物可重生成"的单向契约。
IMPLEMENTED_TARGETS: set[str] = set()


# ═══════════════════════════════════════════════════════════════════════════
# 解析：field-ssot.yaml → dict
# ═══════════════════════════════════════════════════════════════════════════
def load_ssot(path: Path) -> dict:
    """读 SSOT YAML → dict，结构见 docstring 顶部 CONTRACT。

    优先 pyyaml（第三方，但 SSOT YAML 常有嵌套/列表，pyyaml 最稳）；
    未装则走手解析兜底——只支持 docstring 里那种扁平缩进结构，够 stub 跑通。
    TODO(按栈): 生产建议直接 `pip install pyyaml`，删掉手解析分支。
    """
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # 依赖: pyyaml（未装走下面手解析）
        return yaml.safe_load(text) or {}
    except ImportError:
        sys.stderr.write("[warn] pyyaml 未装，走极简手解析（仅支持扁平 SSOT，生产请装 pyyaml）\n")
        # TODO(按栈): 手解析兜底——最小实现即可，只需支持 docstring 样例的缩进结构。
        #   建议按行扫：`tables:` → 遇 `- name:` 起新表 → `fields:` 段内 `- name:` 起新字段，
        #   `key: value` 收进当前对象。不追求覆盖 YAML 全规范，覆盖本 schema 用到的 key 即可。
        return _hand_parse_ssot(text)


def _hand_parse_ssot(text: str) -> dict:
    """pyyaml 缺席时的兜底解析（stub）。

    返回结构必须与 pyyaml 分支一致：{"tables": [{"name","entity","comment","fields":[...]}]}
    TODO(按栈): 实现上面 load_ssot 注释里描述的缩进扫描。stub 先返回空表结构占位，
      让 --help / 空跑不炸；填了才真出内容。
    """
    # TODO(按栈): 真实缩进扫描。以下为占位，返回空以便骨架可跑通。
    return {"tables": []}


def normalize_table(table: dict, common_field_blocks: dict | None = None) -> dict:
    """把单表补全成 render 统一入参：显式字段 + 公共字段块展开。

    对齐 templates/field-ssot.schema.yaml（权威格式）：
    - 表名读 `table`（兼容旧 `name`）；entity 缺省由表名 PascalCase 派生。
    - 公共字段优先按表的 `common_fields:[块名]` 从 SSOT 顶层 `common_field_blocks` 展开；
      无引用/无块时回退内置 PUBLIC_FIELDS（防漏 create_dept）。
    - 校验：表名 + fields 必填，缺则抛错（fail fast，别静默生成半成品）。
    返回 {"name","entity","comment","fields":[...]}，fields 含公共字段。
    """
    name = table.get("table") or table.get("name")
    if not name:
        raise ValueError(f"SSOT 表缺 table 名: {table!r}")
    entity = table.get("entity") or "".join(
        w.capitalize() for w in str(name).removeprefix("t_").split("_") if w)
    biz_fields = list(table.get("fields") or [])
    # 公共字段：按 common_fields 引用从 common_field_blocks 展开；无引用则内置 PUBLIC_FIELDS 兜底。
    refs = table.get("common_fields")
    if refs and common_field_blocks:
        public = [dict(f) for blk in refs for f in (common_field_blocks.get(blk) or [])]
    else:
        public = [dict(f) for f in PUBLIC_FIELDS]
    public_names = {f.get("name") for f in public}
    biz_fields = [f for f in biz_fields if f.get("name") not in public_names]
    return {
        "name": name,
        "entity": entity,
        "comment": table.get("comment", ""),
        "fields": biz_fields + public,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 渲染：每 target 一个函数。函数体是 stub —— 结构清楚、留 TODO 按栈实例化。
# 约定：render_* 输入 = normalize_table 后的单表 dict；输出 = (文件名, 文件内容字符串)。
# ═══════════════════════════════════════════════════════════════════════════
def render_ddl(table: dict) -> tuple[str, str]:
    """渲染 迁移DDL（CREATE TABLE）。

    该渲染什么：
      - `CREATE TABLE <name> (...)`，逐字段一行。
      - 逻辑 type → 具体 SQL 方言（如 varchar(32)/tinyint/datetime，按迁移工具目标库映射）。
      - nullable / default / COMMENT 落到列定义。
      - **UNIQUE 约束必含租户列**（unique 字段单独或联合建 UNIQUE KEY 时把 tenant_id 一起带上，
        否则多租户下唯一性错——这是硬约束，render 时别漏）。
      - 公共字段已在 table["fields"] 里（含 create_dept，别再漏）。
      - 文件名带 **版本/时间戳前缀 + ticket id**（迁移工具按版本顺序应用；stub 先用占位名）。

    实现步骤（约 30 行）：
      1. 写 type_map：逻辑 type → SQL 方言列类型（读 SSOT 顶层 type_map，别硬编码）
      2. 逐字段拼 `<列名> <类型> [NOT NULL] [DEFAULT x] COMMENT '<注释>'`
      3. 收集 field.unique 同名的字段建 UNIQUE KEY，**多租户项目自动前置租户列**
      4. 文件名按迁移工具规范：版本前缀 + ticket id + 表名
      5. 实现完把 "ddl" 加进 IMPLEMENTED_TARGETS
    """
    raise NotImplementedError(
        "render_ddl 未按本项目的迁移工具实例化。\n"
        "  改这里：scripts/ssot_gen.py::render_ddl（约 30 行：类型映射 + 列定义拼接 + "
        "UNIQUE 含租户列 + 文件名规范）\n"
        "  实现后把 'ddl' 加进 IMPLEMENTED_TARGETS。")


def render_entity(table: dict) -> tuple[str, str]:
    """渲染 后端 entity 骨架。

    该渲染什么：
      - entity 类声明（table["entity"]），ORM 表名注解绑到 table["name"]。
      - 逐字段声明：逻辑 type → 后端语言类型（varchar→String / tinyint→Integer / datetime→时间类型…）。
      - **公共字段通常由框架基类（BaseEntity/TenantEntity）自动带**——若你的框架如此，
        render 时应 **只渲染业务字段 + 让 entity extends 基类**，不重复声明公共字段
        （一个实战项目即此：继承 TenantEntity，别把 6 公共字段再写一遍）。TODO 里二选一。
      - 只生成结构骨架，不生成业务逻辑 / getter-setter（若语言需要，用 lombok 类注解等）。

    ⚠️ **默认不建议实现**（见文件头「复用边界」）：entity 生成后迟早要手加业务注解，
       一手改就破坏"产物可重生成"的单向契约。让 AI 照 SSOT 直接写，一致性用
       `--check` 校验。确实要生成再实现，并把 "entity" 加进 IMPLEMENTED_TARGETS。
    """
    raise NotImplementedError(
        "render_entity 未实例化（且默认不建议实现——见文件头「复用边界」）。\n"
        "  推荐替代：让 AI 照 SSOT 写 entity，用 `ssot_gen.py --check` 校验一致性。")


def render_ts(table: dict) -> tuple[str, str]:
    """渲染 前端 TS 类型。

    该渲染什么：
      - `export interface <Entity> { ... }`，逐字段一行。
      - 逻辑 type → TS 类型（varchar→string / tinyint,bigint→number / datetime→string…）。
      - nullable 字段标 `?` 或 `| null`。
      - **dict 字段**：可渲染成字典 key 的联合类型或至少加注释标注关联字典 key（前端下拉对照用）。
      - **禁 any**（强约束：不写 any）——类型拿不准也用 unknown 或具体基元，别 any 兜底。
      - 公共字段可选择性生成（前端列表/详情常用 create_time/create_by，按需保留）。

    ⚠️ **默认不建议实现**（同 render_entity）：前端类型常要按 VO/DTO 裁剪，
       生成后手改即破坏单向契约。让 AI 照 SSOT 写，用 `--check` 校验。
    """
    raise NotImplementedError(
        "render_ts 未实例化（且默认不建议实现——见文件头「复用边界」）。\n"
        "  推荐替代：让 AI 照 SSOT 写 TS 类型，用 `ssot_gen.py --check` 校验一致性。")


RENDERERS = {
    "ddl": render_ddl,
    "entity": render_entity,
    "ts": render_ts,
}


# ═══════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════
def write_output(out_dir: Path, target: str, filename: str, content: str) -> Path:
    """写单份产物到 out_dir/<target>/<filename>。

    TODO(按栈): 迁移DDL 已存在同名文件时**默认不覆盖**（迁移工具铁律：已应用迁移改
      checksum 会导致启动失败）。stub 直接写；生产给 ddl 加 exists 检查跳过 + 提示。
    """
    sub = out_dir / target
    sub.mkdir(parents=True, exist_ok=True)
    dest = sub / filename
    dest.write_text(content, encoding="utf-8")
    return dest


def check_consistency(tables: list[dict], roots: list[Path]) -> list[str]:
    """--check：SSOT 声明的字段名，在给定代码树里能否找到（一致性校验，非生成）。

    这是**栈无关**的那一半价值：不生成代码，只回答「SSOT 改了字段，代码跟上了没有」。
    比生成器便宜得多、换项目直接能用，且不和手写代码打架——推荐 entity/TS 走这条路，
    只有 DDL 用生成（DDL 是一次性不可回改产物，值得机器保证）。

    判定（故意粗粒度、只报可疑，不做语法分析）：
      SSOT 里的列名（snake_case）与其 camelCase 形式，两者在代码树里都搜不到
      → 报"疑似未落地"。粗，但足以逮住"改了 SSOT 忘了改代码"这个主要事故。
    """
    issues = []
    corpus = []
    for root in roots:
        if not root.exists():
            issues.append(f"[check] 路径不存在，跳过: {root}")
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in (".java", ".kt", ".py", ".ts", ".tsx",
                                            ".vue", ".xml", ".sql", ".go"):
                try:
                    corpus.append(p.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
    blob = "\n".join(corpus)
    if not blob:
        issues.append("[check] 代码树里没读到任何源文件——路径给对了吗？")
        return issues
    public = {f["name"] for f in PUBLIC_FIELDS}
    for t in tables:
        for f in t["fields"]:
            name = f.get("name", "")
            if not name or name in public:
                continue          # 公共字段由框架基类带，代码里通常不出现
            parts = name.split("_")
            camel = parts[0] + "".join(w.capitalize() for w in parts[1:])
            if name not in blob and camel not in blob:
                issues.append(f"[check] {t['name']}.{name} —— SSOT 里有，代码树里搜不到"
                              f"（{name} / {camel} 都没命中）：改了 SSOT 忘改代码？")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ssot_gen.py",
        description="字段表 SSOT 单向生成器 + 一致性校验器",
        epilog="未实现的 target 会报错退出，绝不产出 TODO 占位文件（防静默假绿）。",
    )
    parser.add_argument("--ssot", required=True, type=Path,
                        help="field-ssot.yaml 路径（结构见 templates/field-ssot.schema.yaml）")
    parser.add_argument("--out", type=Path,
                        help="产物输出目录（生成模式必填；--check 模式不用）")
    parser.add_argument("--targets", nargs="+", default=["ddl"],
                        choices=VALID_TARGETS, metavar="TARGET",
                        help=f"生成哪些产物，默认 ddl。可选: {', '.join(VALID_TARGETS)}")
    parser.add_argument("--check", nargs="+", type=Path, metavar="CODE_DIR",
                        help="一致性校验模式：给代码树路径，查 SSOT 字段有没有落地（不生成）")
    args = parser.parse_args(argv)

    if not args.ssot.exists():
        sys.stderr.write(f"[error] SSOT 文件不存在: {args.ssot}\n")
        return 1

    try:
        ssot = load_ssot(args.ssot)
    except Exception as e:  # 解析失败 fail fast，别静默半生成
        sys.stderr.write(f"[error] SSOT 解析失败: {e}\n")
        return 1

    raw_tables = ssot.get("tables") or []
    if not raw_tables:
        # 空 SSOT 从来不是"成功"——要么文件写错了，要么手解析兜底没实现。别 exit 0。
        sys.stderr.write("[error] SSOT 里没有 tables —— 文件写对了吗？"
                         "（pyyaml 未装时手解析兜底不支持复杂结构，装 pyyaml 再跑）\n")
        return 1

    common_field_blocks = ssot.get("common_field_blocks") or {}
    tables = []
    for raw in raw_tables:
        try:
            tables.append(normalize_table(raw, common_field_blocks))
        except ValueError as e:
            sys.stderr.write(f"[error] {e}\n")
            return 1

    # ── 校验模式 ──────────────────────────────────────────────────────────
    if args.check:
        issues = check_consistency(tables, args.check)
        for msg in issues:
            print(msg)
        if issues:
            print(f"\n❌ {len(issues)} 处可疑 —— SSOT 与代码不一致（或字段名映射规则特殊，逐条确认）")
            return 1
        print(f"✅ {len(tables)} 张表的字段在代码树里都能找到")
        return 0

    # ── 生成模式：先查 target 是否真实现，未实现直接失败（不写任何文件）──────
    if not args.out:
        sys.stderr.write("[error] 生成模式需要 --out\n")
        return 1
    unimplemented = [t for t in args.targets if t not in IMPLEMENTED_TARGETS]
    if unimplemented:
        sys.stderr.write(
            f"[error] target 未实例化: {', '.join(unimplemented)}\n"
            f"        本项目已实现: {sorted(IMPLEMENTED_TARGETS) or '(无)'}\n"
            f"        实现方法：改 scripts/ssot_gen.py 里对应的 render_<target>()，\n"
            f"        再把名字加进 IMPLEMENTED_TARGETS。\n"
            f"        —— 不产出任何文件。历史版本在这里吐 TODO 占位文件并 exit 0，\n"
            f"           那是静默假绿，比失败更危险。\n")
        return 3

    written: list[Path] = []
    for table in tables:
        for target in args.targets:
            filename, content = RENDERERS[target](table)
            written.append(write_output(args.out, target, filename, content))

    for p in written:
        print(p)
    print(f"[ok] {len(tables)} 表 × {len(args.targets)} target → {len(written)} 份产物",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
