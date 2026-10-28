import assert from 'node:assert/strict'
import test from 'node:test'
import {
  finalizeThinkingSteps,
  hasResidualThinkingSteps,
  liveThinkingFallbackIndex
} from '../../src/utils/thinkingSteps.js'

test('终态收尾把 started/waiting 标记为 skipped，其余状态保持不变', () => {
  const steps = [
    { step_id: 'a', state: 'completed' },
    { step_id: 'b', state: 'started' },
    { step_id: 'c', state: 'waiting' },
    { step_id: 'd', state: 'failed' }
  ]

  const finalized = finalizeThinkingSteps(steps)

  assert.deepEqual(
    finalized.map((step) => step.state),
    ['completed', 'skipped', 'skipped', 'failed']
  )
  assert.deepEqual(
    steps.map((step) => step.state),
    ['completed', 'started', 'waiting', 'failed']
  )
  assert.equal(hasResidualThinkingSteps(steps), true)
  assert.equal(hasResidualThinkingSteps(finalized), false)
})

test('非数组输入安全返回空数组', () => {
  assert.deepEqual(finalizeThinkingSteps(undefined), [])
  assert.deepEqual(finalizeThinkingSteps(null), [])
  assert.equal(hasResidualThinkingSteps(undefined), false)
})

test('run_id 未回填时只把 live 面板挂到流式中且无 run 的那一轮', () => {
  const finished = { run: { run_id: 'run-done' }, status: 'finished', messages: [] }
  const streaming = { status: 'streaming', messages: [] }

  assert.equal(
    liveThinkingFallbackIndex([finished, streaming], { streaming: true }),
    1
  )
  // 上一轮已完成、新一轮尚未进入 conversations 时不能挂到旧答案上。
  assert.equal(liveThinkingFallbackIndex([finished], { streaming: true }), -1)
  // 已有 run 的最后一轮由 activeRunId 精确匹配，兜底不得抢占。
  assert.equal(
    liveThinkingFallbackIndex([{ run: { run_id: 'run-b' }, status: 'streaming' }], { streaming: true }),
    -1
  )
  // 有 activeRunId 或非流式状态都不走兜底。
  assert.equal(liveThinkingFallbackIndex([streaming], { activeRunId: 'run-x', streaming: true }), -1)
  assert.equal(liveThinkingFallbackIndex([streaming], { streaming: false }), -1)
  assert.equal(liveThinkingFallbackIndex(null, { streaming: true }), -1)
})
