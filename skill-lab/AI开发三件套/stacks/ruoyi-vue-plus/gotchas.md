# RuoYi-Vue-Plus 栈坑手册（gotchas）—— 唯一权威

> **2026-08-17 起本文件是该栈知识的单一真相源**（`~/.claude/stacks/ruoyi-vue-plus/`，
> git 跟踪）。此前靠 cp 分发：模板与项目里两份已漂 358 行、同一节结论反转
> ——本文以 一个实战项目版（实测修正过的那份）为基线收编。
> **回流约定**：项目内新踩的坑先写项目自己的 `stacks/<stack>-local.md`，项目收尾时
> 人工 append 回本文件；**不要再 cp 本文件到项目里**，项目里只留一行指针。

# ruoyi-notes.md —— RuoYi-Vue-Plus + uni-app 实战教训（死文档，N=1）

> **性质**：这是 实战项目（RuoYi-Vue-Plus + plus-ui + uni-app + MySQL，5 域 / 66 ticket / 3-5 AI 并行写）的**实战教训沉淀**，不是插件框架。
>
> **为什么是死文档而不是插件**：N=1。这些教训目前只在一个项目复现过。按反过度抽象红线（见 `stacks/README.md` §0），等同栈第 2 个项目确认复现，再蒸馏成 `ruoyi-vue-plus-plugin/`。在那之前：开新 RuoYi 项目时**整篇读一遍当 checklist**，命中哪条就抄哪条。
>
> **占位约定**：本文是栈实例，写真实的 RuoYi 类名/列名是对的（栈细节就该具体）。但项目专属业务值（域名/模块名/具体字段/role_id 段/客户业务）一律用占位符——换个 RuoYi 项目这些会变。占位符权威值在项目 `PROJECT.md`。
> - `<<DOMAINS>>` 业务域列表 · `<<BUSINESS_MODULE_PATHS>>` 业务模块子树 · `<<MIGRATION_DIR>>` 迁移目录 · `<<DB>>` 库名
> - `<<PROJECT_PREFIX>>` 项目业务前缀（例如 `abc`，换项目换前缀）

---

## 0. 栈底座速记（什么不能碰）

RuoYi-Vue-Plus 是第三方框架底座，分四层：`ruoyi-common`（24 个横切子模块）→ `ruoyi-modules`（业务）→ `ruoyi-admin`（唯一可执行 jar）→ `ruoyi-extend`（独立伴生服务）。

- **底座源码只读**（`<<FRAMEWORK_BASE_PATHS>>` 在 settings.json deny）。业务代码只写在 `ruoyi-modules/ruoyi-<<PROJECT_PREFIX>>-*/`（`<<BUSINESS_MODULE_PATHS>>` 在 allow）。
- 横切能力（加密 / 翻译 / 脱敏 / Excel / OSS / 多租户 / 幂等 / 限流 / SSE）**已有现成子模块，不要在业务模块重造**。写横切功能前先去 `ruoyi-demo` 找示例。
- 新 CRUD **优先用 `ruoyi-generator` 生成脚手架**（同时产出 Java + Vue），再定制，不要手抄样板。
- 业务模块之间**不互相依赖**（避免循环依赖）。跨域调用走「`ruoyi-<<PROJECT_PREFIX>>-common` 放 interface + DTO」+ Spring 注入实现。`-common` 共享层**只放 contract，不放业务实现**。
- 权限：后端 `@SaCheckPermission("module:resource:action")`，前端 `v-hasPermi` 同字符串——**两边必须同步**。

---

## 1. 金标准建表 DDL（6 件套 + tenant + del_unique 软删退路）

> 治"65 表审计字段列类型错（437 行 patch）"的根。**所有业务表无一例外按此模式建。**

```sql
CREATE TABLE t_<域>_<业务>_<类型> (
    id              BIGINT       NOT NULL COMMENT '主键',
    -- 业务字段...
    tenant_id       VARCHAR(20)  NOT NULL DEFAULT '1001' COMMENT '租户编号',  -- 注①
    create_dept     BIGINT                                COMMENT '创建部门',  -- 审计 6 件套 ↓
    create_by       BIGINT                                COMMENT '创建者',
    create_time     DATETIME                              COMMENT '创建时间',
    update_by       BIGINT                                COMMENT '更新者',
    update_time     DATETIME                              COMMENT '更新时间',
    remark          VARCHAR(500)                          COMMENT '备注',
    del_flag        CHAR(1)      NOT NULL DEFAULT '0'     COMMENT '删除标志',
    del_unique      BIGINT       NOT NULL DEFAULT 0       COMMENT '软删唯一性辅助',  -- 注②
    PRIMARY KEY (id),
    UNIQUE KEY uk_<biz>_tenant (tenant_id, <biz_field>, del_unique)               -- 注③
) ENGINE=InnoDB COMMENT='<业务描述>';
```

**关键约束（每条都踩过坑）**：

- **注①** `tenant_id VARCHAR(20)`，**不是 BIGINT**——要对齐 ruoyi `TenantEntity.tenantId`（String）。INSERT 不显式赋 tenant_id，走 `InjectionMetaObjectHandler.insertFill` 自动填。V1 全是 `'1001'`，V2 启用多租户拦截器时不用改表。
- **注②③ 软删唯一性退路（核心机制）**：`del_unique BIGINT NOT NULL DEFAULT 0`，且**所有 UNIQUE 索引必须以 `del_unique` 收尾**，含 `tenant_id`。
  - 原方案想用 MySQL 8 GENERATED VIRTUAL 列 `del_unique BIGINT GENERATED ALWAYS AS (IF(del_flag='0',0,id)) VIRTUAL`——**落地翻车：MySQL 8 不允许 GENERATED 列引用 AUTO_INCREMENT 主键（ER 3109）**。
  - 退路 = 普通列 `DEFAULT 0` + **应用层 fill**（见 §4 MetaObjectHandler）：软删时 `del_unique = id`。
  - 为什么不能直接拿 `del_flag` 进 UNIQUE：`del_flag` 只有 '0'/'1'，多行被软删后仍冲突。`del_unique`（已删行=id，未删行=0）保证已删除行 key 互不重复、新数据可插入。
  - 为什么不用 PostgreSQL 部分唯一索引：MySQL 8 不支持（除非函数索引，可读性差）。
- **禁止字段级 UNIQUE** 写法（`xxx_no VARCHAR(32) UNIQUE`）——必须写成命名 `UNIQUE KEY` 且含 `del_unique`。
- **例外**：统计预聚合表（定时任务重算、truncate+重写的）**不要 del_flag / del_unique / version**——它们不软删。

**review 硬门槛**：建模 ticket review 时，任一 UNIQUE 索引不含 `del_unique` → review 不过。

**自检 SQL**：
```sql
SELECT COLUMN_NAME FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='<<DB>>' AND TABLE_NAME='<your_table>'
  AND COLUMN_NAME IN ('tenant_id','del_flag','del_unique','create_by','update_by');
-- 期望 5 行
```

---

## 2. Flyway / DDL 文件 `V<yyyyMMddHHmm>` 模块分段（防多 agent 撞迁移序号）

> 这是"编号资源预分配范式"的 RuoYi 实例（见 `stacks/README.md` §1③）。多个 agent 同日并行写 DDL，靠时间戳排序 + 分钟去重防撞。

**命名**：所有 DDL 放 `<<MIGRATION_DIR>>`（一个实战项目是 `script/sql/<<PROJECT_PREFIX>>/`），文件名

```
V<yyyyMMddHHmm>__<TICKET-ID>-<short-desc>.sql
例：V202605200900__SYS-INIT-001-create-business-tables.sql
    V202605211030__BRD-MD-002-farm-barn-pen.sql
```

- 文件名 timestamp **不是创建时间，是"预期上 staging 的顺序"**——所以初始化建表 ticket 必须用最早时间戳。
- 同一天多个 ticket → 精确到分钟（HHmm）；**同窗口**多 agent 同分钟撞了，AI 自行调到下一分钟（同窗口能看到已选的号）。
- **★并行 worktree 场景（多窗口跑多天）**：各 worktree 从同一 dev 切、**互相看不到对方选的时间戳**，"撞了再 +1 分钟"失效（都从同一基线起算，极易选同号）→ 必须**开跑前给每个并行天/分支预占一段不重叠的 HHmm 区间**（如 `day1=0900-0959 / day2=1000-1059 / day3=1100-1159`），写进各天 `README.md` 顶部，agent 只在本段内选号。这是并行 merge 不撞 Flyway 号的关键（呼应 `daily/README.md §1.5` 的"按天预分段"）。
- **没有回滚**：文件名末尾不加 `.down.sql`。

---

## 3. menu_id / role_id 域段预分配（sys_menu / sys_role 撞号防御）

> 同上，编号预分配范式实例。业务菜单要 INSERT 进 ruoyi 自带 `sys_menu`，多 ticket 各加自己的菜单，靠**段分配**防撞。

**menu_id 段**（值是项目专属，权威在 `PROJECT.md`；这里写 一个实战项目实例当范式参考）：

| 段 | 用途 |
|---|---|
| 1-2000 | ruoyi 自带菜单（占用） |
| 3000-4999 | 留给 ruoyi 未来升级吃掉（不要碰） |
| 5000+ 起按域切 1000 一段 | 每个 `<<DOMAINS>>` 一段（系统底座 / 通用主数据 / 各业务域 / 门店追溯 / 预留扩展） |

**规则**：每个 ticket 的 prompt 里写明「你的 menu_id 用 XXXX-XXXX」，**agent 不能跨域占用**。

**role_id 段**：业务角色 INSERT `sys_role`，从 101 起按域/岗位编号（一个实战项目用 101-112 共 12 个业务角色 + ruoyi 自带 `superadmin` role_id=1）。同样在 PROJECT.md 定权威段。

**跨域共享 sys_* 表的写入纪律**（防并行 agent 同改一张表）：
- 第 1 天：建模 ticket **独占** `sys_user` / `sys_role` / `sys_menu` 的扩字段操作，其他 ticket 当天不能 alter `sys_*`。
- 第 2 天起：`sys_dict_data` 多 ticket 各加自己字典，用 **INSERT IGNORE 防撞**。
- `sys_menu` 多 ticket INSERT 各业务菜单，靠 menu_id 段防撞。

---

## 4. 软删 del_unique 必须在 UpdateWrapper 上显式 set（§1 注②的实现）

**为什么必须有这层**：不填的话软删后 `del_unique` 一直是 0，已删除行仍参与唯一性校验，同业务键（如 slug）永久占死，新数据插不进去（真实症状是 `ERROR 1062`）。

**唯一可用写法**（MP 3.5.16 实测，另一个项目 2026-08 加探针坐实）——两列都在 wrapper 上显式 `.set()`：

```java
ContentWork softDelete = new ContentWork();
softDelete.setId(id);
softDelete.setDelFlag("1");
LambdaUpdateWrapper<ContentWork> luw = Wrappers.lambdaUpdate();
luw.eq(ContentWork::getId, id)
   .set(ContentWork::getDelFlag,   "1")
   .set(ContentWork::getDelUnique, id);
baseMapper.update(softDelete, luw);
```

**两条不能走的路**（都会静默失败，不报错，删完 `del_unique` 停在 0）：

1. **`baseMapper.deleteById/deleteByIds` —— 禁用。** `@TableLogic` 把它翻成 UPDATE 时直接把
   `del_flag` 烘进 SQL 字面量，传给 `updateFill` 的实体里**只有主键**，拿不到 `delFlag`。
2. **靠 `MetaObjectHandler` 自动填 —— 在本版本不生效。** 即便实体带上 `delFlag='1'` 走
   `updateById`：MP 3.5.16 生成 SET 子句时会把 `@TableLogic` 列**排除**（实测 SET 里只剩
   `update_by` / `update_time`），而 handler 在 `updateFill` 里 `setValue` 的 `delUnique`
   同样进不了 SET。handler 确实被调用了（探针证实），但它的填充结果落不到 SQL 上。

**业务实体使用约束**：
- 有软删场景（带 `@TableLogic` 的 `delFlag`）的实体，**必须同时声明 `Long delUnique`** 字段（对应 DB `del_unique BIGINT NOT NULL DEFAULT 0`）。
- 不带 del_unique 的实体（`sys_*` / 统计预聚合表）不受影响。

**验收怎么写才不空转**：断言必须是**真行为**——「删一行 → 查 `del_flag='1' AND del_unique=id` → 用同 slug 再建一条应成功」。
只写 `SELECT COUNT(*) ... WHERE del_flag='1' AND del_unique<>id` 型断言在空表上恒为 0，把实现删光也照样绿。

---

## 4.5 后台权限：单人站只授 superadmin，不 seed 角色

本项目是 负责人 一个人用的个人站后台，**不给 content / site 两域菜单 seed 任何 `sys_role_menu` 授权** ——
superadmin 天然可见全部菜单，够用；多角色是没有需求的复杂度。

**这不构成越权面**：非超管 / 无 token / 无 `clientid` 都打不进管理接口（D1 QA 已逐条验过）。

**将来要扩展**（真的多了人用）：不用改代码，在 admin「系统管理 → 角色」里给新角色勾这两组菜单即可 ——
后端权限已按 `@SaCheckPermission("<域>:<资源>:<动作>")` 标全（域 = `content` / `site`，
如 `content:work:list`、`site:about:edit`，以 `sys_menu.perms` 已落库的值为准），前端 `v-hasPermi` 同串，
授权粒度现成。**别为了"以后可能用得上"现在就 seed 角色**，seed 出来没人用的角色只会让人以为它在生效。

---

## 5. 字典缓存退路 `_post-init.sh`（清 Redis 两类缓存）

> 治"dict 撞墙 4 次"里的缓存粘滞那一类。初始化/升级跑完 DDL 后、首次启动 admin 前必跑。

两类要清的 Redis key：
1. **ruoyi 自带 NullValue 粘滞（`*:sys_dict`）**：清库阶段 DELETE/TRUNCATE 了 `sys_dict_*`，如果 admin 在这之前启过哪怕 1 次，Redisson 字典缓存会把"查空"按 NullValue 写进 `1001:sys_dict`（TTL 1h）。SQL 重新灌回数据后，缓存仍返 NullValue 到自然过期 → 表现为 admin 字典下拉空。
2. **业务字典聚合缓存历史残留（`*<<PROJECT_PREFIX>>:dict*`）**：早期把全量字典缓存到 `<<PROJECT_PREFIX>>:dict:full:1001` 等 key（TTL 1h），改字典后小程序仍返旧快照。现改实时聚合，此步清旧残留。

**脚本骨架**（环境变量可覆盖 `REDIS_CONTAINER` / `REDIS_PASSWORD`）：
```bash
#!/usr/bin/env bash
set -e
REDIS_CONTAINER="${REDIS_CONTAINER:-dev-redis}"
REDIS_PASSWORD="${REDIS_PASSWORD:-<redis-pass>}"
# 1) 清 ruoyi 字典 NullValue 缓存
docker exec "$REDIS_CONTAINER" redis-cli -a "$REDIS_PASSWORD" --no-auth-warning \
  --scan --pattern '*:sys_dict' \
  | xargs -I {} docker exec "$REDIS_CONTAINER" redis-cli -a "$REDIS_PASSWORD" --no-auth-warning DEL {} || true
# 2) 清业务字典聚合缓存残留
docker exec "$REDIS_CONTAINER" redis-cli -a "$REDIS_PASSWORD" --no-auth-warning \
  --scan --pattern '*<<PROJECT_PREFIX>>:dict*' \
  | xargs -I {} docker exec "$REDIS_CONTAINER" redis-cli -a "$REDIS_PASSWORD" --no-auth-warning DEL {} || true
```

**新增字典时的完整流程**：DDL 加 `sys_dict_type` + `sys_dict_data` 行 → 跑 `_post-init.sh` flush 缓存（不跑会 NullValue 缓存 1h）。字典 type 必须以 `<<PROJECT_PREFIX>>_` 开头（命名规范）。

---

## 6. uni-app / mp-weixin / wxss 踩坑清单

> 小程序端（uni-app + Vue3 + wot-design-uni）的"只在小程序复现、本地预览正常"那类坑——每条都排查多轮才钉死。

### 6.1 biz 组件必须直接 `.vue` 路径导入，禁运行时桶口
mp-weixin 编译器要直接 `.vue` 路径才能把组件映射成小程序自定义组件。**走桶口 `index.ts` re-export 运行时导入组件 → 该组件渲染成空白**（不报错，极难查）。
```ts
import DemandPicker from '@/components/biz/DemandPicker/index.vue'   // ✅ 组件走 .vue
import type { EntryFormFieldSchema } from '@/components/biz'         // ✅ 类型走桶口 OK（编译期擦除）
import { DemandPicker } from '@/components/biz'                       // ❌ 组件走运行时桶口 → 渲染空白
```
自检：`grep "import {.*} from '@/components/biz'"` 命中的非 `import type` 行 = 待修。

### 6.2 `:not()` / `*` 等复杂 CSS 选择器在 wxss 禁用
小程序 wxss 不支持部分复杂选择器（`*` 通配、`:not()` 等）。手写 style 模拟控件时会静默失效。**业务页用 `wd-*` 组件，不裸写 view+style 模拟**。

### 6.3 `<wd-datetime-picker>` 的 value 是毫秒时间戳，不是 'YYYY-MM-DD'
官方契约：`type="date"` 的 value 是 13 位毫秒时间戳，`min-date`/`max-date` 也是。后端业务字段（`LocalDate`）是 `'YYYY-MM-DD'` 字符串。**输入/输出两侧都要转，少一侧就翻车**：
- 输出没转 → 发毫秒给后端，Jackson 抛 `Invalid value for EpochDay`。
- 输入没转（直绑字符串） → picker 定位不到目标年，退回默认最小年（如 2016），用户只能选到 2016-2018。
```ts
const someTs = computed(() => ymdToTs(form.someDate))  // 输入侧：字符串→时间戳
function onConfirm(e){ form.someDate = toYmd(e.value) } // 输出侧：时间戳→字符串
```

### 6.4 数字输入禁 `<input type="number">`
原生 `type="number"` 会强制类型转换，截断 19 位 snowflake ID。用 `<wd-input type="digit" />` 收 string（详见 cross-layer-contract skill 契约 1）。

### 6.5 staging 部署只走【本地直传】，不要 push CI 自动 build
**部署唯一正路**：本地 `pnpm upload:mp --robot=1 --mode=staging`（本地 build + miniprogram-ci 上传）。**绝对不要用 ubuntu CI 自动 build+上传**。
- 症状：wot-design-uni 的 virtualHost 复杂组件（`wd-search`/`wd-picker`/`wd-tabs`）在**体验版真机** Shadow Root 渲染为空；简单组件正常；本地开发者工具预览正常 → 极难定位。
- 隔离实验决定性结论：本地产物→任何上传 ✅；**CI(ubuntu) 产物→上传 ❌**。唯一坏变量是 ubuntu 构建环境产出的产物（疑似 `@uni-ku/bundle-optimizer` beta 版在不同 OS/node 下产出不同产物结构）。
- **方法论教训**：排查"只在某环境复现"先做**隔离实验缩小变量**再动手——这次连改 5 次配置全打偏，最后靠"两条上传路径 + 同机真机对比"一次锁定。不要轻信单次 workflow 的机制论结论，必须用产物 grep 自证。

### 6.6 LAN IP 漂移防御
mp 调本地后端，`env/.env` 的 `VITE_SERVER_BASEURL` 必须是当前机器 LAN IP，Mac 切 WiFi 会变。测试前自检（让 AI 跑，不进人力测试黑名单）：
```bash
EXPECTED=$(grep VITE_SERVER_BASEURL env/.env | head -1 | sed "s|.*'http://\(.*\):8080'.*|\1|")
CURRENT=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)
[ "$EXPECTED" = "$CURRENT" ] && echo "✅ 对齐" || echo "❌ 改 env/.env + 重 build"
```

---

## 7. Vue3 `useDict` 字典范式（admin plus-ui 端）

admin 端字典走 ruoyi 自带 `useDict('dict_type')` + pinia `dict` store 缓存，**不 per-page 拉、不 hardcode option**。mp 端走项目自建 `<DictPicker dictType="<<PROJECT_PREFIX>>_xxx" v-model="...">`，启动时全量拉一次缓存到 store。字典 type 一律 `<<PROJECT_PREFIX>>_` 前缀。

---

## 8. role 白名单 / `@SaIgnore` 匿名放行范式

公开/匿名接口（如对外追溯只读页、扫码查询）走 `@SaIgnore` 跳过 Sa-Token 鉴权，**而不是**给业务 controller 加白名单（避免误放行整个 controller）。一个实战项目对外追溯 API 范式：`/<<PROJECT_PREFIX>>/public/trace/...` 走 `@SaIgnore` + Redis 缓存。

**退化登录态防御**：mp 端某些"操作人"字段（如打标人/测量人），若没有员工选择端点 → 取登录态 nickname 兜底；正解是补员工 applet 端点 + EmployeePicker 组件一次性解决多处（一次解决同根的多个 ticket，别逐个打补丁）。

---

## 9. VO 操作人翻译用 `USER_ID_TO_NAME`，不要 `USER_ID_TO_NICKNAME`
ruoyi 5.5.x 的 `ruoyi-common-translation` 只实现了 `UserNameTranslationImpl`（`TransConstant.USER_ID_TO_NAME`，输出 `user_name`）。`USER_ID_TO_NICKNAME` **没有 impl**——编译过但运行时翻译为 null。
```java
@Translation(type = TransConstant.USER_ID_TO_NAME, mapper = "operatorId")
private String operatorName;
```

---

## 9.1 `@AutoMapper(reverseConvertGenerate = false)` 会静默关掉「实体 → VO」转换器

**症状**：编译零 error 零 warning，`target/generated-sources` 里**没有**对应的
`XxxToXxxVoMapperImpl`，运行时 `selectVoList` / `selectVoOne` / `MapstructUtils.convert` 拿到 null
或空列表 —— 表现为「接口通了但返回体是空的」，且没有任何异常。

**根因**：这个参数关的方向与直觉相反。VO 上标 `@AutoMapper(target = 实体.class)` 时：

| 方向 | 谁用 | 被 `reverseConvertGenerate = false` 关掉？ |
|---|---|---|
| 实体 → VO | `selectVoList` / `selectVoOne` / `MapstructUtils.convert` | **是** ← 这正是查询要走的方向 |
| VO → 实体 | 基本用不到 | 否 |

**容易误推的地方**：`Bo` 上也常写这个参数（如 `ContentWorkBo`），那里关掉的是「实体 → BO」——
BO 是入参，确实不需要反向，所以在 BO 上是对的。**看到 BO 上有就照抄到 VO 上，就会踩这一脚**。

**规约**：VO（含所有 `Public*Vo`）一律只写 `@AutoMapper(target = 实体.class)`，**不加任何参数**。

**自检**（改完 VO 必跑，比读代码可靠）：

```bash
mvn -q install -DskipTests -pl ruoyi-modules/ruoyi-<<PROJECT_PREFIX>>-content -am
# 每个 Public VO 都应能在生成目录里找到自己的 Impl
find ruoyi-modules/ruoyi-<<PROJECT_PREFIX>>-*/target/generated-sources -name "*ToPublic*MapperImpl.java" | wc -l
```

> 2026-08-14 D10 实施时撞出。**既有 D1-D9 的 29 处 `@AutoMapper` 已全量核查过，无一处带该参数**，
> 线上不受影响 —— 这条是防复发，不是补救。

---

## 9.2 按 IP 限流的部署前提：反代必须**覆盖**而非透传 XFF

匿名写接口（本项目是官网留言 `POST /<<PROJECT_PREFIX>>/public/feedback`）按 IP 限流时，
IP 来自 `ServletUtils.getClientIP()`，它回溯 `X-Forwarded-For` / `X-Real-IP`。
**代码这一侧保证不了正确性**，两个失效模式都只在生产暴露：

| 反代配置 | 后果 |
|---|---|
| 不设 XFF | 所有请求的 IP 都是网关地址 → **一个人触发限流，全站被限** |
| 透传外部传入的 XFF | 攻击者自带 `X-Forwarded-For: 1.2.3.4` → **限流形同虚设**，且能往 `source_ip` 写任意值污染追查线索 |

**正确做法**：nginx 用 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
（在客户端真实 IP 后追加，而不是原样透传客户端自己发的那个头）。上线冒烟要**实际验一次**：
从两个不同外网 IP 各打 4 次，第一个 IP 第 4 次应被拒、第二个 IP 前 3 次应正常。

> 只在源站验没有意义 —— 源站看到的 IP 和经过反代后看到的不是一回事，这与 §12 里
> 「HTML 缓存必须经由反代验」是同一类错误。

## 9.3 Redisson 限流器的速率是「首次创建时固定」的

`RedisUtils.rateLimiter(...)` 底下是 Redisson `RRateLimiter#trySetRate`，
**只在键不存在时生效**。于是「改了配额但没生效」是必然会踩的一脚：

- 把每小时配额从 10 调到 30 → 已存在的 IP 键**沿用旧速率**，直到 TTL（本项目 86400s）过期
- 表现为「配置改了、重启了、行为没变」，且不报任何错

**改配额后**：要么等一个 TTL 周期，要么手动清键 —— `redis-cli --scan --pattern 'global:rate_limit:*' | xargs redis-cli DEL`
（清 Redis 的既有退路见 §5 `_post-init.sh`，同一类问题）。

---

## 10. 抽插件时的红灯（什么时候这份死文档升级成 plugin）

当**第 2 个 RuoYi-Vue-Plus + uni-app 项目**确认以下经验复现，把对应段从这里蒸馏进 `ruoyi-vue-plus-plugin/`：
- §1 DDL 6 件套 + §4 del_unique 退路 → 金标准片段（schema SSOT 落地骨架）
- §2/§3 编号预分配 → 编号资源预分配范式
- §6 uni-app 踩坑 + §5/§9 → 框架踩坑清单

在那之前保持死文档：复制粘贴 + 项目专属占位符替换即可，不做框架化。

---

## 11. mp/admin 真机 E2E verify harness（一个实战项目2026-06-14 坐实，N=1）

> 验收门：UI/mp ticket 唯一完成标准 = 真机 before/after 截图，**build 绿 ≠ 修好**（详 `STAGE2-verify-BACKLOG.md` #4 + `qa-reviewer` agent「真机验收」铁律）。**去人力 A/B/C 测试层**——QA 自跑真机出截图给 负责人 验收。下面是两栈端起栈机械细节。

### 11.1 后台（admin）— Playwright MCP
本地全栈直连 staging RDS（captcha off，`admin/admin123`）→ Playwright MCP 走真链路 + 机械断言（列头/数字/raw key/无 404，**不判美观**）。
- 改 `ruoyi-<<PROJECT_PREFIX>>-*` 后端 → `mvn install -DskipTests -pl ruoyi-modules/ruoyi-<<PROJECT_PREFIX>>-{域} -am` 再重启（多模块单 `-pl` 编不出依赖，**必带 `-am`**）。
- 改字典/迁移 → 重启（Flyway）+ `_post-init.sh` flush redis（§5；不 flush 旧字典缓存读不到新值，伪装成"没改"）。
- 改 plus-ui → vite HMR；前端改完跑 `pnpm build:dev` 验 TS（HMR 不报的 import/类型错只有 build 才暴）。
- start-stack 注入 env 用 `while read` 逐行读 `.env`，**不能 `source <(sed 's/^/export /')`**——`MASTER_DB_URL` 含 `&` 会被 shell 当后台符截断 → fallback localhost 连不上。
- i18n 显示 raw key（如 `xxx.field.yyy`）= 该 key 不存在 → 对应 namespace 补 `zh_CN`+`en_US`（缺一语言切过去仍 raw）。
- 抓弹框存证用可见弹框 `dialogs.filter(d=>d.offsetParent!==null)` 末个，防上一个弹框 DOM 残留误读。

### 11.2 小程序（mp）— 微信开发者工具 automator（Playwright 驱动不了微信 webview）
harness = `.mp-auto/`（项目根 scratch）：`act.js`（声明式步骤驱动）+ `run.sh`（清僵尸再跑）+ `db.py`（直连库造数/验状态）。**起栈一次性，之后逐条复用**：
1. 后端直连 staging RDS（本机 IP 已白名单）起 8080；macOS 无 `setsid` → `nohup mvn spring-boot:run -pl ruoyi-admin -Dspring-boot.run.profiles=dev >/tmp/be.log 2>&1 </dev/null &`（**只 nohup**，否则后台 wrapper 退出 exit 144 牵连杀 mvn）。
2. `cd <miniapp> && pnpm dev:mp-weixin`（dev-login on，打 LAN IP:8080）→ `dist/dev/mp-weixin`。⚠️ dev 重建把 `project.config.json` appid 重置成 `wxMOCK...` → 改回 `touristappid` 再 cli auto。
3. `echo y | <devtools>/cli auto --project <abs dist/dev/mp-weixin> --auto-port 9420`（DevTools 先开「设置→安全→服务端口」）。
4. `bash .mp-auto/run.sh '[{"reLaunch":"..."},{"wait":2000},{"tapText":"..."},{"navigateTo":"..."},{"shot":"<标识>-after"},{"dump":1}]'`——截图落 `.mp-auto/shots/`，**page data dump 是地面真相，重页优先于截图**。
5. `python3 .mp-auto/db.py "SELECT/UPDATE ..."` 直连库造数/验状态流转，绕表单。

**三坑**：① 别默认 `mp.on('console')`（海量日志冲垮 automator ws 读循环挂死）→ `{"enableConsole":1}` 显式开。② 重页截图超时累积孤儿弄僵渲染 → act.js per-step `Promise.race` 超时 + 全局看门狗干净退，**绝不靠外部 kill**；真僵了 kill IDE→重开→cli auto。run.sh 跑前 `pkill -9 -f "node act.js"` 清僵尸防占 ws 单连接槽。③ 后台任务 wrapper 退出 exit 144 牵连杀 mvn → 后端必 nohup 脱离（见上）。

**升级 plugin 红灯**：第 2 个 RuoYi+uni-app 项目（如另一个新项目）复用此 harness 跑通 = N≥2，才把 §11 蒸馏进 `ruoyi-vue-plus-plugin/`（呼应 §10）。

---

## 12. 首次上线（单台 ECS + 宝塔 + 三站点）实测坑清单

另一个项目 2026-08-13 第一次真跑这套部署时撞到的。全是"脚本报绿但事情没成立"或
"报错指向完全错误的方向"那一类 —— 下次换机器照抄部署脚本时，这几条不看就会再踩一遍。

### 12.1 宝塔伪静态：文件名要问主配置，不能按域名猜

宝塔给**纯静态站**建出来的伪静态文件叫 `html_<域名>.conf`，其他类型才是 `<域名>.conf`，
而站点主配置 `include` 的就是它自己那一份。按域名猜文件名时的后果是最坏的那种：

- 文件写进去了 → `nginx -t` 通过（没人 include 它，语法当然没问题）→ 脚本报 ✅
- 而线上一条规则都没生效

判据只能是「**经反代看到的行为变了**」，不能是「文件写进去了」。落点从主配置里抠：

```bash
grep -oE '/www/server/panel/vhost/rewrite/[^;[:space:]]+' \
  /www/server/panel/vhost/nginx/{,html_}<域名>.conf | head -1
```

还有一种：主配置里**根本没有** include 行（面板按站点类型决定加不加），这时必须自己往
server 块里补一行，否则同样是写了等于没写。

### 12.2 宝塔在 http 层开了全局 `proxy_cache cache_one`

在 `/www/server/nginx/conf/proxy.conf` 里，对**所有** `proxy_pass` 默认生效。两个后果：

- **官网**：nginx 决定存不存看的是**上游**的 `Cache-Control`（`next start` 发的是
  `s-maxage=31536000`），而 `proxy_hide_header` 只改发给客户端的那一份。于是
  "响应头显示 no-cache" 与 "nginx 按一年存着" 完全可以同时成立。
  症状：把上游进程整个停掉，经反代取首页**仍然 200**，还带着上游的 `x-nextjs-cache: HIT`。
- **接口**：比页面陈旧严重得多。GET 默认可缓存，而缓存键里**没有 Authorization** ——
  一个用户的列表响应会被原样发给下一个登录的人。

每个发 HTML / 接口的 location 都要显式 `proxy_cache off;`。想让它从站外可观测，加一句
`add_header X-TD-Cache-Status "[$upstream_cache_status]" always;` —— **方括号不能省**：
nginx 对空值的 `add_header` 直接不发这个头，不包一层就没法把"关着"这个状态表达出来。

### 12.3 Redis 容器：配置文件属主，不是权限

官方 `redis:7-alpine` 的 entrypoint 会 gosu 到 `redis` 用户，而我们生成的 `redis.conf` 是
`root:600` → 容器起来就 `Fatal error, can't open config file: Permission denied` 并进重启循环。
**这条错只出现在 `docker logs redis` 里**，从外面看到的现象是「后端连不上 Redis 而反复重启」，
根因隔了两层。

解法是改属主而不是 `chmod 644`（那等于口令对本机所有用户可读），uid 从镜像里问、别写死：

```bash
IDS=$(docker run --rm --entrypoint sh redis:7-alpine -c 'id -u redis; id -g redis' | tr '\n' ':' | sed 's/:$//')
chown "${IDS}" redis.conf && chmod 600 redis.conf
```

### 12.4 导框架底表必须带 `--default-character-set=utf8mb4`

不带时客户端按 latin1 解释文件里的中文，每个汉字膨胀成 3 个字符，炸在
`Data too long for column 'nick_name'` —— **而此时前一百多张表已经建好了**，库处于"导了一半"
的状态，只能 DROP 重来。

验收也别用「表数是个三位数」这种拍脑袋阈值（实测 `ry_vue_5.X.sql` + `ry_workflow.sql`
一共就 36 张）。跟数据源两侧对账：`grep -ci 'CREATE TABLE' *.sql` 的和 == 库里的表数。
再加一条中文没被吃掉的正证：`SELECT COUNT(*) FROM sys_dict_type WHERE dict_name='用户性别'` 返回 1。

### 12.5 `server.port` 必须进生产覆盖层

框架 `application.yml` 写死 `server.port: 8080`。机器上住着别的项目时那个端口多半被占，
表现是启动即 `Port 8080 was already in use`（这还算好的）；更糟的是端口恰好空着 ——
后端起在 8080，nginx 按变量反代到 8081，于是 API 站点 502 而后端日志里一切正常。

### 12.6 服务器上框架默认开着登录验证码

本机开发库里没人碰过 `sys.account.captchaEnabled`，所以「本地能登、线上登不上」要到
上线才暴露，报错是一句 `验证码已失效`。**别去关它** —— admin 登录页在公网上，该开着。
让验收脚本走真实流程：取 `/auth/code` 拿 uuid → 从 Redis 的 `global:captcha_codes:<uuid>`
读答案 → 带 `code`/`uuid` 再登。答案只有本机读得到，不构成对外弱化。

### 12.7 脚本 `git reset --hard` 会换掉正在执行的自己

`deploy-web.sh` 住在它要同步的那份检出里。bash 按字节偏移边读边执行，文件被换掉之后它
从旧偏移继续读新内容，跑出来是新旧混合体。**症状极难联想**：服务器上文件明明已是新版
（grep 得到新代码、git log 也对），CI 里报的却是上一版的错误提示，看起来像"改的代码完全没生效"。

两种解法：同步完 `exec` 自己一次（带环境变量守卫防递归）；或把整个脚本体裹进 `{ ... }`
并以 `exit $?` 收尾 —— bash 会先把整块解析完再执行。`sync-repo.sh` 用后者（它也会 reset 掉自己）。

### 12.8 传静态产物用 tar，别用 `scp -r`

admin 的 `dist` 是 400 多个小文件，`scp` 每个文件一轮往返，跨境 RTT 下实测 **7 分钟没传完**；
`tar -C dist -cf - . | ssh box 'tar -C dest -xf -'` 是 **5 秒**。单个大文件（后端 jar）没这个问题。
tar 中途断流时 ssh 仍可能返回 0，所以传完要对账两侧文件数。

### 12.9 `plus-ui` 的 `.gitignore` 忽略了 `pnpm-lock.yaml`

框架模板的定位是给人 fork 后各自装依赖，所以忽略了 lockfile。但要把 admin 发到线上时，
CI 的 `pnpm install --frozen-lockfile` 直接 `ERR_PNPM_NO_LOCKFILE`；换 `--no-frozen-lockfile`
更糟 —— 那等于每次发布都重新解析依赖树，线上跑的与本地测过的不是同一套。把它加回版本控制。

### 12.10 OSS：ACL 显示 public-read 但匿名读仍 403

那是账号/桶上开着**「阻止公共访问」**，它把生效 ACL 压成私有而**不改显示值**。
判据：`aliyun oss set-acl oss://<桶> public-read -b` 会报 `Put public bucket acl is not allowed`。
只能在控制台关，脚本改不了。别照着 `oss stat` 的输出去排查 ACL —— 那条路走不通。

### 12.11 写断言时的两条自伤（这轮亲手犯的）

- **`--retry-all-errors` 要 curl 7.71+**，Alibaba Cloud Linux 3 自带的是 7.61。用了之后
  **每一次**取数直接失败，16 过 1 红当场变成 8 过 8 红，而站点毫无问题。
  服务器上的脚本只用老 curl 也有的选项。
- **命令替换里的 `exit` 拦不住主脚本**。`X="$(f)"` 起的是子 shell，`f` 里 `exit 2` 只杀那个
  子 shell，主脚本带着空值若无其事地继续跑。要中止就让函数返回非 0、由调用方
  `|| td_stop` 处理。这正是那个函数本来要消灭的那类"报了错却什么也没拦住"。

---

## 13. 断言自身的失效模式（干净 QA 对抗测试打出来的）

§12 记的是"部署时会踩到什么"，这一节记的是**"你写的那条断言为什么拦不住它要拦的东西"**。
这批是 另一个项目 首次上线后，一个不读实施方任何报告的 QA agent 逐条攻出来的 ——
9 条全部属实，其中 4 条会让"报绿"直接变成谎。价值高于 §12：§12 修的是一次部署，
这一节修的是**以后每一次**。

### 13.1 `if ! cmd; then RC=$?` —— `$?` 恒为 0

```bash
bash -c 'f(){ return 3; }; if ! f; then echo $?; fi'   # 打印 0，不是 3
```

`!` 把状态取反之后，`$?` 拿到的是取反的结果。于是所有靠 `exit "${RC}"` 传播失败的写法
统统变成 `exit 0`。本项目里它让 `deploy-web.sh` 把「构建失败 / 锁冲突 / reload 没接上」
三种情况一律报成成功，CI 打绿勾而官网停在旧版；附带 `case "${RC}"` 永远落 `*)` 分支，
把锁冲突报成"构建失败，live 产物未改动"，指向完全错误的方向。

正确写法**不是** `cmd; RC=$?`。脚本开头通常有 `set -Eeuo pipefail`，命令一失败就当场终止，
`RC=$?` 那行根本执行不到 —— 退出码碰巧还是对的（由 set -e 传出），但下面整个 case 的诊断
信息一句都不会打印，而诊断正是那个 case 存在的唯一理由。本项目第一次修就修成了这样。

三种写法实测对照：

```bash
bash -c 'set -e; f(){ return 3; }; if ! f; then echo "A RC=$?"; fi'      # A RC=0   ← 错
bash -c 'set -e; f(){ return 3; }; f; RC=$?; echo "B RC=$RC"'            # 什么都不打印 ← 诊断丢失
bash -c 'set -e; f(){ return 3; }; RC=0; f || RC=$?; echo "C RC=$RC"'    # C RC=3   ← 对
```

`RC=0; cmd || RC=$?` 才能同时拿到退出码并豁免 set -e。

### 13.2 命令替换里的 `exit` 只杀子 shell

`X="$(f)"` 起的是子 shell。`f` 里调 `td_stop`（`exit 2`）时主脚本**若无其事地带着空值继续跑**。
写"取不到就中止"的辅助函数时：消息进 stderr、失败靠返回码，由调用方 `|| td_stop` 中止。

### 13.3 数量对账挡不住"截断在最后一条记录之后"

`mysqldump` 的备份自检数 `CREATE TABLE` 与库里表数对账 —— 听起来很硬。但截断最常发生在
**磁盘写满**那一刻，而那时所有 CREATE TABLE 早已落盘（最大那张表的 INSERT 占了文件大头），
两侧照样配平、判绿。一份缺 6% 尾巴的备份就这样通过了自检。

凡是"这份文件能不能用"的判据，都要有**结构完整性**那一层：`gzip -t` + 内容结尾标记
（`-- Dump completed`）。数量对账只回答"内容对不对"，不回答"文件全不全"。

同源问题：冒烟里"两天内有备份"如果只 `find | wc -l`，一个 17 字节的垃圾文件就让它绿。
**两条叠起来，整条备份链能端到端全绿而一份能恢复的备份都没有。**

### 13.4 通配 pattern 让白名单断言失去区分力

`next.config.ts` 里写 `hostname: "**.aliyuncs.com"`，于是"OSS 域名已放行"这条断言对
桶名打错、地域写错、甚至换成别人账号的桶**全部判绿**。
白名单类断言要求**字面量命中**；通配可以留在配置里当兜底，但不能拿它当证据。

### 13.5 断言读的是哪一棵树

官网在**服务器上**构建，而 `td_check_remote_patterns` 读的是**执行机本地**的
`next.config.ts`。实测过两棵树差着好几个提交 —— 那条断言验的是一份与线上无关的文件。
凡是"配置对不对"的断言，都要问一句：**出事的那份配置在哪台机器上？**

### 13.6 nginx `add_header` 不继承

本层只要写了任意一条 `add_header`，父层的**全部**丢弃。宝塔在 server 层写 HSTS，
而我们在每个 location 里加了自己的 `add_header` —— 三个站点的 HSTS 就此静默消失，
面板上还显示开着，看配置也看不出问题。每个定义了 `add_header` 的 location 都要补齐父层那些。

同源陷阱：`add_header` 的值为**空**时 nginx 直接不发这个头，所以
"头在且值为空 = 关着"这种判据永远成立不了，要包一层（`"[$upstream_cache_status]"`）。

### 13.7 反代站点的 `root` 要指向一个"发不出完整站点"的目录

全走反代的站点，`root` 平时确实没用。但伪静态一旦不生效，nginx **不会 502**，
而是从 `root` 发磁盘上的文件并返回 **200**。如果那里恰好是上一套站（迁移场景很常见），
冒烟会一路绿：有 `<title>`（绿）、sitemap 的 loc 全是官方域名（绿）、
大小写变体 404（旧站也是真目录，绿）—— 只有缓存头那条会红，而它的报错文案指向
"装 web.rewrite.conf"，没人会想到"你正在看一份半年前的站"。

把 `root` 指到空目录，降级时直接 403/404，一眼看得出来。

### 13.8 只打印不判定的"断言"，和悄悄少验几条的"全绿"

- 某一条只 `td_info` 从不 `pass`/`fail` —— 它被编号成一条检查，却对结果毫无贡献，
  无论线上什么样都不会红。要么改成真判据，要么从编号里拿掉。
- `--no-ssh` 跳过的条目若既不 pass 也不 fail，退出码仍是 0，而文件头写着"0 = 全绿" ——
  于是"全绿"里不包含被跳过的那几条（本项目里恰好是单点部署唯一的兜底：备份）。
  跳过要记 `SKIPPED` 并打进汇总行：**未验的不算绿。**

### 13.9 app 也要绑 127.0.0.1，不能只绑数据库

MySQL/Redis 容器绑环回是常识，Spring Boot / Next 却常常留在 `0.0.0.0`。
共用机器上这很危险：安全组不归你管（本项目实测那台机器上已有别人加的 3306/5432
对 `0.0.0.0/0` 开放），同机任何容器也能直连 `172.17.0.1:<端口>` 绕过 nginx。

**后端**：覆盖层写 `server.address: 127.0.0.1`。实测有效 —— 从内网 IP 与 docker 网关连
都是 `000`，只有 127.0.0.1 通。

**官网（Next）：★别加 `-H 127.0.0.1`。** 本项目试过，当场把线上打成 500：
Next 16 的中间件重写是**自代理**实现的，它按请求头拼自己的地址；绑 127.0.0.1 之后
拼出来的是 `https://localhost:3000`（`https` 来自 nginx 传的 `X-Forwarded-Proto`），
对着自己的明文端口做 TLS 握手 → `EPROTO: packet length too long` → 每个页面 500。
日志里那句是 `Failed to proxy https://localhost:3000/zh`。

官网端口留 `0.0.0.0` 的风险要小得多：它服务的就是公开内容、没有凭证面，且安全组本来
就不放行它。真要收，用防火墙规则挡 3000 的非环回入站，不要动 Next 的绑定地址。

顺带：生产关掉 `springdoc`。`/v3/api-docs` 匿名可读会把整个后台的接口面、参数名、
权限模型免费交出去（本项目实测 345 条路径）。

---

## 14. accept 断言环境的两条自伤（源自 一个实战项目scripts/verify-env.sh.example，2026-08-17 收编）

### 14.1 zsh 不做 word splitting：`$MYSQL -Nse "SQL"` 报 command not found
把 mysql 命令存成变量再 `$MYSQL -Nse ...`，zsh 会把整串当一个命令名（bash 反而正常，
最难查的那种环境差异）。**一律用函数封装**（见栈包 verify-lib.sh 的 `db()`），bash/zsh 通用。

### 14.2 RuoYi 登录缺 clientId → token 为空 → 所有 API 断言静默变红
登录必须带 `clientId` + `grantType`（body 与 header 双处），缺了返回体没有 access_token，
后续 curl 带着空 token 全部 401——但断言只看到"红"，看不出根因。用 verify-lib.sh 的
`login()`，别在 accept 里手写登录。
