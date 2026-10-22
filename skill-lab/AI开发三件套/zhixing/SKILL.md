---
name: zhixing
description: 按需求蓝图与 ticket 执行软件开发，提供可恢复调度、真实验收证据、独立阶段 QA 和有界返工。每个项目固定一个 release 版本。
---

# 软件项目自治执行 · 固定版本 2026-09-22

本版消费 `/xuqiu` 的 REQ、稳定权威锚、ticket、phase-plan 与 workflow.json。每个项目固定 release；升级时另装新版本，不在运行中的项目里替换脚本。

## 运行边界

主会话负责 next → 派单 → 记状态；实现与独立 QA 使用新上下文 subagent。处理完一个 action 继续 next，直到 all_done 或确有硬阻塞，不反复询问是否继续。用户已有授权继续有效；不代签人决策。

- ticket：运行自己的 accept，含必要 UI 操作验收，不做完整 L0–L3。
- 阶段：独立 QA 完整覆盖 phase-plan 的 L0–L3；四级全部完成且当前范围无未解决 S0/S1 才通过。
- 项目：清理未决、人工批式抽验与最终业务演练。qa_passed 不等待人工抽验即可继续下一阶段；交付前清零待抽验。
- 分支仍为 integration + task/Dn，阶段通过后合流。权限、不可逆数据操作和实际发布遵循用户授权范围。

重活用 subagent；主会话不读实施方长报告给 QA 作答案。干净 QA 只读需求、权威、源码、运行实例和问题复现事实，不读实施方解释或其他分片结论。

## 路径与启动

`<ws>` 是 requirements.yaml 所在目录：标准布局为项目根/doc，拍平项目为当前目录。命令使用固定版本根：

```bash
workflow_python=/已安装PyYAML的固定Python绝对路径
workflow_scripts="$HOME/.claude/skills/zhixing/scripts"
"$workflow_python" "$workflow_scripts/workflow.py" --ws <ws> check
"$workflow_python" "$workflow_scripts/workflow.py" --ws <ws> preflight --stage tools
"$workflow_python" "$workflow_scripts/task_state.py" --ws <ws> init
"$workflow_python" "$workflow_scripts/task_state.py" --ws <ws> next
```

Python 以 D0 实际解释器为准，不因 cwd 改变而选中另一份 Python。四项 lint 统一由 check 运行；DAG/ADR 的 exit 2 表示 warning，单独评估，不当结构失败。counterfeit 和必要 E2E 缺失在新版是结构错误。snapshot 仍由需求层维护，未建 snapshot 不能 init；已有 state 不重复 init。

## D0 与资源

按 [D0 说明](templates/preflight.md) 填 workflow.json 的两段实际命令。tools 验执行器契约；integration 验真实环境，在 integration_after 指定的底座票全部完成后必须通过。执行器/配置改变会使 D0 证据失效，需重跑对应检查。适用性按实际栈确定，不给无数据库项目强塞 SQL 检查。

共享验证环境容量为 1。新版 next 默认每批派一票；QA 分片串行使用同一验证锁。`workflow.py run --label <用途> -- <命令及参数>` 将整条命令放进资源锁，防 reseed、服务重启、构建目录互相覆盖。不能在锁外另起破坏性操作，也不嵌套调用同一锁的 runner。命令的子进程结束后会清理同进程组的残留进程，长期服务应由独立运行环境管理，不放在一次性验证命令中后台挂起。

本版不默认开启两个槽位。以后启用多槽，需要源码/构建输出、DB、缓存、端口、证据路径、浏览器会话、迁移历史和设备资源全部隔离，并用跨槽哨兵实测。仅文件 touches 不冲突不构成运行时可并行的证据。

## 完工与产物证据

按 [执行手册](templates/task-loop.md) 操作。先构建并确认正在运行的实例版本，再用 `workflow.py stamp` 保存运行探测日志、实际产物哈希与源码指纹。禁止用 `echo 本地期望值` 冒充实例版本探测。测试输出、build、node_modules 不应误纳入源码输入；source_paths、harness_paths 和必要 ignore 在拆解时确定。

impl 使用 [模板](templates/impl-subagent-prompt.md)：

1. 按权威实现，touches 越界先记录并更新影响评估；会与别的工作冲突时暂停该票。不得在源码 grep 成功后声称按钮已经走通。
2. 运行 `workflow.py accept` 的全部断言，E2E 从真实入口操作并验证业务结果；返回 accept_json。
3. 主会话 `set --ticket T --status done --accept-json <报告> --note <注意事项>`。报告缺失、过期、票面不匹配或文件哈希变化均拒绝。

票面/API/fixture/qa_scope 有冲突时回到权威辨明；可回滚实现细节按授权自行判断，业务边界或无法判断的规格冲突集中提问。规格变更用 authority diff/impact；sync 要记具体理由及受影响 ticket 清单，自动置 stale。不能只重打快照吞掉变更。

## QA 与返工

QA 前 `qa start` 将环境、产物、源码、规格、测试工具、phase、round 和 run_id 固定。按返回的 binding 与 required_checks 派 [独立 QA 模板](templates/qa-subagent-prompt.md)。缺级、漏范围项、零断言、重复认领、错版本/错阶段、变化的证据均不能通过。

执行状态与产品严重度分开：

- completed：检查确实完成，再按 S0–S3 判断产品影响。
- error / blocked：必需检查未完成，合并为 ERROR，环境恢复且绑定输入未变时续跑当前 run；若需修改工具或源码，则先 qa abort 记录原因、重跑过期预检/产物探测，再 qa start。ERROR 不算产品失败轮，也不能算 PASS。连续两次执行失败转具体环境诊断，不无限重试。
- harness 的改进建议可记 S2；导致测试没跑成时仍必须填 error/blocked，不能以降严重度获得通过。

S0：主流程不可用、结果错误或重要数据/权限边界被破坏。S1：重要功能或下游路径受影响。S2/S3：非阻塞问题记账到收尾，不拉整轮返工。

`qa merge` 机械合并；报告中的失败检查必须关联 issue。同根问题由 root_cause 指纹合并，允许多个级各给独立证据。每轮报告必须复验台账里本范围的 open S0/S1，不能通过漏列问题获得绿灯。复验通过由程序关闭 issue，非人工的“我修好了”不能关闭。

只修阻塞问题。修复后 `issue repair` 记录真实修复版本和局部复验证据。新根因出现且旧问题已闭环可以继续；**同根问题经过两次已记录修复仍失败**才触发重复失败停机。累计最多 4 轮已完成的产品 QA；执行 ERROR 不计入该预算。达到预算是诊断/决策信号，不推断一定是需求错。

返工先用资源锁运行逐问题复验和相关机器回归，可减少昂贵探索前的低级失败。参考 [增量复验规则](templates/recheck.md)。**本发行版的阶段放行仍要求完整独立 L0–L3**，不自动复用旧级的 PASS，不把缺乏影响映射的增量检查当最终验收。

人明确接受完整 QA 已发现的残留时，可先记录 qa_failed，再 `set --phase D --status risk_accepted --by <授权人> --note <至少30字具体残留与依据>`。此状态与 qa_passed 分开，保留 FAIL 审计；不能覆盖未完成检查或工具 ERROR。没有明确授权时不得填 by，也不能为了赶进度自行调用。rearm 仅在同根反复失败后有授权时使用，不能突破 4 轮总预算。

`issue accept-risk` 仅用于 S2/S3 的记名决策。S0/S1 必须使用上述阶段级 risk_accepted，不能逐条隐藏后取得 qa_passed。

## 收尾与反馈

all_done 仍按五步：核对未决/接受风险/待人工抽验 → 在授权范围内合流与推送 → 运行 smoke → 在测试环境做 final_rehearsal → 汇总估时与实际。生产只做许可范围的冒烟，不灌测试数据。

事件写 `waves/events.jsonl`，区分资源等候、命令运行、验收、QA 与状态变更。统计墙钟与关键路径，别把并发任务耗时直接相加。比较首次 QA 通过率、有效返工轮数、工具错误、等待和交付后逃逸；不预先承诺 20–25 小时或固定百分比收益。

版本自测：`<固定Python> scripts/test_workflow.py -q`。测试只操作临时目录，不连接产品 DB。底层 `_legacy_state.py` 是本发行版冻结的解析/状态基础设施，由 `task_state.py` 导入，不要单独删除。
