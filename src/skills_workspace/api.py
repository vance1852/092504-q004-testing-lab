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
          headers: dict[str, str] | None = None,
          experiment_service: ExperimentService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    experiments = experiment_service
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
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if experiments is not None:
            status, payload = _route_experiments(experiments, method, parsed.path, body,
                                                 actor_id, query)
            if status is not None:
                return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _route_experiments(experiments: ExperimentService, method: str, path: str,
                       body: dict[str, Any], actor_id: str,
                       query) -> tuple[int | None, dict[str, Any]]:
    """软件测试实验平台路由；未命中时返回 (None, {})。"""

    parts = [segment for segment in path.split("/") if segment]
    if method == "POST" and path == "/builds":
        receipt = experiments.register_build(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/case-packages":
        receipt = experiments.register_case_package(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/environments":
        receipt = experiments.register_environment(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/experiments":
        receipt = experiments.register_experiment(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/runs/open":
        receipt = experiments.open_run(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/runs/shards":
        receipt = experiments.upload_shard(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/runs/recover":
        return 200, {"items": experiments.recover_interrupted()}
    if method == "GET" and len(parts) == 3 and parts[0] == "runs" and parts[2] == "replay":
        return 200, experiments.get_run(parts[1], actor_id or None)
    if method == "GET" and len(parts) == 2 and parts[0] == "experiments":
        return 200, experiments.get_experiment(parts[1], actor_id or None)
    if method == "POST" and path == "/reviews":
        receipt = experiments.open_review(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/reviews/explanation":
        receipt = experiments.submit_explanation(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/reviews/decision":
        receipt = experiments.decide_review(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    if method == "POST" and path == "/stats/snapshots":
        receipt = experiments.stats_snapshot(actor_id=actor_id, **body)
        return (200 if receipt.get("replayed") else 201), receipt
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    experiment_service: ExperimentService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                experiment_service=self.experiment_service)
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

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.experiment_service = ExperimentService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        # 重启后继续合并中断时已齐套但尚未冻结的分片。
        Handler.experiment_service.recover_interrupted()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
