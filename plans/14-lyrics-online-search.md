# 歌詞功能:上網搜尋(backlog 4)

狀態:**規劃中**(2026-06-14 起草,未實作;排在匯入 / 顯示地基之後)。
相關:`plans/lyrics-import.md`(複用 `LyricsEntity` / `lyrics_repository` /
`lyrics_parser`,搜尋結果寫回同一個 `LyricsEntity`,source = online)、
`plans/lyrics-display.md`(歌詞視圖空狀態與選單提供搜尋入口)、
`plans/becklog.md`(項目 4)。

## 修改 / 新增程式碼檔案

新增(`lib/features/lyrics/`,依 CLAUDE.md 一檔一 provider):

- `lyrics_search_result.dart` — 候選結果 model(id / trackName / artistName /
  albumName / durationSec / hasSynced / plainLyrics / syncedLyrics)。
- `lyrics_online_search_service.dart` — LRCLIB API client + service provider
  (查詢、解析 JSON、依 duration 排序候選)。
- `lyrics_search_controller.dart` — 依目前曲目觸發搜尋的衍生狀態
  (查詢中 / 候選清單 / 失敗;`AsyncValue`)。
- `lib/features/player/widgets/lyrics_search_sheet.dart` — 候選選擇
  bottom sheet(列出候選、標示 synced/plain、選定後寫入並關閉)。

修改:

- `pubspec.yaml` — 加 `http`(專案目前無任何網路依賴;LRCLIB 為簡單
  REST + JSON,官方 `http` 足夠,不引入 dio)。
- `lib/features/player/widgets/lyrics_view.dart`(顯示計畫建立)—
  空狀態與視圖選單加「上網搜尋」入口,開 `lyrics_search_sheet`。
- `lib/l10n/app_en.arb` / `app_zh_TW.arb` / `app_zh_CN.arb` — 搜尋入口 /
  搜尋中 / 無結果 / 失敗 / 套用成功 key(其餘 fallback;**待辦:補 Google Sheet**)。

複用(由其他歌詞計畫建立,本計畫只消費):

- `lib/features/lyrics/lyrics_entity.dart` + `lyrics_repository.dart`
  — 選定候選後寫入(唯一索引 replace,覆蓋既有)。
- `lib/features/lyrics/lyrics_parser.dart` — 套用前以 parse 驗證內文可解析。
- `lib/features/music_list/track.dart` — 查詢條件來源(`title` / `artist` /
  `durationMs`)。

## 背景 / 目標

- 對沒有現成歌詞檔的曲目,改用線上歌詞庫以 **曲名 / 演出者 / 時長** 查詢,
  讓使用者從候選中選一筆套用,免去手動找檔。
- 屬歌詞功能群第三階段(M3),排在匯入(2)/ 顯示(3)之後、編輯(6)前後不拘;
  產出與匯入共用同一個 `LyricsEntity`,顯示與編輯無差別對待。

## 結論(設計決策)

1. **來源 LRCLIB**(lrclib.net):免費、無 API key、社群維護,同時提供
   `plainLyrics` 與 `syncedLyrics`(LRC 格式)。請求帶 `User-Agent` 標識 App
   為禮貌慣例。**實作時再驗證服務現況與條款**;先只接一個來源,留介面日後擴充。
2. **查詢策略**:主走 `GET /api/search`(寬鬆:`track_name` + `artist_name`,
   `artist` 缺失時退為純 `q=title`),回傳候選陣列。
   - `Track` 無 album 欄位,故不走需要完整四鍵的 `/api/get`;
   - 客戶端再用 `durationMs`(若有)對候選 duration 做接近度排序(容許 ±2s),
     **不硬性過濾**(時長 metadata 常不準),只用於排序與標示「時長相符」。
3. **優先 synced**:候選同時有 synced/plain 時,套用 `syncedLyrics`、
   format = lrc;只有 plain 時套 `plainLyrics`、format = txt。
   兩者都走既有 `lyrics_parser` → `Lyrics` 模型,顯示路徑與匯入完全一致。
4. **使用者選定才寫入**:搜尋只取候選、不自動覆蓋;使用者在 sheet 點選一筆,
   套用前 parse 驗證,再 `lyrics_repository` 寫入(source = online,
   title 一併存)。覆蓋既有歌詞前若已有歌詞,以確認對話框提示。
5. **不快取候選 / 不存查詢結果**:只存使用者最終選定那筆內文;
   搜尋是一次性網路動作,重來成本低。

## 步驟

1. **依賴**:加 `http`。
2. **model + service**:`lyrics_search_result.dart`、
   `lyrics_online_search_service.dart`(組請求 → 解析 → 依 duration 排序候選),
   失敗(無網路 / 逾時 / 非 2xx / 空結果)回明確錯誤型別。
3. **狀態**:`lyrics_search_controller.dart`(依目前曲目發查詢,`AsyncValue`
   呈現查詢中 / 候選 / 失敗)。
4. **UI**:`lyrics_search_sheet.dart`(候選清單:曲名 / 演出者 / 時長 /
   synced 標記;點選 → 驗證 → 寫入 → 關閉並回播放頁顯示);
   入口加進 `lyrics_view` 空狀態與選單。
5. **l10n**:搜尋入口 / 搜尋中 / 無結果 / 失敗 / 套用成功,en + zh_TW + zh_CN。
6. 驗證:`flutter analyze`、`flutter test`(service 解析 / 排序以假 JSON 測),
   實機對有 / 無 artist、有 / 無 synced、無網路與無結果各走一輪。

## 邊界 / 風險

- **metadata 不全**:`artist` / `durationMs` 可能為 null(取自 MediaStore);
  查詢需在缺欄位時降級,不得崩潰或查空。
- **無結果 / 多結果**:無結果給明確空狀態;多結果一律讓使用者選,不自動套第一筆。
- **網路與服務可用性**:逾時與離線要有清楚提示;LRCLIB 為第三方服務,
  介面設計成可替換(日後加備援來源)。
- **版權**:歌詞僅存本機(同匯入計畫:不同步 Firestore)。
- **mini player 不提供搜尋**,僅完整播放頁歌詞視圖。
