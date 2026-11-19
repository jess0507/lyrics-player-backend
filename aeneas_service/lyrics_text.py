"""歌詞文字處理(純邏輯,不依賴 aeneas,可獨立單元測試)。

規則移植自 ``whisperx_service/lyrics_text.py`` 與 ``whisperx_service/aligner.py``
(兩個容器刻意各自獨立,故複製一份,不跨目錄 import)。aeneas 只產出「整行」
時間,沒有字級時間,所以只搬「只看文字」的部分:

1. **不唱內容不送 aeneas**:整行註記(段落標籤、credits、LRC metadata)
   由 :func:`is_annotation_line` 挑出;行內的 LRC 時間戳 / 重複記號 / 行首段落
   標籤只從送給 TTS 的文字移除(:func:`tts_text`),顯示仍用原文。
2. **文字系統統計**(:func:`script_units`):供 ``aligner.detect_language``
   依歌詞判斷語言(拉丁以單字計,漢字 / 假名 / 諺文逐字計)。
3. **只看文字的切行**(:func:`split_spans`):句末標點後斷;字數過長才在子句
   標點斷;一行內語系只切換一次時在切換處斷。whisperx 的停頓 / 秒數規則需要
   字級時間,不移植。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

# 遮蔽字元:標記不唱的內容(只用於內部計算,不會出現在顯示或 TTS 文字)。
MASK_CHAR = "\x00"

# ── 不唱內容的偵測(同 whisperx)────────────────────────────────────────
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
# 整行方括號 / 【】:LRC metadata([ar:xxx])或註記。
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


def mask_inline(line: str) -> str:
    """把行內不唱的片段換成 :data:`MASK_CHAR`;長度與原文相同、逐字對應。

    整行註記整行遮蔽。切行在遮蔽後的文字上計算,位置可直接套回原文。
    """
    if is_annotation_line(line):
        return MASK_CHAR * len(line)
    chars = list(line)
    for pattern in _INLINE_MASK_RES:
        for m in pattern.finditer(line):
            for i in range(m.start(), m.end()):
                chars[i] = MASK_CHAR
    return "".join(chars)


_WHITESPACE_RE = re.compile(r"\s+")


def tts_text(masked: str) -> str:
    """遮蔽後的文字 → 送給 aeneas(espeak TTS)的文字。

    遮蔽處換成空白、NFKC(全形英數 → 半形)、壓縮連續空白。只用於 TTS,
    長度可與原文不同(顯示文字不受影響)。
    """
    text = masked.replace(MASK_CHAR, " ")
    text = unicodedata.normalize("NFKC", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def has_speech(text: str) -> bool:
    """文字裡是否有可唸的內容(至少一個字母或數字);純標點 / 空白不算。"""
    return any(ch.isalnum() for ch in text)


# ── 文字系統統計(同 whisperx aligner._script_units)────────────────────
def classify_char(ch: str) -> Optional[str]:
    """字元 → 文字系統類別(hangul / kana / han / latin);非字母回 None。"""
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "kana"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
        return "han"
    if ch.isalpha() and (o < 0x0250):  # 基本 + 擴充拉丁
        return "latin"
    return None


# 拉丁單字內部可出現、不切斷單字的連接符(如 "t'aime"、"rock-n-roll")。
_LATIN_WORD_JOINERS = {"'", "’", "-"}


def script_units(lines: List[str]) -> Dict[str, int]:
    """各文字系統的**發音單元**數。

    諺文 / 假名 / 漢字逐字計;拉丁字母以**單字**計(連續字母算一個,內部的
    ``'`` / ``-`` 不切斷)。各行分開掃描,避免前行行尾與下行行首黏成同一字。
    """
    counts = {"hangul": 0, "kana": 0, "han": 0, "latin": 0}
    for line in lines:
        in_word = False
        for ch in line:
            kind = classify_char(ch)
            if kind == "latin":
                if not in_word:
                    counts["latin"] += 1
                    in_word = True
                continue
            if in_word and ch in _LATIN_WORD_JOINERS:
                continue
            in_word = False
            if kind is not None:
                counts[kind] += 1
    return counts


# ── 只看文字的切行(同 whisperx 的 _break_kind / _is_abbreviation / _script_cut)──
# 單元:連續英數為一單元,其餘每個非空白字一單元(遮蔽字元也是一單元,語系中性)。
_UNIT_RE = re.compile(r"[A-Za-z0-9'’]+|\S")

# 句末標點:行內出現即斷行。"." 另判(排除 "..."、小數點與縮寫)。
SENTENCE_END_PUNCT = set("。．｡！？!?‼⁇⁈⁉")
# 子句標點:只有行(句)過長時才在此斷行。頓號「、」是列舉,不斷。
CLAUSE_PUNCT = set("，；：…—～,;:~")
BREAK_PUNCT = set("，。、！？；：…—～,.!?;:~")


def _is_punct(text: str) -> bool:
    return all(unicodedata.category(c).startswith("P") or c in BREAK_PUNCT for c in text)


# 以句點結尾但不是句末的英文縮寫。比對時不分大小寫;不收 "no" / "co" 等
# 歌詞裡常見於句尾的字("I said no.")。
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc",
    "e.g", "i.e", "vol", "fig", "inc", "ltd", "corp", "u.s", "u.k",
}
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
        if end < len(line) and line[end] == ".":
            return "clause"  # "..." 的第一個點
        if end < len(line) and line[end].isdigit():
            return None  # 小數點 "3.5"
        if _is_abbreviation(line, end - 1):
            return None  # "Mr." / "Dr." / "U.S."
        return "sentence"
    if ch in CLAUSE_PUNCT:
        return "clause"
    return None


def _unit_script(text: str) -> Optional[str]:
    """單元的語系:``"latin"`` / ``"other"``;數字、標點、遮蔽字元為 None(中性)。"""
    for ch in text:
        if ch.isalpha():
            return "latin" if "LATIN" in unicodedata.name(ch, "") else "other"
    return None


def _script_cut(text: str) -> Optional[Tuple[int, int]]:
    """``text`` 若剛好是「一段拉丁 + 一段非拉丁」(語系只切換一次),回傳
    ``(第一段結尾, 第二段開頭)`` 的字元位置;否則 None。

    只處理行首 / 行尾的外文("Apple蘋果"、"Absorb,吸收."、"我愛你baby");
    句中夾外文("給我一個kiss吧" 切換兩次)不動。中性單元(數字、標點)跟前段。
    """
    runs = 0
    prev_script: Optional[str] = None
    last_end = 0
    cut: Optional[Tuple[int, int]] = None
    for m in _UNIT_RE.finditer(text):
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


# 一行長度上限(同 whisperx 預設):超過才在子句標點斷。中 / 日文以字計,
# 其他語言(含韓文,以空格分詞)以詞計。
LINE_MAX_CHARS_CJK = int(os.environ.get("LYRICS_LINE_MAX_CHARS_CJK", "14"))
LINE_MAX_WORDS = int(os.environ.get("LYRICS_LINE_MAX_WORDS", "10"))
_SPACED_UNIT_RE = re.compile(r"\S+")


def _is_cjk(text: str) -> bool:
    """漢字 / 假名為主(無空格分詞的語言)?"""
    counts = script_units([text])
    return counts["han"] + counts["kana"] > counts["hangul"] + counts["latin"]


def line_length(text: str) -> int:
    """一段文字的長度(切長行用):漢字 / 假名為主 → 字數(連續英數算一個),
    否則 → 以空格分的詞數。標點、遮蔽字元不計。"""
    unit_re = _UNIT_RE if _is_cjk(text) else _SPACED_UNIT_RE
    return sum(1 for m in unit_re.finditer(text) if any(ch.isalnum() for ch in m.group()))


def split_spans(
    masked: str,
    max_chars_cjk: int = LINE_MAX_CHARS_CJK,
    max_words: int = LINE_MAX_WORDS,
) -> List[Tuple[int, int]]:
    """在遮蔽後的文字上算出切行範圍 ``[(start, end), ...]``(套回原文用)。

    1. 句末標點(。！？!? 等及句點;不含 ``...``、小數點、縮寫)之後必斷。
    2. 斷完的句子若仍過長(見 :func:`line_length`;中 / 日文 > ``max_chars_cjk``
       字,其他 > ``max_words`` 詞),才在**子句標點**(，；：… 等)處斷,且以
       片段為單位貪婪合併:放得下就併在同一行。沒有子句標點就保留長行。
    3. 每段若是「一段拉丁 + 一段非拉丁」,在切換處斷開(見 :func:`_script_cut`)。
    4. 標點跟前段,不會成為新行開頭。

    範圍可能含前後空白,由呼叫端修整;不需切時回傳單一範圍 ``[(0, len)]``。
    """
    if not masked:
        return [(0, 0)]
    # 片段:[start, 開頭斷點強度];"hard" = 句末必斷,"soft" = 子句標點(過長才斷)。
    pieces: List[list] = [[0, None]]
    pending: Optional[str] = None
    seen_unit = False
    for m in _UNIT_RE.finditer(masked):
        punct = _is_punct(m.group())
        if seen_unit and not punct and pending is not None:
            pieces.append([m.start(), "hard" if pending == "sentence" else "soft"])
            pending = None
        seen_unit = True
        kind = _break_kind(masked, m.end()) if punct else None
        if kind is not None:
            pending = kind
        elif not punct:
            pending = None
    bounds = [p[0] for p in pieces] + [len(masked)]
    ranges = [(bounds[i], bounds[i + 1], pieces[i][1]) for i in range(len(pieces))]

    def too_long(s: int, e: int) -> bool:
        text = masked[s:e]
        return line_length(text) > (max_chars_cjk if _is_cjk(text) else max_words)

    # 先在必斷點分句。
    sentences: List[List[Tuple[int, int, Optional[str]]]] = []
    for r in ranges:
        if not sentences or r[2] == "hard":
            sentences.append([])
        sentences[-1].append(r)

    groups: List[Tuple[int, int]] = []
    for sent in sentences:
        if not too_long(sent[0][0], sent[-1][1]):
            groups.append((sent[0][0], sent[-1][1]))
            continue
        # 過長:以片段為單位貪婪合併,放不下才在子句標點處斷。
        g_start: Optional[int] = None
        g_end = 0
        for s, e, _ in sent:
            if g_start is not None and too_long(g_start, e):
                groups.append((g_start, g_end))
                g_start = None
            if g_start is None:
                g_start = s
            g_end = e
        if g_start is not None:
            groups.append((g_start, g_end))

    spans: List[Tuple[int, int]] = []
    for s, e in groups:
        cut = _script_cut(masked[s:e])
        if cut is None:
            spans.append((s, e))
        else:
            spans.append((s, s + cut[0]))
            spans.append((s + cut[1], e))
    return spans


# ── 顯示行 ↔ 對齊行 ─────────────────────────────────────────────────────
def plan_lines(lines: List[str], split: bool = True) -> Tuple[List[dict], List[str]]:
    """把使用者歌詞行拆成「顯示行」與「送進 aeneas 的對齊行」。

    回傳 ``(rows, align_texts)``:

    - ``rows``:每個顯示行 ``{"text": 顯示文字, "align": 對齊行索引或 None}``,
      依輸出順序排列。``align`` 為 None 表示不送 aeneas(整行註記,或沒有可唸
      內容的行),時間由呼叫端沿用前一行。
    - ``align_texts``:送進 aeneas 的 TTS 文字,``align_texts[row["align"]]``
      即該顯示行的對齊文字;每個元素都有可唸內容。

    空行先被濾掉;``split=False`` 時不切行(仍會移除註記與不唱內容)。
    切出的片段若沒有可唸內容(如行尾的 ``(x2)``),併回前一段(行首則併入後一段),
    不會單獨成為一行。
    """
    rows: List[dict] = []
    align_texts: List[str] = []
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        if is_annotation_line(line):
            rows.append({"text": line, "align": None})
            continue
        masked = mask_inline(line)
        spans = split_spans(masked) if split else [(0, len(line))]
        # 沒有可唸內容的片段併入相鄰片段。
        merged: List[List[int]] = []
        carry_start: Optional[int] = None
        for s, e in spans:
            speech = has_speech(tts_text(masked[s:e]))
            if not speech:
                if merged:
                    merged[-1][1] = e
                elif carry_start is None:
                    carry_start = s
                continue
            if carry_start is not None:
                s, carry_start = carry_start, None
            merged.append([s, e])
        if not merged:
            rows.append({"text": line, "align": None})
            continue
        for s, e in merged:
            text = line[s:e].strip()
            if not text:
                continue
            rows.append({"text": text, "align": len(align_texts)})
            align_texts.append(tts_text(masked[s:e]))
    return rows, align_texts
