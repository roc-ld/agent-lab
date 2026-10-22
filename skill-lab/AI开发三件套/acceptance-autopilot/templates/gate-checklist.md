# 四门一策 · 每 phase 先跑什么 / 什么时候 STOP

> 照着做。**每一道门都是程序闸门**：退出码 ≠ 0 就停，不靠自觉。
> `<GATE>` = 项目配置里的 `paths.gate`，`<SHEET>` / `<ROW>` 是当前处理对象。

## 单条处理（主线）

| # | 命令 | 退出码 | 含义与动作 |
|---|---|---|---|
| 1 | `<GATE> image <SHEET> <ROW>` | 0 + `IMAGE=...` | 拿到截图 → **必须真看那张图**（最关键信息） |
| | | 0 + 无图 | 凭描述 + 原型处理；复述卡免 `shot_*` 字段 |
| | | 2 | 已自动记 `blocked_image` → 挂起，列入报人清单，**绝不凭文字猜** |
| 2 | 写复述卡落台账 → `<GATE> restate-check <SHEET> <ROW>` | 0 | 过门，可以动代码 |
| | | 2 | 复述不达标 / 口径门未过 → 补齐再跑 |
| 2b | `<GATE> decisions <SHEET> <ROW>` | 0 | 无现行口径命中，或已正确引用 |
| | | 2 | 命中口径但没引用 → 台账补 `decision_refs`/`decision_note` |
| | | — | 无命中但本条是口径类 → 先登记 clarify（带 fallback）再做，**不停机** |
| 3 | 起栈 → 复现（verify-first） | — | 不复现 → `not_a_bug` + 真实证据；复现 → `fixing` → 改码 → 重测 |
| 4 | 落账（`ledger.upsert` + `save_sheet`） | 抛异常 | **验证门**：证据不齐写不进去。按报错补齐，**不许 force 绕** |
| 5 | 派干净 QA subagent（§4 门） | — | 回填 `audit.{verdict,auditor:independent,evidence}` |
| 6 | `<GATE> audit-coverage` | 0 | 全部 verified+ 都有 `PASS(independent)` |
| | | ≠0 | 有行缺独立验收 → 回去补验，**不得写回** |

## 批量（按日期/按角标）

| # | 命令 | 说明 |
|---|---|---|
| 0 | `sync_shots` 类脚本 | 🔴 **每会话开头必跑**，增量拉取新截图 |
| 0b | `<GATE> confirm-sweep` | 再提探测：已处理项再现 → `regressed`，列顽固清单 |
| 0c | `decisions.py pending` / `closing` / `inbox` | 冷启动三件事：待拍板 / 欠回写 / 甲方答复配对 |
| 1 | `pull.py <SHEET>` | 拉最新（**去掉重复 API 调用，日常查状态用本地台账**） |
| 2 | 过滤 | 只留目标日期且描述非空的行（日期用**数字元组**比较，禁字符串） |
| 3 | 去重 | 跳过"已了结"stage：`verified_*` / `deployed` / `confirmed` / `wont_do` / `not_a_bug` / `clarify` / `blocked_image`。**`triaged` / `fixing` / `fixed_local` / `regressed` 不跳过，续接继续做** |
| 4 | 逐条 | 进上面的单条引擎（四门全走） |
| 5 | **全量 clean-QA** | 一批做完必跑，非可选 |
| 6 | 报告 | 诚实分档：✅verified_device N 条 / verified_e2e N 条 / 🟡fixed_local N 条 / ⛔blocked_image N 条 / ❓clarify N 条。**绝不把 fixed_local 报成「已修」** |

## 写回（唯一对外动作）

```
Tier-1 进度戳：授权标记存在 → <GATE> stamp <SHEET> <ROW> --template accepted|clarify|device [--note '...']
Tier-2 结论：  precheck 过 + 当日授权标记 + audit PASS(independent) → update_range → 独立路径回读复核
```

## STOP 清单（必停报人）

1. 表头自检失败（有人改了表头/列序）
2. 角标 `not_found` / 多命中（行被删 / 真实重复行）
3. 取图门 exit 2 —— 截图拿不到，请人导出/贴图
4. 台账校验反复补不齐 —— 说明流程真没走到（比如视觉类没真机证据）；别 force
5. 同主题 ≥ 阈值条仍 clarify —— 停止逐条修，升级需求级回 ①xuqiu
6. 高风险三类（改状态机枚举 / 改已落库字段语义 / 删功能删数据）没拿到明确拍板
7. **一批里任意一条 verdict ≠ PASS** → 不得带病写回

## 收尾（每次 E2E 后）

- 关掉本轮为测试起的**每一个**后台进程（后端 / 前端 dev server / automator / 浏览器）
- 但**别误杀**人自己的 watch build（用精确 pattern，别用宽泛 pkill）
- docker 基础设施通常长期跑，不关
- 汇报里明示：「已关：<端口清单>；保留：<清单>」
