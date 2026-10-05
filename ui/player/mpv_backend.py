"""可选 libmpv 播放后端：异步初始化、非阻塞命令与 libass 字幕轨管理。

硬约束：
- ``import mpv`` 之前先把 ``libmpv-2.dll`` 目录加入 PATH / add_dll_directory，
  且进程级持有 DLL directory handle；
- libmpv 初始化、播放命令、字幕命令和 terminate 全部在 daemon worker 执行，
  GUI 线程只入队，绝不 join；watchdog 超时后由 PlayerPanel 回退 Qt；
- python-mpv observer 只发 Qt Signal，不直接操作 QWidget；time-pos 限频，避免
  高频回调淹没 GUI 事件队列；
- 预览字幕使用唯一临时文件并按 mpv track id 删除，不把 title 当 id。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal

from .mpv_worker import MpvWorker

logger = logging.getLogger(__name__)

_DLL_NAME = "libmpv-2.dll"
_DLL_DIR_HANDLES: list[object] = []
_DLL_PATHS_PREPARED: set[str] = set()


@dataclass(frozen=True)
class MpvProbeResult:
    """不加载原生库的快速探测；真正可用性由异步 MPV() 初始化确认。"""

    candidate: bool
    dll_path: Optional[Path]
    binding_found: bool
    reason: str


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    try:
        from core.constants import PROJECT_ROOT

        candidates.append(PROJECT_ROOT / _DLL_NAME)
    except Exception:  # noqa: BLE001
        pass
    candidates.append(Path.cwd() / _DLL_NAME)
    return candidates


def find_libmpv() -> Optional[Path]:
    """定位 libmpv-2.dll：项目根 → 工作目录 → PATH。"""
    for path in _candidate_paths():
        if path.is_file():
            return path.resolve()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        path = Path(directory) / _DLL_NAME
        if path.is_file():
            return path.resolve()
    return None


def probe_libmpv() -> MpvProbeResult:
    """快速探测 DLL 与 python-mpv，不 import mpv、不触碰原生代码。"""
    dll = find_libmpv()
    if str(os.environ.get("QSS_DISABLE_MPV", "")).strip().lower() in {"1", "true", "yes"}:
        return MpvProbeResult(False, dll, False, "QSS_DISABLE_MPV 已禁用 libmpv")
    try:
        binding = importlib.util.find_spec("mpv") is not None
    except Exception:  # noqa: BLE001
        binding = False
    if sys.platform != "win32":
        return MpvProbeResult(False, dll, binding, "libmpv-2.dll 仅在 Windows 目标环境启用")
    if dll is None:
        return MpvProbeResult(False, None, binding, f"未找到 {_DLL_NAME}")
    if not binding:
        return MpvProbeResult(False, dll, False, "未安装 python-mpv")
    return MpvProbeResult(True, dll, True, "DLL 与 python-mpv 已就绪，等待异步初始化")


def mpv_available() -> bool:
    """兼容旧调用：只表示具备异步初始化候选条件，不同步 import/实例化。"""
    return probe_libmpv().candidate


def inject_libmpv_dir() -> Optional[Path]:
    """在 import mpv 前准备 DLL 搜索路径，并持有 add_dll_directory handle。"""
    dll = find_libmpv()
    if dll is None:
        return None
    directory = str(dll.parent.resolve())
    key = os.path.normcase(directory)
    if key in _DLL_PATHS_PREPARED:
        return dll

    current = [item for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    if key not in {os.path.normcase(os.path.abspath(item)) for item in current}:
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")

    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        handle = os.add_dll_directory(directory)
        _DLL_DIR_HANDLES.append(handle)  # 必须持有到进程结束
    _DLL_PATHS_PREPARED.add(key)
    return dll


class _MpvSignalBridge(QObject):
    ready = Signal()
    failed = Signal(str)
    time_changed = Signal(float)
    duration_changed = Signal(float)
    playing_changed = Signal(bool)
    surface_clicked = Signal()


class MpvBackend:
    """嵌入 host HWND 的异步 libmpv 后端；公开方法均不阻塞调用线程。"""

    def __init__(
        self,
        host,
        on_time: Callable[[float], None],
        on_duration: Callable[[float], None],
        on_playing: Callable[[bool], None],
        *,
        on_ready: Callable[[], None],
        on_failed: Callable[[str], None],
        on_surface_click: Callable[[], None] | None = None,
        init_timeout: float = 10.0,
    ) -> None:
        # QWidget/WinId 只能在 GUI 线程准备；此后所有 libmpv 调用进入 worker。
        # 顺序不可反：先阻止 native 属性向祖先/兄弟扩散，再只原生化 mpv host。
        # 否则 QMenu/Fluent ComboBox popup 会收到非 top-level transient parent，
        # Windows 控制台反复报 “ComboBoxClassWindow must be a top level window”。
        host.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._host = host
        self._wid = int(host.winId())
        self._bridge = _MpvSignalBridge(host)
        self._bridge.time_changed.connect(on_time)
        self._bridge.duration_changed.connect(on_duration)
        self._bridge.playing_changed.connect(on_playing)
        self._bridge.ready.connect(on_ready)
        self._bridge.failed.connect(on_failed)
        if on_surface_click is not None:
            self._bridge.surface_clicked.connect(on_surface_click)

        self._state_lock = threading.Lock()
        self._position_cache = 0.0
        self._duration_cache = 0.0
        self._last_time_emit = 0.0
        self._subtitle_track_id: Optional[int] = None
        self._subtitle_path: Optional[Path] = None
        self._subtitle_revision = 0
        self._closing = False

        dll = inject_libmpv_dir()
        if dll is None:
            raise FileNotFoundError(_DLL_NAME)
        self._dll_path = dll
        self._worker = MpvWorker(
            self._create_player,
            on_ready=self._emit_ready,
            on_fatal=self._emit_failed,
            on_abandon=self._abandon_player,
            init_timeout=init_timeout,
            queue_limit=32,
        )

    @property
    def dll_path(self) -> Path:
        return self._dll_path

    @property
    def failed(self) -> bool:
        return self._worker.failed

    def _safe_emit(self, signal, *args) -> None:
        if self._closing:
            return
        try:
            signal.emit(*args)
        except RuntimeError:
            # Qt host 已销毁；observer 可能仍在 daemon event thread 收尾。
            pass

    def _emit_ready(self) -> None:
        self._safe_emit(self._bridge.ready)

    def _emit_failed(self, message: str) -> None:
        logger.error("[mpv] %s", message)
        self._safe_emit(self._bridge.failed, message)

    def _create_player(self):
        # inject 已在 GUI 线程完成；这里才允许 import mpv（会加载原生 DLL）。
        import mpv

        player = mpv.MPV(
            wid=self._wid,
            keep_open=True,
            # 音频文件没有视频轨时也强制建立 VO；libass 才能把外部 ASS/SRT
            # 画到嵌入画布。immediate 也让预热阶段提前确认 VO 是否可用。
            force_window="immediate",
            audio_display=False,       # 不让封面图替代纯色字幕背景
            background_color="#111827",
            osc=False,
            osd_level=0,
            # --wid 下 mpv 会在 Qt host 内再创建自己的子窗口；Windows 鼠标消息
            # 因而不会到达 host QWidget。只开放 VO 输入并注册一个强制鼠标绑定，
            # 其它默认键位仍关闭，避免 mpv 抢走应用快捷键或自行切换暂停。
            input_default_bindings=False,
            input_vo_keyboard=True,
        )

        @player.on_key_press("MOUSE_BTN0")
        def surface_click() -> None:
            self._safe_emit(self._bridge.surface_clicked)

        self._surface_click_binding = surface_click
        player.observe_property("time-pos", self._cb_time_pos)
        player.observe_property("duration", self._cb_duration)
        player.observe_property("pause", self._cb_pause)
        return player

    @staticmethod
    def _abandon_player(player) -> None:
        """watchdog 判死后避免析构在 GUI/退出阶段再次同步 terminate。

        原操作已经从阻塞中返回时才会走到这里；先投递异步 quit，再清空 python-mpv
        的主 handle，让 ``__del__`` 不会执行可能永久 join 的 terminate。原生资源最终
        由进程退出回收；该 backend 在本次进程中不再复活。
        """
        try:
            player.command_async("quit")
        except Exception:  # noqa: BLE001
            pass
        try:
            player.handle = None
        except Exception:  # noqa: BLE001
            pass

    # ── mpv event thread → 缓存 + Qt queued signal ─────────────────
    def _cb_time_pos(self, _name, value) -> None:
        if value is None or self._closing:
            return
        try:
            position = float(value)
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        with self._state_lock:
            self._position_cache = position
            if now - self._last_time_emit < 1.0 / 30.0:
                return
            self._last_time_emit = now
        self._safe_emit(self._bridge.time_changed, position)

    def _cb_duration(self, _name, value) -> None:
        if value is None or self._closing:
            return
        try:
            duration = max(0.0, float(value))
        except (TypeError, ValueError):
            return
        with self._state_lock:
            self._duration_cache = duration
        self._safe_emit(self._bridge.duration_changed, duration)

    def _cb_pause(self, _name, value) -> None:
        if self._closing:
            return
        self._safe_emit(self._bridge.playing_changed, not bool(value))

    # ── 非阻塞播放控制 ─────────────────────────────────────────────
    def load(self, media_path: str | Path) -> bool:
        path = str(Path(media_path).resolve())

        def action(player) -> None:
            player.pause = True
            self._remove_subtitle_track(player)
            player.loadfile(path)
            with self._state_lock:
                self._position_cache = 0.0
                self._duration_cache = 0.0

        return self._worker.submit("load", action, timeout=10.0, coalesce_key="load")

    def play(self) -> bool:
        return self._worker.submit(
            "play", lambda player: setattr(player, "pause", False), timeout=2.0,
            coalesce_key="pause-state",
        )

    def pause(self) -> bool:
        return self._worker.submit(
            "pause", lambda player: setattr(player, "pause", True), timeout=2.0,
            coalesce_key="pause-state",
        )

    def stop(self) -> bool:
        def action(player) -> None:
            player.pause = True
            player.command("seek", 0, "absolute")
            with self._state_lock:
                self._position_cache = 0.0

        return self._worker.submit("stop", action, timeout=3.0, coalesce_key="transport")

    def seek(self, seconds: float) -> bool:
        target = max(0.0, float(seconds))

        def action(player) -> None:
            player.time_pos = target
            with self._state_lock:
                self._position_cache = target

        return self._worker.submit(
            "seek", action, timeout=2.0, coalesce_key="seek",
        )

    def position(self) -> float:
        with self._state_lock:
            return self._position_cache

    def duration(self) -> float:
        with self._state_lock:
            return self._duration_cache

    # ── 字幕轨（worker 内串行写文件/按 id 移除）────────────────────
    def set_subtitle(self, text: str, *, is_ass: bool) -> bool:
        with self._state_lock:
            self._subtitle_revision += 1
            revision = self._subtitle_revision
        suffix = "ass" if is_ass else "srt"

        def action(player) -> None:
            from core.constants import ensure_temp_dir

            path = ensure_temp_dir() / f"qss_preview_{id(self):x}_{revision}.{suffix}"
            path.write_text(text, encoding="utf-8", newline="\n")
            self._remove_subtitle_track(player)
            track_id = player.command(
                "sub-add", str(path), "select", "qss-preview", "und",
            )
            parsed_id: Optional[int]
            try:
                parsed_id = int(track_id) if track_id is not None else None
            except (TypeError, ValueError):
                parsed_id = None
            if parsed_id is None:
                # 新旧 mpv 对 sub-add 返回值存在差异；从 track-list 按标题/路径兜底找 id。
                for track in list(getattr(player, "track_list", None) or []):
                    if track.get("type") != "sub":
                        continue
                    if track.get("title") == "qss-preview" or track.get("external-filename") == str(path):
                        try:
                            parsed_id = int(track.get("id"))
                        except (TypeError, ValueError):
                            parsed_id = None
                        break
            with self._state_lock:
                self._subtitle_track_id = parsed_id
                self._subtitle_path = path
            self._request_subtitle_redraw(player)

        return self._worker.submit(
            "subtitle", action, timeout=5.0, fatal_on_error=False,
            coalesce_key="subtitle",
        )

    def clear_subtitle(self) -> bool:
        return self._worker.submit(
            "subtitle-clear", self._remove_subtitle_track, timeout=3.0,
            fatal_on_error=False, coalesce_key="subtitle",
        )

    @staticmethod
    def _request_subtitle_redraw(player) -> None:
        """让 mpv/libass 立即重算当前帧，尤其覆盖暂停状态下的轨道替换。"""
        try:
            # 相对精确 seek 0 不改变播放位置，但会使 VO/libass 重新提交当前帧。
            player.command("seek", "0", "relative+exact")
        except Exception as exc:  # noqa: BLE001
            # 字幕轨已经替换成功；旧版 mpv 不支持该 flags 时不应触发后端回退。
            logger.debug("[mpv] 请求字幕当前帧重绘失败: %s", exc)

    def _remove_subtitle_track(self, player) -> None:
        with self._state_lock:
            track_id = self._subtitle_track_id
            path = self._subtitle_path
            self._subtitle_track_id = None
            self._subtitle_path = None
        if track_id is not None:
            try:
                player.command("sub-remove", str(track_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[mpv] sub-remove id=%s 失败: %s", track_id, exc)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("[mpv] 删除旧预览字幕失败: %s", path)

    def shutdown(self) -> None:
        """请求 worker 清理；立即返回，关闭窗口绝不等待原生 terminate。"""
        if self._closing:
            return
        self._closing = True

        def terminate(player) -> None:
            self._remove_subtitle_track(player)
            player.terminate()

        self._worker.close(terminate, timeout=2.0)


__all__ = [
    "MpvBackend",
    "MpvProbeResult",
    "find_libmpv",
    "inject_libmpv_dir",
    "mpv_available",
    "probe_libmpv",
]
