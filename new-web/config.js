// 主页面运行期配置：由 new-web/docker-compose.yml 单独挂载覆盖 dist 内的同名文件，
// 因此升级 dist 不会丢失这里的接口地址与图谱入口。
//
// 同源部署：前端所有请求走 location.origin 下的 /api/，由 nginx 反代到 api:5050。
window.__backend_api_url = location.origin;

window.__external_page_config = {
  MODULE_KEY: {
    name: '',
    url: '',
    embedded: true,
  },
  TU_GRAPH: {
    embedded: true,
    name: '图谱',
    // 知识图谱前端由 new-web-tugraph 服务提供（宿主机 8081），主页面以 iframe 嵌入。
    url: `${location.protocol}//${location.hostname}:8081/#/analysis?graphName=kg3&embed=1`,
  },
};

window.__external_api_config = {
  MODULE_KEY1: {
    ip: '',
    port: '',
    protocol: 'http',
    url: '',
  },
  MODULE_KEY2: {
    ip: '',
    port: '',
    protocol: 'http',
    url: '',
  },
};
