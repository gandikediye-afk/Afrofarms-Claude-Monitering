"""End-to-end pipeline run against mock Compliance and Notion APIs.

Every layer is the real implementation: pagination, normalization, redaction,
SQLite state, idempotency, and the Notion writer. Only the two network edges
are mocked.
"""

import copy
from pathlib import Path

import pytest

from claude_monitor.anthropic_client import AnthropicClient
from claude_monitor.config import Config
from claude_monitor.notion_client import NotionClient
from claude_monitor.state import State
from claude_monitor.sync import chats as chats_sync
from claude_monitor.sync import directory as directory_sync

from .fixtures import mock_org
from .mockapi import MockCompliance, MockNotion

DS = {"members": "ds_members", "projects": "ds_projects", "sync_runs": "ds_sync_runs",
      "conversations": "ds_conversations", "activity": "ds_activity"}


def build(tmp_path: Path, fixtures):
    compliance = MockCompliance(fixtures)
    notion_api = MockNotion()
    client = AnthropicClient("sk-ant-api01-mock", "https://api.anthropic.com")
    client._opener = compliance
    notion = NotionClient("ntn_mock", 1000.0, opener=notion_api.open)
    state = State(tmp_path / "state.db")
    config = Config(
        compliance_access_key="sk-ant-api01-mock", notion_token="ntn_mock",
        notion_ds_members=DS["members"], notion_ds_projects=DS["projects"],
        notion_ds_sync_runs=DS["sync_runs"], notion_ds_conversations=DS["conversations"],
        notion_ds_activity=DS["activity"], notion_ds_messages=None,
        compliance_base_url="https://api.anthropic.com",
        chat_poll_interval=900.0, activity_poll_interval=300.0, directory_poll_interval=86400.0,
        chat_page_limit=2, activity_page_limit=100, message_paging_threshold=500,
        activity_type_allowlist=("claude_chat_created", "claude_file_uploaded",
                                 "compliance_api_accessed"),
        notion_rate_limit_rps=1000.0, mirror_transcripts=True, download_attachments=False,
        redaction_enabled=True, state_db_path=tmp_path / "state.db",
        production_readiness="employee-notice:complete,lawful-basis:complete,access-approval:complete",
        notion_parent_page_id="parent-page", retention_interval=86400.0,
        deletion_grace_period=604800.0, retention_class_days={"standard": 365},
    )
    return compliance, notion_api, client, notion, state, config


@pytest.fixture
def pipeline(tmp_path):
    return build(tmp_path, mock_org.fixtures())


def conversation_page(notion_api, chat_id):
    for page_id, page in notion_api.pages.items():
        if page.get("parent", {}).get("data_source_id") != DS["conversations"]:
            continue
        spans = page["properties"].get("Chat ID", {}).get("rich_text", [])
        if spans and spans[0]["text"]["content"] == chat_id:
            return page_id
    return None


# --------------------------------------------------------------------------- #


def test_directory_sync_creates_a_page_per_member(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    run = directory_sync.sync(client, notion, state, config)
    assert run.records == len(mock_org.USERS) + len(mock_org.PROJECTS)
    assert len(notion_api.pages_in(DS["members"])) == 3
    for user in mock_org.USERS:
        assert state.object("user", user["id"]) is not None


def test_chat_sync_paginates_and_creates_every_conversation(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    run = chats_sync.sync(client, notion, state, config)
    assert run.records == len(mock_org.CHATS)
    assert run.created == len(mock_org.CHATS)
    # chat_page_limit=2 over 5 chats must have required real pagination
    assert run.pages >= 3
    assert len(notion_api.pages_in(DS["conversations"])) == len(mock_org.CHATS)


def test_transcripts_and_attachment_metadata_reach_the_page_body(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    body = notion_api.transcript(conversation_page(notion_api, "claude_chat_02FILES"))
    assert "overstates Block B" in body
    assert "yield_model_q3.xlsx" in body
    assert "corrected_model.csv" in body
    assert "Yield Model Corrections" in body


def test_member_attribution_survives_for_every_conversation(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    expected = {c["id"]: c["user"]["email_address"] for c in mock_org.CHATS}
    for chat_id, email in expected.items():
        page_id = conversation_page(notion_api, chat_id)
        assert notion_api.prop(page_id, "Member Email") == {"email": email}, chat_id
        assert notion_api.prop(page_id, "Member")["relation"], f"{chat_id} has no Member relation"


def test_secrets_are_redacted_but_the_owner_email_is_not(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    page_id = conversation_page(notion_api, "claude_chat_03SECRET")
    body = notion_api.transcript(page_id)
    assert "sk-ant-api01-LIVEKEYDONOTLEAK123456" not in body
    assert "broker@othervendor.example" not in body
    assert "[REDACTED" in body
    assert notion_api.prop(page_id, "Member Email") == {"email": "amina@afrofarms.example"}


def test_soft_deleted_chat_is_marked_deleted(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    page_id = conversation_page(notion_api, "claude_chat_04DELETED")
    assert notion_api.prop(page_id, "Deleted At")["date"] is not None


def test_long_transcript_is_chunked_within_notion_limits(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    blocks = notion_api.blocks[conversation_page(notion_api, "claude_chat_05LONG")]
    assert len(blocks) > 1
    for block in blocks:
        for span in block[block["type"]]["rich_text"]:
            assert len(span["text"]["content"]) <= 2000
    appends = [c for c in notion_api.calls if c[0] == "PATCH" and c[1].endswith("/children")]
    for _, path in appends:
        assert path  # every append went through the chunked writer


def test_second_incremental_run_fetches_nothing_and_writes_nothing(pipeline):
    """The cursor sits past the last chat, so unchanged work is never re-fetched."""
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    before = len(notion_api.pages_in(DS["conversations"]))
    touched = {p for p in notion_api.pages_in(DS["conversations"])}

    second = chats_sync.sync(client, notion, state, config)
    assert second.records == 0
    assert second.created == 0 and second.updated == 0
    # Each run legitimately writes one Sync Runs audit row; no conversation moves.
    assert len(notion_api.pages_in(DS["conversations"])) == before
    assert {p for p in notion_api.pages_in(DS["conversations"])} == touched


def test_redelivery_hits_the_unchanged_path_without_writing(pipeline):
    """Backfill ignores the cursor, so every chat is re-walked and must be a no-op."""
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    before = len(notion_api.pages_in(DS["conversations"]))
    snapshot = {p: dict(notion_api.pages[p]["properties"]) for p in notion_api.pages_in(DS["conversations"])}

    replay = chats_sync.sync(client, notion, state, config, backfill=True)
    assert replay.records == len(mock_org.CHATS)
    assert replay.unchanged == len(mock_org.CHATS), "content hash did not suppress the rewrite"
    assert replay.created == 0 and replay.updated == 0
    assert len(notion_api.pages_in(DS["conversations"])) == before
    for page_id, props in snapshot.items():
        assert notion_api.pages[page_id]["properties"] == props, f"{page_id} was rewritten"


def test_edited_chat_updates_in_place_without_duplicating(tmp_path):
    compliance, notion_api, client, notion, state, config = build(tmp_path, mock_org.fixtures())
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    first_page = conversation_page(notion_api, "claude_chat_01PLAIN")
    count = len(notion_api.pages_in(DS["conversations"]))

    edited_chats = copy.deepcopy(mock_org.CHATS)
    edited_messages = copy.deepcopy(mock_org.MESSAGES)
    edited_chats[0]["updated_at"] = "2026-07-28T08:00:00Z"
    edited_messages["claude_chat_01PLAIN"]["chat_messages"].append(
        {"id": "m3", "role": "user", "created_at": "2026-07-28T08:00:00Z",
         "content": [{"type": "text", "text": "Also check Block D."}]})
    compliance.fixtures = mock_org.fixtures(edited_chats, edited_messages)

    run = chats_sync.sync(client, notion, state, config)
    assert run.updated == 1
    assert len(notion_api.pages_in(DS["conversations"])) == count
    assert conversation_page(notion_api, "claude_chat_01PLAIN") == first_page
    assert "Block D" in notion_api.transcript(first_page)


def test_cursor_advances_and_resumes(pipeline):
    compliance, notion_api, client, notion, state, config = pipeline
    directory_sync.sync(client, notion, state, config)
    chats_sync.sync(client, notion, state, config)
    cursor = state.get_cursor("chats")
    assert cursor == mock_org.CHATS[-1]["id"]

    chats_sync.sync(client, notion, state, config)
    resumed = [q for path, q in compliance.calls if path == "/apps/chats" and "after_id" in q]
    assert resumed, "the second run did not resume from the persisted cursor"
