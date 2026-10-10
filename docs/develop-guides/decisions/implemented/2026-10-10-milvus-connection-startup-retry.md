# 修复 knowledge_base 启动对 Milvus 的排序缺陷

状态：implemented
类型：architecture
Owner：backend/package/yuxi/knowledge/implementations/milvus.py

## 问题

冷启动全栈时 `api` 会抢在 Milvus proxy 就绪前启动：`knowledge_base` 是 required 启动
组件，`MilvusKB.__init__ → connections.connect` 在 proxy 未就绪时抛 `MilvusException`
（`Fail connecting to server on milvus:19530 ... server unavailable`），启动流程随即
`RequiredStartupComponentError` 退出；uvicorn `--reload` 主进程仍存活而 worker 变
defunct，容器 `healthy` 永远失败，5050 端口无人监听，只能人工重启恢复。

Compose 侧 `api.depends_on` 此前缺少 `milvus`（只有 postgres/redis/minio/sandbox/
storage-migrator），冷启动顺序完全靠运气。

## 决策

1. Compose 编排（容器层）：开发 `docker-compose.yml` 的 `api.depends_on` 增加
   `milvus: condition: service_healthy`，保证 `docker compose up` 时 API 在 Milvus
   健康后再启动；生产 compose 无 `--reload`，由应用层重试与容器 `restart` 策略兜底。
2. 应用层（语义 Owner 层）：`connections.connect` 封装为 `connect_milvus_with_retry`，
   对 `MilvusException` 做**有界指数退避重试**（默认 24 次、1s 起、上限 10s）。sleep
   累计约 3.4 分钟，但每次 `connect` 内部受 pymilvus 连接超时约束（默认 10s），24 次
   全失败时最坏墙钟约 7.4 分钟；超过上限仍抛出最后一次 `MilvusException` 走原
   `RequiredStartupComponentError` 路径——等待依赖就绪而不是"伪装成功"。配置类错误
   （bad URI/token 等，`ConnectionConfigException` 是 `MilvusException` 子类）也会被
   **有界**重试后显式失败，只是不会无限重试；非 `MilvusException`（如 URI 解析错误）
   立即抛出。
3. 重试只在 `MilvusKB`（required 启动路径）使用；`MilvusGraphVectorStore` 是懒加载且
   其同步访问器可能运行在事件循环线程上，不在本次启动排序缺陷范围内，保持不变。

## 替代方案

- **只做 compose depends_on**：能覆盖 `docker compose up` 冷启动，但对 `docker-compose
  restart`、Milvus 中途重启、非 compose 启动等场景无效，进程仍会死成僵尸态。
- **把 knowledge_base 改为 required=False 降级**：违反"Shipping 启动始终包含知识库能力"
  的架构不变量，会把"服务不可用"伪装成"启动成功"。
- **无限重试**：在配置错误时阻塞进程，违背"预设条件不成立时显式失败"。

## 后果

- API 启动遇 Milvus 未就绪时在有界窗口内重试，期间每条重试均有 WARNING 日志；正常冷启动
  （Milvus 120~180s 就绪）会在重试窗口内自愈，不再需要人工重启。Milvus 永久不可用时，
  重试耗尽后仍抛出 `MilvusException` 走 `RequiredStartupComponentError` 显式失败；
  `uvicorn --reload` 的 supervisor 不会自动重启已退出的 worker（仅文件变更触发），
  容器保持 unhealthy 待人工处理——瞬时场景自愈、永久场景显式失败即正确结局。
- `MilvusKB._init_connection` 的语义从"一次直连"变为"有界等待就绪"，日志与错误仍沿用
  原有 `MilvusException` 类型，manager 的失败分类不变。
- 单点 bug-fix、无待裁决替代项或风险（替代方案均在记录中当场否决），按决策 README 直接
  以 implemented 记录，无需 proposal。

## 验证

- 单元测试：`backend/test/unit/plugins/test_milvus_kb.py` 新增六例 —— 首次连接即
  成功（只连一次）、瞬时失败后自愈（第 3 次成功，重试间隔 1s/2s）、退避上限 10s
  （间隔序列 1/2/4/8/10s）、耗尽后抛出最后一次 `MilvusException`（调用次数等于
  上限）、非 `MilvusException` 立即原样抛出且不重试、`max_attempts < 1` 显式
  `ValueError` 且不发起 connect。
- Compose：`docker-compose config -q` 通过。
- 真实链路：`docker-compose restart api` 后容器 `healthy`，`/api/system/health` 与
  `/api/system/ready` 均 200。