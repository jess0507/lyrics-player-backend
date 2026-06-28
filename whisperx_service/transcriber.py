"""WhisperX 自動產生歌詞核心:音訊 → 文字 + 時間(ASR 轉寫 + 字級對齊)。

與對齊路線(`aligner.py`)互補:對齊是「已有文字、只補時間」(forced alignment),
本模組是「**沒有文字**、從音訊直接辨識」(ASR / STT,見
`../plans/15-lyrics-auto-generate.md`)。

WhisperX 標準管線本就是「transcribe → align」兩段,align 後半已落地於 `aligner.py`,
本模組補上前半的 **faster-whisper 轉寫**,再**複用** `aligner._load_align_model` 做
字級對齊精修時間,最後共用 `lrc.build_lrc` 組 LRC:

1. `whisperx.load_audio` 解 16kHz mono;`load_model(...).transcribe(...)` 取
   逐段(segment)文字與時間,**語言預設自動偵測**(歌聲場景不強制鎖定)。
2. 以偵測語言載入對齊模型,`whisperx.align(...)` 精修每段時間(失敗則退回
   ASR 段級時間,不致整批失敗)。
3. 每段文字即一「行」→ 組 LRC。

whisperx / torch 等重依賴延後到實際轉寫時才 import,讓 :func:`segments_to_fragments`
等純邏輯(及其單元測試)無須載入重依賴。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

from aligner import _load_align_model, normalize_language
from lrc import build_lrc

log = logging.getLogger(__name__)

# whisperx.load_audio 固定輸出 16kHz;用來把樣本數換算成秒。
_SAMPLE_RATE = 16000

# ASR 模型大小(env 可調)。CPU 上 large 對長曲易逼近逾時且吃記憶體,預設 small;
# 實測歌聲品質 / 耗時後再調(見計畫「模型大小取捨」)。
_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")

# faster-whisper 的 CPU 量化型別(int8 最省記憶體 / 最快,品質略降)。
_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")

# transcribe 的 batch 大小(VRAM / RAM 取捨;CPU 上影響有限)。
_BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", "16"))

# ASR 模型載入昂貴(下載權重),以模型大小為 key 做行程內快取。
_ASR_MODEL_CACHE: Dict[str, object] = {}


class TranscriptionError(Exception):
    """轉寫失敗(音訊不可用,或辨識不出可用歌詞)。對應端點 422。"""


def _clean_segment_text(text: object) -> str:
    """壓平段內換行 / 連續空白為單一空格並去前後空白(避免 LRC 一行含換行)。"""
    return " ".join(str(text or "").split()).strip()


def segments_to_fragments(segments: List[dict]) -> List[dict]:
    """把 whisper / 對齊後的 segment 串流轉為逐行片段(純函式,可單元測試)。

    每個非空段 → 一行;``begin`` / ``end`` 取段的 ``start`` / ``end``(秒),
    缺漏時沿用前一行結束時間,並夾住為單調遞增、``end >= begin``。空白文字段略過。

    回傳 ``[{index, begin, end, text}, ...]``(index 為過濾後的連續序號)。
    """
    fragments: List[dict] = []
    last_end = 0.0
    for seg in segments:
        text = _clean_segment_text(seg.get("text"))
        if not text:
            continue
        raw_begin = seg.get("start")
        raw_end = seg.get("end")
        begin = float(raw_begin) if raw_begin is not None else last_end
        if begin < last_end:
            begin = last_end
        end = float(raw_end) if raw_end is not None else begin
        if end < begin:
            end = begin
        last_end = end
        fragments.append(
            {"index": len(fragments), "begin": begin, "end": end, "text": text}
        )
    return fragments


def _load_asr_model():
    """載入(並快取)faster-whisper ASR 模型。CPU 推論(Cloud Run 無 GPU)。"""
    cached = _ASR_MODEL_CACHE.get(_MODEL_SIZE)
    if cached is not None:
        return cached
    import whisperx  # 延後 import:重依賴只在實際轉寫時載入。

    try:
        model = whisperx.load_model(
            _MODEL_SIZE, device="cpu", compute_type=_COMPUTE_TYPE
        )
    except Exception as exc:  # 模型不存在 / 下載失敗
        raise TranscriptionError(
            f"無法載入 ASR 模型({_MODEL_SIZE}):{exc}"
        ) from exc
    _ASR_MODEL_CACHE[_MODEL_SIZE] = model
    return model


def transcribe(audio_path: str, language: Optional[str] = None) -> dict:
    """轉寫音訊為含時間的歌詞,回傳 LRC 與逐行片段。

    參數:
        audio_path: 本機音訊檔路徑(任何 ffmpeg 解得的格式)。
        language: 語言提示(選填)。預設 ``None`` → whisper 自動偵測語言;
            給值時正規化為二字母碼鎖定,避免歌聲誤判。

    回傳 ``{"lrc": str, "fragments": [...], "language": str}``。
    失敗丟 :class:`TranscriptionError`(音訊不可用 / 辨識不出歌詞 / 模型不可用)。
    """
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise TranscriptionError("音訊檔不存在或為空")

    import whisperx  # 延後 import。

    audio = whisperx.load_audio(audio_path)
    duration = len(audio) / _SAMPLE_RATE
    if duration <= 0:
        raise TranscriptionError("音訊長度為 0,無法轉寫")
    # 音訊長度直接決定 CPU 轉寫耗時;長曲是 client 端 deadline-exceeded 主因。
    log.info(
        "音訊長度 %.1fs,model=%s compute=%s",
        duration,
        _MODEL_SIZE,
        _COMPUTE_TYPE,
    )

    t_load = time.perf_counter()
    model = _load_asr_model()
    log.info("ASR 模型就緒(載入耗時 %.1fs)", time.perf_counter() - t_load)

    lang_hint = normalize_language(language) if language else None
    t_asr = time.perf_counter()
    try:
        result = model.transcribe(
            audio, batch_size=_BATCH_SIZE, language=lang_hint
        )
    except Exception as exc:  # faster-whisper 內部例外型別繁多,統一轉成轉寫失敗。
        log.exception("whisper transcribe 失敗(已耗時 %.1fs)", time.perf_counter() - t_asr)
        raise TranscriptionError(f"轉寫失敗:{exc}") from exc
    log.info(
        "ASR 轉寫完成 段數=%d lang=%s(耗時 %.1fs)",
        len(result.get("segments") or []),
        result.get("language"),
        time.perf_counter() - t_asr,
    )

    raw_segments = result.get("segments") or []
    lang = result.get("language") or lang_hint or "en"
    if not any(_clean_segment_text(s.get("text")) for s in raw_segments):
        raise TranscriptionError("未辨識出任何歌詞文字")

    # 字級對齊精修每段時間;複用對齊路線的模型快取。對齊失敗不致整批失敗——
    # 退回 ASR 段級時間(粒度較粗但仍可用)。
    segments = raw_segments
    t_align = time.perf_counter()
    try:
        align_model, metadata = _load_align_model(lang)
        aligned = whisperx.align(
            raw_segments,
            align_model,
            metadata,
            audio,
            "cpu",
            return_char_alignments=False,
        )
        segments = aligned.get("segments") or raw_segments
        log.info("字級對齊完成(耗時 %.1fs)", time.perf_counter() - t_align)
    except Exception as exc:  # 無對齊模型 / 對齊失敗 → 退回 ASR 段級時間。
        log.warning(
            "對齊精修失敗,退回 ASR 段級時間(已耗時 %.1fs):%s",
            time.perf_counter() - t_align,
            exc,
        )

    fragments = segments_to_fragments(segments)
    if not fragments:
        raise TranscriptionError("轉寫結果無有效歌詞行")

    return {
        "lrc": build_lrc(fragments),
        "fragments": fragments,
        "language": lang,
    }
