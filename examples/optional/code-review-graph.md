# Optional phase 1.5: read-only code graph

Do not enable this on day one. The default reviewer already reads the PR diff and
bounded surrounding files. Add a graph only after review metrics show that missing
callers, dependants, or tests are a recurring problem.

## Recommendation

Use `code-review-graph` before considering CocoIndex, Cognee, or
CodeGraphContext for this narrow PR-review use case. It is review-focused, stores
its core graph in local SQLite, supports Python and Svelte/TypeScript, and exposes
bounded MCP tools. Keep embeddings disabled initially. Its own documentation
reports limited flow-detection recall and deliberately conservative impact
analysis, so this remains a context selector rather than an evidence source.

The graph is derived context, not decision memory. The Eneo false-positive
registry remains `review_memory.sqlite3` and is never replaced by the graph.

## Safer first integration

Maintain a trusted mirror of the `develop` branch in a separate volume or
container. Index only that trusted branch. The PR reviewer continues to obtain the
untrusted PR diff from GitHub and uses the graph only to ask about existing
callers, imports, tests, and architectural neighbours.

Do not expose graph build, update, refactor, wiki-generation, or embedding tools
to the reviewer. Refresh the graph outside the agent after a trusted push to
`develop`.

Example one-time setup, after verifying and pinning the package version:

```bash
mkdir -p /opt/data/code-index
git clone --filter=blob:none --branch develop \
  https://github.com/eneo-ai/eneo.git /opt/data/code-index/eneo
cd /opt/data/code-index/eneo
uvx --from 'code-review-graph==2.3.6' code-review-graph build
```

Create `/opt/data/bin/code-review-graph-mcp`:

```sh
#!/bin/sh
set -eu
cd /opt/data/code-index/eneo
exec uvx --from 'code-review-graph==2.3.6' code-review-graph serve --tools \
  get_minimal_context_tool,query_graph_tool,get_impact_radius_tool,get_review_context_tool
```

Then add the following to the managed Hermes configuration:

```yaml
platform_toolsets:
  webhook:
    - eneo_review
    - mcp-code_review_graph

mcp_servers:
  code_review_graph:
    command: /opt/data/bin/code-review-graph-mcp
    timeout: 20
    connect_timeout: 20
    tools:
      include:
        - get_minimal_context_tool
        - query_graph_tool
        - get_impact_radius_tool
        - get_review_context_tool
      prompts: false
      resources: false
```

Add this rule to the review skill:

```text
Use graph output only to select surrounding files or form questions. Verify every
published claim against the exact PR diff or a file at the PR head. Never treat a
graph edge, risk score, or missing test edge as proof of a defect.
```

## Refreshing the trusted graph

A simple protected job is enough:

```bash
cd /opt/data/code-index/eneo
git fetch origin develop
git reset --hard origin/develop
uvx --from 'code-review-graph==2.3.6' code-review-graph update
```

Do not run package installation, repository hooks, tests, build scripts, or
contributor code while refreshing. Keep the mirror and graph disposable.

## When to move beyond this

Consider a more general indexing platform only when Eneo needs multi-repository
semantic search, documentation and issue ingestion, organization-wide lineage,
or long-horizon knowledge workflows. Those are valid uses for CocoIndex or
Cognee, but they add an indexing pipeline, model/embedding choices, storage, and
operational policy that the first PR reviewer does not need.

## Build decision and refinements (Codex-validated, MIN_SCORE 9)

Decision: **graph-later, metrics-gated.** The graph is a later triage-quality
enhancement, added only if review telemetry proves missing structural context is a
recurring problem. It cannot fix coverage honesty, evidence quality, or publish
gating.

Note on the large-PR compaction noise (resolved separately, not via a read budget):
the root cause was a mis-set `compression.threshold`. Hermes's legacy
`codex_gpt55_autoraise: false` compatibility switch (the upstream key name; it does
not select GPT-5.5) had dropped the Codex-route trigger from 85% back to the global
50% (~136K of the 272K window), so even medium reviews compacted mid-run and emitted
noisy
"Compacting context" / "Session compressed" status comments. Fixed by setting
`compression.threshold: 0.80` (~218K). A hard read-budget is therefore NOT needed to
prevent compaction, and genuinely huge PRs are allowed to compact (the desired
behaviour for them). **Triage-then-deep-dive** — risk-rank changed files, deep-read
only the top few, publish only gate-surviving findings, state coverage honestly, and
never lower the >=8/10 + >=0.85 bar — remains worthwhile as a soft review-quality
aid for large PRs, but as guidance, not as a compaction control.

When the graph is added (phase 1.5), additionally:

- **Evidence-provenance gate — the single guardrail that matters most.** Every
  published finding must carry an evidence object referencing only exact PR-head /
  diff hunks or bounded PR-head file reads. The publication gate rejects any finding
  whose evidence source is `graph`, `base_graph`, `impact_radius`, or anything not
  tied to PR head/diff. Make it mechanical, not a prompt rule — it prevents
  "evidence laundering" (the model turning a selector signal into a finding), the
  highest-risk failure mode of adding a graph.
- **Staleness governance.** Version every graph response with `base_sha`,
  `indexed_at`, and `staleness_status`. If the indexed base does not match the PR
  merge base within policy, degrade or disable graph selection for that run.

Telemetry that gates the graph decision (collect first, via `review_runs`): missed
caller/dependent/test-context rate, large-PR budget-exhaustion rate, candidate
rejection reasons, and post-human false-negative samples. Add the graph only when
these show diff + path/ownership/test heuristics are recurrently insufficient for
specific miss classes (changed public APIs, transitive callers, framework
registrations, test-neighbour discovery, renamed/moved symbols).
