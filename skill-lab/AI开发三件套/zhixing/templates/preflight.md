# D0：先证明检查器能正常运行与正确报错

workflow.json 使用 xuqiu 固定版模板，填本项目真实命令。project_root 相对权威目录；source_paths 是实际源码根，harness_paths 包含执行器/fixture，均不能指整个项目或 waves。默认忽略 node_modules/target/dist/.git 等；额外构建产物用 ignore 显式声明，不能把业务源码排除。

`preflight.tools` 与 `preflight.integration` 各配置非空检查数组。每项：`id`、`run`、`expect_exit`（默认 0）、`timeout`（可选，秒）。两段均至少一条预期退出 0 的正例和一条预期非零退出的反例。示例是协议形态，不可复制占位命令获得绿灯：

```json
{"id":"db-scalar-positive","run":"python doc/verify/selftest.py scalar","expect_exit":0}
{"id":"db-assert-negative","run":"python doc/verify/selftest.py wrong-expected","expect_exit":1}
```

最小覆盖按栈适用性选择：解释器/PyYAML/构建依赖；DB 标量与空结果、业务断言失败与连接失败；API 业务码与网络错误；旧产物拒绝；reseed 幂等与登录交错；浏览器真实点击、失败断言和日志；进程中断后锁释放。无 DB/移动端时不创建无关测试。

tools 使用小 fixture 验执行器契约，无需尚未实现的业务系统。integration 使用实际开发/测试环境，在 integration_after 列出的底座票全部 done 后运行；空数组表示一开始就应有环境。该列表只能引用真实 ticket，不能加入全项目所有票来拖延环境验收。

每次工具或配置变化使预检过期。正常业务源码变化不要求重新跑 D0。解释器切换需重跑 D0，不能用 fallback YAML 解析掩盖依赖缺失。

保留运行日志与正反例，不扩张成工具的无限元测试。发现执行器无法验证主流程先有界修复执行器，不将其降成 S2 后跳过必需检查。
