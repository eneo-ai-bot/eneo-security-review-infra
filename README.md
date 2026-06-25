# Eneo Hermes PR reviewer

This repository contains the deployable Eneo PR reviewer: a locked-down Hermes
Agent gateway, an Eneo-specific review contract, bounded GitHub read tools, and a
SQLite registry for findings and human feedback.

A trusted maintainer writes exactly `/review` on an open pull request. GitHub
Actions validates the requester and sends a signed webhook to Hermes. Hermes uses
Codex through ChatGPT OAuth, reads only bounded PR context through the bundled
plugin, runs a two-pass evidence review, records every surviving finding in
SQLite, renders the final comment through the plugin finalizer, removes matching
human-approved suppressions, stores the exact rendered Markdown, and publishes
one canonical structured PR comment through the deterministic publisher tool.

The comment includes a short severity-count summary, every active finding as an
expanded section with a local reference such as `F1`, hidden fingerprint metadata
for routing, and one copyable all-findings fix brief for a coding agent. The
reviewer does not get a shell, file-write tool, general GitHub write tool, web
browser, delegation, or code execution.

## What is included

- A Dokploy-ready Compose service and derived Hermes image.
- Codex subscription login through `hermes model`; no OpenAI API key is required.
- A maintainer-only `/review` GitHub Actions trigger, with `@review` kept as a
  compatibility alias.
- Deterministic GitHub comment publication through `eneo_review_publish`.
- Eneo-specific `SOUL.md`, `AGENTS.md`, and a two-pass review skill.
- Ponytail v4.7.0 to prefer the smallest correct remediation without weakening
  validation, security, reliability, data protection, or accessibility.
- A native Hermes plugin that reads bounded GitHub PR context, stores finding
  history in SQLite, and renders the final review comment.
- Human-only decisions for false positives, accepted risks, duplicates,
  resolutions, and reopenings.
- Private coach tooling that exports bounded learning evidence and selects
  deterministic, human-reviewable reviewer-improvement proposals.
- An optional, disabled later-design note for `code-review-graph`.

## Review flow

```text
trusted maintainer comments /review
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
deterministic publisher verifies base/head + stored body
        |
        v
one structured GitHub PR comment
```

The model does not receive a general shell, repository write tool, or arbitrary
GitHub mutation tool. The final Hermes response is logged only; the
`eneo_review_publish` tool loads the stored body and PR target from SQLite,
verifies the exact base/head SHA, and creates or updates the canonical comment.
If the rendered review is too large for one GitHub comment, the publisher splits
it into deterministic continuation comments instead of truncating or hiding
verified findings.

## 1. Create the GitHub reviewer identity

Create a dedicated GitHub machine user or bot account and grant it access only to
`eneo-ai/eneo`. Create a read-only fine-grained personal access token restricted
to that repository with:

- **Contents: read**
- **Pull requests: read**
- **Metadata: read**

Store that token as `GITHUB_READ_TOKEN`.

Create a second fine-grained token for deterministic review publication:

- **Pull requests: read**
- **Issues: read and write**
- **Metadata: read**

Store that token as `ENEO_REVIEW_PUBLISH_GH_TOKEN`. It is used only by the
publisher tool to create or update the canonical PR review comment.

Create a second fine-grained token for the deterministic feedback sidecar:

- **Pull requests: read**
- **Issues: read and write**
- **Metadata: read**

Store that token as `ENEO_FEEDBACK_GH_TOKEN`. It does not need Contents read and
it should not be reused by the main reviewer.

## 2. Deploy with Dokploy

Put this starter directory in a private infrastructure repository. In Dokploy,
create a Docker Compose application using `compose.yaml`.

Copy `.env.example` to `.env` and set at least:

```dotenv
WEBHOOK_SECRET=<output of: openssl rand -hex 32>
ENEO_FEEDBACK_WEBHOOK_SECRET=<different output of: openssl rand -hex 32>
GITHUB_READ_TOKEN=<fine-grained read token>
ENEO_REVIEW_PUBLISH_GH_TOKEN=<fine-grained publisher token>
ENEO_FEEDBACK_GH_TOKEN=<fine-grained feedback token>
ENEO_ALLOWED_REPOSITORIES=eneo-ai/eneo
ENEO_FEEDBACK_ALLOWED_ACTOR_IDS=<comma-separated numeric GitHub user ids>
ENEO_REVIEW_FEEDBACK_ENABLED=true
```

Keep these defaults:

```dotenv
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644
GH_PROMPT_DISABLED=1
ENEO_REVIEW_PUBLISH_MAX_BYTES=60000
HERMES_DASHBOARD=0
API_SERVER_ENABLED=false
```

`ENEO_REVIEW_PUBLISH_MAX_BYTES` controls the maximum size of each GitHub comment,
not the number of findings reviewed or published. Lowering it does not suppress
findings; it only makes the publisher use more continuation comments.

Attach an HTTPS domain such as `review-bot.example.org` to service
`hermes-review`, container port `8644`. Attach a second HTTPS route such as
`review-feedback.example.org` to service `hermes-review-feedback`, container
port `8645`. Only these webhook services should be reachable. The dashboard and
OpenAI-compatible API are disabled. Keep edge rate limiting available on the
feedback route in Dokploy/Traefik; the sidecar verifies HMAC before GitHub or DB
work, but abuse throttling belongs at the HTTP edge.

The compose file intentionally passes an explicit environment allowlist to each
container instead of forwarding every `.env` key. Add any future required runtime
variable to the correct service's `environment:` block deliberately.

The named `hermes_review_data` volume is mounted at `/opt/data` only in the main
reviewer. It contains Hermes configuration, Codex OAuth state, sessions, skills,
and plugins. The SQLite review database lives in the separate
`review_memory_data` volume, mounted at `/opt/data/review-memory` in the reviewer
and `/review-memory` in the feedback sidecar. Never run two Hermes gateways
against the same `hermes_review_data` volume.

If upgrading from an older deployment that stored the database under
`/opt/data/review-memory` inside `hermes_review_data`, do not copy the SQLite
file directly while services are running. Stop the reviewer and feedback
services, mount the old path and new volume into a one-shot shell, then run:

```bash
eneo-review-memory migrate-volume \
  --source /legacy/review_memory.sqlite3 \
  --destination /review-memory/review_memory.sqlite3 \
  --owner-uid 10000 \
  --owner-gid 10000
```

The command checkpoints WAL, uses SQLite's backup API, verifies integrity and
foreign keys, compares table counts, and leaves the source untouched unless the
new destination was written successfully.

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
hermes doctor
hermes plugins list
```

In the `hermes-review-feedback` container, verify the sidecar readiness check:

```bash
curl -fsS http://127.0.0.1:8645/ready
eneo-review-feedback-bridge verify-config
```

The review container does not need a generic `GH_TOKEN`; comment writes go
through `ENEO_REVIEW_PUBLISH_GH_TOKEN`.

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
HERMES_REVIEW_FEEDBACK_URL=https://review-feedback.example.org/webhooks/eneo-review-feedback
HERMES_REVIEW_FEEDBACK_SECRET=<same value as ENEO_FEEDBACK_WEBHOOK_SECRET>
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

## Prompt-injection posture

PR code, comments, commit messages, docs, and review feedback are treated as
untrusted data. The review skill explicitly forbids following instructions found
inside repository content, and the reviewer tools accept structured parameters
instead of free-form shell commands.

The deterministic feedback bridge is intentionally outside the LLM path. It
refetches the GitHub comment, parses only supported `/review ...` commands,
authorizes the numeric GitHub actor id, writes SQLite through the memory owner,
and posts only a reaction or short deterministic explanation. Human feedback and
coach exports can inform future reviewer changes, but they do not automatically
rewrite prompts, skills, suppressions, or policy.

## 5. Run a review

On an open, non-draft pull request, an allowlisted maintainer comments exactly:

```text
/review
```

Hermes reviews the current head and posts one structured PR comment. It
publishes every unsuppressed, evidence-backed, independent root-cause finding
that survives the skeptical gate. Medium and Low findings remain visible and
expanded; lower severity controls ordering and priority, not visibility. When
findings exist, one collapsed **Copyable fix brief for a coding agent** contains
every published finding in a single fenced code block that can be copied into
Codex or Claude Code.

After fixing findings, push the fix commit and comment `/review` again. The
rerun re-checks previous unresolved findings, reviews the new fix delta, and
performs a compact safety sweep of the current PR. Prior current findings stay
active when the latest run does not explicitly classify them; the review marks
those references as needing recheck instead of treating absence as resolution.
When the reviewer can verify a prior finding, it classifies the stable `F`
reference as resolved, invalidated, suppressed by human decision, still present,
partially resolved, or not checked. The deterministic publisher updates the
current canonical bot review comment per PR after GitHub accepts the replacement;
if publication fails, the previous posted review remains authoritative.

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

## 6. Generate reviewer-improvement proposals

The reviewer can collect human decisions and review-quality feedback without
turning every signal into policy. A private operator can export that evidence
from the persistent `/opt/data` volume and produce a deterministic proposal
bundle:

```bash
eneo-review-memory export \
  --output /opt/data/review-memory/export.json

eneo-review-memory coach-export \
  --export /opt/data/review-memory/export.json \
  --repo eneo-ai/eneo \
  --output /opt/data/review-memory/coach-export.json

eneo-review-memory coach-propose \
  --events /opt/data/review-memory/coach-export.json \
  --output-dir /opt/data/review-memory/coach-proposal
```

`coach-propose` writes `proposal.json` and `SUMMARY.md` with mode `0600`. It
groups only promotion-eligible coach events, requires repeated independent
episodes for normal reviewer changes, and keeps isolated accepted-risk decisions
as governance observations. It does not call Claude/Codex, change reviewer
policy, or open PRs; it creates the bounded evidence packet a human or later
coach can use to decide whether a replay, skill, ADR, or plugin change is
actually warranted.

Review-quality feedback is useful only when it is tied to the exact generated
review publication and head SHA. Unprovenanced feedback is listed as not
promoted rather than treated as policy evidence. These artifacts may still
contain bounded maintainer-entered reasons or repository text, so keep them
private unless they have been scrubbed before committing or sharing.

## 7. How the two-pass review works

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
  --latest \
  --actor "github:alice" \
  --reason "The tenant-scoped repository binds tenant_id before this query." \
  --expires-days 180
```

Prefer an exact observation id or PR-local reference when available:

```bash
eneo-review-memory decide a1b2c3d4e5f6 false_positive \
  --observation-id 418 \
  --actor "github:alice" \
  --reason "The tenant-scoped repository binds tenant_id before this query." \
  --expires-days 180

eneo-review-memory decide a1b2c3d4e5f6 resolved \
  --repo eneo-ai/eneo \
  --pr 240 \
  --local-reference F2 \
  --actor "github:alice" \
  --reason "Fixed in the latest commit."
```

Other decisions:

```bash
eneo-review-memory decide <fingerprint> accepted_risk \
  --latest \
  --actor "github:alice" \
  --reason "Approved during the migration window." \
  --expires-days 30

eneo-review-memory decide <fingerprint> duplicate \
  --latest \
  --actor "github:alice" \
  --reason "Tracked by finding 0123456789ab." \
  --expires-days 180

eneo-review-memory decide <fingerprint> resolved \
  --latest \
  --actor "github:alice" \
  --reason "Fixed in PR #456."

eneo-review-memory decide <fingerprint> reopen \
  --latest \
  --actor "github:alice" \
  --reason "The trusted guard changed."
```

The deterministic bridge for PR-comment feedback separates finding decisions
from review-quality feedback. Finding commands use local references from the
latest generated review,
for example `/review false-positive F2 because <what code disproves it>`.
Review-quality commands such as
`/review feedback missed because <what concrete issue was missed and where>`
feed metrics and replay cases, not automatic suppressions. Post feedback as a
new top-level PR comment; do not
edit an old command or reply in an inline diff thread. The feedback path requires
an allowlisted numeric GitHub actor id from `ENEO_FEEDBACK_ALLOWED_ACTOR_IDS`.
Successful feedback receives a `+1` reaction. Invalid, stale, or unsupported
commands receive a `confused` reaction and one short deterministic explanation.
Intentional-design and accepted-risk decisions remain CLI/governance-only until
there is deterministic ADR validation for PR comments.

Only allowlisted human feedback or a human CLI command can create a suppression.
The model may record a finding, but it cannot mark itself correct, dismiss
itself, or alter prior human decisions.

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

Generate a private learning-candidate report from an export:

```bash
eneo-review-memory learning-report \
  --export /opt/data/review-memory/export.json \
  --repo eneo-ai/eneo \
  --output /opt/data/review-memory/learning-candidates.md
```

Generate the bounded untrusted JSON bundle for a private coach workflow:

```bash
eneo-review-memory coach-export \
  --export /opt/data/review-memory/export.json \
  --repo eneo-ai/eneo \
  --after-decision-id 0 \
  --after-feedback-id 0 \
  --output /opt/data/review-memory/coach-export.json
```

Validate replay fixtures:

```bash
eneo-review-memory validate-replay review-learning/replay
```

This is operator tooling, not live reviewer memory. The public webhook reviewer
does not read `review-learning/`, and the route disables local file access,
general skill writes, memory writes, web, shell, code execution, session search,
and delegation. The report surfaces explicit human decisions and any populated
review-quality feedback rows for a private coach workflow. Empty review-quality
sections mean no allowlisted feedback command has been ingested yet. Do not infer
learning from silence, thumbs-up, merges, or a later code change without a linked
decision or test.
New decisions are tied to the exact finding observation that the human judged.
The learning report derives PR, head SHA, path, and local `F` reference from that
observation instead of the mutable latest finding row. Legacy decisions without
observation provenance remain visible but are marked incomplete and
non-promotable.
Scrub reports before moving useful candidates into `review-learning/reports/` as
versioned artifacts.

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

Learning candidates graduate only through normal version-controlled changes:
exact decisions remain in SQLite, architectural context becomes an ADR, visible
comment shape belongs in `AGENTS.md`, review procedure belongs in the skill,
mechanical enforcement belongs in plugin code and tests, and replay behavior
belongs under `review-learning/replay/`. Do not add a second production policy
file for unapproved lessons.

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

`HERMES_IMAGE` is pinned to a reviewed immutable digest in `.env.example`,
`compose.yaml`, and `Dockerfile`. After an update:

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
