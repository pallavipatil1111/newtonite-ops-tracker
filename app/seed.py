"""Demo data: three teams, users with different roles, and some work items. Runs only on an empty database."""
import json

from werkzeug.security import generate_password_hash

from .db import connect, write_tx
from .rules import needs_approval

PASSWORD = "demo1234"
USERS = [  # email, name, admin, {team: role}
    ("admin@ops.demo", "Asha Admin", 1, {}),
    ("lead.support@ops.demo", "Ravi (Support lead)", 0, {"Customer Support": "lead", "Finance": "member"}),
    ("agent@ops.demo", "Neha (Support agent)", 0, {"Customer Support": "member"}),
    ("agent2@ops.demo", "Karan (Support agent)", 0, {"Customer Support": "member"}),
    ("lead.eng@ops.demo", "Meera (Engineering lead)", 0, {"Engineering": "lead"}),
    ("dev@ops.demo", "Arjun (Engineer)", 0, {"Engineering": "member", "Customer Support": "viewer"}),
    ("lead.fin@ops.demo", "Sana (Finance lead)", 0, {"Finance": "lead"}),
]
ITEMS = [  # team, title, kind, priority, assignee email or None
    ("Customer Support", "Customer charged twice for order #48213", "payment", "high", None),
    ("Customer Support", "Refund not received after 10 days", "customer_issue", "medium", "agent@ops.demo"),
    ("Customer Support", "Login OTP not arriving for Airtel users", "customer_issue", "urgent", None),
    ("Engineering", "Checkout API p95 latency above 2 s", "incident", "urgent", "dev@ops.demo"),
    ("Engineering", "Upgrade payment SDK before deprecation", "engineering", "medium", None),
    ("Finance", "Quarterly GST reconciliation", "compliance", "high", None),
    ("Finance", "Vendor invoice mismatch: Acme Logistics", "payment", "low", None),
]


def seed(path):
    conn = connect(path)
    try:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        with write_tx(conn):
            teams = {}
            for name in ("Customer Support", "Engineering", "Finance"):
                teams[name] = conn.execute("INSERT INTO teams (name) VALUES (?)", (name,)).lastrowid
            users = {}
            pw = generate_password_hash(PASSWORD)
            for email, name, admin, roles in USERS:
                uid = conn.execute("INSERT INTO users (email, name, password_hash, is_admin) VALUES (?, ?, ?, ?)",
                                   (email, name, pw, admin)).lastrowid
                users[email] = uid
                for team, role in roles.items():
                    conn.execute("INSERT INTO memberships VALUES (?, ?, ?)", (uid, teams[team], role))
            creator = users["lead.support@ops.demo"]
            for team, title, kind, prio, assignee in ITEMS:
                aid = users.get(assignee) if assignee else None
                item_id = conn.execute("""INSERT INTO work_items (team_id, title, kind, priority, status, requires_approval,
                                          assignee_id, created_by, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                       (teams[team], title, kind, prio, "in_progress" if aid else "open",
                                        int(needs_approval(kind, prio)), aid, creator,
                                        "Created from the demo data.")).lastrowid
                conn.execute("INSERT INTO events (item_id, actor_id, type, detail) VALUES (?, ?, 'created', ?)",
                             (item_id, creator, json.dumps({"title": title})))
    finally:
        conn.close()
