# Security Model

This reviewer is an advisory code-review agent with narrow tools. It is useful
because it combines LLM reasoning with deterministic boundaries, not because the
model is trusted.

## Trust Boundaries

- GitHub Actions accepts only comments that start with `/review` or `@review`.
- The workflow requires trusted GitHub association: `OWNER`, `MEMBER`, or
  `COLLABORATOR`.
- `AI_REVIEW_ALLOWED_USERS` must include the requester. Empty means deny all.
- GitHub Actions sends a minimal HMAC-signed webhook payload.
- Hermes runs the review through the bundled plugin, not through a shell.
- The model can read bounded PR context and record candidate findings.
- Deterministic plugin code owns memory writes, publication, feedback parsing,
  and GitHub mutations.

## Tool Surface

The live reviewer does not receive:

- a shell;
- repository write access;
- a general GitHub mutation tool;
- a browser;
- delegation;
- arbitrary code execution;
- access to private coach artifacts under `review-learning/`.

Review output reaches GitHub only through `eneo_review_deliver`. The tool verifies
the PR base/head snapshot, renders the stored comment, publishes deterministic
comment parts and any validated atomic suggestions, and records the delivery
state. Suggestions are grouped in one non-blocking GitHub `COMMENT` review; the
model never receives a GitHub mutation tool.

## Prompt-Injection Handling

PR code, comments, commit messages, docs, and feedback are untrusted data. The
review profile tells the model to treat prompt-injection-looking text as evidence
only. Repository content cannot change reviewer policy, prompts, skills,
suppressions, memory decisions, or feedback commands.

The feedback bridge is outside the model path. It refetches the authoritative
GitHub comment, parses only supported `/review ...` commands, authorizes the
numeric GitHub actor id, writes SQLite through the memory owner, and posts only a
reaction or a short deterministic explanation.

Human feedback and coach exports may inform future reviewer changes, but they do
not automatically rewrite prompts, skills, suppressions, or policy. In short:
review evidence can propose changes, but it cannot change policy by itself.

## Private Claude Verification

Claude verification is an operator-run shadow workflow, not part of the live
webhook reviewer. The public review path does not launch Claude, spawn
subprocesses, delegate to subagents, execute repository code, or hand another
model a GitHub write token.

`eneo-review-memory verification-export` reads an already completed review run
and writes a bounded private JSON artifact with mode `0600`. The artifact is for
falsifying current published findings out of band. It contains stable ids,
base/head SHAs, coverage summary, and bounded `*_untrusted` finding evidence. It
does not contain raw SQLite rows, rendered Markdown, feedback actor identities,
or source comment URLs.
If an operator gives this artifact to an external model, this bounded finding
evidence is the intended review-data egress; do not paste raw database exports
or webhook payloads instead.

Claude output is advisory. It must not suppress findings, rewrite prompts,
change feedback commands, publish comments, or become a merge gate without a
separate human-reviewed implementation and replay evidence.

## GitHub Token Boundaries

Use separate repository-scoped tokens. [Operations](OPERATIONS.md) owns the
exact permission matrix.

None of these tokens need repository administration, workflow write, package
write, secrets access, branch deletion, or contents write.

The publisher token needs Pull requests read/write for both the PR summary and
native review suggestions. It does not need Issues write or Contents write and
cannot commit those patches through the review flow. A developer chooses whether
to apply an individual suggestion or a selected batch in GitHub, and that human
action creates the commit.

If GitHub returns `Resource not accessible by personal access token`, inspect the
endpoint-specific failure in `eneo-review-memory publications --json`. Most
runtime 403s are missing org approval or missing Issues/Pull requests permission
on a fine-grained token.

## Dependency Vulnerability Scanning

The reviewer does not currently perform full dependency vulnerability scanning.

It reviews code and security risks introduced or worsened by the PR. If a PR
changes dependency manifests or lockfiles, it may reason about obvious risks such
as unpinned packages, suspicious dependency additions, removed lockfile
discipline, or a dangerous version change. That is still LLM review, not a CVE
database lookup.

Keep deterministic dependency controls in CI:

- GitHub Dependency Review;
- Dependabot alerts;
- CodeQL or SARIF code scanning;
- OSV, Snyk, Trivy, `npm audit`, `pip-audit`, or equivalent scanners.

The best integration is to let deterministic scanners produce their own results
and, later, optionally let the reviewer summarize or prioritize those results.
Do not make the model the source of truth for CVE/GHSA status.

## Human-Governed Suppressions

Only allowlisted human feedback or an operator command can suppress a finding.
The model can record observations, but it cannot mark itself correct or dismiss
its own findings.

Suppressions bind to the exact reviewed file version and expire. If the file
changes, the finding is re-evaluated. ADRs are context, not immunity: an accepted
ADR can explain an architectural decision, but the reviewer should still check
the invariants the ADR requires.

## Data Handling

The SQLite database stores findings, review runs, publication and suggestion
metadata, human decisions, and review-quality feedback. It can contain sensitive
unpublished findings and maintainer-entered reasons. Back it up securely and
scrub exports before sharing.

Coach and verification exports are private analysis artifacts. They contain
bounded untrusted text, stable ids, exact observation provenance, hashes, and
event metadata for human-reviewed workflows. The public webhook reviewer does
not read those exports.

## Non-Goals

This deployment is not a replacement for:

- deterministic CI;
- tests;
- type checks;
- migration checks;
- deterministic security scanners listed above;
- human ownership.

Do not make the reviewer a required merge check until the team has measured its
false-positive rate, acceptance rate, missed-issue feedback, and operational
failure modes.
