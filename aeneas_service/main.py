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
把工作進度寫進同一份 ``user/{uid}/lyrics/{trackId}`` 文件的 ``status`` 欄位
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
import subprocess
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

# ``user/{uid}/lyrics/{trackId}.status`` 狀態機。``queued`` 由
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
    """更新工作狀態(``user/{uid}/lyrics/{trackId}.status``),讓 client 不必
    等最終結果就能顯示進度 / 失敗原因。以 merge 寫入,不動 ``content`` 等既有
    欄位;成功轉下一階段時清掉舊的 ``error``。

    只是進度提示,寫入失敗不該讓原本的處理流程跟著中止,故吞例外、只記 log
    (與 :func:`_save_lyrics_snapshot` 刻意不吞例外的原則不同)。
    """
    from google.cloud import firestore

    db = firestore.Client()
    doc_ref = (
        db.collection("user")
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


def _current_usage_month() -> str:
    """用量記帳用的月份 key(UTC ``YYYY-MM``)。"""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _usage_month_ref(db, uid: str, month: str):
    """``usage/{uid}/months/{YYYY-MM}``:每月一份用量文件
    (``{month, secondsUsed, updatedAt}``),保留歷史月份供查詢 / 統計;
    ``functions/main.py`` 的 ``_ensure_quota`` 只讀當月那份。"""
    return db.collection("usage").document(uid).collection("months").document(month)


def _read_month_seconds(db, transaction, uid: str, month: str) -> float:
    """在 transaction 內讀出某月已用秒數。

    相容舊格式:當月文件還不存在時,改看舊版單一文件 ``usage/{uid}``
    (``{month, secondsUsed}``),月份相符就沿用其值,避免上線當月用量歸零。
    """
    snap = _usage_month_ref(db, uid, month).get(transaction=transaction)
    if snap.exists:
        return float((snap.to_dict() or {}).get("secondsUsed", 0) or 0)
    legacy = db.collection("usage").document(uid).get(transaction=transaction)
    data = legacy.to_dict() or {}
    if data.get("month") == month:
        return float(data.get("secondsUsed", 0) or 0)
    return 0.0


def _write_month_seconds(db, transaction, uid: str, month: str, seconds: float) -> None:
    from google.cloud import firestore

    transaction.set(
        _usage_month_ref(db, uid, month),
        {
            "month": month,
            "secondsUsed": max(0.0, seconds),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
    )


def _add_monthly_usage(uid: str, seconds: float, month: str) -> None:
    """把 ``seconds`` 加進 ``usage/{uid}/months/{month}``(見 ``_usage_month_ref``)。

    ``month`` 由呼叫端傳入(記帳當下的 ``_current_usage_month()``),處理失敗
    扣回時才會扣在當初記帳的那個月,不會因為處理期間跨月而扣錯月份。

    ``seconds`` 可為負數,供處理失敗時把先前(下載完成、音訊長度一知道就先
    記的)用量扣回——``max(0, ...)`` 避免扣成負數。

    只是用量記帳,失敗不該讓已經存好的歌詞結果跟著回錯,故吞例外、只記 log
    (與 ``_write_status`` 同樣的原則)。
    """
    from google.cloud import firestore

    db = firestore.Client()

    @firestore.transactional
    def _txn(transaction) -> None:
        current = _read_month_seconds(db, transaction, uid, month)
        _write_month_seconds(db, transaction, uid, month, current + seconds)

    try:
        _txn(db.transaction())
    except Exception:
        log.exception("寫入用量失敗(uid=%s month=%s)", uid, month)


def _payload_quota_seconds(payload: dict) -> float | None:
    """取出派工 payload 的 ``quotaSeconds``(本月上限,秒);沒帶 / 非正數回
    None,代表不限制(豁免帳號,或舊版 Function 派的工)。"""
    value = payload.get("quotaSeconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _reserve_monthly_usage(
    uid: str, seconds: float, quota_seconds: float | None, month: str
) -> bool | None:
    """在同一個 transaction 內檢查並記帳 ``usage/{uid}/months/{month}``:
    ``quota_seconds`` 不為 None 且「該月已用量 + ``seconds``」超過上限時不寫入、
    回 False;否則記帳並回 True。Firestore 失敗回 None(只記 log、放行處理但
    不算已記帳,與 ``_add_monthly_usage`` 吞例外的原則一致)。

    檢查與記帳在同一個 transaction,短時間連發多個請求也不會一起通過檢查而超額。
    """
    from google.cloud import firestore

    db = firestore.Client()

    @firestore.transactional
    def _txn(transaction) -> bool:
        current = _read_month_seconds(db, transaction, uid, month)
        if quota_seconds is not None and current + seconds > quota_seconds:
            log.info(
                "超出本月額度,拒絕(uid=%s month=%s 已用=%.0fs 本次=%.0fs 上限=%.0fs)",
                uid,
                month,
                current,
                seconds,
                quota_seconds,
            )
            return False
        _write_month_seconds(db, transaction, uid, month, current + seconds)
        return True

    try:
        return _txn(db.transaction())
    except Exception:
        log.exception("檢查 / 寫入用量失敗(uid=%s month=%s)", uid, month)
        return None


def _probe_duration_seconds(audio_path: str) -> float:
    """用 ffprobe 量測音訊實際長度(秒)。

    音訊一下載完成就能知道長度,不必等 aeneas 對齊跑完——``align_endpoint``
    據此提早記用量(見該函式對 ``_add_monthly_usage`` 的第一次呼叫),避免
    使用者短時間內連發請求都趕在對齊完成前通過配額檢查而超額。
    """
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return float(out.stdout.strip())


def _fail(uid, track_id, code: str, message: str, http_status: int):
    """回錯誤回應前,順手把失敗狀態記回 Firestore(有 uid/trackId 才記)。"""
    if uid and track_id:
        _write_status(str(uid), str(track_id), STATUS_FAILED, error=code)
    return _error(code, message, http_status)


def _save_lyrics_snapshot(uid: str, track_id: str, title: str, lrc: str) -> None:
    """把對時結果寫回 ``user/{uid}/lyrics/{trackId}``,與 App 端既有的歌詞
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
        db.collection("user")
        .document(uid)
        .collection("lyrics")
        .document(track_id)
    )
    # merge:保留派工時 functions/main.py 寫入的 ``trackMd5`` / ``lyricsMd5`` / ``kind``
    # (同一首歌沿用結果時據此比對);``error`` 明確清掉,避免 Cloud Tasks
    # 重試前的失敗殘留在成功的文件上。
    doc_ref.set(
        {
            "title": title or "",
            "format": "lrc",
            "source": "generated",
            "content": lrc,
            "addedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
            "status": STATUS_DONE,
            "statusUpdatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
            "error": firestore.DELETE_FIELD,
        },
        merge=True,
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

    # 音訊已下載、長度已知——不必等對齊跑完就先記一筆用量,關掉配額檢查與
    # 實際扣量之間的空窗(同一使用者短時間連發多個請求,原本都會在對齊完成
    # 前通過 `_ensure_quota` 檢查而超額)。之後任何一步失敗,
    # 在各自的失敗分支把這筆用量扣回。
    #
    # 有帶 ``quotaSeconds``(``functions/main.py`` 派工時依使用者帶入,豁免帳號
    # 不帶)就在同一個 transaction 內確認「本月已用量 + 這次長度」不超過上限,
    # 超過則不記帳、直接拒絕,避免單一長音檔一次吃掉遠超上限的用量。
    duration_seconds = 0.0
    usage_recorded = False
    usage_month = _current_usage_month()
    if uid and track_id:
        try:
            duration_seconds = _probe_duration_seconds(audio_path)
        except Exception:
            log.exception("量測音訊長度失敗(uid=%s trackId=%s)", uid, track_id)
        quota_seconds = _payload_quota_seconds(payload)
        if quota_seconds is not None and duration_seconds <= 0:
            # 有配額限制卻量不出長度,無法確認是否超額,一律拒絕。
            cleanup()
            return _fail(uid, track_id, "invalid_request", "無法量測音訊長度", 400)
        if duration_seconds > 0:
            reserved = _reserve_monthly_usage(
                str(uid), duration_seconds, quota_seconds, usage_month
            )
            if reserved is False:
                cleanup()
                return _fail(
                    uid,
                    track_id,
                    "quota_exceeded",
                    f"本月剩餘額度不足以處理這段 {duration_seconds:.0f} 秒的音訊",
                    429,
                )
            usage_recorded = reserved is True

    if uid and track_id:
        _write_status(str(uid), str(track_id), STATUS_ALIGNING)

    try:
        result = align([str(x) for x in lines], audio_path, language)
    except AlignmentError as exc:
        if usage_recorded:
            _add_monthly_usage(str(uid), -duration_seconds, usage_month)
        return _fail(uid, track_id, "alignment_failed", str(exc), 422)
    except Exception as exc:  # 非預期失敗
        log.exception("對齊時發生未預期錯誤")
        if usage_recorded:
            _add_monthly_usage(str(uid), -duration_seconds, usage_month)
        return _fail(uid, track_id, "internal", f"內部錯誤:{exc}", 500)
    finally:
        cleanup()

    result.pop("durationSeconds", None)

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
            if usage_recorded:
                _add_monthly_usage(str(uid), -duration_seconds, usage_month)
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
