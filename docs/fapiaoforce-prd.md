# Fapiaoforce — 报销产品 PRD

> **Status:** Product spec v0.1 · Scope-cut, NOT a build plan
> **Audience:** PM / 工程 leads / 客户实施顾问
> **Companion to:** [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md)（已建 AI 核心）, [`industrial-readiness-roadmap.md`](industrial-readiness-roadmap.md)（生产化 roadmap）, [`customer-segmentation.md`](customer-segmentation.md)（卖给谁）, [`multi-entity-design.md`](multi-entity-design.md)（多实体）

---

## TL;DR

Fapiaoforce = **Concur-style 多集团报销产品 · 中国本地化版本**。本 PRD 在现有 ExpenseFlow 基础上扩展三件事：

1. **Group / Policy / Form 三层架构**（Concur 标准）—— 每个员工通过 `User → Group → Policy → Form` 链路自动加载属于自己的报销表单和合规规则
2. **三层组织主数据级联**（客户 / 公司 / 部门）—— Fapiaoforce 特有设计，比 Concur 多一层"客户"前缀，支持 SaaS 多租户场景
3. **完整 Workflow 引擎**（N 级审批 + 委托 + 加签 + 例外审批）—— 取代现有单层 manager → finance 流程

现有 ExpenseFlow 的 AI 核心（OODA fraud investigator / cite-the-rule / eval κ 框架）**不重写**，作为 Fapiaoforce 的「合规前置检查」和「Exception Approval」环节嵌入新的 Workflow 引擎。

**范围**：本 PRD **包含设计**，**不包含实施计划**。实施按需切分（详见 §10 范围切割）。

---

## 1. 跟现有 ExpenseFlow 的关系

### 1.1 ExpenseFlow 现状（已建）

```
employee submit
    ↓
5-Skill compliance pipeline   ← workflow（确定性）
    ↓
AmbiguityDetector + 20 fraud rules   ← Layer 1 cite-the-rule
    ↓
risk_score >= 80? → OODA fraud investigator   ← Layer 2 multi-round agent
    ↓
audit_report + AI 解释卡   ← manager / finance UI
    ↓
1-step approval → finance review → CSV export
```

**强项**：AI 核心扎实，eval 框架严谨，cite-the-rule 可审计。
**弱项**：单实体、单层审批、表单字段硬编码、跨集团/多政策无法配置。

### 1.2 Fapiaoforce 新增（本 PRD）

```
User → Group → Policy → Form         ← 配置驱动报销单
    ↓
合规前置检查（含现有 cite-the-rule）  ← 复用 Layer 1
    ↓
Workflow 引擎多级路由                ← 新增（取代单层审批）
    ↓
Exception Approval 节点              ← 现有 OODA fraud investigator 嵌入此处
    ↓
财务终审 → ERP 入账（按 Group 分库）  ← 复用现有 export，按 Group 隔离
```

**关键观点**：Fapiaoforce 不是 rewrite，是给 ExpenseFlow 加**配置层**和**工作流层**。AI 核心是 Fapiaoforce 的差异化（Concur 没有 Cohen's κ 校准的 LLM agent）。

### 1.3 已建 vs 待建

| Fapiaoforce 模块 | 现有 ExpenseFlow 覆盖度 | 新增 / 改造工作量 |
|---|---|---|
| Group（三层组织） | 0%（只有 cost_center 平面字段） | 2-3 周 |
| Policy（per-Group 政策引擎） | 30%（policy.yaml 单层） | 3-4 周 |
| Form（按 Policy 驱动表单字段） | 0%（HTML 硬编码） | 4-6 周 |
| Header（报销单表头）| 60%（Report 表已有） | 1 周补强 |
| Line Item（费用行） | 60%（Submission 表已有） | 1 周补强 |
| Allocation（分摊） | 0% | 2-3 周 |
| Split（分列子行） | 0% | 2 周 |
| Attendee（参与者） | 60%（SubmissionAttendee 表已有，UI 不完整） | 1-2 周 |
| Allowance（津贴） | 50%（EmployeeAllowance 表已有，用户输入 UI 没有） | 1-2 周 |
| Mileage（私车公用） | 0% | 2 周 |
| Workflow 引擎 | 10%（单层经理审批） | 4-6 周 |
| **总计 1 期** | | **22-32 周（6-8 个月）** |

---

## 2. Concur 标准架构参考

> 这一节是 Concur 官方文档的浓缩，作为 Fapiaoforce 设计的对照基线。

### 2.1 一句话总关系

- **Group** = 组织边界（谁）
- **Policy** = 规则控制（允不允许）
- **Form** = 界面呈现（填什么）

绑定链：

```
User → Group → Policy → Form
```

用户进系统 → 自动带 Group → 加载该 Group 绑定的 Policy → 展示该 Policy 对应的 Form（表单字段 / 布局）。

### 2.2 三者标准定义

**Group（组 / 集团）**
- 多集团 / 多公司代码隔离、权限边界、数据隔离
- 类型：Admin Group（权限）/ Policy Group（费用政策，核心）/ Travel Group（差旅）
- 一个员工只属于**一个** Policy Group；一个 Group 对应**一套**独立管控

**Policy（政策 / 规则）**
- 合规控制引擎：费用标准、额度、车型、住宿上限、超标审批
- 允许的费用类型、税码、CO 对象（CC / WBS / IO）
- 审批流路由、差旅前置申请控制
- **关系：1 Group → 1 Policy**（标准）；多集团必须严格隔离时强制 1:1

**Form（表单模板）**
- 用户可见的界面 + 字段规则
- 报销单表头 / 行明细字段
- 显示 / 必填 / 只读 / 枚举值
- 费用类型列表、附件规则
- **关系：1 Policy → 1 Form**（一对一）；Form 由 Policy 驱动，**不直接绑 Group**

### 2.3 标准对应（多集团推荐）

```
Group01（集团A / 公司代码1000）
  └─ 绑定 → Policy01（集团A 费用政策）
              └─ 驱动 → Form01（集团A 报销单：表头 + 行明细）

Group02（集团B / 公司代码2000）
  └─ 绑定 → Policy02（集团B 费用政策）
              └─ 驱动 → Form02（集团B 报销单：表头 + 行明细）
```

### 2.4 协同流程

1. 用户登录，员工主数据带 GroupID
2. 系统识别 Group，确定组织边界
3. 加载绑定的 Policy（费用类型、额度、规则、审批流、CO 对象范围）
4. Policy 驱动 Form 渲染（允许的字段、必填项、费用行、税码、附件要求）
5. 提交时：Form 数据 → 按 Policy 校验 → 按 Group 隔离存储 → 按 Group 推 SAP

---

## 3. Fapiaoforce 设计（区别于 Concur 的本地化）

### 3.1 Group 命名规则（Fapiaoforce 特有：三层前缀）

Concur 的 Group 是平面的；Fapiaoforce 引入**三层前缀**支持 SaaS 多租户 + 集团内多公司多部门：

| 前缀 | 含义 | 示例 |
|---|---|---|
| `G_` | 客户级（最外层 / 租户）| `G_acme` |
| `C_` | 公司级（客户下的法人实体） | `C_acme_hk`, `C_acme_cn` |
| `D_` | 部门级（公司下的部门 / 成本中心） | `D_acme_cn_eng`, `D_acme_cn_mkt` |

**示例 Group 结构**：

```
G_客户1
  ├── 政策：日常报销、办公费、团建
  │
  ├── C_客户1_公司1
  │     └── 政策：日常报销、办公费、团建
  │
  └── C_客户1_公司2
        ├── 政策：日常报销、办公费、团建
        ├── D_公司2_部门1
        │     └── 政策：仅办公费
        └── D_公司2_部门2
              └── 政策：仅日常报销
```

### 3.2 主数据级联回退（关键设计 ⭐）

员工提交时，按**部门 → 公司 → 客户**优先级查找绑定的 Group，第一个非空命中即采用：

```python
# 伪代码
def resolve_group(employee) -> Group:
    """
    主数据级联：
      employee.department.group  (D_*)  ← 第一优先
      employee.company.group     (C_*)  ← 部门未绑则降级
      employee.customer.group    (G_*)  ← 公司未绑则降级
      DEFAULT_GROUP                       ← 兜底
    """
    if employee.department:
        dept = get_department(employee.department)
        if dept and dept.group_id:
            return get_group(dept.group_id)
    if employee.company:
        comp = get_company(employee.company)
        if comp and comp.group_id:
            return get_group(comp.group_id)
    if employee.customer:
        cust = get_customer(employee.customer)
        if cust and cust.group_id:
            return get_group(cust.group_id)
    return DEFAULT_GROUP
```

**用户主数据**（强制 + 可选字段）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `customer_id` | ✅ | 客户主数据 ID |
| `company_id` | ✅ | 公司主数据 ID |
| `department_id` | ❌ | 部门主数据 ID |
| `default_policy` | ❌ | 直接指定政策（覆盖级联结果，仅高级场景）|

**主数据 Group 绑定示例**：

| 主数据 | 绑定 Group | 行为 |
|---|---|---|
| 客户「客户1」 | `G_客户1` | 兜底 |
| 公司「客户1_公司1」 | `C_客户1_公司1` | 命中，覆盖 G_ |
| 公司「客户1_公司2」 | `C_客户1_公司2` | 命中，覆盖 G_ |
| 公司「客户1_公司3」 | （留空） | 降级到客户层 G_客户1 |
| 部门「公司2_部门1」 | `D_公司2_部门1` | 命中，覆盖 C_ |
| 部门「公司2_部门2」 | `D_公司2_部门2` | 命中，覆盖 C_ |
| 部门「公司2_其他部门」 | （留空） | 降级到公司层 C_客户1_公司2 |
| 部门「公司1_所有部门」 | （留空） | 降级到公司层 C_客户1_公司1 |

**为什么这么设计**：现实中很多客户不愿意为每个部门单独配 Group；级联回退让客户**只在需要差异化的层级配置**，其余自动继承。

### 3.3 Policy 设计

参照 Concur 标准。每个 Policy 包含：

```yaml
- id: P_default_travel
  name: 标准差旅政策
  group_bindings: [G_客户1, C_客户1_公司1]   # 绑定到哪些 Group
  
  expense_types: [meal, accommodation, transport, entertainment]
  
  limits:
    meal:
      tier_1: { L1: 100, L2: 150, L3: 200, L4: 不限 }
      tier_2: { L1: 60, L2: 100, L3: 150, L4: 不限 }
    accommodation:
      tier_1: { L1: 600, L2: 800, L3: 1200, L4: 不限 }
  
  approval_routing:
    - { trigger: amount > 5000, role: director }
    - { trigger: amount > 50000, role: cfo }
  
  exception_rules:
    - "超标必须备注理由"
    - "无发票必须额外审批"
  
  cost_object_dimensions: [cost_center, wbs, internal_order]
  vat_codes: [VAT-CN-6, VAT-CN-9, VAT-CN-13]
```

### 3.4 Form 设计

**两层 Form**（Concur 标准 + Fapiaoforce 简化）：

- **表头 Form**（Header Form）：每个 Policy 对应一个表头 Form。1 个 Form 可服务多个 Policy。
- **行明细 Form**（Line Item Form）：每个**费用类型**对应 1 个行明细 Form。1 个 Form 可服务多个费用类型。

#### 3.4.1 表头字段（标准 + 可配置）

**全局核心字段**（强制存在）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `report_id` | str | ✅ | 报销单 ID |
| `group_id` | str | ✅ | 集团 ID（来自级联）|
| `employee_id` | str | ✅ | 员工 ID |
| `name` | str | ✅ | 姓名 |
| `department` | str | ✅ | 部门 |
| `report_type` | enum | ✅ | 差旅 / 日常 / 招待 / 加班 |
| `business_date` | date | ✅ | 业务日期 |
| `currency` | str | ✅ | 币种 |
| `total_amount` | decimal | ✅ | 总金额（自动汇总）|
| `total_tax` | decimal | ✅ | 总税额（自动汇总）|
| `default_cost_center` | str | ✅ | 默认成本中心 |
| `notes` | text | ❌ | 备注 |
| `attachments_count` | int | ✅ | 附件数 |
| `submission_status` | enum | ✅ | 草稿 / 已提交 / 审批中 / 已通过 / 已拒绝 |

**可选 / 高级字段**（按 Form 配置开启）：

| 字段 | 类型 | 适用场景 |
|---|---|---|
| `wbs` | str | 项目化客户 |
| `internal_order` | str | SAP IO 客户 |
| `profit_center` | str | 多利润中心客户 |
| `request_id` | str | 关联出差申请（2 期）|
| `trip_id` | str | 关联行程单（2 期）|
| `payment_method` | enum | 银行转账 / 备用金 / 公司卡 |
| `payment_account` | str | 收款账户 |

**目标**：一屏可显示 ≥ 10 个字段。

#### 3.4.2 行明细字段

**全局核心字段**（所有费用类型强制）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `line_id` | str | ✅ | 行号 |
| `expense_type` | str | ✅ | 费用类型 |
| `expense_date` | date | ✅ | 发生日期 |
| `location` | str | ✅ | 发生地点 |
| `original_amount` | decimal | ✅ | 原币金额 |
| `original_currency` | str | ✅ | 原币 |
| `exchange_rate` | decimal | ✅ | 汇率 |
| `local_amount` | decimal | ✅ | 本币金额（自动）|
| `tax_code` | str | ✅ | 税码 |
| `tax_rate` | decimal | ✅ | 税率 |
| `tax_amount` | decimal | ✅ | 税额 |
| `is_deductible` | bool | ✅ | 是否抵扣 |
| `invoice_number` | str | ❌* | 发票号（按 Policy 决定是否必填）|
| `merchant` | str | ✅ | 商户名称 |
| `cost_object_override` | str | ❌ | 覆盖表头默认成本中心 |

**按费用类型扩展**（Concur 标准）：

| 费用类型 | 扩展字段 |
|---|---|
| 交通（出租 / 滴滴）| 出发地、目的地、里程、车型、用车类型、申请单号 |
| 住宿 | 入住日、退房日、天数、酒店名称、房费类型 |
| 餐饮 / 招待 | 客户名称、事由、人数、是否抵扣 |
| 机票 / 火车 | 票号、承运人、舱位、出发到达城市 |
| 加班用车 | 加班开始 / 结束时间 |

**目标**：一屏可显示 ≥ 10 行明细。

#### 3.4.3 多集团差异化示例

| 集团 | 强制字段 / 隐藏字段 |
|---|---|
| 集团 A | 强制发票号 + 税码 + 利润中心 |
| 集团 B | 强制 WBS + 项目编码 |
| 集团 C | 强制 Vendor + 合同号（对公报销）|
| 集团 D | 隐藏内部订单，只启用 WBS |

### 3.5 模板 ID 规则

```
TPL_{GroupID}_{ReportType}

示例：
  TPL_G_客户1_TRAVEL
  TPL_C_客户1_公司2_DAILY
  TPL_D_公司2_部门1_OFFICE
```

### 3.6 主数据隔离（关键）

跨集团数据必须隔离，所有主数据带 `group_id`：

| 主数据 | 复合键 |
|---|---|
| 员工 | `employee_id + group_id` |
| 费用类型 | `expense_type + group_id` |
| 科目映射 | `gl_account + group_id + expense_type` |
| 税码 | `tax_code + group_id + country` |

---

## 4. 核心实体设计

### 4.1 Header（报销单表头）

**核心功能**：
- 新建 / 编辑 / 提交 / 撤回 / 作废 / 删除
- 按 Group + Policy 自动加载表单模板
- 自动携带员工主数据、默认成本中心、WBS
- 关联出差申请 / 加班申请 / 招待申请（**2 期**）
- 自动汇总：总金额、总税额、净支付金额
- 附件上传、审批状态展示、历史日志

**多集团控制**：
- 按 Group 显示 / 隐藏 / 必填字段
- 按 Policy 控制是否必须关联申请
- 按 Form 控制表头布局与枚举值

### 4.2 Line Item（费用行 / 行明细）

**核心功能**：
- 新增 / 复制 / 修改 / 删除行
- **从发票直接创建**（OCR → 字段自动填充 → 审核确认）
- **津贴等无需发票的费用类型**：直接追加行明细
- 按费用类型自动加载字段（交通 / 住宿 / 餐饮 / 滴滴）
- 自动计税、自动换算本币
- 支持分摊、分列、添加参与者（点击行明细后弹出）
- 按 Policy 校验额度、合规性、超标提示

**关键字段**：见 §3.4.2。

### 4.3 Allocation（费用分摊 / 1 期）

> 一行费用按金额 / 比例 / 数量分摊到多个成本对象（CC / WBS / IO / 利润中心 / 订单 / 项目）。

**核心功能**：
- 一行费用拆到多个成本对象
- 按比例 / 金额 / 数量任一方式
- 自动按比例拆分金额与税额
- **分摊后总和必须等于原行金额**
- 按集团控制允许的分摊维度

**关键字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `allocation_id` | str | 分摊行 ID |
| `line_item_id` | str | 父行 ID |
| `cost_center` | str | 成本中心 |
| `wbs` | str | WBS（可空）|
| `internal_order` | str | 内部订单（可空）|
| `profit_center` | str | 利润中心（可空）|
| `allocation_pct` | decimal | 分摊比例 |
| `allocated_amount` | decimal | 分摊金额 |
| `allocated_tax` | decimal | 分摊税额 |
| `notes` | text | 分摊备注 |

**入账规则**：分摊后每条记录 → 生成 SAP 对应行项目（BSEG）。

### 4.4 Split（分列明细 / 1 期）

> 一行原始费用拆成多条子费用（典型：一张发票含多种费用类型）。

**核心功能**：
- 拆分场景：
  - 一张发票含住宿 + 早餐 + 停车
  - 不同税率、不同科目、不同日期
- 分列后子行**共用同一张发票附件**
- **子行金额合计 = 父行金额**

**关键字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `split_id` | str | 子行 ID |
| `parent_line_id` | str | 父行 ID |
| `expense_type` | str | 子行费用类型 |
| `amount` | decimal | 子行金额 |
| `tax_code` | str | 税码 |
| `gl_account` | str | 科目 |
| `expense_date` | date | 发生日期 |
| `notes` | text | 备注 |

### 4.5 Attendee（参与者 / 1 期）

> 招待 / 商务活动的参与者记录。

**核心功能**：
- 支持添加：内部员工、外部客户、供应商、其他
- 自动统计人数 → 用于政策校验（餐标 / 招待标准）
- 记录参与者信息用于合规与审计
- 按集团控制：是否必填、最多人数、字段范围

**关键字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `attendee_id` | str | 参与者 ID |
| `line_item_id` | str | 行项目 ID |
| `attendee_type` | enum | 内部 / 客户 / 供应商 / 其他 |
| `name` | str | 姓名 |
| `organization` | str | 单位 |
| `title` | str | 职务 |
| `contact` | str | 联系方式 |
| `purpose` | str | 参与事由 |

**现有 ExpenseFlow 覆盖**：`SubmissionAttendee` 表已存在（PR #38 之前的 compliance work），数据模型基本对得上，只需补 UI。

### 4.6 Allowance（津贴维护 / 1 期）

> 出差期间的每日津贴（不需要发票）。

**核心功能**：
- 用户输入出差**起始 + 结束时间**
- 系统展示该时间区间内**所有津贴选项**给用户勾选
- 津贴分类参考表 `CDW_Fapiaoforce_Expense_Allowance.xlsx`

**典型津贴分类**：
- 早餐津贴（按出差天数）
- 午餐 / 晚餐津贴
- 交通津贴
- 通讯津贴

**现有 ExpenseFlow 覆盖**：`EmployeeAllowance` 表已存在，但用法是「员工常驻津贴 vs 报销互斥检查」（compliance reasoner 用）；本模块的「按日勾选津贴」是**不同的功能**，需新建数据表 `report_allowance_lines`。

### 4.7 Mileage（私车公用 / 2 期）

> 员工用私车出差，按里程报销。

**核心功能**：
- 用户输入里程数
- 按维护的「金额 / 公里」自动计算
- **不同车辆**（按职级、部门）每公里金额可不同
- 用户使用前需先**注册车辆**

**关键字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `vehicle_id` | str | 车辆 ID |
| `line_item_id` | str | 行项目 ID |
| `mileage_km` | decimal | 里程数 |
| `rate_per_km` | decimal | 每公里金额 |
| `calculated_amount` | decimal | 自动计算金额 |

---

## 5. Workflow 引擎设计

### 5.1 定位

> Workflow 是 Policy 的**执行引擎**。Policy 定义"什么需要审批 / 谁来审批"，Workflow 负责按规则**自动找人 + 流转**。

### 5.2 驱动关系

```
User → Group → Policy → Workflow → Form / Header / Line
```

- **Group**：组织边界、公司代码、数据隔离
- **Policy**：额度、合规、是否必须申请、是否超标审批
- **Workflow**：按 Policy 规则**自动路由 + 找人 + 流转**
- **Form**：提交时可见 / 必填字段，影响审批触发条件

### 5.3 核心审批对象

- **Expense Report**（主审批对象）：Header + Line + Allocation + Split + Attendee
- **Request**（申请单）：出差 / 加班 / 招待 / 滴滴用车前置审批 — **2 期**
- **Cash Advance**（借款）：预支借款、冲销借款审批 — **2 期**
- **Trip**（行程单）：差旅行程整体审批 — **2 期**

### 5.4 标准状态流转

```
Draft → Submitted → Pending Approval → Approved → Processed for Payment
                          ↓
                     Rejected / Sent Back / Recalled
```

### 5.5 审批节点类型（Concur 标准 6 种）

| 节点 | 触发逻辑 |
|---|---|
| **Manager Approval** | 直属上级，按组织架构自动带出 |
| **Limit Approval** | 按审批人授权金额上限控制；只能审 ≤ 自己额度的单据 |
| **Cost Object Approval** | CC / WBS / IO / 项目负责人审批 |
| **Exception Approval** | 超政策、超标准、无发票、超岗级时触发 |
| **Financial Approval** | 发票合规、税金、借款冲销、入账前终审 |
| **Delegated Approval** | 审批人委托他人代审，继承权限与额度 |

### 5.6 自动路由规则（智能找人）

| 维度 | 规则 |
|---|---|
| 组织架构 | 直属经理 → 部门经理 → 财务 |
| 金额 | ≤ 5K 经理；5K-2W 总监；> 2W VP / 财务 |
| 成本对象 | CC 负责人、WBS / 项目负责人、IO 负责人 |
| 费用类型 | 招待费 → 行政 + 财务；交通费 → 部门 + 财务 |
| 异常 / 超标 | 超标必须额外审批；超政策必须备注理由 |
| 集团 Group | 不同公司代码走不同财务岗 |

### 5.7 审批动作

| 动作 | 行为 |
|---|---|
| **Approve** | 通过，进入下一节点 |
| **Reject** | 拒绝，单据终止，可退回修改 |
| **Send Back** | 退回修改，允许重新提交 |
| **Recall** | 申请人撤回，回到草稿 |
| **Delegate** | 委托 / 转审（有时限 / 有权限）|
| **Add Ad Hoc Approver** | 加签（临时加审批人）|

### 5.8 多级与并行 / 汇签

- **串行**：按顺序逐级审批（最常用）
- **并行 / 汇签**：多人必须全部通过才流转
- **或签**：一人通过即通过
- **终审节点**：满足条件直接结束

### 5.9 合规前置检查（提交即校验 ⭐ 现有 cite-the-rule 嵌入点）

提交时立即跑：

| 检查 | 失败行为 |
|---|---|
| 是否关联 Request / 出差申请（Policy 控制）| 阻止提交 |
| 费用是否超标、超岗级、超车型 | 触发 Exception Approval 节点 |
| 税金是否正确、是否可抵扣 | 高亮警告 |
| 是否有未冲销借款 | 阻止提交 |
| 附件是否齐全、发票是否合规 | 阻止提交 |
| 成本中心 / WBS 是否有效、是否跨集团 | 阻止提交 |

> **整合点**：现有 ExpenseFlow 的 `agent/violation_registry.py` + cite-the-rule 体系**直接复用**，只需把 `audit_report.violations` 暴露给 Workflow 引擎当作合规前置检查的输入。

### 5.10 委托与代理

- 临时 / 定期委托
- 委托继承审批额度与例外权限
- 可限制只能委托给同级 / 更高级别
- 日志留痕，满足审计

### 5.11 消息与通知

- 提交 / 通过 / 拒绝 / 退回 / 催办
- 邮件、站内信、App 推送
- 审批待办、逾期提醒

### 5.12 审计与日志

- 每步操作留痕：谁、何时、何动作、何意见
- 路由变更、加签、转审全记录
- 支持导出审计报表

### 5.13 标准流程示例

```
1. 员工填报销单
2. 提交 → 合规前置检查（现有 cite-the-rule）
3. 自动路由到直属经理
4. 经理审批
5. 按金额到总监 / VP（Limit Approval）
6. 成本对象负责人审批（如 WBS 项目负责人）
7. Exception Approval（如超标）
   ↓
   触发现有 OODA fraud investigator 调查（4 轮 LLM 决策）
   → verdict: clean / suspicious / fraud
   → 财务参考此 verdict 决定是否驳回
8. 财务终审（发票 / 税金 / 借款）
9. 审批完成 → 自动推 SAP 入账 / 付款（按 Group 推不同公司代码）
```

---

## 6. 整合策略：现有 ExpenseFlow 怎么嵌入 Fapiaoforce

### 6.1 现有 AI 资产 → Fapiaoforce 节点映射

| 现有 ExpenseFlow 模块 | Fapiaoforce 中的位置 |
|---|---|
| `agent.violation_registry` + cite-the-rule | **Workflow §5.9 合规前置检查**（提交即校验阶段）|
| `AmbiguityDetector` 5 因子评分 | **Exception Approval 触发条件**（高 ambiguity_score → 走例外审批）|
| `agent.compliance_reasoner` 跨表 reasoning | **合规前置检查**的 cross-record 部分（出差期间报销、津贴双吃等）|
| `agent.fraud_investigator` OODA agent | **Exception Approval 节点的 AI 决策辅助**（给财务看一份调查报告，不替代人决策）|
| Eval κ 框架 + dashboard | **Per-Group 政策行为校验** —— 每个 Group 跑一次 κ，看 AI 在该集团数据上是否还可信 |
| Auto-Approval Funnel KPI | **Per-Group 漏斗** —— 每集团独立的 T1+T2 自动批准率，CFO 看的报表 |

### 6.2 数据隔离调整

现有 `LLMTrace` / `EvalRun` / `audit_report` 都需要加 `group_id`，按 Group 分库或 row-level isolation。

### 6.3 配置文件迁移

```
现有：
  config/policy.yaml        ← 单一全局政策
  config/expense_types.yaml ← 单一类型清单
  config/approval_flow.yaml ← 单一审批矩阵

迁移后：
  config/groups.yaml                  ← NEW（三层 Group 定义）
  config/policies/{policy_id}.yaml    ← 每个政策一个文件
  config/forms/header/{form_id}.yaml  ← 表头 Form
  config/forms/line/{form_id}.yaml    ← 行明细 Form
  config/group_policy_bindings.yaml   ← Group ↔ Policy ↔ Form 映射
```

### 6.4 命名 / 品牌策略

- **代码 / 仓库名**：保持 `ExpenseFlow`（不重命名仓库）
- **产品对外名**：Fapiaoforce（中文市场） vs ExpenseFlow（英文 / 国际）
- **README**：双品牌定位，README.md = 国际版，README_CN.md = 中国版

---

## 7. 现有 ExpenseFlow UI 升级清单

| 页面 | 现状 | Fapiaoforce 升级 |
|---|---|---|
| `/employee/quick.html` | 单笔上传 + OCR | 加：从发票直接创建多行明细 |
| `/employee/report.html` | 单层报销单 | 加：分摊按钮、分列按钮、参与者按钮、津贴勾选 UI |
| `/employee/my-reports.html` | 报销单列表 | 加：按 Group 筛选、模板切换 |
| `/manager/queue.html` | 单层审批队列 | 重构：N 级审批待办、加签、委托 |
| `/finance/review.html` | 财务复核 | 重构：Exception 队列（OODA agent verdict 在此） |
| `/admin/policy.html` | 全局政策编辑 | 重构：Group / Policy / Form 三层管理 |
| `/admin/employees.html` | 员工档案 | 加：客户 / 公司 / 部门主数据级联 |
| `/admin/groups.html`（NEW）| — | 新建 Group 管理页 |
| `/admin/forms.html`（NEW）| — | 新建 Form 编辑器（表头 + 行明细字段配置） |

---

## 8. 数据库 Schema 增量

新建表（按 1 期范围）：

```python
class Customer(Base):
    """客户主数据 - 最外层租户"""
    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    group_id = Column(String(64), ForeignKey("groups.id"), nullable=True)

class Company(Base):
    """公司主数据 - 客户下的法人实体"""
    id = Column(String(64), primary_key=True)
    customer_id = Column(String(64), ForeignKey("customers.id"))
    name = Column(String(255), nullable=False)
    group_id = Column(String(64), ForeignKey("groups.id"), nullable=True)

class Department(Base):
    """部门主数据"""
    id = Column(String(64), primary_key=True)
    company_id = Column(String(64), ForeignKey("companies.id"))
    name = Column(String(255), nullable=False)
    group_id = Column(String(64), ForeignKey("groups.id"), nullable=True)

class Group(Base):
    """组（Concur Policy Group）"""
    id = Column(String(64), primary_key=True)        # G_xxx / C_xxx / D_xxx
    type = Column(String(20), nullable=False)        # customer / company / department
    name = Column(String(255), nullable=False)
    parent_id = Column(String(64), ForeignKey("groups.id"), nullable=True)

class Policy(Base):
    """费用政策"""
    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    content = Column(JSON, nullable=False)           # 整个 policy yaml 内容

class GroupPolicyBinding(Base):
    """Group ↔ Policy ↔ Form 三角绑定"""
    id = Column(String(36), primary_key=True)
    group_id = Column(String(64), ForeignKey("groups.id"))
    policy_id = Column(String(64), ForeignKey("policies.id"))
    header_form_id = Column(String(64), nullable=True)
    # 一个 Group 只能绑一个 Policy（标准）

class FormDefinition(Base):
    """表单字段定义（Header 或 Line）"""
    id = Column(String(64), primary_key=True)
    form_type = Column(String(20), nullable=False)   # header / line
    expense_type = Column(String(64), nullable=True) # 仅 line form
    fields = Column(JSON, nullable=False)            # 字段配置数组

class SubmissionAllocation(Base):
    """费用分摊"""
    id = Column(String(36), primary_key=True)
    line_item_id = Column(String(36), ForeignKey("submissions.id"))
    cost_center = Column(String(64), nullable=True)
    wbs = Column(String(64), nullable=True)
    internal_order = Column(String(64), nullable=True)
    profit_center = Column(String(64), nullable=True)
    allocation_pct = Column(Numeric(5, 4), nullable=False)
    allocated_amount = Column(Numeric(12, 2), nullable=False)
    allocated_tax = Column(Numeric(12, 2), nullable=True)
    notes = Column(Text, nullable=True)

class SubmissionSplit(Base):
    """分列子行"""
    id = Column(String(36), primary_key=True)
    parent_line_id = Column(String(36), ForeignKey("submissions.id"))
    expense_type = Column(String(64), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    tax_code = Column(String(32), nullable=True)
    gl_account = Column(String(64), nullable=True)
    expense_date = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)

class ReportAllowanceLine(Base):
    """报销单上勾选的每日津贴"""
    id = Column(String(36), primary_key=True)
    report_id = Column(String(36), ForeignKey("reports.id"))
    allowance_kind = Column(String(64), nullable=False)
    date = Column(Date, nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)

class WorkflowInstance(Base):
    """单据的审批流实例"""
    id = Column(String(36), primary_key=True)
    report_id = Column(String(36), ForeignKey("reports.id"))
    current_node_id = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False)
    # draft / pending / approved / rejected / sent_back / recalled

class WorkflowApproval(Base):
    """每一步审批动作"""
    id = Column(String(36), primary_key=True)
    workflow_instance_id = Column(String(36), ForeignKey("workflow_instances.id"))
    node_type = Column(String(32), nullable=False)
    # manager / limit / cost_object / exception / financial / delegated
    approver_id = Column(String(64), nullable=False)
    decided_at = Column(DateTime, nullable=True)
    decision = Column(String(32), nullable=True)
    # approve / reject / send_back / delegate
    comment = Column(Text, nullable=True)

class Delegation(Base):
    """审批委托"""
    id = Column(String(36), primary_key=True)
    delegator_id = Column(String(64), nullable=False)
    delegate_id = Column(String(64), nullable=False)
    valid_from = Column(Date, nullable=False)
    valid_to = Column(Date, nullable=False)
    reason = Column(Text, nullable=True)
```

**现有表的字段增量**：

```python
# Submission（已有），加：
group_id = Column(String(64), ForeignKey("groups.id"), nullable=True, index=True)
header_form_id = Column(String(64), nullable=True)
line_form_id = Column(String(64), nullable=True)

# Employee（已有），加：
customer_id = Column(String(64), ForeignKey("customers.id"), nullable=True)
company_id = Column(String(64), ForeignKey("companies.id"), nullable=True)
department_id = Column(String(64), ForeignKey("departments.id"), nullable=True)
default_policy_id = Column(String(64), ForeignKey("policies.id"), nullable=True)
```

---

## 9. 范围切割（关键 ⭐）

### 9.1 一期（核心，6-8 个月）

**必须做**：
- ✅ 三层 Group + Policy + Form 配置
- ✅ 主数据级联回退（Customer → Company → Department）
- ✅ Header / Line（基于现有改造）
- ✅ Allocation（分摊）
- ✅ Split（分列）
- ✅ Attendee（参与者）
- ✅ Allowance（按日勾选）
- ✅ Workflow 引擎（Manager + Limit + Cost Object + Exception + Financial 5 类节点）
- ✅ 委托 / 加签
- ✅ 现有 cite-the-rule + OODA agent 嵌入到 Workflow

### 9.2 二期（增量，3-4 个月）

- Mileage（私车公用）
- Request（差旅前置申请）
- Cash Advance（借款 / 冲销）
- Trip（行程单整体审批）
- Travel Group（差旅政策独立于 Policy Group）

### 9.3 永远不做（明确划掉）

- **Concur Travel 整套**（机酒预订 / TMC 集成）—— 用 Airwallex Travel / 携程商旅 接 API 即可
- **Concur Drive 完整实现**（GPS 自动里程）—— 用户手填里程足够
- **多语言除 zh / en 外**（西语、德语、日语等）—— 不在中国 + 美国主战场
- **Concur Detect 整套**（深度 AI 反欺诈）—— **现有 OODA fraud investigator 已经覆盖核心场景**，不再重做

---

## 10. 实施时不做这些事（设计纪律）

> 跟其他设计文档（multi-entity-design.md, industrial-readiness-roadmap.md）一样，本 PRD 明确划清"什么时候**不**实施"。

| 坑 | 为什么不踩 |
|---|---|
| 一开始就实装 Workflow 引擎全部 6 节点类型 | 4-6 周工作量，先做 Manager + Financial 2 种够 80% 客户 |
| Form-builder UI 做成 no-code 拖拽 | 10x 工作量；前 5 个客户用 YAML 配置就行 |
| 多 Group 数据库分库 | 用 `group_id` row-level isolation 即可，不用搞 schema-per-tenant |
| ERP 实时双向同步 | Excel-as-bridge 80% 客户够用（见 [`integration-design.md`](integration-design.md)）|
| 重写现有 OODA fraud investigator | 它已经是项目的 AI 差异化亮点；嵌入 Workflow 而不是重写 |
| 完整 Cash Advance 借款流 | 中国客户大部分不用借款；2 期再说 |

---

## 11. Open Questions

1. **Group 是否支持矩阵组织？**（一个员工同时属于「研发部」+「项目 X」两个 Group？）—— Concur 标准是不支持（一员工一 Policy Group），但矩阵化客户会要。设计时是否预留？
2. **Form 配置变更如何应对历史单据？**（中途改了字段，旧单据怎么办？）—— Concur 用「单据 snapshot Form 版本号」，提交时锁定。
3. **Policy 多版本切换**（每年 1 月调整限额）需不需要内置？—— 推荐用 `effective_from / effective_to` 字段做时间版本。
4. **跨 Group 调动员工**（HK 调到 CN 出差）的报销算谁的 Group？—— Concur 用 `transfer_group_id` 字段临时切换；本 PRD 暂不解决。
5. **Workflow 引擎是否用现成 BPMN（如 Camunda / Flowable）？** 推荐**不用**——BPMN 学习曲线陡，简单审批流自己写 ~600 行就够。
6. **Allowance 怎么跟 expense_types 协同**（津贴算不算一种 expense_type？）—— 推荐独立模型，不混入 expense_types。

---

## 12. 实施 / 范围决策点（PM 决定）

| 决策 | 选项 | 推荐 |
|---|---|---|
| 一期是否包含 Workflow 引擎全 5 类节点？ | A. 全做 / B. 只做 Manager + Financial 2 类 | **B**（4 周做完 vs 8 周）|
| Form 配置走 YAML 还是 DB？ | A. YAML / B. DB | **A**（git diff 可审计）|
| 多集团数据隔离用什么？ | A. row-level / B. schema-per-tenant / C. DB-per-tenant | **A**（最便宜；前 10 个客户够用）|
| 现有 ExpenseFlow 仓库是否重命名？ | A. 改名 Fapiaoforce / B. 保留 ExpenseFlow | **B**（双品牌：英文 ExpenseFlow，中文 Fapiaoforce）|
| 现有 OODA fraud investigator 如何整合？ | A. 重写嵌入 / B. 当 Exception Approval 节点的辅助决策 | **B**（不重写，加挂在 Exception 节点）|

---

## 13. References

- [Concur 官方文档 — Group / Policy / Form 标准](https://www.concur.com/expense)
- [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md) —— 现有 AI 核心
- [`evals-reference.md`](evals-reference.md) —— Eval 框架
- [`multi-entity-design.md`](multi-entity-design.md) —— 多实体设计（跟 Group 概念有重叠）
- [`industrial-readiness-roadmap.md`](industrial-readiness-roadmap.md) —— 工业化 8 个 gap
- [`customer-segmentation.md`](customer-segmentation.md) —— Fapiaoforce 主要服务 Segment B（5000 人中国制造业）
- [`integration-design.md`](integration-design.md) —— ERP / 支付集成

---

## 附录 A · Concur 配置画面参考（截图位置）

> 以下截图说明字段映射，实际产品 UI 可参考 Concur 但**不抄袭**。

[Form 配置画面截图]
[报销单创建画面截图]
[审批流配置画面截图]

---

## 附录 B · 跟现有项目对照表（开发参考）

| 现有 Python 模块 | Fapiaoforce 中的角色 |
|---|---|
| `backend/db/store.py::Submission` | Line Item（行明细）|
| `backend/db/store.py::Report` | Header（报销单表头）|
| `backend/db/store.py::SubmissionAttendee` | Attendee（已对得上）|
| `backend/db/store.py::EmployeeAllowance` | 员工常驻津贴档案（**不是** Allowance 行勾选） |
| `agent/ambiguity_detector.py` | 合规前置检查的 ambiguity 部分 |
| `agent/violation_registry.py` | cite-the-rule 注册表 |
| `agent/compliance_reasoner.py` | 合规前置检查的 cross-record 部分 |
| `agent/fraud_investigator.py` | Exception Approval 节点的 AI 辅助 |
| `backend/api/routes/eval.py` | Eval Observatory（per-Group 维度需要扩展）|
| `config/policy.yaml` | 第一个 Policy 模板（迁移到 `config/policies/`）|

---

*本 PRD 故意区别于"实施计划"——它是产品设计的**契约**，不是排期表。当团队决定要做 Fapiaoforce 时，按此 PRD 执行；不做时，作为"我们想清楚了什么"的 portfolio 证据。*
