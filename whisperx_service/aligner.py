"""WhisperX forced alignment 核心:既有純文字 + 音訊 → 每行起始時間。

與 aeneas 路線相同的任務(保留使用者已校對的文字、只補時間),但改以 WhisperX
的 **wav2vec2 對齊**取得**字級**時間戳,粒度較細、中文 / 歌聲對齊一般較佳
(見 `../plans/13-lyrics-auto-sync-whisperx.md`)。

對齊策略(純 forced alignment、不做 ASR 轉寫):
1. 用 ffmpeg 把音訊解成 16kHz mono(`whisperx.load_audio`),取總長度。
2. 把所有歌詞行接成「一個涵蓋整段音訊的 segment」(text = 各行串接),
   交給 wav2vec2 對齊,要求回傳**字級** char alignments。
3. 因 segment 文字就是各行串接,char 串流會與串接文字逐字對應;依各行在串接
   文字中的字元範圍切片,取每行第一個有時間的字元為 begin、最後一個為 end。
4. 組回逐行片段 → LRC。

whisperx 與其 torch / wav2vec2 依賴較重,故 ``import whisperx`` 延後到實際對齊時,
讓 LRC 等純邏輯(`lrc.py`)與 :func:`build_fragments` 的單元測試不必載入重依賴。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from lrc import build_lrc

log = logging.getLogger(__name__)

# whisperx.load_audio 固定輸出 16kHz;用來把樣本數換算成秒。
_SAMPLE_RATE = 16000

# 對齊覆蓋率下限:有取得時間戳的行數佔比低於此值,視為對齊失敗(寧可降級保留
# 原 unsynced 文字,也不硬塞錯時間)。可用環境變數覆寫。
_MIN_COVERAGE = float(os.environ.get("WHISPERX_MIN_COVERAGE", "0.6"))


class AlignmentError(Exception):
    """對齊失敗(輸入不合法,或 wav2vec2 無法產出可用結果)。"""


# whisperx 的對齊模型以**二字母**語言碼索引(load_align_model 的 language_code)。
# 客戶端可能送 BCP-47 / ISO 639-3(沿用 aeneas 路線的預設 "eng"),這裡做寬鬆對應。
_LANGUAGE_MAP = {
    "zh": "zh",
    "zh-hant": "zh",
    "zh-hans": "zh",
    "zh-tw": "zh",
    "zh-cn": "zh",
    "cmn": "zh",
    "yue": "zh",
    "en": "en",
    "eng": "en",
    "ja": "ja",
    "jpn": "ja",
    "ko": "ko",
    "kor": "ko",
    "es": "es",
    "spa": "es",
    "fr": "fr",
    "fra": "fr",
    "de": "de",
    "deu": "de",
    "it": "it",
    "ita": "it",
    "pt": "pt",
    "por": "pt",
    "ru": "ru",
    "rus": "ru",
    "nl": "nl",
    "uk": "uk",
}

# whisperx 對「無空格分詞」的語言(中文 / 日文)以字元為單位處理,串接歌詞行時
# 不應插入空格;其餘語言以單一空格分隔,避免相鄰行尾首字被黏成同一個 token。
_LANGUAGES_WITHOUT_SPACES = {"zh", "ja"}

# 對齊模型載入昂貴(會下載 wav2vec2 權重),以語言碼為 key 做行程內快取。
_ALIGN_MODEL_CACHE: Dict[str, tuple] = {}


def normalize_language(code: str) -> str:
    """把客戶端語言碼正規化為 whisperx 的二字母碼;未知碼取前兩字母、預設 en。"""
    if not code:
        return "en"
    key = code.strip().lower().replace("_", "-")
    if key in _LANGUAGE_MAP:
        return _LANGUAGE_MAP[key]
    # 未知碼:取主語言子標籤的前兩個字母(如 "sv-SE" → "sv"),交給 whisperx 試載。
    return key.split("-")[0][:2] or "en"


def separator_for(language: str) -> str:
    """串接歌詞行時各行之間的分隔字元(中 / 日無空格,其餘用單一空格)。"""
    return "" if language in _LANGUAGES_WITHOUT_SPACES else " "


def _clean_lines(lines: List[str]) -> List[str]:
    """去除空白行並修整;對齊片段與回傳結果都以此清單為準。"""
    return [s.strip() for s in lines if s and s.strip()]


def build_fragments(
    lines: List[str],
    chars: List[dict],
    separator: str,
) -> Tuple[List[dict], int]:
    """把字級 char alignments 依各行字元範圍切回逐行片段(純函式,可單元測試)。

    參數:
        lines: 已清理的歌詞行(對齊用的串接文字即 ``separator.join(lines)``)。
        chars: whisperx 的字級對齊串流,逐元素 ``{"char","start","end",...}``,
               順序與串接文字一致;無法對齊的字元其 ``start`` / ``end`` 可能缺漏。
        separator: 串接各行所用的分隔字元(見 :func:`separator_for`)。

    回傳 ``(fragments, timed_count)``:``fragments`` 為
    ``[{index, begin, end, text}, ...]``(秒);``timed_count`` 為實際取得起始
    時間的行數(供覆蓋率判定)。
    """
    # 各行在串接文字中的 [start, end) 字元索引範圍。
    ranges: List[Tuple[int, int]] = []
    pos = 0
    for i, line in enumerate(lines):
        if i > 0:
            pos += len(separator)
        ranges.append((pos, pos + len(line)))
        pos += len(line)
    total_len = pos

    # 正常情況下 char 串流長度應等於串接文字長度(逐字對應);若 whisperx 因正規化
    # 等因素導致長度不符,退而用比例分配(保證單調遞增、不漏行)。
    if len(chars) != total_len:
        log.warning(
            "char 串流長度(%d)與串接文字(%d)不符,改用比例分配",
            len(chars),
            total_len,
        )
        return _proportional_fragments(lines, chars)

    fragments: List[dict] = []
    last_end = 0.0
    timed = 0
    for i, (s, e) in enumerate(ranges):
        begin: Optional[float] = None
        end: Optional[float] = None
        for c in chars[s:e]:
            cs = c.get("start")
            ce = c.get("end")
            if cs is not None and begin is None:
                begin = float(cs)
            if ce is not None:
                end = float(ce)
        if begin is None:
            # 該行整行無時間:沿用前一行結束時間,保持 LRC 單調遞增。
            begin = last_end
            if end is None:
                end = last_end
        else:
            timed += 1
        if end is None or end < begin:
            end = begin
        last_end = end
        fragments.append(
            {"index": i, "begin": begin, "end": end, "text": lines[i]}
        )
    return fragments, timed


def _proportional_fragments(
    lines: List[str],
    chars: List[dict],
) -> Tuple[List[dict], int]:
    """退路:char 串流無法逐字對應時,按各行字元數在整體時間跨度內比例分配。

    取串流中所有有時間的字元算出整體 [start, end],再依各行累計字元長度等比
    切分。結果一定單調遞增、與輸入行一一對應,但精度低於逐字對應。
    """
    starts = [float(c["start"]) for c in chars if c.get("start") is not None]
    ends = [float(c["end"]) for c in chars if c.get("end") is not None]
    if not starts:
        # 整段都沒有任何時間 → 視為完全無法對齊,交由上層判定失敗。
        return ([
            {"index": i, "begin": 0.0, "end": 0.0, "text": line}
            for i, line in enumerate(lines)
        ], 0)

    span_start = min(starts)
    span_end = max(ends) if ends else span_start
    total_chars = sum(len(line) for line in lines) or 1
    duration = max(span_end - span_start, 0.0)

    fragments: List[dict] = []
    consumed = 0
    for i, line in enumerate(lines):
        begin = span_start + duration * (consumed / total_chars)
        consumed += len(line)
        end = span_start + duration * (consumed / total_chars)
        fragments.append(
            {"index": i, "begin": begin, "end": max(end, begin), "text": line}
        )
    # 比例分配視為「全行皆有估計時間」,covered = 全部行。
    return fragments, len(lines)


def _load_align_model(language: str):
    """載入(並快取)whisperx 的 wav2vec2 對齊模型。CPU 推論(Cloud Run 無 GPU)。"""
    cached = _ALIGN_MODEL_CACHE.get(language)
    if cached is not None:
        return cached
    import whisperx  # 延後 import:重依賴只在實際對齊時載入。

    try:
        model, metadata = whisperx.load_align_model(
            language_code=language, device="cpu"
        )
    except Exception as exc:  # 該語言無對齊模型 / 下載失敗
        raise AlignmentError(f"無對齊模型可用({language}):{exc}") from exc
    _ALIGN_MODEL_CACHE[language] = (model, metadata)
    return model, metadata


def align(lines: List[str], audio_path: str, language: str) -> dict:
    """對齊既有歌詞行與音訊,回傳 LRC 與逐行片段(含字級時間預留欄)。

    參數:
        lines: 純文字歌詞(每元素一行,顯示用文字;空行會先被濾掉)。
        audio_path: 本機音訊檔路徑(任何 ffmpeg 解得的格式)。
        language: 客戶端語言碼(會經 :func:`normalize_language` 正規化)。

    回傳 dict:``{"lrc": str, "fragments": [...], "language": str}``。
    失敗丟 :class:`AlignmentError`(覆蓋率過低 / 無法對齊 / 模型不可用)。
    """
    clean = _clean_lines(lines)
    if not clean:
        raise AlignmentError("lines 去空行後為空,無可對齊內容")
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise AlignmentError("音訊檔不存在或為空")

    lang = normalize_language(language)
    import whisperx  # 延後 import。

    audio = whisperx.load_audio(audio_path)
    duration = len(audio) / _SAMPLE_RATE
    if duration <= 0:
        raise AlignmentError("音訊長度為 0,無法對齊")
    # 音訊長度直接決定 CPU 對齊耗時;長曲是 client 端 deadline-exceeded 主因。
    log.info("音訊長度 %.1fs,行數=%d lang=%s", duration, len(clean), lang)

    t_load = time.perf_counter()
    model, metadata = _load_align_model(lang)
    log.info("對齊模型就緒(載入耗時 %.1fs)", time.perf_counter() - t_load)

    separator = separator_for(lang)
    # 單一 segment 涵蓋整段音訊,text = 各行串接 → wav2vec2 做整段 forced alignment。
    segments = [{"start": 0.0, "end": duration, "text": separator.join(clean)}]

    t_align = time.perf_counter()
    try:
        result = whisperx.align(
            segments,
            model,
            metadata,
            audio,
            "cpu",
            return_char_alignments=True,
        )
    except Exception as exc:  # whisperx 內部例外型別繁多,統一轉成對齊失敗。
        log.exception("whisperx align 失敗(已耗時 %.1fs)", time.perf_counter() - t_align)
        raise AlignmentError(f"whisperx 對齊失敗:{exc}") from exc
    log.info("wav2vec2 對齊完成(耗時 %.1fs)", time.perf_counter() - t_align)

    chars: List[dict] = []
    for seg in result.get("segments", []):
        chars.extend(seg.get("chars", []))
    if not chars:
        raise AlignmentError("對齊未產出任何字級時間")

    fragments, timed = build_fragments(clean, chars, separator)
    coverage = timed / len(clean)
    if coverage < _MIN_COVERAGE:
        raise AlignmentError(
            f"對齊覆蓋率過低({timed}/{len(clean)}),保留原文字"
        )

    return {
        "lrc": build_lrc(fragments),
        "fragments": fragments,
        "language": lang,
    }
