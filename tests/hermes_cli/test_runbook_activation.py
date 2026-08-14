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


@pytest.fixture
def proposed_runbook(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    private_key = Ed25519PrivateKey.generate()
    trust = tmp_path / "installed-runbook-approval"
    trust.mkdir(mode=0o700)
    (trust / "approval-policy.json").write_text(
        json.dumps({"approver": "elliott", "operators": {"alina": {"uid": os.geteuid()}}}),
        encoding="utf-8",
    )
    (trust / "elliott-ed25519.pem").write_bytes(
        private_key.public_key().public_bytes(
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
        "canonical_root": str(activation._canonical_root().resolve()),
        "registry_path": str((activation._canonical_root() / "workflow_registry.db").resolve()),
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


def test_activation_ignores_caller_selected_hermes_home(proposed_runbook, monkeypatch, tmp_path):
    attacker_home = tmp_path / "attacker-home"
    attacker_home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(attacker_home))

    result = activate_reviewed_proposal(_request(proposed_runbook))

    assert result.audit_path.is_relative_to(activation._canonical_root() / "runbooks")
    assert not (attacker_home / "runbooks").exists()
    assert not (attacker_home / "workflow_registry.db").exists()


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
        json.dumps({"approver": "elliott", "operators": {"alina": {"uid": os.geteuid() + 1}}}),
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