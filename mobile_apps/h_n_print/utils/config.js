// utils/config.js — 全局配置
// 默认 BASE_URL 为占位符，提交 git 前无需修改。
// 本地开发时在同目录创建 config.local.js（已排除 git），
// 填入你的实际服务器地址即可覆盖默认值：
//
//   const LOCAL_CONFIG = { BASE_URL: 'https://你的地址' }
//   module.exports = { LOCAL_CONFIG }
//

// 默认值（占位符）
let _base_url = 'https://your-server.com'

// 尝试加载本地覆盖配置（文件不存在则静默忽略）
try {
  const { LOCAL_CONFIG } = require('./config.local')
  if (LOCAL_CONFIG && LOCAL_CONFIG.BASE_URL) {
    _base_url = LOCAL_CONFIG.BASE_URL
  }
} catch (e) {
  // config.local.js 不存在，使用默认值
}

const CONFIG = {
  BASE_URL: _base_url,
}

module.exports = { CONFIG }
