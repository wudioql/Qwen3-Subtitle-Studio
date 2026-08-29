"""core.mms_aligner.engine — MMSAligner 类与全局单例。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

import numpy as np
import librosa

from subs.models import WordTimestamp
from core.constants import MMS_TAIL_EXTEND_MAX
from core.text_utils import extract_pure_words

from .constants import (
    _CHUNK_CONTEXT_SEC,
    _CHUNK_WINDOW_SEC,
    _DEFAULT_MMS_VOCAB,
    _SAMPLE_RATE,
    _WAV2VEC2_STRIDE_SAMPLES,
    _WAV2VEC2_STRIDE_SEC,
)
from .ctc import (
    _compute_vocal_features,
    _ctc_forced_align_canonical,
    _find_next_content_onset,
    _find_singing_vocal_end_frame,
)
from .romanize import (
    _DIGIT_RUN_RE,
    _DIGIT_SPELLINGS,
    _FULLWIDTH_DIGIT_TRANS,
    _contains_kanji,
)

logger = logging.getLogger("core.mms_aligner")

class MMSAligner:
    """MMS-300M-FA 多语言歌词强制对齐器 (ONNX 版)。"""

    def __init__(
        self,
        model_dir: Path | str | None = None,
        device: str = "cuda",
    ) -> None:
        # None = 走用户偏好（设置页「路径」覆盖），偏好为空回退 constants 默认
        if model_dir is None:
            from core.app_config import load_preferences, resolved_paths
            model_dir = resolved_paths(load_preferences().paths)["mms_aligner_model_path"]
        self.model_dir = Path(model_dir)
        self.device = device
        self._session = None
        self._available: Optional[bool] = None   # is_available() 结果缓存
        self._vocab: dict[str, int] = {}
        self._uroman = None
        # 本次会话是否强制 CPU（运行期 CUDA 回退用）；None = 按 self.device 偏好。
        # unload() 时清除，下次任务重新按偏好尝试 CUDA——不再永久改 self.device。
        self._session_want_cuda: Optional[bool] = None
        # 当前 Session 实际执行设备（"GPU (CUDA)" / "GPU (DirectML)" / "CPU"），供状态栏显示。
        self.session_device: str = ""
        self._kakasi = None  # None=未尝试；False=pykakasi 缺失/加载失败（短路重试）；实例=可用
        self._progress_cb: Optional[Callable[[int, int, str], None]] = None

    def set_progress_callback(
        self,
        callback: Optional[Callable[[int, int, str], None]],
    ) -> None:
        self._progress_cb = callback

    def _progress(self, done: int, total: int, description: str) -> None:
        if self._progress_cb is not None:
            self._progress_cb(done, total, description)

    def is_available(self) -> bool:
        """检查模型目录及是否存在任意 .onnx 权重文件（结果进程内缓存）。

        模型目录在运行中基本不变；如需在运行中放置模型后重新探测，
        可重置 ``_available = None``。
        """
        if self._available is None:
            if not self.model_dir.exists() or not self.model_dir.is_dir():
                self._available = False
            else:
                self._available = bool(
                    list(self.model_dir.glob("*.onnx"))
                    or list(self.model_dir.glob("onnx/*.onnx"))
                )
        return self._available

    def _find_onnx_file(self) -> Path:
        """按优先级探测 ONNX 权重文件 (model_fp16 -> model_int8 -> model.onnx -> 任意 *.onnx)。"""
        candidates = [
            self.model_dir / "onnx" / "model_fp16.onnx",
            self.model_dir / "model_fp16.onnx",
            self.model_dir / "onnx" / "model_int8.onnx",
            self.model_dir / "model_int8.onnx",
            self.model_dir / "onnx" / "model_quantized.onnx",
            self.model_dir / "model_quantized.onnx",
            self.model_dir / "onnx" / "model.onnx",
            self.model_dir / "model.onnx",
        ]
        for c in candidates:
            if c.exists() and c.is_file():
                return c
        all_onnx = list(self.model_dir.glob("**/*.onnx"))
        if all_onnx:
            return all_onnx[0]
        raise FileNotFoundError(f"在 {self.model_dir} 中未找到任何 .onnx 模型文件")

    def _load_vocab(self) -> dict[str, int]:
        if self._vocab:
            return self._vocab
        vocab_path = self.model_dir / "vocab.json"
        if vocab_path.exists():
            try:
                raw_vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
                self._vocab = {str(k).lower(): int(v) for k, v in raw_vocab.items()}
                return self._vocab
            except Exception as e:
                logger.debug("[MMSAligner] 读取 vocab.json 失败: %s，使用默认词表", e)
        self._vocab = dict(_DEFAULT_MMS_VOCAB)
        return self._vocab

    def _get_uroman(self):
        if self._uroman is None:
            try:
                import uroman
                self._uroman = uroman.Uroman()
            except Exception as e:
                logger.warning(
                    "[MMSAligner] 导入 uroman 失败（%s），后续词将回退基础转写；"
                    "pip install uroman 可提升罗马化质量", e,
                )
                self._uroman = False   # 失败哨兵：不再逐词重复 import
        return self._uroman or None

    def _get_session(self):
        if self._session is not None:
            return self._session

        onnx_file = self._find_onnx_file()
        try:
            import onnxruntime as ort  # noqa: F401  # 存在性检查
        except ImportError as e:
            raise ImportError(
                "请安装 onnxruntime；目标机 GPU 加速请按部署清单用 onnxruntime-gpu 覆盖"
            ) from e

        from core.ort_cuda import create_inference_session

        # 本次会话优先用 _session_want_cuda（运行期 CUDA 回退后强制 CPU），
        # 否则按用户 device 偏好；self.device 永不被运行期回退改动。
        want_cuda = (
            self._session_want_cuda
            if self._session_want_cuda is not None
            else (self.device == "cuda")
        )
        self._progress(0, 0, "MMS 对齐器：创建 ONNX Session（首次可能较慢）…")
        logger.info(
            "[MMSAligner] 正在加载 MMS-300M-FA 模型: %s (请求: %s)",
            onnx_file.name, "CUDA" if want_cuda else "CPU",
        )
        # 创建前会 prime torch DLL（cudnn64_9.dll）；CUDA/cuDNN 失败则自动 CPU
        self._session, actual_device = create_inference_session(
            onnx_file, want_cuda=want_cuda,
        )
        self.session_device = actual_device
        logger.info("[MMSAligner] MMS-300M-FA 模型加载完成 (执行设备: %s)", actual_device)
        self._progress(0, 0, f"MMS 对齐器：加载完成（{actual_device}）")
        self._load_vocab()
        return self._session

    def _run_session(self, feed_dict: dict):
        """session.run；运行期若缺 cuDNN 则销毁并 CPU 重建后重试一次。"""
        from core.ort_cuda import run_with_cuda_fallback

        session = self._get_session()

        def _recreate_cpu():
            self.unload()
            self._session_want_cuda = False   # 本次会话强制 CPU；不改用户 device 偏好
            return self._get_session()

        outputs, session = run_with_cuda_fallback(
            session, None, feed_dict, recreate_cpu_session=_recreate_cpu,
        )
        self._session = session
        return outputs

    def _generate_emissions_chunked(self, audio_np: np.ndarray) -> np.ndarray:
        """分块滑动窗口生成 CTC Emissions，防止长音频 (3分钟+) 触发 10GB+ 显存占用。"""
        session = self._get_session()
        inputs = session.get_inputs()
        input_name = inputs[0].name
        has_mask = len(inputs) > 1 and "mask" in inputs[1].name.lower()
        mask_name = inputs[1].name if has_mask else None

        total_samples = audio_np.size
        window_samples = int(_CHUNK_WINDOW_SEC * _SAMPLE_RATE)
        context_samples = int(_CHUNK_CONTEXT_SEC * _SAMPLE_RATE)
        step_samples = window_samples - 2 * context_samples
        ratio = _WAV2VEC2_STRIDE_SAMPLES

        if total_samples <= window_samples:
            self._progress(0, 1, "MMS 推理：音频块 1/1…")
            feed = np.expand_dims(audio_np, axis=0).astype(np.float32)
            feed_dict = {input_name: feed}
            if has_mask:
                feed_dict[mask_name] = np.ones_like(feed, dtype=np.int64)
            logits = self._run_session(feed_dict)[0]  # (1, T, 31)
            self._progress(1, 1, "MMS 推理：音频块 1/1 完成")
            return logits[0]

        emissions_list: List[np.ndarray] = []
        total_chunks = max(1, (total_samples + step_samples - 1) // step_samples)
        curr = 0
        chunk_index = 0
        while curr < total_samples:
            self._progress(
                chunk_index,
                total_chunks,
                f"MMS 推理：音频块 {chunk_index + 1}/{total_chunks}…",
            )
            c_start = max(0, curr - context_samples)
            c_end = min(total_samples, curr + step_samples + context_samples)
            chunk_audio = audio_np[c_start:c_end]

            feed = np.expand_dims(chunk_audio, axis=0).astype(np.float32)
            feed_dict = {input_name: feed}
            if has_mask:
                feed_dict[mask_name] = np.ones_like(feed, dtype=np.int64)
            chunk_logits = self._run_session(feed_dict)[0][0]  # (chunk_T, 31)

            ctx_left_frames = (curr - c_start) // ratio
            valid_frames = min(step_samples // ratio, (total_samples - curr) // ratio)

            sliced = chunk_logits[ctx_left_frames : ctx_left_frames + valid_frames]
            if sliced.shape[0] > 0:
                emissions_list.append(sliced)

            curr += step_samples
            chunk_index += 1
            self._progress(
                chunk_index,
                total_chunks,
                f"MMS 推理：已完成 {chunk_index}/{total_chunks} 块",
            )

        if not emissions_list:
            return np.zeros((0, 31), dtype=np.float32)

        return np.concatenate(emissions_list, axis=0)

    def align(
        self,
        audio: Union[str, Path, np.ndarray, Tuple[np.ndarray, int]],
        text: str,
        *,
        language: str = "auto",
        offset_sec: float = 0.0,
        tail_limit_sec: Optional[float] = None,
    ) -> List[WordTimestamp]:
        """对一段音频与文本执行标准 MMS-FA 强制对齐，返回 WordTimestamp 列表。

        tail_limit_sec：末字长拖音追踪的搜索上界（全局时间，秒）。
        单句重对齐时调用方传入稳定锚（下一句起点 / 句尾 + 固定前瞻），
        使末字上界不依赖裁剪窗长度——窗口再宽也不会「给多少吃多少」，
        重复重对齐结果收敛。None = 维持旧行为（音频末端为界，
        供整段全文对齐使用：彼时每个字的上界天然是下一字起点）。
        """
        # 1. 音频准备 (16kHz 单声道 float32)
        if isinstance(audio, (str, Path)):
            audio_np, _ = librosa.load(str(audio), sr=_SAMPLE_RATE, mono=True)
        elif isinstance(audio, tuple):
            samples, sr = audio
            samples = np.asarray(samples, dtype=np.float32)
            if sr != _SAMPLE_RATE:
                audio_np = librosa.resample(samples, orig_sr=sr, target_sr=_SAMPLE_RATE)
            else:
                audio_np = samples
        else:
            audio_np = np.asarray(audio, dtype=np.float32)

        if audio_np.ndim > 1:
            audio_np = np.mean(audio_np, axis=0)

        if audio_np.size == 0 or not text.strip():
            return []

        # 2. 文本切词（过滤标点特殊符号）与多语言罗马化
        words_raw = extract_pure_words(text)
        if not words_raw:
            return []

        vocab = self._load_vocab()
        unk_id = vocab.get("<unk>", vocab.get("<pad>", 3))

        all_targets: List[int] = []
        word_token_ranges: List[Tuple[int, int]] = []

        for w in words_raw:
            w_rom = self._romanize_word(w, language)
            w_tokens: List[int] = []
            for c in w_rom:
                if c in vocab:
                    w_tokens.append(vocab[c])
                elif c.lower() in vocab:
                    w_tokens.append(vocab[c.lower()])
                elif c.isalpha():
                    w_tokens.append(unk_id)
            if not w_tokens:
                w_tokens = [vocab.get("a", 4)]

            s_idx = len(all_targets)
            all_targets.extend(w_tokens)
            e_idx = len(all_targets)
            word_token_ranges.append((s_idx, e_idx))

        # 3. 分块推理生成 CTC Emissions（显存恒定 < 400MB）
        emissions = self._generate_emissions_chunked(audio_np)
        if emissions.shape[0] == 0:
            return []

        # 转换为 Log-Softmax 概率
        max_logits = np.max(emissions, axis=-1, keepdims=True)
        exp_logits = np.exp(emissions - max_logits)
        log_probs = emissions - max_logits - np.log(np.sum(exp_logits, axis=-1, keepdims=True) + 1e-12)
        num_frames = log_probs.shape[0]

        # 4. Alex Graves 经典 2L+1 CTC Trellis Viterbi 解码
        path = _ctc_forced_align_canonical(log_probs, all_targets, blank_id=0)

        # 5. 提取每个词的发音起始帧与元音起点，辅音完整包含 + 元音谐波平坦度追踪长拖音
        num_words = len(words_raw)
        word_start_frames: List[int] = []
        word_vowel_start_frames: List[int] = []

        prev_s = 0
        for s_tok, e_tok in word_token_ranges:
            k_min = 2 * s_tok + 1
            k_max = 2 * (e_tok - 1) + 1
            frames = np.where((path >= k_min) & (path <= k_max))[0]
            if len(frames) > 0:
                s_f = int(frames[0])
            else:
                s_f = prev_s
            word_start_frames.append(s_f)

            # 词内元音起点：取该词最后 token (元音) 在 Viterbi 中的起点；若无则默认 s_f
            last_k = 2 * (e_tok - 1) + 1
            last_tok_frames = np.where(path == last_k)[0]
            if len(last_tok_frames) > 0:
                s_vowel = int(last_tok_frames[0])
            else:
                s_vowel = s_f
            word_vowel_start_frames.append(s_vowel)

            prev_s = s_f + 2

        # 末字拖音搜索上界（三个锚取交集，与裁剪窗长度解耦 → 重对齐幂等）：
        #   ① 调用方锚 tail_limit_sec（单句路径 = 下一句起点；无则音频末端）；
        #   ② 元音起点 + MMS_TAIL_EXTEND_MAX（元音起点由 CTC Viterbi 按文本锚定，
        #      重复重对齐收敛到同一位置；防呼吸声/混响被平坦度误判成发音态时无限延伸）；
        #   ③ CTC 异质发音峰哨兵：窗口内后句人声开口的第一帧（见
        #      _find_next_content_onset）。①② 防的是「无限吃」，③ 防的是
        #      「吃到别人的音」——句间停顿极短、能量无衰减的连唱场景，
        #      能量/平坦度扫描器无间隙可测，唯有内容异质性可判。
        last_word_max_frame = num_frames
        if tail_limit_sec is not None:
            limit_f = int(round((float(tail_limit_sec) - offset_sec) / _WAV2VEC2_STRIDE_SEC))
            last_word_max_frame = max(0, min(num_frames, limit_f))
        _tail_extend_frames = int(round(MMS_TAIL_EXTEND_MAX / _WAV2VEC2_STRIDE_SEC))

        # 频谱平坦度/RMS 与词无关：整段只计算一次，逐词终点搜索共享。
        vocal_features = _compute_vocal_features(audio_np)

        out_words: List[WordTimestamp] = []
        for i in range(num_words):
            s_f = word_start_frames[i]
            s_vowel = word_vowel_start_frames[i]
            if i + 1 < num_words:
                next_s_f = word_start_frames[i + 1]
            else:
                next_s_f = min(last_word_max_frame, s_vowel + _tail_extend_frames)
                # ③ 哨兵：末字自身 token 集（重触发豁免）之外的字符峰 = 新内容开口。
                # 扫描区间与锚解耦：上界用「元音起点 + 前瞻」而非被锚截过的
                # next_s_f——哨兵找的是音频里的内容边界，与用户把锚拖到哪无关。
                # （锚若被拖进后句首字发音中段，被截的扫描区间会恰好把首字峰
                #  挡在区间外 → 哨兵失明、衰减扫描器顶到锚 → 「特定区间才复现」
                #  的吞首字。解耦后首字峰只要在窗内就必被检出。）
                last_s_tok, last_e_tok = word_token_ranges[-1]
                own_tokens = set(int(t) for t in all_targets[last_s_tok:last_e_tok])
                scan_end = min(num_frames, s_vowel + _tail_extend_frames)
                onset = _find_next_content_onset(
                    log_probs, s_vowel + 1, scan_end, own_tokens, blank_id=0,
                )
                if onset is not None:
                    next_s_f = min(next_s_f, max(s_vowel + 2, onset))

            # 包含全部前置辅音 [s_f, s_vowel]，并从元音起点 s_vowel 向后结合频谱谐波平坦度追踪长拖音
            e_f = _find_singing_vocal_end_frame(
                audio_np, s_vowel, next_s_f, features=vocal_features,
            )
            e_f = min(next_s_f, max(s_f + 2, e_f))

            raw_s = offset_sec + s_f * _WAV2VEC2_STRIDE_SEC
            raw_e = offset_sec + e_f * _WAV2VEC2_STRIDE_SEC

            s_time = round(raw_s, 3)
            e_time = round(max(raw_s + 0.030, raw_e), 3)

            out_words.append(WordTimestamp(
                text=words_raw[i],
                start_time=s_time,
                end_time=e_time,
                language=language,
            ))

        return out_words

    def align_with_context(
        self,
        audio: Union[str, Path, np.ndarray, Tuple[np.ndarray, int]],
        prev_text: str,
        text: str,
        next_text: str,
        *,
        language: str = "auto",
        offset_sec: float = 0.0,
    ) -> List[WordTimestamp]:
        """上下文强制对齐：把 [prev_text, text, next_text] 拼成一段对齐，只返回 text 的词。

        MMS 是 CTC **强制对齐**——窗内所有发音都要分配给传入文本的 tokens。单句
        孤对齐时，窗首的前句尾音、窗尾的后句开口没有对应文本，会被 CTC 塞给本句
        首/末字 → 句首/尾漂移（全文对齐则把这些发音正确分给邻句文本，故无此问题）。

        带上邻句文本后，前句尾音由 prev_text 的 tokens 吸收、后句开口由 next_text
        的 tokens 吸收，本句字词边界恢复到与全文对齐同一精度。调用方应保证窗口
        覆盖邻句音频（窗首 ≤ 前句起点、窗尾 ≥ 后句终点）。

        分词数切片：``align`` 内部按 ``extract_pure_words`` 切词，拼接用空格分隔，
        故整段词数 = prev 词数 + 本句词数 + next 词数，切片稳定。
        """
        prev_n = len(extract_pure_words(prev_text or ""))
        cur_n = len(extract_pure_words(text or ""))
        full = " ".join(p for p in (prev_text, text, next_text) if (p or "").strip())
        all_words = self.align(audio, full, language=language, offset_sec=offset_sec)
        lo = min(prev_n, len(all_words))
        hi = min(prev_n + cur_n, len(all_words))
        return all_words[lo:hi]

    def _get_kakasi(self):
        """懒加载 pykakasi 日语汉字读音转换器。

        缺失/失败 → False 哨兵短路（回退 uroman 中文读音），不打断对齐流程。
        """
        if self._kakasi is None:
            try:
                import pykakasi
                self._kakasi = pykakasi.kakasi()
            except Exception as e:
                logger.info(
                    "[MMSAligner] pykakasi 不可用（%s），日语汉字读音回退 uroman；"
                    "pip install pykakasi 可显著提升日语对齐精度", e,
                )
                self._kakasi = False
        return self._kakasi or None

    def _expand_digits(self, word: str, language: str = "") -> str:
        """把词中的数字串逐位展开为该语言的拼读词（详见 `_DIGIT_SPELLINGS`）。

        全角数字先转半角；拼读词以空格夹入，下游罗马化管线天然丢弃空白字符，
        不会破坏「一词一段 token 区间」的约定。未知语言回退英文拼读（拉丁字母
        必在词表内，仍远优于 1 个伪 'a' 占位）。无数字的词原样返回。
        """
        w = word.translate(_FULLWIDTH_DIGIT_TRANS)
        if not _DIGIT_RUN_RE.search(w):
            return w
        table = _DIGIT_SPELLINGS.get(
            (language or "").strip().lower(), _DIGIT_SPELLINGS["english"]
        )
        return _DIGIT_RUN_RE.sub(
            lambda m: " " + " ".join(table[int(d)] for d in m.group(0)) + " ", w,
        )

    def _romanize_word(self, word: str, language: str = "") -> str:
        """把单词/单字/日语音拍罗马化为 ascii 目标串（供 CTC targets 用）。

        层级：
        0. 数字拼读展开：2024 → 各语言逐位拼读（英 two zero two four /
           日 ゼロいちによん / 中 二零二四…），避免纯数字词退化为 1 个伪 'a' token。
        1. 词尾促音（っ/ッ，顿挫气口，后面没有可双写的辅音）剔除后再罗马化
           （uroman 会误读成 'tsu'，あっ→atsu，实际是前一拍的急停）。
        2. 日语 + 含汉字 → pykakasi 整词取假名读音（保留送り仮名语境，咲き→さき），
           修复其促音て/た形辞典缺陷（待って：まつて→まって），再走 uroman
           与纯假名词**同一罗马化管线**，保证全句 token 约定一致。
        3. 其他语言/纯假名 → uroman（きゃ→kya、っと→tto 原生正确）。
        """
        base = word[:-1] if word.endswith(("っ", "ッ")) else word
        base = self._expand_digits(base, language)   # M5：数字→拼读词（先于一切罗马化）
        if not base:
            return ""      # 孤立促音：无发音 token，align() 的词内占位逻辑会注入最小占位

        # 日语汉字 → pykakasi 读音 → 假名 → 统一罗马化
        if language.lower() == "japanese" and _contains_kanji(base):
            kakasi = self._get_kakasi()
            if kakasi is not None:
                try:
                    hira = "".join(it.get("hira", "") for it in kakasi.convert(base))
                    # 促音て/た形修复：原词尾「っX」被误读为「つX」（词典对部分五段
                    # 动词覆盖缺陷：待って→まつて）→ 按形态还原 つ→っ
                    if (
                        len(base) >= 2 and base[-2] == "っ"
                        and len(hira) >= 2 and hira[-2] == "つ"
                        and hira[-1] == base[-1]
                    ):
                        hira = hira[:-2] + "っ" + hira[-1]
                    if hira:
                        romaji = self._romanize_kana(hira)
                        if romaji:
                            return romaji
                except Exception:
                    logger.debug("[MMSAligner] pykakasi 转换失败: %r，回退 uroman", word)

        return self._romanize_kana(base)

    def _romanize_kana(self, text: str) -> str:
        """uroman 罗马化（假名/汉字/其他文字的统一出口）。"""
        uro = self._get_uroman()
        if uro is not None:
            try:
                res = uro.romanize_string(text).lower().strip()
                clean = "".join(c for c in res if c.isalpha() or c in "'")
                return clean or text.lower()
            except Exception:
                pass
        return "".join(c for c in text.lower() if c.isalpha() or c.isalnum()) or "a"

    def unload(self) -> None:
        """销毁 ONNX InferenceSession，释放 ORT 占用的 GPU/CPU 内存。

        注意：ONNX Runtime CUDA EP **没有** 等价于 ``torch.cuda.empty_cache`` 的
        「权重搬到 RAM、会话仍活着」的 park 语义——Session 活着就会占着 CUDA
        分配器里的显存。因此 park / 上下文退出也必须走本方法，不能只改状态位。
        下次 ``align`` / ``_get_session`` 会重新加载（~300MB，通常 1s 内）；
        同时清除「本次会话强制 CPU」标记，下次任务重新按用户 device 偏好尝试 CUDA。
        """
        # 无论 Session 是否仍在，都清除会话级标记（运行期回退只影响当前会话）
        self._session_want_cuda = None
        self.session_device = ""
        if self._session is None:
            return
        try:
            # 部分 ORT 版本提供显式 close；没有则依赖 del + gc
            close = getattr(self._session, "close", None)
            if callable(close):
                close()
        except Exception as e:  # noqa: BLE001
            logger.debug("[MMSAligner] session.close() 忽略: %s", e)
        try:
            del self._session
        except Exception:  # noqa: BLE001
            pass
        self._session = None
        try:
            import gc
            gc.collect()
        except Exception:  # noqa: BLE001
            pass
        logger.info("[MMSAligner] 已销毁 ONNX 会话（ORT CUDA/CPU 显存应已交还）")


# ── 便捷全局单例 ──────────────────────────────────────────────
_GLOBAL_MMS_ALIGNER: Optional[MMSAligner] = None


def get_mms_aligner(
    model_dir: Path | str | None = None,
    device: str = "cuda",
) -> MMSAligner:
    global _GLOBAL_MMS_ALIGNER
    # None = 走用户偏好；偏好变更后 model_dir 变化会触发单例重建。
    # 单例键 = (规范化路径, device)：同一路径以不同 device 请求时返回不同实例，
    # 修复「先 device=cpu 再 device=cuda 却仍拿到 CPU 实例」的一致性问题。
    if model_dir is None:
        from core.app_config import load_preferences, resolved_paths
        model_dir = resolved_paths(load_preferences().paths)["mms_aligner_model_path"]
    if (
        _GLOBAL_MMS_ALIGNER is None
        or _GLOBAL_MMS_ALIGNER.model_dir != Path(model_dir)
        or _GLOBAL_MMS_ALIGNER.device != device
    ):
        _GLOBAL_MMS_ALIGNER = MMSAligner(model_dir=model_dir, device=device)
    return _GLOBAL_MMS_ALIGNER


__all__ = ["MMSAligner", "get_mms_aligner"]
