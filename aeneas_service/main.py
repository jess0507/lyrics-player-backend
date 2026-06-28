"""Seek Player 歌詞自動對時服務(aeneas forced alignment)。

獨立於 Firebase Functions(``functions/``)的 **Cloud Run 容器**:aeneas 需要
espeak / ffmpeg 等系統依賴,標準 Functions runtime 裝不下,故自架容器。

HTTP API(見 ``README.md`` 完整合約):

- ``GET  /healthz`` — 健康檢查。
- ``POST /align``   — 既有純文字 + 音訊 → 同步 LRC。

音訊以 **先壓縮 + GCS 中轉** 提供(``audio.gcs``);另支援 ``audio.inlineBase64``
方便本機/小檔測試。回傳組好的 LRC,client 寫回 ``LyricsEntity``
(source=generated、format=lrc)。
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from typing import Callable, Tuple

from flask import Flask, jsonify, request

from aligner import AlignmentError, align

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# 防呆上限:即使壓縮後,單檔仍不該超過此值(避免濫用 / OOM)。
_MAX_AUDIO_BYTES = 50 * 1024 * 1024


def _error(code: str, message: str, status: int):
    """統一錯誤格式,client 依 ``code`` 映射 l10n。"""
    return jsonify({"error": {"code": code, "message": message}}), status


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/align")
def align_endpoint():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("invalid_request", "需要 JSON body", 400)

    lines = payload.get("lines")
    if not isinstance(lines, list) or not any(str(x).strip() for x in lines):
        return _error("invalid_request", "lines 需為非空字串陣列", 400)

    language = str(payload.get("language") or "eng")
    audio = payload.get("audio")
    if not isinstance(audio, dict):
        return _error("invalid_request", "需提供 audio 物件", 400)

    try:
        audio_path, cleanup = _resolve_audio(audio)
    except ValueError as exc:
        return _error("invalid_request", str(exc), 400)
    except Exception as exc:  # GCS 下載等外部失敗
        log.exception("取得音訊失敗")
        return _error("audio_fetch_failed", f"取得音訊失敗:{exc}", 502)

    try:
        result = align([str(x) for x in lines], audio_path, language)
    except AlignmentError as exc:
        return _error("alignment_failed", str(exc), 422)
    except Exception as exc:  # 非預期失敗
        log.exception("對齊時發生未預期錯誤")
        return _error("internal", f"內部錯誤:{exc}", 500)
    finally:
        cleanup()

    # 上傳到 GCS 的暫存音訊不在此即時刪除;清理交給 bucket lifecycle
    # (align/ 前綴定期刪),涵蓋成功與失敗路徑,後端只需讀取權限。
    return jsonify(result)


def _resolve_audio(audio: dict) -> Tuple[str, Callable[[], None]]:
    """把請求中的音訊取到本機暫存檔,回傳 (路徑, 清理函式)。

    支援兩種來源:
    - ``audio.gcs = {bucket, object}``:從 Cloud Storage 下載(正式路線)。
    - ``audio.inlineBase64``:內嵌 base64(本機 / 小檔測試)。
    """
    suffix = "." + str(audio.get("format") or "bin").lstrip(".")
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    def cleanup() -> None:
        if os.path.exists(path):
            os.remove(path)

    try:
        gcs = audio.get("gcs")
        inline = audio.get("inlineBase64")
        if isinstance(gcs, dict):
            bucket = gcs.get("bucket")
            obj = gcs.get("object")
            if not bucket or not obj:
                raise ValueError("audio.gcs 需含 bucket 與 object")
            _download_gcs(str(bucket), str(obj), path)
        elif inline:
            data = base64.b64decode(inline)
            if len(data) > _MAX_AUDIO_BYTES:
                raise ValueError("音訊超過大小上限")
            with open(path, "wb") as f:
                f.write(data)
        else:
            raise ValueError("audio 需提供 gcs 或 inlineBase64")
    except Exception:
        cleanup()
        raise

    if os.path.getsize(path) > _MAX_AUDIO_BYTES:
        cleanup()
        raise ValueError("音訊超過大小上限")
    return path, cleanup


def _download_gcs(bucket: str, obj: str, dest_path: str) -> None:
    from google.cloud import storage

    client = storage.Client()
    blob = client.bucket(bucket).blob(obj)
    blob.download_to_filename(dest_path)


if __name__ == "__main__":
    # 本機開發用;正式由 gunicorn 啟動(見 Dockerfile)。
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
