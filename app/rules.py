"""Workflow and permission rules, kept free of Flask and SQL so they are easy to read and test."""

STATUSES = ("open", "in_progress", "awaiting_approval", "approved", "resolved", "closed")
PRIORITIES = ("low", "medium", "high", "urgent")
KINDS = ("customer_issue", "engineering", "payment", "incident", "compliance", "task")
ROLE_RANK = {"viewer": 1, "member": 2, "lead": 3}

# Allowed status moves. "who" is the minimum relationship needed:
#   assignee - the person who owns the item (leads may act for them)
#   lead     - a lead of the item's team
TRANSITIONS = {
    ("open", "in_progress"): "assignee",
    ("in_progress", "awaiting_approval"): "assignee",
    ("in_progress", "resolved"): "assignee",          # only when no approval is required
    ("awaiting_approval", "approved"): "lead",
    ("awaiting_approval", "in_progress"): "lead",      # approval rejected, back to work
    ("approved", "resolved"): "assignee",
    ("resolved", "closed"): "lead",
    ("resolved", "in_progress"): "assignee",           # reopened before closing
    ("closed", "open"): "lead",                        # reopened after closing
}

# Payments and compliance work always need a lead's sign-off before they can be resolved.
KINDS_NEEDING_APPROVAL = {"payment", "compliance"}


class RuleError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def needs_approval(kind, priority):
    return kind in KINDS_NEEDING_APPROVAL or priority == "urgent"


def check_transition(item, new_status, user_id, role, is_admin):
    """Raise RuleError unless this user may move the item to new_status."""
    current = item["status"]
    if new_status not in STATUSES:
        raise RuleError(f"Unknown status '{new_status}'.")
    who = TRANSITIONS.get((current, new_status))
    if who is None:
        raise RuleError(f"An item that is '{current}' cannot move to '{new_status}'.", 409)
    if current == "in_progress" and new_status == "resolved" and item["requires_approval"]:
        raise RuleError("This item needs a lead's approval before it can be resolved.", 409)
    if new_status == "in_progress" and current == "open" and item["assignee_id"] is None:
        raise RuleError("Claim the item before starting work on it.", 409)
    if is_admin:
        return
    is_lead = role == "lead"
    if who == "lead" and not is_lead:
        raise RuleError("Only a lead of this team can do that.", 403)
    if who == "assignee" and not (is_lead or item["assignee_id"] == user_id):
        raise RuleError("Only the assignee or a team lead can do that.", 403)
    # Separation of duties: nobody approves their own work.
    if new_status == "approved" and item["assignee_id"] == user_id:
        raise RuleError("You can't approve an item assigned to you. Ask another lead.", 403)


def can(role, is_admin, action):
    """Team-level permissions. Resource-level checks (assignee, approvals) live in check_transition."""
    if is_admin:
        return True
    needed = {
        "view": "viewer",
        "comment": "member",
        "create": "member",
        "claim": "member",
        "edit": "member",
        "reassign": "lead",
        "change_priority": "lead",
    }[action]
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[needed]
