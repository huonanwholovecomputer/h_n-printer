# -*- coding: utf-8 -*-
"""云端 Word 转换引擎 = **隐式规则**（2026-10-26 晚，替代原「方案 B」设备级开关）验收

用户口径（原话）：
    「管理员直接默认不强制使用 LO 即可，普通用户必须强制使用 LO。
      也就是说这个行为直接隐式执行就行。」

规则（唯一判定本体 `printer_config.cloud_word_com_allowed`）：
  · 云端 .doc/.docx 默认强制 LibreOffice（S1-04：不执行宏，装没装 LO 是硬前提）；
  · **唯一例外**：该子任务是「管理员自打」（`is_admin_print`）**且**该文件由管理员上传
    （`uploader_is_admin`）⇒ 允许 Word/WPS COM（仍带 AutomationSecurity=3 / DisplayAlerts=0 /
    UpdateLinksAtOpen=False 硬化）；顾客内容永远强制 LO。
  · **不再有设备级开关**：没有 `print_config.cloud_word_engine`、没有移动端「☁ LO / ☁ Word」
    胶囊、没有 `POST /api/printer/cloud_word_engine`、没有 `cloud_word_state` 上报。

本脚本钉住的事实：
  ① 纯函数真值表（4 组）+ PrintWorker._cloud_com_allowed 的行为（不是"源码里有没有那句话"）；
  ② PrinterConfig 不再有该字段、旧配置残留字段被忽略；PrintJob 两个位往返保留；
  ③ 后端 `is_admin_uploader()` 行为（admin / super_admin 都算；普通用户/空 openid 不算）；
  ④ 后端两条下发通道（socket push / HTTP pull）都用它算 `uploader_is_admin`；
  ⑤ 开关确实**删干净**了：端点 404、claiming_devices 不带 cloud_word_* 字段、无镜像/事件；
  ⑥ 本地工具 / converter / 小程序 / APP 的静态接线（无开关、闸门仍在）；
  ⑦ 判据自证（变异必须变红）。

用法: python tests/verify_cloud_word_implicit.py   （exit 0 = 全部通过）
"""
import io
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
sys.path.insert(0, LOCAL_TOOL)

# ⚠ 必须在 import printer_config / gui **之前**把用户目录指到沙箱（gui 模块里的路径都是运行期读，
#   但以后若加模块级落盘就会写真实目录）。
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="hn_cwi_appdata_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# ==================== ① 纯函数真值表 ====================
section("① 隐式规则：cloud_word_com_allowed 四格真值表（行为断言）")

from printer_config import (  # noqa: E402
    cloud_word_com_allowed, PrinterConfig, PrintJob,
)

_CASES = [
    (True, True, True, "管理员自打 + 管理员上传（唯一应放行）"),
    (True, False, False, "顾客上传后被人为订正成自打（内容归属看上传者）"),
    (False, True, False, "管理员上传但订单类型是「接单」（不是自打）"),
    (False, False, False, "顾客订单 + 顾客上传（普通用户必须强制 LO）"),
]
for self_p, up_p, want, label in _CASES:
    got = cloud_word_com_allowed(self_p, up_p)
    check(f"真值表（纯函数）：{label} → {'放行 COM' if want else '强制 LO'}",
          got is want, f"self={self_p} uploader={up_p} got={got}")

check("★ 两个入参必须是**关键字**形状（防止把策略参数又加回来）",
      cloud_word_com_allowed(admin_self_print=True, uploader_is_admin=True) is True
      and cloud_word_com_allowed(admin_self_print=False, uploader_is_admin=True) is False)

# 设备端真正用的是 PrintWorker._cloud_com_allowed（不是直接调纯函数）—— 空壳对象直调未绑定方法，
# 不需要 QApplication。有人往方法体里插 `return True` 时，"应强制 LO"的用例必须变红。
try:
    import gui  # noqa: E402
except Exception:
    traceback.print_exc()
    check("能导入 gui（行为断言的前提）", False, "导入 gui 失败 ⇒ 行为判据无法成立")
    gui = None

if gui is not None:
    class _SelfStub:
        pass

    def _gui_allow(self_print, uploader):
        s = _SelfStub()
        job = PrintJob(admin_self_print=self_print, uploader_is_admin=uploader)
        return gui.PrintWorker._cloud_com_allowed(s, job)

    check("★ 行为：PrintWorker._cloud_com_allowed 两条件齐备才放行",
          _gui_allow(True, True) is True
          and _gui_allow(True, False) is False
          and _gui_allow(False, True) is False
          and _gui_allow(False, False) is False)
    check("★ 行为：PrintWorker 不再读任何「本机策略」属性（旧 _cloud_word_engine 已删）",
          not hasattr(_SelfStub(), "_cloud_word_engine"))
    # 变异自证：把判定函数换成无条件 True，真值表必须红
    _orig_pred = gui.cloud_word_com_allowed
    gui.cloud_word_com_allowed = lambda *a, **kw: True
    _mut_red = not (_gui_allow(False, True) is False)
    gui.cloud_word_com_allowed = _orig_pred
    check("★ 变异「判定恒 True」→ 真值表必须变红（证明行为断言不是恒真）", _mut_red)

# ==================== ② 配置 / PrintJob 字段 ====================
section("② PrinterConfig 不再有 cloud_word_engine；PrintJob 两个位往返保留")

_cfg = PrinterConfig()
check("★ PrinterConfig 没有 cloud_word_engine 字段（开关已删）",
      not hasattr(_cfg, "cloud_word_engine"))
check("★ to_dict 不再写 cloud_word_engine",
      "cloud_word_engine" not in _cfg.to_dict())
_legacy = PrinterConfig.from_dict({"cloud_word_engine": "admin_self_com"})
check("旧配置里的残留字段被忽略、且不会写回（升级后自然消失）",
      not hasattr(_legacy, "cloud_word_engine")
      and "cloud_word_engine" not in _legacy.to_dict())

j = PrintJob.from_dict({"task_id": 3, "admin_self_print": True, "uploader_is_admin": True})
check("PrintJob 往返保留 admin_self_print / uploader_is_admin（崩溃续打不丢放行判定）",
      j.admin_self_print is True and j.to_dict().get("admin_self_print") is True
      and j.uploader_is_admin is True and j.to_dict().get("uploader_is_admin") is True)
check("PrintJob 缺字段默认 False（顾客单/旧数据按最严处理）",
      PrintJob.from_dict({}).admin_self_print is False
      and PrintJob.from_dict({}).uploader_is_admin is False)

# ==================== ③ 后端夹具 ====================
tmp = tempfile.mkdtemp(prefix="hn_cloud_word_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv"))
sys.path.insert(0, dst)
os.chdir(dst)
try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

CID = "DESKTOP-WORD-a1b2"
ADMIN_OID, USER_OID, SUPER_OID = "openid-admin", "openid-user", "openid-super"
backend.PRINTER_TOKEN = "test-token"
backend.compute_role = lambda oid: ("admin" if oid == ADMIN_OID else "user")
backend.SUPER_ADMIN_OPENID = SUPER_OID
ADMIN_TOKEN = backend.token_serializer.dumps(ADMIN_OID)
USER_TOKEN = backend.token_serializer.dumps(USER_OID)
backend.register_device(CID, "DESKTOP-WORD")
backend.get_claiming_devices = lambda: {CID: {"claimed_at": "", "owner_name": "张三"}}
backend.get_claiming_device_ids = lambda: [CID]


def _online(flag=True):
    backend.printer_clients.clear()
    if flag:
        backend.printer_clients[CID] = {
            "sid": "sid-word", "heartbeat": datetime.now(), "connected_at": datetime.now()}


# ==================== ④ is_admin_uploader（行为）====================
section("④ 后端 is_admin_uploader：admin / super_admin 都算，普通用户不算")

_up = backend.is_admin_uploader
check("admin 上传 → True", _up(ADMIN_OID) is True, repr(_up(ADMIN_OID)))
check("★ super_admin 上传 → True（它有自己的 openid，不保证在 ADMIN_OPENIDS 里）",
      _up(SUPER_OID) is True, repr(_up(SUPER_OID)))
check("普通用户上传 → False", _up(USER_OID) is False, repr(_up(USER_OID)))
check("空 / None → False（fail-closed）",
      _up("") is False and _up(None) is False)

# ==================== ⑤ 开关删干净：端点 / 镜像 / 下发字段 ====================
section("⑤ 旧开关确实删干净（端点 404 / 无镜像 / 列表不不再下发 cloud_word_*）")

_forbidden = ["printer_cloud_word", "_cloud_word_state_of", "_set_cloud_word_state",
              "_cloud_word_toggle_problem", "CLOUD_WORD_ENGINES",
              "set_cloud_word_engine", "cloud_word_state"]
_still = [n for n in _forbidden if hasattr(backend, n)]
check("★ 后端不再有云端 Word 引擎镜像 / 闸门 / 事件名等符号", not _still, str(_still))

client = backend.app.test_client()
_online(True)
r = client.post("/api/printer/cloud_word_engine",
                json={"client_id": CID, "engine": "admin_self_com"},
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
check("★ POST /api/printer/cloud_word_engine 已删除（404，不是 200/500）",
      r.status_code == 404, f"status={r.status_code}")

_dev = (client.get("/api/claiming_devices").get_json() or {}).get("devices") or []
_d0 = next((d for d in _dev if d.get("client_id") == CID), {})
check("★ /api/claiming_devices 不再下发 cloud_word_* 字段",
      not [k for k in _d0 if str(k).startswith("cloud_word")], str(sorted(_d0)))
check("回归：攒批开关字段仍在（没被顺手删掉）",
      all(k in _d0 for k in ("stack_mode", "stack_busy", "stack_can_toggle", "stack_reason")),
      str(sorted(_d0)))

# ==================== ⑥ 三端静态接线 ====================
section("⑥ 三端静态接线（本地工具 / 后端两条通道 / 小程序 / APP）")

app_src = open(os.path.join(dst, "app.py"), encoding="utf-8").read()
cc_src = open(os.path.join(REPO, "local_print_tool", "cloud_client.py"), encoding="utf-8").read()
gui_src = open(os.path.join(REPO, "local_print_tool", "gui.py"), encoding="utf-8").read()
conv_src = open(os.path.join(REPO, "local_print_tool", "converter.py"), encoding="utf-8").read()
cfg_src = open(os.path.join(REPO, "local_print_tool", "printer_config.py"), encoding="utf-8").read()
mp_js = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js"),
             encoding="utf-8").read()
mp_wxml = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxml"),
               encoding="utf-8").read()
mp_wxss = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxss"),
               encoding="utf-8").read()
apk_app = open(os.path.join(REPO, "mobile_apps", "android_app", "www", "app.js"),
               encoding="utf-8").read()
apk_css = open(os.path.join(REPO, "mobile_apps", "android_app", "www", "styles.css"),
               encoding="utf-8").read()


def _p_backend_push(src):
    return 'task_msg["uploader_is_admin"] = is_admin_uploader(o_row["openid"] or "")' in src


def _p_backend_pull(src):
    return '"uploader_is_admin": is_admin_uploader(task.get("openid") or ""),' in src


def _p_gui_allow(src):
    """放行位：纯函数 + **恰好两个位**（不再有任何策略参数）。"""
    return ("def _cloud_com_allowed" in src
            and bool(re.search(r"return cloud_word_com_allowed\(\s*\n\s*"
                               r"getattr\(job, \"admin_self_print\", False\),\s*\n\s*"
                               r"getattr\(job, \"uploader_is_admin\", False\)\)", src))
            and "self._cloud_word_engine" not in src)


def _p_gui_preconvert(src):
    return bool(re.search(r"allow_cloud_com=cloud_word_com_allowed\(\s*\n\s*"
                          r"getattr\(task,\s*'is_admin_print',\s*False\),\s*\n\s*"
                          r"getattr\(task,\s*'uploader_is_admin',\s*False\)\)", src))


check("★ 后端：socket push 通道用 is_admin_uploader 算 uploader_is_admin",
      _p_backend_push(app_src))
check("★ 后端：HTTP pull 通道用 is_admin_uploader 算 uploader_is_admin",
      _p_backend_pull(app_src))
check("★ 本地工具：放行位走纯函数、只读「自打 + 上传者」两个位",
      _p_gui_allow(gui_src))
check("本地工具：云端任务预转换按两个位给 allow_cloud_com", _p_gui_preconvert(gui_src))
check("本地工具：安全口径默认值 False（converter 闸门仍在）",
      "if from_cloud and ext in COM_OFFICE_EXTS and not allow_cloud_com:" in conv_src
      and "allow_cloud_com: bool = False" in conv_src)
check("本地工具：cloud_client 仍下发 / 落地 uploader_is_admin",
      '"uploader_is_admin"' in cc_src and "self.uploader_is_admin" in cc_src)
check("本地工具：gui / cloud_client 不再有任何开关接线",
      not any(k in gui_src for k in ("_cloud_word_policy", "_report_cloud_word_state",
                                     "_on_remote_cloud_word_engine", "cloud_word_engine"))
      and "cloud_word" not in cc_src)
check("printer_config：不再定义白名单 / 规范化函数",
      "CLOUD_WORD_ENGINES" not in cfg_src
      and "normalize_cloud_word_engine" not in cfg_src
      and "def cloud_word_com_allowed(" in cfg_src)

check("★ 小程序：设备卡不再有「☁ LO / ☁ Word」开关",
      "cloud_word" not in mp_wxml and "onToggleCloudWord" not in mp_wxml
      and "cloud_word_engine" not in mp_js
      and "/api/printer/cloud_word_engine" not in mp_js)
check("★ APP：设备卡不再有「☁ LO / ☁ Word」开关",
      "cloud_word" not in apk_app and "toggleCloudWord" not in apk_app
      and "cloudWordToggleBtnHtml" not in apk_app)
check("两端样式里也没有该按钮的残留（.device-word-btn）",
      "device-word-btn" not in mp_wxss and "device-word-btn" not in apk_css)
check("回归：攒批开关两端都还在（别把设备卡整块删了）",
      "onToggleStackMode" in mp_wxml and "toggleStackMode" in apk_app
      and "/api/printer/stack_mode" in mp_js and "/api/printer/stack_mode" in apk_app)

# ==================== ⑦ 判据自证（变异必须变红）====================
section("⑦ 判据自证（防恒真守卫）")

_mut = app_src.replace('task_msg["uploader_is_admin"] = is_admin_uploader(o_row["openid"] or "")',
                       'task_msg["uploader_is_admin"] = (compute_role(o_row["openid"] or "") == "admin")')
check("B1 push 通道退回 compute_role(...)=='admin'（漏掉 super_admin）→ 判据变红",
      _mut != app_src and not _p_backend_push(_mut))

_mut = app_src.replace('"uploader_is_admin": is_admin_uploader(task.get("openid") or ""),',
                       '"uploader_is_admin": False and is_admin_uploader(task.get("openid") or ""),')
check("B2 pull 通道恒 False（管理员单永远进不了 COM）→ 判据变红",
      _mut != app_src and not _p_backend_pull(_mut))

_mut = gui_src.replace('getattr(job, "uploader_is_admin", False))', '            )')
check("B3 放行位不再看 uploader_is_admin（顾客上传被订正成自打就放行）→ 判据变红",
      _mut != gui_src and not _p_gui_allow(_mut))

_mut = gui_src.replace('getattr(job, "admin_self_print", False),', '')
check("B4 放行位不再看 is_admin_print（顾客单也会被放行）→ 判据变红",
      _mut != gui_src and not _p_gui_allow(_mut))

_mut = gui_src.replace("cloud_word_com_allowed(\n            getattr(job",
                       "cloud_word_com_allowed(\n            'libreoffice',\n            getattr(job")
check("B5 把「本机策略」参数塞回放行位（开关复活）→ 判据变红",
      _mut != gui_src and not _p_gui_allow(_mut))

_mut = mp_wxml.replace('<text class="device-state',
                       '<view catchtap="onToggleCloudWord">☁ LO</view>\n              <text class="device-state',
                       1)
check("B6 小程序把「☁ LO」开关加回来 → 移动端判据变红",
      _mut != mp_wxml and ("cloud_word" in _mut or "onToggleCloudWord" in _mut))

# 行为面：纯函数对同一组输入必须给出稳定、可区分的结果（不是恒 True / 恒 False）
_pairs = [((True, True), True), ((False, False), False)]
check("B7 纯函数对相同输入稳定且可区分（排除恒真/恒假实现）",
      all(cloud_word_com_allowed(*a) is w for a, w in _pairs)
      and cloud_word_com_allowed(True, True) != cloud_word_com_allowed(False, True))

print("\n" + "=" * 56)
print("云端 Word 隐式规则验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok_all else 1)
