"""Seek Player 帳號相關 Cloud Functions。

提供三支 callable function:
- ``delete_account_data``:只刪除使用者的雲端資料(Firestore ``users/{uid}``),保留登入帳號。
- ``delete_account``:刪除雲端資料後,再刪除使用者的 Firebase Auth 帳號。
- ``align_lyrics``:歌詞自動對時——把 GCS 上的壓縮音訊 + 純文字轉給 aeneas
  Cloud Run 服務做 forced alignment,回傳同步 LRC。代為注入 Cloud Run 身分、
  並做每使用者每日 rate limit。

皆需登入(以 callable context 的 ``auth.uid`` 為準,使用者只能操作自己的資料)。
"""

import os
from datetime import datetime, timezone

import google.auth.transport.requests
import google.oauth2.id_token
import requests
from firebase_admin import auth, firestore, initialize_app
from firebase_functions import https_fn, options

initialize_app()

# 與 client 端 FirebaseFunctions.instanceFor(region: ...) 必須一致。
_REGION = "asia-east1"

# aeneas Cloud Run 服務的根 URL(無尾斜線、不含路徑),例如
# https://aeneas-align-xxxxx-de.a.run.app。以部署旗標設定:
#   firebase deploy --only functions  搭配 functions/.env 或 --set-env-vars。
# 同時作為取 ID token 的 audience(Cloud Run 要求 audience = 服務根 URL)。
_ALIGN_SERVICE_URL = os.environ.get("ALIGN_SERVICE_URL", "").rstrip("/")

# WhisperX Cloud Run 服務的根 URL(同 `whisperx_service/`)。未設定時,
# 選 WhisperX 引擎會回退到 aeneas 服務(`_ALIGN_SERVICE_URL`)。
_WHISPERX_SERVICE_URL = os.environ.get("WHISPERX_SERVICE_URL", "").rstrip("/")

# client 端傳來的 engine 值 → 對應服務根 URL。
def _service_url_for(engine: str) -> str:
    if engine == "whisperx" and _WHISPERX_SERVICE_URL:
        return _WHISPERX_SERVICE_URL
    return _ALIGN_SERVICE_URL

# 每位使用者每日可呼叫對時的次數上限(防濫用 / 控成本)。可用環境變數覆寫,
# 詳見 plans/lyrics-auto-sync-aeneas.md。
_ALIGN_RATE_LIMIT_PER_DAY = int(os.environ.get("ALIGN_RATE_LIMIT_PER_DAY", "20"))

# 每位使用者每日可呼叫「自動產生歌詞」的次數上限。轉寫(ASR)比對時更重,
# 預設低於對時,並以獨立計數(generate_usage)不與對時互吃額度。
_GENERATE_RATE_LIMIT_PER_DAY = int(
    os.environ.get("GENERATE_RATE_LIMIT_PER_DAY", "5")
)

# 不受每日上限限制的測試 / 內部帳號 uid(逗號分隔),供 QA 反覆測試。
# 名單內的 uid 在 align_lyrics / generate_lyrics 皆跳過配額(不計數)。
_RATE_LIMIT_EXEMPT_UIDS = {
    u.strip()
    for u in os.environ.get("RATE_LIMIT_EXEMPT_UIDS", "").split(",")
    if u.strip()
}


def _require_uid(req: https_fn.CallableRequest) -> str:
    """取出已驗證的 uid;未登入則丟出 UNAUTHENTICATED。"""
    if req.auth is None or not req.auth.uid:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="必須登入才能執行此操作。",
        )
    return req.auth.uid


def _delete_user_data(uid: str) -> None:
    """遞迴刪除 Firestore ``users/{uid}`` 文件及其所有 subcollection。"""
    db = firestore.client()
    db.recursive_delete(db.collection("users").document(uid))


@https_fn.on_call(region=_REGION)
def delete_account_data(req: https_fn.CallableRequest) -> dict:
    """只刪除使用者的雲端資料,保留登入帳號。"""
    uid = _require_uid(req)
    _delete_user_data(uid)
    return {"deleted": True, "uid": uid}


@https_fn.on_call(region=_REGION)
def delete_account(req: https_fn.CallableRequest) -> dict:
    """刪除使用者雲端資料後,刪除其 Firebase Auth 帳號。"""
    uid = _require_uid(req)
    _delete_user_data(uid)
    auth.delete_user(uid)
    return {"deleted": True, "uid": uid}


def _consume_daily_quota(collection: str, uid: str, limit: int) -> bool:
    """以交易方式檢查並累加某使用者當日用量;超過上限回 False(不累加)。

    紀錄於 ``{collection}/{uid}``(`{day: 'YYYY-MM-DD', count: n}`),每日自動歸零
    (以 ``day`` 變更判定,無需排程清理)。對時 / 自動產生各用獨立 collection。
    """
    # 豁免帳號(測試 / 內部)不受上限限制,也不累加用量。
    if uid in _RATE_LIMIT_EXEMPT_UIDS:
        return True

    db = firestore.client()
    ref = db.collection(collection).document(uid)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @firestore.transactional
    def _txn(transaction) -> bool:
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() or {}
        count = data.get("count", 0) if data.get("day") == today else 0
        if count >= limit:
            return False
        transaction.set(ref, {"day": today, "count": count + 1})
        return True

    return _txn(db.transaction())


@https_fn.on_call(
    region=_REGION,
    timeout_sec=600,
    memory=options.MemoryOption.MB_512,
)
def align_lyrics(req: https_fn.CallableRequest) -> dict:
    """歌詞自動對時:轉呼 aeneas Cloud Run 的 ``/align``。

    Request data:
        - ``lines``: list[str],純文字歌詞(已去空行)。
        - ``bucket``: str,音訊所在的 GCS bucket。
        - ``object``: str,音訊物件路徑(App 先壓縮後上傳)。
        - ``language``: str(選填),語言碼,預設 ``eng``。
        - ``format``: str(選填),音訊副檔名提示。

    回傳 ``{"lrc": ..., "language": ...}``;失敗以 HttpsError 回對應錯誤碼。
    """
    uid = _require_uid(req)

    data = req.data or {}
    lines = data.get("lines")
    bucket = data.get("bucket")
    obj = data.get("object")
    language = data.get("language") or "eng"
    audio_format = data.get("format")
    engine = data.get("engine") or "aeneas"
    service_url = _service_url_for(engine)
    if not service_url:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="對時服務未設定(缺 ALIGN_SERVICE_URL)。",
        )
    if not isinstance(lines, list) or not lines or not bucket or not obj:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="缺少 lines / bucket / object。",
        )

    if not _consume_daily_quota("align_usage", uid, _ALIGN_RATE_LIMIT_PER_DAY):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="今日自動對時次數已達上限,請明天再試。",
        )

    # 取對 Cloud Run 服務的 ID token(audience = 服務根 URL);
    # 本 Function 的服務帳號需具該服務的 roles/run.invoker。
    auth_req = google.auth.transport.requests.Request()
    token = google.oauth2.id_token.fetch_id_token(auth_req, service_url)

    # 暫存音訊的清理交給 bucket lifecycle(align/ 前綴定期刪),後端不即時刪除,
    # 故 Cloud Run SA 只需 storage.objectViewer。
    gcs = {"bucket": bucket, "object": obj}
    audio = {"gcs": gcs}
    if audio_format:
        audio["format"] = audio_format
    payload = {"lines": lines, "language": language, "audio": audio}

    try:
        resp = requests.post(
            f"{service_url}/align",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=590,
        )
    except requests.RequestException as e:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAVAILABLE,
            message=f"無法連線對時服務:{e}",
        )

    if resp.status_code == 200:
        body = resp.json()
        return {"lrc": body.get("lrc"), "language": body.get("language")}

    # 映射後端錯誤碼 → callable 錯誤碼(client 端據此降級 / 提示)。
    try:
        code = (resp.json().get("error") or {}).get("code")
    except ValueError:
        code = None
    if resp.status_code == 422 or code == "alignment_failed":
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            message="alignment_failed",
        )
    if resp.status_code in (502, 503) or code == "audio_fetch_failed":
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAVAILABLE,
            message="audio_fetch_failed",
        )
    raise https_fn.HttpsError(
        code=https_fn.FunctionsErrorCode.INTERNAL,
        message=f"對時服務錯誤({resp.status_code})。",
    )


@https_fn.on_call(
    region=_REGION,
    timeout_sec=600,
    memory=options.MemoryOption.MB_512,
)
def generate_lyrics(req: https_fn.CallableRequest) -> dict:
    """歌詞自動產生:轉呼 WhisperX Cloud Run 的 ``/transcribe``。

    與 ``align_lyrics`` 的差別:**沒有既有文字**(不需 ``lines``),由後端 ASR
    直接辨識。僅 WhisperX 服務提供轉寫,故固定路由到 ``WHISPERX_SERVICE_URL``。

    Request data:
        - ``bucket``: str,音訊所在的 GCS bucket。
        - ``object``: str,音訊物件路徑(App 先壓縮後上傳,慣例前綴 ``generate/``)。
        - ``language``: str(選填),語言提示;省略則後端自動偵測。
        - ``format``: str(選填),音訊副檔名提示。

    回傳 ``{"lrc": ..., "language": ...}``;失敗以 HttpsError 回對應錯誤碼。
    """
    uid = _require_uid(req)

    data = req.data or {}
    bucket = data.get("bucket")
    obj = data.get("object")
    language = data.get("language")  # 選填:省略 → 後端自動偵測語言。
    audio_format = data.get("format")

    if not _WHISPERX_SERVICE_URL:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="歌詞產生服務未設定(缺 WHISPERX_SERVICE_URL)。",
        )
    if not bucket or not obj:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="缺少 bucket / object。",
        )

    if not _consume_daily_quota(
        "generate_usage", uid, _GENERATE_RATE_LIMIT_PER_DAY
    ):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="今日自動產生歌詞次數已達上限,請明天再試。",
        )

    # 取對 Cloud Run 服務的 ID token(audience = 服務根 URL);
    # 本 Function 的服務帳號需具該服務的 roles/run.invoker。
    auth_req = google.auth.transport.requests.Request()
    token = google.oauth2.id_token.fetch_id_token(
        auth_req, _WHISPERX_SERVICE_URL
    )

    audio = {"gcs": {"bucket": bucket, "object": obj}}
    if audio_format:
        audio["format"] = audio_format
    payload = {"audio": audio}
    if language:
        payload["language"] = language

    try:
        resp = requests.post(
            f"{_WHISPERX_SERVICE_URL}/transcribe",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=590,
        )
    except requests.RequestException as e:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAVAILABLE,
            message=f"無法連線歌詞產生服務:{e}",
        )

    if resp.status_code == 200:
        body = resp.json()
        return {"lrc": body.get("lrc"), "language": body.get("language")}

    # 映射後端錯誤碼 → callable 錯誤碼(client 端據此降級 / 提示)。
    try:
        code = (resp.json().get("error") or {}).get("code")
    except ValueError:
        code = None
    if resp.status_code == 422 or code == "transcription_failed":
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            message="transcription_failed",
        )
    if resp.status_code in (502, 503) or code == "audio_fetch_failed":
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAVAILABLE,
            message="audio_fetch_failed",
        )
    raise https_fn.HttpsError(
        code=https_fn.FunctionsErrorCode.INTERNAL,
        message=f"歌詞產生服務錯誤({resp.status_code})。",
    )
