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
