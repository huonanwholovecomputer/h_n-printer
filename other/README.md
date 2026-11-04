# other/ —— 验收套件、运维工具、日志与临时产物的收纳目录

2026-11-03 整理：仓库根下这些目录不再散落在根上，统一收进 `other/`。
**根目录只留业务代码与文档**（`local_print_tool/`、`mobile_apps/`、`docs/`、`CLAUDE.md` 等），
其余"跑的东西、产出的东西、历史归档"都在这里。

## 目录一览

| 路径 | 内容 | 入 git |
|------|------|--------|
| `other/tests/` | 全量验收脚本（`verify_*.py\|*.js\|*.mjs`、聚合 runner、`staging/` 压测、`audit_2026_12/` 审计） | ✅ |
| `other/tools/` | 运维/辅助脚本（`staging/` 容量压测、`readme_screenshots/` 截图生成） | ✅ |
| `other/logs/` | 本地验收与构建日志归档 | ❌ |
| `other/_tmp_staging/` | 容量压测的隔离副本与结果（可随时重建） | ❌ |
| `other/_tmp_ctxsplit/` | 历史上把 CLAUDE.md 拆成 `docs/` 子文档的一次性脚本与中间产物 | ❌ |
| `other/_tmp_scroll/` | 滚动几何诊断的探针与报告 | ❌ |
| `other/_tmp_finance/` | 结算页排查的中间产物 | ❌ |
| `other/_tmp_rv/`、`other/_tmp_sandbox/` | 历史一次性排查/沙箱产物 | ❌ |
| `other/_归档/` | 历史发布物归档（旧安装包 / APK，仅本地留存） | ❌ |

## 怎么跑

所有脚本都**从仓库根跑**，路径以根为基准（脚本内部自己往上找根，不依赖 cwd）：

```bash
# 全量验收（约 10 分钟；退出码 0 才算绿）
python other/tests/run_all.py

# 只跑子集 / 连"必须换环境跑"的也跑
python other/tests/run_all.py --only verify_duplex
python other/tests/run_all.py --all

# 单个
python other/tests/verify_print_watch.py
node   other/tests/verify_settlement_invariants.mjs

# 容量压测（隔离副本，约 7 分钟）
python other/tests/staging/99_run_all.py
```

生成的日志/结果都落在这棵子树里（`other/logs/`、`other/_tmp_staging/results/`、`other/tests/staging/results/`），
已在 `.gitignore` 里排除，不会进版本库。

## 改路径时的注意点

- 脚本里"仓库根"一律靠**向上数层**得到，例如 `other/tests/x.py` 用 `dirname` 三次：
  `ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`；
  `other/tests/staging/x.py`、`other/tools/**/x.py` 再加一层。**这里的层数别改错**，错了整套验收会静默跑错对象。
- JS/MJS 用 `path.dirname(path.dirname(__dirname))` 或 `path.join(HERE, '..', '..')` 到根。
- 文档（`docs/`、`CLAUDE.md`、`mobile_apps/docs/`）里的路径以仓库根为基准写成 `other/tests/verify_xxx.py`；
  改完跑 `python other/tests/verify_context_docs.py`，它会把 `other/` 前缀还原成旧写法再和拆分基线逐字对账
  （`MOVE_UNDO` / `unshift_move`），所以文档里改路径不会误报"丢内容"。
