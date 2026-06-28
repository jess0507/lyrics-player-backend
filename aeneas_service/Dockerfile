# aeneas forced-alignment 服務(Cloud Run)。
#
# 為何 Python 3.8:aeneas 1.7.3(2017)含 C 擴充,對新版 Python / numpy 易編
# 譯失敗;3.8 + numpy 1.23.x 是已知穩定組合。本容器與 functions/(python312)
# 互相獨立,版本差異不影響。
FROM python:3.8-slim-bullseye

# 系統依賴:espeak(g2p)、ffmpeg(音訊解碼)、build-essential(編 aeneas C 擴充)。
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak \
        libespeak-dev \
        ffmpeg \
        build-essential \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先裝 numpy 再裝其餘:aeneas 的 setup.py 會在安裝期 import numpy。
COPY requirements.txt .
RUN pip install --no-cache-dir "numpy==1.23.5" \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
# 對齊為 CPU 密集且單檔耗時可達數十秒~分鐘:單 worker 多 thread,逾時放寬。
CMD exec gunicorn --bind ":$PORT" --workers 1 --threads 4 --timeout 600 main:app
