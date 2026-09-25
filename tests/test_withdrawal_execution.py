import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    BatchBlockedError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

ADMIN = Actor("admin", "admin")


class WithdrawalExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.scenario = self._build_scenario()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_participant(self, name):
        return self.service.create(ADMIN, "participant", {"name": name})["id"]

    def _activate_consent(self, participant_id):
        consent = self.service.create(
            ADMIN, "consent", {"participant_id": participant_id, "scope": ["research"]}
        )
        return self.service.transition(
            ADMIN,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )["id"]

    def _store_sample(self, participant_id, consent_id, code):
        sample = self.service.create(
            ADMIN,
            "sample",
            {"participant_id": participant_id, "sample_code": code, "collected_at": "2026-01-01"},
        )
        return self.service.transition(
            ADMIN,
            sample["id"],
            "store",
            {"freezer": "F1", "position": code, "consent_id": consent_id},
        )["id"]

    def _build_scenario(self):
        p1 = self._create_participant("Participant One")
        p2 = self._create_participant("Participant Two")
        consent = self._activate_consent(p1)
        other_consent = self._activate_consent(p2)
        # An old, already superseded consent must not appear on the checklist.
        old_consent = self.service.create(
            ADMIN, "consent", {"participant_id": p1, "scope": ["research"]}
        )
        old_consent = self.service.transition(
            ADMIN,
            old_consent["id"],
            "activate",
            {"scope": ["research"], "version": "v0", "expires_at": "2025-01-01"},
        )
        self.service.transition(
            ADMIN, old_consent["id"], "supersede", {"reason": "renewed"}
        )
        stored_sample = self._store_sample(p1, consent, "B-001")
        # A collected (never stored) sample is also an open sample.
        collected_sample = self.service.create(
            ADMIN,
            "sample",
            {"participant_id": p1, "sample_code": "B-002", "collected_at": "2026-01-02"},
        )["id"]
        foreign_sample = self._store_sample(p2, other_consent, "B-999")
        withdrawal = self.service.create(
            ADMIN, "withdrawal", {"participant_id": p1, "requested_at": "2026-03-01"}
        )["id"]
        self.service.transition(
            ADMIN,
            withdrawal,
            "approve",
            {"reason": "participant request", "sample_ids": [stored_sample, collected_sample]},
        )
        return {
            "p1": p1,
            "p2": p2,
            "consent": consent,
            "old_consent": old_consent["id"],
            "other_consent": other_consent,
            "stored_sample": stored_sample,
            "collected_sample": collected_sample,
            "foreign_sample": foreign_sample,
            "withdrawal": withdrawal,
        }

    def _reconcile(self):
        return self.service.reconcile_withdrawal(ADMIN, self.scenario["withdrawal"])

    def _execute(self, data=None, expected_version=None):
        s = self.scenario
        payload = data or {
            "executed_at": "2026-03-02",
            "samples": [
                {"id": s["stored_sample"], "version": 2},
                {"id": s["collected_sample"], "version": 1},
            ],
            "consents": [{"id": s["consent"], "version": 2}],
        }
        return self.service.execute_withdrawal(
            ADMIN, s["withdrawal"], payload, expected_version
        )

    def _codes(self, exc):
        return {reason["code"] for reason in exc.reasons}

    # -- checklist ---------------------------------------------------------

    def test_checklist_covers_all_open_samples_and_active_consent_only(self):
        result = self._reconcile()
        checklist = result["checklist"]
        sample_ids = {item["id"] for item in checklist["samples"]}
        consent_ids = {item["id"] for item in checklist["consents"]}
        self.assertEqual(sample_ids, {self.scenario["stored_sample"], self.scenario["collected_sample"]})
        self.assertEqual(consent_ids, {self.scenario["consent"]})
        # No duplicate rows.
        self.assertEqual(len(sample_ids), len(checklist["samples"]))
        # Foreign samples and superseded consents are excluded.
        self.assertNotIn(self.scenario["foreign_sample"], sample_ids)
        self.assertNotIn(self.scenario["old_consent"], consent_ids)

    def test_reconcile_leaves_audit_entry(self):
        self._reconcile()
        actions = [
            entry["action"]
            for entry in self.service.audit_log(self.scenario["withdrawal"])
        ]
        self.assertIn("reconcile", actions)

    def test_reconcile_before_approval_rejected(self):
        withdrawal = self.service.create(
            ADMIN, "withdrawal",
            {"participant_id": self.scenario["p1"], "requested_at": "2026-03-03"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.reconcile_withdrawal(ADMIN, withdrawal["id"])

    # -- successful batch --------------------------------------------------

    def test_execute_terminates_consent_and_all_participant_samples(self):
        self._reconcile()
        result = self._execute()
        self.assertEqual(result["status"], "executed")
        self.assertEqual(
            set(result["terminated_sample_ids"]),
            {self.scenario["stored_sample"], self.scenario["collected_sample"]},
        )
        self.assertEqual(
            result["terminated_consent_ids"], [self.scenario["consent"]]
        )
        self.assertEqual(self.service.get(self.scenario["consent"])["status"], "withdrawn")
        self.assertEqual(self.service.get(self.scenario["stored_sample"])["status"], "withdrawn")
        self.assertEqual(self.service.get(self.scenario["collected_sample"])["status"], "withdrawn")
        # Superseded consent and the other participant's data stay untouched.
        self.assertEqual(self.service.get(self.scenario["old_consent"])["status"], "superseded")
        self.assertEqual(self.service.get(self.scenario["foreign_sample"])["status"], "stored")
        self.assertEqual(self.service.get(self.scenario["other_consent"])["status"], "active")

    def test_processed_samples_cannot_be_loaned_or_restored(self):
        self._reconcile()
        self._execute()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                ADMIN,
                self.scenario["stored_sample"],
                "loan",
                {"recipient": "X", "purpose": "p", "due_at": "2026-10-01"},
            )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                ADMIN,
                self.scenario["stored_sample"],
                "store",
                {"freezer": "F2", "position": "B9", "consent_id": self.scenario["consent"]},
            )

    def test_re_executing_is_not_allowed(self):
        self._reconcile()
        self._execute()
        with self.assertRaises(InvalidTransition):
            self._execute()

    def test_successful_execution_audits_every_step(self):
        self._reconcile()
        self._execute()
        withdrawal_actions = [
            entry["action"] for entry in self.service.audit_log(self.scenario["withdrawal"])
        ]
        self.assertEqual(withdrawal_actions[-1], "execute")
        for sample_id in (self.scenario["stored_sample"], self.scenario["collected_sample"]):
            actions = [entry["action"] for entry in self.service.audit_log(sample_id)]
            self.assertEqual(actions[-1], "withdraw")
        consent_actions = [
            entry["action"] for entry in self.service.audit_log(self.scenario["consent"])
        ]
        self.assertEqual(consent_actions[-1], "withdraw")

    # -- blocking rules ----------------------------------------------------

    def test_loaned_sample_blocks_whole_batch_and_changes_nothing(self):
        s = self.scenario
        self.service.transition(
            ADMIN,
            s["stored_sample"],
            "loan",
            {"recipient": "lab", "purpose": "study", "due_at": "2026-10-01"},
        )
        self._reconcile()
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute({
                "executed_at": "2026-03-02",
                "samples": [
                    {"id": s["stored_sample"], "version": 3},
                    {"id": s["collected_sample"], "version": 1},
                ],
                "consents": [{"id": s["consent"], "version": 2}],
            })
        self.assertIn("SAMPLE_ON_LOAN", self._codes(caught.exception))
        self.assertEqual(self.service.get(s["withdrawal"])["status"], "approved")
        self.assertEqual(self.service.get(s["stored_sample"])["status"], "on_loan")
        self.assertEqual(self.service.get(s["consent"])["status"], "active")

    def test_missing_sample_blocks_with_not_covered(self):
        s = self.scenario
        self._reconcile()
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute({
                "executed_at": "2026-03-02",
                "samples": [{"id": s["stored_sample"], "version": 2}],
                "consents": [{"id": s["consent"], "version": 2}],
            })
        self.assertEqual(
            self._codes(caught.exception), {"SAMPLE_NOT_COVERED"}
        )
        self.assertEqual(self.service.get(s["stored_sample"])["status"], "stored")

    def test_foreign_sample_blocks(self):
        s = self.scenario
        self._reconcile()
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute({
                "executed_at": "2026-03-02",
                "samples": [
                    {"id": s["stored_sample"], "version": 2},
                    {"id": s["collected_sample"], "version": 1},
                    {"id": s["foreign_sample"], "version": 2},
                ],
                "consents": [{"id": s["consent"], "version": 2}],
            })
        self.assertIn("SAMPLE_FOREIGN_OR_TERMINAL", self._codes(caught.exception))
        self.assertEqual(self.service.get(s["foreign_sample"])["status"], "stored")

    def test_duplicate_sample_entry_blocks(self):
        s = self.scenario
        self._reconcile()
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute({
                "executed_at": "2026-03-02",
                "samples": [
                    {"id": s["stored_sample"], "version": 2},
                    {"id": s["stored_sample"], "version": 2},
                    {"id": s["collected_sample"], "version": 1},
                ],
                "consents": [{"id": s["consent"], "version": 2}],
            })
        self.assertIn("SAMPLE_DUPLICATE", self._codes(caught.exception))

    def test_stale_sample_version_blocks(self):
        s = self.scenario
        # Loan then return keeps the sample stored but bumps its version.
        self.service.transition(
            ADMIN, s["stored_sample"], "loan",
            {"recipient": "lab", "purpose": "study", "due_at": "2026-10-01"},
        )
        self.service.transition(ADMIN, s["stored_sample"], "return", {"returned_at": "2026-04-01"})
        self._reconcile()
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute({
                "executed_at": "2026-03-02",
                "samples": [
                    {"id": s["stored_sample"], "version": 2},
                    {"id": s["collected_sample"], "version": 1},
                ],
                "consents": [{"id": s["consent"], "version": 2}],
            })
        self.assertIn("SAMPLE_VERSION_CHANGED", self._codes(caught.exception))
        self.assertEqual(self.service.get(s["stored_sample"])["status"], "stored")

    def test_missing_consent_blocks(self):
        s = self.scenario
        self._reconcile()
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute({
                "executed_at": "2026-03-02",
                "samples": [
                    {"id": s["stored_sample"], "version": 2},
                    {"id": s["collected_sample"], "version": 1},
                ],
                "consents": [],
            })
        self.assertIn("CONSENT_NOT_COVERED", self._codes(caught.exception))

    def test_execute_without_reconcile_blocks(self):
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute()
        self.assertIn("MISSING_RECONCILIATION", self._codes(caught.exception))

    def test_stale_withdrawal_version_blocks(self):
        s = self.scenario
        self._reconcile()
        with self.assertRaises(BatchBlockedError) as caught:
            self._execute(expected_version=999)
        self.assertIn("WITHDRAWAL_VERSION_CHANGED", self._codes(caught.exception))

    def test_blocked_execution_leaves_audit_with_reasons(self):
        with self.assertRaises(BatchBlockedError):
            self._execute()
        entries = self.service.audit_log(self.scenario["withdrawal"])
        blocked = [entry for entry in entries if entry["action"] == "execute_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(blocked[0]["detail"]["reasons"])

    # -- permissions and orchestration gate --------------------------------

    def test_viewer_cannot_reconcile_or_execute(self):
        viewer = Actor("viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.reconcile_withdrawal(viewer, self.scenario["withdrawal"])
        with self.assertRaises(PermissionDenied):
            self.service.execute_withdrawal(
                viewer, self.scenario["withdrawal"], {"executed_at": "x", "samples": [], "consents": []}
            )

    def test_direct_sample_withdraw_action_is_rejected(self):
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                ADMIN, self.scenario["stored_sample"], "withdraw", {"reason": "bypass"}
            )

    def test_approve_rejects_foreign_sample(self):
        withdrawal = self.service.create(
            ADMIN, "withdrawal",
            {"participant_id": self.scenario["p1"], "requested_at": "2026-03-05"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                ADMIN,
                withdrawal["id"],
                "approve",
                {"reason": "x", "sample_ids": [self.scenario["foreign_sample"]]},
            )

    def test_execute_on_unapproved_withdrawal_rejected(self):
        withdrawal = self.service.create(
            ADMIN, "withdrawal",
            {"participant_id": self.scenario["p1"], "requested_at": "2026-03-06"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.execute_withdrawal(
                ADMIN, withdrawal["id"],
                {"executed_at": "2026-03-02", "samples": [], "consents": []},
            )


if __name__ == "__main__":
    unittest.main()
