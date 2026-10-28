import MessageProcessor from './messageProcessor.js'
import { enrichTaskToolCalls } from '../components/ToolCallingResult/toolRegistry.js'
import { collapseConversationProcess } from './conversationProcessGrouping.js'

const hasVisibleAssistantBody = (message) => {
  if (!message || message.type !== 'ai') return true

  const { content } = MessageProcessor.parseAssistantMessageBody(message)
  return Boolean(
    content || message.error_type || message.extra_metadata?.error_type || message.isStoppedByUser
  )
}

const defaultEnrichToolCalls = (message) => enrichTaskToolCalls(message?.tool_calls)

// 这两个工具的过程已经由智能体的四步思考区表达，保留原始工具卡片会重复且容易停留在“正在检索”。
const THINKING_TOOL_NAMES = new Set(['search_power_spec', 'trace_fault'])

const getToolCallName = (toolCall) => toolCall?.name || toolCall?.function?.name || ''

/** 将相邻推理和工具调用按顺序归组，正文保持独立。 */
export const getConversationDisplayItems = (
  conv,
  {
    enrichToolCalls = defaultEnrichToolCalls,
    collapseIntermediate = false,
    runTiming = null,
    thinkingSteps = [],
    hideThinkingTools = false
  } = {}
) => {
  if (!Array.isArray(conv?.messages) || conv.messages.length === 0) return []

  const items = []
  let pendingToolGroup = null
  const lastAiIndex = conv.messages.findLastIndex((message) => message.type === 'ai')

  const flushToolGroup = () => {
    if (pendingToolGroup && pendingToolGroup.entries.length > 0) {
      items.push(pendingToolGroup)
    }
    pendingToolGroup = null
  }

  conv.messages.forEach((message, index) => {
    if (message.type !== 'ai') {
      flushToolGroup()
      items.push({
        type: 'message',
        key: message.id || `message-${index}`,
        message,
        sourceIndex: index
      })
      return
    }

    const { reasoningContent } = MessageProcessor.parseAssistantMessageBody(message)
    const toolCalls = enrichToolCalls(message)

    /** 为当前连续处理过程保留稳定的分组标识。 */
    const ensureToolGroup = (segment) => {
      if (!pendingToolGroup) {
        pendingToolGroup = {
          type: 'tool-group',
          key: `tool-group-${message.id || index}-${segment}`,
          toolCalls: [],
          entries: []
        }
      }
      return pendingToolGroup
    }

    // 四步思考过程属于本轮处理过程，归入同一个思考/工具面板；
    // 只挂到本轮最后一条 AI 消息，避免历史轮次重复显示。
    if (thinkingSteps.length && index === lastAiIndex) {
      const group = ensureToolGroup('thinking')
      group.entries.push({
        type: 'thinking',
        key: `thinking-${message.id || index}`,
        steps: thinkingSteps
      })
    }

    if (reasoningContent) {
      ensureToolGroup('reasoning').entries.push({
        type: 'reasoning',
        key: `reasoning-${message.id || index}`,
        content: reasoningContent
      })
    }

    if (hasVisibleAssistantBody(message)) {
      flushToolGroup()
      items.push({
        type: 'message',
        key: message.id || `message-${index}`,
        message: reasoningContent ? { ...message, reasoning_content: '' } : message,
        sourceIndex: index
      })
    }

    const visibleToolCalls = hideThinkingTools
      ? toolCalls.filter((toolCall) => !THINKING_TOOL_NAMES.has(getToolCallName(toolCall)))
      : toolCalls

    if (visibleToolCalls.length > 0) {
      const group = ensureToolGroup('tools')
      group.toolCalls.push(...visibleToolCalls)
      group.entries.push(
        ...visibleToolCalls.map((toolCall, toolIndex) => ({
          type: 'tool',
          key: `tool-${message.id || index}-${toolCall.id || toolIndex}`,
          toolCall
        }))
      )
    }
  })

  flushToolGroup()
  return collapseConversationProcess(items, collapseIntermediate, runTiming)
}
