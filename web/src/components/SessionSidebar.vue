<script setup>
defineProps({
  sessions: { type: Array, required: true },
  currentId: { type: String, default: null },
  groups: { type: Array, required: true },
  sidebarOpen: { type: Boolean, required: true },
  health: { type: Object, required: true },
  status: { type: String, required: true },
  month: { type: String, required: true },
  clock: { type: String, required: true },
  streaming: { type: Boolean, required: true },
});
defineEmits(["new-session", "select-session", "delete-session"]);
</script>

<template>
  <aside
    id="sidebar"
    class="sidebar"
    :class="{ 'is-closed': !sidebarOpen }"
    aria-label="대화 기록"
  >
    <div class="sidebar-top">
      <button
        id="btn-new-chat"
        class="new-chat"
        :disabled="streaming"
        @click="$emit('new-session')"
      >
        <span
          ><span class="material-symbols-outlined">add</span>새 대화 시작</span
        ><kbd>⌘K</kbd>
      </button>
      <div class="history-label">
        <span
          ><span class="material-symbols-outlined">history</span>대화 기록</span
        ><span>{{ sessions.length }}개 세션</span>
      </div>
    </div>
    <div class="history-list custom-scrollbar">
      <div
        v-for="group in groups"
        :key="group.label"
      >
        <h2>{{ group.label }}</h2>
        <div
          v-for="item in group.items"
          :key="item.id"
          class="session-item"
          :class="{ active: currentId === item.id }"
        >
          <button
            class="session-select"
            :disabled="streaming"
            :aria-current="currentId === item.id ? 'true' : null"
            @click="$emit('select-session', item.id)"
          >
            {{ item.title }}</button
          ><button
            class="btn-delete-session icon-button"
            :disabled="streaming"
            aria-label="대화 기록 삭제"
            title="대화 기록 삭제"
            @click="$emit('delete-session', item.id)"
          >
            <span class="material-symbols-outlined">close</span>
          </button>
        </div>
      </div>
    </div>
    <div class="sidebar-bottom">
      <div class="system-card">
        <div class="system-card-title">
          <span
            ><span
              class="status-dot"
              :class="health.online ? 'online' : 'pending'"
            ></span
            >{{ status }}</span
          ><span class="small-badge">{{
            health.online ? "연결됨" : "미연결"
          }}</span>
        </div>
        <dl>
          <div>
            <dt>공시 기준월</dt>
            <dd>{{ month }}</dd>
          </div>
          <div>
            <dt>조회 방식</dt>
            <dd class="green-text">공시 기반 조회</dd>
          </div>
          <div>
            <dt>응답 모델</dt>
            <dd>{{ health.llm || "연결 안 됨" }}</dd>
          </div>
        </dl>
      </div>
      <div class="sidebar-footnote">
        <span
          ><span class="material-symbols-outlined">verified_user</span
          >금융감독원 공시 기반</span
        ><span>{{ clock }}</span>
      </div>
    </div>
  </aside>
</template>
