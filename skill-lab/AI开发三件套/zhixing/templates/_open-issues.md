# 新版问题台账

载体仍为 waves/state.json。issue 字段：id、phase、ticket、root_cause（稳定根因指纹）、severity、level、type、title、detail、status。

状态 open → 独立 QA 复验通过后 resolved；接受风险单列 accepted 并记录 by/note，不能当已修。复发沿用 root_cause、增加 reopened，不靠换标题重置计数。

`issue add --phase D --ticket T --severity S1 --level L2 --type arch --root-cause sample.verify.request-order --title ... --detail ...`

实现方修复后用 `issue repair --id N --note <修复与局部验证> --evidence <日志>`；这一步不关闭问题。qa merge 校验本阶段未决 S0/S1 不得漏列；set 正式结果时按 retests 关闭。

`issue accept-risk --id N --by <已明确授权的人> --note <具体接受依据>` 只记录风险，不能伪装修复，也不能将未完成检查变成 PASS。阶段有残留时须完整 QA 后按独立 risk_accepted 状态记录，原 FAIL 保留。

S2/S3 不阻塞；harness 改进建议封顶 S2。工具导致未执行检查时，级的 execution 必须为 error/blocked，不能靠封顶获得放行。
