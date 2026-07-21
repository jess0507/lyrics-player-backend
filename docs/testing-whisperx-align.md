# 測試 whisperx_service /align(mp3 + 歌詞 txt)

本機測試 `POST /align`(既有歌詞 + 音訊 → 同步 LRC)的完整流程。

## 快速開始(推薦:`dev_test.sh`)
每次測試就下一次指令
```bash
cd whisperx_service
./dev_test.sh --audio song.mp3 --lyrics lyrics.txt -l zh-TW --endpoint align
```

- 第一次跑會自動 build image(約十幾分鐘)、啟動容器、等 `/healthz` 就緒,才送出請求
- 之後改 `main.py` / `aligner.py` 等原始碼不需重 build,腳本每次都會重啟容器讀新碼
- 送出請求後**約需數分鐘**(CPU 跑 wav2vec2 forced-alignment),期間 `api_smoke.py`
  不會印任何東西,不是卡住——可另開終端機用 `./dev_test.sh --logs` 看即時進度
- 檔名含空格 / 括號 / 逗號記得用引號包住,例如:
  `--audio ~/Downloads/"TAEYANG(EYES,NOSE,LIPS).mp3"`

### 其他常用指令

```bash
./dev_test.sh                                          # 只做健康檢查
./dev_test.sh --rebuild --audio a.mp3 --lyrics a.txt --endpoint align   # 改了 Dockerfile/requirements 才需要
./dev_test.sh --logs                                    # tail 容器 log
./dev_test.sh --stop                                    # 收掉容器
```

### 參數(轉給 `api_smoke.py`)

| 參數 | 說明 |
| --- | --- |
| `--audio` | 本機音訊檔(`.mp3/.m4a/.wav…`),以 `inlineBase64` 內嵌,上限 50 MB |
| `--lyrics` | 歌詞 txt(每行一句);提供才測 `/align` |
| `-l`, `--language` | 語言碼(`zh-TW` / `ja` / `ko` / `en`…)。**可省略**——服務會依歌詞文字自動偵測實際語言,與此提示不符時會自動改用偵測結果(見 `aligner.py`) |
| `--endpoint` | `health` / `transcribe` / `align` / `all`(預設 `all`) |
| `--timeout` | 單一請求逾時秒數(預設 600,CPU 對齊較久) |

## 前置需求

- OrbStack(docker daemon)。`dev_test.sh` 若偵測到 daemon 沒起來會自動嘗試 `open -a OrbStack` 並等待,仍失敗就手動確認 OrbStack 已啟動
- `docker`、系統 `python3`(`api_smoke.py` 只用標準庫,不需額外安裝套件)

## 原理

- 服務程式碼跑在**容器**裡:主機沒有 ffmpeg,且 `torch==2.2.2` / `whisperx==3.1.5`
  在較新版 Python(如 3.14)上裝不起來,無法在主機原生跑 `main.py`
- `dev_test.sh` 把 `whisperx_service/` 掛到容器的 `/src`(不是 `/app`——`/app/.cache`
  預烤了 VAD / ASR / 對齊模型,掛蓋會遮住快取導致重新下載,見 Dockerfile),
  用 `python main.py` 跑 Flask dev server,對外開 `localhost:8080`
- `api_smoke.py` 在**主機**執行(非容器內),純標準庫發 HTTP 請求打 `localhost:8080`

## 另一種路線:不經容器,直接跑對齊邏輯(`align_local.py`)

只在主機 Python 版本與 `torch==2.2.2` 相容(建議 3.11/3.12,**3.14 目前裝不起來**)
時可用。繞過 HTTP,直接呼叫 `aligner.align()`,適合快速看對齊品質,不驗證 API 合約。

```bash
cd whisperx_service
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt
python align_local.py song.mp3 lyrics.txt -l zh-TW -o out.lrc -v
```

退出碼:0 成功;2 對齊失敗(降級,應保留原 unsynced 文字);1 其他錯誤。

## 疑難排解

| 現象 | 原因 / 處理 |
| --- | --- |
| `failed to connect to the docker API at unix://…orbstack/run/docker.sock` | OrbStack 沒啟動;`open -a OrbStack` 等它就緒,或改用 `dev_test.sh`(會自動處理) |
| `docker: invalid reference format` + 一堆 `zsh: command not found: -v/-e/…` | 手動貼多行 `docker run \` 續行被 shell 拆開執行;改用 `dev_test.sh` 或把指令整段貼成單行 |
| `ModuleNotFoundError: No module named 'flask'` | 誤在主機（非容器）跑了 `python main.py`；此指令只該在容器內執行,主機測試一律走 `dev_test.sh` / `api_smoke.py` |
| 送出 `/align` 後終端機長時間無輸出 | 正常——CPU 對齊約需數分鐘,回應前 `api_smoke.py` 不印任何東西;可用 `./dev_test.sh --logs` 確認進度 |
| `❌ 連線失敗:Connection refused` | 服務沒起來或還沒就緒;先跑 `./dev_test.sh`(不帶測試參數)確認健康檢查過 |
| `/align` 需要**完整連續**歌詞 | 只給跳行子集,forced alignment 會把這些行依序硬塞進音訊、後段時間被壓縮不準——這是測試素材問題,非服務 bug |

## 相關檔案

- `whisperx_service/dev_test.sh` — 一鍵啟動容器 + 測試腳本
- `whisperx_service/api_smoke.py` — HTTP 煙霧測試(health / transcribe / align)
- `whisperx_service/align_local.py` — 不經容器的本機對齊測試
- `whisperx_service/LOCAL_TESTING.md` — 原始手動流程說明（`dev_test.sh` 即為其自動化版本）
