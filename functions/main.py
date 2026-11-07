"""Seek Player 帳號相關 Cloud Functions。

提供三支 callable function:
- ``delete_account_data``:只刪除使用者的雲端資料(Firestore ``user/{uid}``),保留登入帳號。
- ``delete_account``:刪除雲端資料後,再刪除使用者的 Firebase Auth 帳號。
- ``align_lyrics`` / ``generate_lyrics``:歌詞自動對時 / 自動產生——驗證 + 配額後,
  把工作丟進 Cloud Tasks,由佇列非同步呼叫 aeneas / WhisperX Cloud Run 服務;
  **立刻回應,不等處理完成**。Cloud Run 端點處理完後自行把結果寫入 Firestore
  ``user/{uid}/lyrics/{jobId}``(不經 RPC 回傳歌詞內文)。jobId 由 client 每次
  送出時產生。client 帶音檔 SHA-256 ``trackId``(對時另帶歌詞 SHA-256
  ``lyricsSha256``)時,先 query 任務文件比對同任務種類(``kind``)、同 hash 且
  處理成功的結果(見 ``_reuse_succeeded_task``):有就複製到這次的 jobId 文件並
  回應成功、不重跑;同一首歌同種類的任務還在處理中則回 busy。舊的
  ``fingerprint`` 參數 / 欄位已 deprecated,不再讀取比對。
  派工成功後在同一份文件寫入
  ``status = "queued"``,後續由 Cloud Run 端點接力更新(見
  ``whisperx_service/main.py`` / ``aeneas_service/main.py`` 的狀態機說明),
  讓 client 全程可從 Firestore 讀到進度。

皆需登入(以 callable context 的 ``auth.uid`` 為準,使用者只能操作自己的資料)。
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

from firebase_admin import auth, firestore, initialize_app
from firebase_functions import https_fn, options
from google.cloud import tasks_v2
from google.cloud.firestore_v1.base_query import FieldFilter

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


# ---- 依任務種類 + 音檔 / 歌詞 SHA-256 沿用既有結果 ----
#
# client 每次送出都用新的 jobId 當任務文件 id,文件 id 不再代表曲目,無法再以
# 「同一份文件已有結果」判斷是否處理過。改由 client 附上:
# - ``trackId``:**原始**音檔全檔內容 SHA-256(64 碼小寫 hex,不是上傳到
#   Storage 的壓縮後 m4a;generate / align 都帶)。
# - ``lyricsSha256``:對時送出的歌詞行 SHA-256(僅 align),
#   ``sha256(utf8("\n".join(lines)))``。
# 派工時把它們與任務種類 ``kind``(``generate`` / ``align``)寫進任務文件,下次
# 直接 query 任務文件比對:
# - generate:``trackId`` + ``kind`` 相同,且 ``content`` 非空 → 沿用。
# - align:``trackId`` + ``kind`` + ``lyricsSha256`` 都相同,且 ``content`` 非空 →
#   沿用。歌詞改過(例如重新貼上)就是不同任務,一定重新對齊。
# Cloud Run 存結果時以 merge 寫入,這些欄位會一直保留在文件上。
# ``trackId`` 在更早期版本曾是 jobId 或 sha1 指紋,現在一律代表音檔 SHA-256;
# 格式不符(非 64 碼 hex)就當作沒帶,照常派工、不查沿用。``fingerprint``
# (sha1 頭尾指紋)已 deprecated:不再讀取、也不再寫入。

_KIND_GENERATE = "generate"
_KIND_ALIGN = "align"

# 任務卡在處理中狀態超過這麼久(Cloud Run 中途掛掉等)視為失效,不再擋新派工。
_STALE_IN_PROGRESS_MS = 30 * 60 * 1000

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# 已發佈版 client(<= v1.2.17)的 ``trackId``,當時直接拿來當任務文件 id:
# 40 碼 sha1 頭尾指紋;指紋算不出來時 Android 退回純數字的 MediaStore id。
_LEGACY_TRACK_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9]+)$")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _lyrics_collection(uid: str):
    return firestore.client().collection("user").document(uid).collection("lyrics")


def _valid_sha256(value) -> str | None:
    """合法的 64 碼小寫 SHA-256 hex 才回傳;沒帶或格式不符回 None。"""
    if isinstance(value, str) and _SHA256_PATTERN.match(value):
        return value
    return None


def _resolve_job_and_track_id(data: dict) -> tuple[str | None, str | None]:
    """從 callable 參數取出 ``(任務文件 id, 音檔 SHA-256)``。

    - 有 ``jobId``:``jobId`` 當文件 id;``trackId`` 必須是 64 碼 SHA-256 才拿來
      比對沿用,否則當作沒帶。
    - 沒 ``jobId`` 但 ``trackId`` 是 40 碼 hex 或純數字:已發佈版 client
      (``trackId`` 是 sha1 指紋或 MediaStore id、兼當文件 id),沿用舊行為拿它
      當文件 id。這種請求拿不到音檔
      SHA-256,就不查沿用(``trackId`` 回 None),只靠文件本身的快照判斷。
    - 沒 ``jobId``、``trackId`` 有值但不是上述格式:丟 ``upgrade_required``。
    - 兩者都沒帶:文件 id 回 None,由呼叫端回 ``invalid_request``。

    帶了已不再支援的參數(見 ``_UNSUPPORTED_PARAMS``)一律丟
    ``upgrade_required``。
    """
    unsupported = sorted(k for k in _UNSUPPORTED_PARAMS if k in data)
    if unsupported:
        _raise_upgrade_required(unsupported)
    job_id = data.get("jobId")
    raw_track_id = data.get("trackId")
    if job_id:
        return str(job_id), _valid_sha256(raw_track_id)
    if isinstance(raw_track_id, str) and _LEGACY_TRACK_ID_PATTERN.match(raw_track_id):
        return raw_track_id, None
    if raw_track_id not in (None, ""):
        _raise_upgrade_required(["trackId"])
    return None, None


# 已不再支援的 callable 參數:只出現在未發佈的開發版 client(MD5 / 頭尾指紋
# 識別),收到代表 client 版本過舊,請使用者更新 App。
_UNSUPPORTED_PARAMS = frozenset({"trackMd5", "lyricsMd5", "fingerprint"})


def _raise_upgrade_required(params: list[str]) -> None:
    """client 送了後端已不支援的參數 / 格式:回 ``failed-precondition``
    (``details.code = "upgrade_required"``),由 App 提示使用者更新版本。
    ``details.params`` 列出不支援的參數,方便 log 追查。"""
    raise https_fn.HttpsError(
        code=https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
        message="此版本的 App 已不支援這項功能,請更新到最新版本。",
        details={"code": "upgrade_required", "params": params},
    )


def _lines_sha256(lines: list) -> str:
    """歌詞行的 SHA-256,與 client 送的 ``lyricsSha256`` 同一套算法。"""
    return hashlib.sha256(
        "\n".join(str(line) for line in lines).encode("utf-8")
    ).hexdigest()


def _same_track_query(uid: str, kind: str, track_id: str):
    """同任務種類、同音檔 SHA-256(``trackId``)的任務文件。"""
    return (
        _lyrics_collection(uid)
        .where(filter=FieldFilter("trackId", "==", track_id))
        .where(filter=FieldFilter("kind", "==", kind))
    )


def _find_succeeded_task(
    uid: str, kind: str, track_id: str, lyrics_sha256: str | None
) -> dict | None:
    """同一個任務曾**處理成功**(``status == "done"`` 且 ``content`` 非空)的結果。
    ``lyrics_sha256`` 有值(align)時歌詞 SHA-256 也必須相同。"""
    query = _same_track_query(uid, kind, track_id).where(
        filter=FieldFilter("status", "==", "done")
    )
    if lyrics_sha256 is not None:
        query = query.where(
            filter=FieldFilter("lyricsSha256", "==", lyrics_sha256)
        )
    for snapshot in query.limit(5).stream():
        data = snapshot.to_dict() or {}
        if data.get("content"):
            return data
    return None


def _find_running_task(uid: str, kind: str, track_id: str) -> dict | None:
    """同一首歌同種類的任務仍在處理中(30 分鐘內有更新)的文件。不比對歌詞
    SHA-256:同一首歌一次只跑一件,改過歌詞的對時要等前一件結束再送。"""
    query = _same_track_query(uid, kind, track_id).where(
        filter=FieldFilter("status", "in", sorted(_IN_PROGRESS_STATUSES))
    )
    now = _now_ms()
    for snapshot in query.stream():
        data = snapshot.to_dict() or {}
        if now - int(data.get("statusUpdatedAt") or 0) < _STALE_IN_PROGRESS_MS:
            return data
    return None


def _task_keys(kind: str, track_id: str, lyrics_sha256: str | None) -> dict:
    """寫進任務文件、供之後比對沿用的欄位。"""
    keys = {"trackId": track_id, "kind": kind}
    if lyrics_sha256 is not None:
        keys["lyricsSha256"] = lyrics_sha256
    return keys


def _reuse_succeeded_task(
    uid: str,
    job_id: str,
    kind: str,
    track_id: str | None,
    lyrics_sha256: str | None,
    title,
) -> bool:
    """同一個任務曾處理成功:把結果複製到這次的任務文件並回 True(呼叫端直接
    回應 cached);同一首歌同種類的任務仍在處理中則丟 busy;其餘回 False
    (照常派工)。沒帶合法的 ``trackId``(client 算不出音檔 SHA-256)一律回 False。

    「同一個任務」:``trackId`` + ``kind`` 相同;align 另需 ``lyricsSha256`` 相同
    (``lyrics_sha256`` 由呼叫端保證 align 一定有值)。"""
    if track_id is None:
        return False
    succeeded = _find_succeeded_task(uid, kind, track_id, lyrics_sha256)
    if succeeded is not None:
        now = _now_ms()
        # 與 Cloud Run 的 _save_lyrics_snapshot 同一份 schema,client 監聽到
        # done 即照常存回本機;比對欄位一併寫入,這份也能再被沿用。
        _lyrics_collection(uid).document(job_id).set(
            {
                "title": title or succeeded.get("title") or "",
                "format": succeeded.get("format") or "lrc",
                "source": "generated",
                "content": succeeded["content"],
                "addedAt": now,
                "status": "done",
                "statusUpdatedAt": now,
                **_task_keys(kind, track_id, lyrics_sha256),
            }
        )
        return True
    running = _find_running_task(uid, kind, track_id)
    if running is not None:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            message="這首歌正在處理中,請稍後再試。",
            details={"code": "busy", "activeTitle": title or running.get("title") or ""},
        )
    return False


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


def _quota_seconds_for(uid: str) -> int | None:
    """使用者的每月上限(秒);豁免帳號(測試 / 內部)回 None 代表不限制。"""
    if uid in _RATE_LIMIT_EXEMPT_UIDS:
        return None
    return _MONTHLY_QUOTA_SECONDS


def _monthly_seconds_used(uid: str) -> float:
    """本月(對時 + 自動產生合併計算)已用秒數。

    用量依月份分開存在 ``usage/{uid}/months/{YYYY-MM}``
    (``{month, secondsUsed, updatedAt}``),歷史月份保留不刪;由 Cloud Run
    服務量出音訊長度後寫入(見 ``whisperx_service/main.py`` /
    ``aeneas_service/main.py`` 的 ``_reserve_monthly_usage``)。

    相容舊格式:當月文件還不存在時,改看舊版單一文件 ``usage/{uid}``
    (``{month, secondsUsed}``),月份相符就沿用。
    """
    db = firestore.client()
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    user_ref = db.collection("usage").document(uid)
    doc = user_ref.collection("months").document(current_month).get()
    if not doc.exists:
        doc = user_ref.get()
        if not doc.exists or (doc.to_dict() or {}).get("month") != current_month:
            return 0.0
    return float((doc.to_dict() or {}).get("secondsUsed", 0) or 0)


def _client_duration_seconds(data: dict) -> float | None:
    """client 帶的音訊長度(秒);沒帶 / 非正數回 None。僅供派工前提早擋下,
    不可信任——權威檢查在 Cloud Run 量測實際音訊後進行。"""
    value = data.get("durationSeconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _ensure_quota(uid: str, quota_seconds: int | None, duration: float | None) -> None:
    """派工前檢查本月額度,不足時丟 ``resource-exhausted``:

    - 已用量 ≥ 上限 → ``quota_exceeded``。
    - client 有帶 ``durationSeconds`` 且「已用量 + 長度」> 上限 →
      ``quota_insufficient``(``details`` 附 ``remainingSeconds`` /
      ``durationSeconds``)。

    只檢查、不記帳;記帳與權威檢查由 Cloud Run 依實際音訊長度處理。
    """
    if quota_seconds is None:
        return
    used = _monthly_seconds_used(uid)
    if used >= quota_seconds:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="本月自動對時/產生歌詞額度已用盡,請下個月再試。",
            details={"code": "quota_exceeded"},
        )
    if duration is not None and used + duration > quota_seconds:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
            message="本月剩餘額度不足以處理這段音訊。",
            details={
                "code": "quota_insufficient",
                "remainingSeconds": max(0.0, quota_seconds - used),
                "durationSeconds": duration,
            },
        )

def _has_generated_snapshot(uid: str, job_id: str) -> bool:
    """檢查 ``user/{uid}/lyrics/{jobId}`` 是否已有對時 / 自動產生過的快照
    (``source=generated`` 且有內文)。已有的話代表這首歌處理過,不必重跑——
    ``align_lyrics`` / ``generate_lyrics`` 據此直接回應成功、不再派工。
    """
    doc = (
        firestore.client()
        .collection("user")
        .document(uid)
        .collection("lyrics")
        .document(job_id)
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


def _task_in_progress(uid: str, job_id: str) -> bool:
    """檢查 ``user/{uid}/lyrics/{jobId}`` 是否已有工作在跑(status 屬於
    ``_IN_PROGRESS_STATUSES``)。用來擋使用者短時間內連點造成的重複派工——
    命中的話直接回應已排隊,不再消耗每日配額、也不再建立新 Cloud Task。
    """
    doc = (
        firestore.client()
        .collection("user")
        .document(uid)
        .collection("lyrics")
        .document(job_id)
        .get()
    )
    if not doc.exists:
        return False
    return (doc.to_dict() or {}).get("status") in _IN_PROGRESS_STATUSES


def _write_status(uid: str, job_id: str, status: str, extra: dict | None = None) -> None:
    """以 merge 寫入 ``user/{uid}/lyrics/{jobId}.status``(附 ``statusUpdatedAt``),
    並清掉上次失敗殘留的 ``error``。merge 是因為該 jobId 可能還沒有 lyrics
    文件、或要沿用舊快照的 content / source。

    只是進度提示,寫入失敗不影響 RPC 已回應的結果,故吞例外、只記 log。
    """
    db = firestore.client()
    doc_ref = (
        db.collection("user")
        .document(uid)
        .collection("lyrics")
        .document(job_id)
    )
    try:
        doc_ref.set(
            {
                "status": status,
                "statusUpdatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
                "error": firestore.DELETE_FIELD,
                **(extra or {}),
            },
            merge=True,
        )
    except Exception:
        log.exception(
            "寫入 %s 狀態失敗(uid=%s jobId=%s)", status, uid, job_id
        )


def _write_queued_status(
    uid: str,
    job_id: str,
    kind: str,
    track_id: str | None,
    lyrics_sha256: str | None = None,
) -> None:
    """把工作標記為已進佇列(``status = "queued"``),讓 client 在收到 RPC 回應
    的當下就能顯示進度,不必等 Cloud Run 端點處理到一半才第一次看到狀態。
    有音檔 SHA-256 時一併寫入 ``trackId`` / ``kind``(align 另有
    ``lyricsSha256``),供之後同一個任務比對沿用。
    """
    extra = _task_keys(kind, track_id, lyrics_sha256) if track_id else None
    _write_status(uid, job_id, "queued", extra)


def _write_done_status(uid: str, job_id: str) -> None:
    """命中既有快照、不派工時,把 ``status`` 補成 ``"done"``。舊快照的 status
    可能是舊版寫入前根本沒有、或殘留上一次的 ``failed``;client 監聽 status
    等終態的話會一直等不到,故在回 ``cached`` 前先把狀態收斂到 ``done``。
    """
    _write_status(uid, job_id, "done")


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
        - ``trackId``: str(選填),**原始**音檔全檔內容 SHA-256(64 碼小寫
          hex);有帶且格式正確才查同一個任務是否處理成功過(見
          ``_reuse_succeeded_task``)。已發佈版 client 不帶 ``jobId``、只帶 40 碼
          sha1 指紋或純數字 MediaStore id 的 ``trackId``,此時拿它當任務文件 id,
          不查沿用。
        - ``lyricsSha256``: str(選填),``lines`` 的 SHA-256
          (``sha256(utf8("\\n".join(lines)))``);沒帶 / 格式不符時由
          ``lines`` 自算。
        - ``title``: str(選填),曲名,隨快照一併存入。
        - ``durationSeconds``: number(選填),client 量到的音訊長度(秒);
          有帶就在派工前確認「本月已用量 + 長度」不超過上限(``quota_insufficient``)。
          不可信任,權威檢查由 Cloud Run 量測實際音訊後進行。

    回應語意:
        - 同一個任務(同任務種類、同 ``trackId``、同 ``lyricsSha256``)上次處理成功
          且有內文 → 複製到這次的任務文件
          (``status = "done"``)並回 ``{"saved": True, "cached": True}``,
          不派工、不扣配額;另外也會看任務文件本身是否已有快照。
        - 同一首歌(同 ``trackId``)仍在處理中 → ``failed-precondition``(``details.code = "busy"``)。
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
    # 任務文件 id 與音檔 SHA-256;已發佈版 client 只帶舊 trackId 時拿它當文件 id。
    job_id, track_id = _resolve_job_and_track_id(data)
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
        or not job_id
    ):
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="缺少 lines / bucket / object / jobId。",
            details={"code": "invalid_request"},
        )

    if _has_generated_snapshot(uid, str(job_id)):
        _write_done_status(uid, str(job_id))
        return {"saved": True, "cached": True}

    if _task_in_progress(uid, str(job_id)):
        return {"saved": True, "queued": True}

    # 歌詞 SHA-256 以 client 帶的為準,沒帶 / 格式不符就由實際收到的 lines 自算,
    # 確保 align 的沿用一定比對到歌詞內容。
    lyrics_sha256 = _valid_sha256(data.get("lyricsSha256")) or _lines_sha256(lines)
    if _reuse_succeeded_task(
        uid, str(job_id), _KIND_ALIGN, track_id, lyrics_sha256, title
    ):
        return {"saved": True, "cached": True}

    quota_seconds = _quota_seconds_for(uid)
    _ensure_quota(uid, quota_seconds, _client_duration_seconds(data))

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
        # Cloud Run 端點沿用舊的 payload 鍵名 ``trackId`` 當任務文件 id,
        # 這裡放的是 jobId(不是音檔 SHA-256)。
        "trackId": str(job_id),
        "title": title or "",
    }
    # Cloud Run 量出實際長度後依此做權威的配額檢查;豁免帳號不帶 = 不限制。
    if quota_seconds is not None:
        payload["quotaSeconds"] = quota_seconds

    try:
        _enqueue_task(
            _ALIGN_TASK_QUEUE, f"{service_url}/align", service_url, payload
        )
    except Exception as exc:
        log.exception("派工到 Cloud Tasks 失敗(uid=%s jobId=%s)", uid, job_id)
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message=f"派工失敗:{exc}",
            details={"code": "dispatch_failed"},
        )

    _write_queued_status(uid, str(job_id), _KIND_ALIGN, track_id, lyrics_sha256)
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
        - ``trackId``: str(選填),**原始**音檔全檔內容 SHA-256(64 碼小寫
          hex);有帶且格式正確才查同一個任務是否處理成功過(見
          ``_reuse_succeeded_task``)。已發佈版 client 不帶 ``jobId``、只帶 40 碼
          sha1 指紋或純數字 MediaStore id 的 ``trackId``,此時拿它當任務文件 id,
          不查沿用。
        - ``title``: str(選填),曲名,隨快照一併存入。
        - ``durationSeconds``: number(選填),client 量到的音訊長度(秒);
          有帶就在派工前確認「本月已用量 + 長度」不超過上限(``quota_insufficient``)。
          不可信任,權威檢查由 Cloud Run 量測實際音訊後進行。

    回應語意同 ``align_lyrics``:``{"saved": True, "cached": True}`` 或
    ``{"saved": True, "queued": True}``;失敗以 HttpsError 回對應錯誤碼。
    """
    uid = _require_uid(req)

    data = req.data or {}
    bucket = data.get("bucket")
    obj = data.get("object")
    language = data.get("language")  # 選填:省略 → 後端自動偵測語言。
    audio_format = data.get("format")
    # 任務文件 id 與音檔 SHA-256;已發佈版 client 只帶舊 trackId 時拿它當文件 id。
    job_id, track_id = _resolve_job_and_track_id(data)
    title = data.get("title")

    if not _WHISPERX_SERVICE_URL:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="歌詞產生服務未設定(缺 WHISPERX_SERVICE_URL)。",
            details={"code": "service_unavailable"},
        )
    if not bucket or not obj or not job_id:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="缺少 bucket / object / jobId。",
            details={"code": "invalid_request"},
        )

    if _has_generated_snapshot(uid, str(job_id)):
        _write_done_status(uid, str(job_id))
        return {"saved": True, "cached": True}

    if _task_in_progress(uid, str(job_id)):
        return {"saved": True, "queued": True}

    if _reuse_succeeded_task(
        uid, str(job_id), _KIND_GENERATE, track_id, None, title
    ):
        return {"saved": True, "cached": True}

    quota_seconds = _quota_seconds_for(uid)
    _ensure_quota(uid, quota_seconds, _client_duration_seconds(data))

    audio = {"gcs": {"bucket": bucket, "object": obj}}
    if audio_format:
        audio["format"] = audio_format
    payload = {
        "audio": audio,
        # 讓 Cloud Run 完成後自行寫回 Firestore——Function 已經回應,不會在場。
        "uid": uid,
        # Cloud Run 端點沿用舊的 payload 鍵名 ``trackId`` 當任務文件 id,
        # 這裡放的是 jobId(不是音檔 SHA-256)。
        "trackId": str(job_id),
        "title": title or "",
    }
    # Cloud Run 量出實際長度後依此做權威的配額檢查;豁免帳號不帶 = 不限制。
    if quota_seconds is not None:
        payload["quotaSeconds"] = quota_seconds
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
        log.exception("派工到 Cloud Tasks 失敗(uid=%s jobId=%s)", uid, job_id)
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message=f"派工失敗:{exc}",
            details={"code": "dispatch_failed"},
        )

    _write_queued_status(uid, str(job_id), _KIND_GENERATE, track_id)
    return {"saved": True, "queued": True}
