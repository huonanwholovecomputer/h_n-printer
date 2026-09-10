# tests/audit_2026_12 — 《HN 打印系统全面代码审计报告》12 个 🔴 修复的验收脚本

独立可跑，不依赖 pytest。每个脚本 `exit 0` 表示该组验收全绿。

| 脚本 | 覆盖 | 运行 |
|------|------|------|
| `verify_backend.py` | 🔴1 临时授权消费 SQL ｜ 🔴11 `sqlite3.Row` 访问 ｜ 🔴12 claim 键错位 | `python tests/audit_2026_12/verify_backend.py` |
| `verify_local_tool.py` | 🔴2 SumatraPDF 参数 ｜ 🔴3 `_save_config` 线程安全 ｜ 🔴4 预约状态机 ｜ 🔴5 自更新供应链 ｜ 🔴7 PDF 缓存 key ｜ 🔴8 COM 反初始化 | `python tests/audit_2026_12/verify_local_tool.py` |
| `verify_frontend.js` | 🔴6 上传中删文件 → 结果写错卡片（Android + 小程序，桩环境跑真实代码时序） | `node tests/audit_2026_12/verify_frontend.js` |
| `verify_frontend_legacy.js` | 🔴6 **反证**：用修复前的源码复现旧缺陷，证明验收脚本有效 | `node tests/audit_2026_12/verify_frontend_legacy.js` |
| `verify_settlement.py` | 🔴9 结算页内联事件 XSS ｜ 🔴10 弱哈希兜底 | `python tests/audit_2026_12/verify_settlement.py` |

## 环境要求

- Python 3 + 本地打印工具依赖（PySide6 / PyMuPDF / reportlab / pywin32 / python-socketio）
- Node.js（`verify_frontend*.js` 与 `verify_settlement.py` 的 JS 语法校验用）
- 后端脚本需要 `mobile_apps/printer-backend/config.py` 存在，且后端依赖已安装

## 说明

- 后端脚本会把 `printer-backend/` 复制到临时目录再 import，**不会**碰真实 `orders.db` / `users.db` / `uploads`。
- `verify_frontend.js` 在 `vm` 沙箱里加载**真实**前端源码并驱动「上传中删除前面文件」的完整时序，
  包含「响应已在桥接层派发、`abort()` 拦不住」的竞态窗口。
- `verify_frontend_legacy.js` 默认从提交 `8a3bf19`（审计修复前）取旧源码；换提交用 `AUDIT_LEGACY_REF=<ref>`。
- 实测结论（值得记录）：Android 旧实现的 `onload` 闭包持有的是**对象引用**，`fileId`/页数并不会错写，
  真正错配的是**按索引登记的计时器与按索引重查的辅助函数**（页数轮询挂到别的文件后立即自停）。
  小程序旧实现则确实会把 B 的 `file_id`/页数写进 C 的 `setData` 路径。
  两端本次统一改为「文件稳定 uid + 异步回调现算下标 + 文件已删除则丢弃结果」。

对应修复计划见 `docs/审计修复计划.md`。
