# Ops Tracker: Newtonite Software Engineering Challenge

An internal web app for coordinating operational work: customer issues, engineering problems, payments to
investigate, incidents, compliance requests and tasks that need approval.

Every **work item** shows what it is, how important it is, who owns it, what happened to it and what needs to
happen next. Teams see only their own work, leads approve sensitive items, and the system stays correct when
several people act on the same item at once.

**Stack:** Python 3, Flask, SQLite (WAL mode), vanilla JavaScript, pytest.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                                         # open http://127.0.0.1:5000
```

The database (`ops.db`) and demo data are created on first start. Every demo account uses the password **demo1234**:

| Email | Role |
|---|---|
| agent@ops.demo, agent2@ops.demo | Members of Customer Support |
| lead.support@ops.demo | Lead of Customer Support, member of Finance |
| lead.eng@ops.demo | Lead of Engineering |
| dev@ops.demo | Member of Engineering, **viewer** of Customer Support |
| lead.fin@ops.demo | Lead of Finance |
| admin@ops.demo | Admin (sees every team) |

To see the concurrency handling, open two browsers (or a private window) as `agent@` and `agent2@`, and both
click **Take it** on the same item.

## Run the tests

```bash
pytest -v        # 19 tests, about 5 seconds
```

## What it does

- **Dashboard:** counts of work that needs me, unassigned work, urgent work and items waiting for my approval. Each count opens the matching list.
- **Search and filters:** by text, team, status, priority and owner, with "load more" paging. Lists are paged on the server, never loaded whole into the browser.
- **Work item page:** description, priority, status, type, owner, version, comments and a complete history of every change.
- **Workflow:** open → in progress → (awaiting approval → approved) → resolved → closed, with reopening. Payments, compliance work and urgent items need a lead's approval, and nobody can approve their own work.
- **Teams and roles:** a user can be a viewer, member or lead in each team. Admins see all teams.
- **Notifications:** owners, creators and (for approvals and urgent items) team leads are notified asynchronously.

## The critical behaviours (more than simple CRUD)

| Situation | What the system does | Test |
|---|---|---|
| Two people click **Take it** at the same moment | A conditional `UPDATE … WHERE assignee_id IS NULL` lets exactly one win. The other gets a 409 naming the winner, and the page shows the real owner | `test_only_one_of_two_simultaneous_claims_wins` |
| Someone edits an item from an out-of-date screen | Every change must send the version it was based on (`If-Match`). If the item changed meanwhile, the server refuses with 409 and returns the latest version | `test_update_based_on_an_old_version_is_rejected` |
| A request is repeated because the user isn't sure it worked | Create and comment accept an `Idempotency-Key`; a repeat returns the first result instead of creating a duplicate. Repeating your own claim is also harmless | `test_retried_create_…`, `test_retried_comment_…` |
| Workflow rules | Payments and compliance can't be resolved without approval; members can't approve; leads can't approve their own work | `test_payments_need_…`, `test_lead_cannot_approve_own_work` |
| Authorization | Enforced in the API for every item, not by hiding buttons. Items of other teams return 404, viewers can't change anything, only leads reassign or change priority | `test_users_cannot_see_…`, `test_viewer_…` |
| Asynchronous work fails or runs twice | Notifications go through a transactional outbox. Delivery is retried with backoff, parked as `failed` after 5 attempts, and running a row twice can't create duplicate notifications | `test_outbox_…`, `test_failed_delivery_…` |
| Optimistic UI | Claiming shows the item as yours immediately, then reconciles with the server's answer (rolling back if someone else won) | manual: two browsers |

## Project layout

```
app/
  __init__.py   app factory and settings
  db.py         schema, connections, write transactions
  rules.py      workflow transitions and permissions (no Flask or SQL)
  api.py        JSON API: auth, items, claim, update, comments, search, notifications
  worker.py     outbox worker for asynchronous notifications
  seed.py       demo teams, users and work items
static/         index.html and app.js (no build step)
tests/          pytest tests for the risky behaviour
ENGINEERING_DECISIONS.md   the main decisions and trade-offs
```

## Known limitations

- **SQLite** is the right size for a one-day build and a single server, but it allows one writer at a time. For hundreds of concurrent writers I would move to PostgreSQL; the SQL is written to port easily.
- **Text search uses `LIKE`**, which can't use an index for "contains" searches. At tens of thousands of items I'd switch to SQLite FTS5 or PostgreSQL full-text search.
- **Updates reach other users by polling every 10 seconds**, not push. Stale edits are still blocked by versioning, so correctness doesn't depend on the refresh.
- **The outbox worker runs as a thread inside the web process.** With several web servers it should run as a separate process, and claim rows with `SELECT … FOR UPDATE SKIP LOCKED` on PostgreSQL.
- **Notifications stay inside the app.** Sending email or Slack messages would plug into the worker's `deliver` step.
- **No user or team administration screens:** users, teams and roles come from the seed data.
- **Login is a simple email and password session** with no password reset, SSO or rate limiting.
- **Idempotency keys are never cleaned up**; production would expire them after about 24 hours.
