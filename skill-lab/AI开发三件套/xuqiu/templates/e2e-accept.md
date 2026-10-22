# 页面票的最小操作验收

将以下字段并入 ticket frontmatter。根据实际技术栈实现 runner；不能保留示例命令交差。

```yaml
ui:
  required: true
accept:
  - name: 从首页提交记录并验证保存结果
    form: E2E
    entry: 首页新增入口
    action: 填必填字段并点击保存
    outcome: 请求成功且刷新后显示新记录
    run: node doc/verify/e2e/create-record.mjs
    result: doc/waves/evidence/ui/create-record.json
    counterfeit: 点击处理器不发请求，或先调用被禁止的保存接口时，此条失败
```

runner 打开真实页面，通过真实操作到达结果；不以 API 直接提交代替点击。截图与 trace 用于解释结果，不能单独证明业务成功。保存、核验、权限等高风险状态流再补一个拒绝路径；不要把完整阶段 QA 搬进每票。

`result` 必须是本次新文件。accept 会将成功 E2E 的结果与附件复制到独立 run 目录归档。复跑前只清理自己 runner 的临时结果；不要清理归档目录或别的票的证据。runner 输出 JSON：

```json
{
  "status": "pass",
  "assertions": 4,
  "entry": "首页新增入口",
  "action": "填必填字段并点击保存",
  "outcome": "请求成功且刷新后显示新记录",
  "evidence": [
    {"path": "/绝对路径/本轮-trace.zip", "sha256": "文件的真实 SHA-256"}
  ]
}
```

入口、动作、结果三个字符串与票面一致；断言数必须大于零；evidence 文件必须存在且摘要一致。`workflow.py accept` 实际执行所有 run、检查输出并生成完工证据。该协议防止拿错文件和零用例假绿，不把自报元数据当作恶意防伪机制。
