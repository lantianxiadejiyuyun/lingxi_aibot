FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai FLASK_APP=wsgi:app FLASK_SKIP_DOTENV=1 PORT=8000 \
    AIBOT_ENV_FILE=/app/data/.env PAGE_BIND=0.0.0.0

COPY requirements.txt .
RUN pip install -r requirements.txt gunicorn

COPY app ./app
COPY run.py wsgi.py ./
RUN mkdir -p data/backups data/images \
    && useradd -m -u 1000 aibot \
    && chown -R aibot:aibot /app
USER aibot

EXPOSE 8000
# 单 worker + 多线程：APScheduler 随 worker 进程启动一次，避免多 worker 重复调度
# timeout 300：多轮工具调用的长对话（LLM_TIMEOUT=90 × 多轮）不会在 120s 被截断
# 镜像以 uid 1000 运行；bind mount ./data 时请 chown -R 1000:1000 data
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "300", "wsgi:app"]
