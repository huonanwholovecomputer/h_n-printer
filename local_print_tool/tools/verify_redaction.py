"""Pin what each redaction rule does to a real line, and prove nothing leaks.

The rule table in log_triage.py is ordered, and order is where masks rot: a rule that
widens, a rule that moves ahead of another, and a measurement silently becomes `<FILE>` or
a file name silently survives. Neither shows up as a crash — the corpus still builds, the
report still prints, and the metric still looks like a metric.

So every expectation here is a line taken from the tool's own log (or the shape of one), and
the pair (input, masked) is asserted exactly. Run it after touching REDACTIONS:

    python tools/verify_redaction.py

Exit 0 means every line masked as written down and the preflight stayed clean.

A line is masked for one of two reasons, and they are checked differently:

  SENSITIVE  masking keeps a credential, a name or a customer document out of the payload.
             The preflight must catch the raw form, or the preflight is decoration.
  GATHERED   masking collapses an instance (a retry rung, a release number, a tab index)
             so occurrences pool into one event. Nothing here is private, so the preflight
             is not required to fire on it.
  KEPT       a number that must survive, because it is the measurement a fault shows up in.
             This group is the regression guard for the bug this tool was written to fix.

The fields are grouped by what the rule is for, not by kind of value, because that is the
distinction that decides whether a mistake is a leak or a wrong number.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from log_triage import build_corpus, leaks, normalize, public  # noqa: E402

# --- SENSITIVE: must be masked, and the preflight must catch the raw form ----------------
# Every raw line here is a stand-in shaped like the real one, never the real one: the token
# and the machine name are live credentials, and the case that proves they are masked would
# otherwise be the thing that commits them. Only the shape matters to a rule.
SENSITIVE = [
    # The printer token, the client id and the machine name all ride in the query string,
    # along with a cache-busting timestamp that would shatter the event without the whole
    # query being dropped.
    ("☁ 连接失败: HTTPSConnectionPool(host='example.invalid', port=443): Max retries exceeded with "
     "url: /socket.io/?token=FAKEtokenFAKEtokenFAKEtokenFAKEtokenFAKE&client_id=WORKSTATION-0001-0000000000"
     "&device_name=WORKSTATION-0001&transport=polling&EIO=4&t=1000000000.0000000",
     "☁ 连接失败: HTTPSConnectionPool(host='example.invalid', port=443): Max retries exceeded with url: /socket.io"),
    ("☁ 接单续期失败: 接单启用失败: HTTPSConnectionPool(host='example.invalid', port=443): Max retries "
     "exceeded with url: /api/printer/claim?token=FAKEtokenFAKEtoken",
     "☁ 接单续期失败: 接单启用失败: HTTPSConnectionPool(host='example.invalid', port=443): Max retries "
     "exceeded with url: /api/printer/claim"),
    # Two device names that differ only in a suffix are one event, not two.
    ("☁ 当前接单设备: DESKTOP-AAAA1111-bbbb2222（所有者：示例员工），本机未接单",
     "☁ 当前接单设备: <DEVICE>（所有者：<OWNER>），本机未接单"),
    ("☁ 当前接单设备: DESKTOP-AAAA1111（所有者：示例员工），本机未接单",
     "☁ 当前接单设备: <DEVICE>（所有者：<OWNER>），本机未接单"),
    # Order numbers and task ids.
    ("📋 已分配订单号: HN1-2", "📋 已分配订单号: <ORDER>"),
    ("📋 复用已有订单号: HN1-2", "📋 复用已有订单号: <ORDER>"),
    ("📋 订单已上报云端: HN3-4 (1 个文件)", "📋 订单已上报云端: <ORDER> (1 个文件)"),
    ("☁ 云端任务 #218 下载完成", "☁ 云端任务 #<N> 下载完成"),
    ("☁ 收到云端任务 #2: Screenshot_1_2.jpg", "☁ 收到云端任务 #<N>: <FILE>.jpg"),
    # Customer documents, whatever they are called — including a bare 32-hex name.
    ("📦 PDF 已缓存: 单页文档测试.docx (MD5=deadbee1...)",
     "📦 PDF 已缓存: <FILE>.docx (MD5=<MD5>...)"),
    ("📦 缓存命中: 示例文档.pdf → 0 页 (MD5=deadbee2...)",
     "📦 缓存命中: <FILE>.pdf → 0 页 (MD5=<MD5>...)"),
    ("   → 转换完成: _conv_k1ge2b3.pdf", "→ 转换完成: <FILE>.pdf"),
    ("[1/1] hn_cloud_1_Screenshot_2_3.jpg", "[1/1] <FILE>.jpg"),
    ("☁ 开始下载 #218: 0123456789abcdef0123456789abcdef.jpg",
     "☁ 开始下载 #<N>: <FILE>.jpg"),
    ("☁ 收到云端任务 #2: 示例报告（正反打印，可续页）.docx ⚡无障碍",
     "☁ 收到云端任务 #<N>: <FILE>.docx ⚡无障碍"),
    # Cache keys are hashes, not measurements.
    ("📦 已清理缓存: deadbee3...", "📦 已清理缓存: <MD5>..."),
    # A windows user path. The name rule runs first, but a path with no spaces is one
    # `\\S*?` token, so the file rule then swallows the whole of it — which masks the
    # username too, just less legibly. Either way nothing reaches the payload.
    (r"读取 C:\Users\someuser\Desktop\a.pdf 失败", r"读取 <FILE>.pdf 失败"),
    # With a space in the path the two rules split it, and the username is the part that
    # must not survive.
    (r"读取 C:\Users\someuser\Desktop\my report.pdf 失败",
     r"读取 C:\Users\<USER>\Desktop\my <FILE>.pdf 失败"),
]

# --- GATHERED: masked so occurrences pool. Nothing here is private. ----------------------
GATHERED = [
    # The reconnect ladder. Eighteen rungs, one event; the escalation lives in the
    # occurrence count and the time span, not in the seconds.
    ("☁ 3s 后重连...", "☁ <N>s 后重连..."),
    ("☁ 120s 后重连...", "☁ <N>s 后重连..."),
    # A release number identified an instance, not a condition: keeping it cost two events
    # per release.
    ("☁ 开始下载更新安装包 v4.5.0...", "☁ 开始下载更新安装包 v<VER>..."),
    ("☁ 更新安装包已就绪 v4.5.15，即将退出并自动安装...",
     "☁ 更新安装包已就绪 v<VER>，即将退出并自动安装..."),
    ("📑 新建标签页 2", "📑 新建标签页 <N>"),
    ("已删除标签页 1", "已删除标签页 <N>"),
]

# --- KEPT: the number is the message. Masking any of these hides a fault. ----------------
KEPT = [
    # The batch summary. `成功 3，失败 2` and `成功 1，失败 0` are different events; merging
    # them is what let a failed batch hide behind a passing one.
    ("========== 打印完毕：成功 1，失败 0 ==========",
     "========== 打印完毕：成功 1，失败 0 =========="),
    ("========== 打印完毕：成功 3，失败 2 ==========",
     "========== 打印完毕：成功 3，失败 2 =========="),
    # A cached page count of zero is the fault this tool exists to surface, and the number
    # is the whole message.
    ("📦 缓存命中: 示例文档.pdf → 0 页 (MD5=deadbee2...)",
     "📦 缓存命中: <FILE>.pdf → 0 页 (MD5=<MD5>...)"),
    ("📦 缓存命中: 单页文档测试.docx → 3 页 (MD5=deadbee1...)",
     "📦 缓存命中: <FILE>.docx → 3 页 (MD5=<MD5>...)"),
    ("  ✓ 打印成功 (GDI, 2 面, 1 份)", "✓ 打印成功 (GDI, 2 面, 1 份)"),
    ("   → 正在打印 (份数:1, 双面:on, 方向:portrait)...",
     "→ 正在打印 (份数:1, 双面:on, 方向:portrait)..."),
    ("   → 正在打印 (份数:2, 双面:off, 方向:landscape)...",
     "→ 正在打印 (份数:2, 双面:off, 方向:landscape)..."),
    # A log upload of zero bytes is a fault; a download of 627620 bytes is not. Both are
    # kept, which is what lets the two tell themselves apart.
    ("📋 已回报本机日志（云端日志收集, 0 字节）",
     "📋 已回报本机日志（云端日志收集, 0 字节）"),
    ("📋 已回报本机日志（云端日志收集, 4810 字节）",
     "📋 已回报本机日志（云端日志收集, 4810 字节）"),
    ("☁ 下载完成 #2: 示例文档.pdf (627620 bytes)",
     "☁ 下载完成 #<N>: <FILE>.pdf (627620 bytes)"),
    ("📦 缓存保留时间已同步: 7天 → 1天", "📦 缓存保留时间已同步: 7天 → 1天"),
    ("🔄 价格已与云端配置同步: 单面 0.2 | 双面 0.3 | 首页费 0.10",
     "🔄 价格已与云端配置同步: 单面 0.2 | 双面 0.3 | 首页费 0.10"),
    ("暂存副本目录：1 个文件 / 0.0 MB", "暂存副本目录：1 个文件 / 0.0 MB"),
    ("共 1 个任务待处理", "共 1 个任务待处理"),
    ("🧹 已清理 1 个暂存文件副本（退出清理）", "🧹 已清理 1 个暂存文件副本（退出清理）"),
    # Identity beside a measurement: the tab is masked, the counts are not.
    ("标签页 1: 已添加 1 个文件（1 个已复制到暂存副本，原件删除/移动不影响打印）",
     "标签页 <N>: 已添加 1 个文件（1 个已复制到暂存副本，原件删除/移动不影响打印）"),
]


def main() -> int:
    failures = []

    for group, cases in (("SENSITIVE", SENSITIVE), ("GATHERED", GATHERED), ("KEPT", KEPT)):
        for raw, expected in cases:
            got = normalize(raw)
            if got != expected:
                failures.append("%s mask\n    in       %s\n    expected %s\n    got      %s"
                                % (group, raw, expected, got))

    # The payload is what gets checked, so the invariant that matters is on masked text:
    # nothing sensitive survives masking, anywhere in any group.
    for group, cases in (("SENSITIVE", SENSITIVE), ("GATHERED", GATHERED), ("KEPT", KEPT)):
        for _raw, expected in cases:
            found = leaks(expected)
            if found:
                failures.append("%s line still trips the preflight after masking (%s): %s"
                                % (group, ", ".join(found), expected))

    # And the preflight must actually fire on the raw sensitive forms, or it is decoration.
    for raw, _expected in SENSITIVE:
        if not leaks(raw):
            failures.append("sensitive line passed the preflight unmasked: %s" % raw)

    # A measurement must not gather with its siblings: the failed batch needs its own event.
    events, unparsed = build_corpus([
        "2026-08-27 15:30:14 [INFO] ========== 打印完毕：成功 1，失败 0 ==========",
        "2026-08-27 15:31:14 [INFO] ========== 打印完毕：成功 3，失败 2 ==========",
        "2026-08-27 15:32:14 [WARNING] ========== 打印完毕：成功 3，失败 2 ==========",
    ])
    batch = [e for e in events if "打印完毕" in e["line"]]
    if len(batch) != 2:
        failures.append("the two batch summaries collapsed into %d event(s), expected 2" % len(batch))
    else:
        merged = [e for e in batch if e["count"] == 2][0]
        if merged["level"] != "WARNING":
            failures.append("the cluster kept level %r, expected the worst variant WARNING"
                            % merged["level"])

    # And identity must gather: one event, however many device names and tokens appear.
    events, _ = build_corpus([
        "2026-08-27 15:30:14 [INFO] ☁ 当前接单设备: AAAA-1111（所有者：甲），本机未接单",
        "2026-08-27 15:31:14 [INFO] ☁ 当前接单设备: BBBB-2222（所有者：乙），本机未接单",
    ])
    if len(events) != 1 or events[0]["count"] != 2:
        failures.append("two device names did not gather into one event: %s"
                        % [e["line"] for e in events])

    # public() is the allow-list the fixture is written through; a private field must not
    # survive it, or a raw line lands in a committed file.
    if "_members" in public({"id": "L001", "line": "x", "level": "INFO", "count": 1,
                             "first": "f", "last": "t", "_members": ["raw secret"]}):
        failures.append("public() let _members through to the fixture")

    if unparsed:
        failures.append("a well-formed line was reported unparsed: %s" % unparsed)

    print("cases: %d sensitive | %d gathered | %d kept"
          % (len(SENSITIVE), len(GATHERED), len(KEPT)))
    if failures:
        print("\n%d FAILED\n" % len(failures))
        for f in failures:
            print("  %s\n" % f)
        return 1
    print("all expectations hold; preflight clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
