<template>
  <section
    v-if="visibleSteps.length"
    class="thinking-timeline"
    :class="{ 'is-embedded': embedded }"
    aria-label="思考过程"
  >
    <div v-if="!embedded" class="thinking-timeline__head">
      <span class="thinking-timeline__title">思考过程</span>
      <span class="thinking-timeline__count">{{ completedCount }}/{{ visibleSteps.length }}</span>
    </div>
    <ol class="thinking-timeline__list">
      <li
        v-for="step in visibleSteps"
        :key="step.step_id"
        class="thinking-timeline__item"
        :class="`is-${step.state || 'started'}`"
      >
        <span class="thinking-timeline__dot" aria-hidden="true"></span>
        <div class="thinking-timeline__body">
          <div class="thinking-timeline__row">
            <span class="thinking-timeline__step-title">{{ step.title || step.step_id }}</span>
            <span v-if="step.state === 'waiting'" class="thinking-timeline__badge">等待确认</span>
            <span v-else-if="step.state === 'failed'" class="thinking-timeline__badge is-error">失败</span>
            <span v-else-if="step.state === 'skipped'" class="thinking-timeline__badge">跳过</span>
            <span v-else-if="step.elapsed_ms" class="thinking-timeline__elapsed">{{ formatDuration(step.elapsed_ms) }}</span>
          </div>
          <p v-if="step.content" class="thinking-timeline__content">{{ step.content }}</p>
        </div>
      </li>
    </ol>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  steps: { type: Array, default: () => [] },
  embedded: { type: Boolean, default: false }
})

const visibleSteps = computed(() =>
  [...(props.steps || [])]
    .filter((step) => step && (step.step_id || step.title))
    .sort((left, right) => Number(left.step_index || 0) - Number(right.step_index || 0))
)

const completedCount = computed(
  () => visibleSteps.value.filter((step) => ['completed', 'failed', 'skipped'].includes(step.state)).length
)

const formatDuration = (value) => {
  const ms = Number(value)
  if (!Number.isFinite(ms) || ms <= 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
</script>

<style scoped>
.thinking-timeline {
  margin: 8px 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--gray-150, #e5e7eb);
  border-radius: 10px;
  background: var(--gray-25, #fafafa);
}

.thinking-timeline.is-embedded {
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
}

.thinking-timeline__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.thinking-timeline__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--gray-700, #374151);
}

.thinking-timeline__count {
  font-size: 11px;
  color: var(--gray-500, #6b7280);
}

.thinking-timeline__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.thinking-timeline__item {
  display: flex;
  gap: 8px;
  min-width: 0;
}

.thinking-timeline__dot {
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--gray-300, #d1d5db);
  flex: 0 0 auto;
}

.thinking-timeline__item.is-started .thinking-timeline__dot,
.thinking-timeline__item.is-waiting .thinking-timeline__dot {
  background: var(--primary-color, #006f58);
  animation: thinking-pulse 1.2s ease-in-out infinite;
}

.thinking-timeline__item.is-pending .thinking-timeline__dot {
  background: var(--gray-300, #d1d5db);
}

.thinking-timeline__item.is-completed .thinking-timeline__dot {
  background: var(--success-color, #16a34a);
}

.thinking-timeline__item.is-failed .thinking-timeline__dot {
  background: var(--error-color, #dc2626);
}

.thinking-timeline__body {
  min-width: 0;
}

.thinking-timeline__row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.thinking-timeline__step-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--gray-800, #1f2937);
}

.thinking-timeline__badge {
  padding: 0 5px;
  border-radius: 4px;
  font-size: 10px;
  line-height: 16px;
  color: var(--primary-color, #006f58);
  background: rgba(0, 111, 88, 0.08);
}

.thinking-timeline__badge.is-error {
  color: var(--error-color, #dc2626);
  background: rgba(220, 38, 38, 0.08);
}

.thinking-timeline__elapsed {
  font-size: 10px;
  color: var(--gray-400, #9ca3af);
}

.thinking-timeline__content {
  margin: 2px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--gray-600, #4b5563);
  word-break: break-word;
}

@keyframes thinking-pulse {
  0%,
  100% {
    opacity: 0.4;
  }
  50% {
    opacity: 1;
  }
}
</style>
