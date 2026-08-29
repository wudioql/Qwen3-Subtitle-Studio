"""libmpv 阻塞隔离工作线程（纯 Python，零 Qt/零 mpv import）。

libmpv/python-mpv 的初始化、属性设置、命令和 terminate 都可能进入原生阻塞。
本模块把这些操作串行放入 daemon 线程；GUI 线程只做非阻塞入队。每条操作有
watchdog，超时后停止接收命令并通知 UI 回退 Qt 后端，但绝不在 UI 线程 join。

注意：线程隔离保证界面不会因同步 libmpv 调用而完全失去响应；原生崩溃仍只有
进程隔离才能完全兜住，因此超时后该 backend 会被永久弃用，不尝试在主线程强拆。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ResourceFactory = Callable[[], Any]
ResourceAction = Callable[[Any], None]
ReadyCallback = Callable[[], None]
FatalCallback = Callable[[str], None]


@dataclass
class _Command:
    name: str
    action: ResourceAction
    timeout: float
    fatal_on_error: bool
    coalesce_key: str | None = None


class MpvWorker:
    """串行执行潜在阻塞的 libmpv 操作；所有公开方法均立即返回。"""

    def __init__(
        self,
        factory: ResourceFactory,
        *,
        on_ready: ReadyCallback,
        on_fatal: FatalCallback,
        on_abandon: ResourceAction | None = None,
        init_timeout: float = 10.0,
        queue_limit: int = 32,
    ) -> None:
        self._factory = factory
        self._on_ready = on_ready
        self._on_fatal = on_fatal
        self._on_abandon = on_abandon
        self._init_timeout = max(0.05, float(init_timeout))
        self._queue_limit = max(4, int(queue_limit))

        self._cv = threading.Condition()
        self._commands: deque[_Command] = deque()
        self._coalesced: dict[str, _Command] = {}
        self._resource: Any = None
        self._accepting = True
        self._failed = False
        self._closing = False
        self._fatal_sent = False
        self._inflight_token: object | None = None
        self._inflight_name = ""

        self._thread = threading.Thread(
            target=self._run,
            name="QSS-MpvCommandWorker",
            daemon=True,
        )
        self._thread.start()

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def failed(self) -> bool:
        with self._cv:
            return self._failed

    def submit(
        self,
        name: str,
        action: ResourceAction,
        *,
        timeout: float = 3.0,
        fatal_on_error: bool = True,
        coalesce_key: str | None = None,
    ) -> bool:
        """非阻塞入队；同 key 的尚未执行命令只保留最新参数。"""
        command = _Command(
            name=str(name),
            action=action,
            timeout=max(0.05, float(timeout)),
            fatal_on_error=bool(fatal_on_error),
            coalesce_key=coalesce_key,
        )
        with self._cv:
            if not self._accepting or self._failed or self._closing:
                return False
            if coalesce_key and coalesce_key in self._coalesced:
                pending = self._coalesced[coalesce_key]
                pending.name = command.name
                pending.action = command.action
                pending.timeout = command.timeout
                pending.fatal_on_error = command.fatal_on_error
                return True
            if len(self._commands) >= self._queue_limit:
                logger.warning("[mpv] 命令队列已满，丢弃 %s（GUI 不阻塞）", name)
                return False
            self._commands.append(command)
            if coalesce_key:
                self._coalesced[coalesce_key] = command
            self._cv.notify()
            return True

    def close(self, terminate: ResourceAction, *, timeout: float = 2.0) -> None:
        """丢弃待执行命令并排入 terminate；不等待、不 join。"""
        command = _Command(
            name="shutdown",
            action=terminate,
            timeout=max(0.05, float(timeout)),
            fatal_on_error=False,
        )
        with self._cv:
            if self._closing:
                return
            self._closing = True
            self._accepting = False
            self._commands.clear()
            self._coalesced.clear()
            self._commands.appendleft(command)
            self._cv.notify_all()

    def _notify_fatal(self, message: str) -> None:
        with self._cv:
            if self._fatal_sent or self._closing:
                return
            self._fatal_sent = True
            self._failed = True
            self._accepting = False
            self._commands.clear()
            self._coalesced.clear()
        try:
            self._on_fatal(message)
        except Exception:  # noqa: BLE001
            logger.exception("[mpv] fatal callback 失败")

    def _watchdog(self, token: object, name: str, timeout: float) -> None:
        with self._cv:
            if self._inflight_token is not token or self._closing:
                return
        self._notify_fatal(
            f"libmpv 操作“{name}”超过 {timeout:g}s 未返回；已停用 mpv 并回退 Qt，"
            "阻塞线程不会在界面线程等待。"
        )

    def _execute(self, command: _Command) -> bool:
        token = object()
        with self._cv:
            self._inflight_token = token
            self._inflight_name = command.name
        timer = threading.Timer(
            command.timeout,
            self._watchdog,
            args=(token, command.name, command.timeout),
        )
        timer.daemon = True
        timer.start()
        started = time.monotonic()
        try:
            command.action(self._resource)
            return True
        except Exception as exc:  # noqa: BLE001
            message = f"libmpv 操作“{command.name}”失败：{type(exc).__name__}: {exc}"
            if command.fatal_on_error:
                self._notify_fatal(message)
            else:
                logger.warning("[mpv] %s", message)
            return False
        finally:
            timer.cancel()
            with self._cv:
                if self._inflight_token is token:
                    self._inflight_token = None
                    self._inflight_name = ""
            elapsed = time.monotonic() - started
            if elapsed > 0.25:
                logger.debug("[mpv] 操作 %s 耗时 %.3fs", command.name, elapsed)

    def _next_command(self) -> Optional[_Command]:
        with self._cv:
            while not self._commands and not self._failed:
                self._cv.wait()
            if self._failed and not self._commands:
                return None
            command = self._commands.popleft()
            if command.coalesce_key:
                self._coalesced.pop(command.coalesce_key, None)
            return command

    def _abandon_resource(self) -> None:
        resource = self._resource
        if resource is None or self._on_abandon is None:
            return
        try:
            self._on_abandon(resource)
        except Exception:  # noqa: BLE001
            logger.exception("[mpv] 放弃超时资源时出错")

    def _run(self) -> None:
        init = _Command(
            name="初始化",
            action=lambda _unused: self._create_resource(),
            timeout=self._init_timeout,
            fatal_on_error=True,
        )
        if not self._execute(init):
            self._abandon_resource()
            return
        with self._cv:
            failed = self._failed
            closing = self._closing
        if failed:
            # 初始化可能在 watchdog 触发后才返回；此实例已被 UI 判死，不再复活。
            self._abandon_resource()
            return
        if not closing:
            try:
                self._on_ready()
            except Exception:  # noqa: BLE001
                logger.exception("[mpv] ready callback 失败")

        while True:
            command = self._next_command()
            if command is None:
                self._abandon_resource()
                return
            self._execute(command)
            if command.name == "shutdown":
                return
            with self._cv:
                if self._failed:
                    self._abandon_resource()
                    return

    def _create_resource(self) -> None:
        self._resource = self._factory()


__all__ = ["MpvWorker"]
