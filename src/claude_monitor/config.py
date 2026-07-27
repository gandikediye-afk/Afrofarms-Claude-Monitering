"""Environment-only service configuration."""

from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when service configuration is missing or invalid."""


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h|d)?$")


def duration(value: str) -> float:
    match = _DURATION.fullmatch(value.strip())
    if not match:
        raise ConfigError(f"invalid duration: {value!r}")
    return float(match.group(1)) * {None: 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def boolean(value: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean: {value!r}")


@dataclass(frozen=True, slots=True)
class Config:
    compliance_access_key: str
    notion_token: str
    notion_ds_members: str
    notion_ds_projects: str
    notion_ds_sync_runs: str
    notion_ds_conversations: str
    notion_ds_activity: str
    notion_ds_messages: str | None
    compliance_base_url: str
    chat_poll_interval: float
    activity_poll_interval: float
    directory_poll_interval: float
    chat_page_limit: int
    activity_page_limit: int
    message_paging_threshold: int
    activity_type_allowlist: tuple[str, ...]
    notion_rate_limit_rps: float
    mirror_transcripts: bool
    download_attachments: bool
    redaction_enabled: bool
    state_db_path: Path
    production_readiness: str
    notion_parent_page_id: str
    retention_interval: float
    deletion_grace_period: float
    retention_class_days: dict[str, int]

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration without reading a dotenv file or printing secrets."""
        required = (
            "ANTHROPIC_COMPLIANCE_ACCESS_KEY", "NOTION_TOKEN", "NOTION_DS_MEMBERS",
            "NOTION_DS_PROJECTS", "NOTION_DS_SYNC_RUNS", "NOTION_DS_CONVERSATIONS",
            "NOTION_DS_ACTIVITY", "NOTION_PARENT_PAGE_ID", "PRODUCTION_READINESS",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ConfigError("missing required environment variables: " + ", ".join(missing))
        readiness = os.environ["PRODUCTION_READINESS"].strip()
        expected_readiness = "employee-notice:complete,lawful-basis:complete,access-approval:complete"
        if readiness != expected_readiness:
            raise ConfigError(
                "PRODUCTION_READINESS must explicitly confirm employee notice, lawful basis, "
                "and access approval; expected " + repr(expected_readiness)
            )
        get = os.environ.get
        try:
            chat_limit = int(get("CHAT_PAGE_LIMIT", "100"))
            activity_limit = int(get("ACTIVITY_PAGE_LIMIT", "1000"))
            threshold = int(get("MESSAGE_PAGING_THRESHOLD", "500"))
            rps = float(get("NOTION_RATE_LIMIT_RPS", "2.5"))
            retention_classes = json.loads(get("RETENTION_CLASS_DAYS", '{"standard":365,"sensitive":90,"extended":1095}'))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ConfigError("page limits, threshold, and rate limit must be numeric") from exc
        if not 1 <= chat_limit <= 100 or not 1 <= activity_limit <= 5000 or threshold < 1 or rps <= 0:
            raise ConfigError("limits out of range (chat 1..100, activity 1..5000, threshold/rps > 0)")
        if not isinstance(retention_classes, dict) or not retention_classes or any(
            not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) or v < 1
            for k, v in retention_classes.items()
        ):
            raise ConfigError("RETENTION_CLASS_DAYS must be a non-empty JSON object of positive integer days")
        base = get("COMPLIANCE_BASE_URL", "https://api.anthropic.com").rstrip("/")
        if not base.startswith("https://"):
            raise ConfigError("COMPLIANCE_BASE_URL must use HTTPS")
        redaction_enabled = boolean(get("REDACTION_ENABLED", "true"))
        if not redaction_enabled:
            raise ConfigError("REDACTION_ENABLED cannot be disabled in production")
        return cls(
            compliance_access_key=os.environ["ANTHROPIC_COMPLIANCE_ACCESS_KEY"],
            notion_token=os.environ["NOTION_TOKEN"],
            notion_ds_members=os.environ["NOTION_DS_MEMBERS"],
            notion_ds_projects=os.environ["NOTION_DS_PROJECTS"],
            notion_ds_sync_runs=os.environ["NOTION_DS_SYNC_RUNS"],
            notion_ds_conversations=os.environ["NOTION_DS_CONVERSATIONS"],
            notion_ds_activity=os.environ["NOTION_DS_ACTIVITY"],
            notion_ds_messages=get("NOTION_DS_MESSAGES") or None,
            compliance_base_url=base,
            chat_poll_interval=duration(get("CHAT_POLL_INTERVAL", "15m")),
            activity_poll_interval=duration(get("ACTIVITY_POLL_INTERVAL", "5m")),
            directory_poll_interval=duration(get("DIRECTORY_POLL_INTERVAL", "24h")),
            chat_page_limit=chat_limit, activity_page_limit=activity_limit,
            message_paging_threshold=threshold,
            activity_type_allowlist=tuple(x.strip() for x in get("ACTIVITY_TYPE_ALLOWLIST", "claude_chat_created,claude_file_uploaded,compliance_api_accessed").split(",") if x.strip()),
            notion_rate_limit_rps=rps,
            mirror_transcripts=boolean(get("MIRROR_TRANSCRIPTS", "true")),
            download_attachments=boolean(get("DOWNLOAD_ATTACHMENTS", "false")),
            redaction_enabled=redaction_enabled,
            state_db_path=Path(get("STATE_DB_PATH", "/var/lib/claude-monitor/state.db")),
            production_readiness=readiness,
            notion_parent_page_id=os.environ["NOTION_PARENT_PAGE_ID"].strip(),
            retention_interval=duration(get("RETENTION_INTERVAL", "24h")),
            deletion_grace_period=duration(get("DELETION_GRACE_PERIOD", "7d")),
            retention_class_days=retention_classes,
        )
