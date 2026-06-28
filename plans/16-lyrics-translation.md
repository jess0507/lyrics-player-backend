# 歌詞功能:字幕翻譯(backlog 新增)

狀態:**規劃中**(2026-06-14 起草,未實作)。
相關:`plans/lyrics-display.md`(複用 `Lyrics` / `LyricsLine` 模型與 synced /
unsynced 兩個 view,翻譯顯示掛在其上)、`plans/lyrics-import.md`
(複用 `LyricsEntity` / `lyrics_repository`)、`plans/becklog.md`。

## 背景 / 目標

- 對外語歌詞,提供「逐行翻譯」字幕,讓使用者邊聽邊看母語對照。
- 翻譯為**衍生且取得有成本**的資料(走付費 / 限額 API),故與「歌詞原文即時
  parse、不存解析結果」不同——**翻譯結果要快取存本機**,避免重複呼叫燒額度,
  並支援離線重看與秒顯示。
- 屬歌詞功能群延伸,排在顯示(backlog 3)之後;消費既有 `Lyrics` 模型,
  不改動 parser 與匯入路徑。

## 結論(設計決策)

1. **翻譯來源:Google Cloud Translation API v2**(REST,`translate.googleapis.com`)。
   - 免費額度:每月前 50 萬字元免費(需在 GCP 專案啟用 Cloud Translation API,
     並啟用帳單)。可掛在既有 Firebase 專案 `seek-player-f724e` 上。
   - 自動偵測來源語言(請求不帶 `source`,回應含 `detectedSourceLanguage`)。
   - 一次請求可帶多個 `q`(逐行),整首歌一次翻完,降低往返與字元浪費。

2. **金鑰存放:走 Cloud Functions callable,不在 client 內嵌 API key**(**推薦**)。
   - 專案已有 `cloud_functions: ^6.3.2` 與 `functions/`(Python)基礎建設。
   - 新增一個 callable function `translateLyrics(lines[], targetLang)`,
     API key 留在伺服器環境變數;函式可借既有 Firebase Auth 做**每使用者節流 /
     額度保護**,避免金鑰外洩被盜刷(client 端嵌 key 即使加 Android 限制仍可被抽出)。
   - **替代方案(較簡單但較不安全)**:client 直接打 REST + 內嵌 API key,
     於 GCP Console 對該 key 設 Android 應用限制(package name + SHA-1)
     與 API 限制(僅 Cloud Translation)。若暫不想動 Functions 可先走此路,
     但須接受金鑰可被反編譯取出的風險。**本計畫以 Cloud Functions 為準。**

3. **顯示方式:原文上、譯文下(雙語對照)**。
   - 譯文以較小 / 淡色字顯示於原文行下方;原文保留。
   - 同步歌詞(synced)的高亮與點行 seek 仍**以原文行為準**,譯文只是附屬第二行,
     不影響二分搜尋定位與捲動置中。

4. **目標語言:跟隨 App 語言(locale)**。
   - 取目前 `AppLocalizations` / `Localizations.localeOf` 對應的 Google 語言碼
     (需 Flutter Locale → Google code 對照,如 `zh_TW`→`zh-TW`、`zh_Hant`→`zh-TW`、
     `zh_CN`/`zh_Hans`→`zh-CN`,其餘多為相同小寫碼)。
   - v1 不做語言選單;留介面日後加「選擇翻譯語言」。

5. **翻一次、存本機快取(Isar)**。
   - 新增 `LyricsTranslationEntity`:以 `trackId` + `targetLang` 為複合唯一鍵
     (Isar 用 composite index,或合成字串 key `"$trackId|$targetLang"`),
     存逐行譯文(以 `\n` 串接,行數對齊原文)、`detectedSourceLang`、`addedAt`。
   - 同曲可快取多個語言;**刪除 / 重新匯入歌詞時一併清掉該曲所有語言譯文**
     (掛進 `lyrics_repository.deleteByTrackId` 與匯入覆蓋流程)。
   - 讀取時 cache-first:命中直接顯示;未命中才呼叫服務 → 寫入 → 顯示。

6. **來源語言 == 目標語言則略過翻譯**(偵測結果與目標同語言時不顯示譯文,
   並提示「無需翻譯」),避免無意義呼叫與重複文字。

7. **空行 / 純樂器行不送翻譯**(對齊用空字串占位),省字元額度。

## 修改 / 新增程式碼檔案

新增(`lib/features/lyrics/`,依 CLAUDE.md 一檔一 provider):

- `lyrics_translation.dart` — 譯文 model:`targetLang` / `sourceLang` /
  `lines`(`List<String>`,index 對齊原文 `Lyrics.lines`)。純資料類。
- `lyrics_translation_entity.dart` — Isar entity(trackId + targetLang 複合鍵、
  串接譯文、偵測來源語言、addedAt)+ 產生 `.g.dart`(build_runner)。
- `lyrics_translation_repository.dart` — Isar CRUD:`find(trackId, targetLang)`、
  `save`、`deleteByTrackId`(清該曲所有語言)。+ `lyricsTranslationRepositoryProvider`。
- `lyrics_translation_service.dart` — 翻譯 client(呼叫 Cloud Functions callable
  `translateLyrics`),逐行進、逐行出;失敗(無網路 / 逾時 / 額度 / 非 2xx)
  回明確錯誤型別。+ service provider。
- `lyrics_translation_controller.dart` — `FutureProvider.family`(by trackId)或
  `AsyncNotifier`:cache-first 取譯文,管理 off / loading / translated / error;
  另以 SharedPreferences 持久化「是否顯示譯文」偏好(key `lyrics.translateEnabled`)。
- `lyrics_translation_lang.dart` — Flutter `Locale` → Google 語言碼對照 helper
  (純函式,可單測)。

修改:

- `pubspec.yaml` — 加 `http`(若採 REST 替代方案;走 Cloud Functions 則用既有
  `cloud_functions`,可不加)。
- `lib/features/player/widgets/lyrics_view.dart` — `_LyricsMenu` 加「顯示譯文」
  切換項;watch translation controller,把譯文傳進下層 view;loading / error /
  「無需翻譯」以 SnackBar 或行內提示回報。
- `lib/features/player/widgets/lyrics_synced_view.dart` — 接受可選譯文,
  在每行原文下方加淡色小字第二行(高亮 / seek / 捲動仍以原文為準)。
- `lib/features/player/widgets/lyrics_unsynced_view.dart` — 同上加譯文第二行。
- `lib/features/lyrics/lyrics_repository.dart` — `deleteByTrackId` 連動清譯文快取;
  匯入覆蓋(`lyrics_import_service`)時亦清舊譯文。
- `lib/core/storage/isar_service.dart` — schema 註冊新增 `LyricsTranslationEntitySchema`。
- `functions/` — 新增 callable `translateLyrics`(API key 於環境變數;
  以 Auth 做基本額度保護)。
- `lib/l10n/app_en.arb` / `app_zh_TW.arb` / `app_zh_CN.arb` — 顯示譯文 / 翻譯中 /
  翻譯失敗 / 無需翻譯 key(其餘語系 fallback;**待辦:補 Google Sheet**)。

複用(只消費):

- `lib/features/lyrics/lyrics.dart`(`Lyrics` / `LyricsLine`)、`track_lyrics_provider.dart`
  — 原文來源,譯文 index 對齊其 `lines`。

## 步驟

1. **依賴 / 後端**:啟用 GCP Cloud Translation API(專案 `seek-player-f724e`)+ 帳單;
   新增 `translateLyrics` callable function(API key 入環境變數)。
2. **語言對照**:`lyrics_translation_lang.dart`(Locale → Google code)+ 單測。
3. **快取層**:`lyrics_translation_entity.dart`(+ build_runner)、
   `lyrics_translation_repository.dart`、`isar_service.dart` 註冊 schema。
4. **服務**:`lyrics_translation_service.dart`(呼叫 callable;空行占位、批次逐行、
   typed error)。
5. **狀態**:`lyrics_translation_controller.dart`(cache-first、偏好持久化、AsyncValue)。
6. **顯示**:synced / unsynced view 加譯文第二行;`lyrics_view` 選單加切換與狀態回報。
7. **連動清快取**:`lyrics_repository.deleteByTrackId` 與匯入覆蓋一併清譯文。
8. **l10n**:顯示譯文 / 翻譯中 / 失敗 / 無需翻譯,en + zh_TW + zh_CN。
9. 驗證:`flutter analyze`、`flutter test`(語言對照、service 解析以假回應測、
   空行占位與 index 對齊),實機走同步 / 非同步歌詞、命中 / 未命中快取、
   無網路、來源==目標各一輪。

## 邊界 / 風險

- **金鑰安全**:首選 Cloud Functions,勿在 client 內嵌裸 key;若走 REST 替代方案
  須設應用 / API 限制並接受外洩風險。
- **免費額度**:每月 50 萬字元免費門檻;快取 + 空行略過 + 來源==目標略過 控成本,
  函式端再做每使用者節流。
- **行對齊**:譯文必須與原文 `lines` index 一一對應(空行占位),否則同步高亮錯位。
- **來源語言偵測**:偵測不準時翻譯可能怪異;來源==目標時不顯示譯文。
- **enhanced LRC / 一行多時間戳**:parser 會把共用句展開為多行(同 text),
  翻譯以「去重後的原文行」送出再回填,避免重複字元浪費(實作時注意對齊)。
- **版權 / 隱私**:譯文同歌詞僅存本機(不同步 Firestore);歌詞文字會送往
  Google 翻譯服務,屬第三方處理,需在隱私權政策補述(`web/privacy-policy.md`)。
- **mini player 不提供翻譯**,僅完整播放頁歌詞視圖。
