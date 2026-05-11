# Voice Web Agent — Plan

**Date:** 2026-05-11
**Spec:** `docs/superpowers/specs/2026-05-11-voice-web-agent-design.md`

## Goal

让用户对着麦克风口述就能在 ExpenseFlow 网页上完成报销/审批操作。首发走方案 B（后台 Playwright + Claude tool-use），并把 Action Registry 设计成方案 A 也能复用的协议。

## Phase 1 — Skeleton（先桩、先协议）

- [ ] `backend/services/asr.py`：`ASRAdapter` 接口 + `StubASR` 实现
- [ ] `config/voice_actions/employee.quick.yaml`：`quick.html` 的 actions 注册（select_receipt / set_purpose / submit_report）
- [ ] `backend/services/action_registry.py`：YAML loader + `for_url()` 匹配
- [ ] `backend/api/voice_agent.py`：`POST /agent/voice/sessions`、`/utter`、SSE `/events`
- [ ] 单测：注册表加载 + URL 匹配 + 工具 schema 生成

## Phase 2 — End-to-End MVP

- [ ] `backend/services/playwright_driver.py`：click/fill/select_in_list/screenshot/goto
- [ ] `backend/services/voice_agent_loop.py`：Claude tool-use 多轮循环 + URL 变更刷新工具集 + llm_traces 写入
- [ ] `frontend/employee/voice.html`：文本框（桩 ASR）+ SSE 日志流 + 截图轮询
- [ ] 验收 case：「新建报销，加昨天 86 元滴滴，用途客户拜访，提交给李四」端到端跑通

## Phase 3 — Safety & Coverage

- [ ] confirm 流程（金额阈值 / `confirm: true` 动作）
- [ ] 注册表覆盖 `submit.html` / `report.html` / `my-reports.html`
- [ ] 前端控件补 `data-action` / `data-field` 语义属性（单独 PR）

## Phase 4 — Roles & Eval

- [ ] 经理 `queue.html`、财务 `review.html` 注册表
- [ ] 角色权限过滤（注册表 + 后端二次校验）
- [ ] `tests/eval_cases/voice_agent.yaml` + pytest harness（接 4.18 eval 平台）

## Phase 5 — Real ASR & Cross-system

- [ ] 阿里云 / Whisper adapter 二选一接入
- [ ] 浏览器直播（CDP screencast 或截图轮询）
- [ ] Concur 中国版 PoC：白名单 + SSO 代理

## Phase 6 — Reuse 到方案 A

- [ ] `frontend/shared/voice-sidebar.js`：消费同一份 Action Registry 在浏览器内派发
- [ ] 小程序 webview 适配

## Out of Scope

- 真凭据托管 / 企业级登录池
- 高并发浏览器池（百级以下用每会话 context）
- 截图脱敏 pipeline（合规需求确定后再做）
