# Runbook

## 0. Prerequisite check — do this first

Chat-content endpoints are Claude **Enterprise** only. Verify before anything else:

```bash
curl --fail-with-body -sS \
  "https://api.anthropic.com/v1/compliance/apps/chats?limit=1" \
  --header "x-api-key: $ANTHROPIC_COMPLIANCE_ACCESS_KEY"
```

| Result | Meaning |
|---|---|
| `200` with a `data` array | Enterprise + correct key type + scope. Proceed. |
| `403` | Either an Admin API key (content endpoints reject those) or the scope is missing. |
| `404` / not enabled | Compliance API not enabled for the org, or not an Enterprise plan. |

If the plan is **Team**, the only export route is the capped-lookback CSV under
`Organization settings → Data and privacy`, which contains no chat, file, or project content.
The pull plane cannot be built on it. Stop and resolve the plan question before continuing —
the OTLP push plane still works and gives metadata coverage in the meantime.

## 1. Provision the Compliance Access Key

1. In claude.ai, enable the Compliance API for the organization
   (`Organization settings`). Primary Owner required.
2. Create a **Compliance Access Key** (`sk-ant-api01-…`) — created in claude.ai, not Console.
   An Admin API key (`sk-ant-admin01-…`) reaches the Activity Feed only.
3. Grant exactly two scopes:
   - `read:compliance_activities`
   - `read:compliance_user_data`
4. **Do not grant** `delete:compliance_user_data`. See [`GOVERNANCE.md`](GOVERNANCE.md) §4.
5. Store in the secret manager. Never in the repository, CI config, or an env file on disk.

## 2. Create the Notion databases

Follow the dependency order in [`NOTION-SCHEMA.md`](NOTION-SCHEMA.md): Team Members →
Projects → Sync Runs → Conversations → Agent Activity → (Messages, optional). Relations need
the target data source ID to already exist. Add rollups in a second pass, after relations.

Create a Notion internal integration, share exactly these five databases with it, and record
each data source ID in configuration. Then restrict the parent page per
[`GOVERNANCE.md`](GOVERNANCE.md) §3 — do this before the first sync, not after.

## 3. Configuration

```bash
# Compliance API (pull plane)
ANTHROPIC_COMPLIANCE_ACCESS_KEY=      # secret manager reference
COMPLIANCE_BASE_URL=https://api.anthropic.com
CHAT_POLL_INTERVAL=15m
ACTIVITY_POLL_INTERVAL=5m
DIRECTORY_POLL_INTERVAL=24h
CHAT_PAGE_LIMIT=100
ACTIVITY_PAGE_LIMIT=1000              # max 5000
MESSAGE_PAGING_THRESHOLD=500          # page the messages endpoint above this
ACTIVITY_TYPE_ALLOWLIST=claude_chat_created,claude_file_uploaded,compliance_api_accessed

# OTLP receiver (push plane)
OTLP_LISTEN_ADDR=0.0.0.0:8443
OTLP_SHARED_SECRET=                   # matches the admin console header value
OTLP_ALLOWED_ORIGINS=                 # Office add-in origins, for CORS
OTLP_MAX_BODY_BYTES=4194304

# Notion
NOTION_TOKEN=                         # secret manager reference
NOTION_DS_MEMBERS=
NOTION_DS_PROJECTS=
NOTION_DS_SYNC_RUNS=
NOTION_DS_CONVERSATIONS=
NOTION_DS_ACTIVITY=
NOTION_DS_MESSAGES=                   # blank = Messages database disabled
NOTION_RATE_LIMIT_RPS=2.5             # Notion allows an average of 3
NOTION_PARENT_PAGE_ID=                # restricted Compliance parent; enforced at startup

# Behaviour
MIRROR_TRANSCRIPTS=true               # false = metadata-only mode
DOWNLOAD_ATTACHMENTS=false
REDACTION_ENABLED=true
STATE_DB_PATH=/var/lib/claude-monitor/state.db
PRODUCTION_READINESS=employee-notice:complete,lawful-basis:complete,access-approval:complete
RETENTION_CLASS_DAYS='{"standard":365,"sensitive":90,"extended":1095}'
RETENTION_INTERVAL=24h
DELETION_GRACE_PERIOD=7d
```

Startup fails closed unless `PRODUCTION_READINESS` exactly records completion of all three
governance gates. The integration must be shared only with the configured data sources,
which must all be direct children of `NOTION_PARENT_PAGE_ID`; the service verifies that
parent for every data source before syncing. Restrict that parent to the named Compliance
group and disable workspace-default, guest, and public-link access. Notion does not expose
all sharing settings through this API, so those restrictions remain an administrator control
that must be reviewed quarterly.

`STATE_DB_PATH` must be on a **persistent volume**. Losing it loses the cursors and the
`chat_id → page_id` index, which forces a full re-backfill.

## 4. Configure the OTLP push plane

In `claude.ai → Organization settings → Office agents → Monitoring`:

| Field | Value |
|---|---|
| OTLP endpoint | `https://claude-otel.afrofarms.example/` (HTTPS, port 443, publicly resolvable) |
| OTLP protocol | `http/protobuf`, or `http/json` while debugging |
| OTLP headers | `X-Ingest-Token: <the value of OTLP_SHARED_SECRET>` |

Then **verify end to end** — a misconfiguration here fails silently:

1. Open Excel with the Claude add-in, send one prompt.
2. Confirm a log record reaches the receiver within a minute.
3. If nothing arrives, check CORS first. The add-in runs in a browser sandbox and the
   preflight must be answered; a collector that ignores `OPTIONS` drops telemetry with no
   error shown in the console. See [`DESIGN.md`](DESIGN.md) §4.2. This has been reported
   against the Excel add-in as `anthropics/claude-code#56401`.
4. Then check the header value, then TLS chain validity.

Note the scope: the `Let Claude work across apps` toggle on that same page governs cross-app
context between Excel and PowerPoint — it is a separate setting from monitoring and does not
affect what telemetry is emitted.

## 5. First run

Run in this order, with the rate limiter on for all of it:

```bash
claude-monitor directory sync          # populates Team Members + Projects
claude-monitor backfill --dry-run      # prints estimated call counts and duration
claude-monitor backfill                # order_by=created_at, oldest first
claude-monitor daemon                  # steady state: both planes
```

Estimate the backfill before starting: `chats × (1 + ceil(blocks/100)) ÷ 2.5 rps = seconds`.
Notion, not the Compliance API, sets the duration. Run a large backfill overnight.

After backfill completes, the daemon starts a fresh `order_by=updated_at` walk from no cursor.
The overlap is expected and cheap — the content hash resolves it to `unchanged` without a
Notion write.

## 6. Operations

### Daily

- Check the newest **Sync Runs** rows: `Outcome = ok`, `Errors = 0`.
- Check ingestion lag per plane (`now − max(created_at)` ingested).

### Alerts to configure

| Alert | Condition | Likely cause |
|---|---|---|
| Pull plane stalled | No successful chat run in 1h | Key expired, network, cursor rejected |
| Push plane silent | Zero OTel events in 24h | CORS, header mismatch, cert expiry |
| Notion backpressure | Queue depth rising 30 min | 429 storm, or a very large backfill |
| Compliance budget | >400 req/min sustained | Poll interval too tight, or a runaway loop |
| Secrets detected | Any new `possible-secret` flag | Credential in a conversation — rotate it |

The push-plane alert matters most because its failure mode is silent, and an Office agent
that stopped reporting looks exactly like an Office agent nobody used.

### Common failures

**`400` on a chat request with a stored cursor.** The cursor was issued under a different
`order_by`. Cursors are bound to their sort key. Clear the `chats` cursor row and re-backfill.

**`429` from the Compliance API.** 600 req/min is shared across every key and every
`/v1/compliance/*` endpoint for the whole parent organization — another integration may be
consuming it. Honour the retry headers and resume with the **same** cursor; a failed request
does not advance position.

**`429` from Notion.** Expected under load. Confirm the token bucket is at 2.5 rps and
backoff is exponential. If it persists, another integration is sharing the token.

**`404` on message fetch for a chat that just listed.** The chat was hard-deleted or aged out
between the list and the fetch. Tombstone the row and continue — do not retry.

**Duplicate Notion pages for one chat.** The `chat_index` row was lost or two workers raced.
Partition the queue by `chat_id` hash so one chat is always handled by one worker. Repair by
rebuilding `chat_index` from the `Chat ID` property, keeping the oldest page per ID.

**Ingestion looks complete but is not.** List endpoints return no `total_count` and no
checksum. The **Sync Runs** row — start cursor, terminal cursor, record count, final
`request-id` — is the only attestation. If it was not written, the run is not attestable.

### Key rotation

Swap the secret in the secret manager and restart. Cursors survive key rotation; do not reset
state. Verify with one successful run before revoking the old key.

## 7. Verification checklist before declaring it live

- [ ] `GET /v1/compliance/apps/chats` returns `200` with the production key
- [ ] All five Notion databases exist, relations resolve, rollups populate
- [ ] Notion parent page restricted to the Compliance group; guest and public sharing off
- [ ] One test conversation appears end to end with a correct transcript
- [ ] An edited conversation re-syncs and does **not** duplicate its page
- [ ] An unchanged conversation resolves to `unchanged` with zero Notion writes
- [ ] A soft-deleted chat sets `Deleted At` in Notion
- [ ] One Excel prompt produces an Agent Activity row
- [ ] Redaction fires on a planted test secret and sets `possible-secret`
- [ ] Retention job runs and archives a row past its window
- [ ] Employee notice issued and lawful basis documented ([`GOVERNANCE.md`](GOVERNANCE.md) §2)
- [ ] Restart with a cleared queue resumes from the persisted cursor without gaps
