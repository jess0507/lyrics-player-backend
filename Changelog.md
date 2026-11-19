# Changelog

## develop
- whisperx:`/transcribe` 過濾 ASR 重複幻聽
  - 原因:whisperx 3.1.5 批次解碼不套用 `repetition_penalty`,前奏 / 間奏常解出「啊啊啊…」
  - 同一片語(1~4 單元)連續超過 `ASR_MAX_REPEATS` 次(預設 3,`0` 關閉):語氣詞整串移除,其餘留前 N 次
  - 清完只剩語氣詞 / 標點的段整段丟棄

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