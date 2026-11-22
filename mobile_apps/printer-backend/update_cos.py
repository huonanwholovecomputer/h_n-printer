# -*- coding: utf-8 -*-
"""update_cos.py — 腾讯云 COS **只读**预签名（v5 签名算法，零第三方依赖）

为什么不用 cos-python-sdk-v5：
  · 生产服务器是单机 Flask（/home/printer-backend），每加一个依赖就要在 venv 里
    装一次、锁一次版本、下次重装再来一遍；而这里只需要「给一个对象签一个 3 分钟
    有效的 GET 链接」这一件事。
  · 签名算法本身只有 ~20 行（HMAC-SHA1 两次），自己实现可以被验收脚本逐字节
    钉住；SDK 出问题只能看文档。

安全边界（务必保持）：
  · 本模块只签 **GET**，不接受 method 参数 —— 后端永远没有上传/删除桶内对象的能力。
  · 密钥只能是「该桶 GetObject」的最小权限子账号（见 docs/部署与发版.md）。
  · 链接有效期默认 180 秒，由调用方传更短值也不拦；过期立刻失效。

签名算法（腾讯云 v5，query-string 形态）：
  HttpString  = "get\n" + /对象名 + "\n" + 参数串 + "\n" + "host=<host>\n"
  SignKey     = HMAC-SHA1(SecretKey, KeyTime)        # 结果取 hex 字符串当 key
  StringToSign= "sha1\n" + KeyTime + "\n" + SHA1(HttpString) + "\n"
  Signature   = HMAC-SHA1(SignKey, StringToSign)     # hex
  注意 HttpString 里 header 段后面**还有一个换行**，漏了就是 SignatureDoesNotMatch。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

# 预签名链接默认有效期（秒）。够慢网络拉一个 302，又不至于被人转发到群里。
DEFAULT_TTL = 180
# 允许的最长有效期：即便调用方传更大的值也压回来。
MAX_TTL = 900

_SAFE_PATH = "/-_.~"


def object_host(bucket: str, region: str) -> str:
    """桶的虚拟主机名（bucket 已是 <name>-<appid> 的完整形态）。"""
    b = str(bucket or "").strip()
    r = str(region or "").strip()
    if not b or not r:
        raise ValueError("COS 桶名/地域为空")
    return f"{b}.cos.{r}.myqcloud.com"


def object_uri(key: str) -> str:
    """对象名 → URI 路径。COS 的 key 区分大小写、允许 / 嵌套，逐段转义。"""
    k = str(key or "").strip().lstrip("/")
    if not k:
        raise ValueError("COS 对象名为空")
    return "/" + quote(k, safe=_SAFE_PATH)


def _hmac_sha1_hex(key: str | bytes, msg: str) -> str:
    k = key.encode("utf-8") if isinstance(key, str) else key
    return hmac.new(k, msg.encode("utf-8"), hashlib.sha1).hexdigest()


def _sha1_hex(msg: str) -> str:
    return hashlib.sha1(msg.encode("utf-8")).hexdigest()


def presign_get_url(secret_id: str, secret_key: str, bucket: str, region: str,
                    key: str, ttl: int = DEFAULT_TTL, now: float | None = None) -> str:
    """生成一个只读、短时的对象下载链接（query-string 签名）。

    now 只在验收脚本里传（钉死时间戳做可复现比对），生产调用不传。
    """
    sid = str(secret_id or "").strip()
    skey = str(secret_key or "").strip()
    if not sid or not skey:
        raise ValueError("COS 密钥未配置")
    try:
        ttl_i = int(ttl)
    except (TypeError, ValueError) as e:
        raise ValueError(f"COS 链接有效期非法: {ttl!r}") from e
    if ttl_i <= 0:
        raise ValueError(f"COS 链接有效期必须为正数（得到 {ttl_i}）")
    ttl_i = min(ttl_i, MAX_TTL)

    host = object_host(bucket, region)
    uri = object_uri(key)
    start = int(time.time() if now is None else now)
    sign_time = f"{start};{start + ttl_i}"

    http_string = f"get\n{uri}\n\nhost={quote(host, safe='')}\n"
    sign_key = _hmac_sha1_hex(skey, sign_time)
    string_to_sign = f"sha1\n{sign_time}\n{_sha1_hex(http_string)}\n"
    signature = _hmac_sha1_hex(sign_key, string_to_sign)

    query = (
        "q-sign-algorithm=sha1"
        f"&q-ak={quote(sid, safe='')}"
        f"&q-sign-time={quote(sign_time, safe='')}"
        f"&q-key-time={quote(sign_time, safe='')}"
        "&q-header-list=host"
        "&q-url-param-list="
        f"&q-signature={signature}"
    )
    return f"https://{host}{uri}?{query}"


def config_from_app(cfg) -> dict:
    """从后端 config 模块里取 COS 配置；缺任何一项都当作「通道未配置」。

    返回 dict（空 dict = 未配置）。调用方据此决定是 503 还是 302 —— 未配置时
    客户端会回退到 GitHub Release，功能不会整体挂掉。
    """
    def _get(name: str) -> str:
        try:
            return str(getattr(cfg, name, "") or "").strip()
        except Exception:
            return ""

    data = {
        "secret_id": _get("UPDATE_COS_SECRET_ID"),
        "secret_key": _get("UPDATE_COS_SECRET_KEY"),
        "bucket": _get("UPDATE_COS_BUCKET"),
        "region": _get("UPDATE_COS_REGION"),
        "ttl": _get("UPDATE_COS_URL_TTL"),
    }
    if not all(data[k] for k in ("secret_id", "secret_key", "bucket", "region")):
        return {}
    return data
