"""
printer_config.py — 配置管理模块
负责读取/写入 JSON 配置文件，管理打印机名称、双面模式、任务列表等。
含打印首页(Cover Page) PDF 生成功能。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---- 订单归属默认占位名（仅遗留兼容） ----
# v4.2：归属名单不再预置占位姓名。成员名单一律来自收支统计的成员管理
# （云端模式=云端 finance_config 成员；本地模式=本地 print_data.json 成员），
# 无成员时归属下拉显示「(无成员)」并禁止创建订单。
# 以下常量仅用于旧配置/旧数据库回读时的兜底，不再作为任何默认值。
DEFAULT_OWNER_NAME = "张三"                       # 单个订单归属的遗留兜底名
DEFAULT_OWNER_NAMES = ["张三", "李四"]            # 旧版归属下拉初始占位成员
PLACEHOLDER_OWNER_NAMES = ("张三", "李四", "王五")  # 占位名集合：清洗旧数据时剔除
# v5.25：未绑定成员（未在收支统计中绑定）的普通用户云端订单，本地标签页归属固定显示此名。
# 它不是真实成员（不计入结算/成员账目），仅作为「未绑定顾客订单」的归属标记。
FALLBACK_OWNER_NAME = "普通用户"


@dataclass
class TabSettings:
    """单个标签页的独立设置（v7+：附加服务从全局移入标签页）。"""
    jobs: list = field(default_factory=list)          # list[PrintJob]
    delivery_enabled: bool = False
    delivery_location: str = ""
    cover_page: bool = False
    cover_page_price: float = 0.10
    urgency: str = "低"
    frozen: bool = False  # 打印后锁定，禁止任何编辑，仅允许删除标签页
    # 订单归属（v24）：本标签页订单属于谁 + 是否管理员自行打印（非顾客订单）
    owner_name: str = ""          # 空 = 尚未选择成员（成员名单为空时禁止创建订单）
    is_admin_print: bool = True
    # 订单备注（v4.6）：标签页（订单）级，不依赖选中任务；云端订单注入后端 remark
    remark: str = ""

    def to_dict(self) -> dict:
        return {
            "jobs": [j.to_dict() for j in self.jobs],
            "delivery_enabled": self.delivery_enabled,
            "delivery_location": self.delivery_location,
            "cover_page": self.cover_page,
            "cover_page_price": self.cover_page_price,
            "urgency": self.urgency,
            "frozen": self.frozen,
            "owner_name": self.owner_name,
            "is_admin_print": self.is_admin_print,
            "remark": self.remark,
        }

    @classmethod
    def from_dict(cls, data) -> "TabSettings":
        if isinstance(data, list):
            # 兼容旧格式：直接存 jobs 列表
            return cls(jobs=[PrintJob.from_dict(j) for j in data])
        jobs_data = data.get("jobs", [])
        return cls(
            jobs=[PrintJob.from_dict(j) for j in jobs_data],
            delivery_enabled=bool(data.get("delivery_enabled", False)),
            delivery_location=data.get("delivery_location", ""),
            cover_page=bool(data.get("cover_page", False)),
            cover_page_price=float(data.get("cover_page_price", 0.10)),
            urgency=data.get("urgency", "低"),
            frozen=bool(data.get("frozen", False)),
            owner_name=(data.get("owner_name") or "").strip(),
            is_admin_print=bool(data.get("is_admin_print", True)),
            remark=str(data.get("remark", "") or ""),
        )

    def calc_extra_total(self, base_total: float, config: "PrinterConfig") -> float:
        """计算附加服务总额（派送费+加急费+首页费）。"""
        extra = 0.0
        if self.delivery_enabled and self.delivery_location:
            pct = config.delivery_percentages.get(self.delivery_location, 0.0)
            extra += base_total * (pct / 100.0)
        urgency_price = config.urgency_prices.get(self.urgency, 0.0)
        if urgency_price > 0:
            extra += urgency_price
        if self.cover_page:
            extra += self.cover_page_price
        return extra


@dataclass
class PrintJob:
    """单个打印任务"""
    file_path: str = ""
    copies: int = 1
    duplex: str = "on"       # 'on' | 'off'
    page_range: str = ""     # 页码范围字符串，如 "1-5" 或 "1,3,5-7"
    page_count: int = 0      # 总页数，0 表示未知（非 PDF 文件）
    orientation: str = ""    # "portrait" | "landscape" | "mixed" | ""
    engine: str = "word"     # "word" | "wps" | "libreoffice"
    duplex_mode: str = ""    # "long-edge" | "short-edge" | "" (空=按方向自动)
    cached_pdf: str = ""     # 引擎转换后的 PDF 缓存路径
    dpi: int = 0             # 渲染 DPI，0=跟随全局默认
    image_orientation: str = "auto"   # 图片打印方向: 'auto' | 'landscape' | 'portrait'
    task_id: int = 0         # 云端任务子任务 ID (order_files.id)，0=本地任务
    order_id: int = 0        # 云端父订单 ID (orders.id)，0=本地任务
    source_md5: str = ""     # 源文件 MD5，用于 PDF 缓存查找
    display_name: str = ""   # 显示用的文件名（云端任务用原始文件名，本地任务为空则用 file_path 的 basename）
    order_number: str = ""   # 订单号（云端的来自后端，本地的在复制时生成）
    remark: str = ""         # 订单/任务备注（云端订单来自后端 remark；本地任务可自行填写，≤100 字）
    sent: bool = False       # 是否已打印完成（成功回报后置位，退出时不再放弃；持久化以扛异常退出重载）

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "copies": self.copies,
            "duplex": self.duplex,
            "duplex_mode": self.duplex_mode,
            "page_range": self.page_range,
            "page_count": self.page_count,
            "orientation": self.orientation,
            "image_orientation": self.image_orientation,
            "engine": self.engine,
            "dpi": self.dpi,
            "display_name": self.display_name,
            "task_id": self.task_id,
            "order_id": self.order_id,
            "order_number": self.order_number,
            "remark": self.remark,
            "sent": self.sent,
            # cached_pdf 不持久化，每次启动重新生成
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrintJob":
        return cls(
            file_path=data.get("file_path", ""),
            copies=int(data.get("copies", 1)),
            duplex=data.get("duplex", "on"),
            duplex_mode=data.get("duplex_mode", ""),
            page_range=data.get("page_range", ""),
            page_count=int(data.get("page_count", 0)),
            orientation=data.get("orientation", ""),
            image_orientation=data.get("image_orientation", "auto"),
            engine=data.get("engine", "word"),
            dpi=int(data.get("dpi", 0)),
            display_name=data.get("display_name", ""),
            task_id=int(data.get("task_id", 0)),
            sent=bool(data.get("sent", False)),
            order_id=int(data.get("order_id", 0)),
            order_number=data.get("order_number", ""),
            remark=str(data.get("remark", "") or ""),
        )


def _parse_range_parts(raw: str, total_pages: int) -> set[int]:
    """将页码范围字符串解析为页码集合，支持智能拆分 '23-4' → {2,3,4}。"""
    import logging
    logger = logging.getLogger(__name__)

    pages: set[int] = set()
    skipped: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
                if start < end:
                    for p in range(start, end + 1):
                        if 1 <= p <= total_pages:
                            pages.add(p)
                elif start > end and len(a) > 1:
                    # 智能拆分: "23-4" → 页码 2 + 范围 3-4
                    prefix = int(a[:-1])
                    last = int(a[-1])
                    if prefix < end:
                        for p in range(last, end + 1):
                            if 1 <= p <= total_pages:
                                pages.add(p)
                        if 1 <= prefix <= total_pages:
                            pages.add(prefix)
                else:
                    skipped.append(part)
            except ValueError:
                skipped.append(part)
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p)
                else:
                    skipped.append(part)
            except ValueError:
                skipped.append(part)
    if skipped:
        logger.warning(f"页码范围有无效部分（已忽略）: {skipped}")
    return pages


def _count_pages_in_range(page_range: str, total_pages: int) -> int:
    """解析页码范围，返回实际打印页数。"""
    if not page_range or not page_range.strip():
        return total_pages

    raw = page_range.strip()
    raw = raw.replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "")

    pages = _parse_range_parts(raw, total_pages)
    return len(pages) if pages else total_pages


def calc_cost(
    page_count: int,
    copies: int,
    duplex: str,
    simplex_price: float = 0.2,
    duplex_price: float = 0.3,
    page_range: str = "",
) -> tuple[float, str]:
    """
    计算打印费用（按纸张计费），返回 (金额, 计算式)。

    单价均为每张纸价格：
      - 单面: 1页 = 1张纸 → effective_pages 张 × simplex_price × copies
      - 双面: 2页 = 1张纸 → pairs 张 × duplex_price + remainder 张 × simplex_price
    """
    if page_count <= 0:
        return 0.0, ""

    effective = _count_pages_in_range(page_range, page_count)

    if duplex == "on":
        # 双面：每张纸印 2 页
        pairs = effective // 2      # 双面纸张数
        remainder = effective % 2   # 剩余单面纸张数
        if remainder == 0:
            # 纯双面：全部可配对
            cost = pairs * duplex_price * copies
            formula = f"{pairs}张×{duplex_price:.2f}"
        elif pairs == 0:
            # 单页：无双面纸张，仅单面计费
            cost = remainder * simplex_price * copies
            formula = f"{remainder}张×{simplex_price:.2f}"
        else:
            cost = (pairs * duplex_price + remainder * simplex_price) * copies
            formula = f"{pairs}张×{duplex_price:.2f}+{remainder}张×{simplex_price:.2f}"
        if copies > 1:
            formula = f"({formula})×{copies}份"
    else:
        # 单面：每页即一张纸
        cost = effective * simplex_price * copies
        formula = f"{effective}张×{simplex_price:.2f}"
        if copies > 1:
            formula = f"({formula})×{copies}份"
    return cost, formula


def generate_order_number(last_number: int) -> "tuple[str, int]":
    """生成订单号 HNyyyymmdd-NNNN，返回 (编号字符串, 下一个计数器值)。"""
    from datetime import date
    today = date.today().strftime("%Y%m%d")
    next_num = last_number + 1
    return f"HN{today}-{next_num:04d}", next_num


@dataclass
class PrinterConfig:
    """
    打印机配置数据类。

    JSON 结构示例:
    {
      "printer_name": "HP LaserJet MFP M232-M237",
      "duplex_mode": "long-edge",
      "keep_temp_pdf": false,
      "tabs": {
        "1": [
          {
            "file_path": "C:/docs/report.docx",
            "copies": 2,
            "duplex": "on",
            "duplex_mode": "long-edge",
            "page_range": "1-5",
            "engine": "word"
          }
        ],
        "2": []
      },
      "active_tab": "1"
    }
    """
    printer_name: str = ""
    duplex_mode: str = "long-edge"    # 'simplex' | 'long-edge' | 'short-edge'
    render_dpi: int = 400             # 全局默认渲染 DPI
    keep_temp_pdf: bool = False
    simplex_price: float = 0.2
    duplex_price: float = 0.3
    last_dir: str = ""
    # ---- 附加服务 ----
    delivery_enabled: bool = False          # 是否派送
    delivery_location: str = "1号楼北楼"    # 当前选中的派送地点
    delivery_percentages: dict[str, float] = field(default_factory=lambda: {
        "1号楼北楼": 0.0,
        "1号楼南楼": 5.0,
        "图书馆": 10.0,
        "教学楼E/F": 25.0,
        "女生宿舍": 10.0,
    })  # 百分比（派送费 = 纸张费用 × 百分比/100）
    urgency: str = "低"                     # 当前紧急程度
    urgency_prices: dict[str, float] = field(default_factory=lambda: {
        "低": 0.0,
        "中": 0.08,
        "高": 0.15,
    })
    cover_page: bool = False                # 是否打印首页信息
    cover_page_price: float = 0.10          # 首页信息单价
    pickup_address: str = "1号楼202宿舍"    # 自取地址
    last_order_number: int = 0              # 订单号计数器
    # 订单归属（v24）：管理员姓名列表，供标签页订单归属下拉选择
    # v4.2：不再预置占位名；成员名单由收支统计成员管理同步（云端/本地），空列表=无成员
    admin_names: list[str] = field(default_factory=list)
    # ---- 云端配置 ----
    cloud_enabled: bool = False             # 是否启用云端连接
    cloud_api_url: str = "https://your-server.com"   # 云端 API 地址（替换为你自己的服务器）
    cloud_ws_url: str = "wss://your-server.com"     # WebSocket 地址（替换为你自己的服务器）
    cloud_token: str = ""                   # 打印机客户端认证 token
    cloud_auto_accept: bool = False         # 是否自动接受云端任务（false=手动确认）
    # v5.22：启用接单（多设备共连服务器时，仅启用接单的设备接收订单；同一时间唯一接管者）
    cloud_take_orders: bool = False         # 是否启用接单（保存后由云端设置对话框同步到后端）
    # v5.23：本设备所有者（收支清算「授权」页绑定的成员姓名，由后端 printer_state 推送同步缓存）。
    # 新建标签页时作为默认订单归属者；未绑定/离线时为空串，回退现有「落到第一个成员」逻辑。
    cloud_owner_name: str = ""
    # ---- 标签页任务（v6+）----
    # 每个标签页独立存储任务列表，key 为 "1", "2", "3" ...
    # 兼容旧格式：如果 JSON 中没有 "tabs" 键但有 "jobs"，自动迁移为 {"1": jobs}
    tabs: dict[str, TabSettings] = field(default_factory=lambda: {"1": TabSettings()})
    active_tab: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_name": self.printer_name,
            "duplex_mode": self.duplex_mode,
            "render_dpi": self.render_dpi,
            "keep_temp_pdf": self.keep_temp_pdf,
            "simplex_price": self.simplex_price,
            "duplex_price": self.duplex_price,
            "last_dir": self.last_dir,
            # 附加服务（全局默认值 + 标签页可覆盖）
            "delivery_enabled": self.delivery_enabled,
            "delivery_location": self.delivery_location,
            "delivery_percentages": self.delivery_percentages,
            "urgency": self.urgency,
            "urgency_prices": self.urgency_prices,
            "cover_page": self.cover_page,
            "cover_page_price": self.cover_page_price,
            "pickup_address": self.pickup_address,
            "last_order_number": self.last_order_number,
            "admin_names": self.admin_names,
            # 云端配置
            "cloud_enabled": self.cloud_enabled,
            "cloud_api_url": self.cloud_api_url,
            "cloud_ws_url": self.cloud_ws_url,
            "cloud_token": self.cloud_token,
            "cloud_auto_accept": self.cloud_auto_accept,
            "cloud_take_orders": self.cloud_take_orders,
            "cloud_owner_name": self.cloud_owner_name,
            # 标签页任务（每标签含独立附加服务设置）
            "tabs": {k: v.to_dict() for k, v in self.tabs.items()},
            "active_tab": self.active_tab,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrinterConfig":
        # 兼容旧格式：无 "tabs" 键但有 "jobs" 时自动迁移
        tabs_data = data.get("tabs")
        if tabs_data is None:
            jobs_data = data.get("jobs", [])
            jobs = [PrintJob.from_dict(j) for j in jobs_data] if isinstance(jobs_data, list) else []
            tabs = {"1": TabSettings(jobs=jobs)}
        else:
            tabs = {}
            for k, v in tabs_data.items():
                tabs[k] = TabSettings.from_dict(v)
            if not tabs:
                tabs = {"1": TabSettings()}
        return cls(
            printer_name=data.get("printer_name", ""),
            duplex_mode=data.get("duplex_mode", "long-edge"),
            render_dpi=int(data.get("render_dpi", 400)),
            keep_temp_pdf=bool(data.get("keep_temp_pdf", False)),
            simplex_price=float(data.get("simplex_price", 0.2)),
            duplex_price=float(data.get("duplex_price", 0.3)),
            last_dir=data.get("last_dir", ""),
            # 附加服务
            delivery_enabled=bool(data.get("delivery_enabled", False)),
            delivery_location=data.get("delivery_location", "1号楼北楼"),
            # dict 字段防 null：配置被手改/写坏为 null 时退回默认，避免后续 .get 崩溃
            delivery_percentages=(data.get("delivery_percentages") or data.get("delivery_locations") or {
                "1号楼北楼": 0.0, "1号楼南楼": 5.0,
                "图书馆": 10.0, "教学楼E/F": 25.0, "女生宿舍": 10.0,
            }),
            urgency=data.get("urgency", "低"),
            urgency_prices=(data.get("urgency_prices") or {
                "低": 0.0, "中": 0.08, "高": 0.15,
            }),
            cover_page=bool(data.get("cover_page", False)),
            cover_page_price=float(data.get("cover_page_price", 0.10)),
            pickup_address=data.get("pickup_address", "1号楼202宿舍"),
            last_order_number=int(data.get("last_order_number", 0)),
            # dict/list 字段防 null：被手改/写坏为 null 时退回默认
            # v4.2：admin_names 默认空列表（无成员），占位名不再预置
            admin_names=[str(n).strip() for n in (data.get("admin_names") or []) if str(n).strip()],
            # 云端配置
            cloud_enabled=bool(data.get("cloud_enabled", False)),
            cloud_api_url=data.get("cloud_api_url", "https://your-server.com"),
            cloud_ws_url=data.get("cloud_ws_url", "wss://your-server.com"),
            cloud_token=data.get("cloud_token", ""),
            cloud_auto_accept=bool(data.get("cloud_auto_accept", False)),
            cloud_take_orders=bool(data.get("cloud_take_orders", False)),
            cloud_owner_name=(data.get("cloud_owner_name") or "").strip(),
            # 标签页任务
            tabs=tabs,
            active_tab=data.get("active_tab", "1"),
        )

    def save(self, path: str) -> None:
        """保存配置到 JSON 文件（同目录临时文件 + os.replace 原子替换，防写一半损坏）"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: str) -> "PrinterConfig":
        """从 JSON 文件加载配置；文件不存在则创建默认配置并保存，损坏则返回默认配置"""
        import logging
        logger = logging.getLogger(__name__)

        if not os.path.exists(path):
            cfg = cls()
            logger.info(f"配置文件不存在，已自动创建默认配置: {path}")
            cfg.save(path)
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"加载配置失败 ({path}): {e}，将使用默认配置")
            # 损坏文件改名保留为 .corrupt-{时间戳}（不删除，便于排查），下次启动自动重建
            try:
                backup_path = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                shutil.move(path, backup_path)
                logger.warning(f"已保留损坏配置为: {backup_path}")
            except Exception as e2:
                logger.warning(f"备份损坏配置失败: {e2}")
            return cls()


# ============================================================
# 打印首页 (Cover Page) 生成
# ============================================================

def generate_cover_page_pdf(
    output_path: str,
    config: "PrinterConfig",
    jobs: list["PrintJob"],
    order_number: str = "",
    created_at: str = "",
) -> bool:
    """
    生成打印首页 PDF 文件。

    首页包含:
      - 标题 & 订单号
      - 发起时间 / 实际执行时间
      - 订单备注（有才显示）
      - 文件清单（文件名、份数、单双面、页数）
      - 费用明细（纸张费、派送费、加急费、首页费）
      - 总金额
      - 派送地址 / 自取地址

    Returns: True 表示生成成功。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm, cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable,
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from datetime import datetime

    # ── 中文字体注册 ──
    _font_registered = False
    _font_name = "Helvetica"
    _font_name_bold = "Helvetica-Bold"
    for _ttf_path in [
        os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "msyh.ttc"),
        os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "msyhbd.ttc"),
    ]:
        if os.path.isfile(_ttf_path):
            try:
                pdfmetrics.registerFont(TTFont("MSYH", _ttf_path, subfontIndex=0))
                _font_name = "MSYH"
                _font_name_bold = "MSYH"
                _font_registered = True
                break
            except Exception:
                pass

    # 如果没有微软雅黑，尝试 SimSun
    if not _font_registered:
        for _ttf_path in [
            os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "simsun.ttc"),
        ]:
            if os.path.isfile(_ttf_path):
                try:
                    pdfmetrics.registerFont(TTFont("SimSun", _ttf_path, subfontIndex=0))
                    _font_name = "SimSun"
                    _font_name_bold = "SimSun"
                    break
                except Exception:
                    pass

    # ── 样式 ──
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "CoverTitle", parent=styles["Title"],
        fontName=_font_name_bold, fontSize=22, leading=30,
        alignment=TA_CENTER, spaceAfter=4 * mm,
        textColor=HexColor("#1a1a2e"),
    )
    subtitle_style = ParagraphStyle(
        "CoverSubtitle", parent=styles["Normal"],
        fontName=_font_name, fontSize=10, leading=14,
        alignment=TA_CENTER, spaceAfter=10 * mm,
        textColor=HexColor("#666666"),
    )
    section_style = ParagraphStyle(
        "CoverSection", parent=styles["Heading2"],
        fontName=_font_name_bold, fontSize=13, leading=18,
        spaceBefore=6 * mm, spaceAfter=3 * mm,
        textColor=HexColor("#1a1a2e"),
    )
    body_style = ParagraphStyle(
        "CoverBody", parent=styles["Normal"],
        fontName=_font_name, fontSize=10, leading=16,
        textColor=HexColor("#333333"),
    )
    body_right_style = ParagraphStyle(
        "CoverBodyRight", parent=body_style,
        alignment=TA_RIGHT,
    )
    total_style = ParagraphStyle(
        "CoverTotal", parent=styles["Normal"],
        fontName=_font_name_bold, fontSize=16, leading=22,
        alignment=TA_RIGHT, spaceBefore=4 * mm,
        textColor=HexColor("#d4380d"),
    )
    footer_style = ParagraphStyle(
        "CoverFooter", parent=styles["Normal"],
        fontName=_font_name, fontSize=8, leading=12,
        alignment=TA_CENTER, textColor=HexColor("#999999"),
    )

    # ── 构建内容 ──
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    story = []

    # 标题
    story.append(Paragraph("HN 打印 · 首页信息", title_style))
    story.append(Paragraph(
        f"订单号: {order_number}" if order_number else "本地打印任务",
        subtitle_style,
    ))

    # ── 时间信息 ──
    story.append(Paragraph("📅 时间信息", section_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#e0e0e0")))
    time_data = [
        [Paragraph("<b>发起时间</b>", body_style),
         Paragraph(created_at or now_str, body_style)],
        [Paragraph("<b>执行时间</b>", body_style),
         Paragraph(now_str, body_style)],
    ]
    time_table = Table(time_data, colWidths=[80, 380])
    time_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(time_table)

    # ── 备注（订单备注，有才显示；同订单多文件共享同一备注，取首个非空）──
    remark_text = ""
    for _job in jobs:
        if getattr(_job, "remark", "").strip():
            remark_text = _job.remark.strip()
            break
    if remark_text:
        # 转义 HTML 特殊字符（Paragraph 支持标记，防 <>& 报错），换行转 <br/>
        _esc = remark_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph("📝 备注", section_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#e0e0e0")))
        story.append(Paragraph(_esc.replace("\n", "<br/>"), body_style))

    # ── 文件清单 ──
    story.append(Paragraph("📄 文件清单", section_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#e0e0e0")))

    file_header = [
        Paragraph("<b>#</b>", body_style),
        Paragraph("<b>文件名</b>", body_style),
        Paragraph("<b>份数</b>", body_style),
        Paragraph("<b>模式</b>", body_style),
        Paragraph("<b>页码范围</b>", body_style),
        Paragraph("<b>页数</b>", body_style),
    ]
    file_rows = [file_header]
    for i, job in enumerate(jobs, 1):
        fname = os.path.basename(job.file_path) if job.file_path else "(未知)"
        # 截断过长文件名
        if len(fname) > 40:
            fname = fname[:37] + "…"
        duplex_label = "双面" if job.duplex == "on" else "单面"
        page_range_label = job.page_range if job.page_range.strip() else "全部"
        page_count_label = str(job.page_count) if job.page_count > 0 else "?"
        file_rows.append([
            Paragraph(str(i), body_style),
            Paragraph(fname, body_style),
            Paragraph(str(job.copies), body_right_style),
            Paragraph(duplex_label, body_style),
            Paragraph(page_range_label, body_style),
            Paragraph(page_count_label, body_right_style),
        ])

    file_table = Table(file_rows, colWidths=[20, 235, 40, 45, 70, 40])
    file_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, HexColor("#cccccc")),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, HexColor("#e0e0e0")),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f5f5f5")),
    ]))
    story.append(file_table)

    # ── 费用明细 ──
    story.append(Paragraph("💰 费用明细", section_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#e0e0e0")))

    bill_rows = []
    bill_rows.append([
        Paragraph("<b>项目</b>", body_style),
        Paragraph("<b>详情</b>", body_style),
        Paragraph("<b>金额</b>", body_right_style),
    ])

    base_total = 0.0
    all_known = True
    for i, job in enumerate(jobs, 1):
        cost, formula = calc_cost(
            job.page_count, job.copies, job.duplex,
            config.simplex_price, config.duplex_price,
            job.page_range,
        )
        base_total += cost
        if job.page_count == 0:
            all_known = False
        bill_rows.append([
            Paragraph(f"文件{i}", body_style),
            Paragraph(formula if formula else "—", body_style),
            Paragraph(f"¥{cost:.2f}", body_right_style),
        ])

    # 纸张费小计
    bill_rows.append([
        Paragraph("<b>纸张费小计</b>", body_style),
        Paragraph("", body_style),
        Paragraph(f"<b>¥{base_total:.2f}</b>", body_right_style),
    ])

    total = base_total

    # 派送费
    if config.delivery_enabled:
        pct = config.delivery_percentages.get(config.delivery_location, 0.0)
        delivery_fee = base_total * (pct / 100.0)
        total += delivery_fee
        bill_rows.append([
            Paragraph("派送费", body_style),
            Paragraph(f"{config.delivery_location} ({pct:.0f}%)", body_style),
            Paragraph(f"¥{delivery_fee:.2f}", body_right_style),
        ])

    # 加急费
    urgency_price = config.urgency_prices.get(config.urgency, 0.0)
    if urgency_price > 0:
        total += urgency_price
        bill_rows.append([
            Paragraph("加急费", body_style),
            Paragraph(f"优先级: {config.urgency}", body_style),
            Paragraph(f"¥{urgency_price:.2f}", body_right_style),
        ])

    # 首页费
    if config.cover_page:
        total += config.cover_page_price
        bill_rows.append([
            Paragraph("首页费", body_style),
            Paragraph("打印首页信息", body_style),
            Paragraph(f"¥{config.cover_page_price:.2f}", body_right_style),
        ])

    bill_table = Table(bill_rows, colWidths=[80, 290, 80])
    bill_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f5f5f5")),
    ]))
    story.append(bill_table)

    # 合计
    prefix = "≈ " if not all_known else ""
    story.append(Paragraph(f"{prefix}合计: ¥{total:.2f}", total_style))

    # ── 地址信息 ──
    story.append(Paragraph("📍 取件信息", section_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#e0e0e0")))
    if config.delivery_enabled:
        addr_text = f"派送至: <b>{config.delivery_location}</b>"
    else:
        addr_text = f"自取地址: <b>{config.pickup_address}</b>"
    story.append(Paragraph(addr_text, body_style))

    # 页脚
    story.append(Spacer(1, 15 * mm))
    story.append(HRFlowable(width="60%", thickness=0.3, color=HexColor("#e0e0e0")))
    story.append(Paragraph(
        f"HN 本地打印工具 · 生成于 {now_str}",
        footer_style,
    ))

    # ── 生成 PDF ──
    try:
        doc = SimpleDocTemplate(
            output_path, pagesize=A4,
            leftMargin=20 * mm, rightMargin=20 * mm,
            topMargin=15 * mm, bottomMargin=15 * mm,
        )
        doc.build(story)
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"生成首页 PDF 失败: {e}")
        return False
