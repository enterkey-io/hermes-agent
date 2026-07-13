"""Hermes hooks for one shared GBrain and profile-private relationship memory."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .queue import enqueue_turn

logger = logging.getLogger(__name__)

MAX_WINDOW_CHARS = 5000
MAX_TURN_CHARS = 1200
RECALL_TIMEOUT_SECONDS = 3.0


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return " ".join(parts)
    return ""


def _build_window(conversation_history: list[dict[str, Any]] | None, user_message: str) -> str:
    turns: list[str] = []
    for message in (conversation_history or [])[-6:]:
        role = str(message.get("role", "")).lower()
        if role not in {"user", "assistant"}:
            continue
        text = _text_content(message.get("content", "")).strip()
        if text:
            turns.append(f"{role}: {text[:MAX_TURN_CHARS]}")
    if not turns or turns[-1] != f"user: {user_message[:MAX_TURN_CHARS]}":
        turns.append(f"user: {user_message[:MAX_TURN_CHARS]}")
    return "\n".join(turns)[-MAX_WINDOW_CHARS:]


def _call_gbrain(
    tool: str,
    params: dict[str, Any],
    *,
    timeout: float,
    source: str | None = None,
) -> Any:
    executable = os.environ.get("GBRAIN_BIN", "gbrain")
    command = [executable, "call"]
    if source:
        command.extend(["--source", source])
    command.extend([tool, json.dumps(params, ensure_ascii=True)])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gbrain {tool} failed with exit {completed.returncode}")
    return json.loads(completed.stdout)


def _format_context(result: Any) -> str:
    pages = result.get("pages", []) if isinstance(result, dict) else []
    if not isinstance(pages, list) or not pages:
        return ""
    lines = [
        "GBrain shared-context pointers (open/search before relying on details):",
    ]
    for page in pages[:3]:
        if not isinstance(page, dict):
            continue
        source = str(page.get("source_id", "unknown"))[:80]
        display = str(page.get("display", "record"))[:160]
        slug = str(page.get("slug", ""))[:240]
        synopsis = " ".join(str(page.get("synopsis", "")).split())[:500]
        line = f"- [{source}] {display} ({slug})"
        if synopsis:
            line += f": {synopsis}"
        lines.append(line)
    if len(lines) == 1:
        return ""
    lines.append(
        "Treat source labels as provenance; verify current operational truth in Craft before acting."
    )
    return "\n".join(lines)


_STOPWORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "does", "elliott",
    "for", "from", "how", "i", "in", "is", "it", "me", "my", "of", "on",
    "or", "should", "that", "the", "this", "to", "was", "what", "when", "with",
}


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        if raw in _STOPWORDS or len(raw) < 3:
            continue
        token = raw
        for suffix in ("ing", "ed", "es", "s"):
            if token.endswith(suffix) and len(token) > len(suffix) + 3:
                token = token[: -len(suffix)]
                break
        tokens.add(token)
    return tokens


def _format_facts(result: Any, query: str) -> str:
    rows = result.get("facts", []) if isinstance(result, dict) else []
    if not isinstance(rows, list) or not rows:
        return ""

    query_tokens = _tokens(query)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fact = " ".join(str(row.get("fact", "")).split())[:700]
        if not fact:
            continue
        overlap = len(query_tokens & _tokens(fact))
        if overlap == 0:
            continue
        confidence = row.get("confidence", 0.0)
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        notability = str(row.get("notability", "medium")).lower()
        notability_bonus = {"high": 0.5, "medium": 0.2, "low": 0.0}.get(notability, 0.0)
        ranked.append((overlap * 3.0 + confidence + notability_bonus, {**row, "fact": fact}))

    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = [row for _, row in ranked[:5]]
    if not selected:
        return ""

    lines = ["GBrain shared facts (cross-agent memory; verify mutable records in Craft):"]
    for row in selected:
        fact_id = row.get("id", "?")
        lines.append(f"- [shared fact {fact_id}] {row['fact']}")
    return "\n".join(lines)


def on_pre_llm_call(
    *,
    session_id: str = "",
    user_message: str = "",
    conversation_history: list[dict[str, Any]] | None = None,
    **_: Any,
) -> dict[str, str] | None:
    if not user_message.strip():
        return None
    window = _build_window(conversation_history, user_message)
    calls = {
        "pages": (
            "volunteer_context",
            {
                "window": window,
                "source_id": "__all__",
                "max_pages": 3,
                "min_confidence": 0.7,
                "session_id": session_id,
            },
            None,
        ),
        "facts": ("recall", {"limit": 50}, "shared"),
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gbrain-recall") as executor:
        futures = {
            name: executor.submit(
                _call_gbrain,
                tool,
                params,
                timeout=RECALL_TIMEOUT_SECONDS,
                source=source,
            )
            for name, (tool, params, source) in calls.items()
        }
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                logger.debug("GBrain shared %s recall unavailable: %s", name, type(exc).__name__)

    blocks = [
        _format_facts(results.get("facts"), window),
        _format_context(results.get("pages")),
    ]
    context = "\n\n".join(block for block in blocks if block)
    return {"context": context} if context else None


def _active_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def _active_profile(home: Path) -> str:
    explicit = os.environ.get("HERMES_PROFILE", "").strip()
    if explicit:
        return explicit
    return home.name if home.parent.name == "profiles" else "default"


def on_post_llm_call(
    *,
    session_id: str = "",
    turn_id: str = "",
    user_message: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    if not user_message.strip():
        return
    try:
        home = _active_home()
        enqueue_turn(
            home=home,
            profile=_active_profile(home),
            session_id=session_id,
            turn_id=turn_id,
            user_message=user_message,
            platform=platform,
        )
    except Exception as exc:
        logger.warning("GBrain capture enqueue failed: %s", type(exc).__name__)


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
