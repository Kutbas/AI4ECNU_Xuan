"""抓取编排：列表分页 -> 增量识别 -> 详情获取 -> zip 下载解压。

增量策略：逐页扫描列表，当连续 ``known_stop_pages`` 个整页的所有条目
均为已知状态（done/missing/external/重试耗尽的 failed）时提前结束；
``full`` 模式关闭早停，扫描全部页用于首次全量或补漏。
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .client import DownloadError, MoeClient
from .extractor import _uniquify, extract_zip, sanitize_name
from .parsing import ListItem, parse_attachments, parse_detail_page, parse_list_page
from .store import Store

logger = logging.getLogger(__name__)


class StopRequested(Exception):
    """收到停止信号（Ctrl+C / SIGTERM），需要优雅退出。"""


@dataclass
class RunStats:
    """单轮运行统计。"""

    mode: str
    pages_scanned: int = 0
    new_docs: int = 0
    processed: int = 0
    downloaded: int = 0
    failed: int = 0
    missing: int = 0
    recovered: int = 0
    stop_reason: str = "completed"


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class Crawler:
    def __init__(
        self,
        client: MoeClient,
        store: Store,
        files_dir: Path,
        zips_dir: Path,
        *,
        delay: float = 1.0,
        jitter: float = 0.5,
        keep_zip: bool = False,
        max_disk_bytes: int | None = None,
        max_attempts: int = 3,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.files_dir = Path(files_dir)
        self.zips_dir = Path(zips_dir)
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self.keep_zip = keep_zip
        self.max_disk_bytes = max_disk_bytes
        self.max_attempts = max(1, max_attempts)
        self._stop = stop_event or threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        mode: str = "run",
        channelid: int = 239993,
        max_pages: int | None = None,
        max_docs: int | None = None,
        known_stop_pages: int = 3,
        retry_failed: bool = False,
        download: bool = True,
    ) -> RunStats:
        """执行一轮抓取，返回统计信息。

        :param max_pages: 最多扫描的列表页数（None 为不限制）
        :param max_docs: 本轮最多处理的文档数（None 为不限制，测试用）
        :param known_stop_pages: 连续 N 个整页均为已知文档时早停；0 表示不早停
        :param retry_failed: 无视次数上限，重试全部失败文档
        :param download: False 时仅建立索引（不抓详情、不下载）
        """
        known_stop_pages = max(0, known_stop_pages)
        stats = RunStats(mode=mode)
        run_id = self.store.begin_run(mode)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.zips_dir.mkdir(parents=True, exist_ok=True)
        consecutive_known = 0
        page = 1
        logger.info(
            "开始抓取（mode=%s, 早停=%s页, 下载=%s, 磁盘上限=%s）",
            mode,
            known_stop_pages or "否",
            "开" if download else "关",
            f"{self.max_disk_bytes / 2**30:.1f}GiB" if self.max_disk_bytes else "无",
        )

        try:
            # --retry-failed：先直接处理库内的 failed/pending（无需翻页扫描）
            prefetch_aborted = False
            if retry_failed:
                prefetch_aborted = self._prefetch_retries(stats, max_docs)

            while not prefetch_aborted:
                if self._stop.is_set():
                    stats.stop_reason = "interrupted"
                    break
                if max_pages is not None and page > max_pages:
                    stats.stop_reason = "max-pages"
                    break

                try:
                    list_page = parse_list_page(
                        self.client.get_search_page(page, channelid)
                    )
                except StopRequested:
                    stats.stop_reason = "interrupted"
                    break
                except Exception as exc:  # 列表页失败：重试已在上层用尽
                    logger.error("第 %d 页列表抓取失败：%s", page, exc)
                    stats.stop_reason = f"list-error: {exc}"
                    break

                stats.pages_scanned += 1
                if page == 1 and list_page.total_pages:
                    logger.info(
                        "频道共 %s 条记录，%d 页（每页 %d 条）",
                        list_page.record_count,
                        list_page.total_pages,
                        list_page.page_size,
                    )
                if not list_page.items:
                    stats.stop_reason = "empty-page"
                    break

                page_known = 0
                abort_reason: str | None = None
                for item in list_page.items:
                    if self._stop.is_set():
                        abort_reason = "interrupted"
                        break
                    if self._register(item):
                        stats.new_docs += 1
                        logger.info(
                            "发现新文档 [%s] %s (%s)",
                            item.doc_id,
                            item.title,
                            item.publish_date,
                        )
                    row = self.store.get(item.url)
                    if not self._needs_work(row, retry_failed):
                        page_known += 1
                        continue
                    if not download:
                        # 索引模式：仅登记，保持 pending，供后续下载
                        continue
                    if max_docs is not None and stats.processed >= max_docs:
                        abort_reason = "max-docs"
                        break
                    if self._disk_exceeded():
                        abort_reason = "disk-limit"
                        break
                    abort_reason = self._handle(item, row, stats)
                    if abort_reason:
                        break
                    try:
                        self._polite_sleep()
                    except StopRequested:
                        abort_reason = "interrupted"
                        break

                if abort_reason:
                    stats.stop_reason = abort_reason
                    break

                if page_known == len(list_page.items):
                    consecutive_known += 1
                    logger.info(
                        "第 %d 页全部为已知文档（连续 %d/%s）",
                        page,
                        consecutive_known,
                        known_stop_pages or "∞",
                    )
                    if known_stop_pages and consecutive_known >= known_stop_pages:
                        stats.stop_reason = "known-stop"
                        break
                else:
                    consecutive_known = 0

                page += 1
                try:
                    self._polite_sleep()
                except StopRequested:
                    stats.stop_reason = "interrupted"
                    break
        finally:
            self.store.finish_run(run_id, stats)
            logger.info(
                "本轮结束（%s）：扫描 %d 页，新发现 %d，下载 %d，失败 %d，"
                "缺失 %d，恢复 %d",
                stats.stop_reason,
                stats.pages_scanned,
                stats.new_docs,
                stats.downloaded,
                stats.failed,
                stats.missing,
                stats.recovered,
            )
        return stats

    # ------------------------------------------------------------------
    # 内部流程
    # ------------------------------------------------------------------

    def _register(self, item: ListItem) -> bool:
        """登记列表条目，返回是否为新发现。站外链接直接标记 external。"""
        if not item.doc_id:
            item.doc_id = "id_" + hashlib.md5(item.url.encode("utf-8")).hexdigest()[:10]
        is_new = self.store.upsert_list_item(item)
        host = urlparse(item.url).hostname or ""
        if is_new and not host.endswith("moe.gov.cn"):
            self.store.mark_external(item.url)
            logger.info("站外链接，跳过下载：%s", item.url)
        return is_new

    def _needs_work(self, row, retry_failed: bool) -> bool:
        """判断文档是否还需要处理。"""
        if row is None:
            return False
        status = row["status"]
        if status == "pending":
            return True
        if status == "failed":
            return retry_failed or row["attempts"] < self.max_attempts
        return False  # done / missing / external

    def _disk_exceeded(self) -> bool:
        if not self.max_disk_bytes:
            return False
        used = self.store.total_size_bytes()
        if used >= self.max_disk_bytes:
            logger.warning(
                "数据占用 %.2f GiB 已达阈值 %.2f GiB，本轮停止采集"
                "（生产环境可用 --max-disk-gb 0 解除限制）",
                used / 2**30,
                self.max_disk_bytes / 2**30,
            )
            return True
        return False

    def _polite_sleep(self) -> None:
        """请求间的礼貌延时（基础延时 + 随机抖动），可被停止信号打断。"""
        deadline = time.monotonic() + self.delay + random.uniform(0, self.jitter)
        while True:
            if self._stop.is_set():
                raise StopRequested
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

    def _prefetch_retries(self, stats: RunStats, max_docs: int | None) -> bool:
        """--retry-failed：直接处理库内 failed/pending 文档，无需翻页扫描。

        返回 True 表示本轮应中止（不再进入翻页流程）。
        """
        queue = self.store.list_work_queue()
        if not queue:
            return False
        logger.info("直接处理库内待处理文档 %d 篇", len(queue))
        for row in queue:
            if self._stop.is_set():
                stats.stop_reason = "interrupted"
                return True
            if max_docs is not None and stats.processed >= max_docs:
                stats.stop_reason = "max-docs"
                return True
            if self._disk_exceeded():
                stats.stop_reason = "disk-limit"
                return True
            item = ListItem(
                url=row["url"],
                title=row["title"] or "",
                publish_date=row["publish_date"] or "",
                doc_id=row["doc_id"] or "",
            )
            abort = self._handle(item, row, stats)
            if abort:
                stats.stop_reason = abort
                return True
            try:
                self._polite_sleep()
            except StopRequested:
                stats.stop_reason = "interrupted"
                return True
        return False

    def _handle(self, item: ListItem, row, stats: RunStats) -> str | None:
        """处理单篇文档并更新统计；返回中止原因（interrupted）或 None。"""
        stats.processed += 1
        try:
            outcome = self._process(item, row)
        except StopRequested:
            return "interrupted"
        except Exception as exc:
            stats.failed += 1
            self.store.mark_failed(item.url, f"{type(exc).__name__}: {exc}")
            logger.error("处理失败 [%s] %s：%s", item.doc_id, item.url, exc)
            return None
        if outcome == "done":
            stats.downloaded += 1
        elif outcome == "missing":
            stats.missing += 1
        elif outcome == "recovered":
            stats.recovered += 1
        return None

    def _process(self, item: ListItem, row) -> str:
        """处理单篇文档：抓详情 -> 下载 zip -> 解压归档。

        返回 done / missing / recovered；异常由调用方记为 failed。
        """
        url = item.url
        doc_id = row["doc_id"] or item.doc_id
        self.store.mark_attempt(url)

        dir_name = sanitize_name(f"{doc_id}_{row['title'] or item.title}")
        final_dir = self.files_dir / dir_name

        # 崩溃恢复：上次在"解压完成、落库之前"中断，目录已存在
        outcome = self._try_recover(url, final_dir)
        if outcome:
            return outcome

        # 1) 详情页
        try:
            detail_html = self.client.get_detail(url)
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 404:
                self.store.mark_missing(url)
                logger.warning("详情页不存在（404）：%s", url)
                return "missing"
            raise
        detail = parse_detail_page(detail_html)
        self.store.update_detail(
            url,
            opertime=detail.opertime,
            wenhao=detail.wenhao,
            publish_date=detail.publish_date,
        )
        logger.debug(
            "详情解析 [%s] 标题=%r 发文字号=%r opertime=%r",
            doc_id,
            detail.title,
            detail.wenhao,
            detail.opertime,
        )

        # 详情页标题更完整，优先用于目录命名；冲突时追加 url 摘要
        dir_name = sanitize_name(f"{doc_id}_{detail.title or row['title'] or item.title}")
        if self.store.find_dir_conflict(dir_name, url):
            dir_name = f"{dir_name}_{hashlib.md5(url.encode()).hexdigest()[:6]}"
        final_dir = self.files_dir / dir_name
        outcome = self._try_recover(url, final_dir)
        if outcome:
            return outcome

        self._polite_sleep()

        # 2) 下载 zip 并解压（临时目录 -> 成功后改名，保证原子性）
        tmp_root = self.files_dir / ".tmp"
        tmp_zip = tmp_root / f"{doc_id}.zip"
        tmp_dir = tmp_root / f"{doc_id}.extract"
        try:
            zip_bytes = self.client.download_zip(url, detail.opertime, tmp_zip)
            n_files, extracted_bytes = extract_zip(tmp_zip, tmp_dir)
            if n_files == 0:
                raise RuntimeError("zip 内没有有效文件")
            if final_dir.exists():
                shutil.rmtree(final_dir)
            tmp_dir.rename(final_dir)
            size_bytes = extracted_bytes
            zip_path = None
            if self.keep_zip:
                zip_dest = self.zips_dir / f"{dir_name}.zip"
                shutil.move(str(tmp_zip), str(zip_dest))
                size_bytes += zip_bytes
                zip_path = str(zip_dest)
            self.store.mark_done(url, dir_name, size_bytes, zip_path=zip_path)
            logger.info(
                "已下载并解压：%s（%d 个文件，%.1f KiB）",
                dir_name,
                n_files,
                extracted_bytes / 1024,
            )
            return "done"
        except (DownloadError, requests.HTTPError) as exc:
            # 转换接口对个别页面永久失败（550/404，重试已在上层用尽）：
            # 兜底归档——保存页面 HTML 快照并直接下载页面内附件
            logger.warning("转换接口不可用（%s），改用兜底方式归档：%s", exc, url)
            if self._fallback_archive(url, dir_name, final_dir, detail_html):
                return "done"
            raise
        finally:
            tmp_zip.unlink(missing_ok=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _fallback_archive(
        self, url: str, dir_name: str, final_dir: Path, detail_html: str
    ) -> bool:
        """兜底归档：保存页面 HTML 快照 + 直接下载正文附件。

        成功返回 True；失败返回 False（由调用方按 failed 记录）。
        """
        doc_id = final_dir.name.split("_", 1)[0]
        tmp_dir = self.files_dir / ".tmp" / f"{doc_id}.fallback"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "page.html").write_text(detail_html, encoding="utf-8")
            n_files = 0
            for attach_url, text in parse_attachments(detail_html, url):
                path = urlparse(attach_url).path
                ext = os.path.splitext(path)[1].lower()
                stem = sanitize_name(text) if text else sanitize_name(
                    os.path.basename(path) or "attachment"
                )
                target = _uniquify(tmp_dir / f"{stem}{ext}")
                try:
                    resp = self.client.get(attach_url)
                    target.write_bytes(resp.content)
                    n_files += 1
                except Exception as exc:  # 单个附件失败不影响整体归档
                    logger.warning("附件下载失败，跳过：%s（%s）", attach_url, exc)
                self._polite_sleep()
            if final_dir.exists():
                shutil.rmtree(final_dir)
            tmp_dir.rename(final_dir)
            self.store.mark_done(url, dir_name, _dir_size(final_dir))
            logger.info("兜底归档完成：%s（页面快照 + %d 个附件）", dir_name, n_files)
            return True
        except StopRequested:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        except Exception as exc:
            logger.error("兜底归档失败：%s", exc)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return False

    def _try_recover(self, url: str, final_dir: Path) -> str | None:
        """目标目录已存在且非空时，直接补记为完成（上次运行中断的遗留）。"""
        if not final_dir.exists():
            return None
        size = _dir_size(final_dir)
        if size <= 0:
            return None
        self.store.mark_done(url, final_dir.name, size)
        logger.info("检测到已存在的归档目录，补记为完成：%s", final_dir.name)
        return "recovered"
