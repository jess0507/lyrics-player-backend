"""Seek Player 歌詞自動對時服務(aeneas forced alignment)。

獨立於 Firebase Functions(``functions/``)的 **Cloud Run 容器**:aeneas 需要
espeak / ffmpeg 等系統依賴,標準 Functions runtime 裝不下,故自架容器。

HTTP API(見 ``README.md`` 完整合約):

- ``GET  /healthz`` — 健康檢查。
- ``POST /align``   — 既有純文字 + 音訊 → 同步 LRC。

音訊以 **先壓縮 + GCS 中轉** 提供(``audio.gcs``);另支援 ``audio.inlineBase64``
方便本機/小檔測試。回傳組好的 LRC,client 寫回 ``LyricsEntity``
(source=generated、format=lrc)。

**狀態機**:有帶 ``uid``/``trackId`` 時(Cloud Tasks 派工皆會帶),本服務會
把工作進度寫進同一份 ``users/{uid}/lyrics/{trackId}`` 文件的 ``status`` 欄位
(``functions/main.py`` 派工成功已先寫 ``"queued"``):
``downloading_audio`` → ``aligning`` → ``saving`` → ``"done"``。任何一步
失敗則寫 ``"failed"``,並在 ``error`` 欄位附上對應的錯誤 code
(``invalid_request`` / ``audio_fetch_failed`` / ``alignment_failed`` /
``internal`` / ``firestore_write_failed``,與 HTTP 回應的 ``error.code``
一致;與 ``whisperx_service`` 相同,惟本服務無 ``align_model_unavailable``)。
Cloud Tasks 重試會覆寫回較早的狀態,故 client 應以 Firestore 上的最新值為準,
而非假設狀態只會前進。
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from typing import Callable, Tuple

from datetime import datetime, timezone

from flask import Flask, jsonify, request

from aligner import AlignmentError, align
from logctx import bind_request, configure_logging

configure_logging()
log = logging.getLogger(__name__)

app = Flask(__name__)

# 防呆上限:即使壓縮後,單檔仍不該超過此值(避免濫用 / OOM)。
_MAX_AUDIO_BYTES = 50 * 1024 * 1024

# ``users/{uid}/lyrics/{trackId}.status`` 狀態機。``queued`` 由
# ``functions/main.py`` 派工成功時寫入;本服務接力寫入其餘階段。失敗時額外
# 寫 ``error``(沿用 ``_error()`` 的 code)。與 ``whisperx_service`` 共用同一
# 套值(僅本服務無對應 ``align_model_unavailable`` 的中介狀態)。
STATUS_DOWNLOADING_AUDIO = "downloading_audio"
STATUS_ALIGNING = "aligning"
STATUS_SAVING = "saving"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _error(code: str, message: str, status: int):
    """統一錯誤格式,client 依 ``code`` 映射 l10n。"""
    return jsonify({"error": {"code": code, "message": message}}), status


def _write_status(uid: str, track_id: str, status: str, error: str | None = None) -> None:
    """更新工作狀態(``users/{uid}/lyrics/{trackId}.status``),讓 client 不必
    等最終結果就能顯示進度 / 失敗原因。以 merge 寫入,不動 ``content`` 等既有
    欄位;成功轉下一階段時清掉舊的 ``error``。

    只是進度提示,寫入失敗不該讓原本的處理流程跟著中止,故吞例外、只記 log
    (與 :func:`_save_lyrics_snapshot` 刻意不吞例外的原則不同)。
    """
    from google.cloud import firestore

    db = firestore.Client()
    doc_ref = (
        db.collection("users")
        .document(uid)
        .collection("lyrics")
        .document(track_id)
    )
    data = {
        "status": status,
        "statusUpdatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        "error": error if error else firestore.DELETE_FIELD,
    }
    try:
        doc_ref.set(data, merge=True)
    except Exception:
        log.exception(
            "更新狀態到 Firestore 失敗(uid=%s trackId=%s status=%s)",
            uid,
            track_id,
            status,
        )


def _fail(uid, track_id, code: str, message: str, http_status: int):
    """回錯誤回應前,順手把失敗狀態記回 Firestore(有 uid/trackId 才記)。"""
    if uid and track_id:
        _write_status(str(uid), str(track_id), STATUS_FAILED, error=code)
    return _error(code, message, http_status)


def _save_lyrics_snapshot(uid: str, track_id: str, title: str, lrc: str) -> None:
    """把對時結果寫回 ``users/{uid}/lyrics/{trackId}``,與 App 端既有的歌詞
    備份 schema(sync v5,見 ``lib/core/sync/lyrics_sync.dart``)一致
    (``title`` / ``format`` / ``source`` / ``content`` / ``addedAt`` 毫秒
    epoch int),並附上 ``status = "done"`` 讓狀態機收尾。

    由本服務(而非 ``functions/main.py``)直接寫入,是因為呼叫端(Cloud
    Tasks 派工)已經不等待處理完成、Function 早已回應——結果只能由實際做完
    對時的這裡自己存,寫入失敗要讓呼叫端(Cloud Tasks)知道以觸發重試,
    故**不吞例外**。
    """
    from google.cloud import firestore

    db = firestore.Client()
    doc_ref = (
        db.collection("users")
        .document(uid)
        .collection("lyrics")
        .document(track_id)
    )
    doc_ref.set(
        {
            "title": title or "",
            "format": "lrc",
            "source": "generated",
            "content": lrc,
            "addedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
            "status": STATUS_DONE,
            "statusUpdatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/align")
def align_endpoint():
    bind_request("/align")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("invalid_request", "需要 JSON body", 400)

    # 提早取出:除了 body 不是 JSON 這種連欄位都拿不到的情況,後續任何驗證 /
    # 執行失敗都要能把 STATUS_FAILED 寫回 Firestore(見 `_fail`)。選填,
    # Cloud Tasks 派工時會帶上(見 `functions/main.py` align_lyrics);直接呼叫
    # 本端點測試 / 除錯時可省略,行為等同不寫 Firestore。
    uid = payload.get("uid")
    track_id = payload.get("trackId")
    title = payload.get("title")

    lines = payload.get("lines")
    if not isinstance(lines, list) or not any(str(x).strip() for x in lines):
        return _fail(uid, track_id, "invalid_request", "lines 需為非空字串陣列", 400)

    language = str(payload.get("language") or "eng")
    audio = payload.get("audio")
    if not isinstance(audio, dict):
        return _fail(uid, track_id, "invalid_request", "需提供 audio 物件", 400)

    if uid and track_id:
        _write_status(str(uid), str(track_id), STATUS_DOWNLOADING_AUDIO)

    try:
        audio_path, cleanup = _resolve_audio(audio)
    except ValueError as exc:
        return _fail(uid, track_id, "invalid_request", str(exc), 400)
    except Exception as exc:  # GCS 下載等外部失敗
        log.exception("取得音訊失敗")
        return _fail(uid, track_id, "audio_fetch_failed", f"取得音訊失敗:{exc}", 502)

    if uid and track_id:
        _write_status(str(uid), str(track_id), STATUS_ALIGNING)

    try:
        result = align([str(x) for x in lines], audio_path, language)
    except AlignmentError as exc:
        return _fail(uid, track_id, "alignment_failed", str(exc), 422)
    except Exception as exc:  # 非預期失敗
        log.exception("對齊時發生未預期錯誤")
        return _fail(uid, track_id, "internal", f"內部錯誤:{exc}", 500)
    finally:
        cleanup()

    if uid and track_id:
        _write_status(str(uid), str(track_id), STATUS_SAVING)
        try:
            _save_lyrics_snapshot(str(uid), str(track_id), str(title or ""), result["lrc"])
        except Exception as exc:
            # 寫入失敗回 5xx,讓 Cloud Tasks 依佇列重試設定重送;不吞例外,
            # 否則會有「對時算完了但沒人知道、也沒存到」的靜默遺失。
            log.exception(
                "寫入歌詞快照到 Firestore 失敗(uid=%s trackId=%s)", uid, track_id
            )
            return _fail(
                uid, track_id, "firestore_write_failed", f"寫入 Firestore 失敗:{exc}", 500
            )

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
