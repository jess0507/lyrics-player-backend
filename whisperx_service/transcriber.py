"""WhisperX 自動產生歌詞核心:音訊 → 文字 + 時間(ASR 轉寫 + 字級對齊)。

與對齊路線(`aligner.py`)互補:對齊是「已有文字、只補時間」(forced alignment),
本模組是「**沒有文字**、從音訊直接辨識」(ASR / STT,見
`../plans/15-lyrics-auto-generate.md`)。

WhisperX 標準管線本就是「transcribe → align」兩段,align 後半已落地於 `aligner.py`,
本模組補上前半的 **faster-whisper 轉寫**,再**複用** `aligner._load_align_model` 做
字級對齊精修時間,最後共用 `lrc.build_lrc` 組 LRC:

1. `whisperx.load_audio` 解 16kHz mono;`load_model(...).transcribe(...)` 取
   逐段(segment)文字與時間,**語言預設自動偵測**(歌聲場景不強制鎖定)。
2. 以偵測語言載入對齊模型,**逐段**取該段音訊、以 `aligner._char_alignments`
   (與 ``/align`` 同一套 `forced_align` + `frame_spans`)精修逐字時間。不用
   `whisperx.align`:它把字間的 CTC blank 幀併進前一字,字與字首尾相接、停頓恆為 0,
   中文 ASR 又無標點 → 切行找不到任何斷點(整段 30 秒一行)。單段對齊失敗只該段
   退回 ASR 段級時間,不致整批失敗。
3. 依字級時間把長段切成短行(與 ``/align`` 共用規則:句末標點 / 長停頓必斷,
   過長才在子句標點 / 停頓斷,見 :func:`split_segments_into_lines`)→ 組 LRC。行首無時間的字(字典外)依發音單元數往回推起點。
4. 對齊時餵 NFKC / 繁 → 簡的副本(中文對齊模型字典只有簡體;逐字等長,時間
   直接對回 Whisper 原文);輸出時整行轉成
   client 要的字形(``script``,或由 ``zh-TW`` / ``zh-CN`` 推定),再把每行起點
   提前 ``ALIGN_LEAD_SECONDS``(與 ``/align`` 共用)。

whisperx / torch 等重依賴延後到實際轉寫時才 import,讓 :func:`segments_to_fragments`
等純邏輯(及其單元測試)無須載入重依賴。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional

from aligner import (
    ALIGN_LEAD_SECONDS,
    ALIGN_OOV_UNIT_SECONDS,
    _char_alignments,
    _leading_untimed_units,
    _load_align_model,
    apply_lead,
    normalize_language,
    separator_for,
)
from lrc import build_lrc
from lyrics_text import (
    LINE_GAP_SECONDS,
    LINE_LONG_GAP_SECONDS,
    LINE_MAX_CHARS_CJK,
    LINE_MAX_SECONDS,
    LINE_MAX_WORDS,
    convert_script,
    normalize_for_alignment,
    output_script_for,
    split_aligned_lines,
)

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


# ── 重複幻聽過濾 ────────────────────────────────────────────────────────
# whisperx 3.1.5 的批次解碼(generate_segment_batched)只把 beam_size 等少數參數傳給
# 模型,repetition_penalty / no_repeat_ngram_size / compression_ratio_threshold /
# 溫度 fallback 都不生效 → 前奏、間奏等純音樂段常解出「啊啊啊…」整串。只能在 ASR
# 之後、對齊之前把這類段清掉(也避免把垃圾字硬對進音訊)。

# 同一個片語(1~_REPEAT_MAX_UNITS 個單元)連續出現超過此次數即視為重複幻聽。
ASR_MAX_REPEATS = int(os.environ.get("ASR_MAX_REPEATS", "3"))
# 偵測的片語最長單元數(中文一字一單元;英數一個詞一單元;標點也算一單元)。
_REPEAT_MAX_UNITS = 4

# 單元:英數詞整個算一個,其餘非空白字元一字一個(與 lyrics_text 切行同一套)。
_UNIT_RE = re.compile(r"[A-Za-z0-9'’]+|\S")
_WORD_CHAR_RE = re.compile(r"\w")

# 語氣詞 / 襯字:整串由這些組成的重複片語整段移除;整行只剩這些的段直接丟掉。
_FILLER_CJK = set("啊阿呀哦喔噢嗯哼哈啦呐哇唔欸诶誒耶嘿哎嗚呜喲哟")
_FILLER_LATIN = {
    "ah", "aah", "oh", "ooh", "o", "la", "na", "da", "uh", "um", "mm", "mmm",
    "hmm", "hm", "yeah", "ya", "hey", "whoa", "woah", "wo", "wu", "ha",
}


def _is_punct_unit(unit: str) -> bool:
    return not _WORD_CHAR_RE.search(unit)


def _is_filler_unit(unit: str) -> bool:
    return unit in _FILLER_CJK or unit.lower() in _FILLER_LATIN


def _is_filler_only(units: List[str]) -> bool:
    """非標點的單元全是語氣詞(且至少一個)。"""
    words = [u for u in units if not _is_punct_unit(u)]
    return bool(words) and all(_is_filler_unit(u) for u in words)


def collapse_repetitions(text: str, max_repeats: int = ASR_MAX_REPEATS) -> str:
    """清掉文字中的重複幻聽(純函式,可單元測試)。

    同一片語(1~4 個單元)連續出現超過 ``max_repeats`` 次時:
    - 片語全是語氣詞(「啊」「la la」…)→ 整串移除;
    - 否則保留前 ``max_repeats`` 次、移除其餘。

    ``max_repeats <= 0`` 時不處理。
    """
    if max_repeats <= 0:
        return text
    matches = list(_UNIT_RE.finditer(text))
    keys = [m.group().lower() for m in matches]
    n = len(keys)
    cuts: List[tuple] = []  # 要移除的原文字元區間 [start, end)
    i = 0
    while i < n:
        advanced = False
        for size in range(1, _REPEAT_MAX_UNITS + 1):
            if i + size * (max_repeats + 1) > n:
                break
            pattern = keys[i : i + size]
            reps = 1
            while keys[i + reps * size : i + (reps + 1) * size] == pattern:
                reps += 1
            if reps <= max_repeats:
                continue
            run_end = i + reps * size  # 不含
            keep = 0 if _is_filler_only(pattern) else max_repeats
            cut_from = i + keep * size
            cuts.append((matches[cut_from].start(), matches[run_end - 1].end()))
            i = run_end
            advanced = True
            break
        if not advanced:
            i += 1
    if not cuts:
        return text
    pieces: List[str] = []
    pos = 0
    for start, end in cuts:
        pieces.append(text[pos:start])
        pos = end
    pieces.append(text[pos:])
    # 直接接回(中文句中截斷不可補空格),再壓平英文截斷處留下的連續空白。
    return _clean_segment_text("".join(pieces))


def drop_hallucinated_segments(
    segments: List[dict], max_repeats: int = ASR_MAX_REPEATS
) -> List[dict]:
    """過濾 ASR 段的重複幻聽(純函式,不改輸入)。

    每段文字先經 :func:`collapse_repetitions`;清完後沒有字、或只剩語氣詞 / 標點的
    段整段丟掉(前奏被解成「啊啊」也一併去除)。其餘欄位(start / end)原樣保留。
    """
    out: List[dict] = []
    for seg in segments:
        text = _clean_segment_text(seg.get("text"))
        cleaned = collapse_repetitions(text, max_repeats)
        units = _UNIT_RE.findall(cleaned)
        if not units or all(_is_punct_unit(u) for u in units) or _is_filler_only(units):
            if text:
                log.info("丟棄重複 / 語氣詞段 [%s-%s]:%.40s", seg.get("start"), seg.get("end"), text)
            continue
        if cleaned != text:
            log.info("清除段內重複:%.40s → %.40s", text, cleaned)
            seg = {**seg, "text": cleaned}
        out.append(seg)
    return out


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


def _word_time(word: dict, key: str) -> Optional[float]:
    """取字級時間(秒);缺漏或非數值回 None(數字等字典外字元常無時間)。"""
    value = word.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _split_segment(
    seg: dict,
    separator: str,
    split_params: dict,
    oov_unit_seconds: float = ALIGN_OOV_UNIT_SECONDS,
) -> List[dict]:
    """依字級時間把單一 segment 切成多個子段(``{text, start, end}``)。

    把 ``words`` 串成一行文字 + 逐字時間(詞的時間在詞內逐字線性內插;只有
    start 或 end 時給首字 / 末字),
    交給與 ``/align`` 共用的 :func:`lyrics_text.split_aligned_lines` 切行
    (句末標點 / 長停頓必斷;過長才在子句標點 / 停頓斷;行首尾外文斷開)。

    無 ``words`` 時原樣回傳。子段缺時間時,首段沿用原段 ``start``、末段沿用原段
    ``end``,其餘交給 :func:`segments_to_fragments` 沿用前一行時間。

    子段開頭有無時間的字(字典外:中文裡的英文 / 數字等)時,起點依其發音單元數
    × ``oov_unit_seconds`` 從第一個有時間的字往回推,不早於前一子段結束;
    跨段的下限由 :func:`segments_to_fragments` 夾住。
    """
    words = [
        (text, _word_time(w, "start"), _word_time(w, "end"))
        for w in (seg.get("words") or [])
        for text in [str(w.get("word") or "").strip()]
        if text
    ]
    if not words:
        return [seg]

    line = separator.join(t for t, _s, _e in words)
    chars: List[dict] = []
    for i, (text, w_start, w_end) in enumerate(words):
        if i > 0:
            chars.extend({"char": c, "start": None, "end": None} for c in separator)
        n = len(text)
        if w_start is not None and w_end is not None and w_end > w_start:
            # 詞內逐字線性內插:一個詞被切成兩行時(如 "Absorb,吸收." 在語系
            # 切換處斷開),後半行才有自己的估計起點,而不是跟前半行同一時間。
            step = (w_end - w_start) / n
            chars.extend(
                {"char": ch, "start": w_start + step * k, "end": w_start + step * (k + 1)}
                for k, ch in enumerate(text)
            )
            continue
        for k, ch in enumerate(text):
            chars.append(
                {
                    "char": ch,
                    "start": w_start if k == 0 else None,
                    "end": w_end if k == n - 1 else None,
                }
            )

    new_lines, new_chars = split_aligned_lines([line], chars, separator, **split_params)

    out: List[dict] = []
    prev_line_end: Optional[float] = None
    pos = 0
    for li, text in enumerate(new_lines):
        if li > 0:
            pos += len(separator)
        line_chars = new_chars[pos : pos + len(text)]
        pos += len(text)
        first = next((k for k, c in enumerate(line_chars) if c["start"] is not None), None)
        ends = [c["end"] for c in line_chars if c["end"] is not None]
        start = line_chars[first]["start"] if first is not None else None
        if first and oov_unit_seconds > 0:
            units = _leading_untimed_units("".join(c["char"] for c in line_chars[:first]))
            if units:
                floor = prev_line_end if prev_line_end is not None else 0.0
                start = max(start - units * oov_unit_seconds, floor, 0.0)
        out.append({"text": text, "start": start, "end": ends[-1] if ends else None})
        if ends:
            prev_line_end = ends[-1]
    if out[0]["start"] is None:
        out[0]["start"] = seg.get("start")
    if out[-1]["end"] is None:
        out[-1]["end"] = seg.get("end")
    return out


def split_segments_into_lines(
    segments: List[dict],
    language: str,
    gap_seconds: float = LINE_GAP_SECONDS,
    max_chars_cjk: int = LINE_MAX_CHARS_CJK,
    max_words: int = LINE_MAX_WORDS,
    max_seconds: float = LINE_MAX_SECONDS,
    long_gap_seconds: float = LINE_LONG_GAP_SECONDS,
    oov_unit_seconds: float = ALIGN_OOV_UNIT_SECONDS,
) -> List[dict]:
    """把對齊後的段依字級時間(``words``)切成短行(純函式,可單元測試)。

    切行規則與 ``/align`` 相同(見 :func:`lyrics_text.split_aligned_lines`);
    每個 ASR 段本身就是邊界。中 / 日文的 ``words`` 為逐字、以空字串串接並以字數
    計上限;其餘語言為逐詞、以空格串接並以詞數計上限。無 ``words`` 的段(如對齊
    失敗退回 ASR 段級時間)原樣保留。
    """
    separator = separator_for(language)
    split_params = dict(
        max_chars_cjk=max_chars_cjk,
        max_words=max_words,
        max_seconds=max_seconds,
        gap_seconds=gap_seconds,
        long_gap_seconds=long_gap_seconds,
    )
    out: List[dict] = []
    for seg in segments:
        out.extend(_split_segment(seg, separator, split_params, oov_unit_seconds))
    return out


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


def chars_to_words(text: str, chars: List[dict], separator: str) -> List[dict]:
    """把逐字對齊結果併成 whisperx 同形的 ``words``(純函式,可單元測試)。

    ``chars`` 與 ``text`` 逐字對應(``_char_alignments`` 的輸出,字典外的字
    start / end 為 None)。中 / 日文(``separator == ""``)每個非空白字一個 word;
    其餘語言以空白分詞,詞的 ``start`` 取第一個有時間的字、``end`` 取最後一個。
    整詞無時間則不帶 start / end(交給 :func:`_split_segment` 處理字典外字)。
    """
    words: List[dict] = []

    def emit(word_chars: List[dict]) -> None:
        word = {"word": "".join(c["char"] for c in word_chars)}
        starts = [c["start"] for c in word_chars if c.get("start") is not None]
        ends = [c["end"] for c in word_chars if c.get("end") is not None]
        if starts:
            word["start"] = starts[0]
        if ends:
            word["end"] = ends[-1]
        words.append(word)

    if separator == "":
        for c in chars:
            if not c["char"].isspace():
                emit([c])
        return words

    cur: List[dict] = []
    for c in chars:
        if c["char"].isspace():
            if cur:
                emit(cur)
                cur = []
        else:
            cur.append(c)
    if cur:
        emit(cur)
    return words


def _align_segments(segments: List[dict], lang: str, audio) -> List[dict]:
    """逐段做字級 forced alignment,回傳帶 ``words`` 的段(時間為整首絕對秒數)。

    每段只取 ``[start, end]`` 的音訊,文字為壓平空白後的 Whisper 原文;對齊餵
    :func:`_alignment_segments` 的正規化副本(逐字等長),時間對回原文字元。
    單段對齊失敗(字典外字過多、文字比音訊長等)則該段不帶 ``words``,
    由 :func:`split_segments_into_lines` 原樣保留為 ASR 段級時間。
    對齊模型載入失敗則直接丟出(由呼叫端整批退回 ASR 段級時間)。
    """
    model, metadata = _load_align_model(lang)
    separator = separator_for(lang)
    cleaned = [
        {**seg, "text": _clean_segment_text(seg.get("text"))} for seg in segments
    ]
    normalized = _alignment_segments(cleaned, lang)
    out: List[dict] = []
    for seg, norm in zip(cleaned, normalized):
        text = seg["text"]
        start, end = seg.get("start"), seg.get("end")
        if not text or start is None or end is None:
            out.append(seg)
            continue
        s = max(int(float(start) * _SAMPLE_RATE), 0)
        e = min(int(float(end) * _SAMPLE_RATE), len(audio))
        if e <= s:
            out.append(seg)
            continue
        try:
            chars = _char_alignments(model, metadata, audio[s:e], norm["text"])
        except Exception as exc:  # AlignmentError / torch 內部錯誤 → 該段退回
            log.warning("段 %.1f~%.1fs 對齊失敗,退回段級時間:%s", start, end, exc)
            out.append(seg)
            continue
        offset = s / _SAMPLE_RATE
        timed = [
            {
                "char": orig,
                "start": c["start"] + offset if c["start"] is not None else None,
                "end": c["end"] + offset if c["end"] is not None else None,
            }
            for orig, c in zip(text, chars)
        ]
        out.append({**seg, "words": chars_to_words(text, timed, separator)})
    return out


def _alignment_segments(segments: List[dict], lang: str) -> List[dict]:
    """複製一份給字級對齊的段(純函式):文字做 NFKC / 中文繁 → 簡,
    逐字等長(中文對齊模型字典只有簡體;Whisper 輸出常繁簡混雜)。

    不遮蔽段落標籤:對齊後的字詞文字會直接顯示,不能留遮蔽字元。
    """
    out = []
    for seg in segments:
        copy = dict(seg)
        copy["text"] = normalize_for_alignment(
            str(seg.get("text") or ""), lang, mask_annotations=False
        )
        out.append(copy)
    return out


def finalize_fragments(
    fragments: List[dict],
    script: Optional[str],
    lead_seconds: float = ALIGN_LEAD_SECONDS,
) -> List[dict]:
    """顯示前的最後處理(純函式,原地修改並回傳):整行轉成目標字形、起點提前。"""
    for frag in fragments:
        frag["text"] = convert_script(frag["text"], script)
    return apply_lead(fragments, lead_seconds)


def transcribe(
    audio_path: str,
    language: Optional[str] = None,
    script: Optional[str] = None,
) -> dict:
    """轉寫音訊為含時間的歌詞,回傳 LRC 與逐行片段。

    參數:
        audio_path: 本機音訊檔路徑(任何 ffmpeg 解得的格式)。
        language: 語言提示(選填)。預設 ``None`` → whisper 自動偵測語言;
            給值時正規化為二字母碼鎖定,避免歌聲誤判。
        script: 中文輸出字形(``"traditional"`` / ``"simplified"``,選填)。
            省略時由 ``language`` 推定(``zh-TW`` → 繁體、``zh-CN`` → 簡體),
            仍無法判定則不轉換。

    回傳 ``{"lrc": str, "fragments": [...], "language": str,
    "durationSeconds": float}``(``durationSeconds`` 供呼叫端累加每月配額用量,
    不對外回傳)。失敗丟 :class:`TranscriptionError`(音訊不可用 / 辨識不出歌詞 /
    模型不可用)。
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
    # 先清重複幻聽(前奏 / 間奏的「啊啊啊…」)再對齊,垃圾字不進對齊、也不出現在歌詞。
    n_raw = len(raw_segments)
    raw_segments = drop_hallucinated_segments(raw_segments)
    if len(raw_segments) != n_raw:
        log.info("重複幻聽過濾 段數=%d → %d", n_raw, len(raw_segments))
    if not any(_clean_segment_text(s.get("text")) for s in raw_segments):
        raise TranscriptionError("未辨識出任何歌詞文字")

    # 逐段字級對齊精修時間;與 /align 共用 _load_align_model / _char_alignments
    # (不快取模型,每次重新載入)。模型不可用則整批退回 ASR 段級時間(粒度較粗但仍可用)。
    segments = raw_segments
    t_align = time.perf_counter()
    try:
        segments = _align_segments(raw_segments, lang, audio)
        log.info("字級對齊完成(耗時 %.1fs)", time.perf_counter() - t_align)
    except Exception as exc:  # 無對齊模型 / 對齊失敗 → 退回 ASR 段級時間。
        log.warning(
            "對齊精修失敗,退回 ASR 段級時間(已耗時 %.1fs):%s",
            time.perf_counter() - t_align,
            exc,
        )

    # 依字級時間切短行(無 words 的段原樣保留,故對齊失敗時等同不切)。
    lines = split_segments_into_lines(segments, lang)
    log.info("切行完成 段數=%d → 行數=%d", len(segments), len(lines))

    fragments = segments_to_fragments(lines)
    if not fragments:
        raise TranscriptionError("轉寫結果無有效歌詞行")
    target_script = script or output_script_for(language)
    finalize_fragments(fragments, target_script)

    return {
        "lrc": build_lrc(fragments),
        "fragments": fragments,
        "language": lang,
        "durationSeconds": duration,
    }
