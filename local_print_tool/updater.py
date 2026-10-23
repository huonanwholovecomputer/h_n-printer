"""
updater.py — 本地打印工具自更新模块（v4.5）

安全加固（S1-02 / 审计 2026-10-23 DS-03、DS-05），流程顺序**不可调换**：
  1. 下载清单            GET https://hn-space.cn/updates/update.json
  2. 结构 + host 白名单   只允许 https + {hn-space.cn, COS 桶}；重定向后**再次**校验
  3. 哈希字段必填         sha256 优先，md5 兼容；缺失/格式非法 = 整份清单作废
  4. ed25519 验签         公钥内置客户端（UPDATE_PUBLIC_KEY_B64），离线签名工具
                          local_print_tool/tools/sign_update_manifest.py
  5. 下载安装包          同上白名单；落到私有目录 %LOCALAPPDATA%\\HN打印工具\\updates\\<随机子目录>
  6. 校验哈希             复用下载时的哈希句柄 + 落盘后复算一次（缩小 TOCTOU 窗口）
  7. 校验通过再重命名到位  ready\\<fname>；上一版安装包归档到 previous\\（回滚用）
  8. 用户明确点击后才生成 update.cmd（/VERYSILENT 静默安装）；启动检查只提示

为什么是这个顺序：清单是整个更新链的"授权根"。若先下包后验签，攻击者已经拿到一次
"以管理员权限运行任意安装包"的机会（UAC 弹窗一旦被点过就来不及了）。所以**验签失败
必须在发起安装包下载之前返回**。

决策依据（docs/安全加固修复计划_2026-10-23.md §S1-02 / §11 D-1）：
**不买代码签名证书** ⇒ host 白名单 + ed25519 验签 + 哈希必填 + 关闭静默装就是最终形态，
不是过渡方案。"未知发布者"的 UAC 警告是已接受的残余风险。

兼容窗口（必须读懂再用）：
  存量门店客户端（≤4.5.32）只认 md5，且**服务器上的 update.json 目前没有 sig 字段**。
  为了让新版客户端（4.5.33+）在发布流程尚未改造的窗口内仍能升级，本模块对
  **"清单缺少 sig"** 采取：告警 + 继续用哈希校验放行（`SIG_UNSIGNED`）；
  而 **"sig 存在但验签失败"** 一律硬拒绝（改清单 = 全店任意代码执行，这是本次要堵的路）。
  TODO(S1-02 兼容期解除条件)：所有门店客户端 ≥ 4.5.33、且发布流程已签名后，把
  ALLOW_UNSIGNED_MANIFEST 置 False（改为缺 sig 也硬拒绝），并删掉本条告警路径。

约定（与部署侧保持一致）：
- 更新清单：https://hn-space.cn/updates/update.json
  {"version": "4.5.33",
   "url": "https://hn-printer-updates-1324480550.cos.ap-guangzhou.myqcloud.com/h_n-printer_setup_4.5.33.exe",
   "sha256": "…64 位十六进制…",           # 优先；没有则用 md5（两者都缺 = 拒绝）
   "md5": "…32 位十六进制…",              # 兼容保留
   "sig_algo": "ed25519",                 # 可选；出现则必须是 ed25519
   "sig": "…base64(ed25519 签名)…",       # 签名载荷见 canonical_manifest_payload()
   "notes": "…"}
- 安装包：Inno Setup 产物（英文文件名，避免 cmd/路径编码问题），静默参数
  /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-
- 安装目录：C:\\Program Files\\h_n printer（英文名，无中文路径）
- 用户数据在 %APPDATA%\\HN打印工具（paths.py），安装覆盖程序目录不影响配置
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import net_direct  # 更新流量也走云端直连（见 net_direct.py）

logger = logging.getLogger(__name__)

UPDATE_MANIFEST_URL = "https://hn-space.cn/updates/update.json"
FETCH_TIMEOUT = 10
DOWNLOAD_CHUNK = 65536
_WAIT_MAX_LOOPS = 60  # update.cmd 轮询等待主程序退出上限（约 2 分钟）
MAX_REDIRECTS = 3     # 更新链只允许极少量跳转（COS 回源/CDN 常见 0~1 次）
MAX_MANIFEST_BYTES = 512 * 1024

# ── host 白名单（改动 1）────────────────────────────────────────────────────────
# 只允许两个域：
#   hn-space.cn                                       —— 清单与服务器上托管的安装包
#   hn-printer-updates-1324480550.cos.ap-guangzhou.myqcloud.com  —— 腾讯云 COS 桶（exe 本体）
# 为什么必须白名单：校验"scheme 是 https"只保证传输加密，**不保证下载源是谁**。
# 任何人只要能让清单里的 url 指向自己的 https 域名（改清单 / 中间人 / 撞库），
# 就能让全部门店以管理员权限装他给的 exe。白名单把"下载源"收敛到发布者自有资产。
ALLOWED_DOWNLOAD_SCHEMES = ("https",)
ALLOWED_DOWNLOAD_PORTS = (443,)
ALLOWED_DOWNLOAD_HOSTS = (
    "hn-space.cn",
    "hn-printer-updates-1324480550.cos.ap-guangzhou.myqcloud.com",
)

# ── 清单签名（改动 3）───────────────────────────────────────────────────────────
# 内置的是 **ed25519 公钥**（raw 32 字节的 base64）。私钥不入库，发布者在自己的机器上
# 用 tools/sign_update_manifest.py 签名。换密钥 = 换客户端（旧客户端只认内置公钥）。
UPDATE_PUBLIC_KEY_B64 = "gs0Y8Jwqnq11s8lKFHjV8a+0J4GneboDpqJbMBLTPos="
SIG_ALGO = "ed25519"
# 兼容期开关：缺少 sig 时是否放行。解除条件见文件头 TODO（所有门店 ≥4.5.33）。
ALLOW_UNSIGNED_MANIFEST = True

# ── 哈希字段（改动 2）───────────────────────────────────────────────────────────
# sha256 优先，md5 保留兼容（存量发布脚本仍在算 md5）。两者都没有 = 拒绝更新。
HASH_ALGO_PRIORITY = ("sha256", "md5")
_HASH_HEX_LEN = {"sha256": 64, "md5": 32}
_HASH_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

# 私有落地目录（改动 5）：%LOCALAPPDATA%\HN打印工具\updates
# 绝不落在 %TEMP%：%TEMP% 是"同用户任意进程可写"，安装包在哈希校验之后、UAC 确认之前
# 被替换就是一次"点一下 UAC 执行任意程序"（TOCTOU，审计 LT-13）。
APP_DIR_NAME = "HN打印工具"
UPDATES_DIR_NAME = "updates"
ROLLBACK_NOTE_NAME = "ROLLBACK_README.txt"

# 签名校验结果（verify_manifest_signature 的返回状态）
SIG_OK = "ok"
SIG_UNSIGNED = "unsigned"

# 清单被拒的最近原因（fetch_update_info 返回 None 时供 GUI 显示具体原因）
_last_reject_reason = ""


class UpdateCancelled(Exception):
    """用户取消下载。"""


class VersionFormatError(ValueError):
    """版本号含非数字段（如 "4.5.13a"）。"""


class ManifestError(ValueError):
    """更新清单未通过安全校验（host / 哈希 / 签名）。整份清单作废。"""


def last_reject_reason() -> str:
    """最近一次 fetch_update_info 拒绝清单的原因（空串 = 不是安全拒绝）。"""
    return _last_reject_reason


def _set_reject_reason(reason: str) -> None:
    global _last_reject_reason
    _last_reject_reason = str(reason or "")


# ============================================================================
# URL / 重定向校验（改动 1）
# ============================================================================

def _norm_host(host: str | None) -> str:
    return str(host or "").strip().lower().rstrip(".")


def validate_download_url(url: str) -> str:
    """校验清单里的下载地址：必须 https + host 在白名单内；返回原 url。

    拒绝的情形（一律 ValueError，调用方当"整份清单作废/拒绝下载"处理）：
      · 空地址、非 allowed scheme（http / ftp / file / data …）
      · host 不在 ALLOWED_DOWNLOAD_HOSTS（含空 host、IP 直连、同形异义域）
      · 带 userinfo（https://user:pass@host/…，常被用来伪装真实 host）
      · 非默认端口（白名单域名之外的 8080/8443 等也可能被劫持）
    """
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("下载地址为空")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise ValueError(
            f"拒绝非 HTTPS 下载地址（scheme={scheme or '空'}）: {raw[:120]}")
    if parts.username or parts.password:
        raise ValueError(f"下载地址不允许携带用户名/口令: {raw[:120]}")
    host = _norm_host(parts.hostname)
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError(f"下载地址 host 不在白名单（{host or '空'}）: {raw[:120]}")
    try:
        port = parts.port
    except ValueError as e:  # 非法端口（如 https://host:abc/）
        raise ValueError(f"下载地址端口非法: {raw[:120]}") from e
    if port is not None and port not in ALLOWED_DOWNLOAD_PORTS:
        raise ValueError(f"下载地址端口不在允许范围（得到 {port}）: {raw[:120]}")
    return raw


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随重定向，但**每一次跳转都重新过白名单**。

    旧实现只校验初始 URL：https://hn-space.cn/… 可以 302 到 http://… 或任意第三方域，
    urlopen 默认会照跟 ⇒ 白名单等于没装（审计 LT-13「3xx 可降级」）。
    """

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        # urllib 已把 newurl 转为绝对地址；这里只看它本身，不继承初始 url 的任何信任。
        try:
            validate_download_url(newurl)
        except ValueError as e:
            logger.error(f"拒绝重定向：{e}")
            raise urllib.error.URLError(f"重定向目标被拒绝: {e}") from e
        logger.info(f"更新链重定向 {code} → {urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_AllowlistRedirectHandler())


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url, headers={"User-Agent": "HN-Print-Updater/2.0"})


def _check_final_url(resp) -> None:
    """重定向后的**最终**地址再校验一次（handler 之外的第二道闸，防 handler 被绕过）。"""
    final = resp.geturl()
    try:
        validate_download_url(final)
    except ValueError as e:
        raise ManifestError(f"下载最终地址非法（重定向后）: {e}") from e


def _read_url(url: str, timeout: int, max_bytes: int) -> bytes:
    """读取一个白名单 URL（带重定向白名单与大小上限）。"""
    validate_download_url(url)
    net_direct.ensure_direct(url)
    opener = _build_opener()
    with opener.open(_request(url), timeout=timeout) as resp:
        _check_final_url(resp)
        status = getattr(resp, "status", 200) or 200
        if status != 200:
            raise ValueError(f"HTTP {status}")
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"响应体超过上限 {max_bytes} 字节")
    return data


# ============================================================================
# 清单哈希字段（改动 2）
# ============================================================================

def expected_hash(manifest: dict) -> tuple[str, str]:
    """从清单取期望哈希 → (algo, hex)。sha256 优先；缺失或格式非法抛 ManifestError。

    为什么必填：旧实现对缺 md5 只 warning，然后 `download_setup` 在"本地已有同名文件
    且 expected_md5 为空"时**直接复用**（零校验）—— 一条 `rm md5` 就能让整条更新链
    失去完整性校验。现在缺哈希 = 拒绝更新。

    sha256 字段存在但格式非法时**不回落 md5**（那看起来像攻击者想让我们忽略新字段）。
    """
    sha = str(manifest.get("sha256") or "").strip()
    md5 = str(manifest.get("md5") or "").strip()
    if sha:
        if len(sha) != _HASH_HEX_LEN["sha256"] or not _HASH_HEX_RE.match(sha):
            raise ManifestError(f"sha256 字段格式非法（应为 64 位十六进制）: {sha[:80]!r}")
        return "sha256", sha.lower()
    if md5:
        if len(md5) != _HASH_HEX_LEN["md5"] or not _HASH_HEX_RE.match(md5):
            raise ManifestError(f"md5 字段格式非法（应为 32 位十六进制）: {md5[:80]!r}")
        return "md5", md5.lower()
    raise ManifestError(
        "清单缺少 sha256/md5 字段（哈希必填：缺失即拒绝更新，"
        "不再只依赖 HTTPS 传输完整性）")


def _new_hasher(algo: str):
    if algo == "sha256":
        return hashlib.sha256()
    if algo == "md5":
        return hashlib.md5()
    raise ValueError(f"不支持的哈希算法: {algo!r}")


def hash_file(path: str, algo: str) -> str:
    """对文件算哈希（只读句柄，用于落盘后复算）。"""
    hasher = _new_hasher(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_file_hash(path: str, algo: str, expected_hex: str) -> bool:
    """落盘文件哈希是否匹配（异常/读不到一律 False）。"""
    try:
        return hash_file(path, algo) == str(expected_hex or "").lower()
    except OSError as e:
        logger.warning(f"读取文件失败，无法校验哈希: {path}: {e}")
        return False


# ============================================================================
# 清单签名（改动 3）
# ============================================================================

def canonical_manifest_payload(manifest: dict) -> bytes:
    """签名载荷：清单去掉 sig / sig_algo 后的规范化 JSON（sort_keys + 紧凑分隔符）。

    签名工具（tools/sign_update_manifest.py）必须调用同一个函数生成载荷，
    否则"签了个别的字节"这种错误只有在门店机更新时才会炸。
    """
    payload = {k: v for k, v in dict(manifest or {}).items()
               if k not in ("sig", "sig_algo")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _public_key(public_key_b64: str | None = None):
    """加载内置 ed25519 公钥（延迟 import cryptography，见 requirements.txt / .spec）。"""
    key_b64 = (public_key_b64 or UPDATE_PUBLIC_KEY_B64 or "").strip()
    if not key_b64 or key_b64.startswith("__"):
        raise ManifestError("客户端未内置更新公钥，无法校验清单签名")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as e:  # 打包漏收 cryptography 时会走到这里（.spec hiddenimports）
        raise ManifestError(
            "缺少 cryptography 依赖，无法校验更新清单签名（打包需收集该库）") from e
    try:
        raw = base64.b64decode(key_b64, validate=True)
    except Exception as e:
        raise ManifestError(f"内置更新公钥不是合法 base64: {e}") from e
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_manifest_signature(manifest: dict, public_key_b64: str | None = None) -> str:
    """校验清单签名。返回 SIG_OK / SIG_UNSIGNED；**失败一律抛 ManifestError**。

    · sig 缺失：兼容期返回 SIG_UNSIGNED（调用方告警后继续用哈希校验）。
      理由：服务器上现存的 update.json 还没有 sig，而正在升级的这批门店客户端必须
      能先升到 4.5.33（否则要人工逐店部署）。解除条件见文件头 TODO。
    · sig 存在但验签失败 / 算法不认识 / base64 损坏：硬拒绝 —— 这正是"改清单即可
      全店提权"的攻击路径，绝不能降级成告警。
    """
    sig = str(manifest.get("sig") or "").strip()
    algo = str(manifest.get("sig_algo") or SIG_ALGO).strip().lower()
    if not sig:
        return SIG_UNSIGNED
    if algo != SIG_ALGO:
        raise ManifestError(f"不支持的清单签名算法: {algo!r}（只接受 {SIG_ALGO}）")
    try:
        from cryptography.exceptions import InvalidSignature
    except ImportError as e:
        raise ManifestError(
            "缺少 cryptography 依赖，无法校验更新清单签名（打包需收集该库）") from e
    key = _public_key(public_key_b64)
    try:
        sig_bytes = base64.b64decode(sig, validate=True)
    except Exception as e:
        raise ManifestError(f"清单 sig 字段不是合法 base64: {e}") from e
    try:
        key.verify(sig_bytes, canonical_manifest_payload(manifest))
    except InvalidSignature as e:
        raise ManifestError("清单签名校验失败（sig 与清单内容不匹配，已拒绝更新）") from e
    except Exception as e:  # 长度非法等
        raise ManifestError(f"清单签名校验失败: {e}") from e
    return SIG_OK


# ============================================================================
# 清单校验 / 拉取
# ============================================================================

def validate_manifest(manifest: dict) -> dict:
    """校验整份清单的安全字段；不合法抛 ManifestError。

    顺序：结构 → 版本 → 下载 URL（host 白名单）→ **验签** → 哈希字段。
    验签放在哈希之前是刻意的：哈希字段本身也在签名保护范围内，先验签才能信任它。
    """
    if not isinstance(manifest, dict):
        raise ManifestError("更新清单不是 JSON 对象")
    if not str(manifest.get("version") or "").strip():
        raise ManifestError("更新清单缺少 version 字段")
    manifest = dict(manifest)
    try:
        manifest["url"] = validate_download_url(manifest.get("url", ""))
    except ValueError as e:
        # 统一成 ManifestError：调用方只需处理一种"清单不可信"异常
        raise ManifestError(str(e)) from e

    state = verify_manifest_signature(manifest)
    if state == SIG_UNSIGNED:
        # 兼容期：告警 + 继续（见文件头 TODO 与 ALLOW_UNSIGNED_MANIFEST）
        if not ALLOW_UNSIGNED_MANIFEST:
            raise ManifestError("清单缺少 sig 字段（签名已强制，验签失败即拒绝更新）")
        logger.warning(
            "更新清单缺少 sig 字段：兼容期仅按哈希校验（解除条件：所有门店 ≥4.5.33）")

    expected_hash(manifest)  # 缺失/非法 → ManifestError
    return manifest


def fetch_update_info(timeout: int = FETCH_TIMEOUT) -> dict | None:
    """拉取并校验更新清单。任何安全校验失败返回 None（并记 ERROR + last_reject_reason）。

    网络失败/格式错误同样返回 None（静默，不打扰用户），但不设置 last_reject_reason
    （GUI 据此区分"网络问题"与"清单被拒"）。"""
    _set_reject_reason("")
    try:
        raw = _read_url(UPDATE_MANIFEST_URL, timeout, MAX_MANIFEST_BYTES)
        data = json.loads(raw.decode("utf-8"))
    except ManifestError as e:
        _set_reject_reason(str(e))
        logger.error(f"更新清单下载地址非法，拒绝该清单: {e}")
        return None
    except Exception as e:
        logger.info(f"检查更新失败（忽略）: {e}")
        return None

    if not isinstance(data, dict) or not data.get("version"):
        logger.warning(f"更新清单格式异常: {data!r}")
        return None
    try:
        data = validate_manifest(data)
    except (ManifestError, ValueError) as e:
        _set_reject_reason(str(e))
        logger.error(f"更新清单未通过安全校验，拒绝更新: {e}")
        return None
    return data


# ============================================================================
# 版本号
# ============================================================================

_VERSION_SEG_RE = re.compile(r"^[0-9]+$")


def parse_version(v: str) -> list[int]:
    """解析点分数字版本号。任一段非纯数字（如 "13a"）→ 抛 VersionFormatError。

    P1（审计 2026-12）：非数字段**报错**而不是静默归零——旧实现把
    `"4.5.13a"` 归零成 `[4,5,0]`，反而小于 `"4.5.13"`，于是带后缀的 hotfix
    版本所有用户永远收不到更新（静默失效，最难排查）。
    """
    segs = str(v).strip().split(".")
    out: list[int] = []
    for seg in segs:
        if not _VERSION_SEG_RE.match(seg):
            raise VersionFormatError(f"版本号含非数字段: {v!r}（段 {seg!r}）")
        out.append(int(seg))
    return out


def compare_versions(current: str, latest: str) -> bool:
    """latest > current 返回 True。按 . 分段数字比较（4.4.10 > 4.4.9）。

    任一侧版本号格式非法 → 抛 VersionFormatError，由调用方显式提示
    （不允许"静默当作无更新"）。
    """
    try:
        return parse_version(latest) > parse_version(current)
    except VersionFormatError:
        logger.error(f"版本号格式非法，无法比较: current={current!r}, latest={latest!r}")
        raise


# ============================================================================
# 私有落地目录（改动 5）
# ============================================================================

def updates_root() -> str:
    """更新包私有根目录：%LOCALAPPDATA%\\HN打印工具\\updates。

    **不回落 %TEMP%**：%TEMP% 同用户任意进程可写，安装包在校验后、UAC 前可被替换。
    极端环境（无 LOCALAPPDATA）退回 APPDATA / 用户目录，仍不用 %TEMP%。
    """
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.expanduser("~"))
    return os.path.join(base, APP_DIR_NAME, UPDATES_DIR_NAME)


def _restrict_dir_private(path: str) -> None:
    """尽量把目录 ACL 收紧到"仅当前用户"（best-effort：失败只记 debug，不影响更新）。

    Windows 上 %LOCALAPPDATA% 本身已按用户隔离；这里再去掉继承 ACE，防同机器其它
    用户（或提权前的低权限进程）在"校验完成 → UAC 确认"窗口里替换安装包。
    """
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or ""
            if not user:
                return
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                capture_output=True, timeout=15, check=False)
        else:
            os.chmod(path, 0o700)
    except Exception as e:  # noqa: BLE001 —— 收紧 ACL 失败不应阻断更新
        logger.debug(f"收紧更新目录 ACL 失败（忽略）: {path}: {e}")


def _prepare_private_dirs(root: str) -> tuple[str, str]:
    """建好 root / ready / previous，返回 (ready_dir, previous_dir)。"""
    os.makedirs(root, exist_ok=True)
    _restrict_dir_private(root)
    ready = os.path.join(root, "ready")
    previous = os.path.join(root, "previous")
    for d in (ready, previous):
        os.makedirs(d, exist_ok=True)
    return ready, previous


def _write_rollback_note(root: str, exe_dir: str = "") -> None:
    """写回滚说明（只写一次；更新器只升不降，回滚=发更高版本的回退包）。"""
    path = os.path.join(root, ROLLBACK_NOTE_NAME)
    if os.path.isfile(path):
        return
    target = exe_dir or r"C:\Program Files\h_n printer"
    text = (
        "HN 打印工具 · 更新包目录说明 / 手动回滚\r\n"
        "====================================================\r\n"
        "ready\\     最近一次下载并通过哈希校验的安装包（安装成功后由 update.cmd 删除）。\r\n"
        "previous\\  上一版安装包（只保留 1 份），可用于手动回滚。\r\n"
        "\r\n"
        "手动回滚（需要管理员权限）：\r\n"
        f'    previous\\ 里的安装包 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="{target}"\r\n'
        "\r\n"
        "注意：更新器只升不降 —— 回滚到较低版本后，客户端下次检查更新会再次提示升级。\r\n"
        "正式回滚应由发布者发一个**版本号更高、内容为回退代码**的安装包。\r\n"
        "\r\n"
        "目录 ACL 已尽量收紧为仅当前用户；请勿把本目录共享或复制到公共路径。\r\n"
    )
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as e:
        logger.debug(f"写回滚说明失败（忽略）: {e}")


def _cleanup_stale_setups(dest_dir: str, keep_fname: str,
                          exts: tuple[str, ...] = (".exe", ".part", ".tmp")) -> None:
    """清理更新目录中的残留（防堆积）：保留 keep_fname，删其它命中 exts 的文件。

    ⚠ ready\\ 目录**只清 .part/.tmp**（调用方传 exts=(".part", ".tmp")）：
    旧写法把 .exe 一起清掉，会在 `_archive_previous` 之前就把上一版安装包删了，
    等于"保留上一版做回滚"形同虚设（S1-02 改动 5 的回滚材料）。"""
    try:
        for name in os.listdir(dest_dir):
            if name == keep_fname:
                continue
            if name.lower().endswith(tuple(exts)):
                try:
                    os.remove(os.path.join(dest_dir, name))
                except OSError:
                    pass
    except OSError:
        pass


def _archive_previous(ready_dir: str, previous_dir: str, keep_fname: str) -> None:
    """把 ready\\ 里的上一版安装包挪到 previous\\（只保留 1 份）。"""
    try:
        names = sorted(
            n for n in os.listdir(ready_dir)
            if n != keep_fname and n.lower().endswith(".exe"))
    except OSError:
        return
    for name in names:
        src = os.path.join(ready_dir, name)
        try:
            # 先把 previous 里的旧包清掉（只留最新这一份），再挪过去
            for old in os.listdir(previous_dir):
                if old.lower().endswith(".exe") and old != name:
                    try:
                        os.remove(os.path.join(previous_dir, old))
                    except OSError:
                        pass
            os.replace(src, os.path.join(previous_dir, name))
            logger.info(f"上一版安装包已归档：previous\\{name}")
        except OSError as e:
            logger.debug(f"归档上一版安装包失败（忽略）: {e}")


# ============================================================================
# 下载 + 校验（改动 2/5）
# ============================================================================

def _safe_setup_name(url: str) -> str:
    """从 URL 取安装包文件名（去 query，禁路径分隔符，兜底 setup.exe）。"""
    name = os.path.basename(urlsplit(url).path) or "setup.exe"
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(". ")
    if not name.lower().endswith(".exe"):
        name = (name or "setup") + ".exe"
    return name


def _infer_algo(expected_hash_hex: str, algo: str | None) -> str:
    if algo:
        if algo not in _HASH_HEX_LEN:
            raise ValueError(f"不支持的哈希算法: {algo!r}")
        return algo
    n = len(str(expected_hash_hex or ""))
    for name, ln in _HASH_HEX_LEN.items():
        if n == ln:
            return name
    raise ValueError(f"哈希长度非法（{n}），无法判断算法")


def download_setup(url: str, expected_hash: str, dest_dir: str,
                   progress_cb=None, algo: str | None = None) -> str | None:
    """下载安装包 → 校验哈希 → 落到私有目录 ready\\。返回最终路径，失败/取消返回 None。

    dest_dir 作为**私有根目录**（默认 updates_root()）；下载先落在 root 下的随机子目录
    `dl-xxxx\\`（半截文件不污染 ready\\），校验通过后才 `os.replace` 进 `ready\\`
    （同卷原子重命名），随后对**落盘文件**再复算一次哈希，把"校验的是 tmp、执行的是
    dest"的 TOCTOU 窗口压到最小。

    本地已有同目标文件且哈希匹配时直接复用（UAC 拒绝/安装失败后安装包被保留，
    下次检查更新无需重新下载）。

    progress_cb(downloaded, total) 在下载循环中回调；返回 False 表示用户取消
    （抛 UpdateCancelled 并清理半截文件）。
    """
    try:
        url = validate_download_url(url)
    except ValueError as e:
        logger.error(f"安装包下载地址非法，拒绝下载: {e}")
        return None
    if not str(expected_hash or "").strip():
        # 哈希必填（fail-closed）：没有期望哈希就不下载、更不复用本地文件
        logger.error("拒绝下载：未提供期望哈希（sha256/md5 必填）")
        return None
    try:
        inferred = _infer_algo(expected_hash, algo)
    except ValueError as e:
        logger.error(f"拒绝下载：{e}")
        return None
    expected = str(expected_hash).strip().lower()
    if not _HASH_HEX_RE.match(expected) or len(expected) != _HASH_HEX_LEN[inferred]:
        logger.error(f"拒绝下载：期望哈希格式非法（{inferred}）: {expected[:80]!r}")
        return None

    root = dest_dir or updates_root()
    ready_dir, previous_dir = _prepare_private_dirs(root)
    exe_dir = os.path.dirname(os.path.abspath(__import__("sys").executable))
    _write_rollback_note(root, exe_dir if getattr(__import__("sys"), "frozen", False) else "")

    fname = _safe_setup_name(url)
    final_path = os.path.join(ready_dir, fname)
    # 只清 ready\ 里的半截文件；**不删 .exe** —— 上一版安装包要留给 _archive_previous
    _cleanup_stale_setups(ready_dir, fname, exts=(".part", ".tmp"))

    # 本地复用：哈希匹配才算数（旧实现"expected 为空也复用"= 零校验，已删）
    if os.path.isfile(final_path) and verify_file_hash(final_path, inferred, expected):
        logger.info(f"复用本地已校验的安装包: {final_path}")
        if progress_cb:
            try:
                size = os.path.getsize(final_path)
                progress_cb(size, size)
            except Exception:
                pass
        return final_path

    staging = tempfile.mkdtemp(prefix="dl-", dir=root)
    _restrict_dir_private(staging)
    part = os.path.join(staging, fname + ".part")
    net_direct.ensure_direct(url)  # 安装包在 COS 等域名，同样绕开代理
    try:
        fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            hasher = _new_hasher(inferred)
            opener = _build_opener()
            with opener.open(_request(url), timeout=120) as resp:
                _check_final_url(resp)
                status = getattr(resp, "status", 200) or 200
                if status != 200:
                    raise ValueError(f"HTTP {status}")
                total = int(resp.headers.get("Content-Length", 0) or 0)
                downloaded = 0
                chunk_idx = 0
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        chunk_idx += 1
                        # 约每 1MB 回调一次，避免高频信号刷 UI
                        if chunk_idx % 16 == 0 or downloaded >= total:
                            if progress_cb(downloaded, total) is False:
                                raise UpdateCancelled("用户取消下载")
            f.flush()
            os.fsync(f.fileno())
            actual = hasher.hexdigest()
            if actual != expected:
                raise ManifestError(
                    f"安装包 {inferred} 校验失败: 期望 {expected}, 实际 {actual}")
        # 校验通过 → 同卷原子重命名进 ready\；再对落盘文件复算一次（缩小 TOCTOU）
        _archive_previous(ready_dir, previous_dir, fname)
        os.replace(part, final_path)
        if not verify_file_hash(final_path, inferred, expected):
            logger.error("安装包落盘后哈希不一致（疑似被替换），已删除")
            try:
                os.remove(final_path)
            except OSError:
                pass
            return None
        logger.info(f"安装包已就绪（{inferred} 校验通过）: {final_path}")
        return final_path
    except UpdateCancelled:
        logger.info("下载被用户取消")
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    except Exception as e:  # noqa: BLE001 —— 网络/校验/磁盘任何失败都只返回 None
        logger.error(f"下载或校验安装包失败: {e}")
        try:
            os.remove(part)
        except OSError:
            pass
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ============================================================================
# update.cmd（改动 4：静默安装必须由用户明确点击触发）
# ============================================================================

def write_update_cmd(setup_path: str, exe_path: str, cmd_path: str,
                     start_cmd: str = "", install_dir: str = "",
                     allow_silent_install: bool = False) -> bool:
    """生成 update.cmd：轮询等主程序退出 → 静默安装 → 启动新版本 → 把安装包归档到 previous\\。

    `allow_silent_install=False`（默认）直接抛 ValueError：`/VERYSILENT` 相当于
    "以管理员权限在无界面下改程序目录"，**只能在用户明确点了「立即更新」之后**生成。
    启动时的自动检查只提示，绝不走到这里（S1-02 改动 4）。

    cmd 按本地代码页（GBK）解析，文件用 gbk 编码写，避免中文路径乱码。
    start_cmd：启动新程序的完整命令（可含 cd /d + 参数）；为空时默认
    `start "" "{exe_path}"`（打包版直接启动 exe）。
    install_dir：显式指定安装目录（/DIR），默认空 = 用安装包编译时的 DefaultDirName。
    **关键**：必须传当前程序所在目录——否则发布者修改默认安装位置后，已装旧位置的
    用户更新会把新版装到新默认目录，而启动命令仍指向旧位置，导致"更新无效"。"""
    if not allow_silent_install:
        raise ValueError(
            "拒绝生成静默安装脚本：必须先获得用户明确确认（启动检查只提示，不自动安装）")
    exe_name = os.path.basename(exe_path)
    if not start_cmd:
        start_cmd = f'start "" "{exe_path}"'
    if install_dir:
        install_line = (f'"{setup_path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-'
                        f' /DIR="{install_dir}"')
    else:
        install_line = f'"{setup_path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-'
    # 回滚材料（改动 5）：安装成功后**不删**安装包，而是把它挪进上一级 previous\（只留 1 份）。
    # 这样下一次升级（4.5.34）出问题时，previous\ 里躺着的正是"上一版"（4.5.33）安装包，
    # 操作员手动跑一遍即可回滚。旧写法 `del /q` 会让 previous\ 永远是空的（Python 侧的
    # _archive_previous 只在那次下载前 ready\ 里恰好有别的 exe 时才有东西可归档）。
    ready_dir = os.path.dirname(os.path.abspath(setup_path))
    previous_dir = os.path.join(os.path.dirname(ready_dir), "previous")
    lines = [
        "@echo off",
        # 本文件按 GBK 写（含中文路径）；显式固定代码页，避免它被从 UTF-8 控制台
        # （chcp 65001）启动时把 GBK 字节按 UTF-8 解码 → 中文路径变乱码 → "系统找不到
        # 指定的文件"。chcp 行是纯 ASCII，在任何继承代码页下都能先执行成功。
        "chcp 936 >nul",
        "rem HN print tool self-update (silent install: user-confirmed only)",
        "set n=0",
        ":wait",
        f'tasklist | findstr /i "{exe_name}" >nul',
        "if errorlevel 1 goto install",
        "set /a n+=1",
        f"if %n% gtr {_WAIT_MAX_LOOPS} goto install",
        "ping -n 2 127.0.0.1 >nul",
        "goto wait",
        ":install",
        install_line,
        "if errorlevel 1 goto install_failed",
        start_cmd,
        "ping -n 2 127.0.0.1 >nul",
        f'if not exist "{previous_dir}" mkdir "{previous_dir}"',
        f'del /q "{previous_dir}\\*.exe" >nul 2>nul',
        f'move /y "{setup_path}" "{previous_dir}" >nul',
        "goto done",
        ":install_failed",
        "rem 安装失败（如 UAC 拒绝）：保留安装包供下次检查更新复用，不删除；重新启动程序",
        "rem 需要回滚时看更新目录的 ROLLBACK_README.txt（previous\\ 里有上一版安装包）",
        start_cmd,
        ":done",
        "del \"%~f0\"",
    ]
    try:
        with open(cmd_path, "w", encoding="gbk", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
        return True
    except OSError as e:
        logger.error(f"写入 update.cmd 失败: {e}")
        return False


def _run_cmd_minimized(cmd_path: str) -> bool:
    """以最小化窗口运行 update.cmd（SW_SHOWMINNOACTIVE=7）。"""
    try:
        import ctypes
        ctypes.windll.shell32.ShellExecuteW(None, "open", cmd_path, None, None, 7)
        return True
    except Exception as e:
        logger.error(f"启动更新脚本失败: {e}")
        return False


# ============================================================================
# 顶层：准备更新
# ============================================================================

def prepare_update(manifest: dict, current_version: str, progress_cb=None,
                   user_confirmed: bool = False) -> tuple[str, str] | None:
    """下载并准备更新。成功返回 (setup_path, exe_path)；任何环节失败返回 None。

    `user_confirmed` 必须由调用方在"用户明确点击更新"后传 True：这是改动 4 的
    第二道闸（GUI 侧 `_perform_update` 是唯一调用点），启动自动检查永远不会传 True。

    顺序（与文件头一致）：validate_manifest（含**验签**）→ 下载 → 校验哈希
    → 落盘复算 → 返回。验签失败发生在任何一次网络下载安装包之前。
    """
    if not user_confirmed:
        logger.error("拒绝准备更新：未获得用户明确确认（启动检查只提示，不下载/不安装）")
        return None
    try:
        manifest = validate_manifest(dict(manifest or {}))
        algo, digest = expected_hash(manifest)
    except (ManifestError, ValueError) as e:
        logger.error(f"更新清单未通过安全校验，拒绝下载: {e}")
        return None
    setup_path = download_setup(
        manifest.get("url", ""), digest, updates_root(),
        progress_cb=progress_cb, algo=algo)
    if not setup_path:
        return None
    # 交给安装器之前**再复算一次**（这是最后一次能便宜地发现"文件被换掉"的机会）
    if not verify_file_hash(setup_path, algo, digest):
        logger.error("安装包在准备安装前哈希不一致，已终止更新")
        try:
            os.remove(setup_path)
        except OSError:
            pass
        return None
    import sys
    exe_path = os.path.abspath(sys.executable)
    return setup_path, exe_path
