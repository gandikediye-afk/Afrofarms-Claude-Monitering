# Notion schema

Five databases. Create them in dependency order — relations need the target's data source
ID to exist first.

```
1. Team Members       (no dependencies)
2. Claude Projects    (relates → Team Members)
3. Sync Runs          (no dependencies)
4. Conversations      (relates → Team Members, Claude Projects, Sync Runs)
5. Agent Activity     (relates → Team Members, Conversations)
6. Messages           (relates → Conversations)   — optional, off by default
```

DDL below uses the `CREATE TABLE` syntax accepted by Notion's `create-database` tool.
Substitute the real `ds_*` identifiers where placeholders appear.

> Two naming rules: a property literally named `id` or `url` must be written
> `userDefined:id` / `userDefined:URL` when set through the API. All property names below
> avoid those, so no prefixing is needed.

---

## 1. Team Members

One page per Claude user, sourced from `GET /v1/compliance/users`. Populate this **first** —
conversation rows relate to it.

```sql
CREATE TABLE (
  "Name"           TITLE,
  "User ID"        RICH_TEXT   COMMENT 'user_* — stable join key, survives email changes',
  "Email"          EMAIL,
  "Notion Person"  PEOPLE      COMMENT 'manually mapped; enables @mentions and person filters',
  "Organization"   RICH_TEXT   COMMENT 'linked org this member belongs to',
  "Claude Role"    SELECT('primary_owner':red, 'owner':orange, 'admin':yellow, 'member':blue, 'unknown':gray),
  "Groups"         MULTI_SELECT(),
  "Department"     SELECT('Farm Ops':green, 'Finance':blue, 'Sales':orange, 'Compliance':purple, 'Admin':gray),
  "First Seen"     DATE,
  "Last Active"    DATE,
  "Account Status" SELECT('active':green, 'deactivated':gray, 'unknown':default),
  "Synced At"      DATE
)
```

`User ID` is the primary join key everywhere in this pipeline. `Email` is a display
convenience only — it changes, `user_id` does not.

---

## 2. Claude Projects

```sql
CREATE TABLE (
  "Project Name"   TITLE,
  "Project ID"     RICH_TEXT   COMMENT 'claude_proj_*',
  "Owner"          RELATION('ds_team_members', DUAL 'Projects'),
  "Created"        DATE,
  "Attachments"    NUMBER,
  "Docs"           NUMBER,
  "Retention"      SELECT('standard':blue, 'extended':orange, 'legal-hold':red),
  "Synced At"      DATE
)
```

---

## 3. Sync Runs

The completeness attestation. List endpoints return no `total_count` and no checksum, so
this table is the record that a run covered what it claims to have covered.

```sql
CREATE TABLE (
  "Run"              TITLE       COMMENT 'e.g. chats-2026-07-27T08:15Z',
  "Plane"            SELECT('chats':blue, 'activities':purple, 'otel':green, 'directory':gray, 'backfill':orange),
  "Started"          DATE,
  "Finished"         DATE,
  "Start Cursor"     RICH_TEXT,
  "End Cursor"       RICH_TEXT,
  "Pages"            NUMBER,
  "Records"          NUMBER,
  "Chats Created"    NUMBER,
  "Chats Updated"    NUMBER,
  "Chats Unchanged"  NUMBER,
  "Errors"           NUMBER,
  "Final Request ID" RICH_TEXT   COMMENT 'request-id header of the last page — chain of custody',
  "Outcome"          SELECT('ok':green, 'partial':yellow, 'failed':red)
)
```

---

## 4. Conversations

The core table. One page per `claude_chat_*`. The transcript lives in the **page body**,
not in a property — properties cannot hold a conversation.

```sql
CREATE TABLE (
  "Title"            TITLE       COMMENT 'chat.name, or "(untitled)" — never a message excerpt',
  "Chat ID"          RICH_TEXT   COMMENT 'claude_chat_* — idempotency key',
  "Member"           RELATION('ds_team_members', DUAL 'Conversations'),
  "Member Email"     EMAIL       COMMENT 'denormalized for quick filtering',
  "Surface"          SELECT('claude.ai':blue, 'Claude Code':purple, 'Cowork':green, 'Excel':orange, 'Word':blue, 'PowerPoint':red, 'Outlook':yellow),
  "Model"            SELECT('claude-opus-5':purple, 'claude-sonnet-5':blue, 'claude-haiku-4-5':green, 'other':gray),
  "Project"          RELATION('ds_claude_projects', DUAL 'Conversations'),
  "Started"          DATE        COMMENT 'chat.created_at',
  "Last Activity"    DATE        COMMENT 'chat.updated_at — drives the incremental cursor',
  "Messages"         NUMBER,
  "Member Turns"     NUMBER,
  "Claude Turns"     NUMBER,
  "Transcript Chars" NUMBER,
  "Attachments"      NUMBER      COMMENT 'count of files across all messages',
  "Generated Files"  NUMBER,
  "Artifacts"        NUMBER,
  "Open in Claude"   URL         COMMENT 'chat.href',
  "Deleted At"       DATE        COMMENT 'set when the member soft-deletes in claude.ai',
  "Tombstoned"       CHECKBOX    COMMENT 'true when hard-deleted or aged out — content no longer retrievable',
  "Retention Class"  SELECT('standard':blue, 'extended':orange, 'legal-hold':red),
  "Flags"            MULTI_SELECT('possible-pii':red, 'possible-secret':red, 'financial':orange, 'personnel':purple, 'redacted':gray),
  "Review Status"    STATUS,
  "Content Hash"     RICH_TEXT   COMMENT 'sha256 — skip the write when unchanged',
  "Last Synced"      DATE,
  "Sync Run"         RELATION('ds_sync_runs', DUAL 'Conversations')
)
```

### Page body layout

```
> Callout — Member · Surface · Model · Started · Open in Claude

### Member · 2026-04-10T08:09:10Z
<paragraph chunks, ≤1800 chars each>
  📎 dashboard_mockup_v1.pdf (application/pdf) · claude_file_01Ua…

### Claude · 2026-04-10T08:09:11Z
<paragraph chunks>
  📄 Dashboard Requirements Draft · text/markdown · claude_artifact_version_01Km…
```

Append in batches of ≤100 blocks. Do not exceed 2,000 characters in one rich-text object.

### Recommended views

| View | Type | Configure |
|---|---|---|
| Recent activity | table | `SORT BY "Last Activity" DESC` |
| By member | board | `GROUP BY "Member Email"` |
| Needs review | table | `FILTER "Flags" contains "possible-secret"` |
| Deleted by member | table | `FILTER "Deleted At" is not empty` |
| Volume over time | chart | `CHART line`, `AGGREGATE count`, by `Started` |

---

## 5. Agent Activity

Both the Compliance Activity Feed and the OTel push plane land here. **No conversation
content** — this table is metadata by construction, which is what makes it safe to share more
widely than Conversations.

```sql
CREATE TABLE (
  "Event"          TITLE       COMMENT 'human label, e.g. "claude_chat_created · amina@"',
  "Event ID"       RICH_TEXT   COMMENT 'activity_* or OTel record id — dedup key',
  "Plane"          SELECT('compliance-activity':blue, 'otel-event':purple),
  "Event Type"     RICH_TEXT   COMMENT 'raw type string; keep unknown values verbatim',
  "Actor"          RELATION('ds_team_members', DUAL 'Activity'),
  "Actor Email"    EMAIL,
  "Actor Type"     SELECT('user_actor':blue, 'api_actor':orange, 'admin_api_key_actor':red, 'unauthenticated_user_actor':yellow, 'anthropic_actor':gray, 'scim_directory_sync_actor':green),
  "Surface"        SELECT('claude.ai':blue, 'Excel':orange, 'Word':blue, 'PowerPoint':red, 'Outlook':yellow, 'Claude Code':purple, 'Cowork':green),
  "Occurred At"    DATE,
  "Prompt ID"      RICH_TEXT   COMMENT 'OTel prompt.id — groups every event from one prompt',
  "Conversation"   RELATION('ds_conversations', DUAL 'Activity'),
  "IP Address"     RICH_TEXT,
  "User Agent"     RICH_TEXT,
  "Cells Read"     NUMBER      COMMENT 'Excel: sheet.cells_read',
  "Cells Written"  NUMBER      COMMENT 'Excel: sheet.cells_written',
  "Cells Copied"   NUMBER      COMMENT 'Excel: sheet.cells_copied',
  "Attributes"     RICH_TEXT   COMMENT 'JSON of unmapped attributes — forward compatibility',
  "Ingested At"    DATE
)
```

`Attributes` is deliberate: new event types and attributes ship without notice, and dropping
them loses signal permanently. Keep the raw JSON, promote fields to real properties when they
prove useful.

### Volume warning

The Activity Feed records **every** authentication, chat, file, project, administrative, and
platform action. At full fidelity this table will dwarf the others and can outgrow what Notion
handles comfortably. Filter at ingest with `activity_types[]` to a named allowlist, and send
the unfiltered stream to a log store if you need it. Suggested starting allowlist:

```
claude_chat_created
claude_file_uploaded
compliance_api_accessed
sso_login_initiated
```

plus chat-deletion and member-lifecycle types.

---

## 6. Messages — optional, off by default

Turn on only if per-message filtering in Notion proves necessary. Expect 20–50 pages per
conversation and a proportional increase in write volume against the 3 rps budget.

```sql
CREATE TABLE (
  "Excerpt"         TITLE       COMMENT 'first ~80 chars — the full text lives in the page body',
  "Message ID"      RICH_TEXT   COMMENT 'claude_chat_msg_*',
  "Conversation"    RELATION('ds_conversations', DUAL 'Message Rows'),
  "Role"            SELECT('user':blue, 'assistant':green),
  "Sent At"         DATE        COMMENT 'assistant messages: when generation finished',
  "Member Email"    EMAIL,
  "Characters"      NUMBER,
  "Attachments"     NUMBER,
  "Generated Files" NUMBER,
  "Artifacts"       NUMBER,
  "Flags"           MULTI_SELECT('possible-pii':red, 'possible-secret':red, 'redacted':gray)
)
```

---

## Rollups to add after relations exist

Rollups need the relation property to exist first, so add them in a second
`update-data-source` pass:

| On | Property | Definition |
|---|---|---|
| Team Members | Chat Count | `ROLLUP('Conversations', 'Chat ID', 'count')` |
| Team Members | Total Messages | `ROLLUP('Conversations', 'Messages', 'sum')` |
| Team Members | Latest Chat | `ROLLUP('Conversations', 'Last Activity', 'latest_date')` |
| Claude Projects | Chats | `ROLLUP('Conversations', 'Chat ID', 'count')` |

---

## Field mapping reference

| Notion property | Source | Path |
|---|---|---|
| Chat ID | chats list | `data[].id` |
| Title | chats list | `data[].name` |
| Member Email | chats list | `data[].user.email_address` |
| Member (relation) | chats list → members | `data[].user.id` → Team Members row |
| Model | chats list | `data[].model` |
| Project | chats list | `data[].project_id` |
| Started | chats list | `data[].created_at` |
| Last Activity | chats list | `data[].updated_at` |
| Deleted At | chats list | `data[].deleted_at` |
| Open in Claude | chats list | `data[].href` |
| Messages / turns | messages | `chat_messages[]` count, grouped by `role` |
| Attachments | messages | `Σ len(chat_messages[].files)` |
| Generated Files | messages | `Σ len(chat_messages[].generated_files)` |
| Artifacts | messages | `Σ len(chat_messages[].artifacts)` |
| Transcript blocks | messages | `chat_messages[].content[].text` |
| Actor Email | activities | `data[].actor.email_address` |
| Actor Type | activities | `data[].actor.type` |
| Event Type | activities | `data[].type` |
| Occurred At | activities | `data[].created_at` |
| Prompt ID | OTel | `prompt.id` attribute |
| Cells Read/Written/Copied | OTel | `sheet.cells_*` attributes |
