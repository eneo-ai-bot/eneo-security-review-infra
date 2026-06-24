# Eneo reviewer learning pipeline

This directory is for private, human-governed reviewer improvement work. It is
not installed into the public webhook reviewer and it is not a policy source.

The public `eneo-reviewer` profile stays locked down: `bootstrap/config.yaml`
disables local file access, shell/code execution, session search, web, memory
writes, skill writes, and delegation. The reviewer writes bounded SQLite
observations through the Eneo plugin. A private `eneo-review-coach` workflow may
read exported observations, propose improvements, and produce normal Git
changes for humans to review.

## First slice

Generate an export from the operator CLI:

```bash
eneo-review-memory export \
  --output /opt/data/review-memory/export.json
```

Then generate a private candidate report from that export:

```bash
eneo-review-memory learning-report \
  --export /opt/data/review-memory/export.json \
  --repo eneo-ai/eneo \
  --output /opt/data/review-memory/learning-candidates.md
```

The report reads explicit human decisions and any populated
`review_quality_feedback` rows. In the current bundle, review-quality feedback
has a table and export path but no public writer yet, so that section is often
empty. That is correct; do not infer learning from silence, merges, thumbs-up,
or a later code change.

New decisions are anchored to the exact `finding_observations.id` that the human
judged. The report derives repository, PR number, head SHA, path, title, and
local `F` reference from that immutable observation, not from the mutable latest
`findings` identity row. Older decisions without observation provenance remain
visible as historical context, but they are marked incomplete and are not
eligible for promotion into policy.

Generated reports may contain maintainer-entered reasons, private URLs, or
customer-specific details. Review and scrub them before committing or sharing.
Move only scrubbed reports that are useful to review as versioned artifacts into
`review-learning/reports/`.

## Signal strength

Strong signals:

- explicit `false_positive` or `intentional_by_design` decision with a reason;
- missed issue linked to a bug, incident, security issue, or concrete example;
- severity correction from an authorized maintainer;
- invalidated or reopened finding with counter-evidence;
- fixed finding backed by a regression test.

Medium signals:

- duplicate finding decisions that show repeated root-cause splitting;
- remediation feedback where the proposed fix was unsafe or impractical;
- repeated unclear or too-verbose feedback across reviews.

Weak signals are ignored:

- silence;
- a PR merge without addressing a finding;
- thumbs-up or generic praise;
- a later code change without a linked decision or test.

## Promotion ladder

1. `captured`: a decision or feedback row exists in SQLite.
2. `candidate`: the export report surfaces it as an improvement candidate.
3. `replay-tested`: a historical replay case proves the current reviewer would
   make the same mistake or should preserve the same useful behavior.
4. `human-approved`: a maintainer accepts the policy, ADR, skill, or plugin
   change.
5. `shadow`: the change is measured on real reviews without making it a gate.
6. `active`: the change is deployed through version control and
   `/opt/eneo-bootstrap/install.sh --force-agents`.
7. `retired/replaced`: a better canonical owner absorbs it or the lesson stops
   matching current architecture.

## Where approved lessons go

Do not create a second always-on policy file for production. Fold approved
lessons into the narrowest canonical owner:

- exact finding decisions stay in SQLite;
- architectural context becomes an accepted ADR;
- visible review shape belongs in `bootstrap/workspace/AGENTS.md`;
- review procedure belongs in `bootstrap/skills/eneo-pr-review/SKILL.md`;
- mechanical enforcement belongs in plugin code and tests;
- replay behavior belongs in `review-learning/replay/`.

Hermes `/learn` can help a private coach draft a skill from curated source
material, but do not run it on arbitrary PR comments, contributor branches, raw
session transcripts, or unsanitized exports. UpSkill-style retrospectives are
useful as a process model: proposed learnings stay advisory until a human
approves them and a replay or focused test protects the behavior.
