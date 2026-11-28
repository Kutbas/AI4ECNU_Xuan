"""SQLite 状态存储：文档索引、处理状态与运行历史。

状态机：
    pending -> done | failed | missing | external
    failed  -> done | failed | missing   （重试）
    missing / external / done 为终态（missing、external 不再处理）
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .crawler import RunStats

from .parsing import ListItem

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    url             TEXT PRIMARY KEY,
    doc_id          TEXT,
    title           TEXT,
    publish_date    TEXT,
    opertime        TEXT,
    wenhao          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    dir_path        TEXT,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    zip_path        TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    first_seen_at   TEXT,
    last_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_dir ON documents(dir_path);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    mode          TEXT,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    new_docs      INTEGER NOT NULL DEFAULT 0,
    processed     INTEGER NOT NULL DEFAULT 0,
    downloaded    INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    stop_reason   TEXT
);
"""

#: status 命令固定展示的状态键
STATUS_KEYS = ("done", "failed", "pending", "missing", "external")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """SQLite 状态库（线程安全，WAL 模式）。"""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # documents 表
    # ------------------------------------------------------------------

    def upsert_list_item(self, item: ListItem) -> bool:
        """登记列表条目；返回是否为首次发现的新条目。

        已有记录仅刷新列表元数据（且不打扰已完成文档的原始记录）。
        """
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM documents WHERE url = ?", (item.url,)
            ).fetchone()
            is_new = row is None
            if is_new:
                self._conn.execute(
                    "INSERT INTO documents (url, doc_id, title, publish_date, first_seen_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (item.url, item.doc_id or None, item.title, item.publish_date or None, now),
                )
            else:
                self._conn.execute(
                    "UPDATE documents SET title = ?, publish_date = COALESCE(?, publish_date),"
                    " doc_id = COALESCE(doc_id, ?) WHERE url = ?",
                    (item.title, item.publish_date or None, item.doc_id or None, item.url),
                )
        return is_new

    def get(self, url: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documents WHERE url = ?", (url,)
            ).fetchone()

    def mark_attempt(self, url: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE documents SET attempts = attempts + 1, last_attempt_at = ? WHERE url = ?",
                (_now(), url),
            )

    def update_detail(
        self,
        url: str,
        *,
        opertime: str | None = None,
        wenhao: str | None = None,
        publish_date: str | None = None,
    ) -> None:
        """用详情页元数据补充索引（仅填充空缺，不覆盖已有值）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE documents SET opertime = COALESCE(?, opertime),"
                " wenhao = COALESCE(?, wenhao), publish_date = COALESCE(?, publish_date)"
                " WHERE url = ?",
                (opertime, wenhao, publish_date, url),
            )

    def mark_done(
        self,
        url: str,
        dir_name: str,
        size_bytes: int,
        zip_path: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE documents SET status = 'done', dir_path = ?, size_bytes = ?,"
                " zip_path = ?, last_error = NULL WHERE url = ?",
                (dir_name, int(size_bytes), zip_path, url),
            )

    def mark_failed(self, url: str, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE documents SET status = 'failed', last_error = ? WHERE url = ?",
                (error[:500], url),
            )

    def mark_missing(self, url: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE documents SET status = 'missing', last_error = '详情页 404' WHERE url = ?",
                (url,),
            )

    def mark_external(self, url: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE documents SET status = 'external', last_error = '非 moe.gov.cn 链接，跳过下载' WHERE url = ?",
                (url,),
            )

    def list_done(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT url, doc_id, dir_path FROM documents"
                " WHERE status = 'done' AND dir_path IS NOT NULL"
            ).fetchall()

    def list_work_queue(self) -> list[sqlite3.Row]:
        """列出库内全部待处理文档（failed / pending），供 --retry-failed 直接处理。"""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documents WHERE status IN ('failed', 'pending')"
                " ORDER BY status, first_seen_at"
            ).fetchall()

    def reset_to_pending(self, url: str) -> None:
        """将文档重置为待处理（归档目录缺失时由 repair 调用）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE documents SET status = 'pending', dir_path = NULL,"
                " size_bytes = 0, zip_path = NULL, attempts = 0, last_error ="
                " 'repair: 归档目录缺失，已重置待重新下载' WHERE url = ?",
                (url,),
            )

    def find_dir_conflict(self, dir_name: str, url: str) -> bool:
        """检查目录名是否已被其他文档占用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM documents WHERE dir_path = ? AND url <> ? LIMIT 1",
                (dir_name, url),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
            ).fetchall()
        result = {key: 0 for key in STATUS_KEYS}
        for row in rows:
            result[row["status"]] = row["n"]
        return result

    def total_size_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM documents WHERE status = 'done'"
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # runs 表
    # ------------------------------------------------------------------

    def begin_run(self, mode: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, mode) VALUES (?, ?)", (_now(), mode)
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: "RunStats") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, pages_scanned = ?, new_docs = ?,"
                " processed = ?, downloaded = ?, failed = ?, stop_reason = ? WHERE id = ?",
                (
                    _now(),
                    stats.pages_scanned,
                    stats.new_docs,
                    stats.processed,
                    stats.downloaded,
                    stats.failed,
                    stats.stop_reason,
                    run_id,
                ),
            )

    def recent_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
