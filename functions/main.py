"""Seek Player 帳號相關 Cloud Functions。

提供三支 callable function:
- ``delete_account_data``:只刪除使用者的雲端資料(Firestore ``user/{uid}``),保留登入帳號。
- ``delete_account``:刪除雲端資料後,再刪除使用者的 Firebase Auth 帳號。
- ``align_lyrics`` / ``generate_lyrics``:歌詞自動對時 / 自動產生——驗證 + 配額後,
  把工作丟進 Cloud Tasks,由佇列非同步呼叫 aeneas / WhisperX Cloud Run 服務;
  **立刻回應,不等處理完成**。Cloud Run 端點處理完後自行把結果寫入 Firestore
  ``user/{uid}/lyrics/{jobId}``(不經 RPC 回傳歌詞內文)。jobId 由 client 每次
  送出時產生(舊版 client 以 ``trackId`` 參數傳入曲目指紋,同樣當文件 id 用)。
  client 另帶音檔內容指紋 ``fingerprint`` 時,先查同一首歌是否處理過
  (見 ``_lookup_cached_job``):同任務種類、同指紋且上次處理成功,就複製到
  這次的 jobId 文件並回應成功、不重跑;同一個任務還在處理中則回 busy。
  派工成功後在同一份文件寫入
  ``status = "queued"``,後續由 Cloud Run 端點接力更新(見
  ``whisperx_service/main.py`` / ``aeneas_service/main.py`` 的狀態機說明),
  讓 client 全程可從 Firestore 讀到進度。

皆需登入(以 callable context 的 ``auth.uid`` 為準,使用者只能操作自己的資料)。
"""

import json
import logging
import os
import re
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

# 每位使用者每月可用的對時 / 自動產生歌詞總時長上限(秒),兩功能合併計算、
# 以實際音訊秒數計費(而非呼叫次數)。可用環境變數覆寫。
_MONTHLY_QUOTA_SECONDS = int(
    os.environ.get("MONTHLY_QUOTA_SECONDS", str(60 * 60))
)

# 不受每月上限限制的測試 / 內部帳號 uid(逗號分隔),供 QA 反覆測試。
# 名單內的 uid 在 align_lyrics / generate_lyrics 皆跳過配額檢查。
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


# ---- 依音檔內容指紋沿用既有結果 ----
#
# client 每次送出都用新的 jobId 當任務文件 id,文件 id 不再代表曲目,無法再以
# 「同一份文件已有結果」判斷是否處理過。改由 client 附上音檔內容指紋
# ``fingerprint``(``sha1("{size}:" + 頭 64KB + 尾 64KB)``,與舊版 trackId 同一套
# 演算法),本 Function 在 ``user/{uid}/lyricsCache/{cacheKey}`` 記下「這個
# 任務種類 + 指紋最後一次派工的 jobId」,下次同一個任務先查這裡;只有上次
# 處理成功(``status == "done"``)才沿用。
#
# 索引放在獨立子集合、只由本 Function 寫入:Cloud Run 存結果時以 ``set()``
# 整份覆寫 lyrics 文件,欄位放在 lyrics 文件裡會被蓋掉。

_CACHE_COLLECTION = "lyricsCache"

# 任務卡在處理中狀態超過這麼久(Cloud Run 中途掛掉等)視為失效,不再擋新派工。
_STALE_IN_PROGRESS_MS = 30 * 60 * 1000

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _user_doc(uid: str):
    return firestore.client().collection("user").document(uid)


def _cache_key(kind: str, fingerprint) -> str | None:
    """沿用結果的索引 key:任務種類(``generate`` / ``align``)+ 指紋,兩者都
    相同才算同一個任務;``fingerprint`` 不合法(舊版 client 沒帶)回 None。
    """
    if not isinstance(fingerprint, str) or not _FINGERPRINT_PATTERN.match(fingerprint):
        return None
    return f"{kind}_{fingerprint}"


def _lookup_cached_job(uid: str, cache_key: str):
    """查同一個任務(同 cache key)上次派工的結果。

    回傳 ``("done", data)``:上次**處理成功**(``status == "done"`` 且有內文),
    可沿用;``("busy", data)``:仍在處理中;``None``:沒有紀錄,或上次失敗 /
    卡住失效——照常派工(派工後索引改指向新的 jobId,不會蓋掉成功的紀錄:
    有成功的紀錄早在這裡就回 done 了)。
    """
    user = _user_doc(uid)
    index = user.collection(_CACHE_COLLECTION).document(cache_key).get()
    job_id = (index.to_dict() or {}).get("jobId") if index.exists else None
    if not job_id:
        return None
    snapshot = user.collection("lyrics").document(str(job_id)).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    status = data.get("status")
    if status == "done" and data.get("content"):
        return ("done", data)
    updated_at = data.get("statusUpdatedAt") or 0
    if (
        status in _IN_PROGRESS_STATUSES
        and _now_ms() - int(updated_at) < _STALE_IN_PROGRESS_MS
    ):
        return ("busy", data)
    return None


def _apply_cache(uid: str, job_id: str, cache_key: str | None, title) -> bool:
    """命中既有結果時複製到這次的任務文件並回 True(呼叫端直接回應 cached);
    同一首歌仍在處理中則丟 busy;沒有紀錄回 False(照常派工)。"""
    if cache_key is None:
        return False
    found = _lookup_cached_job(uid, cache_key)
    if found is None:
        return False
    state, data = found
    if state == "busy":
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            message="這首歌正在處理中,請稍後再試。",
            details={"code": "busy", "activeTitle": title or data.get("title") or ""},
        )
    now = _now_ms()
    # 與 Cloud Run 的 _save_lyrics_snapshot 同一份 schema,client 監聽到 done
    # 即照常存回本機。
    _user_doc(uid).collection("lyrics").document(job_id).set(
        {
            "title": title or data.get("title") or "",
            "format": data.get("format") or "lrc",
            "source": "generated",
            "content": data["content"],
            "addedAt": now,
            "status": "done",
            "statusUpdatedAt": now,
        }
    )
    return True


def _remember_job(uid: str, cache_key: str | None, job_id: str) -> None:
    """派工成功後記下這個 cache key 最新的 jobId。只是加速下次查詢,失敗只記 log。"""
    if cache_key is None:
        return
    try:
        _user_doc(uid).collection(_CACHE_COLLECTION).document(cache_key).set(
            {"jobId": job_id, "createdAt": _now_ms()}
        )
    except Exception:
        log.exception("寫入 lyricsCache 失敗(uid=%s key=%s)", uid, cache_key)


def _delete_user_data(uid: str) -> None:
    """遞迴刪除 Firestore ``user/{uid}`` 文件及其所有 subcollection。"""
    db = firestore.client()
    db.recursive_delete(db.collection("user").document(uid))


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


def _monthly_quota_exceeded(uid: str) -> bool:
    """檢查使用者本月(對時 + 自動產生合併計算)用量是否已達上限。

    只檢查、不累加——實際用量是處理完成後,由 Cloud Run 服務依實際音訊秒數
    寫入 ``usage/{uid}``(``{month: "YYYY-MM", secondsUsed: n}``,見
    ``whisperx_service/main.py`` / ``aeneas_service/main.py`` 的
    ``_add_monthly_usage``)。以 ``month`` 變更判定跨月,無需排程歸零。
    """
    # 豁免帳號(測試 / 內部)不受上限限制。
    if uid in _RATE_LIMIT_EXEMPT_UIDS:
        return False

    doc = firestore.client().collection("usage").document(uid).get()
    if not doc.exists:
        return False
    data = doc.to_dict() or {}
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if data.get("month") != current_month:
        return False
    return data.get("secondsUsed", 0) >= _MONTHLY_QUOTA_SECONDS


def _has_generated_snapshot(uid: str, track_id: str) -> bool:
    """檢查 ``user/{uid}/lyrics/{trackId}`` 是否已有對時 / 自動產生過的快照
    (``source=generated`` 且有內文)。已有的話代表這首歌處理過,不必重跑——
    ``align_lyrics`` / ``generate_lyrics`` 據此直接回應成功、不再派工。
    """
    doc = (
        firestore.client()
        .collection("user")
        .document(uid)
        .collection("lyrics")
        .document(track_id)
        .get()
    )
    if not doc.exists:
        return False
    data = doc.to_dict() or {}
    return data.get("source") == "generated" and bool(data.get("content"))


_IN_PROGRESS_STATUSES = {
    "queued",
    "downloading_audio",
    "aligning",
    "transcribing",
    "saving",
}


def _task_in_progress(uid: str, track_id: str) -> bool:
    """檢查 ``user/{uid}/lyrics/{trackId}`` 是否已有工作在跑(status 屬於
    ``_IN_PROGRESS_STATUSES``)。用來擋使用者短時間內連點造成的重複派工——
    命中的話直接回應已排隊,不再消耗每日配額、也不再建立新 Cloud Task。
    """
    doc = (
        firestore.client()
        .collection("user")
        .document(uid)
        .collection("lyrics")
        .document(track_id)
        .get()
    )
    if not doc.exists:
        return False
    return (doc.to_dict() or {}).get("status") in _IN_PROGRESS_STATUSES


def _write_status(uid: str, track_id: str, status: str) -> None:
    """以 merge 寫入 ``user/{uid}/lyrics/{trackId}.status``(附 ``statusUpdatedAt``),
    並清掉上次失敗殘留的 ``error``。merge 是因為該 trackId 可能還沒有 lyrics
    文件、或要沿用舊快照的 content / source。

    只是進度提示,寫入失敗不影響 RPC 已回應的結果,故吞例外、只記 log。
    """
    db = firestore.client()
    doc_ref = (
        db.collection("user")
        .document(uid)
        .collection("lyrics")
        .document(track_id)
    )
    try:
        doc_ref.set(
            {
                "status": status,
                "statusUpdatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
                "error": firestore.DELETE_FIELD,
            },
            merge=True,
        )
    except Exception:
        log.exception(
            "寫入 %s 狀態失敗(uid=%s trackId=%s)", status, uid, track_id
        )


def _write_queued_status(uid: str, track_id: str) -> None:
    """把工作標記為已進佇列(``status = "queued"``),讓 client 在收到 RPC 回應
    的當下就能顯示進度,不必等 Cloud Run 端點處理到一半才第一次看到狀態。
    """
    _write_status(uid, track_id, "queued")


def _write_done_status(uid: str, track_id: str) -> None:
    """命中既有快照、不派工時,把 ``status`` 補成 ``"done"``。舊快照的 status
    可能是舊版寫入前根本沒有、或殘留上一次的 ``failed``;client 監聽 status
    等終態的話會一直等不到,故在回 ``cached`` 前先把狀態收斂到 ``done``。
    """
    _write_status(uid, track_id, "done")


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
    Cloud Run 端點自行存入 Firestore ``user/{uid}/lyrics/{jobId}``
    (不經本 RPC 回傳歌詞內文)。

    Request data:
        - ``lines``: list[str],純文字歌詞(已去空行)。
        - ``bucket``: str,音訊所在的 GCS bucket。
        - ``object``: str,音訊物件路徑(App 先壓縮後上傳)。
        - ``language``: str(選填),語言碼,預設 ``eng``。
        - ``format``: str(選填),音訊副檔名提示。
        - ``jobId``: str,這次任務的文件 id;結果存在
          ``user/{uid}/lyrics/{jobId}``。新版 client 必帶。
        - ``trackId``: str,舊版 client 的曲目指紋,沒帶 ``jobId`` 時當任務
          文件 id 用(新版 client 也會帶,值同 ``jobId``)。兩者至少一個。
        - ``fingerprint``: str(選填),音檔內容指紋(40 碼 sha1 hex);有帶才
          查同一首歌是否處理過(見 ``_lookup_cached_job``)。
        - ``title``: str(選填),曲名,隨快照一併存入。

    回應語意:
        - 同一個任務(同任務種類、同指紋)上次處理成功 → 複製到這次的任務文件
          (``status = "done"``)並回 ``{"saved": True, "cached": True}``,
          不派工、不扣配額;舊版 client 則看任務文件本身是否已有快照。
        - 同一首歌仍在處理中 → ``failed-precondition``(``details.code = "busy"``)。
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
    # 任務文件 id:新版 client 帶 jobId;舊版只帶 trackId(曲目指紋)。
    track_id = data.get("jobId") or data.get("trackId")
    fingerprint = data.get("fingerprint")
    title = data.get("title")
    engine = data.get("engine") or "aeneas"
    service_url = _service_url_for(engine)
    if not service_url:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="對時服務未設定(缺 ALIGN_SERVICE_URL)。",
            details={"code": "service_unavailable"},
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
            message="缺少 lines / bucket / object / jobId。",
            details={"code": "invalid_request"},
        )

    if _has_generated_snapshot(uid, str(track_id)):
        _write_done_status(uid, str(track_id))
        return {"saved": True, "cached": True}

    if _task_in_progress(uid, str(track_id)):
        return {"saved": True, "queued": True}

    cache_key = _cache_key("align", fingerprint)
    if _apply_cache(uid, str(track_id), cache_key, title):
        return {"saved": True, "cached": True}

    if _monthly_quota_exceeded(uid):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="本月自動對時/產生歌詞額度已用盡,請下個月再試。",
            details={"code": "quota_exceeded"},
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
            details={"code": "dispatch_failed"},
        )

    _write_queued_status(uid, str(track_id))
    _remember_job(uid, cache_key, str(track_id))
    return {"saved": True, "queued": True}


@https_fn.on_call(
    region=_REGION,
    timeout_sec=60,
    memory=options.MemoryOption.MB_256,
)
def generate_lyrics(req: https_fn.CallableRequest) -> dict:
    """歌詞自動產生:驗證 + 配額後,把工作丟進 Cloud Tasks 非同步轉呼
    WhisperX Cloud Run 的 ``/transcribe``,**立刻回應、不等轉寫完成**。結果由
    Cloud Run 端點自行存入 Firestore ``user/{uid}/lyrics/{jobId}``
    (不經本 RPC 回傳歌詞內文)。

    與 ``align_lyrics`` 的差別:**沒有既有文字**(不需 ``lines``),由後端 ASR
    直接辨識。僅 WhisperX 服務提供轉寫,故固定路由到 ``WHISPERX_SERVICE_URL``。

    Request data:
        - ``bucket``: str,音訊所在的 GCS bucket。
        - ``object``: str,音訊物件路徑(App 先壓縮後上傳,慣例前綴 ``generate/``)。
        - ``language``: str(選填),語言提示;省略則後端自動偵測。
        - ``format``: str(選填),音訊副檔名提示。
        - ``jobId``: str,這次任務的文件 id;結果存在
          ``user/{uid}/lyrics/{jobId}``。新版 client 必帶。
        - ``trackId``: str,舊版 client 的曲目指紋,沒帶 ``jobId`` 時當任務
          文件 id 用(新版 client 也會帶,值同 ``jobId``)。兩者至少一個。
        - ``fingerprint``: str(選填),音檔內容指紋(40 碼 sha1 hex);有帶才
          查同一首歌是否處理過(見 ``_lookup_cached_job``)。
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
    # 任務文件 id:新版 client 帶 jobId;舊版只帶 trackId(曲目指紋)。
    track_id = data.get("jobId") or data.get("trackId")
    fingerprint = data.get("fingerprint")
    title = data.get("title")

    if not _WHISPERX_SERVICE_URL:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="歌詞產生服務未設定(缺 WHISPERX_SERVICE_URL)。",
            details={"code": "service_unavailable"},
        )
    if not bucket or not obj or not track_id:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="缺少 bucket / object / jobId。",
            details={"code": "invalid_request"},
        )

    if _has_generated_snapshot(uid, str(track_id)):
        _write_done_status(uid, str(track_id))
        return {"saved": True, "cached": True}

    if _task_in_progress(uid, str(track_id)):
        return {"saved": True, "queued": True}

    cache_key = _cache_key("generate", fingerprint)
    if _apply_cache(uid, str(track_id), cache_key, title):
        return {"saved": True, "cached": True}

    if _monthly_quota_exceeded(uid):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="本月自動對時/產生歌詞額度已用盡,請下個月再試。",
            details={"code": "quota_exceeded"},
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
            details={"code": "dispatch_failed"},
        )

    _write_queued_status(uid, str(track_id))
    _remember_job(uid, cache_key, str(track_id))
    return {"saved": True, "queued": True}
