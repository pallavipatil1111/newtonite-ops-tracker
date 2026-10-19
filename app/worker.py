"""Background delivery of notifications from the outbox.

The primary action (create, claim, update) commits its event and an outbox row in one transaction.
This worker turns those rows into notifications afterwards. It is safe to run twice on the same row
(notifications are UNIQUE per user and event), and failures are retried with backoff, then parked as 'failed'.
"""
import json
import threading
import time

from .db import connect, write_tx

MAX_ATTEMPTS = 5


def recipients(conn, event):
    """Who should hear about this event: the assignee, the creator, and team leads for approvals."""
    item = conn.execute("SELECT * FROM work_items WHERE id = ?", (event["item_id"],)).fetchone()
    people = {item["assignee_id"], item["created_by"]}
    detail = json.loads(event["detail"])
    status_to = (detail.get("status") or {}).get("to") if isinstance(detail.get("status"), dict) else None
    if status_to == "awaiting_approval" or (event["type"] == "created" and item["priority"] == "urgent"):
        people |= {r[0] for r in conn.execute(
            "SELECT user_id FROM memberships WHERE team_id = ? AND role = 'lead'", (item["team_id"],))}
    people.discard(None)
    people.discard(event["actor_id"])  # don't notify people about their own actions
    return item, people


def describe(event, item, actor):
    t = event["type"]
    if t == "created":
        return f'{actor} created "{item["title"]}"'
    if t == "claimed":
        return f'{actor} took "{item["title"]}"'
    if t == "commented":
        return f'{actor} commented on "{item["title"]}"'
    return f'{actor} updated "{item["title"]}"'


def process_one(conn, row, deliver=None):
    """Deliver one outbox row. `deliver` lets tests inject failures."""
    event = conn.execute("SELECT e.*, u.name AS actor FROM events e JOIN users u ON u.id = e.actor_id WHERE e.id = ?",
                         (row["event_id"],)).fetchone()
    item, people = recipients(conn, event)
    message = describe(event, item, event["actor"])
    with write_tx(conn):
        for uid in people:
            if deliver:
                deliver(uid, message)
            conn.execute("INSERT OR IGNORE INTO notifications (user_id, event_id, message) VALUES (?, ?, ?)",
                         (uid, event["id"], message))
        conn.execute("UPDATE outbox SET status = 'done', attempts = attempts + 1 WHERE id = ?", (row["id"],))


def run_once(path, deliver=None, batch=50):
    """Process due outbox rows once. Returns how many were handled successfully."""
    conn = connect(path)
    done = 0
    try:
        rows = conn.execute("""SELECT * FROM outbox WHERE status = 'pending'
                               AND available_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                               ORDER BY id LIMIT ?""", (batch,)).fetchall()
        for row in rows:
            try:
                process_one(conn, row, deliver)
                done += 1
            except Exception as e:  # noqa: BLE001 - any failure is recorded and retried
                attempts = row["attempts"] + 1
                with write_tx(conn):
                    conn.execute("""UPDATE outbox SET attempts = ?, last_error = ?,
                                    status = CASE WHEN ? >= ? THEN 'failed' ELSE 'pending' END,
                                    available_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
                                    WHERE id = ?""",
                                 (attempts, str(e)[:500], attempts, MAX_ATTEMPTS, f"+{2 ** attempts} seconds", row["id"]))
    finally:
        conn.close()
    return done


def start(path, interval=1.0):
    def loop():
        while True:
            try:
                run_once(path)
            except Exception:  # noqa: BLE001 - keep the worker alive
                pass
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="outbox-worker").start()
