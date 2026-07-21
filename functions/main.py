"""Seek Player 帳號相關 Cloud Functions。

提供三支 callable function:
- ``delete_account_data``:只刪除使用者的雲端資料(Firestore ``users/{uid}``),保留登入帳號。
- ``delete_account``:刪除雲端資料後,再刪除使用者的 Firebase Auth 帳號。
- ``align_lyrics`` / ``generate_lyrics``:歌詞自動對時 / 自動產生——驗證 + 配額後,
  把工作丟進 Cloud Tasks,由佇列非同步呼叫 aeneas / WhisperX Cloud Run 服務;
  **立刻回應,不等處理完成**。Cloud Run 端點處理完後自行把結果寫入 Firestore
  ``users/{uid}/lyrics/{trackId}``(不經 RPC 回傳歌詞內文)。若該 trackId 已有
  產生過的快照,直接回應成功、不重跑。

皆需登入(以 callable context 的 ``auth.uid`` 為準,使用者只能操作自己的資料)。
"""

import json
import logging
import os
from datetime import datetime, timezone

from firebase_admin import auth, firestore, initialize_app
from firebase_functions import https_fn, options
from google.cloud import tasks_v2

initialize_app()

log = logging.getLogger(__name__)

# 與 client 端 FirebaseFunctions.instanceFor(region: ...) 必須一致。
_REGION = "asia-east1"

# aeneas Cloud Run 服務的根 URL(無尾斜線、不含路徑),例如
# https://aeneas-align-xxxxx-de.a.run.app。以部署旗標設定:
#   firebase deploy --only functions  搭配 functions/.env 或 --set-env-vars。
# 同時作為 Cloud Tasks OIDC token 的 audience(Cloud Run 要求 audience = 服務根 URL)。
_ALIGN_SERVICE_URL = os.environ.get("ALIGN_SERVICE_URL", "").rstrip("/")

# WhisperX Cloud Run 服務的根 URL(同 `whisperx_service/`)。未設定時,
# 選 WhisperX 引擎會回退到 aeneas 服務(`_ALIGN_SERVICE_URL`)。
_WHISPERX_SERVICE_URL = os.environ.get("WHISPERX_SERVICE_URL", "").rstrip("/")

# client 端傳來的 engine 值 → 對應服務根 URL。
def _service_url_for(engine: str) -> str:
    if engine == "whisperx" and _WHISPERX_SERVICE_URL:
        return _WHISPERX_SERVICE_URL
    return _ALIGN_SERVICE_URL

# Cloud Tasks 佇列所在區域;預設同 Functions region,可用環境變數覆寫
# (佇列與 Cloud Run 服務可能部署在不同區域)。
_TASKS_LOCATION = os.environ.get("TASKS_LOCATION", _REGION)

# 對時 / 自動產生各自的 Cloud Tasks 佇列名稱(需事先用 gcloud 建立,見部署待辦)。
_ALIGN_TASK_QUEUE = os.environ.get("ALIGN_TASK_QUEUE", "align-lyrics")
_GENERATE_TASK_QUEUE = os.environ.get("GENERATE_TASK_QUEUE", "generate-lyrics")

# Cloud Tasks 派工時用來簽 OIDC token 的服務帳號(需具目標 Cloud Run 服務的
# roles/run.invoker,且本 Function 的執行 SA 需具該帳號的
# roles/iam.serviceAccountTokenCreator,才能請 Cloud Tasks 代簽,見部署待辦)。
_TASKS_INVOKER_SERVICE_ACCOUNT = os.environ.get("TASKS_INVOKER_SERVICE_ACCOUNT", "")

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


def _has_generated_snapshot(uid: str, track_id: str) -> bool:
    """檢查 ``users/{uid}/lyrics/{trackId}`` 是否已有對時 / 自動產生過的快照
    (``source=generated`` 且有內文)。已有的話代表這首歌處理過,不必重跑——
    ``align_lyrics`` / ``generate_lyrics`` 據此直接回應成功、不再派工。
    """
    doc = (
        firestore.client()
        .collection("users")
        .document(uid)
        .collection("lyrics")
        .document(track_id)
        .get()
    )
    if not doc.exists:
        return False
    data = doc.to_dict() or {}
    return data.get("source") == "generated" and bool(data.get("content"))


def _enqueue_task(queue: str, url: str, audience: str, payload: dict) -> None:
    """把工作丟進 Cloud Tasks,由佇列非同步 POST 到 Cloud Run 端點(帶 OIDC
    token 驗證身分),**不等待處理完成**——這是本次改版的核心:Function 派工
    後立刻回應 200,實際對時 / 轉寫在背景跑,結果由 Cloud Run 端點自行寫入
    Firestore。佇列本身內建重試(暫時性派工失敗自動重送),比 Function 內開
    背景執行緒可靠(Function 容器可能在回應後就被回收,背景工作不保證跑完)。
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
        "GCLOUD_PROJECT"
    )
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project, _TASKS_LOCATION, queue)
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode("utf-8"),
            "oidc_token": {
                "service_account_email": _TASKS_INVOKER_SERVICE_ACCOUNT,
                "audience": audience,
            },
        },
    }
    client.create_task(parent=parent, task=task)


@https_fn.on_call(
    region=_REGION,
    timeout_sec=60,
    memory=options.MemoryOption.MB_256,
)
def align_lyrics(req: https_fn.CallableRequest) -> dict:
    """歌詞自動對時:驗證 + 配額後,把工作丟進 Cloud Tasks 非同步轉呼 aeneas /
    WhisperX Cloud Run 的 ``/align``,**立刻回應、不等對時完成**。對時結果由
    Cloud Run 端點自行存入 Firestore ``users/{uid}/lyrics/{trackId}``
    (不經本 RPC 回傳歌詞內文)。

    Request data:
        - ``lines``: list[str],純文字歌詞(已去空行)。
        - ``bucket``: str,音訊所在的 GCS bucket。
        - ``object``: str,音訊物件路徑(App 先壓縮後上傳)。
        - ``language``: str(選填),語言碼,預設 ``eng``。
        - ``format``: str(選填),音訊副檔名提示。
        - ``trackId``: str,曲目 id(內容指紋);結果的存放位置
          (``users/{uid}/lyrics/{trackId}``),必填。
        - ``title``: str(選填),曲名,隨快照一併存入。

    回應語意:
        - 該 trackId 已有對時 / 產生過的快照 → ``{"saved": True, "cached": True}``。
        - 否則派工成功 → ``{"saved": True, "queued": True}``,**此時歌詞尚未
          產生完成**,client 不應假設此刻已可讀到內容。
        - 驗證失敗 / 配額用盡 / 派工失敗以 HttpsError 回對應錯誤碼。
    """
    uid = _require_uid(req)

    data = req.data or {}
    lines = data.get("lines")
    bucket = data.get("bucket")
    obj = data.get("object")
    language = data.get("language") or "eng"
    audio_format = data.get("format")
    track_id = data.get("trackId")
    title = data.get("title")
    engine = data.get("engine") or "aeneas"
    service_url = _service_url_for(engine)
    if not service_url:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="對時服務未設定(缺 ALIGN_SERVICE_URL)。",
        )
    if (
        not isinstance(lines, list)
        or not lines
        or not bucket
        or not obj
        or not track_id
    ):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="缺少 lines / bucket / object / trackId。",
        )

    if _has_generated_snapshot(uid, str(track_id)):
        return {"saved": True, "cached": True}

    if not _consume_daily_quota("align_usage", uid, _ALIGN_RATE_LIMIT_PER_DAY):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="今日自動對時次數已達上限,請明天再試。",
        )

    # 暫存音訊的清理交給 bucket lifecycle(align/ 前綴定期刪),後端不即時刪除,
    # 故 Cloud Run SA 只需 storage.objectViewer。
    gcs = {"bucket": bucket, "object": obj}
    audio = {"gcs": gcs}
    if audio_format:
        audio["format"] = audio_format
    payload = {
        "lines": lines,
        "language": language,
        "audio": audio,
        # 讓 Cloud Run 完成後自行寫回 Firestore——Function 已經回應,不會在場。
        "uid": uid,
        "trackId": str(track_id),
        "title": title or "",
    }

    try:
        _enqueue_task(
            _ALIGN_TASK_QUEUE, f"{service_url}/align", service_url, payload
        )
    except Exception as exc:
        log.exception("派工到 Cloud Tasks 失敗(uid=%s trackId=%s)", uid, track_id)
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message=f"派工失敗:{exc}",
        )

    return {"saved": True, "queued": True}


@https_fn.on_call(
    region=_REGION,
    timeout_sec=60,
    memory=options.MemoryOption.MB_256,
)
def generate_lyrics(req: https_fn.CallableRequest) -> dict:
    """歌詞自動產生:驗證 + 配額後,把工作丟進 Cloud Tasks 非同步轉呼
    WhisperX Cloud Run 的 ``/transcribe``,**立刻回應、不等轉寫完成**。結果由
    Cloud Run 端點自行存入 Firestore ``users/{uid}/lyrics/{trackId}``
    (不經本 RPC 回傳歌詞內文)。

    與 ``align_lyrics`` 的差別:**沒有既有文字**(不需 ``lines``),由後端 ASR
    直接辨識。僅 WhisperX 服務提供轉寫,故固定路由到 ``WHISPERX_SERVICE_URL``。

    Request data:
        - ``bucket``: str,音訊所在的 GCS bucket。
        - ``object``: str,音訊物件路徑(App 先壓縮後上傳,慣例前綴 ``generate/``)。
        - ``language``: str(選填),語言提示;省略則後端自動偵測。
        - ``format``: str(選填),音訊副檔名提示。
        - ``trackId``: str,曲目 id(內容指紋);結果的存放位置
          (``users/{uid}/lyrics/{trackId}``),必填。
        - ``title``: str(選填),曲名,隨快照一併存入。

    回應語意同 ``align_lyrics``:``{"saved": True, "cached": True}`` 或
    ``{"saved": True, "queued": True}``;失敗以 HttpsError 回對應錯誤碼。
    """
    uid = _require_uid(req)

    data = req.data or {}
    bucket = data.get("bucket")
    obj = data.get("object")
    language = data.get("language")  # 選填:省略 → 後端自動偵測語言。
    audio_format = data.get("format")
    track_id = data.get("trackId")
    title = data.get("title")

    if not _WHISPERX_SERVICE_URL:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="歌詞產生服務未設定(缺 WHISPERX_SERVICE_URL)。",
        )
    if not bucket or not obj or not track_id:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="缺少 bucket / object / trackId。",
        )

    if _has_generated_snapshot(uid, str(track_id)):
        return {"saved": True, "cached": True}

    if not _consume_daily_quota(
        "generate_usage", uid, _GENERATE_RATE_LIMIT_PER_DAY
    ):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="今日自動產生歌詞次數已達上限,請明天再試。",
        )

    audio = {"gcs": {"bucket": bucket, "object": obj}}
    if audio_format:
        audio["format"] = audio_format
    payload = {
        "audio": audio,
        # 讓 Cloud Run 完成後自行寫回 Firestore——Function 已經回應,不會在場。
        "uid": uid,
        "trackId": str(track_id),
        "title": title or "",
    }
    if language:
        payload["language"] = language

    try:
        _enqueue_task(
            _GENERATE_TASK_QUEUE,
            f"{_WHISPERX_SERVICE_URL}/transcribe",
            _WHISPERX_SERVICE_URL,
            payload,
        )
    except Exception as exc:
        log.exception("派工到 Cloud Tasks 失敗(uid=%s trackId=%s)", uid, track_id)
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message=f"派工失敗:{exc}",
        )

    return {"saved": True, "queued": True}
