"""JSON API. Every endpoint checks permissions on the server, whatever the UI shows."""
import json
import sqlite3
from functools import wraps

from flask import Blueprint, g, jsonify, request, session
from werkzeug.security import check_password_hash

from .db import get_conn, write_tx
from .rules import KINDS, PRIORITIES, STATUSES, RuleError, can, check_transition, needs_approval

api = Blueprint("api", __name__, url_prefix="/api")

PAGE_SIZE_MAX = 100


# ------------------------------------------------------------------ helpers
def error(message, status):
    return jsonify({"error": message}), status


@api.errorhandler(RuleError)
def rule_error(e):
    return error(e.message, e.status)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return error("Please sign in.", 401)
        g.conn = get_conn()
        try:
            user = g.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not user:
                session.clear()
                return error("Please sign in.", 401)
            g.user = user
            g.roles = {r["team_id"]: r["role"] for r in g.conn.execute(
                "SELECT team_id, role FROM memberships WHERE user_id = ?", (uid,))}
            return view(*args, **kwargs)
        finally:
            g.conn.close()
    return wrapper


def role_in(team_id):
    return g.roles.get(team_id)


def require(team_id, action):
    if not can(role_in(team_id), bool(g.user["is_admin"]), action):
        # 404 for teams you can't even see, so item ids don't leak; 403 otherwise.
        if role_in(team_id) is None and not g.user["is_admin"]:
            raise RuleError("Work item not found.", 404)
        raise RuleError("You don't have permission to do that in this team.", 403)


def item_row(conn, item_id):
    return conn.execute("""
        SELECT w.*, t.name AS team_name, a.name AS assignee_name, c.name AS created_by_name
        FROM work_items w
        JOIN teams t ON t.id = w.team_id
        LEFT JOIN users a ON a.id = w.assignee_id
        JOIN users c ON c.id = w.created_by
        WHERE w.id = ?""", (item_id,)).fetchone()


def load_item(item_id, action="view"):
    row = item_row(g.conn, item_id)
    if not row:
        raise RuleError("Work item not found.", 404)
    require(row["team_id"], action)
    return row


def to_json(row):
    return {k: row[k] for k in row.keys()}


def record(conn, item_id, type_, detail):
    """Append to the item's history and queue it for asynchronous processing, in the caller's transaction."""
    event_id = conn.execute("INSERT INTO events (item_id, actor_id, type, detail) VALUES (?, ?, ?, ?)",
                            (item_id, g.user["id"], type_, json.dumps(detail))).lastrowid
    conn.execute("INSERT INTO outbox (event_id) VALUES (?)", (event_id,))
    return event_id


def expected_version():
    """Clients send the version they last saw (If-Match header or JSON field)."""
    raw = request.headers.get("If-Match") or (request.get_json(silent=True) or {}).get("version")
    try:
        return int(str(raw).strip('"'))
    except (TypeError, ValueError):
        raise RuleError("Send the item's current version (If-Match header) so changes aren't lost.", 428)


def stale_response(item_id):
    current = item_row(g.conn, item_id)
    return jsonify({"error": "Someone else changed this item. Here is the latest version; please review and try again.",
                    "current": to_json(current)}), 409


def idempotent(view):
    """If the same Idempotency-Key is sent again, return the stored result instead of acting twice."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        key = request.headers.get("Idempotency-Key")
        if not key:
            return view(*args, **kwargs)
        uid = g.user["id"]
        prior = g.conn.execute("SELECT status_code, response FROM idempotency_keys WHERE user_id = ? AND key = ?",
                               (uid, key)).fetchone()
        if prior:
            resp = jsonify(json.loads(prior["response"]))
            resp.headers["Idempotent-Replay"] = "true"
            return resp, prior["status_code"]
        g.idempotency_key = key
        return view(*args, **kwargs)
    return wrapper


def remember(conn, body, status):
    key = getattr(g, "idempotency_key", None)
    if key:
        try:
            conn.execute("INSERT INTO idempotency_keys (user_id, key, status_code, response) VALUES (?, ?, ?, ?)",
                         (g.user["id"], key, status, json.dumps(body)))
        except sqlite3.IntegrityError:
            # The same request is being handled right now; roll this copy back.
            raise RuleError("This request was already received. Refresh to see the result.", 409)


# ------------------------------------------------------------------ auth
@api.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    try:
        user = conn.execute("SELECT * FROM users WHERE email = ?", ((data.get("email") or "").lower().strip(),)).fetchone()
    finally:
        conn.close()
    if not user or not check_password_hash(user["password_hash"], data.get("password") or ""):
        return error("Wrong email or password.", 401)
    session.clear()
    session["user_id"] = user["id"]
    return jsonify({"id": user["id"], "name": user["name"]})


@api.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@api.get("/me")
@login_required
def me():
    teams = g.conn.execute("""SELECT t.id, t.name, m.role FROM memberships m JOIN teams t ON t.id = m.team_id
                              WHERE m.user_id = ? ORDER BY t.name""", (g.user["id"],)).fetchall()
    unread = g.conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0",
                            (g.user["id"],)).fetchone()[0]
    return jsonify({"id": g.user["id"], "name": g.user["name"], "email": g.user["email"],
                    "is_admin": bool(g.user["is_admin"]), "teams": [dict(t) for t in teams], "unread": unread})


@api.get("/teams/<int:team_id>/members")
@login_required
def team_members(team_id):
    require(team_id, "view")
    rows = g.conn.execute("""SELECT u.id, u.name, m.role FROM memberships m JOIN users u ON u.id = m.user_id
                             WHERE m.team_id = ? ORDER BY u.name""", (team_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


# ------------------------------------------------------------------ listing and search
@api.get("/items")
@login_required
def list_items():
    """Filtered, paginated list. Keyset pagination on (updated_at, id) stays fast as history grows."""
    args = request.args
    where, params = [], []
    if not g.user["is_admin"]:
        if not g.roles:
            return jsonify({"items": [], "next_cursor": None})
        where.append(f"w.team_id IN ({','.join('?' * len(g.roles))})")
        params += list(g.roles)
    if args.get("team"):
        where.append("w.team_id = ?"); params.append(int(args["team"]))
    if args.get("status"):
        statuses = [s for s in args["status"].split(",") if s in STATUSES]
        where.append(f"w.status IN ({','.join('?' * len(statuses))})"); params += statuses
    else:
        where.append("w.status != 'closed'")
    if args.get("priority"):
        prios = [p for p in args["priority"].split(",") if p in PRIORITIES]
        where.append(f"w.priority IN ({','.join('?' * len(prios))})"); params += prios
    if args.get("assignee") == "me":
        where.append("w.assignee_id = ?"); params.append(g.user["id"])
    elif args.get("assignee") == "none":
        where.append("w.assignee_id IS NULL")
    if args.get("q"):
        where.append("(w.title LIKE ? OR w.description LIKE ?)")
        params += [f"%{args['q']}%"] * 2
    if args.get("view") == "attention":
        lead_teams = [t for t, r in g.roles.items() if r == "lead"] or [-1]
        where.append(f"""(
            (w.assignee_id = ? AND w.status IN ('open','in_progress','approved'))
            OR (w.assignee_id IS NULL AND w.priority IN ('high','urgent') AND w.status = 'open')
            OR (w.status = 'awaiting_approval' AND w.team_id IN ({','.join('?' * len(lead_teams))}))
        )""")
        params += [g.user["id"], *lead_teams]
    if args.get("cursor"):
        updated_at, last_id = args["cursor"].rsplit("|", 1)
        where.append("(w.updated_at < ? OR (w.updated_at = ? AND w.id < ?))")
        params += [updated_at, updated_at, int(last_id)]
    limit = max(1, min(int(args.get("limit", 25)), PAGE_SIZE_MAX))
    sql = f"""SELECT w.id, w.title, w.kind, w.priority, w.status, w.team_id, t.name AS team_name,
                     w.assignee_id, a.name AS assignee_name, w.version, w.updated_at
              FROM work_items w JOIN teams t ON t.id = w.team_id LEFT JOIN users a ON a.id = w.assignee_id
              {'WHERE ' + ' AND '.join(where) if where else ''}
              ORDER BY w.updated_at DESC, w.id DESC LIMIT ?"""
    rows = g.conn.execute(sql, params + [limit + 1]).fetchall()
    items = [dict(r) for r in rows[:limit]]
    next_cursor = f"{items[-1]['updated_at']}|{items[-1]['id']}" if len(rows) > limit else None
    return jsonify({"items": items, "next_cursor": next_cursor})


@api.get("/summary")
@login_required
def summary():
    """Counts for the dashboard, computed in SQL rather than in the browser."""
    team_ids = list(g.roles) if not g.user["is_admin"] else [r[0] for r in g.conn.execute("SELECT id FROM teams")]
    if not team_ids:
        return jsonify({})
    ph = ",".join("?" * len(team_ids))
    lead_teams = [t for t, r in g.roles.items() if r == "lead"] or [-1]
    row = g.conn.execute(f"""SELECT
        SUM(assignee_id = ? AND status IN ('open','in_progress','approved')) AS mine,
        SUM(assignee_id IS NULL AND status = 'open') AS unassigned,
        SUM(priority = 'urgent' AND status NOT IN ('resolved','closed')) AS urgent,
        SUM(status = 'awaiting_approval' AND team_id IN ({','.join('?' * len(lead_teams))})) AS to_approve
        FROM work_items WHERE team_id IN ({ph})""", [g.user["id"], *lead_teams, *team_ids]).fetchone()
    return jsonify({k: row[k] or 0 for k in row.keys()})


# ------------------------------------------------------------------ items
@api.post("/items")
@login_required
@idempotent
def create_item():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    kind, priority = data.get("kind"), data.get("priority", "medium")
    try:
        team_id = int(data.get("team_id"))
    except (TypeError, ValueError):
        raise RuleError("Choose a team.")
    if not title or len(title) > 200:
        raise RuleError("A title of 1 to 200 characters is required.")
    if kind not in KINDS or priority not in PRIORITIES:
        raise RuleError("Choose a valid type and priority.")
    require(team_id, "create")
    with write_tx(g.conn) as conn:
        item_id = conn.execute("""INSERT INTO work_items (team_id, title, description, kind, priority, status,
                                  requires_approval, created_by) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
                               (team_id, title, (data.get("description") or "").strip(), kind, priority,
                                int(needs_approval(kind, priority)), g.user["id"])).lastrowid
        record(conn, item_id, "created", {"title": title, "priority": priority, "kind": kind})
        body = to_json(item_row(conn, item_id))
        remember(conn, body, 201)
    return jsonify(body), 201


@api.get("/items/<int:item_id>")
@login_required
def get_item(item_id):
    row = load_item(item_id)
    resp = jsonify(to_json(row))
    resp.headers["ETag"] = f'"{row["version"]}"'
    return resp


@api.get("/items/<int:item_id>/history")
@login_required
def history(item_id):
    load_item(item_id)
    events = g.conn.execute("""SELECT e.id, e.type, e.detail, e.created_at, u.name AS actor
                               FROM events e JOIN users u ON u.id = e.actor_id WHERE e.item_id = ? ORDER BY e.id""",
                            (item_id,)).fetchall()
    comments = g.conn.execute("""SELECT c.id, c.body, c.created_at, u.name AS author
                                 FROM comments c JOIN users u ON u.id = c.author_id WHERE c.item_id = ? ORDER BY c.id""",
                              (item_id,)).fetchall()
    return jsonify({"events": [dict(e, detail=json.loads(e["detail"])) for e in events],
                    "comments": [dict(c) for c in comments]})


@api.post("/items/<int:item_id>/claim")
@login_required
def claim(item_id):
    """Take ownership. The conditional UPDATE guarantees only one of two simultaneous claims wins."""
    load_item(item_id, "claim")
    with write_tx(g.conn) as conn:
        changed = conn.execute("""UPDATE work_items SET assignee_id = ?, status = 'in_progress', version = version + 1,
                                  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                  WHERE id = ? AND assignee_id IS NULL AND status = 'open'""",
                               (g.user["id"], item_id)).rowcount
        if not changed:
            current = item_row(conn, item_id)
            if current["assignee_id"] == g.user["id"]:
                return jsonify(to_json(current))  # already yours: repeating the click is harmless
            owner = current["assignee_name"] or "someone"
            return jsonify({"error": f"{owner} already took this item.", "current": to_json(current)}), 409
        record(conn, item_id, "claimed", {"assignee": g.user["name"]})
        body = to_json(item_row(conn, item_id))
    return jsonify(body)


@api.patch("/items/<int:item_id>")
@login_required
def update_item(item_id):
    """Edit fields or move through the workflow, only if nobody changed the item since the client loaded it."""
    item = load_item(item_id, "edit")
    version = expected_version()
    data = request.get_json(silent=True) or {}
    role, is_admin = role_in(item["team_id"]), bool(g.user["is_admin"])
    sets, params, changes = [], [], {}

    if "title" in data or "description" in data:
        if not (is_admin or role == "lead" or item["assignee_id"] in (None, g.user["id"])):
            raise RuleError("Only the assignee or a team lead can edit this item.", 403)
        for field in ("title", "description"):
            if field in data:
                value = (data[field] or "").strip()
                if field == "title" and not 1 <= len(value) <= 200:
                    raise RuleError("A title of 1 to 200 characters is required.")
                sets.append(f"{field} = ?"); params.append(value); changes[field] = value
    if "priority" in data and data["priority"] != item["priority"]:
        if data["priority"] not in PRIORITIES:
            raise RuleError("Unknown priority.")
        if not can(role, is_admin, "change_priority"):
            raise RuleError("Only a team lead can change priority.", 403)
        sets += ["priority = ?", "requires_approval = ?"]
        params += [data["priority"], int(needs_approval(item["kind"], data["priority"]) or item["requires_approval"])]
        changes["priority"] = {"from": item["priority"], "to": data["priority"]}
    if "assignee_id" in data and data["assignee_id"] != item["assignee_id"]:
        if not can(role, is_admin, "reassign"):
            raise RuleError("Only a team lead can reassign work.", 403)
        new_owner = data["assignee_id"]
        if new_owner is not None and not g.conn.execute(
                "SELECT 1 FROM memberships WHERE user_id = ? AND team_id = ? AND role != 'viewer'",
                (new_owner, item["team_id"])).fetchone():
            raise RuleError("Work can only be assigned to a member of this team.")
        sets.append("assignee_id = ?"); params.append(new_owner)
        changes["assignee"] = {"from": item["assignee_name"], "to": new_owner}
    if "status" in data and data["status"] != item["status"]:
        check_transition(item, data["status"], g.user["id"], role, is_admin)
        sets.append("status = ?"); params.append(data["status"])
        changes["status"] = {"from": item["status"], "to": data["status"]}
    if not sets:
        return jsonify(to_json(item))

    with write_tx(g.conn) as conn:
        changed = conn.execute(f"""UPDATE work_items SET {', '.join(sets)}, version = version + 1,
                                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                   WHERE id = ? AND version = ?""", params + [item_id, version]).rowcount
        if not changed:
            return stale_response(item_id)
        if "assignee" in changes and changes["assignee"]["to"] is not None:
            changes["assignee"]["to"] = conn.execute("SELECT name FROM users WHERE id = ?",
                                                     (changes["assignee"]["to"],)).fetchone()[0]
        record(conn, item_id, "updated", changes)
        body = to_json(item_row(conn, item_id))
    resp = jsonify(body)
    resp.headers["ETag"] = f'"{body["version"]}"'
    return resp


@api.post("/items/<int:item_id>/comments")
@login_required
@idempotent
def add_comment(item_id):
    load_item(item_id, "comment")
    text = ((request.get_json(silent=True) or {}).get("body") or "").strip()
    if not 1 <= len(text) <= 5000:
        raise RuleError("A comment of 1 to 5000 characters is required.")
    with write_tx(g.conn) as conn:
        comment_id = conn.execute("INSERT INTO comments (item_id, author_id, body) VALUES (?, ?, ?)",
                                  (item_id, g.user["id"], text)).lastrowid
        conn.execute("UPDATE work_items SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (item_id,))
        record(conn, item_id, "commented", {"comment_id": comment_id})
        body = {"id": comment_id, "body": text, "author": g.user["name"]}
        remember(conn, body, 201)
    return jsonify(body), 201


# ------------------------------------------------------------------ notifications
@api.get("/notifications")
@login_required
def notifications():
    rows = g.conn.execute("""SELECT n.id, n.message, n.read, n.created_at, e.item_id FROM notifications n
                             JOIN events e ON e.id = n.event_id WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 30""",
                          (g.user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@api.post("/notifications/read")
@login_required
def mark_read():
    with write_tx(g.conn) as conn:
        conn.execute("UPDATE notifications SET read = 1 WHERE user_id = ?", (g.user["id"],))
    return jsonify({"ok": True})
