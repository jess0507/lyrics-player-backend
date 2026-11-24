"""aeneas forced alignment 核心:既有純文字 + 音訊 → 每行起始時間。

本模組封裝 aeneas 呼叫,對外只暴露 :func:`align`。aeneas 依賴 espeak(g2p)
與 ffmpeg(音訊解碼),兩者由 Docker 映像安裝;import aeneas 故延後到實際
對齊時,讓 LRC 等純邏輯與單元測試不必載入這些重依賴。
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import List

from lrc import build_lrc
from lyrics_text import plan_lines, script_units

log = logging.getLogger(__name__)

# 對齊前是否依句末標點 / 子句標點自動切行(使用者原文的分行會因此改變)。
ALIGN_SPLIT_LINES = os.environ.get("ALIGN_SPLIT_LINES", "1") not in ("0", "false", "False")


class AlignmentError(Exception):
    """對齊失敗(輸入不合法,或 aeneas 無法產出可用結果)。"""


# 客戶端可能送 BCP-47 / 二字母碼;aeneas 用 ISO 639-3。做寬鬆對應,
# 未知碼原樣傳給 aeneas(其本身也接受部分變體)。
_LANGUAGE_MAP = {
    "zh": "cmn",
    "zh-hant": "cmn",
    "zh-hans": "cmn",
    "zh-tw": "cmn",
    "zh-cn": "cmn",
    "cmn": "cmn",
    "yue": "yue",
    "en": "eng",
    "eng": "eng",
    "ja": "jpn",
    "jpn": "jpn",
    "ko": "kor",
    "kor": "kor",
    "es": "spa",
    "fr": "fra",
    "de": "deu",
    "it": "ita",
    "pt": "por",
    "ru": "rus",
}


def normalize_language(code: str) -> str:
    """把客戶端語言碼正規化為 aeneas 慣用的 ISO 639-3。"""
    if not code:
        return "eng"
    return _LANGUAGE_MAP.get(code.strip().lower().replace("_", "-"), code)


# 以拉丁字母書寫的語言(aeneas 碼):歌詞為拉丁字母時,client 的語言提示落在
# 此集合才可信;否則(如 zh-TW 介面配英文歌)改用 eng。
_LATIN_SCRIPT_LANGS = {
    "eng", "fra", "deu", "spa", "ita", "por", "nld", "ces", "pol", "hun",
    "fin", "tur", "dan", "vie", "cat", "ron", "swe", "nor", "nob",
}
# 漢字為主時可沿用的提示(粵語歌用 yue 的 espeak 發音);其餘一律 cmn。
_HAN_LANGS = {"cmn", "yue"}


def detect_language(lines: List[str], hint: str) -> str:
    """由歌詞的文字系統推定 aeneas 語言碼(ISO 639-3);提示只在不衝突時採用。

    App 送來的 ``language`` 是 **UI locale**,與歌曲語言經常不一致(例:zh-TW
    介面放韓文歌,若照用就會以 cmn TTS 唸韓文)。規則同 whisperx:
    - 諺文佔比 ≥ 30% → kor;假名 ≥ 10% → jpn(先於漢字,日文漢字假名混寫);
      漢字 ≥ 30% → cmn(提示為 yue 則沿用);拉丁 ≥ 50% → 提示屬拉丁語系則
      沿用,否則 eng。
    - 佔比以發音單元計(拉丁一個單字算一單元,見 ``lyrics_text.script_units``)。
    - 無法判定(純數字 / 標點,或沒有明顯多數)→ 正規化後的提示。
    """
    normalized_hint = normalize_language(hint)
    counts = script_units(lines)
    letters = sum(counts.values())
    if letters == 0:
        return normalized_hint
    if counts["hangul"] / letters >= 0.3:
        return "kor"
    if counts["kana"] / letters >= 0.1:
        return "jpn"
    if counts["han"] / letters >= 0.3:
        return normalized_hint if normalized_hint in _HAN_LANGS else "cmn"
    if counts["latin"] / letters >= 0.5:
        return normalized_hint if normalized_hint in _LATIN_SCRIPT_LANGS else "eng"
    return normalized_hint


def assemble_fragments(rows: List[dict], aligned: List[dict]) -> List[dict]:
    """把對齊行的片段依顯示行組回逐行片段(純函式,可單元測試)。

    ``rows`` 為 ``lyrics_text.plan_lines`` 的顯示行;``aligned`` 為 aeneas 對
    「送進去的行」產出的片段(數量須等於對齊行數,由呼叫端檢查)。
    沒送 aeneas 的顯示行(註記等)時間沿用前一行的起點(第一行則 0),
    ``end`` 與 ``begin`` 相同,並標 ``aligned: False``。``text`` 一律為顯示原文。
    """
    out: List[dict] = []
    prev_begin = 0.0
    for row in rows:
        idx = row.get("align")
        if idx is None:
            begin = end = prev_begin
            is_aligned = False
        else:
            frag = aligned[idx]
            begin = float(frag["begin"])
            end = float(frag["end"])
            is_aligned = True
        out.append(
            {
                "index": len(out),
                "begin": begin,
                "end": end,
                "text": row["text"],
                "aligned": is_aligned,
            }
        )
        prev_begin = begin
    return out


def align(lines: List[str], audio_path: str, language: str) -> dict:
    """對齊既有歌詞行與音訊,回傳 LRC 與逐行片段。

    參數:
        lines: 純文字歌詞(每元素一行,顯示用文字;空行會先被濾掉)。
        audio_path: 本機音訊檔路徑(任何 ffmpeg 解得的格式)。
        language: 客戶端語言碼(多為 App UI locale),只作為語言偵測的提示。

    流程:``plan_lines`` 把使用者行拆成「顯示行」與「送進 aeneas 的對齊行」
    (移除整行註記、行內不唱內容只從 TTS 文字移除、可選切行)→ 依對齊行的
    文字系統偵測語言 → aeneas 對齊 → 依顯示行組回 LRC。

    回傳 dict:``{"lrc": str, "fragments": [...], "language": str,
    "durationSeconds": float}``(``fragments`` 與 LRC 行一一對應,
    ``durationSeconds`` 供呼叫端累加每月配額用量,不對外回傳)。
    失敗丟 :class:`AlignmentError`。
    """
    rows, align_texts = plan_lines(lines, split=ALIGN_SPLIT_LINES)
    if not rows:
        raise AlignmentError("lines 去空行後為空,無可對齊內容")
    if not align_texts:
        raise AlignmentError("lines 全為註記(段落標籤 / credits 等),無可對齊的歌詞")
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise AlignmentError("音訊檔不存在或為空")

    lang = detect_language(align_texts, language)
    hinted = normalize_language(language)
    if lang != hinted:
        log.info("依歌詞文字系統改用語言 %s(client 提示 %r → %s)", lang, language, hinted)
    log.info(
        "對齊行數 %d(顯示行 %d,註記 / 不唱 %d)",
        len(align_texts),
        len(rows),
        sum(1 for r in rows if r["align"] is None),
    )

    aligned = _run_aeneas(align_texts, audio_path, lang)

    if len(aligned) != len(align_texts):
        # aeneas 正常會 1:1 對應;數量不符代表對齊異常,寧可失敗也不塞錯時間。
        raise AlignmentError(
            f"對齊片段數({len(aligned)})與送入的歌詞行數({len(align_texts)})不符"
        )

    fragments = assemble_fragments(rows, aligned)

    # aeneas 不像 whisperx 會解碼整段音訊,沒有現成的音檔總長度;以最後一個
    # 對齊片段的結束時間作為近似值(供每月配額用量計費,只需大致成正比)。
    duration = aligned[-1]["end"] if aligned else 0.0

    return {
        "lrc": build_lrc(fragments),
        "fragments": fragments,
        "language": lang,
        "durationSeconds": duration,
    }


def _run_aeneas(lines: List[str], audio_path: str, language: str) -> List[dict]:
    """實際呼叫 aeneas;回傳 ``[{index, begin, end, text}, ...]``(秒)。"""
    # 延後 import:讓 lrc / 測試不必載入 aeneas 與其原生依賴。
    from aeneas.executetask import ExecuteTask
    from aeneas.task import Task

    text_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            # plain 文字型:一行一 fragment。
            f.write("\n".join(lines))
            text_path = f.name

        config = (
            f"task_language={language}|"
            "is_text_type=plain|"
            "os_task_file_format=json"
        )
        task = Task(config_string=config)
        task.audio_file_path_absolute = audio_path
        task.text_file_path_absolute = text_path

        try:
            ExecuteTask(task).execute()
        except Exception as exc:  # aeneas 內部例外型別繁多,統一轉成對齊失敗
            log.exception("aeneas execute 失敗")
            raise AlignmentError(f"aeneas 對齊失敗:{exc}") from exc

        fragments = []
        for leaf in task.sync_map_leaves():
            # 只取一般片段(去掉 HEAD / TAIL 等非歌詞片段)。
            if not getattr(leaf, "is_regular", False):
                continue
            fragments.append(
                {
                    "index": len(fragments),
                    "begin": float(leaf.begin),
                    "end": float(leaf.end),
                    "text": leaf.text or "",
                }
            )
        return fragments
    finally:
        if text_path and os.path.exists(text_path):
            os.remove(text_path)
