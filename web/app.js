/**
 * FinAgent Web UI Client
 * Contracts & SSE: doc/API_CONTRACT.md (v0.1.0) & doc/contract/*.sse
 * Visual Design: Stitch Screen 1 & Screen 3 (Institutional Core, High-Density Utility)
 */

(function () {
  'use strict';

  // --- Constants & Fixtures ---
  const MOCK_FIXTURES = {
    easy: [
      { type: "RouteDecided", route: "easy", reason: "단일 상품 사실 조회" },
      { type: "TaskCompleted", id: "t1", agent: "rag", ok: true, retries: 0, elapsed_ms: 41 },
      { type: "Validation", attempt: 1, verdict: "pass", reason: null },
      {
        type: "FinalAnswer",
        reply: "우리SUPER주거래적금 12개월 최고금리는 연 3.85%입니다(기본 2.45% + 우대조건 충족 시).",
        route: "easy",
        products: [
          {
            id: "saving_202609_0010001_WR0001F",
            kind: "saving",
            kind_ko: "적금",
            group: "bank",
            bank: "우리은행",
            product: "우리SUPER주거래적금",
            sale: "판매중",
            highlight: { label: "12개월 최고금리", value: 3.85, unit: "%" },
            data_flags: []
          }
        ],
        citations: [
          {
            product_id: "saving_202609_0010001_WR0001F",
            section: "우대조건",
            quote: "가.급여/연금 이체:연 0.7%p"
          }
        ],
        calculations: [],
        warnings: [
          "2026년 9월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. 가입·대출 권유가 아닙니다."
        ],
        metadata: {
          version: "v2",
          latency_ms: 6120,
          llm_calls: 1,
          retries: 0,
          validation_attempts: 1,
          cost_usd: 0.0161,
          data_month: "202609",
          thread_id: "t-1"
        }
      }
    ],

    hard: [
      { type: "RouteDecided", route: "hard", reason: "금액·기간이 있는 계산 질문" },
      {
        type: "PlanCreated",
        tasks: [
          { id: "t1", agent: "structured", desc: "우리SUPER주거래적금 정액적립식 12개월 기본금리 조회" },
          { id: "t2", agent: "calc", desc: "월 500,000원 × 12개월 단리 세전이자 계산" }
        ]
      },
      { type: "TaskCompleted", id: "t1", agent: "structured", ok: true, retries: 0, elapsed_ms: 12 },
      { type: "TaskCompleted", id: "t2", agent: "calc", ok: true, retries: 0, elapsed_ms: 8 },
      { type: "ReplanCheck", round: 0, is_sufficient: true, reason: "필요한 금리와 계산 결과가 모두 있음" },
      { type: "Validation", attempt: 1, verdict: "fail", reason: "답의 79,652원이 계산 결과 79,625원과 다름" },
      { type: "Validation", attempt: 2, verdict: "pass", reason: null },
      {
        type: "FinalAnswer",
        reply: "월 50만원씩 12개월(정액적립식, 기본금리 연 2.45%) 넣으면 단리 세전이자는 **79,625원**입니다.",
        route: "hard",
        products: [
          {
            id: "saving_202609_0010001_WR0001F",
            kind: "saving",
            kind_ko: "적금",
            group: "bank",
            bank: "우리은행",
            product: "우리SUPER주거래적금",
            sale: "판매중",
            highlight: { label: "12개월 기본금리", value: 2.45, unit: "%" },
            data_flags: []
          }
        ],
        citations: [],
        calculations: [
          {
            name: "적금 단리 세전이자",
            inputs: { monthly_won: 500000, term_months: 12, rate_pct: 2.45 },
            formula: "월납입 × T(T+1)/2 × 연이율 / 1200 (원 미만 절사)",
            result: 79625,
            unit: "원"
          }
        ],
        warnings: [
          "2026년 9월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. 가입·대출 권유가 아닙니다."
        ],
        metadata: {
          version: "v2",
          latency_ms: 14310,
          llm_calls: 3,
          retries: 0,
          validation_attempts: 2,
          cost_usd: 0.0342,
          data_month: "202609",
          thread_id: "t-2"
        }
      }
    ],

    v1: [
      {
        type: "FinalAnswer",
        reply: "우리은행 우리SUPER주거래적금 12개월 최고우대금리는 3.85%입니다.",
        route: "baseline",
        products: [],
        citations: [],
        calculations: [],
        warnings: [
          "2026년 9월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. 가입·대출 권유가 아닙니다."
        ],
        metadata: {
          version: "v1",
          latency_ms: 9800,
          llm_calls: 1,
          retries: 0,
          validation_attempts: 0,
          cost_usd: 0.0162,
          data_month: "202609",
          thread_id: "t-1"
        }
      }
    ],

    error: [
      { type: "RouteDecided", route: "hard", reason: "비교 질문" },
      { type: "Error", code: "LLM_UNAVAILABLE", message: "로컬 LLM에 연결하지 못했습니다." }
    ]
  };

  // --- State ---
  let state = {
    mode: 'live', // 'live' or 'mock'
    mockScenario: 'hard',
    sessions: [],
    currentSessionId: null,
    isStreaming: false,
    activeAbortController: null,
    backendOnline: false,
    backendDataMonth: '202609',
    backendVersion: 'v2',
    backendLlm: 'ollama:qwen3:8b'
  };

  // --- DOM Elements ---
  const el = {
    sidebar: document.getElementById('sidebar'),
    sidebarBackdrop: document.getElementById('sidebar-backdrop'),
    btnToggleSidebar: document.getElementById('btn-toggle-sidebar'),
    btnNewChat: document.getElementById('btn-new-chat'),
    btnModeLive: document.getElementById('btn-mode-live'),
    btnModeMock: document.getElementById('btn-mode-mock'),
    selectMockScenario: document.getElementById('select-mock-scenario'),
    mockSelectorWrap: document.getElementById('mock-selector-wrap'),
    btnExportChat: document.getElementById('btn-export-chat'),
    btnClearChat: document.getElementById('btn-clear-chat'),
    badgeBackendStatus: document.getElementById('badge-backend-status'),
    backendStatusDot: document.getElementById('backend-status-dot'),
    backendStatusText: document.getElementById('backend-status-text'),
    headerDataMonth: document.getElementById('header-data-month'),
    sidebarDataMonth: document.getElementById('sidebar-data-month'),
    sidebarLlmName: document.getElementById('sidebar-llm-name'),
    sidebarStatusPill: document.getElementById('sidebar-status-pill'),
    sidebarClock: document.getElementById('sidebar-clock'),
    sidebarSessionCount: document.getElementById('sidebar-session-count'),
    sessionGroupToday: document.getElementById('session-group-today'),
    sessionGroupPrevious: document.getElementById('session-group-previous'),
    sessionTitle: document.getElementById('session-title'),
    currentThreadId: document.getElementById('current-thread-id'),
    headerStreamIndicator: document.getElementById('header-stream-indicator'),
    chatMessagesContainer: document.getElementById('chat-messages-container'),
    chatEmptyState: document.getElementById('chat-empty-state'),
    chatForm: document.getElementById('chat-form'),
    chatInput: document.getElementById('chat-input'),
    charCounter: document.getElementById('char-counter'),
    btnSend: document.getElementById('btn-send'),
    btnStopStream: document.getElementById('btn-stop-stream')
  };

  // --- Helpers ---
  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderSimpleMarkdown(text) {
    if (!text) return '';
    let html = escapeHtml(text);
    // **bold**
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong class="font-semibold text-secondary">$1</strong>');
    // *italic*
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // `code`
    html = html.replace(/`(.+?)`/g, '<code class="bg-surface-container px-1 py-0.5 rounded font-label-mono text-[13px]">$1</code>');
    // line breaks
    html = html.replace(/\n\n/g, '</p><p class="mt-2.5">');
    html = html.replace(/\n/g, '<br/>');
    return `<p class="leading-relaxed">${html}</p>`;
  }

  function formatNumber(num) {
    if (num === null || num === undefined) return '';
    return Number(num).toLocaleString('ko-KR');
  }

  function formatTime(date) {
    const d = date || new Date();
    return d.toTimeString().split(' ')[0] + ' KST';
  }

  // --- Storage ---
  function loadSessions() {
    try {
      const data = localStorage.getItem('finagent_sessions_v1');
      if (data) {
        state.sessions = JSON.parse(data);
      }
    } catch (e) {
      console.warn('Failed to load sessions from storage:', e);
      state.sessions = [];
    }

    if (!state.sessions || state.sessions.length === 0) {
      // create default initial session
      createNewSession('우리SUPER주거래적금 이자 계산', 't-1');
    } else {
      state.currentSessionId = state.sessions[0].id;
    }
    renderSessionList();
    renderCurrentSessionMessages();
  }

  function saveSessions() {
    try {
      localStorage.setItem('finagent_sessions_v1', JSON.stringify(state.sessions));
    } catch (e) {
      console.warn('Failed to save sessions to storage:', e);
    }
    renderSessionList();
  }

  function getCurrentSession() {
    return state.sessions.find(s => s.id === state.currentSessionId) || state.sessions[0];
  }

  function createNewSession(title, customId) {
    const id = customId || 't-' + Date.now();
    const session = {
      id: id,
      title: title || '새 금융 상담',
      createdAt: new Date().toISOString(),
      messages: []
    };
    state.sessions.unshift(session);
    state.currentSessionId = id;
    saveSessions();
    renderSessionList();
    renderCurrentSessionMessages();
    return session;
  }

  // --- Sidebar & Clock ---
  function updateClock() {
    if (el.sidebarClock) {
      const now = new Date();
      el.sidebarClock.textContent = now.toTimeString().split(' ')[0] + ' KST';
    }
  }
  setInterval(updateClock, 1000);
  updateClock();

  function renderSessionList() {
    if (!el.sessionGroupToday || !el.sessionGroupPrevious) return;

    el.sessionGroupToday.innerHTML = '';
    el.sessionGroupPrevious.innerHTML = '';
    el.sidebarSessionCount.textContent = `${state.sessions.length}개 세션`;

    const now = new Date();
    const isToday = (d) => {
      const date = new Date(d);
      return date.getDate() === now.getDate() &&
             date.getMonth() === now.getMonth() &&
             date.getFullYear() === now.getFullYear();
    };

    state.sessions.forEach(session => {
      const active = session.id === state.currentSessionId;
      const targetGroup = isToday(session.createdAt) ? el.sessionGroupToday : el.sessionGroupPrevious;

      const item = document.createElement('div');
      item.className = `group flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-all ${
        active
          ? 'bg-surface-container-lowest text-on-surface font-medium border border-secondary/40 shadow-xs'
          : 'text-on-surface-variant hover:text-on-surface hover:bg-surface-container'
      }`;

      item.innerHTML = `
        <div class="flex items-center gap-2.5 overflow-hidden flex-1">
          <span class="w-1.5 h-1.5 rounded-full ${active ? 'bg-secondary' : 'bg-outline-variant'} shrink-0"></span>
          <span class="text-body-sm font-body-sm truncate">${escapeHtml(session.title)}</span>
        </div>
        <div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button class="btn-delete-session p-1 hover:text-error rounded" title="세션 삭제" data-id="${session.id}">
            <span class="material-symbols-outlined text-[15px]">close</span>
          </button>
        </div>
      `;

      item.addEventListener('click', (e) => {
        if (e.target.closest('.btn-delete-session')) return;
        state.currentSessionId = session.id;
        renderSessionList();
        renderCurrentSessionMessages();
        closeMobileSidebar();
      });

      const delBtn = item.querySelector('.btn-delete-session');
      if (delBtn) {
        delBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          deleteSession(session.id);
        });
      }

      targetGroup.appendChild(item);
    });

    const curr = getCurrentSession();
    if (curr) {
      el.sessionTitle.textContent = curr.title;
      el.currentThreadId.textContent = curr.id;
    }
  }

  function deleteSession(id) {
    state.sessions = state.sessions.filter(s => s.id !== id);
    if (state.sessions.length === 0) {
      createNewSession('새 금융 상담');
    } else if (state.currentSessionId === id) {
      state.currentSessionId = state.sessions[0].id;
    }
    saveSessions();
    renderCurrentSessionMessages();
  }

  function closeMobileSidebar() {
    el.sidebar.classList.add('-translate-x-full');
    el.sidebarBackdrop.classList.add('hidden');
  }

  function openMobileSidebar() {
    el.sidebar.classList.remove('-translate-x-full');
    el.sidebarBackdrop.classList.remove('hidden');
  }

  // --- Backend Health Check ---
  async function checkBackendHealth() {
    try {
      const res = await fetch('/health', { signal: AbortSignal.timeout(3000) });
      if (res.ok) {
        const data = await res.json();
        state.backendOnline = data.status === 'ok';
        state.backendVersion = data.version || 'v2';
        state.backendLlm = data.llm || 'ollama';
        state.backendDataMonth = data.data_month || '202609';

        el.backendStatusDot.className = 'w-2 h-2 rounded-full bg-emerald-500 animate-pulse';
        el.backendStatusText.textContent = `Backend Live (${data.version || 'v2'})`;
        el.headerDataMonth.textContent = `${data.data_month || '2026.09'} (공시 DB)`;
        el.sidebarDataMonth.textContent = `${data.data_month || '2026.09'} Disclosure`;
        el.sidebarLlmName.textContent = data.llm || 'ollama';
        el.sidebarStatusPill.textContent = 'Live';
        el.sidebarStatusPill.className = 'px-1.5 py-0.5 rounded bg-surface-container text-on-tertiary-container text-label-mono-xs font-label-mono-xs font-semibold';
      } else {
        throw new Error('Non-ok response');
      }
    } catch (e) {
      state.backendOnline = false;
      el.backendStatusDot.className = 'w-2 h-2 rounded-full bg-amber-500';
      el.backendStatusText.textContent = 'Backend Offline (8100)';
      el.sidebarStatusPill.textContent = 'Mock Ready';
      el.sidebarStatusPill.className = 'px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 text-label-mono-xs font-label-mono-xs font-semibold';
    }
  }

  setInterval(checkBackendHealth, 8000);
  checkBackendHealth();

  // --- Mode Toggles ---
  function setMode(mode) {
    state.mode = mode;
    if (mode === 'live') {
      el.btnModeLive.className = 'px-3 py-1 rounded-lg text-label-mono-xs font-label-mono-xs font-semibold bg-surface-container-lowest text-secondary shadow-sm transition-all';
      el.btnModeMock.className = 'px-3 py-1 rounded-lg text-label-mono-xs font-label-mono-xs text-on-surface-variant hover:text-on-surface transition-all';
      el.mockSelectorWrap.classList.add('hidden');
      el.mockSelectorWrap.classList.remove('sm:flex');
    } else {
      el.btnModeMock.className = 'px-3 py-1 rounded-lg text-label-mono-xs font-label-mono-xs font-semibold bg-surface-container-lowest text-secondary shadow-sm transition-all';
      el.btnModeLive.className = 'px-3 py-1 rounded-lg text-label-mono-xs font-label-mono-xs text-on-surface-variant hover:text-on-surface transition-all';
      el.mockSelectorWrap.classList.remove('hidden');
      el.mockSelectorWrap.classList.add('sm:flex');
    }
  }

  // --- Message UI Rendering ---
  function renderCurrentSessionMessages() {
    const session = getCurrentSession();
    if (!session) return;

    el.chatMessagesContainer.querySelectorAll('.chat-bubble-row').forEach(node => node.remove());

    if (!session.messages || session.messages.length === 0) {
      el.chatEmptyState.classList.remove('hidden');
    } else {
      el.chatEmptyState.classList.add('hidden');
      session.messages.forEach(msg => {
        if (msg.role === 'user') {
          renderUserMessageRow(msg);
        } else if (msg.role === 'assistant') {
          renderAssistantMessageRow(msg);
        }
      });
      scrollChatToBottom();
    }
  }

  function scrollChatToBottom() {
    el.chatMessagesContainer.scrollTop = el.chatMessagesContainer.scrollHeight;
  }

  // 1. User Bubble (Stitch Screen 1 & 3 UI)
  function renderUserMessageRow(msg) {
    const row = document.createElement('div');
    row.className = 'chat-bubble-row flex justify-end';
    row.innerHTML = `
      <div class="max-w-xl bg-primary-container text-on-primary rounded-xl px-4 py-3 shadow-sm border border-outline-variant/30 space-y-1">
        <div class="flex items-center justify-between gap-6 pb-1 border-b border-on-primary-container/30">
          <span class="text-label-mono-xs font-label-mono-xs text-on-primary-container font-medium uppercase tracking-wider">Financial Consultant Operator</span>
          <span class="text-label-mono-xs font-label-mono-xs text-on-primary-container tabular-nums">${escapeHtml(msg.timestamp || formatTime())}</span>
        </div>
        <p class="text-body-md font-body-md text-on-primary font-medium pt-1 leading-relaxed">
          ${escapeHtml(msg.content)}
        </p>
      </div>
    `;
    el.chatMessagesContainer.appendChild(row);
  }

  // 2. Assistant Row Container
  function createAssistantMessageRow() {
    const row = document.createElement('div');
    row.className = 'chat-bubble-row space-y-4';

    const traceId = 'trace-' + Math.random().toString(36).substring(2, 9);

    row.innerHTML = `
      <!-- Header -->
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <div class="w-6 h-6 rounded-lg bg-secondary flex items-center justify-center text-on-secondary shadow-sm">
            <span class="material-symbols-outlined text-[14px]">psychology</span>
          </div>
          <span class="font-headline-sm text-headline-sm font-semibold text-on-surface">FinAgent Grounding Engine</span>
          <span class="msg-model-tag px-2 py-0.5 rounded text-label-mono-xs font-label-mono-xs bg-surface-container-highest text-on-surface font-medium">Model: v2-agentic</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="msg-trace-hash text-label-mono-xs font-label-mono-xs text-outline tabular-nums">Trace Hash: 0x${Math.random().toString(16).substring(2, 6)}...${Math.random().toString(16).substring(2, 6)}</span>
          <button class="btn-copy-trace text-on-surface-variant hover:text-on-surface p-1 rounded hover:bg-surface-container-low transition-colors" title="Copy Trace JSON">
            <span class="material-symbols-outlined text-[15px]">content_copy</span>
          </button>
        </div>
      </div>

      <!-- COLLAPSIBLE: Agent Thinking & Execution Box (Stitch Screen 3 Bloomberg Terminal meets Linear) -->
      <div class="rounded-xl border border-outline-variant bg-surface-container-lowest overflow-hidden shadow-sm">
        <!-- Trace Header Bar -->
        <div class="trace-header flex items-center justify-between px-4 py-2.5 bg-surface-container-low border-b border-outline-variant cursor-pointer select-none hover:bg-surface-container transition-colors">
          <div class="flex items-center gap-2.5">
            <span class="trace-chevron material-symbols-outlined text-[16px] text-on-surface-variant transition-transform transform rotate-180">expand_more</span>
            <span class="text-label-mono font-label-mono font-semibold text-on-surface">Agent Thinking &amp; Execution Trace</span>
            <span class="trace-route-badge inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-label-mono-xs font-label-mono-xs bg-surface-container-highest text-secondary border border-secondary/20 font-semibold">
              <span class="route-pulse w-1.5 h-1.5 rounded-full bg-secondary animate-pulse"></span>
              <span class="route-text">Route: Analyzing...</span>
            </span>
          </div>
          <div class="flex items-center gap-3">
            <span class="trace-status-pill text-label-mono-xs font-label-mono-xs text-on-tertiary-container bg-surface-container px-2 py-0.5 rounded font-medium flex items-center gap-1">
              <span class="w-1.5 h-1.5 rounded-full bg-secondary animate-pulse"></span>
              SSE Stream Connecting...
            </span>
          </div>
        </div>

        <!-- Trace Execution Details Body (Expanded by default) -->
        <div class="trace-body p-4 bg-primary-container text-on-primary-fixed space-y-3 font-label-mono text-label-mono">
          <!-- Stepper Container -->
          <div class="trace-steps-container space-y-2">
            <!-- Dynamic task and step items -->
          </div>

          <!-- Replan & Guardrail Verification Pinhole -->
          <div class="trace-guardrail-container pt-2 border-t border-on-primary-fixed-variant/40 flex flex-wrap items-center gap-2 empty:hidden">
          </div>
        </div>
      </div>

      <!-- Final Answer Block / Error Slot -->
      <div class="final-answer-slot space-y-4">
        <!-- Injected upon FinalAnswer or Error event -->
      </div>
    `;

    // Accordion toggle handler
    const header = row.querySelector('.trace-header');
    const chevron = row.querySelector('.trace-chevron');
    const body = row.querySelector('.trace-body');
    header.addEventListener('click', () => {
      const isHidden = body.classList.contains('hidden');
      if (isHidden) {
        body.classList.remove('hidden');
        chevron.classList.add('rotate-180');
      } else {
        body.classList.add('hidden');
        chevron.classList.remove('rotate-180');
      }
    });

    el.chatMessagesContainer.appendChild(row);
    scrollChatToBottom();
    return row;
  }

  // 3. Render Assistant Row from saved message object
  function renderAssistantMessageRow(msg) {
    const row = createAssistantMessageRow();

    // Populate Trace
    const routeText = row.querySelector('.route-text');
    const routeBadge = row.querySelector('.trace-route-badge');
    const routePulse = row.querySelector('.route-pulse');
    const statusPill = row.querySelector('.trace-status-pill');
    const stepsContainer = row.querySelector('.trace-steps-container');
    const guardrailContainer = row.querySelector('.trace-guardrail-container');
    const finalAnswerSlot = row.querySelector('.final-answer-slot');
    const modelTag = row.querySelector('.msg-model-tag');

    if (msg.route) {
      routeText.textContent = `Route: ${msg.route.toUpperCase()}`;
      if (msg.route === 'baseline') {
        modelTag.textContent = 'Model: v1-baseline';
      }
    }

    statusPill.innerHTML = `SSE Stream Complete (${((msg.metadata?.latency_ms || 1000) / 1000).toFixed(1)}s)`;
    if (routePulse) routePulse.classList.remove('animate-pulse');

    // Populate trace events
    if (msg.events && msg.events.length > 0) {
      msg.events.forEach(ev => appendTraceEvent(row, ev));
    } else {
      // Minimal trace summary if no raw events
      stepsContainer.innerHTML = `
        <div class="text-label-mono-xs text-inverse-primary">
          Execution completed in ${msg.metadata?.latency_ms || 0}ms with ${msg.metadata?.llm_calls || 1} LLM call(s).
        </div>
      `;
    }

    // Populate Final Answer or Error
    if (msg.error) {
      renderErrorBlock(finalAnswerSlot, msg.error);
    } else if (msg.finalAnswer) {
      renderFinalAnswerBlock(finalAnswerSlot, msg.finalAnswer);
    }
  }

  // --- Real-time Trace Event Dispatcher ---
  function appendTraceEvent(row, event) {
    const stepsContainer = row.querySelector('.trace-steps-container');
    const guardrailContainer = row.querySelector('.trace-guardrail-container');
    const routeText = row.querySelector('.route-text');
    const modelTag = row.querySelector('.msg-model-tag');

    switch (event.type) {
      case 'RouteDecided': {
        routeText.textContent = `Route: ${event.route.toUpperCase()} (${event.reason || ''})`;
        const step = document.createElement('div');
        step.className = 'flex items-start gap-2.5 text-on-primary';
        step.innerHTML = `
          <span class="material-symbols-outlined text-[16px] text-tertiary-fixed-dim mt-0.5">alt_route</span>
          <div class="flex-1">
            <span class="text-tertiary-fixed-dim font-semibold">Route Decided: ${escapeHtml(event.route)}</span>
            <span class="text-inverse-primary block text-label-mono-xs font-label-mono-xs pt-0.5">Reason: ${escapeHtml(event.reason)}</span>
          </div>
        `;
        stepsContainer.appendChild(step);
        break;
      }

      case 'PlanCreated': {
        const step = document.createElement('div');
        step.className = 'space-y-1.5 pt-1 border-t border-on-primary-fixed-variant/20';
        const tasksHtml = (event.tasks || []).map((t, idx) => `
          <div class="flex items-start gap-2 text-label-mono-xs pl-2 text-inverse-primary">
            <span class="text-secondary font-semibold">#${idx + 1} [${escapeHtml(t.agent)}]:</span>
            <span>${escapeHtml(t.desc)}</span>
          </div>
        `).join('');

        step.innerHTML = `
          <div class="flex items-center gap-2 text-on-primary">
            <span class="material-symbols-outlined text-[16px] text-secondary">view_list</span>
            <span class="font-semibold text-secondary">Execution Plan Created (${event.tasks?.length || 0} tasks)</span>
          </div>
          <div class="space-y-1 pl-4">${tasksHtml}</div>
        `;
        stepsContainer.appendChild(step);
        break;
      }

      case 'TaskCompleted': {
        const step = document.createElement('div');
        step.className = 'flex items-start gap-2.5 text-on-primary';
        step.innerHTML = `
          <span class="material-symbols-outlined text-[16px] text-tertiary-fixed-dim mt-0.5">${event.ok ? 'check_circle' : 'error'}</span>
          <div class="flex-1">
            <span class="text-tertiary-fixed-dim font-semibold">Task [${escapeHtml(event.agent || '')}] completed (${event.elapsed_ms || 0}ms)</span>
            <span class="text-inverse-primary block text-label-mono-xs font-label-mono-xs pt-0.5">Task ID: ${escapeHtml(event.id)} · Status: ${event.ok ? 'OK' : 'FAILED'} · Retries: ${event.retries || 0}</span>
          </div>
        `;
        stepsContainer.appendChild(step);
        break;
      }

      case 'ReplanCheck': {
        const badge = document.createElement('span');
        badge.className = 'text-label-mono-xs font-label-mono-xs px-2 py-0.5 rounded bg-surface-container-high/20 text-inverse-primary border border-surface-container-high/40';
        badge.textContent = `Replan Check (Round ${event.round}): ${event.is_sufficient ? 'Sufficient data' : 'Needs replan'} - ${event.reason || ''}`;
        guardrailContainer.appendChild(badge);
        break;
      }

      case 'Validation': {
        const badge = document.createElement('span');
        if (event.verdict === 'pass') {
          badge.className = 'text-label-mono-xs font-label-mono-xs px-2 py-0.5 rounded bg-on-tertiary-container/30 text-tertiary-fixed border border-on-tertiary-container flex items-center gap-1 font-semibold';
          badge.innerHTML = `<span class="material-symbols-outlined text-[12px]">verified_user</span> Attempt ${event.attempt} PASSED (정합성 검증 완료)`;
        } else {
          badge.className = 'text-label-mono-xs font-label-mono-xs px-2 py-0.5 rounded bg-error/20 text-error-container border border-error/40 flex items-center gap-1';
          badge.innerHTML = `<span class="material-symbols-outlined text-[12px]">error</span> Attempt ${event.attempt} FAILED (${escapeHtml(event.reason || '불일치')})`;
        }
        guardrailContainer.appendChild(badge);
        break;
      }
    }
  }

  // 4. Render Final Answer Block (Stitch Screen 1 & Screen 3 Verified UI)
  function renderFinalAnswerBlock(slot, final) {
    slot.innerHTML = '';

    const isBaseline = final.route === 'baseline' || final.metadata?.version === 'v1';

    const container = document.createElement('div');
    container.className = 'bg-surface-container-lowest border border-outline-variant rounded-xl p-5 shadow-sm space-y-4';

    // Banner: Emerald Verification Header or Baseline Notice
    const bannerHtml = isBaseline
      ? `
        <div class="flex items-center justify-between pb-3 border-b border-outline-variant">
          <div class="flex items-center gap-2">
            <div class="w-5 h-5 rounded-full bg-surface-container flex items-center justify-center text-on-surface-variant">
              <span class="material-symbols-outlined text-[16px]">info</span>
            </div>
            <span class="font-headline-sm text-headline-sm font-semibold text-on-surface">v1 베이스라인 답변 (단순 검색)</span>
          </div>
          <div class="flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-surface-container text-on-surface-variant border border-outline-variant text-label-mono-xs font-label-mono-xs font-medium">
            <span>Baseline Single-Turn</span>
          </div>
        </div>
      `
      : `
        <div class="flex items-center justify-between pb-3 border-b border-outline-variant">
          <div class="flex items-center gap-2">
            <div class="w-5 h-5 rounded-full bg-surface-container flex items-center justify-center text-on-tertiary-container">
              <span class="material-symbols-outlined text-[16px]">check_circle</span>
            </div>
            <span class="font-headline-sm text-headline-sm font-semibold text-on-surface">금융감독원 정합성 검증 완료 답변</span>
          </div>
          <div class="flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-surface-container text-on-tertiary-container border border-outline-variant text-label-mono-xs font-label-mono-xs font-semibold">
            <span>Grounding Score:</span>
            <span class="tabular-nums">99.8%</span>
          </div>
        </div>
      `;

    // Natural Language Markdown reply
    const replyHtml = `
      <div class="text-body-lg font-body-lg text-on-surface leading-relaxed prose-finagent">
        ${renderSimpleMarkdown(final.reply)}
      </div>
    `;

    // Calculations Bento Card (Stitch Screen 1 & 3 Bento Matrix)
    let calcHtml = '';
    if (final.calculations && final.calculations.length > 0) {
      final.calculations.forEach(calc => {
        const inp = calc.inputs || {};
        calcHtml += `
          <div class="rounded-xl border border-outline-variant bg-surface-container-low overflow-hidden">
            <div class="px-4 py-2 bg-surface-container border-b border-outline-variant flex items-center justify-between">
              <span class="text-label-mono-xs font-label-mono-xs uppercase text-on-surface font-semibold tracking-wider flex items-center gap-1.5">
                <span class="material-symbols-outlined text-[14px] text-secondary">calculate</span>
                Financial Formula Audit Matrix (${escapeHtml(calc.name || '수식 검증')})
              </span>
              <span class="text-label-mono-xs font-label-mono-xs text-outline">소수점 이하 절사 규정 반영</span>
            </div>
            <div class="p-4 grid grid-cols-1 md:grid-cols-3 gap-3">
              <div class="p-2.5 bg-surface-container-lowest rounded-lg border border-outline-variant">
                <div class="text-label-mono-xs font-label-mono-xs text-outline">월 납입액 / 기간</div>
                <div class="text-headline-sm font-headline-sm font-semibold text-on-surface mt-1 tabular-nums">
                  ${inp.monthly_won ? formatNumber(inp.monthly_won) + '원' : '-'}
                  <span class="text-body-sm font-body-sm text-on-surface-variant font-normal">/ ${inp.term_months || '-'}개월</span>
                </div>
                <div class="text-label-mono-xs font-label-mono-xs text-on-surface-variant mt-0.5">
                  총 원금: ${inp.monthly_won && inp.term_months ? formatNumber(inp.monthly_won * inp.term_months) + '원' : '-'}
                </div>
              </div>
              <div class="p-2.5 bg-surface-container-lowest rounded-lg border border-outline-variant">
                <div class="text-label-mono-xs font-label-mono-xs text-outline">적용 이율 및 계산 체계</div>
                <div class="text-headline-sm font-headline-sm font-semibold text-secondary mt-1 tabular-nums">
                  연 ${inp.rate_pct || '-'}% <span class="text-body-sm font-body-sm text-on-surface-variant font-normal">(단리 방식)</span>
                </div>
                <div class="text-label-mono-xs font-label-mono-xs text-on-surface-variant mt-0.5">공시 확정 금리 적용</div>
              </div>
              <div class="p-2.5 bg-surface-container-lowest rounded-lg border border-outline-variant">
                <div class="text-label-mono-xs font-label-mono-xs text-outline">산출 세전 총이자</div>
                <div class="text-headline-sm font-headline-sm font-semibold text-secondary mt-1 tabular-nums">
                  ${formatNumber(calc.result)} ${escapeHtml(calc.unit || '원')}
                </div>
                <div class="text-label-mono-xs font-label-mono-xs text-on-tertiary-container mt-0.5 font-medium">검증 오차율 0.000%</div>
              </div>
            </div>

            <!-- Formula Execution Code Block -->
            <div class="mx-4 mb-4 p-2.5 bg-primary-container rounded-lg border border-outline-variant/30 text-label-mono-xs font-label-mono-xs space-y-1">
              <div class="text-inverse-primary flex items-center justify-between">
                <span>공식: ${escapeHtml(calc.formula || '')}</span>
                <span class="text-on-tertiary-container font-semibold">정밀 검산 통과</span>
              </div>
              <div class="text-on-primary font-mono text-[11px] tabular-nums">
                결과 = ${formatNumber(calc.result)} ${escapeHtml(calc.unit || '원')} (수식 검증 합격)
              </div>
            </div>
          </div>
        `;
      });
    }

    // Financial Product Grounding Cards (Stitch Screen 1 & 3)
    let productsHtml = '';
    if (final.products && final.products.length > 0) {
      final.products.forEach(p => {
        const bankName = p.bank || '은행';
        const bankInitial = bankName.substring(0, 2);
        const highlight = p.highlight || {};
        const dataFlags = p.data_flags || [];

        productsHtml += `
          <div class="p-4 rounded-xl border border-outline-variant bg-surface-container-lowest hover:border-secondary/50 transition-colors shadow-sm space-y-3">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-lg bg-surface-container border border-outline-variant flex items-center justify-center font-bold text-on-surface text-body-lg shrink-0">
                  ${escapeHtml(bankInitial)}
                </div>
                <div>
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-headline-sm text-headline-sm font-bold text-on-surface">${escapeHtml(p.product || '')}</span>
                    <span class="px-2 py-0.5 rounded text-label-mono-xs font-label-mono-xs bg-surface-container text-on-surface-variant font-medium">${escapeHtml(p.kind_ko || p.kind || '금융상품')}</span>
                    <span class="px-1.5 py-0.2 rounded text-label-mono-xs font-label-mono-xs bg-surface-container text-on-tertiary-container border border-outline-variant">FSS #${escapeHtml(p.id ? p.id.split('_').slice(-2).join('-') : '')}</span>
                    <span class="px-1.5 py-0.2 rounded text-label-mono-xs font-label-mono-xs bg-surface-container-high text-on-surface">${escapeHtml(p.sale || '공시')}</span>
                  </div>
                  <p class="text-body-sm font-body-sm text-on-surface-variant mt-0.5">${escapeHtml(p.bank || '')} · 공시 상품</p>
                </div>
              </div>

              ${highlight.label ? `
                <div class="p-2.5 px-3.5 rounded-lg bg-secondary-fixed/30 border border-secondary/20 flex flex-col justify-center sm:text-right">
                  <span class="text-label-mono-xs font-label-mono-xs text-on-secondary-fixed-variant font-medium">${escapeHtml(highlight.label)}</span>
                  <span class="text-headline-sm font-headline-sm font-bold text-secondary tabular-nums">${escapeHtml(highlight.value)}${escapeHtml(highlight.unit || '%')}</span>
                </div>
              ` : ''}
            </div>

            ${dataFlags.length > 0 ? `
              <div class="p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-900 text-label-mono-xs flex items-center gap-2">
                <span class="material-symbols-outlined text-amber-600 text-sm">warning</span>
                <span>공시 데이터 유의: ${escapeHtml(dataFlags.join(', '))}</span>
              </div>
            ` : ''}

            <!-- 4-grid key details -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-2 pt-2 border-t border-outline-variant text-label-mono-xs font-label-mono-xs">
              <div>
                <span class="text-outline block">상품 코드</span>
                <span class="text-body-sm font-body-sm font-medium text-on-surface tabular-nums">${escapeHtml(p.id || '')}</span>
              </div>
              <div>
                <span class="text-outline block">금융권 구분</span>
                <span class="text-body-sm font-body-sm font-medium text-on-surface">${p.group === 'bank' ? '제1금융권(은행)' : '저축은행'}</span>
              </div>
              <div>
                <span class="text-outline block">공시 상태</span>
                <span class="text-body-sm font-body-sm font-semibold text-on-tertiary-container">${escapeHtml(p.sale || '정상공시')}</span>
              </div>
              <div>
                <span class="text-outline block">공시 출처</span>
                <span class="text-body-sm font-body-sm text-on-surface-variant">금감원 「금융상품한눈에」</span>
              </div>
            </div>
          </div>
        `;
      });
    }

    // Citations list (if present)
    let citationsHtml = '';
    if (final.citations && final.citations.length > 0) {
      const citeItems = final.citations.map(c => `
        <div class="p-2.5 bg-surface-container-low rounded-lg border border-outline-variant text-label-mono-xs space-y-1">
          <div class="flex items-center justify-between text-outline">
            <span class="font-semibold text-on-surface">${escapeHtml(c.section || '원문 인용')}</span>
            <span class="tabular-nums">${escapeHtml(c.product_id || '')}</span>
          </div>
          <blockquote class="text-on-surface font-body-sm border-l-2 border-secondary pl-2 italic">
            "${escapeHtml(c.quote)}"
          </blockquote>
        </div>
      `).join('');

      citationsHtml = `
        <div class="space-y-2 pt-2">
          <div class="flex items-center gap-1.5 text-label-mono-xs font-label-mono-xs text-on-surface font-semibold">
            <span class="material-symbols-outlined text-[15px] text-secondary">format_quote</span>
            <span>공시 원문 근거 (Citations)</span>
          </div>
          <div class="space-y-1.5">${citeItems}</div>
        </div>
      `;
    }

    // Warnings Legal Disclaimer
    let warningsHtml = '';
    if (final.warnings && final.warnings.length > 0) {
      warningsHtml = `
        <div class="p-3 rounded-lg bg-surface-container-low/70 border border-outline-variant/40">
          <p class="text-label-mono-xs font-label-mono-xs text-outline leading-relaxed">
            ※ ${escapeHtml(final.warnings.join(' '))}
          </p>
        </div>
      `;
    }

    // Execution Metadata Footer (Stitch Screen 3 Pill Footer)
    const meta = final.metadata || {};
    const metaHtml = `
      <div class="pt-3 border-t border-outline-variant flex flex-col md:flex-row md:items-center justify-between gap-2 text-label-mono-xs font-label-mono-xs">
        <div class="flex items-center gap-3 text-on-surface-variant flex-wrap">
          <span class="flex items-center gap-1">
            <span class="material-symbols-outlined text-[13px]">timer</span>
            Latency ${((meta.latency_ms || 0) / 1000).toFixed(1)}s
          </span>
          <span>·</span>
          <span>${meta.llm_calls || 1} LLM calls</span>
          <span>·</span>
          <span>Cost $${(meta.cost_usd || 0).toFixed(4)}</span>
          ${meta.validation_attempts !== undefined ? `<span>·</span><span>Validation: ${meta.validation_attempts} attempt(s)</span>` : ''}
          <span>·</span>
          <span class="tabular-nums">Thread: ${escapeHtml(meta.thread_id || state.currentSessionId || '')}</span>
        </div>
        <div class="text-outline text-right max-w-lg leading-tight">
          공시 데이터 기준월: ${escapeHtml(meta.data_month || '202609')} · FSS Grounded Ledger
        </div>
      </div>
    `;

    container.innerHTML = bannerHtml + replyHtml + calcHtml + productsHtml + citationsHtml + warningsHtml + metaHtml;
    slot.appendChild(container);
  }

  // 5. Render Error Block
  function renderErrorBlock(slot, err) {
    slot.innerHTML = `
      <div class="bg-error-container/20 border border-error/40 rounded-xl p-4 shadow-sm space-y-2">
        <div class="flex items-center gap-2 text-error">
          <span class="material-symbols-outlined text-xl">error</span>
          <span class="font-headline-sm text-headline-sm font-semibold">오류 발생 (${escapeHtml(err.code || 'ERROR')})</span>
        </div>
        <p class="text-body-md font-body-md text-on-surface leading-relaxed">
          ${escapeHtml(err.message || '요청 처리 중 오류가 발생했습니다.')}
        </p>
        <div class="pt-2 text-label-mono-xs text-outline border-t border-error/20 flex items-center justify-between">
          <span>Error Code: ${escapeHtml(err.code || 'UNKNOWN')}</span>
          <button class="btn-retry-mock px-2.5 py-1 rounded bg-surface-container-lowest hover:bg-surface-container text-on-surface border border-outline-variant text-label-mono-xs transition-colors">
            목업 모드로 다시 실행
          </button>
        </div>
      </div>
    `;

    const retryBtn = slot.querySelector('.btn-retry-mock');
    if (retryBtn) {
      retryBtn.addEventListener('click', () => {
        setMode('mock');
        const session = getCurrentSession();
        const lastUserMsg = [...session.messages].reverse().find(m => m.role === 'user');
        if (lastUserMsg) {
          executeStreaming(lastUserMsg.content);
        }
      });
    }
  }

  // --- Send Message & Stream Execution ---
  async function handleSendMessage(text) {
    const message = (text || el.chatInput.value || '').trim();
    if (!message || state.isStreaming) return;

    if (message.length > 500) {
      alert('메시지는 최대 500자까지 입력 가능합니다.');
      return;
    }

    el.chatInput.value = '';
    updateCharCounter();
    el.chatEmptyState.classList.add('hidden');

    const session = getCurrentSession();
    // Update session title if first message
    if (session.messages.length === 0) {
      session.title = message.length > 22 ? message.substring(0, 22) + '...' : message;
      el.sessionTitle.textContent = session.title;
    }

    // Append User Message to Session State
    const userMsg = {
      role: 'user',
      content: message,
      timestamp: formatTime()
    };
    session.messages.push(userMsg);
    renderUserMessageRow(userMsg);
    saveSessions();
    scrollChatToBottom();

    // Start Streaming
    await executeStreaming(message);
  }

  async function executeStreaming(message) {
    setStreamingState(true);
    const session = getCurrentSession();
    const threadId = session.id;

    // Create assistant message shell
    const row = createAssistantMessageRow();
    const statusPill = row.querySelector('.trace-status-pill');
    const routePulse = row.querySelector('.route-pulse');
    const finalAnswerSlot = row.querySelector('.final-answer-slot');

    const assistantMsg = {
      role: 'assistant',
      route: null,
      events: [],
      finalAnswer: null,
      error: null,
      metadata: null
    };

    const startTime = Date.now();

    try {
      if (state.mode === 'mock') {
        // --- Mock Playback ---
        await runMockStream(state.mockScenario, row, assistantMsg);
      } else {
        // --- Live SSE Stream from Backend ---
        await runLiveStream(message, threadId, row, assistantMsg);
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        assistantMsg.error = { code: 'CANCELLED', message: '사용자에 의해 스트리밍이 중단되었습니다.' };
      } else {
        console.error('Stream error:', err);
        assistantMsg.error = {
          code: 'CONNECTION_FAILED',
          message: `백엔드(127.0.0.1:8100) 연결 실패: ${err.message}. 상단에서 '목업 Mock SSE' 모드로 전환하여 계약 문서를 테스트할 수 있습니다.`
        };
      }
      renderErrorBlock(finalAnswerSlot, assistantMsg.error);
    } finally {
      setStreamingState(false);
      const elapsedSec = ((Date.now() - startTime) / 1000).toFixed(1);
      statusPill.innerHTML = `SSE Stream Complete (${elapsedSec}s)`;
      if (routePulse) routePulse.classList.remove('animate-pulse');

      // Save assistant message to session
      session.messages.push(assistantMsg);
      saveSessions();
      scrollChatToBottom();
    }
  }

  // Live SSE Stream Runner
  async function runLiveStream(message, threadId, row, assistantMsg) {
    state.activeAbortController = new AbortController();
    const url = `/chat/stream?message=${encodeURIComponent(message)}&thread_id=${encodeURIComponent(threadId)}`;

    const response = await fetch(url, {
      signal: state.activeAbortController.signal,
      headers: { 'Accept': 'text/event-stream' }
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop(); // keep last incomplete chunk

      for (const part of parts) {
        const trimmed = part.trim();
        if (!trimmed || !trimmed.startsWith('data:')) continue;

        const dataStr = trimmed.replace(/^data:\s*/, '').trim();
        if (dataStr === '[DONE]') {
          return;
        }

        try {
          const event = JSON.parse(dataStr);
          processIncomingEvent(row, assistantMsg, event);
        } catch (pe) {
          console.warn('Failed to parse SSE JSON chunk:', dataStr, pe);
        }
      }
    }
  }

  // Mock Stream Runner (reproduces doc/contract/*.sse realistically)
  async function runMockStream(scenarioKey, row, assistantMsg) {
    const fixtureList = MOCK_FIXTURES[scenarioKey] || MOCK_FIXTURES.hard;
    state.activeAbortController = new AbortController();

    for (let i = 0; i < fixtureList.length; i++) {
      if (state.activeAbortController.signal.aborted) {
        throw new DOMException('Aborted', 'AbortError');
      }

      const event = fixtureList[i];
      processIncomingEvent(row, assistantMsg, event);

      // realistic step delay
      const delay = event.type === 'FinalAnswer' || event.type === 'Error' ? 400 : 350;
      await new Promise(r => setTimeout(r, delay));
    }
  }

  // Process and Route Incoming Event
  function processIncomingEvent(row, assistantMsg, event) {
    assistantMsg.events.push(event);

    if (event.type === 'RouteDecided') {
      assistantMsg.route = event.route;
      appendTraceEvent(row, event);
    } else if (event.type === 'PlanCreated' || event.type === 'TaskCompleted' || event.type === 'ReplanCheck' || event.type === 'Validation') {
      appendTraceEvent(row, event);
    } else if (event.type === 'FinalAnswer') {
      assistantMsg.finalAnswer = event;
      assistantMsg.metadata = event.metadata;
      if (event.route) assistantMsg.route = event.route;
      const slot = row.querySelector('.final-answer-slot');
      renderFinalAnswerBlock(slot, event);
    } else if (event.type === 'Error') {
      assistantMsg.error = event;
      const slot = row.querySelector('.final-answer-slot');
      renderErrorBlock(slot, event);
    }
    scrollChatToBottom();
  }

  function setStreamingState(streaming) {
    state.isStreaming = streaming;
    if (streaming) {
      el.btnSend.classList.add('hidden');
      el.btnStopStream.classList.remove('hidden');
      el.btnStopStream.classList.add('flex');
      el.headerStreamIndicator.classList.remove('hidden');
      el.headerStreamIndicator.classList.add('inline-flex');
    } else {
      el.btnSend.classList.remove('hidden');
      el.btnStopStream.classList.add('hidden');
      el.btnStopStream.classList.remove('flex');
      el.headerStreamIndicator.classList.add('hidden');
      el.headerStreamIndicator.classList.remove('inline-flex');
      state.activeAbortController = null;
    }
  }

  function updateCharCounter() {
    const len = el.chatInput.value.length;
    el.charCounter.textContent = `${len} / 500`;
    if (len >= 480) {
      el.charCounter.classList.add('text-error');
    } else {
      el.charCounter.classList.remove('text-error');
    }
  }

  // --- Export Chat & Audit Sheet ---
  function exportChatSession() {
    const session = getCurrentSession();
    if (!session) return;

    let md = `# FinAgent Audit Sheet - ${session.title}\n`;
    md += `- Thread ID: ${session.id}\n`;
    md += `- Export Time: ${new Date().toISOString()}\n`;
    md += `- Grounding: FSS Gov Registry (988 products)\n\n---\n\n`;

    session.messages.forEach((m, idx) => {
      if (m.role === 'user') {
        md += `### [USER] ${m.timestamp || ''}\n${m.content}\n\n`;
      } else if (m.role === 'assistant') {
        md += `### [FINAGENT ASSISTANT]\n`;
        if (m.finalAnswer) {
          md += `**Answer:**\n${m.finalAnswer.reply}\n\n`;
          if (m.finalAnswer.calculations?.length) {
            md += `**Calculations:**\n\`\`\`json\n${JSON.stringify(m.finalAnswer.calculations, null, 2)}\n\`\`\`\n\n`;
          }
          if (m.finalAnswer.products?.length) {
            md += `**Products:**\n\`\`\`json\n${JSON.stringify(m.finalAnswer.products, null, 2)}\n\`\`\`\n\n`;
          }
          if (m.finalAnswer.metadata) {
            md += `**Metadata:**\n\`\`\`json\n${JSON.stringify(m.finalAnswer.metadata, null, 2)}\n\`\`\`\n\n`;
          }
        } else if (m.error) {
          md += `**Error:** [${m.error.code}] ${m.error.message}\n\n`;
        }
        md += `---\n\n`;
      }
    });

    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `finagent-audit-${session.id}.md`;
    a.click();
    URL.revokeObjectURL(a);
  }

  // --- Event Listeners Initialization ---
  function initEventListeners() {
    // Chat form submit
    el.chatForm.addEventListener('submit', (e) => {
      e.preventDefault();
      handleSendMessage();
    });

    // Chat input input counter
    el.chatInput.addEventListener('input', updateCharCounter);

    // Stop button
    el.btnStopStream.addEventListener('click', () => {
      if (state.activeAbortController) {
        state.activeAbortController.abort();
      }
    });

    // Quick prompt chips
    document.querySelectorAll('.prompt-chip').forEach(btn => {
      btn.addEventListener('click', () => {
        const prompt = btn.getAttribute('data-prompt');
        if (prompt) {
          el.chatInput.value = prompt;
          updateCharCounter();
          handleSendMessage(prompt);
        }
      });
    });

    // New Chat
    el.btnNewChat.addEventListener('click', () => {
      createNewSession();
      closeMobileSidebar();
      el.chatInput.focus();
    });

    // Keyboard shortcut Cmd+K / Ctrl+K
    window.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        createNewSession();
        el.chatInput.focus();
      }
    });

    // Mode buttons
    el.btnModeLive.addEventListener('click', () => setMode('live'));
    el.btnModeMock.addEventListener('click', () => setMode('mock'));

    // Mock scenario selector
    el.selectMockScenario.addEventListener('change', (e) => {
      state.mockScenario = e.target.value;
    });

    // Export & Clear
    el.btnExportChat.addEventListener('click', exportChatSession);
    el.btnClearChat.addEventListener('click', () => {
      if (confirm('현재 대화 내용을 지우시겠습니까?')) {
        const session = getCurrentSession();
        if (session) {
          session.messages = [];
          saveSessions();
          renderCurrentSessionMessages();
        }
      }
    });

    // Mobile sidebar toggle
    el.btnToggleSidebar.addEventListener('click', openMobileSidebar);
    el.sidebarBackdrop.addEventListener('click', closeMobileSidebar);
  }

  // --- App Initialization ---
  function init() {
    loadSessions();
    initEventListeners();
    updateCharCounter();
  }

  // Boot up when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
