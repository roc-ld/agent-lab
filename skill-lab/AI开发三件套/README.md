# AI 开发三件套（Claude Code Skills）

用 AI 开发「小程序 + 管理后台」这类业务系统时，我固定用的三个 skill。它们把一个外包项目拆成三步，每一步都有机器检查兜底，而不是靠人记：

| 步骤 | Skill | 做什么 |
|---|---|---|
| ① 需求拆解 | `xuqiu` | 把客户零散的需求（文档、表格、截图）逐条抽成带编号的需求清单 → 整理成业务流程、字段表、页面清单 → 拆成带「做完怎么算对」验收断言的小任务 → 出一页确认单 |
| ② 自治执行 | `zhixing` | 按任务分组串行写代码：每个任务开一个全新的 AI 实现并自测；一组写完，再派一个干净的 AI 当测试员把整组完整测一遍，测不过就返工，全过才进下一组。状态落盘，断了能续 |
| ③ 验收闭环 | `acceptance-autopilot` | 客户在共享表格里提问题（页面、描述、截图），AI 逐条取证、定位代码、判断是 bug 还是理解不同，先列清单给人过目再改，改完验证并回写进度 |

另附两个技术栈包（`stacks/`）：

- `ruoyi-vue-plus`：若依（RuoYi-Vue-Plus）+ plus-ui + uni-app 的常量和踩坑记录，是视频里讲的那套技术栈
- `fastapi-local-web`：FastAPI 本地 Web 项目的常量和踩坑记录

## 安装

需要 Python 3.9+ 和 PyYAML（`pip install pyyaml`）。

```bash
mkdir -p ~/.claude/skills ~/.claude/stacks
cp -R xuqiu zhixing acceptance-autopilot ~/.claude/skills/
cp -R stacks/* ~/.claude/stacks/
```

三个 skill 要放在同一个 `skills/` 目录下：`zhixing` 会按相对位置调用 `xuqiu` 的检查脚本。

装好后自检一次：

```bash
cd ~/.claude/skills/zhixing/scripts && python3 test_workflow.py -q
```

## 怎么用

在项目目录里打开 Claude Code：

1. **拆需求**：把需求素材放进 `_input/`，输入 `/xuqiu`，或者直接说「拆需求」。产出 `requirements.yaml`、`authority/`（业务流 / 字段 / 页面）、`tickets/`、`phase-plan.yaml` 和确认单。四条检查全绿才算拆完：

   ```bash
   python3 ~/.claude/skills/xuqiu/scripts/coverage_lint.py --write-back
   python3 ~/.claude/skills/xuqiu/scripts/authority_lint.py check
   python3 ~/.claude/skills/xuqiu/scripts/adr_check.py
   python3 ~/.claude/skills/zhixing/scripts/dag_lint.py
   ```

2. **执行**：确认单过目后，输入 `/zhixing`，或者说「开始执行」。主会话只做调度，重活交给一次性的子 agent。
3. **验收**：上线后，客户在表格里提问题。按 `acceptance-autopilot/templates/autopilot.config.yaml` 给项目填一份配置（问题表在哪、怎么写回），再说「处理测试问题」。

## 几条用之前要知道的

- **人始终在环里**：确认单、设计方案、涉及钱的规则（分账、价格），AI 只登记不拍板，`decided_by` 只能是人。
- **不同技术栈要自己适配**：skill 正文是技术栈无关的，具体约定写在 `stacks/<栈>/profile.yaml`。换栈就照着新建一个栈包。
- **验收是骨架**：`acceptance-autopilot` 只给机制和配置模板，问题表的读写（飞书、金山文档、GitHub Issues 等）要按你的项目实现。
- 文档里的 `<ws>` 指 `requirements.yaml` 所在的目录（一般是项目根下的 `doc/`）；`owner` 指项目负责人，也就是你自己。
- 这些 skill 从我的真实外包项目里沉淀出来，涉及客户的业务和数据都已去掉。按原样提供，用在你自己的项目里之前，先在小项目上跑一遍。
