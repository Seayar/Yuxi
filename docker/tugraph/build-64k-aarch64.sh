#!/usr/bin/env bash
# 在 64K 页内核（aarch64，如 Kunpeng/统信 UOS）上构建可运行的 TuGraph 运行时镜像。
#
# 背景：官方 arm64 镜像的 jemalloc 在“编译期”固化页大小（LG_PAGE），4K 版在 64K 页
# 内核上启动即崩溃：`<jemalloc>: Unsupported system page size`。jemalloc 静态链接进
# lgraph_server，无法用 LD_PRELOAD 绕过，只能在目标机重编 jemalloc 并重新链接 TuGraph。
# 上游 issue：https://github.com/TuGraph-family/tugraph-db/issues/477
#
# 产出：tugraph/tugraph-runtime-centos7:<VERSION>-64k
#
# 依赖：docker、可访问 gitee（源码镜像）或 github（走 HTTP 代理）、可访问
#       tugraph-web OSS 或 github 代理下载 jemalloc 官方 release 包。
set -euo pipefail

VERSION="${TUGRAPH_VERSION:-4.5.2}"
COMPILE_IMAGE="${COMPILE_IMAGE:-tugraph/tugraph-compile-arm64v8-centos7:1.2.7}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-tugraph/tugraph-runtime-centos7:${VERSION}}"
OUTPUT_IMAGE="${OUTPUT_IMAGE:-tugraph/tugraph-runtime-centos7:${VERSION}-64k}"
JEMALLOC_VERSION="${JEMALLOC_VERSION:-5.3.0}"
PROXY_PREFIX="${GITHUB_PROXY:-https://gh-proxy.com/}"
WORKDIR="${WORKDIR:-/opt/tugraph64k}"
JOBS="${JOBS:-$(nproc)}"

echo "==> 校验内核页大小"
pagesize="$(getconf PAGESIZE)"
echo "PAGESIZE=${pagesize}（非 4096 才需要本流程；本流程在 ${pagesize} 上会编出匹配的 jemalloc）"

mkdir -p "${WORKDIR}"
cd "${WORKDIR}"

if [[ ! -d tugraph-db/.git ]]; then
    echo "==> 拉取 TuGraph v${VERSION} 源码（gitee 镜像，失败回退 github 代理）"
    git clone --depth 1 --branch "v${VERSION}" https://gitee.com/mirrors/tuGraph-db tugraph-db \
        || git clone --depth 1 --branch "v${VERSION}" \
            "${PROXY_PREFIX}https://github.com/TuGraph-family/tugraph-db" tugraph-db
fi
git -C tugraph-db submodule update --init --depth 1 \
    || git -C tugraph-db -c url."${PROXY_PREFIX}https://github.com/".insteadOf="https://github.com/" \
        submodule update --init --depth 1

if [[ ! -f "jemalloc-rel-${JEMALLOC_VERSION}.tar.bz2" ]]; then
    echo "==> 下载 jemalloc ${JEMALLOC_VERSION} 官方 release（含 configure，免 autotools）"
    curl -fsSL -o "jemalloc-rel-${JEMALLOC_VERSION}.tar.bz2" \
        "${PROXY_PREFIX}https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2"
fi
cp -f "jemalloc-rel-${JEMALLOC_VERSION}.tar.bz2" "jemalloc-rel.tar.bz2"

echo "==> 启动编译容器（${COMPILE_IMAGE}）"
docker rm -f tgb >/dev/null 2>&1 || true
docker run -d --name tgb -v "${WORKDIR}:/build" "${COMPILE_IMAGE}" sleep infinity

echo "==> 用本机(64K)重编 jemalloc 并覆盖镜像内的 4K 版"
docker exec tgb sh -c '
set -e
cd /build && rm -rf jemalloc-src && mkdir jemalloc-src
tar xjf /build/jemalloc-rel.tar.bz2 -C jemalloc-src --strip-components=1
cd jemalloc-src
CFLAGS="-fPIC" CXXFLAGS="-fPIC" ./configure --prefix=/usr/local >/tmp/jc.log 2>&1
grep -i "LG_PAGE" /tmp/jc.log | head -1
make -j"$(nproc)" >/tmp/jm.log 2>&1
make install >/tmp/ji.log 2>&1
'

echo "==> 配置并编译 TuGraph（仅 lgraph_server 足够 Bolt 服务；如需全部工具可去掉目标名）"
docker exec tgb sh -c '
set -e
cd /build/tugraph-db
mkdir -p deps/install
ln -sfn /usr/local/lib deps/install/lib
ln -sfn /usr/local/lib64 deps/install/lib64
ln -sfn /usr/local/include deps/install/include
rm -rf build && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DENABLE_BUILD_ON_AARCH64=ON -DENABLE_WALL=OFF >/tmp/cm.log 2>&1
make -j"$(nproc)" lgraph_server >/tmp/make.log 2>&1
'

echo "==> 生成 ${OUTPUT_IMAGE}"
rm -rf artifacts64k && mkdir -p artifacts64k
docker cp tgb:/build/tugraph-db/build/output/lgraph_server artifacts64k/lgraph_server
docker cp tgb:/build/tugraph-db/build/output/liblgraph.so artifacts64k/liblgraph.so
cat > artifacts64k/Dockerfile <<EOF
FROM ${RUNTIME_IMAGE}
COPY lgraph_server /usr/local/bin/lgraph_server
COPY liblgraph.so /usr/local/lib64/liblgraph.so
RUN chmod 755 /usr/local/bin/lgraph_server && ldconfig
EOF
docker build -t "${OUTPUT_IMAGE}" artifacts64k

echo "==> 完成：${OUTPUT_IMAGE}"
echo "验证： docker run --rm -d --name tugraph64k -p 17687:7687 ${OUTPUT_IMAGE} lgraph_server -c /usr/local/etc/lgraph.json -d run && docker logs tugraph64k"
