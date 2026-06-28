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

log = logging.getLogger(__name__)


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


def _clean_lines(lines: List[str]) -> List[str]:
    """去除空白行並修整;對齊片段與回傳結果都以此清單為準。"""
    return [s.strip() for s in lines if s and s.strip()]


def align(lines: List[str], audio_path: str, language: str) -> dict:
    """對齊既有歌詞行與音訊,回傳 LRC 與逐行片段。

    參數:
        lines: 純文字歌詞(每元素一行,顯示用文字;空行會先被濾掉)。
        audio_path: 本機音訊檔路徑(任何 ffmpeg 解得的格式)。
        language: 客戶端語言碼(會經 :func:`normalize_language` 正規化)。

    回傳 dict:``{"lrc": str, "fragments": [...], "language": str}``。
    失敗丟 :class:`AlignmentError`。
    """
    clean = _clean_lines(lines)
    if not clean:
        raise AlignmentError("lines 去空行後為空,無可對齊內容")
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise AlignmentError("音訊檔不存在或為空")

    lang = normalize_language(language)
    fragments = _run_aeneas(clean, audio_path, lang)

    if len(fragments) != len(clean):
        # aeneas 正常會 1:1 對應;數量不符代表對齊異常,寧可失敗也不塞錯時間。
        raise AlignmentError(
            f"對齊片段數({len(fragments)})與歌詞行數({len(clean)})不符"
        )

    return {
        "lrc": build_lrc(fragments),
        "fragments": fragments,
        "language": lang,
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
