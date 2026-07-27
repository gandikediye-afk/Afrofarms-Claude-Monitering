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

The implementation keeps cursors, idempotency indexes, sanitized durable work, and audit
runs in SQLite. Binary attachment retrieval remains disabled; only safe attachment metadata
is mirrored.
