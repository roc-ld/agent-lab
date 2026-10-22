# OQ 跟踪表 —— 待甲方/负责人 答的小问题 + Fallback 决策

> 任何对方没明确答但实现需要的细节进这里。**每条必有 Fallback 让开发不被卡住**
>（这正是「非阻塞问题不停机」在需求层的形态；执行层的对应物是 task_state 的 issue 台账）。
> client 项目：负责人 每周批量推甲方拿答案。solo 项目：自审单 sweep 时一并过。
>
> **closed_by 列是闭环的证据**（2026-08-17 加，adr_check.py A5 校验）：
> 落地状态标 ✅ 的行必须填——取值限 `ADR-NNNN`（答案沉淀成了决策记录）或
> code anchor（`file:line`，答案直接落进了代码/配置）。空着 = 凭感觉 Closed。

## OQ 完整列表

| ID | 问题 | 影响 ticket | Due | 对方答 | Fallback（先行） | 落地状态 | closed_by |
|---|---|---|---|---|---|---|---|
| OQ-1 | <一句话问题（附口语 vs 文档矛盾点）> | <TICKET-ID> | <D2 前> | _待答_ | <不被卡住的默认路径 + 可调开关> | ⏳ | ⬜ |

## 状态枚举

| 状态 | 含义 |
|---|---|
| ⏳ Open | 待对方答，Fallback 未启用 |
| 🟢 Fallback adopted | 按 Fallback 先行实现（对方后答不同 → 走变更记录改） |
| ✅ Closed | 有明确答案且已落地（**closed_by 必填**） |
| ❌ Dropped | 问题不再成立（写一句为什么） |
