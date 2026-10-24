// utils/config.js — 全局配置
//
// S2-09（DS-22/MB-04）：**生产域名是默认值**，仓库里不再放第三方占位域。
// 旧写法把 `https://your-server.com` 当默认值、真地址只存在于被 gitignore 的
// config.local.js 里 —— 换台机器 `git clone` 直接构建时，token 与测试文档会真的
// 发往一个可被他人注册的域名，且"这次发的到底是哪个服务器"在仓库里无法复现。
//
// 本地/测试要换地址时，在同目录建 config.local.js（已排除 git，模板见
// config.local.js.example），它**只用于覆盖成本机/测试地址**：
//
//   const LOCAL_CONFIG = { BASE_URL: 'http://127.0.0.1:5000' }
//   module.exports = { LOCAL_CONFIG }
//
// 发布前断言：`tests/verify_mobile_hardening.py` 会静态检查这里的默认值必须是
// https 的生产域名（防止有人把占位域改回来）。
const PRODUCTION_BASE_URL = 'https://hn-space.cn'

// 默认值 = 生产域名；config.local.js 仅作本地/测试覆盖
let _base_url = PRODUCTION_BASE_URL

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
  PRODUCTION_BASE_URL,
}

module.exports = { CONFIG }
