## Eneo AI code & security review

There are 2 current findings: 1 High (P1) and 1 Medium (P2).

### F1 · High (P1): Tenant context is dropped before the background job
`backend/src/intric/jobs/service.py:142` · security

The new enqueue path passes the document ID but not the verified tenant ID. The
worker later reloads the row by primary key, so the authorization boundary from
the request is no longer present in the asynchronous path.

**Impact:** a tenant A job can load tenant B's document when IDs collide.

**Suggested change:** include the trusted tenant ID in the job payload and scope
the worker lookup by both tenant and document ID.

**Reviewer checks:** confirmed the enqueue path has document ID only, and the
worker lookup does not re-bind the tenant context.

### F2 · Medium (P2): Regression test misses the cross-tenant worker path
`backend/tests/jobs/test_service.py:88` · tests

The added test covers the happy path for a worker loading its own document, but it
would also have passed before the tenant boundary fix because it never creates a
second tenant with a conflicting document ID.

**Impact:** the tenant-boundary regression can return without a failing test.

**Suggested change:** add a regression case with tenant A and tenant B documents
and assert that the worker created under tenant A cannot load tenant B's row.

**Reviewer checks:** compared the new test against the unsafe lookup path; it
does not exercise the cross-tenant case.

<details>
<summary>Copyable fix brief for a coding agent</summary>

```text
Task:
Review and address all current findings from the Eneo PR review.

Review basis:
PR #123 at commit a1b2c3d.

Before changing code:
Re-check every finding against the current PR head. Skip anything already fixed
and explain why. Do not blindly apply this brief if the code has changed.

Findings:

F1 - High (P1) - security
Location: backend/src/intric/jobs/service.py:142
Problem: Tenant context is dropped before the background job.
Impact: The worker lookup can run outside the tenant that created the job.
Suggested approach: Carry tenant_id in the job payload and scope the worker
lookup by tenant_id and document_id.
Reviewer checks: Confirmed the enqueue path has document ID only, and the worker
lookup does not re-bind the tenant context.

F2 - Medium (P2) - tests
Location: backend/tests/jobs/test_service.py:88
Problem: The regression test misses the cross-tenant worker path.
Impact: The tenant-boundary regression can return without a failing test.
Suggested approach: Add tenant A and tenant B documents with conflicting IDs or
equivalent fixtures, then assert the tenant A job cannot load tenant B's row.
Reviewer checks: Compared the new test against the unsafe lookup path; it does
not exercise the cross-tenant case.

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

<details>
<summary>Give feedback on this review</summary>

Post one command as a new top-level PR comment after replacing the text in angle brackets.
Use the F reference from the relevant finding heading. The bot reacts 👍 when
feedback is recorded.

It does not need to be a reply to the bot comment. Do not edit an old feedback
command after posting it.

**The finding is incorrect**

```text
/review false-positive F1 because <what code, guard, or invariant disproves it>
```

**The review missed an important issue**

```text
/review feedback missed because <what concrete issue was missed and where>
```

</details>

<!--
eneo-review:
head=a1b2c3d4e5f678901234567890abcdef12345678
F1=a1b2c3d4e5f6
F2=b2c3d4e5f6a1
-->
