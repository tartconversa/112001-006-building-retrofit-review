"""Comparison, service-evidence and prioritisation logic.

Design invariants (these are the rules the application exists to enforce):

* The retrofit works window and the equipment commissioning window are never
  merged into one comparison period.  The post period begins the day after
  commissioning ends.
* A change of building use type makes an energy comparison incomparable.
  Open-hours changes are normalised and flagged, never credited as efficiency.
* Meter replacement, tenant change and other commissioning events overlapping
  a comparison window are confounders.  They are surfaced and, unless a
  reviewer has confirmed the readings are reconciled, downgrade the project to
  "needs measurement".
* Comfort evidence is gated: a project cannot be ranked as a success when
  indoor comfort is below target, however large the energy saving.  When
  sampling is thin the conclusion is a range, not a point estimate.
* Every decision carries machine-readable reason codes plus a human-readable
  explanation, so an asset manager can justify priority, deferral or a request
  for more measurements.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from math import sqrt
from statistics import NormalDist
from typing import Any, Iterable

from .inputs import (
    ComfortRecord,
    Dataset,
    Measurement,
    Project,
    SiteEvent,
    UsePlan,
    days_between,
    overlap_days,
)


# ---------------------------------------------------------------------------
# configuration and result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisConfig:
    baseline_mode: str = "same_period_last_year"  # or "preceding"
    comparison_days: int = 90
    min_energy_coverage_pct: float = 95.0
    min_post_days: int = 60
    gas_kwh_per_m3: float = 9.9
    min_comfort_samples: int = 30
    min_comfort_coverage_pct: float = 15.0
    comfort_confidence: float = 0.90
    meaningful_saving_pct: float = 5.0
    schedule_tolerance_pct: float = 5.0
    weather_sensitivity_pct: float = 15.0
    max_closure_share_pct: float = 25.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AnalysisConfig":
        if not data:
            return cls()
        known = {field_name for field_name in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        config = cls(**clean)
        if config.baseline_mode not in {"same_period_last_year", "preceding"}:
            raise ValueError("baseline_mode must be same_period_last_year or preceding")
        return config

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class Window:
    start: date
    end: date  # exclusive
    label: str

    @property
    def days(self) -> int:
        return days_between(self.start, self.end)

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": (self.end - timedelta(days=1)).isoformat(), "days": self.days, "label": self.label}


@dataclass
class EnergyAggregate:
    electric_kwh: float = 0.0
    gas_m3: float = 0.0
    degree_days: float = 0.0
    covered_days: int = 0
    coverage_min: float | None = None
    periods: list[str] = field(default_factory=list)
    meter_ids: set[str] = field(default_factory=set)
    complete: bool = False
    has_data: bool = False
    missing_days: int = 0
    measured_days: int = 0

    @property
    def kwh_e(self) -> float:
        return self.electric_kwh

    def total(self, gas_factor: float) -> float:
        return self.electric_kwh + self.gas_m3 * gas_factor

    def as_dict(self, gas_factor: float) -> dict[str, Any]:
        return {
            "electric_kwh": round(self.electric_kwh, 1),
            "gas_m3": round(self.gas_m3, 1),
            "kwh_e": round(self.total(gas_factor), 1),
            "degree_days": round(self.degree_days, 1),
            "covered_days": self.covered_days,
            "coverage_min": self.coverage_min,
            "periods": self.periods,
            "meter_ids": sorted(self.meter_ids),
            "complete": self.complete,
            "has_data": self.has_data,
            "missing_days": self.missing_days,
            "measured_days": self.measured_days,
        }


@dataclass
class ComfortAssessment:
    post_pct: float | None = None
    baseline_pct: float | None = None
    interval_low: float | None = None
    interval_high: float | None = None
    sample_count: int | None = None
    success_count: float | None = None
    coverage_pct: float | None = None
    target: float | None = None
    target_use_type: str | None = None
    state: str = "no_data"  # pass | fail | unproven | no_data
    samples_sufficient: bool = False
    records: list[str] = field(default_factory=list)
    complaints: int = 0
    notes: list[str] = field(default_factory=list)
    baseline_state: str | None = None
    range_widened_for_coverage: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "post_pct": _round(self.post_pct),
            "baseline_pct": _round(self.baseline_pct),
            "baseline_state": self.baseline_state,
            "interval_low": _round(self.interval_low),
            "interval_high": _round(self.interval_high),
            "sample_count": self.sample_count,
            "coverage_pct": _round(self.coverage_pct),
            "target": _round(self.target),
            "target_use_type": self.target_use_type,
            "state": self.state,
            "samples_sufficient": self.samples_sufficient,
            "range_widened_for_coverage": self.range_widened_for_coverage,
            "records": self.records,
            "complaints": self.complaints,
            "notes": self.notes,
        }


@dataclass
class Confounder:
    code: str
    severity: str  # info | warning | blocking
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class ClosureImpact:
    days: int = 0
    open_days_lost: float = 0.0
    open_hours_lost: float = 0.0
    share_of_works_pct: float = 0.0
    conflicts_open_target: bool = False
    note: str = ""
    registerable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "open_days_lost": round(self.open_days_lost, 1),
            "open_hours_lost": round(self.open_hours_lost, 1),
            "share_of_works_pct": round(self.share_of_works_pct, 1),
            "conflicts_open_target": self.conflicts_open_target,
            "note": self.note,
            "registerable": self.registerable,
        }


@dataclass
class ProjectAssessment:
    project: Project
    phase: str
    decision: str = "not_started"
    decision_label: str = "未评估"
    reasons: list[str] = field(default_factory=list)
    explanation: str = ""
    baseline: Window | None = None
    post: Window | None = None
    works: Window | None = None
    commissioning: Window | None = None
    baseline_energy: EnergyAggregate | None = None
    post_energy: EnergyAggregate | None = None
    saving_kwh_e: float | None = None
    saving_pct: float | None = None
    raw_saving_pct: float | None = None
    weather_adjustment_pct: float | None = None
    weather_adjustment_method: str | None = None
    schedule_adjustment_pct: float | None = None
    annualised_saving_kwh_e: float | None = None
    comfort: ComfortAssessment | None = None
    confounders: list[Confounder] = field(default_factory=list)
    closure: ClosureImpact | None = None
    outstanding: list[str] = field(default_factory=list)
    evidence_score: float = 0.0
    rank_score: float | None = None
    cost_effectiveness: float | None = None  # cost per annual kWh-e saved
    is_completed: bool = False
    measurements: list[Measurement] = field(default_factory=list)
    precedent: dict[str, Any] | None = None
    baseline_kind: str | None = None

    def as_dict(self, config: AnalysisConfig) -> dict[str, Any]:
        return {
            "project_id": self.project.project_id,
            "building_id": self.project.building_id,
            "name": self.project.name,
            "measures": self.project.measures,
            "phase": self.phase,
            "decision": self.decision,
            "decision_label": self.decision_label,
            "reasons": self.reasons,
            "explanation": self.explanation,
            "works": self.works.as_dict() if self.works else None,
            "commissioning": self.commissioning.as_dict() if self.commissioning else None,
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "baseline_kind": self.baseline_kind,
            "post": self.post.as_dict() if self.post else None,
            "baseline_energy": self.baseline_energy.as_dict(config.gas_kwh_per_m3) if self.baseline_energy else None,
            "post_energy": self.post_energy.as_dict(config.gas_kwh_per_m3) if self.post_energy else None,
            "saving_kwh_e": _round(self.saving_kwh_e, 1),
            "saving_pct": _round(self.saving_pct),
            "raw_saving_pct": _round(self.raw_saving_pct),
            "weather_adjustment_pct": _round(self.weather_adjustment_pct),
            "weather_adjustment_method": self.weather_adjustment_method,
            "schedule_adjustment_pct": _round(self.schedule_adjustment_pct),
            "annualised_saving_kwh_e": _round(self.annualised_saving_kwh_e, 1),
            "comfort": self.comfort.as_dict() if self.comfort else None,
            "confounders": [c.as_dict() for c in self.confounders],
            "closure": self.closure.as_dict() if self.closure else None,
            "outstanding": self.outstanding,
            "evidence_score": round(self.evidence_score, 2),
            "rank_score": _round(self.rank_score, 2),
            "cost_effectiveness": _round(self.cost_effectiveness, 2),
            "is_completed": self.is_completed,
            "predicted_saving_pct": _round(self.project.predicted_saving_pct),
            "cost": _round(self.project.cost, 0),
            "status": self.project.status,
            "approval": {
                "approved_by": self.project.approved_by,
                "approved_on": self.project.approved_on.isoformat() if self.project.approved_on else None,
                "accepted_on": self.project.accepted_on.isoformat() if self.project.accepted_on else None,
            },
            "verification_measurements": [
                {
                    "measurement_id": m.measurement_id,
                    "date": m.date.isoformat(),
                    "electric_kwh": m.electric_kwh,
                    "gas_m3": m.gas_m3,
                    "degree_days": m.degree_days,
                    "comfort_sample_pct": m.comfort_sample_pct,
                    "sample_count": m.sample_count,
                    "note": m.note,
                }
                for m in self.measurements
            ],
            "precedent": self.precedent,
        }


@dataclass
class AnalysisResult:
    as_of: date
    config: AnalysisConfig
    assessments: list[ProjectAssessment]
    completed_ranking: list[ProjectAssessment]
    candidate_ranking: list[ProjectAssessment]
    data_issues: list[dict[str, str]]
    summary: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def by_project(self) -> dict[str, ProjectAssessment]:
        return {a.project.project_id: a for a in self.assessments}

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "config": self.config.as_dict(),
            "summary": self.summary,
            "data_issues": self.data_issues,
            "warnings": self.warnings,
            "projects": [a.as_dict(self.config) for a in self.assessments],
            "completed_ranking": [a.project.project_id for a in self.completed_ranking],
            "candidate_ranking": [a.project.project_id for a in self.candidate_ranking],
        }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _pct_change(baseline: float, post: float) -> float | None:
    if baseline <= 0:
        return None
    return (baseline - post) / baseline * 100.0


def _midpoint(window: Window) -> date:
    return window.start + timedelta(days=max(window.days // 2, 0))


def _window(start: date, end: date, label: str) -> Window:
    return Window(start=start, end=end, label=label)


def _plan_at(dataset: Dataset, building_id: str, day: date) -> UsePlan | None:
    return dataset.plan_on(building_id, day)


def _weekly_open_hours(plan: UsePlan | None) -> float | None:
    if plan is None:
        return None
    return plan.open_days * plan.open_hours_per_day


def wilson_interval(successes: float, n: int, confidence: float) -> tuple[float, float]:
    """Wilson score interval for a proportion, in percent."""

    if n <= 0:
        return 0.0, 100.0
    confidence = min(max(confidence, 0.5), 0.999)
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p = min(max(successes / n, 0.0), 1.0)
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, (centre - margin) * 100), min(100.0, (centre + margin) * 100)


def _covered_days(intervals: Iterable[tuple[date, date]], start: date, end: date) -> int:
    """Union of covered days within [start, end)."""

    clipped = []
    for a, b in intervals:
        a2, b2 = max(a, start), min(b, end)
        if b2 > a2:
            clipped.append((a2, b2))
    if not clipped:
        return 0
    clipped.sort()
    merged_start, merged_end = clipped[0]
    total = 0
    for a, b in clipped[1:]:
        if a <= merged_end:
            merged_end = max(merged_end, b)
        else:
            total += (merged_end - merged_start).days
            merged_start, merged_end = a, b
    total += (merged_end - merged_start).days
    return total


def aggregate_energy(
    dataset: Dataset,
    building_id: str,
    window: Window | None,
    config: AnalysisConfig,
    measurements: list[Measurement] | None = None,
) -> EnergyAggregate:
    aggregate = EnergyAggregate()
    if window is None:
        return aggregate
    rows = dataset.energy_overlapping(building_id, window.start, window.end)
    if not rows and not measurements:
        aggregate.missing_days = window.days
        return aggregate

    if rows:
        aggregate.has_data = True
        coverage_weighted = 0.0
        weight_total = 0.0
        for row in rows:
            period_days = max((row.end - row.start).days, 1)
            overlap = overlap_days(row.start, row.end, window.start, window.end)
            if overlap <= 0:
                continue
            share = overlap / period_days
            if row.electric_kwh is not None:
                aggregate.electric_kwh += row.electric_kwh * share
            if row.gas_m3 is not None:
                aggregate.gas_m3 += row.gas_m3 * share
            if row.degree_days is not None:
                aggregate.degree_days += row.degree_days * share
            if row.meter_id:
                aggregate.meter_ids.add(row.meter_id)
            if row.coverage_pct is not None:
                # the weakest period dominates the evidence
                aggregate.coverage_min = (
                    row.coverage_pct
                    if aggregate.coverage_min is None
                    else min(aggregate.coverage_min, row.coverage_pct)
                )
                coverage_weighted += row.coverage_pct * overlap
                weight_total += overlap
            aggregate.periods.append(row.period)
        aggregate.covered_days = _covered_days(
            ((row.start, row.end) for row in rows), window.start, window.end
        )

    unmeasured = window.days - aggregate.covered_days
    if measurements and unmeasured > 0:
        # Verification measurements only fill days the utility data does not
        # cover; adding them on top of metered periods would double count.
        covered_intervals = [(row.start, row.end) for row in rows]
        direct = []
        for m in measurements:
            if not (window.start <= m.date < window.end):
                continue
            if m.electric_kwh is None and m.gas_m3 is None:
                continue
            if any(a <= m.date < b for a, b in covered_intervals):
                continue
            direct.append(m)
        for m in direct:
            aggregate.electric_kwh += m.electric_kwh or 0.0
            aggregate.gas_m3 += m.gas_m3 or 0.0
            aggregate.degree_days += m.degree_days or 0.0
            aggregate.periods.append(f"实测:{m.measurement_id}")
            aggregate.has_data = True
        if direct:
            aggregate.covered_days += unmeasured
            aggregate.measured_days = unmeasured

    aggregate.missing_days = max(window.days - aggregate.covered_days, 0)
    aggregate.complete = (
        aggregate.missing_days == 0
        and (aggregate.coverage_min is None or aggregate.coverage_min >= config.min_energy_coverage_pct)
    )
    if aggregate.coverage_min is None and weight_total:
        aggregate.coverage_min = coverage_weighted / weight_total
    return aggregate


def _annualise(saving: float, window_days: int) -> float:
    if window_days <= 0:
        return 0.0
    return saving * 365.0 / window_days


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------


def build_windows(
    dataset: Dataset, project: Project, config: AnalysisConfig, as_of: date
) -> dict[str, Window | None]:
    """Build works, commissioning, post and baseline windows.

    Works and commissioning are disjoint.  The post window starts after
    commissioning; the baseline is the same calendar window one year earlier
    when data allows, otherwise the immediately preceding window.  The
    returned ``baseline_kind`` records which one was used so the reviewer can
    see it.
    """

    works = _window(project.measure_start, project.measure_end + timedelta(days=1), "改造施工期")

    commissioning: Window | None = None
    if project.commissioning_start and project.commissioning_end:
        commissioning = _window(
            project.commissioning_start,
            project.commissioning_end + timedelta(days=1),
            "设备投运/调试期",
        )
        if commissioning.start < works.end:
            # Overlapping stages are a data problem: keep them separate by
            # trimming the commissioning window to start after the works end.
            commissioning = _window(
                max(commissioning.start, works.end),
                commissioning.end,
                "设备投运/调试期（施工记录重叠，已自施工结束日起算）",
            )
    post_start = commissioning.end if commissioning else works.end

    completed = _phase(project, as_of) == "completed"
    length = max(works.days, config.comparison_days)
    energy_ends = [e.end for e in dataset.energy if e.building_id == project.building_id]
    measurement_dates = [
        m.date + timedelta(days=1) for m in dataset.measurements if m.project_id == project.project_id
    ]
    available_end = min(max(energy_ends + measurement_dates + [as_of]), as_of)
    if completed:
        post_end = min(post_start + timedelta(days=length), available_end)
        if post_end <= post_start:
            post_end = post_start
        post: Window | None = _window(post_start, post_end, "改造后稳定运行期")
    else:
        post = None

    def overlaps_own_stages(candidate: Window) -> bool:
        if overlap_days(candidate.start, candidate.end, works.start, works.end) > 0:
            return True
        if commissioning and overlap_days(candidate.start, candidate.end, commissioning.start, commissioning.end) > 0:
            return True
        return False

    baseline: Window | None = None
    baseline_kind = "preceding"
    candidates: list[tuple[Window, str]] = []
    # A candidate measure has no stable post-operation period yet.  Its baseline
    # is the service/energy reference immediately before works start; using a
    # hypothetical "post" window would fabricate evidence.
    if _phase(project, as_of) == "completed":
        if config.baseline_mode == "same_period_last_year":
            try:
                start = post_start.replace(year=post_start.year - 1)
                end = post_end.replace(year=post_end.year - 1)
                candidates.append((_window(start, end, "上年同期基线"), "same_period"))
            except ValueError:
                pass
    preceding_end = min(works.start, available_end)
    preceding_start = preceding_end - timedelta(days=length)
    candidates.append((_window(preceding_start, preceding_end, "改造前基线"), "preceding"))

    usable: list[tuple[Window, str]] = []
    for candidate, kind in candidates:
        if overlaps_own_stages(candidate):
            continue
        aggregate = aggregate_energy(dataset, project.building_id, candidate, config)
        if aggregate.has_data and aggregate.missing_days == 0:
            usable.append((candidate, kind))
    if usable:
        # same-period is preferred when both are usable
        usable.sort(key=lambda item: 0 if item[1] == "same_period" else 1)
        baseline, baseline_kind = usable[0]
    else:
        baseline, baseline_kind = candidates[-1]

    return {
        "works": works,
        "commissioning": commissioning,
        "post": post,
        "baseline": baseline,
        "baseline_kind": baseline_kind,
    }


# ---------------------------------------------------------------------------
# confounders
# ---------------------------------------------------------------------------


def _events_for(dataset: Dataset, building_id: str) -> list[SiteEvent]:
    return sorted((e for e in dataset.events if e.building_id == building_id), key=lambda e: e.start)


def assess_confounders(
    dataset: Dataset,
    project: Project,
    windows: dict[str, Window | None],
    config: AnalysisConfig,
) -> list[Confounder]:
    confounders: list[Confounder] = []
    baseline, post = windows["baseline"], windows["post"]
    if baseline is None:
        return confounders

    events = _events_for(dataset, project.building_id)
    for event in events:
        kind = event.event_type
        in_baseline = overlap_days(event.start, event.end + timedelta(days=1), baseline.start, baseline.end) > 0
        in_post = (
            post is not None
            and overlap_days(event.start, event.end + timedelta(days=1), post.start, post.end) > 0
        )
        if not (in_baseline or in_post):
            continue
        where = "基线期" if in_baseline and not in_post else ("改造后期间" if in_post and not in_baseline else "基线期和改造后期间")
        if kind == "tenant_change":
            confounders.append(
                Confounder(
                    "tenant_change",
                    "blocking" if not event.verified else "info",
                    f"{where}发生租户/使用方变更（{event.start.isoformat()} 至 {event.end.isoformat()}）："
                    + ("已由复核人确认无影响。" if event.verified else "能耗与舒适度变化不能归因于改造，需补充核查。"),
                )
            )
        elif kind == "meter_replacement":
            confounders.append(
                Confounder(
                    "meter_replacement",
                    "warning" if event.verified else "blocking",
                    f"{where}发生电表更换（{event.start.isoformat()}）："
                    + ("已核对新旧表读数衔接。" if event.verified else "需核对新旧表读数衔接，表面能耗差不能直接判定为节能量。"),
                )
            )
        elif kind == "equipment_commissioning":
            confounders.append(
                Confounder(
                    "other_commissioning",
                    "warning",
                    f"{where}有其他设备投运调试（{event.start.isoformat()}）：与本项目投运期分开核算，必要时顺延比较起点。",
                )
            )
        elif kind == "use_change":
            confounders.append(
                Confounder(
                    "use_change_event",
                    "blocking",
                    f"{where}登记了用途/开放安排变化：不得作为设备效率提升。",
                )
            )
        else:
            confounders.append(
                Confounder("other_event", "warning", f"{where}有其他现场事件：{event.note or kind}")
            )
    return confounders


# ---------------------------------------------------------------------------
# comfort
# ---------------------------------------------------------------------------


def _comfort_records(
    dataset: Dataset,
    building_id: str,
    start: date,
    end: date,
    project_id: str | None = None,
    *,
    post_install: bool = True,
) -> list[ComfortRecord]:
    """Usable comfort survey rows in a window.

    Survey rows attached to the works/commissioning stage are excluded from a
    post-installation assessment: they describe the disruption, not steady
    operation.  In the baseline (``post_install=False``) such rows are exactly
    what describes the pre-works service level.
    """

    rows = dataset.comfort_for(building_id, start, end)
    usable = []
    for record in rows:
        stage = (record.stage or "").strip().lower()
        if post_install and stage in {"commissioning", "works", "construction", "planned"}:
            continue
        if project_id and record.project_id and record.project_id != project_id and stage != "accepted":
            continue
        usable.append(record)
    return usable


def assess_comfort(
    dataset: Dataset,
    project: Project,
    window: Window | None,
    config: AnalysisConfig,
    *,
    post_install: bool = True,
    measurements: list[Measurement] | None = None,
) -> ComfortAssessment:
    assessment = ComfortAssessment()
    if window is None or window.days <= 0:
        assessment.notes.append("没有可用的舒适度评价期间。")
        return assessment

    plan = _plan_at(dataset, project.building_id, _midpoint(window))
    if plan:
        assessment.target_use_type = plan.use_type
        target = dataset.target_for(plan.use_type, window.start)
        if target:
            assessment.target = target.min_comfort_sample_pct

    records = _comfort_records(
        dataset, project.building_id, window.start, window.end, project.project_id, post_install=post_install
    )
    survey_samples = [
        (r.record_id, r.comfort_sample_pct, r.sample_count, r.sample_coverage_pct)
        for r in records
        if r.comfort_sample_pct is not None
    ]
    if measurements:
        for m in measurements:
            if m.comfort_sample_pct is None or not (window.start <= m.date < window.end):
                continue
            survey_samples.append((m.measurement_id, m.comfort_sample_pct, m.sample_count, None))
    assessment.records = [r.record_id for r in records] + [
        m.measurement_id
        for m in (measurements or [])
        if m.comfort_sample_pct is not None and window.start <= m.date < window.end
    ]
    if survey_samples:
        successes = 0.0
        n = 0
        coverage_values = []
        for _, pct, sample_n, coverage in survey_samples:
            if sample_n is not None and sample_n > 0:
                successes += sample_n * pct / 100.0
                n += sample_n
                if coverage is not None:
                    coverage_values.append(coverage)
        if n > 0:
            assessment.sample_count = n
            assessment.success_count = successes
            assessment.post_pct = successes / n * 100.0
            low, high = wilson_interval(successes, n, config.comfort_confidence)
            assessment.interval_low, assessment.interval_high = low, high
            assessment.samples_sufficient = n >= config.min_comfort_samples
        else:
            # Point estimates without a sample size: report the observed range,
            # not a false point conclusion.
            values = [pct for _, pct, _, _ in survey_samples]
            assessment.post_pct = sum(values) / len(values)
            assessment.interval_low, assessment.interval_high = min(values), max(values)
            assessment.notes.append("抽测记录缺少样本量，结论仅能按记录区间展示。")
            assessment.samples_sufficient = False
        if coverage_values:
            # Coverage is the study's sampling intensity over the window; a
            # low value weakens the evidence, so it is reported and folded
            # into the interval rather than silently inflating certainty.
            assessment.coverage_pct = sum(coverage_values) / len(coverage_values)
            if assessment.coverage_pct < config.min_comfort_coverage_pct:
                assessment.notes.append(
                    f"抽测覆盖率 {assessment.coverage_pct:.1f}% 低于要求 {config.min_comfort_coverage_pct:.0f}%，"
                    "结论按较宽的区间展示。"
                )
                if assessment.interval_low is not None:
                    spread = max(assessment.interval_high - assessment.interval_low, 12.0) * 1.5
                    centre = (assessment.interval_high + assessment.interval_low) / 2
                    assessment.interval_low = max(0.0, centre - spread / 2)
                    assessment.interval_high = min(100.0, centre + spread / 2)
                    assessment.range_widened_for_coverage = True
        assessment.complaints = sum(r.complaint_count or 0 for r in records)
        if not assessment.samples_sufficient:
            assessment.notes.append(
                f"有效样本 {n if n > 0 else '未知'} 人，低于最低样本要求 {config.min_comfort_samples} 人，须给出区间而非单点结论。"
            )
    else:
        assessment.notes.append("评价期间内没有可用的舒适度抽测记录。")

    if assessment.post_pct is None or assessment.interval_low is None:
        assessment.state = "no_data"
    elif assessment.target is None:
        assessment.state = "unproven"
        assessment.notes.append("缺少该用途的服务目标，无法判定舒适度是否达标。")
    elif assessment.interval_high is not None and assessment.interval_high < assessment.target:
        assessment.state = "fail"
    elif assessment.interval_low >= assessment.target:
        assessment.state = "pass"
    else:
        assessment.state = "unproven"
        assessment.notes.append(
            f"满意率区间 {assessment.interval_low:.1f}%–{assessment.interval_high:.1f}% 跨越目标 {assessment.target:.0f}%，"
            "现有抽测不足以判定达标。"
        )
    if assessment.state in {"pass", "fail", "unproven"} and not assessment.samples_sufficient:
        if assessment.state == "pass":
            assessment.state = "unproven"
    return assessment


# ---------------------------------------------------------------------------
# closure impact
# ---------------------------------------------------------------------------


def assess_closure(
    dataset: Dataset, project: Project, config: AnalysisConfig
) -> ClosureImpact | None:
    if not (project.planned_closure_start and project.planned_closure_end):
        return None
    closure_days = days_between(project.planned_closure_start, project.planned_closure_end + timedelta(days=1))
    if closure_days <= 0:
        return None
    impact = ClosureImpact(days=closure_days)
    works_days = max(days_between(project.measure_start, project.measure_end + timedelta(days=1)), 1)
    impact.share_of_works_pct = closure_days / works_days * 100.0
    open_factor = 5 / 7
    plan = _plan_at(dataset, project.building_id, project.planned_closure_start)
    if plan:
        open_factor = plan.open_days / 7.0
        impact.open_days_lost = closure_days * open_factor
        impact.open_hours_lost = impact.open_days_lost * plan.open_hours_per_day
        target = dataset.target_for(plan.use_type, project.planned_closure_start)
        if target and plan.open_days < target.min_open_days:
            impact.conflicts_open_target = True
        if target and plan.open_hours_per_day < target.min_open_hours_per_day:
            impact.conflicts_open_target = True
    impact.registerable = impact.share_of_works_pct <= config.max_closure_share_pct
    notes = [
        f"预计停用 {closure_days} 天，损失约 {impact.open_days_lost:.0f} 个开放日、{impact.open_hours_lost:.0f} 开放小时。"
    ]
    if not impact.registerable:
        notes.append(
            f"停用时长占施工期 {impact.share_of_works_pct:.0f}%，超过 {config.max_closure_share_pct:.0f}% 上限，"
            "需调整施工组织或临时开放安排。"
        )
    else:
        notes.append("停用时段将从未来的比较期中剔除，改造后比较期自恢复开放且投运稳定后起算。")
    impact.note = " ".join(notes)
    return impact


# ---------------------------------------------------------------------------
# project assessment
# ---------------------------------------------------------------------------


def _phase(project: Project, as_of: date) -> str:
    if project.accepted_on and as_of >= project.accepted_on:
        return "completed"
    if project.commissioning_start and as_of >= project.commissioning_start:
        return "commissioning"
    if as_of >= project.measure_start:
        return "in_progress"
    if project.status in {"approved", "handed_off"}:
        return "approved"
    return "proposed"


def candidate_comfort_window(
    project: Project, config: AnalysisConfig, dataset: Dataset | None = None
) -> Window:
    """Most recent service evidence available before a measure starts."""

    length = max(config.comparison_days, 180)
    end = project.measure_start
    start = end - timedelta(days=length)
    if dataset is not None:
        # A use-type change makes surveys from the previous use incomparable;
        # start the service baseline at the change, not before it.
        plans = dataset.plans_for(project.building_id)
        current = dataset.plan_on(project.building_id, end)
        if current is not None:
            start = max(start, current.valid_from)
    return _window(start, end, "改造前服务基线（近期抽测）")


def assess_project(
    dataset: Dataset,
    project: Project,
    config: AnalysisConfig,
    as_of: date,
    precedents: dict[str, Any] | None = None,
) -> ProjectAssessment:
    windows = build_windows(dataset, project, config, as_of)
    phase = _phase(project, as_of)
    assessment = ProjectAssessment(
        project=project,
        phase=phase,
        works=windows["works"],
        commissioning=windows["commissioning"],
        post=windows["post"],
        baseline=windows["baseline"],
        is_completed=phase == "completed",
        measurements=dataset.measurements_for(project.project_id),
    )
    assessment.closure = assess_closure(dataset, project, config)
    if assessment.is_completed:
        assessment.comfort = assess_comfort(
            dataset,
            project,
            windows["post"],
            config,
            measurements=assessment.measurements,
        )
        if assessment.comfort and windows["baseline"] is not None:
            baseline_comfort = assess_comfort(
                dataset, project, windows["baseline"], config, post_install=False
            )
            assessment.comfort.baseline_pct = baseline_comfort.post_pct
            assessment.comfort.baseline_state = baseline_comfort.state
    else:
        assessment.comfort = assess_comfort(
            dataset,
            project,
            candidate_comfort_window(project, config, dataset),
            config,
            post_install=False,
        )
    assessment.baseline_energy = aggregate_energy(dataset, project.building_id, windows["baseline"], config)
    assessment.post_energy = aggregate_energy(
        dataset,
        project.building_id,
        windows["post"],
        config,
        measurements=assessment.measurements if assessment.is_completed else None,
    )
    assessment.baseline_kind = windows.get("baseline_kind")
    confounders = assess_confounders(dataset, project, windows, config)
    assessment.precedent = precedents.get(project.building_id) if precedents else None

    # ---- stage/energy checks -------------------------------------------
    if not assessment.is_completed:
        _assess_candidate(dataset, assessment, config, as_of)
        _finalise_common(dataset, assessment, config, confounders)
        assessment.outstanding = list_outstanding(dataset, project, assessment, config)
        return assessment

    # ---- use-type comparability ----------------------------------------
    baseline_plan = _plan_at(dataset, project.building_id, _midpoint(windows["baseline"]))
    post_plan = _plan_at(dataset, project.building_id, _midpoint(windows["post"])) if windows["post"].days > 0 else None
    use_change = bool(baseline_plan and post_plan and baseline_plan.use_type != post_plan.use_type)
    if baseline_plan and post_plan and not use_change:
        hours_before = _weekly_open_hours(baseline_plan)
        hours_after = _weekly_open_hours(post_plan)
        if hours_before and hours_after and hours_before > 0 and hours_after > 0:
            change = (hours_after - hours_before) / hours_before * 100.0
            if abs(change) > config.schedule_tolerance_pct:
                confounders.append(
                    Confounder(
                        "schedule_change",
                        "warning",
                        f"基线期周开放 {hours_before:.0f} 小时，改造后 {hours_after:.0f} 小时（{change:+.0f}%）。"
                        "已按开放小时折算后比较，开放时间变化不计为设备效率。",
                    )
                )
        if baseline_plan.open_days != post_plan.open_days:
            confounders.append(
                Confounder(
                    "open_days_change",
                    "warning",
                    f"每周开放天数由 {baseline_plan.open_days} 天变为 {post_plan.open_days} 天；能耗按开放小时折算，服务目标单独核对。",
                )
            )

    # ---- energy comparison ---------------------------------------------
    base_e = assessment.baseline_energy
    post_e = assessment.post_energy
    gas_factor = config.gas_kwh_per_m3
    if base_e.has_data and post_e.has_data and base_e.total(gas_factor) > 0 and post_e.total(gas_factor) > 0:
        raw_base = base_e.total(gas_factor)
        raw_post = post_e.total(gas_factor)
        assessment.raw_saving_pct = _pct_change(raw_base, raw_post)

        adjusted_post = raw_post
        # weather normalisation is only applied when degree days differ and
        # both windows have them; it is labelled as a sensitivity adjustment.
        if base_e.degree_days > 0 and post_e.degree_days > 0:
            dd_change = (post_e.degree_days - base_e.degree_days) / base_e.degree_days * 100.0
            if abs(dd_change) >= 5.0:
                adjusted_post *= base_e.degree_days / post_e.degree_days
                assessment.weather_adjustment_pct = (
                    (adjusted_post / raw_post * 100.0 - 100.0) if raw_post > 0 else 0.0
                )
                assessment.weather_adjustment_method = "degree_day_ratio"
                if abs(dd_change) >= config.weather_sensitivity_pct:
                    confounders.append(
                        Confounder(
                            "weather_difference",
                            "info",
                            f"比较期采暖度日相差 {dd_change:+.0f}%，已作气象修正（修正系数按度日比）；"
                            "建议改用上年同期基线复核。",
                        )
                    )
            else:
                assessment.weather_adjustment_method = "degree_days_comparable"
        else:
            assessment.weather_adjustment_method = "not_applied_no_degree_days"
            if abs((assessment.raw_saving_pct or 0)) >= config.meaningful_saving_pct:
                confounders.append(
                    Confounder(
                        "weather_not_normalised",
                        "warning",
                        "比较期缺少采暖度日，气象影响未修正；节能结论需谨慎，建议补充气象数据。",
                    )
                )
        if baseline_plan and post_plan and baseline_plan.use_type == post_plan.use_type:
            hours_before = _weekly_open_hours(baseline_plan)
            hours_after = _weekly_open_hours(post_plan)
            if hours_before and hours_after and hours_after > 0:
                ratio = hours_before / hours_after
                if abs(ratio - 1) > 0.0001:
                    adjusted_post *= ratio
                    assessment.schedule_adjustment_pct = ratio * 100.0 - 100.0

        saving = raw_base - adjusted_post
        assessment.saving_kwh_e = saving
        assessment.saving_pct = saving / raw_base * 100.0
        assessment.annualised_saving_kwh_e = _annualise(saving, windows["post"].days)
        if project.cost and assessment.annualised_saving_kwh_e and assessment.annualised_saving_kwh_e > 0:
            assessment.cost_effectiveness = project.cost / assessment.annualised_saving_kwh_e

    # ---- service and evidence gates ------------------------------------
    comfort = assessment.comfort
    service_fail = comfort is not None and comfort.state == "fail"
    service_unproven = comfort is None or comfort.state in {"unproven", "no_data"}
    use_changed = use_change
    blocking = [c for c in confounders if c.severity == "blocking"]
    data_gap = post_e.missing_days > 0 or base_e.missing_days > 0 or not base_e.has_data or not post_e.has_data
    short_post = windows["post"].days < config.min_post_days

    if use_changed:
        assessment.decision = "incomparable"
        assessment.reasons.append("use_type_change")
        assessment.explanation = (
            f"用途在比较期内由 {baseline_plan.use_type} 变更为 {post_plan.use_type}，"
            "能耗与舒适度基准不再可比，不得作为设备效率提升。请另设同用途基线和目标后复核。"
        )
        assessment.evidence_score = 0.0
    elif service_fail:
        assessment.decision = "service_fail"
        assessment.reasons.append("comfort_below_target")
        low = f"{comfort.interval_low:.0f}%" if comfort and comfort.interval_low is not None else "?"
        high = f"{comfort.interval_high:.0f}%" if comfort and comfort.interval_high is not None else "?"
        saving_text = (
            f"实测节能 {assessment.saving_pct:+.0f}%"
            if assessment.saving_pct is not None
            else "能耗资料尚未形成结论"
        )
        assessment.explanation = (
            f"改造后舒适度满意率区间 {low}–{high}，上界仍低于目标 {comfort.target:.0f}%。"
            f"{saving_text}，但服务未达标不能由节能抵消；不列入优先投资排序，先整改。"
        )
        assessment.evidence_score = 0.2
    elif blocking:
        assessment.decision = "needs_measurement"
        assessment.reasons.extend(c.code for c in blocking)
        assessment.explanation = (
            "存在未消除的混杂因素："
            + "；".join(c.message for c in blocking)
            + " 需补充核对/实测后才能把能耗差计入节能量。"
        )
        assessment.evidence_score = 0.3
    elif data_gap or short_post:
        assessment.decision = "needs_measurement"
        if data_gap:
            assessment.reasons.append("energy_data_gap")
        if short_post:
            assessment.reasons.append("post_period_too_short")
        assessment.explanation = (
            f"能耗资料不完整（基线缺 {base_e.missing_days} 天、改造后缺 {post_e.missing_days} 天）或改造后稳定期不足 "
            f"{config.min_post_days} 天，暂不能形成可靠结论；需补录能耗或顺延比较期。"
        )
        assessment.evidence_score = 0.35
    elif service_unproven:
        assessment.decision = "needs_measurement"
        assessment.reasons.append("comfort_evidence_insufficient")
        detail = "；".join(comfort.notes) if comfort and comfort.notes else "缺少改造后舒适度抽测。"
        assessment.explanation = f"能耗结论已有初步结果，但舒适度证据不足：{detail} 需补测后再排序。"
        assessment.evidence_score = 0.45
    elif assessment.saving_pct is None:
        assessment.decision = "needs_measurement"
        assessment.reasons.append("no_energy_comparison")
        assessment.explanation = "缺少可配对的基线或改造后能耗，无法计算节能表现。"
        assessment.evidence_score = 0.2
    elif assessment.saving_pct < 0:
        assessment.decision = "defer"
        assessment.reasons.append("negative_saving")
        assessment.explanation = (
            f"按可比口径改造后能耗上升 {abs(assessment.saving_pct):.0f}%，未见节能效果，暂缓扩大投资并核查运行策略。"
        )
        assessment.evidence_score = 0.6
    elif assessment.saving_pct < config.meaningful_saving_pct:
        assessment.decision = "defer"
        assessment.reasons.append("saving_below_threshold")
        assessment.explanation = (
            f"实测节能 {assessment.saving_pct:.1f}%，低于 {config.meaningful_saving_pct:.0f}% 的实质节能门槛，"
            "投入产出不足以优先安排资金。"
        )
        assessment.evidence_score = 0.6
    else:
        assessment.decision = "priority"
        assessment.reasons.append("verified_saving")
        assessment.explanation = _priority_explanation(assessment, comfort)
        assessment.evidence_score = _evidence_score(assessment, comfort, config)

    _finalise_common(dataset, assessment, config, confounders)
    assessment.outstanding = list_outstanding(dataset, project, assessment, config)
    return assessment


def _priority_explanation(assessment: ProjectAssessment, comfort: ComfortAssessment | None) -> str:
    parts = [
        f"实测节能 {assessment.saving_pct:.1f}%（{assessment.saving_kwh_e:,.0f} kWh-e/比较期，"
        f"年化 {assessment.annualised_saving_kwh_e:,.0f} kWh-e）"
        if assessment.saving_kwh_e is not None and assessment.annualised_saving_kwh_e is not None
        else "实测节能成立"
    ]
    if assessment.weather_adjustment_pct is not None and abs(assessment.weather_adjustment_pct) >= 0.5:
        parts.append(f"已按度日修正气象差异 {assessment.weather_adjustment_pct:+.0f}%")
    if assessment.schedule_adjustment_pct is not None and abs(assessment.schedule_adjustment_pct) >= 0.5:
        parts.append(f"开放时间差异按小时折算 {assessment.schedule_adjustment_pct:+.0f}%")
    if comfort and comfort.state == "pass" and comfort.interval_low is not None:
        parts.append(
            f"舒适度抽测 {comfort.sample_count} 人、满意率下界 {comfort.interval_low:.0f}% ≥ 目标 {comfort.target:.0f}%"
        )
    parts.append("施工期与设备投运期已分开，比较期取投运稳定后")
    return "；".join(parts) + "。"


def _evidence_score(assessment: ProjectAssessment, comfort: ComfortAssessment | None, config: AnalysisConfig) -> float:
    score = 1.0
    if assessment.post_energy and assessment.post_energy.coverage_min is not None:
        if assessment.post_energy.coverage_min < 99:
            score -= 0.05
    if any(c.code == "schedule_change" for c in assessment.confounders):
        score -= 0.1
    if any(c.code == "weather_difference" for c in assessment.confounders):
        score -= 0.05
    if comfort and comfort.samples_sufficient and comfort.coverage_pct is not None and comfort.coverage_pct < 30:
        score -= 0.05
    if assessment.post and assessment.post.days < config.comparison_days:
        score -= 0.1
    return max(0.1, min(1.0, score))


def _assess_candidate(
    dataset: Dataset,
    assessment: ProjectAssessment,
    config: AnalysisConfig,
    as_of: date,
) -> None:
    """Assess a measure that has not yet been accepted (pipeline candidate)."""

    project = assessment.project
    comfort = assessment.comfort
    closure = assessment.closure
    baseline = assessment.baseline
    baseline_energy = assessment.baseline_energy
    plan = _plan_at(dataset, project.building_id, _midpoint(baseline)) if baseline else None
    target = dataset.target_for(plan.use_type, baseline.start) if plan and baseline else None

    expected_annual = None
    if baseline_energy and baseline_energy.has_data and baseline_energy.total(config.gas_kwh_per_m3) > 0:
        annual = baseline_energy.total(config.gas_kwh_per_m3) * 365.0 / max(baseline.days, 1)
        predicted = project.predicted_saving_pct
        if predicted is not None:
            expected_annual = annual * predicted / 100.0
    assessment.annualised_saving_kwh_e = expected_annual
    if project.cost and expected_annual and expected_annual > 0:
        assessment.cost_effectiveness = project.cost / expected_annual

    reasons: list[str] = ["pipeline_candidate"]
    explanation_bits: list[str] = []

    open_shortfall = False
    if plan and target:
        if plan.open_days < target.min_open_days or plan.open_hours_per_day < target.min_open_hours_per_day:
            open_shortfall = True
            reasons.append("open_service_below_target")
            explanation_bits.append(
                f"当前开放安排 {plan.open_days} 天/周 × {plan.open_hours_per_day:g} 小时，"
                f"低于该用途目标 {target.min_open_days} 天 × {target.min_open_hours_per_day:g} 小时；"
                "改造不得以压缩开放时间实现节能。"
            )

    # For a candidate the comfort window *is* the pre-works baseline, so a
    # failing state here is a failing service baseline.
    baseline_comfort_fail = comfort is not None and (
        comfort.state == "fail" or comfort.baseline_state == "fail"
    )

    if closure and not closure.registerable:
        assessment.decision = "defer"
        reasons.append("closure_conflict")
        explanation_bits.append(closure.note)
    elif baseline_comfort_fail:
        assessment.decision = "needs_measurement"
        reasons.append("baseline_comfort_below_target")
        target_text = f"{comfort.target:.0f}%" if comfort.target is not None else "目标"
        ok_share = (
            f"{comfort.interval_low:.0f}%–{comfort.interval_high:.0f}%"
            if comfort.interval_low is not None
            else "未达标"
        )
        explanation_bits.append(
            "改造前舒适度抽测已低于目标（"
            f"满意率区间 {ok_share} 对目标 {target_text}），"
            "改造方案必须先明确舒适度整改内容，不能仅以能耗下降立项。"
        )
        if not comfort.samples_sufficient:
            explanation_bits.append("现有抽样不足，需同步补足抽测。")
    elif comfort is None or comfort.state in {"unproven", "no_data"} or not comfort.samples_sufficient:
        assessment.decision = "needs_measurement"
        reasons.append("baseline_comfort_unproven")
        detail = "；".join(comfort.notes) if comfort and comfort.notes else "缺少可用的舒适度基线抽测。"
        explanation_bits.append(f"立项前舒适度证据不足：{detail} 需补测后确认服务基线。")
    elif project.predicted_saving_pct is None:
        assessment.decision = "needs_measurement"
        reasons.append("no_predicted_saving")
        explanation_bits.append("未登记预计节能率，无法安排资金优先级。")
    elif project.predicted_saving_pct < config.meaningful_saving_pct:
        assessment.decision = "defer"
        reasons.append("predicted_saving_below_threshold")
        explanation_bits.append(
            f"预计节能 {project.predicted_saving_pct:.1f}% 低于 {config.meaningful_saving_pct:.0f}% 门槛。"
        )
    else:
        assessment.decision = "conditional"
        reasons.append("awaiting_implementation")
        explanation_bits.append(
            f"已登记拟改造措施与预计停用时段，预计节能 {project.predicted_saving_pct:.1f}%；"
            "完工验收后须用实测能耗和舒适度抽测复核，未复核前不形成最终投资结论。"
        )
        if open_shortfall:
            assessment.decision = "defer"
            reasons.append("service_mitigation_required")
            explanation_bits.append("需先调整开放服务方案，保障开放时长不因改造下降。")

    if assessment.precedent:
        precedent = assessment.precedent
        if precedent.get("decision") == "priority":
            explanation_bits.append(
                f"同楼已完成项目 {precedent['project_id']} 实测节能 {precedent['saving_pct']:.0f}% 且服务达标，可作为本地业绩参照。"
            )
        elif precedent.get("decision") in {"service_fail", "incomparable"}:
            explanation_bits.append(
                f"同楼已完成项目 {precedent['project_id']} 的结论为“{precedent['decision_label']}”，"
                "本措施需先说明如何避免同类问题。"
            )

    assessment.decision_label = DECISION_LABELS[assessment.decision]
    assessment.reasons = reasons
    assessment.explanation = "".join(explanation_bits)
    assessment.rank_score = _candidate_score(assessment, config)


def _candidate_score(assessment: ProjectAssessment, config: AnalysisConfig) -> float | None:
    if assessment.decision not in {"conditional", "priority"}:
        return None
    saving = assessment.annualised_saving_kwh_e
    if not saving or saving <= 0:
        return None
    precedent = assessment.precedent or {}
    factor = 0.6
    if precedent.get("decision") == "priority":
        realised = precedent.get("saving_pct") or 0
        predicted = assessment.project.predicted_saving_pct or 0
        if predicted > 0:
            realisation = min(max(realised / predicted, 0.5), 1.2)
            factor = 0.5 + 0.5 * realisation
    elif precedent.get("decision") in {"needs_measurement", "defer"}:
        factor = 0.5
    score = saving * factor
    if assessment.project.cost:
        score = score / max(assessment.project.cost, 1.0) * 10_000.0
    return score


DECISION_LABELS = {
    "priority": "优先",
    "conditional": "有条件优先（待实测复核）",
    "needs_measurement": "需补测",
    "defer": "暂缓",
    "service_fail": "服务不达标",
    "incomparable": "不可比",
    "not_started": "未开工",
}


def list_outstanding(
    dataset: Dataset,
    project: Project,
    assessment: ProjectAssessment,
    config: AnalysisConfig,
) -> list[str]:
    items: list[str] = []
    phase = assessment.phase
    if phase in {"completed", "commissioning"} and not project.accepted_on:
        items.append("缺少竣工验收记录，需补录验收日期。")
    if phase == "completed" and not assessment.measurements:
        energy_complete = assessment.post_energy and assessment.post_energy.complete
        comfort_ok = assessment.comfort and assessment.comfort.samples_sufficient
        if not (energy_complete and comfort_ok):
            items.append("已完工但缺少实测复核资料，需补录投运后实测记录。")
    if any(c.code == "meter_replacement" and c.severity == "blocking" for c in assessment.confounders):
        items.append("电表更换读数尚未核对，需确认前后计量衔接。")
    if any(c.code == "tenant_change" and c.severity == "blocking" for c in assessment.confounders):
        items.append("租户变化影响尚未核实，需补充使用情况说明。")
    if phase in {"proposed", "approved"}:
        # A pipeline measure is scored on the reference period before works;
        # a missing post window is expected, not an outstanding defect.
        if project.commissioning_start is None or project.commissioning_end is None:
            items.append("未登记计划设备投运期，无法预先分离施工与投运阶段。")
        if not project.predicted_saving_pct:
            items.append("未登记预计节能率，无法排序。")
        if project.planned_closure_start is None:
            items.append("未登记计划停用时段，无法评估开放服务影响。")
        if assessment.closure and not assessment.closure.registerable:
            items.append("计划停用时段与开放服务目标冲突，需调整施工安排。")
        if assessment.comfort and not assessment.comfort.samples_sufficient:
            items.append(
                f"立项前舒适度抽测不足（{assessment.comfort.sample_count or 0} 人），需补测确认服务基线。"
            )
        return items
    if not assessment.baseline_energy or not assessment.baseline_energy.has_data:
        items.append("缺少改造前基线能耗。")
    elif assessment.baseline_energy.missing_days > 0:
        items.append(f"基线期能耗缺 {assessment.baseline_energy.missing_days} 天。")
    if not assessment.post_energy or not assessment.post_energy.has_data:
        if phase in {"completed", "commissioning"}:
            items.append("缺少改造后稳定运行期能耗。")
    elif assessment.post_energy.missing_days > 0:
        items.append(f"改造后比较期能耗缺 {assessment.post_energy.missing_days} 天。")
    if project.commissioning_start is None or project.commissioning_end is None:
        if phase in {"completed", "commissioning", "in_progress"}:
            items.append("未登记设备投运期，施工期与投运期无法分离。")
    comfort = assessment.comfort
    if phase in {"completed", "commissioning"}:
        if comfort is None or comfort.post_pct is None:
            items.append("缺少改造后舒适度抽测。")
        else:
            if not comfort.samples_sufficient:
                items.append(
                    f"舒适度抽测样本不足（{comfort.sample_count or 0} 人，要求 {config.min_comfort_samples} 人），需补测。"
                )
            if comfort.state == "unproven" and comfort.samples_sufficient:
                items.append("舒适度满意率区间跨越目标，需增加抽测以形成结论。")
    if any(c.code == "tenant_change" and c.severity == "blocking" for c in assessment.confounders):
        items.append("租户变化影响尚未核实，需补充使用情况说明。")
    if assessment.closure and not assessment.closure.registerable:
        items.append("计划停用时段与开放服务目标冲突，需调整施工安排。")
    if project.approved_on and project.status == "proposed":
        items.append("项目已批准但状态仍为拟改造，需移交设施团队跟进。")
    return items


def _finalise_common(
    dataset: Dataset,
    assessment: ProjectAssessment,
    config: AnalysisConfig,
    confounders: list[Confounder],
) -> None:
    existing = {c.code for c in confounders}
    for confounder in assessment.confounders:
        if confounder.code not in existing:
            confounders.append(confounder)
    assessment.confounders = confounders
    assessment.decision_label = DECISION_LABELS[assessment.decision]
    if assessment.is_completed and assessment.decision != "priority":
        # Rank score for completed projects: verified saving weighted by evidence.
        if assessment.annualised_saving_kwh_e is not None and assessment.decision in {"needs_measurement", "defer"}:
            assessment.rank_score = assessment.annualised_saving_kwh_e * assessment.evidence_score
    if assessment.is_completed and assessment.decision == "priority":
        score = (assessment.annualised_saving_kwh_e or 0) * assessment.evidence_score
        if assessment.project.cost:
            score = score / max(assessment.project.cost, 1.0) * 10_000.0
        assessment.rank_score = score


# ---------------------------------------------------------------------------
# analysis entry point
# ---------------------------------------------------------------------------


def analyse(dataset: Dataset, config: AnalysisConfig | None = None, as_of: date | None = None) -> AnalysisResult:
    config = config or AnalysisConfig()
    as_of = as_of or date.today()
    warnings: list[str] = []

    # Determine local precedent: best completed project per building, used to
    # temper expectations for pipeline candidates in the same building.
    precedent_by_building: dict[str, Any] = {}
    ordered_projects = sorted(dataset.projects, key=lambda p: (p.building_id, p.measure_start))
    assessments = [
        assess_project(dataset, project, config, as_of, precedents=None)
        for project in ordered_projects
    ]
    for assessment in assessments:
        if not assessment.is_completed:
            continue
        rank = {"priority": 0, "service_fail": 1, "incomparable": 2, "needs_measurement": 3, "defer": 4}.get(
            assessment.decision
        )
        if rank is None:
            continue
        current = precedent_by_building.get(assessment.project.building_id)
        candidate = {
            "project_id": assessment.project.project_id,
            "decision": assessment.decision,
            "decision_label": assessment.decision_label,
            "saving_pct": assessment.saving_pct or 0.0,
            "comfort_state": assessment.comfort.state if assessment.comfort else "no_data",
            "_rank": rank,
        }
        # A verified, service-passing precedent is the best reference; failing
        # precedents are kept only when nothing better exists in the building.
        if current is None or (rank, -(candidate["saving_pct"] or 0)) < (
            current["_rank"],
            -(current["saving_pct"] or 0),
        ):
            precedent_by_building[assessment.project.building_id] = candidate
    for precedent in precedent_by_building.values():
        precedent.pop("_rank", None)
    # Re-assess candidates with precedent available.
    assessments = [
        assess_project(dataset, project, config, as_of, precedents=precedent_by_building)
        for project in ordered_projects
    ]

    completed = [a for a in assessments if a.is_completed]
    candidates = [a for a in assessments if not a.is_completed]

    completed_rankable = [
        a for a in completed if a.decision in {"priority", "defer", "needs_measurement"}
    ]
    completed_rankable.sort(
        key=lambda a: (
            0 if a.decision == "priority" else (1 if a.decision == "needs_measurement" else 2),
            -(a.rank_score or 0),
        )
    )
    failed = [a for a in completed if a.decision in {"service_fail", "incomparable"}]

    candidate_rankable = [a for a in candidates if a.decision in {"conditional", "priority"}]
    candidate_rankable.sort(key=lambda a: -(a.rank_score or 0))
    candidate_held = [a for a in candidates if a.decision not in {"conditional", "priority"}]

    for index, assessment in enumerate(completed_rankable, start=1):
        assessment.explanation = f"已完成项目第 {index} 位。" + assessment.explanation
    for index, assessment in enumerate(candidate_rankable, start=1):
        assessment.explanation = f"下一年度候选措施第 {index} 位。" + assessment.explanation

    counts: dict[str, int] = {}
    for assessment in assessments:
        counts[assessment.decision] = counts.get(assessment.decision, 0) + 1
    total_verified_saving = sum(
        a.annualised_saving_kwh_e or 0 for a in completed if a.decision == "priority"
    )
    summary = {
        "buildings": len(dataset.building_ids()),
        "projects": len(assessments),
        "completed_projects": len(completed),
        "candidate_projects": len(candidates),
        "decision_counts": counts,
        "verified_annual_saving_kwh_e": round(total_verified_saving, 1),
        "completed_ranked": [a.project.project_id for a in completed_rankable],
        "candidates_ranked": [a.project.project_id for a in candidate_rankable],
        "held": [a.project.project_id for a in candidate_held + failed],
        "input_errors": sum(1 for issue in dataset.issues if issue.severity == "error"),
        "input_warnings": sum(1 for issue in dataset.issues if issue.severity == "warning"),
    }

    issues = [
        {"severity": i.severity, "dataset": i.dataset, "row": i.row, "message": i.message}
        for i in dataset.issues
    ]
    if summary["input_errors"]:
        warnings.append(f"输入资料有 {summary['input_errors']} 项错误，相关记录未参与分析。")
    if not dataset.projects:
        warnings.append("尚未导入改造项目记录。")
    if not dataset.comfort:
        warnings.append("尚未导入舒适度抽测记录，服务结论将显示为证据不足。")

    return AnalysisResult(
        as_of=as_of,
        config=config,
        assessments=assessments,
        completed_ranking=completed_rankable,
        candidate_ranking=candidate_rankable,
        data_issues=issues,
        summary=summary,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# basis snapshots / re-audit
# ---------------------------------------------------------------------------


def basis_snapshot(assessment: ProjectAssessment) -> dict[str, Any]:
    """Immutable record of *why* a project was approved, kept for re-audit."""

    comfort = assessment.comfort
    return {
        "project_id": assessment.project.project_id,
        "decision": assessment.decision,
        "decision_label": assessment.decision_label,
        "reasons": list(assessment.reasons),
        "explanation": assessment.explanation,
        "saving_pct": _round(assessment.saving_pct),
        "annualised_saving_kwh_e": _round(assessment.annualised_saving_kwh_e, 1),
        "evidence_score": round(assessment.evidence_score, 2),
        "comfort": {
            "state": comfort.state if comfort else "no_data",
            "post_pct": _round(comfort.post_pct) if comfort else None,
            "interval_low": _round(comfort.interval_low) if comfort else None,
            "interval_high": _round(comfort.interval_high) if comfort else None,
            "sample_count": comfort.sample_count if comfort else None,
            "target": _round(comfort.target) if comfort else None,
        },
        "baseline": assessment.baseline.as_dict() if assessment.baseline else None,
        "baseline_kind": assessment.baseline_kind,
        "post": assessment.post.as_dict() if assessment.post else None,
        "commissioning": assessment.commissioning.as_dict() if assessment.commissioning else None,
        "works": assessment.works.as_dict() if assessment.works else None,
        "confounders": [c.as_dict() for c in assessment.confounders],
        "closure": assessment.closure.as_dict() if assessment.closure else None,
        "outstanding": list(assessment.outstanding),
    }


def compare_to_approved(assessment: ProjectAssessment, approved_basis: dict[str, Any]) -> dict[str, Any]:
    """Diff the current assessment against the basis frozen at approval."""

    current = basis_snapshot(assessment)
    changes: list[str] = []
    if current["decision"] != approved_basis.get("decision"):
        changes.append(
            f"结论由“{approved_basis.get('decision_label')}”变为“{current['decision_label']}”。"
        )
    old_saving = approved_basis.get("saving_pct")
    new_saving = current.get("saving_pct")
    if old_saving is not None and new_saving is not None and abs(new_saving - old_saving) >= 3.0:
        changes.append(f"实测节能率由 {old_saving:.1f}% 变为 {new_saving:.1f}%。")
    old_comfort = approved_basis.get("comfort") or {}
    new_comfort = current.get("comfort") or {}
    if old_comfort.get("state") != new_comfort.get("state"):
        changes.append(
            f"舒适度结论由“{old_comfort.get('state')}”变为“{new_comfort.get('state')}”。"
        )
    old_low, new_low = old_comfort.get("interval_low"), new_comfort.get("interval_low")
    target = new_comfort.get("target")
    if target is not None and old_low is not None and new_low is not None:
        old_ok, new_ok = old_low >= target, new_low >= target
        if old_ok != new_ok:
            changes.append(f"舒适度满意率下界相对目标 {target:.0f}% 的达标状态发生变化。")
    old_outstanding = set(approved_basis.get("outstanding") or [])
    new_outstanding = set(current.get("outstanding") or [])
    added = sorted(new_outstanding - old_outstanding)
    resolved = sorted(old_outstanding - new_outstanding)
    if added:
        changes.append("新增未完成事项：" + "；".join(added))
    if resolved:
        changes.append("已消除事项：" + "；".join(resolved))
    if current["reasons"] != approved_basis.get("reasons"):
        changes.append("排序理由代码发生变化：" + ", ".join(current["reasons"]) + "。")
    return {
        "project_id": assessment.project.project_id,
        "affected": bool(changes),
        "changes": changes,
        "approved_basis": approved_basis,
        "current_basis": current,
    }
