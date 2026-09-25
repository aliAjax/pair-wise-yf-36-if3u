from .domain import (
    BatchConflictError,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# Sample statuses that belong to a participant's unfinished lifecycle and must
# therefore be covered (and terminated) when a withdrawal is executed.
SAMPLE_OPEN_STATUSES = ("collected", "stored", "on_loan")
SAMPLE_TERMINAL_STATUSES = ("anonymized", "destroyed", "withdrawn")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_participant(actor, data, lookup):
    if len(data.get("name", "")) < 2:
        raise ValidationError("participant name is required")


def _validate_consent(actor, data, lookup):
    participant = _find_one(lookup, "participant", "id", data.get("participant_id"))
    if not participant or participant["status"] == "closed":
        raise ValidationError("consent requires an active participant")
    if not data.get("scope"):
        raise ValidationError("consent scope is required")


def _participant_has_executed_withdrawal(lookup, participant_id):
    return any(
        withdrawal["status"] == "executed"
        for withdrawal in (lookup("withdrawal", "participant_id", participant_id) or [])
    )


def _validate_sample_store(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", data.get("consent_id"))
    if not consent or consent["status"] != "active":
        raise ValidationError("storage requires active consent")
    if "research" not in consent["data"].get("scope", []):
        raise ValidationError("consent does not include research use")
    return {"stored_at": "2026-09-24T00:00:00Z"}


def _validate_consent_activate(actor, entity, data, lookup):
    participant_id = entity["data"].get("participant_id")
    if participant_id and _participant_has_executed_withdrawal(lookup, participant_id):
        raise ValidationError(
            "cannot activate consent after participant withdrawal has been executed"
        )
    return {}


def _participant_manifest(lookup, participant_id):
    """Build the authoritative per-participant checklist.

    Open samples: every sample of the participant that is not in a terminal
    state. Active consents: every consent that would still govern samples.
    Every item carries its current version so execution can detect any change
    that happened after committee approval.
    """
    open_samples = sorted(
        (
            {
                "id": sample["id"],
                "status": sample["status"],
                "version": sample["version"],
            }
            for sample in (lookup("sample", "participant_id", participant_id) or [])
            if sample["status"] in SAMPLE_OPEN_STATUSES
        ),
        key=lambda item: item["id"],
    )
    active_consents = sorted(
        (
            {
                "id": consent["id"],
                "status": consent["status"],
                "version": consent["version"],
            }
            for consent in (lookup("consent", "participant_id", participant_id) or [])
            if consent["status"] == "active"
        ),
        key=lambda item: item["id"],
    )
    return {"samples": open_samples, "active_consents": active_consents}


def _validate_withdrawal_approve(actor, entity, data, lookup):
    sample_ids = data.get("sample_ids") or []
    participant_id = entity["data"].get("participant_id")

    participant = _find_one(lookup, "participant", "id", participant_id)
    if not participant:
        raise ValidationError("withdrawal participant no longer exists")

    if len(set(sample_ids)) != len(sample_ids):
        raise ConflictError("sample_ids contains duplicates")

    manifest = _participant_manifest(lookup, participant_id)
    open_by_id = {item["id"]: item for item in manifest["samples"]}
    open_ids = set(open_by_id)

    for sample_id in sample_ids:
        sample = _find_one(lookup, "sample", "id", sample_id)
        if not sample:
            raise ValidationError("unknown sample: " + str(sample_id))
        if sample["data"].get("participant_id") != participant_id:
            raise ValidationError(
                "sample %s belongs to another participant" % sample_id
            )
        if sample_id not in open_ids:
            raise ValidationError(
                "sample %s is already terminated (%s)"
                % (sample_id, sample["status"])
            )

    listed = set(sample_ids)
    missing = sorted(open_ids - listed)
    if missing:
        raise ValidationError(
            "sample list is not exhaustive; missing open samples: "
            + ", ".join(missing)
        )

    return {
        "approved_by": actor.user_id,
        # Frozen snapshot: execution trusts only this list, never live queries.
        "manifest": manifest,
    }


CUSTOM_CREATE = {
    "participant": _validate_participant,
    "consent": _validate_consent,
}
CUSTOM_TRANSITIONS = {
    ("sample", "store"): _validate_sample_store,
    ("consent", "activate"): _validate_consent_activate,
    ("withdrawal", "approve"): _validate_withdrawal_approve,
}


class RuleEngine:
    ALIASES = {'participants': 'participant', 'consents': 'consent', 'samples': 'sample', 'withdrawals': 'withdrawal'}
    INITIAL_STATUS = {'participant': 'registered', 'consent': 'draft', 'sample': 'collected', 'withdrawal': 'requested'}
    # ``withdrawn`` is a terminal sample status with no outgoing transitions;
    # it can only be reached by the withdrawal batch execution, never a
    # per-sample action, so withdrawn samples can never be loaned or restocked.
    TRANSITIONS = {'participant': {'close_participant': (('registered',), 'closed')}, 'consent': {'activate': (('draft',), 'active'), 'supersede': (('active',), 'superseded'), 'withdraw': (('active',), 'withdrawn')}, 'sample': {'store': (('collected',), 'stored'), 'loan': (('stored',), 'on_loan'), 'return': (('on_loan',), 'stored'), 'anonymize': (('stored',), 'anonymized'), 'destroy': (('stored',), 'destroyed')}, 'withdrawal': {'approve': (('requested',), 'approved'), 'execute': (('approved',), 'executed')}}
    CREATE_REQUIRED = {'participant': ('name',), 'consent': ('participant_id', 'scope'), 'sample': ('participant_id', 'sample_code', 'collected_at'), 'withdrawal': ('participant_id', 'requested_at')}
    ACTION_REQUIRED = {('consent', 'activate'): ('scope', 'version', 'expires_at'), ('consent', 'supersede'): ('reason',), ('consent', 'withdraw'): ('reason',), ('sample', 'store'): ('freezer', 'position', 'consent_id'), ('sample', 'loan'): ('recipient', 'purpose', 'due_at'), ('sample', 'anonymize'): ('reason',), ('sample', 'destroy'): ('reason',), ('withdrawal', 'approve'): ('reason', 'sample_ids'), ('withdrawal', 'execute'): ('executed_at',)}
    CREATE_ROLES = {'participant': ('admin', 'biobank'), 'consent': ('admin', 'committee'), 'sample': ('admin', 'biobank'), 'withdrawal': ('admin', 'biobank')}
    ROLE_ACTIONS = {'close_participant': ('admin', 'biobank'), 'activate': ('admin', 'committee'), 'supersede': ('admin', 'committee'), 'withdraw': ('admin', 'committee'), 'store': ('admin', 'biobank'), 'loan': ('admin', 'biobank'), 'return': ('admin', 'biobank'), 'anonymize': ('admin', 'biobank'), 'destroy': ('admin', 'biobank'), 'approve': ('admin', 'committee'), 'execute': ('admin', 'biobank')}

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

    def plan_withdrawal_execution(self, actor, withdrawal, live_entities, executed_at):
        """Re-check one participant's whole batch and plan the single commit.

        The approved manifest is the only trusted checklist. Live rows must
        still match it exactly: every listed sample must belong to the same
        participant, be at the same version and still be unfinished; coverage
        must match the participant's current open samples; every active
        consent must belong to the manifest at the same version.

        Returns ``(operations, audit_entries)`` where each operation is
        ``(entity_id, expected_version, status, data)``. If any check fails a
        BatchConflictError carries *all* reasons and nothing is returned.
        """
        data = withdrawal["data"]
        participant_id = data.get("participant_id")
        manifest = data.get("manifest")
        if not manifest or "samples" not in manifest or "active_consents" not in manifest:
            raise InvalidTransition(
                "approved withdrawal is missing its manifest; re-approval is required"
            )

        reasons = []
        sample_specs = sorted(manifest["samples"], key=lambda item: item["id"])
        consent_specs = sorted(manifest["active_consents"], key=lambda item: item["id"])
        listed_ids = {item["id"] for item in sample_specs}

        # Re-validate each frozen sample against the live row. The on-loan
        # check is deliberately independent of version drift: an item approved
        # while already on loan must also be refused on its live status.
        for spec in sample_specs:
            sample = live_entities.get(("sample", spec["id"]))
            if not sample:
                reasons.append("sample %s no longer exists" % spec["id"])
                continue
            if sample["data"].get("participant_id") != participant_id:
                reasons.append(
                    "sample %s now belongs to another participant" % spec["id"]
                )
            if sample["version"] != spec["version"]:
                reasons.append(
                    "sample %s version changed (approved v%s, now v%s)"
                    % (spec["id"], spec["version"], sample["version"])
                )
            elif sample["status"] != spec["status"]:
                reasons.append(
                    "sample %s status changed from %s to %s"
                    % (spec["id"], spec["status"], sample["status"])
                )
            if sample["status"] == "on_loan":
                reasons.append(
                    "sample %s is currently on loan; it must be returned first"
                    % spec["id"]
                )

        # Coverage: no new un-terminated sample may exist, and a listed sample
        # that was terminated out-of-band must also block the stale manifest.
        for sample in live_entities.get("samples", []):
            if sample["data"].get("participant_id") != participant_id:
                continue
            if sample["status"] in SAMPLE_OPEN_STATUSES and sample["id"] not in listed_ids:
                reasons.append(
                    "sample %s is open but not covered by the approved checklist"
                    % sample["id"]
                )
            if sample["id"] in listed_ids and sample["status"] in SAMPLE_TERMINAL_STATUSES:
                reasons.append(
                    "sample %s was already terminated (%s) after approval"
                    % (sample["id"], sample["status"])
                )

        # Consents: every still-active consent must be the frozen one at the
        # frozen version, and all frozen ones must still be active.
        for spec in consent_specs:
            consent = live_entities.get(("consent", spec["id"]))
            if not consent:
                reasons.append("consent %s no longer exists" % spec["id"])
                continue
            if consent["data"].get("participant_id") != participant_id:
                reasons.append(
                    "consent %s now belongs to another participant" % spec["id"]
                )
            if consent["version"] != spec["version"]:
                reasons.append(
                    "consent %s version changed (approved v%s, now v%s)"
                    % (spec["id"], spec["version"], consent["version"])
                )
            elif consent["status"] != "active":
                reasons.append(
                    "consent %s status changed from active to %s"
                    % (spec["id"], consent["status"])
                )
        frozen_consent_ids = {item["id"] for item in consent_specs}
        for consent in live_entities.get("consents", []):
            if consent["data"].get("participant_id") != participant_id:
                continue
            if consent["status"] == "active" and consent["id"] not in frozen_consent_ids:
                reasons.append(
                    "consent %s became active after approval and is not covered"
                    % consent["id"]
                )

        if reasons:
            raise BatchConflictError(sorted(set(reasons)))

        reason_text = data.get("reason") or "participant withdrawal"
        operations = []
        audit_entries = []
        withdrawn_at = executed_at
        for spec in sample_specs:
            sample = live_entities[("sample", spec["id"])]
            new_data = dict(sample["data"])
            new_data["withdrawn_at"] = withdrawn_at
            new_data["withdrawal_id"] = withdrawal["id"]
            operations.append(
                (sample["id"], sample["version"], "withdrawn", new_data)
            )
            audit_entries.append(
                {
                    "entity_id": sample["id"],
                    "action": "withdraw",
                    "from_status": sample["status"],
                    "to_status": "withdrawn",
                    "detail": {
                        "withdrawal_id": withdrawal["id"],
                        "reason": reason_text,
                    },
                }
            )
        for spec in consent_specs:
            consent = live_entities[("consent", spec["id"])]
            new_data = dict(consent["data"])
            new_data["withdrawn_at"] = withdrawn_at
            new_data["withdrawal_id"] = withdrawal["id"]
            operations.append(
                (consent["id"], consent["version"], "withdrawn", new_data)
            )
            audit_entries.append(
                {
                    "entity_id": consent["id"],
                    "action": "withdraw",
                    "from_status": "active",
                    "to_status": "withdrawn",
                    "detail": {
                        "withdrawal_id": withdrawal["id"],
                        "reason": reason_text,
                    },
                }
            )

        new_withdrawal_data = dict(data)
        new_withdrawal_data["executed_at"] = executed_at
        new_withdrawal_data["executed_by"] = actor.user_id
        new_withdrawal_data["sample_ids"] = [item["id"] for item in sample_specs]
        operations.append(
            (withdrawal["id"], withdrawal["version"], "executed", new_withdrawal_data)
        )
        audit_entries.append(
            {
                "entity_id": withdrawal["id"],
                "action": "execute",
                "from_status": "approved",
                "to_status": "executed",
                "detail": {
                    "executed_at": executed_at,
                    "sample_count": len(sample_specs),
                    "consent_count": len(consent_specs),
                    "sample_ids": [item["id"] for item in sample_specs],
                    "consent_ids": [item["id"] for item in consent_specs],
                },
            }
        )
        return operations, audit_entries
