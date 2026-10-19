"""Initialize NapCat's shared HTTP + reverse-WebSocket config without printing secrets.

Run with the project's Python environment. Existing account-specific configs are
left intact; stop NapCat and explicitly update them when changing credentials.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key


def initialize(env_file: Path, data_dir: Path, websocket_url: str):
    parsed = urlsplit(websocket_url)
    if (parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or any(ord(c) <= 32 for c in websocket_url)):
        raise ValueError("反向 WebSocket 地址必须是无凭据、无查询参数的 ws/wss 地址")
    values = dotenv_values(env_file) if env_file.exists() else {}
    token = values.get("ONEBOT_ACCESS_TOKEN") or secrets.token_urlsafe(36)
    if (not 32 <= len(token) <= 512 or not token.isascii()
            or any(ord(c) <= 32 or ord(c) >= 127 for c in token)):
        raise ValueError("ONEBOT_ACCESS_TOKEN 必须为 32–512 个无空白的 ASCII 字符")
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir = data_dir / "config"
    config_dir.mkdir(exist_ok=True)
    target = config_dir / "onebot11.json"
    if target.exists() or list(config_dir.glob("onebot11_*.json")):
        raise ValueError("已有 NapCat 连接配置；请在管理页修改，初始化不会覆盖账号配置")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch(exist_ok=True)
    config = {
        "network": {
            "httpServers": [{"enable": True, "name": "lingxi-http", "host": "0.0.0.0", "port": 3000,
                             "enableCors": False, "enableWebsocket": False, "messagePostFormat": "array",
                             "token": token, "debug": False}],
            "httpSseServers": [], "httpClients": [], "websocketServers": [],
            "websocketClients": [{"enable": True, "name": "lingxi", "url": websocket_url,
                                  "reportSelfMessage": False, "messagePostFormat": "array", "token": token,
                                  "debug": False, "heartInterval": 30000, "reconnectInterval": 5000}],
            "plugins": [],
        }, "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False,
    }
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    webui = config_dir / "webui.json"
    if not webui.exists():
        webui.write_text(json.dumps({"host": "0.0.0.0", "port": 6099, "prefix": "",
                                    "token": secrets.token_urlsafe(32), "loginRate": 3}) + "\n", encoding="utf-8")
        webui.chmod(0o600)
    for key, value in {"ONEBOT_ENABLED": "1", "ONEBOT_ACCESS_TOKEN": token,
                       "ONEBOT_API_URL": values.get("ONEBOT_API_URL") or "http://napcat:3000"}.items():
        set_key(str(env_file), key, value)
    env_file.chmod(0o600)
    for directory in (data_dir / "qq", data_dir / "plugins"):
        directory.mkdir(exist_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description="初始化 NapCat 直连配置；密钥仅写入本地配置文件")
    parser.add_argument("--env-file", type=Path, default=Path("data/.env"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/napcat"))
    parser.add_argument("--ws-url", default="ws://app:8000/lingxi/api/v1/integrations/onebot/ws")
    args = parser.parse_args()
    try:
        target = initialize(args.env_file, args.data_dir, args.ws_url)
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"已生成 {target}。管理页登录令牌保存在同目录 webui.json 的 token 字段；不要提交到 Git。")


if __name__ == "__main__":
    main()
