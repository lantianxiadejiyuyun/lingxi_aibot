"""开发入口：python run.py

默认监听 127.0.0.1:5000（仅本机可访问）；可用环境变量 HOST / PORT 覆盖。
调试器（Werkzeug debugger）默认关闭：FLASK_DEBUG=1 时开启，但监听非回环地址
会强制关闭调试器（调试器可在服务端执行任意代码，暴露公网/局域网等于 RCE）。
⚠️ 仅用于开发调试；公网生产部署请用 wsgi.py + waitress（见 README-部署.md）。
"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    if debug and host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠️  FLASK_DEBUG=1 且监听非回环地址，已强制关闭调试器（防 Werkzeug 调试器 RCE）。")
        debug = False
    app.run(host=host, port=port, debug=debug, use_reloader=False)
