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

2. **切行**(:func:`split_aligned_lines`,``/align`` 與 ``/transcribe`` 共用同一套
   規則):避免一個時間點顯示一大段歌詞。
   - 原有的換行(``/align`` 的使用者分行、``/transcribe`` 的 ASR 段)一律保留。
   - 句末標點(。！？. 等)必斷;停頓 ≥ :data:`LINE_LONG_GAP_SECONDS` 必斷(不論長短)。
   - 句子過長才在子句標點(，；：…)或停頓 ≥ :data:`LINE_GAP_SECONDS` 處斷;
     沒有標點也沒有停頓的地方不硬切。
   - 一段裡語系只切換一次(行首 / 行尾的外文,如 "Apple 蘋果")時在切換處斷。
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
# 過長的句子:相鄰兩字(詞)間隔 ≥ 此秒數的停頓可作為斷點(與子句標點同級)。
LINE_GAP_SECONDS = float(os.environ.get("LYRICS_LINE_GAP_SECONDS", "0.6"))
# 停頓 ≥ 此秒數一律斷行,不論句子長短(與句末標點同級)。
LINE_LONG_GAP_SECONDS = float(os.environ.get("LYRICS_LINE_LONG_GAP_SECONDS", "1.5"))
# 中 / 日文一行最多字數(無空格語言以字計),超過才找斷點。
LINE_MAX_CHARS_CJK = int(os.environ.get("LYRICS_LINE_MAX_CHARS_CJK", "14"))
# 其他語言一行最多詞數(以空格分詞計),超過才找斷點。
LINE_MAX_WORDS = int(os.environ.get("LYRICS_LINE_MAX_WORDS", "10"))
# 一行最長秒數(首字 start → 末字 end),超過才找斷點(子句標點 / 停頓)。
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
    mask_annotations: bool = True,
) -> str:
    """產生對齊用文字:長度與 ``line`` 相同、逐字對應(見模組說明)。

    ``t2s`` 為單字元繁 → 簡函式(測試可注入);預設中文用 OpenCC。
    ``mask_annotations=False`` 時不遮蔽段落標籤 / credits(``/transcribe`` 用:
    ASR 不會產生這類標記,且對齊結果的文字會直接拿去顯示,不能留遮蔽字元)。
    """
    if language == "zh" and t2s is None:
        t2s = _t2s_char
    mask = _mask_spans(line) if mask_annotations else [False] * len(line)
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


# ── 輸出文字的繁簡 ────────────────────────────────────────────────────────
SCRIPT_TRADITIONAL = "traditional"
SCRIPT_SIMPLIFIED = "simplified"

# 語言提示(BCP-47,小寫、底線換連字號)→ 輸出字形。
_TRADITIONAL_HINTS = {"zh-tw", "zh-hk", "zh-mo", "zh-hant", "yue"}
_SIMPLIFIED_HINTS = {"zh-cn", "zh-sg", "zh-hans"}


def output_script_for(language_hint: Optional[str]) -> Optional[str]:
    """由 client 語言提示決定中文輸出字形;無法判定回 None(不轉換)。

    ``zh-TW`` / ``zh-HK`` / ``zh-Hant-*`` / ``yue`` → 繁體;``zh-CN`` / ``zh-Hans-*``
    → 簡體;``zh`` 或其他語言 → None。
    """
    if not language_hint:
        return None
    key = language_hint.strip().lower().replace("_", "-")
    for hints, script in (
        (_TRADITIONAL_HINTS, SCRIPT_TRADITIONAL),
        (_SIMPLIFIED_HINTS, SCRIPT_SIMPLIFIED),
    ):
        if key in hints or any(key.startswith(h + "-") for h in hints):
            return script
    return None


@lru_cache(maxsize=2)
def _script_converter(config: str) -> Optional[Callable[[str], str]]:
    try:
        from opencc import OpenCC
    except ImportError:
        log.warning("opencc 未安裝,略過輸出文字繁簡轉換")
        return None
    return OpenCC(config).convert


def convert_script(text: str, script: Optional[str]) -> str:
    """把整行文字轉成指定字形(``s2tw`` / ``t2s``);script 為 None 原樣回傳。

    整行轉換而非逐字:簡 → 繁一對多(发 → 發 / 髮、干 → 乾 / 幹),OpenCC
    需要上下文詞組才能選對。轉換後長度可能改變,只可用在已對完時間的顯示文字。
    """
    if not script or not text:
        return text
    config = "s2tw" if script == SCRIPT_TRADITIONAL else "t2s"
    conv = _script_converter(config)
    return conv(text) if conv is not None else text


# ── 切行(/align、/transcribe 共用)────────────────────────────────────────────────────────
# 中 / 日文的單元:連續英數(混寫的英文字)為一單元,其餘每個非空白字一單元。
_CJK_UNIT_RE = re.compile(r"[A-Za-z0-9'’]+|\S")
_SPACED_UNIT_RE = re.compile(r"\S+")

# 句末標點:行內出現即斷行(不論行長)。"." 另判(排除 "..." 與小數點)。
SENTENCE_END_PUNCT = set("。．｡！？!?‼⁇⁈⁉")
# 子句標點:只有行(句)過長時才在此斷行。頓號「、」是列舉,不斷。
CLAUSE_PUNCT = set("，；：…—～,;:~")


def _is_punct(text: str) -> bool:
    return all(unicodedata.category(c).startswith("P") or c in BREAK_PUNCT for c in text)


# 以句點結尾但不是句末的英文縮寫("Mr. Jackson"、"Dr. Smith")。比對時不分大小寫。
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc",
    "e.g", "i.e", "vol", "fig", "inc", "ltd", "corp", "u.s", "u.k",
}  # 不收 "no" / "co" 等歌詞裡常見於句尾的字("I said no.")
_WORD_BEFORE_DOT_RE = re.compile(r"([A-Za-z](?:[A-Za-z.]*[A-Za-z])?)$")


def _is_abbreviation(line: str, dot: int) -> bool:
    """``line[dot]`` 的句點是否屬於縮寫(``Mr.``、``U.S.``)或單一字母縮寫(``J.``)。"""
    m = _WORD_BEFORE_DOT_RE.search(line[:dot])
    if not m:
        return False
    word = m.group(1)
    # 單一大寫字母視為名字縮寫("J. K."),但 "I" / "A" 常是句尾("so am I.")。
    return word.lower() in _ABBREVIATIONS or (
        len(word) == 1 and word.isupper() and word not in ("I", "A")
    )


def _break_kind(line: str, end: int) -> Optional[str]:
    """``line[:end]`` 結尾的斷點種類:``"sentence"`` / ``"clause"`` / None。"""
    ch = line[end - 1]
    if ch in SENTENCE_END_PUNCT:
        return "sentence"
    if ch == ".":
        if end >= 2 and line[end - 2] == ".":
            return "clause"  # "..." 視同刪節號
        if end < len(line) and line[end].isdigit():
            return None  # 小數點 "3.5"
        if _is_abbreviation(line, end - 1):
            return None  # "Mr." / "Dr." / "U.S."
        return "sentence"
    if ch in CLAUSE_PUNCT:
        return "clause"
    return None


def _unit_script(text: str) -> Optional[str]:
    """單元的語系:``"latin"`` / ``"other"``(漢字、假名、韓文…);數字、標點、
    遮蔽字元為 None(中性,不算語系切換)。"""
    for ch in text:
        if ch.isalpha():
            return "latin" if "LATIN" in unicodedata.name(ch, "") else "other"
    return None


def _script_cut(text: str, unit_re: "re.Pattern[str]" = None) -> Optional[Tuple[int, int]]:
    """``text`` 若剛好是「一段拉丁 + 一段非拉丁」(語系只切換一次),回傳
    ``(第一段結尾, 第二段開頭)`` 的字元位置;否則 None。

    只處理行首 / 行尾的外文(如 "Apple蘋果"、"Baby 我想你"、"我愛你baby");
    句中夾外文("給我一個kiss吧" 切換兩次)不動。中性單元(數字、標點)跟著前段。

    一律以字元級單元(拉丁英數串 / 單一其他字元)判斷,不用呼叫端的分詞:有空格
    語言裡沒空格隔開的混寫("Absorb,吸收.")也能在切換處斷開。``unit_re`` 參數
    僅為相容保留,不使用。
    """
    unit_re = _CJK_UNIT_RE
    runs = 0
    prev_script: Optional[str] = None
    last_end = 0
    cut: Optional[Tuple[int, int]] = None
    for m in unit_re.finditer(text):
        script = _unit_script(m.group())
        if script is not None and script != prev_script:
            runs += 1
            if runs == 2:
                cut = (last_end, m.start())
            elif runs > 2:
                return None
            prev_script = script
        last_end = m.end()
    return cut


def split_aligned_lines(
    lines: List[str],
    chars: List[dict],
    separator: str,
    max_chars_cjk: int = LINE_MAX_CHARS_CJK,
    max_words: int = LINE_MAX_WORDS,
    max_seconds: float = LINE_MAX_SECONDS,
    gap_seconds: float = LINE_GAP_SECONDS,
    long_gap_seconds: float = LINE_LONG_GAP_SECONDS,
) -> Tuple[List[str], List[dict]]:
    """依標點 / 停頓 / 語系把行切短(純函式,可單元測試)。

    ``chars`` 須與 ``separator.join(lines)`` 逐字對應(長度不符則原樣回傳);
    停頓 = 下一單元首字 start − 前一個有時間的字 end(無時間的字不算)。

    1. 輸入的每一行本身就是邊界,不會跨行合併。
    2. **必斷**:句末標點(。．！？!? 等及句點)之後;停頓 ≥ ``long_gap_seconds``。
    3. 斷完的句子若仍過長(單元數 > 上限,或時長 > ``max_seconds``),才在
       **子句標點**(，；：… 等)或停頓 ≥ ``gap_seconds`` 處斷,且以片段為單位
       貪婪合併:放得下就併在同一行,避免切成一堆碎行。沒有這類斷點就保留長行,
       不在詞中間硬切。
    4. 斷完的每段若是「一段拉丁 + 一段非拉丁」(語系只切換一次,如
       "Apple蘋果"),在切換處斷開;句中夾外文不斷(見 :func:`_script_cut`)。
    5. 標點跟著前一段,不會成為新行開頭。

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

    def too_long(pieces: List[list]) -> bool:
        words = sum(p[2] for p in pieces)
        begins = [p[3] for p in pieces if p[3] is not None]
        ends = [p[4] for p in pieces if p[4] is not None]
        return words > max_units or (
            bool(begins) and bool(ends) and ends[-1] - begins[0] > max_seconds
        )

    pos = 0
    for li, line in enumerate(lines):
        if li > 0:
            pos += len(separator)
        line_chars = chars[pos : pos + len(line)]
        pos += len(line)

        # 片段:[start, end, words, begin_t, end_t, 開頭斷點強度]。在每個可能的斷點
        # (前一單元以標點結尾,或與前一個有時間的字有停頓)之前開新片段;
        # 強度 "hard" = 必斷,"soft" = 過長才斷。標點單元併入前一片段。
        pieces: List[list] = []
        prev_end: Optional[float] = None
        pending: Optional[str] = None  # 前一單元結尾的標點斷點種類
        for m in unit_re.finditer(line):
            seg = line_chars[m.start() : m.end()]
            starts = [float(c["start"]) for c in seg if c.get("start") is not None]
            ends = [float(c["end"]) for c in seg if c.get("end") is not None]
            punct = _is_punct(m.group())
            strength: Optional[str] = None
            if pieces and not punct:
                gap = (
                    starts[0] - prev_end
                    if starts and prev_end is not None
                    else None
                )
                if pending == "sentence" or (gap is not None and gap >= long_gap_seconds):
                    strength = "hard"
                elif pending == "clause" or (gap is not None and gap >= gap_seconds):
                    strength = "soft"
            if not pieces or strength is not None:
                pieces.append([m.start(), m.end(), 0, None, None, strength])
            cur = pieces[-1]
            cur[1] = m.end()
            if not punct:
                cur[2] += 1
            if cur[3] is None and starts:
                cur[3] = starts[0]
            if ends:
                cur[4] = ends[-1]
                prev_end = ends[-1]
            kind = _break_kind(line, m.end())
            if kind is not None:
                pending = kind
            elif not punct:
                pending = None

        if not pieces:
            emit(line, line_chars)
            continue

        # 先在必斷點分句。
        sentences: List[List[list]] = []
        for p in pieces:
            if not sentences or p[5] == "hard":
                sentences.append([])
            sentences[-1].append(p)

        groups: List[List[list]] = []
        for sent in sentences:
            if not too_long(sent):
                groups.append(sent)
                continue
            # 過長:以片段為單位貪婪合併,放不下才在片段邊界(子句標點 / 停頓)斷。
            group: List[list] = []
            for p in sent:
                if group and too_long(group + [p]):
                    groups.append(group)
                    group = []
                group.append(p)
            if group:
                groups.append(group)

        if len(groups) == 1 and _script_cut(line, unit_re) is None:
            emit(line, line_chars)  # 不切:原樣保留(含行首尾空白)
            continue
        for group in groups:
            s, e = group[0][0], group[-1][1]
            cut = _script_cut(line[s:e], unit_re)
            if cut is None:
                emit(line[s:e], line_chars[s:e])
            else:
                a, b = s + cut[0], s + cut[1]
                emit(line[s:a], line_chars[s:a])
                emit(line[b:e], line_chars[b:e])

    return new_lines, new_chars
