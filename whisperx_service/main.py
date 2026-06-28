"""Seek Player 歌詞自動對時服務(WhisperX forced alignment)。

aeneas 路線(`../aeneas_service/`)的**升級選項**:同一個 HTTP 合約、可直接替換,
但內部改以 WhisperX 的 wav2vec2 對齊取得字級時間(粒度更細、中文 / 歌聲一般較佳,
見 `../plans/13-lyrics-auto-sync-whisperx.md`)。

切換方式:把 Firebase Function(`functions/main.py` 的 `align_lyrics`)的
``ALIGN_SERVICE_URL`` 指向本服務即可,**Flutter 端與 Function 程式碼皆不需改動**。

獨立於 Firebase Functions(``functions/``)的 **Cloud Run 容器**:wav2vec2 / torch
等依賴較重,標準 Functions runtime 裝不下,故自架容器。

HTTP API(與 aeneas 服務完全一致,見 ``README.md``):

- ``GET  /healthz`` — 健康檢查。
- ``POST /align``   — 既有純文字 + 音訊 → 同步 LRC。

音訊以 **先壓縮 + GCS 中轉** 提供(``audio.gcs``);另支援 ``audio.inlineBase64``
方便本機 / 小檔測試。回傳組好的 LRC,client 寫回 ``LyricsEntity``
(source=generated、format=lrc)。
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from typing import Callable, Tuple

from flask import Flask, jsonify, request

from aligner import AlignmentError, align
from logctx import bind_request, configure_logging
from transcriber import TranscriptionError, transcribe

configure_logging()
log = logging.getLogger(__name__)

app = Flask(__name__)

# 防呆上限:即使壓縮後,單檔仍不該超過此值(避免濫用 / OOM)。
_MAX_AUDIO_BYTES = 50 * 1024 * 1024


def _error(code: str, message: str, status: int):
    """統一錯誤格式,client 依 ``code`` 映射 l10n。"""
    return jsonify({"error": {"code": code, "message": message}}), status


def _describe_audio(audio: dict) -> str:
    """以可讀字串描述音訊來源,供 log 對照(GCS object 內含 trackId)。"""
    gcs = audio.get("gcs")
    if isinstance(gcs, dict):
        return f"gcs={gcs.get('bucket')}/{gcs.get('object')}"
    inline = audio.get("inlineBase64")
    if inline:
        # base64 長度 ~= 原始位元組 * 4/3,僅約略反映大小。
        return f"inlineBase64(~{len(str(inline)) * 3 // 4}B)"
    return "unknown"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/align")
def align_endpoint():
    bind_request("/align")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("invalid_request", "需要 JSON body", 400)

    lines = payload.get("lines")
    if not isinstance(lines, list) or not any(str(x).strip() for x in lines):
        return _error("invalid_request", "lines 需為非空字串陣列", 400)

    language = str(payload.get("language") or "en")
    audio = payload.get("audio")
    if not isinstance(audio, dict):
        return _error("invalid_request", "需提供 audio 物件", 400)

    # 階段耗時是判斷 client 端 network/deadline-exceeded 的關鍵:可區分
    # 卡在 GCS 下載、模型載入、還是對齊太久逼近 Function 的 590s 逾時。
    t0 = time.perf_counter()
    log.info(
        "/align 開始 lang=%s 行數=%d %s",
        language,
        len(lines),
        _describe_audio(audio),
    )

    try:
        audio_path, cleanup = _resolve_audio(audio)
    except ValueError as exc:
        return _error("invalid_request", str(exc), 400)
    except Exception as exc:  # GCS 下載等外部失敗
        log.exception("取得音訊失敗(已耗時 %.1fs)", time.perf_counter() - t0)
        return _error("audio_fetch_failed", f"取得音訊失敗:{exc}", 502)

    t_fetched = time.perf_counter()
    log.info(
        "音訊就緒 %.1fMB,下載/解碼耗時 %.1fs",
        os.path.getsize(audio_path) / 1024 / 1024,
        t_fetched - t0,
    )

    try:
        result = align([str(x) for x in lines], audio_path, language)
    except AlignmentError as exc:
        log.warning("對齊失敗(422,已耗時 %.1fs):%s", time.perf_counter() - t0, exc)
        return _error("alignment_failed", str(exc), 422)
    except Exception as exc:  # 非預期失敗
        log.exception("對齊時發生未預期錯誤(已耗時 %.1fs)", time.perf_counter() - t0)
        return _error("internal", f"內部錯誤:{exc}", 500)
    finally:
        cleanup()

    log.info(
        "/align 完成 行數=%d lang=%s,對齊耗時 %.1fs(總計 %.1fs)",
        len(result.get("fragments") or []),
        result.get("language"),
        time.perf_counter() - t_fetched,
        time.perf_counter() - t0,
    )
    # 上傳到 GCS 的暫存音訊不在此即時刪除;清理交給 bucket lifecycle
    # (align/ 前綴定期刪),涵蓋成功與失敗路徑,後端只需讀取權限。
    return jsonify(result)


@app.post("/transcribe")
def transcribe_endpoint():
    """自動產生歌詞:從音訊直接 ASR 轉寫 + 對齊,回傳同步 LRC。

    與 ``/align`` 的差別:**不需 ``lines``**(無既有文字)。``language`` 選填,
    省略則自動偵測。音訊來源同 ``/align``(``audio.gcs`` 或 ``audio.inlineBase64``)。
    """
    bind_request("/transcribe")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("invalid_request", "需要 JSON body", 400)

    # 語言為選填提示;省略 → whisper 自動偵測。
    language = payload.get("language")
    audio = payload.get("audio")
    if not isinstance(audio, dict):
        return _error("invalid_request", "需提供 audio 物件", 400)

    # 階段耗時是判斷 client 端 network/deadline-exceeded 的關鍵:可區分
    # 卡在 GCS 下載、模型載入、還是轉寫太久逼近 Function 的 590s 逾時。
    t0 = time.perf_counter()
    log.info("/transcribe 開始 lang=%s %s", language, _describe_audio(audio))

    try:
        audio_path, cleanup = _resolve_audio(audio)
    except ValueError as exc:
        return _error("invalid_request", str(exc), 400)
    except Exception as exc:  # GCS 下載等外部失敗
        log.exception("取得音訊失敗(已耗時 %.1fs)", time.perf_counter() - t0)
        return _error("audio_fetch_failed", f"取得音訊失敗:{exc}", 502)

    t_fetched = time.perf_counter()
    log.info(
        "音訊就緒 %.1fMB,下載/解碼耗時 %.1fs",
        os.path.getsize(audio_path) / 1024 / 1024,
        t_fetched - t0,
    )

    try:
        result = transcribe(audio_path, str(language) if language else None)
    except TranscriptionError as exc:
        # 記下「可預期失敗」的原因(辨識不出歌詞 / 音訊不可用),否則 422
        # 在 log 無痕,難以判斷是音訊問題還是模型沒抓到歌聲。
        log.warning("轉寫失敗(422,已耗時 %.1fs):%s", time.perf_counter() - t0, exc)
        return _error("transcription_failed", str(exc), 422)
    except Exception as exc:  # 非預期失敗
        log.exception("轉寫時發生未預期錯誤(已耗時 %.1fs)", time.perf_counter() - t0)
        return _error("internal", f"內部錯誤:{exc}", 500)
    finally:
        cleanup()

    log.info(
        "/transcribe 完成 行數=%d lang=%s,轉寫耗時 %.1fs(總計 %.1fs)",
        len(result.get("fragments") or []),
        result.get("language"),
        time.perf_counter() - t_fetched,
        time.perf_counter() - t0,
    )
    # 暫存音訊清理同 /align:交給 bucket lifecycle(generate/ 前綴定期刪)。
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
