"""生产入口：waitress 启动（宝塔 + Supervisor / Python 项目管理器）。

启动命令（在项目根目录）：
    venv/bin/waitress-serve --host=127.0.0.1 --port=8000 --threads=8 --channel-timeout=3600 wsgi:app

参数说明：
    --host=127.0.0.1   只监听本机，公网访问一律走 Nginx 反代（勿直接暴露端口）
    --threads=8        单进程多线程；调度器只启动一次，不会重复执行定时任务
    --channel-timeout=3600   AI 对话 SSE 长连接的空闲超时（默认 120s 会掐断思考中的回答）
"""
from app import create_app

app = create_app()
