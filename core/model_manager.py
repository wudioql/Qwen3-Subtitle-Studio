"""core.model_manager — 模型生命周期管理（原生 transformers API + ONNXRuntime）

策略：懒加载 + 随用随激活；**按后端区分任务结束行为**：

- **Qwen ASR / Qwen Aligner（torch）**  
  ``[not_loaded] → [in_vram] --park--> [in_ram] --to(cuda)--> [in_vram]``  
  park = 权重搬到系统 RAM，再激活约 1–2s。

- **MMS-FA（ONNX Runtime）**  
  ``[not_loaded] → [in_vram/Session] --任务结束--> [not_loaded]``  
  ORT **没有** torch 式 RAM 驻留：必须 ``MMSAligner.unload()`` 销毁  
  InferenceSession 才能还 CUDA 显存；``torch.cuda.empty_cache`` 无效。  
  建 CUDA EP 前经 ``core.ort_cuda`` 预热 torch 自带 cuDNN DLL。

对外对象：ASR（AutoProcessor + AutoModelForMultimodalLM）、  
Qwen Aligner（AutoProcessor + AutoModelForTokenClassification）、  
MMS 经 ``get_mms_aligner()`` 全局单例（Session 生命周期由本模块 context/park 约束）。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from .constants import (
    DEFAULT_ATTN_IMPL,
    DEFAULT_DTYPE_STR,
)

logger = logging.getLogger(__name__)


ModelState = Literal["not_loaded", "loading", "activating", "in_ram", "in_vram"]
ModelProgressCallback = Callable[[int, int, str], None]


def _model_progress(callback: ModelProgressCallback | None, description: str) -> None:
    if callback is not None:
        callback(0, 0, description)


@dataclass
class ModelStatus:
    """对外暴露的模型状态快照（用于 UI 状态栏显示）。"""
    asr_state: ModelState
    aligner_state: ModelState
    gpu_name: str = ""
    vram_total_gb: float = 0.0
    vram_used_gb: float = 0.0


class ModelManager:
    """单例风格的模型管理器（全局共享一个实例即可）。"""

    def __init__(
        self,
        asr_path: Path | str | None = None,
        aligner_path: Path | str | None = None,
        dtype: Any | None = None,
        attn_implementation: str = DEFAULT_ATTN_IMPL,
        device: str = "cuda",
    ) -> None:
        # None = 走用户偏好（设置页「路径」覆盖），偏好为空再回退 constants 默认值。
        # 旧实现的默认参数直接写死 constants，resolved_paths() 没有任何调用方，路径页整页无效。
        if asr_path is None or aligner_path is None:
            from .app_config import load_preferences, resolved_paths
            _rp = resolved_paths(load_preferences().paths)
            if asr_path is None:
                asr_path = _rp["asr_model_path"]
            if aligner_path is None:
                aligner_path = _rp["aligner_model_path"]
        self.asr_path: Path = Path(asr_path)
        self.aligner_path: Path = Path(aligner_path)

        # dtype/device 懒解析：构造时不强制 import torch（状态机/UI 单测可无 torch 跑）
        self._dtype_arg = dtype
        self._device_pref = device
        self._dtype_resolved: Any = None
        self._device_resolved: str | None = None
        self.attn_implementation = attn_implementation

        # 底层对象（运行时类型来自 transformers，注解仅 TYPE_CHECKING）
        self.asr_processor: Any = None
        self.asr_model: Any = None
        self.aligner_processor: Any = None
        self.aligner_model: Any = None

        # 状态独立解耦，彻底避免 MMS 状态影响 Qwen3 或反之
        self.asr_state: ModelState = "not_loaded"
        self.qwen_aligner_state: ModelState = "not_loaded"
        self.mms_aligner_state: ModelState = "not_loaded"
        self.active_aligner: str = "qwen"
        # using_mms_aligner 嵌套深度：>0 表示外层已持有 Session，内层 align_sentence 勿再包一层
        self._mms_ctx_depth: int = 0

    def _ensure_torch_runtime(self) -> Any:
        """首次需要 dtype/device 时再 import torch。"""
        import torch
        if self._dtype_resolved is None:
            if self._dtype_arg is None:
                self._dtype_resolved = (
                    torch.bfloat16 if DEFAULT_DTYPE_STR == "bfloat16"
                    else torch.float16 if DEFAULT_DTYPE_STR == "float16"
                    else torch.float32
                )
            else:
                self._dtype_resolved = self._dtype_arg
        if self._device_resolved is None:
            pref = self._device_pref or "cuda"
            self._device_resolved = pref if torch.cuda.is_available() else "cpu"
        return torch

    @property
    def dtype(self) -> Any:
        self._ensure_torch_runtime()
        return self._dtype_resolved

    @dtype.setter
    def dtype(self, value: Any) -> None:
        self._dtype_resolved = value

    @property
    def device(self) -> str:
        # 状态机测试可不装 torch：默认当 cpu
        try:
            self._ensure_torch_runtime()
            return self._device_resolved or "cpu"
        except ModuleNotFoundError:
            return "cpu"

    @device.setter
    def device(self, value: str) -> None:
        self._device_resolved = value

    @property
    def aligner_state(self) -> ModelState:
        return self.mms_aligner_state if self.active_aligner == "mms" else self.qwen_aligner_state

    @aligner_state.setter
    def aligner_state(self, val: ModelState) -> None:
        if self.active_aligner == "mms":
            self.mms_aligner_state = val
        else:
            self.qwen_aligner_state = val

    # ------------------------------------------------------------------
    def status_text(self) -> str:
        """返回统一标准格式的模型状态文本，供 UI 状态栏实时呈现。"""
        _STATE_LABEL = {
            "not_loaded": "未加载",
            "loading": "加载中",
            "activating": "正在激活",
            "in_ram": "驻留RAM",
            "in_vram": "已激活",
        }
        asr_t = _STATE_LABEL.get(self.asr_state, self.asr_state)
        cur_align_state = self.aligner_state
        align_t = _STATE_LABEL.get(cur_align_state, cur_align_state)

        if self.active_aligner == "mms":
            align_name = "MMS-FA"
            # 显示 MMS Session 实际执行设备（CPU 回退不再被误标为「已激活」而无差别）
            engine_str = "engine=ONNX"
            try:
                from .mms_aligner import get_mms_aligner
                dev = (get_mms_aligner().session_device or "").strip()
                if dev:
                    engine_str = f"engine=ONNX({dev})"
            except Exception:  # noqa: BLE001
                pass
        else:
            align_name = "Qwen3"
            engine_str = "attn=FA2" if self.is_flash_attn_active else "attn=SDPA"

        if self.asr_state == "not_loaded" and align_t == "未加载":
            return f"模型：ASR 未加载 ｜ 对齐器({align_name}) 未加载"

        if align_t == "未加载":
            return f"模型：ASR {asr_t} ｜ 对齐器({align_name}) 未加载 ｜ {engine_str}"

        return f"模型：ASR {asr_t} ｜ 对齐器({align_name}) {align_t} ｜ {engine_str}"

    @property
    def status(self) -> ModelStatus:
        gpu_name = ""
        vram_total = 0.0
        vram_used = 0.0
        try:
            import torch
            if self.device == "cuda" and torch.cuda.is_available():
                dev = torch.cuda.current_device()
                prop = torch.cuda.get_device_properties(dev)
                gpu_name = prop.name
                vram_total = prop.total_memory / (1024 ** 3)
                vram_used = torch.cuda.memory_allocated(dev) / (1024 ** 3)
        except Exception:
            logger.debug("[ModelManager] GPU 属性读取失败")
        return ModelStatus(
            asr_state=self.asr_state,
            aligner_state=self.aligner_state,
            gpu_name=gpu_name,
            vram_total_gb=round(vram_total, 2),
            vram_used_gb=round(vram_used, 2),
        )

    def _empty_cache(self) -> None:
        try:
            import torch
            if self.device == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            logger.debug("[ModelManager] empty_cache 跳过（无 torch/CUDA）")

    # ------------------------------------------------------------------
    # ASR
    # ------------------------------------------------------------------
    def get_asr(self, progress_cb: ModelProgressCallback | None = None) -> tuple[Any, Any]:
        """获取 (processor, model)，并报告首次加载/RAM 唤醒阶段。"""
        if self.asr_model is not None and self.asr_processor is not None:
            if self.asr_state != "in_vram":
                self.asr_state = "activating"
                _model_progress(progress_cb, "ASR 模型：正在从 RAM 移动到计算设备…")
                logger.info("[ModelManager]: ASR RAM → VRAM（快速恢复）...")
                try:
                    self.asr_model.to(self.device)
                except Exception:
                    self.asr_state = "in_ram"
                    raise
                self.asr_state = "in_vram"
                self._empty_cache()
                _model_progress(progress_cb, "ASR 模型：激活完成")
                logger.info("[ModelManager]: ASR 已激活")
            return self.asr_processor, self.asr_model

        # 首次加载
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        logger.info(f"[ModelManager]: 首次加载 ASR（{self.asr_path}）...")
        self.asr_state = "loading"
        try:
            _model_progress(progress_cb, "ASR 模型：检查本地权重…")
            _check_model_path(self.asr_path, "ASR")
            _model_progress(progress_cb, "ASR 模型：加载 Processor…")
            self.asr_processor = AutoProcessor.from_pretrained(
                str(self.asr_path), local_files_only=True,
            )
            _model_progress(progress_cb, "ASR 模型：读取权重（首次可能较慢）…")
            self.asr_model = _try_load_with_attn_fallback(
                AutoModelForMultimodalLM,
                str(self.asr_path),
                dtype=self.dtype,
                device=self.device,
                attn_implementation=self.attn_implementation,
                progress_cb=progress_cb,
                model_label="ASR",
            )
        except Exception:
            self.asr_state = "not_loaded"
            self.asr_model = None
            raise
        self.asr_state = "in_vram"
        self._empty_cache()
        _model_progress(progress_cb, "ASR 模型：加载完成")
        logger.info("[ModelManager]: ASR 加载完成")
        return self.asr_processor, self.asr_model

    def park_asr(self) -> None:
        if self.asr_model is not None:
            logger.info("[ModelManager]: ASR VRAM → RAM 驻留")
            self.asr_model.to("cpu")
            self._empty_cache()
        self.asr_state = "in_ram"

    def unload_asr(self) -> None:
        if self.asr_model is not None:
            logger.info("[ModelManager]: 完全卸载 ASR")
            del self.asr_model
            self.asr_model = None
            self._empty_cache()
        self.asr_processor = None
        self.asr_state = "not_loaded"

    # ------------------------------------------------------------------
    # Aligner
    # ------------------------------------------------------------------
    def get_aligner(self, progress_cb: ModelProgressCallback | None = None) -> tuple[Any, Any]:
        """获取 Qwen Aligner，并报告首次加载/RAM 唤醒阶段。"""
        if self.aligner_model is not None and self.aligner_processor is not None:
            if self.qwen_aligner_state != "in_vram":
                self.qwen_aligner_state = "activating"
                _model_progress(progress_cb, "Qwen 对齐器：正在从 RAM 移动到计算设备…")
                logger.info("[ModelManager]: Qwen3 Aligner RAM → VRAM（快速恢复）...")
                try:
                    self.aligner_model.to(self.device)
                except Exception:
                    self.qwen_aligner_state = "in_ram"
                    raise
                self.qwen_aligner_state = "in_vram"
                self._empty_cache()
                _model_progress(progress_cb, "Qwen 对齐器：激活完成")
                logger.info("[ModelManager]: Qwen3 Aligner 已激活")
            return self.aligner_processor, self.aligner_model

        from transformers import AutoModelForTokenClassification, AutoProcessor

        logger.info(f"[ModelManager]: 首次加载 Qwen3 Aligner（{self.aligner_path}）...")
        self.qwen_aligner_state = "loading"
        try:
            _model_progress(progress_cb, "Qwen 对齐器：检查本地权重…")
            _check_model_path(self.aligner_path, "Aligner")
            _model_progress(progress_cb, "Qwen 对齐器：加载 Processor…")
            self.aligner_processor = AutoProcessor.from_pretrained(
                str(self.aligner_path), local_files_only=True,
            )
            _model_progress(progress_cb, "Qwen 对齐器：读取权重（首次可能较慢）…")
            self.aligner_model = _try_load_with_attn_fallback(
                AutoModelForTokenClassification,
                str(self.aligner_path),
                dtype=self.dtype,
                device=self.device,
                attn_implementation=self.attn_implementation,
                progress_cb=progress_cb,
                model_label="Qwen 对齐器",
            )
        except Exception:
            self.qwen_aligner_state = "not_loaded"
            self.aligner_model = None
            raise
        self.qwen_aligner_state = "in_vram"
        self._empty_cache()
        _model_progress(progress_cb, "Qwen 对齐器：加载完成")
        logger.info("[ModelManager]: Qwen3 Aligner 加载完成")
        return self.aligner_processor, self.aligner_model

    def _release_mms_session(self) -> None:
        """销毁全局 MMS ONNX Session（ORT 无 torch 式 RAM 驻留，只能整会话卸）。"""
        try:
            from .mms_aligner import get_mms_aligner
            get_mms_aligner().unload()
        except Exception:
            logger.debug("[ModelManager] 释放 MMS ONNX 会话失败", exc_info=True)
        self.mms_aligner_state = "not_loaded"

    def park_aligner(self) -> None:
        """释放当前激活对齐后端占用的加速器内存。

        - Qwen3：``model.to('cpu')``，状态 ``in_ram``（真·RAM 驻留，再激活快）。
        - MMS-FA（ONNX）：ORT CUDA Session **无法** 把权重「搬到 RAM 仍保活」；
          只能销毁 Session 才能还显存。状态记 ``not_loaded``（下次 align 再加载，
          ~300MB / 约 1s）。旧实现只改状态位 + ``torch.cuda.empty_cache()``，
          ORT 显存完全不会降——这是「MMS 卸不掉显存」的根因。
        """
        if self.active_aligner == "mms":
            self._release_mms_session()
            self._empty_cache()
            logger.info("[ModelManager]: MMS-FA 对齐器已销毁 ONNX 会话（显存交还）")
            return

        if self.aligner_model is not None:
            logger.info("[ModelManager]: Qwen3 Aligner VRAM → RAM 驻留")
            self.aligner_model.to("cpu")
            self._empty_cache()
        self.qwen_aligner_state = "in_ram"

    def unload_aligner(self) -> None:
        if self.aligner_model is not None:
            logger.info("[ModelManager]: 完全卸载 Qwen3 Aligner")
            del self.aligner_model
            self.aligner_model = None
            self._empty_cache()
        self.aligner_processor = None
        self.qwen_aligner_state = "not_loaded"
        # 无论当前 active 是谁，MMS 会话都卸掉（可能上次任务留下）
        self._release_mms_session()

    # ------------------------------------------------------------------
    # 全局
    # ------------------------------------------------------------------
    def park_all(self) -> None:
        """ASR/Qwen 真 RAM 驻留；MMS 始终销毁 Session（与 active 无关）。"""
        self.park_asr()
        # Qwen park（不依赖 active_aligner 分支）
        if self.aligner_model is not None:
            try:
                self.aligner_model.to("cpu")
                self._empty_cache()
            except Exception:
                logger.debug("[ModelManager] park_all: Qwen to cpu 失败", exc_info=True)
            self.qwen_aligner_state = "in_ram"
        # MMS：只要曾经加载过就必须卸 Session（ORT 不跟 torch empty_cache）
        self._release_mms_session()
        self._empty_cache()

    def unload_all(self) -> None:
        self.unload_asr()
        self.unload_aligner()
        try:
            from .vocal_separator import get_vocal_separator
            get_vocal_separator().unload()
        except Exception:
            pass
        # unload_aligner 已 release MMS；再调一次幂等
        self._release_mms_session()
        self.asr_state = "not_loaded"
        self.qwen_aligner_state = "not_loaded"
        self.mms_aligner_state = "not_loaded"

    def cleanup(self) -> None:
        self.unload_all()

    # ------------------------------------------------------------------
    # 运行时 attn 实现检查
    # ------------------------------------------------------------------
    def effective_attn_implementation(self) -> str:
        if self.aligner_model is not None:
            impl = getattr(self.aligner_model.config, '_attn_implementation', None)
            if impl:
                return str(impl)
        if self.asr_model is not None:
            impl = getattr(self.asr_model.config, '_attn_implementation', None)
            if impl:
                return str(impl)
        from .constants import DEFAULT_ATTN_IMPL
        return DEFAULT_ATTN_IMPL

    @property
    def is_flash_attn_active(self) -> bool:
        return self.effective_attn_implementation() == "flash_attention_2"

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------
    def using_asr(self, progress_cb: ModelProgressCallback | None = None):
        return _ASRContext(self, progress_cb)

    def using_aligner(self, progress_cb: ModelProgressCallback | None = None):
        return _AlignerContext(self, progress_cb)

    def using_vocal_separator(self):
        return _VocalSeparatorContext(self)

    def using_mms_aligner(self, progress_cb: ModelProgressCallback | None = None):
        return _MMSAlignerContext(self, progress_cb)


class _VocalSeparatorContext:
    def __init__(self, m: ModelManager) -> None:
        self._m = m

    def __enter__(self):
        from .vocal_separator import get_vocal_separator
        return get_vocal_separator()

    def __exit__(self, *exc) -> None:
        from .vocal_separator import get_vocal_separator
        get_vocal_separator().unload()
        self._m._empty_cache()


class _MMSAlignerContext:
    def __init__(self, m: ModelManager, progress_cb: ModelProgressCallback | None = None) -> None:
        self._m = m
        self._progress_cb = progress_cb

    def _on_progress(self, done: int, total: int, description: str) -> None:
        if "加载完成" in description or done > 0:
            self._m.mms_aligner_state = "in_vram"
        if self._progress_cb is not None:
            self._progress_cb(done, total, description)

    def __enter__(self):
        from .mms_aligner import get_mms_aligner
        # active_aligner = 用户在工具栏选择的后端身份（状态栏显示/路径判断用）。
        # 执行期临时切到 mms，退出时必须恢复进入前的值——否则任何一次执行的
        # 后端会把用户的工具栏选择顶掉，状态栏从此显示错后端。
        self._prev_active = self._m.active_aligner
        self._m._mms_ctx_depth = int(getattr(self._m, "_mms_ctx_depth", 0)) + 1
        self._m.mms_aligner_state = "loading"
        self._m.active_aligner = "mms"
        aligner = get_mms_aligner()
        if hasattr(aligner, "set_progress_callback"):
            aligner.set_progress_callback(self._on_progress)
        _model_progress(self._progress_cb, "MMS 对齐器：准备 ONNX Session…")
        return aligner

    def __exit__(self, *exc) -> None:
        # 嵌套退出：仅最外层销毁 ORT Session（torch.cuda.empty_cache 清不掉 ORT CUDA）。
        depth = int(getattr(self._m, "_mms_ctx_depth", 1)) - 1
        self._m._mms_ctx_depth = max(0, depth)
        if self._m._mms_ctx_depth == 0:
            try:
                from .mms_aligner import get_mms_aligner
                aligner = get_mms_aligner()
                if hasattr(aligner, "set_progress_callback"):
                    aligner.set_progress_callback(None)
                self._m._release_mms_session()
                self._m._empty_cache()
                logger.info("[ModelManager]: MMS-FA 任务结束，ONNX 会话已销毁（显存交还）")
            except Exception:
                logger.debug("[ModelManager] using_mms_aligner 退出时释放失败", exc_info=True)
                self._m.mms_aligner_state = "not_loaded"
        self._m.active_aligner = self._prev_active


# ─────────────────────────────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────────────────────────────

def _check_model_path(path: Path, model_label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(
            f"找不到{model_label}模型目录：{path}\n"
            f"请先下载模型放到 {path}（或到设置面板指定模型路径）。\n"
            f"ASR 模型：Qwen/Qwen3-ASR-1.7B-hf；对齐器：Qwen/Qwen3-ForcedAligner-0.6B-hf"
        )


def _is_flash_attn_error(exc: BaseException) -> bool:
    """是否可归因于 Flash Attention 不可用的加载错误（唯一允许回退 SDPA 的情形）。

    其它异常（权重损坏、权限、磁盘、dtype、OOM 等）必须原样抛出——旧实现把
    它们一律回退 SDPA 重载，掩盖真实故障并可能叠加峰值内存。
    """
    if isinstance(exc, ImportError):
        return True   # transformers 内部 attn 后端 import 失败最典型（FA2 缺算子等）
    msg = str(exc).lower()
    return any(
        key in msg
        for key in ("flash_attn", "flash attention", "flashattention", "attn_implementation")
    )


def _try_load_with_attn_fallback(
    ctor_cls,
    path,
    *,
    dtype,
    device,
    attn_implementation,
    progress_cb: ModelProgressCallback | None = None,
    model_label: str = "模型",
):
    # FA2 预检：flash_attn 未安装时直接走 SDPA，避免「先按 FA2 加载失败、再按
    # SDPA 重载」导致离线环境读两遍权重、或误触发 HF Hub 连接超时。
    effective = attn_implementation
    if effective == "flash_attention_2":
        try:
            if importlib.util.find_spec("flash_attn") is None:
                logger.info(
                    "[ModelManager]: flash_attn 未安装，%s 直接使用 SDPA（跳过 FA2 尝试）",
                    model_label,
                )
                effective = "sdpa"
        except Exception:  # noqa: BLE001 — find_spec 异常按「未安装」处理，由 SDPA 路径兜底
            effective = "sdpa"
    model = None
    try:
        model = ctor_cls.from_pretrained(
            path,
            dtype=dtype,
            attn_implementation=effective,
            low_cpu_mem_usage=True,
            local_files_only=True,
        )
        if device != "cpu":
            _model_progress(progress_cb, f"{model_label}：移动到 {device}…")
            model.to(device)
        actual = getattr(model.config, '_attn_implementation', None) or effective
        if actual != effective:
            logger.warning(
                "[ModelManager]: 请求 attn=%s 但实际得到 attn=%s（模型自行调整）",
                effective, actual,
            )
        else:
            logger.info("[ModelManager]: attn_implementation=%s ✓", effective)
        return model
    except Exception as e:  # noqa: BLE001
        if effective == "sdpa" or not _is_flash_attn_error(e):
            # 已经是 SDPA，或错误与 FA2 无关 → 原样抛出，不掩盖真实故障
            raise
        logger.warning(
            "[ModelManager]: %s 加载失败（%s），回退 sdpa…",
            effective, e,
        )
        _model_progress(progress_cb, f"{model_label}：Flash Attention 不可用，回退 SDPA…")
        # 显式释放可能已部分构造的模型，再以 SDPA 重载，避免叠加峰值内存
        if model is not None:
            try:
                del model
            except Exception:  # noqa: BLE001
                pass
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
        model = ctor_cls.from_pretrained(
            path,
            dtype=dtype,
            attn_implementation="sdpa",
            low_cpu_mem_usage=True,
            local_files_only=True,
        )
        if device != "cpu":
            _model_progress(progress_cb, f"{model_label}：以 SDPA 移动到 {device}…")
            model.to(device)
        logger.info("[ModelManager]: 回退 attn_implementation=sdpa ✓")
        return model


class _ASRContext:
    def __init__(self, m: ModelManager, progress_cb: ModelProgressCallback | None = None) -> None:
        self._m = m
        self._progress_cb = progress_cb

    def __enter__(self):
        return self._m.get_asr(progress_cb=self._progress_cb)

    def __exit__(self, *exc) -> None:
        self._m.park_asr()


class _AlignerContext:
    def __init__(self, m: ModelManager, progress_cb: ModelProgressCallback | None = None) -> None:
        self._m = m
        self._progress_cb = progress_cb

    def __enter__(self):
        # 同 _MMSAlignerContext——执行期切换、退出恢复用户所选身份
        self._prev_active = self._m.active_aligner
        self._m.active_aligner = "qwen"
        return self._m.get_aligner(progress_cb=self._progress_cb)

    def __exit__(self, *exc) -> None:
        self._m.park_aligner()          # park 按 active_aligner 选后端，须在恢复之前执行
        self._m.active_aligner = self._prev_active
