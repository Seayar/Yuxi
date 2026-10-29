# 自有前端独立部署（new-web 双服务）

状态：implemented
类型：architecture
Owner：new-web/docker-compose.yml

## 问题

团队自行开发的前端有两份构建产物：

- `dist-web-main.7z`：主页面（Vite 静态站，标题「复杂配电网智能辅助决策系统」），`assets/js/config.js` 写死 `window.__backend_api_url = location.origin`，所有业务请求为 `${origin}/api/...`（含 SSE `/api/agent/runs/{id}/events`）。
- `dist-web-tugraph.7z`：知识图谱页（UMI 静态资源，只有 `resource/**`），主页面通过 `window.__external_page_config.TU_GRAPH.url` 以 iframe 嵌入，并等待子页 `postMessage` 的 `TUGRAPH_READY`。

图谱页运行期还会请求同源 `/api/*`（例如 `/api/get_procedure_demo`）并读取 `/resource/graph-config.json`，因此它必须与 TuGraph DB Web 后端同源；主页面则必须与应用 API 同源。仓库内 `web/` 服务是上游 Yuxi 的 Vite dev server，无法托管这两份构建产物。页面代码还要能从宿主机目录直接替换，用于后续升级前端版本。

## 决策

在 `new-web/` 下自包含部署，两个 `nginx:alpine` 容器，页面文件全部由宿主机目录挂载：

| 服务 | 容器 | 宿主机端口 | 挂载与反代 |
|---|---|---|---|
| `new-web` | `yuxi-new-web` | `8080`（`YUXI_NEW_WEB_PORT`） | `/usr/share/nginx/html` ← `./dist`；`assets/js/config.js` 单独覆盖；`/api/` → `api:5050`，`/minio/public/` → `minio:9000` |
| `new-web-tugraph` | `yuxi-new-web-tugraph` | `8081`（`YUXI_NEW_WEB_TUGRAPH_PORT`） | `/usr/share/nginx/graph` ← `./tugraph`；`index.html` 单独覆盖；`/api/` → TuGraph DB Web 后端（当前 `192.168.138.5:8000`） |

- 站点配置分别由 `new-web/nginx-main.conf`、`new-web/nginx-tugraph.conf` 拥有；主页面保留与主仓库 `docker/nginx/default.conf` 一致的 SSE 与上传语义（`proxy_buffering off`、`client_max_body_size 20M`）。
- 主页面运行期配置由 `new-web/config.js` 拥有并单独挂载覆盖 `dist/assets/js/config.js`，升级 `dist` 不会丢失接口地址与图谱入口；`TU_GRAPH.url` 指向 `${location.hostname}:8081`。
- 图谱页 `index.html` 由 `new-web/tugraph-index.html` 拥有并单独挂载，入口脚本固定为 `/resource/umi.js`；解包 `dist-web-tugraph.7z` 后把 `resource/umi.*.js` 复制为 `resource/umi.js`，因此替换 `resource/` 不需要改 `index.html`。
- `./dist`、`./tugraph` 与 `*.7z` 被 `.gitignore` 忽略；`docker-compose.yml`、两个 nginx 配置、`config.js`、`tugraph-index.html` 进入版本控制。
- 独立 compose 通过 `external` 网络接入 `yuxi_app-network`，可独立启停，不修改上游根 compose。
- 只提供 HTTP 入口（8080/8081）。`new-web/certs/` 保留为将来加 TLS 的素材，当前 compose 不挂载。

## 替代方案

1. **单容器两个 server 块**：图谱页固定请求根路径 `/resource/*`，与主页面的 `/assets/*` 同源部署会互相干扰，无法用子路径隔离；因此分配独立端口（独立 origin）。
2. **图谱页只做静态托管**：缺少 `/api/*` 后端会直接显示“图谱加载失败”，必须与 TuGraph DB Web 后端同源，故用 nginx 反代。
3. **把资源直接覆盖到 TuGraph DB Web 宿主机的 static 目录**：后端在另一台机器（192.168.138.5），本仓库部署只覆盖本服务器，且无法保证对方目录可写与可回滚。
4. **写进根 `docker-compose.yml`**：会与上游持续合并产生冲突，且停用需回滚共享文件。
5. **`window.__backend_api_url` 指向 `:5050`**：构成跨源请求，需要放开 CORS 并暴露后端端口，与同源语义不一致。

## 后果

- 升级前端不需要重建镜像：替换 `new-web/dist/` 或 `new-web/tugraph/resource/` 后 `docker-compose restart`（或 `up -d`）即可；`config.js` 与 `tugraph-index.html` 不随产物被覆盖。
- 图谱页与后端版本绑定：`new-web-tugraph` 只提供前端静态资源，`/api/*` 全部落到 `192.168.138.5:8000`；后端地址变化时只需改 `new-web/nginx-tugraph.conf` 的 `proxy_pass`。
- 主页面依赖 `api`/`minio` 两个服务名，重命名主 compose 服务会破坏反代。
- 两张前端都直接暴露 HTTP：内网可接受，公网部署需在前面加 TLS 反向代理。
- 静态资源无版本前缀失效策略，替换目录后浏览器可能命中旧缓存；升级后可让用户强制刷新。
- 图谱页 `index.html` 由宿主机单文件覆盖，因此 `./tugraph` 目录必须可写挂载（只读目录无法在其下创建挂载点）。

## 验证

实测（2026-10-29，服务器 192.168.138.10）：

- 主页面：`GET http://127.0.0.1:8080/` → 200（2961 字节新构建）；`/assets/js/app-BdQeJm21.js`、`/assets/js/app-BiebSLq1.js`、`/assets/styles/index-BC-nmoKe.css`、`/vite.svg` → 200。
- 运行期配置：`GET /assets/js/config.js` 返回挂载版本，其中 `window.__backend_api_url = location.origin`、`TU_GRAPH.url` 指向 `:8081`。
- 主页面反代：`GET /api/system/health`、`/api/system/ready` → 200。
- 图谱页：`GET http://127.0.0.1:8081/` → 200；`/resource/umi.js` → 200（1,082,293 字节）；`/resource/graph-config.json` → 200；`POST /api/get_procedure_demo` → 200（已到达 TuGraph DB Web 后端）。
- 未验证范围：浏览器内 iframe 的 `TUGRAPH_READY` 握手与图谱页交互未做端到端验证（需人工打开页面）；`/minio/public/` 未以真实对象验证；登录后的 SSE 长连接未验证。
