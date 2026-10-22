# 新版执行循环

仅用于 workflow_version=2、release=2026-09-22。所有命令使用本发行版 scripts 目录，`--ws` 为权威目录。不是本版本创建的 state 不接管，需要用 task_state.py init 新建。

1. `workflow.py --ws <ws> check` → `preflight --stage tools` → `task_state.py --ws <ws> init`，不覆盖已有 state。
2. `next` 返回 impl：派一位全新实现 agent；按模板构建、版本探测、运行 accept 并附报告设置 done。失败自修至多两次，其余独立票可继续。
3. `preflight_required`：查看明确缺失项；底座具备后执行 `workflow.py --ws <ws> preflight --stage integration`，再 next。
4. `qa_gate/qa_rework`：后者先修阻塞问题，运行局部复验，`issue repair --id N --note <修复与复验> --evidence <真实日志>`；然后启动完整独立 QA。

```bash
# 前提：真实构建已完成，probe 要查询运行实例版本并对照本次产物
workflow.py --ws <ws> stamp --environment <环境ID> --artifact <本次产物文件> --probe '<运行实例版本断言命令>' --out <产物清单.json>
workflow.py --ws <ws> accept --ticket T --environment <环境ID> --artifact-manifest <产物清单.json>
task_state.py --ws <ws> set --ticket T --status done --accept-json <上一步报告> --note '<非显然项>'
task_state.py --ws <ws> qa start --phase D1 --environment <环境ID> --artifact-manifest <产物清单.json>
# 主会话把原样 binding、required_checks 和分片路径给独立 QA；共享运行栈分片串行
task_state.py --ws <ws> qa merge --phase D1
task_state.py --ws <ws> set --phase D1 --status qa_passed   # 仅 verdict=PASS
# verdict=FAIL 改为 qa_failed；ERROR 修环境续跑同 run，不设 qa_failed
```

qa_resume：续跑当前 binding；不要另起轮覆盖旧证据。源码/环境版本需要改变时，以具体原因 `qa abort --phase D --reason ...` 保留旧 run 后重启；已合成 PASS/FAIL 的有效产品轮不可 abort，先记 verdict。

blueprint_changed：authority diff/impact → 确认业务变更 → 更新 accept/fixture/qa_scope → snapshot。`sync --reason ... --impact-json ...` 清单含 from_spec_hash、to_spec_hash、tickets（受影响 id 列表）；从 fingerprint 取得当前摘要，旧摘要在 state 中。脚本将受影响票置 stale。不要用空清单消音。

exception：集中报告根因、已尝试修复、缺什么决策。新问题不同于重复修复失败；不要沿用旧版“两轮失败必是口径问题”的理由。已获授权的实现修复照常继续。

all_done：执行 SKILL.md 的五步收尾。qa_passed 后即进下一阶段；人工抽验异步标 accepted。风险接受保留 human_override 与原 FAIL，交付时明确列出。
