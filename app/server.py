"""Loopback-only HTTP interface for the local replenishment app."""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import secrets
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from backend import APP_DIR, DEFAULT_DB, DEFAULT_SEED, VERSION, AppError, Store, now


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_handler(store, token, static_dir):
    static_dir = Path(static_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ReplenishmentLocal/1"

        def log_message(self, fmt, *args):
            # No request body, token, or business data are logged.
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

        def send_bytes(self, payload, status=200, content_type="application/json; charset=utf-8", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def json_response(self, value, status=200):
            self.send_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), status)

        def check_local(self, write=False):
            port = self.server.server_address[1]
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            if host not in allowed_hosts:
                raise AppError("仅允许本机访问", 403, "invalid_host")
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + host:
                raise AppError("不允许跨站访问", 403, "invalid_origin")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise AppError("不允许跨站访问", 403, "cross_site")
            if write:
                supplied = self.headers.get("X-App-Token", "")
                if not hmac.compare_digest(supplied, token):
                    raise AppError("本机访问凭据失效，请刷新页面", 403, "invalid_token")
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    raise AppError("请求内容必须是JSON", 415, "content_type")

        def read_body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise AppError("请求长度无效") from None
            if length < 0 or length > 12_000_000:
                raise AppError("请求内容过大", 413, "too_large")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=lambda _: None)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AppError("JSON内容无效") from None
            if not isinstance(value, dict):
                raise AppError("JSON必须是对象")
            return value

        def dispatch(self):
            write = self.command != "GET"
            self.check_local(write)
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            parts = [p for p in path.split("/") if p]
            if self.command == "GET":
                if path == "/api/health":
                    return self.json_response({"ok": True, "app": "shoe-replenishment", "version": VERSION,
                                               "database": str(store.path)})
                if path == "/api/bootstrap":
                    data = store.bootstrap()
                    data["token"] = token
                    data["metadata"]["token"] = token
                    return self.json_response(data)
                if path == "/api/recommendations":
                    try:
                        days = int(query.get("days", ["35"])[0])
                    except ValueError:
                        raise AppError("目标天数无效") from None
                    return self.json_response(store.recommend(query.get("style", [None])[0], days))
                if path == "/api/audit":
                    return self.json_response(store.audit())
                if path == "/api/returns":
                    try:
                        days = int(query.get("days", ["28"])[0])
                    except ValueError:
                        raise AppError("退货窗口天数无效") from None
                    return self.json_response(store.returns(days, query.get("style", [None])[0]))
                if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[3] == "export.csv":
                    return self.send_bytes(store.export_csv(parts[2]), content_type="text/csv; charset=utf-8",
                                           extra={"Content-Disposition": f'attachment; filename="{parts[2]}.csv"'})
                if path.startswith("/api/"):
                    raise AppError("接口不存在", 404, "not_found")
                if path.startswith("/product-images/"):
                    target = store.product_image_path(path)
                    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                    return self.send_bytes(target.read_bytes(), content_type=ctype)
                relative = path.lstrip("/") or "index.html"
                target = (static_dir / relative).resolve()
                if not target.is_relative_to(static_dir) or not target.is_file():
                    raise AppError("页面文件不存在", 404, "not_found")
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                if target.suffix in (".js", ".mjs"):
                    ctype = "application/javascript"
                return self.send_bytes(target.read_bytes(), content_type=ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
            payload = self.read_body()
            if self.command == "POST":
                if path == "/api/plans":
                    return self.json_response(store.create_plan(payload), 201)
                if path == "/api/feedback":
                    return self.json_response(store.feedback(payload), 201)
                if path == "/api/backup":
                    return self.json_response(store.backup(), 201)
                if path == "/api/snapshots":
                    return self.json_response(store.import_snapshot(payload.get("document")))
                if path == "/api/returns":
                    return self.json_response(store.import_returns(payload.get("document")))
                if len(parts) == 4 and parts[:2] == ["api", "plans"]:
                    if parts[3] == "adopt":
                        return self.json_response(store.adopt_plan(parts[2], payload))
                    if parts[3] == "confirm":
                        return self.json_response(store.confirm_plan(parts[2], payload))
                if len(parts) == 4 and parts[:2] == ["api", "purchases"]:
                    if parts[3] == "cancel":
                        return self.json_response(store.cancel_purchase(parts[2], payload))
                    if parts[3] == "match-shipment":
                        return self.json_response(store.match_lingxing_shipment(parts[2], payload))
                    if parts[3] == "events":
                        return self.json_response(store.purchase_event(parts[2], payload))
                    if parts[3] == "reconcile":
                        return self.json_response(store.reconcile_purchase(parts[2], payload))
            if (self.command == "PUT" and len(parts) == 4
                    and parts[:2] == ["api", "purchases"] and parts[3] == "logistics"):
                return self.json_response(store.update_purchase_logistics(parts[2], payload))
            if self.command == "PUT" and len(parts) == 3:
                if parts[:2] == ["api", "plans"]:
                    return self.json_response(store.update_plan(parts[2], payload))
                if parts[:2] == ["api", "cycles"]:
                    return self.json_response(store.update_cycle(parts[2], payload))
                if parts[:2] == ["api", "styles"]:
                    return self.json_response(store.update_style_preference(parts[2], payload))
                if parts[:2] == ["api", "historical-batches"]:
                    return self.json_response(store.record_historical_batch(parts[2], payload))
                if parts == ["api", "settings", "planning"]:
                    return self.json_response(store.update_planning_settings(payload))
            raise AppError("接口不存在", 404, "not_found")

        def handle_request(self):
            try:
                self.dispatch()
            except AppError as error:
                self.json_response({"error": error.message, "code": error.code}, error.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                traceback.print_exc()
                self.json_response({"error": "本地服务处理失败；记录未完成保存，请重试或查看服务日志。", "code": "server_error"}, 500)

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request

        def do_OPTIONS(self):
            self.json_response({"error": "不允许跨站请求", "code": "not_allowed"}, 403)

    return Handler


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="鞋款补货本地服务")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--runtime-file", type=Path, default=DEFAULT_DB.parent / "runtime.json")
    parser.add_argument("--static-dir", type=Path, default=APP_DIR / "static")
    args = parser.parse_args(argv)
    store = Store(args.db, args.seed, actor="local-ui")
    token = secrets.token_urlsafe(36)
    server = LocalServer(("127.0.0.1", args.port), make_handler(store, token, args.static_dir))
    port = server.server_address[1]
    runtime = dict(pid=os.getpid(), port=port, url=f"http://127.0.0.1:{port}", token=token,
                   db=str(store.path), database=str(store.path), started_at=now(), version=VERSION,
                   app="shoe-replenishment")
    args.runtime_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path = args.runtime_file.with_suffix(".tmp")
    temp_path.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(args.runtime_file)
    print(f"鞋款补货本地服务已启动：http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            existing = json.loads(args.runtime_file.read_text(encoding="utf-8"))
            if existing.get("pid") == os.getpid():
                args.runtime_file.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
