<template>
  <BaseToolCall :tool-call="toolCall" :hide-params="true">
    <template #header>
      <div class="spec-header">
        <span class="spec-title">电力规范检索</span>
        <span class="separator" v-if="summary.query">|</span>
        <span class="spec-query">{{ summaryText }}</span>
      </div>
    </template>

    <template #result>
      <div class="spec-result">
        <div v-if="summary.notice" class="spec-notice">{{ summary.notice }}</div>

        <div v-else class="spec-summary">
          <span class="spec-count">共 {{ cards.length }} 条相关条款</span>
          <span v-if="summary.retrievalMode" class="spec-mode">{{ summary.retrievalMode }}</span>
          <span v-if="summary.reviewIssues" class="spec-reviewed">已结合审查意见</span>
        </div>

        <article v-for="(card, index) in cards" :key="`spec-card-${index}`" class="result-card">
          <div class="rc-head">
            <span class="rc-standard">{{ card.standard_name }}</span>
            <span class="rc-no">{{ card.standard_no }}</span>
            <span class="rc-badge">条款 {{ card.clause_no }}</span>
          </div>

          <div class="rc-body" v-html="card.clause_body"></div>
        </article>
      </div>
    </template>
  </BaseToolCall>
</template>

<script setup>
import { computed } from 'vue'
import BaseToolCall from '../BaseToolCall.vue'

const props = defineProps({
  toolCall: {
    type: Object,
    required: true
  },
  appearance: {
    type: String,
    default: 'card'
  },
  defaultExpanded: {
    type: Boolean,
    default: false
  }
})

const PLAIN_MESSAGES = {
  未检索到资料: '未检索到资料',
  电力规范知识库尚未完成条款级重建: '电力规范知识库尚未完成条款级重建',
  多次未确认问题意图: '多次未确认问题意图，请重新描述您的电力规范问题'
}

let lastContent = null
let lastParsed = null

const parseResult = (content) => {
  if (content === lastContent) return lastParsed

  let payload = content
  if (typeof content === 'string') {
    const trimmed = content.trim()
    if (PLAIN_MESSAGES[trimmed]) {
      lastContent = content
      lastParsed = { plain: trimmed }
      return lastParsed
    }
    try {
      payload = JSON.parse(trimmed)
    } catch {
      lastContent = content
      lastParsed = { plain: trimmed }
      return lastParsed
    }
  }

  lastContent = content
  lastParsed = payload && typeof payload === 'object' ? payload : { plain: String(content || '') }
  return lastParsed
}

const parsed = computed(() => parseResult(props.toolCall.tool_call_result?.content) || {})

const cards = computed(() => {
  const items = parsed.value.spec_results
  return Array.isArray(items) ? items : []
})

const summaryText = computed(() => {
  const plan = parsed.value.intent_plan
  if (plan && plan.normalized_query) {
    return `意图：${plan.normalized_query}`
  }
  if (parsed.value.query) {
    return String(parsed.value.query)
  }
  if (parsed.value.plain) {
    return parsed.value.plain
  }
  return '正在检索电力规范'
})

const summary = computed(() => ({
  query: summaryText.value,
  notice: parsed.value.plain || '',
  retrievalMode: parsed.value.retrieval_mode || '',
  reviewIssues: parsed.value.answer_verified === false
}))
</script>

<style scoped lang="less">
.spec-header {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;

  .spec-title {
    font-weight: 500;
    color: var(--text-primary);
    flex: 0 0 auto;
  }

  .separator {
    color: var(--gray-300);
  }

  .spec-query {
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.spec-result {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px;

  .spec-notice {
    padding: 10px 12px;
    background: #fffbe6;
    border: 1px solid #ffe58f;
    border-radius: 8px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .spec-summary {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12.5px;
    color: var(--text-secondary);

    .spec-reviewed {
      color: var(--text-secondary);
    }
  }

  .result-card {
    border: 1px solid var(--gray-150);
    border-radius: 10px;
    overflow: hidden;
    background: var(--gray-0);

    .rc-head {
      display: flex;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 4px 8px;
      padding: 10px 14px;
      background: var(--gray-25);
      border-bottom: 1px solid var(--gray-100);

      .rc-standard {
        font-size: 13px;
        font-weight: 500;
        color: var(--text-primary);
      }

      .rc-no {
        font-size: 12.5px;
        color: var(--text-secondary);
      }

      .rc-badge {
        margin-left: auto;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--primary);
        color: #fff;
        font-size: 11.5px;
      }
    }

    .rc-body {
      padding: 4px 14px 10px;
      font-size: 13.5px;
      line-height: 1.9;
      color: var(--text-primary);
      white-space: pre-wrap;
      word-break: break-word;

      :deep(mark) {
        background: #fff3b0;
        padding: 0 2px;
        border-radius: 2px;
      }
    }
  }
}
</style>
