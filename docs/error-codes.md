# Error Code

前端可依 `code` 做多語系翻譯對照

- **同步 RPC**(`align_lyrics` / `generate_lyrics` 呼叫當下失敗):code 放在 `HttpsError.details.code`。
- **非同步處理**(派工後,Cloud Run 服務跑完才知道成功或失敗):code 寫進 Firestore `users/{uid}/lyrics/{trackId}.error`,搭配 `status = "failed"`。

## 同步 RPC(`functions/main.py`)

`align_lyrics` / `generate_lyrics` 這兩支 callable function,派工前的驗證/配額失敗會直接丟 `HttpsError`,前端從 `error.details.code` 讀取:

| code | 觸發時機 | Firebase `error.code` |
|---|---|---|
| `service_unavailable` | 對應的 Cloud Run 服務未設定(缺 `ALIGN_SERVICE_URL` / `WHISPERX_SERVICE_URL`) | `internal` |
| `invalid_request` | 缺必填欄位(`lines` / `bucket` / `object` / `trackId`) | `invalid-argument` |
| `quota_exceeded` | 本月額度(對時 + 自動產生合併計算,預設 60 分鐘)已用盡 | `resource-exhausted` |
| `dispatch_failed` | 派工到 Cloud Tasks 失敗 | `internal` |

## 非同步處理(`whisperx_service/main.py` / `aeneas_service/main.py`)

Cloud Run 服務處理失敗時,寫入 `users/{uid}/lyrics/{trackId}`:`status = "failed"`、`error = <code>`。前端應以 Firestore 上的最新值為準(Cloud Tasks 重試可能覆寫回較早的狀態)。

| code | 觸發時機 | 服務 |
|---|---|---|
| `invalid_request` | request body 驗證失敗(如 `lines` 非陣列、缺 `audio`) | 兩者皆有 |
| `audio_fetch_failed` | 從 GCS 下載音訊失敗 | 兩者皆有 |
| `align_model_unavailable` | 對齊模型從 HuggingFace 載入失敗(逾時等暫時性問題,可重試) | 僅 `whisperx_service` `/align` |
| `alignment_failed` | 對齊完成但結果不可用(覆蓋率過低等) | 兩者皆有 |
| `transcription_failed` | ASR 辨識不出歌詞文字 | 僅 `whisperx_service` `/transcribe` |
| `internal` | 其他非預期例外 | 兩者皆有 |
| `firestore_write_failed` | 結果算出來了,但寫回 Firestore 失敗 | 兩者皆有 |

## 沒有 code、無法客製化的情況

以下兩種是 Firebase Functions / Cloud Run 平台層級直接產生的錯誤,不會經過我們的程式碼,無論怎麼改後端都無法附加 `details.code`,前端翻譯表需對這兩種原生 `error.code` 做 fallback 顯示:

- **`unauthenticated`**:呼叫帶的認證 token 無效或過期(SDK 在進到 `align_lyrics`/`generate_lyrics` 之前就擋下)。若完全沒帶 token,則會進到 `_require_uid` 判斷,理論上可以補 `details.code`,但目前尚未加(見開發討論)。
- **`deadline-exceeded`**:超過 callable function 的 `timeout_sec`(目前 60 秒)逾時,由基礎設施強制中斷。
