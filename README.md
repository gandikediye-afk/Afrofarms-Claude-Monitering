# Afro Farms — Claude Conversation Monitoring

Design for a service that captures Claude usage across the whole team and mirrors it
into Notion databases.

## Read this first

The request was "a webhook that receives all conversations of all team members from
Claude." **Anthropic does not push conversation content to a webhook.** There is no
outbound event carrying chat transcripts, and the OTLP endpoint in
`Organization settings → Office agents → Monitoring` does not carry them either.

Two separate telemetry planes exist, and they cover different surfaces:

| | Compliance API | OpenTelemetry (OTLP) |
|---|---|---|
| Direction | **Pull** — you poll `api.anthropic.com` | **Push** — Claude posts to your collector |
| Carries transcripts? | **Yes** — full user + assistant text, files, artifacts | **No** — event records only |
| Surfaces | claude.ai chats and projects | Office agents, Claude Code, Cowork |
| Plan | Claude Enterprise | Claude Enterprise / direct-provider |
| Latency | ~1 min indexing lag | near real-time |

So the conversations you want come from the **pull** plane, and the thing you can point
that OTLP field at is the **push** plane. The design runs both and merges them.

## The simple webhook (what deploys today)

`src/claude_monitor/simple.py` is the whole thing: Claude Office Agents posts an event,
it writes a row to Notion. No database, no cron jobs, no queue.

Three environment variables:

```bash
OTLP_SHARED_SECRET=   # openssl rand -base64 32 -- goes in Claude's "OTLP headers" box
NOTION_TOKEN=         # your Notion integration token
NOTION_DS_ACTIVITY=   # the Agent Activity data source id
```

Deploy (`vercel.json` and `api/index.py` are already wired to it), then in
`claude.ai -> Organization settings -> Office agents -> Monitoring`:

| Field | Value |
|---|---|
| OTLP endpoint | `https://your-app.vercel.app` (base URL, no path) |
| OTLP protocol | `http/protobuf` (JSON also accepted) |
| OTLP headers | `Authorization=Bearer <OTLP_SHARED_SECRET>` (`=`, not `:`) |

Duplicates are avoided by asking Notion whether the Event ID already exists, so there is
no local index to lose. If Notion is unreachable the endpoint answers 503 and the OTLP
exporter retries.

**This carries no conversation text.** Office Agents telemetry reports who used Claude, in
which app, when, and how much. That is a limit of what Claude sends, not of this code.
The fuller pipeline in `claude_monitor.serverless` (durable queue, chat-text sync via the
Compliance API) remains in the repository for if that changes.

## Documents

| Doc | Contents |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Architecture, both ingestion planes, sync loops, failure modes |
| [`docs/NOTION-SCHEMA.md`](docs/NOTION-SCHEMA.md) | The five Notion databases, with DDL |
| [`docs/GOVERNANCE.md`](docs/GOVERNANCE.md) | Legal basis, access control, deletion mirroring, retention |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Key provisioning, configuration, deployment, operations |

## Prerequisite that gates the whole build

Chat-content endpoints require **Claude Enterprise**. On a Team plan the only export
route is a capped-lookback CSV from `Organization settings → Data and privacy`, with no
chat, file, or project content. Confirm the plan before implementation starts —
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) has the check.

## Implementation

The `src/claude_monitor` package provides the pull-plane service and installs the
`claude-monitor` executable. Configure it with the environment variables in the runbook,
then run `directory sync`, `backfill --dry-run`, `backfill`, and `daemon` in that order.

Before starting transcript synchronization, verify the access key and read scope:

```console
ANTHROPIC_COMPLIANCE_ACCESS_KEY=... claude-monitor compliance check
```

The check makes only `GET /v1/compliance/apps/chats?limit=1` against the configured
Anthropic API origin. A `403` usually means the credential is the wrong key type or lacks
read scope; an unavailable endpoint usually means Compliance API access is not enabled.
The key is never printed. Transcript synchronization stays disabled until the check passes,
while the independent OTLP activity collector remains available.

The implementation keeps cursors, idempotency indexes, sanitized durable work, and audit
runs in SQLite, or in Postgres when `DATABASE_URL` is set. Hosts without a persistent
disk (Vercel, Lambda) **must** use Postgres: losing the state store loses the
`chat_id -> notion_page_id` index, and the writer then creates a duplicate Notion page for
every chat on each cold start. See `docs/RUNBOOK.md` §3a. Binary attachment retrieval remains disabled; only safe attachment metadata
is mirrored.
