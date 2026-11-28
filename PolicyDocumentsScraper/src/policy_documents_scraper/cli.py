"""命令行入口：run / loop / status。"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .client import MoeClient
from .crawler import Crawler
from .store import Store

logger = logging.getLogger("policy_documents_scraper")

#: run 正常结束的停止原因（退出码 0）；interrupted / list-error 等返回 1
_OK_REASONS = frozenset(
    {"completed", "known-stop", "max-pages", "max-docs", "empty-page", "disk-limit"}
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scraper",
        description="教育部政府门户网站（www.moe.gov.cn）政策文件增量爬虫",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--data-dir",
            type=Path,
            default=Path("data"),
            help="数据目录（状态库/归档文件/日志，默认 ./data）",
        )
        p.add_argument(
            "--verbose", "-v", action="store_true", help="输出调试日志"
        )

    def add_run_options(p: argparse.ArgumentParser) -> None:
        add_common(p)
        p.add_argument(
            "--full",
            action="store_true",
            help="全量模式：关闭已知页早停，扫描全部列表页（只补缺失文档）",
        )
        p.add_argument(
            "--max-pages", type=int, default=None, help="最多扫描的列表页数"
        )
        p.add_argument(
            "--max-docs",
            type=int,
            default=None,
            help="本轮最多处理的文档数（测试用）",
        )
        p.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="相邻请求基础间隔秒数（默认 1.0，另加 0~0.5s 随机抖动）",
        )
        p.add_argument(
            "--known-stop-pages",
            type=int,
            default=3,
            help="连续 N 个整页均为已知文档时早停（默认 3，0=不早停）",
        )
        p.add_argument(
            "--retry-failed",
            action="store_true",
            help="重试全部失败文档（忽略重试次数上限）",
        )
        p.add_argument(
            "--no-download",
            action="store_true",
            help="仅建立索引（抓列表入库），不下载文件",
        )
        p.add_argument(
            "--keep-zip",
            action="store_true",
            help="解压后保留原始 zip（默认删除以节省磁盘）",
        )
        p.add_argument(
            "--max-disk-gb",
            type=float,
            default=50.0,
            help="数据占用上限（GiB），超过即停止采集；0 表示不限制（生产环境用）；默认 50",
        )

    add_run_options(sub.add_parser("run", help="执行一轮抓取"))

    loop_p = sub.add_parser("loop", help="常驻模式：按固定间隔反复执行抓取")
    add_run_options(loop_p)
    loop_p.add_argument(
        "--interval",
        type=int,
        default=21600,
        help="两轮抓取之间的间隔秒数（默认 21600，即 6 小时）",
    )

    status_p = sub.add_parser("status", help="查看抓取状态统计")
    add_common(status_p)

    sub.add_parser(
        "repair",
        help="校验已归档目录完整性；缺失/为空的重置为待下载（下次运行自动补齐）",
    ).add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="数据目录（默认 ./data）",
    )
    return parser


def setup_logging(logs_dir: Path, verbose: bool = False) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    # Windows 控制台默认 GBK，统一改 UTF-8 避免中文日志乱码/报错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 非标准流时忽略
            pass

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "scraper.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def cmd_status(store: Store, data_dir: Path) -> int:
    stats = store.stats()
    total = sum(stats.values())
    size = store.total_size_bytes()
    print("=" * 56)
    print(f"状态库    ：{data_dir / 'state.db'}")
    print(f"归档目录  ：{data_dir / 'files'}")
    print(f"文档总数  ：{total}")
    for key in ("done", "failed", "pending", "missing", "external"):
        if stats.get(key):
            print(f"  {key:<10}{stats[key]}")
    print(f"数据占用  ：{human_size(size)}")
    print("-" * 56)
    print("最近运行：")
    runs = store.recent_runs(5)
    if not runs:
        print("  （暂无）")
    for run in runs:
        print(
            f"  {run['started_at']}  {run['mode']:<5}"
            f" 页:{run['pages_scanned']:<4} 新:{run['new_docs']:<5}"
            f" 下载:{run['downloaded']:<5} 失败:{run['failed']:<4}"
            f" -> {run['stop_reason']}"
        )
    print("=" * 56)
    return 0


def cmd_repair(store: Store, data_dir: Path) -> int:
    """校验所有 done 文档的归档目录，缺失/为空的重置为 pending。"""
    files_dir = data_dir / "files"
    rows = store.list_done()
    broken: list[str] = []
    for row in rows:
        doc_dir = files_dir / row["dir_path"]
        if not doc_dir.is_dir() or not any(doc_dir.iterdir()):
            broken.append(row["url"])
    for url in broken:
        store.reset_to_pending(url)
    print(f"校验 done 文档 {len(rows)} 篇：完整 {len(rows) - len(broken)}，"
          f"缺失 {len(broken)}（已重置为待下载）")
    for url in broken[:20]:
        print(f"  {url}")
    if len(broken) > 20:
        print(f"  ...（其余 {len(broken) - 20} 篇略）")
    if broken:
        print("提示：运行 uv run scraper run --full 补齐上述文档。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir: Path = args.data_dir.resolve()
    setup_logging(data_dir / "logs", verbose=getattr(args, "verbose", False))

    store = Store(data_dir / "state.db")
    try:
        if args.command == "status":
            return cmd_status(store, data_dir)
        if args.command == "repair":
            return cmd_repair(store, data_dir)

        client = MoeClient()
        shutdown = threading.Event()

        def _on_signal(signum, _frame) -> None:  # noqa: ANN001
            if not shutdown.is_set():
                logger.info("收到信号 %s，正在优雅停止（可再次 Ctrl+C 强制退出）…", signum)
            shutdown.set()

        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)

        crawler = Crawler(
            client,
            store,
            data_dir / "files",
            data_dir / "zips",
            delay=args.delay,
            keep_zip=args.keep_zip,
            max_disk_bytes=int(args.max_disk_gb * 2**30) if args.max_disk_gb > 0 else None,
            stop_event=shutdown,
        )
        run_kwargs = dict(
            max_pages=args.max_pages,
            max_docs=args.max_docs,
            known_stop_pages=0 if args.full else args.known_stop_pages,
            retry_failed=args.retry_failed,
            download=not args.no_download,
        )

        if args.command == "run":
            stats = crawler.run(mode="run", **run_kwargs)
            return 0 if stats.stop_reason in _OK_REASONS else 1

        # loop 模式：常驻定时执行
        logger.info("进入常驻模式，轮询间隔 %d 秒", args.interval)
        while not shutdown.is_set():
            stats = crawler.run(mode="loop", **run_kwargs)
            logger.info(
                "本轮结束（%s），%d 秒后开始下一轮（Ctrl+C 退出）",
                stats.stop_reason,
                args.interval,
            )
            if shutdown.wait(args.interval):
                break
        logger.info("已退出常驻模式")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
