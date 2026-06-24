# Eneo Hermes PR reviewer

This repository contains the deployable Eneo PR reviewer: a locked-down Hermes
Agent gateway, an Eneo-specific review contract, bounded GitHub read tools, and a
SQLite registry for findings and human feedback.

A trusted maintainer writes exactly `@review` on an open pull request. GitHub
Actions validates the requester and sends a signed webhook to Hermes. Hermes uses
Codex through ChatGPT OAuth, reads only bounded PR context through the bundled
plugin, runs a two-pass evidence review, records every surviving finding in
SQLite, renders the final comment through the plugin finalizer, removes matching
human-approved suppressions, and posts one structured PR comment.

The comment includes a short severity-count summary, every active finding as an
expanded section with a local reference such as `F1`, hidden fingerprint metadata
for routing, and one copyable all-findings fix brief for a coding agent. The
reviewer does not get a shell, file-write tool, general GitHub write tool, web
browser, delegation, or code execution.

## What is included

- A Dokploy-ready Compose service and derived Hermes image.
- Codex subscription login through `hermes model`; no OpenAI API key is required.
- A maintainer-only `@review` GitHub Actions trigger.
- Native Hermes `github_comment` delivery through the GitHub CLI.
- Eneo-specific `SOUL.md`, `AGENTS.md`, and a two-pass review skill.
- Ponytail v4.7.0 to prefer the smallest correct remediation without weakening
  validation, security, reliability, data protection, or accessibility.
- A native Hermes plugin that reads bounded GitHub PR context, stores finding
  history in SQLite, and renders the final review comment.
- Human-only decisions for false positives, accepted risks, duplicates,
  resolutions, and reopenings.
- An optional, disabled later-design note for `code-review-graph`.

## Review flow

```text
trusted maintainer comments @review
        |
        v
GitHub Actions checks username + repository association
        |
        v
HMAC-signed minimal webhook payload
        |
        v
Hermes + Codex
  pass 1: candidate review
  pass 2: skeptical falsification gate
        |
        v
SQLite finding/suppression check
        |
        v
deterministic comment finalizer
        |
        v
one structured GitHub PR comment
```

The model does not receive a general shell, repository write tool, or arbitrary
GitHub mutation tool. Direct posting still works because Hermes performs the
single configured `github_comment` delivery after the model has finished. This
keeps the desired output while avoiding a broad write surface.

## 1. Create the GitHub reviewer identity

Create a dedicated GitHub machine user or bot account and grant it access only to
`eneo-ai/eneo`. Create a fine-grained personal access token restricted to that
repository with:

- **Contents: read**
- **Pull requests: read and write**
- **Metadata: read**

GitHub also accepts **Issues: write** for issue-style PR comments, but one write
permission is sufficient. Store this token only as `GH_TOKEN` on the VPS. Do not
put it in the public repository or in the trigger workflow.

## 2. Deploy with Dokploy

Put this starter directory in a private infrastructure repository. In Dokploy,
create a Docker Compose application using `compose.yaml`.

Copy `.env.example` to `.env` and set at least:

```dotenv
WEBHOOK_SECRET=<output of: openssl rand -hex 32>
GH_TOKEN=<fine-grained GitHub bot token>
ENEO_ALLOWED_REPOSITORIES=eneo-ai/eneo
```

Keep these defaults:

```dotenv
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644
GH_PROMPT_DISABLED=1
HERMES_DASHBOARD=0
API_SERVER_ENABLED=false
```

Attach an HTTPS domain such as `review-bot.example.org` to service
`hermes-review`, container port `8644`. Only the webhook service should be
reachable. The dashboard and OpenAI-compatible API are disabled.

The named `hermes_review_data` volume is mounted at `/opt/data`. It contains
Hermes configuration, Codex OAuth state, sessions, skills, plugins, and the
review database. Never run two Hermes gateways against the same volume.

Deploy the application.

## 3. Install the reviewer and connect Codex

Open a terminal in the `hermes-review` container and run:

```bash
/opt/eneo-bootstrap/install.sh
hermes plugins list
hermes model
```

Choose **OpenAI Codex** and complete the ChatGPT device-code login with the
subscription account intended for this reviewer. Restart the Dokploy service,
then verify:

```bash
curl -fsS http://127.0.0.1:8644/health
gh auth status
hermes doctor
hermes plugins list
```

`GH_TOKEN` is consumed directly by the GitHub CLI, so an interactive `gh auth
login` is not required when the environment variable is present.

## 4. Install the GitHub trigger

Copy:

```text
examples/github/ai-review-request.yml
```

to the public Eneo repository as:

```text
.github/workflows/ai-review-request.yml
```

Create these Actions secrets:

```text
HERMES_REVIEW_URL=https://review-bot.example.org/webhooks/eneo-review
HERMES_WEBHOOK_SECRET=<same value as WEBHOOK_SECRET>
```

Create this Actions variable:

```text
AI_REVIEW_ALLOWED_USERS=alice,bob,security-maintainer
```

The value may use commas, spaces, or newlines. An empty variable denies all
requests. The workflow also requires GitHub's trusted association to be
`OWNER`, `MEMBER`, or `COLLABORATOR`.

Protect the workflow with CODEOWNERS or a ruleset, for example:

```text
/.github/workflows/ai-review-request.yml @eneo-ai/security-maintainers
```

The workflow has `permissions: {}`, does not check out the pull request, and
sends only repository name, PR number, requester, and request ID to Hermes. It
must exist on the repository's default branch (`develop` for Eneo) before an
`issue_comment` event can start it. The request allows up to 15 minutes for the
agent review and GitHub delivery; the workflow job itself has a 20-minute cap.
Retries reuse the original comment ID so Hermes' idempotency cache does not start
a duplicate review.

## 5. Run a review

On an open, non-draft pull request, an allowlisted maintainer comments exactly:

```text
@review
```

Hermes reviews the current head and posts one structured PR comment. It
publishes every unsuppressed, evidence-backed, independent root-cause finding
that survives the skeptical gate. Medium and Low findings remain visible and
expanded; lower severity controls ordering and priority, not visibility. When
findings exist, one collapsed **Copyable fix brief for a coding agent** contains
every published finding in a single fenced code block that can be copied into
Codex or Claude Code.

After fixing findings, push the fix commit and comment `@review` again. The
rerun re-checks previous unresolved findings, reviews the new fix delta, and
performs a compact safety sweep of the current PR. A later publisher slice should
update one current bot review comment per PR and preserve stable `F1`/`F2`
references across review iterations.

The reviewer covers:

- tenant isolation, authentication, OIDC/JWT, RBAC, secrets, files, retrieval,
  webhooks, MCP/tool boundaries, and provider credentials;
- correctness, transactions, concurrency, idempotency, background jobs, error
  paths, and data integrity;
- FastAPI, OpenAPI, Pydantic, strict Python typing, TypeScript contracts,
  validation, and compatibility;
- tests that prove the actual failure mode;
- maintainability, ownership boundaries, duplicated policy, hidden coupling,
  misleading abstractions, and concrete AI-generated overbuilding;
- query behavior, performance, Alembic migrations, locking, rollback, and data
  loss.

It omits style comments, vague possibilities, generic best-practice lectures,
and findings that do not survive the skeptical second pass.

A direct smoke request is available:

```bash
python3 scripts/smoke_webhook.py \
  --url https://review-bot.example.org/webhooks/eneo-review \
  --secret "$WEBHOOK_SECRET" \
  --repo eneo-ai/eneo \
  --pr 123
```

This test will post a real comment when the PR and credentials are valid.

## 6. How the two-pass review works

The first pass creates every concrete candidate it can tie to the diff. The
second pass tries to reject each candidate by checking nearby guards, callers,
callees, base behavior, framework guarantees, transactions, and relevant tests.

A finding is publishable only when all of the following hold:

- it is introduced or materially worsened by the diff;
- it has an exact changed file and line;
- it has a concrete failure or exploit path;
- benign explanations were actively checked;
- confidence is at least `0.85`;
- the internal evidence score is at least `8/10` for Critical/High or `7/10`
  for Medium/Low;
- the proposed remediation is the smallest change that actually holds.

Medium and Low findings are reserved for concrete, actionable feedback. They are
published when they survive the same evidence gate as higher-severity findings,
and their details stay expanded in the visible review. Compact writing and
deterministic ordering keep the comment scannable; visibility is not the
severity control.

This is a lightweight council pattern inside one Codex run: proposer first,
skeptic second, editor last. The passes share the same PR context and the second
pass must actively look for guards, framework guarantees, and tests that defeat
the first-pass claim. It gives most of the false-positive benefit without three
models independently generating three walls of text.

## 7. Durable finding and false-positive memory

The curated database is:

```text
/opt/data/review-memory/review_memory.sqlite3
```

Each published finding receives a stable fingerprint based on repository, rule,
path, symbol, and semantic anchor. The line number is deliberately excluded so a
line move does not create a new identity.

List findings:

```bash
eneo-review-memory list --repo eneo-ai/eneo
```

Inspect one using its 12-character fingerprint prefix. Fingerprints are operator
metadata; the developer-facing review body should use local references such as
`F1` and `F2` once publication mapping lands.

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

Other decisions:

```bash
eneo-review-memory decide <fingerprint> accepted_risk \
  --actor "github:alice" \
  --reason "Approved during the migration window." \
  --expires-days 30

eneo-review-memory decide <fingerprint> duplicate \
  --actor "github:alice" \
  --reason "Tracked by finding 0123456789ab." \
  --expires-days 180

eneo-review-memory decide <fingerprint> resolved \
  --actor "github:alice" \
  --reason "Fixed in PR #456."

eneo-review-memory decide <fingerprint> reopen \
  --actor "github:alice" \
  --reason "The trusted guard changed."
```

Future PR-comment feedback should separate finding decisions from review-quality
feedback. Finding commands can use local references, for example
`@review false-positive F2 <reason>`, `@review intentional F2 ADR-0042`, and
`@review reopen F2 <reason>`. Review-quality commands such as
`@review feedback unclear F2 <reason>` or `@review feedback missed <issue>`
should feed metrics and replay cases, not automatic suppressions.

Only a human CLI command can create a suppression. The model may record a
finding, but it cannot mark itself correct, dismiss itself, or alter prior human
decisions.

Suppressions are conservative. The decision is tied to the trusted GitHub blob
hash that a human reviewed and expires by default after 180 days. If that file
changes, automatic suppression is removed and the issue must be revalidated.
The old reason still appears to the reviewer as a historical hint, which usually
lets the skeptical pass reject an unchanged false positive without silently
hiding a new regression.

Export the registry:

```bash
eneo-review-memory export \
  --output /opt/data/review-memory/export.json
```

Back up the `/opt/data` volume securely. It contains OAuth credentials and may
contain sensitive unpublished findings.

## 8. Tune the review policy

Canonical policy lives in:

```text
bootstrap/SOUL.md
bootstrap/workspace/AGENTS.md
bootstrap/skills/eneo-pr-review/SKILL.md
```

Keep `SOUL.md` focused on reviewer identity, evidence, tone, and brevity.
Keep `AGENTS.md` focused on Eneo invariants and the visible comment contract.
Keep the skill focused on the exact two-pass procedure.

After editing the version-controlled source, rebuild/redeploy and run:

```bash
/opt/eneo-bootstrap/install.sh --force-agents
```

The installer preserves a live `AGENTS.md` by default to avoid destroying local
edits. Prefer editing the source and forcing an explicit update rather than
allowing policy drift inside the volume.

## 9. Indexing recommendation

Do not add CocoIndex, Cognee, CodeGraphContext, or any vector store to phase one.
The PR diff and bounded file reads are faster to operate and easier to trust.

When measurements show repeated misses involving callers, dependants, or tests,
start with `code-review-graph` in read-only shadow mode. It is the narrowest fit:
local SQLite storage, Tree-sitter relationships, Python and Svelte/TypeScript
support, incremental updates, and MCP tool filtering. Keep embeddings off and
expose only query tools. The project itself documents limited flow-detection
recall and conservative impact results, so graph output must remain a context
selector rather than evidence. See:

```text
examples/optional/code-review-graph.md
```

The code graph is disposable derived context. It must never become the source of
truth for false-positive decisions, and a graph edge or risk score is not enough
to publish a finding.

## 10. Rollout and measurements

Treat the first 20 to 30 reviews as advisory calibration. Track:

```text
reviews requested
reviews completed or incomplete
findings published
findings accepted and fixed
findings rejected as false positive
review-quality feedback
missed issues reported
findings repeated after a prior decision
median visible comment length
```

A good early target is that developers act on at least half of published
findings. If the acceptance rate is lower, tighten the evidence gate and review
severity calibration before adding more models or more context systems.

Do not make this a required merge check until the team has reviewed its behavior.
It is an AI code and security review, not a replacement for CodeQL, dependency
review, tests, type checking, migration checks, or human ownership.

## Updating and validation

For production, pin `HERMES_IMAGE` to a reviewed immutable digest. After an
update:

```bash
/opt/eneo-bootstrap/install.sh
```

Then restart and request a review on a controlled PR.

Run the bundle checks locally:

```bash
./scripts/check_bundle.sh
```

The supplied code is validated with Python compilation, YAML parsing, and unit
tests. It cannot live-test your Dokploy routing, GitHub bot token, ChatGPT OAuth,
or repository policies from this bundle.
