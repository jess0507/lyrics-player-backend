"""歌詞文字處理(純邏輯,不依賴 whisperx / torch,可獨立單元測試)。

兩件事:

1. **對齊用正規化**(:func:`normalize_for_alignment`):產生一份「只給 CTC 對齊看」
   的文字,**長度與原文逐字相同**,讓字級時間戳仍能一一對回原文(顯示 / 回傳
   的文字維持使用者原文):
   - NFKC:全形英數 → 半形(只取單字元結果,維持長度)。
   - 中文繁 → 簡:zh 對齊模型(``wav2vec2-large-xlsr-53-chinese-zh-cn``)字典
     只有 3503 個簡體字,常用繁體字多半不在字典 → 取不到時間戳。逐字轉換。
   - 遮蔽不唱的內容(段落標籤、作詞作曲等 credits、殘留的 LRC 時間戳、
     重複記號):換成字典外的 :data:`MASK_CHAR`,不參與對齊,也不會被硬塞到
     音訊某處而拉歪附近的時間。
   標點本來就不在對齊字典裡(``aligner.tokenize_text`` 會略過),不需處理。

2. **切行**:把過長的行依標點 / 換氣停頓 / 長度上限切短,避免一個時間點顯示
   一大段歌詞。``/transcribe`` 與 ``/align`` 共用下列門檻(env 可調)。
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from functools import lru_cache
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── 切行門檻(/transcribe、/align 共用)──────────────────────────────────
# 相鄰兩字(詞)的間隔 ≥ 此秒數 → 換行(依換氣停頓斷行)。
LINE_GAP_SECONDS = float(os.environ.get("LYRICS_LINE_GAP_SECONDS", "0.6"))
# 中 / 日文一行最多字數(無空格語言以字計)。
LINE_MAX_CHARS_CJK = int(os.environ.get("LYRICS_LINE_MAX_CHARS_CJK", "14"))
# 其他語言一行最多詞數(以空格分詞計)。
LINE_MAX_WORDS = int(os.environ.get("LYRICS_LINE_MAX_WORDS", "10"))
# 一行最長秒數(首字 start → 末字 end),超過強制換行。
LINE_MAX_SECONDS = float(os.environ.get("LYRICS_LINE_MAX_SECONDS", "7"))
# /align 是否自動切長行(使用者原文的分行會因此改變)。
ALIGN_SPLIT_LINES = os.environ.get("ALIGN_SPLIT_LINES", "1") not in ("0", "false", "False")

# 遮蔽字元:不在任何對齊模型字典中 → tokenize 時被略過、時間戳留 None。
MASK_CHAR = "\x00"

# 句讀:行內出現於單元結尾時,是長行的優先斷點(撇號不算,避免切 singin')。
BREAK_PUNCT = set("，。、！？；：…—～,.!?;:~")

# ── 不唱內容的偵測 ──────────────────────────────────────────────────────
_SECTION_WORDS = (
    r"chorus|pre[- ]?chorus|post[- ]?chorus|verse|bridge|intro|outro|hook|refrain|"
    r"interlude|instrumental|solo|break|rap|repeat|"
    r"副歌|主歌|前奏|間奏|间奏|尾奏|過門|过门|導歌|导歌|橋段|桥段|獨白|独白|重複|重复|"
    r"コーラス|サビ|間奏|イントロ|アウトロ"
)
# 整行都是括號內的段落標籤:"[Chorus]"、"(副歌)"、"【Verse 2】"、"<Bridge>"。
_SECTION_LINE_RE = re.compile(
    r"^\s*[\[\(（【<〈]\s*(?:" + _SECTION_WORDS + r")[^\]\)）】>〉]*[\]\)）】>〉]\s*$",
    re.IGNORECASE,
)
# 整行是不帶括號的段落標籤:"Chorus:"、"Verse 1"、"副歌:"。
_BARE_SECTION_LINE_RE = re.compile(
    r"^\s*(?:" + _SECTION_WORDS + r")\s*\d*\s*[:：]?\s*$",
    re.IGNORECASE,
)
# 整行方括號 / 【】:LRC metadata([ar:xxx])或註記,歌詞幾乎不會整行寫在方括號裡。
_SQUARE_LINE_RE = re.compile(r"^\s*(?:\[[^\]]*\]|【[^】]*】)\s*$")
# credits 行:"作詞:xxx"、"Lyrics by xxx"、"Composer: xxx"。
_CREDIT_LINE_RE = re.compile(
    r"^\s*(?:作詞|作词|作曲|編曲|编曲|填詞|填词|詞|词|曲|監製|监制|製作人?|制作人?|"
    r"演唱|原唱|歌手|和聲|和声|混音|"
    r"lyrics?|lyricist|composer|music|arranger|arrangement|producer|vocals?|"
    r"written|composed|arranged|produced)"
    r"(?:\s*by)?\s*[:：/／]",
    re.IGNORECASE,
)
# 行內:LRC 時間戳 "[01:23.45]"、行首段落標籤 "[Chorus] ..."、重複記號 "(x2)" / "×3"。
_INLINE_MASK_RES = (
    re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]"),
    re.compile(r"^\s*[\[【](?:" + _SECTION_WORDS + r")[^\]】]*[\]】]\s*", re.IGNORECASE),
    re.compile(r"[\(（]\s*[x×]\s*\d+\s*[\)）]|[\(（]\s*\d+\s*[x×]\s*[\)）]", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])[x×]\s*\d+\s*$", re.IGNORECASE),
)


def is_annotation_line(line: str) -> bool:
    """整行為不唱的內容(段落標籤 / credits / metadata)?"""
    return bool(
        _SECTION_LINE_RE.match(line)
        or _BARE_SECTION_LINE_RE.match(line)
        or _SQUARE_LINE_RE.match(line)
        or _CREDIT_LINE_RE.match(line)
    )


def _mask_spans(line: str) -> List[bool]:
    """逐字標記是否遮蔽(不參與對齊)。"""
    if is_annotation_line(line):
        return [True] * len(line)
    mask = [False] * len(line)
    for pattern in _INLINE_MASK_RES:
        for m in pattern.finditer(line):
            for i in range(m.start(), m.end()):
                mask[i] = True
    return mask


@lru_cache(maxsize=1)
def _t2s_converter() -> Optional[Callable[[str], str]]:
    """OpenCC 繁 → 簡轉換器;套件不可用時回 None(退回不轉換,記警告)。"""
    try:
        from opencc import OpenCC  # opencc-python-reimplemented(純 Python)
    except ImportError:
        log.warning("opencc 未安裝,略過繁 → 簡轉換(繁體歌詞對齊品質會下降)")
        return None
    return OpenCC("t2s").convert


@lru_cache(maxsize=8192)
def _t2s_char(ch: str) -> str:
    conv = _t2s_converter()
    if conv is None:
        return ch
    out = conv(ch)
    return out if len(out) == 1 else ch


def _nfkc_char(ch: str) -> str:
    out = unicodedata.normalize("NFKC", ch)
    return out if len(out) == 1 else ch


def normalize_for_alignment(
    line: str,
    language: str,
    t2s: Optional[Callable[[str], str]] = None,
) -> str:
    """產生對齊用文字:長度與 ``line`` 相同、逐字對應(見模組說明)。

    ``t2s`` 為單字元繁 → 簡函式(測試可注入);預設中文用 OpenCC。
    """
    if language == "zh" and t2s is None:
        t2s = _t2s_char
    mask = _mask_spans(line)
    out = []
    for ch, masked in zip(line, mask):
        if masked:
            out.append(MASK_CHAR)
            continue
        c = _nfkc_char(ch)
        if language == "zh" and t2s is not None:
            converted = t2s(c)
            c = converted if len(converted) == 1 else c
        out.append(c)
    return "".join(out)


# ── /align 切長行 ────────────────────────────────────────────────────────
# 中 / 日文的單元:連續英數(混寫的英文字)為一單元,其餘每個非空白字一單元。
_CJK_UNIT_RE = re.compile(r"[A-Za-z0-9'’]+|\S")
_SPACED_UNIT_RE = re.compile(r"\S+")


def _is_punct(text: str) -> bool:
    return all(unicodedata.category(c).startswith("P") or c in BREAK_PUNCT for c in text)


def split_aligned_lines(
    lines: List[str],
    chars: List[dict],
    separator: str,
    gap_seconds: float = LINE_GAP_SECONDS,
    max_chars_cjk: int = LINE_MAX_CHARS_CJK,
    max_words: int = LINE_MAX_WORDS,
    max_seconds: float = LINE_MAX_SECONDS,
) -> Tuple[List[str], List[dict]]:
    """依字級時間把過長的行切短(純函式,可單元測試)。

    ``chars`` 須與 ``separator.join(lines)`` 逐字對應(長度不符則原樣回傳)。
    只切「過長」的行(單元數 > 上限,或時長 > ``max_seconds``);短行即使含標點
    也保留原樣,盡量尊重使用者分行。長行內的斷點(在下一單元之前斷開):
    前一單元以句讀結尾、與前一個有時間的字間隔 ≥ ``gap_seconds``、單元數達上限、
    或行長超過 ``max_seconds``。句讀單元不會成為新行開頭。

    回傳 ``(new_lines, new_chars)``,同樣滿足
    ``len(new_chars) == len(separator.join(new_lines))``,可直接交給
    ``aligner.build_fragments``。
    """
    total = sum(len(l) for l in lines) + len(separator) * max(len(lines) - 1, 0)
    if len(chars) != total:
        return lines, chars

    cjk = separator == ""
    unit_re = _CJK_UNIT_RE if cjk else _SPACED_UNIT_RE
    max_units = max_chars_cjk if cjk else max_words

    new_lines: List[str] = []
    new_chars: List[dict] = []

    def emit(text: str, text_chars: List[dict]) -> None:
        if new_lines and separator:
            new_chars.extend({"char": c, "start": None, "end": None} for c in separator)
        new_lines.append(text)
        new_chars.extend(text_chars)

    pos = 0
    for li, line in enumerate(lines):
        if li > 0:
            pos += len(separator)
        line_chars = chars[pos : pos + len(line)]
        pos += len(line)

        units = []  # (start, end, begin_t, end_t, is_punct)
        for m in unit_re.finditer(line):
            seg = line_chars[m.start() : m.end()]
            starts = [float(c["start"]) for c in seg if c.get("start") is not None]
            ends = [float(c["end"]) for c in seg if c.get("end") is not None]
            units.append(
                (
                    m.start(),
                    m.end(),
                    starts[0] if starts else None,
                    ends[-1] if ends else None,
                    _is_punct(m.group()),
                )
            )

        word_count = sum(1 for u in units if not u[4])
        all_starts = [u[2] for u in units if u[2] is not None]
        all_ends = [u[3] for u in units if u[3] is not None]
        too_long = word_count > max_units or (
            all_starts and all_ends and all_ends[-1] - all_starts[0] > max_seconds
        )
        if not units or not too_long:
            emit(line, line_chars)
            continue

        groups: List[List[tuple]] = []
        current: List[tuple] = []
        cur_words = 0
        line_start: Optional[float] = None
        prev_end: Optional[float] = None
        for u in units:
            _s, _e, u_begin, u_end, punct = u
            if current and not punct:
                prev_text = line[current[-1][0] : current[-1][1]]
                punct_break = prev_text[-1] in BREAK_PUNCT
                gap_break = (
                    u_begin is not None
                    and prev_end is not None
                    and u_begin - prev_end >= gap_seconds
                )
                length_break = cur_words >= max_units
                duration_break = (
                    line_start is not None
                    and u_end is not None
                    and u_end - line_start > max_seconds
                )
                if punct_break or gap_break or length_break or duration_break:
                    groups.append(current)
                    current, cur_words, line_start = [], 0, None
            current.append(u)
            if not punct:
                cur_words += 1
            if line_start is None and u_begin is not None:
                line_start = u_begin
            if u_end is not None:
                prev_end = u_end
        if current:
            groups.append(current)

        for group in groups:
            s, e = group[0][0], group[-1][1]
            emit(line[s:e], line_chars[s:e])

    return new_lines, new_chars
