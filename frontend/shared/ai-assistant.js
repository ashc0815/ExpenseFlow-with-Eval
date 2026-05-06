/**
 * AI 报销助手 — 可嵌入任何员工页面的侧边栏 drawer。
 *
 * 用法：在页面底部加 <script src="/shared/ai-assistant.js"></script>
 * 调用 /api/chat/message (unified `employee` agent)。
 *
 * 可选：宿主页面用 window.aiPageContext() 返回 {report_id} 让 AI
 * 感知当前打开的报销单。安全不靠这个——所有写工具内部都有 ACL。
 */
(function () {
  "use strict";

  // ── Inject CSS ──
  const style = document.createElement("style");
  style.textContent = `
    .ai-fab {
      position:fixed; bottom:1.5rem; right:1.5rem; width:52px; height:52px;
      border-radius:50%; background:#18b48e; color:white; border:none;
      font-size:1.4rem; cursor:pointer; box-shadow:0 4px 12px rgba(0,0,0,.15);
      z-index:90; display:flex; align-items:center; justify-content:center;
      transition:transform .2s;
    }
    .ai-fab:hover { transform:scale(1.08); }
    .ai-drawer {
      position:fixed; top:0; right:-400px; width:380px; height:100vh;
      background:white; border-left:1px solid #e2e8f0; z-index:100;
      display:flex; flex-direction:column; transition:right .3s ease;
      box-shadow:-4px 0 20px rgba(0,0,0,.08);
    }
    .ai-drawer.open { right:0; }
    .ai-drawer-header {
      padding:.8rem 1rem; border-bottom:1px solid #e2e8f0;
      display:flex; justify-content:space-between; align-items:center;
    }
    .ai-drawer-header h3 { margin:0; font-size:.95rem; }
    .ai-drawer-close {
      background:none; border:none; font-size:1.2rem; cursor:pointer;
      color:#64748b; padding:.2rem;
    }
    .ai-messages {
      flex:1; overflow-y:auto; padding:1rem; display:flex;
      flex-direction:column; gap:.6rem;
    }
    .ai-msg {
      max-width:85%; padding:.5rem .75rem; border-radius:12px;
      font-size:.85rem; line-height:1.5; word-break:break-word;
    }
    .ai-msg.user {
      align-self:flex-end; background:#18b48e; color:white;
      border-bottom-right-radius:4px;
    }
    .ai-msg.assistant {
      align-self:flex-start; background:#f1f5f9; color:#0f172a;
      border-bottom-left-radius:4px;
    }
    .ai-input-bar {
      padding:.6rem .8rem; border-top:1px solid #e2e8f0;
      display:flex; gap:.4rem;
    }
    .ai-input-bar input {
      flex:1; border:1px solid #e2e8f0; border-radius:8px;
      padding:.5rem .6rem; font-size:.85rem; outline:none;
    }
    .ai-input-bar input:focus { border-color:#18b48e; }
    .ai-input-bar button {
      background:#18b48e; color:white; border:none; border-radius:8px;
      padding:.5rem .8rem; font-size:.85rem; cursor:pointer;
    }
    .ai-input-bar button:disabled { opacity:.5; cursor:not-allowed; }
    .ai-overlay {
      position:fixed; inset:0; background:rgba(0,0,0,.2); z-index:99; display:none;
    }
    .ai-overlay.open { display:block; }
    .ai-suggestions {
      display:flex; flex-wrap:wrap; gap:.3rem; padding:0 1rem .5rem;
    }
    .ai-suggestions button {
      background:#f1f5f9; border:1px solid #e2e8f0; border-radius:16px;
      padding:.3rem .6rem; font-size:.75rem; color:#475569; cursor:pointer;
    }
    .ai-suggestions button:hover { background:#e2e8f0; }
  `;
  document.head.appendChild(style);

  // ── Inject HTML ──
  const _t = window.t || (k => k);

  // Role-aware welcome / placeholder / suggestion buttons. Mirrors the
  // backend's agent_role routing: ctx.role manager|finance_admin → manager
  // copilot tools (audit drill-down, queue, team spend); else → employee.
  // This is purely cosmetic — backend re-determines role server-side.
  function _isApprover() {
    try {
      const u = window.auth && window.auth.getUser && window.auth.getUser();
      const roles = (u && u.roles) || [];
      return roles.includes("manager") || roles.includes("finance_admin");
    } catch (_) { return false; }
  }
  const _approver = _isApprover();
  const welcomeText = _t(_approver ? "ai.welcome-manager" : "ai.welcome-qa").replace(/\n/g, "<br>");
  const placeholderText = _t(_approver ? "ai.placeholder-manager" : "ai.placeholder-qa");
  const suggestionsHtml = _approver
    ? `
        <button data-q="${_t("ai.sug-mgr-why-q")}">${_t("ai.sug-mgr-why")}</button>
        <button data-q="${_t("ai.sug-mgr-queue-q")}">${_t("ai.sug-mgr-queue")}</button>
        <button data-q="${_t("ai.sug-mgr-team-q")}">${_t("ai.sug-mgr-team")}</button>
        <button data-q="${_t("ai.sug-policy-q")}">${_t("ai.sug-policy")}</button>`
    : `
        <button data-q="${_t("ai.sug-monthly-q")}">${_t("ai.sug-monthly")}</button>
        <button data-q="${_t("ai.sug-budget-q")}">${_t("ai.sug-budget")}</button>
        <button data-q="${_t("ai.sug-dup-q")}">${_t("ai.sug-dup")}</button>
        <button data-q="${_t("ai.sug-policy-q")}">${_t("ai.sug-policy")}</button>`;

  const wrapper = document.createElement("div");
  wrapper.innerHTML = `
    <div class="ai-overlay" id="ai-overlay"></div>
    <button class="ai-fab" id="ai-fab" title="${_t("ai.title")}">💡</button>
    <div class="ai-drawer" id="ai-drawer">
      <div class="ai-drawer-header">
        <h3>${_t("ai.title")}</h3>
        <button class="ai-drawer-close" id="ai-close">✕</button>
      </div>
      <div class="ai-messages" id="ai-messages">
        <div class="ai-msg assistant">${welcomeText}</div>
      </div>
      <div class="ai-suggestions" id="ai-suggestions">${suggestionsHtml}
      </div>
      <div class="ai-input-bar">
        <input id="ai-input" placeholder="${placeholderText}">
        <button id="ai-send-btn">${_t("ai.send")}</button>
      </div>
    </div>`;
  document.body.appendChild(wrapper);

  // ── State ──
  const chatHistory = [];
  let streaming = false;

  function esc(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function getHeaders() {
    if (window.auth && window.auth.getHeaders) return await window.auth.getHeaders();
    return {};
  }

  function toggle() {
    document.getElementById("ai-drawer").classList.toggle("open");
    document.getElementById("ai-overlay").classList.toggle("open");
    if (document.getElementById("ai-drawer").classList.contains("open")) {
      document.getElementById("ai-input").focus();
    }
  }

  async function send(text) {
    if (streaming) return;
    if (!text) text = document.getElementById("ai-input").value.trim();
    if (!text) return;
    document.getElementById("ai-input").value = "";

    const msgBox = document.getElementById("ai-messages");
    document.getElementById("ai-suggestions").style.display = "none";

    msgBox.innerHTML += `<div class="ai-msg user">${esc(text)}</div>`;
    chatHistory.push({ role: "user", content: text });

    const aDiv = document.createElement("div");
    aDiv.className = "ai-msg assistant";
    aDiv.textContent = _t("ai.thinking");
    msgBox.appendChild(aDiv);
    msgBox.scrollTop = msgBox.scrollHeight;

    streaming = true;
    document.getElementById("ai-send-btn").disabled = true;

    try {
      const headers = await getHeaders();
      headers["Content-Type"] = "application/json";

      // Optional page context — host pages can expose report_id via
      // window.aiPageContext(). Back-end uses it only to inject report
      // info into the prompt; all write tools enforce ACL themselves.
      const context = (typeof window.aiPageContext === 'function')
        ? window.aiPageContext() || {}
        : {};

      const url = "/api/chat/message";
      const body = { messages: chatHistory.slice(-10), context };

      const resp = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let fullText = "", buffer = "";
      aDiv.textContent = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const ev = JSON.parse(line.slice(6));
            window.dispatchEvent(new CustomEvent("ai-assistant-event", { detail: ev }));
            if (ev.type === "assistant_text") {
              fullText += ev.text;
              aDiv.textContent = fullText;
              msgBox.scrollTop = msgBox.scrollHeight;
            } else if (ev.type === "tool_start") {
              aDiv.innerHTML = (fullText ? esc(fullText) + "<br>" : "") +
                '<span style="color:#94a3b8;font-size:.78rem">🔍 ' + esc(ev.tool || "查询中") + "…</span>";
            } else if (ev.type === "error") {
              aDiv.innerHTML = '<span style="color:#ef4444">' + esc(ev.message) + "</span>";
            }
          } catch {}
        }
      }

      if (fullText) {
        aDiv.textContent = fullText;
        chatHistory.push({ role: "assistant", content: fullText });
      }
    } catch (err) {
      aDiv.innerHTML = '<span style="color:#ef4444">' + esc(_t("ai.request-fail")) + esc(err.message) + "</span>";
    } finally {
      streaming = false;
      document.getElementById("ai-send-btn").disabled = false;
      msgBox.scrollTop = msgBox.scrollHeight;
    }
  }

  // ── Event listeners ──
  document.getElementById("ai-fab").addEventListener("click", toggle);
  document.getElementById("ai-close").addEventListener("click", toggle);
  document.getElementById("ai-overlay").addEventListener("click", toggle);
  document.getElementById("ai-send-btn").addEventListener("click", function () { send(); });
  document.getElementById("ai-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") send();
  });
  document.querySelectorAll("#ai-suggestions button[data-q]").forEach(function (btn) {
    btn.addEventListener("click", function () { send(btn.dataset.q); });
  });
})();
