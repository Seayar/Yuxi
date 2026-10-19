# 对齐 Yuxi 模型配置与新模型服务拓扑

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/power_spec/tool.py

## 问题

2026-10-10 上午模型服务在 `/opt/vllm/docker-compose.yml`（systemd `vllm-stack.service`）下重建，端口与模型全部重排：chat `Qwen3.6-35B-A3B-w8a8` 从 8003 移到 8000（`max_model_len` 从 102400 降到 20480）；8003 改为 vLLM reranker `Qwen3-Reranker-4B`（pooling，`/v1/rerank`）；8005 的 embedding 从 TEI `bge-m3` 换为 `Qwen3-Embedding-4B`（2560 维）；ASR 移到 8001、PaddleOCR-VL 移到 8004。

Yuxi 侧数据库配置未同步，导致：

- 会话第一个模型请求发往 `http://192.168.138.10:8003/v1`（现为 reranker 服务），返回 404 `{'detail': 'Not Found'}`，前端只显示 `Error streaming messages: Error code: 404 - {'detail': 'Not Found'}`。
- `POWER_SPEC_INTENT_MAX_TOKENS` 默认 65536 大于新 chat 窗口 20480，意图识别/条款校对/最终合并三类调用会被服务端以 400 拒绝（`max_tokens cannot be greater than max_model_len`）。
- `local:bge-m3` 指向的 8005 不再提供 `bge-m3`；`local:bge-reranker-v2-m3` 指向的 8008 已无监听。

## 决策

1. chat：`model_providers.local.base_url` 改为 `http://192.168.138.10:8000/v1`，模型 `Qwen3.6-35B-A3B-w8a8` 不变。
2. embedding：`local:bge-m3` 增加 `base_url_override = http://bge-m3-cpu:8015/v1/embeddings`（CPU bge-m3，1024 维），不迁移到 `Qwen3-Embedding-4B`；既有 Milvus 集合与全部知识库 `embedding_model_spec` 保持可用。
3. rerank：`local.bge-reranker-v2-m3` 替换为 `local:Qwen3-Reranker-4B`，`rerank_base_url` 改为 `http://192.168.138.10:8003/v1/rerank`（协议与 OpenAI 兼容 reranker 一致）；`system_options.reranker` 与 8 个知识库 `query_params`（含历史嵌套的 `options.options.reranker_model`）同步改名。
4. `POWER_SPEC_INTENT_MAX_TOKENS` 默认 65536 → 4096：输出额度与提示词共享 `max_model_len`，三处调用（意图识别、条款校对、最终合并）的输出都是短 JSON/短文本，需为可能较大的条款提示词留出余量；仍可用环境变量覆盖。
5. 同步 `speech_recognition_opts.server_url` → 8001、`paddleocr_api_opts.api_url` → 8004。

## 替代方案

- **迁移到 `Qwen3-Embedding-4B`（2560 维）**：需要全量重嵌并重建所有 Milvus 集合（含图谱集合），超出恢复范围，且旧 KB 数据全部失效；先保持 bge-m3 兼容。重新引入条件：经评估决定切换 embedding 模型时，以一次全量重索引为载体执行。
- **关闭 rerank（`use_reranker=false`）**：会改变检索行为与卡片"相关度"语义，且新 reranker 服务协议兼容，没有理由降级。
- **回退到旧 `/opt/models-startup` 部署**：已被新栈与 systemd 服务取代，不回退。
- **把输出额度做成动态值（窗口减提示词）**：需要引入 token 估算或读取服务端窗口配置，为三类短输出调用引入新依赖，当前固定值加环境变量覆盖已满足需求。

## 后果

- 查询与入库 embedding 改由 CPU bge-m3 提供服务：单查询延迟可接受，批量入库吞吐低于原 NPU TEI；`local-cpu-bge` 提供者仍独立可用。
- reranker 由 bge-reranker-v2-m3 换成 Qwen3-Reranker-4B，相关度分数分布变化，历史卡片数值不可直接比较；排序方向经真实样本验证正确。
- power_spec 三类模型调用输出上限 4096 tokens；需要更长输出时提高 `POWER_SPEC_INTENT_MAX_TOKENS`，但必须小于服务端 20480 窗口减去提示词长度。
- 新 chat 窗口 20480 明显小于旧 102400，超长历史对话在摘要/提示词侧可能触达上限，属新部署的已知约束。

## 验证

- 直连（api 容器内，Yuxi 客户端代码）：
  - `select_model("local:Qwen3.6-35B-A3B-w8a8")` 带 `max_tokens=4096` 正常返回；
  - `select_embedding_model("local:bge-m3")` 返回 1024 维向量（CPU 端点）；
  - `get_reranker("local:Qwen3-Reranker-4B")` 对真实条款对评分排序正确。
- 端到端真实 HTTP（用户 9601 身份，问题"110kV主变压器日常运行巡视需要检查哪些内容？"，KB `M1-PowerSpec-R5-Formal50-20260929`）：
  - run#1 在意图确认处 `interrupted`，确认问题文本由模型生成；
  - 以"正确，继续检索" resume 的 run#2 `completed`（约 115s），`power-spec-organizer`、`power-spec-reviewer` 子运行均 `completed`；
  - 最终 assistant 消息为模型整理的规范卡片（非降级固定卡片）；worker 无 WARNING/ERROR；验证线程已删除。
- 单元测试：`test/unit/knowledge -k power_spec` 146 passed；`test/unit -m "not slow"` 2447 passed / 54 skipped。
