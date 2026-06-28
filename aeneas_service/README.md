# aeneas 歌詞自動對時服務(Cloud Run)

對應計畫:`../plans/lyrics-auto-sync-aeneas.md`(backlog 5)。

把**既有的純文字歌詞 + 音訊**做 forced alignment,產出每行起始時間的同步
LRC。App 端拿回 LRC 後寫回同一個 `LyricsEntity`(`source = generated`、
`format = lrc`),顯示端(`lyrics-display`)無需任何改動即可同步捲動。

> 這是獨立於 `../functions/`(Firebase Functions / python312)的 **Cloud Run
> 容器**:aeneas 需要 espeak / ffmpeg 等系統依賴,標準 Functions runtime 裝不
> 下,故自架容器。

## 檔案

| 檔案 | 職責 |
| --- | --- |
| `main.py` | Flask app:`/healthz`、`/align` 端點;音訊取得(GCS / inline)。 |
| `aligner.py` | aeneas 呼叫核心:純文字 + 音訊 → 逐行 begin/end 秒。 |
| `lrc.py` | 對齊秒數 → 標準 LRC(`[mm:ss.xx]`),純函式。 |
| `test_lrc.py` | `lrc.py` 單元測試(免 aeneas / ffmpeg / GCS)。 |
| `Dockerfile` | Python 3.8 + espeak + ffmpeg + aeneas 的建置。 |
| `requirements.txt` | Python 依賴(版本對 aeneas 敏感,勿隨意升級)。 |

## API 合約

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
    "gcs": { "bucket": "seek-player-align", "object": "uid123/abc.opus" },
    "format": "opus"
  }
}
```

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `lines` | ✓ | 純文字歌詞,每元素一行(**已去空行 / 修飾**)。後端會再濾掉空行,對齊結果與此清單一一對應。 |
| `language` | | 客戶端語言碼(BCP-47 或二字母),後端正規化為 aeneas 的 ISO 639-3。預設 `eng`。常見:`zh*`→`cmn`、`ja`→`jpn`、`en`→`eng`。 |
| `audio` | ✓ | 音訊來源,二擇一:`gcs` 或 `inlineBase64`。 |
| `audio.gcs` | | `{ bucket, object }`:從 Cloud Storage 下載(正式路線)。後端**不刪除**該物件,暫存音訊清理交給 bucket lifecycle(見部署)。 |
| `audio.inlineBase64` | | base64 內嵌音訊,**僅供本機 / 小檔測試**(上限 50 MB)。 |
| `audio.format` | | 副檔名提示(`opus` / `m4a` / `mp3` …);僅影響暫存檔名,實際解碼交給 ffmpeg。 |

**音訊建議**:forced alignment 只需語音,App 端應**先壓成單聲道低取樣**
(如 16 kHz mono opus)再經 GCS 中轉,大幅降低傳輸量與成本(見計畫決策)。

**Response 200**:

```json
{
  "lrc": "[00:00.00]第一行歌詞\n[00:12.34]第二行歌詞",
  "fragments": [
    { "index": 0, "begin": 0.0,  "end": 12.34, "text": "第一行歌詞" },
    { "index": 1, "begin": 12.34, "end": 25.0,  "text": "第二行歌詞" }
  ],
  "language": "cmn"
}
```

App 端通常只需 `lrc`,直接寫入 `LyricsEntity.content`(`format = lrc`、
`source = generated`)。`fragments` 供除錯 / 信心評估用。

**錯誤**(HTTP 狀態 + `{"error": {"code", "message"}}`):

| code | 狀態 | 意義 | App 端對應 |
| --- | --- | --- | --- |
| `invalid_request` | 400 | 缺欄位 / lines 全空 / audio 來源缺失 | 內部錯誤(不該發生,記 log) |
| `audio_fetch_failed` | 502 | GCS 下載失敗 | 提示稍後重試 |
| `alignment_failed` | 422 | aeneas 無法產出 / 片段數不符 | **降級**:保留原 unsynced 純文字,提示對齊失敗 |
| `internal` | 500 | 非預期錯誤 | 提示稍後重試 |

> 失敗一律不回半套時間;對齊失敗時 App 應保留原本的純文字歌詞(計畫的「失敗降級」)。

## 本機驗證

純邏輯(LRC 格式化),不需任何重依賴:

```bash
cd aeneas_service
python -m unittest test_lrc
```

完整對齊需 aeneas + ffmpeg + espeak,建議直接在容器內測:

```bash
cd aeneas_service
docker build -t aeneas-service .
docker run --rm -p 8080:8080 aeneas-service
# 另開終端,用 inlineBase64 丟一段語音 + 對應文字(見上方合約)。
curl -s localhost:8080/healthz
```

## 部署(Cloud Run)

> 後端部署 / 驗證由專案維護者執行(需 GCP 權限,無法於開發環境代跑)。

**CI 自動部署**:`.github/workflows/cloud-run-aeneas-deploy.yml` 會在 `master`
上 `aeneas_service/**` 變更時,以 Cloud Build 從原始碼建置並 `gcloud run deploy`
(也可手動 `workflow_dispatch`)。前置:在 repo 設好 `GCP_RUN_DEPLOY_SA` secret
(具 Cloud Run / Cloud Build / Artifact Registry / Storage 部署權限的 service
account JSON),GCS bucket 與 IAM 仍依下方手動建一次。

手動部署:

```bash
# 1. 部署容器(於 aeneas_service/ 內;以 --source 讓 Cloud Build 用本 Dockerfile)
gcloud run deploy aeneas-align \
  --source . \
  --region asia-east1 \
  --memory 2Gi --cpu 2 --timeout 600 \
  --no-allow-unauthenticated

# 2. 暫存音訊清理:App 上傳到 Firebase 預設 bucket 的 align/{uid}/**。
#    後端不即時刪除,完全交給 lifecycle:align/ 前綴 1 天後自動刪
#    (涵蓋成功與失敗路徑)。僅限 align/ 前綴,不影響 bucket 內其他檔案。
gcloud storage buckets update gs://<DEFAULT_FIREBASE_BUCKET> \
  --lifecycle-file=storage-lifecycle.json

# 3. IAM:Cloud Run 服務帳號只需「讀取」該 bucket(不刪除,故 objectViewer 即可)
gsutil iam ch \
  serviceAccount:<RUN_SERVICE_ACCOUNT>:roles/storage.objectViewer \
  gs://<DEFAULT_FIREBASE_BUCKET>
```

> `<DEFAULT_FIREBASE_BUCKET>` 即 Firebase 預設 Storage bucket(本實作不另建
> `seek-player-align`,直接用預設 bucket + `align/` 前綴,見計畫部署前置)。
> lifecycle 規則檔為 `storage-lifecycle.json`(版控,勿手抄 `/tmp`)。

**呼叫端鑑權(下一輪 Flutter 端決定)**:服務以 `--no-allow-unauthenticated`
部署,建議由 Firebase Function 代呼叫並注入身分,或讓 App 帶 ID token;同時
App 上傳到 GCS 的授權(signed URL 或 Firebase Storage 規則)一併在 Flutter
端計畫中處理。本服務只負責「給音訊 + 文字 → 回 LRC」。

## 已知風險(摘自計畫)

- **中文對齊品質**:aeneas 靠 espeak g2p,中文 / 歌聲(拉長音、配樂)需實測;
  不足則改 WhisperX 路線(`../plans/lyrics-auto-sync-whisperx.md`)。
- **冷啟動 / 成本**:容器較大、對齊 CPU 密集;低頻使用控制在低額度內。
- **aeneas 安裝脆弱**:Python / numpy 版本敏感,Dockerfile 已釘版本,勿隨意升級。

## GitHub Actions 部署流程
GitHub Actions
↓
Service Account (SA)
↓
Artifact Registry (存 Docker Image)
↓
Cloud Build (建置 Image)
↓
Cloud Run (部署 Spring Boot)
↓
GCS Bucket (暫存檔案)