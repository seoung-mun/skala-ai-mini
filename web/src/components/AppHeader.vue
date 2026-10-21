<script setup>
defineProps({
  view: { type: String, required: true },
  sidebarOpen: { type: Boolean, required: true },
  health: { type: Object, required: true },
  status: { type: String, required: true },
  month: { type: String, required: true },
  streaming: { type: Boolean, required: true },
});
defineEmits([
  "toggle-sidebar",
  "home",
  "change-view",
  "refresh",
  "export",
  "clear",
]);
</script>

<template>
  <header class="topbar">
    <div class="brand-group">
      <button
        id="btn-toggle-sidebar"
        class="icon-button mobile-menu"
        aria-label="대화 기록 열기"
        aria-controls="sidebar"
        :aria-expanded="sidebarOpen"
        @click="$emit('toggle-sidebar')"
      >
        <span class="material-symbols-outlined">menu</span>
      </button>
      <a
        class="brand"
        href="#chat-messages-container"
        aria-label="FinAgent 대화로 이동"
        @click.prevent="$emit('home')"
        ><span class="brand-icon material-symbols-outlined"
          >account_balance</span
        ><span
          ><span class="brand-title"
            >FinAgent
            <span class="institutional-badge">공시 기반 금융 상담</span></span
          ><span class="brand-subtitle"
            >금융감독원 공시 데이터 기반 상담</span
          ></span
        ></a
      >
      <nav
        class="top-nav"
        aria-label="대화 탐색"
      >
        <button
          v-for="tab in [
            { key: 'chat', label: '대화' },
            { key: 'traces', label: '분석 과정' },
            { key: 'citations', label: '출처' },
          ]"
          :key="tab.key"
          :id="'btn-show-' + tab.key"
          :aria-current="view === tab.key ? 'page' : null"
          :aria-controls="
            tab.key === 'chat' ? 'chat-messages-container' : 'session-records'
          "
          @click="$emit('change-view', tab.key)"
        >
          {{ tab.label }}
        </button>
      </nav>
    </div>
    <div class="header-actions">
      <div
        class="connection-badge"
        role="status"
      >
        <span
          class="status-dot"
          :class="health.online ? 'online' : 'pending'"
        ></span
        ><span>{{ status }}</span
        ><span class="connection-month">{{ month }} 공시</span>
      </div>
      <div class="header-tools">
        <button
          id="btn-sync"
          class="icon-button"
          title="연결 상태 새로고침"
          aria-label="연결 상태 새로고침"
          @click="$emit('refresh')"
        >
          <span class="material-symbols-outlined">sync</span></button
        ><button
          id="btn-export-chat"
          class="icon-button"
          title="대화 내보내기"
          aria-label="대화 내보내기"
          :disabled="streaming"
          @click="$emit('export')"
        >
          <span class="material-symbols-outlined">download</span></button
        ><button
          id="btn-clear-chat"
          class="icon-button"
          title="현재 대화 지우기"
          aria-label="현재 대화 지우기"
          :disabled="streaming"
          @click="$emit('clear')"
        >
          <span class="material-symbols-outlined">delete_outline</span>
        </button>
      </div>
      <span
        class="user-avatar"
        aria-label="내 프로필"
        >나</span
      >
    </div>
  </header>
</template>
