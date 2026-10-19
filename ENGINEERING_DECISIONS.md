# Engineering decisions

## 1. Correctness lives in the database, not in the application's memory

Every check-then-change happens as one conditional SQL statement inside a write transaction (`BEGIN IMMEDIATE`).

- **Claiming** is `UPDATE … SET assignee_id = me WHERE id = ? AND assignee_id IS NULL AND status = 'open'`. If two people click at the same moment, the database lets exactly one row change, and the other request sees `rowcount = 0` and gets a 409 saying who won.
- No locks are held in Python, so this keeps working if more web processes are added.

**Trade-off:** SQLite allows one writer at a time. That is fine for a first version and keeps setup to `pip install`. The same SQL works on PostgreSQL, which is the next step when write traffic grows.

## 2. Optimistic concurrency with versions for every edit

Each work item has a `version` that increases on every change.

- Clients must send the version they last saw (`If-Match`). An `UPDATE … WHERE version = ?` that matches nothing means someone else changed the item first.
- The API then returns 409 **with the latest version**, so the user can review the change and try again.
- Requests without a version get 428, so a client can't accidentally skip the check.

**I chose this over locking items while someone has them open:** people often open an item and walk away, and locks would block everyone else. Conflicts are rare and cheap to resolve.

## 3. History is append-only and written in the same transaction as the change

Every action writes a row to `events` (who, what, from → to) in the same transaction as the change itself. If the change commits, its history exists. If it fails, neither exists. The item page shows this history, so nothing important disappears silently.

**Trade-off:** history grows forever. It is indexed by `(item_id, id)`, so reading one item's history stays fast. Very old events could later be moved to cheaper storage.

## 4. Asynchronous notifications through a transactional outbox

Notifying people is secondary work, so it shouldn't slow down or break the main action.

- The action writes an `outbox` row in the same transaction as the event. A background worker delivers it later.
- **If delivery fails**, the row is retried with exponential backoff (2, 4, 8… seconds) and marked `failed` after 5 attempts, with the error saved.
- **If it runs twice** (for example a crash after delivering but before marking the row done), `UNIQUE (user_id, event_id)` on notifications makes the second run harmless.
- **If it's delayed**, nothing is lost: the work is waiting in the table.

**Trade-off:** the worker is a thread in the web process to keep the one-day setup simple. With several servers it should become its own process.

## 5. Authorization checked on the server for every resource

Roles are per team: **viewer**, **member** or **lead**, plus a global admin. Every endpoint loads the item first and then checks the caller's role in that item's team.

- Items from teams you don't belong to return **404**, not 403, so item ids don't reveal what other teams are working on.
- Workflow permissions are in `rules.py`, separate from Flask and SQL so they're easy to read and test:
  - only the assignee or a lead moves work forward;
  - only leads approve, reassign or change priority;
  - **nobody approves their own work**;
  - payments, compliance and urgent items can't be resolved without approval.

The UI hides buttons you can't use, but that's only convenience; the API is what enforces the rules.

## 6. Idempotency for actions people repeat

Users double-click or press again when the network is slow.

- Create and comment accept an `Idempotency-Key`; the browser generates one per action and reuses it on retry. The server stores the first response and returns it for repeats.
- Two identical requests arriving at the same time are caught by the table's primary key, which rolls back the second.
- Claim is naturally idempotent: claiming something you already own just returns it.

## 7. Built to stay responsive as data grows

- Lists use **keyset pagination** on `(updated_at, id)`, which stays fast on page 500, unlike `OFFSET`.
- Filters and dashboard counts run in SQL with indexes on `(team_id, status, updated_at)`, `(assignee_id, status)` and `(priority, status)`.
- The browser never loads the full dataset.

## What I chose not to build (yet)

- **Live push updates** (WebSockets or server-sent events). Polling every 10 seconds plus version checks keeps data correct; push would mainly improve freshness.
- **Full-text search, SLAs and escalation timers, attachments, and admin screens** for users and teams.

## If the company grew significantly

1. Move to PostgreSQL. Use `SKIP LOCKED` for the outbox and full-text indexes for search.
2. Run the worker separately and push updates to browsers over server-sent events.
3. Add SLA deadlines, so the "needs attention" view can flag work that's about to breach.
4. Add audit export and retention rules for the event history.
