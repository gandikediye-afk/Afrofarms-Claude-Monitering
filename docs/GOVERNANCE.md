# Governance

> **Scope of the current deployment.** Only the Office Agents telemetry webhook is
> deployed. It records who used Claude, in which app, when, and how much — no conversation
> text. That removes the content-exposure concerns in §6 and most of §1, but **not** the
> employee-monitoring obligations: under the Kenya Data Protection Act 2019, "Amina used
> Claude in Excel 40 times last week" is still personal data about an identified employee.
> Sections 2 (notice and lawful basis), 3 (access control), and 5 (retention) apply in full.

This pipeline copies every team member's Claude conversations — including whatever they
happened to paste in — into Notion. That is what was asked for, and the design delivers it.
This document covers what has to be true around it for that to be defensible.

## 1. The one thing to decide before building

Anthropic's own integration guidance is to rely on direct API retrieval and **avoid
maintaining a parallel copy**, exporting only when a legal-hold or eDiscovery horizon exceeds
what retention will keep, or when content must survive a planned deletion. A permanent full
transcript mirror in Notion is a deliberate departure from that.

The tradeoff, stated plainly:

| | Full transcript mirror | Metadata + on-demand retrieval |
|---|---|---|
| Search across conversations | Native Notion search | Requires an API call per chat |
| Survives Claude retention expiry | Yes | No |
| Exposure surface | Every conversation readable by anyone with Notion access | Only what a reviewer pulls, when they pull it |
| Access is audited | Notion's audit log | Compliance API emits `compliance_api_accessed` per read |
| Deletion obligations | Must be mirrored manually (§4) | Handled by Claude's retention |

A defensible middle path, and the recommendation if there is no legal-hold requirement
forcing a full mirror: **mirror metadata + the Agent Activity table for everyone, mirror
transcripts only for conversations that match a defined trigger** — a retention class, a
project, a DLP flag, or a named custodian under hold. `Retention Class` on the Conversations
database is the switch for this.

Nothing below depends on which option is chosen; all of it applies either way.

## 2. Legal basis and notice

Afro Farms Holding Ltd is Kenya-registered, so the **Data Protection Act, 2019** governs
first: lawful basis, purpose limitation, data-minimisation, and a data subject's right to be
informed. Employee monitoring is not exempt from any of these. If any team member, contractor,
or customer whose data appears in a conversation is in the EU/EEA or the UK, **GDPR** applies
in parallel (Art. 6 lawful basis, Art. 13 notice, Art. 35 DPIA — large-scale systematic
monitoring of employees is a standard DPIA trigger).

Before the first sync run:

- [ ] Written notice to every team member: that Claude conversations are captured, what is
      captured, where it is stored, who can read it, how long it is kept, and why.
- [ ] Documented lawful basis. Consent is a weak basis in an employment relationship —
      legitimate interest or legal obligation is usually the defensible one, with the
      balancing test written down.
- [ ] Purpose statement, narrow and specific. "Compliance and audit" is a purpose;
      "management visibility" invites scope creep and undermines the balancing test.
- [ ] Confirm whether ODPC registration as a data controller is required for the entity.
- [ ] DPIA if any subject is in scope of GDPR.
- [ ] Retention period agreed and written down, with the deletion mechanism actually built
      (§5) rather than promised.

Consult counsel on the balancing test and the notice text. This document is not legal advice.

## 3. Access control

The default assumption to correct: **Notion is a weaker security boundary than Claude
Enterprise.** Claude conversations are private to the member; the moment they land in Notion
they inherit Notion's sharing model, which is permissive by design and easy to widen by
accident.

- [ ] Conversations and Messages restricted to a named **Compliance** group. Not "everyone
      at Afro Farms", not a workspace-default-visible page, and not inside a shared teamspace.
- [ ] Agent Activity may be shared more widely — it holds no conversation content by
      construction, which is the reason it is a separate database.
- [ ] The pipeline's Notion integration gets access to these five databases only.
- [ ] Guest and public-link sharing disabled on the parent page. Re-check after any
      restructure; moving a page can change inherited permissions.
- [ ] Quarterly review of who holds access, recorded in Sync Runs or a separate log.

## 4. Credentials

| Secret | Where | Rules |
|---|---|---|
| Compliance Access Key (`sk-ant-api01-…`) | Secret manager, injected at runtime | `read:compliance_activities` + `read:compliance_user_data` only |
| Notion integration token | Secret manager | Scoped to the five databases |
| OTLP shared secret | Secret manager + the admin console header field | Rotate quarterly |

**Never grant `delete:compliance_user_data`.** This pipeline reads. Deletion through the
Compliance API is permanent and immediate, with no recovery window; an ingestion bug holding
that scope is an unrecoverable data-loss event. Deletion, if ever needed, is a separate
manually-run tool with a separate key.

Compliance API cursors survive key rotation, so rotating does not force a re-backfill.

Every call this pipeline makes emits a `compliance_api_accessed` activity attributable to the
key via `actor.api_key_id`. That is a feature — ingest those events so the archive records its
own reads.

## 5. Deletion and retention

This is where a mirror most commonly goes wrong. A member deletes a conversation in claude.ai
and reasonably believes it is gone; the Notion copy persists indefinitely. That is a
data-subject-rights failure and, in an employment context, a bad surprise.

Required behaviour:

1. **Soft-delete mirroring.** A chat the member deleted still appears in the Compliance API
   with `deleted_at` populated. On seeing `deleted_at`, set `Deleted At` on the Notion page
   and apply the retention rule for that class. Default: archive the Notion page after the
   grace period; do not keep the transcript live.
2. **Hard-delete tombstoning.** A chat that disappears from the list is either hard-deleted or
   aged out of retention. Set `Tombstoned`, strip the page body, keep the metadata row.
   Content is unrecoverable at that point, by design.
3. **Retention job.** A scheduled task that enforces the agreed period against `Last Activity`
   and `Retention Class`. Without this, "we keep conversations for N months" is aspirational.
4. **Legal hold.** `Retention Class = legal-hold` exempts a row from the retention job.
   Setting it should be a deliberate, logged action.
5. **Subject access requests.** A member asking what is held about them should be answerable
   from the Member relation on the Conversations database. Verify this works before the notice
   in §2 promises it.

## 6. Content risk at ingest

Conversations will contain things nobody intended to archive: API keys pasted for debugging,
payroll figures, supplier pricing, a health disclosure in a message about leave. The mirror
concentrates all of it in one searchable place.

Run a redaction pass in the normalizer, **before** the Notion write:

- [ ] Secret detection (API keys, private keys, connection strings, bearer tokens) → redact
      the match in place, set `Flags = possible-secret`. Redact rather than skip: a silently
      dropped conversation is worse than a marked one.
- [ ] PII detection (national ID, phone, bank account, email of non-members) → `possible-pii`.
- [ ] Category tagging for `financial` and `personnel` so retention rules can differ.
- [ ] `Review Status` on flagged rows so someone actually triages them.

A detected secret is a live incident, not just a tag. Route `possible-secret` to whoever
handles credential rotation — the point of finding it is to revoke it.

## 7. Attachments

Binary attachment download is **off by default**. Turning it on means uploaded PDFs,
spreadsheets, and images leave Claude and land in your storage, which multiplies both cost and
exposure. If enabled:

- Store in object storage with encryption at rest, never as Notion file uploads.
- Reference by ID from the Notion page; do not embed.
- Apply the same retention job as the parent conversation.
- Verify the `Content-MD5` header on download and record it alongside the object — that hash
  plus the source endpoint, query parameters, and run timestamp is the chain-of-custody record
  that makes an export admissible.

## 8. Scope boundary

Not covered by this pipeline, and worth saying out loud so nobody assumes otherwise:

- Prompt text and responses from Claude Console / Claude API workloads.
- Claude Code and Cowork conversation content (metadata only, via OTel).
- Content already removed by retention before the first backfill ran.
- Anything hard-deleted through the Compliance API.

"All conversations of all team members" means all **claude.ai** conversations. If someone
needs to believe the archive is complete, they need to know where it ends.
