# ExpenseFlow Voice Web Agent — Design Spec

**Date:** 2026-05-11
**Status:** Draft
**Owner:** Platform / Agent team
**Related:** `docs/fapiaoforce-prd.md`, `docs/superpowers/specs/2026-04-18-eval-platform-design.md`

## 1. Problem

ExpenseFlow 现有员工 H5（`frontend/employee/*.html`）、经理审批、财务复核三套网页前端，对标微信生态内 Fapiaoforce 小程序 + Concur 中国版的票据识别与抵扣增强。目前所有操作都需要用户**手动点击/输入**：选发票、填金额、填用途、选审批人、提交。

我们希望加入一个 **"口述即操作"** 的 agent：

- 用户对着麦克风说："新建一张报销，加一张昨天的滴滴 86 块，用途客户拜访，提交给李经理"
- agent 自动在网页上完成同一序列的点击与输入，结果对用户可见、可中断、可回滚。

目标场景拆为两类：

| 场景 | 形态 | 典型用户 |
|------|------|----------|
| 员工日常报销 | H5 内嵌助手（高频、低门槛） | 出差员工 |
| 跨系统代跑 / Concur 对接 / 演示 | 后台 Playwright 浏览器代跑 | 财务 power user、销售演示、批处理 |

本文档采用 **方案 B（后台 Playwright + Claude tool-use）** 作为首发实现，并保留 A 的接入口（同一份 action 注册表两端复用）。

## 2. 方案 A vs B 对比

### 方案 A — H5 内嵌语音助手

在 `frontend/shared/` 加一个语音侧边栏组件，注入到每个页面。ASR 转文字 → 后端意图解析 → 通过页内 JS（`window.postMessage` 或直接 dispatchEvent）派发 click/input。

**优点**
- 与现有 H5 / 小程序 webview 无缝集成，**单用户级**，无登录态隔离问题
- 时延低（无远程浏览器、无截图回传）
- 用户能**亲眼**看到 agent 操作，可随时手动接管
- 不消耗服务端浏览器实例，规模化成本低
- 失败回退优雅：让用户自己继续点

**缺点**
- 只能操作**当前页**和站内 SPA 路由，跨域跨系统（如 Concur 真站）无法操作
- 必须为关键控件加 `data-action` / 语义标注，前端侵入性较强
- 浏览器环境差异大（小程序 webview 与 Safari/Chrome 行为不一致）
- 无视觉理解：DOM 选择器失效时会硬错

### 方案 B — 后台 Playwright + Claude 代跑

服务端启 headless/headed Chromium，挂载用户登录态，Claude 拿到工具集（goto, click, fill, screenshot, query_action）后驱动浏览器；前端只显示进度日志与可选直播流。

**优点**
- 可跨任意网页（ExpenseFlow、Concur、税局），与第三方系统对接的唯一路径
- 视觉 + DOM 双模：可以 fallback 到截图理解，控件没注解也能跑
- 易做**录制回放**与 eval（每一步都是 JSON 化工具调用，天然可入 `llm_traces`）
- 服务端可统一鉴权、注入凭据、做安全围栏

**缺点**
- **登录态/会话隔离**：每个用户要么共享凭据（合规风险）、要么远程账户托管（成本+风控）
- **成本与时延**：单次任务几秒到几十秒，调用 Claude tool-use 多轮 + 截图 token 费用显著
- 出错时用户**不在现场**，回滚/审计责任在系统侧
- 并发扩缩容复杂，1000 个并发等于 1000 个浏览器
- 不适合做"每个员工随手报一张票"这种高频低价值任务

### 决策

- **首发**：方案 B，瞄准两个场景 — ① Concur 中国版的批量代填演示；② 财务批量复核辅助（"把所有金额 > 5000 的报销退回")
- **同一份 Action Registry 两端复用**：员工 H5 助手（A）后续可直接拿这份注册表作为前端 JS 派发表，避免重复设计
- **不在 MVP**：真正用 Whisper/讯飞做语音；登录态托管；高并发浏览器池

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Web UI (新页面 frontend/employee/voice.html)                          │
│   ─ 麦克风按钮 + 文本框（ASR 桩：文本即"已转写"）                       │
│   ─ 任务日志流（SSE）                                                  │
│   ─ 可选：浏览器直播 iframe（CDP screencast / 静态截图轮询）            │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ POST /agent/voice/sessions
                                 │ GET  /agent/voice/sessions/:id/events (SSE)
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Backend: backend/api/voice_agent.py                                  │
│   ─ ASR Adapter（接口 + stub 实现）                                    │
│   ─ Session Manager（每会话一个 Playwright Context）                   │
│   ─ Agent Loop（Claude tool-use，每轮决定下一动作）                    │
│   ─ Tool Executor → Playwright Driver                                 │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
┌──────────────┐         ┌──────────────────┐      ┌──────────────────┐
│ ASR Adapter  │         │ Action Registry  │      │ Playwright Pool  │
│  (stub now)  │         │ config/actions/  │      │ chromium ctx/usr │
└──────────────┘         └──────────────────┘      └──────────────────┘
                                 │
                                 ▼
                         ┌──────────────────┐
                         │ LLM Trace Store  │ ← 复用 eval-platform 的 llm_traces
                         └──────────────────┘
```

## 4. 组件设计

### 4.1 ASR Adapter（桩接口）

```python
# backend/services/asr.py
class ASRAdapter(Protocol):
    async def transcribe(self, audio: bytes, *, lang: str = "zh-CN") -> ASRResult: ...

@dataclass
class ASRResult:
    text: str
    confidence: float
    duration_ms: int
    provider: str

class StubASR:
    """MVP：前端直接把文本框内容当作'转写结果'传上来，桩原样返回。"""
    async def transcribe(self, audio: bytes, **_) -> ASRResult:
        return ASRResult(text=audio.decode("utf-8"), confidence=1.0, duration_ms=0, provider="stub")
```

切换厂商时只换 adapter 实现，上游不动。候选：
- 阿里云一句话识别（中文票据术语好）
- 讯飞 RTASR（流式）
- OpenAI Whisper API / 自托管 whisper.cpp（离线、合规友好）

### 4.2 Action Registry（核心）

每个网页可执行动作显式注册一份 schema，**Claude 的工具列表 = 当前页面注册的 actions + 通用导航工具**。

```yaml
# config/voice_actions/employee.submit.yaml
page: employee.submit
url_pattern: "/employee/submit.html*"
description: "员工新建/编辑报销单页面"

actions:
  - name: select_receipt
    description: 从待报销发票列表勾选一张/多张
    params:
      merchant_keyword: {type: string, description: "商户名关键字，模糊匹配"}
      amount:           {type: number, optional: true}
      date:             {type: string, optional: true, format: yyyy-mm-dd}
    selector:
      list: "[data-receipt-row]"
      checkbox: "input[type=checkbox]"
      fields:
        merchant: "[data-field=merchant]"
        amount:   "[data-field=amount]"
        date:     "[data-field=date]"

  - name: set_purpose
    description: 设置报销用途
    params:
      purpose: {type: string}
    selector: "textarea[name=purpose]"

  - name: choose_approver
    description: 选择审批人
    params:
      name_keyword: {type: string}
    selector:
      trigger: "[data-action=open-approver]"
      option:  "[data-approver-name*='{name_keyword}']"

  - name: submit_report
    description: 提交报销单
    selector: "[data-action=submit-report]"
    confirm: true   # 需要 agent 在调用前显式确认
```

注册表的两个用途：
1. **生成 Claude tool schema**：启动 agent 前根据当前 URL 选出匹配的 actions，转成 Anthropic SDK 的 `tools=[...]`
2. **执行映射**：tool 调用回来后由 `PlaywrightDriver` 翻译成具体 `page.click()/fill()`

**前端侧需要做的事**（侵入性最小化）：把现有按钮/控件加上 `data-action`、`data-field`、`data-receipt-row` 等属性。这部分在本 spec 之外，单独 plan。

### 4.3 Agent Loop（Claude tool-use）

```python
# backend/services/voice_agent_loop.py
async def run_session(session: VoiceSession, utterance: str) -> AsyncIterator[Event]:
    page = await session.playwright.current_page()
    actions = registry.for_url(page.url) + GLOBAL_NAV_ACTIONS

    messages = [{"role": "user", "content": utterance}]
    system = build_system_prompt(page.url, page.title)

    while True:
        resp = await anthropic.messages.create(
            model="claude-opus-4-7",
            system=system,
            tools=[a.to_tool_schema() for a in actions],
            messages=messages,
            max_tokens=1024,
            # 缓存 system + tools，减少多轮成本
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        yield Event.thinking(resp)

        if resp.stop_reason == "end_turn":
            yield Event.done(resp.content)
            return

        for block in resp.content:
            if block.type == "tool_use":
                result = await execute_tool(page, block.name, block.input)
                yield Event.tool_call(block.name, block.input, result)
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({
                    "role": "user",
                    "content": [{"type":"tool_result","tool_use_id":block.id,"content":result.repr}]
                })
                if result.page_changed:
                    actions = registry.for_url(page.url) + GLOBAL_NAV_ACTIONS
                break
```

要点：
- **每次 URL 变化时刷新 tool 列表**（重要：Claude 才知道这页能干啥）
- 失败的 tool 调用要把错误回传，让 Claude 自愈（找不到选择器 → 让 Claude 改关键字 / 截图重看）
- `confirm: true` 的动作要先生成 `Event.confirm_required`，等用户/策略放行
- **强制 system prompt 包含**："非用户明确要求的金额修改、提交、退回、删除一律先 confirm"
- 复用 `llm_traces` 表（4.18 eval spec）写入每一轮请求/响应/工具结果

### 4.4 Playwright Driver

```python
# backend/services/playwright_driver.py
class PlaywrightDriver:
    async def click(self, selector: str, *, timeout=5000): ...
    async def fill(self,  selector: str, value: str): ...
    async def select_in_list(self, list_sel, match: dict): ...
    async def screenshot(self) -> bytes: ...   # PNG，喂给 Claude 当 fallback 视觉
    async def goto(self, url: str): ...
    async def current(self) -> PageSnapshot: ...   # url + title + visible_text 摘要
```

策略：
- 默认走 DOM 选择器；连续两次找不到控件就调一次 `screenshot()` 让 Claude 看图重定位
- 每次 navigation 完成后吐一个 `PageSnapshot`（url、title、注册表 page id），驱动 agent loop 刷新工具集

### 4.5 Session Manager 与登录态

MVP 简化：
- 每个 session 在内存里持一个 Playwright `BrowserContext`
- 启动时从当前登录的员工用 backend mock-auth 注入 cookie（已有 `window.AUTH_MODE = "mock"`）
- **不做** 真凭据托管 / 共享登录池（写在 future work）

会话生命周期：
```
POST /agent/voice/sessions          → 返回 session_id，启动 context，导航到 /employee/quick.html
POST /agent/voice/sessions/:id/utter  body: {text}     → 推一轮指令到 agent loop
GET  /agent/voice/sessions/:id/events SSE              → thinking / tool_call / done / confirm_required
POST /agent/voice/sessions/:id/confirm body: {tool_use_id, approve}
DELETE /agent/voice/sessions/:id     → 关闭 context
```

## 5. 安全与边界

| 风险 | 缓解 |
|------|------|
| Agent 误提交大额报销 | 注册表标 `confirm: true` 的动作必须人工放行；金额阈值 > 5000 强制二次确认 |
| Prompt 注入（用户口述夹带恶意指令） | system prompt 钉死任务边界；禁用 `goto` 到非白名单域 |
| 凭据泄漏 | session context 与 user_id 强绑定，过期销毁；截图脱敏（手机号、身份证打码） |
| 越权（员工 agent 调到管理员页） | 注册表按角色过滤 actions；后端再做一次权限校验 |
| 死循环 / 烧 token | 单次 utterance 最多 12 轮工具调用；超时回 user 兜底 |

## 6. Eval & 观测

复用 `llm_traces` 表（4.18 spec 已建）：
- 新增 `component = "voice_agent"`
- 字段 `extra` 存 `{session_id, page_url, tool_calls: [...]}`

新增评测集 `tests/eval_cases/voice_agent.yaml`：
```yaml
- id: VA-001
  utterance: "新建报销，加昨天 86 元滴滴，用途客户拜访，提交给李四"
  setup: {url: "/employee/quick.html", fixtures: ["receipt_didi_86"]}
  expect:
    final_url: "/employee/my-reports.html"
    db_assert: "SELECT count(*) FROM expense_report WHERE submitter=:me AND total=86"
    tool_calls_min: 4
    tool_calls_max: 10
```

Grader 全部 code-based（DB 断言 + URL 断言），符合 4.18 spec 的"code grader > model grader"原则。

## 7. 交付阶段

| Phase | 范围 | 产出 |
|-------|------|------|
| 0 (本 spec) | 设计、A/B 对比、协议 | 本文件 + plan 文件 |
| 1 | ASR stub + Action Registry loader + 1 个页面（quick.html）actions | `backend/api/voice_agent.py`, `config/voice_actions/employee.quick.yaml` |
| 2 | Playwright driver + agent loop + SSE 事件流 + voice.html | 端到端跑通 "新建报销" 1 条 case |
| 3 | confirm 流程 + 多页注册表（submit/report/my-reports） | 员工流程覆盖 |
| 4 | 经理/财务页注册表 + 角色权限过滤 | 跨角色 demo |
| 5 | 接真 ASR + 浏览器直播 + Concur 真站 PoC | 销售演示就绪 |
| 6 (later) | 把 Action Registry 复用到方案 A 内嵌助手 | H5 高频场景 |

## 8. Open Questions

1. **浏览器池** vs **每会话独立 context**：百级并发以下后者更简单，预算？
2. **截图是否进 llm_traces**：调试价值高，但合规与存储成本？是否单独 `voice_screenshots` 表外挂 S3？
3. **小程序 webview 兼容性**：方案 B 不依赖前端，但用户最终入口在小程序里 — 是否在小程序内开一个"远程模式"按钮直接跳到 voice.html？
4. **真凭据托管**：Concur 真站需要员工 SSO，是否走企业微信 OAuth 代理？

---

附：A/B 在协议层是同构的。当 Phase 6 接入 H5 内嵌助手时，前端 JS 实现一个 `ClientSideDriver`（与 `PlaywrightDriver` 同接口），消费同一份 Action Registry — 这也是为什么 registry 设计上把 selector 和语义动作分离。
