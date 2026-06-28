# 本機測試 whisperx_service API

用容器跑起服務、再用 `api_smoke.py` 打 `localhost` 的端點(`/healthz`、
`/transcribe`、`/align`)做煙霧測試。純邏輯單元測試見 README「本機驗證」。

## 為何要用容器

主機**無法**原生跑 `main.py`:它在 import 階段就需要 `whisperx / torch`,而本機是
Python 3.14/3.12、無 ffmpeg,且 `torch==2.2.2 / whisperx==3.1.5` 裝不起來 → import
直接崩。容器裡有完整 deps + ffmpeg + 預烤模型,所以服務跑在容器、但仍是 `localhost`。

`api_smoke.py` 只用 Python 標準庫(urllib / json / base64),**不需安裝任何套件**,
直接用系統 `python3` 在主機執行即可(它只是個 HTTP client)。

## 啟動服務

```bash
cd whisperx_service

# 1. 起 docker daemon(本機用 OrbStack)
open -a OrbStack

# 2. build image(約十幾分鐘、約 2.77GB;build 階段會預烤 VAD + small ASR 模型)
docker build -t whisperx-service .

# 3. 跑「主機這份 main.py」:把原始碼掛到 /src,用 Flask dev server 跑
docker run -d --name whisperx-local -p 8080:8080 \
  -v "$PWD":/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 \
  whisperx-service \
  python main.py
```

> ⚠️ **原始碼掛到 `/src`,不可掛蓋 `/app`。** image 在 `/app/.cache` 預烤了 VAD +
> ASR 模型;掛蓋 `/app` 會遮住快取 → runtime 重新下載、踩 Dockerfile 已修掉的
> checksum 422。deps 在 site-packages、模型走 `TORCH_HOME`/`HF_HOME` 絕對路徑,
> 從 `/src` 跑無妨。

改完 `main.py` 不必重 build,`docker restart whisperx-local` 即生效。

## 用 api_smoke.py 測

```bash
# 健康檢查(不需音訊)
python3 api_smoke.py --endpoint health

# 自動產生歌詞(ASR):音訊 → LRC;省略 -l 則自動偵測語言
python3 api_smoke.py --audio song.mp3 --endpoint transcribe

# 對齊既有歌詞:音訊 + 歌詞 txt → 同步 LRC
python3 api_smoke.py --audio song.mp3 --lyrics lyrics.txt -l zh-TW --endpoint align

# 一次跑全部(healthz + transcribe;有 --lyrics 時連 align)
python3 api_smoke.py --audio song.mp3 --lyrics lyrics.txt -l zh-TW
```

| 參數 | 說明 |
| --- | --- |
| `--url` | 服務 base URL(預設 `http://localhost:8080`) |
| `--audio` | 本機音訊檔(`.mp3/.m4a/.wav…`);以 `inlineBase64` 內嵌,上限 50 MB(建議短片段) |
| `--lyrics` | 歌詞 txt(每行一句);提供才測 `/align` |
| `-l`, `--language` | 語言碼(`zh-TW` / `ja` / `en`…);`transcribe` 可省略 |
| `--endpoint` | `health` / `transcribe` / `align` / `all`(預設 `all`) |
| `--timeout` | 單一請求逾時秒數(CPU 對齊較久,預設 600) |

退出碼:0 全部通過;1 有任一失敗 / 連線失敗。LRC 會單獨換行印,其餘 body 印縮排 JSON。

## 實測結果(參考)

約 2 分鐘的英文新聞語音,CPU(`small` / `int8`):

| 端點 | 結果 | 耗時 |
| --- | --- | --- |
| `GET /healthz` | 200 `{"status":"ok"}` | — |
| `POST /transcribe`(自動偵測) | 200,偵測 `en`,15 句 LRC | ~64s |
| `POST /align`(文字 + 音訊) | 200,同步 LRC | ~30s |
| 缺 `lines` / 缺 `audio` / 非 JSON | 400 `invalid_request` | — |

> `/align` 需給**完整連續**歌詞。若只給跳行子集,forced alignment 會把這些行依序
> 硬塞進音訊、後段時間被壓縮而不準 —— 這是測試素材問題,非服務 bug。

## 常用維運指令

```bash
docker logs -f whisperx-local      # 看服務 log
docker restart whisperx-local      # 改 main.py 後重啟
docker rm -f whisperx-local        # 收掉服務
```
