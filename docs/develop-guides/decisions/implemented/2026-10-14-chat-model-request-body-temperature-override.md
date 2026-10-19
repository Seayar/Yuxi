# 本地 chat 模型全局 temperature=0：放行 request_body_overrides 的 temperature 字段

状态：implemented
类型：feature
Owner：backend/package/yuxi/models/providers/service.py

运行时合并边界（`load_chat_model` 的 `extra_body` 组装与 OpenAI SDK 的覆盖优先级）由 `backend/package/yuxi/models/chat.py` 与上游 SDK 语义所有，本记录不重复其规则。

## 问题

本地推理服务的 chat 模型 `local:Qwen3.6-35B-A3B-w8a8` 同时是系统默认模型与快速模型，需要对该模型的每次调用下发 `temperature=0`，使输出更确定。模型供应商管理页已有「模型请求参数 JSON」（`enabled_models[].request_body_overrides`，运行时经 `extra_body` 合并到请求体顶层），但保存校验白名单 `ALLOWED_EXTRA_BODY_FIELDS` 只允许思考类字段：

- 页面保存 `{"temperature": 0}` 报「包含不支持的 extra_body 字段: temperature」；
- 绕过校验直写 DB 虽可运行，但该模型之后无法再从页面保存（校验失败），配置进入不可维护状态。

## 决策

1. `ALLOWED_EXTRA_BODY_FIELDS` 增加 `temperature`；白名单仍只限制顶层字段，作用域仍限定 OpenAI 兼容供应商的 chat 模型（`_validate_request_body_overrides_scope` 不变）。
2. `local` 供应商的 `Qwen3.6-35B-A3B-w8a8` 设置 `request_body_overrides = {"temperature": 0}`（DB 运行时数据，非代码）。经模型缓存刷新后，`load_chat_model` 在 `disable_thinking` 的两个分支都把该覆盖合并进 `extra_body`，最终由 OpenAI SDK 的 `_merge_mappings(json_data, extra_json)` 以 `extra_body` 优先落到请求体顶层——即该配置对调用方显式 `temperature` 是**覆盖**而非默认，符合「全局参数」语义。
3. 前端帮助文案、`docs/intro/model-config.md` 字段表与 changelog 枚举同步加入 `temperature`。

## 替代方案

- **只改 DB、不放行白名单**：运行时生效，但页面保存该校验失败、模型配置不可维护（否决）。
- **新增模型级 `temperature` 一等字段或系统级温度配置**：引入新的配置面与 UI，现有 `request_body_overrides` 已覆盖需求，不为单一消费者建立第二套机制（否决）。
- **在推理服务端（vLLM）改默认采样**：影响该服务所有客户端，且不归 Yuxi 配置边界所有；用户要求在 Yuxi 侧配置（否决）。

## 后果

- `Qwen3.6-35B-A3B-w8a8` 的所有 Yuxi 调用（默认聊天、快速模型、子智能体、工具内直调、知识图谱抽取等）请求体顶层恒为 `temperature: 0`；调用方显式温度会被覆盖，例如某知识库图谱抽取配置中的 `temperature: 0.1` 成为无效声明，本次不改动该配置并在此记录。
- 白名单只放行一个数值型采样字段；`chat_template_kwargs` 等仍只能由调用点显式传入；Anthropic/Gemini 等非 OpenAI 兼容供应商与 embedding/rerank 模型不受影响。
- DB 与 Redis 模型缓存是运行时状态，不随 git 发布；新环境需在模型配置中自行设置该参数。
- 小而完整、无待裁决替代或风险（替代方案均在记录中当场否决），按决策 README 直接以 implemented 记录，无需 proposal。

## 验证

- 单元测试：`test/unit/services/test_model_provider_service.py` 白名单接受性用例覆盖 `temperature`（同文件既有用例锁定 `messages` 等字段仍被拒绝）；`test/unit/services/test_model_selectors.py` 新增 wire 级用例：DB 覆盖 `{"temperature": 0}` 时，调用方 `temperature=0.6` 在最终请求体中被覆盖为 `0`。全套 `test/unit -m "not slow"`：2510 passed / 54 skipped。
- 真实链路：DB 写入后按路由同款路径重建模型缓存，api 与 worker 容器均读到 `{'temperature': 0}`；以真实 `load_chat_model` 配合 MockTransport 抓取请求体，顶层为 `temperature: 0`；真实 vLLM 非流式（15.8s 返回 `2`）与流式（218 个增量返回 `5`）均正常。
- 保存路径：api 进程 `--reload` 日志确认已加载新白名单（`WatchFiles detected changes in 'package/yuxi/models/providers/service.py'. Reloading...`）；`ruff check` / `ruff format --check` / `ruff check --select I` 与 `git diff --check` 通过。
