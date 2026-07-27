# Design — Claude → Notion conversation monitoring

## 1. What Claude actually emits

### 1.1 There is no conversation webhook

Anthropic ships webhooks, but they belong to the Managed Agents product: session, vault,
environment, and memory-store lifecycle events, configured in Console under
`Manage → Webhooks`. They carry an event `type` and `id` only — the documented pattern is
that the receiver then does a `GET` for the object. Nothing in that event set carries
claude.ai chat content, and there is no subscription that does.

### 1.2 The Compliance API is poll-only, and it has the transcripts

Everything lives under `/v1/compliance/*` on `https://api.anthropic.com`, authenticated
with `x-api-key`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/compliance/activities` | Activity Feed — auth, chat, file, project, admin events |
| `GET /v1/compliance/apps/chats` | Chat metadata, org-wide |
| `GET /v1/compliance/apps/chats/{chat_id}/messages` | **Full message content** |
| `GET /v1/compliance/apps/chats/files/{id}/content` | Uploaded file bytes |
| `GET /v1/compliance/apps/chats/generated_files/{id}/content` | Tool-generated file bytes |
| `GET /v1/compliance/apps/artifacts/{version_id}/content` | Artifact text, per version |
| `GET /v1/compliance/apps/projects…` | Projects, attachments, documents |
| `GET /v1/compliance/organizations\|users\|roles\|groups` | Directory |

Key facts that constrain the design:

- **600 requests/minute** shared across every key, every linked org, and every
  `/v1/compliance/*` endpoint, per parent organization.
- Activities are queryable **within 1 minute** and retained **6 years**.
- Chat and file content is retained per **your** claude.ai retention policy, not
  Anthropic's — content can age out from under you.
- Delivery is **at-least-once**. Deduplicate on `id`.
- Two key types: a **Compliance Access Key** (`sk-ant-api01-…`, created in claude.ai)
  reaches everything; an **Admin API key** (`sk-ant-admin01-…`) reaches the Activity Feed
  only and gets `403` on content endpoints.
- Scopes: `read:compliance_activities`, `read:compliance_user_data`, and
  `delete:compliance_user_data` (which this pipeline must **not** be granted).

### 1.3 OTLP is push, and it has no transcripts

The `Monitoring / OTLP endpoint` field in `Organization settings → Office agents` makes
Claude post OpenTelemetry **log records** to a collector you run. Office agents export
five event types, structurally identical to Claude Code and Cowork events, all sharing a
`prompt.id` UUID that ties together every event from one user prompt. Office agents emit
**logs/events only** — no metrics, no traces. Surface-specific attributes exist for Excel
(`sheet.cells_read`, `sheet.cells_written`, `sheet.cells_copied`) and Word (a document-edit
funnel); PowerPoint and Outlook carry only the common schema.

This plane gives you *that a member used Claude in Excel at 14:02, touching 400 cells* —
not what they asked or what Claude said. Treat it as activity metadata, and as the only
coverage you get for surfaces the Compliance API's content endpoints do not serve.

### 1.4 Coverage gaps to state plainly

The Compliance API's content endpoints serve **claude.ai data only**. It does not include
prompt text or model responses from Claude Console / Claude API workloads, content already
removed by retention, or content hard-deleted through the API. Claude Code and Cowork
conversation content is not retrievable through it. So "all conversations of all team
members" resolves in practice to:

- **claude.ai chats** → full transcripts, via the pull plane.
- **Office agents, Claude Code, Cowork** → event metadata only, via the push plane.
- **Direct API/Console usage** → not covered by either. Out of scope.

---

## 2. Architecture

```
                    PULL PLANE                              PUSH PLANE
        ┌──────────────────────────────┐        ┌──────────────────────────────┐
        │  api.anthropic.com           │        │  Claude Office agents        │
        │  /v1/compliance/*            │        │  Claude Code · Cowork        │
        └──────────────┬───────────────┘        └───────────────┬──────────────┘
                       │ poll (cursored)                        │ OTLP/HTTP POST
                       ▼                                        ▼
        ┌──────────────────────────────┐        ┌──────────────────────────────┐
        │  compliance-poller           │        │  otlp-receiver               │
        │  · activities cursor         │        │  · shared-secret header      │
        │  · chats cursor (updated_at) │        │  · CORS preflight            │
        │  · message + artifact fetch  │        │  · 200 fast, then enqueue    │
        └──────────────┬───────────────┘        └───────────────┬──────────────┘
                       └──────────────┬─────────────────────────┘
                                      ▼
                          ┌───────────────────────┐
                          │  normalizer           │
                          │  · canonical record   │
                          │  · redaction / DLP    │
                          │  · content hash       │
                          └───────────┬───────────┘
                                      ▼
                          ┌───────────────────────┐      ┌────────────────────┐
                          │  durable queue        │◄────►│  state store       │
                          │  (at-least-once)      │      │  cursors           │
                          └───────────┬───────────┘      │  chat_id→page_id   │
                                      ▼                   │  content hashes    │
                          ┌───────────────────────┐      └────────────────────┘
                          │  notion-writer        │
                          │  · 2.5 rps token bkt  │
                          │  · upsert by page_id  │
                          │  · block chunking     │
                          └───────────┬───────────┘
                                      ▼
                            Notion (5 databases)
```

Five components. The state store is the load-bearing one — without it you cannot resume,
cannot deduplicate, and cannot avoid rewriting unchanged chats every run.

### 2.1 State store

SQLite on a persistent volume is sufficient at Afro Farms' headcount; Postgres if you want
concurrent workers. Three tables:

```sql
CREATE TABLE cursors (
  plane       TEXT PRIMARY KEY,   -- 'activities' | 'chats'
  cursor      TEXT,               -- opaque; never parse
  updated_at  TEXT NOT NULL
);

CREATE TABLE chat_index (
  chat_id        TEXT PRIMARY KEY,   -- claude_chat_*
  notion_page_id TEXT NOT NULL,
  content_hash   TEXT NOT NULL,      -- sha256 of canonicalized transcript
  message_count  INTEGER NOT NULL,
  deleted_at     TEXT,
  last_synced_at TEXT NOT NULL
);

CREATE TABLE run_log (
  run_id       TEXT PRIMARY KEY,
  plane        TEXT NOT NULL,
  start_cursor TEXT,
  end_cursor   TEXT,
  records      INTEGER NOT NULL,
  final_request_id TEXT,             -- chain-of-custody attestation
  outcome      TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT
);
```

`chat_index` is what makes the Notion write idempotent. **Never search Notion to find the
page for a chat** — search is eventually consistent and burns the write budget.

---

## 3. Pull plane

### 3.1 Chat sync — the primary loop

List chats org-wide sorted by `updated_at`, ascending, and walk forward with `after_id`.
Omitting `user_ids[]` gives every chat under the parent organization, so one loop covers
the whole team without enumerating members first.

```python
CHATS = "https://api.anthropic.com/v1/compliance/apps/chats"

def sync_chats(state):
    cursor = state.get_cursor("chats")
    params = {"order_by": "updated_at", "limit": 100}
    if cursor:
        params["after_id"] = cursor

    pages = 0
    while True:
        page = api_get(CHATS, params)          # retries 429/5xx with same cursor
        for chat in page["data"]:
            enqueue("chat", chat)              # idempotent, keyed on chat["id"]
        pages += 1

        if page.get("last_id"):
            params["after_id"] = page["last_id"]
        if not page["has_more"]:
            break

    # persist ONLY after the walk is drained
    state.set_cursor("chats", params.get("after_id"))
```

Why this is correct for incremental sync: a chat that is edited gets a new `updated_at` of
*now*, which is always beyond the saved cursor, so it reappears ahead of the cursor and is
picked up on the next run. New chats and modified chats arrive through the same walk. That
is why the writer must be an upsert keyed on chat `id`, not an insert.

Two constraints the loop must respect:

- Cursors are bound to the sort key. An `after_id` issued under `order_by=updated_at` is
  rejected with `400` under `created_at`. If you ever change `order_by`, discard the cursor
  and re-backfill.
- Time bounds must match the sort key: `updated_at.*` pairs with `order_by=updated_at`,
  `created_at.*` with the default. Backward pagination (`before_id`) and the `project_ids[]`
  filter are not available in the org-wide form.

### 3.2 Message fetch

For each chat from the walk, fetch content:

```
GET /v1/compliance/apps/chats/{chat_id}/messages
```

Omitting `limit` returns the whole message set in one response. Pass `limit` + `after_id`
only for chats long enough to risk a timeout — set a threshold (say, page at 500) rather
than paging everything.

Each message carries `role`, `created_at`, a `content` array of typed parts, and three
nullable attachment arrays:

- `files` — user's binary uploads (`claude_file_*`)
- `generated_files` — files Claude produced via tool use (`claude_gen_file_*`)
- `artifacts` — versioned documents (`claude_artifact_*` + `version_id`)

For assistant messages `created_at` is when generation *finished*, not started. That
matters if you compute response latency.

**Attachment policy.** Downloading every binary is expensive and is where the storage and
legal exposure concentrates. Default: record attachment metadata (id, filename, mime type)
in Notion, do not download bytes. Fetch artifact *text* (`.../artifacts/{version_id}/content`)
since it is small and usually the substantive output. Make binary download an explicit
opt-in per retention class — see [`GOVERNANCE.md`](GOVERNANCE.md).

### 3.3 Activity Feed sync

Runs independently of chat sync, with its own cursor. It feeds the Agent Activity database
and supplies deletion and admin signals that the chat list alone does not give you.

Activities sort **newest first**, which inverts the cursor meaning relative to chats. Use
the catch-up pattern: persist `first_id`, pass it back as `before_id`.

```python
ACTIVITIES = "https://api.anthropic.com/v1/compliance/activities"

def sync_activities(state):
    cursor = state.get_cursor("activities")
    while True:
        params = {"limit": 1000}
        if cursor:
            params["before_id"] = cursor
        page = api_get(ACTIVITIES, params)
        store(page["data"])
        if page.get("first_id"):
            cursor = page["first_id"]
        if not page["has_more"]:
            break
    state.set_cursor("activities", cursor)
```

Do **not** treat one response as caught up while `has_more` is `true` — the unfetched pages
are the *newer* ones, and they stay unread until the loop drains.

If you prefer stateless workers, window-poll instead with `created_at.gte` / `created_at.lt`,
reusing each window's `lt` as the next `gte`. If you do, set `lt` **at least 1 minute in the
past**. A bound too close to now silently and permanently drops late-indexed activities:
once `gte` advances past them nothing can recover them. Overlap windows by a few minutes and
deduplicate on `id`.

Activity types worth filtering on for this pipeline (the feed has hundreds; pass through
unrecognized ones rather than dropping them):

- `claude_chat_created`, chat update and deletion events → drive re-sync and tombstoning
- `claude_file_uploaded` → attachment provenance
- `compliance_api_accessed` → **this pipeline's own reads**; ingest them so the audit trail
  records who queried compliance data

Build forward-compatible handlers: unknown `type` and `actor.type` values must pass through,
unexpected fields must be ignored.

### 3.4 Scheduling and rate budget

| Loop | Cadence | Typical calls/run |
|---|---|---|
| Activity Feed catch-up | 5 min | 1–3 |
| Chat list walk | 15 min | 1–5 |
| Message fetch | on enqueue | 1 per changed chat |
| Directory (users/groups) | daily | 5–20 |

At 600 rpm the Compliance API is not the constraint. Notion is — see §5.3.

---

## 4. Push plane — the OTLP receiver

This is the component that answers the literal "webhook" in the request. It is the endpoint
you type into `Organization settings → Office agents → Monitoring → OTLP endpoint`.

### 4.1 Contract

- **HTTPS, publicly resolvable, port 443.** No self-signed certs.
- **Path:** OTLP/HTTP logs are posted to `/v1/logs` relative to the configured base. Configure
  the collector base URL (e.g. `https://claude-otel.afrofarms.example/`) and let the exporter
  append the signal path.
- **Protocol:** the dropdown offers OTLP/HTTP variants. Choose `http/protobuf` if your
  collector speaks it; `http/json` is easier to debug and to hand-parse. Pick one and keep the
  receiver strict about `Content-Type`.
- **Headers:** the `OTLP headers` field is the only authentication available. Put a
  high-entropy shared secret there, e.g. `X-Ingest-Token: <32 bytes base64url>`. Verify it with
  a constant-time compare and reject with `401` otherwise. Rotate on a schedule; the field
  accepts a new value without redeploying the collector.

### 4.2 CORS is a real trap

The Office add-ins run inside a browser sandbox, so the OTLP POST is subject to CORS. A
collector that does not answer the preflight drops telemetry **silently** — there is no error
surfaced in the admin console. This has been reported against the Excel add-in
(`anthropics/claude-code#56401`). The receiver must handle `OPTIONS` and return:

```
Access-Control-Allow-Origin: <the add-in origin, or * if you accept that>
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: content-type, x-ingest-token
Access-Control-Max-Age: 86400
```

Validate end to end after configuring: send one prompt from Excel and confirm a log record
lands, rather than assuming the config took.

### 4.3 Handler shape

```
POST /v1/logs
  1. constant-time check of the shared-secret header      → 401
  2. size guard (reject > 4 MiB)                           → 413
  3. decode ResourceLogs → ScopeLogs → LogRecord
  4. append raw records to the durable queue
  5. return 200 immediately
```

Never write to Notion from inside the handler. Notion's 3 rps ceiling will stall the
response, the exporter will time out and retry, and you get duplicates on top of a backlog.
Acknowledge fast, process asynchronously.

Flatten OTLP `KeyValue`/`AnyValue` attribute pairs into a flat map at the normalizer, keeping
unknown keys — the schema will grow.

### 4.4 Correlation across planes

`prompt.id` groups every event from one user prompt. `user.email` (or the equivalent actor
attribute) joins to the Compliance API's `actor.email_address`. Where possible prefer
`actor.user_id` as the join key: it is stable and opaque, and it does not change when someone's
email or display name changes. Maintain an `email → user_id` map from the directory endpoint
and resolve OTel events through it at normalization time, so a rename does not fork a member's
history into two Notion rows.

---

## 5. Notion writer

Full schema in [`NOTION-SCHEMA.md`](NOTION-SCHEMA.md). This section covers mechanics.

### 5.1 Upsert

```python
def upsert_chat(chat, messages, state):
    h = sha256(canonical(chat, messages))
    row = state.chat_index.get(chat["id"])

    if row and row.content_hash == h:
        return "unchanged"                       # costs zero Notion calls

    props = build_properties(chat, messages)
    if row:
        notion.update_page(row.notion_page_id, props)
        rewrite_transcript(row.notion_page_id, messages)
        state.chat_index.update(chat["id"], hash=h)
        return "updated"

    page = notion.create_page(parent=CONVERSATIONS_DS, properties=props)
    append_transcript(page["id"], messages)
    state.chat_index.insert(chat["id"], page["id"], h)
    return "created"
```

The hash check is the single biggest cost saver. Because the `updated_at` walk re-surfaces
chats on any change — including changes that do not alter message content — a large fraction
of re-deliveries resolve to `unchanged` and never touch Notion.

Canonicalize before hashing: sort message ids, exclude volatile fields (`last_synced`,
signed URLs), and include `deleted_at` so a soft-delete registers as a change.

### 5.2 Block chunking

Notion limits a single rich-text object to **2,000 characters**, and a request payload to
**1,000 block elements / 500 KB**. Append children in batches of 100.

```python
MAX_RT      = 2000
CHUNK       = 1800     # headroom for markdown escaping
MAX_APPEND  = 100

def transcript_blocks(messages):
    for m in messages:
        who = "Member" if m["role"] == "user" else "Claude"
        yield heading_3(f"{who} · {m['created_at']}")
        text = "".join(p["text"] for p in m["content"] if p["type"] == "text")
        for piece in chunks(text, CHUNK):
            yield paragraph(piece)
        for f in (m.get("files") or []):
            yield bulleted(f"📎 {f['filename']} ({f['mime_type']}) · {f['id']}")
        for a in (m.get("artifacts") or []):
            yield bulleted(f"📄 {a['title']} · {a['artifact_type']} · {a['version_id']}")

def append_transcript(page_id, messages):
    for batch in batched(transcript_blocks(messages), MAX_APPEND):
        notion.append_children(page_id, batch)   # rate-limited
```

A paragraph block can hold several rich-text objects, so 2,000 chars is a per-object limit
rather than a per-block one — but one chunk per paragraph keeps the page readable and the
code simple.

For an updated chat, prefer appending only new messages (track `last_message_id` in
`chat_index`) over rewriting the page. Full rewrite is the fallback when message ids do not
line up, e.g. after an edit-and-regenerate.

### 5.3 Rate limiting — Notion is the bottleneck

Notion allows an **average of 3 requests/second** per integration. Run a token bucket at
**2.5 rps** with jitter and exponential backoff on `429` and `409` (conflict).

Worked example: a 200-message chat at ~3 blocks/message is ~600 blocks → 1 create + 6
appends = 7 calls ≈ 2.8 s. A day producing 300 changed chats of that size is ~35 minutes of
continuous writing. Comfortable, but it means the writer is a background worker with a queue,
not a synchronous path — and it means a large historical backfill should be run once,
overnight, with the rate limiter in place.

### 5.4 Ordering

Process the queue with per-chat ordering only. Two workers writing the same chat page
concurrently produce a `409`; partition the queue by `chat_id` hash so one chat is always
handled by one worker.

---

## 6. Backfill

The initial import is a distinct mode, not the steady-state loop.

1. Snapshot the directory: `GET /v1/compliance/organizations|users|groups` → populate
   **Team Members** first, so conversation rows can relate to real member pages.
2. Walk chats from the beginning with `order_by=created_at` (no cursor), oldest first.
   Note that this cursor is **not interchangeable** with the steady-state `updated_at` cursor.
3. On completion, start a fresh `order_by=updated_at` walk from no cursor to establish the
   incremental cursor, and let the hash check absorb the overlap.
4. Record start cursor, terminal `last_id`, record count, and the final page's `request-id`
   in **Sync Runs**. There is no `total_count` or checksum on list endpoints, so this run log
   *is* your completeness attestation.

Backfill respects the same 2.5 rps Notion budget. Estimate before starting: chats × (1 +
blocks/100) calls ÷ 2.5 = seconds.

---

## 7. Failure modes

| Failure | Detection | Response |
|---|---|---|
| `429` from Compliance API | Status + retry headers | Honour retry contract, resume with the **same** cursor — a failed request does not advance position |
| `429` from Notion | Status | Token bucket + exponential backoff; queue absorbs it |
| Cursor lost / state volume wiped | Cursor row absent | Re-backfill; hash check makes it cheap in Notion terms but it costs Compliance API calls |
| Cursor rejected with `400` | Error type | `order_by` changed underneath it — discard and re-backfill |
| Late-indexed activity | Silent | Overlap windows, or use the cursor pattern which does not have this failure |
| Duplicate delivery | Same `id` twice | Expected — at-least-once. Dedup on `id` |
| Notion page deleted by a human | `404` on update | Clear the `chat_index` row, recreate on next pass |
| OTLP silently dropped | No events arriving | CORS preflight or header mismatch — see §4.2. Alert on "zero OTel events in 24h" |
| Content aged out of retention | `404` on message fetch | Not recoverable. This is the argument for exporting before the window closes |
| Chat hard-deleted via Compliance API | Absent from list | Tombstone in Notion; content is unrecoverable |
| Key rotated | `401` | Cursors survive key rotation — swap the secret, do not reset state |

## 8. Observability of the pipeline itself

Emit to your own monitoring, and mirror the summary into **Sync Runs**:

- lag: `now − max(created_at)` ingested, per plane
- records ingested, chats upserted, chats skipped-unchanged, per run
- Notion 429 rate and queue depth
- Compliance API request budget consumed against 600/min
- zero-event alarms per surface (an Office agent that stops reporting looks identical to an
  Office agent nobody used)

## 9. Open decisions

1. **Content vs metadata.** Anthropic's own integration guidance is to avoid a parallel copy
   and rely on direct API retrieval unless retention or legal-hold horizons force an export.
   A full transcript mirror in Notion is a deliberate departure from that. See
   [`GOVERNANCE.md`](GOVERNANCE.md) §1 before committing.
2. **Messages database on or off.** One page per message makes transcripts searchable and
   filterable in Notion, at roughly 20–50× the page count. Recommended: off at launch,
   transcripts in the conversation page body; turn on only if per-message filtering proves
   necessary.
3. **Binary attachment archival.** Off by default. Turning it on needs a storage target
   (object storage, not Notion) and a retention rule.
4. **Claude Code / Cowork coverage.** If these matter, they need their own OTel collector
   configuration, separate from the Office agents one, and they still yield metadata only.
