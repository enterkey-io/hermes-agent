from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_cli import runbook_activation as activation
from hermes_cli import runbook_secure_io as secure_io
from hermes_cli import runbook_store
from hermes_cli import workflow_registry as registry
from hermes_cli.runbook_activation import ActivationRequest, activate_reviewed_proposal
from hermes_cli.sqlite_util import write_txn
from hermes_cli.workflow_models import WorkflowConflictError


def _markdown(slug: str = "daily-brief", title: str = "Daily Brief") -> str:
    return f"""---
id: wf_daily_brief
slug: {slug}
title: {title}
purpose: Prepare a concise daily operating brief.
owner_profile: alina
status: active
runtime:
  kind: hermes
  ref: gateway
schedules: []
steps:
  - step_key: collect
    name: Collect context
    executor_profile: alina
inputs: {{}}
outputs: {{}}
permitted_writes: []
approval_rules: {{}}
retry:
  max_attempts: 2
timeout:
  seconds: 1800
deduplication:
  strategy: date
related: {{}}
---
# {title}

## Procedure

1. Collect context.
"""


def _sha256(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _sign(evidence: dict, private_key: Ed25519PrivateKey) -> None:
    fields = activation._signed_fields(evidence)
    message = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    evidence["signature"] = base64.b64encode(private_key.sign(message)).decode("ascii")


def _sign_internal_review(evidence: dict, private_key: Ed25519PrivateKey) -> None:
    fields = activation._internal_review_signed_fields(evidence)
    message = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    evidence["review_signature"] = base64.b64encode(private_key.sign(message)).decode("ascii")


@pytest.fixture
def proposed_runbook(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    private_key = Ed25519PrivateKey.generate()
    reviewer_private_key = Ed25519PrivateKey.generate()
    trust = tmp_path / "installed-runbook-approval"
    trust.mkdir(mode=0o700)
    (trust / "approval-policy.json").write_text(
        json.dumps(
            {
                "approver": "elliott",
                "canonical_root": str(home),
                "operators": {"alina": {"uid": os.geteuid()}},
                "internal_review": {
                    "enabled": True,
                    "reviewers": {"reese": {"public_key_file": "reese-ed25519.pem"}},
                    "permitted_change_classes": ["routine_internal_repair"],
                    "protected_action_categories": [
                        "money_movement",
                        "purchases",
                        "public_publication",
                        "external_commitments",
                        "credential_disclosure_or_change",
                        "destructive_user_data_loss",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (trust / "elliott-ed25519.pem").write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    (trust / "reese-ed25519.pem").write_bytes(
        reviewer_private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    for path in trust.iterdir():
        path.chmod(0o600)
    monkeypatch.setattr(activation, "_TRUST_ROOT", trust)
    monkeypatch.setattr(activation, "_TRUST_OWNER_UID", os.geteuid())
    active = _markdown()
    active_path = tmp_path / "active.md"
    active_path.write_text(active, encoding="utf-8")
    active_parsed = runbook_store.read_runbook(active_path)
    active_record = runbook_store.save_runbook(
        active_parsed.metadata, active_parsed.body, approved_by="elliott"
    )
    candidate = _markdown(title="Daily Brief — reviewed")
    proposal = runbook_store.propose_edit(
        "daily-brief", candidate, proposed_by="sloane", summary="Reviewed revision"
    )
    return {
        "active": active_record,
        "candidate": candidate,
        "proposal_id": proposal.stem,
        "proposal_sha256": _sha256(candidate),
        "private_key": private_key,
        "reviewer_private_key": reviewer_private_key,
        "trust": trust,
    }


def _request(proposed_runbook, **overrides) -> ActivationRequest:
    active = proposed_runbook["active"]
    evidence_overrides = overrides.pop("evidence", {})
    data = {
        "slug": "daily-brief",
        "proposal_id": proposed_runbook["proposal_id"],
        "proposal_sha256": proposed_runbook["proposal_sha256"],
        "expected_active_revision": active.revision,
        "operator": "alina",
        "canonical_root": str(Path(active.path).parents[2].resolve()),
        "registry_path": str(Path(active.path).parents[2] / "workflow_registry.db"),
    }
    data.update(overrides)
    evidence = {
        "scope": "runbook_proposal_activation",
        "approval_id": "plaud-production-repair",
        "approved_by": "elliott",
        "approved_at": "2026-08-14T03:30:00Z",
        "approval_reference": "kanban:t_e7df81eb",
        **data,
    }
    evidence.update(evidence_overrides)
    _sign(evidence, proposed_runbook["private_key"])
    request_data = {
        key: data[key]
        for key in ("slug", "proposal_id", "proposal_sha256", "expected_active_revision", "operator")
    }
    return ActivationRequest(**request_data, approval_evidence=evidence)


def _internal_review_request(proposed_runbook, **overrides) -> ActivationRequest:
    active = proposed_runbook["active"]
    evidence_overrides = overrides.pop("evidence", {})
    data = {
        "slug": "daily-brief",
        "proposal_id": proposed_runbook["proposal_id"],
        "proposal_sha256": proposed_runbook["proposal_sha256"],
        "expected_active_revision": active.revision,
        "operator": "alina",
        "canonical_root": str(Path(active.path).parents[2].resolve()),
        "registry_path": str(Path(active.path).parents[2] / "workflow_registry.db"),
    }
    data.update(overrides)
    evidence = {
        "scope": "runbook_proposal_activation",
        "review_id": "daily-brief-internal-review",
        "reviewed_by": "reese",
        "reviewed_at": "2026-08-16T22:30:00Z",
        "review_reference": "kanban:t_6566d611",
        "change_class": "routine_internal_repair",
        **data,
    }
    evidence.update(evidence_overrides)
    _sign_internal_review(evidence, proposed_runbook["reviewer_private_key"])
    request_data = {
        key: data[key]
        for key in ("slug", "proposal_id", "proposal_sha256", "expected_active_revision", "operator")
    }
    return ActivationRequest(**request_data, approval_evidence=evidence)


def _create_request(proposed_runbook, *, status: str = "active", **overrides) -> tuple[ActivationRequest, str]:
    candidate = _markdown(slug="recording-pipeline", title="Recording Pipeline Health")
    candidate = candidate.replace("id: wf_daily_brief", "id: wf_recording_pipeline")
    candidate = candidate.replace("status: active", f"status: {status}")
    proposal = runbook_store.propose_edit(
        "recording-pipeline", candidate, proposed_by="sloane", summary="Reviewed successor"
    )
    canonical_root = Path(proposed_runbook["active"].path).parents[2]
    data = {
        "slug": "recording-pipeline",
        "proposal_id": proposal.stem,
        "proposal_sha256": _sha256(candidate),
        "expected_active_revision": "absent",
        "operator": "alina",
        "canonical_root": str(canonical_root.resolve()),
        "registry_path": str(canonical_root / "workflow_registry.db"),
    }
    data.update(overrides)
    evidence = {
        "scope": "runbook_proposal_activation",
        "approval_id": "recording-pipeline-create",
        "approved_by": "elliott",
        "approved_at": "2026-08-14T03:30:00Z",
        "approval_reference": "kanban:t_181cdb98",
        **data,
    }
    _sign(evidence, proposed_runbook["private_key"])
    request_data = {
        key: data[key]
        for key in ("slug", "proposal_id", "proposal_sha256", "expected_active_revision", "operator")
    }
    return ActivationRequest(**request_data, approval_evidence=evidence), candidate


def _retirement_request(proposed_runbook) -> tuple[ActivationRequest, str]:
    candidate = proposed_runbook["candidate"].replace("status: active", "status: retired")
    proposal = runbook_store.propose_edit(
        "daily-brief", candidate, proposed_by="sloane", summary="Reviewed retirement"
    )
    canonical_root = Path(proposed_runbook["active"].path).parents[2]
    data = {
        "slug": "daily-brief",
        "proposal_id": proposal.stem,
        "proposal_sha256": _sha256(candidate),
        "expected_active_revision": proposed_runbook["active"].revision,
        "operator": "alina",
        "canonical_root": str(canonical_root.resolve()),
        "registry_path": str(canonical_root / "workflow_registry.db"),
    }
    evidence = {
        "scope": "runbook_proposal_activation",
        "review_id": "daily-brief-retirement-review",
        "reviewed_by": "reese",
        "reviewed_at": "2026-08-16T22:30:00Z",
        "review_reference": "kanban:t_3c6ac575",
        "change_class": "routine_internal_repair",
        **data,
    }
    _sign_internal_review(evidence, proposed_runbook["reviewer_private_key"])
    return (
        ActivationRequest(
            **{key: data[key] for key in ("slug", "proposal_id", "proposal_sha256", "expected_active_revision", "operator")},
            approval_evidence=evidence,
        ),
        candidate,
    )


def _activation_events() -> list[dict]:
    with registry.connect_closing() as conn:
        return [
            item
            for item in registry.list_events(conn, entity_type="workflow_definition", entity_id="wf_daily_brief")
            if item["event_type"] == "runbook_proposal_activated"
        ]


def test_activate_reviewed_proposal_binds_evidence_and_writes_terminal_audit(proposed_runbook):
    result = activate_reviewed_proposal(_request(proposed_runbook))

    assert result.replayed is False
    assert result.runbook.title == "Daily Brief — reviewed"
    assert result.runbook.source_hash == proposed_runbook["proposal_sha256"]
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["state"] == "activated"
    assert audit["approval_id"] == "plaud-production-repair"
    assert audit["proposal_sha256"] == proposed_runbook["proposal_sha256"]
    assert audit["previous_revision"] == proposed_runbook["active"].revision
    assert len(_activation_events()) == 1


def test_internal_reviewer_attestation_activates_exact_bound_proposal(proposed_runbook):
    (proposed_runbook["trust"] / "elliott-ed25519.pem").unlink()
    result = activate_reviewed_proposal(_internal_review_request(proposed_runbook))

    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert result.replayed is False
    assert result.runbook.source_hash == proposed_runbook["proposal_sha256"]
    assert audit["approval_id"] == "daily-brief-internal-review"
    assert audit["approval_kind"] == "internal_reviewer_attestation"
    assert audit["reviewed_by"] == "reese"
    assert audit["operator"] == "alina"


def test_internal_reviewer_attestation_rejects_self_review(proposed_runbook):
    policy_path = proposed_runbook["trust"] / "approval-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["operators"]["reese"] = {"uid": os.geteuid()}
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    policy_path.chmod(0o600)

    with pytest.raises(PermissionError, match="independent"):
        activate_reviewed_proposal(_internal_review_request(proposed_runbook, operator="reese"))


def test_internal_reviewer_attestation_rejects_unauthorized_identity_and_tampering(proposed_runbook):
    with pytest.raises(PermissionError, match="operator is not authorized"):
        activate_reviewed_proposal(_internal_review_request(proposed_runbook, operator="untrusted"))

    with pytest.raises(PermissionError, match="reviewer"):
        activate_reviewed_proposal(
            _internal_review_request(proposed_runbook, evidence={"reviewed_by": "untrusted"})
        )

    request = _internal_review_request(proposed_runbook)
    tampered = dict(request.approval_evidence)
    tampered["review_reference"] = "kanban:tampered"
    with pytest.raises(PermissionError, match="signature"):
        activate_reviewed_proposal(
            ActivationRequest(**{**request.__dict__, "approval_evidence": tampered})
        )


def test_internal_reviewer_attestation_replay_conflict_and_stale_revision_are_rejected(proposed_runbook):
    request = _internal_review_request(proposed_runbook)
    assert activate_reviewed_proposal(request).replayed is False
    assert activate_reviewed_proposal(request).replayed is True

    conflicting = dict(request.approval_evidence)
    conflicting["review_reference"] = "kanban:conflicting-review"
    _sign_internal_review(conflicting, proposed_runbook["reviewer_private_key"])
    with pytest.raises(PermissionError, match="different activation"):
        activate_reviewed_proposal(
            ActivationRequest(**{**request.__dict__, "approval_evidence": conflicting})
        )

    stale_request = _internal_review_request(
        proposed_runbook,
        evidence={"review_id": "daily-brief-stale-review"},
    )
    with pytest.raises(PermissionError, match="active runbook revision"):
        activate_reviewed_proposal(stale_request)


def test_internal_reviewer_attestation_rejects_cross_root_binding_and_protected_actions(proposed_runbook, tmp_path):
    with pytest.raises(PermissionError, match="canonical_root"):
        activate_reviewed_proposal(
            _internal_review_request(proposed_runbook, canonical_root=str(tmp_path / "other-root"))
        )

    with pytest.raises(PermissionError, match="change class"):
        activate_reviewed_proposal(
            _internal_review_request(proposed_runbook, evidence={"change_class": "money_movement"})
        )


def test_legacy_owner_signed_evidence_remains_compatible_with_internal_review_policy(proposed_runbook):
    result = activate_reviewed_proposal(_request(proposed_runbook))

    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert result.replayed is False
    assert audit["approval_kind"] == "owner_signature"
    assert audit["approved_by"] == "elliott"


def test_activate_reviewed_proposal_creates_missing_active_successor(proposed_runbook):
    request, candidate = _create_request(proposed_runbook)

    result = activate_reviewed_proposal(request)

    canonical_root = Path(proposed_runbook["active"].path).parents[2]
    runbook_path = canonical_root / "runbooks" / "recording-pipeline" / "RUNBOOK.md"
    assert result.replayed is False
    assert result.runbook.status == "active"
    assert runbook_path.read_text(encoding="utf-8") == candidate
    assert not (runbook_path.parent / ".revisions" / "recording-pipeline-create.md").exists()
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["previous_revision"] == "absent"
    with registry.connect_closing() as conn:
        assert registry.get_definition(conn, "wf_recording_pipeline").status == "active"


def test_internal_reviewer_attestation_retires_existing_canonical_runbook(proposed_runbook):
    request, candidate = _retirement_request(proposed_runbook)

    result = activate_reviewed_proposal(request)

    assert result.replayed is False
    assert result.runbook.status == "retired"
    assert result.runbook.source_hash == _sha256(candidate)
    with registry.connect_closing() as conn:
        assert registry.get_definition(conn, "wf_daily_brief").status == "retired"


def test_create_activation_rejects_reviewed_retirement_candidate(proposed_runbook):
    request, _ = _create_request(proposed_runbook, status="retired")

    with pytest.raises(PermissionError, match="requires an existing canonical runbook"):
        activate_reviewed_proposal(request)


def test_create_activation_rejects_draft_candidate_and_non_absent_precondition(proposed_runbook):
    draft_request, _ = _create_request(proposed_runbook, status="draft")
    with pytest.raises(PermissionError, match="status active"):
        activate_reviewed_proposal(draft_request)

    stale_request, _ = _create_request(
        proposed_runbook,
        expected_active_revision="sha256:deadbeefdeadbeef",
    )
    with pytest.raises(PermissionError, match="requires expected active revision 'absent'"):
        activate_reviewed_proposal(stale_request)


@pytest.mark.parametrize("boundary", ["canonical", "index", "projection", "event"])
def test_create_activation_failure_leaves_no_canonical_successor(proposed_runbook, monkeypatch, boundary):
    request, _ = _create_request(proposed_runbook)

    def fail(name: str) -> None:
        if name == boundary:
            raise OSError(f"injected failure at {name}")

    monkeypatch.setattr(activation, "_persistence_boundary", fail)
    with pytest.raises(OSError, match=boundary):
        activate_reviewed_proposal(request)

    canonical_root = Path(proposed_runbook["active"].path).parents[2]
    runbook_dir = canonical_root / "runbooks" / "recording-pipeline"
    assert not (runbook_dir / "RUNBOOK.md").exists()
    assert not (runbook_dir / ".index.json").exists()
    assert not (runbook_dir / ".activations" / "recording-pipeline-create.json").exists()
    with registry.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0


def test_create_rollback_does_not_race_a_canonical_store_writer(proposed_runbook, monkeypatch):
    request, candidate = _create_request(proposed_runbook)
    canonical_written = threading.Event()
    allow_rollback = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    activation_error: list[Exception] = []
    replacement = candidate.replace("# Recording Pipeline Health", "# Writer Replacement")

    def fail_after_canonical(name: str) -> None:
        if name == "canonical":
            canonical_written.set()
            assert allow_rollback.wait(timeout=5)
        elif name == "index":
            raise OSError("injected failure at index")

    def activate_in_thread() -> None:
        try:
            activate_reviewed_proposal(request)
        except Exception as exc:  # asserted below to preserve the thread traceback context
            activation_error.append(exc)

    def write_in_thread() -> None:
        writer_started.set()
        runbook_store.save_runbook_markdown(replacement, approved_by="elliott")
        writer_finished.set()

    monkeypatch.setattr(activation, "_persistence_boundary", fail_after_canonical)
    activation_thread = threading.Thread(target=activate_in_thread)
    activation_thread.start()
    assert canonical_written.wait(timeout=5)
    writer_thread = threading.Thread(target=write_in_thread)
    writer_thread.start()
    assert writer_started.wait(timeout=5)
    assert not writer_finished.wait(timeout=0.1)
    allow_rollback.set()
    activation_thread.join(timeout=5)
    writer_thread.join(timeout=5)

    assert isinstance(activation_error[0], OSError)
    assert writer_finished.is_set()
    canonical_root = Path(proposed_runbook["active"].path).parents[2]
    assert (canonical_root / "runbooks" / "recording-pipeline" / "RUNBOOK.md").read_text(
        encoding="utf-8"
    ) == replacement


def test_activation_ignores_caller_selected_hermes_home(proposed_runbook, monkeypatch, tmp_path):
    attacker_home = tmp_path / "attacker-home"
    attacker_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(attacker_home))

    result = activate_reviewed_proposal(_request(proposed_runbook))

    assert result.audit_path.is_relative_to(Path(proposed_runbook["active"].path).parents[2] / "runbooks")
    assert not (attacker_home / "runbooks").exists()
    assert not (attacker_home / "workflow_registry.db").exists()


def test_activation_ignores_caller_selected_home_and_hermes_home(proposed_runbook, monkeypatch, tmp_path):
    attacker_home = tmp_path / "attacker-home"
    attacker_home.mkdir(mode=0o700)
    attacker_hermes = tmp_path / "attacker-hermes"
    attacker_hermes.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(attacker_home))
    monkeypatch.setenv("HERMES_HOME", str(attacker_hermes))
    monkeypatch.setattr(Path, "home", lambda: attacker_home)

    result = activate_reviewed_proposal(_request(proposed_runbook))

    assert result.audit_path.is_relative_to(Path(proposed_runbook["active"].path).parents[2])
    assert not (attacker_home / ".hermes" / "runbooks").exists()
    assert not (attacker_hermes / "runbooks").exists()
    assert not (attacker_hermes / "workflow_registry.db").exists()


def test_cross_root_signed_approval_replay_is_rejected(proposed_runbook, tmp_path):
    alternative_root = tmp_path / "alternative-root"
    alternative_root.mkdir(mode=0o700)
    policy_path = proposed_runbook["trust"] / "approval-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "approver": "elliott",
                "canonical_root": str(alternative_root),
                "operators": {"alina": {"uid": os.geteuid()}},
            }
        ),
        encoding="utf-8",
    )
    policy_path.chmod(0o600)

    with pytest.raises(PermissionError, match="canonical_root"):
        activate_reviewed_proposal(_request(proposed_runbook))


def test_activation_rejects_registry_database_symlink(proposed_runbook, tmp_path):
    registry_target = tmp_path / "attacker-registry.db"
    registry_target.write_bytes(b"")
    registry_target.chmod(0o600)
    registry_path = Path(proposed_runbook["active"].path).parents[2] / "workflow_registry.db"
    registry_path.symlink_to(registry_target)

    with pytest.raises(PermissionError, match="unsafe"):
        activate_reviewed_proposal(_request(proposed_runbook))
    assert registry_target.read_bytes() == b""


def test_registry_swap_after_descriptor_open_fails_without_redirecting_persistence(
    proposed_runbook, monkeypatch, tmp_path
):
    canonical_root = Path(proposed_runbook["active"].path).parents[2]
    registry_path = canonical_root / "workflow_registry.db"
    registry_path.touch(mode=0o600)
    original = Path(proposed_runbook["active"].path).read_bytes()
    redirected = tmp_path / "redirected-registry.db"
    redirected.write_bytes(b"")
    redirected.chmod(0o600)
    original_connect = registry.connect_closing_fd
    original_record = registry.record_runbook_activation
    record_attempted = False

    def swap_then_connect(fd: int, **kwargs):
        registry_path.unlink()
        registry_path.symlink_to(redirected)
        return original_connect(fd, **kwargs)

    def capture_record(*args, **kwargs):
        nonlocal record_attempted
        record_attempted = True
        return original_record(*args, **kwargs)

    monkeypatch.setattr(registry, "connect_closing_fd", swap_then_connect)
    monkeypatch.setattr(registry, "record_runbook_activation", capture_record)
    with pytest.raises(PermissionError, match="registry"):
        activate_reviewed_proposal(_request(proposed_runbook))

    assert record_attempted is False
    assert redirected.read_bytes() == b""
    assert Path(proposed_runbook["active"].path).read_bytes() == original
    assert not (canonical_root / "runbooks" / "daily-brief" / ".activations" / "plaud-production-repair.json").exists()
    assert not (canonical_root / "runbooks" / "daily-brief" / ".revisions" / "plaud-production-repair.md").exists()
    assert not (canonical_root / "runbooks" / "daily-brief" / ".revisions" / "plaud-production-repair.json").exists()


def test_audit_precommit_failure_does_not_enter_registry_recovery(
    proposed_runbook, monkeypatch
):
    original = Path(proposed_runbook["active"].path).read_bytes()
    recovery_attempts = 0

    def fail_at_audit(name: str) -> None:
        if name == "audit":
            raise OSError("injected failure at audit")

    def failed_restore(*args, **kwargs) -> None:
        nonlocal recovery_attempts
        recovery_attempts += 1
        raise OSError("injected registry restore failure")

    monkeypatch.setattr(activation, "_persistence_boundary", fail_at_audit)
    monkeypatch.setattr(activation, "_restore_registry", failed_restore)
    with pytest.raises(OSError, match="audit"):
        activate_reviewed_proposal(_request(proposed_runbook))

    assert recovery_attempts == 0
    assert Path(proposed_runbook["active"].path).read_bytes() == original
    assert _activation_events() == []
    with registry.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0
    runbook_dir = Path(proposed_runbook["active"].path).parent
    assert not (runbook_dir / ".activations" / "plaud-production-repair.json").exists()
    assert not (runbook_dir / ".revisions" / "plaud-production-repair.md").exists()
    assert not (runbook_dir / ".revisions" / "plaud-production-repair.json").exists()


def test_persistent_all_recovery_hook_failures_after_audit_precommit_leave_no_activation_residue(
    proposed_runbook, monkeypatch
):
    runbook_path = Path(proposed_runbook["active"].path)
    original_markdown = runbook_path.read_bytes()
    original_index = (runbook_path.parent / ".index.json").read_bytes()
    recovery_attempts = 0

    def fail_at_audit(name: str) -> None:
        if name == "audit":
            raise OSError("injected failure at audit")

    def compensation_failure(*args, **kwargs) -> None:
        nonlocal recovery_attempts
        recovery_attempts += 1
        raise OSError("persistent compensation failure")

    monkeypatch.setattr(activation, "_persistence_boundary", fail_at_audit)
    monkeypatch.setattr(activation, "_remove_audit", compensation_failure)
    monkeypatch.setattr(activation, "_remove_audit_direct", compensation_failure)
    monkeypatch.setattr(activation, "_restore_registry", compensation_failure)
    monkeypatch.setattr(activation, "_restore_registry_state", compensation_failure)
    monkeypatch.setattr(activation, "_restore_registry_direct", compensation_failure)
    monkeypatch.setattr(activation, "_restore_canonical", compensation_failure)
    monkeypatch.setattr(activation, "_restore_canonical_direct", compensation_failure)
    with pytest.raises(OSError, match="injected failure at audit"):
        activate_reviewed_proposal(_request(proposed_runbook))

    assert recovery_attempts == 0
    runbook_dir = runbook_path.parent
    assert runbook_path.read_bytes() == original_markdown
    assert (runbook_dir / ".index.json").read_bytes() == original_index
    assert not (runbook_dir / ".activations" / "plaud-production-repair.json").exists()
    assert not (runbook_dir / ".revisions" / "plaud-production-repair.md").exists()
    assert not (runbook_dir / ".revisions" / "plaud-production-repair.json").exists()
    assert _activation_events() == []
    with registry.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0


def test_real_audit_write_failure_precedes_all_persistent_recovery_layers(
    proposed_runbook, monkeypatch
):
    runbook_path = Path(proposed_runbook["active"].path)
    original_markdown = runbook_path.read_bytes()
    original_index = (runbook_path.parent / ".index.json").read_bytes()
    original_replace = secure_io.replace_file
    recovery_attempts = 0

    def fail_real_audit_write(directory, name, value, **kwargs):
        if directory.path.name == ".activations" and name == "plaud-production-repair.json":
            raise OSError("injected real audit write failure")
        return original_replace(directory, name, value, **kwargs)

    def persistent_recovery_failure(*args, **kwargs) -> None:
        nonlocal recovery_attempts
        recovery_attempts += 1
        raise OSError("persistent compensation failure")

    monkeypatch.setattr(secure_io, "replace_file", fail_real_audit_write)
    monkeypatch.setattr(activation, "_remove_audit", persistent_recovery_failure)
    monkeypatch.setattr(activation, "_remove_audit_direct", persistent_recovery_failure)
    monkeypatch.setattr(activation, "_restore_registry", persistent_recovery_failure)
    monkeypatch.setattr(activation, "_restore_registry_state", persistent_recovery_failure)
    monkeypatch.setattr(activation, "_restore_registry_direct", persistent_recovery_failure)
    monkeypatch.setattr(activation, "_restore_canonical", persistent_recovery_failure)
    monkeypatch.setattr(activation, "_restore_canonical_direct", persistent_recovery_failure)

    with pytest.raises(OSError, match="injected real audit write failure"):
        activate_reviewed_proposal(_request(proposed_runbook))

    runbook_dir = runbook_path.parent
    assert recovery_attempts == 0
    assert runbook_path.read_bytes() == original_markdown
    assert (runbook_dir / ".index.json").read_bytes() == original_index
    assert not (runbook_dir / ".activations" / "plaud-production-repair.json").exists()
    assert not (runbook_dir / ".revisions" / "plaud-production-repair.md").exists()
    assert not (runbook_dir / ".revisions" / "plaud-production-repair.json").exists()
    assert _activation_events() == []
    with registry.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0


def test_compensation_removes_its_identity_without_reverting_newer_same_hash_projection(
    proposed_runbook, monkeypatch
):
    original_assert_same_file = secure_io.assert_same_file
    assertion_count = 0

    def fail_after_commit_with_concurrent_projection(*args, **kwargs):
        nonlocal assertion_count
        assertion_count += 1
        if assertion_count != 3:
            return original_assert_same_file(*args, **kwargs)
        with registry.connect_closing() as conn:
            with write_txn(conn):
                conn.execute(
                    "UPDATE workflow_definitions SET description = ?, version = version + 1 WHERE id = ?",
                    ("concurrent registry update", "wf_daily_brief"),
                )
        raise OSError("injected post-commit failure")

    monkeypatch.setattr(secure_io, "assert_same_file", fail_after_commit_with_concurrent_projection)
    with pytest.raises(OSError, match="injected post-commit failure"):
        activate_reviewed_proposal(_request(proposed_runbook))

    with registry.connect_closing() as conn:
        assert registry.get_definition(conn, "wf_daily_brief").description == "concurrent registry update"
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0
    assert _activation_events() == []


@pytest.mark.parametrize(
    ("directory_name", "filename"),
    [
        (".revisions", "plaud-production-repair.md"),
        (".revisions", "plaud-production-repair.json"),
        ("daily-brief", "RUNBOOK.md"),
        ("daily-brief", ".index.json"),
        (".activations", "plaud-production-repair.json"),
    ],
)
def test_activation_artifact_write_errors_leave_no_partial_activation(
    proposed_runbook, monkeypatch, directory_name, filename
):
    runbook_path = Path(proposed_runbook["active"].path)
    original_markdown = runbook_path.read_bytes()
    original_index = (runbook_path.parent / ".index.json").read_bytes()
    original_replace = secure_io.replace_file

    def fail_target(directory, name, value, **kwargs):
        if directory.path.name == directory_name and name == filename:
            raise OSError(f"injected write failure for {directory_name}/{filename}")
        return original_replace(directory, name, value, **kwargs)

    monkeypatch.setattr(secure_io, "replace_file", fail_target)
    with pytest.raises(OSError, match="injected write failure"):
        activate_reviewed_proposal(_request(proposed_runbook))

    assert runbook_path.read_bytes() == original_markdown
    assert (runbook_path.parent / ".index.json").read_bytes() == original_index
    assert not (runbook_path.parent / ".activations" / "plaud-production-repair.json").exists()
    assert not (runbook_path.parent / ".revisions" / "plaud-production-repair.md").exists()
    assert not (runbook_path.parent / ".revisions" / "plaud-production-repair.json").exists()
    assert _activation_events() == []
    with registry.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0


def test_recovery_write_error_is_retried_without_leaving_candidate_residue(proposed_runbook, monkeypatch):
    runbook_path = Path(proposed_runbook["active"].path)
    original_markdown = runbook_path.read_bytes()
    original_replace = secure_io.replace_file
    candidate = proposed_runbook["candidate"].encode("utf-8")
    failed_recovery = False

    def fail_first_recovery(directory, name, value, **kwargs):
        nonlocal failed_recovery
        if name == "RUNBOOK.md" and value != candidate and not failed_recovery:
            failed_recovery = True
            raise OSError("injected recovery write failure")
        return original_replace(directory, name, value, **kwargs)

    def fail_at_index(boundary: str) -> None:
        if boundary == "index":
            raise OSError("injected failure at index")

    monkeypatch.setattr(secure_io, "replace_file", fail_first_recovery)
    monkeypatch.setattr(activation, "_persistence_boundary", fail_at_index)
    with pytest.raises(OSError, match="index"):
        activate_reviewed_proposal(_request(proposed_runbook))

    assert failed_recovery is True
    assert runbook_path.read_bytes() == original_markdown
    assert _activation_events() == []
    with registry.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0


def test_env_selected_forged_key_and_self_signed_claimed_approval_fail(proposed_runbook, monkeypatch, tmp_path):
    forged = Ed25519PrivateKey.generate()
    forged_path = tmp_path / "caller-selected.pem"
    forged_path.write_bytes(
        forged.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    forged_path.chmod(0o600)
    monkeypatch.setenv("HERMES_RUNBOOK_APPROVAL_PUBLIC_KEY", str(forged_path))
    forged_request = _request(proposed_runbook)
    forged_evidence = dict(forged_request.approval_evidence)
    _sign(forged_evidence, forged)
    with pytest.raises(PermissionError, match="signature"):
        activate_reviewed_proposal(
            ActivationRequest(**{**forged_request.__dict__, "approval_evidence": forged_evidence})
        )

    self_signed = _request(proposed_runbook)
    self_evidence = dict(self_signed.approval_evidence)
    _sign(self_evidence, Ed25519PrivateKey.generate())
    with pytest.raises(PermissionError, match="signature"):
        activate_reviewed_proposal(
            ActivationRequest(**{**self_signed.__dict__, "approval_evidence": self_evidence})
        )


def test_claimed_operator_string_without_installed_uid_binding_fails(proposed_runbook):
    policy_path = proposed_runbook["trust"] / "approval-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "approver": "elliott",
                "canonical_root": str(Path(proposed_runbook["active"].path).parents[2]),
                "operators": {"alina": {"uid": os.geteuid() + 1}},
            }
        ),
        encoding="utf-8",
    )
    policy_path.chmod(0o600)
    with pytest.raises(PermissionError, match="authenticated local agent"):
        activate_reviewed_proposal(_request(proposed_runbook))


@pytest.mark.parametrize("boundary", ["revision", "canonical", "index", "projection", "event", "audit"])
def test_persistence_failure_matrix_leaves_no_partial_activation(proposed_runbook, monkeypatch, boundary):
    original = Path(proposed_runbook["active"].path).read_bytes()

    def fail(name: str) -> None:
        if name == boundary:
            raise OSError(f"injected failure at {name}")

    monkeypatch.setattr(activation, "_persistence_boundary", fail)
    with pytest.raises(OSError, match=boundary):
        activate_reviewed_proposal(_request(proposed_runbook))
    assert Path(proposed_runbook["active"].path).read_bytes() == original
    assert _activation_events() == []
    audit = Path(proposed_runbook["active"].path).parent / ".activations" / "plaud-production-repair.json"
    assert not audit.exists()
    with registry.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runbook_activation_identities").fetchone()[0] == 0


def test_secure_paths_reject_symlinked_proposal_audit_and_evidence(proposed_runbook, tmp_path):
    proposal_dir = Path(proposed_runbook["active"].path).parent / ".proposals"
    proposal = proposal_dir / f"{proposed_runbook['proposal_id']}.md"
    replacement = tmp_path / "replacement.md"
    replacement.write_text(proposed_runbook["candidate"], encoding="utf-8")
    proposal.unlink()
    proposal.symlink_to(replacement)
    with pytest.raises(PermissionError, match="proposal"):
        activate_reviewed_proposal(_request(proposed_runbook))

    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    evidence.chmod(0o600)
    evidence_link = tmp_path / "evidence-link.json"
    evidence_link.symlink_to(evidence)
    with pytest.raises(PermissionError, match="unsafe"):
        activation.load_approval_evidence(evidence_link)


def test_secure_paths_reject_symlinked_audit_directory(proposed_runbook, tmp_path):
    runbook_dir = Path(proposed_runbook["active"].path).parent
    audit_target = tmp_path / "attacker-audits"
    audit_target.mkdir(mode=0o700)
    (runbook_dir / ".activations").symlink_to(audit_target, target_is_directory=True)
    with pytest.raises(PermissionError, match="trusted activation directory"):
        activate_reviewed_proposal(_request(proposed_runbook))


def test_activation_replay_is_atomic_and_concurrent(proposed_runbook):
    results: list[bool] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            results.append(activate_reviewed_proposal(_request(proposed_runbook)).replayed)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert results.count(False) == 1
    assert results.count(True) == 5
    assert len(_activation_events()) == 1


def test_non_equivalent_reuse_conflicts_and_stale_revision_is_rejected(proposed_runbook):
    activate_reviewed_proposal(_request(proposed_runbook))
    with pytest.raises(PermissionError, match="different activation"):
        activate_reviewed_proposal(
            _request(proposed_runbook, evidence={"approval_reference": "kanban:conflict"})
        )

    replacement_path = Path(proposed_runbook["active"].path)
    replacement_path.write_text(_markdown(title="Concurrent canonical change"), encoding="utf-8")
    with pytest.raises(PermissionError, match="active runbook revision"):
        activate_reviewed_proposal(_request(proposed_runbook, evidence={"approval_id": "fresh-approval"}))


def test_database_identity_rejects_non_equivalent_reuse(proposed_runbook):
    activate_reviewed_proposal(_request(proposed_runbook))
    with registry.connect_closing() as conn:
        with write_txn(conn):
            with pytest.raises(WorkflowConflictError, match="different activation"):
                registry.record_runbook_activation(
                    conn,
                    approval_id="plaud-production-repair",
                    identity={"approval_id": "plaud-production-repair", "operator": "sloane"},
                    workflow_id="wf_daily_brief",
                    payload={},
                )