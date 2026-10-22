---
name: xuqiu
description: 软件项目需求拆解（AI 工作流三件套之①）。触发词——用户说"需求拆解 / 拆需求 / 拆解需求 / 出蓝图 / 拆 ticket / 生成设计文档和任务 / 把需求拆成任务"，或输入 /xuqiu。（编排层 只把素材整理成文件放 _input/，不 headless 驱动本 skill——主入口是你手动起会话。）把甲方零散需求（需求文档/流程图/原型）逐条抽成 REQ 需求点清单 → **结构化权威蓝图**（业务流/字段/UI/数据主权，全部带稳定锚 id，AI 可按锚精确取用、可细粒度 diff）→ 带机器可验收断言的 ticket 清单 → 编成任务计划 D1-Dn + 一页确认单。三条机器闸门：coverage_lint（漏拆/镀金）· authority_lint（锚点有效 + **权威变更反查受影响 ticket**）· dag_lint（结构+断言强度）。权威在执行/验收期反复调整是常态，靠 authority_lint impact 把变更传播到 ticket，不靠人记。读回 _manifest.json 供编排层转述。产物供 ②执行(/zhixing) 消费。stack-agnostic 骨架。
---

## 新版接缝（release 2026-09-22）

本版（release 2026-09-22）适用于新项目。REQ、稳定权威锚、设计确认与内容块回流、机器覆盖检查、独立阶段 QA 保留。

拆票前按 [workflow.json 模板](templates/workflow.json) 生成 `<ws>/workflow.json`（`<ws>` 是 requirements.yaml 所在目录，标准布局为项目根/doc）。填真实 project_root、源码输入与执行器路径、D0 命令；发行版本固定，不使用共享目录的可变脚本作为运行时。

- 有 UI 锚点，或 touches 涉及 pages/views/components/router/routes 的票，写 `ui.required: true`，至少一条 `form: E2E`。入口、动作、业务结果、结果文件、反例字段必填，见 [E2E 接缝](templates/e2e-accept.md)。API 接线虽不命中路径，也应依据业务影响声明 UI 验收。实际 diff 超出 touches 必须更新影响评估，不能借漏列规避验收。
- 每票只验关键链路；新增/修改状态的页面要从真实入口点击到提交，核对请求与刷新后的结果。只读页面验证筛选/导航/下载。H5 是快速覆盖；平台登录、原生组件、下载分享仍保留对应真机检查。
- 每条 accept 必须有 counterfeit；lint 硬检查的是结构完整性，不宣称静态扫描证明了断言强度。关键事件未绑定、请求顺序错、未落库等反例应能让 E2E 变红。
- `phase-plan.yaml` 每阶段 L0–L3 范围均写具体可执行检查，交付演练保留。QA start 将各范围项编成检查 id，报告不能漏项。
- D0 分 tools 与 integration 两段。tools 用小 fixture 验执行器契约；integration 在 `integration_after` 指定的底座票全部完成后验证实际连接。两段都含成功样例和预期失败样例；不用不存在的业务功能卡死底座初始化。无 DB/移动端的项目明确说明不适用，不加无关检查。
- 对跨票递增的菜单/字段集合声明断言所有权和适用阶段；后续合法扩展不能被早期“全集精确相等”误报。规格改动传播到 accept、fixture、API 契约及 qa_scope。
- 交接统一跑 `workflow.py --ws <ws> check`（DAG、coverage、authority、ADR 四项），保留 authority snapshot；执行期不擅自弱化断言、代签人决策或跳过需求确认。

D0 与证据协议由 [zhixing 固定版](~/.claude/skills/zhixing/SKILL.md) 承接。共享运行环境先按容量 1 执行；不承诺自动多槽并发。成熟工具按栈实例化，勿把 Java/uni-app 工具硬塞进其他栈的 scaffold。


# ① 需求拆解 /xuqiu（工人技能 · 三件套之一）

> **三件套里的位置**：①需求拆解(/xuqiu) → ②[执行 /zhixing](~/.claude/skills/zhixing/SKILL.md) → ③验收（acceptance-autopilot / 项目级）。
> 调用名 `/xuqiu`（也可直接说"需求拆解 / 拆需求 / 出蓝图和 ticket"触发；目录=调用名=xuqiu）。
> **你是"技术工人"，不是"编排层"**：编排、记忆、定时、通知、跟甲方来回沟通全归 编排层。本 skill 无状态——给素材 → 吐蓝图+ticket+manifest → 结束。**不要在 skill 里重建 自建的记忆/调度系统**（记忆/调度/上下文管理都别碰，那是编排层的活）。
> **主入口 = 你手动**：编排层 只做前期备料（把零散需求整理成素材文件放进 `_input/`），**不 headless 驱动本 skill**——需求层本就要你在环里（审确认单、找甲方澄清），headless 反而脆。你主动起 Claude Code 会话调 `/xuqiu` 交互产出 + 当场审。

## 0.0 Skill 根与工作区根（先读这一节 —— 这是两个不同的路径）

### Skill 装在哪 —— 命令一律用**实体路径**，两种环境通用

| 运行环境 | 从哪加载 |
|---|---|
| Claude Code | `~/.claude/skills/xuqiu` |
| **其他 agent 环境** | `~/.agents/skills/xuqiu` |
| **两者的实体都是这一个** | **`~/.claude/skills/xuqiu`** |

所以本文里所有脚本一律写成 **`~/.claude/skills/xuqiu/scripts/xxx.py`** 这种字面绝对路径：

- Claude Code 与 其他 agent 环境 **都能直接跑**，不需要预先 `export` 任何变量（`~` 由 shell 展开）；
- 两个环境走的是**同一份文件**，不存在"改了这边那边没改"；
- 绝不写裸 `scripts/xxx.py` —— cwd 是项目工作区不是 skill 目录，相对路径必然
  `No such file or directory`（这个报错很容易被误读成"工作区不对"，进而引出乱 `cd`）。

姊妹 skill 同理：`~/.claude/skills/zhixing/scripts/xxx.py`。

### `<ws>`：工作区根 —— `requirements.yaml` / `authority/` / `tickets/` 所在的那一层

同一套脚本要服务两种真实存在的布局，**都认**：

| 布局 | 形态 | 真实实例 |
|---|---|---|
| 标准（优先） | `<ws>/doc/{requirements.yaml,authority/,tickets/,waves/}` | `<项目>/` |
| 拍平 | `<ws>/{requirements.yaml,authority/,tickets/,waves/}` | `.../实战项目/doc-phase2/` |

定位顺序：**显式 `--ws` > `$XUQIU_WORKDIR` > 自动探测（先看 `cwd/doc`，再看 cwd）> 兜底 `doc/`**。

> 探测**先认 `doc/`** 是有意的：标准布局下 `<ws>` 是项目根、真实目录是 `doc/authority`，
> 先认 `doc/` 就与脚本改造前的默认值**逐字一致**——老项目的调用行为一个字节都不变。
> 只有 cwd 下没有 `doc/authority`（即拍平布局）时才会走第二档。

探测到非默认根时会往 stderr 打一行 `[ws] 工作区根 = …`，**不静默**。

> ⚠️ **多阶段项目（同一个甲方做二期/三期）必须 `cd` 进那一层再跑**，或显式传 `--ws doc-phase2`。
> 以前路径写死 `doc/…`，在 `doc-phase2/` 下**一个文件都读不到却照样打绿灯**——
> 2026-09-13 体检测出：`authority_lint check` 报「0 条权威 × 0 个 ticket」并 ✅ 退出 0，
> 而真实数据是 556 条。现在已加零输入守卫（读到 0 条直接 error），但**先 cd 对**才是正解。

```bash
cd <ws>          # 标准布局：项目根；拍平布局：doc-phase2/ 本身
```

> 本文其余地方写的 `doc/requirements.yaml`、`doc/tickets/` 这类路径，**一律读作 `<ws>/…`**。
> 脚本两种都自动认，不用手传路径——**显式传 `--req doc/…` 反而会在拍平布局下指错**。

## 0. 双模式契约（standalone + 编排层 调用）

**standalone**：负责人 在 Claude Code 里 `/xuqiu <素材目录>`，交互式产出、当场审。

**编排层 headless（预留，层1不作主驱动）**：契约保留（编排层喂 `<workdir>/_input/`、跑完读回 `<workdir>/_manifest.json`），但按 Q2 拍板：需求层 编排层 **只备料不 headless 驱动**（需求要你在环里审），主入口是你手动 `/xuqiu`。此模式留给未来需要时启用，别当默认路径。

| | 约定 |
|---|---|
| **输入**（编排层喂） | `mode=client`：`<workdir>/_input/`（需求文档 / 流程图 / 原型截图，⓪/fangan 确认稿是主素材——⓪→① 是人工承接）。`mode=solo`：**`docs/research/<slug>/product-brief.yaml`**（模板 `templates/product-brief.yaml`，idea-research 产出；散文调研报告不是合法需求源）。模式写进 `requirements.yaml` 的 `meta.mode`，详见 §0.7 |
| **输出**（回给编排层） | 蓝图三件套 + `doc/tickets/*` + 一页确认单 + `<workdir>/_manifest.json`（见下） |
| **何时叫醒 负责人** | `needs_human`：确认单就绪待审 / 需求存在硬歧义待澄清 —— **绝不自己替甲方拍板字段口径** |

**`_manifest.json`（给弱模型编排层稳定解析，别让它读长文）**：
```json
{"skill":"xuqiu","status":"done | needs_human | exception",
 "artifacts":{"requirements":"doc/requirements.yaml",
              "blueprint":["业务流图","字段表SSOT","UI索引"],"domain_ownership":"doc/domain-ownership.md（多系统项目才有）",
              "tickets_dir":"doc/tickets/",
              "confirmation_sheets":{"<板块>":{"path":"doc/confirmation/<板块>.md","status":"pending|confirmed|revised"}},"phase_plan":"doc/phase-plan.yaml","round":1,
              "change_log":"变更记录路径"},
 "coverage":{"total":106,"covered":106,"rate":1.0,"missing":[],"clarify":["REQ-STORE-011"]},
 "summary":"一句话给编排层转述（N 条需求 → M 个 ticket，编成 K 个任务，X 处需确认）",
 "needs_human":{"route":"client | owner","why":"单一场景短句（别用 | 塞多义）","what":"要人做什么（对象由 route 决定）"},
 "exception":{"kind":"input_unreadable | ssot_gen_failed","detail":"...","action":"..."},
 "next":"zhixing"}
```

- **`needs_human.route` 决定编排层 ping 谁**：`client` → 甲方（甩确认单在线文档链接过目，不叫 负责人）；`owner` → 微信叫 负责人 拍口径 / 审设计。确认单待审=`client`；需求硬歧义 与 **设计方案待选**（关键页面 2-3 方案等 负责人 选定，`why="设计方案待选"`）=`owner`——视觉拍板归 负责人 不归甲方。别把这几类压成一个 flag。
- **确认单多轮循环靠 `confirmation_sheets` + `round` 续接**：逐板块 `pending|confirmed|revised` + 轮次，弱模型一眼算出还剩哪些板块待甲方过目。**headless 重入契约**：编排层把甲方反馈落 `<workdir>/_input/feedback/` 后重新触发本 skill，skill 读 feedback + 上轮 manifest 续接（工人仍无状态，续接锚点在 manifest + _input，由编排层外层驱动）。
- `exception` 仅指工人自身崩（读不到 `_input` 素材 / `ssot_gen` 挂）；需求层的缺料/矛盾走 `needs_human` 不走 exception。

## 0.3 ★ 先把「最终想法」问出来（① 的第一步，先于 REQ 清单）

> 借 `superpowers:brainstorming` 的做法。为什么补这一步：2026-08 T006 里我攒了一长串
> 澄清项甩给 负责人，他的回话是**「#24 这些我无法判断，你自己自行判断执行」**和
> **「我已经不是很明白了，我只知道核心需求，这三个拍板你自己决定吧」**。
> 需求层没收敛，后面每一层都在猜——**这才是返工的真正上游**。

### 六条规矩

1. **先分档，并且说出来**，让人有机会当场否掉你的判断：
   - **探路**：「能不能做 / 行不行得通」——产出是一个答复，不是要留下的代码。两三句
     说清你要试什么，点头就去试。不写 spec。
   - **有界**：改一处已经存在的东西。问几个关键问题，**在对话里**给一段简短设计
     （几句到几段），点头才动手。不写 spec 文件。
   - **架构级**：新项目 / 新子系统 / 改变模块之间怎么接。走完整流程：问 → 出 2-3 个
     方案 → 分节确认 → 写 spec → 才拆 ticket。
   拿不准就走**重**的那档。中途发现藏了复杂度只能**往上升**，不能往下降。

2. **一条消息只问一个问题。** 攒成清单甩过去，收到的一定是「你自己定吧」——
   那等于这一步没做。

3. **优先选择题，把你的推荐放第一个并说明为什么。** 给的是选择题不是问答题。

4. ★ **只问他答得上来的**：目的、取舍、边界、什么算成功、哪些**明确不做**。
   凡是要读过代码才答得上来的（字段叫什么、放哪张表、用哪个库），**不是问题，
   是你的活**——自己定，记进 REQ 的 `note`，别署他的名（见下方拍板边界）。

5. **问题有预算**：架构级 ≤ 8 问，有界 ≤ 3 问，探路 ≤ 1 问。
   超预算说明你在**拿提问代替判断**。剩下的按第 4 条自己定。

6. **收尾出一份「最终想法」当场过目**（十几行以内：要做什么 / 明确不做什么 /
   什么算成功 / 已知不确定），他点头才动 `requirements.yaml`。
   **YAGNI 要狠**——每一条都问一句「这期真的要吗」，砍掉的写进「明确不做」。

### 拍板边界（谁定什么，全模式通用）

原来的 STOP「绝不替甲方拍板」写得太满，结果是**把所有模糊都推给人**，而人只答得上
关乎结果的那部分。精确的分法是：

| 类型 | 谁定 | 怎么记 |
|---|---|---|
| **可回滚的实现口径**（命名 / 排序 / 默认值 / 文案 / UI 细节） | **你定**，继续拆不停机 | REQ 标 `status: clarify` + 默认口径写进 `note`，`decided_*` 四字段**留空** |
| **不可逆或跨边界**（数据模型主键与迁移 / 计费定价 / 对外 API 与隐私承诺 / 删改存量数据 / 触碰 `deferred` 的需求） | **必须停下来问** | 确认后才填 `decided_*` |

写 `decided_*` 四字段的权力**永远只属于人**；你的默认口径只进 `note`、不署名。
`coverage_lint` 会报「clarify 与 decided_by 并存」当绊线。

### 出口

「最终想法」点头之后才进第 1 步抽 REQ 清单。**这一段对话本身就是需求源**——
里面被砍掉的、被确认的、被推到 V2 的，逐条落进 `requirements.yaml`
（砍掉的记 `status: deferred` + `defer_to`，别只留在聊天记录里）。

## 0.7 solo 模式（独立开发线 —— 一套三件套两种 profile，不是两套）

`meta.mode: solo` 时 负责人 就是甲方。**主干全共用**（REQ 清单 / 蓝图三件套+snapshot /
ticket+accept / 四项 lint / phase-plan.yaml / QA 门），只分叉四处：

| # | 分叉点 | client | solo |
|---|---|---|---|
| a | 入口素材 | ⓪/fangan 确认稿 + `_input/` | `docs/research/<slug>/product-brief.yaml`。**缺文件或缺 `website_slug` → `exception: input_unreadable` 拒绝启动**——「要做」当天先做完落地页三件事（官网 product 记录 wip + 回填 slug + build-log 路径固定推导 `code/<slug>/docs/build-log.md`），做不完不许起 /xuqiu（规则 0.5 同级） |
| b | 人工触点 | `route` 可 `client`；确认单按板块阻塞 | `route` **只许 `owner`**；确认单降级为**一张全项目自审单，不阻塞拆 ticket**（别给 负责人 发一张要他确认自己刚想清楚的事的单子——开了跳过的口子，外包项目也会被顺手跳过） |
| c | change-log 用途 | 返工成本举证 / 下单报价证据 | 范围膨胀检测：`out_of_scope` 逐条录成 `status: deferred` 的 REQ（`defer_to: V2+`），coverage_lint 盯「ticket 引用 deferred/dropped REQ → error」；要做回来先改状态 + change-log 留一行 |
| d | ② 之后的下游 | ③ 由甲方提问题触发 | 官网 product 记录状态推进 + build-log 沉淀（自媒体母稿）+ 指标上报 → 项目级验收 skill |

**拍板边界**：已提升为**全模式通用**的 §0.3，solo 不再单列一套（两份必然漂移）。
solo 只多一条落点：自审单 sweep 时 负责人 批量过一遍 clarify——认可的
`clarify → pending` 并填 `decided_*`。

**`meta.scale: micro`**（预估 ticket ≤10 或 ≤3 天，由 product-brief 的 min_scope 推）——
仪式重量旋钮，骨架不动：
1. authority 允许**单文件** `doc/authority/authority.yaml`（authority_lint 按 glob 读，零改动）；
2. phase-plan.yaml 允许单 phase（qa_scope 照写）；
3. 设计段收缩：3b 只跑「内容块回流」一节且保留全强度（它就是静态产品的需求主体）；
   token 抽取与组件全集扫描在「单主题 + 页面 ≤3」时跳过；「2-3 方案待选」保留不动；
4. L1 跨表对账的适用性由栈声明（静态网页无 db 自然豁免）。
**保留全强度**：REQ 清单、accept 断言、四项 lint、snapshot 基线。

## 0.5 粒度：需求点 → ticket → 任务 → 项目（四层，全程别混）

| 层 | 是什么 | 载体 | 完工时测什么 |
|---|---|---|---|
| **需求点 REQ** | 甲方文档里的一条 | `doc/requirements.yaml` | —（是被覆盖的对象） |
| **ticket** | 一个功能点 | `doc/tickets/<id>/prompt.md` | 自己的 accept（2-3 条轻量断言） |
| **任务 daily** | 多个 ticket 的集合 | front-matter 的 `phase` + `doc/phase-plan.md` | ★ 干净 QA subagent 跑完整 L0-L3 |
| 项目 | 多个任务 | — | 最终验收演练 |

**完整测试只在任务边界做**，不在每个 ticket 后做。xuqiu 的活是把 REQ 拆成 ticket、把 ticket 编成任务；②zhixing 按任务串行跑。

## 1. 交付物（这是"done"的定义）

返工的根在开工前——本方法论把它前移。顺序是硬约束：**REQ 清单 → 蓝图 → 出 2-3 设计方案 → 负责人 审选定 → ★设计权威抽取（含内容块回流）→ 才拆 ticket**
（视觉未定不拆，否则实现层猜 UI = 返工；**回流没做完也不拆**，否则稿上的内容块永远不进 ticket）。

**① 需求点清单 REQ**（第一步，先于蓝图）：把甲方文档**逐条**变成有 id 的实体，落 `<ws>/requirements.yaml`。结构化源（xlsx/csv）跑 `~/.claude/skills/xuqiu/scripts/req_extract.py` 自动抽；非结构化源（word/pdf/聊天记录）AI 逐条读手写。模板 `~/.claude/skills/xuqiu/templates/requirements.schema.yaml`。

> **一条需求一个 id，绝不合并**——合并后覆盖率永远 100%，这层校验就废了。
> 这一层解决的是"确认每一个需求点"：蓝图锚的是**实现结构**，回答不了"甲方第 37 行拆没拆"。

**② 权威蓝图**（先行，不是开发中期补建）。**一律走结构化 YAML + 稳定锚 id**，落 `doc/authority/*.yaml`：

| 件 | 内容 | 锚前缀 | 文件 |
|---|---|---|---|
| 业务流权威图 | 端到端流程（谁→做什么→产出什么状态），**拆到步** | `FLOW:` | `authority/flows.yaml` |
| **字段表 SSOT** | 每表每字段的唯一真相源——名/类型/字典/约束 | `FIELD:` | `authority/field-ssot.yaml` |
| UI 权威索引 | 每页 = 原型锚点 + IA + 组件形态（弹窗/抽屉/整页） | `UI:` | `authority/ui-index.yaml` |
| **领域主权表**（多系统必做） | 哪张表由谁写 / 另一侧能不能改 / 冲突听谁的 / 业务键 | `OWN:` | `authority/ownership.yaml` |

格式见 `templates/authority.schema.yaml`（字段表另有 `field-ssot.schema.yaml`）。
人看的 markdown 由 `authority_lint.py render` **单向渲染**，别手改渲染产物。

> **为什么不用散文承载**（这是被实践打出来的）：
> ① **AI 查不了** —— impl/QA subagent 拿到 `blueprint_refs` 得把整份蓝图读进上下文再自己找。
>    106 需求点 × 两系统时读不全、找错段、吃光上下文。结构化后 `authority_lint show <锚>` 一条即取。
> ② **改不动** —— 散文没法算细粒度 hash，改一个字段就得让锚了整节的 ticket 全部失效。
> ③ **锚会漂移** —— `字段表#§5.2` 这种章节号锚，蓝图插一节后**静默指向别的内容**（还能解析，只是指错）。
>
> 多系统项目**不做领域主权表，所有集成事故都会在上线后暴露**——其余三件回答"数据长什么样"，回答不了"谁有权改它"。

**②c 权威会变，所以传播链必须是机器的**（`~/.claude/skills/xuqiu/scripts/authority_lint.py`）：

```bash
authority_lint.py snapshot     # 打基线（每条记录单独 hash）
authority_lint.py diff         # 权威改了哪几条锚
authority_lint.py impact       # 反查 blueprint_refs → 哪些 ticket 要重做 + accept 要跟着改
authority_lint.py check        # 悬空引用 / 锚到废弃记录 / 章节号锚
authority_lint.py show <锚id>  # 精确取一条（给 AI 用，免全文读）
```

**改完权威必须跑 `impact`**。跳过它直接 `snapshot` = 把变更悄悄吞掉：已完工 ticket 建在错图上、
accept 还绿着（期望值写死在旧口径），而且**再也查不出来**。脚本对此有护栏（有未消化变更时拒绝打快照）。

**③ 字段一致性保障**（注意：**不是"一键生成三层"**）：
- **DDL 用生成**：`~/.claude/skills/xuqiu/scripts/ssot_gen.py --targets ddl` —— DDL 最机械、最易漏（漏 `create_dept` 到 INSERT 才炸）、且迁移文件不可回改，值得机器保证。renderer 按栈实现（约 30 行）。
- **entity / 前端类型让 AI 照 SSOT 直接写**，一致性用 `ssot_gen.py --check <代码树>` 校验。理由：生成的 entity 迟早要手加业务注解，一手改就破坏"产物可重生成"的单向契约。
- ⚠️ **未实例化的 target 会报错退出（exit 3），不产出占位文件**。要么真生成，要么明确失败。

**③ 设计方案（2-3 个待审）**：蓝图确定后、拆 ticket 前，对**关键页面**（一级导航主页 / 高频录入 / 看板 / 复杂交互）各出 **2-3 个设计方案**交 负责人 审选定。视觉权威源 = **Claude Design（claude.ai/design）**——mp/admin 视觉一律以它为准，优先出 Claude Design mockup，覆盖不到的用 HTML 草案。每方案：一句话定位 + 关键差异 + 截图/链接 + 取舍。模板 `templates/design-options.md`。**负责人 选定后才进 ticket 拆解**；任一关键页面未选定 → 不拆。

**③b 设计权威抽取（★ 定稿后、拆 ticket 前必跑，模板 `templates/design-authority.md`）**：
设计定稿 ≠ 设计权威。交接包是给人看的（截图 + 一份手写交接文档），必须结构化抽取成 `authority/ui-index.yaml` + token + 组件全集。
**这一步的核心不是抄视觉，是「内容块回流」**：逐页逐块问「这块会不会变 / 谁改它 / 能不能算出来」——
会变且使用方要改的，就是一条**新需求 + 新字段**；能算出来的（计数/汇总）**禁止手填**。

> **为什么必须单列一步**（实践打出来的）：设计稿是**需求来源之一**，但没人把它当需求来源。
> 直接从"方案选定"跳到"拆 ticket"，稿上那些会变的内容块（Hero 元数据条、统计数字、
> 卡片组、状态徽章、详情页多出来的元数据格）**没有任何一步负责把它们变成字段**，
> 于是它们要么在写 schema 时被偶然发现，要么拖到实现期才炸。
> 另外交接包里**混着被否决方向的稿**是常态，不标坑就会被当 ground truth 误用。
>
> **回环**：本步产出新 REQ → **必须回到 ① 需求清单并重跑 `coverage_lint`**，否则这些需求永远不进 ticket。

**④ ticket 清单**（每 ticket = prompt 正文 + 机器可读头）：机器可读头就是三件套接缝 `templates/ticket.frontmatter.yaml`——`track / phase / req_refs / depends_on / touches / blueprint_refs / accept`。这些字段是 ②zhixing 的全部输入。

> `req_refs`（满足甲方哪几条需求）与 `blueprint_refs`（建在蓝图哪几段上）**分工不同、都要写**：
> 前者查"漏拆 / 镀金"，后者查"蓝图改了要重做哪些 ticket"。

**⑤ ticket 拆解时即写 accept 断言**：每 ticket 附 2-3 条机器可验断言，按 `templates/accept-assertion-forms.md` 的强制形态写（菜单类必断 path 非空、API 类必断行内容、DDL 类必断公共字段全集）。**写不出 accept = 需求没拆清楚，不算就绪。**
★ 每条再配一行 `counterfeit`（把实现改成什么样，这条会红）——**返工的上游不是检得太严，是断言写得太松**：T006 的 39 条 accept 证伪时发现 35 条「实现坏了照样绿」，D1 的 QA 门为此跑了 7 轮。在这一层补一行的代价是改 yaml。
断言强度由 ②的 `dag_lint.py` C5 **按 run 内容实检**（不是只查 form 拼写）——但要注意 **C5 是启发式提示，判出问题只报 warning、不阻塞**：它唯一硬拦的是「`run` 为空」与「DATA 形态自造数据自证」。真正的强度保证靠上面那行 `counterfeit` + 变异测试，别指望 lint 替你兜底。

**⑥ 任务计划**：把 tickets 编成任务 D1-Dn，落 **`doc/phase-plan.yaml`**（机器可读权威，模板 `templates/phase-plan.yaml`）：每任务 `order` 显式定序（返工任务 D9X 也登记才能插队）+ `goal` + `depends_on_phases` + **`qa_scope`（键固定 L0-L3）**——qa_gate 时原样进 payload，两轮 QA 拿同一份可 diff 的范围；顶层 `final_rehearsal.scenarios` 写交付前全链路演练剧本（写不出剧本 = 任务计划没想清楚）。`/zhixing` 按任务**串行**跑、每个任务末一道**干净 QA 门**。ticket → 任务的归属在 front-matter 的 `phase` 字段（**必填**，dag_lint C7 拦；phase 不在 yaml 里 init/sync/C8 都拒收）。

**⑦ 交付前跑四条 lint，全绿才交 ②zhixing**：

```bash
python3 "$HOME/.claude/skills/xuqiu/scripts/coverage_lint.py" --write-back   # cwd=<ws>，路径自动探测
python3 "$HOME/.claude/skills/xuqiu/scripts/authority_lint.py" check        # 锚点悬空 / 锚到废弃记录 / 章节号锚（含 ADR-NNNN 锚）
python3 "$HOME/.claude/skills/xuqiu/scripts/adr_check.py"                   # ADR 双轨 status / anchor 存在性 / 口径自相矛盾 / OQ closed_by
python3 "$HOME/.claude/skills/zhixing/scripts/dag_lint.py"                  # 省略目录则自动探测 <ws>/tickets
```

## 2. 交付门（轻量，不搞签字冻结）

> 甲方按实际随时调整是常态，逐单审批太重。用**过目确认 + 变更可追溯**代替冻结。

1. **一页确认单**（`templates/confirmation-sheet.md`）：按板块出，甲方/负责人 过目确认覆盖了需求。编排层甩链接（飞书在线文档）给甲方最省事。
2. **一行式变更记录**（`templates/change-log.md`）：每次需求改动记一行——日期 / 改了什么 / 波及哪些 ticket / 谁提的。**「波及 ticket」一列不用手填**，粘 `authority_lint.py impact` 的输出即可（手填必漏，而漏掉是无声的）。目的不是拦改动，是让返工成本可见可追溯。
3. **原型导出版本化**：原型每次导出打版本号 + 目录树快照。对照原型施工前先认准版本（对错版本整轮返工是真实事故）。
4. **就绪信号**（交给 ②zhixing 的判据，全绿才交）：
   - **§0.3「最终想法」已当场过目点头**（这一条在最前面：它没收敛，下面几条全是在猜）
   - REQ 清单齐 + `coverage_lint` 零漏拆零镀金
   - **每条 accept 都写得出 `counterfeit`**（把实现改成什么样它会红）——`dag_lint`
     检查的 counterfeit 缺项清零。这一条清不掉，返工的账会记到 ②zhixing 的 QA 门上
   - 权威蓝图结构化落 `doc/authority/*.yaml` + `authority_lint check` 零悬空锚 + 已打 snapshot
   - 多系统项目：领域主权表未决项清零
   - **有 UI 的项目：设计权威抽取已跑完**（`design-authority.md` §F 判据全绿，内容块回流出的 REQ 已并入清单）
   - 全部 ticket front-matter 齐 + `dag_lint` 全绿
   - 任务计划齐，每个任务写得出 QA 范围

## 3. 流程（steps）

```
0. **选栈**：定 requirements.yaml 的 meta.stack（已有栈包见 ~/.claude/stacks/；
   新栈 N=1 填 none 走死文档）。声明了具体栈但 stacks/<id>/ 不存在 → **当场生成最小
   骨架**（profile.yaml 仅 public_fields 一键 + 空 gotchas.md，一次 Write，不硬 STOP），
   并在确认单/自审单标注一行「新栈 <id> 已建最小骨架」。同时 doc/lint-profile.yaml
   写 `stack: <id>`（dag_lint 据此取公共字段集；有 DDL accept 却既无栈也无显式
   public_fields 会报 error）
0.5 ★ **先把「最终想法」问出来**（§0.3）：分档并说出来 → 一条消息一个问题、
   优先选择题、推荐放第一个 → 只问他答得上来的（目的/取舍/边界/明确不做），
   要读代码才答得上的自己定 → 问题预算（架构级 ≤8 / 有界 ≤3 / 探路 ≤1）
   → 出十几行的「最终想法」当场过目。**他点头才进第 1 步。**
   这段对话本身就是需求源：砍掉的记 deferred + defer_to，别只留在聊天记录里。
1. 读 _input/ 素材 → **逐条抽 REQ 清单**（doc/requirements.yaml）
   结构化源: python3 "$HOME/.claude/skills/xuqiu/scripts/req_extract.py" --src <xlsx> --sheet <s> --domain <D> --prefix REQ-<D> --out <ws>/requirements.yaml
   非结构化源: AI 逐条读手写。一条需求一个 id，绝不合并。
   然后补 priority / note；有歧义的标 status=clarify + 写清要问什么
   架构级口径随手立 ADR（templates/adr.md，双轨 status + decision 块；ADR-NNNN 是锚，
   ticket 用 adr_refs 引用）；待答小问题进 doc/_oq.md（templates/_oq.md，✅ 必填 closed_by）
2. 走一遍业务流，标出模糊点 → 写权威蓝图到 doc/authority/*.yaml（**结构化 + 稳定锚 id**）
   业务流 FLOW:（拆到步）· 字段 FIELD: · UI 页面 UI: · 数据主权 OWN:
   **多系统项目**（≥2 后端/库/集成既有系统）：领域主权表必做且未决项清零 —— 建在未决边界上的 ticket 一定返工
   写完 `authority_lint.py snapshot` 打基线，`render` 出人看的 md
3. **蓝图确认后出设计方案交 负责人 审**：对关键页面各出 2-3 个设计方案（Claude Design mockup / HTML 草案，
   模板 templates/design-options.md）→ 落 _manifest.json（needs_human.route=owner, why=设计方案待选）
   → 负责人 选定后视觉锁基准。**任一关键页面未选定 → 不进下一步拆 ticket**
3b. **设计权威抽取**（`templates/design-authority.md`）：标误导料（被否决方向的稿）→ 抽 token（每主题一套完整变量）
   → 逐页扫组件全集（**别抄设计师清单，它必然漏**）→ ★**内容块回流**（逐块判"会变/谁改/能否算"→ 出新 REQ + 新字段）
   → 登记稿与需求的冲突（如"需求要双主题、稿只交付一套"）
   → **回到第 1 步补 REQ，重跑 coverage_lint**，再进第 4 步
4. 按业务边界拆 ticket：分 track（域）、锚 req_refs（满足哪几条 REQ）、连 depends_on（只连 ticket）、
   列 touches（含共享文件）、锚 blueprint_refs、写 accept（按形态表）
   ★ 每条 accept 配一行 `counterfeit`：**把实现改成什么样，这条会红？**
   写不出就是装饰品，当场重写。这是**最省钱的一条规矩**——T006 的 39 条 accept
   有 35 条「实现坏了照样绿」，在这里补一行是改 yaml，等到 ②zhixing 的 QA 门
   才发现是**整轮返工**。八种见过的假绿形态见 templates/accept-assertion-forms.md。
5. 把 ticket 编成任务 D1-Dn（每 ticket 填 phase）→ 出 doc/phase-plan.yaml（order 定序 +
   每任务 qa_scope L0-L3 + final_rehearsal 剧本）。写不出 QA 范围 = 任务切得不对；
   写不出演练剧本 = 任务计划没想清楚
6. **四项静态检查统一入口**（使用 D0 固定的 Python；warning 单独评估，exit 2 不当结构错误）：
   python3 ~/.claude/skills/zhixing/scripts/workflow.py --ws <权威目录> check
   漏拆 → 补 ticket；镀金 → 删或补 req_refs；弱断言 → 重写 accept
7. 出一页确认单 → 落 _manifest.json（needs_human=待审）→ 交编排层转述 负责人/甲方
8. 收到修改意见 → 改蓝图/ticket + 追一行变更记录 → 重跑第 6 步 → 反复直到确认
9. 确认后 status=done, next=zhixing

**★ 甲方后续补新版需求文件（常态，不是例外）**：
```
python3 "$HOME/.claude/skills/xuqiu/scripts/req_extract.py" --src <新文件> --sheet <s> --domain <D> --prefix REQ-<D> \
        --out <ws>/requirements.yaml --merge      # 按 板块+名称 / 原文相似 匹配，
                                                 # **原地更新原文、REQ id 保持不变**，
                                                 # 保留人写的 priority/note/clarify
python3 "$HOME/.claude/skills/xuqiu/scripts/authority_lint.py" diff           # 哪几条需求原文变了
python3 "$HOME/.claude/skills/xuqiu/scripts/authority_lint.py" impact         # → 哪些 ticket 要重做 + accept 要跟着改
```
- **绝不能用 `--append`** 接新版：它只按 source 去重，**检测不到原文改动**，
  改了措辞的需求会被当成"没变"，下游 ticket 继续建在旧口径上。
- **`--merge` 不自动标废弃**：本次文件里没出现的需求只报不改——甲方常常只是
  发个补充文件，不代表那些需求取消了。确认真取消才手工标 `status: dropped`（**别删行**，
  删了历史 ticket 的 req_refs 就悬空）。
- 匹配**不用 source 当主键**：甲方在表中间插一行，后面所有单元格引用位移，
  按 source 匹配会把整批判成"全删+全新增"、id 全换 → 所有 req_refs 悬空。
```

## 4. 归因回流（补上一直缺的闭环）

每条验收问题（来自 ③验收层）回流时打因标：`cause: doc`（蓝图缺陷）/ `cause: impl`（实现缺陷）/ `cause: misread`（测试人误解）。量化后才知道下个项目该在哪层加投入，也是向甲方解释返工成本、下个项目报价的证据。因标归到**域**即可（ticket 级归因不可靠，别硬做）。

**回流的不只是计数，还有口径内容**（2026-09-07，一个已上线项目维护期的实测教训：743 条验收问题里 160 条把「待甲方拍板」埋在台账文本里没人再读，蓝图停更 40 天）：③验收层每条 `cause: doc` 必须挂到一条 **decision 记录**（项目级 `doc/decisions.yaml`，形态 = 本 skill `requirements.yaml` 的 `decided_*` 四字段 + `question/fallback`，AI 只能登记 clarify + fallback，`decided_*` 只准人填）。拆解期用 requirements.yaml，**验收/维护期用 decisions.yaml**，两者 `decided_at` 同为新鲜度比较字段；decisions 里 decided 且牵动蓝图的条目走本 skill 的 authority 变更链（改 `authority/*.yaml` → `authority_lint diff/impact`），不再靠人记。实现见 一个实战项目`doc/test/_autopilot/lib/decisions.py`。

**一等闸门口径带到需求层**：对「蓝图和实现可能一起错」「审美体感」这两类**机器天花板**需求（口径翻案 / 中文语感 / 间距），明示真实拦截来自 ③验收层的**客户批式真机轮**（一等闸门，不撤、非兜底——它是真问题的主来源）；需求层只负责把这类页面标出来供批式验收覆盖，别指望 accept/回归包能拦住。

## 5. STOP（叫醒 负责人 / 编排层转人）

0. **没跑 §0.3「最终想法」就直接抽 REQ → STOP。** 需求层没收敛，后面每一层都在猜，
   而返工的账要到 ②zhixing 的 QA 门才结——那时一轮的单位是整个任务。
1. 素材自相矛盾 / 关键业务流缺失 → 别脑补，`needs_human` 列"我有什么 / 我缺什么"
2. 口径有多种合理解释 → 按 **§0.3 拍板边界**分两类，**别一律推给人**（全模式通用）：
   - **可回滚的实现口径**（命名/排序/默认值/文案/UI 细节）→ **你定**，REQ 标
     `status: clarify` + 默认口径写进 `note`、`decided_*` 留空，**继续拆不停机**。
   - **不可逆或跨边界**（数据模型主键与迁移/计费定价/对外 API 与隐私承诺/删改存量
     数据/触碰 `deferred`）→ **STOP**，`needs_human` 列澄清项。
   ⚠️ 「绝不替甲方拍板」写得太满就会**把所有模糊都推给人**，而人只答得上关乎结果的
   那部分。T006 的实况是攒了一长串澄清项过去，回话是「这些我无法判断，你自己
   自行判断执行」—— 那等于这一步白做。写 `decided_*` 的权力仍然只属于人。
3. 原型版本对不上 / 有多套原型 → STOP 确认权威版本再拆
4. 多系统项目的领域主权未决（谁写这张表说不清）→ STOP，主权定不下来拆出的 ticket 必返工
4b. **权威改了但没跑 `impact`** → STOP。已完工 ticket 可能建在错图上而 accept 仍绿，
    这是"绿着错"，比红着错危险得多
4c. **设计定稿了但没跑「设计权威抽取」→ STOP**。稿上会变的内容块没回流成 REQ/字段，
    等于把一批需求留在图片里 —— 它们不会自己变成 ticket，只会在实现期变成返工。
5. `coverage_lint` 报漏拆但你认为"那条本期不做" → **不许直接改 status 混过去**，
   先跟甲方/负责人 确认，确认后才标 `deferred` 并写 `defer_to`

## 6. 实例参考

本资源包不含客户项目实例（涉及客户业务，已移除）。具体栈的约定和踩坑见资源包里的 `stacks/<栈>/`（如 RuoYi-Vue-Plus 的 `profile.yaml` 与 `gotchas.md`）；本文正文保持 stack-agnostic。
