"""本地 mock OpenAI 兼容图片接口：供无真实 Key 时验证生成/改图全链路。

用法（开发测试用）：
  1. python scripts/mock_image_server.py            # 默认 127.0.0.1:18888
  2. 设置页「图片生成」填入：
     BaseURL: http://127.0.0.1:18888/v1
     API Key: sk-mock
     模型:    mock-model
  3. 生成/改图均返回一张 1x1 PNG（b64 与 url 两种模式都支持，
     /images/edits 也实现，便于测试编辑链路）
"""
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 18888

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        if self.path.endswith("/images/generations"):
            if payload.get("response_format") == "b64_json":
                self._json(200, {"data": [{"b64_json": base64.b64encode(PNG_1x1).decode()}]})
            else:
                self._json(200, {"data": [{"url": f"http://127.0.0.1:{PORT}/mock.png"}]})
        elif self.path.endswith("/images/edits"):
            self._json(200, {"data": [{"b64_json": base64.b64encode(PNG_1x1).decode()}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_GET(self):
        if self.path == "/mock.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG_1x1)))
            self.end_headers()
            self.wfile.write(PNG_1x1)
        else:
            self._json(404, {"error": {"message": "not found"}})


if __name__ == "__main__":
    print(f"mock image server on http://127.0.0.1:{PORT} (base_url: http://127.0.0.1:{PORT}/v1)")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
