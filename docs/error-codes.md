# Error Code

前端可依 `code` 做多語系翻譯對照

- **同步 RPC**(`align_lyrics` / `generate_lyrics` 呼叫當下失敗):code 放在 `HttpsError.details.code`。
- **非同步處理**(派工後,Cloud Run 服務跑完才知道成功或失敗):code 寫進 Firestore `users/{uid}/lyrics/{jobId}.error`,搭配 `status = "failed"`。狀態值一覽見 `lyrics-status.md`。

## 同步 RPC(`functions/main.py`)

`align_lyrics` / `generate_lyrics` 這兩支 callable function,派工前的驗證/配額失敗會直接丟 `HttpsError`,前端從 `error.details.code` 讀取:

| code | 觸發時機 | Firebase `error.code` |
|---|---|---|
| `service_unavailable` | 對應的 Cloud Run 服務未設定(缺 `ALIGN_SERVICE_URL` / `WHISPERX_SERVICE_URL`) | `internal` |
| `invalid_request` | 缺必填欄位(`lines` / `bucket` / `object` / `jobId`;已發佈版 client(<= v1.2.17)沒帶 `jobId` 時,40 碼 hex 或純數字的 `trackId` 也算數) | `invalid-argument` |
| `upgrade_required` | App 版本過舊:帶了已不支援的參數(`trackMd5` / `lyricsMd5` / `fingerprint`),或沒帶 `jobId` 且 `trackId` 不是 40 碼 hex / 純數字 / 空值;`details.params` 列出不支援的參數。App 應提示使用者更新版本 | `failed-precondition` |
| `busy` | 同一首歌同任務種類(同 `trackId`(音檔 SHA-256)+ `kind`)仍在處理中;`details.activeTitle` 為曲名 | `failed-precondition` |
| `quota_exceeded` | 本月額度(對時 + 自動產生合併計算,預設 60 分鐘)已用盡 | `resource-exhausted` |
| `quota_insufficient` | client 帶了 `durationSeconds`,且「本月已用量 + 這段音訊長度」超過上限;`details.remainingSeconds` / `details.durationSeconds` 為剩餘額度與音訊長度(秒) | `resource-exhausted` |
| `dispatch_failed` | 派工到 Cloud Tasks 失敗 | `internal` |

## 非同步處理(`whisperx_service/main.py` / `aeneas_service/main.py`)

Cloud Run 服務處理失敗時,寫入 `users/{uid}/lyrics/{jobId}`:`status = "failed"`、`error = <code>`。前端應以 Firestore 上的最新值為準(Cloud Tasks 重試可能覆寫回較早的狀態)。

| code | 觸發時機 | 服務 |
|---|---|---|
| `invalid_request` | request body 驗證失敗(如 `lines` 非陣列、缺 `audio`),或有配額限制卻量不出音訊長度 | 兩者皆有 |
| `audio_fetch_failed` | 從 GCS 下載音訊失敗 | 兩者皆有 |
| `align_model_unavailable` | 對齊模型從 HuggingFace 載入失敗(逾時等暫時性問題,可重試) | 僅 `whisperx_service` `/align` |
| `alignment_failed` | 對齊完成但結果不可用(覆蓋率過低等) | 兩者皆有 |
| `transcription_failed` | ASR 辨識不出歌詞文字 | 僅 `whisperx_service` `/transcribe` |
| `internal` | 其他非預期例外 | 兩者皆有 |
| `firestore_write_failed` | 結果算出來了,但寫回 Firestore 失敗 | 兩者皆有 |
| `quota_exceeded` | 量出實際音訊長度後,「本月已用量 + 長度」超過派工時帶的 `quotaSeconds`(不記帳、不處理) | 兩者皆有 |

## 沒有 code、無法客製化的情況

以下兩種是 Firebase Functions / Cloud Run 平台層級直接產生的錯誤,不會經過我們的程式碼,無論怎麼改後端都無法附加 `details.code`,前端翻譯表需對這兩種原生 `error.code` 做 fallback 顯示:

- **`unauthenticated`**:呼叫帶的認證 token 無效或過期(SDK 在進到 `align_lyrics`/`generate_lyrics` 之前就擋下)。若完全沒帶 token,則會進到 `_require_uid` 判斷,理論上可以補 `details.code`,但目前尚未加(見開發討論)。
- **`deadline-exceeded`**:超過 callable function 的 `timeout_sec`(目前 60 秒)逾時,由基礎設施強制中斷。
