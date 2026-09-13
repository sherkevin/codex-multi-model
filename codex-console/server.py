#!/usr/bin/env python3
"""codex-console — Codex 多模型「配置器」（不是聊天客户端）。

定位：一个只绑 127.0.0.1 的本地网页，用来可视化配置 Codex 的 API/AK/模型，
维持「绕过账号登录」状态，并一键重启 Codex 桌面 App 让配置生效。
**真正用模型是在 Codex 本体里**；本网页只负责写 Codex 会读的那批文件，
因此「网页配置」与「Codex 实际配置」天然一致（同一份文件）。

它读写：
  ~/.codex/config.toml            provider / base_url / 默认模型 / 思考档
  ~/.codex/custom-model-catalog.json   /model 显示项
  ~/.codex/auth.json              apikey 模式（绕过 ChatGPT 账号登录）
  ~/.codex/router-secrets.env     上游 AK（掩码，绝不回显）
并可重启：Codex 桌面 App（ChatGPT.app） / 本地中转。
"""
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config_io

HOST = os.environ.get("CODEX_CONSOLE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_CONSOLE_PORT", "8420"))
ROUTER = os.environ.get("CODEX_ROUTER_URL", "http://127.0.0.1:8317").rstrip("/")
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def router_alive():
    try:
        with urllib.request.urlopen(f"{ROUTER}/v1/models", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-console/0.2"

    def log_message(self, *a):
        pass

    # ── helpers ──
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def _static(self, rel):
        path = os.path.normpath(os.path.join(WEB, rel))
        if not path.startswith(WEB) or not os.path.isfile(path):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            self._send(200, f.read(), CTYPES.get(ext, "application/octet-stream"))

    # ── GET ──
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        try:
            if p in ("/", "/index.html"):
                self._static("index.html")
            elif p == "/api/health":
                self._send(200, {"ok": True, "router": router_alive(), "router_url": ROUTER})
            elif p == "/api/status":
                self.api_status()
            elif p == "/api/mode":
                self._send(200, config_io.read_run_mode())
            elif p == "/api/models":
                self.api_models()
            elif p == "/api/config":
                self._send(200, config_io.read_config())
            elif p == "/api/catalog":
                self._send(200, config_io.read_catalog())
            elif p == "/api/routes":
                self.api_routes()
            elif p.startswith("/app.js") or p.startswith("/styles.css") or p.startswith("/vendor/"):
                self._static(p.lstrip("/"))
            else:
                self._send(404, {"error": f"no route {p}"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def api_status(self):
        """一致性面板：Codex 自己报告的登录态 + 实时 config + 中转 + 目录。"""
        try:
            cfg = config_io.read_config()["top"]
        except Exception:
            cfg = {}
        try:
            catn = len(config_io.read_catalog()["models"])
        except Exception:
            catn = None
        self._send(200, {
            "login": config_io.codex_login_status(),
            "auth": config_io.read_auth(),
            "config": cfg,
            "router": router_alive(),
            "router_url": ROUTER,
            "catalog_models": catn,
            "desktop_app": config_io.CODEX_APP,
            "app_process": config_io.APP_PROCESS,
        })

    def api_models(self):
        ids = []
        try:
            with urllib.request.urlopen(f"{ROUTER}/v1/models", timeout=5) as r:
                ids = [m["id"] for m in json.loads(r.read()).get("data", [])]
        except Exception as e:
            self._send(502, {"error": f"中转不可达：{e}", "router_url": ROUTER})
            return
        disp = {}
        try:
            for m in config_io.read_catalog()["models"]:
                disp[m["slug"]] = m
        except Exception:
            pass
        out = [{"id": mid,
                "display_name": disp.get(mid, {}).get("display_name") or mid,
                "description": disp.get(mid, {}).get("description") or ""}
               for mid in ids]
        self._send(200, {"models": out})

    def api_routes(self):
        """透明路由：真相取自中转 /v1/routes（生效值），中转不可达则回退本地文件。
        合并目录显示字段 + 自动判定该直连还是走中转，供前端三张卡展示。"""
        try:
            with urllib.request.urlopen(f"{ROUTER}/v1/routes", timeout=5) as r:
                live = json.loads(r.read())
        except Exception:
            live = None
        routing = live or config_io.read_routes_file() or \
            {"providers": {}, "models": {}, "prefix_routes": []}
        disp = {}
        try:
            for m in config_io.read_catalog()["models"]:
                disp[m["slug"]] = m
        except Exception:
            pass
        try:
            cur = config_io.read_config()["top"].get("model_provider")
        except Exception:
            cur = None
        self._send(200, {
            "routing": routing,
            "live": bool(live),
            "source": routing.get("source"),
            "backend": config_io.compute_backend_mode(routing),
            "current_provider": cur,
            "display": disp,
        })

    # ── POST ──
    def do_POST(self):
        p = self.path.split("?", 1)[0]
        try:
            if p == "/api/config":
                self._send(200, config_io.write_config(self._read_json()))
            elif p == "/api/catalog":
                self._send(200, config_io.write_catalog(self._read_json()))
            elif p == "/api/secrets":
                self.api_secrets()
            elif p == "/api/auth":
                self._send(200, config_io.ensure_auth_bypass())
            elif p == "/api/mode":
                self.api_mode()
            elif p == "/api/routes":
                self._send(200, config_io.write_routes_file(self._read_json()))
            elif p == "/api/restart":
                self.api_restart()
            else:
                self._send(404, {"error": f"no route {p}"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def api_secrets(self):
        body = self._read_json()
        if "set" in body:
            self._send(200, config_io.write_secret(body["set"].get("name"),
                                                   body["set"].get("value", "")))
        else:
            names = body.get("names") or []
            if not names:
                try:
                    names = sorted({v["env_key"] for v in
                                    config_io.read_config()["providers"].values()
                                    if v.get("env_key")})
                except Exception:
                    names = []
            self._send(200, config_io.read_secret_status(names))

    def api_restart(self):
        target = self._read_json().get("target", "desktop")
        res = config_io.restart_router() if target == "router" else config_io.restart_desktop()
        self._send(200 if res.get("ok") else 500, res)

    def api_mode(self):
        target = (self._read_json().get("target") or "").strip()
        if target not in ("custom", "native"):
            self._send(400, {"error": "target 必须是 custom 或 native"})
            return
        res = config_io.set_run_mode(target)
        self._send(200 if res.get("ok") else 500, res)


class Server(ThreadingHTTPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def main():
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
