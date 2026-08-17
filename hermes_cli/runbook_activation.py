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
_CANONICAL_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# This literal is signed along with the rest of the activation evidence. It
# distinguishes an audited create from an update without accepting a blank or
# caller-defined "missing" revision.
_ABSENT_REVISION = "absent"

# Installation-owned configuration.  Unlike the removed environment override,
# a caller cannot select a different trust root for one invocation.
_TRUST_ROOT = Path("/etc/hermes/runbook-activation")
_TRUST_OWNER_UID = 0
_POLICY_FILE = "approval-policy.json"
_PUBLIC_KEY_FILE = "elliott-ed25519.pem"
_INTERNAL_REVIEW_EVIDENCE_FIELDS = frozenset(
    {
        "scope", "review_id", "reviewed_by", "reviewed_at", "review_reference", "change_class",
        "slug", "proposal_id", "proposal_sha256", "expected_active_revision", "operator",
        "canonical_root", "registry_path", "review_signature",
    }
)
_INTERNAL_REVIEW_SIGNED_FIELDS = (
    "scope", "review_id", "reviewed_by", "reviewed_at", "review_reference", "change_class",
    "slug", "proposal_id", "proposal_sha256", "expected_active_revision", "operator",
    "canonical_root", "registry_path",
)
_INTERNAL_REVIEW_POLICY_FIELDS = frozenset(
    {"enabled", "reviewers", "permitted_change_classes", "protected_action_categories"}
)
_PROTECTED_ACTION_CATEGORIES = frozenset(
    {
        "money_movement",
        "purchases",
        "public_publication",
        "external_commitments",
        "credential_disclosure_or_change",
        "destructive_user_data_loss",
    }
)


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


def _trusted_policy() -> tuple[dict[str, Any], Ed25519PublicKey | None]:
    try:
        with secure_io.open_anchor(_TRUST_ROOT, owner_uid=_TRUST_OWNER_UID) as trust:
            policy_raw = secure_io.read_file(trust, _POLICY_FILE, owner_uid=_TRUST_OWNER_UID)
            key_raw = secure_io.read_optional_file(trust, _PUBLIC_KEY_FILE, owner_uid=_TRUST_OWNER_UID)
        policy = json.loads(policy_raw.decode("utf-8"))
    except (secure_io.SecurePathError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("trusted runbook approval policy is unavailable or unsafe") from exc
    if not isinstance(policy, dict):
        raise PermissionError("trusted runbook approval policy is invalid")
    if key_raw is None:
        return policy, None
    try:
        key = serialization.load_pem_public_key(key_raw)
    except (ValueError, TypeError) as exc:
        raise PermissionError("trusted runbook approval policy is unavailable or unsafe") from exc
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
    if expected_uid != secure_io.current_uid():
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


def _internal_review_signed_fields(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable reviewer-attestation payload, excluding its signature."""
    return {key: evidence.get(key) for key in _INTERNAL_REVIEW_SIGNED_FIELDS}


def _parse_utc_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or _CANONICAL_UTC_TIMESTAMP.fullmatch(value) is None:
        raise PermissionError(f"invalid {field} timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise PermissionError(f"invalid {field} timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise PermissionError(f"invalid {field} timestamp")
    return value


def _internal_review_policy(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    internal = policy.get("internal_review")
    if not isinstance(internal, Mapping) or set(internal) != _INTERNAL_REVIEW_POLICY_FIELDS:
        raise PermissionError("internal reviewer policy is unavailable or invalid")
    if internal.get("enabled") is not True:
        raise PermissionError("internal reviewer policy is not enabled")
    reviewers = internal.get("reviewers")
    change_classes = internal.get("permitted_change_classes")
    protected = internal.get("protected_action_categories")
    if (
        not isinstance(reviewers, Mapping)
        or not isinstance(change_classes, list)
        or not isinstance(protected, list)
        or not all(isinstance(item, str) and item for item in change_classes + protected)
        or not _PROTECTED_ACTION_CATEGORIES.issubset(protected)
        or _PROTECTED_ACTION_CATEGORIES.intersection(change_classes)
    ):
        raise PermissionError("internal reviewer policy is unavailable or invalid")
    return internal


def _reviewer_public_key(policy: Mapping[str, Any], reviewer: str) -> Ed25519PublicKey:
    internal = _internal_review_policy(policy)
    reviewers = internal["reviewers"]
    binding = reviewers.get(reviewer) if isinstance(reviewers, Mapping) else None
    if not isinstance(binding, Mapping) or set(binding) != {"public_key_file"}:
        raise PermissionError("reviewer is not authorized by the installed approval policy")
    key_name = binding.get("public_key_file")
    if (
        not isinstance(key_name, str)
        or not _SAFE_ID.fullmatch(key_name)
        or "/" in key_name
        or key_name in {_POLICY_FILE, _PUBLIC_KEY_FILE}
    ):
        raise PermissionError("reviewer approval-policy binding is invalid")
    try:
        with secure_io.open_anchor(_TRUST_ROOT, owner_uid=_TRUST_OWNER_UID) as trust:
            key_raw = secure_io.read_file(trust, key_name, owner_uid=_TRUST_OWNER_UID)
        key = serialization.load_pem_public_key(key_raw)
    except (secure_io.SecurePathError, OSError, ValueError, TypeError) as exc:
        raise PermissionError("reviewer approval key is unavailable or unsafe") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise PermissionError("reviewer approval key must be Ed25519")
    return key


def _validate_internal_review_evidence(
    policy: Mapping[str, Any], evidence: Mapping[str, Any], request: ActivationRequest,
    canonical_root: Path,
) -> dict[str, str] | None:
    if "review_id" not in evidence:
        return None
    if set(evidence) != _INTERNAL_REVIEW_EVIDENCE_FIELDS:
        raise PermissionError("internal reviewer attestation is invalid")
    review_id = _normalize_identifier(evidence.get("review_id"), "review_id")
    reviewer = _normalize_identifier(evidence.get("reviewed_by"), "reviewed_by")
    if reviewer == request.operator:
        raise PermissionError("internal reviewer must be independent from the operator")
    reviewed_at = _parse_utc_timestamp(evidence.get("reviewed_at"), "reviewed_at")
    review_reference = str(evidence.get("review_reference") or "").strip()
    change_class = str(evidence.get("change_class") or "").strip()
    internal = _internal_review_policy(policy)
    permitted = internal["permitted_change_classes"]
    if change_class not in permitted or change_class in _PROTECTED_ACTION_CATEGORIES:
        raise PermissionError("internal reviewer attestation change class is not permitted")
    if not review_reference:
        raise PermissionError("internal reviewer attestation requires a review reference")
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
            raise PermissionError(f"internal reviewer attestation is not bound to this {field}")
    public_key = _reviewer_public_key(policy, reviewer)
    try:
        signature = base64.b64decode(str(evidence.get("review_signature") or ""), validate=True)
        message = json.dumps(
            _internal_review_signed_fields(evidence), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        public_key.verify(signature, message)
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise PermissionError("internal reviewer attestation signature is invalid") from exc
    return {
        "approval_id": review_id,
        "approval_kind": "internal_reviewer_attestation",
        "approved_by": "internal-policy",
        "approved_at": reviewed_at,
        "approval_reference": review_reference,
        "authorized_by": "internal-policy",
        "authorization_reference": review_reference,
        "attested_at": reviewed_at,
        "reviewed_by": reviewer,
        "reviewed_at": reviewed_at,
        "review_reference": review_reference,
        "change_class": change_class,
    }


def _validate_approval_evidence(
    policy: Mapping[str, Any], evidence: Mapping[str, Any], request: ActivationRequest,
    public_key: Ed25519PublicKey | None,
    canonical_root: Path,
) -> dict[str, str]:
    if not isinstance(evidence, Mapping):
        raise PermissionError("recorded approval evidence is required")
    internal = _validate_internal_review_evidence(policy, evidence, request, canonical_root)
    if internal is not None:
        return internal
    if policy.get("approver") != "elliott" or public_key is None:
        raise PermissionError("trusted owner approval public key is unavailable")
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
        "approval_kind": "owner_signature",
        "approved_by": approved_by,
        "approval_reference": approval_reference,
        "approved_at": approved_at,
        "authorized_by": approved_by,
        "authorization_reference": approval_reference,
        "attested_at": approved_at,
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
            runbook_dir, (".proposals",), owner_uid=secure_io.current_uid()
        )
        try:
            markdown = secure_io.read_file(proposals, f"{proposal_id}.md", owner_uid=secure_io.current_uid())
            metadata = json.loads(
                secure_io.read_file(proposals, f"{proposal_id}.json", owner_uid=secure_io.current_uid()).decode("utf-8")
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
    identity = {
        "scope": _APPROVAL_SCOPE, "slug": request.slug, "proposal_id": request.proposal_id,
        "proposal_sha256": request.proposal_sha256,
        "expected_active_revision": request.expected_active_revision, "operator": request.operator,
        "approval_id": approval["approval_id"], "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"], "approval_reference": approval["approval_reference"],
        "approval_kind": approval["approval_kind"], "authorized_by": approval["authorized_by"],
        "authorization_reference": approval["authorization_reference"], "attested_at": approval["attested_at"],
        "canonical_root": str(canonical_root.resolve()),
        "registry_path": _registry_identity(canonical_root),
    }
    if approval["approval_kind"] == "internal_reviewer_attestation":
        identity.update(
            {
                "reviewed_by": approval["reviewed_by"],
                "reviewed_at": approval["reviewed_at"],
                "review_reference": approval["review_reference"],
                "change_class": approval["change_class"],
            }
        )
    return identity


def _index_value(metadata: dict[str, Any], source_hash: str, operator: str) -> bytes:
    return json.dumps(
        {"metadata": metadata, "source_hash": source_hash, "updated_at": _now(), "approved_by": operator},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _write_index(
    runbook_dir: secure_io.SecureDir, metadata: dict[str, Any], source_hash: str, operator: str,
    *, value: bytes | None = None,
) -> bytes:
    value = _index_value(metadata, source_hash, operator) if value is None else value
    secure_io.replace_file(runbook_dir, ".index.json", value, owner_uid=secure_io.current_uid())
    return value


def _write_revision_snapshot(
    runbook_dir: secure_io.SecureDir, approval_id: str, current: bytes, operator: str
) -> tuple[secure_io.SecureDir, str, str]:
    revisions = secure_io.open_descendant(runbook_dir, (".revisions",), owner_uid=secure_io.current_uid(), create=True)
    markdown_name = f"{approval_id}.md"
    metadata_name = f"{approval_id}.json"
    try:
        secure_io.replace_file(revisions, markdown_name, current, owner_uid=secure_io.current_uid())
        secure_io.replace_file(
            revisions, metadata_name,
            json.dumps({"approved_by": operator, "created_at": _now(), "sha256": _sha256_bytes(current)}, sort_keys=True).encode("utf-8"),
            owner_uid=secure_io.current_uid(),
        )
        return revisions, markdown_name, metadata_name
    except Exception:
        for name in (markdown_name, metadata_name):
            try:
                secure_io.unlink_optional(revisions, name, owner_uid=secure_io.current_uid())
            except Exception:
                pass
        revisions.close()
        raise


def _restore_registry_state(
    snapshot: dict[str, Any], candidate_projection: dict[str, Any], *, record: runbook_store.RunbookRecord,
    approval_id: str, event_id: str, db_fd: int, db_identity: str,
) -> None:
    """Restore only our candidate state, leaving a newer projection untouched."""
    with registry.connect_closing_fd(db_fd, db_identity=db_identity) as conn:
        with write_txn(conn):
            # These rows are uniquely ours. Remove them even if another writer
            # superseded the projection; projection restoration remains CAS-guarded.
            conn.execute(
                "DELETE FROM runbook_activation_identities WHERE approval_id = ? AND event_id = ?",
                (approval_id, event_id),
            )
            conn.execute("DELETE FROM workflow_events WHERE id = ?", (event_id,))
            if snapshot_projection(conn, workflow_id=record.id, slug=record.slug) != candidate_projection:
                return
            restore_projection_transaction(conn, snapshot, candidate_workflow_id=record.id)


def _restore_registry(
    snapshot: dict[str, Any], candidate_projection: dict[str, Any], *, record: runbook_store.RunbookRecord,
    approval_id: str, event_id: str, db_fd: int, db_identity: str,
) -> None:
    _restore_registry_state(
        snapshot, candidate_projection, record=record, approval_id=approval_id, event_id=event_id,
        db_fd=db_fd, db_identity=db_identity,
    )


def _restore_registry_direct(
    snapshot: dict[str, Any], candidate_projection: dict[str, Any], *, record: runbook_store.RunbookRecord,
    approval_id: str, event_id: str, db_fd: int, db_identity: str,
) -> None:
    """Independently sweep this activation from the held registry inode."""
    with registry.connect_closing_fd(db_fd, db_identity=db_identity) as conn:
        with write_txn(conn):
            conn.execute(
                "DELETE FROM runbook_activation_identities WHERE approval_id = ? AND event_id = ?",
                (approval_id, event_id),
            )
            conn.execute("DELETE FROM workflow_events WHERE id = ?", (event_id,))
            if snapshot_projection(conn, workflow_id=record.id, slug=record.slug) != candidate_projection:
                return
            restore_projection_transaction(conn, snapshot, candidate_workflow_id=record.id)


def _restore_canonical(
    runbook_dir: secure_io.SecureDir, *, candidate: bytes, candidate_index: bytes, previous: bytes | None,
    previous_index: bytes | None,
    revisions: secure_io.SecureDir | None, revision_names: tuple[str, str] | None,
) -> None:
    cleanup_error: Exception | None = None
    try:
        current = secure_io.read_file(runbook_dir, "RUNBOOK.md", owner_uid=secure_io.current_uid())
        if current == candidate:
            if previous is None:
                secure_io.unlink_if_matches(runbook_dir, "RUNBOOK.md", candidate, owner_uid=secure_io.current_uid())
            else:
                secure_io.replace_file(runbook_dir, "RUNBOOK.md", previous, owner_uid=secure_io.current_uid())
            current_index = secure_io.read_optional_file(runbook_dir, ".index.json", owner_uid=secure_io.current_uid())
            if current_index == candidate_index:
                if previous_index is None:
                    secure_io.unlink_optional(runbook_dir, ".index.json", owner_uid=secure_io.current_uid())
                else:
                    secure_io.replace_file(runbook_dir, ".index.json", previous_index, owner_uid=secure_io.current_uid())
    finally:
        if revisions is not None and revision_names is not None:
            for name in revision_names:
                try:
                    secure_io.unlink_optional(revisions, name, owner_uid=secure_io.current_uid())
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
            revisions.close()
    if cleanup_error is not None:
        raise cleanup_error


def _restore_canonical_direct(
    runbook_dir: secure_io.SecureDir, *, candidate: bytes, candidate_index: bytes, previous: bytes | None,
    previous_index: bytes | None, revision_names: tuple[str, str] | None,
) -> None:
    """Use a fresh descriptor path if the normal recovery path is unavailable."""
    cleanup_error: Exception | None = None
    current = secure_io.read_file(runbook_dir, "RUNBOOK.md", owner_uid=secure_io.current_uid())
    if current == candidate:
        if previous is None:
            secure_io.unlink_if_matches(runbook_dir, "RUNBOOK.md", candidate, owner_uid=secure_io.current_uid())
        else:
            secure_io.replace_file(runbook_dir, "RUNBOOK.md", previous, owner_uid=secure_io.current_uid())
        current_index = secure_io.read_optional_file(runbook_dir, ".index.json", owner_uid=secure_io.current_uid())
        if current_index == candidate_index:
            if previous_index is None:
                secure_io.unlink_optional(runbook_dir, ".index.json", owner_uid=secure_io.current_uid())
            else:
                secure_io.replace_file(runbook_dir, ".index.json", previous_index, owner_uid=secure_io.current_uid())
    if revision_names is not None:
        revisions = secure_io.open_descendant(runbook_dir, (".revisions",), owner_uid=secure_io.current_uid())
        try:
            for name in revision_names:
                try:
                    secure_io.unlink_optional(revisions, name, owner_uid=secure_io.current_uid())
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
        finally:
            revisions.close()
    if cleanup_error is not None:
        raise cleanup_error


def _remove_audit(audit_dir: secure_io.SecureDir, name: str, expected: bytes | None) -> None:
    if expected is not None:
        secure_io.unlink_if_matches(audit_dir, name, expected, owner_uid=secure_io.current_uid())


def _remove_audit_direct(audit_dir: secure_io.SecureDir, name: str, expected: bytes | None) -> None:
    """Repeat checked cleanup without a raw unlink TOCTOU fallback."""
    if expected is not None:
        secure_io.unlink_if_matches(audit_dir, name, expected, owner_uid=secure_io.current_uid())


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
        policy, normalized.approval_evidence, normalized, public_key, canonical_root
    )
    target = runbook_store.runbook_path(slug, root=target_root)
    registry_identity = _registry_identity(canonical_root)
    audit_path = target.parent / ".activations" / f"{approval['approval_id']}.json"

    with secure_io.open_anchor(canonical_root, owner_uid=secure_io.current_uid()) as canonical_dir:
        runbook_dir = secure_io.open_descendant(
            canonical_dir, ("runbooks", slug), owner_uid=secure_io.current_uid()
        )
        try:
            with secure_io.exclusive_lock(runbook_dir, ".activation.lock", owner_uid=secure_io.current_uid()):
                with secure_io.open_regular_file(
                    canonical_dir, "workflow_registry.db", owner_uid=secure_io.current_uid(), create=True
                ) as registry_file:
                    secure_io.assert_same_file(canonical_dir, registry_file, owner_uid=secure_io.current_uid())
                    candidate, candidate_metadata = _read_proposal(
                        runbook_dir, slug, normalized.proposal_id, normalized.proposal_sha256, target
                    )
                    candidate_record, _ = _record_from_bytes(target, candidate)
                    if candidate_record.status not in {"active", "retired"}:
                        raise PermissionError("reviewed candidate must declare status active or retired")
                    audit_dir = secure_io.open_descendant(
                        runbook_dir, (".activations",), owner_uid=secure_io.current_uid(), create=True
                    )
                    revisions: secure_io.SecureDir | None = None
                    revision_names: tuple[str, str] | None = None
                    event_id: str | None = None
                    snapshot: dict[str, Any] | None = None
                    candidate_projection: dict[str, Any] | None = None
                    db_committed = False
                    mutating = False
                    current: bytes | None = None
                    previous_index: bytes | None = None
                    candidate_index = b""
                    audit_bytes: bytes | None = None
                    try:
                        audit_raw = secure_io.read_optional_file(
                            audit_dir, f"{approval['approval_id']}.json", owner_uid=secure_io.current_uid()
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
                            replayed_current = secure_io.read_file(
                                runbook_dir, "RUNBOOK.md", owner_uid=secure_io.current_uid()
                            )
                            current_record, _ = _record_from_bytes(target, replayed_current)
                            if current_record.source_hash != candidate_record.source_hash:
                                raise PermissionError("prior activation audit conflicts with the canonical runbook")
                            with registry.connect_closing_fd(
                                registry_file.fd, db_identity=registry_identity
                            ) as conn:
                                secure_io.assert_same_file(
                                    canonical_dir, registry_file, owner_uid=secure_io.current_uid()
                                )
                                workflow = registry.get_definition(conn, candidate_record.id).to_dict()
                                workflow["steps"] = [
                                    step.to_dict() for step in registry.list_steps(conn, candidate_record.id)
                                ]
                            secure_io.assert_same_file(
                                canonical_dir, registry_file, owner_uid=secure_io.current_uid()
                            )
                            return ActivationResult(current_record, workflow, audit_path, replayed=True)

                        current = secure_io.read_optional_file(
                            runbook_dir, "RUNBOOK.md", owner_uid=secure_io.current_uid()
                        )
                        if current is None:
                            if normalized.expected_active_revision != _ABSENT_REVISION:
                                raise PermissionError(
                                    "missing canonical runbook requires expected active revision 'absent'"
                                )
                            if candidate_record.status == "retired":
                                raise PermissionError(
                                    "reviewed retirement requires an existing canonical runbook"
                                )
                            current_record = None
                        else:
                            current_record, _ = _record_from_bytes(target, current)
                            if normalized.expected_active_revision == _ABSENT_REVISION:
                                raise PermissionError("expected active revision 'absent' requires no canonical runbook")
                            if current_record.revision != normalized.expected_active_revision:
                                raise PermissionError("active runbook revision does not match the approved revision")
                        previous_revision = (
                            _ABSENT_REVISION if current_record is None else current_record.revision
                        )
                        previous_index = secure_io.read_optional_file(
                            runbook_dir, ".index.json", owner_uid=secure_io.current_uid()
                        )
                        # The terminal audit is a write-ahead commitment. Its
                        # real durable write must happen before any candidate
                        # canonical or Registry state, not merely its synthetic
                        # fault-injection seam. If the audit write itself fails,
                        # no compensator is needed (or trusted) to remove a
                        # half-activation.
                        _persistence_boundary("audit")
                        audit = _audit_payload(
                            normalized, approval, candidate_record, candidate_record.id,
                            previous_revision,
                            canonical_root,
                        )
                        audit_bytes = json.dumps(audit, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        secure_io.replace_file(
                            audit_dir, f"{approval['approval_id']}.json", audit_bytes,
                            owner_uid=secure_io.current_uid(),
                        )
                        mutating = True
                        if current is not None:
                            revisions, revision_md, revision_json = _write_revision_snapshot(
                                runbook_dir, approval["approval_id"], current, normalized.operator
                            )
                            revision_names = (revision_md, revision_json)
                        _persistence_boundary("revision")
                        secure_io.replace_file(runbook_dir, "RUNBOOK.md", candidate, owner_uid=secure_io.current_uid())
                        _persistence_boundary("canonical")
                        candidate_index = _index_value(
                            candidate_metadata, candidate_record.source_hash, normalized.operator
                        )
                        _write_index(
                            runbook_dir, candidate_metadata, candidate_record.source_hash, normalized.operator,
                            value=candidate_index,
                        )
                        _persistence_boundary("index")

                        with registry.connect_closing_fd(
                            registry_file.fd, db_identity=registry_identity
                        ) as conn:
                            with write_txn(conn):
                                snapshot = snapshot_projection(conn, workflow_id=candidate_record.id, slug=slug)
                                workflow = project_runbook_transaction(conn, candidate_record, candidate_metadata)
                                candidate_projection = snapshot_projection(
                                    conn, workflow_id=candidate_record.id, slug=slug
                                )
                                _persistence_boundary("projection")
                                replayed, event_id = registry.record_runbook_activation(
                                    conn, approval_id=approval["approval_id"],
                                    identity=_activation_identity(normalized, approval, canonical_root),
                                    workflow_id=candidate_record.id,
                                    payload={
                                        **_activation_identity(normalized, approval, canonical_root),
                                        "audit_path": str(audit_path),
                                        "previous_revision": previous_revision,
                                        "active_revision": candidate_record.revision,
                                    },
                                )
                                if replayed:
                                    raise WorkflowConflictError(
                                        "activation identity exists without terminal audit"
                                    )
                                _persistence_boundary("event")
                                # The namespace must still name the checked inode before
                                # this transaction becomes externally visible.
                                secure_io.assert_same_file(
                                    canonical_dir, registry_file, owner_uid=secure_io.current_uid()
                                )
                        db_committed = True
                        secure_io.assert_same_file(canonical_dir, registry_file, owner_uid=secure_io.current_uid())
                        if revisions is not None:
                            revisions.close()
                        revisions = None
                        return ActivationResult(candidate_record, workflow, audit_path, replayed=False)
                    except Exception:
                        # Every compensator runs while the activation lock and DB descriptor remain held.
                        if mutating:
                            try:
                                _remove_audit(
                                    audit_dir, f"{approval['approval_id']}.json", audit_bytes
                                )
                            except Exception:
                                pass
                            try:
                                _remove_audit_direct(
                                    audit_dir, f"{approval['approval_id']}.json", audit_bytes
                                )
                            except Exception:
                                pass
                        if (
                            mutating and db_committed and snapshot is not None
                            and candidate_projection is not None and event_id is not None
                        ):
                            try:
                                _restore_registry(
                                    snapshot, candidate_projection, record=candidate_record,
                                    approval_id=approval["approval_id"], event_id=event_id,
                                    db_fd=registry_file.fd, db_identity=registry_identity,
                                )
                            except Exception:
                                pass
                            try:
                                _restore_registry_state(
                                    snapshot, candidate_projection, record=candidate_record,
                                    approval_id=approval["approval_id"], event_id=event_id,
                                    db_fd=registry_file.fd, db_identity=registry_identity,
                                )
                            except Exception:
                                pass
                            try:
                                _restore_registry_direct(
                                    snapshot, candidate_projection, record=candidate_record,
                                    approval_id=approval["approval_id"], event_id=event_id,
                                    db_fd=registry_file.fd, db_identity=registry_identity,
                                )
                            except Exception:
                                pass
                        if mutating:
                            try:
                                _restore_canonical(
                                    runbook_dir, candidate=candidate, candidate_index=candidate_index,
                                    previous=current, previous_index=previous_index,
                                    revisions=revisions, revision_names=revision_names,
                                )
                            except Exception:
                                pass
                            try:
                                _restore_canonical(
                                    runbook_dir, candidate=candidate, candidate_index=candidate_index,
                                    previous=current, previous_index=previous_index,
                                    revisions=None, revision_names=None,
                                )
                            except Exception:
                                pass
                            try:
                                _restore_canonical_direct(
                                    runbook_dir, candidate=candidate, candidate_index=candidate_index,
                                    previous=current, previous_index=previous_index,
                                    revision_names=revision_names,
                                )
                            except Exception:
                                pass
                            if revisions is not None:
                                try:
                                    revisions.close()
                                except OSError:
                                    pass
                                revisions = None
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
        with secure_io.open_anchor(evidence_path.parent, owner_uid=secure_io.current_uid()) as parent:
            payload = json.loads(secure_io.read_file(parent, evidence_path.name, owner_uid=secure_io.current_uid()).decode("utf-8"))
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
