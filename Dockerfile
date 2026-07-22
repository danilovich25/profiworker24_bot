FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --user -r requirements.txt

FROM python:3.12-slim
# ffmpeg режет голосовые длиннее 30 секунд на куски под лимит SpeechKit
RUN apt-get update && apt-get install -y --no-install-recommends tini ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
COPY . .
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "app.main"]
