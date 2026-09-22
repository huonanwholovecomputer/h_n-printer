"""net_direct.py — 云端直连：让打印工具的云端流量绕开系统/环境代理

背景（2026-09 现场排查）：用户机器上装了 Clash/mihomo 之类的代理，并把
HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 写进用户环境变量（或打开系统代理）。打印工具的
云端流量（requests 与 engineio 底层的 websocket-client）会自动继承这些设置，于是：

  · 代理进程活着、但节点/规则/订阅处于异常状态时，本地代理端口照样秒握手成功 ——
    客户端只看到「TCP 连上了、5 秒收不到任何数据」（日志里的 Read timed out / SSL EOF），
    请求根本到不了服务器；
  · 代理重载配置 / 切节点 / 订阅更新会掐掉空闲长连接 —— 工具长期挂后台时正好是那条
    空闲连接，表现为「放后台久了就云端断连」。

对策：把云端主机名写进本进程的 NO_PROXY，让三条出网路径都对该主机直连：
  · requests          —— should_bypass_proxies() 读 no_proxy / NO_PROXY
  · websocket-client  —— proxy_info 读 NO_PROXY（engineio 的 websocket 传输走它）
  · urllib.request    —— proxy_bypass() 读 no_proxy / NO_PROXY（updater 走它）

覆盖不到的情况：TUN / 全局路由接管（在应用之外，改 NO_PROXY 无效），需要代理侧
DIRECT 规则或静态路由把服务器地址排除出 TUN。

用法（幂等，可在多处调用）：
    import net_direct
    net_direct.ensure_direct(api_url, ws_url)
"""

from __future__ import annotations

import logging
import os
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 环境变量名：requests 的 get_proxy("no_proxy") 与 websocket-client 都优先读小写，
# urllib 两者都认。写入时两个都写，读取时取并集，避免受其它组件写法影响。
_NO_PROXY_KEYS = ("no_proxy", "NO_PROXY")


def _host_of(url: str) -> str:
    """从 URL 或裸主机名里取小写主机名；取不到返回空串。"""
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""
    try:
        # 裸主机名（无 scheme）时补上 "//"，urlparse 才认它是 netloc
        parsed = urlparse(raw if "//" in raw else f"//{raw}")
        host = parsed.hostname or ""
    except ValueError:
        return ""
    return host.strip().strip(".").lower()


def _current_entries() -> list[str]:
    """当前 NO_PROXY 条目（两个大小写变量的并集，去重、保序）。"""
    entries: list[str] = []
    seen: set[str] = set()
    for key in _NO_PROXY_KEYS:
        for item in (os.environ.get(key) or "").split(","):
            item = item.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                entries.append(item)
    return entries


def _configured_proxy() -> str:
    """当前环境/注册表里配置的代理地址（没有则空串）。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return ""
    return proxies.get("https") or proxies.get("http") or proxies.get("all") or ""


def ensure_direct(*urls: str) -> list[str]:
    """把 urls 的主机名并入本进程 NO_PROXY（幂等）。返回本次新增的主机名。

    新主机首次加入时打一条日志（含是否检测到系统代理），便于事后判断
    「云端断连」是不是代理造成的。
    """
    hosts: list[str] = []
    for url in urls:
        host = _host_of(url)
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        return []

    entries = _current_entries()
    seen = {e.lower() for e in entries}
    added = [h for h in hosts if h not in seen]
    if not added:
        return []

    merged = ",".join(entries + added)
    for key in _NO_PROXY_KEYS:
        os.environ[key] = merged

    proxy = _configured_proxy()
    if proxy:
        logger.info(
            f"云端直连：已让 {', '.join(added)} 不走系统/环境代理 {proxy}"
            f"（代理异常时会让云端连接卡死/断开）")
    else:
        logger.info(f"云端直连：{', '.join(added)} 不经过代理（未检测到代理配置）")
    return added


def describe(url: str) -> str:
    """该 URL 当前的出网状态描述（供启动自检/日志）。"""
    host = _host_of(url)
    if not host:
        return ""
    proxy = _configured_proxy()
    if not proxy:
        return f"{host}：未检测到代理配置，直连"
    try:
        direct = urllib.request.proxy_bypass(host)
    except Exception:
        direct = False
    if direct:
        return f"{host}：已对系统/环境代理 {proxy} 直连"
    return f"{host}：⚠ 仍会走系统/环境代理 {proxy}（代理异常时云端会连不上）"
