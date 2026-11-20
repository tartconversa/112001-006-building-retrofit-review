"""Workspace state: user-entered measures, approvals and the audit trail.

The workspace deliberately keeps three things apart:

* ``records``  – the imported source data (CSV or JSON), replaceable on a
  correction/re-import without losing decisions.
* ``approvals`` – the frozen basis at the moment a project was approved, which
  is what a later re-audit is compared against.
* ``log`` – an append-only decision log (who did what, when, why).

Nothing in this module recomputes a decision.  It delegates to
:mod:`retrofit.analysis` and stores what came back.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from . import analysis
from .analysis import AnalysisConfig, AnalysisResult, ProjectAssessment, basis_snapshot, compare_to_approved
from .inputs import (
    Dataset,
    DataError,
    Project,
    dataset_from_records,
    load_csv_dir,
    load_json_dataset,
    parse_date,
    parse_number,
)

WORKSPACE_VERSION = 1
DATASET_KEYS = ("buildings", "energy", "comfort", "targets", "projects", "measurements", "events")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class Workspace:
    records: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {key: [] for key in DATASET_KEYS}
    )
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    as_of: str | None = None

    # ---- persistence ----------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Workspace":
        path = Path(path)
        if not path.exists():
            return cls()
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise DataError("workspace file must contain a JSON object")
        version = data.get("version", WORKSPACE_VERSION)
        if version > WORKSPACE_VERSION:
            raise DataError(f"workspace version {version} is newer than this application supports")
        records = data.get("records") or {}
        workspace = cls(
            records={key: list(records.get(key, [])) for key in DATASET_KEYS},
            approvals=dict(data.get("approvals") or {}),
            log=list(data.get("log") or []),
            config=dict(data.get("config") or {}),
            as_of=data.get("as_of"),
        )
        return workspace

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": WORKSPACE_VERSION,
            "as_of": self.as_of,
            "config": self.config,
            "records": self.records,
            "approvals": self.approvals,
            "log": self.log,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
        tmp.replace(path)

    # ---- records --------------------------------------------------------

    def dataset(self) -> Dataset:
        dataset = dataset_from_records(
            {key: list(rows) for key, rows in self.records.items() if key in DATASET_KEYS}
        )
        return dataset

    def import_csv_dir(self, directory: str | Path, *, replace: bool = True) -> Dataset:
        dataset = load_csv_dir(directory)
        if replace:
            self.records = {key: [] for key in DATASET_KEYS}
        for key in DATASET_KEYS:
            rows = getattr(dataset, _attr_for(key))
            if replace or not self.records.get(key):
                self.records[key] = [_record_to_row(item) for item in rows]
            else:
                by_id = {_row_key(key, row): row for row in self.records[key]}
                for item in rows:
                    row = _record_to_row(item)
                    by_id[_row_key(key, row)] = row
                self.records[key] = list(by_id.values())
        self._log("import", None, "system", f"导入 {Path(directory).name} 资料")
        return dataset

    def import_json(self, files: Iterable[str | Path], *, replace: bool = True) -> Dataset:
        dataset = load_json_dataset(files)
        if replace:
            self.records = {key: [] for key in DATASET_KEYS}
        for key in DATASET_KEYS:
            rows = getattr(dataset, _attr_for(key))
            if replace or not self.records.get(key):
                self.records[key] = [_record_to_row(item) for item in rows]
            else:
                by_id = {_row_key(key, row): row for row in self.records[key]}
                for item in rows:
                    row = _record_to_row(item)
                    by_id[_row_key(key, row)] = row
                self.records[key] = list(by_id.values())
        self._log("import", None, "system", "导入 JSON 资料")
        return dataset

    def import_records(
        self,
        incoming: dict[str, list[dict[str, Any]]],
        *,
        replace: bool = False,
        actor: str = "system",
        note: str = "资料补录",
    ) -> Dataset:
        """Merge records row-by-row; duplicate ids are upserted by key."""

        if replace:
            self.records = {key: [] for key in DATASET_KEYS}
        seen: dict[str, list[dict[str, Any]]] = {}
        for key in DATASET_KEYS:
            existing = [] if replace else list(self.records.get(key, []))
            by_id = {_row_key(key, row): row for row in existing}
            for row in incoming.get(key, []):
                by_id[_row_key(key, row)] = {str(k): ("" if v is None else str(v)) for k, v in row.items()}
            seen[key] = list(by_id.values())
        self.records = seen
        self._log("import", None, actor, note)
        return self.dataset()

    # ---- decision actions ----------------------------------------------

    def _log(self, action: str, project_id: str | None, actor: str, note: str, **extra: Any) -> None:
        entry = {
            "at": _now(),
            "action": action,
            "project_id": project_id,
            "actor": actor or "unknown",
            "note": note,
            **extra,
        }
        self.log.append(entry)

    def analyse(self, as_of: date | None = None) -> AnalysisResult:
        config = AnalysisConfig.from_dict(self.config)
        when = parse_date(as_of) if as_of else (parse_date(self.as_of) if self.as_of else date.today())
        return analysis.analyse(self.dataset(), config=config, as_of=when)

    def _approval_rows(self, result: AnalysisResult) -> dict[str, dict[str, Any]]:
        """Approval summaries embedded in the analysis payload given to the UI."""

        by_project = result.by_project()
        rows: dict[str, dict[str, Any]] = {}
        for project_id, approval in self.approvals.items():
            assessment = by_project.get(project_id)
            rows[project_id] = {
                **approval,
                "current": compare_to_approved(assessment, approval["basis"]) if assessment else None,
            }
        return rows

    def register_measure(self, payload: dict[str, Any], actor: str) -> ProjectAssessment:
        """Register a proposed measure, its measures and planned closure."""

        required = ("project_id", "building_id", "measure_start", "measure_end")
        missing = [key for key in required if not str(payload.get(key, "")).strip()]
        if missing:
            raise DataError("缺少必填字段：" + ", ".join(missing))
        row = {
            "project_id": str(payload["project_id"]).strip(),
            "building_id": str(payload["building_id"]).strip(),
            "name": str(payload.get("name") or payload["project_id"]).strip(),
            "measures": str(payload.get("measures") or "").strip(),
            "measure_start": str(payload["measure_start"]).strip(),
            "measure_end": str(payload["measure_end"]).strip(),
            "commissioning_start": str(payload.get("commissioning_start") or "").strip(),
            "commissioning_end": str(payload.get("commissioning_end") or "").strip(),
            "planned_closure_start": str(payload.get("planned_closure_start") or "").strip(),
            "planned_closure_end": str(payload.get("planned_closure_end") or "").strip(),
            "predicted_saving_pct": str(payload.get("predicted_saving_pct") or "").strip(),
            "cost": str(payload.get("cost") or "").strip(),
            "status": str(payload.get("status") or "proposed").strip(),
            "priority_note": str(payload.get("priority_note") or "").strip(),
        }
        start, end = parse_date(row["measure_start"]), parse_date(row["measure_end"])
        if end < start:
            raise DataError("施工结束日期不能早于开始日期。")
        closure_start = row["planned_closure_start"]
        closure_end = row["planned_closure_end"]
        if closure_start or closure_end:
            if not (closure_start and closure_end):
                raise DataError("计划停用时段需同时填写起止日期。")
            cs, ce = parse_date(closure_start), parse_date(closure_end)
            if ce < cs:
                raise DataError("停用结束日期不能早于开始日期。")
            if cs < start or ce > end:
                raise DataError("计划停用时段必须落在施工期内，避免把停用影响带入比较期。")
        if row["predicted_saving_pct"]:
            predicted = parse_number(row["predicted_saving_pct"], field_name="predicted_saving_pct")
            if predicted is None or not 0 <= predicted <= 100:
                raise DataError("预计节能率应在 0–100% 之间。")
        if row["cost"]:
            cost = parse_number(row["cost"], field_name="cost")
            if cost is None or cost < 0:
                raise DataError("预计投资应为非负数。")

        self.records["projects"] = [
            existing
            for existing in self.records.get("projects", [])
            if existing.get("project_id") != row["project_id"]
        ] + [row]
        self._log("register_measure", row["project_id"], actor, str(payload.get("note") or "登记拟改造措施"))
        return self._assessment_for(row["project_id"])

    def register_event(self, payload: dict[str, Any], actor: str) -> None:
        event_type = str(payload.get("event_type") or "").strip()
        if event_type not in {"meter_replacement", "tenant_change", "equipment_commissioning", "use_change", "other"}:
            raise DataError("未知的现场事件类型。")
        row = {
            "event_id": str(payload.get("event_id") or f"EV-{len(self.records.get('events', [])) + 1:03d}"),
            "building_id": str(payload.get("building_id") or "").strip(),
            "event_type": event_type,
            "start": str(payload.get("start") or "").strip(),
            "end": str(payload.get("end") or payload.get("start") or "").strip(),
            "verified": "true" if payload.get("verified") else "false",
            "note": str(payload.get("note") or "").strip(),
        }
        if not row["building_id"] or not row["start"]:
            raise DataError("现场事件需填写建筑和日期。")
        self.records["events"] = [
            existing for existing in self.records.get("events", []) if existing.get("event_id") != row["event_id"]
        ] + [row]
        self._log("register_event", None, actor, f"登记 {row['building_id']} {event_type}")

    def approve(self, project_id: str, actor: str, note: str) -> dict[str, Any]:
        result = self.analyse()
        assessment = result.by_project().get(project_id)
        if assessment is None:
            raise DataError(f"未找到项目 {project_id}")
        if assessment.decision not in {"priority", "conditional", "needs_measurement", "defer"}:
            raise DataError(
                f"项目 {project_id} 当前结论为“{assessment.decision_label}”，不能直接批准："
                + assessment.explanation
            )
        if assessment.phase in {"completed", "commissioning", "in_progress"}:
            raise DataError(f"项目 {project_id} 已开工或完工，不能按拟改造项目批准。")
        basis = basis_snapshot(assessment)
        self.approvals[project_id] = {
            "approved_by": actor or "unknown",
            "approved_at": _now(),
            "decided_on": result.as_of.isoformat(),
            "note": note,
            "decision": assessment.decision,
            "basis": basis,
            "handoff": None,
            "acceptance": None,
        }
        self._update_project(project_id, {"status": "approved", "approved_by": actor or "unknown", "approved_on": result.as_of.isoformat()})
        self._log(
            "approve",
            project_id,
            actor,
            note or "批准立项",
            decision=assessment.decision,
            saving_pct=assessment.saving_pct,
        )
        return self.approvals[project_id]

    def handoff(self, project_id: str, actor: str, team: str, note: str) -> dict[str, Any]:
        approval = self.approvals.get(project_id)
        if approval is None:
            raise DataError("项目尚未批准，不能移交设施团队。")
        if not team.strip():
            raise DataError("需填写设施团队名称。")
        approval["handoff"] = {"by": actor or "unknown", "at": _now(), "team": team.strip(), "note": note}
        self._update_project(project_id, {"status": "handed_off"})
        self._log("handoff", project_id, actor, note or f"移交 {team}")
        return approval["handoff"]

    def record_measurements(
        self, project_id: str, payload: dict[str, Any], actor: str
    ) -> list[dict[str, Any]]:
        items = payload if isinstance(payload, list) else [payload]
        stored = []
        existing_ids = {row.get("measurement_id") for row in self.records.get("measurements", [])}
        for index, item in enumerate(items, start=1):
            measurement_id = str(item.get("measurement_id") or "").strip()
            if not measurement_id:
                measurement_id = f"M-{project_id}-{len(existing_ids) + index:03d}"
            row = {
                "measurement_id": measurement_id,
                "project_id": project_id,
                "date": str(item.get("date") or "").strip(),
                "electric_kwh": str(item.get("electric_kwh") or "").strip(),
                "gas_m3": str(item.get("gas_m3") or "").strip(),
                "degree_days": str(item.get("degree_days") or "").strip(),
                "comfort_sample_pct": str(item.get("comfort_sample_pct") or "").strip(),
                "sample_count": str(item.get("sample_count") or "").strip(),
                "note": str(item.get("note") or "").strip(),
            }
            if not row["date"]:
                raise DataError("实测记录需填写日期。")
            parse_date(row["date"])
            self.records["measurements"] = [
                existing
                for existing in self.records.get("measurements", [])
                if existing.get("measurement_id") != measurement_id
            ] + [row]
            stored.append(row)
        self._log("record_measurements", project_id, actor, f"补录 {len(stored)} 条实测记录")
        return stored

    def accept(self, project_id: str, actor: str, note: str, accepted_on: date | None = None) -> dict[str, Any]:
        approval = self.approvals.get(project_id)
        if approval is None:
            raise DataError("项目未经批准，不能登记完工验收。")
        on = accepted_on or date.today()
        self._update_project(project_id, {"status": "completed", "accepted_on": on.isoformat()})
        assessment = self._assessment_for(project_id, as_of=on)
        recheck = compare_to_approved(assessment, approval["basis"])
        approval["acceptance"] = {
            "by": actor or "unknown",
            "at": _now(),
            "accepted_on": on.isoformat(),
            "note": note,
            "recheck": recheck,
        }
        self._log("accept", project_id, actor, note or "登记完工验收", changes=recheck["changes"])
        return approval["acceptance"]

    # ---- re-audit -------------------------------------------------------

    def submit_correction(
        self,
        project_id: str | None,
        actor: str,
        note: str,
        updates: dict[str, Any] | None = None,
        measurements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Apply a data correction and report what it changed.

        ``project_id`` scopes the correction note; the actual record updates
        may touch any table (a meter correction, an acceptance date, etc.).
        """

        if updates:
            for key, rows in updates.items():
                if key not in DATASET_KEYS:
                    raise DataError(f"未知资料类型 {key}")
                for row in rows:
                    identity = _row_key(key, {str(k): str(v) for k, v in row.items()})
                    replaced = False
                    for index, existing in enumerate(self.records.get(key, [])):
                        if _row_key(key, existing) == identity:
                            merged = dict(existing)
                            merged.update({str(k): ("" if v is None else str(v)) for k, v in row.items()})
                            self.records[key][index] = merged
                            replaced = True
                            break
                    if not replaced:
                        self.records[key].append(
                            {str(k): ("" if v is None else str(v)) for k, v in row.items()}
                        )
        if measurements and project_id:
            self.record_measurements(project_id, measurements, actor)
        self._log("correction", project_id, actor, note or "资料更正")
        return self.reaudit()

    def reaudit(self, as_of: date | None = None) -> dict[str, Any]:
        result = self.analyse(as_of)
        by_project = result.by_project()
        affected = []
        for project_id, approval in self.approvals.items():
            assessment = by_project.get(project_id)
            if assessment is None:
                continue
            diff = compare_to_approved(assessment, approval["basis"])
            if diff["affected"]:
                affected.append(
                    {
                        "project_id": project_id,
                        "building_id": assessment.project.building_id,
                        "name": assessment.project.name,
                        "phase": assessment.phase,
                        "previous_decision": approval["basis"].get("decision"),
                        "previous_label": approval["basis"].get("decision_label"),
                        "current_decision": assessment.decision,
                        "current_label": assessment.decision_label,
                        "changes": diff["changes"],
                        "outstanding": assessment.outstanding,
                        "retained_basis": approval["basis"],
                    }
                )
        rankings = {
            "completed": [
                {"project_id": a.project.project_id, "decision": a.decision, "rank_score": a.rank_score}
                for a in result.completed_ranking
            ],
            "candidates": [
                {"project_id": a.project.project_id, "decision": a.decision, "rank_score": a.rank_score}
                for a in result.candidate_ranking
            ],
        }
        return {
            "as_of": result.as_of.isoformat(),
            "affected": affected,
            "rankings": rankings,
            "summary": result.summary,
            "data_issues": result.data_issues,
        }

    # ---- helpers --------------------------------------------------------

    def _update_project(self, project_id: str, changes: dict[str, Any]) -> None:
        for row in self.records.get("projects", []):
            if row.get("project_id") == project_id:
                row.update({key: "" if value is None else str(value) for key, value in changes.items()})

    def _assessment_for(self, project_id: str, as_of: date | None = None) -> ProjectAssessment:
        result = self.analyse(as_of)
        assessment = result.by_project().get(project_id)
        if assessment is None:
            raise DataError(f"未找到项目 {project_id}")
        return assessment

    def approval_view(self, result: AnalysisResult) -> dict[str, Any]:
        by_project = result.by_project()
        rows = []
        for project_id, approval in self.approvals.items():
            assessment = by_project.get(project_id)
            diff = compare_to_approved(assessment, approval["basis"]) if assessment else None
            rows.append(
                {
                    "project_id": project_id,
                    "approved_by": approval.get("approved_by"),
                    "approved_at": approval.get("approved_at"),
                    "decided_on": approval.get("decided_on"),
                    "decision": approval.get("decision"),
                    "basis": approval.get("basis"),
                    "handoff": approval.get("handoff"),
                    "acceptance": approval.get("acceptance"),
                    "current": diff,
                }
            )
        return {"approvals": rows, "by_project": self._approval_rows(result), "log": self.log}


def _attr_for(key: str) -> str:
    return {
        "buildings": "buildings",
        "energy": "energy",
        "comfort": "comfort",
        "targets": "targets",
        "projects": "projects",
        "measurements": "measurements",
        "events": "events",
    }[key]


def _record_to_row(item: Any) -> dict[str, Any]:
    """Serialise a loaded record back to a CSV-like row for storage."""

    data = asdict(item)
    if "start" in data and "end" in data and "period" in data:
        data["note"] = data.get("note", "")
    if "valid_from" in data:
        data["valid_from"] = _iso(data["valid_from"])
    for key in (
        "valid_to",
        "effective_date",
        "measure_start",
        "measure_end",
        "commissioning_start",
        "commissioning_end",
        "planned_closure_start",
        "planned_closure_end",
        "approved_on",
        "accepted_on",
        "verification_start",
        "verification_end",
        "date",
        "start",
        "end",
    ):
        if key in data and data[key] is not None:
            data[key] = _iso(data[key])
        elif key in data:
            data[key] = ""
    if "period" in data and "start" in data and "end" in data:
        # EnergyPeriod start/end are derived; keep the original label only.
        data.pop("start", None)
        data.pop("end", None)
    row = {key: "" if value is None else str(value) for key, value in data.items()}
    row.pop("source_file", None)
    if "verified" in row:
        row["verified"] = "true" if row["verified"].lower() == "true" else "false"
    return row


def _row_key(kind: str, row: dict[str, Any]) -> str:
    if kind == "buildings":
        return f"{row.get('building_id')}|{row.get('valid_from')}"
    if kind == "energy":
        return f"{row.get('building_id')}|{row.get('period')}"
    if kind == "comfort":
        return str(row.get("record_id"))
    if kind == "targets":
        return f"{row.get('use_type')}|{row.get('valid_from')}"
    if kind == "projects":
        return str(row.get("project_id"))
    if kind == "measurements":
        return str(row.get("measurement_id"))
    if kind == "events":
        return str(row.get("event_id"))
    return json.dumps(row, sort_keys=True, ensure_ascii=False)
