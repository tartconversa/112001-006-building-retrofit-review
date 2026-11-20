from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from retrofit.analysis import basis_snapshot
from retrofit.inputs import DataError
from retrofit.state import Workspace, _record_to_row
from tests.test_analysis import TARGETS, build_dataset, completed_project, library, month_rows, survey


class WorkspaceTests(unittest.TestCase):
    def make_workspace(self, **kwargs):
        dataset = build_dataset(**kwargs)
        workspace = Workspace()
        for key in ("buildings", "energy", "comfort", "targets", "projects", "measurements", "events"):
            workspace.records[key] = [_record_to_row(row) for row in getattr(dataset, key)]
        return workspace

    def test_round_trip_persistence(self):
        workspace = self.make_workspace()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ws.json"
            workspace.save(path)
            loaded = Workspace.load(path)
            self.assertEqual(len(loaded.records["projects"]), 1)
            self.assertEqual(loaded.analyse().summary["completed_projects"], 1)

    def test_register_measure_validation(self):
        workspace = self.make_workspace()
        with self.assertRaises(DataError):
            workspace.register_measure(
                {
                    "project_id": "PX",
                    "building_id": "BL01",
                    "measure_start": "2026-03-01",
                    "measure_end": "2026-04-01",
                    "planned_closure_start": "2026-02-01",
                    "planned_closure_end": "2026-02-10",
                },
                "tester",
            )

    def test_register_measure_and_handoff(self):
        workspace = self.make_workspace(
            projects=[
                completed_project(
                    accepted_on="",
                    status="proposed",
                    measure_start="2026-12-01",
                    measure_end="2027-01-15",
                    commissioning_start="2027-01-16",
                    commissioning_end="2027-01-31",
                    predicted_saving_pct="18",
                )
            ],
            comfort=[survey("C1", "2025-04-20", 92, 120), survey("C2", "2025-08-20", 55, 120)],
        )
        # remove accepted_on from the serialised row so phase is proposed
        workspace.records["projects"][0]["accepted_on"] = ""
        assessment = workspace._assessment_for("PR01")
        self.assertFalse(assessment.is_completed)

    def test_approve_and_correct_flow(self):
        workspace = self.make_workspace(
            projects=[
                completed_project(
                    accepted_on="",
                    status="proposed",
                    measure_start="2026-12-01",
                    measure_end="2027-01-15",
                    commissioning_start="2027-01-16",
                    commissioning_end="2027-01-31",
                    predicted_saving_pct="18",
                )
            ],
            comfort=[survey("C1", "2026-09-20", 92, 120), survey("C2", "2026-10-20", 90, 120)],
        )
        workspace.records["projects"][0]["accepted_on"] = ""
        # evaluate as if the measure were still ahead of us
        workspace.as_of = "2026-11-20"
        workspace.approvals["PR01"] = {
            "approved_by": "资产管理员",
            "approved_at": "2026-11-21T00:00:00+00:00",
            "decided_on": "2026-11-20",
            "note": "试点",
            "decision": "conditional",
            "basis": basis_snapshot(workspace._assessment_for("PR01")),
            "handoff": None,
            "acceptance": None,
        }
        workspace.records["projects"][0]["status"] = "approved"
        workspace.handoff("PR01", "资产管理员", "设施运维组", "移交跟进")
        self.assertEqual(workspace.approvals["PR01"]["handoff"]["team"], "设施运维组")

        report = workspace.reaudit()
        self.assertEqual(report["as_of"], "2026-11-20")

        # A correction to the pre-measure comfort survey changes the basis.
        workspace.records["comfort"] = [
            row for row in workspace.records["comfort"] if row["record_id"] != "C2"
        ] + [
            {
                "record_id": "C2",
                "building_id": "BL01",
                "date": "2026-10-20",
                "comfort_sample_pct": "60",
                "complaint_count": "9",
                "sample_count": "120",
                "project_id": "PR01",
                "stage": "planned",
            }
        ]
        report = workspace.reaudit()
        affected = {item["project_id"]: item for item in report["affected"]}
        self.assertIn("PR01", affected)
        self.assertTrue(affected["PR01"]["changes"])
        self.assertEqual(affected["PR01"]["retained_basis"]["decision"], "conditional")


class ApprovalGuardTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace()
        dataset = build_dataset(comfort=[survey("C1", "2025-04-20", 91, 110), survey("C2", "2025-08-20", 70, 110)])
        for key in ("buildings", "energy", "comfort", "targets", "projects", "measurements", "events"):
            self.workspace.records[key] = [_record_to_row(row) for row in getattr(dataset, key)]

    def test_completed_project_cannot_be_approved_as_measure(self):
        with self.assertRaises(DataError):
            self.workspace.approve("PR01", "资产管理员", "")

    def test_handoff_requires_approval(self):
        with self.assertRaises(DataError):
            self.workspace.handoff("PR01", "资产管理员", "设施运维组", "")


if __name__ == "__main__":
    unittest.main()
