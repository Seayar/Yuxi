# 推理端并发治理：Agent 并发额度、流停滞不重试与 vLLM 服务参数

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/agent_request_queue_service.py

## 问题

电力规范检索模块回答从 11:31 起几乎停摆。实测证据：vLLM 引擎在 6 个并发请求下每 20 秒才完成一次解码步进，全引擎生成吞吐 0.25–1.4 tok/s（历史 p50 为 31 tok/s）；同机 embedding 1.58s、rerank 1.33s 正常，NPU health OK、温度 78–83℃，排除整机故障。并发回落到 3 之后引擎自愈到 45 tok/s。

服务端配置与客户端语义各放大了问题：vLLM 只有 `cudagraph_capture_sizes=[1,4]` 却允许 `--max-num-seqs 8`（启动日志已告警解码批大小超出图捕获范围），且关闭了 prefix caching；客户端把 `StreamChunkTimeoutError`（连接仍在、只是 120 秒没有 chunk）当作可重试网络错误，重发同一份大 prompt，继续抢占算力。最终多条 Run 被 1 小时作业超时打断，停在 `pending` 并占住线程与并发额度。

## 决策

1. **每 Agent 在途上限**：新增 `YUXI_AGENT_MAX_CONCURRENT_RUNS`（默认 0 不限制），在派发事务内统计该 `agent_slug` 下 `pending`、`running` 与 `cancel_requested` 的 Run 数，并先取事务级 PostgreSQL advisory lock，把「计数 + 建 Run + 提交」串行化。达到上限时 ready 的队头保持 `queued` 且不创建 Run；人工继续队列返回 `agent_at_capacity` 冲突，不绕过闸门。额度释放由 worker 既有的 30 秒恢复扫描重新派发，不新增调度循环。`queue_policy=reject` 的同步调用在额度已满时按该策略直接拒绝。
2. **流停滞不重试**：`StreamChunkTimeoutError` 在 `NetworkRetryMiddleware` 的网络预算重试与父类次数重试两条路径上都被判为不可重试，Run 以 failed 结束并给出错误，不再重发同一份 prompt。
3. **对齐推理端并发**：vLLM 启动参数改为 `--max-model-len 100000`、`--enable-prefix-caching`、`--max-num-seqs 4`，`cudagraph_capture_sizes` 保持 `[1,4]`。批大小上限与图捕获范围一致，解码不再回退到 eager。
4. **收敛历史残留**：把 4 条停在非终态的 Run 经 `AgentRunRepository.set_terminal_status` 收敛为 `failed`（`error_type=stale_pending_run`），而不是直接改表。

## 替代方案

1. 下调 `ARQ_MAX_JOBS` 限制并发：作用于所有 Agent 与所有任务类型，粒度错误。
2. 在 worker 内用信号量排队等待：会让 Run 在等待期间仍显示 `running` 并占用 ARQ 槽位，与「排队」语义冲突。
3. 把 `cudagraph_capture_sizes` 扩到 `[1,2,4,8]` 以容纳 8 并发：在本机 310P/TP2 上模型初始化失败（`AclmdlRICaptureEnd` 507903，`rtStreamEndCapture` 失败），已回滚。因此改为下调 `--max-num-seqs` 对齐既有捕获集。
4. 保留流停滞重试、只调大超时：不改变「推理端饱和时重发放大负载」的机制，只是把雪崩推迟。

## 后果

- 并发额度按 `agent_slug` 计算且是全局的：多个进程共用同一个 PostgreSQL 时（例如开发环境与测试环境）共享同一份额度。advisory lock 也按 `agent_slug` 跨进程生效。
- 额度释放靠 30 秒恢复扫描，排队请求最多多等一个扫描周期才开始。
- 额度已满时，`queue_policy=reject` 的同步 Agent Call 会收到 `rejected` 响应而不是排队；调用方需要把它当作可重试的容量失败。
- `--max-num-seqs 4` 降低了单实例峰值并发；同时跑多个 Agent 时，超出的模型调用会进入 vLLM 自己的等待队列。
- 流停滞现在直接失败而不是重试：瞬时抖动会以可见错误结束 Run，需要用户重发，这是为避免放大负载接受的代价。
- 残留风险：Run 被 `retry_released` 后若重投递未再发生，会停在 `pending` 并占用额度；恢复扫描在会话身份不再匹配时无法重投，本次靠人工收敛，尚未在代码层收敛这类 `pending`。

## 验证

| 主张 | 证据 |
| --- | --- |
| 派发闸门生效 | 单测：达到上限时请求保持 `queued` 且不创建 Run；未达上限照常派发；`completed`/`failed`/`cancelled`/`interrupted` 不占额度；`cancel_requested` 占额度 |
| 并发 intake 不超限 | 集成测试 `test_agent_capacity_serializes_concurrent_intake`：limit=1、两个线程并发提交，结果为一个 `dispatched`、一个 `queued`，数据库只有 1 条 Run |
| 人工继续不绕过闸门 | 单测 `test_continue_reports_capacity_conflict`：额度已满时返回 409 `agent_at_capacity` |
| 环境变量语义 | 单测：`3→3`、`0/-1/空/非法→0`（不限制） |
| 流停滞不再重试 | 单测：`_is_network_error` 与 `_retry_non_network_errors` 对流停滞均返回 False；普通 `TimeoutError` 仍按网络错误处理 |
| 改动未回归 | `pytest test/unit -m "not slow"`：2928 passed，54 skipped；`pytest test/unit/agents/test_network_retry.py test/unit/services/test_agent_request_queue_service.py test/unit/services/test_run_worker.py` 通过；`ruff check package` 通过；改动文件的 `ruff format --check` 通过（仓库仍有 4 个与本改动无关的历史文件待格式化） |
| 推理端恢复 | 单请求 64 token 耗时 2.9s（22.3 tok/s）；4 并发聚合 49.3 tok/s；卡死期间为 0.25–1.4 tok/s |
| prefix caching 生效 | 相同 4092 token 前缀二次请求 4.15s → 1.22s；引擎 `vllm:prefix_cache_hits_total/queries_total` 命中率 41.7% |
| 参数确实生效 | `docker inspect` 显示 `--enable-prefix-caching`，容器 healthy；`/health` 返回 200 |
| 残留清理 | `agent_runs` 中 `pending`/`running`/`cancel_requested` 计数为 0；`/api/system/ready` 返回 200 |

vLLM 启动参数与历史 Run 的人工收敛属于部署侧操作，不在本仓库的交付物中；本仓库交付的是队列并发闸门与流停滞判断。部署侧参数的验证记录只用于说明当时的运行环境。
