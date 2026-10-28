# React web-new 思考过程接口契约

状态：proposed
类型：architecture
Owner：backend/package/yuxi/agents/thinking.py

## 目的

本文给 `web-new`（React）前端提供“智能体四步思考过程”的接口、状态机、持久化恢复和渲染约束。前端按本文实现，不需要复制 `web/` 的 Vue 组件，也不需要硬编码电力规范或故障溯源的步骤标题。

非目标：本文不重新定义电力规范检索、故障溯源的数据链路，也不要求前端实现工具卡片、台账、MinIO 或知识图谱接口。

## 问题

`web-new` 使用 React，不能直接复用 `web/` 的 Vue composable 和组件。后端已经通过 Agent 详情、Run SSE 和线程历史提供思考过程数据，但缺少一份 React 可直接消费的字段、状态和渲染契约，前端容易硬编码标题、遗漏历史恢复，或在审批/排队阶段清空 loading。

## 提案

React 前端以 Agent 详情的 `thinking_steps` 为步骤清单，以 Run SSE 的 `yuxi.thinking` 事件为实时状态，以历史消息的 `extra_metadata.thinking_steps` 为恢复来源；步骤合并、状态机、路由和 loading 约束如下。

## 1. 智能体步骤清单

### 请求

```http
GET /api/agent/{agent_slug}
Authorization: Bearer <token>
```

`agent_slug` 是 Agent 的 slug，例如 `power-spec-agent`、`fault-trace-agent`。

### 响应

```json
{
  "agent": {
    "id": 12,
    "slug": "power-spec-agent",
    "name": "电力规范检索",
    "thinking_steps": [
      {
        "step_id": "parse_query",
        "title": "解析自然语言查询",
        "description": "识别查询类型、设备、电压等级、关注维度与检索关键词。"
      },
      {
        "step_id": "retrieve_kb",
        "title": "检索规程标准知识库",
        "description": "在可见知识库中执行向量与关键词召回并重排。"
      },
      {
        "step_id": "locate_clauses",
        "title": "定位相关条款",
        "description": "融合、过滤并校验可回溯的条款证据。"
      },
      {
        "step_id": "generate_result",
        "title": "生成检索结果",
        "description": "整理、审查并生成规范条款卡片。"
      }
    ]
  }
}
```

`GET /api/agent` 返回的 `agents[]` 中每个 Agent 也有同一个 `thinking_steps` 字段。

约束：

- `thinking_steps` 是只读步骤清单，按数组顺序表示 1..4；
- 没有该字段或为空数组的 Agent 不显示思考区（内置助手就是这种情况）；
- 前端不得自行补充、改名、排序或写入步骤；
- 步骤标题以 `thinking_steps[].title` 和后续 SSE 事件的 `thinking.title` 为准。

## 2. Run 事件流

### 2.1 创建 Run

```http
POST /api/agent/runs
Content-Type: application/json
Authorization: Bearer <token>

{
  "query": "110kV 变压器绝缘油击穿电压下限是多少？",
  "agent_slug": "power-spec-agent",
  "thread_id": "<thread_id>",
  "meta": { "request_id": "<client_request_id>" },
  "model_spec": "local:Qwen3.6-35B-A3B-w8a8",
  "tool_approval_mode": "default",
  "queue_policy": "enqueue"
}
```

响应至少包含：

```json
{
  "status": "queued",
  "run_id": "…",
  "request_id": "…",
  "queue_position": 1
}
```

`status` 可能是 `queued`、`running` 或 `rejected`；只有拿到 `run_id` 后才订阅 Run 事件流。请求已派发但 Run 尚未启动时，前端仍要显示“正在生成回复/排队中”，不要清空当前对话。

### 2.2 订阅 SSE

```http
GET /api/agent/runs/{run_id}/events?verbose=false
Accept: text/event-stream
Authorization: Bearer <token>
Last-Event-ID: <last_event_id>
```

也可以用 `after_seq` 查询参数代替 `Last-Event-ID`。事件帧格式：

```text
event: custom
data: {"run_id":"…","thread_id":"…","request_id":"…","payload":{"name":"yuxi.thinking","chunk":{"status":"thinking","thinking":{…}}}}
id: 1792978293131-0


```

要点：

- `event` 是 SSE 事件名，`data` 是 JSON；`id` 是游标；
- 使用 `verbose=false` 时后端仍保留 `payload.chunk.thinking`，这是本契约的一部分；
- 心跳可能是以 `:` 开头的注释帧，忽略即可；
- 断线重连时把最后一个 `id` 放入 `Last-Event-ID`，或作为 `after_seq` 传入；
- 不要根据 `data` 中是否含 `event` 字段判断事件类型，使用 SSE 的 `event:` 字段。

浏览器实现时不要直接用原生 `EventSource`：它不能携带 `Authorization` 头。使用 `fetch` + `ReadableStream`（或支持 headers 的 SSE 封装），按空行切帧，解析 `event:`、`data:`、`id:`；把最后一个 `id` 保存到该线程的 live run 状态，重连时作为 `Last-Event-ID` 或 `after_seq` 传回。

### 2.3 思考事件载荷

完整 SSE 帧示例见 [react-thinking-run-event.json](./fixtures/react-thinking-run-event.json)。

```json
{
  "name": "yuxi.thinking",
  "chunk": {
    "status": "thinking",
    "thinking": {
      "version": "v1",
      "agent": "power-spec-agent",
      "step_id": "locate_clauses",
      "step_index": 3,
      "title": "定位相关条款",
      "state": "started",
      "content": "正在校验条款证据。",
      "data": { "evidence_count": 0, "verified": false },
      "started_at": "2026-10-26T03:31:52.012377+00:00",
      "finished_at": null,
      "elapsed_ms": 0,
      "error": null
    }
  }
}
```

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | `"v1"` | 协议版本 |
| `agent` | string | 发射方标识，前端不要用来查找步骤清单 |
| `step_id` | string | 步骤唯一键，同一 Run 内不变 |
| `step_index` | number | 1..4，后端权威顺序 |
| `title` | string | 步骤标题，后端权威；不要前端硬编码 |
| `state` | `started` / `waiting` / `completed` / `failed` / `skipped` | 当前状态 |
| `content` | string | 当前阶段的短文本 |
| `data` | object | 业务明细，形状由工具定义；前端只展示已知字段，未知字段忽略 |
| `started_at` | ISO 8601 string | 该步骤第一次 `started` 的时间 |
| `finished_at` | ISO 8601 string \| null | 进入终态的时间 |
| `elapsed_ms` | number | 后端计算的耗时 |
| `error` | `{code,message}` \| null | 失败原因 |

`pending` 不是后端状态。前端用 Agent 详情的步骤清单生成 `pending` 骨架，收到第一条同 step 事件后替换。

### 2.4 状态语义

| 状态 | 含义 | UI |
|---|---|---|
| `pending` | 前端骨架，尚未收到事件 | 灰色圆点，不显示耗时 |
| `started` | 已进入该阶段 | 动态圆点/加载指示 |
| `waiting` | 等待用户确认或补充信息 | “等待确认”标签；加载态不能消失 |
| `completed` | 阶段完成 | 绿色圆点，显示耗时 |
| `failed` | 阶段失败 | 红色圆点，显示错误文案；后续步骤不执行 |
| `skipped` | 阶段跳过 | 灰色/跳过标签 |

同一步的终态不能再被后续 `started`/`waiting` 回退。重连重放要按 `step_id` upsert，不能追加重复行。

### 2.5 四个步骤示例

电力规范检索：

```text
parse_query      解析自然语言查询
retrieve_kb      检索规程标准知识库
locate_clauses   定位相关条款
generate_result  生成检索结果
```

故障溯源：

```text
parse_fault          解析故障描述
match_causal_chain   匹配因果链
trace_topology       拓扑关系追溯
generate_report      生成溯源报告
```

前端不要从 `agent` 值推断步骤；使用 `step_id` 和 `step_index`。

## 3. 历史恢复

```http
GET /api/chat/thread/{thread_id}/history
Authorization: Bearer <token>
```

历史消息在响应的 `history` 数组中；最终 AI 消息带有：

```json
{
  "history": [
    {
      "id": 123,
      "type": "ai",
      "content": "…",
      "extra_metadata": {
        "thinking_steps": [
          {
            "version": "v1",
            "agent": "power-spec-agent",
            "step_id": "parse_query",
            "step_index": 1,
            "title": "解析自然语言查询",
            "state": "completed",
            "content": "识别为安全规范查询，电力变压器，110kV问题。",
            "data": {},
            "started_at": "…",
            "finished_at": "…",
            "elapsed_ms": 4070,
            "error": null
          }
        ]
      }
    }
  ],
  "runs": []
}
```

约束：

- `thinking_steps` 是 `ThinkingStepEvent[]`，不是步骤清单；
- 只在**对应 Run 的最后一条 AI 消息**上读取，避免一条消息重复挂载；
- 同一线程有多轮 Run 时，每轮只显示自己的四步；
- 历史加载后不要再用上一轮 live 步骤覆盖，除非该线程确实有新的活跃 Run；
- `extra_metadata.thinking_steps` 缺失表示该轮没有思考过程，不显示空时间线。

## 4. React 状态模型

建议按线程保存：

```ts
type ThreadThinkingState = {
  liveRunId: string | null
  steps: ThinkingStepView[]
  isRunActive: boolean
  replyLoadingVisible: boolean
  pendingInterrupt: boolean
}
```

完整 TypeScript 类型和纯函数在 [react-thinking-contract.ts](./fixtures/react-thinking-contract.ts)。

### 4.1 初始化

1. 选择 Agent 后读取 `thinking_steps`，写入 `steps`，状态为 `pending`；
2. 提交时立即显示乐观用户消息和 `replyLoadingVisible=true`，**不要等待创建线程或首条 SSE 事件**；
3. 新建线程完成并切换 `/agent/:thread_id` 时，不要让路由同步重新加载当前线程并清空乐观状态；
4. Run 返回 `run_id` 后建立 SSE，`init` 到达后保持加载态。

### 4.2 事件合并

```ts
if (!isThinkingEnvelope(envelope)) return
setSteps((current) => upsertThinkingStep(current, envelope.payload.chunk.thinking))
```

同时：

- `waiting`：显示“等待确认”，保留加载态；
- `failed`：停止该轮后续步骤，保留错误；
- 全部 terminal：结束当前思考区，等待最终回答；
- `end`/`error`：终态收尾；不要在非终态清空步骤。

### 4.3 活跃行与历史行

实时四步必须挂在**当前活跃 Run 所属的对话行**，不能简单挂在“最后一个 conversation 行”：

- 优先按 `activeRunId` 找对话行；
- `run_id` 尚未回填时，只允许落在“流式进行中且还没有 run”的那一轮；没有命中就不渲染 live 面板，不能退回最后一条含 AI 消息的对话行（否则会挂到上一轮答案上）；
- 活跃行可能暂时只有用户消息，没有 AI/tool group，此时也要创建 live thinking 容器；
- 运行结束后移除 live 容器，回退到历史 `extra_metadata.thinking_steps`。

## 5. 渲染约定

- 思考过程放在原有“推理与工具调用/处理过程”折叠区内，不单独悬浮在消息列表外；
- 固定最多四行，不渲染第 5 个顶层步骤；
- 摘要显示当前阶段：
  - `正在{title}`
  - `等待确认：{title}`
  - `思考过程 · n/4 步`
- 状态标签：`进行中`、`等待确认`、`已完成`、`失败`；
- 活跃阶段自动展开；结束后保留可折叠时间线；
- 有思考步骤时，隐藏 `search_power_spec` / `trace_fault` 的原始工具卡片，避免重复和“电力规范检索｜正在检索电力规范”这类残留卡片；`list_kbs` 等辅助工具可以保留；
- 不显示内部模型 JSON、organizer/reviewer 子智能体内容。

## 6. 断线与异常

- SSE 断开：用最后 `id`/`after_seq` 重连；重放事件按 `step_id` 去重；
- 断线期间 `steps` 和 `replyLoadingVisible` 不能清空；
- 只有 Run 终态、用户取消或明确失败时才结束 loading；
- `waiting` 到审批卡片出现之间保持 loading，文案为“等待确认...”；
- 请求已派发但 Run 尚未启动时显示“排队中/正在生成回复”，不能出现空白。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| Agent 详情返回 `thinking_steps`，前端用它生成 4 行骨架 | 步骤标题/顺序与 Agent 不一致 | `agent_repository.py` | `GET /api/agent/{slug}` 响应 | 删除 `thinking_steps`，前端不应渲染时间线 | Inferred |
| `verbose=false` 的 SSE 保留 `payload.chunk.thinking` | 前端收不到实时步骤 | `agent_run_service.py` | `GET /runs/{run_id}/events?verbose=false` | 从压缩白名单删除 `thinking`，契约测试失败 | Passed |
| 同一 step 的事件按 `step_id` upsert，不重复追加 | 重连后步骤重复/错序 | `web/src/utils/thinkingSteps.js` | 前端 upsert 单测 | 同一 `step_id` 发两次，行数仍为 4 | Inferred |
| 审批等待显示 `waiting`，卡片出现前不空白 | 页面清空、无状态 | `useAgentStreamHandler.js` | 真实页面采样 | 审批时清空 `replyLoadingVisible`，断言失败 | Inferred |
| 刷新后从 `extra_metadata.thinking_steps` 恢复四步 | 刷新丢步骤 | `conversation_service.py` | 历史接口 + 页面 | 删除历史 `thinking_steps`，前端无四步 | Inferred |
| 新建对话路由切换不清空乐观消息和加载态 | 页面刷新清空 | `AgentChatComponent.vue` | 草稿发送真实页面采样 | 路由同步强制 `selectChat`，断言失败 | Inferred |
| 有思考过程时不显示重复的 `search_power_spec` 工具卡片 | 残留“正在检索电力规范” | `messageGrouping.js` | 页面断言 | 保留原始卡片，断言失败 | Inferred |
| 内置助手没有 `thinking_steps` 时不显示思考区 | 内置助手错误显示四步 | `agent_repository.py` | 页面断言 | 给默认 Agent 注入步骤，断言失败 | Inferred |

## 8. 前端交付清单

- API client：`getAgentDetail(slug)`、`streamRunEvents(runId, cursor)`；
- 类型：按本文第 3 节的字段表定义（不再附带独立的 `.ts` 契约样本文件，避免与后端实现漂移后无人校验）；
- Hook：`useThinkingProcess(agentSlug, threadId, runId)`；
- 组件：`ThinkingTimeline`、`ThinkingSummary`；
- 测试：upsert、终态不回退、重连去重、历史恢复、审批等待、路由切换。

后端已经提供字段和事件；`web-new` 不需要扫描或复用 `web/` 的 Vue 组件。当前 Vue 前端的实现与终态收尾口径见 [智能体自有思考过程](../implemented/2026-10-28-agent-owned-thinking.md)。

## 替代方案

- **React 前端硬编码两个 Agent 的步骤。** 新增 Agent 或调整步骤必须发前端版本，放弃。
- **复用 `web/` 的 Vue 组件或 composable。** React 无法直接消费 Vue 组件，复制会继续维护两套实现，放弃。
- **从 `agent` 字段推断步骤。** `agent` 是发射方标识，不是步骤清单 Owner，放弃。
- **每个 SSE 事件追加一行。** 重连重放会产生重复行，且同一步会多次出现，放弃。
- **实时步骤只挂最后一条 conversation。** 活跃 Run 可能对应只有用户消息的行，会显示空白或旧步骤，放弃。

## 风险

- React 和 Vue 两套前端对同一 SSE 契约的消费可能漂移；字段与状态以本文和 [智能体自有思考过程](../implemented/2026-10-28-agent-owned-thinking.md) 为准，后端字段变更需同步更新。
- `data` 是工具业务字段，前端只能展示已知字段；未知字段必须忽略，不能因字段缺失导致渲染失败。
- SSE 断线、审批等待和 Run 排队是三个不同的非终态；错误地提前关闭 loading 会重现空白页。
- 历史消息可能来自旧版本、没有 `thinking_steps`；前端必须允许为空，不显示空时间线。
