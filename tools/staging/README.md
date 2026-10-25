# tools/staging —— 本机隔离 staging（S0-01 / S2-12 验收）

隔离 staging：**真 nginx 边缘 → 真实 app.py（16 线程池）**，全部 127.0.0.1，不碰生产。
完整说明见 `docs/staging_2026-10-25.md`。

## 拓扑

```
client(127.0.0.x 每台设备一个源 IP) → nginx 127.0.0.1:18080 → backend 127.0.0.1:18081
                                                              └ 旁路控制口 127.0.0.1:18082
```

- 实例目录 `_tmp_staging/instance/`：`mobile_apps/printer-backend` 的整份拷贝 + **假 config**
  （staging 专用 token）+ 库副本（仓库里的开发副本）+ 独立 `uploads/` `logs/`。
- 后端通过 `HN_CONFIG_PATH` 读实例 config，`app.py` **一行未改**。
- nginx 配置由 `nginx-staging.conf` 渲染，取值与生产
  `mobile_apps/printer-backend/nginx-production-hn-space.conf` +
  `nginx-conf-d-00-security-hardening.conf` **逐行一致**。

## 命令

```bash
PY="C:/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe"

# nginx 二进制（一次性；已在 _tmp_staging/vendor/ 可跳过）
cd _tmp_staging/vendor && curl -O https://nginx.org/download/nginx-1.29.0.zip \
  && python -c "import zipfile;zipfile.ZipFile('nginx-1.29.0.zip').extractall('.')" && cd ../..

PYTHONPATH= PYTHONUTF8=1 "$PY" tools/staging/staging_up.py [--fresh]   # 起（--fresh 重建实例）
PYTHONPATH= PYTHONUTF8=1 "$PY" tools/staging/staging_status.py         # 状态
PYTHONPATH= PYTHONUTF8=1 "$PY" tools/staging/staging_down.py           # 停

PYTHONPATH= PYTHONUTF8=1 "$PY" tools/staging/s0_01_saturation.py       # S0-01：16 条（约 85 s）
PYTHONPATH= PYTHONUTF8=1 "$PY" tools/staging/s2_12_capacity.py         # S2-12：3/6/10 台三档
```

⚠ 端口 18080 段与 `tests/staging/00–09`（99_run_all）冲突：跑那条链路前先 `staging_down.py`。

## 文件

| 文件 | 作用 |
|---|---|
| `staging_common.py` | 路径/端口/假 token、实例副本、进程启停、HTTP（可绑源 IP）、**原始 Engine.IO WS 握手** |
| `staging_server.py` | 后端启动器：`HN_CONFIG_PATH` + 微信上游打桩 + `ThreadPoolExecutor(16)`（≈ gunicorn gthread）+ 旁路 `/stats` |
| `nginx-staging.conf` | 生产 S0-01 边缘配置的本机版（去掉 TLS/域名；**取值不改**；全相对路径） |
| `staging_up.py` / `staging_down.py` / `staging_status.py` | 启停/状态（幂等） |
| `s0_01_saturation.py` | S0-01 16 条连接视角 + 16 条验收点，写 `_tmp_staging/results/s0_01.json` |
| `s2_12_capacity.py` | S2-12 三档容量（每设备独立源 IP），写 `_tmp_staging/results/s2_12.json` |

## 环境坑（踩过）

1. `PYTHONPATH` 置空、`PYTHONUTF8=1`；
2. nginx for Windows 的 argv 是 ANSI：**命令行/配置里不能出现中文绝对路径**（1113），
   一律 `cwd=nginx 根` + 相对路径；`include` 相对的是**配置文件所在目录**；
3. `limit_req`/`limit_conn` zone 跨用例残留 ⇒ 验收脚本开跑先重启 nginx，且不同阶段用不同源 IP；
4. `limit_conn` 释放有延迟（约 1–3 s）才能重新建连；
5. websocket-client 1.9 不支持 `source_address`，绑源 IP 用 `staging_common.ws_connect` 的原始握手。
