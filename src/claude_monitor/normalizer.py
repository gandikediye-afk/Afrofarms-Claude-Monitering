"""Deterministic message normalization and pre-persistence redaction."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

SECRET_PATTERNS = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s,;]{8,}"),
)
PII_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b(?:\+?\d[ .()-]?){8,15}\d\b"),
)


def redact(text: str, enabled: bool = True) -> tuple[str, set[str]]:
    if not enabled: return text, set()
    flags: set[str] = set()
    for pattern in SECRET_PATTERNS:
        text, count = pattern.subn("[REDACTED SECRET]", text)
        if count: flags.update(("possible-secret", "redacted"))
    for pattern in PII_PATTERNS:
        text, count = pattern.subn("[REDACTED PII]", text)
        if count: flags.update(("possible-pii", "redacted"))
    return text, flags


def _metadata(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: item.get(key) for key in keys if item.get(key) is not None}


def normalize_messages(messages: Iterable[dict[str, Any]], redaction_enabled: bool = True) -> dict[str, Any]:
    """Return canonical, redacted messages; signed URLs and binary bodies are discarded."""
    normalized, flags = [], set()
    for message in sorted(messages, key=lambda m: (str(m.get("created_at", "")), str(m.get("id", "")))):
        parts = []
        for part in message.get("content") or []:
            kind = str(part.get("type", "unknown"))
            value = part.get("text")
            if value is None:
                value = json.dumps({k: v for k, v in part.items() if k not in {"url", "signed_url"}}, sort_keys=True, separators=(",", ":"))
            value, found = redact(str(value), redaction_enabled); flags |= found
            parts.append({"type": kind, "text": value})
        normalized.append({
            "id": message.get("id"), "role": message.get("role", "unknown"),
            "created_at": message.get("created_at"), "content": parts,
            "files": [_metadata(x, ("id", "filename", "mime_type", "size_bytes")) for x in message.get("files") or []],
            "generated_files": [_metadata(x, ("id", "filename", "mime_type", "size_bytes")) for x in message.get("generated_files") or []],
            "artifacts": [_metadata(x, ("id", "title", "artifact_type", "version_id")) for x in message.get("artifacts") or []],
        })
    roles: dict[str, int] = {}
    for item in normalized: roles[item["role"]] = roles.get(item["role"], 0) + 1
    return {"messages": normalized, "roles": roles,
            "attachments": sum(len(m["files"]) for m in normalized),
            "generated_files": sum(len(m["generated_files"]) for m in normalized),
            "artifacts": sum(len(m["artifacts"]) for m in normalized),
            "flags": sorted(flags)}


def canonical_chat(chat: dict[str, Any], normalized: dict[str, Any]) -> tuple[str, str]:
    stable_chat = {key: chat.get(key) for key in ("id", "name", "user", "project_id", "model", "created_at", "updated_at", "deleted_at", "href")}
    document = json.dumps({"chat": stable_chat, **normalized}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return document, hashlib.sha256(document.encode()).hexdigest()


def transcript_blocks(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for message in normalized["messages"]:
        who = "Member" if message["role"] == "user" else "Claude"
        blocks.append(_block("heading_3", f"{who} · {message.get('created_at') or ''}"))
        text = "".join(part["text"] for part in message["content"])
        for start in range(0, len(text), 1800): blocks.append(_block("paragraph", text[start:start + 1800]))
        for file in message["files"]: blocks.append(_block("bulleted_list_item", f"📎 {file.get('filename', '(unnamed)')} ({file.get('mime_type', 'unknown')}) · {file.get('id', '')}"))
        for file in message["generated_files"]: blocks.append(_block("bulleted_list_item", f"📎 Generated: {file.get('filename', '(unnamed)')} ({file.get('mime_type', 'unknown')}) · {file.get('id', '')}"))
        for artifact in message["artifacts"]: blocks.append(_block("bulleted_list_item", f"📄 {artifact.get('title', '(untitled)')} · {artifact.get('artifact_type', 'unknown')} · {artifact.get('version_id', '')}"))
    return blocks


def _block(kind: str, text: str) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": text}}]}}
