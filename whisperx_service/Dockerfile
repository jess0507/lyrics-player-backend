# WhisperX forced-alignment 服務(Cloud Run)。
#
# 與 aeneas_service/(Python 3.8)不同,whisperx 需要較新的 Python / torch;此處
# 用 Python 3.11。本容器與 functions/(python312)、aeneas_service/ 互相獨立。
FROM python:3.11-slim-bookworm

# 系統依賴:
# - ffmpeg:whisperx.load_audio 以 ffmpeg 解碼(執行期)。
# - git:部分 pip 套件需。
# - build-essential:PyAV 從源碼編譯需要 gcc,slim 映像不含,缺則報
#   「command 'gcc' failed: No such file or directory」。
# - pkg-config + libav*-dev:whisperx 連帶的 PyAV(av)在無對應預編譯 wheel 時需
#   從原始碼對系統 ffmpeg 編譯,缺這些會在 build wheel 階段報「pkg-config is
#   required for building PyAV」。
# 模型權重在執行期由 HuggingFace 下載,故 HF 快取目錄需可寫。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        build-essential \
        pkg-config \
        libavformat-dev \
        libavcodec-dev \
        libavdevice-dev \
        libavutil-dev \
        libavfilter-dev \
        libswscale-dev \
        libswresample-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch / torchaudio 走官方 CPU wheel(Cloud Run 預設無 GPU);其餘依 requirements。
COPY requirements.txt .
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt

# 預烤 WhisperX 的 VAD 權重。whisperx 3.1.5 寫死的 S3 URL
# (whisperx.s3.eu-west-2…)已失效:bucket 實在 us-west-2 且已關閉公開存取,
# 直連回 301/403、跟轉址也只拿到數百 byte 的 XML 錯誤頁 → runtime 載模型時
# SHA256 checksum 不符而整批轉寫 422。改從 whisperX repo 打包的 assets 取「同一份」
# 權重(sha256 與 whisperx 期望值 0b5b3216… 一致),驗證後放到 whisperx 預期路徑
# ($TORCH_HOME),runtime 直接命中、不下載、不再 checksum 失敗。
# 放在 COPY 應用碼之前,讓本層隨 requirements 一起被 layer 快取。
# HF_HOME / TORCH_HOME 在預烤前就設好,讓下載的權重落在 image 內、runtime 直接命中。
ENV TORCH_HOME=/app/.cache/torch
ENV HF_HOME=/app/.cache/huggingface
RUN python -c "import torch, os, hashlib, requests; \
url='https://raw.githubusercontent.com/m-bain/whisperX/main/whisperx/assets/pytorch_model.bin'; \
expect='0b5b3216d60a2d32fc086b47ea8c67589aaeb26b7e07fcbe620d6d0b83e209ea'; \
d=torch.hub._get_torch_home(); os.makedirs(d, exist_ok=True); \
fp=os.path.join(d, 'whisperx-vad-segmentation.bin'); \
r=requests.get(url, allow_redirects=True, timeout=120); r.raise_for_status(); \
open(fp, 'wb').write(r.content); \
h=hashlib.sha256(r.content).hexdigest(); \
assert h == expect, 'VAD sha256 mismatch: ' + h; \
print('VAD cached:', fp, len(r.content), 'bytes, sha256 OK')"

# 預烤 faster-whisper ASR 模型(small):否則 runtime 首呼才下載,且 gunicorn 多
# 執行緒 / 高並發時會同時下載到同一 HF 快取 → 互相寫壞 → 「SHA256 checksum does
# not match」422。build 時抓好放進 image,runtime 不下載、無 race(亦大幅縮短冷啟動)。
# 模型大小 / 量化須與 transcriber.py 的 WHISPER_MODEL_SIZE / WHISPER_COMPUTE_TYPE 一致。
RUN python -c "import whisperx; whisperx.load_model('small', device='cpu', compute_type='int8'); print('ASR model cached')"

COPY . .

ENV PORT=8080
# 對齊為 CPU 密集且單檔耗時可達數十秒~分鐘:單 worker 多 thread,逾時放寬到 600s
# (與 functions/ 的 align_lyrics callable timeout_sec=600 一致)。
CMD exec gunicorn --bind ":$PORT" --workers 1 --threads 4 --timeout 600 main:app
