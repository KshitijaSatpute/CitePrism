"""Hash-chain and event-to-record integrity checks."""
from src.trust.canonical import sha256_json
from src.trust.ids import new_uuid7, utc_now
from src.trust.models import AuditEvent, AuditVerification

ZERO_HASH = "0" * 64


def audit_event_body(event):
    # Python mode retains enums/datetimes, which canonical_json handles consistently.
    return event.model_dump(mode="json", exclude={"event_hash"})


def build_audit_event(*, tenant_id, sequence_number, previous_hash, actor,
                      action, aggregate_type, aggregate_id, payload):
    event = AuditEvent(id=new_uuid7(), tenant_id=tenant_id,
                       sequence_number=sequence_number, occurred_at=utc_now(), actor=actor,
                       action=action, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
                       payload=payload, previous_hash=previous_hash, event_hash=ZERO_HASH)
    return event.model_copy(update={"event_hash": sha256_json(audit_event_body(event))})


def verify_audit_chain(events, *, tenant_id=None, records=None):
    events = sorted(events, key=lambda e: e.sequence_number)
    head = events[-1].event_hash if events else None

    def fail(error):
        return AuditVerification(valid=False, event_count=len(events), head_hash=head, error=error)

    tenant_id = tenant_id or (events[0].tenant_id if events else None)
    previous = ZERO_HASH
    bindings = {}
    for number, event in enumerate(events, start=1):
        if event.tenant_id != tenant_id:
            return fail(f"tenant mismatch at sequence {number}")
        if event.sequence_number != number:
            return fail(f"sequence gap at {number}")
        if event.previous_hash != previous:
            return fail(f"previous hash mismatch at {number}")
        if event.event_hash != sha256_json(audit_event_body(event)):
            return fail(f"event hash mismatch at {number}")
        key = (event.aggregate_type, event.aggregate_id)
        digest = event.payload.get("record_hash")
        if digest is None:
            return fail(f"missing record binding at {number}")
        if key in bindings:
            return fail(f"duplicate creation event for {key}")
        bindings[key] = digest
        previous = event.event_hash
    if records is not None:
        if bindings.keys() != records.keys():
            return fail("missing entity or unaudited entity in snapshot")
        for key, (record, stored_digest) in records.items():
            digest = sha256_json(record)
            if record.tenant_id != tenant_id:
                return fail(f"record tenant mismatch for {key}")
            if digest != stored_digest or digest != bindings[key]:
                return fail(f"record/audit digest mismatch for {key}")
    return AuditVerification(valid=True, event_count=len(events), head_hash=head)
