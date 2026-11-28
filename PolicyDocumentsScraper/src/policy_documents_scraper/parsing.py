"""页面解析：列表页条目提取与详情页元数据提取。"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .client import BASE_URL


class ParseError(ValueError):
    """页面结构与预期不符。"""


@dataclass
class ListItem:
    """列表页中的一条文档记录。"""

    url: str
    title: str
    publish_date: str
    doc_id: str


@dataclass
class ListPage:
    """一页列表的解析结果。"""

    items: list[ListItem]
    record_count: int | None = None
    page_size: int = 15
    total_pages: int | None = None


@dataclass
class DetailInfo:
    """详情页元数据。"""

    title: str | None = None
    opertime: str | None = None
    wenhao: str | None = None
    publish_date: str | None = None
    source: str | None = None


_DOC_ID_RE = re.compile(r"t\d{8}_(\d+)\.html?$")
_RECORD_COUNT_RE = re.compile(r"var\s+recordCount\s*=\s*(\d+)")
_PAGE_SIZE_RE = re.compile(r"var\s+pageSize\s*=\s*(\d+)")
_PUBLISH_RE = re.compile(r"发布日期[：:]\s*(\d{4}-\d{2}-\d{2})")
_SOURCE_RE = re.compile(r"来源[：:]\s*(\S+)")

#: 详情页附件的常见扩展名（兜底直接下载时使用）
ATTACHMENT_EXTS = frozenset(
    {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".zip",
     ".rar", ".7z", ".wps", ".et", ".dps", ".txt", ".jpg", ".jpeg", ".png"}
)


def extract_doc_id(url: str) -> str | None:
    """从详情页 URL 提取文档 id，如 t20260916_1451040.html -> 1451040。"""
    m = _DOC_ID_RE.search(url)
    return m.group(1) if m else None


def parse_list_page(html: str, page_url: str = "") -> ListPage:
    """解析列表页：条目链接、发布日期、总记录数与总页数。"""
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("div.scy_lbsj-right-nr")
    if container is None:
        raise ParseError("未找到列表容器 div.scy_lbsj-right-nr，页面结构可能已变化")

    base = page_url or BASE_URL + "/"
    items: list[ListItem] = []
    for li in container.select("ul li"):
        a = li.find("a", href=True)
        if a is None:
            continue
        url = urljoin(base, a["href"])
        title = a.get_text(" ", strip=True)
        span = li.find("span")
        date = span.get_text(strip=True) if span else ""
        items.append(
            ListItem(
                url=url,
                title=title,
                publish_date=date,
                doc_id=extract_doc_id(url) or "",
            )
        )

    m = _RECORD_COUNT_RE.search(html)
    record_count = int(m.group(1)) if m else None
    m = _PAGE_SIZE_RE.search(html)
    page_size = int(m.group(1)) if m else 15
    total_pages = (
        math.ceil(record_count / page_size) if record_count and page_size else None
    )
    return ListPage(
        items=items,
        record_count=record_count,
        page_size=page_size,
        total_pages=total_pages,
    )


def parse_detail_page(html: str) -> DetailInfo:
    """解析详情页元数据（标题 / opertime / 发文字号 / 发布日期 / 来源）。

    各字段均为可选——不同栏目的页面模板存在差异，缺失时由调用方兜底。
    """
    soup = BeautifulSoup(html, "lxml")
    info = DetailInfo()

    h1 = soup.select_one("#downloadContent h1") or soup.find("h1")
    if h1:
        info.title = h1.get_text(" ", strip=True)

    opertime_input = soup.find("input", id="opertime")
    if opertime_input and opertime_input.get("value"):
        info.opertime = str(opertime_input["value"]).strip()

    shuxing = soup.select_one(".moe-detail-shuxing")
    if shuxing:
        text = shuxing.get_text(" ", strip=True)
        m = _PUBLISH_RE.search(text)
        if m:
            info.publish_date = m.group(1)
        m = _SOURCE_RE.search(text)
        if m:
            info.source = m.group(1)

    info.wenhao = _find_table_field(soup, "发文字号")
    return info


def _find_table_field(soup: BeautifulSoup, label: str) -> str | None:
    """在详情属性表（td.policy-item-title / td.policy-item-cont）中查找字段值。"""
    for td in soup.select("td.policy-item-title"):
        if td.get_text(strip=True).rstrip("：:").startswith(label):
            cont = td.find_next_sibling("td")
            if cont is not None:
                value = cont.get_text(" ", strip=True)
                return value or None
    return None


def parse_attachments(html: str, base_url: str) -> list[tuple[str, str]]:
    """提取详情页正文中的附件链接，返回 (绝对地址, 链接文本) 列表。

    用于转换接口不可用时的兜底归档：直接下载附件 + 保存页面 HTML。
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("#downloadContent") or soup
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        absolute = urljoin(base_url, href)
        ext = os.path.splitext(urlparse(absolute).path)[1].lower()
        if ext not in ATTACHMENT_EXTS:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        result.append((absolute, a.get_text(" ", strip=True)))
    return result
