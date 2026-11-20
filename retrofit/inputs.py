"""Input loading for the public-building retrofit review application.

The app is deliberately strict about *time*: every record is turned into an
interval and then compared against the comparison windows built in
:mod:`retrofit.analysis`.  Rows that cannot be placed in time are kept as data
quality issues instead of being silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
import csv
import json
import re
from typing import Any, Iterable


class DataError(ValueError):
    """Raised when an input file is structurally unusable."""


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("empty date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    # also accept 2026/03/01
    normalised = text.replace("/", "-")
    return date.fromisoformat(normalised)


def parse_optional_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return parse_date(text)


def parse_number(value: Any, *, field_name: str = "value") -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be numeric, got {text!r}") from exc


def _parse_int(value: Any) -> int | None:
    number = parse_number(value)
    if number is None:
        return None
    if number != int(number):
        raise ValueError(f"expected whole number, got {value!r}")
    return int(number)


def parse_period(text: str) -> tuple[date, date, str]:
    """Parse a reporting period such as ``2026-Q1`` or ``2026-03``.

    Returns ``(start, end_exclusive, label)``.  Periods are fixed reporting
    buckets, not retrofit comparison windows; the analysis app builds the
    latter from the project stage dates.
    """

    text = str(text).strip()
    match = re.fullmatch(r"(\d{4})-?Q([1-4])", text, flags=re.IGNORECASE)
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        start_month = 3 * (quarter - 1) + 1
        start = date(year, start_month, 1)
        end = date(year + (start_month + 2) // 12, (start_month + 2) % 12 + 1, 1)
        return start, end, f"{year}-Q{quarter}"
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        start = date(year, month, 1)
        end = date(year + (month // 12), month % 12 + 1, 1)
        return start, end, f"{year}-{month:02d}"
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})/(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        end = date(int(match.group(4)), int(match.group(5)), int(match.group(6))) + timedelta(days=1)
        return start, end, f"{start.isoformat()}..{end.isoformat()}"
    raise ValueError(f"unrecognised period {text!r} (use 2026-Q1, 2026-03 or 2026-01-01/2026-03-31)")


def days_between(start: date, end: date) -> int:
    return max((end - start).days, 0)


def overlap_days(a_start: date, a_end: date, b_start: date, b_end: date) -> int:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max((end - start).days, 0)


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsePlan:
    building_id: str
    use_type: str
    open_days: int
    open_hours_per_day: float
    valid_from: date
    valid_to: date | None = None

    def weekday_open_hours(self) -> float:
        return self.open_days * self.open_hours_per_day


@dataclass(frozen=True)
class EnergyPeriod:
    building_id: str
    period: str
    start: date
    end: date  # exclusive
    electric_kwh: float | None
    gas_m3: float | None
    degree_days: float | None
    coverage_pct: float | None
    meter_id: str | None = None
    note: str = ""
    source_file: str = ""


@dataclass(frozen=True)
class ComfortRecord:
    record_id: str
    building_id: str
    date: date
    comfort_sample_pct: float | None
    complaint_count: int | None
    project_id: str | None
    stage: str | None
    effective_date: date | None
    sample_count: int | None = None
    sample_coverage_pct: float | None = None
    source_file: str = ""


@dataclass(frozen=True)
class ServiceTarget:
    use_type: str
    min_open_days: int
    min_open_hours_per_day: float
    min_comfort_sample_pct: float
    valid_from: date


@dataclass(frozen=True)
class SiteEvent:
    """A site change that can confound a before/after energy comparison.

    Examples: meter replacement, tenant/occupancy change, unrelated equipment
    commissioning.  ``verified`` means a reviewer has confirmed the readings
    are reconciled (or the event is otherwise accounted for).
    """

    event_id: str
    building_id: str
    event_type: str
    start: date
    end: date
    verified: bool = False
    note: str = ""


@dataclass(frozen=True)
class Project:
    project_id: str
    building_id: str
    name: str
    measures: str
    measure_start: date
    measure_end: date
    commissioning_start: date | None
    commissioning_end: date | None
    planned_closure_start: date | None
    planned_closure_end: date | None
    predicted_saving_pct: float | None
    cost: float | None
    status: str = "proposed"
    approved_by: str | None = None
    approved_on: date | None = None
    accepted_on: date | None = None
    priority_note: str = ""
    verification_start: date | None = None
    verification_end: date | None = None


@dataclass(frozen=True)
class Measurement:
    """Post-completion verification measurement (measured data).

    ``comfort_sample_pct`` is the share of surveyed occupants satisfied, and
    ``sample_count`` is how many occupants were actually surveyed.
    """

    measurement_id: str
    project_id: str
    date: date
    electric_kwh: float | None
    gas_m3: float | None
    degree_days: float | None
    comfort_sample_pct: float | None
    sample_count: int | None = None
    note: str = ""


@dataclass
class InputIssue:
    severity: str  # "error" | "warning"
    dataset: str
    row: str
    message: str


@dataclass
class Dataset:
    buildings: list[UsePlan] = field(default_factory=list)
    energy: list[EnergyPeriod] = field(default_factory=list)
    comfort: list[ComfortRecord] = field(default_factory=list)
    targets: list[ServiceTarget] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    measurements: list[Measurement] = field(default_factory=list)
    events: list[SiteEvent] = field(default_factory=list)
    issues: list[InputIssue] = field(default_factory=list)

    # ---- lookup helpers -------------------------------------------------

    def plans_for(self, building_id: str) -> list[UsePlan]:
        return sorted(
            (p for p in self.buildings if p.building_id == building_id),
            key=lambda p: p.valid_from,
        )

    def plan_on(self, building_id: str, day: date) -> UsePlan | None:
        for plan in self.plans_for(building_id):
            if plan.valid_from <= day and (plan.valid_to is None or day <= plan.valid_to):
                return plan
        return None

    def target_for(self, use_type: str, day: date) -> ServiceTarget | None:
        candidates = [t for t in self.targets if t.use_type == use_type and t.valid_from <= day]
        return max(candidates, key=lambda t: t.valid_from) if candidates else None

    def building_ids(self) -> list[str]:
        ids = {p.building_id for p in self.buildings}
        ids |= {e.building_id for e in self.energy}
        ids |= {c.building_id for c in self.comfort}
        ids |= {p.building_id for p in self.projects}
        return sorted(ids)

    def projects_for(self, building_id: str) -> list[Project]:
        return sorted(
            (p for p in self.projects if p.building_id == building_id),
            key=lambda p: (p.measure_start, p.project_id),
        )

    def comfort_for(self, building_id: str, start: date | None = None, end: date | None = None) -> list[ComfortRecord]:
        rows = [c for c in self.comfort if c.building_id == building_id]
        if start is not None:
            rows = [c for c in rows if c.date >= start]
        if end is not None:
            rows = [c for c in rows if c.date < end]
        return sorted(rows, key=lambda c: c.date)

    def energy_overlapping(self, building_id: str, start: date, end: date) -> list[EnergyPeriod]:
        rows = [
            e
            for e in self.energy
            if e.building_id == building_id and overlap_days(e.start, e.end, start, end) > 0
        ]
        return sorted(rows, key=lambda e: e.start)

    def measurements_for(self, project_id: str) -> list[Measurement]:
        return sorted((m for m in self.measurements if m.project_id == project_id), key=lambda m: m.date)


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

DATASET_ALIASES = {
    "buildings": "buildings",
    "energy": "energy",
    "energy_weather": "energy",
    "comfort": "comfort",
    "comfort_projects": "comfort",
    "targets": "targets",
    "service_targets": "targets",
    "projects": "projects",
    "retrofit_projects": "projects",
    "measurements": "measurements",
    "commissioning_measurements": "measurements",
    "site_events": "events",
    "events": "events",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise DataError(f"{path.name}: missing header row")
        rows = []
        for raw in reader:
            if None in raw:
                raise DataError(f"{path.name}: row {reader.line_num} has extra columns")
            rows.append({(k or "").strip(): (v or "").strip() for k, v in raw.items()})
        return rows


def _require(row: dict[str, str], *names: str) -> None:
    missing = [n for n in names if n not in row]
    if missing:
        raise DataError(f"missing required column(s): {', '.join(missing)}")


def load_csv_dir(directory: str | Path) -> Dataset:
    """Load every recognised CSV in *directory* into one :class:`Dataset`."""

    directory = Path(directory)
    if not directory.is_dir():
        raise DataError(f"not a directory: {directory}")
    dataset = Dataset()
    seen: set[str] = set()
    for path in sorted(directory.glob("*.csv")):
        if path.name == "fixture_manifest.csv":
            continue
        kind = DATASET_ALIASES.get(path.name[:-4].lower())
        if kind is None:
            dataset.issues.append(
                InputIssue("warning", path.name, "-", "unrecognised CSV file, ignored")
            )
            continue
        if kind in seen:
            dataset.issues.append(
                InputIssue("warning", path.name, "-", f"duplicate {kind} dataset, ignored")
            )
            continue
        seen.add(kind)
        try:
            rows = _read_csv(path)
            _load_rows(dataset, kind, rows, path.name)
        except DataError as exc:
            dataset.issues.append(InputIssue("error", path.name, "-", str(exc)))
        except ValueError as exc:
            dataset.issues.append(InputIssue("error", path.name, "-", str(exc)))
    _validate(dataset)
    return dataset


def _load_rows(dataset: Dataset, kind: str, rows: list[dict[str, str]], source: str) -> None:
    if kind == "buildings":
        _require(rows[0] if rows else {}, "building_id", "use_type", "open_days", "open_hours_per_day", "valid_from")
        for index, row in enumerate(rows, start=2):
            try:
                plan = UsePlan(
                    building_id=row["building_id"],
                    use_type=row["use_type"],
                    open_days=int(float(row["open_days"])),
                    open_hours_per_day=float(row["open_hours_per_day"]),
                    valid_from=parse_date(row["valid_from"]),
                    valid_to=parse_optional_date(row.get("valid_to")),
                )
                if plan.open_days < 0 or plan.open_days > 7:
                    raise ValueError("open_days must be between 0 and 7")
                if plan.open_hours_per_day < 0:
                    raise ValueError("open_hours_per_day must be >= 0")
                dataset.buildings.append(plan)
            except (ValueError, KeyError) as exc:
                dataset.issues.append(InputIssue("error", source, str(index), str(exc)))
    elif kind == "energy":
        _require(rows[0] if rows else {}, "building_id", "period", "electric_kwh", "gas_m3", "degree_days")
        for index, row in enumerate(rows, start=2):
            try:
                start, end, label = parse_period(row["period"])
                dataset.energy.append(
                    EnergyPeriod(
                        building_id=row["building_id"],
                        period=label,
                        start=start,
                        end=end,
                        electric_kwh=parse_number(row.get("electric_kwh"), field_name="electric_kwh"),
                        gas_m3=parse_number(row.get("gas_m3"), field_name="gas_m3"),
                        degree_days=parse_number(row.get("degree_days"), field_name="degree_days"),
                        coverage_pct=parse_number(row.get("coverage_pct"), field_name="coverage_pct"),
                        meter_id=row.get("meter_id") or None,
                        note=row.get("note") or "",
                        source_file=source,
                    )
                )
            except (ValueError, KeyError) as exc:
                dataset.issues.append(InputIssue("error", source, str(index), str(exc)))
    elif kind == "comfort":
        _require(rows[0] if rows else {}, "record_id", "building_id", "date", "comfort_sample_pct")
        for index, row in enumerate(rows, start=2):
            try:
                complaints = row.get("complaint_count")
                dataset.comfort.append(
                    ComfortRecord(
                        record_id=row["record_id"],
                        building_id=row["building_id"],
                        date=parse_date(row["date"]),
                        comfort_sample_pct=parse_number(row.get("comfort_sample_pct"), field_name="comfort_sample_pct"),
                        complaint_count=int(float(complaints)) if complaints else None,
                        project_id=row.get("project_id") or None,
                        stage=row.get("stage") or None,
                        effective_date=parse_optional_date(row.get("effective_date")),
                        sample_count=_parse_int(row.get("sample_count")),
                        sample_coverage_pct=parse_number(
                            row.get("sample_coverage_pct"), field_name="sample_coverage_pct"
                        ),
                        source_file=source,
                    )
                )
            except (ValueError, KeyError) as exc:
                dataset.issues.append(InputIssue("error", source, str(index), str(exc)))
    elif kind == "targets":
        _require(rows[0] if rows else {}, "use_type", "min_open_days", "min_open_hours_per_day", "min_comfort_sample_pct")
        for index, row in enumerate(rows, start=2):
            try:
                dataset.targets.append(
                    ServiceTarget(
                        use_type=row["use_type"],
                        min_open_days=int(float(row["min_open_days"])),
                        min_open_hours_per_day=float(row["min_open_hours_per_day"]),
                        min_comfort_sample_pct=float(row["min_comfort_sample_pct"]),
                        valid_from=parse_date(row.get("valid_from") or "2025-01-01"),
                    )
                )
            except (ValueError, KeyError) as exc:
                dataset.issues.append(InputIssue("error", source, str(index), str(exc)))
    elif kind == "projects":
        _require(rows[0] if rows else {}, "project_id", "building_id", "measure_start", "measure_end")
        for index, row in enumerate(rows, start=2):
            try:
                project = Project(
                    project_id=row["project_id"],
                    building_id=row["building_id"],
                    name=row.get("name") or row["project_id"],
                    measures=row.get("measures") or "",
                    measure_start=parse_date(row["measure_start"]),
                    measure_end=parse_date(row["measure_end"]),
                    commissioning_start=parse_optional_date(row.get("commissioning_start")),
                    commissioning_end=parse_optional_date(row.get("commissioning_end")),
                    planned_closure_start=parse_optional_date(row.get("planned_closure_start")),
                    planned_closure_end=parse_optional_date(row.get("planned_closure_end")),
                    predicted_saving_pct=parse_number(row.get("predicted_saving_pct"), field_name="predicted_saving_pct"),
                    cost=parse_number(row.get("cost"), field_name="cost"),
                    status=(row.get("status") or "proposed").strip() or "proposed",
                    approved_by=row.get("approved_by") or None,
                    approved_on=parse_optional_date(row.get("approved_on")),
                    accepted_on=parse_optional_date(row.get("accepted_on")),
                    priority_note=row.get("priority_note") or "",
                    verification_start=parse_optional_date(row.get("verification_start")),
                    verification_end=parse_optional_date(row.get("verification_end")),
                )
                if project.measure_end <= project.measure_start:
                    raise ValueError("measure_end must be after measure_start")
                if (
                    project.commissioning_start
                    and project.commissioning_end
                    and project.commissioning_end < project.commissioning_start
                ):
                    raise ValueError("commissioning_end before commissioning_start")
                dataset.projects.append(project)
            except (ValueError, KeyError) as exc:
                dataset.issues.append(InputIssue("error", source, str(index), str(exc)))
    elif kind == "measurements":
        _require(rows[0] if rows else {}, "measurement_id", "project_id", "date")
        for index, row in enumerate(rows, start=2):
            try:
                dataset.measurements.append(
                    Measurement(
                        measurement_id=row["measurement_id"],
                        project_id=row["project_id"],
                        date=parse_date(row["date"]),
                        electric_kwh=parse_number(row.get("electric_kwh"), field_name="electric_kwh"),
                        gas_m3=parse_number(row.get("gas_m3"), field_name="gas_m3"),
                        degree_days=parse_number(row.get("degree_days"), field_name="degree_days"),
                        comfort_sample_pct=parse_number(
                            row.get("comfort_sample_pct"), field_name="comfort_sample_pct"
                        ),
                        sample_count=_parse_int(row.get("sample_count")),
                        note=row.get("note") or "",
                    )
                )
            except (ValueError, KeyError) as exc:
                dataset.issues.append(InputIssue("error", source, str(index), str(exc)))
    elif kind == "events":
        _require(rows[0] if rows else {}, "event_id", "building_id", "event_type", "start")
        for index, row in enumerate(rows, start=2):
            try:
                start = parse_date(row["start"])
                dataset.events.append(
                    SiteEvent(
                        event_id=row["event_id"],
                        building_id=row["building_id"],
                        event_type=row["event_type"].strip().lower(),
                        start=start,
                        end=parse_optional_date(row.get("end")) or start,
                        verified=(row.get("verified") or "").strip().lower() in {"1", "true", "yes", "y", "是"},
                        note=row.get("note") or "",
                    )
                )
            except (ValueError, KeyError) as exc:
                dataset.issues.append(InputIssue("error", source, str(index), str(exc)))


def _validate(dataset: Dataset) -> None:
    # duplicate ids / overlapping use plans
    seen_energy: set[tuple[str, str]] = set()
    for entry in dataset.energy:
        key = (entry.building_id, entry.period)
        if key in seen_energy:
            dataset.issues.append(
                InputIssue("error", entry.source_file, entry.period, f"duplicate energy period for {entry.building_id}")
            )
        seen_energy.add(key)

    by_building: dict[str, list[UsePlan]] = {}
    for plan in dataset.buildings:
        by_building.setdefault(plan.building_id, []).append(plan)
    for building_id, plans in by_building.items():
        plans.sort(key=lambda p: p.valid_from)
        for previous, current in zip(plans, plans[1:]):
            if previous.valid_to is None or previous.valid_to >= current.valid_from:
                dataset.issues.append(
                    InputIssue(
                        "error",
                        "buildings",
                        building_id,
                        f"overlapping or open-ended use plan contradicts plan starting {current.valid_from.isoformat()}",
                    )
                )

    project_ids = {p.project_id for p in dataset.projects}
    for record in dataset.comfort:
        if record.project_id and record.project_id not in project_ids:
            dataset.issues.append(
                InputIssue(
                    "warning",
                    record.source_file,
                    record.record_id,
                    f"comfort record references unknown project {record.project_id}",
                )
            )
    for measurement in dataset.measurements:
        if measurement.project_id not in project_ids:
            dataset.issues.append(
                InputIssue(
                    "error",
                    "measurements",
                    measurement.measurement_id,
                    f"unknown project {measurement.project_id}",
                )
            )

    for record in dataset.comfort:
        if record.comfort_sample_pct is not None and not 0 <= record.comfort_sample_pct <= 100:
            dataset.issues.append(
                InputIssue(
                    "error",
                    record.source_file,
                    record.record_id,
                    "comfort_sample_pct must be between 0 and 100",
                )
            )
        if record.sample_count is not None and record.sample_count < 0:
            dataset.issues.append(
                InputIssue("error", record.source_file, record.record_id, "sample_count must be >= 0")
            )
        if record.sample_coverage_pct is not None and not 0 <= record.sample_coverage_pct <= 100:
            dataset.issues.append(
                InputIssue(
                    "error",
                    record.source_file,
                    record.record_id,
                    "sample_coverage_pct must be between 0 and 100",
                )
            )
    for entry in dataset.energy:
        if entry.coverage_pct is not None and not 0 <= entry.coverage_pct <= 100:
            dataset.issues.append(
                InputIssue("error", entry.source_file, entry.period, "coverage_pct must be between 0 and 100")
            )


# ---------------------------------------------------------------------------
# JSON records (user-entered state)
# ---------------------------------------------------------------------------

STATE_VERSION = 1


def dataset_from_records(records: dict[str, list[dict[str, Any]]]) -> Dataset:
    dataset = Dataset()
    for kind, rows in records.items():
        normalised = [
            {str(k): ("" if v is None else str(v)) for k, v in row.items()} for row in rows
        ]
        if normalised:
            _load_rows(dataset, kind, normalised, f"<{kind}>")
    _validate(dataset)
    return dataset


def load_json_dataset(files: Iterable[str | Path]) -> Dataset:
    """Load datasets from a JSON document.

    Accepted shapes::

        {"projects": [...], "comfort": [...], "energy": [...], ...}

    or a list of such documents.  This is the format written by the web UI's
    "export workspace" action, so a state file round-trips to an import file.
    """

    records: dict[str, list[dict[str, Any]]] = {
        "buildings": [],
        "energy": [],
        "comfort": [],
        "targets": [],
        "projects": [],
        "measurements": [],
        "events": [],
    }
    for file in files:
        path = Path(file)
        with path.open(encoding="utf-8") as stream:
            document = json.load(stream)
        documents = document if isinstance(document, list) else [document]
        for item in documents:
            if not isinstance(item, dict):
                raise DataError(f"{path.name}: expected JSON object or list of objects")
            found = False
            for key, target in (
                ("projects", "projects"),
                ("comfort", "comfort"),
                ("energy", "energy"),
                ("buildings", "buildings"),
                ("targets", "targets"),
                ("service_targets", "targets"),
                ("measurements", "measurements"),
                ("events", "events"),
                ("site_events", "events"),
            ):
                if key in item:
                    records[target].extend(item[key])
                    found = True
            if not found:
                raise DataError(f"{path.name}: no recognised dataset keys")
    return dataset_from_records(records)


def merge_datasets(*datasets: Dataset) -> Dataset:
    """Combine datasets, letting later datasets win on identical record ids."""

    merged = Dataset()
    issues: list[InputIssue] = []
    for dataset in datasets:
        issues.extend(dataset.issues)
        merged.buildings.extend(dataset.buildings)
        merged.targets.extend(dataset.targets)
        merged.energy.extend(dataset.energy)
        merged.projects.extend(dataset.projects)
        merged.measurements.extend(dataset.measurements)
        merged.events.extend(dataset.events)
        merged.comfort.extend(dataset.comfort)

    def dedupe(items: list, key, label: str) -> list:
        seen: dict[Any, Any] = {}
        for item in items:
            item_key = key(item)
            if item_key in seen:
                issues.append(
                    InputIssue(
                        "warning",
                        label,
                        str(item_key),
                        f"duplicate {label} record; keeping the last occurrence",
                    )
                )
            seen[item_key] = item
        return list(seen.values())

    merged.buildings = dedupe(
        merged.buildings, lambda p: (p.building_id, p.valid_from), "buildings"
    )
    merged.targets = dedupe(
        merged.targets, lambda t: (t.use_type, t.valid_from), "service_targets"
    )
    merged.energy = dedupe(
        merged.energy, lambda e: (e.building_id, e.period), "energy_weather"
    )
    merged.projects = dedupe(merged.projects, lambda p: p.project_id, "projects")
    merged.measurements = dedupe(
        merged.measurements, lambda m: m.measurement_id, "measurements"
    )
    merged.comfort = dedupe(merged.comfort, lambda c: c.record_id, "comfort_projects")
    merged.events = dedupe(merged.events, lambda e: e.event_id, "site_events")
    merged.issues = issues
    _validate(merged)
    return merged
