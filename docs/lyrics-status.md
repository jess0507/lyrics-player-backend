# Lyrics Status

歌詞製作(對時 `align_lyrics` / 自動產生 `generate_lyrics`)是非同步流程:callable function 派工到 Cloud Tasks 後立刻回應,實際處理由 Cloud Run 服務在背景執行。前端唯一能觀察進度的地方是 Firestore 文件

```
user/{uid}/lyrics/{jobId}
```

上的 `status` 欄位。`jobId` 由 client 每次送出時產生(callable 參數 `jobId`;`trackId` 參數是音檔 SHA-256,只用來比對沿用,不當文件 id。例外:已發佈版 client(<= v1.2.17)不帶 `jobId`、只帶 40 碼 sha1 指紋或純數字 MediaStore id 的 `trackId`,後端沿用舊行為拿它當文件 id,這種請求沒有 SHA-256,不查沿用),所以一份文件就是一次任務。本文列出所有狀態值、寫入者與流程順序。錯誤碼見 `error-codes.md`。

## 狀態一覽

| status | 寫入者 | 意義 |
|---|---|---|
| `queued` | `functions/main.py` | 已派進 Cloud Tasks 佇列,Cloud Run 尚未開始處理 |
| `downloading_audio` | `whisperx_service` / `aeneas_service` | 從 GCS 下載音檔中 |
| `aligning` | `whisperx_service` / `aeneas_service` `/align` | 強制對齊中(已有歌詞文字,只補時間) |
| `transcribing` | `whisperx_service` `/transcribe` | ASR 轉寫中(無歌詞文字,直接產生) |
| `saving` | `whisperx_service` / `aeneas_service` | 結果組成 LRC,準備寫回 Firestore |
| `done` | `whisperx_service` / `aeneas_service`;`functions/main.py`(命中既有結果時) | 完成。Cloud Run 寫入時同一筆一併帶上歌詞內容(見下方「done 的寫入內容」);`functions/main.py` 依指紋命中時把舊結果整份複製進這次的任務文件,舊版 client 命中同一份文件時只補 `status` |
| `failed` | `whisperx_service` / `aeneas_service` | 失敗。同時寫入 `error = <code>`,code 對照 `error-codes.md` |

前端可把 `queued` / `downloading_audio` / `aligning` / `transcribing` / `saving` 視為「進行中」,`done` / `failed` 視為終態。

## 流程順序

**對時(`align_lyrics` → `/align`)**

```
queued → downloading_audio → aligning → saving → done
                                                └→ failed(任一階段出錯)
```

**自動產生(`generate_lyrics` → `/transcribe`)**

```
queued → downloading_audio → transcribing → saving → done
                                                    └→ failed(任一階段出錯)
```

`aligning` 只出現在 `/align`,`transcribing` 只出現在 `/transcribe`,其餘階段共用。`aeneas_service` 沒有 `/transcribe`,所以不會寫 `transcribing`。

## 附帶欄位

每次更新 `status` 都會一併寫:

| 欄位 | 說明 |
|---|---|
| `statusUpdatedAt` | 毫秒 epoch(int),該狀態的寫入時間 |
| `error` | 僅 `failed` 時存在,值為錯誤碼字串。其他狀態寫入時會以 `DELETE_FIELD` 清掉,避免殘留上一次的錯誤 |

### `done` 的寫入內容

`done` 不是單獨更新 `status`,而是跟歌詞快照一起以 **merge** 寫入(保留派工時寫入的 `trackId` / `lyricsSha256` / `kind`,並清掉 `error`),欄位與 App 端 sync v5 的 `LyricsEntity` schema 一致:

| 欄位 | 值 |
|---|---|
| `title` | 請求帶的歌名,可能為空字串 |
| `format` | `"lrc"` |
| `source` | `"generated"` |
| `content` | 產出的 LRC 全文 |
| `addedAt` | 毫秒 epoch(int) |
| `status` | `"done"` |
| `statusUpdatedAt` | 毫秒 epoch(int) |

其他狀態(以及 `functions/main.py` 命中既有快照時補寫的 `done`)都是 `merge=True`,只動 `status` / `statusUpdatedAt` / `error`,**不會改變 `source`**。也就是處理期間 `source` 維持文件原有的值(例如使用者先前匯入的歌詞來源),只有成功到 `done` 時才變成 `generated`。

## 後端如何使用這些狀態

`functions/main.py` 在 `align_lyrics` / `generate_lyrics` 派工前依序檢查:

1. **任務文件本身已有產物**(同一個 jobId 重送,或已發佈版 client 以舊 trackId 為文件 id 時會命中):`source == "generated"` 且 `content` 非空 → 直接回 `{"saved": true, "cached": true}`,不派工、不扣配額。此判斷只看 `source` 與 `content`,不看 `status`。回應前會以 merge 把該文件的 `status` 補寫成 `done`(附 `statusUpdatedAt`、清掉 `error`),讓監聽 status 的前端能收到終態;舊快照可能沒有 `status`、或殘留上一次的 `failed`。
2. **任務文件本身進行中**:`status` 屬於 `queued` / `downloading_audio` / `aligning` / `transcribing` / `saving` → 回 `{"saved": true, "queued": true}`,不重複派工、不扣配額。
3. **依任務種類 + SHA-256 沿用**(client 有帶合法 `trackId` 時):直接 query `user/{uid}/lyrics` 裡 `trackId == 音檔 SHA-256` 且 `kind == 任務種類` 的任務文件(align 另加 `lyricsSha256 == 歌詞 SHA-256`):
   - 有一份 `status == "done"` 且 `content` 非空(**處理成功過**)→ 把 `title` / `format` / `source` / `content` 複製到這次的任務文件(同時寫入 `trackId` / `lyricsSha256` / `kind`、`status = "done"`),回 `{"saved": true, "cached": true}`,不派工、不扣配額。align 的歌詞改過(`lyricsSha256` 不同)就不會沿用,一定重新對齊。
   - 同一首歌同種類(只比 `trackId` + `kind`,不比歌詞)有一份仍在進行中(且 `statusUpdatedAt` 在 30 分鐘內)→ 丟 `busy`(`failed-precondition`)。
   - 其餘(沒有紀錄、都失敗或卡住)→ 照常派工。
4. **配額**:本月額度用盡 → 丟 `quota_exceeded`。
5. 通過後派工到 Cloud Tasks,再寫 `status = "queued"`(有 `trackId` 時一併寫入 `trackId` / `lyricsSha256` / `kind`)。

每個 jobId 文件都是一個獨立的任務;沒有另外的快取集合,沿用時比對的就是任務文件本身的欄位。

### 比對用的欄位

| 欄位 | 寫入者 | 說明 |
|---|---|---|
| `trackId` | `functions/main.py`(派工 / 沿用時) | **原始**音檔全檔內容 SHA-256 的 64 碼小寫 hex(不是上傳到 Storage 的壓縮後 m4a);client 算不出來 / 格式不符就不寫、不比對。更早期版本此欄位曾是 jobId 或 sha1 指紋,現在一律代表音檔 SHA-256 |
| `lyricsSha256` | `functions/main.py`(align 派工 / 沿用時) | 送對時的歌詞行 SHA-256:`sha256(utf8("\n".join(lines)))`;client 沒帶 / 格式不符時由 `lines` 自算 |
| `fingerprint` | 舊版 `functions/main.py` | **deprecated**。舊的 sha1 頭尾指紋,只留在舊文件上;client 帶的 `fingerprint` 參數一律忽略,不再寫入也不再比對 |
| `kind` | `functions/main.py`(派工 / 沿用時) | 任務種類 `generate` / `align` |

Cloud Run 存結果(`done`)與更新狀態都以 merge 寫入,這些欄位會一直留在任務文件上。查詢只有等值 / `in` 條件,若 Firestore 回報需要 composite index,依錯誤訊息裡的連結建立即可。

## 注意事項

- **狀態寫入是 best-effort**:`_write_status` 寫 Firestore 失敗只記 log,不中斷處理。唯一例外是 `done` 那筆快照寫入,失敗會回 `firestore_write_failed` 並讓 Cloud Tasks 重試。
- **Cloud Tasks 重試可能讓狀態倒退**:重試時 Cloud Run 會從 `downloading_audio` 重新寫起,前端應以 Firestore 的最新值為準,不要假設狀態單向遞進。
- **`failed` 不會自動清除**:再次呼叫 `align_lyrics` / `generate_lyrics` 會重新派工並覆寫成 `queued`(`failed` 不在進行中集合,不會被擋)。
- **直接呼叫 Cloud Run 端點除錯時**若不帶 `uid` / `trackId`(Cloud Run payload 的 `trackId` 是任務文件 id,即 jobId),則完全不寫 Firestore。

## 程式碼位置

狀態值目前沒有共用的 Enum,是三處各自定義的字串常數,新增或改名時需同步修改:

| 位置 | 內容 |
|---|---|
| `functions/main.py` `_IN_PROGRESS_STATUSES` | 5 個進行中狀態的集合;`_write_queued_status` / `_write_done_status` 經共用的 `_write_status` 寫字串 `"queued"` / `"done"` |
| `whisperx_service/main.py` `STATUS_*` | 6 個常數(含 `TRANSCRIBING`) |
| `aeneas_service/main.py` `STATUS_*` | 5 個常數(無 `TRANSCRIBING`) |
