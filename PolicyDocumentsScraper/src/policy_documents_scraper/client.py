"""HTTP 客户端：对 www.moe.gov.cn 的请求封装（重试、退避、zip 下载校验）。"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "http://www.moe.gov.cn"
SEARCH_URL = BASE_URL + "/was5/web/search"
CONVERT_URL = BASE_URL + "/htmlToPDF/WebPd4MlPDFConvert.jsp"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 视为临时性故障、可以重试的状态码。
#: 5xx 全部视为临时性（含 EdgeOne/CDN 自定义的 550、524 等），
#: 仅追加 408/429 两个 4xx；404 等其余 4xx 为永久性，不重试。
TRANSIENT_STATUS = frozenset({408, 429})


def _is_transient(status: int) -> bool:
    return status >= 500 or status in TRANSIENT_STATUS
#: 单个 zip 的体积保护上限（1 GiB），防止异常响应写满磁盘
MAX_ZIP_BYTES = 1 << 30
_CHUNK = 64 * 1024


class DownloadError(RuntimeError):
    """下载内容不是合法 zip，或体积超过保护上限。"""


class MoeClient:
    """带重试与礼貌延时的 requests 会话封装。"""

    def __init__(
        self,
        timeout: float = 30,
        retries: int = 3,
        backoff: float = 2.0,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def _sleep_before_retry(self, attempt: int, exc: Exception) -> None:
        delay = self.backoff**attempt
        logger.warning("请求失败(%d/%d)：%s；%.1fs 后重试", attempt, self.retries, exc, delay)
        time.sleep(delay)

    def get(self, url: str, **params: str) -> requests.Response:
        """GET 并对临时性故障重试；404 等永久性 4xx 直接抛出不重试。"""
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp
                if _is_transient(resp.status_code):
                    last_exc = requests.HTTPError(
                        f"HTTP {resp.status_code} {url}", response=resp
                    )
                else:
                    # 404/403 等：重试无意义
                    resp.raise_for_status()
            if attempt < self.retries:
                self._sleep_before_retry(attempt, last_exc)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _decode_html(resp: requests.Response) -> str:
        """站点页面均为 UTF-8；响应头未声明 charset 时强制按 UTF-8 解码。

        否则 requests 会回退到 ISO-8859-1，导致标题等中文元数据乱码。
        """
        content_type = resp.headers.get("Content-Type", "")
        if "charset" not in content_type.lower():
            resp.encoding = "utf-8"
        return resp.text

    def get_search_page(self, page: int, channelid: int = 239993) -> str:
        """抓取列表页 HTML。"""
        resp = self.get(SEARCH_URL, channelid=str(channelid), page=str(page))
        return self._decode_html(resp)

    def get_detail(self, url: str) -> str:
        """抓取详情页 HTML 文本。"""
        resp = self.get(url)
        return self._decode_html(resp)

    # ------------------------------------------------------------------
    # zip 下载（复刻详情页"下载"按钮）
    # ------------------------------------------------------------------

    def download_zip(self, page_url: str, opertime: str | None, dest: Path) -> int:
        """模拟详情页 ``#pdfdownload`` 按钮的表单提交，将 zip 写入 dest。

        服务端接口：POST /htmlToPDF/WebPd4MlPDFConvert.jsp
        表单字段：url=encodeURIComponent(详情页地址)、opertime=页面发布时间。

        返回写入的字节数；响应不是 zip 时抛出 DownloadError。
        """
        form = {
            "url": page_url,
            "opertime": opertime
            or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._download_once(form, dest)
            except DownloadError:
                # 服务端明确返回了非 zip 内容，重试无意义
                raise
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.HTTPError,
            ) as exc:
                last_exc = exc
            if attempt < self.retries:
                self._sleep_before_retry(attempt, last_exc)
        assert last_exc is not None
        raise last_exc

    def _download_once(self, form: dict[str, str], dest: Path) -> int:
        with self.session.post(
            CONVERT_URL, data=form, stream=True, timeout=(self.timeout, 300)
        ) as resp:
            if resp.status_code != 200:
                if _is_transient(resp.status_code):
                    raise requests.HTTPError(
                        f"HTTP {resp.status_code} {CONVERT_URL}", response=resp
                    )
                resp.raise_for_status()
            chunks = resp.iter_content(chunk_size=_CHUNK)
            first = next(chunks, b"")
            # zip 魔数校验：PK\x03\x04
            if not first.startswith(b"PK"):
                preview = first + b"".join(next(chunks, b"") for _ in range(8))
                raise DownloadError(
                    "转换接口未返回 zip（Content-Type=%s）：%r"
                    % (resp.headers.get("Content-Type", ""), preview[:200])
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = len(first)
            with open(dest, "wb") as fh:
                fh.write(first)
                for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_ZIP_BYTES:
                        raise DownloadError(
                            f"zip 体积超过 {MAX_ZIP_BYTES} 字节保护上限"
                        )
                    fh.write(chunk)
            return size
