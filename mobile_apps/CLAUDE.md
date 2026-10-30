# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HN 云打印 — 微信小程序云打印系统，三个组件协作：

| 组件 | 目录 | 技术栈 |
|---|---|---|
| 微信小程序前端 | `h_n_print/` | 微信原生框架 (Component 模式, 自定义 tabBar) |
| Android App | `android_app/` | Capacitor WebView（页面逻辑与小程序同源，`www/` 即前端源码） |
| 后端 API 服务 | `printer-backend/` | Flask + Flask-SocketIO + SQLite + APScheduler |

**数据流**: 用户小程序上传文件 → 后端存储(MD5去重) → SocketIO 实时推送/HTTP 拉取 → Windows 客户端下载渲染 → GDI 直打打印机

## 移动端文档索引

| 子文档 | 内容 | 何时读 |
|--------|------|--------|
| `docs/移动端_后端架构.md` | `printer-backend/app.py` 单文件架构、订单模型与状态机、设备注册与授权、任务分发 | 改后端接口/订单状态/调度 |
| `docs/移动端_小程序.md` | 页面结构与滚动引擎、文件卡片、上传与隐私告知、tabBar、「我」页布局 | 改小程序页面/交互 |
| `docs/移动端_APP打包与部署.md` | Android App 打包（必须走 `build-release.ps1`）、部署与备份 | 打 APK / 部署后端 |
| `docs/移动端_运维与价格与预览.md` | S3 安全运营、价格模型与复制校验、页数归属、深色模式、文件预览、关键文件索引 | 改价格/预览/运维 |

## 与根文档的关系

根 `CLAUDE.md` 是仓库总纲（含本地打印工具与两子系统桥接）；本文件只管 `mobile_apps/` 三组件。
跨端的硬规矩（订单来源不得被本地工具改写、人工订正只允许终态、设备是最终裁决者、
攒批远程开关的三端同判据）在根 `CLAUDE.md` 的硬规矩里，**两边都改才算改完**。
