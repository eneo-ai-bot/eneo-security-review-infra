# Operations

This document owns setup, configuration, deployment, recovery, and operator
commands for the Hermes GitHub PR review agent.

## Prerequisites

- A private infrastructure repository that deploys this bundle.
- A Docker Compose host or Dokploy project.
- An HTTPS route to the review webhook service.
- A second HTTPS route to the feedback webhook service.
- A GitHub account or bot with repository-scoped fine-grained tokens.
- A ChatGPT/Codex subscription account for `hermes auth add openai-codex`.
- Permission to add one GitHub Actions workflow, Actions secrets, and an Actions
  variable to each reviewed repository.

## GitHub Tokens

Create separate fine-grained personal access tokens. Scope each token to the
reviewed repository, for example `<org>/<repo>`.

| Token env var | Required permissions | Purpose |
| --- | --- | --- |
| `GITHUB_READ_TOKEN` | Contents read, Pull requests read, Metadata read | PR metadata, diff, and file reads. |
| `ENEO_REVIEW_PUBLISH_GH_TOKEN` | Metadata read, Pull requests read/write | Create, update, and delete PR summary comments and publish native suggested changes. |
| `ENEO_FEEDBACK_GH_TOKEN` | Issues read/write, Metadata read, Pull requests read | Add feedback reactions and read PR/comment state. |

The publisher tries `GITHUB_READ_TOKEN` for read paths first and uses
`ENEO_REVIEW_PUBLISH_GH_TOKEN` for comment and review writes. The publisher token
does not need Contents write or Issues write: GitHub accepts Pull requests write
for comments on pull requests, and only the developer's GitHub action creates a
commit from a proposed patch. Endpoint-specific failures such as
`github_403_get_pull_request`, `github_403_list_issue_comments`,
`github_403_create_issue_comment`, or
`github_403_create_pull_request_review` identify the
missing permission or org approval path.

## Environment

Set these values in the Dokploy Compose environment:

| Name | Required | Default | Notes |
| --- | --- | --- | --- |
| `HERMES_IMAGE` | yes | pinned digest in `.env.example` | Keep image updates reviewed. |
| `TZ` | no | `Europe/Stockholm` | Container timezone. |
| `WEBHOOK_ENABLED` | yes | `true` | Enables Hermes webhook mode. |
| `WEBHOOK_PORT` | yes | `8644` | Review webhook port. |
| `WEBHOOK_SECRET` | yes | none | HMAC secret for `/webhooks/eneo-review`. |
| `ENEO_FEEDBACK_WEBHOOK_SECRET` | yes | none | Different HMAC secret for feedback. |
| `GITHUB_READ_TOKEN` | yes | none | Read token described above. |
| `ENEO_REVIEW_PUBLISH_GH_TOKEN` | yes | none | Publisher token described above. |
| `ENEO_REVIEW_PUBLISH_MAX_BYTES` | no | `60000` | Max bytes per GitHub comment, not a finding cap. |
| `ENEO_FEEDBACK_GH_TOKEN` | yes | none | Feedback token described above. |
| `ENEO_ALLOWED_REPOSITORIES` | yes | none | Comma-separated exact repositories, for example `<org>/<repo>`. |
| `ENEO_FEEDBACK_ALLOWED_ACTOR_IDS` | yes | none | Comma-separated numeric GitHub user ids allowed to give feedback. |
| `ENEO_REVIEW_FEEDBACK_ENABLED` | no | `false` | Enables feedback help in rendered comments. |
| `GH_PROMPT_DISABLED` | yes | `1` | Prevents interactive GitHub auth prompts. |
| `HERMES_DASHBOARD` | yes | `0` | Keep the dashboard off for this deployment. |
| `API_SERVER_ENABLED` | yes | `false` | Keep the OpenAI-compatible API off. |
| `PYTHONUNBUFFERED` | no | `1` | Easier logs. |

`ENEO_REVIEW_DB` is not a public `.env` setting. Compose sets it explicitly:

```text
ENEO_REVIEW_DB: /opt/data/review-memory/review_memory.sqlite3
```

## Deploy In Dokploy

Deploy `compose.yaml` as a Docker Compose application. Attach:

- review domain -> service `hermes-review`, port `8644`;
- feedback domain -> service `hermes-review-feedback`, port `8645`.

Only those webhook routes should be public. Keep the Hermes dashboard and API
server disabled.

The Compose file intentionally forwards an explicit environment allowlist to
each container. Add future runtime variables to the correct service explicitly.

## Persistent State

The deployment uses two named volumes:

| Volume | Mounted in | Purpose |
| --- | --- | --- |
| `hermes_review_data` | `hermes-review` at `/opt/data` | Hermes config, Codex OAuth state, sessions, managed skills, and plugins. |
| `review_memory_data` | reviewer at `/opt/data/review-memory`, feedback at `/review-memory` | SQLite review database. |

Do not run two Hermes gateways against the same `hermes_review_data` volume.

The `review-memory-init` one-shot service runs before the reviewer and feedback
sidecar on each deploy. It refreshes the managed profile and plugin under
`/opt/data`, then runs the idempotent SQLite schema migration. Seeing
`review-memory-init` as `Exited (0)` is expected. Use its logs only for startup
failures.

Manual recovery only:

```bash
/opt/eneo-bootstrap/install.sh --force-agents
eneo-review-memory init
```

Run those commands inside the `hermes-review` container, then restart the
service.

## Connect Codex

Inside the `hermes-review` container:

```bash
hermes plugins list
hermes auth add openai-codex
/opt/eneo-bootstrap/install.sh
```

Complete the ChatGPT device-code login with the intended subscription account.
The managed profile, rather than the interactive model picker, owns
`openai-codex`, `gpt-5.6-sol`, and `xhigh`. Restart the service and verify:

```bash
curl -fsS http://127.0.0.1:8644/health
hermes status
hermes doctor
hermes plugins list
```

Inside the `hermes-review-feedback` container:

```bash
curl -fsS http://127.0.0.1:8645/ready
eneo-review-feedback-bridge verify-config
```

## Install The GitHub Trigger

Copy `examples/github/ai-review-request.yml` to the reviewed repository as:

```text
.github/workflows/ai-review-request.yml
```

Create these Actions secrets:

```text
HERMES_REVIEW_URL=https://review.example.org/webhooks/eneo-review
HERMES_WEBHOOK_SECRET=<same value as WEBHOOK_SECRET>
HERMES_REVIEW_FEEDBACK_URL=https://review-feedback.example.org/webhooks/eneo-review-feedback
HERMES_REVIEW_FEEDBACK_SECRET=<same value as ENEO_FEEDBACK_WEBHOOK_SECRET>
```

Create this Actions variable:

```text
AI_REVIEW_ALLOWED_USERS=alice,bob,security-maintainer
```

The workflow accepts commas, spaces, or newlines. An empty value denies all
requests. GitHub must also report the commenter as `OWNER`, `MEMBER`, or
`COLLABORATOR`.

Protect the workflow with CODEOWNERS or a ruleset, for example:

```text
/.github/workflows/ai-review-request.yml @<org>/<maintainer-team>
```

The workflow grants `issues: write` and `pull-requests: write` to its built-in
token for the non-blocking eyes reaction on an accepted PR comment. GitHub's
Actions integration returned `403 Resource not accessible by integration` for
this PR-comment reaction when only `issues: write` was granted, even though the
REST path is under issue comments. Keep both permissions unless a production
workflow run proves the pull-request permission is no longer required.
Webhook secrets are scoped to the dispatch step and are not inherited by the
reaction step. The workflow does not check out PR code. It sends only repository
name, PR number, requester, and request id to Hermes. The workflow must exist on
the repository's default branch before an `issue_comment` event can start it.

Set `ENEO_REVIEW_FEEDBACK_ENABLED=true` in Dokploy if the rendered review comment
should show the copyable feedback commands documented below.

## Run A Review

On an open, non-draft pull request, an allowlisted maintainer comments:

```text
/review
```

After fixing findings, push the fix commit and comment `/review` again. Every
explicit request after the previous run reaches a terminal state creates a new
chronological review round such as `Review 2`, including a deliberate rerun of
the same base/head snapshot.

For an exact, independently safe local fix, the reviewer may publish a native
GitHub suggestion in **Files changed**. All suggestions for one review round are
grouped in one non-blocking `COMMENT` review instead of separate timeline
comments. Review and apply them individually, or add only the patches you want to
GitHub's suggestion batch and commit that selection together. Coordinated fixes
remain in the copyable coding-agent brief. After either path, run CI and post a
fresh top-level `/review`; applying a suggestion does not mark its finding
resolved. To keep the native review scannable, one round publishes at most 12
highest-priority, non-overlapping atomic patches.

Different PRs may run concurrently. A second `/review` on the same PR is treated
as a duplicate while a run is active.

## Developer Feedback

Post feedback as a new top-level PR comment. Do not edit an old command and do
not reply inside an inline diff thread.

```text
/review false-positive F2 because <what code, guard, or invariant disproves it>

/review feedback scope F2 because <why this finding is in the diff but outside the intended PR scope>

/review feedback missed because <what concrete issue was missed and where>
```

`false-positive` is a durable finding decision. `feedback scope` records
author-intent or stacked-branch scope confusion without suppressing the finding.
`feedback missed` records review-quality feedback for metrics, replay cases, and
private reviewer-improvement analysis.

Successful feedback receives a `+1` reaction. Invalid, stale, not-current, or
unsupported commands receive a `confused` reaction and one deterministic
explanation. Intentional-design and accepted-risk decisions remain CLI or
governance actions until there is deterministic ADR validation for PR comments.

## Runbook

Inspect recent runs:

```bash
eneo-review-memory runs --repo <org>/<repo> --limit 10
eneo-review-memory runs --repo <org>/<repo> --stats --json
```

Inspect publication state:

```bash
eneo-review-memory publications --repo <org>/<repo> --pr <number>
eneo-review-memory publications --repo <org>/<repo> --pr <number> --json
```

Inspect coverage for one run:

```bash
eneo-review-memory coverage --run-id <id> --json
```

Mark stale runs failed after a crash:

```bash
eneo-review-memory runs --mark-stalled --older-than-minutes 10 --repo <org>/<repo> --pr <number>
```

Common states:

| Symptom | Meaning |
| --- | --- |
| `generated` with no `posting` timestamp | Old review skill did not call the delivery tool. |
| `publish_failed` | GitHub publication was attempted and failed; inspect `failure=`. |
| `body_too_large` | Review could not fit within the configured per-comment byte budget. |
| `stale` | PR base or head changed before posting. |
| `stalled` or old `running` | Run heartbeat stopped; mark stale runs failed before retrying. |

The run ledger includes `phase`, `heartbeat`, and `failure`. A healthy run moves
through `accepted`, `fetching_pr`, `collecting_diff`, `reviewing`, `rendering`,
`publishing`, and `posted`.

## Memory And Decisions

List findings:

```bash
eneo-review-memory list --repo <org>/<repo>
```

Show one finding:

```bash
eneo-review-memory show <fingerprint-prefix>
```

Prefer exact observation ids or PR-local references when recording decisions:

```bash
eneo-review-memory decide <fingerprint> false_positive \
  --repo <org>/<repo> \
  --pr <number> \
  --local-reference F2 \
  --actor "github:alice" \
  --reason "The tenant-scoped repository binds tenant_id before this query." \
  --expires-days 180

eneo-review-memory decide <fingerprint> resolved \
  --repo <org>/<repo> \
  --pr <number> \
  --local-reference F2 \
  --actor "github:alice" \
  --reason "Fixed in the latest commit."
```

Other decision values are `accepted_risk`, `duplicate`, and `reopen`. Security
owns the suppression trust rules in [docs/SECURITY.md](SECURITY.md).

## Backups And Migration

Back up the `review_memory_data` volume securely. It may contain unpublished
findings and human-entered reasons.

If upgrading from an older deployment that stored the database under
`/opt/data/review-memory` inside `hermes_review_data`, stop the reviewer and
feedback services before migrating:

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

## Private Reviewer-Improvement Exports

Export the registry:

```bash
eneo-review-memory export \
  --output /opt/data/review-memory/export.json
```

Generate a learning report:

```bash
eneo-review-memory learning-report \
  --export /opt/data/review-memory/export.json \
  --repo <org>/<repo> \
  --output /opt/data/review-memory/learning-candidates.md
```

Generate a bounded coach bundle:

```bash
eneo-review-memory coach-export \
  --export /opt/data/review-memory/export.json \
  --repo <org>/<repo> \
  --after-decision-id 0 \
  --after-feedback-id 0 \
  --output /opt/data/review-memory/coach-export.json
```

Generate a bounded private verification bundle for one completed review run:

```bash
eneo-review-memory verification-export \
  --run-id <id> \
  --output /opt/data/review-memory/verification/run-<id>.json
```

This is the private verifier slice. The export is a shadow artifact, not a live
review step. It does not publish comments. The bundle contains stable
run/publication ids, exact base/head SHAs, coverage summary, and bounded
`*_untrusted` evidence for the current published findings. A maintainer may hand
it to Claude or another private review tool and ask for falsification. Verifier
output can be stored in SQLite for audit, but raw verifier verdicts are not
authoritative: only an explicit Codex reconciliation decision for the same run
can drop a recorded candidate before publication.

The schema is provider-neutral (`provider`, `model`, `mode`, `status`) so future
profiles can choose Codex-only, advisory verification, or gated verification.
This repository does not launch Claude from the webhook reviewer in the current
slice; adding that runner is a separate reviewed runtime change.

Do not paste raw SQLite exports into an LLM. Use `verification-export` for
review-finding falsification and `coach-export` for reviewer-improvement
signals.

Validate replay fixtures:

```bash
eneo-review-memory validate-replay review-learning/replay
```

The public webhook reviewer does not read `review-learning/`. Coach exports are
private LLM input artifacts. They can contain bounded maintainer-entered reasons
or repository text, so scrub them before committing or sharing.

## Updating And Validation

`HERMES_IMAGE` is pinned to the Hermes 0.18.2 release tag and its immutable
multi-platform digest in `.env.example`, `compose.yaml`, and `Dockerfile`.
Update both the human-readable tag and digest through a reviewed dependency
bump. Never replace this with the moving `latest` or `main` tag.

Hermes 0.18.2 predates GPT-5.6 in its offline Codex picker, but its Codex route
accepts an explicitly configured model and uses live model discovery when the
OAuth endpoint is available. The managed profile therefore configures
`gpt-5.6-sol` directly instead of depending on the picker. A controlled review
after deployment is the final proof that the subscription is entitled to the
model and the OAuth route accepts it.

After a source update, redeploy. The `review-memory-init` service refreshes the
managed `/opt/data` profile and migrates SQLite before the gateway starts.

Run local bundle checks:

```bash
./scripts/check_bundle.sh
```

The checks cover Python imports, strict type checks, unit tests, replay fixtures,
and YAML. They do not prove Dokploy routing, GitHub org token approval, ChatGPT
OAuth state, or repository rules.
