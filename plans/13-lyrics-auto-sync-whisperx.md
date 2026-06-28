# 歌詞功能:自動對時(txt → 同步)— WhisperX 路線(backlog 5)

狀態:**後端容器已實作、待部署驗證;Flutter 端 / Function 無需改動**(2026-06-24)。

## 實作進度 / 決策(2026-06-24)

- **關鍵發現:本路線與 aeneas 路線(plan 12)是同一個 HTTP 合約。** aeneas 路線已
  完整落地(`aeneas_service/` 容器 + `functions/main.py` 的 `align_lyrics` callable
  + Flutter `lib/features/lyrics/auto_sync/`)。WhisperX 只是**換掉 Cloud Run 容器
  內部的對齊引擎**,`POST /align` / `GET /healthz` 的 request / response / 錯誤碼
  完全不變 → **Flutter 端與 Function 程式碼皆不需改動**。
- **新增獨立容器 `whisperx_service/`**(鏡像 `aeneas_service/` 的結構與風格):
  - `main.py`(`/healthz`、`/align`,與 aeneas 版幾乎相同)、`aligner.py`
    (WhisperX 對齊核心)、`lrc.py`(秒 → `[mm:ss.xx]`,與 aeneas 共用慣例)、
    `test_lrc.py` + `test_align.py`(純邏輯單元測試,**19 項全過**,免 whisperx /
    torch / ffmpeg)。
  - `Dockerfile` 用 **Python 3.11 + torch CPU wheel + whisperx 3.1.5**;模型權重
    執行期由 HuggingFace 下載(冷啟動較慢,屬已知取捨)。
- **對齊策略(純 forced alignment、不做 ASR)**:把所有歌詞行串接成「一個涵蓋整段
  音訊的 segment」交給 `whisperx.align(..., return_char_alignments=True)` 取**字級**
  時間;因 segment 文字即各行串接,char 串流逐字對應 → 依各行字元範圍切回逐行
  begin/end。中 / 日不插空格分隔,其餘語言用單一空格(`separator_for`)。char 串流
  長度與串接文字不符時退用比例分配(`build_fragments` 內含 fallback)。
- **失敗降級**:對齊覆蓋率(取得起始時間的行數佔比)低於 `WHISPERX_MIN_COVERAGE`
  (預設 0.6)→ 回 `alignment_failed`(422 / callable `failed-precondition`),
  App 既有邏輯會保留原 unsynced 文字。字級時間保留於 `fragments`,供未來逐字
  highlight。
- **語言碼**:`normalize_language` 轉 whisperx 二字母碼(`zh*`→`zh`、`eng`→`en`、
  `jpn`→`ja`…),與 aeneas 的 ISO 639-3 不同,故各自成檔。
- **CI**:新增 `.github/workflows/cloud-run-whisperx-deploy.yml`(部署 `whisperx-align`
  服務),沿用 `GCP_RUN_DEPLOY_SA` / 同 bucket / lifecycle / IAM。
- **切換路線**:把 Function 的 `ALIGN_SERVICE_URL` 指向 whisperx-align 的 Cloud Run
  URL 即可(兩容器可並存),其餘不動。詳見 `whisperx_service/README.md`。
- **待辦(需專案維護者、需 GCP 權限)**:部署容器、設 `run.invoker`、改
  `ALIGN_SERVICE_URL` 重新部署 Function、實測中文 / 歌聲對齊品質與冷啟動 / 成本。

---

原始規劃(2026-06-14):
第三條路線見 `plans/13b-lyrics-auto-sync-whispercpp-ondevice.md`(whisper.cpp
**手機端本機執行、不需後端**;惟為 ASR 回貼非 forced alignment)。
姊妹計畫:`plans/lyrics-auto-sync-aeneas.md`(同一任務的起步路線,依賴單純、
容器化容易、行級對齊)。本路線為**升級選項**:字級時間更細、中文對齊一般較佳。
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

## 為何選 WhisperX(本路線定位)

- **WhisperX / NeMo Forced Aligner**:以 **wav2vec2 對齊**,產出**字級**
  時間戳,粒度比 aeneas 的行級更細。
- **中文對齊品質一般優於 aeneas 的 espeak g2p 路線**——當 aeneas 行級對齊在
  中文 / 歌聲上漂移時,這是主要升級方向。
- 仍是 forced alignment 的範疇:保留使用者已校對的文字,只補時間、不改字。
  (若要再追求極致準確度,MFA 最準但需各語言發音字典、較重,屬更後期選項。)
- **成本權衡**:wav2vec2 模型較重,Cloud Run 運算 / 記憶體需求高於 aeneas,
  冷啟動與費用需估算。

## 部署

- 專案已用 Firebase Functions(GCP),把 WhisperX 包成 **Cloud Run 容器**、
  由 Function 觸發(模型較大,留意映像體積 / 記憶體 / GPU 與否)。
- **音訊留在自家後端,隱私 / 版權風險最低**(本任務處理使用者本機音樂)。
  沒有穩定的「免費託管 forced-alignment API」——要正解品質基本得自架。

## 流程(後端對齊 service)

1. 取整段音訊解碼檔 / PCM(**注意:App 以 content URI 播放、不持有檔案本體**,
   需把音訊送後端;上傳成本 / 權限待確認——與 auto-generate 共用此未解問題)。
2. 取 `.txt` 逐行純文字(去空行 / 修飾)。
3. WhisperX forced alignment(wav2vec2)→ 字級時間;聚合回每行起始
   (可含結束)時間。
4. 組 LRC(`[mm:ss.xx]` 每行;字級時間預留給未來逐字 highlight)。
5. 回傳並存回 `LyricsEntity`(`source = generated`、`format = lrc`),
   `invalidate` 對應 `trackLyricsProvider`;顯示自動切到同步視圖。

## 建議路線(決策)

1. **作為 aeneas 的升級**:當 `lyrics-auto-sync-aeneas.md` 在中文 / 細緻度
   實測不足時切到本路線。若一開始就以中文歌為主,可直接評估 WhisperX。
2. **字級時間**:LRC 先用行級,字級時間保留供未來逐字 highlight。
3. **產物**:組成 LRC 寫回同一 `LyricsEntity`(`source = generated`、
   `format = lrc`);UI 標示「自動對時、可能有誤」,引導用編輯歌詞
   (backlog 7)修正。**顯示端(lyrics-display)完全不用改**。

## 邊界 / 風險 / 待調查

- **音訊取得**:content URI → 後端,與 auto-generate 同一待解問題(權限 /
  傳輸量 / 大檔)。
- **模型重量**:wav2vec2 映像 / 記憶體 / 冷啟動成本高於 aeneas,需估算
  (是否需 GPU)。
- **歌聲難度**:拉長音 / 配樂干擾仍是挑戰,但字級對齊容錯通常較佳。
- **失敗降級**:對齊失敗 / 信心低 → 保留原 unsynced 純文字,不硬塞錯時間。
- **成本**:Cloud Run 運算 + 冷啟動;低頻使用需估算是否仍在可接受額度。
- **隱私 / 版權**:自架明顯優於上傳第三方。

## 修改 / 新增(預計)

- `functions/`(或新 Cloud Run 容器)— WhisperX forced-alignment 端點。
- `lib/features/lyrics/` — 呼叫對齊的 service + 衍生 provider
  (一檔一 provider,依 CLAUDE.md)。
- 產物複用 `LyricsEntity`(`source = generated`、`format = lrc`);
  顯示端 `lyrics-display` 不改。
- `l10n` — 對時中 / 失敗 / 「自動對時、可能有誤」標示
  (en + zh_TW + zh_CN,其餘 fallback,待補 Google Sheet)。
