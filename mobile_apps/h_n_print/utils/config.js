// utils/config.js — 全局配置
// 部署时只需要修改这里的 BASE_URL

const CONFIG = {
  // 后端 API 地址（生产环境改为你的云服务器域名）
  // 本地测试: http://127.0.0.1:5000
  // 生产环境: https://your-domain.com
  BASE_URL: 'https://your-server.com',  // ⚠️ 部署前改为你的服务器地址
}

module.exports = { CONFIG }
