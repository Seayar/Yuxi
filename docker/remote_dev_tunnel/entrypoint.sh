#!/bin/sh
# 建立到远端测试环境的 SSH 隧道，把中间件端口暴露给其余 Compose 服务。
set -eu

: "${YUXI_REMOTE_HOST:?需要设置 YUXI_REMOTE_HOST}"
: "${YUXI_REMOTE_SSH_USER:=root}"
: "${YUXI_REMOTE_SSH_KEY:=/ssh/id_ed25519}"
: "${YUXI_REMOTE_KNOWN_HOSTS:=/ssh/known_hosts}"

test -r "$YUXI_REMOTE_SSH_KEY" || { echo "缺少 SSH 私钥：$YUXI_REMOTE_SSH_KEY" >&2; exit 1; }
test -r "$YUXI_REMOTE_KNOWN_HOSTS" || { echo "缺少 known_hosts：$YUXI_REMOTE_KNOWN_HOSTS" >&2; exit 1; }

# Windows 宿主机的挂载权限不是 0600，ssh 会拒绝使用，先在容器内落到私有副本。
key="/tmp/remote-dev-id"
cp "$YUXI_REMOTE_SSH_KEY" "$key"
chmod 0600 "$key"

# 转发必须绑定 0.0.0.0，其他容器才能通过服务名访问。
forwards=""
add_forward() {
    forwards="$forwards -L 0.0.0.0:$1:$2:$3"
}

add_forward 5432 127.0.0.1 5432     # PostgreSQL
add_forward 6379 127.0.0.1 6379     # Redis
add_forward 9000 127.0.0.1 9000     # MinIO
add_forward 19530 127.0.0.1 19530   # Milvus
add_forward 18786 172.17.0.1 18786  # MinerU（只监听 docker0，必须经宿主转发）
add_forward 8001 127.0.0.1 8001     # 语音识别 vLLM（共享库配置写作 localhost:8001）

# shellcheck disable=SC2086
exec autossh -M 0 -N \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$YUXI_REMOTE_KNOWN_HOSTS" \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -i "$key" \
    $forwards \
    "$YUXI_REMOTE_SSH_USER@$YUXI_REMOTE_HOST"
