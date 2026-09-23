# 语音识别

语音识别把浏览器录制的音频转成文字，供需要语音输入的页面（例如电力规范检索）填充输入框。模型部署在自托管 vLLM 上，API 进程只做协议转译，不在进程内加载推理运行时。

预定读者是调用该接口的前端与集成方，以及配置自托管 ASR 服务的管理员。

## 接口契约

`POST /api/speech/transcriptions`

- 认证：`Authorization: Bearer <token>`，需要登录用户；未认证返回 `401`。
- 请求：`multipart/form-data`，字段 `file` 携带音频文件；`filename` 与 `content-type` 原样转发给上游。缺少 `file` 字段由 FastAPI 返回 `422`。
- 响应：`200` 加 `text/event-stream`。

单个文件上限 20 MB。经反向代理时，`client_max_body_size 20M` 约束整个 multipart 请求体（含表单开销），超出由代理直接返回 `413`，请求不会到达本接口；直连 API 端口时请求体会先被完整接收（FastAPI 在进入端点前已解析表单），接口再按上限判断，超限以 `error` 事件结束，送去识别的内容不超过上限加一个字节。

| 事件 | data | 含义 |
|---|---|---|
| `delta` | `{"text":"变压器"}` | 一段增量文字，按识别顺序到达 |
| `completed` | `{"text":"变压器油温过高"}` | 识别完成，`text` 为本次识别的完整文本 |
| `error` | `{"message":"无法连接语音识别服务，请检查服务地址与服务运行状态"}` | 识别失败，`message` 可直接展示给用户 |

```text
event: delta
data: {"text":"变压器"}

event: delta
data: {"text":"油温"}

event: completed
data: {"text":"变压器油温"}
```

客户端累积 `delta` 文本做实时上屏，并在收到 `completed` 后用其中的 `text` 校准结果。事件流以 `completed` 或 `error` 结束，两者不会同时出现；上游中途失败时，已发出的 `delta` 之后是 `error`。`delta` 的分段粒度由上游模型决定，后端不重新分词。

音频为空、超过上限、服务未配置、上游不可达、上游返回非 200、上游响应不是 SSE、上游把错误写进流、流在结束标记之前结束，都以 `error` 事件结束；失败不会被写成 `completed` 或空识别结果。上游错误详情只写进日志，`error.message` 只包含可直接展示的原因与状态码。

## 配置

语音识别服务由管理员配置项 `speech_recognition_opts` 拥有，字段与回退环境变量：

| 字段 | 环境变量 | 说明 |
|---|---|---|
| `server_url` | `SPEECH_ASR_BASE_URL` | vLLM OpenAI 兼容服务地址，例如 `http://host.docker.internal:8004` |
| `model` | `SPEECH_ASR_MODEL` | vLLM 启动参数 `--served-model-name` 的值 |
| `api_key` | `SPEECH_ASR_API_KEY` | 鉴权用 API Key；服务未启用鉴权时留空，此时不发 `Authorization` 头 |

数据库中的非空值优先于环境变量；两项都为空时接口返回 `error` 事件并写一条告警日志，不返回 500。该配置项目前没有设置页面，通过环境变量或下面的接口配置；保存后立即生效，无需重建容器：

```bash
curl -X PUT http://localhost:5050/api/system/config/options/speech_recognition_opts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"value":{"server_url":"http://host.docker.internal:8004","model":"Qwen3-ASR-1.7B","api_key":""}}'
```

API 进程在容器内运行时（`RUNNING_IN_DOCKER=true`），`server_url` 中出现的 `http://localhost` 与 `http://127.0.0.1` 会被替换成 `http://host.docker.internal`，因此按宿主机部署的 vLLM 直接填写 `http://localhost:<port>` 也能连通；`https://localhost` 等其他写法不改写。

模型的部署不在本仓库范围内：Compose 不包含 ASR 服务，镜像、权重和 `--served-model-name` 由部署方提供，本接口只负责调用它。

## 上游协议与失败语义

API 以 `stream=true` 调用上游 `/v1/audio/transcriptions`，要求上游返回 SSE：增量文字在 `choices[0].delta.content`，并以带 `finish_reason` 的 chunk 和 `data: [DONE]` 结束。上游返回非 200、`Content-Type` 不是 `text/event-stream`、把错误写进流、写出不可解析的数据、在结束标记之前结束或连接失败时，接口产出 `error` 事件并写一条告警日志，不把失败伪装成空结果或截断结果。

Qwen3-ASR 的原始输出以 `language <语言><asr_text>` 开头。上游的非流式响应会剥掉这段前缀，流式响应不会，因此接口在流开头缓冲到该标记出现再转发，`delta` 只包含识别文字；正文不以 `language` 开头时立即转发，缓冲超过 64 个字符仍未出现标记时按无前缀处理、原样转发并恢复增量。

## 源码定位与验证

- 路由与请求边界：[`speech_router.py`](https://github.com/xerrors/Yuxi/blob/main/backend/server/routers/speech_router.py)
- 事件投影与失败语义：[`speech_service.py`](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/services/speech_service.py)
- 配置定义：[`options.py`](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/config/options.py)

```bash
docker compose exec api uv run --group test pytest test/unit/services/test_speech_service.py
docker compose exec api uv run --group test pytest test/integration/api/test_speech_router.py
```

integration 用例在测试进程内启动桩上游，通过管理员配置接口把 `server_url` 指向它，因此断言的是真实 HTTP 上传、认证、增量到达时序与 SSE 事件序列，不需要 GPU 或真实模型。

## 已验证与未验证范围

2026-09-23 在本机 `vllm-asr`（Qwen3-ASR-1.7B，端口 8004）上用一段 6 秒英文语音验证：接口返回 22 个 `delta` 与 1 个 `completed`，`completed.text` 为 `Mr. Quilter is the apostle of the middle classes, and we are glad to welcome his gospel.`，与上游非流式结果一致，端到端用时 1.1 秒。

未验证：中文与其他语言的识别质量、浏览器录制的 WebM/Opus 输入、长音频的识别行为。模型能处理哪些音频格式取决于上游服务，接口只转发文件字节与 `content-type`。
