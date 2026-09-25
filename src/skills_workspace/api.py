"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .experiments import ExperimentService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = dict(body or {})
    # 请求体中的 actor_id 表示操作者，改由 X-Actor-Id 头提供，避免与关键字参数重复。
    body.pop("actor_id", None)
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # ---- 软件测试实验平台路由，需要 ExperimentService ----
        if not isinstance(service, ExperimentService):
            return 404, {"error": "route_not_found", "message": "接口不存在"}
        if method == "POST" and parsed.path == "/builds":
            receipt = service.register_build(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/case-packages":
            receipt = service.register_case_package(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/environments":
            receipt = service.register_environment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/runs":
            receipt = service.create_run(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "shards" and method == "POST":
            result = service.upload_shard(
                actor_id=actor_id, run_id=segments[1],
                shard_index=body["shard_index"], shard=body["shard"],
            )
            return 200, result
        if len(segments) == 2 and segments[0] == "runs" and method == "GET":
            return 200, service.get_run(segments[1])
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "replay" and method == "GET":
            return 200, service.replay_run(segments[1])
        if method == "POST" and parsed.path == "/reviews":
            receipt = service.open_review(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if len(segments) == 2 and segments[0] == "reviews" and method == "GET":
            return 200, service.get_review(segments[1])
        if len(segments) == 3 and segments[0] == "reviews" and segments[2] == "supplement" and method == "POST":
            receipt = service.submit_supplement(actor_id=actor_id, review_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if len(segments) == 3 and segments[0] == "reviews" and segments[2] == "decision" and method == "POST":
            receipt = service.decide_review(actor_id=actor_id, review_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/statistics":
            query = parse_qs(parsed.query)
            include = query.get("include_superseded", ["false"])[0].lower() == "true"
            return 200, service.statistics(include_superseded=include)
        if method == "GET" and parsed.path == "/signatures":
            query = parse_qs(parsed.query)
            signature = query.get("signature", [""])[0]
            return 200, service.search_signature(signature)
        if method == "POST" and parsed.path == "/maintenance/resume":
            return 200, {"frozen_runs": service.resume_pending(),
                         "expired_reviews": service.expire_due_reviews()}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动软件测试实验运行与缺陷复现服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    service = ExperimentService(database)
    # 中断重启后：继续未完成的分片合并，并过期已截止的复核。
    service.resume_pending()
    service.expire_due_reviews()
    Handler.service = service
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
