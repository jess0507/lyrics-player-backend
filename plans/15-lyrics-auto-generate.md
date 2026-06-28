# 歌詞功能:自動產生(audio → 文字 + 時間)— WhisperX 全管線(backlog 6)

狀態:**已設計、待實作**(2026-06-24,自原佔位改寫)。
影響範圍(預計):`whisperx_service/`(新端點)、`functions/main.py`(新 callable)、
`lib/features/lyrics/auto_generate/`(新 feature,鏡像 `auto_sync/`)、
`lib/features/player/widgets/`(選單動作)、`l10n`。

## 與 auto-sync(plan 13)的關係:同一條管線、只多接「轉寫」前半

- **auto-sync(已落地)**:`既有文字 + audio → 時間`(forced alignment)。
  使用者已匯入 `.txt`,後端只補每行起始時間。
- **本任務 auto-generate**:`audio → 文字 + 時間`(ASR 轉寫 + 對齊)。
  曲目**完全沒有歌詞**(backlog 4 線上搜尋也失敗的 fallback),從音訊直接辨識。

關鍵洞察:**WhisperX 標準管線本身就是「transcribe → align」兩段**,而 align 這後半
在 plan 13 已完整落地於 `whisperx_service/aligner.py`。本任務只需補上前半的
**ASR 轉寫**(`whisperx.load_model` + `model.transcribe`),其餘(字級對齊、LRC 組裝、
語言正規化、GCS 音訊取得、Dockerfile、Function 注入身分 / rate limit、Flutter 壓縮 /
上傳 / 進度 UI / 寫回 `LyricsEntity`)幾乎**整套複用 auto-sync 的既有結構**。

> 產物與 auto-sync 完全相同:組成 LRC 寫回同一 `LyricsEntity`
> (`source = generated`、`format = lrc`),`invalidate(trackLyricsProvider)`,
> 顯示端(`lyrics-display`)**完全不需改動**即可同步捲動。

## 背景 / 目標

- 對沒有現成歌詞檔、線上也搜不到的曲目,以語音辨識(STT/ASR)從音訊直接產生歌詞,
  理想含逐行時間戳以複用同步捲動顯示。
- 與 auto-sync 的差異不只是「少一份文字輸入」,而是**問題難度本質不同**:
  - auto-sync:文字已知且已校對,只解時間,對歌聲容錯高、品質可期。
  - **auto-generate:文字未知,要在配樂干擾下辨識歌聲——這是公認困難場景**,
    辨識錯字、漏字、斷句不像真實歌詞分行都屬常態。產物定位為「草稿」,
    UI 明確標示「自動產生、可能有誤」,引導用編輯歌詞(backlog 7)修正。

## 為何沿用 WhisperX(而非新引擎)

- 容器、torch / wav2vec2 依賴、CI、GCS / lifecycle、Function 路由、Flutter 壓縮上傳
  全已為 plan 13 建好;auto-generate 復用同一套基礎設施,**邊際成本最低**。
- WhisperX = faster-whisper(ASR)+ wav2vec2(字級對齊),一個函式庫涵蓋兩段,
  且 align 後半我們已驗證可用。
- 自架、音訊留在自家後端,隱私 / 版權風險最低(處理使用者本機音樂)。

## 後端:`whisperx_service/` 新增轉寫端點

新增 **`POST /transcribe`**(與 `/align` 並存於同容器),不影響既有 auto-sync。

- **新檔 `transcriber.py`**(鏡像 `aligner.py` 的風格與延後 import 慣例):
  1. `whisperx.load_audio(path)` 解 16kHz mono(與 aligner 共用,可抽共用 helper)。
  2. `model = whisperx.load_model(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")`
     → `result = model.transcribe(audio, batch_size=…, language=hint_or_None)`
     得 `segments`(各含 text、start、end)與偵測語言。
  3. 以偵測 / 提示語言載入對齊模型(**直接複用 `aligner._load_align_model` 與其
     行程內快取**),`whisperx.align(...)` 取字級時間,聚合回逐行 begin/end。
  4. 各 segment 文字即一「行」→ 複用 `lrc.build_lrc` 組 LRC。
- **模型大小取捨**:以 `WHISPER_MODEL_SIZE` 環境變數控制(`tiny`/`base`/`small`/
  `medium`/`large-v3`)。CPU 上 large 對長曲可能逼近 600s 逾時且記憶體吃緊;
  **建議預設 `small` 或 `medium` 起步**,實測歌聲品質 / 耗時後再調。
  ASR 模型權重首呼由 HuggingFace 下載(冷啟動更慢,屬已知取捨)。
- **複用既有**:`main.py` 的 `_resolve_audio`(GCS / inlineBase64)、`_MAX_AUDIO_BYTES`、
  `_error` 統一錯誤格式、`normalize_language` / `separator_for`、`lrc.py` 全部沿用。
- **失敗降級**:轉寫無 segment / 平均信心(`avg_logprob`)過低 / 對齊覆蓋率過低
  → 回 `transcription_failed`(422)。寧可不產出,也不塞一篇亂碼。
- **單元測試**:`transcriber.py` 的 segment→行→LRC 純邏輯抽出可測(免 whisper/torch),
  比照 `test_align.py`。

### `/transcribe` 合約(對齊 `/align` 風格)

Request:
```json
{ "language": "zh-TW", "audio": { "gcs": {"bucket": "...", "object": "generate/uid/x.m4a"}, "format": "m4a" } }
```
- `language` 選填:**有提示就鎖定語言**(避免歌聲誤判),留空則 whisper 自動偵測。
- `audio` 同 `/align`(`gcs` 或 `inlineBase64`)。

Response 200:`{ "lrc": "...", "fragments": [...], "language": "zh" }`(與 `/align` 同形)。

錯誤碼:`invalid_request`(400)、`audio_fetch_failed`(502)、
`transcription_failed`(422,對應 auto-sync 的 `alignment_failed`)、`internal`(500)。

## Function:新增 `generate_lyrics` callable

`functions/main.py` 新增與 `align_lyrics` 平行的 callable(因 request **無 `lines`**,
合約不同,獨立成支較清楚;**大量複用既有 helper**):

- 複用 `_require_uid`、ID token 注入(audience = WhisperX 服務根 URL)、
  `requests.post` 轉呼模式;路由固定到 `_WHISPERX_SERVICE_URL`(ASR 僅 WhisperX 有)。
- **rate limit**:轉寫比對齊更重,**用獨立、更低的每日上限**(例如
  `GENERATE_RATE_LIMIT_PER_DAY`,預設低於對時的 20);`align_usage` 改用獨立 doc
  `generate_usage/{uid}` 或同 doc 加欄位,避免互相吃額度。
- 轉呼 `/transcribe`,回 `{lrc, language}`;錯誤碼映射比照 `align_lyrics`
  (422→`failed-precondition`、502/503→`unavailable`…)。

## Flutter:新增 `lib/features/lyrics/auto_generate/`(鏡像 `auto_sync/`)

嚴守 CLAUDE.md「一檔一 provider、provider 不與 widget 同檔」:

- `lyrics_auto_generate_service.dart`:`LyricsAutoGenerateService` + 其 provider。
  流程鏡像 `LyricsAutoSyncService`,**差異**:
  - **不讀既有歌詞**(本就為無歌詞曲目);觸發前置條件是「**沒有**歌詞」而非「有純文字」。
  - 複用 `audioCompressorProvider` 壓縮、`FirebaseStorage` 上傳(改前綴 `generate/{uid}/`)、
    `_resolveAudioPath`(可抽共用 helper)。
  - 呼叫 `generate_lyrics` callable;回傳 LRC 寫回 `LyricsEntity`
    (`source = generated`、`format = lrc`)、`invalidate(trackLyricsProvider)`。
  - 階段 enum 去掉「需既有文字」相關,沿用 compressing / uploading / 改名
    transcribing(對應 aligning)。錯誤 enum 比照,新增 `transcriptionFailed`。
- `lyrics_auto_generate_controller.dart`:鏡像 `LyricsAutoSyncController`
  (family by trackId、idle/running/success/failure 狀態)。

### UI 整合(`lib/features/player/widgets/`)

- `lyrics_menu_action.dart`:新增 `LyricsMenuAction.autoGenerate`(icon 如
  `Icons.lyrics`),**僅在 `!hasLyrics` 時顯示**(與 auto-sync 的 `canAutoSync`
  互補:有純文字才對時、完全沒歌詞才產生)。`lyricsMenuActions` 加參數 / 分支。
- `lyrics_auto_generate_action.dart`:鏡像 `lyrics_auto_sync_action.dart` 的進度框 +
  SnackBar(可抽共用 dialog,但先各自一份維持清晰)。

## l10n(en + zh_TW + zh_CN,其餘 fallback)

新增字串:`lyrics_auto_generate`、`..._transcribing`、`..._success`、`..._failed`、
`..._need_login`、`..._rate_limited`、`..._no_audio`、`..._network`,以及顯示端的
「自動產生、可能有誤」提示。比照既有 `lyrics_auto_sync_*` 補齊(待補 Google Sheet)。

## 邊界 / 風險 / 待調查

- **歌聲 ASR 品質(最大風險)**:whisper 以語音訓練,配樂 + 拉長音 + 和聲下辨識率
  明顯低於說話。錯字 / 漏句 / 斷行不符真實歌詞都會發生。**產物定位為草稿**。
  未來可評估 **demucs 人聲分離**前處理提升品質(較重,屬後期選項)。
- **CPU 耗時 / 逾時**:transcribe + align 雙段,長曲在 CPU 上更逼近 600s;
  必要時評估縮短音訊 / 升記憶體 / 改 GPU。模型大小是主要旋鈕。
- **語言偵測誤判**:歌聲使 whisper 語言偵測不穩;**優先用 App locale 當提示鎖定語言**。
- **斷句 = ASR segment 而非真實歌詞行**:LRC 行界以 whisper segment 為準,
  不會等於原曲分行,可接受(generated 草稿,使用者可編輯)。
- **成本 / 濫用**:比對時更重 → 獨立且更低的 rate limit;低頻使用仍需估算額度。
- **音訊存取**:沿用 auto-sync 已解的 content URI → 壓縮 → GCS 路徑,無新問題。
- **失敗降級**:轉寫 / 對齊失敗或信心過低 → 不產出、提示失敗,不塞亂碼。

## 取捨決策(建議)

1. **沿用 WhisperX 容器加端點**,不另起引擎——基礎設施全可複用,邊際成本最低。
2. **模型預設 `small`/`medium`**,以 env var 可調;先求能跑、再實測歌聲調品質 / 成本。
3. **語言以 App locale 鎖定**(非自動偵測),歌聲場景更穩。
4. **產物即草稿**:`source = generated`,UI 標「可能有誤」,導向編輯歌詞修正;
   顯示端不改。
5. **demucs 人聲分離**列為提升品質的後期選項,首版不做。

## 修改 / 新增(預計)

- `whisperx_service/transcriber.py`(新)、`main.py`(加 `/transcribe`)、
  `Dockerfile` / `requirements.txt`(確認含 ASR 模型所需,faster-whisper 已隨 whisperx)、
  `test_transcribe.py`(新,純邏輯)、`README.md`(補 `/transcribe` 合約)。
- `functions/main.py`(新 `generate_lyrics` callable + 獨立 rate limit)。
- `lib/features/lyrics/auto_generate/`(service + controller,一檔一 provider)。
- `lib/features/player/widgets/lyrics_menu_action.dart`(加 autoGenerate 分支)、
  `lyrics_auto_generate_action.dart`(新進度 / SnackBar)。
- `lib/l10n/`(新字串)。
- 產物複用 `LyricsEntity`(`source = generated`、`format = lrc`);顯示端不改。

## 待辦(需專案維護者 / GCP 權限)

- 後端部署新端點後實測**中文歌聲**轉寫品質、耗時、冷啟動與成本(決定模型大小)。
- 設 `WHISPERX_SERVICE_URL`(已存在)即可路由;確認 `generate_lyrics` 的
  `run.invoker` 與 `generate/` 前綴的 bucket lifecycle(比照 `align/`)。
- 依實測決定是否需要 demucs 前處理 / GPU。

---

原始佔位(2026-06-14):本任務曾標「最遠期,暫不設計」。WhisperX 對齊路線(plan 13)
落地後,自動產生只需補 ASR 前半 + 鏡像 auto-sync 的 Flutter / Function 結構,
故於 2026-06-24 補上具體設計。相關:`plans/11-lyrics-import.md`(地基:`LyricsEntity`
/ 統一 `Lyrics` 模型 / parser)、`plans/10-lyrics-display.md`(顯示,產物直接複用)、
`plans/13-lyrics-auto-sync-whisperx.md`(姊妹:已有文字、只缺時間)。
