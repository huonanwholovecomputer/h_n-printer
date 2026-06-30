"""
printer_config.py — 配置管理模块
负责读取/写入 JSON 配置文件，管理打印机名称、双面模式、任务列表等。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "copies": self.copies,
            "duplex": self.duplex,
            "duplex_mode": self.duplex_mode,
            "page_range": self.page_range,
            "page_count": self.page_count,
            "orientation": self.orientation,
            "engine": self.engine,
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
            engine=data.get("engine", "word"),
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


@dataclass
class PrinterConfig:
    """
    打印机配置数据类。

    JSON 结构示例:
    {
      "printer_name": "HP LaserJet MFP M232-M237",
      "duplex_mode": "long-edge",
      "keep_temp_pdf": false,
      "jobs": [
        {
          "file_path": "C:/docs/report.docx",
          "copies": 2,
          "duplex": "on",
          "duplex_mode": "long-edge",
          "page_range": "1-5",
          "engine": "word"
        }
      ]
    }
    """
    printer_name: str = ""
    duplex_mode: str = "long-edge"    # 'simplex' | 'long-edge' | 'short-edge'
    keep_temp_pdf: bool = False
    simplex_price: float = 0.2
    duplex_price: float = 0.3
    last_dir: str = ""
    jobs: list[PrintJob] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_name": self.printer_name,
            "duplex_mode": self.duplex_mode,
            "keep_temp_pdf": self.keep_temp_pdf,
            "simplex_price": self.simplex_price,
            "duplex_price": self.duplex_price,
            "last_dir": self.last_dir,
            "jobs": [job.to_dict() for job in self.jobs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrinterConfig":
        jobs_data = data.get("jobs", [])
        jobs = [PrintJob.from_dict(j) for j in jobs_data] if isinstance(jobs_data, list) else []
        return cls(
            printer_name=data.get("printer_name", ""),
            duplex_mode=data.get("duplex_mode", "long-edge"),
            keep_temp_pdf=bool(data.get("keep_temp_pdf", False)),
            simplex_price=float(data.get("simplex_price", 0.2)),
            duplex_price=float(data.get("duplex_price", 0.3)),
            last_dir=data.get("last_dir", ""),
            jobs=jobs,
        )

    def save(self, path: str) -> None:
        """保存配置到 JSON 文件"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "PrinterConfig":
        """从 JSON 文件加载配置；文件不存在或损坏则返回默认配置"""
        import logging
        logger = logging.getLogger(__name__)

        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"加载配置失败 ({path}): {e}，将使用默认配置")
            return cls()
