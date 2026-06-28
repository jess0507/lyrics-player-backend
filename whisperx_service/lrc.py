"""LRC 組裝(純函式,不依賴 whisperx,可獨立單元測試)。

對齊結果以「秒(浮點)」表示每行起始時間,本模組把它格式化為 Seek Player 既有
parser 認得的標準 LRC(`[mm:ss.xx]` 每行,百分之一秒)。與
`lib/features/lyrics/lyrics_parser.dart` 的小數秒慣例一致(2 位 → 百分秒)。

> 與 `aeneas_service/lrc.py` 內容一致(兩條對齊路線共用同一 LRC 慣例),刻意各自
> 成檔以維持兩個容器互相獨立、可分別部署。
"""

from __future__ import annotations

from typing import Iterable, Mapping


def format_timestamp(seconds: float) -> str:
    """秒 → ``[mm:ss.xx]``(百分之一秒,分鐘不補零上限,長曲亦正確)。"""
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_centis = int(round(seconds * 100))
    minutes, rem = divmod(total_centis, 60 * 100)
    secs, centis = divmod(rem, 100)
    return f"[{minutes:02d}:{secs:02d}.{centis:02d}]"


def build_lrc(fragments: Iterable[Mapping[str, object]]) -> str:
    """把對齊片段(含 ``begin`` 秒與 ``text``)組成 LRC 字串。

    每行:``[mm:ss.xx]`` + 該行文字。空白文字會被去除前後空白後輸出
    (不丟行,維持與輸入行一一對應)。
    """
    out = []
    for frag in fragments:
        begin = float(frag.get("begin") or 0.0)
        text = str(frag.get("text") or "").strip()
        out.append(f"{format_timestamp(begin)}{text}")
    return "\n".join(out)
