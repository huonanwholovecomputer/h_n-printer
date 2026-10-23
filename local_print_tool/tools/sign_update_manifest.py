# -*- coding: utf-8 -*-
"""
sign_update_manifest.py — 更新清单离线签名小工具（S1-02 / ed25519）

为什么需要它：`update.json` 是整条更新链的授权根。能改这份清单的人，过去就等于能在
全部门店以管理员权限执行代码。现在清单带 ed25519 签名，客户端内置公钥，改清单不再够用，
还需要**私钥**（只在你自己的机器上，绝不入库）。

签名载荷 = `updater.canonical_manifest_payload()`：清单去掉 `sig` / `sig_algo` 后按
`sort_keys=True, separators=(",", ":")` 序列化的 UTF-8 JSON。签名工具与客户端**共用
同一个函数**，不要手搓 JSON。

用法（在本机 / 发布机上跑，私钥文件路径由用户自己保管）：

  # 1) 首次：生成密钥对（只做一次；私钥写到仓库外的路径）
  python local_print_tool/tools/sign_update_manifest.py --gen-key C:\\Users\\你\\.hn-print-update\\update_signing_key.pem
     → 打印 base64 公钥；把它粘进 updater.py 的 UPDATE_PUBLIC_KEY_B64，然后重新打包客户端。
       换公钥 = 发新客户端（旧客户端只认内置公钥）。

  # 2) 每次发版：给清单签名并写回
  python local_print_tool/tools/sign_update_manifest.py local_print_tool/installer/update.json \\
      --key C:\\Users\\你\\.hn-print-update\\update_signing_key.pem --write

  # 3) 只校验（发布前自查 / 线上清单核查）
  python local_print_tool/tools/sign_update_manifest.py local_print_tool/installer/update.json --check

退出码：0 = 成功；1 = 失败（验签不过、私钥读不到、参数不对）。

⚠ 私钥丢失 = 无法再给**已发布客户端**签新清单（只能发一版内置新公钥的客户端）。
  请把私钥备份到离线介质；不要复制进仓库、不要进构建产物、不要发给同事的聊天窗口。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_TOOL = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(LOCAL_TOOL)
if LOCAL_TOOL not in sys.path:
    sys.path.insert(0, LOCAL_TOOL)

import updater  # noqa: E402 —— 复用 canonical_manifest_payload / verify_manifest_signature

DEFAULT_KEY_HINT = r"%USERPROFILE%\.hn-print-update\update_signing_key.pem"


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}", file=sys.stderr)
    return 1


def _load_private_key(key_path: str):
    from cryptography.hazmat.primitives import serialization

    if not os.path.isfile(key_path):
        raise FileNotFoundError(f"私钥文件不存在: {key_path}")
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _public_key_b64(private_key) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def cmd_gen_key(path: str) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if os.path.exists(path):
        return _fail(f"私钥文件已存在，拒绝覆盖（换密钥请先手工备份/删除）: {path}")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    with open(path, "wb") as f:
        f.write(pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"私钥已写入: {path}")
    print("（请立刻备份到离线介质；不要入库、不要进安装包）")
    print()
    print("把下面这行粘进 local_print_tool/updater.py 的 UPDATE_PUBLIC_KEY_B64，然后重新打包客户端：")
    print(f'UPDATE_PUBLIC_KEY_B64 = "{_public_key_b64(key)}"')
    print()
    print(f"当前 updater.py 里的公钥 = \"{updater.UPDATE_PUBLIC_KEY_B64}\"")
    if _public_key_b64(key) != updater.UPDATE_PUBLIC_KEY_B64:
        print("[WARN] 与当前内置公钥不一致 —— 用这把钥匙签的清单，现有客户端会验签失败。")
    return 0


def _read_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("清单不是 JSON 对象")
    return data


def _sign(private_key, manifest: dict) -> str:
    payload = updater.canonical_manifest_payload(manifest)
    return base64.b64encode(private_key.sign(payload)).decode("ascii")


def cmd_check(path: str) -> int:
    manifest = _read_manifest(path)
    try:
        state = updater.verify_manifest_signature(manifest)
    except updater.ManifestError as e:
        return _fail(f"验签失败: {e}")
    if state == updater.SIG_UNSIGNED:
        return _fail("清单没有 sig 字段（兼容期客户端会告警后放行；发布前请补签）")
    expected_hash, detail = _hash_detail(manifest)
    print(f"[OK] 验签通过（ed25519），哈希字段 {expected_hash}")
    if detail:
        print(detail)
    return 0


def _hash_detail(manifest: dict) -> tuple[str, str]:
    try:
        algo, digest = updater.expected_hash(manifest)
    except updater.ManifestError as e:
        return "缺失/非法", f"[WARN] 哈希字段问题: {e}"
    return f"{algo}={digest}", ""


def cmd_sign(path: str, key_path: str, write: bool) -> int:
    private_key = _load_private_key(key_path)
    manifest = _read_manifest(path)
    # 先做一遍结构/哈希检查（签一份本身不合格的清单没有意义）
    try:
        updater.expected_hash(manifest)
    except updater.ManifestError as e:
        return _fail(f"清单哈希字段不合格，拒绝签名: {e}")

    sig_b64 = _sign(private_key, manifest)

    # 自检：用**客户端内置的公钥**验证，避免"签了个客户端不认的钥匙"
    signed_view = dict(manifest)
    signed_view["sig"] = sig_b64
    signed_view["sig_algo"] = updater.SIG_ALGO
    try:
        updater.verify_manifest_signature(signed_view)
    except updater.ManifestError as e:
        print(f"[WARN] 用内置公钥自检失败: {e}", file=sys.stderr)
        print(f"[WARN] 本钥公钥 = {_public_key_b64(private_key)}", file=sys.stderr)
        print(f"[WARN] 内置公钥   = {updater.UPDATE_PUBLIC_KEY_B64}", file=sys.stderr)
        print("[WARN] 请确认私钥与 updater.py 的公钥是一对，否则新客户端会拒绝该清单。",
              file=sys.stderr)

    if write:
        out = dict(manifest)
        out["sig_algo"] = updater.SIG_ALGO
        out["sig"] = sig_b64
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        print(f"[OK] 已写入签名: {path}")
        print(f'  "sig_algo": "{updater.SIG_ALGO}"')
        print(f'  "sig": "{sig_b64}"')
    else:
        print(sig_b64)
        print()
        print(f'（清单 {path} 未被修改。加 --write 可直接写回 sig / sig_algo。）')
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="HN 打印工具 · 更新清单 ed25519 签名工具（私钥不入库）")
    ap.add_argument("manifest", nargs="?",
                    help="更新清单路径（如 local_print_tool/installer/update.json）")
    ap.add_argument("--key", default=os.environ.get("HN_UPDATE_SIGNING_KEY_PATH", ""),
                    help=f"私钥 PEM 路径（也可用环境变量 HN_UPDATE_SIGNING_KEY_PATH；默认提示 {DEFAULT_KEY_HINT}）")
    ap.add_argument("--gen-key", metavar="PATH", default=None,
                    help="生成 ed25519 密钥对到 PATH（仓库外），并打印公钥")
    ap.add_argument("--write", action="store_true",
                    help="把 sig / sig_algo 写回清单（默认只打印 base64 签名）")
    ap.add_argument("--check", action="store_true", help="只校验现有 sig，不签名")
    args = ap.parse_args(argv)

    if args.gen_key:
        return cmd_gen_key(args.gen_key)
    if not args.manifest:
        ap.print_help()
        return 1
    if not os.path.isfile(args.manifest):
        return _fail(f"清单不存在: {args.manifest}")
    if args.check:
        return cmd_check(args.manifest)
    if not args.key:
        return _fail("缺少私钥路径：用 --key <私钥.pem> 或环境变量 HN_UPDATE_SIGNING_KEY_PATH")
    try:
        return cmd_sign(args.manifest, args.key, args.write)
    except (OSError, ValueError) as e:
        return _fail(str(e))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
