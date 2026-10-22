# ⚠️ 复制成 doc/_adr/NNNN-<slug>.md 时从下面第一个 `---` 开始复制，本说明不带走。
#
# ADR（架构/口径决策记录）—— 「哪些决策是最终的」的唯一载体
# ============================================================================
# 源自 一个实战项目一期 20 份实战 ADR 的格式（doc/_adr/README.md），2026-08-17
# 提炼进 skill 并加结构化 decision 字段。要点：
#
# 1. **双轨 status**（一期最重要的发明）：`status` 管决策本身、`implementation_status`
#    管代码现状。「决策没被推翻、但实现被代码改掉了」是最常见的状态——只有一个
#    字段时无法表达，AI 读到 accepted 就会照着不存在的设计写代码。
# 2. **最终口径必须写进 frontmatter 的 decision 块，正文只写理由。**
#    口径写在散文里会自相矛盾（实证：一期 ADR-0011 同一份 accepted 文件里 :24 与 :65
#    两个互相矛盾的口径并存，impl subagent 只能靠「顶部看起来更新」猜）。
#    authority_lint 只 hash frontmatter 语义字段——改理由措辞不触发 impact，改口径才触发。
# 3. **ADR-NNNN 本身就是锚**：ticket front-matter 用 `adr_refs: [ADR-0011]` 引用；
#    ADR 翻案（status/decision 变了）→ authority_lint diff/impact 反查受影响 ticket，
#    与蓝图翻案同一条传播链。
# 4. 翻案必须回头改被推翻那份（status: superseded + superseded_by）——这是唯一的硬要求。
# 5. anchor 必须能 grep 到（file:line / 表名 / 类名）；adr_check.py 验存在性。
#    写「以代码为准」这种没锚点的话等于没写。
# ============================================================================

---
id: ADR-0001
status: accepted                    # proposed | accepted | amended | deprecated | superseded
implementation_status: implemented  # implemented | partially-implemented | not-yet-implemented
                                    # | superseded-by-code（代码已不这么干但决策没被推翻）
                                    # | needs-verification（没人核对过，不可当依据）
supersedes: []                      # 推翻了哪些 ADR（对方要同步标 superseded + superseded_by）
superseded_by: []
amends: []                          # 澄清/收窄了哪份（对方仍生效）
amended_by: []
verified: 2026-08-17                # 上次拿代码核对 implementation_status 的日期
anchor: "src/core/EarNoAllocator.java:58"   # 机器可验锚点，adr_check.py 查存在性
decision:                           # ★ 最终口径（结构化，正文不许另写一套）
  key: ear_no.seq_width             # 口径的机器名（点分层级）
  value: "3 位（001-999，单农场每天 ≤999 头）"
  rejected_values:                  # 明确不采纳的口径——正文提到它们必须带「不采纳」字样，
    - "4 位（客户码表原稿）"         #   否则 adr_check 报「口径自相矛盾」（抓 ADR-0011:65 那类）
  decided_by: owner                 # 拍板人（永远是人，不是 AI）
  decided_at: 2026-08-17
---
# ADR-0001: <一句话标题：决策了什么>

**决策者**: <谁、依据什么、什么时候拍板>
**关联**: <涉及的类 / 表 / 字典 / ticket>

## 背景（为什么需要这个决策）

<两三段，说清楚问题和约束。>

## 决策（与 frontmatter decision 一致，展开理由）

<为什么选 value、为什么不选 rejected_values——提到被否口径时写明「不采纳」。>

## 后果

<这个决策带来的约束：下游要遵守什么、什么情况下该重开这份 ADR。>
