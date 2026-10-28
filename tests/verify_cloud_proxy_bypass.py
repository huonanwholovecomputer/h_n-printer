# -*- coding: utf-8 -*-
"""验收脚本 · 云端直连（绕开系统/环境代理）+ 建连超时/失败清理

现场事故（2026-09-22）：某台机器上的打印工具在 19:11 掉线后 1.5 小时连不上，
日志全是「Read timed out (read timeout=5)」——TCP 能连上、5 秒收不到任何数据，
请求根本没到服务器；同一时段该机 5 分钟一次的 /api/pricing 普通 HTTP 心跳也一起消失。
本机实测该环境装有 mihomo（TUN + fake-ip）且把 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
写进用户环境变量：requests 与 engineio 的 websocket-client 会自动继承这些代理设置，
代理节点/规则异常时正是「本地秒握手、上游不回数据」的形态；代理重载配置还会掐掉空闲
长连接（工具长期挂后台时恰好是那条空闲连接）。

本脚本验收修复：
  1. net_direct.ensure_direct() 让云端主机在 requests / websocket-client / urllib
     三条路径上都直连（且不误伤其它域名）；
  2. CloudClient 建连使用放大的超时（request_timeout 20s / wait_timeout 25s）；
  3. 建连失败（含 namespace 握手超时）时断开已建立的 transport，不泄漏活连接；
  4. 旧连接的迟到回调不再篡改当前连接状态。

无网络：socketio 用桩模块替换，HTTP 只做本地判断。
    python tests/verify_cloud_proxy_bypass.py      # exit 0 = 全绿
"""
import io
import os
import sys
import types
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import net_direct                    # noqa: E402
import requests.utils as req_utils   # noqa: E402
from websocket._http import get_proxy_info  # noqa: E402

FAKE_PROXY = "http://127.0.0.1:7899"   # 假装本机开着 Clash/mihomo
CLOUD = "https://hn-space.cn"
OTHER = "https://example.org"

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _set_proxy_env(no_proxy: str | None):
    """模拟「装了代理并写进用户环境变量」的机器（no_proxy=None 表示清空豁免）。"""
    os.environ["HTTP_PROXY"] = FAKE_PROXY
    os.environ["HTTPS_PROXY"] = FAKE_PROXY
    os.environ["ALL_PROXY"] = FAKE_PROXY
    for key in ("no_proxy", "NO_PROXY"):
        if no_proxy is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = no_proxy


print("=== 1. 代理环境下：修复前确实会走代理（复现现场条件） ===")
_set_proxy_env(None)
check("requests 修复前会用代理（不豁免）",
      not req_utils.should_bypass_proxies(f"{CLOUD}/api/ping", None))
check("websocket-client 修复前会用代理",
      get_proxy_info("hn-space.cn", True)[0] == "127.0.0.1",
      str(get_proxy_info("hn-space.cn", True)))
check("urllib 修复前会用代理", not urllib.request.proxy_bypass("hn-space.cn"))

print("\n=== 2. ensure_direct 后三条出网路径都直连 ===")
added = net_direct.ensure_direct(CLOUD, "wss://hn-space.cn")
check("返回新增主机名", added == ["hn-space.cn"], str(added))
check("requests 已直连（should_bypass_proxies=True）",
      req_utils.should_bypass_proxies(f"{CLOUD}/api/ping", None))
check("websocket-client 已直连（engineio 的 websocket 传输）",
      get_proxy_info("hn-space.cn", True) == (None, 0, None),
      str(get_proxy_info("hn-space.cn", True)))
check("urllib 已直连（updater 走它）", urllib.request.proxy_bypass("hn-space.cn"))

print("\n=== 3. 豁免范围不扩大：其它域名照旧走代理 ===")
check("其它域名仍走代理（requests）",
      not req_utils.should_bypass_proxies(f"{OTHER}/x", None))
check("其它域名仍走代理（websocket-client）",
      get_proxy_info("example.org", True)[0] == "127.0.0.1")
check("子域一并覆盖（www.hn-space.cn）",
      req_utils.should_bypass_proxies("https://www.hn-space.cn/x", None))
check("伪造前缀域名不被误豁免（evilhn-space.cn）",
      not req_utils.should_bypass_proxies("https://evilhn-space.cn/x", None))

print("\n=== 4. 幂等 / 与既有豁免项合并 / 脏输入不炸 ===")
check("重复调用不再新增", net_direct.ensure_direct(CLOUD) == [])
before = os.environ["NO_PROXY"]
net_direct.ensure_direct(CLOUD, CLOUD, "", None)
check("重复/空输入不改动 NO_PROXY", os.environ["NO_PROXY"] == before)

_set_proxy_env("localhost,127.0.0.1")
net_direct.ensure_direct("hn-space.cn")
entries = [e.strip() for e in os.environ["NO_PROXY"].split(",")]
check("既有豁免项保留", entries[:2] == ["localhost", "127.0.0.1"], str(entries))
check("新主机追加到尾部", entries[-1] == "hn-space.cn", str(entries))
check("大小写两个变量同步写入",
      os.environ.get("no_proxy") == os.environ.get("NO_PROXY"))
check("裸主机名/带端口/无 scheme 都能取到主机名",
      net_direct._host_of("hn-space.cn") == "hn-space.cn"
      and net_direct._host_of("http://a.b.cn:8080/x") == "a.b.cn"
      and net_direct._host_of("") == "" and net_direct._host_of("://坏") == "")
check("describe() 在代理环境下报告直连",
      "已对系统/环境代理" in net_direct.describe(CLOUD), net_direct.describe(CLOUD))

print("\n=== 5. CloudClient 建连参数 + 失败清理（socketio 用桩） ===")
import cloud_client as cc           # noqa: E402
from cloud_client import CloudClient  # noqa: E402


class _FakeSioClient:
    """socketio.Client 桩：记录构造/connect 参数，可让 connect 抛错。

    disconnect() 的行为对齐真库：连着才回调 disconnect 处理器。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.handlers = {}
        self.connect_calls = []
        self.disconnect_calls = 0
        self.connect_error = None
        self.connected = False

    def on(self, event):
        def deco(fn):
            self.handlers[event] = fn
            return fn
        return deco

    def connect(self, url, **kwargs):
        self.connect_calls.append((url, kwargs))
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        fn = self.handlers.get("connect")
        if fn:
            fn()

    def disconnect(self):
        self.disconnect_calls += 1
        if self.connected:
            self.connected = False
            fn = self.handlers.get("disconnect")
            if fn:
                fn()

    def emit(self, *a, **k):
        pass


_fake_module = types.ModuleType("socketio")
_fake_module.Client = _FakeSioClient
sys.modules["socketio"] = _fake_module

check("超时常量已放大（库默认 5s / 原值 10s）",
      cc.CONNECT_REQUEST_TIMEOUT == 20 and cc.CONNECT_WAIT_TIMEOUT == 25,
      f"{cc.CONNECT_REQUEST_TIMEOUT}/{cc.CONNECT_WAIT_TIMEOUT}")

client = CloudClient(api_url="", ws_url="", token="", client_id="test-device")
msgs = []
client.status_message.connect(msgs.append)
client._stop_event.set()          # 不让成功路径阻塞在等待断开的循环里

client._connect_and_wait()
first = client._sio
check("Client 构造带上了放大的 request_timeout",
      first is not None and first.kwargs.get("request_timeout") == cc.CONNECT_REQUEST_TIMEOUT,
      str(first.kwargs if first else None))
check("connect 用放大的 wait_timeout",
      first.connect_calls and first.connect_calls[0][1].get("wait_timeout") == cc.CONNECT_WAIT_TIMEOUT,
      str(first.connect_calls))
check("连接 URL 带 token/client_id/device_name",
      first.connect_calls and "client_id=test-device" in first.connect_calls[0][0],
      str(first.connect_calls))

# 旧连接的迟到回调不得篡改当前连接状态
stale_connected_before = client._connected
first.handlers["disconnect"]()     # 此时 self._sio 仍指向它 → 应当生效
check("当前连接的 disconnect 回调仍生效", client._connected is False)
client._sio = None                 # 模拟已被替换/释放
client._connected = True
first.handlers["disconnect"]()     # 旧回调 → 应被忽略
check("旧连接的迟到 disconnect 回调被忽略（不篡改当前状态）", client._connected is True)

print("\n=== 6. namespace 握手失败：断开 transport，不泄漏活连接 ===")
client._sio = None
client._connected = False
msgs.clear()
client._release_sio()              # 保证干净起点
# 让下一次建连在 connect 阶段抛错（模拟「One or more namespaces failed to connect」）
orig_client_cls = sys.modules["socketio"].Client
created = []


class _FailingSioClient(_FakeSioClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connect_error = RuntimeError("One or more namespaces failed to connect")
        created.append(self)


sys.modules["socketio"].Client = _FailingSioClient
client._connect_and_wait()
sys.modules["socketio"].Client = orig_client_cls
check("失败后 _sio 已清空（不留活着的 transport）", client._sio is None)
check("失败后对已建立的 transport 调了 disconnect",
      created and created[-1].disconnect_calls == 1,
      f"disconnect_calls={created[-1].disconnect_calls if created else 'n/a'}")
check("失败原因已上报界面日志",
      any("连接失败" in m for m in msgs), str(msgs))

print("\n=== 7. 退出/释放：断开回调照常生效（日志仍保留「已断开云端连接」） ===")
msgs.clear()
client._connect_and_wait()
check("建连成功后 _connected=True 且已注册断开回调",
      client._connected is True and "disconnect" in client._sio.handlers)
client.stop()
check("stop() 后 _connected=False（供 UI 判在线状态）", client._connected is False)
check("stop() 仍上报「已断开云端连接」（现场排查要靠这行日志）",
      any("已断开云端连接" in m for m in msgs), str(msgs))
check("stop() 后 _sio 已清空", client._sio is None)

print("\n=== 8. 静态接线：各出网入口都登记了直连 ===")
for mod, hint in (("cloud_client.py", "云端长连接/HTTP"),
                  ("gui.py", "启动自检"),
                  ("updater.py", "自更新下载"),
                  ("stats_server.py", "收支清算页代理")):
    src = open(os.path.join(LOCAL_TOOL, mod), encoding="utf-8").read()
    check(f"{mod} 调用了 net_direct.ensure_direct（{hint}）",
          "net_direct.ensure_direct" in src)
check("gui.py 启动日志含直连自检结果", "net_direct.describe" in
      open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read())

print("\n=== 9. 断连可诊断：TUN 接管自检 + 断开原因 ===")
# 现场（2026-10-28）：iKuuuVPN 的 TUN 抢走默认路由（198.18.0.2 metric 0）→ 云端流量整条走
# VPN，切换/抖动 VPN 即断连；而日志只有「☁ 已断开云端连接」一句，分不清是断网、VPN 抖动
# 还是服务端踢人。这里钉住两处自检（纯日志，不参与建连/重连决策）。
_real_tunnel = net_direct.tunnel_egress
egress = _real_tunnel()
check("tunnel_egress() 只认 fake-ip 段（198.18/198.19）",
      egress == "" or egress.startswith(("198.18.", "198.19.")), repr(egress))
# 探测本身也要钉住：把 socket 换成桩，验「出口 IP 落在 198.18/198.19 才算 TUN」这段判断
# （只 monkeypatch 返回值的话，真实现返回恒空也没人发现 —— 变异自证时正是这么漏的）。
class _FakeRouteSock:
    def __init__(self, ip):
        self._ip = ip

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, _timeout):
        pass

    def connect(self, _addr):
        pass

    def getsockname(self):
        return (self._ip, 40000)


_real_socket_module = net_direct.socket
try:
    for fake_ip, expect in (("198.18.0.1", "198.18.0.1"), ("198.19.7.7", "198.19.7.7"),
                            ("192.168.153.150", ""), ("10.150.192.5", "")):
        net_direct.socket = types.SimpleNamespace(
            socket=lambda *a, _ip=fake_ip, **k: _FakeRouteSock(_ip),
            AF_INET=_real_socket_module.AF_INET,
            SOCK_DGRAM=_real_socket_module.SOCK_DGRAM)
        got_ip = _real_tunnel()
        check(f"tunnel_egress 出口 {fake_ip} → {expect or '(不算 TUN)'}", got_ip == expect, repr(got_ip))
finally:
    net_direct.socket = _real_socket_module

try:
    net_direct.tunnel_egress = lambda: "198.18.0.1"      # 假装 TUN 接管默认路由
    check("describe() 在 TUN 接管时告警（NO_PROXY 对 TUN 无效）",
          "TUN 隧道接管" in net_direct.describe(CLOUD), net_direct.describe(CLOUD))
    check("告警里带上本机隧道出口地址", "198.18.0.1" in net_direct.describe(CLOUD))
    net_direct.tunnel_egress = lambda: ""                 # 假装没有 TUN
    check("没有 TUN 时不误报", "TUN 隧道接管" not in net_direct.describe(CLOUD),
          net_direct.describe(CLOUD))
finally:
    net_direct.tunnel_egress = _real_tunnel

check("断开原因 client disconnect 翻成人话", "本机主动断开" in cc.disconnect_hint("client disconnect"))
check("断开原因 server disconnect 指向顶号/后端重启",
      "同一设备号" in cc.disconnect_hint("server disconnect"))
check("断开原因 transport error 指向网络/VPN 抖动",
      "VPN" in cc.disconnect_hint("transport error"))
check("拿不到原因时不多写一个字（旧库/桩不传参）",
      cc.disconnect_hint(None) == "" and cc.disconnect_hint("") == "")
check("未识别的原因原样透出，不吞掉", "odd-reason" in cc.disconnect_hint("odd-reason"))

_src = open(os.path.join(LOCAL_TOOL, "cloud_client.py"), encoding="utf-8").read()
check("断开回调接收 reason 并写进日志（静态接线）",
      "def _on_disconnect(reason=None):" in _src
      and '"☁ 已断开云端连接" + disconnect_hint(reason)' in _src)

print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
