from datetime import date
import unittest

from retrofit.analysis import (
    AnalysisConfig,
    aggregate_energy,
    analyse,
    assess_comfort,
    basis_snapshot,
    compare_to_approved,
    wilson_interval,
)
from retrofit.inputs import dataset_from_records


TARGETS = [
    {
        "use_type": "library",
        "min_open_days": "5",
        "min_open_hours_per_day": "10",
        "min_comfort_sample_pct": "85",
        "valid_from": "2025-01-01",
    },
    {
        "use_type": "clinic",
        "min_open_days": "7",
        "min_open_hours_per_day": "16",
        "min_comfort_sample_pct": "90",
        "valid_from": "2025-01-01",
    },
]


def month_rows(building_id, start_year, start_month, count, kwh, gas, dd, coverage="99", factor=1.0):
    rows = []
    year, month = start_year, start_month
    for _ in range(count):
        rows.append(
            {
                "building_id": building_id,
                "period": f"{year}-{month:02d}",
                "electric_kwh": str(round(kwh * factor)),
                "gas_m3": str(round(gas * factor)),
                "degree_days": str(dd),
                "coverage_pct": coverage,
            }
        )
        month += 1
        if month > 12:
            month = 1
            year += 1
    return rows


def library(open_days="6", open_hours="12", valid_to=""):
    return {
        "building_id": "BL01",
        "use_type": "library",
        "open_days": open_days,
        "open_hours_per_day": open_hours,
        "valid_from": "2024-01-01",
        "valid_to": valid_to,
    }


def completed_project(**overrides):
    row = {
        "project_id": "PR01",
        "building_id": "BL01",
        "name": "照明与空调改造",
        "measures": "LED 与热回收",
        "measure_start": "2025-06-01",
        "measure_end": "2025-06-30",
        "commissioning_start": "2025-07-01",
        "commissioning_end": "2025-07-14",
        "status": "completed",
        "accepted_on": "2025-11-05",
    }
    row.update(overrides)
    return row


def survey(record_id, day, pct, n, stage="accepted", project_id="PR01", coverage=None):
    row = {
        "record_id": record_id,
        "building_id": "BL01",
        "date": day,
        "comfort_sample_pct": str(pct),
        "complaint_count": "2",
        "sample_count": str(n),
        "project_id": project_id,
        "stage": stage,
    }
    if coverage is not None:
        row["sample_coverage_pct"] = str(coverage)
    return row


def build_dataset(energy_extra=None, comfort=None, projects=None, events=None, buildings=None):
    energy = month_rows("BL01", 2024, 1, 19, 13000, 150, 800)
    energy += month_rows("BL01", 2025, 8, 6, 10400, 120, 800)
    if energy_extra:
        energy += energy_extra
    return dataset_from_records(
        {
            "buildings": buildings or [library()],
            "targets": TARGETS,
            "energy": energy,
            "comfort": comfort
            if comfort is not None
            else [
                survey("C1", "2025-04-20", 91, 110),
                survey("C2", "2025-08-20", 94, 110),
            ],
            "projects": projects or [completed_project()],
            "events": events or [],
        }
    )


class WindowTests(unittest.TestCase):
    def test_works_and_commissioning_are_separate_and_post_starts_after_commissioning(self):
        result = analyse(build_dataset(), as_of=date(2026, 6, 30))
        assessment = result.by_project()["PR01"]
        self.assertEqual(assessment.commissioning.start, date(2025, 7, 1))
        self.assertEqual(assessment.post.start, date(2025, 7, 15))
        self.assertGreaterEqual(assessment.post.start, assessment.commissioning.end)
        self.assertLess(assessment.works.end, assessment.post.start)

    def test_overlapping_commissioning_record_is_trimmed_not_merged(self):
        dataset = build_dataset(
            projects=[completed_project(commissioning_start="2025-06-20", commissioning_end="2025-07-14")]
        )
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertEqual(assessment.commissioning.start, date(2025, 7, 1))
        self.assertGreaterEqual(assessment.post.start, date(2025, 7, 15))

    def test_same_period_baseline_used_when_available(self):
        assessment = analyse(build_dataset(), as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertEqual(assessment.baseline_kind, "same_period")
        self.assertEqual(assessment.baseline.start.year, assessment.post.start.year - 1)

    def test_candidate_has_no_post_window(self):
        dataset = build_dataset(
            projects=[
                completed_project(
                    status="proposed",
                    accepted_on="",
                    measure_start="2026-12-01",
                    measure_end="2027-01-15",
                    commissioning_start="2027-01-16",
                    commissioning_end="2027-01-31",
                    predicted_saving_pct="18",
                )
            ]
        )
        assessment = analyse(dataset, as_of=date(2026, 11, 20)).by_project()["PR01"]
        self.assertFalse(assessment.is_completed)
        self.assertIsNone(assessment.post)


class ServiceGateTests(unittest.TestCase):
    def test_comfort_below_target_cannot_be_offset_by_saving(self):
        dataset = build_dataset(comfort=[survey("C1", "2025-04-20", 91, 110), survey("C2", "2025-08-20", 70, 110)])
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertGreater(assessment.saving_pct, 15)
        self.assertEqual(assessment.decision, "service_fail")
        self.assertIn("comfort_below_target", assessment.reasons)
        self.assertIn("服务未达标不能由节能抵消", assessment.explanation)

    def test_thin_sample_gives_range_not_single_point(self):
        dataset = build_dataset(comfort=[survey("C1", "2025-04-20", 91, 110), survey("C2", "2025-08-20", 93, 6)])
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        comfort = assessment.comfort
        self.assertFalse(comfort.samples_sufficient)
        self.assertNotEqual(comfort.state, "pass")
        self.assertEqual(assessment.decision, "needs_measurement")
        self.assertIn("comfort_evidence_insufficient", assessment.reasons)
        self.assertGreaterEqual(comfort.interval_high - comfort.interval_low, 20)

    def test_interval_crossing_target_is_unproven(self):
        dataset = build_dataset(comfort=[survey("C1", "2025-04-20", 91, 110), survey("C2", "2025-08-20", 92, 40)])
        comfort = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"].comfort
        self.assertEqual(comfort.target, 85)
        self.assertEqual(comfort.state, "unproven")
        self.assertLess(comfort.interval_low, 85)
        self.assertGreater(comfort.interval_high, 85)

    def test_commissioning_survey_not_used_as_steady_state_evidence(self):
        dataset = build_dataset(
            comfort=[
                survey("C1", "2025-04-20", 91, 110),
                survey("C2", "2025-07-05", 40, 200, stage="commissioning"),
            ]
        )
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertNotIn("C2", assessment.comfort.records)
        self.assertEqual(assessment.comfort.state, "no_data")
        self.assertEqual(assessment.decision, "needs_measurement")


class ComparabilityTests(unittest.TestCase):
    def test_use_type_change_is_incomparable(self):
        buildings = [
            {
                "building_id": "BL01",
                "use_type": "community_hall",
                "open_days": "5",
                "open_hours_per_day": "9",
                "valid_from": "2024-01-01",
                "valid_to": "2025-06-30",
            },
            {
                "building_id": "BL01",
                "use_type": "library",
                "open_days": "6",
                "open_hours_per_day": "12",
                "valid_from": "2025-07-01",
                "valid_to": "",
            },
        ]
        dataset = build_dataset(buildings=buildings)
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertEqual(assessment.decision, "incomparable")
        self.assertIn("use_type_change", assessment.reasons)
        self.assertIsNone(assessment.rank_score)

    def test_open_hours_change_is_normalised_not_credited(self):
        buildings = [
            library("6", "12", valid_to="2025-06-30"),
            {**library("4", "8"), "valid_from": "2025-07-01"},
        ]
        # Post-period raw energy falls by 20% purely because hours were cut.
        dataset = build_dataset(
            buildings=buildings,
            energy_extra=month_rows("BL01", 2025, 8, 6, 10400 * 32 / 72, 120 * 32 / 72, 800),
        )
        # remove the default identical post rows so only the reduced ones remain
        dataset.energy = [e for e in dataset.energy if not (e.start.year == 2025 and e.start.month >= 8 and abs(e.electric_kwh - 10400) < 1)]
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertIn("schedule_change", [c.code for c in assessment.confounders])
        self.assertLess(abs(assessment.saving_pct), 10)
        self.assertEqual(assessment.decision, "defer")
        self.assertIsNotNone(assessment.schedule_adjustment_pct)

    def test_meter_replacement_blocks_until_reconciled(self):
        event = {
            "event_id": "EV1",
            "building_id": "BL01",
            "event_type": "meter_replacement",
            "start": "2025-09-01",
            "end": "2025-09-01",
            "verified": "false",
        }
        assessment = analyse(build_dataset(events=[event]), as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertEqual(assessment.decision, "needs_measurement")
        self.assertIn("meter_replacement", assessment.reasons)
        self.assertTrue(any("电表" in item for item in assessment.outstanding))

    def test_verified_meter_replacement_does_not_block(self):
        event = {
            "event_id": "EV1",
            "building_id": "BL01",
            "event_type": "meter_replacement",
            "start": "2025-09-01",
            "end": "2025-09-01",
            "verified": "true",
        }
        assessment = analyse(build_dataset(events=[event]), as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertEqual(assessment.decision, "priority")

    def test_candidate_is_blocked_by_event_in_its_reference_period(self):
        event = {
            "event_id": "EV2",
            "building_id": "BL01",
            "event_type": "tenant_change",
            "start": "2026-10-01",
            "end": "2026-10-01",
            "verified": "false",
        }
        dataset = build_dataset(
            events=[event],
            projects=[
                completed_project(
                    status="proposed",
                    accepted_on="",
                    measure_start="2026-12-01",
                    measure_end="2027-01-15",
                    commissioning_start="2027-01-16",
                    commissioning_end="2027-01-31",
                    predicted_saving_pct="18",
                )
            ],
            comfort=[survey("C1", "2026-10-15", 92, 120)],
        )
        assessment = analyse(dataset, as_of=date(2026, 11, 20)).by_project()["PR01"]
        # Candidates have no post window; the reference period still carries
        # the confounder so the reviewer sees it in the evidence trail.
        self.assertIn("tenant_change", [c.code for c in assessment.confounders])
        self.assertTrue(any("租户" in item for item in assessment.outstanding))

    def test_weather_difference_is_adjusted_and_labelled(self):
        energy = month_rows("BL01", 2024, 1, 19, 13000, 150, 800)
        energy += month_rows("BL01", 2025, 8, 6, 10400, 120, 1200)  # colder post period
        dataset = dataset_from_records(
            {
                "buildings": [library()],
                "targets": TARGETS,
                "energy": energy,
                "comfort": [survey("C1", "2025-04-20", 91, 110), survey("C2", "2025-08-20", 94, 110)],
                "projects": [completed_project()],
            }
        )
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertIsNotNone(assessment.weather_adjustment_pct)
        self.assertEqual(assessment.weather_adjustment_method, "degree_day_ratio")
        self.assertGreater(assessment.saving_pct, assessment.raw_saving_pct)


class RankingAndAuditTests(unittest.TestCase):
    def test_priority_projects_rank_above_needs_measurement(self):
        passing = analyse(build_dataset(), as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertEqual(passing.decision, "priority")
        self.assertGreater(passing.rank_score or 0, 0)

    def test_basis_snapshot_and_diff_survive_correction(self):
        dataset = build_dataset()
        result = analyse(dataset, as_of=date(2026, 6, 30))
        assessment = result.by_project()["PR01"]
        frozen = basis_snapshot(assessment)

        # A later correction reveals the survey was thinner than recorded.
        corrected = build_dataset(comfort=[survey("C1", "2025-04-20", 91, 110), survey("C2", "2025-08-20", 60, 110)])
        after = analyse(corrected, as_of=date(2026, 6, 30)).by_project()["PR01"]
        diff = compare_to_approved(after, frozen)
        self.assertTrue(diff["affected"])
        self.assertEqual(diff["approved_basis"]["decision"], "priority")
        self.assertEqual(diff["current_basis"]["decision"], "service_fail")
        self.assertTrue(any("结论" in change for change in diff["changes"]))

    def test_outstanding_lists_missing_verification(self):
        dataset = build_dataset(comfort=[survey("C1", "2025-04-20", 91, 110)])
        assessment = analyse(dataset, as_of=date(2026, 6, 30)).by_project()["PR01"]
        self.assertTrue(any("舒适度" in item for item in assessment.outstanding))


class AggregateTests(unittest.TestCase):
    def test_energy_is_prorated_across_partial_periods(self):
        dataset = build_dataset()
        config = AnalysisConfig()
        aggregate = aggregate_energy(
            dataset,
            "BL01",
            __import__("retrofit.analysis", fromlist=["Window"]).Window(date(2025, 8, 1), date(2025, 8, 16), "half"),
            config,
        )
        self.assertAlmostEqual(aggregate.electric_kwh, 10400 / 2, delta=250)
        self.assertEqual(aggregate.missing_days, 0)

    def test_wilson_interval_narrows_with_sample_size(self):
        small = wilson_interval(90, 100, 0.9)
        large = wilson_interval(900, 1000, 0.9)
        self.assertGreater(small[0], 0)
        self.assertLess(small[1], 100)
        self.assertLess(large[1] - large[0], small[1] - small[0])


if __name__ == "__main__":
    unittest.main()
