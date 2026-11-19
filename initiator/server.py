"""Loopback-only HTTP API; no arbitrary commands or unauthenticated file reads."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

WEB = Path(__file__).with_name("web")


def make_server(service, port=0, token=None):
    token = token or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.headers_common(len(body))
            self.end_headers()
            self.wfile.write(body)

        def headers_common(self, length):
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")

        def authorized(self):
            if self.headers.get("Host") not in ("127.0.0.1:%d" % self.server.server_port, "localhost:%d" % self.server.server_port):
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://127.0.0.1:%d" % self.server.server_port, "http://localhost:%d" % self.server.server_port):
                return False
            return hmac.compare_digest(self.headers.get("X-Runner-Token", ""), token)

        def do_GET(self):
            target = urlsplit(self.path)
            if not target.path.startswith("/api/"):
                routes = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/app.css": ("app.css", "text/css")}
                if target.path not in routes:
                    self.reply(dict(error="找不到页面"), 404)
                    return
                name, mime = routes[target.path]
                body = (WEB / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.headers_common(len(body))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self.authorized():
                self.reply(dict(error="本地访问凭证无效，请从启动器重新打开页面"), 403)
                return
            try:
                args = {k: v[0] for k, v in parse_qs(target.query, keep_blank_values=True).items()}
                if target.path == "/api/bootstrap":
                    self.reply(service.bootstrap(args.get("session"), args.get("host_id"), args.get("archived") == "true"))
                elif target.path == "/api/browse":
                    self.reply(service.browse(args["host_id"], args.get("path", "")))
                elif target.path.startswith("/api/sessions/"):
                    parts = target.path.split("/")
                    identifier = parts[3]
                    if len(parts) == 4:
                        self.reply(service.session(identifier))
                    elif len(parts) == 5 and parts[4] == "logs":
                        self.reply(service.logs(identifier, args["task_id"], args.get("stream", "stdout"), args.get("cursor", 0)))
                    else:
                        self.reply(dict(error="找不到接口"), 404)
                else:
                    self.reply(dict(error="找不到接口"), 404)
            except KeyError as exc:
                self.reply(dict(error=str(exc)), 404)
            except Exception as exc:
                self.reply(dict(error=str(exc)), 400)

        def do_POST(self):
            if not self.authorized():
                self.reply(dict(error="本地访问凭证无效"), 403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4 * 1024 * 1024:
                    raise ValueError("请求大小无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求必须为 JSON 对象")
                path = urlsplit(self.path).path
                if path == "/api/hosts/probe":
                    self.reply(dict(host=service.probe(data)))
                elif path == "/api/hosts/rename":
                    self.reply(dict(host=service.rename(data["id"], data["label"])))
                elif path == "/api/hosts/capacity":
                    self.reply(service.capacity(data["id"], data["capacity"], data.get("execute") is True))
                elif path == "/api/sessions/create":
                    self.reply(service.create(data["request"]))
                elif path.startswith("/api/sessions/"):
                    parts = path.split("/")
                    if len(parts) != 5:
                        raise ValueError("接口路径无效")
                    if parts[4] == "deploy":
                        self.reply(service.deploy(parts[3], data))
                    elif parts[4] == "reconnect":
                        self.reply(service.reconnect(parts[3]))
                    elif parts[4] == "assign":
                        self.reply(service.assign(parts[3], data))
                    elif parts[4] in ("archive", "restore"):
                        self.reply(service.archive(parts[3], archived=parts[4] == "archive"))
                    elif parts[4] == "delete":
                        self.reply(service.delete(parts[3]))
                    else:
                        self.reply(dict(error="找不到接口"), 404)
                else:
                    self.reply(dict(error="找不到接口"), 404)
            except KeyError as exc:
                self.reply(dict(error=str(exc)), 404)
            except Exception as exc:
                self.reply(dict(error=str(exc)), 400)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server, token
