"""Drift-detection: openapi/agent-api.yaml ↔ hand-written Pydantic mirror.

The OpenAPI spec at `apps/backend/openapi/agent-api.yaml` is the
contract; `app/core/agent_gateway/types.py` is the hand-written Pydantic
mirror today (codegen automation is a named follow-on). The two have
drifted before — someone adds a field to the spec, forgets to mirror it
(or vice versa), and we only notice when the wire breaks.

This test walks every schema in the spec that has a Python mirror, fully
resolves `$ref` + `allOf` composition, and asserts every YAML property
name appears as a Pydantic field on the matching class. Field-type
checking is intentionally light — name presence catches 90% of drift
(rename, add, remove) with minimal maintenance cost.

What gets checked:
    YAML schema name → Python class (see `_SCHEMA_TO_CLASS`).

What doesn't:
    - HTTP-level shapes the FastAPI layer handles natively
      (`ErrorEnvelope` → `HTTPException`, `AgentCommand` union →
      discriminator handled by Pydantic's `Annotated[Union, Discriminator]`).
    - Field types (`string` vs. `str`, `array` vs. `tuple`). Easier to
      maintain a name-only check than chase pydantic-type-coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.agent_gateway import types as gateway_types

# ── Configuration ──────────────────────────────────────────────────────


_SPEC_PATH = Path(__file__).resolve().parents[4] / "openapi" / "agent-api.yaml"


# YAML schema → (Python class name, optional[set of fields to skip])
# Skipping is for fields that are spec-only (e.g. computed-on-wire).
_SCHEMA_TO_CLASS: dict[str, tuple[str, set[str]]] = {
    "IdentityExchangeRequest": ("IdentityExchangeRequest", set()),
    "IdentityExchangeResponse": ("IdentityExchangeResponse", set()),
    "HeartbeatRequest": ("HeartbeatRequest", set()),
    "HeartbeatWorkspaceEntry": ("HeartbeatWorkspaceEntry", set()),
    "HeartbeatResponse": ("HeartbeatResponse", set()),
    "ClaimRequest": ("ClaimRequest", set()),
    "CommandBase": ("_CommandBase", set()),
    "CreateWorkspaceCommand": ("CreateWorkspaceCommand", set()),
    "WriteFilesCommand": ("WriteFilesCommand", set()),
    "RefreshWorkspaceAuthCommand": ("RefreshWorkspaceAuthCommand", set()),
    "InvokeClaudeCodeCommand": ("InvokeClaudeCodeCommand", set()),
    "CleanupWorkspaceCommand": ("CleanupWorkspaceCommand", set()),
    "AgentConfig": ("AgentConfig", set()),
    "ConfigUpdateCommand": ("ConfigUpdateCommand", set()),
    "AgentEvent": ("AgentEvent", set()),
    "AgentMetadata": ("AgentMetadata", set()),
    "WorkspaceEvent": ("WorkspaceEvent", set()),
}

# YAML schemas intentionally skipped — see module docstring.
_SKIPPED = {"ErrorEnvelope", "AgentCommand"}


# ── Helpers ────────────────────────────────────────────────────────────


def _load_spec() -> dict[str, Any]:
    with _SPEC_PATH.open() as fh:
        return yaml.safe_load(fh)


def _resolve_schema(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve allOf composition + $ref into a flat property dict.

    Returns `{property_name: property_schema}` with all parent properties
    merged. Doesn't recurse into nested object properties — caller is
    only checking top-level field names on the Python class.
    """
    out: dict[str, Any] = {}
    if "allOf" in schema:
        for part in schema["allOf"]:
            if "$ref" in part:
                ref = part["$ref"].rsplit("/", 1)[-1]
                resolved = spec["components"]["schemas"][ref]
                out.update(_resolve_schema(spec, resolved))
            else:
                out.update(_resolve_schema(spec, part))
    if "properties" in schema:
        out.update(schema["properties"])
    return out


def _pydantic_field_names(cls: type) -> set[str]:
    """Return the set of declared field names on a Pydantic model."""
    return set(cls.model_fields.keys())


# ── Tests ──────────────────────────────────────────────────────────────


def test_every_schema_in_spec_has_a_known_handler() -> None:
    """If the spec gains a new top-level schema, the test config must say
    how to treat it (mirror class name or explicit skip). Catches the
    silent-addition case."""
    spec = _load_spec()
    schemas_in_spec = set(spec["components"]["schemas"].keys())
    known = set(_SCHEMA_TO_CLASS.keys()) | _SKIPPED
    unhandled = schemas_in_spec - known
    assert not unhandled, (
        f"OpenAPI spec has new schemas the drift test doesn't know about: {sorted(unhandled)}. "
        f"Either add them to `_SCHEMA_TO_CLASS` (with a Pydantic mirror) or add to `_SKIPPED`."
    )


@pytest.mark.parametrize(
    "schema_name, class_name, skip_fields",
    [(s, c, sf) for s, (c, sf) in _SCHEMA_TO_CLASS.items()],
)
def test_pydantic_mirror_has_every_yaml_property(
    schema_name: str, class_name: str, skip_fields: set[str]
) -> None:
    """Every property in the YAML schema must appear as a field on the
    Python mirror class. Tolerates extra Python-only fields (those are
    deliberate convenience accessors)."""
    spec = _load_spec()
    raw = spec["components"]["schemas"][schema_name]
    props = _resolve_schema(spec, raw)

    cls = getattr(gateway_types, class_name, None)
    assert cls is not None, f"Pydantic class {class_name} missing from types.py"
    py_fields = _pydantic_field_names(cls)

    yaml_fields = set(props.keys()) - skip_fields
    missing = yaml_fields - py_fields
    assert not missing, (
        f"YAML schema {schema_name} declares properties {sorted(missing)} "
        f"that are NOT mirrored on Pydantic class {class_name}. "
        f"Update types.py to match the spec (or add to _SCHEMA_TO_CLASS's skip set if "
        f"deliberate)."
    )


def test_command_kinds_match_discriminator_mapping() -> None:
    """The AgentCommand union's discriminator mapping in the spec must
    exactly match the AgentCommandKind StrEnum on the Python side."""
    spec = _load_spec()
    mapping = spec["components"]["schemas"]["AgentCommand"]["discriminator"]["mapping"]
    yaml_kinds = set(mapping.keys())
    py_kinds = {v.value for v in gateway_types.AgentCommandKind}
    assert yaml_kinds == py_kinds, (
        f"AgentCommand discriminator drift: spec={sorted(yaml_kinds)} python={sorted(py_kinds)}"
    )


def test_agent_event_kinds_match_python_enum() -> None:
    """AgentEvent.kind enum values must match `AgentEventKind`."""
    spec = _load_spec()
    yaml_kinds = set(spec["components"]["schemas"]["AgentEvent"]["properties"]["kind"]["enum"])
    py_kinds = {v.value for v in gateway_types.AgentEventKind}
    assert yaml_kinds == py_kinds, (
        f"AgentEvent.kind drift: spec={sorted(yaml_kinds)} python={sorted(py_kinds)}"
    )


def test_workspace_event_kinds_match_python_enum() -> None:
    """WorkspaceEvent.kind enum values must match `WorkspaceEventKind`."""
    spec = _load_spec()
    yaml_kinds = set(spec["components"]["schemas"]["WorkspaceEvent"]["properties"]["kind"]["enum"])
    py_kinds = {v.value for v in gateway_types.WorkspaceEventKind}
    assert yaml_kinds == py_kinds, (
        f"WorkspaceEvent.kind drift: spec={sorted(yaml_kinds)} python={sorted(py_kinds)}"
    )
