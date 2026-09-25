from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# Sample lifecycle boundaries used by withdrawal reconciliation.
SAMPLE_TERMINAL_STATUSES = ("anonymized", "destroyed", "withdrawn")
SAMPLE_OPEN_STATUSES = ("collected", "stored", "on_loan")
# Actions that only make sense inside a batch orchestration; the generic
# single-entity action endpoint must never accept them.
BATCH_ONLY_TRANSITIONS = {("sample", "withdraw")}


def _validate_participant(actor, data, lookup):
    if len(data.get("name", "")) < 2:
        raise ValidationError("participant name is required")


def _validate_consent(actor, data, lookup):
    participant = _find_one(lookup, "participant", "id", data.get("participant_id"))
    if not participant or participant["status"] == "closed":
        raise ValidationError("consent requires an active participant")
    if not data.get("scope"):
        raise ValidationError("consent scope is required")


def _validate_sample_store(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", data.get("consent_id"))
    if not consent or consent["status"] != "active":
        raise ValidationError("storage requires active consent")
    if "research" not in consent["data"].get("scope", []):
        raise ValidationError("consent does not include research use")
    return {"stored_at": "2026-09-24T00:00:00Z"}


def _validate_withdrawal_approve(actor, entity, data, lookup):
    samples = data.get("sample_ids") or []
    if len(set(samples)) != len(samples):
        raise ConflictError("sample_ids contains duplicates")
    for sample_id in samples:
        sample = _find_one(lookup, "sample", "id", sample_id)
        if not sample:
            raise ValidationError("unknown sample: " + str(sample_id))
        if sample["data"].get("participant_id") != entity["data"].get("participant_id"):
            raise ValidationError(
                "sample %s belongs to another participant" % sample_id
            )
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'participant': _validate_participant, 'consent': _validate_consent}
CUSTOM_TRANSITIONS = {('sample', 'store'): _validate_sample_store, ('withdrawal', 'approve'): _validate_withdrawal_approve}


class RuleEngine:
    ALIASES = {'participants': 'participant', 'consents': 'consent', 'samples': 'sample', 'withdrawals': 'withdrawal'}
    INITIAL_STATUS = {'participant': 'registered', 'consent': 'draft', 'sample': 'collected', 'withdrawal': 'requested'}
    TRANSITIONS = {'participant': {'close_participant': (('registered',), 'closed')}, 'consent': {'activate': (('draft',), 'active'), 'supersede': (('active',), 'superseded'), 'withdraw': (('active',), 'withdrawn')}, 'sample': {'store': (('collected',), 'stored'), 'loan': (('stored',), 'on_loan'), 'return': (('on_loan',), 'stored'), 'anonymize': (('stored',), 'anonymized'), 'destroy': (('stored',), 'destroyed'), 'withdraw': (SAMPLE_OPEN_STATUSES, 'withdrawn')}, 'withdrawal': {'approve': (('requested',), 'approved'), 'execute': (('approved',), 'executed')}}
    CREATE_REQUIRED = {'participant': ('name',), 'consent': ('participant_id', 'scope'), 'sample': ('participant_id', 'sample_code', 'collected_at'), 'withdrawal': ('participant_id', 'requested_at')}
    ACTION_REQUIRED = {('consent', 'activate'): ('scope', 'version', 'expires_at'), ('consent', 'supersede'): ('reason',), ('consent', 'withdraw'): ('reason',), ('sample', 'store'): ('freezer', 'position', 'consent_id'), ('sample', 'loan'): ('recipient', 'purpose', 'due_at'), ('sample', 'anonymize'): ('reason',), ('sample', 'destroy'): ('reason',), ('withdrawal', 'approve'): ('reason', 'sample_ids'), ('withdrawal', 'execute'): ('executed_at',)}
    CREATE_ROLES = {'participant': ('admin', 'biobank'), 'consent': ('admin', 'committee'), 'sample': ('admin', 'biobank'), 'withdrawal': ('admin', 'biobank')}
    ROLE_ACTIONS = {'close_participant': ('admin', 'biobank'), 'activate': ('admin', 'committee'), 'supersede': ('admin', 'committee'), 'withdraw': ('admin', 'committee'), 'store': ('admin', 'biobank'), 'loan': ('admin', 'biobank'), 'return': ('admin', 'biobank'), 'anonymize': ('admin', 'biobank'), 'destroy': ('admin', 'biobank'), 'approve': ('admin', 'committee'), 'reconcile': ('admin', 'biobank', 'committee'), 'execute': ('admin', 'biobank')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        if (kind, action) in BATCH_ONLY_TRANSITIONS:
            raise InvalidTransition(
                "%s must be handled through participant withdrawal execution" % action
            )
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    # -- participant withdrawal: reconcile + batch execution --------------

    def validate_withdrawal_request(self, actor, withdrawal, action):
        """Common gate for reconcile/execute on a withdrawal."""
        self._ensure_role(actor, self.ROLE_ACTIONS.get(action, ("admin",)))
        participant_id = withdrawal["data"].get("participant_id")
        if not participant_id:
            raise ValidationError("withdrawal is missing participant_id")
        return participant_id

    def require_fields(self, data, fields):
        self._require(data, fields)

    def build_checklist(self, lookup, participant_id):
        """Authoritative per-participant checklist: every open sample and
        every active consent, never duplicated, never foreign."""
        samples = [
            self._checklist_item(entity)
            for entity in lookup("sample", "participant_id", participant_id)
            if entity["status"] in SAMPLE_OPEN_STATUSES
        ]
        consents = [
            self._checklist_item(entity)
            for entity in lookup("consent", "participant_id", participant_id)
            if entity["status"] == "active"
        ]
        return {"samples": samples, "consents": consents}

    @staticmethod
    def _checklist_item(entity):
        return {
            "id": entity["id"],
            "status": entity["status"],
            "version": entity["version"],
        }

    def review_execution(self, withdrawal, participant, checklist, data):
        """Validate the checklist the operator submits against the current
        state. Returns (samples, consents) targets or a list of blocking
        reasons. Any reason means the whole batch must not take effect."""
        reasons = []
        participant_id = withdrawal["data"].get("participant_id")
        submitted_samples = self._submitted_targets(data.get("samples"), "sample", reasons)
        submitted_consents = self._submitted_targets(data.get("consents"), "consent", reasons)

        if participant and participant["status"] == "closed":
            reasons.append(self._reason(
                "PARTICIPANT_CLOSED",
                "participant is closed",
                {"participant_id": participant_id},
            ))

        for item in submitted_samples:
            sample_id = item["id"]
            # Submitted id not present in the authoritative checklist.
            if sample_id not in {row["id"] for row in checklist["samples"]}:
                reasons.append(self._reason(
                    "SAMPLE_FOREIGN_OR_TERMINAL",
                    "sample %s is not an open sample of this participant" % sample_id,
                    {"sample_id": sample_id},
                ))

        for item in submitted_consents:
            consent_id = item["id"]
            if consent_id not in {row["id"] for row in checklist["consents"]}:
                reasons.append(self._reason(
                    "CONSENT_FOREIGN_OR_INACTIVE",
                    "consent %s is not an active consent of this participant" % consent_id,
                    {"consent_id": consent_id},
                ))

        current_samples = {row["id"]: row for row in checklist["samples"]}
        current_consents = {row["id"]: row for row in checklist["consents"]}

        for item in submitted_samples:
            current = current_samples.get(item["id"])
            if not current:
                continue
            if int(item["version"]) != int(current["version"]):
                reasons.append(self._reason(
                    "SAMPLE_VERSION_CHANGED",
                    "sample %s version changed: expected %s, found %s"
                    % (item["id"], current["version"], item["version"]),
                    {"sample_id": item["id"], "expected": current["version"], "submitted": item["version"]},
                ))
            if current["status"] == "on_loan":
                reasons.append(self._reason(
                    "SAMPLE_ON_LOAN",
                    "sample %s is on loan and cannot be withdrawn" % item["id"],
                    {"sample_id": item["id"]},
                ))

        for item in submitted_consents:
            current = current_consents.get(item["id"])
            if not current:
                continue
            if int(item["version"]) != int(current["version"]):
                reasons.append(self._reason(
                    "CONSENT_VERSION_CHANGED",
                    "consent %s version changed: expected %s, found %s"
                    % (item["id"], current["version"], item["version"]),
                    {"consent_id": item["id"], "expected": current["version"], "submitted": item["version"]},
                ))

        # Coverage: every open sample / active consent must be submitted once.
        for current in checklist["samples"]:
            matches = [item for item in submitted_samples if item["id"] == current["id"]]
            if not matches:
                reasons.append(self._reason(
                    "SAMPLE_NOT_COVERED",
                    "open sample %s is missing from the checklist" % current["id"],
                    {"sample_id": current["id"]},
                ))
        for current in checklist["consents"]:
            matches = [item for item in submitted_consents if item["id"] == current["id"]]
            if not matches:
                reasons.append(self._reason(
                    "CONSENT_NOT_COVERED",
                    "active consent %s is missing from the checklist" % current["id"],
                    {"consent_id": current["id"]},
                ))

        return reasons, submitted_samples, submitted_consents

    @staticmethod
    def _submitted_targets(values, kind, reasons):
        targets = []
        seen = set()
        if not isinstance(values, list):
            reasons.append(RuleEngine._reason(
                "%sS_INVALID" % kind.upper(),
                "%s checklist must be a list" % kind,
            ))
            return targets
        for value in values:
            if not isinstance(value, dict) or not value.get("id") or value.get("version") is None:
                reasons.append(RuleEngine._reason(
                    "%sS_INVALID" % kind.upper(),
                    "each %s entry requires id and version" % kind,
                    {"entry": value},
                ))
                continue
            target_id = str(value["id"])
            if target_id in seen:
                reasons.append(RuleEngine._reason(
                    "%s_DUPLICATE" % kind.upper(),
                    "%s listed more than once: %s" % (kind, target_id),
                    {"%s_id" % kind: target_id},
                ))
                continue
            try:
                version = int(value["version"])
            except (TypeError, ValueError):
                reasons.append(RuleEngine._reason(
                    "%sS_INVALID" % kind.upper(),
                    "%s version must be an integer: %s" % (kind, target_id),
                    {"%s_id" % kind: target_id},
                ))
                continue
            seen.add(target_id)
            targets.append({"id": target_id, "version": version})
        return targets

    @staticmethod
    def _reason(code, message, detail=None):
        reason = {"code": code, "message": message}
        if detail:
            reason["detail"] = detail
        return reason


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
