# WhisperX 歌詞自動對時服務(Cloud Run)

對應計畫:`../plans/13-lyrics-auto-sync-whisperx.md`(backlog 5,**aeneas 路線的
升級選項**)。

把**既有的純文字歌詞 + 音訊**做 forced alignment,產出每行起始時間的同步 LRC。
與 `../aeneas_service/` 是**同一個 HTTP 合約**(可直接互換),差別在內部改用
**WhisperX 的 wav2vec2 對齊**取得**字級**時間 —— 粒度比 aeneas 的行級更細、中文 /
歌聲對齊一般較佳。

> App 端拿回 LRC 後寫回同一個 `LyricsEntity`(`source = generated`、
> `format = lrc`),顯示端(`lyrics-display`)無需任何改動即可同步捲動。

## 與 aeneas 路線的關係(如何切換)

**合約相同、可無痛替換。** Flutter 端(`lib/features/lyrics/auto_sync/`)與
Firebase Function(`functions/main.py` 的 `align_lyrics`)**完全不需改動**;切換只是
把 Function 的環境變數 `ALIGN_SERVICE_URL` 從 aeneas-align 的 Cloud Run URL 改指向
本服務(whisperx-align)的 URL:

```bash
cd functions
# 改寫 .env 的 ALIGN_SERVICE_URL=https://whisperx-align-xxxxx-de.a.run.app
firebase deploy --only functions:align_lyrics
```

兩個容器可並存於同專案(各自獨立部署),由 `ALIGN_SERVICE_URL` 決定當前採用哪條
路線。建議先以 aeneas 起步,中文 / 細緻度實測不足時再切到本路線(見計畫決策)。

## 檔案

| 檔案 | 職責 |
| --- | --- |
| `main.py` | Flask app:`/healthz`、`/align` 端點;音訊取得(GCS / inline)。 |
| `aligner.py` | WhisperX 對齊核心:純文字 + 音訊 → 字級時間 → 逐行 begin/end 秒。 |
| `lrc.py` | 對齊秒數 → 標準 LRC(`[mm:ss.xx]`),純函式。 |
| `test_lrc.py` / `test_align.py` | 純邏輯單元測試(免 whisperx / torch / ffmpeg / GCS)。 |
| `Dockerfile` | Python 3.11 + ffmpeg + torch(CPU)+ whisperx 的建置。 |
| `requirements.txt` | Python 依賴(版本對 torch 敏感,勿隨意升級)。 |

## API 合約

與 `../aeneas_service/README.md` **完全一致**,以下為摘要。

### `GET /healthz`

```json
{ "status": "ok" }
```

### `POST /align`

**Request**(`Content-Type: application/json`):

```json
{
  "lines": ["第一行歌詞", "第二行歌詞", "..."],
  "language": "zh-TW",
  "audio": {
    "gcs": { "bucket": "<DEFAULT_FIREBASE_BUCKET>", "object": "align/uid123/abc.m4a" },
    "format": "m4a"
  }
}
```

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `lines` | ✓ | 純文字歌詞,每元素一行(**已去空行 / 修飾**)。後端會再濾掉空行,對齊結果與此清單一一對應。 |
| `language` | | 客戶端語言碼(BCP-47 / 二字母 / ISO 639-3),後端正規化為 whisperx 的二字母碼。預設 `en`。常見:`zh*`→`zh`、`ja`→`ja`、`eng`→`en`。 |
| `audio` | ✓ | 音訊來源,二擇一:`gcs` 或 `inlineBase64`。 |
| `audio.gcs` | | `{ bucket, object }`:從 Cloud Storage 下載(正式路線)。後端**不刪除**該物件,暫存音訊清理交給 bucket lifecycle(見部署)。 |
| `audio.inlineBase64` | | base64 內嵌音訊,**僅供本機 / 小檔測試**(上限 50 MB)。 |
| `audio.format` | | 副檔名提示(`m4a` / `mp3` / `opus` …);僅影響暫存檔名,實際解碼交給 ffmpeg。 |

**Response 200**:

```json
{
  "lrc": "[00:00.00]第一行歌詞\n[00:12.34]第二行歌詞",
  "fragments": [
    { "index": 0, "begin": 0.0,  "end": 12.34, "text": "第一行歌詞" },
    { "index": 1, "begin": 12.34, "end": 25.0,  "text": "第二行歌詞" }
  ],
  "language": "zh"
}
```

App 端通常只需 `lrc`,直接寫入 `LyricsEntity.content`(`format = lrc`、
`source = generated`)。`fragments` 帶字級對齊聚合後的逐行 begin/end,供除錯 / 信心
評估;字級時間本身保留供未來逐字 highlight。

**錯誤**(HTTP 狀態 + `{"error": {"code", "message"}}`,與 aeneas 一致):

| code | 狀態 | 意義 | App 端對應 |
| --- | --- | --- | --- |
| `invalid_request` | 400 | 缺欄位 / lines 全空 / audio 來源缺失 | 內部錯誤(不該發生,記 log) |
| `audio_fetch_failed` | 502 | GCS 下載失敗 | 提示稍後重試 |
| `alignment_failed` | 422 | wav2vec2 無法產出 / 覆蓋率過低 | **降級**:保留原 unsynced 純文字,提示對齊失敗 |
| `internal` | 500 | 非預期錯誤 | 提示稍後重試 |

> 失敗一律不回半套時間。對齊覆蓋率(取得起始時間的行數佔比)低於
> `WHISPERX_MIN_COVERAGE`(預設 0.6)時回 `alignment_failed`,App 保留原文字。

### `POST /transcribe`

**自動產生歌詞**(`audio → 文字 + 時間`,ASR 轉寫 + 對齊):對應計畫
`../plans/15-lyrics-auto-generate.md`(backlog 6)。與 `/align` 的差別是**沒有既有
文字**,後端用 faster-whisper 直接辨識歌詞再對齊。

**Request**(`Content-Type: application/json`):

```json
{
  "language": "zh-TW",
  "audio": {
    "gcs": { "bucket": "<DEFAULT_FIREBASE_BUCKET>", "object": "generate/uid123/abc.m4a" },
    "format": "m4a"
  }
}
```

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `language` | | 語言**提示**;省略 → whisper **自動偵測**(歌聲場景預設不鎖定)。給值則正規化為二字母碼鎖定。 |
| `audio` | ✓ | 音訊來源,同 `/align`(`gcs` 或 `inlineBase64`)。物件慣例前綴 `generate/`。 |

**Response 200**:與 `/align` 同形(`{ "lrc", "fragments", "language" }`);
`language` 為偵測 / 鎖定的結果。

**錯誤**:`invalid_request`(400)、`audio_fetch_failed`(502)、
`transcription_failed`(422,辨識不出可用歌詞 → App 提示失敗、不寫入)、
`internal`(500)。

模型大小由 `WHISPER_MODEL_SIZE`(預設 `small`)、量化 `WHISPER_COMPUTE_TYPE`
(預設 `int8`)控制;CPU 上 large 對長曲易逼近逾時,實測後再調。

## 本機驗證

純邏輯(LRC 格式化、字級→逐行映射),不需任何重依賴:

```bash
cd whisperx_service
python -m unittest test_lrc test_align test_transcribe
```

帶重依賴的本機品質測試(免容器 / GCS,直接跑對齊或自動產生):

```bash
python align_local.py song.mp3 lyrics.txt -l zh-TW   # 對齊:文字 + 音訊 → LRC
python transcribe_local.py song.mp3                  # 自動產生:音訊 → LRC(自動偵測語言)
```

完整服務需 whisperx + torch + ffmpeg,建議直接在容器內測:

```bash
cd whisperx_service
docker build -t whisperx-service .
docker run --rm -p 8080:8080 whisperx-service
# 另開終端,用 inlineBase64 丟一段語音 + 對應文字(見上方合約)。
curl -s localhost:8080/healthz
```

完整的「容器跑起服務 + `api_smoke.py` 打 `localhost` 端點」流程(含掛主機原始碼
即時改 `main.py`、實測耗時參考),見 [`LOCAL_TESTING.md`](LOCAL_TESTING.md)。

## 部署(Cloud Run)

> 後端部署 / 驗證由專案維護者執行(需 GCP 權限,無法於開發環境代跑)。

**CI 自動部署**:`.github/workflows/cloud-run-whisperx-deploy.yml` 會在 `master`
上 `whisperx_service/**` 變更時,以 Cloud Build 從原始碼建置並 `gcloud run deploy`
(也可手動 `workflow_dispatch`)。沿用 aeneas 路線的 `GCP_RUN_DEPLOY_SA` secret 與
同一個 GCS bucket / lifecycle / IAM(僅 Cloud Run 服務名不同)。

手動部署:

```bash
# 1. 部署容器(於 whisperx_service/ 內;--source 讓 Cloud Build 用本 Dockerfile)。
#    模型較重,記憶體 / CPU 比 aeneas 高;CPU 推論,無需 GPU。
gcloud run deploy whisperx-align \
  --source . \
  --region asia-east1 \
  --memory 8Gi --cpu 4 --concurrency 1 --timeout 600 \
  --no-allow-unauthenticated

# 2. 暫存音訊清理:App 上傳到 Firebase 預設 bucket 的 align/{uid}/**。
#    後端不即時刪,完全交給 lifecycle:align/ 前綴 1 天後自動刪(與 aeneas 共用)。
gcloud storage buckets update gs://<DEFAULT_FIREBASE_BUCKET> \
  --lifecycle-file=storage-lifecycle.json

# 3. IAM:Cloud Run 服務帳號只需「讀取」該 bucket(不刪除,故 objectViewer 即可)。
gsutil iam ch \
  serviceAccount:<RUN_SERVICE_ACCOUNT>:roles/storage.objectViewer \
  gs://<DEFAULT_FIREBASE_BUCKET>

# 4. Function SA → 本服務的 run.invoker(Function 取 ID token 後才能呼叫受保護服務)。
gcloud run services add-iam-policy-binding whisperx-align \
  --region asia-east1 \
  --member "serviceAccount:<FUNCTION_RUNTIME_SA>" \
  --role roles/run.invoker

# 5. 切換路線:把 align_lyrics 的 ALIGN_SERVICE_URL 指向本服務(見上方「如何切換」)。
```

若 aeneas 已部署過第 2、3 步(同 bucket / lifecycle),無需重做;只需第 1、4、5 步。

## 已知風險(摘自計畫)

- **映像 / 記憶體 / 冷啟動**:torch + wav2vec2 模型較重,映像體積、記憶體與冷啟動
  成本高於 aeneas;模型權重在首呼由 HuggingFace 下載(冷啟動更慢)。低頻使用需估算
  是否仍在可接受額度。
- **CPU 推論耗時**:長曲在 CPU 上對齊可能逼近 600s 逾時;必要時評估縮短音訊 /
  升記憶體 / 改 GPU。
- **歌聲難度**:拉長音 / 配樂干擾仍是挑戰,但字級對齊容錯通常較佳。
- **依賴敏感**:whisperx 對 torch 版本敏感,Dockerfile / requirements 已釘版本,
  勿隨意升級。

## GitHub Actions 部署流程

GitHub Actions
↓
Service Account (SA)
↓
Artifact Registry (存 Docker Image)
↓
Cloud Build (建置 Image)
↓
Cloud Run (部署 WhisperX 服務)
↓
GCS Bucket (暫存檔案)
