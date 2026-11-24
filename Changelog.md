# Changelog

## develop

## 1.0.4(2026112402)
- whisperx:`/transcribe` 修正中文段落裡的英文詞黏在一起
  - 原因:辨識為中 / 日文時逐字串接、不加空格,原文英文詞之間的空白被丟掉(1.0.1 改逐段字級對齊後出現)
  - 保留原文字間的空白,`Take it or leave it` 不再變成 `Takeitorleaveit`

## 1.0.3(2026112401)
- whisperx、aeneas、functions:歌詞結果記錄產出它的流程版本,該流程改版後同一首歌 / 同一份歌詞不再沿用舊結果
  - 流程分三條各自計版:`whisperx.transcribe`、`whisperx.align`、`aeneas.align`
  - 版本為該流程程式最後一次修改所屬的版本 tag,部署時從 git 歷史算出,以 `TRANSCRIBE_PIPELINE_VERSION` / `ALIGN_PIPELINE_VERSION` 帶入
  - 結果快照新增 `backendVersion`(如 `whisperx.transcribe@1.0.2`)
  - `align_lyrics` / `generate_lyrics` 沿用前比對該流程的現行版本(`system/backendVersions`),流程不同、版本不同或沒有版本就重新辨識 / 對時
  - 讀不到現行版本時維持原本的沿用行為

## 1.0.2(20261124)
- whisperx、aeneas:`/align` 與 `/transcribe` 不再因語系切換斷行
  - 原因:對齊一次只處理一種語言,另一語系切出去的行沒有可靠時間
  - `Apple蘋果`、`我愛你baby` 維持一行;輸入原有的換行、標點與停頓斷行規則不變

## 1.0.1(2026111901)
- whisperx:`/transcribe` 過濾 ASR 重複幻聽
  - 原因:whisperx 3.1.5 批次解碼不套用 `repetition_penalty`,前奏 / 間奏常解出「啊啊啊…」
  - 同一片語(1~4 單元)連續超過 `ASR_MAX_REPEATS` 次(預設 3,`0` 關閉):語氣詞整串移除,其餘留前 N 次
  - 清完只剩語氣詞 / 標點的段整段丟棄
- whisperx:`/transcribe` 改用與 `/align` 相同的字級對齊,修正中文歌整段約 30 秒擠成一行
  - 原因:`whisperx.align` 字間停頓恆為 0,ASR 又無標點,切行找不到斷點
  - 改為逐段取音訊做 forced alignment,停頓斷點(`LYRICS_LINE_GAP_SECONDS` / `LYRICS_LINE_LONG_GAP_SECONDS`)恢復作用
  - 單段對齊失敗只該段退回 ASR 段級時間

## 1.0.0(20261119)
- 首個標記版本
- whisperx:`/align` 與 `/transcribe` 共用同一套切行規則(保留原分行;句末標點、長停頓 ≥ `LYRICS_LINE_LONG_GAP_SECONDS` 必斷;過長句才在子句標點 / 停頓處斷;拉丁字母與其他文字單次切換處斷)
- whisperx:`/align` 行起點時間校正 — 行首字典外字(英文 / 數字 / 生僻字)依發音單元往回補時(`ALIGN_OOV_UNIT_SECONDS`),每行起點再提前 `ALIGN_LEAD_SECONDS`
- whisperx:`/transcribe` **對齊前**改餵 NFKC + 繁轉簡副本(OpenCC `t2s` 逐字轉、字數不變,僅對齊語言為 `zh` 時;副本只給對齊模型、不顯示,與 `/align` 相同),修正繁簡混雜導致對齊失準
- whisperx:`/transcribe` 新增 `script` 參數(`traditional` / `simplified`);**輸出時**(對齊完、組 LRC 前)以 OpenCC 整行轉換顯示字形(`s2tw` / `t2s`);省略時由 `language` 推定,推不出則不轉
- whisperx:`transcribe_local.py` 新增 `-s` / `--script` 選項
- aeneas:`/align` 整行註記(段落標籤、作詞作曲、`[ar:…]` 等)不送 aeneas,輸出時插回原位、時間沿用前一行;行內時間戳、`(x2)`、行首段落標籤只從 TTS 文字移除,顯示保留原文
- aeneas:`/align` 依歌詞文字系統偵測語言(諺文 → `kor`、假名 → `jpn`、漢字 → `cmn`、拉丁 → 提示或 `eng`),`language` 僅在不衝突時採用;回應的 `language` 為實際使用的語言
- aeneas:`/align` 對齊前自動切行 — 句末標點後斷、過長句(`LYRICS_LINE_MAX_CHARS_CJK` / `LYRICS_LINE_MAX_WORDS`)才在子句標點斷、拉丁與其他文字單次切換處斷;可用 `ALIGN_SPLIT_LINES=0` 關閉
- aeneas:`/align` 回應的 `fragments` 與 LRC 行一一對應,新增 `aligned` 欄位標示該行是否實際對齊
- Deploy only from version tags and diff against previous tag