"""Mock Afro Farms org: members, a project, and four chats chosen to exercise
attribution, attachments, redaction, deletion, and block chunking."""

USERS = [
    {"id": "user_01AMINA", "name": "Amina Yusuf", "email_address": "amina@afrofarms.example",
     "role": "member", "status": "active", "created_at": "2026-01-04T06:00:00Z",
     "last_active_at": "2026-07-26T15:12:00Z", "organization_id": "org_01AFK"},
    {"id": "user_01JOSEPH", "name": "Joseph Kariuki", "email_address": "joseph@afrofarms.example",
     "role": "admin", "status": "active", "created_at": "2026-01-04T06:00:00Z",
     "last_active_at": "2026-07-27T05:40:00Z", "organization_id": "org_01AFK"},
    {"id": "user_01FATUMA", "name": "Fatuma Ali", "email_address": "fatuma@afrofarms.example",
     "role": "member", "status": "deactivated", "created_at": "2026-02-11T06:00:00Z",
     "last_active_at": "2026-06-30T09:00:00Z", "organization_id": "org_01AFK"},
]

PROJECTS = [
    {"id": "claude_proj_01HARVEST", "name": "Harvest Planning 2026",
     "owner_id": "user_01JOSEPH", "created_at": "2026-03-01T08:00:00Z",
     "attachments": [{"id": "claude_file_01A"}], "documents": [{"id": "claude_proj_doc_01B"}]},
]


def _chat(cid, name, user, updated, **extra):
    base = {"id": cid, "name": name, "created_at": "2026-07-20T08:00:00Z",
            "updated_at": updated, "deleted_at": None, "model": "claude-opus-5",
            "organization_uuid": "org-uuid-afk", "project_id": None,
            "href": f"https://claude.ai/chat/{cid}", "user": user}
    base.update(extra)
    return base


_AMINA = {"id": "user_01AMINA", "email_address": "amina@afrofarms.example"}
_JOSEPH = {"id": "user_01JOSEPH", "email_address": "joseph@afrofarms.example"}
_FATUMA = {"id": "user_01FATUMA", "email_address": "fatuma@afrofarms.example"}

CHATS = [
    _chat("claude_chat_01PLAIN", "Irrigation schedule for Block C", _AMINA,
          "2026-07-21T09:30:00Z"),
    _chat("claude_chat_02FILES", "Q3 yield model review", _JOSEPH,
          "2026-07-22T11:00:00Z", project_id="claude_proj_01HARVEST"),
    _chat("claude_chat_03SECRET", "Debugging the sensor uploader", _AMINA,
          "2026-07-23T14:20:00Z"),
    _chat("claude_chat_04DELETED", "Draft supplier terms", _FATUMA,
          "2026-07-24T07:05:00Z", deleted_at="2026-07-25T10:00:00Z"),
    _chat("claude_chat_05LONG", "Full agronomy handbook review", _JOSEPH,
          "2026-07-26T16:45:00Z"),
]


def _msg(mid, role, text, created, **extra):
    out = {"id": mid, "role": role, "created_at": created,
           "content": [{"type": "text", "text": text}]}
    out.update(extra)
    return out


MESSAGES = {
    "claude_chat_01PLAIN": {"id": "claude_chat_01PLAIN", "chat_messages": [
        _msg("m1", "user", "When should we irrigate Block C given the forecast?",
             "2026-07-21T09:29:00Z"),
        _msg("m2", "assistant", "Given 12mm of rain forecast Tuesday, hold irrigation "
             "until Wednesday evening and then apply 18mm.", "2026-07-21T09:30:00Z"),
    ]},
    "claude_chat_02FILES": {"id": "claude_chat_02FILES", "chat_messages": [
        _msg("m1", "user", "Review the attached yield model.", "2026-07-22T10:58:00Z",
             files=[{"id": "claude_file_01YIELD", "filename": "yield_model_q3.xlsx",
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}]),
        _msg("m2", "assistant", "The model overstates Block B by roughly 8%.",
             "2026-07-22T11:00:00Z",
             generated_files=[{"id": "claude_gen_file_01FIX", "filename": "corrected_model.csv",
                               "mime_type": "text/csv"}],
             artifacts=[{"id": "claude_artifact_01NOTE", "version_id": "claude_artifact_version_01NOTE1",
                         "title": "Yield Model Corrections", "artifact_type": "text/markdown"}]),
    ]},
    # Planted secret + a third-party address: both must be redacted, while the
    # chat owner's own address must survive as the attribution key.
    "claude_chat_03SECRET": {"id": "claude_chat_03SECRET", "chat_messages": [
        _msg("m1", "user", "The uploader fails. Config is api_key=sk-ant-api01-LIVEKEYDONOTLEAK123456 "
             "and it emails broker@othervendor.example on failure.", "2026-07-23T14:19:00Z"),
        _msg("m2", "assistant", "Rotate that key immediately, then retry.",
             "2026-07-23T14:20:00Z"),
    ]},
    "claude_chat_04DELETED": {"id": "claude_chat_04DELETED", "chat_messages": [
        _msg("m1", "user", "Draft payment terms for the Nakuru supplier.",
             "2026-07-24T07:04:00Z"),
    ]},
    # ~9k characters of assistant text -> forces paragraph chunking at 1800 chars
    # and more than one 100-block append.
    "claude_chat_05LONG": {"id": "claude_chat_05LONG", "chat_messages": [
        _msg("m1", "user", "Summarise the agronomy handbook.", "2026-07-26T16:40:00Z"),
        _msg("m2", "assistant", "Section notes. " + ("Rotate legumes before maize. " * 340),
             "2026-07-26T16:45:00Z"),
    ]},
}

ACTIVITIES = [
    {"id": "activity_01", "created_at": "2026-07-21T09:29:00Z", "organization_id": "org_01AFK",
     "type": "claude_chat_created", "claude_chat_id": "claude_chat_01PLAIN",
     "actor": {"type": "user_actor", "email_address": "amina@afrofarms.example",
               "user_id": "user_01AMINA", "ip_address": "197.248.0.9", "user_agent": "Mozilla/5.0"}},
    {"id": "activity_02", "created_at": "2026-07-22T10:58:00Z", "organization_id": "org_01AFK",
     "type": "claude_file_uploaded", "claude_chat_id": "claude_chat_02FILES",
     "actor": {"type": "user_actor", "email_address": "joseph@afrofarms.example",
               "user_id": "user_01JOSEPH", "ip_address": "197.248.0.11", "user_agent": "Mozilla/5.0"}},
    {"id": "activity_03", "created_at": "2026-07-27T05:00:00Z", "organization_id": None,
     "type": "compliance_api_accessed",
     "actor": {"type": "api_actor", "api_key_id": "apikey_01MONITOR",
               "ip_address": "10.0.0.4", "user_agent": "claude-monitor/0.1.0"}},
]


def fixtures(chats=None, messages=None):
    return {"chats": chats if chats is not None else CHATS,
            "messages": messages if messages is not None else MESSAGES,
            "activities": ACTIVITIES, "users": USERS, "projects": PROJECTS}
