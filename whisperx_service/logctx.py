"""統一 log 設定:為每一行 log 自動加上「請求 ID + 端點」tag。

問題:同一次請求會跨多個模組(``main`` / ``transcriber`` / ``aligner``)各自輸出
log;在 GCP Logs Explorer 裡這些行會與其他請求交錯,難以判斷某行屬於哪一次請求、
哪個端點、哪個階段。

解法:用 :class:`contextvars.ContextVar` 在請求開始時綁定一個短 ID 與端點,透過
:class:`logging.Filter` 把它注入**每一筆** log record。如此所有子模組的 log 不必逐行
修改,就會自動帶上同一個 tag。每個請求(每條執行緒)各有獨立 context,tag 不會互相污染。

GCP Logs Explorer 用法(見模組底部 README 對照):以該請求 ID 字串搜尋,即可撈出
單次請求從「開始 → 下載 → 模型載入 → 轉寫 → 對齊 → 完成」的完整生命週期。
"""

from __future__ import annotations

import contextvars
import logging
import uuid

# 預設 "-" 代表「非請求情境」的 log(例如服務啟動、模型預載)。
_request_tag: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_tag", default="-"
)


def bind_request(endpoint: str) -> str:
    """在請求開始時呼叫:產生短 ID 並綁定到目前 context,回傳該 ID。

    之後同一請求(同一執行緒)內所有 log 都會自動帶上 ``<id> <endpoint>`` tag。
    """
    req_id = uuid.uuid4().hex[:8]
    _request_tag.set(f"{req_id} {endpoint}")
    return req_id


class _TagFilter(logging.Filter):
    """把目前 context 的請求 tag 注入 record,供 formatter 取用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.tag = _request_tag.get()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """設定 root logger:輸出格式為 ``LEVEL [<id> <端點>] <模組>: <訊息>``。

    取代 ``logging.basicConfig``;對 root 設定,故 ``main`` / ``transcriber`` /
    ``aligner`` 等所有 logger 都會套用同一格式與 tag。
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(levelname)s [%(tag)s] %(name)s: %(message)s")
    )
    handler.addFilter(_TagFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)
