# 本地开发专用镜像：把远端测试环境的中间件端口转发进 Compose 网络。
FROM alpine:3.20

RUN apk add --no-cache openssh-client autossh

COPY entrypoint.sh /usr/local/bin/remote-dev-tunnel
RUN chmod 0755 /usr/local/bin/remote-dev-tunnel

ENTRYPOINT ["/usr/local/bin/remote-dev-tunnel"]
