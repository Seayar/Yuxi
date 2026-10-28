/** 思考步骤的终态收尾工具。 */

const RESIDUAL_STATES = new Set(['started', 'waiting'])

/**
 * 流结束时把仍未完成的步骤标记为 skipped，避免面板一直显示“进行中”。
 * 非数组输入返回空数组；调用方继续持有原值。
 */
export const finalizeThinkingSteps = (steps) => {
  if (!Array.isArray(steps)) return []
  return steps.map((step) => (RESIDUAL_STATES.has(step?.state) ? { ...step, state: 'skipped' } : step))
}

/** 是否还有未完成的思考步骤。 */
export const hasResidualThinkingSteps = (steps) =>
  Array.isArray(steps) && steps.some((step) => RESIDUAL_STATES.has(step?.state))

/**
 * 新一轮刚发出、run_id 还没回填时的 live 面板归属。
 *
 * 只在最后一轮已经是“流式进行中且尚无 run”的会话时返回它的下标，避免把思考面板挂到
 * 上一轮已完成的答案上；有 activeRunId 时由调用方按 run 精确匹配，不使用本兜底。
 */
export const liveThinkingFallbackIndex = (conversations, { activeRunId, streaming } = {}) => {
  if (activeRunId || !streaming) return -1
  const list = Array.isArray(conversations) ? conversations : []
  const index = list.length - 1
  const last = list[index]
  if (!last || last.status !== 'streaming' || last.run?.run_id) return -1
  return index
}
