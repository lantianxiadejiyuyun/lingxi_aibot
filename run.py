"""开发入口：python run.py

默认监听 0.0.0.0:5000（局域网/同网段机器可访问）。
可用环境变量覆盖：HOST / PORT。
⚠️ 仅用于开发调试；公网生产部署请用 wsgi.py + waitress（见 docs/部署.md）。
"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    app.run(host=host, port=port, debug=True, use_reloader=False)
