# 自有前端独立部署（new-web）

状态：implemented
类型：architecture
Owner：new-web/docker-compose.yml

## 问题

`/root/dist.7z` 是团队自行开发的前端构建产物（Vite 静态站，标题「复杂配电网智能辅助决策系统」，含 `power-search` 电力规范页），与仓库内 `web/`（上游 Yuxi 前端）不是同一份应用。仓库现有的 `web` 服务以 Vite dev server 形态跑上游源码，无法托管这份外部构建产物；需要在不改动上游 `docker-compose.yml`、不重启既有服务的前提下，把该静态站按同源语义部署出来。

关键约束：构建产物的 `assets/js/config.js` 写死 `window.__backend_api_url = location.origin`，且所有业务请求为 `${origin}/api/...`（含 SSE `/api/agent/runs/{id}/events`）。因此前端必须与其 API 同源，不能直接指向 `:5050`（跨源）。

## 决策

在 `new-web/` 下自包含部署，使用 `nginx:alpine` 托管 `new-web/dist`，并反代 `/api/` 到主 compose 的 `api:5050`、`/minio/public/` 到 `minio:9000`，从而形成同源入口。

本记录 Owner 是 `new-web/docker-compose.yml`（拥有部署拓扑：镜像、端口、网络接入、挂载）。站点本身的反代与 SPA 回退语义由 `new-web/nginx.conf` 拥有，静态文件由 `new-web/dist/` 拥有；三者在同一变更中一起生效，任一被改动都需回看本记录。

- 静态文件放在 `new-web/dist/`（已被 `.gitignore` 的 `dist` 规则忽略，不进入版本控制）。
- 站点配置 `new-web/nginx.conf` 与主仓库 `docker/nginx/default.conf` 保持同一套反代语义（`proxy_buffering off` 以支持 SSE，`client_max_body_size 20M`）。
- 独立 `new-web/docker-compose.yml` 通过 `external` 网络接入 `yuxi_app-network`，因此可独立启停、不依赖主 compose 启动顺序，也不把新服务写进会被上游合并频繁改动的根 compose。
- 对外端口取 8080（`YUXI_NEW_WEB_PORT` 可覆盖）。

## 替代方案

1. **写进根 `docker-compose.yml` 的 `web` 服务**：会改变既有服务的镜像来源，且根 compose 需要与上游持续合并，新服务在其中易产生冲突；停用需回滚共享文件。
2. **复用上游 `web` 的 production 阶段镜像**：该阶段从 `web/` 源码构建，与 `dist.7z` 构建产物不是同一份应用，无法托管。
3. **`window.__backend_api_url` 指向 `http://<host>:5050`**：构成跨源请求，需要放开 CORS，并让前端直接暴露后端端口，与既有同源部署形态不一致。

## 后果

- 新增一个长期维护面：`new-web/` 需要在其构建产物更新时重新解包并替换 `dist/`，`docker-compose up -d` 后 nginx 直接读取挂载目录，无需重建镜像。
- 该服务与主 compose 只在网络层耦合：`api` / `minio` 服务名是它解析依赖的契约，重命名主 compose 服务会破坏反代。
- 后端每 20MB 以上的上传（`client_max_body_size`）会被 nginx 拒绝；与上游生产配置一致。
- 前端静态资源无版本前缀失效策略，替换 `dist/` 后浏览器可能命中旧缓存。

## 验证

部署后实测（2026-10-09）：

- `GET http://127.0.0.1:8080/` → 200 `text/html`；`/assets/js/app-DQpUYnPz.js` → 200 `application/javascript`；`/assets/styles/index-CCzCl5QG.css` → 200 `text/css`。
- SPA 深链 `GET /power-search` → 200 `text/html`（`try_files` 回退 `index.html`）。
- 反代：`GET /api/system/health` → 200 `{"status":"ok",...,"version":"0.7.3"}`；`GET /api/system/ready` → 200。
- 浏览器实测：页面跳转 `/login`，`window.__backend_api_url === location.origin`，`#root` 已挂载，控制台无错误、无失败请求；提交探测账号触发 `POST /api/auth/token` → 401 `{"detail":"登录标识或密码错误"}`，证明鉴权链路经 nginx 到达真实后端。
- 局域网可达：`GET http://192.168.138.10:8080/` → 200。
- 未验证范围：使用真实账号的登录后主流程与 SSE 长连接未做端到端验证（缺可用凭据）；`/minio/public/` 反代未以真实对象验证。
