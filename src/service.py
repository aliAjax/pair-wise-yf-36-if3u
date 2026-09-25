from uuid import uuid4

from .audit import AuditTrail
from .domain import BatchConflictError, ConflictError, InvalidTransition, NotFoundError
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
        if kind == "withdrawal" and action == "execute":
            return self._execute_withdrawal(
                actor, entity, dict(data or {}), expected_version
            )
        return self._apply_single_transition(actor, entity, action, data, expected_version)

    def _apply_single_transition(self, actor, entity, action, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _execute_withdrawal(self, actor, withdrawal, data, expected_version):
        # Permission/status/required-field checks run through the state machine
        # first; the batch effects are planned and committed atomically below.
        self.rules.validate_transition(actor, withdrawal, "execute", data, self._lookup)
        if expected_version is not None and withdrawal["version"] != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, withdrawal["version"])
            )

        participant_id = withdrawal["data"].get("participant_id")
        live_entities = {
            "samples": self.repository.list_entities(kind="sample"),
            "consents": self.repository.list_entities(kind="consent"),
        }
        for item in live_entities["samples"]:
            live_entities[("sample", item["id"])] = item
        for item in live_entities["consents"]:
            live_entities[("consent", item["id"])] = item

        try:
            operations, audit_entries = self.rules.plan_withdrawal_execution(
                actor, withdrawal, live_entities, data["executed_at"]
            )
        except BatchConflictError as exc:
            # A blocked attempt still leaves an audit trail; nothing is changed.
            self._record_blocked(actor, withdrawal, exc.reasons)
            raise

        for entry in audit_entries:
            entry["actor_id"] = actor.user_id
            entry["actor_role"] = actor.role
        try:
            self.repository.update_batch(operations, audit_entries)
        except (ConflictError, NotFoundError) as exc:
            # Lost race against a loan/version change between check and commit:
            # the whole batch is already rolled back; record why for audit.
            self._record_blocked(actor, withdrawal, [str(exc)])
            raise

        return self.repository.get_entity(withdrawal["id"])

    def _record_blocked(self, actor, withdrawal, reasons):
        self.audit.record(
            withdrawal["id"],
            actor,
            "execute_blocked",
            withdrawal["status"],
            withdrawal["status"],
            {"reasons": sorted(set(str(reason) for reason in reasons))},
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
