"""Real pending requests for native prompt/callback correlation tests."""

from tools import approval


def pending_pair(monkeypatch, session_key):
    older = approval._ApprovalEntry({"command": "older fixture", "request_id": "older-request"})
    newer = approval._ApprovalEntry({"command": "newer fixture", "request_id": "newer-request"})
    monkeypatch.setattr(approval, "_gateway_queues", {session_key: [older, newer]})
    monkeypatch.setattr(approval, "_gateway_expired", {})
    return older, newer
