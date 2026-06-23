# Eneo AI code and security reviewer

This guide explains the deployed reviewer in this repository: a manual,
maintainer-triggered Hermes Agent review for Eneo pull requests. An allowlisted
maintainer comments exactly `@review`; GitHub Actions verifies the requester;
Hermes runs Codex with only the `eneo_review` toolset; the plugin reads bounded
PR context and records every surviving finding in SQLite; Hermes posts one
structured GitHub comment back to the PR.

The reviewer is advisory. CI, CodeQL, dependency review, tests, type checks,
human ownership, and migration checks remain the merge gates. The model cannot
execute contributor code, write repository files, browse the web, delegate to
subagents, or mutate GitHub except through Hermes' final configured
`github_comment` delivery.

## 1. Recommended design

Use this flow first:

```text
allowlisted maintainer comments @review
        |
        v
GitHub Actions verifies username + trusted repository association
        |
        v
small HMAC-signed webhook payload
        |
        v
Hermes Agent on Dokploy
        |
        +--> Codex through ChatGPT/Codex OAuth
        +--> bounded GitHub PR read tools
        +--> Eneo SOUL.md + AGENTS.md + review skill
        +--> SQLite findings and decision registry
        |
        v
Hermes native github_comment delivery
        |
        v
one structured, constructive PR comment
```

Hermes’ current webhook adapter supports HMAC verification, route-specific skills, idempotency, and `github_comment` delivery through the GitHub CLI. The official Docker image keeps mutable state under `/opt/data`, which is the right place for Codex OAuth state, configuration, plugins, sessions, and the SQLite review database.

### What this is

It is a contextual **AI code and security review** that concentrates on:

- tenant isolation, OIDC/JWT, RBAC, credentials, files, retrieval, webhooks, MCP and tool boundaries;
- correctness, transactions, concurrency, idempotency, background jobs, and error paths;
- FastAPI, OpenAPI, Pydantic, strict Python typing, TypeScript contracts, and compatibility;
- regression tests that prove the real failure mode;
- maintainability, ownership boundaries, duplicated policy, hidden coupling, misleading abstractions, and AI-generated overbuilding;
- performance, query behavior, Alembic migrations, locking, rollback, and data loss.

### What this is not

It is not GitHub CodeQL or SARIF code scanning in the Security tab. Keep deterministic CI, CodeQL, dependency review, tests, Pyright, migration checks, and human ownership as the merge gates. This reviewer adds contextual engineering judgment and posts an advisory PR comment.

It also does not execute contributor code on the VPS. Posting directly to GitHub and withholding a general shell are independent choices: Hermes posts through its configured delivery adapter after the model finishes. This keeps fork pull requests from turning the reviewer host into a remote-code-execution service.

## 2. Why one Codex run with two passes

Do not start with Claude, Codex, and Gemini all scanning the full diff independently. That tends to multiply repeated observations and prose.

The phase-one reviewer uses a small internal council pattern inside one Codex run:

1. **Proposer pass:** produce every concrete candidate finding tied to the diff.
2. **Skeptic pass:** try to disprove every candidate by checking guards, callers, callees, base behavior, framework guarantees, transactions, and tests.
3. **Editor pass:** publish every surviving finding in natural language, with lower-priority details collapsed.

A finding may be published only when it:

- is introduced or materially worsened by the pull request;
- has an exact changed path and line;
- has a concrete failure or exploit path;
- survives active attempts to find a benign explanation;
- has confidence of at least `0.85`;
- scores at least `8/10` for Critical/High or `7/10` for Medium/Low on
  evidence, impact, causality, falsification, and remediation;
- has a small, concrete fix rather than an architecture rewrite.

Medium and Low findings are for actionable lower-priority feedback only. They are
published when they survive the same evidence gate as higher-severity findings,
but their details are collapsed so they do not crowd the main review.

This mirrors the useful part of an iterative peer-review loop without making one model call another model or exposing internal deliberation to the developer.

## 3. How Ponytail is used

The starter vendors Ponytail v4.7.0, but does **not** blindly preload the unmodified skill into the webhook. Ponytail’s general output rules are written for implementation tasks and can conflict with a review-specific comment format.

Instead, its useful ladder is incorporated into the Eneo review skill:

1. Can the new code be deleted?
2. Can the standard library, framework, browser, or database already solve it?
3. Can an existing Eneo abstraction solve it?
4. Can one local change solve it?
5. Only then propose new machinery.

Ponytail is allowed to reduce unnecessary code, abstractions, dependencies, and prose. It is never allowed to remove authorization, tenant isolation, validation, data-loss protection, reliability, accessibility, or required tests.

## 4. Comment contract

The review should feel like a thoughtful colleague, not a scanner dump.

The canonical live contract for the visible comment is
`bootstrap/workspace/AGENTS.md`. In human terms, the comment is limited to:

- one summary sentence;
- every surviving finding;
- a short prose budget spent on evidence and the fix;
- one compact section per finding;
- collapsed details for Medium and Low findings;
- one collapsed, copyable fix brief containing all findings when findings exist.

Each finding contains:

- a short descriptive heading;
- `path:line`, category, and severity;
- the verified behavior and concrete consequence;
- the smallest practical correction;
- a quiet 12-character fingerprint for later triage.

The reviewer must not post:

- style, naming, or formatting preferences;
- generic “best practice” lectures;
- weak possibilities or a watchlist;
- praise filler;
- the same issue repeated as separate evidence, impact, and recommendation essays;
- claims that tests ran or passed when the agent did not run them;
- “safe to merge,” “approved,” or `GREEN_LIGHT` language, because the review is not exhaustive.

Use wording such as “This path can…” and “A minimal fix is…”, not “You forgot…” or “You should obviously…”.

### Example

````md
## Eneo AI code & security review

I found two issues worth addressing before merge.

| Severity | Category | Location | Finding | ID |
| --- | --- | --- | --- | --- |
| High / P1 important | security | `backend/src/intric/jobs/service.py:142` | Tenant context is dropped before the background job | `a1b2c3d4e5f6` |
| Medium / P2 useful improvement | tests | `backend/tests/jobs/test_service.py:88` | Regression test misses the cross-tenant worker path | `b2c3d4e5f6a1` |

### Tenant context is dropped before the background job
`backend/src/intric/jobs/service.py:142` · security · **High / P1 important**

The new enqueue path passes the document ID but not the verified tenant ID. The worker later reloads the row by primary key, so the authorization boundary from the request is no longer present in the asynchronous path.

**Suggested change:** include the trusted tenant ID in the job payload and scope the worker lookup by both tenant and document ID. Add a regression test where a job created under tenant A cannot load tenant B's document.

<details>
<summary>Medium / P2 useful improvement · Regression test misses the cross-tenant worker path · backend/tests/jobs/test_service.py:88</summary>

`backend/tests/jobs/test_service.py:88` · tests · **Medium / P2 useful improvement**

The added test covers the happy path for a worker loading its own document, but it would also have passed before the tenant boundary fix because it never creates a second tenant with a conflicting document ID.

**Suggested change:** add a regression case with tenant A and tenant B documents and assert that the worker created under tenant A cannot load tenant B's row.

</details>

<details>
<summary>Copyable fix brief for a coding agent</summary>

```text
Goal: Preserve the verified tenant boundary across the background job.
Findings:
1. High / P1 security - backend/src/intric/jobs/service.py:142 - Tenant context is dropped before the background job. Carry tenant_id in the job payload and scope the worker lookup by tenant_id + document_id.
2. Medium / P2 tests - backend/tests/jobs/test_service.py:88 - Regression test misses the cross-tenant worker path. Add a two-tenant regression case proving tenant A cannot load tenant B's row.
Files:
- backend/src/intric/jobs/service.py
- backend/tests/jobs/test_service.py
Changes: Preserve tenant_id through enqueue and worker lookup; add the missing cross-tenant regression test.
Constraints: Reuse the existing tenant-scoped repository/service; do not add a second authorization path.
Verification: Add a regression test proving tenant A cannot load tenant B's document, then run the focused backend tests and strict Pyright for changed modules.
```

</details>

<sub>Eneo two-pass review · findings `a1b2c3d4e5f6`, `b2c3d4e5f6a1`</sub>
````

The collapsed brief is simpler than uploading a generated Markdown file. The developer can copy it directly into Claude Code or Codex. A separate artifact can be added later if the team proves it is useful.

## 5. Files in the starter bundle

```text
compose.yaml                         Dokploy-ready service
Dockerfile                           Hermes image plus GitHub CLI
.env.example                         required environment variables
bootstrap/config.yaml                restricted webhook route and delivery
bootstrap/SOUL.md                    identity, tone, evidence, brevity
bootstrap/workspace/AGENTS.md        canonical Eneo review contract
bootstrap/skills/eneo-pr-review/     two-pass review procedure
bootstrap/skills/ponytail/           vendored upstream skill and licence
bootstrap/plugins/eneo_review_tools/ bounded GitHub reads + SQLite memory
examples/github/ai-review-request.yml maintainer-only @review trigger
examples/comments/example-review.md  desired developer-facing style
tools/eneo_review_memory.py          human triage CLI
examples/optional/code-review-graph.md optional later indexing design
```

## 6. Prerequisites

You need:

- the existing Hetzner VPS and Dokploy;
- an HTTPS hostname such as `review-bot.example.org`;
- a dedicated GitHub machine user or bot account;
- a ChatGPT/Codex subscription account used for Hermes’ Codex OAuth login;
- permission to add a workflow, Actions secrets, and an Actions variable to `eneo-ai/eneo`.

Use SSH rather than a provider browser console when pasting secrets or Docker commands. Hermes’ current Docker documentation specifically warns that some VPS browser consoles can corrupt characters such as `:`, `@`, and `=`.

## 7. Create the GitHub reviewer identity

Create a dedicated GitHub machine user or bot and grant it access only to the Eneo repository.

Create a fine-grained personal access token restricted to `eneo-ai/eneo` with:

```text
Contents: read
Pull requests: read and write
Metadata: read
```

Store it only as `GH_TOKEN` in Dokploy. Hermes’ `github_comment` delivery uses the `gh` CLI. The model itself does not receive an arbitrary GitHub write tool.

## 8. Deploy in Dokploy

Put the starter bundle in a private infrastructure repository. Create a Dokploy Docker Compose application using `compose.yaml`.

Copy `.env.example` to `.env` and set:

```dotenv
HERMES_IMAGE=nousresearch/hermes-agent:latest
TZ=Europe/Stockholm

WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644
WEBHOOK_SECRET=<output from: openssl rand -hex 32>

GH_TOKEN=<fine-grained GitHub bot token>
GH_PROMPT_DISABLED=1

ENEO_ALLOWED_REPOSITORIES=eneo-ai/eneo
ENEO_REVIEW_DB=/opt/data/review-memory/review_memory.sqlite3

HERMES_DASHBOARD=0
API_SERVER_ENABLED=false
PYTHONUNBUFFERED=1
```

For production, replace `latest` with a reviewed immutable image digest after the initial proof of concept.

Route an HTTPS domain to service `hermes-review`, container port `8644`. Do not expose a dashboard, shell, database, or OpenAI-compatible API.

The named volume is mounted at `/opt/data`. Back it up securely. It contains Codex OAuth state, the GitHub review database, configuration, and possibly sensitive unpublished finding text. Never run two Hermes gateways against the same volume.

Deploy the service.

## 9. Install the Eneo reviewer and connect Codex

Open a terminal in the running `hermes-review` container:

```bash
/opt/eneo-bootstrap/install.sh
hermes plugins list
hermes model
```

Choose the **OpenAI Codex** OAuth/provider option and complete the device-code login using the ChatGPT/Codex subscription account intended for the reviewer.

Restart the Dokploy service, then check:

```bash
curl -fsS http://127.0.0.1:8644/health
gh auth status
hermes doctor
hermes plugins list
```

Expected webhook health response:

```json
{"status":"ok","platform":"webhook"}
```

The installer copies the policy, skill, and plugin into `/opt/data`, creates the SQLite database, and enables the `eneo-review-tools` plugin. It preserves model/provider configuration written by `hermes model`.

## 10. Install the GitHub trigger

Copy:

```text
examples/github/ai-review-request.yml
```

to:

```text
.github/workflows/ai-review-request.yml
```

The workflow must be present on the repository’s default branch before `issue_comment` events can start it. Eneo currently uses `develop` as its default branch.

Create these Actions secrets:

```text
HERMES_REVIEW_URL=https://review-bot.example.org/webhooks/eneo-review
HERMES_WEBHOOK_SECRET=<same value as WEBHOOK_SECRET>
```

Create this Actions variable:

```text
AI_REVIEW_ALLOWED_USERS=alice,bob,security-maintainer
```

The allowlist accepts commas, spaces, or newlines. An empty variable denies all requests. The workflow additionally requires GitHub’s author association to be `OWNER`, `MEMBER`, or `COLLABORATOR`.

Protect the workflow with CODEOWNERS or a ruleset:

```text
/.github/workflows/ai-review-request.yml @eneo-ai/security-maintainers
```

The workflow:

- has `permissions: {}`;
- does not check out or run contributor code;
- sends only repository name, PR number, requester, and the triggering comment ID;
- signs the payload with HMAC-SHA256;
- allows up to 15 minutes for the review and comment delivery;
- uses the original GitHub comment ID as the stable delivery ID, so a workflow retry does not create a duplicate agent run inside Hermes’ one-hour idempotency window.

## 11. Request the first review

On an open, non-draft pull request, an allowlisted maintainer comments exactly:

```text
@review
```

Hermes reads the current PR head, performs both passes, consults the memory database, and posts one comment through the dedicated GitHub identity.

To review an updated head, write a new `@review` comment. A new comment ID intentionally starts a new review. Re-running the same Actions job reuses the original ID and should not duplicate it.

A direct smoke test is included:

```bash
python3 scripts/smoke_webhook.py \
  --url https://review-bot.example.org/webhooks/eneo-review \
  --secret "$WEBHOOK_SECRET" \
  --repo eneo-ai/eneo \
  --pr 123
```

This posts a real comment when the PR, route, and credentials are valid.

## 12. Durable findings and false-positive memory

Do not use Hermes’ small conversational `MEMORY.md` as the authoritative suppression registry. Use the included SQLite database:

```text
/opt/data/review-memory/review_memory.sqlite3
```

### What is stored

For each published finding, the database stores a stable identity, including:

- repository;
- rule ID;
- path;
- function, route, class, migration, or component symbol;
- semantic anchor;
- severity, evidence summary, impact, and smallest fix;
- PR and head SHA;
- a trusted GitHub file/blob context hash;
- later human decisions.

The line number is excluded from the fingerprint so a harmless line move does not make a brand-new finding.

### Why the database is a Hermes plugin, not MCP

For phase one, a native plugin is the simpler and safer choice:

- no additional network service;
- no MCP server lifecycle or authentication;
- the tool schema can expose only bounded lookup and append operations;
- the database stays local inside `/opt/data`;
- the model cannot create a human suppression decision.

The **skill** tells Codex when and how to consult the registry. The **plugin** performs the trusted GitHub reads and database operations. MCP becomes useful later only if Claude, Codex, CI jobs, and other services all need the same remote decision service.

### Suppression behavior

A human may classify a finding as:

```text
false_positive
accepted_risk
duplicate
resolved
reopen
```

A false-positive or accepted-risk suppression is valid only when its trusted file context still matches. If the file changes, the old decision is no longer an automatic suppression. Its reason is still shown as historical context, allowing the skeptic pass to verify whether the prior explanation remains true.

Suppressions expire by default after 180 days. This prevents a one-time decision from silently hiding a later regression forever.

### Triage commands

List findings:

```bash
eneo-review-memory list --repo eneo-ai/eneo
```

Inspect one using the 12-character fingerprint shown in the PR comment:

```bash
eneo-review-memory show a1b2c3d4e5f6
```

Mark a verified false positive:

```bash
eneo-review-memory decide a1b2c3d4e5f6 false_positive \
  --actor "github:alice" \
  --reason "The tenant-scoped repository binds tenant_id before this query." \
  --expires-days 180
```

Record an accepted temporary risk:

```bash
eneo-review-memory decide a1b2c3d4e5f6 accepted_risk \
  --actor "github:alice" \
  --reason "Approved only for the migration window." \
  --expires-days 30
```

Mark resolved:

```bash
eneo-review-memory decide a1b2c3d4e5f6 resolved \
  --actor "github:alice" \
  --reason "Fixed in PR #456."
```

Reopen after the trusted guard changes:

```bash
eneo-review-memory decide a1b2c3d4e5f6 reopen \
  --actor "github:alice" \
  --reason "The repository scoping contract changed."
```

Export the registry:

```bash
eneo-review-memory export \
  --output /opt/data/review-memory/export.json
```

The model can record observations, but only a human CLI action can dismiss, accept, resolve, duplicate, or reopen a finding.

## 13. Prompt and policy ownership

Keep policy separated by purpose:

```text
SOUL.md       who the reviewer is, its tone, evidence standard, and brevity
AGENTS.md     Eneo invariants, review areas, severity, score, comment contract
SKILL.md      the exact webhook review procedure and tool sequence
SQLite        mutable findings, decisions, suppressions, and audit history
```

Do not put dependency versions, temporary exceptions, or hundreds of past false positives into `SOUL.md`. Keep the identity short and durable.

The current prompt explicitly treats PR metadata, source, comments, and diffs as untrusted data. Repository text cannot override the system policy or request new tools.

After changing version-controlled prompt files, rebuild/redeploy and run:

```bash
/opt/eneo-bootstrap/install.sh --force-agents
```

## 14. Should Eneo index the codebase now?

**No, not for phase one.** Start with the diff plus bounded reads of exact head/base files. Measure the review first.

An index helps when the reviewer repeatedly misses:

- callers several hops away;
- tests covering a changed symbol;
- cross-module dependants;
- architectural blast radius in a large change.

It does not automatically make findings true. A stale or imprecise graph can create its own false positives.

### Option assessment

| Project | Fit for phase one | Assessment |
|---|---:|---|
| `code-review-graph` | Best later option | Narrow review focus, Tree-sitter graph, local SQLite, incremental updates, Python and Svelte/TypeScript support, MCP tool filtering. Use only as a context selector. |
| CocoIndex | Too broad now | Strong incremental indexing/data-lineage engine, but introduces an indexing pipeline, storage choices, transformations, and possibly embeddings for a problem the first reviewer may not have. |
| Cognee | Too broad now | General long-term agent memory and knowledge-graph platform. Valuable for organization-wide memory, but not needed for a small, deterministic false-positive registry. |
| CodeGraphContext | More machinery than needed | Capable MCP/CLI code graph, but adds an embedded or external graph backend and a broader dependency surface. |

### Recommended phase 1.5

Only after metrics show a recurring context problem, run `code-review-graph` in **read-only shadow mode**:

- keep a separate trusted mirror of `develop`;
- refresh the graph outside the agent after trusted pushes;
- keep embeddings disabled initially;
- expose only `get_minimal_context_tool`, `query_graph_tool`, `get_impact_radius_tool`, and `get_review_context_tool`;
- do not expose graph build, update, refactor, apply, wiki, or embedding tools;
- verify every published claim against the exact PR diff or exact PR-head file;
- never store false-positive decisions in the code graph.

The project itself reports limited flow-detection recall and deliberately conservative impact analysis, so a graph edge, risk score, or “missing test” relation is never sufficient evidence for a PR comment.

The starter includes a disabled example at:

```text
examples/optional/code-review-graph.md
```

## 15. Rollout

Treat the first 20 to 30 reviews as calibration. Keep the reviewer advisory and manually triggered.

Track:

```text
reviews requested
reviews completed or incomplete
findings published
findings accepted and fixed
findings rejected as false positives
findings repeated after a prior human decision
median visible comment length
```

A reasonable early target is that developers act on at least half of the published findings. If acceptance is lower, tighten the evidence gate and review severity calibration before adding more models, more prompt text, or an index.

Do not make the AI review a required merge check during this phase. GitHub Actions failure should mean the reviewer infrastructure failed, not that the pull request is insecure.

## 16. Next phase, later

After the PR reviewer is stable, add the separate Mattermost/scheduled profile for:

- weekly critical-path review of auth, OIDC/JWT, RBAC, tenant isolation, retrieval, and tool boundaries;
- trend summaries from the findings database;
- full-codebase exploratory scanning;
- optional multi-model adjudication only for shortlisted high-impact findings.

That profile should have separate memory, skills, schedule, and output rules from the PR reviewer.

## 17. Current upstream references

The guide and starter were checked against the current upstream material on 22 June 2026:

- Hermes webhook and GitHub comment delivery: <https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks>
- Hermes Docker state layout and `/opt/data`: <https://hermes-agent.nousresearch.com/docs/user-guide/docker>
- Hermes plugin guide: <https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin>
- Hermes releases: <https://github.com/NousResearch/hermes-agent/releases/latest>
- Ponytail: <https://github.com/DietrichGebert/ponytail>
- code-review-graph: <https://github.com/tirth8205/code-review-graph>
- CocoIndex: <https://github.com/cocoindex-io/cocoindex>
- Cognee: <https://github.com/topoteretes/cognee>
- CodeGraphContext: <https://github.com/CodeGraphContext/CodeGraphContext>

At the review date, the latest Hermes release shown upstream was v0.17.0 (`v2026.6.19`), and its release notes include active Codex OAuth support.

## 18. Validation boundary

The starter can be statically checked and unit tested, but a local build cannot prove your external configuration. Before relying on it, verify in your environment:

- Dokploy TLS and routing;
- the actual GitHub bot token permissions;
- Codex OAuth login and renewal;
- webhook HMAC matching;
- the exact Hermes image version you pin;
- direct posting on a controlled pull request;
- backup and restore of `/opt/data`.
