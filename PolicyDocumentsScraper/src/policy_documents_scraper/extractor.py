"""zip 解压：GBK 文件名修复、路径穿越防护、同名冲突处理。

教育部站点由转换接口生成的 zip 不带 UTF-8 文件名标志位（flag_bits & 0x800），
Python 的 zipfile 会按 cp437 解码导致中文乱码，这里统一按 GBK 重解码。
"""

from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows 保留设备名，作为文件名时会出问题
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _sanitize_segment(segment: str) -> str:
    """清洗 zip 成员的单个路径段，保证跨平台（Windows）合法。

    - 去除 ``<>:"/\\|?*`` 与控制字符（zip 内个别文件名带 ``?`` 等）
    - 去除首尾空格与尾部点号
    - 保留设备名（CON/NUL/COM1...）加下划线前缀
    """
    segment = _FORBIDDEN.sub(" ", segment).strip().rstrip(".")
    if not segment:
        return "_"
    stem = segment.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        segment = "_" + segment
    return segment


def sanitize_name(name: str, max_len: int = 60) -> str:
    """清理为 Windows/Linux 通用安全的文件/目录名。"""
    name = _FORBIDDEN.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip().rstrip(".")
    return name or "untitled"


def fix_zip_name(info: zipfile.ZipInfo) -> str:
    """修复 zip 成员文件名：无 UTF-8 标志时按 GBK 重新解码。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # 极少数情况下既不是 cp437 也不是 GBK，保留原名
        return name


def extract_zip(zip_path: Path, dest_dir: Path) -> tuple[int, int]:
    """安全解压 zip 到 dest_dir，返回 (文件数, 解压后总字节数)。

    - 先做 CRC 完整性校验（testzip）；
    - 拒绝绝对路径与包含 ``..`` 的成员（防 zip-slip）；
    - 同名成员自动追加 ``(1)``、``(2)`` 序号。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_count = 0
    total_bytes = 0
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise zipfile.BadZipFile(f"CRC 校验失败: {bad}")
        for info in zf.infolist():
            raw = fix_zip_name(info).replace("\\", "/").strip()
            if not raw:
                continue
            parts = [p for p in raw.split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                logger.warning("跳过可疑的压缩包成员: %r", info.filename)
                continue
            target = dest_dir.joinpath(*(_sanitize_segment(p) for p in parts))
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target = _uniquify(target)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            file_count += 1
            total_bytes += info.file_size
    return file_count, total_bytes


def _uniquify(path: Path) -> Path:
    """若目标文件已存在，追加序号生成不冲突的路径。"""
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1
