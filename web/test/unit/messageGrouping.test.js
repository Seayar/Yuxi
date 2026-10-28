import assert from 'node:assert/strict'
import test from 'node:test'
import {
  groupConversationContinuations,
  collapseConversationProcess,
  formatProcessDuration,
  isConversationSettled,
  formatEmptyRunStatus
} from '../../src/utils/conversationProcessGrouping.js'
import { getConversationDisplayItems } from '../../src/utils/messageGrouping.js'

test('关联续跑组成连续回答，消息归属不变且只累计执行耗时', () => {
  const groups = [
    {
      run: { run_id: 'a', timing: { total_latency_ms: 1000 } },
      status: 'finished',
      messages: [{ id: 'a1', run_id: 'a', type: 'ai', isLast: true }]
    },
    {
      run: {
        run_id: 'b',
        run_type: 'resume',
        created_by_run_id: 'a',
        timing: { total_latency_ms: 2000 }
      },
      status: 'finished',
      messages: [{ id: 'b1', run_id: 'b', type: 'ai', isLast: true }]
    },
    {
      run: {
        run_id: 'c',
        run_type: 'resume',
        created_by_run_id: 'b',
        timing: { total_latency_ms: 3000 }
      },
      status: 'streaming',
      messages: [{ id: 'c1', run_id: 'c', type: 'ai', isLast: true }]
    }
  ]
  const original = structuredClone(groups)
  const result = groupConversationContinuations(groups)
  assert.equal(result.length, 1)
  assert.deepEqual(
    result[0].messages.map((message) => message.run_id),
    ['a', 'b', 'c']
  )
  assert.equal(result[0].run, groups[2].run)
  assert.equal(result[0].status, 'streaming')
  assert.equal(result[0].displayKey, 'a')
  assert.equal(result[0].processTiming.total_latency_ms, 6000)
  assert.deepEqual(result[0].messages.map((message) => message.isLast), [false, false, true])
  assert.deepEqual(groups, original)
  groups[1].run.timing = null
  assert.equal(groupConversationContinuations(groups)[0].processTiming.total_latency_ms, null)
})

test('新用户、独立 Run、未知关系及零消息失败不并入前一回答', () => {
  const first = { run: { run_id: 'a' }, messages: [{ type: 'ai' }] }
  for (const next of [
    { run: { run_id: 'b', run_type: 'chat', created_by_run_id: 'a' }, messages: [{ type: 'ai' }] },
    {
      run: { run_id: 'b', run_type: 'resume', created_by_run_id: 'other' },
      messages: [{ type: 'ai' }]
    },
    { messages: [{ type: 'ai' }] },
    {
      run: { run_id: 'b', run_type: 'resume', created_by_run_id: 'a' },
      messages: [{ type: 'human' }, { type: 'ai' }]
    },
    {
      run: { run_id: 'b', run_type: 'resume', created_by_run_id: 'a', status: 'failed' },
      messages: []
    }
  ]) {
    assert.deepEqual(groupConversationContinuations([first, next]), [first, next])
  }
  const resume = { run: { run_id: 'b', run_type: 'resume', created_by_run_id: 'a' }, messages: [{ type: 'ai' }] }
  for (const previous of [{ messages: [{ type: 'ai' }] }, { run: { run_id: 'a', status: 'failed' }, messages: [] }]) {
    assert.deepEqual(groupConversationContinuations([previous, resume]), [previous, resume])
  }
})

test('已完成对话使用后端 Run 总耗时聚合过程组', () => {
  const items = collapseConversationProcess(
    [
      { key: 'h1', type: 'message', message: { type: 'human' } },
      { key: 'a1', type: 'message', message: { type: 'ai' } },
      { key: 'tools', type: 'tool-group', toolCalls: [{ id: 't1' }, { id: 't2' }] },
      {
        key: 'a3',
        type: 'message',
        message: {
          type: 'ai',
          run_started_at: '2026-08-20T00:00:00Z',
          run_finished_at: '2026-08-20T00:01:05Z',
          run_id: 'run-final'
        }
      }
    ],
    true,
    { total_latency_ms: 64000 }
  )
  assert.deepEqual(items.map((item) => item.type), ['message', 'process-group', 'message'])
  assert.equal(items[1].messageCount, 1)
  assert.equal(items[1].toolCallCount, 2)
  assert.equal(items[1].durationMs, 64000)
})

test('只有旧 Run 时间戳时不推算过程耗时', () => {
  const items = collapseConversationProcess(
    [
      { key: 'h1', type: 'message', message: { type: 'human' } },
      { key: 'a1', type: 'message', message: { type: 'ai' } },
      {
        key: 'a2',
        type: 'message',
        message: {
          type: 'ai',
          run_started_at: '2026-08-20T00:00:00Z',
          run_finished_at: '2026-08-20T00:01:05Z'
        }
      }
    ],
    true
  )

  assert.equal(items[1].durationMs, null)
  assert.equal(formatProcessDuration(items[1].durationMs), '处理过程')
})

test('运行中或最终消息后仍有工具调用时不聚合过程', () => {
  const items = [
    { key: 'h1', type: 'message', message: { type: 'human' } },
    { key: 'a1', type: 'message', message: { type: 'ai' } },
    { key: 'tools', type: 'tool-group', toolCalls: [{ id: 't1' }] }
  ]
  assert.equal(collapseConversationProcess(items).some((item) => item.type === 'process-group'), false)
  assert.equal(
    collapseConversationProcess(items, true).some((item) => item.type === 'process-group'),
    false
  )
})

test('思考步骤归入最后一条 AI 消息前的思考/工具面板', () => {
  const steps = [
    { step_id: 'parse_query', step_index: 1, title: '解析自然语言查询', state: 'completed' },
    { step_id: 'retrieve_kb', step_index: 2, title: '检索规程标准知识库', state: 'started' }
  ]
  const conv = {
    messages: [
      { id: 'human-1', type: 'human', content: '查询 10kV 开关柜规范' },
      { id: 'ai-1', type: 'ai', content: '回答' }
    ]
  }

  const items = getConversationDisplayItems(conv, {
    enrichToolCalls: () => [],
    thinkingSteps: steps
  })

  assert.deepEqual(items.map((item) => item.type), ['message', 'tool-group', 'message'])
  assert.deepEqual(items[1].entries[0], {
    type: 'thinking',
    key: 'thinking-ai-1',
    steps
  })
})

test('历史轮次不重复展示当前思考步骤', () => {
  const steps = [{ step_id: 'parse_query', step_index: 1, title: '解析', state: 'completed' }]
  const conv = {
    messages: [
      { id: 'human-1', type: 'human', content: '第一问' },
      { id: 'ai-1', type: 'ai', content: '第一答' },
      { id: 'human-2', type: 'human', content: '第二问' },
      { id: 'ai-2', type: 'ai', content: '第二答' }
    ]
  }

  const items = getConversationDisplayItems(conv, {
    enrichToolCalls: () => [],
    thinkingSteps: steps
  })

  assert.equal(items.filter((item) => item.type === 'tool-group').length, 1)
  assert.equal(items.find((item) => item.type === 'tool-group').entries[0].key, 'thinking-ai-2')
})

test('四步思考存在时隐藏重复的检索工具卡片，普通工具保留', () => {
  const steps = [{ step_id: 'parse_query', step_index: 1, title: '解析', state: 'completed' }]
  const conv = {
    messages: [
      { id: 'human-1', type: 'human', content: '查询' },
      { id: 'ai-1', type: 'ai', content: '回答' }
    ]
  }
  const enrichToolCalls = () => [
    { id: 'tool-1', name: 'search_power_spec', args: { query_text: '查询' } },
    { id: 'tool-2', name: 'read_file', args: { path: '/a.md' } }
  ]

  const hidden = getConversationDisplayItems(conv, {
    enrichToolCalls,
    thinkingSteps: steps,
    hideThinkingTools: true
  })
  const shown = getConversationDisplayItems(conv, { enrichToolCalls, thinkingSteps: steps })

  const toolNames = (items) =>
    items
      .filter((item) => item.type === 'tool-group')
      .flatMap((item) => item.entries.filter((entry) => entry.type === 'tool'))
      .map((entry) => entry.toolCall.name)

  assert.deepEqual(toolNames(hidden), ['read_file'])
  assert.ok(
    hidden.some((item) => item.entries?.some((entry) => entry.type === 'thinking')),
    '思考面板必须保留'
  )
  // 开关关闭时（历史轮次）两张工具卡片都要保留，证明去重只由独立开关驱动。
  assert.deepEqual(toolNames(shown), ['search_power_spec', 'read_file'])
})

test('空白思考步骤数组不会隐式隐藏工具卡片', () => {
  const conv = {
    messages: [
      { id: 'human-1', type: 'human', content: '查询' },
      { id: 'ai-1', type: 'ai', content: '' }
    ]
  }

  const items = getConversationDisplayItems(conv, {
    enrichToolCalls: () => [{ id: 'tool-1', name: 'search_power_spec', args: {} }],
    thinkingSteps: []
  })

  assert.deepEqual(
    items
      .filter((item) => item.type === 'tool-group')
      .flatMap((item) => item.entries.filter((entry) => entry.type === 'tool'))
      .map((entry) => entry.toolCall.name),
    ['search_power_spec']
  )
})

test('流式会话可用独立开关隐藏思考工具卡片，不依赖思考步骤已到达', () => {
  const conv = {
    messages: [
      { id: 'human-1', type: 'human', content: '查询' },
      { id: 'ai-1', type: 'ai', content: '' }
    ]
  }

  const items = getConversationDisplayItems(conv, {
    enrichToolCalls: () => [{ id: 'tool-1', name: 'trace_fault', args: {} }],
    thinkingSteps: [],
    hideThinkingTools: true
  })

  assert.equal(items.some((item) => item.type === 'tool-group'), false)
})

test('formatProcessDuration: 复用 Run 时延格式', () => {
  assert.equal(formatProcessDuration(0), '耗时 0ms')
  assert.equal(formatProcessDuration(null), '处理过程')
  assert.equal(formatProcessDuration(undefined), '处理过程')
  assert.equal(formatProcessDuration(5000), '耗时 5.0s')
  assert.equal(formatProcessDuration(59000), '耗时 59s')
  assert.equal(formatProcessDuration(60000), '耗时 1m 0s')
  assert.equal(formatProcessDuration(65000), '耗时 1m 5s')
  assert.equal(formatProcessDuration(125000), '耗时 2m 5s')
})


test('零消息后续 Run 不隐藏上一条完成回答的操作栏，关联 resume 仍等待续写', () => {
  const answer = { run: { run_id: 'run-a', status: 'completed' }, messages: [{ type: 'human' }, { type: 'ai' }] }
  const empty = { run: { run_id: 'run-b', run_type: 'chat', status: 'failed' }, messages: [] }
  assert.equal(isConversationSettled([answer, empty], answer), true)
  const resume = { run: { run_id: 'resume', run_type: 'resume', created_by_run_id: 'run-a' }, messages: [] }
  assert.equal(isConversationSettled([answer, resume], answer), false)
  assert.equal(isConversationSettled([answer], answer, true), false)
  assert.equal(formatEmptyRunStatus(empty.run.status), '本次运行失败')
  assert.equal(formatEmptyRunStatus('cancelled'), '本次运行已取消')
})
