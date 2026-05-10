/**
 * api.js — Thin wrapper around the ExpenseFlow backend REST API.
 *
 * Usage:
 *   const submissions = await api.listSubmissions();
 *   const sub = await api.submitExpense(formData);
 *   await api.pollSubmissionStatus(id, (s) => updateUI(s));
 */
(function (global) {
  "use strict";

  const BASE = (global.API_BASE || "").replace(/\/$/, "");

  async function _request(method, path, options = {}) {
    const headers = await global.auth.getHeaders();
    Object.assign(headers, options.headers || {});

    const init = { method, headers };
    if (options.body) init.body = options.body;
    if (options.json) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.json);
    }

    const res = await fetch(`${BASE}${path}`, init);
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw Object.assign(new Error(detail), { status: res.status });
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("Content-Type") || "";
    if (ct.includes("application/json")) return res.json();
    return res.text();
  }

  // ── Submissions ────────────────────────────────────────────────

  async function submitExpense(formData) {
    const headers = await global.auth.getHeaders();
    const res = await fetch(`${BASE}/api/submissions`, {
      method: "POST",
      headers,
      body: formData,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw Object.assign(new Error(detail), { status: res.status });
    }
    return res.json();
  }

  async function getSubmission(id) {
    return _request("GET", `/api/submissions/${id}`);
  }

  async function listSubmissions(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return _request("GET", `/api/submissions${qs ? "?" + qs : ""}`);
  }

  /**
   * Poll submission status until it is no longer "processing".
   * Calls onUpdate(submission) on each poll.
   * Stops after maxPolls (default 30) × intervalMs (default 3000 ms).
   */
  async function pollSubmissionStatus(id, onUpdate, {
    intervalMs = 3000,
    maxPolls = 30,
  } = {}) {
    let polls = 0;
    return new Promise((resolve, reject) => {
      const tick = async () => {
        try {
          const sub = await getSubmission(id);
          onUpdate(sub);
          if (sub.status !== "processing") {
            resolve(sub);
            return;
          }
          polls++;
          if (polls >= maxPolls) {
            resolve(sub);
            return;
          }
          setTimeout(tick, intervalMs);
        } catch (err) {
          reject(err);
        }
      };
      setTimeout(tick, intervalMs);
    });
  }

  // ── Approvals ──────────────────────────────────────────────────

  async function approveSubmission(id, comment = "") {
    return _request("POST", `/api/submissions/${id}/approve`, { json: { comment } });
  }

  async function rejectSubmission(id, comment = "") {
    return _request("POST", `/api/submissions/${id}/reject`, { json: { comment } });
  }

  async function bulkApprove(ids, comment = "") {
    return _request("POST", "/api/submissions/bulk-approve", { json: { ids, comment } });
  }

  // ── OCR ────────────────────────────────────────────────────────

  async function ocrExtract(file) {
    const headers = await global.auth.getHeaders();
    const fd = new FormData();
    fd.append("receipt_image", file);
    const res = await fetch(`${BASE}/api/ocr/extract`, {
      method: "POST",
      headers,
      body: fd,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw Object.assign(new Error(detail), { status: res.status });
    }
    return res.json();
  }

  // ── Admin ──────────────────────────────────────────────────────

  async function getPolicy() {
    return _request("GET", "/api/admin/policy");
  }

  async function listProjects() {
    return _request("GET", "/api/admin/projects");
  }

  async function updatePolicy(data) {
    return _request("PUT", "/api/admin/policy", { json: data });
  }

  async function getAuditLog(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return _request("GET", `/api/admin/audit-log${qs ? "?" + qs : ""}`);
  }

  async function getStats() {
    return _request("GET", "/api/admin/stats");
  }

  async function exportCsv() {
    const headers = await global.auth.getHeaders();
    const res = await fetch(`${BASE}/api/admin/export`, { headers });
    if (!res.ok) throw new Error("Export failed");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "submissions.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  // ── Employees ──────────────────────────────────────────────────

  async function listEmployees(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return _request("GET", `/api/employees${qs ? "?" + qs : ""}`);
  }

  async function getMyEmployee() {
    return _request("GET", "/api/employees/me").catch(err => {
      if (err.status === 404) return null;
      throw err;
    });
  }

  async function createEmployee(data) {
    return _request("POST", "/api/employees", { json: data });
  }

  async function updateEmployee(id, data) {
    return _request("PUT", `/api/employees/${id}`, { json: data });
  }

  async function deleteEmployee(id) {
    return _request("DELETE", `/api/employees/${id}`);
  }

  // ── Finance ────────────────────────────────────────────────────

  async function financeApprove(reportId, body = {}) {
    return _request("POST", `/api/reports/${reportId}/finance-approve`, { json: body });
  }

  async function financeReject(reportId, comment = "") {
    return _request("POST", `/api/reports/${reportId}/finance-reject`, { json: { comment } });
  }

  async function financeBulkApprove(ids, comment = "") {
    return _request("POST", "/api/reports/queue/finance/bulk-approve", { json: { ids, comment } });
  }

  async function financeQueue() {
    return _request("GET", "/api/reports/queue/finance");
  }

  // ── Chat / Agent ───────────────────────────────────────────────

  async function createDraft() {
    return _request("POST", "/api/chat/drafts");
  }

  async function getDraft(id) {
    return _request("GET", `/api/chat/drafts/${id}`);
  }

  async function uploadDraftReceipt(id, file) {
    const headers = await global.auth.getHeaders();
    const fd = new FormData();
    fd.append("receipt_image", file);
    const res = await fetch(`${BASE}/api/chat/drafts/${id}/receipt`, {
      method: "POST", headers, body: fd,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw Object.assign(new Error(detail), { status: res.status });
    }
    return res.json();
  }

  /**
   * 发送消息到 agent，以 SSE 流式接收事件。
   * 用法：
   *   await chatStream(draftId, "处理发票", (event) => { ... });
   */
  async function chatStream(draftId, message, onEvent) {
    const headers = await global.auth.getHeaders();
    headers["Content-Type"] = "application/json";
    const res = await fetch(`${BASE}/api/chat/drafts/${draftId}/message`, {
      method: "POST", headers,
      body: JSON.stringify({ message }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw Object.assign(new Error(detail), { status: res.status });
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE 事件以 \n\n 分隔，每条以 "data: " 开头
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);
        if (chunk.startsWith("data: ")) {
          try {
            const event = JSON.parse(chunk.slice(6));
            onEvent(event);
          } catch (e) { /* ignore bad chunk */ }
        }
      }
    }
  }

  async function submitDraft(id) {
    return _request("POST", `/api/chat/drafts/${id}/submit`);
  }

  /**
   * 嵌入式 AI 解释卡 — 经理/财务点开报销时调用，返回结构化 JSON。
   * 不是 SSE，不是对话 —— 单次请求 / 单次响应。
   */
  async function getExplanation(submissionId) {
    return _request("POST", `/api/chat/explain/${submissionId}`);
  }

  /**
   * Stateless Q&A agent stream — 前端维护完整消息历史，后端不持久化。
   * messages = [{role:"user"|"assistant", content:"..."}, ...]
   */
  async function qaStream(messages, onEvent) {
    const headers = await global.auth.getHeaders();
    headers["Content-Type"] = "application/json";
    const res = await fetch(`${BASE}/api/chat/message`, {
      method: "POST", headers,
      body: JSON.stringify({ messages }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw Object.assign(new Error(detail), { status: res.status });
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);
        if (chunk.startsWith("data: ")) {
          try {
            const event = JSON.parse(chunk.slice(6));
            onEvent(event);
          } catch (e) { /* ignore bad chunk */ }
        }
      }
    }
  }

  async function financeExportPreview() {
    return _request("GET", "/api/finance/export/preview");
  }

  async function financeExport(ids) {
    const headers = await global.auth.getHeaders();
    headers["Content-Type"] = "application/json";
    const res = await fetch(`${BASE}/api/finance/export`, {
      method: "POST",
      headers,
      body: JSON.stringify({ ids }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw Object.assign(new Error(detail), { status: res.status });
    }
    const blob = await res.blob();
    const batchId = res.headers.get("X-Batch-Id") || "batch";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `erp_export_${batchId.slice(0, 8)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    return { batch_id: batchId, count: ids.length };
  }

  // ── Budget ────────────────────────────────────────────────────────

  async function getBudgetStatus(costCenter, amount, period) {
    const params = new URLSearchParams();
    if (amount != null) params.set('amount', amount);
    if (period) params.set('period', period);
    const qs = params.toString();
    return _request('GET', `/api/budget/status/${encodeURIComponent(costCenter)}${qs ? '?' + qs : ''}`);
  }

  async function getBudgetSnapshot(period) {
    const qs = period ? `?period=${encodeURIComponent(period)}` : '';
    return _request('GET', `/api/budget/snapshot/me${qs}`);
  }

  async function unblockSubmission(id) {
    return _request('PATCH', `/api/submissions/${id}/unblock`);
  }

  async function getBudgetAmounts(period) {
    const qs = period ? `?period=${encodeURIComponent(period)}` : '';
    return _request('GET', `/api/budget/amounts${qs}`);
  }

  async function updateBudgetPolicy(costCenter, body) {
    const cc = encodeURIComponent(costCenter || '_default');
    return _request('PUT', `/api/budget/policies/${cc}`, { json: body });
  }

  async function upsertBudgetAmount(body) {
    return _request('POST', '/api/budget/amounts', { json: body });
  }

  // ── Public ─────────────────────────────────────────────────────
  global.api = {
    submitExpense,
    getSubmission,
    listSubmissions,
    pollSubmissionStatus,
    approveSubmission,
    rejectSubmission,
    bulkApprove,
    ocrExtract,
    getPolicy,
    listProjects,
    updatePolicy,
    getAuditLog,
    getStats,
    exportCsv,
    // employees
    listEmployees,
    getMyEmployee,
    createEmployee,
    updateEmployee,
    deleteEmployee,
    // finance
    financeApprove,
    financeReject,
    financeBulkApprove,
    financeQueue,
    financeExportPreview,
    financeExport,
    // chat / agent
    createDraft,
    getDraft,
    uploadDraftReceipt,
    chatStream,
    qaStream,
    submitDraft,
    getExplanation,
    // budget
    getBudgetStatus,
    getBudgetSnapshot,
    unblockSubmission,
    getBudgetAmounts,
    updateBudgetPolicy,
    upsertBudgetAmount,
  };
})(window);
