**[EN](README.md)** | **[中文](README_CN.md)**

# ExpenseFlow

AI 驱动的企业报销管理平台 — 从发票提交到付款入账的全流程自动化，内置 5-Skill 合规审核管道、对话式 Agent 助手和 Eval 评估框架。

> 核心设计原则：区分 Workflow 和 Agent、按观众分层、工具白名单防 prompt injection。
> 参照 [Anthropic *Building Effective Agents*](https://www.anthropic.com/research/building-effective-agents)（2024）。

---

## 目录

- [这个项目做了什么](#这个项目做了什么)
- [架构总览](#架构总览)
- [架构亮点 — 跟一般「AI 报销项目」差在哪](#架构亮点--跟一般ai-报销项目差在哪)
- [5-Skill 合规审核管道](#5-skill-合规审核管道)
- [对话式 Agent](#对话式-agent)
- [核心设计决策](#核心设计决策)
- [审批与预算流程](#审批与预算流程)
- [角色权限（RBAC）](#角色权限rbac)
- [Eval 评估平台](#eval-评估平台)
- [API 总览](#api-总览)
- [我们故意不做的 5 件事](#我们故意不做的-5-件事)
- [目录结构](#目录结构)
- [快速启动](#快速启动)
- [技术栈](#技术栈)

---

## 这个项目做了什么

ExpenseFlow 模拟了一套完整的企业报销系统：员工上传发票 -> AI 自动审核（OCR、规则引擎、模糊检测）-> 经理审批（附 AI 决策解释）-> 财务复核 -> 凭证生成 -> 付款执行。

**核心差异化：**

1. **5-Skill 合规审核管道** -- 发票验证、审批链、合规检查（含 AmbiguityDetector 五维模糊评分）、凭证生成、付款执行，全部配置驱动
2. **对话式 Agent** -- 员工通过自然语言完成报销（OCR 识别 -> 分类建议 -> 查重 -> 预算检查），经理获得 AI 解释卡辅助审批决策
3. **Eval 评估框架** -- YAML 定义测试用例，覆盖 Agent 路由、风险分级、RBAC 权限、工具白名单安全

---

## 架构总览

```
员工提交发票
  |
  v
+--------------------------- FastAPI Backend ----------------------------+
|                                                                        |
|  +--------------+    +--------------------------------------+          |
|  |  Chat Agent  |    |     5-Skill 合规审核管道              |          |
|  |              |    |                                      |          |
|  | - Submit     |    |  发票验证 -> 审批链 -> 合规检查        |          |
|  | - Q&A        |    |            | AmbiguityDetector       |          |
|  | - Explain    |    |  凭证生成 -> 付款执行                 |          |
|  +------+-------+    +--------------+-----------------------+          |
|         |                           |                                  |
|  +------v---------------------------v--------------------------+       |
|  |                     数据层                                  |       |
|  |  SQLAlchemy Async | Submissions | Drafts | Budgets          |       |
|  |  Employees | AuditLogs | CostCenterBudgets                  |       |
|  +-------------------------------------------------------------+       |
|                          |                                             |
|  +-----------------------v---------------------------------+           |
|  |                  YAML 配置层                             |           |
|  |  policy | approval_flow | expense_types | workflow      |           |
|  |  city_mapping | fx_rates                                |           |
|  +---------------------------------------------------------+           |
+------------------------------------------------------------------------+
       |                                      |
  +----v------+                      +--------v--------+
  | 前端       |                      | Eval 评估框架   |
  | 员工端     |                      | YAML 测试用例   |
  | 经理端     |                      | Agent 行为      |
  | 财务端     |                      | 验证            |
  +-----------+                      +-----------------+
```

---

## 架构亮点 — 跟一般「AI 报销项目」好在哪

大部分 portfolio 上的「AI for X」项目就是包一层 GPT-4o。这个项目是**按生产级欺诈检测系统真实面对的工程取舍来设计的**。六处差异：

### 1. 混合架构：确定性规则 + OODA agent

- **Layer 1** — 20 条确定性 fraud 规则 + 5 因子 `AmbiguityDetector`。每笔报销都跑，毫秒级。每个 flag 引用一个具体的 `rule_id`，完全可审计。
- **Layer 2** — OODA agent，**只在** Layer 1 判定「这单可疑」时才触发（约 10% 的报销）。多轮（最多 4 轮）、LLM 自主选 tool、构建 evidence chain、输出 verdict（`clean` / `suspicious` / `fraud`）+ 置信度。

纯规则抓不到未知 pattern；纯 agent 每笔 ~$0.02、不确定性高。混合架构同时拿到两边的好处 —— 跟 Airwallex Spend AI / Stripe Radar / Concur Detect 是同一种形态。

[完整设计：`docs/hybrid-fraud-architecture.md`](docs/hybrid-fraud-architecture.md)

### 2. Cohen's κ 评估框架 — 不是简单 accuracy

大部分 AI 产品 portfolio 拍胸脯说「准确率 95%」却没说怎么测的。这个项目：

- 有 **人工标注 ground-truth 数据集**（`backend/tests/eval_datasets/*_human_labeled.yaml`）—— 每个 case 都带 `human_label` + `labeler_note` 解释**为什么人会这么标**（Hamel 的 specification gulf 落实）
- 测 **Cohen's κ**（评分者间一致性）—— 减掉「凭运气也能对」的部分。类别不均衡时，accuracy 会骗人
- CI 里 assert **κ ≥ 0.40**；漂移会让测试失败
- Dashboard 上展示 **κ + confusion matrix**（Review Quality tab），人能直接看
- Placeholder 数据 SKIP 而不是 fake-PASS —— **不要假绿信号**

[完整框架：`docs/evals-reference.md`](docs/evals-reference.md)

### 3. 工具白名单作为安全边界（Concur Joule 模式）

Agent 的写工具集 = 空集。**构造性安全**，不靠提示词约束。Dispatcher 就一行：

```python
fn = INVESTIGATION_TOOLS.get(tool_name)
if fn is None:
    raise ValueError(f"unknown tool: {tool_name}")
```

Prompt injection 能让 LLM 输出任何字符串。但**没有任何字符串**能映射到一个改业务状态的函数 —— 因为字典里**根本没有这种函数**。Tool registry 里就 8 个 read-only 函数。安全在边界强制，不在 prompt。

跟 Concur Joule、Expensify Concierge 一个套路：**LLM 允许不可靠；边界不允许**。

### 4. 诚实的 Workflow vs Agent 标签

「Agentic AI」这个词被滥用。[Design Decisions §1](#1-workflow-vs-agent诚实申报) 表格把代码里每个「看起来像 agent」的组件都映射到两类：

- **Workflow**（确定性、步骤预定）—— 系统大部分都是这个，是故意的
- **Agent**（LLM 真的做决策）—— 只在确实需要的地方

6 行总计，**只有 1 个**是真正的 multi-round agent（fraud investigator）。其他都老实标成 workflow 或单轮 dispatcher。避开了 portfolio 常见的「啥都叫 agentic 因为听着唬人」的坑。

### 5. Cite-the-rule 可解释性

系统给的每个 flag 都指向一个具体 `rule_id`，附结构化元数据：rule 文本、severity（`info` / `warn` / `error`）、修复建议、证据。AI 说「这单有问题」，经理看到的是**哪条规则** + **为什么** —— 不只是一个风险分数。

结构化的 `violations[]` 数组存在 `audit_report` 里，AI 解释卡上以「📋 触发规则」区块呈现。审计师能把每个红 flag 追溯到具体可引用的规则。

[实现：`agent/violation_registry.py`](agent/violation_registry.py)

### 6. 诚实划范围 — 多实体「写了设计但不实装」

大部分 portfolio 项目过度承诺。这个项目：

- **单实体生产代码** —— 真能跑通整条流程
- **多实体架构设计** —— [`docs/multi-entity-design.md`](docs/multi-entity-design.md) —— 4 层解耦（Entity / Category / Mapping / Policy） —— **故意不实装**，因为没有真客户在等
- **README 里写明范围** —— "Configuration-driven within fixed schema"（不是「零代码适配任何客户」）

把**没做什么**也标出来本身就是 portfolio 信号。大部分候选人吹牛；写好契约延后做的，反而比堆半成品更可信。

### 7. 工业级落差先文档化、再实装

从「portfolio demo」到「production-grade SaaS」之间还差 8 件事 —— 多租户、真 ERP 集成、真支付、SOC 2 认证、运营成熟度等等 —— **全部以 roadmap 文档形式记录**，而不是堆半成品代码。每个 gap 都有工作量估算、决策点、需要找谁谈、依赖关系。

加上一份 **客户分群** 文档（50 人 SaaS / 5,000 人制造业 / 500 人跨境电商哪个先做）和一份 **集成设计** 文档（NetSuite + Stripe Issuing + 给没好 API 的 ERP 用的 Excel-as-bridge 替代方案）。

[Roadmap：`docs/industrial-readiness-roadmap.md`](docs/industrial-readiness-roadmap.md) · [客户分群：`docs/customer-segmentation.md`](docs/customer-segmentation.md) · [集成设计：`docs/integration-design.md`](docs/integration-design.md)

---

**Portfolio 评审建议阅读顺序：**

1. **[`docs/case-study-hybrid-fraud.md`](docs/case-study-hybrid-fraud.md) — 只读一篇就读这个（2 分钟，1 页）**
2. 本 README [Design Decisions](#核心设计决策) 部分 — 分类法和原则
3. [`docs/hybrid-fraud-architecture.md`](docs/hybrid-fraud-architecture.md) — Layer 1 + Layer 2 设计故事（10 分钟）
4. [`docs/evals-reference.md`](docs/evals-reference.md) — Eval 方法论（Hamel 框架适配 + Anthropic Eval Harness 术语）
5. [`docs/eval-harness-plan.md`](docs/eval-harness-plan.md) — 能力 vs 回归集 + bug-hunt 循环 + 60 分目标
6. [`docs/multi-entity-design.md`](docs/multi-entity-design.md) — 故意延后了什么、为什么
7. [`docs/industrial-readiness-roadmap.md`](docs/industrial-readiness-roadmap.md) — 工业级要求（9 个 gap）
8. [`docs/customer-segmentation.md`](docs/customer-segmentation.md) — 应该先卖给谁
9. [`docs/integration-design.md`](docs/integration-design.md) — NetSuite + Stripe + Excel-bridge 真 API 设计
10. [`docs/fapiaoforce-prd.md`](docs/fapiaoforce-prd.md) — Concur-style 多集团产品 PRD（中国企业市场 Segment B 的产品化）

---

## 5-Skill 合规审核管道

每笔报销提交后，后台异步执行 5 个 Skill，全部由 `workflow.yaml` 编排：

| Skill | 功能 | 关键能力 |
|-------|------|----------|
| **01 发票验证** | 发票格式、抬头、查重、日期校验 | 发票号全局唯一约束；城市名标准化（"SH"/"沪"/"上海" -> 统一） |
| **02 审批链** | 按费用类型 x 金额 x 员工等级构建审批链 | 超时升级（24h 提醒 -> 48h 升级 -> 72h 自动升级）；等级豁免 |
| **03 合规检查** | 逐行项 A/B/C 合规判定 + AmbiguityDetector | 五维模糊评分；score >50 触发 Claude 深度语义分析 |
| **04 凭证生成** | 会计分录、增值税拆分 | 专票进项税自动拆分；借贷平衡校验 |
| **05 付款执行** | 五重预校验 + 付款模拟 | >=5000 银行转账，<5000 备用金 |

**Shield 机制：** 当 Skill-03 的模糊评分触发人工复核（30-70 分）或建议拒绝（>70 分），管道停止，标记 `PENDING_REVIEW`，等待人工介入。

**配置驱动（在固定 schema 内）：** 跳过审批只需 `workflow.yaml: approval.enabled: false`，改费用限额只需改 `policy.yaml`，调 AmbiguityDetector 灵敏度只需改 `eval_config.json` —— 全部零代码。

**当前范围：单实体（single-entity）。** 多法人实体的 GL 科目、VAT、审批链分层是架构级扩展，已写入 [`docs/multi-entity-design.md`](docs/multi-entity-design.md)。Schema 级定制（每个费用类型自定义字段、每实体覆盖规则）在 roadmap 上但尚未实装 —— 设计文档里把 4 层解耦讲清楚了，等真有客户需要时按图施工。

### AmbiguityDetector -- 五维模糊评分模型

| 因素 | 权重 | 触发条件 |
|------|------|----------|
| 描述模糊度 | 25% | 描述 <10字 或含泛化词（"其他""杂项""费用"） |
| 金额边界 | 20% | 金额在限额的 90%-110% 区间 |
| 模式异常 | 25% | 7天内 >=3笔同类型 +/-15% 金额 |
| 时间异常 | 15% | 周末的餐费/交通费 |
| 城市不匹配 | 15% | 城市名未识别 或 标准化前后不一致 |

评分 -> 决策：`<30` 自动通过 / `30-70` 人工复核 / `>70` 建议拒绝

Score >50 时调用 Claude API 做深度语义分析（未配置 Key 则回退到规则评分模型）。

---

## 对话式 Agent

一个统一 drawer 跑在所有员工页面，另加一个专用的 submit-chat 跑在 quick 添加流上。安全**不**靠"按 role 切工具白名单"，而是两层：每个写工具自带 data-level ACL，加上一条底线——**AI 没有任何改业务状态的动作**：没有 `submit_expense`、没有 `approve`、没有 `reject`、没有 `pay`。

| Role | 在哪用 | 可用工具 |
|------|--------|---------|
| **employee_submit** | `quick.html` 的内嵌 chat（草稿创建） | OCR 识别、类别推荐、发票查重、草稿写字段、预算查询 |
| **employee** | 所有员工页面共用 drawer（`/api/chat/message`） | 历史记录、报销单详情、消费汇总、预算快照、政策规则、**行项目编辑**（归属 + 状态校验在工具内部） |
| **manager_explain** | 经理/财务审批页的 AI 风险解释卡（`/api/chat/explain/{id}`） | 读审计报告 + 员工历史，组装风险评估 + 审批建议 |

**LLM 抽象：** 默认 MockLLM（确定性状态机，不需 API Key，适合 demo / eval）。设 `OPENAI_API_KEY` + `AGENT_USE_REAL_LLM=1` 切到 GPT-4o。

---

## 核心设计决策

### 0. 设计原则（对标 Airwallex Spend AI / Concur Joule / Ramp Copilot）

ExpenseFlow 沿用现代 AI 原生报销平台（[Airwallex Spend AI 2026](https://www.airwallex.com/blog/meet-your-finance-ai-agents-a-new-way-to-manage-bills-and-expenses)、Concur Joule、Ramp Copilot）共有的三条设计原则：

1. **Automation-first（自动化优先）** —— 重复性、规则化的工作（OCR 识别、政策核对、凭证生成、付款路由）不应消耗人力。实现于 **5-Skill 合规审核管道**。

2. **Explainability（可解释）** —— 每个 AI 决定**透明、可审计、可追溯到具体规则**。具体实现：
   - **Cite the rule（指明规则）** —— `audit_report.violations[]` 列出每条被触发的 rule_id（如 `policy.limit_exceeded`、`ambiguity.description_vague`），附带自然语言说明 + 建议修复动作，在 AI 解释卡上以独立的 **「📋 触发规则」** 区块呈现
   - **Phased timeline（渐进时间线）** —— `audit_report.timeline` 永远只反映**已经发生**的事，不预报未来
   - **每个工具有审计日志** —— 每个写工具都会写一条 audit_log，标明谁 / 哪个角色 / 触发了哪条规则

3. **Human-in-the-loop（人工保留决定权）** —— AI **不执行**承担法律责任的状态变更（提交 / 批准 / 拒绝 / 付款）。这些都是 UI 按钮。实现于聊天 agent 的空动作白名单（`TOOL_REGISTRY["employee"]` 从构造上就不包含这些工具）。

Eval Observatory 仪表盘上的 **AI 自动批准漏斗（Auto-Approval Funnel）** KPI 跟踪这套原则在生产数据上的实际效果（例如「最近 71.8% 报销由 AI 自动批准、21.1% 进入人工复核、7.0% 建议拒绝」），用以判断 tiering 是否真的在做有用的工作 —— 而不只是 eval 套件能跑过。

### 1. Workflow vs Agent：诚实申报

> "Agentic AI" 这个词被滥用。这里明确标出哪里是 agent，哪里是 workflow。

| 位置 | 实现方式 | 实质 | 原因 |
|------|---------|------|------|
| 员工 submit：happy path（上传发票 -> 填字段）| OCR -> dup_check -> suggest -> write，线性流水线 | **Workflow** | 步骤预定，LLM 不需要决策 |
| 员工 submit：用户改字段（"把金额改成 380"）| 需要 Real LLM 解析意图 | **真 Agent** | LLM 需解析意图、动态选 tool |
| 员工 my-reports QA drawer | 关键词匹配 -> 单 tool -> 格式化 | **Workflow** | 单 tool 调用，无决策环节 |
| 5-Skill 审核管道 | 5 步顺序执行，policy_engine 硬规则 | **Workflow（故意）** | 合规要求确定性，不允许 LLM 改流程 |
| 经理/财务 AI 解释卡 | 调只读 tool，组装风险评估 + 审批建议 | **Agent（轻量）** | 需自行决定调哪些 tool 收集证据 |
| Fraud investigator（高风险报销 Layer 2）| OODA 循环，最多 4 轮，LLM 自主从 8 个 tool 里挑，输出 verdict | **真 Agent（多轮）** | 项目第一次出现 multi-round LLM-driven decision。详见 [`docs/hybrid-fraud-architecture.md`](docs/hybrid-fraud-architecture.md) |

### 2. Unified Agent + Data-level ACL（Concur Joule / Expensify Concierge 模式）

共享 drawer 是**单一 agent**——不按页面或角色切行为。授权靠工具内部的校验，不靠路由层白名单：

1. **硬上限：AI 没有任何改业务状态的工具。** Submit / approve / reject / pay 是 UI-only，因为它们承担法律与合规责任，必须由用户点按钮确认。`employee` 白名单里**根本不列出**这些工具。
2. **写工具自带 ACL。** `tool_update_report_line_field` 在 handler 内部再次校验归属（`report.employee_id == ctx.user_id`）、状态（`status ∈ {open, needs_revision}`）、字段（`∈ EDITABLE_FIELDS`）。即使 LLM 被 prompt injection 传入别人的 `line_id` 或攻击 `pending` 状态的单，也会被**工具**拦下，不是路由。
3. **Dispatcher 二次校验。** 在工具自校验之前，如果 LLM 调用的工具名不在当前 role 的白名单里（比如 `employee` 里硬要调 `update_draft_field`），dispatch 阶段直接拒绝。

```python
TOOL_REGISTRY = {
    # 专用流——quick.html 的草稿填写流水线
    "employee_submit": ["extract_receipt_fields", "suggest_category",
                        "check_duplicate_invoice", "get_my_recent_submissions",
                        "update_draft_field", "check_budget_status"],
    # 统一 drawer——所有员工页面共用，授权靠工具内部校验
    "employee":        ["get_my_recent_submissions", "get_report_detail",
                        "get_spend_summary", "get_budget_summary",
                        "get_policy_rules",
                        "update_report_line_field"],   # 工具内部校验归属 + 状态
    # 审批页风险卡——单次请求，结构化 JSON，不是 chat
    "manager_explain": ["get_submission_for_review",
                        "get_employee_submission_history"],
}
```

**为什么选 Unified 而不是按页分 role？** 真实用户都有双重身份——几乎每个中层都是自己交报销的员工 + 审批下属的经理。按 role 分会强迫用户"切帽子"或跳页，才能拿到对的工具集。Data-level ACL 天然处理这种一人两角：LLM 按用户意图挑对的工具，工具在内部对具体对象校验归属。业界前例：SAP Joule、Expensify Concierge、Ramp Copilot、Navan Ava 都是这个模式。

> Agent 永远没有 `submit_expense` / `approve` 工具。这不是 limitation，是 helper vs. actor 的边界。

### 3. 渐进式审计时间线（Phased Timeline）

**问题**：原实现在提交时就把 5-skill 全部结果写入 `audit_report.timeline`，导致"凭证生成 / 付款执行"在经理批准之前就出现在 AI 解释卡里。

**修复**：

```
提交后         timeline = [step0, step1, step2]          phase="submit"
经理批准后     timeline.append(step3: "凭证已生成")      phase="manager_approved"
财务批准后     timeline.append(step4: "付款已执行")      phase="finance_approved"
```

`audit_report.timeline` 永远只反映"已经发生的事"。

### 4. 按观众分层（Audience Layering）

AI 解释卡的信息分两层：

| 信息层 | 谁看 | 展示条件 |
|--------|------|----------|
| 推荐决策、flags、advisory | 所有用户（经理/财务）| 始终显示 |
| tool 调用明细、agent_role | 开发者 / 面试官 | `auth.isDev()` = true |

激活 dev 模式：URL 加 `?dev=1`，或点击导航栏工具按钮。

---

## 审批与预算流程

### 状态机

```
processing -> reviewed -> manager_approved -> finance_approved -> exported
                               |                    |
                           rejected              rejected
```

### 预算管控

每个成本中心按季度设置预算，提交时实时检查：
- **info**（75%-95%）：提醒接近预算
- **blocked**（>=95%）：自动拦截，需财务解锁
- **over_budget**（>100%）：超预算警告

### 风险等级

| 等级 | AI 推荐 | 风险分 | 含义 |
|------|---------|--------|------|
| T1 | approve | <=25 | 发票合规、金额正常、描述具体 |
| T2 | approve | 25-50 | 低风险，有轻微注意项 |
| T3 | review  | 50-75 | 需人工核对，金额偏高或描述模糊 |
| T4 | reject  | >75 | 高风险，金额异常 / 凭证缺失 |

---

## 角色权限（RBAC）

| 角色 | 能力 |
|------|------|
| **employee** | 提交报销、查看自己的报销、对话助手 |
| **manager** | 审批下属报销、查看 AI 解释卡 |
| **finance_admin** | 财务审批、解锁预算拦截、导出凭证、批量操作 |
| **admin** | 员工管理、政策配置、审计日志、预算设置 |

> **Mock 模式**：`AUTH_MODE=mock`（默认），用 `X-User-Id`/`X-User-Role` 请求头模拟身份。浏览器端通过导航栏角色切换下拉框或 URL 参数 `?as=manager` 切换角色。

---

## Eval 评估平台

Eval 系统分三层：**YAML 测试数据集** 定义测什么，**统一评估引擎** 用代码评分器执行测试，**Eval Observatory** 提供可视化仪表盘 + REST API 浏���结果、对比运行、管理 Prompt���

### 架构

```
+-------------------+     pytest      +---------------------+     POST     +-------------------+
|  YAML 数据集       | ------------->  |  统一评估引擎        | ----------> |  Observatory API  |
|                   |                 |  test_eval_harness   |             |  /api/eval/runs   |
|  fraud_llm_rules  |   逐 case      |  .py                 |  运行结果   |                   |
|  fraud_rules_det  |   代码评分      |                      |  + 元数据   |  存入 DB:         |
|  ambiguity_detect | <----------->  |  通过率汇总           |             |  EvalRun, LLMTrace|
|  layer_decision   |  code_graders  |  P/R/F1 按组件统计    |             |                   |
|  category_classif |  .py           |                      |             |  Eval 仪表盘       |
+-------------------+                +---------------------+             +-------------------+
```

### 被评估的 5 个组件

| 组件 | 数据集文件 | 用例数 | 测试内容 |
|------|-----------|--------|----------|
| **确定性欺诈规则** | `fraud_rules_deterministic.yaml` | ~60 | 14 条规则函数（同餐重复、地理冲突、阈值接近、周末频率、整数金额、连号发票、商户类别不匹配、离职前突击、汇率套利、共谋模式、供应商频率、季节异常、幽灵员工、时间戳冲突）。每条提供输入 + 期望信号（触发/不触发） |
| **LLM 欺诈分析** | `fraud_llm_rules.yaml` | ~15 | GPT-4o 语义分析（模板检测、票据矛盾、人均金额合理性、描述模糊度评分）。支持 pass^k 多次试验以应对非确定性输出。需要 `OPENAI_API_KEY` |
| **模糊检测器** | `ambiguity_detector.yaml` | ~12 | 五维评分模型：分数范围验证、触发因素核查、推荐动作检查（auto_pass / human_review / suggest_reject） |
| **分层决策** | `layer_decision.yaml` | ~20 | 快速提交分层路由：给定 OCR/分类/查重/预算信号，验证正确的层级分配（green/yellow/red） |
| **类别分类器** | `category_classifier.yaml` | ~20 | 商户名 -> 费用类别映射（餐费/交通/住宿/娱乐/其他） |

### YAML 测试用例格式

所有用例遵循统一结构：

```yaml
- id: rule1_positive_overlap
  component: fraud_rules_deterministic
  rule: duplicate_attendee
  description: "A和B同日同商户报餐，B的attendee含A -> 应触发"
  input:
    submissions:
      - id: "s1"
        employee_id: "emp-A"
        amount: 200
        category: "meal"
        date: "2026-04-10"
        merchant: "海底捞"
        attendees: ["emp-B"]
      - id: "s2"
        employee_id: "emp-B"
        amount: 180
        category: "meal"
        date: "2026-04-10"
        merchant: "海底捞"
        attendees: ["emp-A"]
  expect:
    has_signal: true
    rule_name: "duplicate_attendee"
```

新增测试场景只需在 YAML 里加一条，不需要改 Python。

### 代码评分器（评分环节无 LLM）

所有评分都是确定性的 -- `backend/tests/graders/code_graders.py`：

| 评分器 | 检查内容 |
|--------|----------|
| `grade_score_range` | 实际分数在 [lo, hi] 范��内 |
| `grade_field_match` | 字段值精确匹配 |
| `grade_enum_in` | 值在允许集合内 |
| `grade_list_contains` | 列表包含所有必需项 |
| `grade_bool` | 布尔匹配（用于 `has_signal`） |
| `classify_detection` | TP/FP/FN/TN 分类，计算 P/R/F1 |

通用 `grade_case(actual_output, expect)` 函数自动分派：`*_range` 键用范围评���器，`has_signal` 用布尔评分器，`layer` 用精确匹配，等等。

### 6 因素可复现性追踪

每次评估运行捕获 6 个因素到 `eval_config.json`，确保可复现：

| 因素 | 追踪内容 | 示例 |
|------|----------|------|
| **Prompt 版本** | 当前激活的 prompt 模板 | `v1` |
| **模型** | LLM 模型 + 快照 | `gpt-4o-2025-03-01` |
| **采样参数** | temperature, top_p, max_tokens | `0.0, 1.0, 1024` |
| **配置阈值** | 规则级别的调参旋钮 | `threshold_proximity_pct: 0.03` |
| **解析版本** | LLM 输出如何解析为结构化数据 | `v1 (JSON + regex fallback)` |
| **数据集哈希** | 所有 YAML 数据集文件的联合 MD5 | 自动计算 |

当通过率下降时，通��� `GET /api/eval/runs/{a}/diff/{b}` 对比两次运行，精确定位哪个因素变了、哪些用例回退了。

### Observatory 仪表盘 & API

**Web 界面** `http://localhost:8000/eval/dashboard.html`：
- KPI 卡片：总用例数、通过率、按组件分解
- 通过率趋势图（最近 10 次运行）
- 逐 case 下钻：输入、期望、实际输出、分类
- 运行对比（diff 视图）：元数据变化 + case 回退/改进
- Prompt 管理：查看/编辑/版本化 prompt 模板，设置激活版本
- 一键触发评估（后台运行 pytest）

**REST API** `/api/eval/`：

| 方法 | 路径 | 功能 |
|------|------|------|
| `GET` | `/runs` | 评估运行列表（分页） |
| `GET` | `/runs/{id}` | 单次运行详情 + 所有 case 结果 |
| `POST` | `/runs` | 记录评估运行（由引擎自动调用） |
| `GET` | `/runs/{a}/diff/{b}` | 对比两次运行：元数据差异 + case 回退 |
| `GET` | `/traces` | LLM 调用追踪列表（可按组件/错误状态过滤） |
| `GET` | `/traces/{id}` | 单条追踪 + 完整 prompt & response |
| `GET` | `/stats` | 聚合统计：通过率趋势 + 组件错误率 |
| `GET/PUT` | `/config` | 读取/更新 6 因素配置 |
| `POST` | `/trigger` | 触发评估（后台 pytest 子进程） |
| `GET` | `/trigger/status` | 检查评估是否正在运行 |
| `GET` | `/prompts` | 所有 prompt 模板及版本数 |
| `GET` | `/prompts/{key}` | 完整 prompt + 所有版本 |
| `PUT` | `/prompts/{key}/versions/{v}` | 创建/更新 prompt 版本 |
| `PUT` | `/prompts/{key}/active` | 设置激活的 prompt 版本 |

### 检测质量指标

对确定性欺诈规则，引擎按规则计算 **Precision / Recall / F1**：

```
  -- Detection Quality (P/R) --
  fraud_rule_duplicate_attendee:    P=100% R=100% F1=100%
  fraud_rule_threshold_proximity:   P=100% R=100% F1=100%
  fraud_rule_weekend_frequency:     P=100% R=67%  F1=80%
```

分类矩阵使用业务语言：
- **正确标记** (TP) = 规则正确触发
- **误报** (FP) = 规则不该触发但触发了
- **漏报** (FN) = 规则该触发但没触发
- **正确放行** (TN) = 规则正确地没有触发

### 运行评估

```bash
# 运行全部 eval case（确定性部分无需 API Key）
pytest backend/tests/test_eval_harness.py -v

# 只跑欺诈规则
pytest backend/tests/test_eval_harness.py -v -k deterministic

# 只跑模糊检测器
pytest backend/tests/test_eval_harness.py -v -k ambiguity

# 带 LLM 欺诈分析（需要 OPENAI_API_KEY）
OPENAI_API_KEY=sk-... pytest backend/tests/test_eval_harness.py -v -k llm

# Agent 行为评估（独立引擎）
pytest backend/tests/test_agent_eval.py -v -s

# 通过 Observatory API 触发（从仪表盘一键运行）
curl -X POST http://localhost:8000/api/eval/trigger \
  -H 'Content-Type: application/json' -d '{"component": "all"}'
```

结果自动 POST 到 Observatory API（如果服务在运行）；否则保存到 `backend/tests/eval_last_run.json`，稍后导入

---

## API 总览

**报销单（Reports — 员工把多条行项目打包成一张报销单整体提交）**

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| `POST` | `/api/reports` | employee | 新建报销单 |
| `GET` | `/api/reports` | employee | 我的报销单列表 |
| `GET` | `/api/reports/{id}` | all | 报销单详情（员工只能看自己的） |
| `POST` | `/api/reports/{id}/submit` | employee | 提交报销单进入审批 |
| `POST` | `/api/reports/{id}/withdraw` | employee | 撤回已提交/已批准的报销单 |
| `POST` | `/api/reports/{id}/resubmit` | employee | 重新提交 `needs_revision` 的报销单 |
| `POST` | `/api/reports/{id}/approve` | manager | 经理批准 |
| `POST` | `/api/reports/{id}/reject` | manager | 经理拒绝 |
| `POST` | `/api/reports/{id}/return` | manager | 退回修改（`needs_revision`） |
| `POST` | `/api/reports/{id}/finance-approve` | finance_admin | 财务批准 |
| `POST` | `/api/reports/{id}/finance-reject` | finance_admin | 财务拒绝 |
| `PATCH` | `/api/reports/{id}/title` | employee | 重命名报销单 |
| `PATCH` | `/api/reports/{id}/lines/{sid}` | employee | 编辑某条行项目 |
| `DELETE` | `/api/reports/{id}/lines/{sid}` | employee | 删除某条行项目 |
| `DELETE` | `/api/reports/{id}` | employee | 删除空报销单（仅限 open + 0 条目） |

**行项目 Submissions**

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| `POST` | `/api/submissions` | employee | 提交报销单，返回 202 + 后台 AI 审核 |
| `GET` | `/api/submissions/{id}` | all | 查询单条（员工只能查自己的） |
| `GET` | `/api/submissions` | all | 列表（员工只见自己的） |
| `POST` | `/api/submissions/{id}/approve` | manager | 经理批准 |
| `POST` | `/api/submissions/{id}/reject` | manager | 经理拒绝 |
| `POST` | `/api/finance/submissions/{id}/approve` | finance_admin | 财务批准 + 凭证号 |
| `POST` | `/api/finance/submissions/{id}/reject` | finance_admin | 财务拒绝 |
| `GET` | `/api/finance/export/preview` | finance_admin | 待导出列表 |
| `POST` | `/api/finance/export` | finance_admin | 批量导出 CSV |

**Agent / Chat**

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| `POST` | `/api/chat/drafts` | employee | 新建 draft |
| `POST` | `/api/chat/drafts/{id}/receipt` | employee | 上传发票到 draft |
| `POST` | `/api/chat/drafts/{id}/message` | employee | Agent 1：`quick.html` 内嵌 submit chat（SSE） |
| `POST` | `/api/chat/drafts/{id}/submit` | employee | Draft 转为正式 submission |
| `POST` | `/api/chat/message` | employee | Agent 2：所有员工页面共用 drawer（SSE）。可选 `context: {report_id}` 告诉 LLM 当前打开的是哪张报销单 |
| `POST` | `/api/chat/explain/{id}` | manager / finance_admin | Agent 3：结构化 AI 风险卡（JSON，不是 chat） |

**预算**

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| `GET` | `/api/budget/status/{cost_center}` | all | 成本中心预算状态 |
| `GET` | `/api/budget/snapshot/me` | employee | 我的预算概况 |
| `GET/PUT` | `/api/budget/policies/{cc}` | finance_admin | 预算策略配置 |
| `GET/POST` | `/api/budget/amounts` | finance_admin | 预算额度管理 |

**管理**

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| `GET/PUT` | `/api/admin/policy` | admin | 报销政策 |
| `GET` | `/api/admin/audit-log` | admin | 审计日志 |
| `GET` | `/api/admin/stats` | admin | 汇总统计 |
| `GET` | `/api/users/me` | all | 当前用户信息 |

---

## 目录结构

```
backend/
  main.py                          # FastAPI 入口
  config.py                        # DATABASE_URL 等环境配置
  storage.py                       # 文件存储抽象（Local / R2）
  api/
    middleware/auth.py              # RBAC 认证中间件（Mock / Clerk 双模式）
    routes/
      submissions.py               # 报销提交 + 5-Skill 管道触发
      chat.py                      # 对话 Agent（3 种角色）
      reports.py                   # 报销单管理
      approvals.py                 # 经理审批 + 渐进 timeline
      finance.py                   # 财务审批 + 凭证号 + CSV 导出
      budget.py                    # 预算管理
      fx.py                        # 外币汇率转换
      admin.py                     # 政策配置 / 审计日志 / 统计
      employees.py                 # 员工档案 CRUD
      ocr.py                       # OCR 识别接口
      eval.py                      # Eval 仪表盘 API
  db/store.py                      # SQLAlchemy async ORM + CRUD
  quick/
    pipeline.py                    # 快速提交管道编排
    layer_decision.py              # 分层决策引擎
    finalize.py                    # 草稿 -> 正式提交转换
  services/
    fraud_rules.py                 # 确定性欺诈检测规则
    llm_fraud_analyzer.py          # LLM 欺诈分析
    fx_service.py                  # 汇率服务
    config_loader.py               # YAML 配置加载器
    trace.py                       # 调用链追踪
  tests/
    eval_cases.yaml                # Eval harness 用例
    eval_datasets/                 # Eval YAML 测试数据集
    graders/                       # 自定义评分器
    test_*.py                      # 单元 + 集成测试
agent/
  controller.py                    # ExpenseController -- workflow 编排引擎
  ambiguity_detector.py            # 五维模糊评分 + Claude API 深度分析
skills/
  skill_01_receipt.py              # 四重发票校验
  skill_02_approval.py             # 审批链 + 超时升级
  skill_03_compliance.py           # A/B/C 合规判定 + Shield
  skill_04_voucher.py              # 会计凭证 + 增值税拆分
  skill_05_payment.py              # 五重预校验 + 付款模拟
  skill_fraud_check.py             # 欺诈检查 Skill
config/
  policy.yaml                      # 费用标准、城市分级、员工等级限额
  approval_flow.yaml               # 审批矩阵、超时升级机制
  expense_types.yaml               # 费用类型、会计科目、增值税配置
  workflow.yaml                    # 管道编排（启用/禁用/失败策略）
  city_mapping.yaml                # 城市名标准化映射
  fx_rates.yaml                    # 外币汇率
frontend/
  employee/                        # 员工端：提交、草稿、报销单、历史
  manager/                         # 经理端：审批队列
  finance/                         # 财务端：复核、导出
  admin/                           # 管理端：政策、员工、审计日志
  eval/                            # Eval 仪表盘
  shared/                          # 公共 JS/CSS、API 封装、认证
models/                            # 数据模型（Pydantic）
rules/                             # 策略引擎 + 城市标准化
mock_data/                         # 7 个测试场景工厂函数
scripts/seed_demo_data.py          # 演示数据种子脚本
Dockerfile / docker-compose.yml    # 容器化部署
requirements.txt                   # Python 依赖
```

---

## 快速启动

```bash
# 1. 安装依赖
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. （可选）植入演示数据
python scripts/seed_demo_data.py

# 3. 启动服务（默认 MockLLM 模式，全本地，无需 API Key）
uvicorn backend.main:app --reload --port 8000
```

**访问入口**（mock 模式下可通过导航栏下拉框或 URL 参数 `?as=<role>` 切换角色）：

| 角色 | 链接 |
|------|------|
| 员工 | `http://localhost:8000/employee/quick.html` |
| 经理 | `http://localhost:8000/manager/queue.html` |
| 财务 | `http://localhost:8000/finance/review.html` |
| 管理员 | `http://localhost:8000/admin/dashboard.html` |
| Eval Observatory | `http://localhost:8000/eval/dashboard.html` |
| OpenAPI 文档 | `http://localhost:8000/docs` |

**5 分钟端到端演练：**

| 步骤 | 角色 | 操作 |
|------|------|------|
| 1 | employee | `quick.html` → 上传任意发票 → AI 识别字段 → 确认提交 |
| 2 | — | AI 后台审核（1–3 秒）；`my-reports.html` 轮询到 `status=reviewed` |
| 3 | manager | `queue.html` → 点开报销单 → 查看 AI 解释卡 → 点通过 |
| 4 | finance_admin | `review.html` → 通过后生成凭证号 |
| 5 | finance_admin | `export.html` → 批量导出 CSV |

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AUTH_MODE` | `mock` | `mock`（开发）/ `clerk`（生产）|
| `DATABASE_URL` | SQLite 本地文件 | 生产用 `postgresql+asyncpg://...` |
| `EVAL_DATABASE_URL` | SQLite `concurshield_eval.db` | 与业务库物理隔离，存 LLM traces 和 eval runs，trace 增长不影响主库性能 |
| `STORAGE_BACKEND` | `local` | `local` / `r2`（Cloudflare R2）|
| `ANTHROPIC_API_KEY` | -- | 可选：AmbiguityDetector 深度分析（score >50 时触发） |
| `OPENAI_API_KEY` | -- | 可选：对话 Agent 使用 GPT-4o |
| `OPENAI_MODEL` | `gpt-4o` | 配置 `OPENAI_API_KEY` 时可覆盖默认模型 |
| `AGENT_USE_REAL_LLM` | -- | 设为 `1` + 提供 API Key -> 切换 RealLLM |

### MockLLM vs RealLLM

| 模式 | 条件 | 行为 |
|------|------|------|
| **MockLLM**（默认）| 无需 API Key | 确定性状态机：happy path 线性跑完，keyword routing，deterministic eval |
| **RealLLM（GPT-4o）** | `OPENAI_API_KEY` + `AGENT_USE_REAL_LLM=1` | GPT-4o 真实推理，解锁"用户改字段"Agent 行为 |

---

## 我们故意不做的 5 件事

| # | 没做的事 | 为什么 |
|---|---------|--------|
| 1 | **5-Skill 管道改成 Agent** | 合规要求确定性可审计，把法律责任从规则转移到 LLM 是不可接受的 |
| 2 | **Agent 拥有 submit / approve 工具** | 白名单是防注入最后一道闸，破开等于拆防线 |
| 3 | **审批页加 chat drawer** | 经理一天审 30 单，多 30 秒/单 = 一天多 15 分钟，经理会直接关 AI |
| 4 | **AI 自动提交报销** | 法律责任在员工，Submit 必须由人 confirm（SAP Concur Joule 2026 同样决策） |
| 5 | **一上来就接 Agent SDK** | MVP 原生 tool calling 够用；SDK 价值在 subagents/memory，当前用不上 |

---

## 技术栈

- **Backend**: FastAPI, SQLAlchemy (async), aiosqlite/asyncpg
- **AI**: Claude API (AmbiguityDetector), OpenAI GPT-4o (Chat Agent), MockLLM (默认)
- **Frontend**: 原生 HTML/JS（无框架依赖）
- **Database**: SQLite (开发) / PostgreSQL (生产)
- **Config**: YAML 配置驱动（policy, workflow, approval, expense types, city mapping）
