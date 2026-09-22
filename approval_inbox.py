"""Approval Inbox helpers.

Classifies waiting tasks by outbound-risk keywords and builds the
operator review payload. Approval still goes through mini_orch so
dispatch remains human-gated and AI has no execution authority.
"""

from __future__ import annotations

RISK_RULES = (
    (
        "price",
        (
            "price",
            "pricing",
            "discount",
            "promo",
            "優惠",
            "优惠",
            "價格",
            "价格",
            "價錢",
            "价钱",
        ),
    ),
    (
        "refund",
        (
            "refund",
            "return",
            "退款",
            "退貨",
            "退货",
            "chargeback",
        ),
    ),
    (
        "claim",
        (
            "claim",
            "efficacy",
            "功效",
            "聲稱",
            "声称",
        ),
    ),
    (
        "outbound",
        (
            "publish",
            "email",
            "whatsapp",
            "reply",
            "campaign",
            "ad ",
            "ads",
            "發佈",
            "发布",
            "對外",
            "对外",
            "廣告",
            "广告",
        ),
    ),
)


def classify_risk(task, advisory=None):
    haystack_parts = [
        str(task.get("id") or ""),
        str(task.get("title") or ""),
        " ".join(str(item) for item in task.get("command") or []),
    ]
    if isinstance(advisory, dict):
        haystack_parts.append(str(advisory.get("summary") or ""))
        haystack_parts.append(
            " ".join(str(item) for item in advisory.get("risks") or [])
        )
        haystack_parts.append(str(advisory.get("recommended_action") or ""))

    haystack = " ".join(haystack_parts).lower()
    matched = []
    for risk, keywords in RISK_RULES:
        if any(keyword.lower() in haystack for keyword in keywords):
            matched.append(risk)
    return matched or ["other"]


def is_waiting_approval(task, state):
    if not task.get("requires_approval"):
        return False
    status = (state or {}).get("status")
    approval_status = (state or {}).get("approval_status")
    if status == "waiting_approval":
        return True
    if task.get("requires_approval") and approval_status == "waiting_approval":
        return True
    return False


def inbox_item(task_view, advisory=None):
    task = {
        "id": task_view.get("id"),
        "title": task_view.get("title"),
        "command": task_view.get("command"),
    }
    risks = classify_risk(task, advisory)
    high_risk = any(risk != "other" for risk in risks)
    return {
        **task_view,
        "advisory": advisory,
        "risk_tags": risks,
        "high_risk": high_risk,
        "can_approve": is_waiting_approval(task_view, task_view.get("state")),
    }
