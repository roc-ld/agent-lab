#!/usr/bin/env python3
"""ssot_gen 的 RuoYi-Vue-Plus（MySQL/Flyway）render_ddl 即贴实现。

用法：把 `render_ddl` 函数体贴进项目 `scripts/ssot_gen.py::render_ddl`（替换
NotImplementedError），并把 "ddl" 加进 IMPLEMENTED_TARGETS。契约与 ssot_gen
docstring 一致：入参 table 是 normalize_table 之后的 dict，返回 (文件名, 内容)。

方言约定（本栈）：
- 类型映射优先读 SSOT 顶层 type_map（table["_type_map"]），缺席用下面的默认映射；
- UNIQUE 必含 tenant_id（多租户唯一性硬约束）+ 末列 del_unique（软删后同键可重建，
  一个实战项目实践，gotchas §4）；
- 文件名 Flyway 规范 V<占位时间戳>__<表名>.sql——真实时间戳由拆 ticket 时发放
  （比现存最大更大 + HHmm 模块段，ADR-0008），生成后人工改名一次。
"""

DEFAULT_TYPE_MAP = {
    "string": "varchar(255)", "string32": "varchar(32)", "string64": "varchar(64)",
    "text": "text", "int": "int", "bigint": "bigint", "decimal": "decimal(12,2)",
    "bool": "char(1)", "date": "date", "datetime": "datetime", "json": "json",
    "id": "bigint",
}


def render_ddl(table: dict) -> tuple[str, str]:
    tmap = dict(DEFAULT_TYPE_MAP)
    tmap.update(table.get("_type_map") or {})
    name = table["name"]
    cols, uniques = [], []
    for f in table["fields"]:
        ftype = tmap.get(str(f.get("type", "string")), f.get("type", "varchar(255)"))
        parts = [f"  `{f['name']}` {ftype}"]
        if not f.get("nullable", True):
            parts.append("NOT NULL")
        if f.get("default") is not None:
            d = str(f["default"])
            # 只有 NULL/CURRENT_TIMESTAMP 裸写；其余一律加引号——数字裸写会被 MySQL
            # 按数值折叠（varchar 列 DEFAULT 000000 → '0'，tenant_id 默认值就中招）
            parts.append(f"DEFAULT {d}" if d.upper() in ("NULL", "CURRENT_TIMESTAMP")
                         else f"DEFAULT '{d}'")
        if f.get("comment"):
            parts.append(f"COMMENT '{f['comment']}'")
        cols.append(" ".join(parts))
        if f.get("unique"):
            uniques.append(f["name"])
    field_names = {f["name"] for f in table["fields"]}
    if uniques:
        ukey = list(uniques)
        # 多租户唯一性：tenant_id 前置；软删可重建：del_unique 收尾（都存在才带）
        if "tenant_id" in field_names and "tenant_id" not in ukey:
            ukey.insert(0, "tenant_id")
        if "del_unique" in field_names and "del_unique" not in ukey:
            ukey.append("del_unique")
        cols.append(f"  UNIQUE KEY `uk_{name}_{'_'.join(uniques)}` "
                    f"({', '.join(f'`{c}`' for c in ukey)})")
    body = ",\n".join(cols)
    comment = table.get("comment", "")
    content = (f"-- ssot_gen 生成：{name}（改 SSOT 重生成，别手改本文件）\n"
               f"CREATE TABLE `{name}` (\n  `id` bigint NOT NULL COMMENT '主键',\n{body},\n"
               f"  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
               f"COMMENT='{comment}';\n")
    return f"V__TODO_TIMESTAMP__{name}.sql", content


if __name__ == "__main__":
    # 自测：一张带 unique + 租户列的表
    demo = {"name": "t_demo_item", "comment": "演示",
            "fields": [
                {"name": "slug", "type": "string32", "nullable": False,
                 "comment": "标识", "unique": True},
                {"name": "tenant_id", "type": "string32", "nullable": False,
                 "default": "000000", "comment": "租户"},
                {"name": "del_flag", "type": "bool", "nullable": False,
                 "default": "0", "comment": "删除标记"},
                {"name": "del_unique", "type": "bigint", "nullable": False,
                 "default": "0", "comment": "软删唯一化"},
            ]}
    fn, sql = render_ddl(demo)
    assert "UNIQUE KEY" in sql and "`tenant_id`, `slug`, `del_unique`" in sql, sql
    print(fn)
    print(sql)
