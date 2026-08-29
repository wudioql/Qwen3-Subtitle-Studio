"""core.temp_cleanup — .temp/ 临时文件清理

.temp/ 现役产物一览（与清理策略一一对应）：
  - app.log                                   日志：不删，启动时轮转截断
  - Qwen3SubtitleStudio/*.lock                单实例锁：不删（进程退出系统自动释放）
  - _ffmpeg_probe_*.wav                       FFmpeg 冒烟探针：正常已自删；退出/启动兜底删
  - <uuid32>.wav                              extract_audio 无缓存名兜底产物：退出即删
  - {stem}_{size}_{mtime}__sr*_ch1.wav        prepare_audio 确定性提取缓存：**跨会话保留**，
                                              启动时按 TEMP_MAX_AGE_DAYS 龄期删除
  - vocals_{stem}_{size}_{mtime}.wav          人声分离缓存（ONNX 推理产物，代价高）：同上跨会话保留
  - 导出产物（用户显式保存到 .temp 下的文件）  不删

策略：
  - 启动时：删除超龄的缓存 WAV 与一切残留探针/uuid WAV + 日志轮转
  - 退出时：只删一次性文件（探针 / uuid WAV），缓存 WAV 保留给下次会话复用
"""

from __future__ import annotations

import logging
import re
import time

from .constants import TEMP_DIR, TEMP_MAX_AGE_DAYS, LOG_MAX_BYTES

logger = logging.getLogger(__name__)


# 确定性缓存名（跨会话保留）：
#   提取缓存 {stem}_{size}_{mtime}__sr{sr}_ch1.wav（core.audio_io.prepare_audio）
#   人声缓存 vocals_{stem}_{size}_{mtime}.wav（core.vocal_separator.extract_vocals_to_wav）
_EXTRACT_CACHE_RE = re.compile(r"__sr\d+_ch1\.wav$", re.IGNORECASE)
_VOCALS_CACHE_RE = re.compile(r"^vocals_.+_\d+_\d+\.wav$", re.IGNORECASE)


def _is_cache_wav(name: str) -> bool:
    """确定性缓存 WAV：跨会话保留，仅按龄期清理。"""
    return bool(_EXTRACT_CACHE_RE.search(name) or _VOCALS_CACHE_RE.match(name))


def _is_disposable_wav(name: str) -> bool:
    """一次性 WAV：探针文件与 uuid 兜底产物，会话结束即无意义。"""
    if name.startswith("_ffmpeg_probe_"):
        return True
    stem = name[:-4] if name.lower().endswith(".wav") else name
    # extract_audio 兜底名 / prepare_audio stat 失败兜底名：32 位纯 hex
    return len(stem) == 32 and all(c in "0123456789abcdef" for c in stem.lower())


def startup_cleanup() -> None:
    """启动时清理：删残留一次性文件 + 超龄缓存 + 日志轮转。

    注意调用顺序：main.py 需在 logging 挂 FileHandler **之前**调用本函数，
    否则 _rotate_log 重写 app.log 会与已打开的句柄冲突（Windows 下直接失败）。
    """
    if not TEMP_DIR.exists():
        return

    now = time.time()
    max_age_sec = TEMP_MAX_AGE_DAYS * 86400
    deleted = 0

    try:
        for entry in TEMP_DIR.iterdir():
            # 锁目录（单实例）不动；其他目录（如用户导出目录）一律不动
            if entry.is_dir():
                continue
            if not entry.is_file():
                continue
            if entry.name == "app.log":
                continue
            if entry.suffix.lower() != ".wav":
                continue

            try:
                if _is_disposable_wav(entry.name):
                    # 一次性文件：残留即孤儿（上次异常退出），直接删
                    entry.unlink(missing_ok=True)
                    deleted += 1
                elif _is_cache_wav(entry.name):
                    # 缓存文件：只删超龄的
                    if now - entry.stat().st_mtime > max_age_sec:
                        entry.unlink(missing_ok=True)
                        deleted += 1
                else:
                    # 未知 WAV（历史版本产物等）：按龄期兜底清理
                    if now - entry.stat().st_mtime > max_age_sec:
                        entry.unlink(missing_ok=True)
                        deleted += 1
            except OSError:
                pass  # 被占用或权限不足，跳过

    except OSError:
        pass  # TEMP_DIR 遍历失败，静默

    if deleted:
        logger.info("[temp] 启动清理：删 %d 个临时文件", deleted)

    _rotate_log()


def shutdown_cleanup() -> None:
    """退出时清理：只删一次性文件（探针 / uuid WAV）。

    确定性缓存 WAV（提取件 / 人声分离件）**保留**，供下次会话直接复用；
    它们的生命周期由 startup_cleanup 的龄期规则管理。
    """
    if not TEMP_DIR.exists():
        return

    deleted = 0
    try:
        for entry in TEMP_DIR.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() != ".wav":
                continue
            if not _is_disposable_wav(entry.name):
                continue
            try:
                entry.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                pass
    except OSError:
        pass

    if deleted:
        logger.debug("[temp] 退出清理：删 %d 个一次性临时文件", deleted)


def _rotate_log() -> None:
    """日志轮转：app.log 超过 LOG_MAX_BYTES 则截断保留尾部 70%。"""
    log_path = TEMP_DIR / "app.log"
    try:
        size = log_path.stat().st_size
    except OSError:
        return

    if size <= LOG_MAX_BYTES:
        return

    try:
        # 读取尾部 70% 内容
        keep_bytes = int(LOG_MAX_BYTES * 0.7)
        with open(log_path, "rb") as f:
            f.seek(size - keep_bytes)
            _ = f.readline()  # 跳到下一个完整行
            tail = f.read()

        with open(log_path, "wb") as f:
            f.write(b"... [log rotated] ...\n")
            f.write(tail)

        logger.info("[temp] app.log 轮转：%d → %d 字节", size, len(tail) + 30)
    except Exception:
        pass  # 日志轮转失败不影响运行
