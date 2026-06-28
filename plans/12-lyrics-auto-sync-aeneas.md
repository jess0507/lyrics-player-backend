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
4. **呼叫** callable `align_lyrics`,帶 `{lines, bucket, object, language, format}`。
   - `lines`:由現存 `LyricsEntity` parse 後取非空白行(與後端再濾空行一一對應)。
   - `language`:取裝置 locale 的 BCP-47(後端正規化為 aeneas ISO 639-3)。
5. Function 驗 uid → rate limit → 取 Cloud Run ID token → 轉呼 `/align`
   → 回 `{lrc}`。暫存音訊不即時刪,完全交給 bucket lifecycle 清理(見部署)。
6. App 把 LRC 寫回**同一** `LyricsEntity`(`source=generated`、`format=lrc`)、
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
