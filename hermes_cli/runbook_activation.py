"""Fail-closed, audited activation for reviewed canonical runbook proposals."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hermes_cli import runbook_secure_io as secure_io
from hermes_cli import runbook_store
from hermes_cli import workflow_registry as registry
from hermes_cli.runbook_projection import (
    project_runbook_transaction,
    restore_projection_transaction,
    snapshot_projection,
)
from hermes_cli.runbook_schema import RunbookValidationError, split_frontmatter
from hermes_cli.sqlite_util import write_txn
from hermes_cli.workflow_models import WorkflowConflictError


_APPROVAL_SCOPE = "runbook_proposal_activation"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Installation-owned configuration.  Unlike the removed environment override,
# a caller cannot select a different trust root for one invocation.
_TRUST_ROOT = Path("/etc/hermes/runbook-activation")
_TRUST_OWNER_UID = 0
_POLICY_FILE = "approval-policy.json"
_PUBLIC_KEY_FILE = "elliott-ed25519.pem"


def _canonical_root(policy: Mapping[str, Any]) -> Path:
    """Return the canonical root selected by installed approval policy only."""
    raw_root = policy.get("canonical_root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise PermissionError("trusted runbook approval policy has no canonical_root")
    root = Path(raw_root)
    if not root.is_absolute() or "~" in root.parts:
        raise PermissionError("trusted runbook approval policy canonical_root is invalid")
    return root


def _registry_path(canonical_root: Path) -> Path:
    """Keep registry identity inside the signed, installation-selected root."""
    return canonical_root / "workflow_registry.db"


def _registry_identity(canonical_root: Path) -> str:
    """Bind approvals to the configured DB leaf, not a symlink target."""
    return str(canonical_root.resolve() / "workflow_registry.db")


@dataclass(frozen=True)
class ActivationRequest:
    slug: str
    proposal_id: str
    proposal_sha256: str
    expected_active_revision: str
    operator: str
    approval_evidence: Mapping[str, Any]


@dataclass(frozen=True)
class ActivationResult:
    runbook: runbook_store.RunbookRecord
    workflow: dict[str, Any]
    audit_path: Path
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "runbook": self.runbook.to_dict(),
            "workflow": self.workflow,
            "audit_path": str(self.audit_path),
            "replayed": self.replayed,
        }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize_identifier(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise PermissionError(f"invalid {field}")
    return normalized


def _normalize_sha256(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _HEX_SHA256.fullmatch(normalized):
        raise PermissionError(f"invalid {field}")
    return normalized


def _persistence_boundary(_name: str) -> None:
    """Fault-injection seam for the activation failure matrix."""


def _trusted_policy() -> tuple[dict[str, Any], Ed25519PublicKey]:
    try:
        with secure_io.open_anchor(_TRUST_ROOT, owner_uid=_TRUST_OWNER_UID) as trust:
            policy_raw = secure_io.read_file(trust, _POLICY_FILE, owner_uid=_TRUST_OWNER_UID)
            key_raw = secure_io.read_file(trust, _PUBLIC_KEY_FILE, owner_uid=_TRUST_OWNER_UID)
        policy = json.loads(policy_raw.decode("utf-8"))
        key = serialization.load_pem_public_key(key_raw)
    except (secure_io.SecurePathError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("trusted runbook approval policy is unavailable or unsafe") from exc
    if not isinstance(policy, dict) or policy.get("approver") != "elliott":
        raise PermissionError("trusted runbook approval policy is invalid")
    if not isinstance(key, Ed25519PublicKey):
        raise PermissionError("trusted approval public key must be Ed25519")
    return policy, key


def _authorize_operator(policy: Mapping[str, Any], operator: str) -> None:
    operators = policy.get("operators")
    binding = operators.get(operator) if isinstance(operators, Mapping) else None
    if not isinstance(binding, Mapping):
        raise PermissionError("operator is not authorized by the installed approval policy")
    try:
        expected_uid = int(binding["uid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("operator approval-policy binding is invalid") from exc
    if expected_uid != os.geteuid():
        raise PermissionError("claimed operator does not match the authenticated local agent")


def _signed_fields(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: evidence.get(key)
        for key in (
            "scope", "approval_id", "approved_by", "approved_at", "approval_reference",
            "slug", "proposal_id", "proposal_sha256", "expected_active_revision", "operator",
            "canonical_root", "registry_path",
        )
    }


def _validate_approval_evidence(
    evidence: Mapping[str, Any], request: ActivationRequest, public_key: Ed25519PublicKey,
    canonical_root: Path,
) -> dict[str, str]:
    if not isinstance(evidence, Mapping):
        raise PermissionError("recorded approval evidence is required")
    approved_by = str(evidence.get("approved_by") or "").strip().lower()
    if approved_by != "elliott":
        raise PermissionError("activation requires Elliott's recorded approval")
    approval_id = _normalize_identifier(evidence.get("approval_id"), "approval_id")
    approval_reference = str(evidence.get("approval_reference") or "").strip()
    approved_at = str(evidence.get("approved_at") or "").strip()
    if not approval_reference or not approved_at:
        raise PermissionError("approval evidence must include reference and timestamp")
    expected = {
        "scope": _APPROVAL_SCOPE,
        "slug": request.slug,
        "proposal_id": request.proposal_id,
        "proposal_sha256": request.proposal_sha256,
        "expected_active_revision": request.expected_active_revision,
        "operator": request.operator,
        "canonical_root": str(canonical_root.resolve()),
        "registry_path": _registry_identity(canonical_root),
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise PermissionError(f"approval evidence is not bound to this {field}")
    try:
        signature = base64.b64decode(str(evidence.get("signature") or ""), validate=True)
        message = json.dumps(_signed_fields(evidence), sort_keys=True, separators=(",", ":")).encode("utf-8")
        public_key.verify(signature, message)
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise PermissionError("approval signature is not valid for this activation") from exc
    return {
        "approval_id": approval_id,
        "approved_by": approved_by,
        "approval_reference": approval_reference,
        "approved_at": approved_at,
    }


def _record_from_bytes(path: Path, markdown: bytes) -> tuple[runbook_store.RunbookRecord, dict[str, Any]]:
    try:
        parsed = split_frontmatter(markdown.decode("utf-8"))
    except (UnicodeDecodeError, RunbookValidationError) as exc:
        raise PermissionError("reviewed proposal Markdown is invalid") from exc
    metadata = parsed.metadata
    source_hash = _sha256_bytes(markdown)
    return runbook_store.RunbookRecord(
        id=metadata["id"], slug=metadata["slug"], title=metadata["title"],
        purpose=metadata["purpose"], owner_profile=metadata["owner_profile"],
        status=metadata["status"], path=str(path), source_hash=source_hash,
        revision=str(metadata.get("source_revision") or f"sha256:{source_hash[:16]}"),
    ), metadata


def _read_proposal(
    runbook_dir: secure_io.SecureDir, slug: str, proposal_id: str, proposal_sha256: str, target: Path
) -> tuple[bytes, dict[str, Any]]:
    try:
        proposals = secure_io.open_descendant(
            runbook_dir, (".proposals",), owner_uid=os.geteuid()
        )
        try:
            markdown = secure_io.read_file(proposals, f"{proposal_id}.md", owner_uid=os.geteuid())
            metadata = json.loads(
                secure_io.read_file(proposals, f"{proposal_id}.json", owner_uid=os.geteuid()).decode("utf-8")
            )
        finally:
            proposals.close()
    except (secure_io.SecurePathError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PermissionError("reviewed proposal is unavailable") from exc
    if not isinstance(metadata, dict) or _sha256_bytes(markdown) != proposal_sha256:
        raise PermissionError("proposal hash does not match the reviewed candidate")
    target_text = str(target)
    if metadata.get("sha256") != proposal_sha256 or metadata.get("target") != target_text:
        raise PermissionError("proposal metadata does not bind the reviewed candidate")
    record, parsed = _record_from_bytes(target, markdown)
    if record.slug != slug:
        raise PermissionError("proposal slug does not match activation target")
    return markdown, parsed


def _audit_payload(
    request: ActivationRequest, approval: Mapping[str, str], record: runbook_store.RunbookRecord,
    workflow_id: str, previous_revision: str, canonical_root: Path,
) -> dict[str, Any]:
    return {
        "activation_id": approval["approval_id"], "state": "activated", "created_at": _now(),
        "activated_at": _now(), "scope": _APPROVAL_SCOPE, "slug": request.slug,
        "proposal_id": request.proposal_id, "proposal_sha256": request.proposal_sha256,
        "expected_active_revision": request.expected_active_revision,
        "previous_revision": previous_revision, "operator": request.operator,
        "canonical_root": str(canonical_root.resolve()),
        "registry_path": _registry_identity(canonical_root),
        **dict(approval), "active_revision": record.revision,
        "active_source_hash": record.source_hash, "workflow_id": workflow_id,
    }


def _activation_identity(
    request: ActivationRequest, approval: Mapping[str, str], canonical_root: Path
) -> dict[str, str]:
    return {
        "scope": _APPROVAL_SCOPE, "slug": request.slug, "proposal_id": request.proposal_id,
        "proposal_sha256": request.proposal_sha256,
        "expected_active_revision": request.expected_active_revision, "operator": request.operator,
        "approval_id": approval["approval_id"], "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"], "approval_reference": approval["approval_reference"],
        "canonical_root": str(canonical_root.resolve()),
        "registry_path": _registry_identity(canonical_root),
    }


def _write_index(runbook_dir: secure_io.SecureDir, metadata: dict[str, Any], source_hash: str, operator: str) -> None:
    value = json.dumps(
        {"metadata": metadata, "source_hash": source_hash, "updated_at": _now(), "approved_by": operator},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    secure_io.replace_file(runbook_dir, ".index.json", value, owner_uid=os.geteuid())


def _write_revision_snapshot(
    runbook_dir: secure_io.SecureDir, approval_id: str, current: bytes, operator: str
) -> tuple[secure_io.SecureDir, str, str]:
    revisions = secure_io.open_descendant(runbook_dir, (".revisions",), owner_uid=os.geteuid(), create=True)
    markdown_name = f"{approval_id}.md"
    metadata_name = f"{approval_id}.json"
    try:
        secure_io.replace_file(revisions, markdown_name, current, owner_uid=os.geteuid())
        secure_io.replace_file(
            revisions, metadata_name,
            json.dumps({"approved_by": operator, "created_at": _now(), "sha256": _sha256_bytes(current)}, sort_keys=True).encode("utf-8"),
            owner_uid=os.geteuid(),
        )
        return revisions, markdown_name, metadata_name
    except Exception:
        for name in (markdown_name, metadata_name):
            try:
                secure_io.unlink_optional(revisions, name, owner_uid=os.geteuid())
            except Exception:
                pass
        revisions.close()
        raise


def _restore_registry_state(
    snapshot: dict[str, Any], *, record: runbook_store.RunbookRecord, approval_id: str,
    event_id: str, db_fd: int, db_identity: str,
) -> None:
    """Restore only our candidate state, leaving a newer projection untouched."""
    with registry.connect_closing_fd(db_fd, db_identity=db_identity) as conn:
        row = conn.execute("SELECT source_hash FROM workflow_definitions WHERE id = ?", (record.id,)).fetchone()
        if row is None or row["source_hash"] != record.source_hash:
            return
        with write_txn(conn):
            conn.execute("DELETE FROM runbook_activation_identities WHERE approval_id = ?", (approval_id,))
            conn.execute("DELETE FROM workflow_events WHERE id = ?", (event_id,))
            restore_projection_transaction(conn, snapshot, candidate_workflow_id=record.id)


def _restore_registry(
    snapshot: dict[str, Any], *, record: runbook_store.RunbookRecord, approval_id: str,
    event_id: str, db_fd: int, db_identity: str,
) -> None:
    _restore_registry_state(
        snapshot, record=record, approval_id=approval_id, event_id=event_id,
        db_fd=db_fd, db_identity=db_identity,
    )


def _restore_canonical(
    runbook_dir: secure_io.SecureDir, *, candidate: bytes, previous: bytes, previous_index: bytes | None,
    revisions: secure_io.SecureDir | None, revision_names: tuple[str, str] | None,
) -> None:
    cleanup_error: Exception | None = None
    try:
        current = secure_io.read_file(runbook_dir, "RUNBOOK.md", owner_uid=os.geteuid())
        if current == candidate:
            secure_io.replace_file(runbook_dir, "RUNBOOK.md", previous, owner_uid=os.geteuid())
            if previous_index is None:
                secure_io.unlink_optional(runbook_dir, ".index.json", owner_uid=os.geteuid())
            else:
                secure_io.replace_file(runbook_dir, ".index.json", previous_index, owner_uid=os.geteuid())
    finally:
        if revisions is not None and revision_names is not None:
            for name in revision_names:
                try:
                    secure_io.unlink_optional(revisions, name, owner_uid=os.geteuid())
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
            revisions.close()
    if cleanup_error is not None:
        raise cleanup_error


def activate_reviewed_proposal(request: ActivationRequest) -> ActivationResult:
    """Activate exactly one signed reviewed proposal without mutating Cron."""
    policy, public_key = _trusted_policy()
    canonical_root = _canonical_root(policy)
    target_root = canonical_root / "runbooks"
    slug = runbook_store.runbook_path(request.slug, root=target_root).parent.name
    normalized = ActivationRequest(
        slug=slug,
        proposal_id=_normalize_identifier(request.proposal_id, "proposal_id"),
        proposal_sha256=_normalize_sha256(request.proposal_sha256, "proposal_sha256"),
        expected_active_revision=str(request.expected_active_revision or "").strip(),
        operator=_normalize_identifier(request.operator, "operator"),
        approval_evidence=request.approval_evidence,
    )
    if not normalized.expected_active_revision:
        raise PermissionError("expected active revision is required")
    _authorize_operator(policy, normalized.operator)
    approval = _validate_approval_evidence(
        normalized.approval_evidence, normalized, public_key, canonical_root
    )
    target = runbook_store.runbook_path(slug, root=target_root)
    registry_path = _registry_path(canonical_root)
    registry_identity = _registry_identity(canonical_root)
    audit_path = target.parent / ".activations" / f"{approval['approval_id']}.json"

    with secure_io.open_anchor(canonical_root, owner_uid=os.geteuid()) as canonical_dir:
        runbook_dir = secure_io.open_descendant(
            canonical_dir, ("runbooks", slug), owner_uid=os.geteuid()
        )
        try:
            with secure_io.exclusive_lock(runbook_dir, ".activation.lock", owner_uid=os.geteuid()):
                with secure_io.open_regular_file(
                    canonical_dir, "workflow_registry.db", owner_uid=os.geteuid(), create=True
                ) as registry_file:
                    candidate, candidate_metadata = _read_proposal(
                        runbook_dir, slug, normalized.proposal_id, normalized.proposal_sha256, target
                    )
                    candidate_record, _ = _record_from_bytes(target, candidate)
                    audit_dir = secure_io.open_descendant(
                        runbook_dir, (".activations",), owner_uid=os.geteuid(), create=True
                    )
                    revisions: secure_io.SecureDir | None = None
                    revision_names: tuple[str, str] | None = None
                    event_id: str | None = None
                    snapshot: dict[str, Any] | None = None
                    db_committed = False
                    mutating = False
                    current = b""
                    previous_index: bytes | None = None
                    try:
                        audit_raw = secure_io.read_optional_file(
                            audit_dir, f"{approval['approval_id']}.json", owner_uid=os.geteuid()
                        )
                        if audit_raw is not None:
                            audit = json.loads(audit_raw.decode("utf-8"))
                            if not isinstance(audit, dict) or audit.get("state") != "activated" or any(
                                audit.get(key) != value
                                for key, value in _activation_identity(
                                    normalized, approval, canonical_root
                                ).items()
                            ):
                                raise PermissionError("approval id is already bound to a different activation")
                            current = secure_io.read_file(runbook_dir, "RUNBOOK.md", owner_uid=os.geteuid())
                            current_record, _ = _record_from_bytes(target, current)
                            if current_record.source_hash != candidate_record.source_hash:
                                raise PermissionError("prior activation audit conflicts with the canonical runbook")
                            with registry.connect_closing_fd(
                                registry_file.fd, db_identity=registry_identity
                            ) as conn:
                                workflow = registry.get_definition(conn, candidate_record.id).to_dict()
                                workflow["steps"] = [
                                    step.to_dict() for step in registry.list_steps(conn, candidate_record.id)
                                ]
                            secure_io.assert_same_file(
                                canonical_dir, registry_file, owner_uid=os.geteuid()
                            )
                            return ActivationResult(current_record, workflow, audit_path, replayed=True)

                        current = secure_io.read_file(runbook_dir, "RUNBOOK.md", owner_uid=os.geteuid())
                        current_record, _ = _record_from_bytes(target, current)
                        if current_record.revision != normalized.expected_active_revision:
                            raise PermissionError("active runbook revision does not match the approved revision")
                        previous_index = secure_io.read_optional_file(
                            runbook_dir, ".index.json", owner_uid=os.geteuid()
                        )
                        mutating = True
                        revisions, revision_md, revision_json = _write_revision_snapshot(
                            runbook_dir, approval["approval_id"], current, normalized.operator
                        )
                        revision_names = (revision_md, revision_json)
                        _persistence_boundary("revision")
                        secure_io.replace_file(runbook_dir, "RUNBOOK.md", candidate, owner_uid=os.geteuid())
                        _persistence_boundary("canonical")
                        _write_index(
                            runbook_dir, candidate_metadata, candidate_record.source_hash, normalized.operator
                        )
                        _persistence_boundary("index")

                        with registry.connect_closing_fd(
                            registry_file.fd, db_identity=registry_identity
                        ) as conn:
                            with write_txn(conn):
                                snapshot = snapshot_projection(conn, workflow_id=candidate_record.id, slug=slug)
                                workflow = project_runbook_transaction(conn, candidate_record, candidate_metadata)
                                _persistence_boundary("projection")
                                replayed, event_id = registry.record_runbook_activation(
                                    conn, approval_id=approval["approval_id"],
                                    identity=_activation_identity(normalized, approval, canonical_root),
                                    workflow_id=candidate_record.id,
                                    payload={
                                        **_activation_identity(normalized, approval, canonical_root),
                                        "audit_path": str(audit_path),
                                        "previous_revision": current_record.revision,
                                        "active_revision": candidate_record.revision,
                                    },
                                )
                                if replayed:
                                    raise WorkflowConflictError(
                                        "activation identity exists without terminal audit"
                                    )
                                _persistence_boundary("event")
                        db_committed = True
                        secure_io.assert_same_file(canonical_dir, registry_file, owner_uid=os.geteuid())
                        audit = _audit_payload(
                            normalized, approval, candidate_record, candidate_record.id,
                            current_record.revision, canonical_root,
                        )
                        secure_io.replace_file(
                            audit_dir, f"{approval['approval_id']}.json",
                            json.dumps(audit, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                            owner_uid=os.geteuid(),
                        )
                        _persistence_boundary("audit")
                        if revisions is not None:
                            revisions.close()
                        revisions = None
                        return ActivationResult(candidate_record, workflow, audit_path, replayed=False)
                    except Exception:
                        # Every compensator runs while the activation lock and DB descriptor remain held.
                        if mutating:
                            try:
                                secure_io.unlink_optional(
                                    audit_dir, f"{approval['approval_id']}.json", owner_uid=os.geteuid()
                                )
                            except Exception:
                                try:
                                    secure_io.unlink_optional(
                                        audit_dir, f"{approval['approval_id']}.json", owner_uid=os.geteuid()
                                    )
                                except Exception:
                                    pass
                        if mutating and db_committed and snapshot is not None and event_id is not None:
                            try:
                                _restore_registry(
                                    snapshot, record=candidate_record,
                                    approval_id=approval["approval_id"], event_id=event_id,
                                    db_fd=registry_file.fd, db_identity=registry_identity,
                                )
                            except Exception:
                                try:
                                    _restore_registry_state(
                                        snapshot, record=candidate_record,
                                        approval_id=approval["approval_id"], event_id=event_id,
                                        db_fd=registry_file.fd, db_identity=registry_identity,
                                    )
                                except Exception:
                                    pass
                        if mutating:
                            try:
                                _restore_canonical(
                                    runbook_dir, candidate=candidate, previous=current,
                                    previous_index=previous_index,
                                    revisions=revisions, revision_names=revision_names,
                                )
                            except Exception:
                                try:
                                    _restore_canonical(
                                        runbook_dir, candidate=candidate, previous=current,
                                        previous_index=previous_index,
                                        revisions=None, revision_names=None,
                                    )
                                except Exception:
                                    pass
                        raise
                    finally:
                        audit_dir.close()
        finally:
            runbook_dir.close()


def load_approval_evidence(path: str | Path) -> Mapping[str, Any]:
    """Load evidence via a descriptor-checked absolute owner-only path."""
    evidence_path = Path(path)
    if not evidence_path.is_absolute() or "~" in evidence_path.parts:
        raise PermissionError("approval evidence path must be absolute and must not use ~")
    try:
        with secure_io.open_anchor(evidence_path.parent, owner_uid=os.geteuid()) as parent:
            payload = json.loads(secure_io.read_file(parent, evidence_path.name, owner_uid=os.geteuid()).decode("utf-8"))
    except (secure_io.SecurePathError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermissionError("approval evidence file is unavailable or unsafe") from exc
    if not isinstance(payload, dict):
        raise PermissionError("approval evidence file must contain a JSON object")
    return payload


def activate_from_args(args) -> int:
    evidence = load_approval_evidence(args.approval_evidence)
    result = activate_reviewed_proposal(
        ActivationRequest(
            slug=args.slug, proposal_id=args.proposal_id, proposal_sha256=args.proposal_sha256,
            expected_active_revision=args.expected_active_revision, operator=args.operator,
            approval_evidence=evidence,
        )
    )
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0