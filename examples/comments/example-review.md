## Eneo AI code & security review

I found one High / P1 and one Medium / P2 finding that survived the evidence gate.

### High / P1 important - Tenant context is dropped before the background job
`backend/src/intric/jobs/service.py:142` · security

The new enqueue path passes the document ID but not the verified tenant ID. The
worker later reloads the row by primary key, so the authorization boundary from
the request is no longer present in the asynchronous path.

**Suggested change:** include the trusted tenant ID in the job payload and scope
the worker lookup by both tenant and document ID. Add a regression test where a
job created under tenant A cannot load tenant B's document.

<details>
<summary>Medium / P2 useful improvement - Regression test misses the cross-tenant worker path - backend/tests/jobs/test_service.py:88</summary>

`backend/tests/jobs/test_service.py:88` · tests

The added test covers the happy path for a worker loading its own document, but it
would also have passed before the tenant boundary fix because it never creates a
second tenant with a conflicting document ID.

**Suggested change:** add a regression case with tenant A and tenant B documents
and assert that the worker created under tenant A cannot load tenant B's row.

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
