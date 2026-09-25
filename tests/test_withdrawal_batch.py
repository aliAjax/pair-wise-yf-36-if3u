import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.domain import (
    Actor,
    BatchConflictError,
    ConflictError,
    InvalidTransition,
    ValidationError,
)
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin-1", "admin")


class WithdrawalBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    # --- fixtures -------------------------------------------------------

    def participant(self, name="Participant"):
        return self.service.create(
            ADMIN, "participant", {"name": name}
        )

    def active_consent(self, participant_id, version="v1"):
        consent = self.service.create(
            ADMIN,
            "consent",
            {"participant_id": participant_id, "scope": ["research"]},
        )
        return self.service.transition(
            ADMIN,
            consent["id"],
            "activate",
            {
                "scope": ["research"],
                "version": version,
                "expires_at": "2099-01-01",
            },
        )

    def sample(self, participant_id, code):
        return self.service.create(
            ADMIN,
            "sample",
            {
                "participant_id": participant_id,
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )

    def store(self, sample_id, consent_id):
        return self.service.transition(
            ADMIN,
            sample_id,
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent_id},
        )

    def open_samples(self, participant_id, consent_id, codes, keep_last_collected=False):
        created = [self.sample(participant_id, code) for code in codes]
        to_store = created[:-1] if keep_last_collected else created
        rest = created[len(to_store):]
        stored = [self.store(entity["id"], consent_id) for entity in to_store]
        # Return fresh rows so statuses/versions seen by callers match the DB.
        fresh_rest = [self.service.get(entity["id"]) for entity in rest]
        return stored + fresh_rest

    def approved_withdrawal(self, participant_id, sample_ids):
        withdrawal = self.service.create(
            ADMIN,
            "withdrawal",
            {"participant_id": participant_id, "requested_at": "2026-03-01"},
        )
        approved = self.service.transition(
            ADMIN,
            withdrawal["id"],
            "approve",
            {"reason": "participant request", "sample_ids": sample_ids},
        )
        self.assertEqual(approved["status"], "approved")
        return approved

    def execute(self, withdrawal_id):
        return self.service.transition(
            ADMIN,
            withdrawal_id,
            "execute",
            {"executed_at": "2026-03-02T09:00:00Z"},
        )

    # --- happy path -----------------------------------------------------

    def test_execute_terminates_consent_and_every_open_sample_in_one_batch(self):
        p = self.participant("One")
        consent = self.active_consent(p["id"])
        consent2 = self.active_consent(p["id"], version="v2")
        samples = self.open_samples(
            p["id"], consent["id"], ["B-001", "B-002", "B-003"], True
        )
        # Two stored, one still collected: both open states must be covered.
        self.assertEqual(
            [s["status"] for s in samples], ["stored", "stored", "collected"]
        )
        withdrawal = self.approved_withdrawal(
            p["id"], [s["id"] for s in samples]
        )

        result = self.execute(withdrawal["id"])

        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["data"]["executed_by"], "admin-1")
        for entity in samples:
            done = self.service.get(entity["id"])
            self.assertEqual(done["status"], "withdrawn")
            self.assertEqual(done["data"]["withdrawal_id"], withdrawal["id"])
            self.assertEqual(done["data"]["withdrawn_at"], "2026-03-02T09:00:00Z")
        for consent_id in (consent["id"], consent2["id"]):
            self.assertEqual(self.service.get(consent_id)["status"], "withdrawn")

        actions = {
            row["entity_id"]: row["action"]
            for row in self.service.audit_log()
        }
        for entity in samples:
            self.assertEqual(actions[entity["id"]], "withdraw")
        execute_rows = [
            row
            for row in self.service.audit_log(withdrawal["id"])
            if row["action"] == "execute"
        ]
        self.assertEqual(len(execute_rows), 1)
        self.assertEqual(execute_rows[0]["detail"]["sample_count"], 3)
        self.assertEqual(execute_rows[0]["detail"]["consent_count"], 2)

    # --- approval checklist validation ----------------------------------

    def test_approve_rejects_duplicate_sample_ids(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.service.create(
            ADMIN,
            "withdrawal",
            {"participant_id": p["id"], "requested_at": "2026-03-01"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                ADMIN,
                withdrawal["id"],
                "approve",
                {"reason": "r", "sample_ids": [s["id"], s["id"]]},
            )

    def test_approve_rejects_samples_of_other_participants(self):
        p1 = self.participant("One")
        p2 = self.participant("Two")
        c1 = self.active_consent(p1["id"])
        c2 = self.active_consent(p2["id"])
        own = self.open_samples(p1["id"], c1["id"], ["B-1"])[0]
        foreign = self.open_samples(p2["id"], c2["id"], ["X-1"])[0]
        withdrawal = self.service.create(
            ADMIN,
            "withdrawal",
            {"participant_id": p1["id"], "requested_at": "2026-03-01"},
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                ADMIN,
                withdrawal["id"],
                "approve",
                {"reason": "r", "sample_ids": [own["id"], foreign["id"]]},
            )
        self.assertIn("another participant", str(ctx.exception))
        # Nothing moved.
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "requested")

    def test_approve_rejects_unknown_sample(self):
        p = self.participant()
        withdrawal = self.service.create(
            ADMIN,
            "withdrawal",
            {"participant_id": p["id"], "requested_at": "2026-03-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                ADMIN,
                withdrawal["id"],
                "approve",
                {"reason": "r", "sample_ids": ["missing-id"]},
            )

    def test_approve_requires_exhaustive_open_sample_list(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s1, s2 = self.open_samples(p["id"], consent["id"], ["B-1", "B-2"])
        withdrawal = self.service.create(
            ADMIN,
            "withdrawal",
            {"participant_id": p["id"], "requested_at": "2026-03-01"},
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                ADMIN,
                withdrawal["id"],
                "approve",
                {"reason": "r", "sample_ids": [s1["id"]]},
            )
        self.assertIn("not exhaustive", str(ctx.exception))
        self.assertIn(s2["id"], str(ctx.exception))

    def test_terminated_samples_are_excluded_and_cannot_be_relisted(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s1, s2 = self.open_samples(p["id"], consent["id"], ["B-1", "B-2"])
        self.service.transition(
            ADMIN, s1["id"], "destroy", {"reason": "contaminated"}
        )
        # Listing only the remaining open sample is sufficient.
        withdrawal = self.approved_withdrawal(p["id"], [s2["id"]])
        self.assertEqual(withdrawal["status"], "approved")
        # Re-approving elsewhere with a terminated sample stays rejected.
        other = self.service.create(
            ADMIN,
            "withdrawal",
            {"participant_id": p["id"], "requested_at": "2026-03-03"},
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                ADMIN,
                other["id"],
                "approve",
                {"reason": "r", "sample_ids": [s1["id"], s2["id"]]},
            )
        self.assertIn("already terminated", str(ctx.exception))

    # --- execution-time batch guard -------------------------------------

    def test_execute_blocked_when_sample_is_on_loan(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s1, s2 = self.open_samples(p["id"], consent["id"], ["B-1", "B-2"], True)
        self.service.transition(
            ADMIN,
            s1["id"],
            "loan",
            {"recipient": "lab-x", "purpose": "study", "due_at": "2026-04-01"},
        )
        withdrawal = self.approved_withdrawal(p["id"], [s1["id"], s2["id"]])

        with self.assertRaises(BatchConflictError) as ctx:
            self.execute(withdrawal["id"])
        reasons = ctx.exception.reasons
        self.assertTrue(
            any("on loan" in reason and s1["id"] in reason for reason in reasons),
            reasons,
        )
        # Whole batch is ineffective.
        self.assertEqual(self.service.get(s1["id"])["status"], "on_loan")
        # s2 was deliberately left collected: an unfinished sample that the
        # batch must still leave untouched when blocked.
        self.assertEqual(self.service.get(s2["id"])["status"], "collected")
        self.assertEqual(self.service.get(consent["id"])["status"], "active")
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")
        blocked = [
            row
            for row in self.service.audit_log(withdrawal["id"])
            if row["action"] == "execute_blocked"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertIn("reasons", blocked[0]["detail"])

    def test_execute_blocked_when_version_drifts_after_approval(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s["id"]])
        # Loan then return: status is stored again but the version moved.
        self.service.transition(
            ADMIN,
            s["id"],
            "loan",
            {"recipient": "lab-x", "purpose": "study", "due_at": "2026-04-01"},
        )
        self.service.transition(ADMIN, s["id"], "return", {"note": "back"})

        with self.assertRaises(BatchConflictError) as ctx:
            self.execute(withdrawal["id"])
        self.assertTrue(
            any("version changed" in reason for reason in ctx.exception.reasons)
        )
        self.assertEqual(self.service.get(s["id"])["status"], "stored")

    def test_execute_blocked_when_new_open_sample_appears(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s1 = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s1["id"]])
        s2 = self.open_samples(p["id"], consent["id"], ["B-2"])[0]

        with self.assertRaises(BatchConflictError) as ctx:
            self.execute(withdrawal["id"])
        self.assertTrue(
            any(s2["id"] in reason and "not covered" in reason
                for reason in ctx.exception.reasons),
            ctx.exception.reasons,
        )
        self.assertEqual(self.service.get(s1["id"])["status"], "stored")

    def test_execute_blocked_when_consent_changes(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s["id"]])
        self.service.transition(
            ADMIN, consent["id"], "supersede", {"reason": "new policy"}
        )

        with self.assertRaises(BatchConflictError) as ctx:
            self.execute(withdrawal["id"])
        self.assertTrue(
            any(consent["id"] in reason for reason in ctx.exception.reasons)
        )
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")

    def test_blocked_execution_recovers_after_loan_returned(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s["id"]])
        self.service.transition(
            ADMIN,
            s["id"],
            "loan",
            {"recipient": "lab-x", "purpose": "study", "due_at": "2026-04-01"},
        )
        # Approval snapshot was taken while stored; the loan moves the version,
        # so returning the sample also makes its version drift. This
        # demonstrates that a stale checklist must be re-approved rather than
        # silently executed against the wrong intent.
        self.service.transition(ADMIN, s["id"], "return", {"note": "back"})
        with self.assertRaises(BatchConflictError):
            self.execute(withdrawal["id"])
        # Re-approve with the fresh checklist, then the batch succeeds.
        fresh = self.approved_withdrawal(p["id"], [s["id"]])
        result = self.execute(fresh["id"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(self.service.get(s["id"])["status"], "withdrawn")

    # --- post-execution freeze ------------------------------------------

    def test_withdrawn_samples_cannot_be_loaned_or_restocked(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s["id"]])
        self.execute(withdrawal["id"])

        for action, payload in (
            ("loan", {"recipient": "x", "purpose": "y", "due_at": "2026-04-01"}),
            ("return", {"note": "x"}),
            ("store", {"freezer": "F", "position": "2", "consent_id": consent["id"]}),
            ("destroy", {"reason": "x"}),
            ("anonymize", {"reason": "x"}),
        ):
            with self.assertRaises(InvalidTransition):
                self.service.transition(ADMIN, s["id"], action, payload)
        self.assertEqual(self.service.get(s["id"])["status"], "withdrawn")

    def test_no_consent_can_be_activated_after_execution(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s["id"]])
        self.execute(withdrawal["id"])

        new_consent = self.service.create(
            ADMIN,
            "consent",
            {"participant_id": p["id"], "scope": ["research"]},
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                ADMIN,
                new_consent["id"],
                "activate",
                {"scope": ["research"], "version": "v3", "expires_at": "2099-01-01"},
            )
        self.assertIn("withdrawal", str(ctx.exception))

    def test_execute_cannot_run_twice(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s["id"]])
        self.execute(withdrawal["id"])
        with self.assertRaises(InvalidTransition):
            self.execute(withdrawal["id"])

    def test_execute_still_guards_optimistic_version_of_the_withdrawal(self):
        p = self.participant()
        consent = self.active_consent(p["id"])
        s = self.open_samples(p["id"], consent["id"], ["B-1"])[0]
        withdrawal = self.approved_withdrawal(p["id"], [s["id"]])
        # Wrong expected_version on the withdrawal itself must be rejected.
        with self.assertRaises(ConflictError):
            self.service.transition(
                ADMIN,
                withdrawal["id"],
                "execute",
                {"executed_at": "2026-03-02"},
                expected_version=withdrawal["version"] + 5,
            )
        # Nothing happened.
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")


class WithdrawalHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        static_dir = Path(__file__).resolve().parent.parent / "static"
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), str(static_dir))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _post(self, path, payload):
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "admin-1",
                "X-Role": "admin",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_409_response_lists_all_block_reasons(self):
        p = self.service.create(
            ADMIN, "participant", {"name": "HTTP Participant"}
        )
        consent = self.service.create(
            ADMIN,
            "consent",
            {"participant_id": p["id"], "scope": ["research"]},
        )
        consent = self.service.transition(
            ADMIN,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        s = self.service.create(
            ADMIN,
            "sample",
            {
                "participant_id": p["id"],
                "sample_code": "H-1",
                "collected_at": "2026-01-01",
            },
        )
        self.service.transition(
            ADMIN,
            s["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
        )
        withdrawal = self.service.create(
            ADMIN,
            "withdrawal",
            {"participant_id": p["id"], "requested_at": "2026-03-01"},
        )
        status, body = self._post(
            "/api/entities/%s/actions" % withdrawal["id"],
            {"action": "approve", "data": {"reason": "r", "sample_ids": [s["id"]]}},
        )
        self.assertEqual(status, 200)
        self.service.transition(
            ADMIN,
            s["id"],
            "loan",
            {"recipient": "lab", "purpose": "p", "due_at": "2026-04-01"},
        )
        status, body = self._post(
            "/api/entities/%s/actions" % withdrawal["id"],
            {"action": "execute", "data": {"executed_at": "2026-03-02"}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["type"], "BatchConflictError")
        self.assertTrue(body["reasons"])
        self.assertTrue(
            any("on loan" in reason for reason in body["reasons"]),
            body["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
