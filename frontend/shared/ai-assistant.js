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
      position:fixed; top:0; right:0; width:min(520px, 92vw); height:100vh;
      background:white; border-left:1px solid #e2e8f0; z-index:100;
      display:flex; flex-direction:column; transform:translateX(100%);
      transition:transform .3s ease;
      box-shadow:-4px 0 20px rgba(0,0,0,.08);
    }
    .ai-drawer.open { transform:translateX(0); }
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
    .ai-msg.assistant.ai-policy-msg {
      max-width:96%; width:96%; background:#fff; border:1px solid #e2e8f0;
      padding:.65rem; box-shadow:0 1px 2px rgba(15,23,42,.04);
    }
    .ai-msg.assistant strong { color:#047857; font-weight:700; }
    .ai-tool-status { color:#94a3b8; font-size:.78rem; }
    .ai-error-text { color:#ef4444; }
    .ai-policy-card { display:flex; flex-direction:column; gap:.65rem; }
    .ai-policy-title {
      display:flex; align-items:center; justify-content:space-between; gap:.5rem;
      font-size:.9rem; font-weight:800; color:#0f172a;
    }
    .ai-policy-badge {
      font-size:.68rem; font-weight:700; color:#047857; background:#ecfdf5;
      border:1px solid #bbf7d0; border-radius:999px; padding:.16rem .45rem;
      white-space:nowrap;
    }
    .ai-policy-section {
      border:1px solid #e2e8f0; border-radius:8px; overflow:hidden; background:#fff;
    }
    .ai-policy-section h4 {
      margin:0; padding:.48rem .6rem; font-size:.74rem; color:#334155;
      background:#f8fafc; border-bottom:1px solid #e2e8f0;
    }
    .ai-policy-body { padding:.55rem .6rem; }
    .ai-policy-chip-row { display:flex; flex-wrap:wrap; gap:.35rem; }
    .ai-policy-chip {
      display:inline-flex; align-items:center; gap:.24rem; border-radius:999px;
      border:1px solid #dbeafe; background:#eff6ff; color:#1e3a8a;
      font-size:.7rem; padding:.18rem .45rem; font-weight:600;
    }
    .ai-policy-table-wrap { overflow-x:auto; }
    .ai-policy-table { width:100%; border-collapse:collapse; font-size:.72rem; min-width:420px; }
    .ai-policy-table th, .ai-policy-table td {
      padding:.42rem .5rem; border-bottom:1px solid #e2e8f0; text-align:left;
      white-space:nowrap;
    }
    .ai-policy-table th { background:#f8fafc; color:#475569; font-weight:700; }
    .ai-policy-table tr:last-child td { border-bottom:0; }
    .ai-policy-rule-grid { display:grid; grid-template-columns:1fr; gap:.4rem; }
    .ai-policy-rule {
      border:1px solid #dcfce7; background:#f0fdf4; color:#14532d;
      border-radius:8px; padding:.45rem .55rem; font-size:.72rem;
    }
    .ai-policy-note { font-size:.7rem; color:#64748b; line-height:1.45; }
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
      // mock auth also stores a `mock_role` key; check both that and
      // auth.getUser() to be robust against load-order edge cases.
      const stored = (typeof localStorage !== "undefined")
        ? localStorage.getItem("mock_role") : null;
      if (stored === "manager" || stored === "finance_admin") return true;
      const u = window.auth && window.auth.getUser && window.auth.getUser();
      const roles = (u && u.roles) || [];
      return roles.includes("manager") || roles.includes("finance_admin");
    } catch (_) { return false; }
  }
  function _pageContext() {
    try {
      return (typeof window.aiPageContext === "function")
        ? (window.aiPageContext() || {})
        : {};
    } catch (_) {
      return {};
    }
  }
  function _isQuickExpenseSurface() {
    const ctx = _pageContext();
    return ctx.page === "quick" || ctx.surface === "quick_expense";
  }
  function _suggestionsHtml(approver) {
    if (!approver && _isQuickExpenseSurface()) {
      return `
        <button data-q="${_t("ai.sug-quick-didi-q")}" data-action="prefill">${_t("ai.sug-quick-didi")}</button>
        <button data-q="${_t("ai.sug-quick-missing-q")}">${_t("ai.sug-quick-missing")}</button>
        <button data-q="${_t("ai.sug-quick-edit-q")}">${_t("ai.sug-quick-edit")}</button>
        <button data-q="${_t("ai.sug-policy-q")}">${_t("ai.sug-policy")}</button>`;
    }
    return approver
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
  }
  function _renderRoleAwareUI() {
    const approver = _isApprover();
    const quick = _isQuickExpenseSurface();
    const welcomeText = _t(approver ? "ai.welcome-manager" : quick ? "ai.welcome-submit" : "ai.welcome-qa").replace(/\n/g, "<br>");
    const placeholderText = _t(approver ? "ai.placeholder-manager" : quick ? "ai.placeholder-submit" : "ai.placeholder-qa");
    const msgBox = document.getElementById("ai-messages");
    const sugBox = document.getElementById("ai-suggestions");
    const inp    = document.getElementById("ai-input");
    if (msgBox && msgBox.children.length <= 1) {
      // Only re-render the welcome when the chat is fresh (one assistant
      // greeting bubble). Don't clobber an in-progress conversation.
      msgBox.innerHTML = `<div class="ai-msg assistant">${welcomeText}</div>`;
    }
    if (sugBox) sugBox.innerHTML = _suggestionsHtml(approver);
    if (inp)    inp.placeholder = placeholderText;
  }

  const _approverInitial = _isApprover();
  const _quickInitial = _isQuickExpenseSurface();
  const welcomeText = _t(_approverInitial ? "ai.welcome-manager" : _quickInitial ? "ai.welcome-submit" : "ai.welcome-qa").replace(/\n/g, "<br>");
  const placeholderText = _t(_approverInitial ? "ai.placeholder-manager" : _quickInitial ? "ai.placeholder-submit" : "ai.placeholder-qa");
  const suggestionsHtml = _suggestionsHtml(_approverInitial);

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

  function renderPlainAssistantText(text) {
    return esc(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\n/g, "<br>");
  }

  function yuan(v) {
    if (v === undefined || v === null || v === "") return "—";
    if (String(v) === "不限") return "不限";
    return "¥" + esc(v);
  }

  function getPolicyFocus(query) {
    const q = String(query || "").toLowerCase();
    if (/交通|打车|出租|滴滴|车费|通勤/.test(q)) {
      return {
        mode: "focused",
        title: "交通费报销政策",
        limitKeys: ["local_transport_per_day"],
        categoryMatch: item => /交通|transport/.test(String(item.subtype || item.id || item.limit_key || "")),
      };
    }
    if (/住宿|酒店|宾馆/.test(q)) {
      return {
        mode: "focused",
        title: "住宿报销政策",
        limitKeys: ["accommodation_per_night"],
        categoryMatch: item => /住宿|accommodation|酒店/.test(String(item.subtype || item.id || item.limit_key || "")),
      };
    }
    if (/餐|饭|午餐|晚餐|餐饮|用餐/.test(q)) {
      return {
        mode: "focused",
        title: "餐费报销政策",
        limitKeys: ["meals_per_person"],
        categoryMatch: item => /餐|meal/.test(String(item.subtype || item.id || item.limit_key || "")),
      };
    }
    if (/招待|客户|宴请/.test(q)) {
      return {
        mode: "focused",
        title: "业务招待费报销政策",
        limitKeys: ["meals_per_person"],
        categoryMatch: item => /招待|客户|宴请|client/.test(String(item.category || item.subtype || item.id || "")),
      };
    }
    if (/发票|票据/.test(q)) {
      return {
        mode: "requirements",
        title: "发票与材料要求",
        limitKeys: [],
        categoryMatch: item => !!item.requires_invoice || !!item.requires_attendee_list,
      };
    }
    if (/付款|转账|备用金|支付/.test(q)) {
      return { mode: "payment", title: "付款与超标规则", limitKeys: [], categoryMatch: null };
    }
    return { mode: "overview", title: "报销政策概览", limitKeys: [], categoryMatch: null };
  }

  function renderPolicyCard(policy, fallbackText, query) {
    if (!policy || policy.error) return renderPlainAssistantText(fallbackText || (policy && policy.error) || "");
    const focus = getPolicyFocus(query || fallbackText);
    const showOverview = focus.mode === "overview";
    const showPaymentOnly = focus.mode === "payment";

    const levels = policy.employee_levels_struct || [];
    const levelHtml = levels.length
      ? levels.map(lv => `<span class="ai-policy-chip">${esc(lv.id)} · ${esc(lv.name)}</span>`).join("")
      : (policy.employee_levels || []).map(v => `<span class="ai-policy-chip">${esc(v)}</span>`).join("");

    const cities = policy.city_tiers_struct || [];
    const cityRows = cities.length
      ? cities.map(row => `<tr><td>${esc(row.tier)}</td><td>${esc((row.cities || []).join("、"))}</td></tr>`).join("")
      : (policy.city_tiers || []).map(v => {
          const parts = String(v).split(":");
          return `<tr><td>${esc(parts[0] || "")}</td><td>${esc(parts.slice(1).join(":").trim())}</td></tr>`;
        }).join("");

    const limits = (policy.limit_matrix || []).filter(row => {
      if (showOverview || showPaymentOnly || focus.mode === "requirements") return false;
      return !focus.limitKeys.length || focus.limitKeys.includes(row.key);
    });
    const limitRows = limits.length
      ? limits.map(row => `<tr><td>${esc(row.name || row.key)}</td><td>${esc(row.tier)}</td><td>${yuan(row.L1)}</td><td>${yuan(row.L2)}</td><td>${yuan(row.L3)}</td><td>${yuan(row.L4)}</td></tr>`).join("")
      : (policy.limits || []).map(v => `<tr><td colspan="6">${esc(v)}</td></tr>`).join("");

    const catsAll = policy.expense_categories_struct || [];
    const cats = focus.categoryMatch ? catsAll.filter(focus.categoryMatch) : catsAll;
    const catRows = cats.length
      ? cats.map(item => {
          const flags = [];
          if (item.requires_invoice) flags.push("需发票");
          if (item.requires_attendee_list) flags.push("需参会人员名单");
          return `<tr><td>${esc(item.category)}</td><td>${esc(item.subtype)}</td><td>${esc(flags.join("、") || "无特殊要求")}</td></tr>`;
        }).join("")
      : (policy.expense_categories || []).map(v => `<tr><td colspan="3">${esc(v)}</td></tr>`).join("");

    const rules = []
      .concat(Object.values(policy.payment_rules || {}))
      .concat(Object.values(policy.tolerance_rules || {}));
    const ruleHtml = rules.map(v => `<div class="ai-policy-rule">${esc(v)}</div>`).join("");

    const overviewHtml = showOverview ? `
        <div class="ai-policy-section">
          <h4>可查询的政策模块</h4>
          <div class="ai-policy-body">
            <div class="ai-policy-chip-row">
              <span class="ai-policy-chip">餐费限额</span>
              <span class="ai-policy-chip">住宿限额</span>
              <span class="ai-policy-chip">交通费</span>
              <span class="ai-policy-chip">发票要求</span>
              <span class="ai-policy-chip">付款规则</span>
              <span class="ai-policy-chip">超标处理</span>
            </div>
          </div>
        </div>` : "";

    const levelSection = (!showPaymentOnly && !showOverview) ? `
        <div class="ai-policy-section">
          <h4>员工级别</h4>
          <div class="ai-policy-body"><div class="ai-policy-chip-row">${levelHtml || "—"}</div></div>
        </div>` : "";

    const citySection = (!showPaymentOnly && !showOverview && focus.mode !== "requirements") ? `
        <div class="ai-policy-section">
          <h4>城市级别</h4>
          <div class="ai-policy-table-wrap"><table class="ai-policy-table"><thead><tr><th>等级</th><th>城市</th></tr></thead><tbody>${cityRows || "<tr><td colspan='2'>—</td></tr>"}</tbody></table></div>
        </div>` : "";

    const limitSection = (!showOverview && !showPaymentOnly && focus.mode !== "requirements" && limits.length) ? `
        <div class="ai-policy-section">
          <h4>费用限额</h4>
          <div class="ai-policy-table-wrap"><table class="ai-policy-table"><thead><tr><th>费用</th><th>城市</th><th>L1</th><th>L2</th><th>L3</th><th>L4</th></tr></thead><tbody>${limitRows}</tbody></table></div>
        </div>` : "";

    const categorySection = (!showPaymentOnly && cats.length) ? `
        <div class="ai-policy-section">
          <h4>${showOverview ? "费用类别与材料要求" : "相关类别与材料要求"}</h4>
          <div class="ai-policy-table-wrap"><table class="ai-policy-table"><thead><tr><th>大类</th><th>子类</th><th>要求</th></tr></thead><tbody>${catRows}</tbody></table></div>
        </div>` : "";

    const paymentSection = (showOverview || showPaymentOnly || focus.mode === "focused") ? `
        <div class="ai-policy-section">
          <h4>付款与超标规则</h4>
          <div class="ai-policy-body"><div class="ai-policy-rule-grid">${ruleHtml || "—"}</div></div>
        </div>` : "";

    return `
      <div class="ai-policy-card">
        <div class="ai-policy-title">
          <span>${esc(policy.company || "公司")} ${esc(focus.title)}</span>
          <span class="ai-policy-badge">Policy</span>
        </div>
        ${overviewHtml}
        ${levelSection}
        ${citySection}
        ${limitSection}
        ${categorySection}
        ${paymentSection}
        <div class="ai-policy-note">如需判断某一笔具体报销，请告诉我费用类型、城市、员工级别、金额和发票状态。</div>
      </div>`;
  }

  function applyAssistantRender(node, text, context) {
    const policy = context && context.policy;
    node.classList.toggle("ai-policy-msg", !!policy);
    node.innerHTML = policy ? renderPolicyCard(policy, text, context && context.query) : renderPlainAssistantText(text);
  }

  async function getHeaders() {
    if (window.auth && window.auth.getHeaders) return await window.auth.getHeaders();
    return {};
  }

  function toggle() {
    document.getElementById("ai-drawer").classList.toggle("open");
    document.getElementById("ai-overlay").classList.toggle("open");
    if (document.getElementById("ai-drawer").classList.contains("open")) {
      // Re-evaluate role on every open. If the user switched role in
      // another tab and came back, the drawer reflects it without page
      // reload.
      _renderRoleAwareUI();
      document.getElementById("ai-input").focus();
    }
  }
  function openDrawer() {
    const drawer = document.getElementById("ai-drawer");
    if (!drawer.classList.contains("open")) {
      toggle();
    } else {
      _renderRoleAwareUI();
      document.getElementById("ai-input").focus();
    }
  }
  window.aiAssistantOpen = openDrawer;

  async function send(text) {
    if (streaming) return;
    const inputEl = document.getElementById("ai-input");
    if (!text) text = inputEl.value.trim();
    if (!text) return;

    let context = _pageContext();
    if (typeof window.aiAssistantBeforeSend === "function") {
      const prepared = await window.aiAssistantBeforeSend({ text, context });
      if (prepared === false) return;
      if (prepared && typeof prepared === "object") {
        text = prepared.text || text;
        context = { ...context, ...(prepared.context || {}) };
      }
    } else {
      context = _pageContext();
    }
    inputEl.value = "";

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
      let fullText = "", buffer = "", activePolicy = null;
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
              applyAssistantRender(aDiv, fullText, { policy: activePolicy, query: text });
              msgBox.scrollTop = msgBox.scrollHeight;
            } else if (ev.type === "tool_call") {
              const agentLabel = ev.subagent ? esc(ev.subagent) + " · " : "";
              aDiv.innerHTML = (fullText ? renderPlainAssistantText(fullText) + "<br>" : "") +
                '<span class="ai-tool-status">🔍 ' + agentLabel + esc(ev.name || "查询中") + "…</span>";
            } else if (ev.type === "tool_result" && ev.name === "get_policy_rules") {
              activePolicy = ev.result || null;
            } else if (ev.type === "error") {
              aDiv.innerHTML = '<span class="ai-error-text">' + esc(ev.message) + "</span>";
            }
          } catch {}
        }
      }

      if (fullText) {
        applyAssistantRender(aDiv, fullText, { policy: activePolicy, query: text });
        chatHistory.push({ role: "assistant", content: fullText });
      }
    } catch (err) {
      aDiv.innerHTML = '<span class="ai-error-text">' + esc(_t("ai.request-fail")) + esc(err.message) + "</span>";
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
  document.getElementById("ai-suggestions").addEventListener("click", function (e) {
    const btn = e.target.closest("button[data-q]");
    if (!btn) return;
    const q = btn.dataset.q || btn.textContent.trim();
    const inputEl = document.getElementById("ai-input");
    inputEl.value = q;
    inputEl.focus();
    const placeholderStart = q.indexOf("[");
    const placeholderEnd = q.indexOf("]", placeholderStart + 1);
    if (placeholderStart >= 0 && placeholderEnd > placeholderStart) {
      inputEl.setSelectionRange(placeholderStart, placeholderEnd + 1);
    } else {
      inputEl.setSelectionRange(q.length, q.length);
    }
    if (btn.dataset.action === "prefill") return;
    send(q);
  });
})();
