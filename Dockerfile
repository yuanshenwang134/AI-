# AI 篮球分析软件 · 套餐 A
# 只装最小依赖（能跑合成 demo + Web 服务 + 自检）。
# 需要真视频推理时，再在容器里额外 pip install ultralytics opencv-python。
FROM python:3.11-slim

# ffmpeg 用于高光剪辑；装不上也不影响其他功能（会降级为时间码）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY README.md ./

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# 默认只跑 API；要跑 CLI 就 docker compose run api python -m aihoop.cli demo --out out/demo
CMD ["uvicorn", "aihoop.api:app", "--host", "0.0.0.0", "--port", "8000"]
