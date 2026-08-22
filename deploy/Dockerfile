# ============================================================
# 灵犀 AiBot — Docker 镜像
# 运行方式：waitress 单进程多线程（避免 APScheduler 定时任务重复执行）
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    FLASK_APP=wsgi:app

WORKDIR /app

# 先装依赖（利用 Docker 层缓存，代码改动不重复装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app
COPY wsgi.py run.py ./

# 运行时数据目录（备份 / 生成图片）
RUN mkdir -p /app/data/backups /app/data/images

# 非 root 运行（更安全）
RUN useradd -m -u 1000 aibot && chown -R aibot:aibot /app
USER aibot

EXPOSE 8000

# 生产启动：waitress 单进程 8 线程；channel-timeout 3600 保证 AI 对话 SSE 长连接不被掐断
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "--threads=8", "--channel-timeout=3600", "wsgi:app"]
