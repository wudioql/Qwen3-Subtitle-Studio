"""Qwen3 Subtitle Studio 入口

运行：
    .venv\\Scripts\\activate   (Windows) 或 source .venv/bin/activate (macOS/Linux)
    python main.py

职责：
- 把项目根目录塞进 sys.path（防御式：允许从任意目录启动）
- 全局异常 hook：未捕获异常不会让 Qt 直接 abort，写进 .temp/app.log 再弹框
- 单实例（单进程）检查：PySide6 QLockFile 简单保护
- PySide6-Fluent-Widgets 主题初始化（深/浅色）
- 创建 QApplication → 打开 MainWindow → exec()
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path


# ── 路径修正（defensive） ──────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 日志：先只挂控制台；FileHandler 待启动清理（含 app.log 轮转）之后再挂——
# 轮转以 "wb" 重写 app.log，若文件已被 FileHandler 占用，Windows 下会直接失败。
# 临时/日志目录：与 core.constants.TEMP_DIR 同源（QSS_TEMP_DIR 可重定向），
# 惰性创建（parents=True），不在 import 期写盘。
_TEMP = Path(os.environ["QSS_TEMP_DIR"]) if os.environ.get("QSS_TEMP_DIR") else PROJECT_ROOT / ".temp"
_TEMP.mkdir(parents=True, exist_ok=True)
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")


def _attach_file_logging() -> None:
    """把 app.log FileHandler 挂到根 logger（在 startup_cleanup 轮转之后调用）。"""
    try:
        handler = logging.FileHandler(_TEMP / "app.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logging.getLogger().addHandler(handler)
    except Exception:  # noqa: BLE001
        logger.warning("app.log FileHandler 挂载失败，仅控制台日志")


# ── 单实例（QLockFile） ───────────────────────────────────────
def _ensure_single_instance(app_id: str) -> bool:
    """若已有本应用实例在跑，返回 False；否则占锁并返回 True。

    QLockFile 进程退出时系统自动释放，不用手动 release。
    """
    try:
        from PySide6.QtCore import QLockFile
    except Exception:  # noqa: BLE001
        return True   # 环境缺 Qt，放过（开发阶段调试 import 时用）

    lock_dir = Path(os.environ.get("LOCALAPPDATA") or _TEMP) / "Qwen3SubtitleStudio"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = QLockFile(str(lock_dir / f"{app_id}.lock"))
    if not lock_file.tryLock(200):
        logger.warning("已有实例在运行（lock 占用），退出。")
        return False
    # 把它挂到 globals 上避免被回收
    globals()["__qss_lock_file"] = lock_file  # type: ignore[attr-defined]
    return True


# ── 全局异常 hook ─────────────────────────────────────────────
def _excepthook(exc_type, exc_value, exc_tb):
    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical("未捕获异常:\n%s", tb)
    try:
        from PySide6.QtWidgets import QMessageBox, QApplication
        app = QApplication.instance()
        if app is not None:
            active = app.activeWindow()
            QMessageBox.critical(active, "严重错误",
                                 f"{exc_type.__name__}: {exc_value}\n\n详细日志见 .temp/app.log")
    except Exception:  # noqa: BLE001
        pass  # Qt 未初始化时无法弹框，只能静默
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _excepthook


def main() -> int:
    if not _ensure_single_instance("Qwen3SubtitleStudio-v1"):
        # GUI 双击运行无控制台，弹窗提示比阻塞 stdin 更可靠；Qt 不可用时仅日志
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.information(None, "Qwen3 Subtitle Studio",
                                    "已有一个实例正在运行，本次启动退出。")
            del app
        except Exception:  # noqa: BLE001
            pass
        return 1

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # Windows QtMultimedia/FFmpeg：在创建 QApplication / 加载后端之前禁用硬解设备，
    # 否则 HEVC 会先尝试 d3d11 并刷 “Failed setup for format d3d11”（与是否安装
    # 微软 HEVC 扩展无关）。空列表 = 不使用任何 HW device。
    os.environ["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"] = ""
    os.environ.setdefault("QT_FFMPEG_HW_DECODING", "0")

    # HiDPI / native child 隔离：必须在 QApplication() 之前设置。
    # libmpv 画布需要独立 HWND，但不能因此把 ComboBox 等兄弟控件一并原生化；
    # 否则其 Popup transient parent 不是 top-level，Windows 会持续输出
    # “ComboBoxClassWindow must be a top level window”。
    try:
        QApplication.setAttribute(
            Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings, True,
        )
    except Exception:  # noqa: BLE001
        pass  # 旧版 Qt 不支持此属性时无害降级
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:  # noqa: BLE001
        pass  # 旧版 Qt 不支持此 API 时无害降级

    app = QApplication(sys.argv)
    app.setApplicationName("Qwen3 Subtitle Studio")
    app.setOrganizationName("Qwen3SubtitleStudio")
    app.setStyle("Fusion")

    # 启动时清理残留临时文件 + 日志轮转（必须先于 FileHandler 挂载，见文件头说明）
    try:
        from core.temp_cleanup import startup_cleanup
        startup_cleanup()
    except Exception:
        pass  # 清理失败不影响启动
    _attach_file_logging()

    # 深浅色主题：由 PySide6-Fluent-Widgets 原生绘制。
    try:
        from ui.themes import apply_theme, load_theme
    except ModuleNotFoundError as exc:
        if exc.name == "qfluentwidgets":
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(
                None,
                "缺少 Fluent UI 依赖",
                "请在项目虚拟环境运行：\n\n"
                "python -m pip install -r requirements.txt\n\n"
                "本项目使用 PySide6，请安装 PySide6-Fluent-Widgets，"
                "不要混用 PyQt5 绑定版。",
            )
            return 2
        raise
    apply_theme(app, load_theme() == "dark")

    # 应用图标（Windows 任务栏/标题栏/对话框统一取此图标）。
    # assets/icon.ico 缺失时 QIcon 仅得到空图标，启动不受影响。
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(str(PROJECT_ROOT / "assets" / "icon.ico")))

    from ui.main_window import MainWindow
    w = MainWindow()
    w.show()

    # 退出清理由 MainWindow.closeEvent 统一负责（shutdown_cleanup），此处不再重复注册

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
