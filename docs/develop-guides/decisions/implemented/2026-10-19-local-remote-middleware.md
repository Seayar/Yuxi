# 本地开发连接远端中间件

状态：implemented
类型：architecture
Owner：docker-compose.remote-dev.yml

## 问题

开发环境原本与中间件同机。代码迁到本机后，`docker compose up` 会启动一整套本地中间件，与测试环境的数据和 Schema 分叉；直接把 `.env` 指向远端又连不通：远端只把 TuGraph 与 vLLM 绑定在 `0.0.0.0`，PostgreSQL、Redis、MinIO、Milvus 绑定在 `127.0.0.1`，MinerU 只监听 docker0。开发侧要求本机改动不影响测试人员正在使用的版本，同时共享数据。

## 决策

在 Compose 网络内运行 `remote-dev-tunnel`：一个 SSH 客户端容器，把远端 PostgreSQL、Redis、MinIO、Milvus、MinerU 端口转发成网络内服务名，应用按 `remote-dev-tunnel:<port>` 连接。隧道容器再把 MinerU（`18786`）与语音识别（`8001`）发布到宿主机，满足共享数据库里 `host.docker.internal:18786` 与 `localhost:8001` 这两种按环境写死的写法。TuGraph 与 vLLM 由应用直连远端局域网地址。

本机不启动 `postgres`、`redis`、`minio`、`milvus`、`etcd`、`graph`、`tugraph`，启动入口收敛到 `scripts/dev-remote.ps1`：先起隧道并等健康，再跑 `storage-migrator`，最后以 `--no-deps` 启动 `api`、`worker`、`web`、`sandbox-provisioner`。Redis 使用独占库 `/9`，远端主栈占 `/0`、隔离栈占 `/1`。`docker-compose.remote-dev.yml` 只放宽 worker 健康检查超时：本机冷启动 `uv` 并导入 `yuxi` 约需 20 秒，超过镜像默认的 10 秒。

## 替代方案

1. 修改远端 compose 把中间件绑定 `0.0.0.0`：不需要隧道，但要重启远端中间件，并把无鉴权的 Redis、MinIO 暴露到局域网。
2. 在宿主机直接跑 `ssh -L`：隧道独立于 Compose，需要手工维护，且依赖 Windows 防火墙放行 `ssh.exe` 的入站监听。
3. 用 socat 影子服务替换 Compose 里的中间件服务：可以保留 `docker compose up -d` 原样可用，但同名服务指向远端会掩盖真实拓扑。Compose 无法用覆盖文件删除 `depends_on`（覆盖文件只合并映射），这也是本方案必须带 `--no-deps` 的原因。
4. 本机启动完整中间件：数据与测试环境分叉，违背共享数据的前提。

## 后果

- 中间件流量经单条 SSH 连接多路复用，索引构建等大流量操作慢于本机部署。
- 本机调试产生的会话、消息与运行记录进入共享库。Schema 变更仍被 `storage-migrator` 的版本校验挡住：本机分支的版本与远端库不一致时迁移直接失败，不会静默改写远端表结构。
- `storage-migrator` 会同步代码里的系统配置定义，保留管理员已保存的值，只覆盖名称、说明与参数定义。
- 启动必须经 `scripts/dev-remote.ps1`；直接 `docker compose up -d` 仍会启动本地中间件。
- 隧道依赖 `docker/volumes/remote-dev/ssh` 下的私钥与 `known_hosts`，两者都不进入版本库。
- 隧道重建或断线会断开 worker 的 Redis 连接，worker 子进程退出后 `watchfiles` 不会重启它，容器仍显示 `Up`；此时 `/api/system/ready` 报 `worker: error`，用 `scripts/dev-remote.ps1 restart` 恢复。
- 首次启动时本机 worker 曾短暂使用 `/1`，消化了隔离栈中 147 条已过期队列任务。共享库内没有 run 记录被改动，过期任务本就不会被执行；改用 `/9` 后两套实例在 Redis 上互不可见。

## 验证

| 主张 | 证据 |
| --- | --- |
| Compose 配置可解析 | `docker compose -f docker-compose.yml -f docker-compose.remote-dev.yml config` 通过，解析出 12 个服务 |
| 中间件全部经隧道可用 | 隧道容器健康检查通过；容器内 `PING` 得 `+PONG`；MinIO `bucket_exists('knowledgebases')` 为真；Milvus `list_collections()` 返回 11 |
| 接流量前依赖就绪 | `/api/system/ready` 返回 `ready`，`postgres`、`redis`、`worker` 与全部必需组件均为 `ok` |
| 共享数据可读 | 容器内查询 `knowledge_bases=9`、`messages=9002`、`users=14` |
| 图数据库可用 | `get_shared_graph_connection()` 连上 `tugraph`，`RETURN 1` 返回 `[{'ok': 1}]` |
| 队列隔离 | 本机 worker 只写 Redis 库 `/9`，远端主栈 `/0` 与隔离栈 `/1` 的键未被覆盖 |
| 测试环境未受影响 | 远端容器运行时长不变，隔离栈 `/api/system/ready` 仍返回 200 |
