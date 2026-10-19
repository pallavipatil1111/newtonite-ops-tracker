"""Tests for the behaviour that would be most harmful if it were wrong."""
import threading

from app.worker import run_once
from conftest import item_id, team_id

AGENT, AGENT2, SUPPORT_LEAD = "agent@ops.demo", "agent2@ops.demo", "lead.support@ops.demo"
DEV, FIN_LEAD, ADMIN = "dev@ops.demo", "lead.fin@ops.demo", "admin@ops.demo"


# ---------------------------------------------------------------- 1. two people claiming the same work
def test_only_one_of_two_simultaneous_claims_wins(app, login, db):
    iid = item_id(db, "OTP not arriving")
    clients = [login(AGENT), login(AGENT2)]
    results = []
    barrier = threading.Barrier(2)

    def claim(c):
        barrier.wait()
        results.append(c.post(f"/api/items/{iid}/claim").status_code)

    threads = [threading.Thread(target=claim, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409]
    assert db.execute("SELECT COUNT(*) FROM events WHERE item_id = ? AND type = 'claimed'", (iid,)).fetchone()[0] == 1


def test_repeating_your_own_claim_is_harmless(login, db):
    iid = item_id(db, "OTP not arriving")
    agent = login(AGENT)
    assert agent.post(f"/api/items/{iid}/claim").status_code == 200
    assert agent.post(f"/api/items/{iid}/claim").status_code == 200  # double click / retry
    assert db.execute("SELECT COUNT(*) FROM events WHERE item_id = ? AND type = 'claimed'", (iid,)).fetchone()[0] == 1


def test_losing_claim_is_told_who_won(login, db):
    iid = item_id(db, "OTP not arriving")
    login(AGENT).post(f"/api/items/{iid}/claim")
    r = login(AGENT2).post(f"/api/items/{iid}/claim")
    assert r.status_code == 409 and "Neha" in r.json["error"]
    assert r.json["current"]["assignee_name"].startswith("Neha")


# ---------------------------------------------------------------- 2. stale information
def test_update_based_on_an_old_version_is_rejected(login, db):
    iid = item_id(db, "Customer charged twice")
    lead = login(SUPPORT_LEAD)
    v = lead.get(f"/api/items/{iid}").json["version"]
    assert lead.patch(f"/api/items/{iid}", json={"priority": "urgent"}, headers={"If-Match": f'"{v}"'}).status_code == 200
    # A second change made from the same (now old) screen must not overwrite the first one.
    r = lead.patch(f"/api/items/{iid}", json={"priority": "low"}, headers={"If-Match": f'"{v}"'})
    assert r.status_code == 409
    assert r.json["current"]["priority"] == "urgent"


def test_updates_without_a_version_are_refused(login, db):
    iid = item_id(db, "Customer charged twice")
    r = login(SUPPORT_LEAD).patch(f"/api/items/{iid}", json={"priority": "low"})
    assert r.status_code == 428


# ---------------------------------------------------------------- 3. duplicate operations
def test_retried_create_with_same_idempotency_key_creates_one_item(login, db):
    agent = login(AGENT)
    body = {"title": "Customer cannot download invoice", "team_id": team_id(db, "Customer Support"),
            "kind": "customer_issue", "priority": "medium"}
    first = agent.post("/api/items", json=body, headers={"Idempotency-Key": "abc-123"})
    second = agent.post("/api/items", json=body, headers={"Idempotency-Key": "abc-123"})
    assert first.status_code == second.status_code == 201
    assert first.json["id"] == second.json["id"]
    assert second.headers.get("Idempotent-Replay") == "true"
    assert db.execute("SELECT COUNT(*) FROM work_items WHERE title = ?", (body["title"],)).fetchone()[0] == 1


def test_retried_comment_is_posted_once(login, db):
    iid = item_id(db, "Refund not received")
    agent = login(AGENT)
    for _ in range(3):
        agent.post(f"/api/items/{iid}/comments", json={"body": "Called the bank"}, headers={"Idempotency-Key": "k1"})
    assert db.execute("SELECT COUNT(*) FROM comments WHERE item_id = ?", (iid,)).fetchone()[0] == 1


# ---------------------------------------------------------------- 4. workflow rules
def _move(client, iid, status):
    v = client.get(f"/api/items/{iid}").json["version"]
    return client.patch(f"/api/items/{iid}", json={"status": status}, headers={"If-Match": f'"{v}"'})


def test_payments_need_a_different_lead_to_approve(login, db):
    iid = item_id(db, "Customer charged twice")  # payment: requires approval
    agent, lead = login(AGENT), login(SUPPORT_LEAD)
    agent.post(f"/api/items/{iid}/claim")
    assert _move(agent, iid, "resolved").status_code == 409          # can't skip approval
    assert _move(agent, iid, "awaiting_approval").status_code == 200
    assert _move(agent, iid, "approved").status_code == 403          # members can't approve
    assert _move(lead, iid, "approved").status_code == 200
    assert _move(agent, iid, "resolved").status_code == 200


def test_lead_cannot_approve_own_work(login, db):
    iid = item_id(db, "Quarterly GST")  # compliance in Finance
    lead = login(FIN_LEAD)
    lead.post(f"/api/items/{iid}/claim")
    assert _move(lead, iid, "awaiting_approval").status_code == 200
    r = _move(lead, iid, "approved")
    assert r.status_code == 403 and "approve an item assigned to you" in r.json["error"]


def test_invalid_jumps_are_rejected(login, db):
    iid = item_id(db, "Refund not received")
    assert _move(login(AGENT), iid, "closed").status_code == 409


# ---------------------------------------------------------------- 5. authorization enforced by the server
def test_users_cannot_see_other_teams_items(login, db):
    iid = item_id(db, "Quarterly GST")  # Finance
    agent = login(AGENT)                # Customer Support only
    assert agent.get(f"/api/items/{iid}").status_code == 404
    titles = [i["title"] for i in agent.get("/api/items").json["items"]]
    assert not any("GST" in t for t in titles)


def test_viewer_cannot_change_anything(login, db):
    iid = item_id(db, "OTP not arriving")  # dev is only a viewer in Customer Support
    dev = login(DEV)
    assert dev.get(f"/api/items/{iid}").status_code == 200
    assert dev.post(f"/api/items/{iid}/claim").status_code == 403
    assert dev.post(f"/api/items/{iid}/comments", json={"body": "hi"}).status_code == 403


def test_members_cannot_reassign_or_change_priority(login, db):
    iid = item_id(db, "Refund not received")
    agent = login(AGENT)
    v = agent.get(f"/api/items/{iid}").json["version"]
    assert agent.patch(f"/api/items/{iid}", json={"priority": "urgent"}, headers={"If-Match": f'"{v}"'}).status_code == 403
    assert agent.patch(f"/api/items/{iid}", json={"assignee_id": None}, headers={"If-Match": f'"{v}"'}).status_code == 403


def test_cannot_assign_to_someone_outside_the_team(login, db):
    iid = item_id(db, "Refund not received")
    lead = login(SUPPORT_LEAD)
    fin_lead_id = db.execute("SELECT id FROM users WHERE email = ?", (FIN_LEAD,)).fetchone()[0]
    v = lead.get(f"/api/items/{iid}").json["version"]
    r = lead.patch(f"/api/items/{iid}", json={"assignee_id": fin_lead_id}, headers={"If-Match": f'"{v}"'})
    assert r.status_code == 400


def test_must_sign_in(app):
    assert app.test_client().get("/api/items").status_code == 401


# ---------------------------------------------------------------- 6. history and asynchronous processing
def test_every_change_is_recorded_in_history(login, db):
    iid = item_id(db, "Customer charged twice")
    lead = login(SUPPORT_LEAD)
    v = lead.get(f"/api/items/{iid}").json["version"]
    lead.patch(f"/api/items/{iid}", json={"priority": "urgent"}, headers={"If-Match": f'"{v}"'})
    events = lead.get(f"/api/items/{iid}/history").json["events"]
    assert events[-1]["detail"]["priority"] == {"from": "high", "to": "urgent"}


def test_outbox_delivers_notifications_once_even_if_run_twice(app, login, db):
    iid = item_id(db, "Refund not received")  # assigned to Neha
    login(SUPPORT_LEAD).post(f"/api/items/{iid}/comments", json={"body": "Any update?"})
    assert run_once(app.config["DATABASE"]) >= 1
    db.execute("UPDATE outbox SET status = 'pending'")  # simulate a crash before the row was marked done
    run_once(app.config["DATABASE"])
    neha = db.execute("SELECT id FROM users WHERE email = ?", (AGENT,)).fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ?", (neha,)).fetchone()[0] == 1


def test_failed_delivery_is_retried_later_then_parked(app, login, db):
    iid = item_id(db, "Refund not received")
    login(SUPPORT_LEAD).post(f"/api/items/{iid}/comments", json={"body": "Ping"})

    def broken(uid, message):
        raise RuntimeError("mail server down")

    assert run_once(app.config["DATABASE"], deliver=broken) == 0
    row = db.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1 and "mail server down" in row["last_error"]
    assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0   # nothing half-delivered
    db.execute("UPDATE outbox SET attempts = 4, available_at = '2000-01-01'")
    run_once(app.config["DATABASE"], deliver=broken)
    assert db.execute("SELECT status FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0] == "failed"


# ---------------------------------------------------------------- 7. search, filtering and pagination
def test_attention_view_and_keyset_pagination(login, db):
    lead = login(SUPPORT_LEAD)
    support = team_id(db, "Customer Support")
    for n in range(30):
        lead.post("/api/items", json={"title": f"Bulk item {n}", "team_id": support, "kind": "task", "priority": "low"})
    page1 = lead.get("/api/items?q=Bulk&limit=20").json
    page2 = lead.get(f"/api/items?q=Bulk&limit=20&cursor={page1['next_cursor']}").json
    ids = [i["id"] for i in page1["items"] + page2["items"]]
    assert len(ids) == 30 and len(set(ids)) == 30 and page2["next_cursor"] is None
    attention = lead.get("/api/items?view=attention").json["items"]
    assert any("OTP" in i["title"] for i in attention)  # unassigned urgent work shows up
