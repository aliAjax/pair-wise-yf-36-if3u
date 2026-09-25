from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    BatchBlockedError,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    ValidationError,
)
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "withdrawal" and action == "reconcile":
            return self.reconcile_withdrawal(actor, entity_id)
        if kind == "withdrawal" and action == "execute":
            return self.execute_withdrawal(actor, entity_id, data or {}, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    # -- participant withdrawal ------------------------------------------

    def _load_withdrawal(self, entity_id):
        withdrawal = self.repository.get_entity(entity_id)
        if not withdrawal:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(withdrawal["kind"]) != "withdrawal":
            raise InvalidTransition("entity %s is not a withdrawal" % entity_id)
        return withdrawal

    @staticmethod
    def _require_approved(withdrawal):
        if withdrawal["status"] != "approved":
            raise InvalidTransition(
                "cannot act on withdrawal from status %s" % withdrawal["status"]
            )

    def reconcile_withdrawal(self, actor, entity_id):
        """Per-participant check: produce the authoritative checklist of
        every open sample and active consent. Does not change any state but
        leaves an audit entry for the check."""
        withdrawal = self._load_withdrawal(entity_id)
        participant_id = self.rules.validate_withdrawal_request(actor, withdrawal, "reconcile")
        self._require_approved(withdrawal)
        participant = self.repository.get_entity(participant_id)
        if not participant:
            raise ValidationError("participant not found: " + participant_id)
        checklist = self.rules.build_checklist(self._lookup, participant_id)
        self.audit.record(
            entity_id,
            actor,
            "reconcile",
            withdrawal["status"],
            withdrawal["status"],
            {"participant_id": participant_id, "checklist": checklist},
        )
        return {
            "withdrawal_id": entity_id,
            "participant_id": participant_id,
            "status": withdrawal["status"],
            "checklist": checklist,
        }

    def execute_withdrawal(self, actor, entity_id, data, expected_version=None):
        """Terminate the approved consent(s) and every open sample for one
        participant as a single batch. Any loaned sample, stale version or
        checklist mismatch blocks the whole batch."""
        withdrawal = self._load_withdrawal(entity_id)
        participant_id = self.rules.validate_withdrawal_request(actor, withdrawal, "execute")
        self._require_approved(withdrawal)
        self.rules.require_fields(
            data, self.rules.ACTION_REQUIRED[("withdrawal", "execute")]
        )
        payload = dict(data)
        executed_at = payload["executed_at"]
        # Empty lists are valid input (a participant may have nothing open);
        # coverage review then decides whether that matches reality.
        if "samples" not in payload or "consents" not in payload:
            raise ValidationError("missing required field: samples/consents")

        participant = self.repository.get_entity(participant_id)
        if not participant:
            raise ValidationError("participant not found: " + participant_id)

        checklist = self.rules.build_checklist(self._lookup, participant_id)

        reasons, target_samples, target_consents = self.rules.review_execution(
            withdrawal, participant, checklist, payload
        )

        if expected_version is not None and int(expected_version) != int(withdrawal["version"]):
            reasons.append(self.rules._reason(
                "WITHDRAWAL_VERSION_CHANGED",
                "withdrawal version changed: expected %s, found %s"
                % (expected_version, withdrawal["version"]),
                {"withdrawal_id": entity_id, "expected": int(expected_version),
                 "found": withdrawal["version"]},
            ))

        if not self._has_reconcile_audit(entity_id):
            reasons.append(self.rules._reason(
                "MISSING_RECONCILIATION",
                "reconcile the participant checklist before execution",
                {"withdrawal_id": entity_id},
            ))

        if reasons:
            self._block_execution(actor, withdrawal, reasons)

        updates = []
        audits = []
        terminated_samples = []
        terminated_consents = []
        for target in target_consents:
            consent = self.repository.get_entity(target["id"])
            patch = {
                "withdrawn_at": executed_at,
                "withdrawn_by": actor.user_id,
                "withdrawal_id": entity_id,
            }
            merged = dict(consent["data"])
            merged.update(patch)
            updates.append(
                (consent["id"], consent["version"], ("active",), "withdrawn", merged)
            )
            audits.append((
                consent["id"], actor.user_id, actor.role, "withdraw",
                consent["status"], "withdrawn",
                {"withdrawal_id": entity_id, "patch": patch},
            ))
            terminated_consents.append(consent["id"])

        for target in target_samples:
            sample = self.repository.get_entity(target["id"])
            patch = {
                "withdrawn_at": executed_at,
                "withdrawn_by": actor.user_id,
                "withdrawal_id": entity_id,
            }
            merged = dict(sample["data"])
            merged.update(patch)
            updates.append(
                (sample["id"], sample["version"],
                 ("collected", "stored", "on_loan"), "withdrawn", merged)
            )
            audits.append((
                sample["id"], actor.user_id, actor.role, "withdraw",
                sample["status"], "withdrawn",
                {"withdrawal_id": entity_id, "patch": patch},
            ))
            terminated_samples.append(sample["id"])

        executed_patch = {
            "executed_at": executed_at,
            "executed_by": actor.user_id,
            "sample_ids": terminated_samples,
            "consent_ids": terminated_consents,
        }
        merged_withdrawal = dict(withdrawal["data"])
        merged_withdrawal.update(executed_patch)
        updates.append(
            (entity_id, withdrawal["version"], ("approved",), "executed", merged_withdrawal)
        )
        audits.append((
            entity_id, actor.user_id, actor.role, "execute", "approved", "executed",
            {"patch": executed_patch},
        ))

        try:
            self.repository.apply_batch(updates, audits)
        except ConflictError:
            # State changed between reconcile and commit: re-check against
            # fresh state and report the concrete reasons. Nothing changed.
            fresh_withdrawal = self.repository.get_entity(entity_id)
            fresh_reasons = []
            if fresh_withdrawal["status"] != "approved":
                fresh_reasons.append(self.rules._reason(
                    "WITHDRAWAL_NOT_APPROVED",
                    "withdrawal is no longer approved (status %s)"
                    % fresh_withdrawal["status"],
                    {"withdrawal_id": entity_id, "status": fresh_withdrawal["status"]},
                ))
            fresh_checklist = self.rules.build_checklist(self._lookup, participant_id)
            more_reasons, _, _ = self.rules.review_execution(
                fresh_withdrawal, participant, fresh_checklist, payload
            )
            fresh_reasons.extend(more_reasons)
            if not fresh_reasons:
                fresh_reasons.append(self.rules._reason(
                    "BATCH_CONFLICT",
                    "state changed during execution; reconcile and retry",
                    {"withdrawal_id": entity_id},
                ))
            self._block_execution(actor, withdrawal, fresh_reasons)

        return {
            "withdrawal_id": entity_id,
            "participant_id": participant_id,
            "status": "executed",
            "executed_at": executed_at,
            "terminated_consent_ids": terminated_consents,
            "terminated_sample_ids": terminated_samples,
        }

    def _block_execution(self, actor, withdrawal, reasons):
        self.audit.record(
            withdrawal["id"],
            actor,
            "execute_blocked",
            withdrawal["status"],
            withdrawal["status"],
            {"reasons": reasons},
        )
        raise BatchBlockedError(reasons)

    def _has_reconcile_audit(self, entity_id):
        return any(
            entry["action"] == "reconcile"
            for entry in self.repository.list_audit(entity_id=entity_id)
        )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
