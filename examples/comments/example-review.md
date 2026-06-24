## Eneo AI code & security review

I found one High / P1 and one Medium / P2 finding.

### F1 - High / P1: Tenant context is dropped before the background job
`backend/src/intric/jobs/service.py:142` · security

The new enqueue path passes the document ID but not the verified tenant ID. The
worker later reloads the row by primary key, so the authorization boundary from
the request is no longer present in the asynchronous path.

**Suggested change:** include the trusted tenant ID in the job payload and scope
the worker lookup by both tenant and document ID.

**Verify:** add a regression test where a job created under tenant A cannot load
tenant B's document.

### F2 - Medium / P2: Regression test misses the cross-tenant worker path
`backend/tests/jobs/test_service.py:88` · tests

The added test covers the happy path for a worker loading its own document, but it
would also have passed before the tenant boundary fix because it never creates a
second tenant with a conflicting document ID.

**Suggested change:** add a regression case with tenant A and tenant B documents
and assert that the worker created under tenant A cannot load tenant B's row.

**Verify:** the new test must fail against the unsafe worker lookup and pass only
when the tenant ID is part of the lookup.

<details>
<summary>Copyable fix brief for a coding agent</summary>

```text
Task:
Address all confirmed findings from the Eneo PR review.

Review basis:
PR #123 at commit a1b2c3d.

Before changing code:
Re-check every finding against the current PR head. Skip anything already fixed
and explain why. Do not blindly apply this brief if the code has changed.

Findings:

F1 - High / P1 - security
Location: backend/src/intric/jobs/service.py:142
Problem: Tenant context is dropped before the background job.
Required outcome: The worker lookup remains scoped to the tenant that created
the job.
Suggested approach: Carry tenant_id in the job payload and scope the worker
lookup by tenant_id and document_id.
Verification: Add a regression test proving a tenant A job cannot load tenant B's
document.

F2 - Medium / P2 - tests
Location: backend/tests/jobs/test_service.py:88
Problem: The regression test misses the cross-tenant worker path.
Required outcome: The test fails against the unsafe lookup and passes only when
tenant_id is part of the lookup.
Suggested approach: Add tenant A and tenant B documents with conflicting IDs or
equivalent fixtures, then assert the tenant A job cannot load tenant B's row.
Verification: Run the focused backend tests and strict Pyright for changed
modules.

Constraints:
- Reuse the existing tenant-scoped repository or service.
- Do not add a second authorization path.
- Avoid unrelated refactoring.
- Do not weaken validation or error handling.

Completion:
Run the focused tests, relevant type checks, and formatting checks. Summarize
what changed and identify any finding that was not implemented.
```

</details>

<!--
eneo-review:
head=a1b2c3d4e5f678901234567890abcdef12345678
F1=a1b2c3d4e5f6
F2=b2c3d4e5f6a1
-->
