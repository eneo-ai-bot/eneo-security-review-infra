## Eneo AI code & security review

I found one issue worth addressing before merge.

### Tenant context is dropped before the background job
`backend/src/intric/jobs/service.py:142` · security · **High / P1 important**

The new enqueue path passes the document ID but not the verified tenant ID. The
worker later reloads the row by primary key, so the authorization boundary from
the request is no longer present in the asynchronous path.

**Suggested change:** include the trusted tenant ID in the job payload and scope
the worker lookup by both tenant and document ID. Add a regression test where a
job created under tenant A cannot load tenant B's document.

<details>
<summary>Copyable fix brief for a coding agent</summary>

```text
Goal: Preserve the verified tenant boundary across the background job.
Files: backend/src/intric/jobs/service.py and its worker/test modules.
Changes: Carry tenant_id in the job payload and scope the worker lookup by tenant_id + document_id.
Constraints: Reuse the existing tenant-scoped repository/service; do not add a second authorization path.
Verification: Add a regression test proving tenant A cannot load tenant B's document, then run the focused backend tests and strict Pyright for changed modules.
```

</details>

<sub>Eneo two-pass review · finding `a1b2c3d4e5f6`</sub>
