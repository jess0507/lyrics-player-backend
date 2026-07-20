# 歌詞功能:播放中即時字幕(live captions,backlog 9)

狀態:**設計中、待實作**(2026-07-20)。
影響範圍(預計):`whisperx_service/`(新端點 + 新輕量轉寫模組)、
`functions/main.py`(新 callable + 新 quota 維度)、
`seek_player/lib/features/lyrics/live_caption/`(新 feature)、
`seek_player/lib/features/player/widgets/`(新顯示元件 + 觸發入口)、l10n。
相關:`plans/15-lyrics-auto-generate.md`(姊妹管線:完整品質、非即時)、
`plans/13-lyrics-auto-sync-whisperx.md`(對齊模型快取機制沿用)、
`seek_player/plans/6-statistics-isar-firestore-sync.md` 增補「歌詞備份」段落
(`users/{uid}/lyrics/{trackId}` snapshot,本功能與其銜接見下)。

## 背景 / 目標

現有 `generate_lyrics`(plan 15)是**一次性、整曲**的管線:壓縮上傳整首歌 →
後端 CPU 跑完整段 ASR + 字級對齊(常需 1–2 分鐘、逾時上限 10 分鐘)→ 一次回傳
完整 LRC。使用者觸發後只能等待,期間看不到任何內容。

本功能要解決的是「**沒有歌詞的曲目,播放當下就想看到字幕**」的體驗缺口:
使用者選擇「即時字幕(Beta)」後,字幕**跟著播放進度逐句冒出**,近似串流字幕,
而不必等整曲跑完的背景任務。

**關鍵洞察**:音檔本身**已完整存在本機**(不是麥克風即時輸入),不需要真正的
雙向串流協定。「即時」在這裡等於「client 端依播放進度分段預先送出、
server 端用快速模型即時回應」的**流水線(pipelining)**,基礎設施可大幅沿用
既有的 Function-proxy-to-Cloud-Run 架構。

## 與既有管線的關係(互補、非取代)

| | `generate_lyrics`(plan 15) | 本功能(live captions) |
| --- | --- | --- |
| 觸發 | 使用者手動點「自動產生歌詞」 | 使用者手動開啟「即時字幕」模式 |
| 範圍 | 整曲一次送出 | 依播放進度切小段、連續送出 |
| 模型 | `WHISPER_MODEL_SIZE`(預設 small)+ 字級對齊 | 更小/更快模型,**不做字級對齊** |
| 時間戳精度 | wav2vec2 字級對齊 | 段落即為送出視窗的絕對時間(client 已知),不需對齊模型 |
| 產物 | 存回 `LyricsEntity`(`source=generated`),進一步靠 sync v5 備份到 Firestore | **不落地**,純顯示用的暫時性字幕,離開播放頁即丟棄 |
| 定位 | 「正式草稿」,可編輯 / 同步備份 | 「立即反饋」,體驗層,品質更粗略 |

**建議:開啟即時字幕時,順手在背景觸發一次 `generate_lyrics`**(沿用既有
`lyrics_background_runner.dart` 前景服務,同一份配額),讓「正式版」在使用者
聽歌途中默默跑完。完成後(`LyricsBackgroundEventType.done` → `invalidate
trackLyricsProvider`)畫面自動從即時字幕切換成同步捲動的正式歌詞——
與目前已實作的「本機無歌詞 → 讀 Firestore 快照」降級路徑（見
`functions/main.py` `_save_lyrics_snapshot`、`track_lyrics_provider.dart`
`_fetchRemoteLyricsSnapshot`）自然銜接,不用另建一套完成通知機制。

## 為何不需要真正的雙向串流(WebSocket / gRPC streaming)

音檔已在本機、可任意 seek,client 永遠知道「現在要哪一段」,不像麥克風輸入
只能被動等資料到來。因此方案退化為:

1. Client 依播放位置維護一個「已送出涵蓋到第 N 秒」的游標,**提前**於
   播放頭 1–2 個視窗裁切送出(不是等播放頭走到才送,那樣會來不及)。
2. 每個視窗是一次獨立、短命的 HTTP 請求 / 回應,沒有長連線,重試 / 斷線
   恢復都簡單。
3. 之後若實測延遲不夠低,才需要升級成真正的串流(見「待調查」)。

## 分段策略

- **視窗長度**:建議 6–8 秒,前後與相鄰視窗重疊 ~1 秒,避免切在字詞中間。
- **裁切**:沿用 `audio_compressor.dart` 的 ffmpeg 慣例,改用
  `-ss <start> -t <len>` 直接對本機音檔(`track_audio_resolver.dart` 解出的
  真實路徑)裁切 + 壓縮單聲道 16kHz,不需先讀整檔進記憶體。
- **時間戳不需對齊模型**:因為 client 明確知道每個視窗對應音檔的絕對秒數,
  後端只要回傳「這段的文字」,字幕的起始時間 = 該視窗的起始秒數。
  **本功能完全跳過 wav2vec2 字級對齊**,只做 ASR——比 `generate_lyrics`
  簡化許多,也是成本大幅降低的關鍵。
- **靜音 / 前奏 /間奏處理**:啟用 faster-whisper 的 VAD filter,近乎無語音的
  視窗直接跳過辨識(省成本、避免辨識出幻覺文字)。
- **重疊去重**:相鄰視窗重疊區間若重複辨識出同一句尾 / 句首,client 端以
  簡單的字串後綴 / 前綴比對去重(可接受不完美,live 字幕本就是草稿定位)。

## 後端

### 新模組 `whisperx_service/live_caption.py`

鏡像 `transcriber.py` 的行程內模型快取模式(`_load_asr_model`),但:

- 獨立模型大小環境變數 `LIVE_CAPTION_MODEL_SIZE`(預設 `tiny` 或 `base`,
  待實測選定——延遲優先,犧牲一些準確度可接受)。
- 不呼叫 `aligner._load_align_model` / `whisperx.align`:純 ASR,
  `model.transcribe(chunk_audio, batch_size=...)` 直接取 segments 拼成一段文字
  即可(視窗本身已經夠短,不需要再切 segment)。
- 啟用 VAD(`vad_filter=True` 等 faster-whisper 參數),近靜音視窗回空字串。

### 新端點 `POST /transcribe_live_chunk`(`whisperx_service/main.py`)

Request:
```json
{ "language": "zh-TW", "audio": { "inlineBase64": "...", "format": "m4a" } }
```
- 視窗檔案小(數百 KB 內),走 `inlineBase64` 即可,不必為每個視窗上傳 GCS
  中轉(GCS 上傳 + 下載的往返延遲對這個場景反而是主要開銷)。
- `language`:比照 `/align` 的作法,以 App locale 當提示鎖定語言,避免短視窗
  誤判語言。

Response 200:`{ "text": "..." }`(無需 begin/end,client 自己知道視窗秒數)。
空/靜音視窗回 `{ "text": "" }`(200,非錯誤)。

### 新 callable `live_caption_chunk`(`functions/main.py`)

- 複用 `_require_uid`、ID token 注入模式,路由固定到 `_WHISPERX_SERVICE_URL`。
- **獨立 rate limit 維度**:不能沿用「次數」配額(一次播放可能觸發數十次
  請求),改用**累積秒數**:`live_caption_usage/{uid}` 記錄當日已處理的音訊
  秒數,超過 `LIVE_CAPTION_SECONDS_PER_DAY`(預設值待定,例如 1800 秒=
  30 分鐘)即回 `resource-exhausted`,client 端提示「今日即時字幕額度已用完,
  可改用『自動產生歌詞』」。
- **批次化以攤提 callable 開銷**(見下「延遲 vs 開銷取捨」):v1 建議
  client 端每次打包 **3–4 個視窗**(約 20–25 秒音訊)一次呼叫,而非每個
  視窗各打一次——大幅減少 callable 冷啟動 / 配額交易的固定開銷佔比。
  Request 可為 `{ "chunks": [{startSec, audio}, ...], "language": ... }`,
  Response `{ "results": [{startSec, text}, ...] }`。

## 前端(`seek_player`)

新增 `lib/features/lyrics/live_caption/`(鏡像 `auto_sync/` / `auto_generate/`,
依 CLAUDE.md 一檔一 provider):

- **`live_caption_service.dart`**:核心排程器。
  - 監聽 `AudioPlayerService.positionDataStream`(`core/audio/audio_player_service.dart`)
    取得目前播放秒數。
  - 維護「已送出涵蓋到第 N 秒」游標,當播放頭接近涵蓋邊界前(例如剩餘 <10 秒
    緩衝)就用 `TrackAudioResolver` 解出的本機路徑,依分段策略裁切下一批
    視窗(複用/擴充 `audio_compressor.dart` 的 ffmpeg 呼叫,改吃
    `-ss/-t`)、打包呼叫 `live_caption_chunk`。
  - seek(使用者拖進度條)時捨棄舊排程、以新位置重新起算游標——不用回填
    已跳過的片段。
  - 暫停時停止排程(不預先燒配額);背景播放(App 切到背景)是否繼續跑
    留待「待調查」。
- **`live_caption_controller.dart`**:Riverpod state(family by trackId),
  持有目前已收到的 `{startSec, text}` 清單 + idle/running/error 狀態,
  供顯示端消費;曲目切換時重置。
- **顯示**:`lib/features/player/widgets/lyrics_live_view.dart`(新),
  依目前播放秒數在已收到清單中找最新一句 highlight,樣式比照
  `LyricsSyncedView` 但資料源是 controller 的記憶體清單,不經
  `trackLyricsProvider` / Isar。
- **觸發入口**:`lyrics_view.dart` 的 `_EmptyLyrics` 加一顆「即時字幕
  (Beta)」按鈕(與既有「自動產生歌詞」、「匯入」並列),點下後:
  1. 啟動 `live_caption_service`。
  2. 依「結論」段決策,同時透過 `lyricsBackgroundRunnerProvider` 觸發一次
     背景 `generate_lyrics`(若尚未有背景任務在跑)。
  3. 背景任務完成、`trackLyricsProvider` 被 invalidate 後,`LyricsView`
     自然拿到正式 `Lyrics`,從 `LyricsLiveView` 切回
     `LyricsSyncedView`——**不需要新的完成通知機制**,沿用現有
     provider 重讀機制即可。

## 邊界 / 風險 / 待調查

- **CPU 上小模型的即時率(RTF)未知,需先做基準測試**:8 秒視窗若處理
  超過 8 秒,player 端進度會被追過、字幕永遠落後甚至堆積請求。建議實作
  client pipeline 之前,先手寫一支基準腳本(`whisperx_service/` 下,類似
  `api_smoke.py`)量測 `tiny` / `base` 在 Cloud Run CPU 上處理 8 秒 clip 的
  實際耗時(含冷啟動與熱機兩種情境)。若 RTF 不夠,選項包括:縮小視窗、
  換更小模型、或評估 GPU Cloud Run(成本大幅上升)。
- **成本 / 濫用控制**:一次完整聽歌可能觸發數十次請求,遠比現有「次數配額」
  重。採「秒數配額」+ 批次化;必要時可再加「同時最多一個 track 在跑即時
  字幕」的 client 端限制(比照背景任務 `_active` 單任務限制)。
- **延遲 vs callable 開銷取捨**:v1 建議批次(3–4 視窗/請求)換取更低
  基礎設施複雜度;若批次延遲仍太高、需要更即時的反饋,再考慮
  client 直連 Cloud Run(帶自己的 Firebase ID token,Flask 端驗證)取代
  Function-proxy 以省一層開銷——**這是架構層級的改動,建議先以 v1 批次方案
  驗證實際體驗是否已經夠好,不要一開始就上直連**。
- **App 背景 / 螢幕關閉時是否繼續跑**:與 `lyrics_background_runner.dart`
  的「一次性長任務、允許背景執行」不同,即時字幕理論上只在使用者盯著
  播放頁看歌詞時有意義。建議 v1 **範圍限定在前景播放 + 播放頁開著**,
  App 切背景就暫停排程(離開頁面不燒配額);換手機情境不在 v1 範圍。
- **語言誤判**:短視窗比整曲更容易誤判語言,務必用 App locale 鎖定
  `language`,不要交給模型自動偵測(比照既有 auto-generate 決策)。
- **正式版與即時版同時顯示的競態**:若使用者在即時字幕跑到一半時,背景
  `generate_lyrics` 已完成並寫回,`LyricsView` 該立刻切換或等目前句子讀完
  再切,屬 UX 細節,留待實作時決定(建議:立即切換,正式版品質較高)。
- **既有 `LyricsAutoGenerateError.busy` 語意**:背景 runner 一次只跑一個
  任務,若使用者在即時字幕觸發背景 generate 的同時,又手動點了「自動產生
  歌詞」,需複用既有 busy 拒絕邏輯,避免重複任務。

## 取捨決策(建議)

1. **不做真正雙向串流,靠 client 端依播放進度分段預先送出**——音檔已在
   本機是關鍵前提,基礎設施可大幅沿用現有 Function-proxy 架構。
2. **即時字幕不做字級對齊**,只做 ASR:時間戳用視窗起始秒數頂替,
   省去對齊模型開銷,是延遲與成本的關鍵簡化。
3. **不落地儲存**:即時字幕是體驗層、離開播放頁即丟,正式產物仍走既有
   `generate_lyrics` → `LyricsEntity` → sync v5 → Firestore 快照這條路。
4. **開啟即時字幕時順手背景觸發 `generate_lyrics`**,完成後自動切換到
   正式歌詞,銜接現有的 provider 重讀機制,不必新建通知管道。
5. **v1 用秒數配額 + client 端批次(3–4 視窗/請求)**,不急著上直連
   Cloud Run 或真正串流;等實測延遲/體驗不夠好,再評估升級。
6. **範圍限定前景播放 + 播放頁開著**,不做背景/鎖屏下的即時字幕。

## 修改 / 新增(預計)

- `whisperx_service/live_caption.py`(新)、`main.py`(加
  `/transcribe_live_chunk`)、README 補合約、基準測試腳本(先於 client 端
  實作前完成,驗證 RTF 假設)。
- `functions/main.py`(新 `live_caption_chunk` callable + `live_caption_usage`
  秒數配額)。
- `seek_player/lib/features/lyrics/live_caption/`
  (`live_caption_service.dart` + `live_caption_controller.dart`)。
- `seek_player/lib/features/player/widgets/lyrics_live_view.dart`(新)、
  `lyrics_view.dart`(`_EmptyLyrics` 加「即時字幕(Beta)」入口)。
- `l10n`:即時字幕相關字串(啟動 / 額度用盡 / 錯誤降級提示),
  en + zh_TW + zh_CN,其餘 fallback,待補 Google Sheet。

## 待辦(需維護者決定 / 需 GCP 權限驗證)

- 先跑後端基準測試,決定 `LIVE_CAPTION_MODEL_SIZE` 與視窗長度(8 秒是否
  可行,或需縮到 5–6 秒)。
- 決定 `LIVE_CAPTION_SECONDS_PER_DAY` 預設值(參考現有 align 20 次/日、
  generate 5 次/日的保守精神抓一個合理秒數上限)。
- 實測後決定是否需要 v2(client 直連 Cloud Run,或改真正串流協定)。
