"""Canonical Markdown runbook store for Hermes workflows."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import fcntl

from markdown import markdown as render_markdown_html

from hermes_cli.runbook_schema import (
    ParsedRunbook,
    RunbookValidationError,
    render_frontmatter,
    split_frontmatter,
)
from hermes_constants import get_default_hermes_root
from utils import atomic_json_write, atomic_replace


RUNBOOK_FILENAME = "RUNBOOK.md"


@dataclass(frozen=True)
class RunbookRecord:
    id: str
    slug: str
    title: str
    purpose: str
    owner_profile: str
    status: str
    path: str
    source_hash: str
    revision: str | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def runbook_root() -> Path:
    """Return the shared machine-level canonical runbook root."""
    return get_default_hermes_root() / "runbooks"


def runbook_path(slug: str, *, root: Path | None = None) -> Path:
    safe_slug = _validate_slug(slug)
    return (root or runbook_root()) / safe_slug / RUNBOOK_FILENAME


def read_runbook(path: Path) -> ParsedRunbook:
    text = path.read_text(encoding="utf-8")
    return split_frontmatter(text)


def list_runbooks(*, root: Path | None = None) -> list[RunbookRecord]:
    base = root or runbook_root()
    records: list[RunbookRecord] = []
    if not base.exists():
        return records
    for path in sorted(base.glob(f"*/{RUNBOOK_FILENAME}")):
        try:
            parsed = read_runbook(path)
        except (OSError, RunbookValidationError):
            continue
        records.append(_record_from(path, parsed))
    return records


def search_runbooks(query: str, *, root: Path | None = None) -> list[RunbookRecord]:
    needle = query.strip().lower()
    if not needle:
        return list_runbooks(root=root)
    result: list[RunbookRecord] = []
    for record in list_runbooks(root=root):
        haystack = " ".join(
            [record.slug, record.title, record.purpose, record.owner_profile, record.status]
        ).lower()
        if needle in haystack:
            result.append(record)
    return result


def get_runbook(slug: str, *, root: Path | None = None) -> RunbookRecord | None:
    """Return the canonical record for *slug*, or ``None`` when it is absent."""
    path = runbook_path(slug, root=root)
    if not path.exists():
        return None
    return _record_from(path, read_runbook(path))


def save_runbook(
    metadata: dict[str, Any],
    body: str,
    *,
    root: Path | None = None,
    approved_by: str | None = None,
) -> RunbookRecord:
    """Validate and atomically save a canonical RUNBOOK.md.

    If the target already exists, a revision snapshot is written before the
    new content replaces it. Invalid metadata never replaces the active file.
    """
    if not approved_by:
        raise PermissionError("active runbook saves require an approver")
    text = render_frontmatter(metadata, body)
    parsed = split_frontmatter(text)
    path = runbook_path(parsed.metadata["slug"], root=root)
    with _canonical_lock(path):
        if path.exists():
            _snapshot_revision(path, approved_by=approved_by)
        _atomic_text_write(path, text)
        _write_index_sidecar(path, parsed, approved_by=approved_by)
    return _record_from(path, parsed)


def save_runbook_markdown(
    markdown: str,
    *,
    root: Path | None = None,
    approved_by: str | None = None,
) -> RunbookRecord:
    """Activate already-validated Markdown without changing its content hash.

    Reviewed proposals bind approval to the exact candidate bytes.  Unlike
    :func:`save_runbook`, this preserves those bytes rather than rendering the
    parsed frontmatter again, so the canonical source hash remains the exact
    reviewed proposal SHA-256.
    """
    if not approved_by:
        raise PermissionError("active runbook saves require an approver")
    parsed = split_frontmatter(markdown)
    path = runbook_path(parsed.metadata["slug"], root=root)
    with _canonical_lock(path):
        if path.exists():
            _snapshot_revision(path, approved_by=approved_by)
        _atomic_text_write(path, markdown)
        _write_index_sidecar(path, parsed, approved_by=approved_by)
    return _record_from(path, parsed)


def activate_runbook_bytes(
    markdown: bytes,
    *,
    expected_revision: str,
    approved_by: str,
    root: Path | None = None,
) -> RunbookRecord:
    """CAS-activate exact reviewed bytes under a per-runbook advisory lock."""
    try:
        text = markdown.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PermissionError("reviewed proposal is not UTF-8") from exc
    parsed = split_frontmatter(text)
    path = runbook_path(parsed.metadata["slug"], root=root)
    with _canonical_lock(path):
        if path.parent.is_symlink():
            raise PermissionError("runbook directory must not be a symlink")
        if not path.exists() or path.is_symlink():
            raise PermissionError("canonical runbook is unavailable or unsafe")
        current = _record_from(path, read_runbook(path))
        if current.revision != expected_revision:
            raise PermissionError("active runbook revision does not match the approved revision")
        _snapshot_revision(path, approved_by=approved_by)
        _atomic_bytes_write(path, markdown)
        _write_index_sidecar(path, parsed, approved_by=approved_by)
        return _record_from(path, parsed)


def propose_edit(
    slug: str,
    markdown: str,
    *,
    root: Path | None = None,
    proposed_by: str,
    summary: str | None = None,
) -> Path:
    """Store a proposed runbook edit without activating it."""
    split_frontmatter(markdown)
    path = runbook_path(slug, root=root)
    proposal_id = _timestamp()
    proposals_dir = path.parent / ".proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = proposals_dir / f"{proposal_id}.md"
    _atomic_text_write(proposal_path, markdown)
    atomic_json_write(
        proposals_dir / f"{proposal_id}.json",
        {
            "proposed_by": proposed_by,
            "summary": summary,
            "created_at": proposal_id,
            "target": str(path),
            "sha256": _sha256_text(markdown),
        },
    )
    return proposal_path


def render_preview(markdown: str) -> str:
    split_frontmatter(markdown)
    body = markdown.split("\n---\n", 1)[1]
    return render_markdown_html(body, extensions=["tables", "fenced_code"])


def diff_against_current(
    slug: str,
    candidate_markdown: str,
    *,
    root: Path | None = None,
) -> str:
    path = runbook_path(slug, root=root)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            candidate_markdown.splitlines(keepends=True),
            fromfile=str(path),
            tofile="candidate",
        )
    )


def list_revisions(slug: str, *, root: Path | None = None) -> list[Path]:
    revisions = runbook_path(slug, root=root).parent / ".revisions"
    if not revisions.exists():
        return []
    return sorted(revisions.glob("*.md"))


def rollback_revision(
    slug: str,
    revision_path: Path,
    *,
    root: Path | None = None,
    approved_by: str,
) -> RunbookRecord:
    target = runbook_path(slug, root=root)
    markdown = revision_path.read_text(encoding="utf-8")
    parsed = split_frontmatter(markdown)
    if parsed.metadata["slug"] != slug:
        raise RunbookValidationError("revision slug does not match target")
    with _canonical_lock(target):
        if target.exists():
            _snapshot_revision(target, approved_by=approved_by)
        _atomic_text_write(target, markdown)
        _write_index_sidecar(target, parsed, approved_by=approved_by)
    return _record_from(target, parsed)


def _record_from(path: Path, parsed: ParsedRunbook) -> RunbookRecord:
    metadata = parsed.metadata
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
    return RunbookRecord(
        id=metadata["id"],
        slug=metadata["slug"],
        title=metadata["title"],
        purpose=metadata["purpose"],
        owner_profile=metadata["owner_profile"],
        status=metadata["status"],
        path=str(path),
        source_hash=source_hash,
        revision=str(metadata.get("source_revision") or f"sha256:{source_hash[:16]}"),
    )


def _snapshot_revision(path: Path, *, approved_by: str) -> Path:
    revisions = path.parent / ".revisions"
    revisions.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    revision = revisions / f"{stamp}.md"
    text = path.read_text(encoding="utf-8")
    _atomic_text_write(revision, text)
    atomic_json_write(
        revisions / f"{stamp}.json",
        {
            "approved_by": approved_by,
            "created_at": stamp,
            "source": str(path),
            "sha256": _sha256_text(text),
        },
    )
    return revision


@contextlib.contextmanager
def _canonical_lock(path: Path):
    """Serialize every canonical writer with audited activation recovery."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".activation.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_index_sidecar(path: Path, parsed: ParsedRunbook, *, approved_by: str) -> None:
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_json_write(
        path.parent / ".index.json",
        {
            "metadata": parsed.metadata,
            "source_hash": source_hash,
            "updated_at": _timestamp(),
            "approved_by": approved_by,
        },
    )


def _atomic_text_write(path: Path, text: str) -> None:
    _atomic_bytes_write(path, text.encode("utf-8"))


def _atomic_bytes_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_slug(slug: str) -> str:
    value = str(slug).strip().lower()
    if not value or "/" in value or "\\" in value or value.startswith("."):
        raise ValueError(f"invalid runbook slug: {slug!r}")
    return value
