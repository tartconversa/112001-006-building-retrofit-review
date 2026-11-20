"""HTTP application for the public-building retrofit review workspace.

Standard library only: ``http.server`` plus JSON over ``fetch``.  The server is
deliberately thin - all domain rules live in :mod:`retrofit.analysis` and
:mod:`retrofit.state` so they can be tested without a browser.

Run with ``python -m retrofit.server --workspace state/workspace.json``.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import urlparse

from .analysis import AnalysisConfig
from .inputs import DataError, parse_date
from .state import Workspace

STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY = 8 * 1024 * 1024


class Application:
    def __init__(self, workspace_path: str | Path, seed_dir: str | Path | None = None):
        self.workspace_path = Path(workspace_path)
        self.workspace = Workspace.load(self.workspace_path)
        self.seed_dir = Path(seed_dir) if seed_dir else None
        if not self.workspace.records["buildings"] and self.seed_dir and self.seed_dir.is_dir():
            self.workspace.import_csv_dir(self.seed_dir, replace=True)
            self.workspace.save(self.workspace_path)

    def save(self) -> None:
        self.workspace.save(self.workspace_path)

    def analysis_payload(self) -> dict:
        result = self.workspace.analyse()
        dataset = self.workspace.dataset()
        approval_view = self.workspace.approval_view(result)
        return {
            "analysis": result.as_dict(),
            "approvals": approval_view["by_project"],
            "approval_log": approval_view,
            "reaudit": self.workspace.reaudit(),
            "records": {key: rows for key, rows in self.workspace.records.items()},
            "building_options": [
                {
                    "building_id": b,
                    "use_type": (
                        dataset.plan_on(b, result.as_of).use_type
                        if dataset.plan_on(b, result.as_of)
                        else ""
                    ),
                }
                for b in dataset.building_ids()
            ],
            "used_types": sorted({p.use_type for p in dataset.buildings}),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "RetrofitReview/1.0"
    app: Application

    # ---- plumbing ------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # quieter tests
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise DataError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise DataError("request body must be a JSON object")
        return payload

    # ---- routing -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._send_file(STATIC_DIR / "app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._send_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
            elif path == "/charts.js":
                self._send_file(STATIC_DIR / "charts.js", "text/javascript; charset=utf-8")
            elif path == "/api/analysis":
                self._send_json(self.app.analysis_payload())
            elif path == "/api/reaudit":
                self._send_json(self.app.workspace.reaudit())
            elif path == "/api/export":
                payload = {
                    **{key: rows for key, rows in self.app.workspace.records.items()},
                }
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=retrofit-workspace.json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (DataError, ValueError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            self._send_json({"error": f"internal error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            status, result = self._dispatch_post(path, payload)
            self.app.save()
            self._send_json(result, status)
        except DataError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            self._send_json({"error": f"internal error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _dispatch_post(self, path: str, payload: dict) -> tuple[int, dict]:
        app = self.app
        if path == "/api/import":
            directory = payload.get("directory")
            if not directory:
                raise DataError("缺少目录路径。")
            dataset = app.workspace.import_csv_dir(directory, replace=bool(payload.get("replace", True)))
            return HTTPStatus.OK, {"imported": len(dataset.projects), "payload": app.analysis_payload()}
        if path == "/api/import-records":
            incoming = payload.get("records")
            if not isinstance(incoming, dict):
                raise DataError("records 必须是对象。")
            dataset = app.workspace.import_records(
                incoming,
                replace=bool(payload.get("replace", False)),
                actor=str(payload.get("actor") or "资料员"),
                note=str(payload.get("note") or "JSON 资料导入"),
            )
            return HTTPStatus.OK, {
                "imported": len(dataset.projects),
                "payload": app.analysis_payload(),
            }
        if path == "/api/config":
            config = AnalysisConfig.from_dict(payload.get("config") or {})
            app.workspace.config = config.as_dict()
            return HTTPStatus.OK, {"config": app.workspace.config, "payload": app.analysis_payload()}
        if path == "/api/as-of":
            value = payload.get("as_of")
            app.workspace.as_of = parse_date(value).isoformat() if value else None
            return HTTPStatus.OK, {"as_of": app.workspace.as_of, "payload": app.analysis_payload()}
        if path == "/api/projects":
            actor = str(payload.pop("actor", "资产管理员"))
            assessment = app.workspace.register_measure(payload, actor)
            return HTTPStatus.CREATED, {
                "assessment": assessment.as_dict(app.workspace.analyse().config),
                "payload": app.analysis_payload(),
            }
        if path == "/api/events":
            actor = str(payload.pop("actor", "资产管理员"))
            app.workspace.register_event(payload, actor)
            return HTTPStatus.CREATED, {"payload": app.analysis_payload()}
        if path == "/api/approve":
            project_id = str(payload.get("project_id") or "")
            if not project_id:
                raise DataError("缺少 project_id。")
            approval = app.workspace.approve(
                project_id,
                str(payload.get("actor") or "资产管理员"),
                str(payload.get("note") or ""),
            )
            return HTTPStatus.OK, {"approval": approval, "payload": app.analysis_payload()}
        if path == "/api/handoff":
            project_id = str(payload.get("project_id") or "")
            handoff = app.workspace.handoff(
                project_id,
                str(payload.get("actor") or "资产管理员"),
                str(payload.get("team") or ""),
                str(payload.get("note") or ""),
            )
            return HTTPStatus.OK, {"handoff": handoff, "payload": app.analysis_payload()}
        if path == "/api/measurements":
            project_id = str(payload.get("project_id") or "")
            items = payload.get("measurements") or payload.get("measurement") or []
            stored = app.workspace.record_measurements(
                project_id, items, str(payload.get("actor") or "设施团队")
            )
            return HTTPStatus.CREATED, {"measurements": stored, "payload": app.analysis_payload()}
        if path == "/api/accept":
            project_id = str(payload.get("project_id") or "")
            accepted_on = payload.get("accepted_on")
            acceptance = app.workspace.accept(
                project_id,
                str(payload.get("actor") or "设施团队"),
                str(payload.get("note") or ""),
                parse_date(accepted_on) if accepted_on else None,
            )
            return HTTPStatus.OK, {"acceptance": acceptance, "payload": app.analysis_payload()}
        if path == "/api/correction":
            project_id = payload.get("project_id") or None
            updates = payload.get("updates") or {}
            measurements = payload.get("measurements") or []
            report = app.workspace.submit_correction(
                project_id,
                str(payload.get("actor") or "资料员"),
                str(payload.get("note") or ""),
                updates=updates,
                measurements=measurements,
            )
            return HTTPStatus.OK, {"reaudit": report, "payload": app.analysis_payload()}
        raise DataError(f"unknown endpoint {path}")


def make_server(
    workspace_path: str | Path,
    seed_dir: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    verbose: bool = False,
) -> ThreadingHTTPServer:
    app = Application(workspace_path, seed_dir=seed_dir)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.verbose = verbose
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公共建筑改造复核服务")
    parser.add_argument("--workspace", default="state/workspace.json", help="workspace JSON path")
    parser.add_argument("--seed", default="data", help="CSV directory to seed an empty workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    httpd = make_server(args.workspace, args.seed, args.host, args.port, args.verbose)
    print(f"公共建筑改造复核已启动： http://{args.host}:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
