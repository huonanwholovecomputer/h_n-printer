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
import socket
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 环境变量名：requests 的 get_proxy("no_proxy") 与 websocket-client 都优先读小写，
# urllib 两者都认。写入时两个都写，读取时取并集，避免受其它组件写法影响。
_NO_PROXY_KEYS = ("no_proxy", "NO_PROXY")

# TUN 模式的 fake-ip 段（Clash/mihomo 一类把 198.18.0.0/15 当占位网段）
_TUN_EGRESS_PREFIXES = ("198.18.", "198.19.")
# UDP connect 不发包，只是让内核按当前路由表选出口
_ROUTE_PROBE = ("8.8.8.8", 53)
_tunnel_logged = False


def tunnel_egress() -> str:
    """默认路由被 TUN 接管时返回本机在该隧道上的出口地址，否则空串。

    这是唯一能自检的 TUN 证据：出口落在 fake-ip 段 ⇒ 云端流量在 IP 层就被隧道接走，
    下面的 NO_PROXY 全部无效（TUN 切节点/抖动的表现就是「连接超时」「已断开云端连接」）。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(_ROUTE_PROBE)
            ip = sock.getsockname()[0]
    except OSError:
        return ""
    return ip if ip.startswith(_TUN_EGRESS_PREFIXES) else ""


def _tunnel_warning(ip: str) -> str:
    return (f"⚠ 默认路由被 TUN 隧道接管（本机出口 {ip}）：云端流量实际走 VPN，"
            f"切换/抖动 VPN 即断连，NO_PROXY 对 TUN 无效")


def _log_tunnel_once() -> None:
    """每次进程只报一次 TUN 接管（重连时 ensure_direct 会反复被调）。"""
    global _tunnel_logged
    if _tunnel_logged:
        return
    ip = tunnel_egress()
    if ip:
        _tunnel_logged = True
        logger.warning(_tunnel_warning(ip))


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
    _log_tunnel_once()
    return added


def describe(url: str) -> str:
    """该 URL 当前的出网状态描述（供启动自检/日志）。"""
    host = _host_of(url)
    if not host:
        return ""
    proxy = _configured_proxy()
    if not proxy:
        text = f"{host}：未检测到代理配置，直连"
    else:
        try:
            direct = urllib.request.proxy_bypass(host)
        except Exception:
            direct = False
        if direct:
            text = f"{host}：已对系统/环境代理 {proxy} 直连"
        else:
            text = f"{host}：⚠ 仍会走系统/环境代理 {proxy}（代理异常时云端会连不上）"
    tunnel = tunnel_egress()
    if tunnel:
        text += "；" + _tunnel_warning(tunnel)
    return text
