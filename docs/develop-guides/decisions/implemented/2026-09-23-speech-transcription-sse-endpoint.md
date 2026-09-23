# 语音识别 SSE 接口

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/speech_service.py

## 问题

电力规范检索等模块的原型要求「文字 / 语音」双输入，后端此前没有任何音频转写入口：录音只能作为普通附件上传，或依赖浏览器内置识别，前端拿不到服务端统一的识别结果。识别模型以 vLLM 部署在本机（OpenAI 兼容 `/v1/audio/transcriptions`），需要一条把浏览器录音转成增量文字的 HTTP 链路。

## 决策

`POST /api/speech/transcriptions` 是语音识别的唯一入口：请求为 `multipart/form-data`，字段 `file` 携带音频；响应为 `text/event-stream`；认证沿用 `get_required_user`。事件契约固定为三种：

| 事件 | data | 含义 |
|---|---|---|
| `delta` | `{"text": "增量文字"}` | 一段增量识别文字 |
| `completed` | `{"text": "最终全文"}` | 识别完成，附完整文本 |
| `error` | `{"message": "说明"}` | 识别失败，说明可直接展示 |

`speech_service.stream_transcription` 是唯一的事件来源：它以 `stream=true` 调用上游 vLLM 转写接口，把上游 SSE 的 `choices[0].delta.content` 逐段投影成 `delta`，只有在上游给出 `finish_reason="stop"` 或 `data: [DONE]` 后才发 `completed`；上游以 `length` 等截断原因结束时按失败处理。空音频、超过 20 MB、上游非 200、`Content-Type` 不是 `text/event-stream`、上游把错误写进流、上游流不可解析、流在结束标记之前结束、连接失败、服务地址或模型未配置都走 `error` 事件，不用 HTTP 500 或空流表达失败；配置读取本身失败也落在同一投影内，不会在已发出响应头后截断连接。上游错误详情只进日志，`error.message` 只包含状态码与可直接展示的原因，避免把上游内部信息交给任意登录用户。`delta` 的分段粒度由上游决定，后端不重新分词。

上传上限为 20 MB，与 nginx 的 `client_max_body_size` 一致。FastAPI 在解算路由依赖之前就会读完 multipart 表单，端点内或依赖内的 `Content-Length` 检查都无法阻止请求体落盘，因此接收边界由反向代理承担；API 只保证读取量不超过上限加一个字节，超限请求以 `error` 事件结束。

Qwen3-ASR 的原始输出以 `language <语言><asr_text>` 开头：上游的非流式响应会剥掉它，流式响应不会，因此本服务在流开头缓冲到该标记出现再转发，`delta` 只包含识别文字；正文不以 `language` 开头时立即转发，保留增量到达时序，缓冲超过 64 个字符仍未出现标记时按无前缀处理、原样转发。

配置由管理员配置项 `speech_recognition_opts`（`server_url`、`model`、`api_key`）拥有，并回退 `SPEECH_ASR_BASE_URL`、`SPEECH_ASR_MODEL`、`SPEECH_ASR_API_KEY` 环境变量。`api_key` 为空时不发送 `Authorization` 头，以支持未启用鉴权的自托管服务。`get_docker_safe_url` 把容器内的 `localhost` / `127.0.0.1` 改写成 `host.docker.internal`，因此宿主机部署的 vLLM 可以直接填 `http://localhost:<port>`。

## 替代方案

- **注册为模型供应商的一种 `model_type`**：与模型列表、前端模型管理更统一，但需要同时改 `VALID_MODEL_TYPES`、模型缓存和前端类型选择；ASR 是自托管 HTTP 服务，形态与 MinerU、PaddleX 一致，复用等价配置项更小。
- **只读环境变量，不建配置项**：少一个 Option，但改地址必须重建 api 容器；平台已有「环境变量 + 管理员覆盖」约定，保持一致。
- **上游非流式调用，后端按标点切分伪增量**：实现最简单，但会产生原文不存在的分段语义，且首字延迟等于整段识别时间。

## 后果

- 语音识别能力随 API 进程启动即存在；ASR 服务的部署（镜像、权重、`--served-model-name`）不在本仓库范围内，由部署方提供，未配置时接口以 `error` 事件显式失败。
- 前端语音输入与配置界面不属于本次范围，管理员通过环境变量或 `PUT /api/system/config/options/speech_recognition_opts` 配置。
- `completed` 携带全文，客户端可在流末校准累积文本；`delta` 边界不承诺字词边界。
- 音频经 API 进程内存转发（单次读取上限 20 MB + 1 字节），不做落盘、转码与重采样。请求体在 FastAPI 解析表单阶段已可能写入临时文件，接收阶段的磁盘与带宽占用不由本接口控制。
- 识别质量、语言的输出文本完全取决于上游模型；本接口只做协议转译、前缀剥离和失败投影。
- 上游 ASR 引擎启动时按 `gpu_memory_utilization` 校验设备空闲显存并预留 KV cache，与共享同一芯片的其他服务（如 embedding 服务）存在显存竞争：芯片余量不足时引擎启动失败，余量接近打满时首次推理会触发设备级故障。部署方需要为该引擎保留足够的芯片余量。

## 验证

实际执行（本机 `uv run` 因挂载目录权限不可用，改用容器内解释器；命令在 `/app` 下运行）：

```bash
docker exec yuxi-api-1 sh -c 'cd /app && python -m pytest -p no:cacheprovider -q test/unit/services/test_speech_service.py test/integration/api/test_speech_router.py'
docker exec yuxi-api-1 sh -c 'cd /app && python -m pytest -p no:cacheprovider -q test/unit -m "not slow"'
docker exec yuxi-api-1 sh -c 'cd /app && python -m pytest -p no:cacheprovider -q test/integration/api/test_system_router_api.py'
```

integration 用例需要 `backend/test/.env.test` 提供 `TEST_USERNAME`/`TEST_PASSWORD`；未配置时整批 skip。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 认证用户上传音频后按 `delta`…`completed` 顺序收到增量文字与全文 | 路由未注册、SSE 帧格式错误、上游 chunk 未解析 | `backend/server/routers/speech_router.py`、`backend/package/yuxi/services/speech_service.py` | `test/integration/api/test_speech_router.py::test_transcription_streams_deltas_then_completed`（真实 HTTP + 桩上游，断言音频与 `model`/`stream` 参数到达上游） | 未认证请求返回 401 | Passed |
| 增量文字在识别结束前就能读到 | 整段缓冲后一次返回，`delta` 名存实亡 | 同上 | 同文件 `::test_transcription_delivers_delta_while_stream_is_open`（上游末块被 gate 拦住时已能读到首个 `delta`） | 上游未释放时不应出现 `completed` | Passed |
| 上游非 200、不是 SSE、流不可解析、缺结束标记、连接失败、服务未配置时产出 `error` 且不产出 `completed` | 失败被吞成 `completed` 或空识别结果 | `speech_service.py` | `test/unit/services/test_speech_service.py` | 桩上游返回 500、`application/json` 的 200、`data: {oops`、只有 `delta` 就 EOF、`server_url` 为空 | Passed |
| 上游错误详情不出现在客户端事件中 | 内部堆栈、路径泄露给任意登录用户 | `speech_service.py` | 同文件（断言 `error.message` 不含上游原文） | 500 响应体含 `EngineCore` 字样 | Passed |
| `delta` 不含 Qwen3-ASR 的 `language <语言><asr_text>` 前缀 | 前端显示模型控制标记 | `speech_service.py` | `test/unit/services/test_speech_service.py::test_stream_transcription_strips_language_prefix`（前缀跳 chunk 的真实分块方式）、`::test_stream_transcription_strips_long_language_prefix` | 无前缀时按原样转发、不丢内容；正文以 `language` 开头且无标记时完整交出；超出缓冲上限时原样转发 | Passed |
| 上游以 `length` 等截断原因结束时产出 `error` 而不是 `completed` | 截断文本被当成完整识别结果 | `speech_service.py` | `test/unit/services/test_speech_service.py::test_stream_transcription_reports_truncated_stream`、`::test_parse_stream_line_rejects_truncated_finish_reason` | `finish_reason="length"` 的 chunk | Passed |
| 空音频与超过上限的音频被拒绝，且不触发上游调用 | 空流、截断音频被送去识别 | `speech_router.py`、`speech_service.py` | `test/unit/services/test_speech_service.py::test_router_rejects_oversized_upload_without_calling_upstream`；`test/integration/api/test_speech_router.py::test_transcription_rejects_oversized_upload_without_calling_upstream` | 空 bytes、`MAX_AUDIO_BYTES + 1`（真实 HTTP 上传得到 `error` 事件且桩上游无请求） | Passed |
| 配置项随启动同步并出现在管理员配置列表 | 新增 Option 未同步、管理员无法配置 | `backend/package/yuxi/config/options.py` | `test/integration/api/test_system_router_api.py`（断言配置项 key 集合精确匹配） | 集合不包含 `speech_recognition_opts` 即失败 | Passed |
| 真实模型（vLLM `Qwen3-ASR-1.7B`）端到端识别 | 上游实际响应格式与桩不同 | 部署侧 vLLM 服务（不在本仓库） | 本机 8004 实调：`curl -N -X POST http://localhost:5050/api/speech/transcriptions -F file=@speech.flac` 得到 22 个 `delta` + 1 个 `completed`，`completed.text` 为 `Mr. Quilter is the apostle of the middle classes, and we are glad to welcome his gospel.`（与上游非流式结果一致，1.1s） | — | Passed |

未验证范围：在 8004 上用英文语音段验证，中文/其他语言的识别质量、浏览器录制的 WebM/Opus 输入、长音频行为未验证。验证时上游引擎已获得足够的设备显存余量（同芯片的 embedding 服务限制在约 20 GB 以内，引擎预留 KV cache 9.73 GiB）。
