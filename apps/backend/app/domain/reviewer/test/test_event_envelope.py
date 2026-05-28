"""JSON-safety of reviewer domain event payloads.

`_DomainEventEnvelope` has been removed — reviewer events now route to the
SSE bus directly via `core/sse.publish_general_after_commit` (using
`_json_safe` + `asdict`), and to the audit log via `FindingAuditPayload`.

The `_json_safe` coercion is exercised through the `dispatch_events` path
which is covered by `test_dispatch_events_service.py`.  The audit payload
shape is covered by `test_dispatch_audits_service.py`.

This file is intentionally empty — it exists to mark that the old envelope
tests were removed when the envelope class was deleted.
"""
