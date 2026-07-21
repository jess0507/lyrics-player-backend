# 歌詞功能:自動對時(txt → 同步)— aeneas 路線(backlog 5)

狀態:**後端容器已實作、待部署驗證;Flutter 端未做**(2026-06-16)。

### 實作進度 / 決策(2026-06-16)

- **範圍**:先做後端容器(本輪),Flutter 端待下一輪。後端部署 / 對齊品質
  驗證需 GCP 權限,於開發環境無法代跑。
- **後端落在新目錄 `aeneas_service/`**(獨立於 `functions/` 的 Firebase
  Functions):aeneas 需 espeak / ffmpeg 系統依賴,標準 Functions runtime 裝
  不下,故走 **Cloud Run 自架容器**(`Dockerfile` + Flask `main.py`)。
  - `main.py`(`/healthz`、`/align`)、`aligner.py`(aeneas 核心)、
    `lrc.py`(秒 → `[mm:ss.xx]`,沿用 import 計畫的百分秒慣例)、
    `test_lrc.py`(純邏輯單元測試,`python -m unittest test_lrc` 9 項全過)。
  - Dockerfile 釘 **Python 3.8 + numpy 1.23.5 + aeneas 1.7.3.0**:aeneas 為
    2017 舊套件、含 C 擴充,對新版 Python/numpy 易編譯失敗,先裝 numpy 再裝
    aeneas。
- **音訊傳輸採「先壓縮 + GCS 中轉」**:`/align` 的 `audio.gcs={bucket,object}`
  為正式路線(後端下載、清理靠 bucket lifecycle);另留
  `audio.inlineBase64`(≤50MB)供本機 / 小檔測試。App 端應先壓成單聲道低取樣
  (如 16kHz mono opus)再上傳。
- **「音訊取得」其實有解**:`on_audio_query` 的 `SongModel.data` 是真實檔案路徑
  (`music_library.dart:38` 已用),匯入服務也以 `File(path)` 讀檔——Flutter 端
  可由此讀音訊 bytes、壓縮後上傳,毋須只靠 content URI。
- **API 合約 / 錯誤碼 / 部署步驟**:見 `aeneas_service/README.md`。失敗一律不回
  半套時間,對齊失敗(`alignment_failed` 422)時 App 應保留原 unsynced 文字。
- **待辦(下一輪 Flutter 端)**:讀檔 + 壓縮 + 上傳 GCS、呼叫 `/align`(鑑權:
  Firebase Function 代呼或帶 ID token)、寫回 `LyricsEntity`、進度 / 失敗 UI、
  l10n。→ **已完成,見下節**。

---

## Flutter 端 + Firebase Function 實作(2026-06-16)

選定路線(經使用者確認):**需登入**(走 Firebase Function 代理,不讓 App 直連
Cloud Run)+ **GCS 中轉 + 壓縮**。

### 流程

1. App 由 `trackId` 反查本機音訊真實路徑(`on_audio_query` 的 `SongModel.data`)。
2. **壓縮**:ffmpeg 轉單聲道 16kHz **AAC/m4a**(`audio_compressor.dart`)。
   - 原計畫寫 opus,但 opus 需特定 ffmpeg 建置;**改用 AAC**(各平台 ffmpeg
     內建 aac 編碼器,相容性最高)。目的不變:砍掉立體聲 / 高取樣以降低上傳量。
   - 用 `ffmpeg_kit_flutter_new`(原 `ffmpeg_kit_flutter` 已停更)。**需
     Android minSdk ≥ 24**,已在 `android/app/build.gradle.kts` 以
     `maxOf(flutter.minSdkVersion, 24)` 設定。
3. **上傳**:Firebase Storage **預設 bucket**,路徑 `align/{uid}/{trackId}-{ts}.m4a`
   (`firebase_storage`)。上傳後立即刪本機暫存壓縮檔。
4. **呼叫** callable `align_lyrics`,帶
   `{lines, bucket, object, language, format, trackId, title}`。
   - `lines`:由現存 `LyricsEntity` parse 後取非空白行(與後端再濾空行一一對應)。
   - `language`:取裝置 locale 的 BCP-47(後端正規化為 aeneas ISO 639-3)。
   - `trackId`:必填,後端存放結果的位置(見下)。
5. Function 驗 uid → rate limit → 取 Cloud Run ID token → 轉呼 `/align`
   → **直接把結果存入 Firestore `users/{uid}/lyrics/{trackId}`,不經 RPC
   回傳歌詞內文**(2026-07-20 改版,見下方增補),只回 `{saved: true,
   language}`。暫存音訊不即時刪,完全交給 bucket lifecycle 清理(見部署)。
6. App 收到成功回應後,**讀回**剛存的 Firestore 快照取得內文,寫回**同一**
   `LyricsEntity`(`source=generated`、`format=lrc`)、
   `invalidate(trackLyricsProvider)`,顯示端自動切同步視圖。**顯示端未改**。

### 失敗降級

後端不回半套時間;對齊失敗(callable `failed-precondition` / 後端 422
`alignment_failed`)時 **App 保留原 unsynced 文字**,只跳 SnackBar 提示。其餘錯誤
(rate limit / 未登入 / 找不到音訊 / 連線)各有對應 l10n 提示。

### Rate limit(可設定)

- 在 `align_lyrics`(`functions/main.py`)做**每使用者每日**上限,用 Firestore
  交易紀錄於 `align_usage/{uid}`(`{day, count}`,跨日自動歸零、免排程清理)。
- **預設 20 次/日**,以環境變數 `ALIGN_RATE_LIMIT_PER_DAY` 覆寫。超過回
  `resource-exhausted` → App 顯示「今日次數已達上限」。
- 調整方式:部署時加 `--set-env-vars ALIGN_RATE_LIMIT_PER_DAY=50`(或寫
  `functions/.env`)。

### 新增檔案

| 檔案 | 職責 |
| --- | --- |
| `functions/main.py` `align_lyrics` | callable:登入 + rate limit + 注入 ID token + 轉呼 `/align` + 錯誤映射。 |
| `lib/features/lyrics/auto_sync/audio_compressor.dart` | ffmpeg 壓縮(16kHz mono AAC)。 |
| `lib/features/lyrics/auto_sync/lyrics_auto_sync_service.dart` | 編排:路徑→壓縮→上傳→callable→寫回 entity。 |
| `lib/features/lyrics/auto_sync/lyrics_auto_sync_controller.dart` | 進度 / 結果狀態(family by trackId)。 |
| `lib/features/player/widgets/lyrics_auto_sync_action.dart` | 進度對話框 + SnackBar 觸發。 |
| `LyricsModeMenu` | 「自動對時」選單項(僅 unsynced 歌詞時出現)。 |
| `app_en/zh_TW/zh_CN.arb` | l10n(其餘語言 fallback,待補 Google Sheet)。 |

### 部署前置(需專案維護者執行,需 GCP 權限)

1. **Function 環境變數**:`ALIGN_SERVICE_URL` = aeneas-align 的 Cloud Run 根 URL
   (無尾斜線、不含 `/align`);選填 `ALIGN_RATE_LIMIT_PER_DAY`。

   ```bash
   cd functions
   # 寫進 .env 或部署時帶旗標
   firebase deploy --only functions:align_lyrics
   ```

   `functions/requirements.txt` 已加 `google-auth`、`requests`。

2. **Function SA → Cloud Run invoker**:Function 取 ID token 後要能呼叫受保護的
   Cloud Run 服務,其執行 SA 需該服務的 `roles/run.invoker`。

   ```bash
   gcloud run services add-iam-policy-binding aeneas-align \
     --region asia-east1 \
     --member "serviceAccount:<FUNCTION_RUNTIME_SA>" \
     --role roles/run.invoker
   ```

3. **Cloud Run SA → 讀 Storage 物件**:對齊服務以 `audio.gcs` 下載 App 上傳到
   **Firebase 預設 bucket** 的音訊,其執行 SA 只需該 bucket 的
   `roles/storage.objectViewer`(後端不刪除,清理交給 lifecycle,故毋須刪除權限)。

   ```bash
   gsutil iam ch \
     serviceAccount:<RUN_SERVICE_ACCOUNT>:roles/storage.objectViewer \
     gs://<DEFAULT_FIREBASE_BUCKET>
   ```

   暫存音訊清理**完全交給 lifecycle**(後端不即時刪,涵蓋成功與失敗路徑)。
   **務必**對預設 bucket 套用 `align/` 前綴的規則:

   ```bash
   gcloud storage buckets update gs://<DEFAULT_FIREBASE_BUCKET> \
     --lifecycle-file=aeneas_service/storage-lifecycle.json
   ```

   規則檔 `aeneas_service/storage-lifecycle.json`:`align/` 前綴 1 天後自動刪。

   > 註:README 範例另建了 `seek-player-align` 純 GCS bucket;本實作改走 Firebase
   > 預設 bucket(`firebase_storage` 直接支援、免另設 signed URL / 規則)。

4. **Storage 安全規則**:確保登入使用者可寫 `align/{uid}/**`(僅自己的目錄)。

原始規劃(2026-06-14):
姊妹計畫:`plans/lyrics-auto-sync-whisperx.md`(同一任務的另一條技術路線,
字級時間更細、中文品質一般較佳)。
相關:`plans/lyrics-import.md`(地基:`LyricsEntity` / 統一 `Lyrics` 模型 /
parser)、`plans/lyrics-display.md`(同步捲動顯示,本任務產物直接複用)、
`plans/lyrics-auto-generate.md`(backlog 6,「沒有歌詞 → STT 從零產生」)。
本任務是其姊妹:**已有正確文字、只缺時間**。產物寫回同一 `LyricsEntity`
(`source = generated`、`format = lrc`)。

## 背景 / 目標

- 使用者已匯入 `.txt`(unsynced)歌詞:文字正確、但無時間戳,顯示時只能當
  靜態整篇。目標是**偵測每行起始時間**,產出同步歌詞(LRC),複用顯示計畫的
  逐行 highlight + 自動置中捲動 + 點行 seek。
- 與「自動產生(6)」的關鍵差異:
  - auto-generate:`audio → 文字 + 時間`(STT,從零辨識,丟棄既有文字)。
  - **本任務(auto-sync):`既有文字 + audio → 時間`**(forced alignment)。
    文字已知且已校對,問題更窄、對歌聲的容錯遠高,品質可期。

## 為何選 aeneas(本路線定位)

- **aeneas** 專為「文字 ↔ 朗讀音訊」對齊而生:espeak g2p + DTW,多語,
  直接輸出 SRT/VTT(可轉 LRC)。是 forced alignment 的**起步首選**——
  依賴單純、容器化容易、行級對齊夠用。
- 既然歌詞文字已知,正解就是 forced alignment(保留使用者已校對的文字,
  只補時間、不改字),遠優於純 ASR 轉寫。
- **限制 / 風險**:aeneas 靠 espeak g2p,**中文對齊品質需實測**;歌聲
  (拉長音、配樂干擾)比朗讀難對。若中文 / 細緻度不足 → 升 WhisperX 路線
  (見姊妹計畫)。

## 部署

- 專案已用 Firebase Functions(GCP),可把 aeneas 包成 **Cloud Run 容器**、
  由 Function 觸發。低頻使用在免費 / 低額度內可行。
- **音訊留在自家後端,隱私 / 版權風險最低**(本任務處理使用者本機音樂)。
  沒有穩定的「免費託管 forced-alignment API」——要正解品質基本得自架。

## 流程(後端對齊 service)

1. 取整段音訊解碼檔 / PCM(**注意:App 以 content URI 播放、不持有檔案本體**,
   需把音訊送後端;上傳成本 / 權限待確認——與 auto-generate 共用此未解問題)。
2. 取 `.txt` 逐行純文字(去空行 / 修飾)。
3. aeneas forced alignment → 每行起始(可含結束)時間。
4. 組 LRC(`[mm:ss.xx]` 每行)。
5. 回傳並存回 `LyricsEntity`(`source = generated`、`format = lrc`),
   `invalidate` 對應 `trackLyricsProvider`;顯示自動切到同步視圖。

## 建議路線(決策)

1. **以 aeneas 起步**:容器化、Cloud Run + Firebase Function 觸發,先驗證
   行級對齊在實際歌曲上的品質。
2. **中文 / 細緻度不足再升級**:轉 `plans/lyrics-auto-sync-whisperx.md`
   (wav2vec2 字級對齊)或 MFA。
3. **產物**:組成 LRC 寫回同一 `LyricsEntity`(`source = generated`、
   `format = lrc`);UI 標示「自動對時、可能有誤」,引導用編輯歌詞
   (backlog 7)修正。**顯示端(lyrics-display)完全不用改**。

## 邊界 / 風險 / 待調查

- **音訊取得**:content URI → 後端,與 auto-generate 同一待解問題(權限 /
  傳輸量 / 大檔)。
- **中文對齊**:aeneas 靠 espeak,中文需實測;不足則改 WhisperX。
- **歌聲難度**:拉長音 / 配樂干擾比朗讀難對,行級時間可能漂移。
- **失敗降級**:對齊失敗 / 信心低 → 保留原 unsynced 純文字,不硬塞錯時間。
- **成本**:Cloud Run 冷啟動 + 運算;免費 / 低額度內低頻可行,需估算。
- **隱私 / 版權**:自架明顯優於上傳第三方。

## 修改 / 新增(預計)

- `functions/`(或新 Cloud Run 容器)— aeneas forced-alignment 端點。
- `lib/features/lyrics/` — 呼叫對齊的 service + 衍生 provider
  (一檔一 provider,依 CLAUDE.md)。
- 產物複用 `LyricsEntity`(`source = generated`、`format = lrc`);
  顯示端 `lyrics-display` 不改。
- `l10n` — 對時中 / 失敗 / 「自動對時、可能有誤」標示
  (en + zh_TW + zh_CN,其餘 fallback,待補 Google Sheet)。

## 增補:`align_lyrics` 改為直存 Firestore、不經 RPC 回傳內文(2026-07-20)

- **動機**:歌詞內文沒必要經 callable RPC 往返一次——後端本來就要把結果存
  進 `users/{uid}/lyrics/{trackId}`(見上方增補的歌詞備份 snapshot 機制),
  RPC 只需回報成敗即可,省一份重複傳輸。
- **`trackId` 由選填改必填**:沒有 trackId 就沒有地方存,呼叫直接
  `invalid-argument`。
- **失敗語意收斂**:對時服務呼叫失敗(既有 422/502/503 映射)或寫入
  Firestore 失敗,一律以 `HttpsError` 回錯誤碼,不會有「callable 回 200
  但其實沒存到」的半套狀態——`_save_lyrics_snapshot` 寫入失敗會被
  `align_lyrics` 轉成 `internal` 錯誤(不同於 `generate_lyrics` 仍走靜默
  best-effort,因為後者本來就把 lrc 回傳給 client 自行寫本機,快照只是
  順手備份)。
- **App 端補一次讀回**:`lyrics_auto_sync_service.dart` 的 `autoSync` 在
  callable 成功後,改為讀一次剛寫入的 Firestore 文件取得內文,再寫進本機
  `LyricsEntity`。**這一步不只是圖方便**:歌詞同步(sync v5)是「本機 Isar
  有什麼、雲端就整份覆寫成什麼」的全量覆寫語意(見 `lyrics_sync.dart`
  `push()`)——如果本機完全沒有這筆資料,之後任何其他歌詞變更觸發的全量
  同步,會把後端剛寫的這份雲端快照當「本機沒有的多餘文件」誤刪。讀回寫本機
  可以避免這個資料遺失的坑,`generate_lyrics` 則不受影響(仍走 RPC 回傳
  + client 直接寫本機的原路徑,未變動)。

## 增補:改為 Cloud Tasks 非同步派工、不等對時完成(2026-07-20)

**動機**:上一版仍是「Function 同步呼叫 Cloud Run、等到對時跑完才回應」,
client 端 callable 逾時得設 10 分鐘,使用者體感是整個操作卡住直到完成。
本次改版讓 `align_lyrics` 只做「驗證 + 配額 + 派工」,立刻回應,實際對時在
背景跑,完成後由 Cloud Run 自己寫回 Firestore。

**架構(取代原本的同步 HTTP 代理)**:

1. `align_lyrics` 驗證欄位、檢查 `users/{uid}/lyrics/{trackId}` 是否已有
   `source=generated` 的快照(`_has_generated_snapshot`)——**有就直接回應
   `{saved: true, cached: true}`,不重跑**(這正是「trackId 已在 Firestore
   就回 200」的要求)。
2. 沒有快照 → 扣配額 → 用 `google-cloud-tasks` 把工作丟進 Cloud Tasks 佇列
   (`_enqueue_task`),任務內容是「POST 到 aeneas / WhisperX Cloud Run 的
   `/align`,帶 OIDC token」。**丟完立刻回應 `{saved: true, queued: true}`,
   不等 Cloud Run 處理完成**——這是「上傳完就回 200,不需要等到字幕產生」
   的核心。
3. 為何選 Cloud Tasks 而非「Function 內開背景執行緒後就回應」:Google 官方
   明確表示 Function 回應後,背景工作**不保證**能跑完(container 可能提早
   被回收),會有「使用者以為送出了,結果從沒真的存到 Firestore」的偶發
   遺失。Cloud Tasks 是獨立服務,任務一旦入列就不受 Function 生命週期影響,
   且內建重試。
4. **Cloud Run 端點自己寫 Firestore**:`aeneas_service/main.py` 與
   `whisperx_service/main.py` 的 `/align`(及 `/transcribe`)現在接受選填的
   `uid` / `trackId` / `title`(Cloud Tasks 派工時會帶上;直接手動呼叫測試時
   可省略,行為等同舊版)。算完結果後,若帶了 `uid`/`trackId` 就直接呼叫新增
   的 `_save_lyrics_snapshot` 寫入 `users/{uid}/lyrics/{trackId}`——因為
   `functions/main.py` 早就回應完畢、不在場做這件事了。寫入失敗回 5xx,讓
   Cloud Tasks 依佇列重試設定重送。
5. **失敗語意的取捨**:原本 Function 同步呼叫時,對時失敗(422
   `alignment_failed`)會即時映射成 callable 錯誤,client 顯示對應
   SnackBar、保留原 unsynced 文字。**改成非同步後這個即時回饋沒了**——
   Function 早就回了 200,對時是否真的成功只有 Cloud Run 端知道。目前策略
   是:失敗(對齊失敗 / 信心過低)Cloud Run 就是不寫 Firestore、只記
   log,不特別通知使用者(與既有「不硬塞錯時間、失敗降級」的既定哲學一致,
   也呼應本專案「背景操作靜默、不通知使用者」的慣例)。使用者體感是「送出後
   過一陣子打開歌詞頁,同步歌詞出現了(或沒出現,維持原狀)」。
6. **App 端配合改動**:`lyrics_auto_sync_service.dart` 的 `autoSync` 現在
   依回應的 `cached` 欄位分流——`cached: true` 才立即讀 Firestore 寫本機
   (沿用上一版增補的讀回邏輯);`queued: true` 則直接返回、不嘗試讀取(此刻
   內容還沒產生)。之後使用者重新開啟歌詞頁,`trackLyricsProvider` 的
   Firestore 快照降級路徑(本機無歌詞時讀雲端)會自然接住背景完成的結果,
   不需要新的通知或輪詢機制。
7. **Function 資源縮編**:不再同步等 Cloud Run,`timeout_sec` 從 600 降到
   60、`memory` 從 MB_512 降到 MB_256(派工本身很快)。

**待辦(需維護者 / GCP 權限,無法由程式碼代勞)**:

- 建立兩個 Cloud Tasks 佇列(對時 / 自動產生各一,避免共用佇列時互相排擠):
  ```bash
  gcloud tasks queues create align-lyrics --location=asia-east1
  gcloud tasks queues create generate-lyrics --location=asia-east1
  ```
  依實測失敗率調整佇列的重試設定(`--max-attempts`、`--min-backoff` /
  `--max-backoff`);**務必設合理的 `--max-attempts` 上限**(例如 3–5)——
  對齊 / 轉寫本身失敗(非暫時性,例如歌詞對不上)Cloud Run 目前設計是回
  5xx 只在「寫 Firestore 失敗」時,其餘失敗回應碼未在本次改版涵蓋,若沿用
  舊有的 422/500 邏輯需確認佇列不會無意義重試永久性失敗。
- 決定 / 建立 `TASKS_INVOKER_SERVICE_ACCOUNT`(Cloud Tasks 派工用來簽 OIDC
  token 的服務帳號):
  - 該帳號需具 aeneas / WhisperX 兩個 Cloud Run 服務的 `roles/run.invoker`
    (沿用舊版對 Function SA 的要求,現在改授權給這個新帳號)。
  - `align_lyrics`/`generate_lyrics` 所在 Function 的執行 SA 需具這個帳號的
    `roles/iam.serviceAccountTokenCreator`,才能請 Cloud Tasks 代簽 token。
  - 部署時設環境變數 `TASKS_INVOKER_SERVICE_ACCOUNT`(必填,否則派工會因
    OIDC token 帳號為空而失敗)、選填 `TASKS_LOCATION`(預設同 `_REGION`)、
    `ALIGN_TASK_QUEUE` / `GENERATE_TASK_QUEUE`(預設 `align-lyrics` /
    `generate-lyrics`,對應上面建立的佇列名稱)。
- **Cloud Run 服務帳號要能寫 Firestore**:aeneas / WhisperX 兩個 Cloud Run
  服務的執行 SA 需加 `roles/datastore.user`(或更細的自訂角色),否則
  `_save_lyrics_snapshot` 會失敗(進而讓任務被判定失敗、觸發重試)。
  ```bash
  gcloud projects add-iam-policy-binding <PROJECT_ID> \
    --member "serviceAccount:<CLOUD_RUN_SERVICE_ACCOUNT>" \
    --role roles/datastore.user
  ```
- `functions/main.py` 需要 Cloud Functions 執行環境具備
  `roles/cloudtasks.enqueuer`(建立/派工到佇列的權限),部署時一併確認。
- 兩個 Cloud Run 服務的 `requirements.txt` 新增了
  `google-cloud-firestore==2.16.0`,重新建置映像才會生效
  (`gcloud run deploy` 前記得重新 build)。
