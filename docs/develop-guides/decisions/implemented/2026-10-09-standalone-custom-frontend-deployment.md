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
- **HTTPS**：可信入口为 443，自签证书（`new-web/certs/`，gitignored，SAN 覆盖 `192.168.138.10`/`127.0.0.1`/`localhost`）；HTTP `8080`（`YUXI_NEW_WEB_PORT`）保留并 301 重定向到 443（面向浏览器入口；仍以 `http://…:8080/api` 为 base 的调用方跟随重定向会降级为 GET，API 已同源走 HTTPS，可接受）。nginx 终止 TLS 后仍以 `http://api:5050` 反代，前端 `location.origin` 即 `https://<host>`，同源语义不变。

## 替代方案

1. **写进根 `docker-compose.yml` 的 `web` 服务**：会改变既有服务的镜像来源，且根 compose 需要与上游持续合并，新服务在其中易产生冲突；停用需回滚共享文件。
2. **复用上游 `web` 的 production 阶段镜像**：该阶段从 `web/` 源码构建，与 `dist.7z` 构建产物不是同一份应用，无法托管。
3. **`window.__backend_api_url` 指向 `http://<host>:5050`**：构成跨源请求，需要放开 CORS，并让前端直接暴露后端端口，与既有同源部署形态不一致。

## 后果

- 新增一个长期维护面：`new-web/` 需要在其构建产物更新时重新解包并替换 `dist/`，`docker-compose up -d` 后 nginx 直接读取挂载目录，无需重建镜像。
- 该服务与主 compose 只在网络层耦合：`api` / `minio` 服务名是它解析依赖的契约，重命名主 compose 服务会破坏反代。
- 后端每 20MB 以上的上传（`client_max_body_size`）会被 nginx 拒绝；与上游生产配置一致。
- 前端静态资源无版本前缀失效策略，替换 `dist/` 后浏览器可能命中旧缓存。
- **自签证书**：浏览器首次访问会报证书不受信任，需在各客户端导入 `new-web/certs/server.crt` 或手动信任；证书有效期到 2027-11-10，到期前需重新签发并替换 `new-web/certs/`（无需重建镜像，nginx 直接读挂载目录）。内网无公网域名，未采用 Let's Encrypt。`new-web/certs/` 是未跟踪目录，部署前必须先按下面命令生成（缺失时 nginx 报 `cannot load certificate` 并 crash-loop）：
```bash
mkdir -p new-web/certs
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 397 \
  -keyout new-web/certs/server.key -out new-web/certs/server.crt \
  -subj "/CN=192.168.138.10" \
  -addext "subjectAltName=IP:192.168.138.10,IP:127.0.0.1,DNS:localhost"
chmod 600 new-web/certs/server.key
```

## 验证

实测（2026-10-09）：

- HTTPS：`curl -sk https://127.0.0.1/` 与 `https://192.168.138.10/` → 200 `text/html`；`/assets/js/app-DQpUYnPz.js` → 200 `application/javascript`；SPA 深链 `/power-search` → 200（`try_files` 回退）。
- 反代经 TLS：`GET /api/system/health|ready` → 200（`{"status":"ok","version":"0.7.3"}`）；`POST /api/auth/token` → FastAPI 422 校验响应（已到达真实后端）。
- 重定向：`http://<host>:8080/`、SPA 深链、SSE 路径与 `POST /api/auth/token` 均 301 → `https://<host>/`，Location 不带端口。
- TLS：`openssl s_client` 确认 TLSv1.2/1.3、ECDHE-RSA-AES256-GCM-SHA384，TLS1.0/1.1 被拒；证书 SAN 覆盖 `IP:192.168.138.10`、`IP:127.0.0.1`、`DNS:localhost`。
- 浏览器（忽略自签错误上下文）实测 `https://192.168.138.10/login` 渲染、`window.__backend_api_url === location.origin`（https）、`#root` 挂载、控制台无错误。
- 未验证范围：使用真实账号的登录后主流程与 SSE 长连接未做端到端验证（缺可用凭据）；`/minio/public/` 反代未以真实对象验证；自签证书在真实客户端的信任流程未逐机验证。
