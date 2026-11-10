"""LOOM access policy: who may receive which tier of a person's health
context, for how long, and with how much friction.

Rules (product concept v3):

* Depth is set by the task, never by credit. Each known task has a
  deepest tier it needs (L0 raw signal ... L3 constraints), the fields
  it may receive, and a natural validity window in days.
* Commercial requesters with no task basis are denied by default; that
  is not misconduct, so credit is unchanged.
* Asking for a deeper tier or a field the task does not need is
  overreach: the request is blocked and the publisher loses credit.
* Fetching under an expired grant is denied and costs a little credit.
* Credit belongs to the publisher (company or developer), not to one
  agent instance, and only changes friction: validity length, renewal,
  and how often the person must confirm.
* Retrying the same request_id with identical content returns the
  original decision without touching credit again. Reusing a request_id
  for different content is refused as a conflict.

Time is passed in by the caller in days, so a simulation with logical
time gets deterministic decisions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

POLICY_VERSION = "loom-policy-0.1.0"

TIERS = ["L0", "L1", "L2", "L3"]          # L0 deepest, L3 shallowest
TIER_FIELDS = {
    "L0": {"raw_signal"},
    "L1": {"readings"},
    "L2": {"glucose_patterns", "alerts"},
    "L3": {"constraints"},
}

# task -> deepest tier, fields allowed, natural validity (days, 0 = once)
TASKS: dict[str, dict[str, Any]] = {
    "shopping_list": {"tier": "L3", "fields": {"constraints"},
                      "days": 0, "categories": {"agent_for_user"}},
    "meal_plan": {"tier": "L2", "fields": {"glucose_patterns", "constraints"},
                  "days": 7, "categories": {"agent_for_user"}},
    "clinical_visit": {"tier": "L2",
                       "fields": {"glucose_patterns", "alerts", "constraints"},
                       "days": 14, "categories": {"care_provider"}},
}

BASELINE_CREDIT = 60
PENALTY_OVERREACH = 20
PENALTY_EXPIRED = 5
REWARD_CLEAN_GRANT = 1


def depth(tier: str) -> int:
    """Smaller is deeper: L0 -> 0, L3 -> 3."""
    return TIERS.index(tier)


def friction(credit: int) -> dict[str, Any]:
    if credit >= 80:
        return {"band": "high", "auto_renew": True,
                "confirmation": "beyond_rules", "validity_factor": 1.0}
    if credit >= 50:
        return {"band": "medium", "auto_renew": False,
                "confirmation": "first_request", "validity_factor": 1.0}
    return {"band": "low", "auto_renew": False,
            "confirmation": "every_time", "validity_factor": 0.0}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True,
                                     separators=(",", ":")).encode()
                          ).hexdigest()[:16]


@dataclass
class Grant:
    grant_id: str
    agent_id: str
    publisher: str
    task: str
    tier: str
    fields: list[str]
    issued_at: float
    expires_at: float
    single_use: bool
    used: int = 0


@dataclass
class Portal:
    """One namespace of portal state (one person's record, one run)."""

    record: dict[str, Any]
    credit: dict[str, int] = field(default_factory=dict)
    grants: dict[str, Grant] = field(default_factory=dict)
    seen: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    # -- helpers ------------------------------------------------------

    def credit_of(self, publisher: str) -> int:
        return self.credit.get(publisher, BASELINE_CREDIT)

    def _adjust(self, publisher: str, delta: int) -> tuple[int, int]:
        before = self.credit_of(publisher)
        after = max(0, min(100, before + delta))
        self.credit[publisher] = after
        return before, after

    def view(self, tier: str, fields: list[str]) -> dict[str, Any]:
        """The record compressed to a tier, limited to fields."""
        views = {
            "constraints": self.record["constraints"],
            "glucose_patterns": self.record["patterns"],
            "alerts": self.record["alerts"],
            "readings": self.record["readings"],
            "raw_signal": self.record.get("raw_signal", []),
        }
        allowed = set().union(*(TIER_FIELDS[t] for t in TIERS
                                if depth(t) >= depth(tier)))
        return {f: views[f] for f in fields if f in allowed}

    def _decide(self, op: str, key: str, body: dict[str, Any],
                fn) -> dict[str, Any]:
        """Idempotency wrapper around one state-changing decision.

        ``at`` is when the portal (or its gateway) received the request,
        not part of what was asked, so a retried copy that arrives later
        is still the same request.
        """
        content = _digest({k: v for k, v in body.items() if k != "at"})
        if key in self.seen:
            prev_content, prev = self.seen[key]
            if prev_content != content:
                return {"decision": "conflict", "request_id": key,
                        "reason": "request_id reused with different content",
                        "replay": False, "policy": POLICY_VERSION}
            return dict(prev, replay=True)
        out = fn()
        out.update(request_id=key, replay=False, policy=POLICY_VERSION,
                   op=op)
        self.seen[key] = (content, out)
        self.log.append(dict(out))
        return out

    # -- operations ---------------------------------------------------

    def request(self, req: dict[str, Any]) -> dict[str, Any]:
        """A new access request.

        req: request_id, agent_id, publisher, category, task, purpose,
        tier, fields, at (days).
        """
        def decide():
            pub = req["publisher"]
            at = float(req["at"])
            task = TASKS.get(req.get("task", ""))
            asked_tier = req.get("tier", "L3")
            asked_fields = list(req.get("fields", []))
            base = {"agent_id": req["agent_id"], "publisher": pub,
                    "task": req.get("task"), "tier_requested": asked_tier,
                    "fields_requested": asked_fields}
            credit = self.credit_of(pub)
            if asked_tier not in TIERS:
                return dict(base, decision="denied", reason="unknown_tier",
                            credit_before=credit, credit_after=credit)
            if task is None or req.get("category") not in task["categories"]:
                # No task basis (e.g. marketing): default deny, no penalty.
                return dict(base, decision="denied",
                            reason="no_task_basis",
                            credit_before=credit, credit_after=credit)
            too_deep = depth(asked_tier) < depth(task["tier"])
            extra = sorted(set(asked_fields) - task["fields"])
            if too_deep or extra:
                before, after = self._adjust(pub, -PENALTY_OVERREACH)
                return dict(base, decision="blocked", reason="overreach",
                            needed_tier=task["tier"], extra_fields=extra,
                            credit_before=before, credit_after=after)
            fr = friction(credit)
            days = task["days"] * fr["validity_factor"]
            grant_fields = [f for f in asked_fields if f in task["fields"]]
            gid = "g-" + _digest([req["request_id"], pub, at])[:10]
            grant = Grant(gid, req["agent_id"], pub, req["task"],
                          task["tier"], grant_fields, at, at + days,
                          single_use=(days == 0), used=1)
            self.grants[gid] = grant
            before, after = self._adjust(pub, REWARD_CLEAN_GRANT)
            return dict(base, decision="granted", reason="task_basis",
                        grant_id=gid, tier_granted=grant.tier,
                        fields_granted=grant_fields,
                        valid_days=days, expires_at=grant.expires_at,
                        single_use=grant.single_use,
                        friction=fr, credit_before=before,
                        credit_after=after,
                        data=self.view(grant.tier, grant_fields))
        return self._decide("request", req["request_id"], req, decide)

    def fetch(self, req: dict[str, Any]) -> dict[str, Any]:
        """Fresh data under an existing grant.

        req: request_id, grant_id, agent_id, at (days).
        """
        def decide():
            at = float(req["at"])
            g = self.grants.get(req.get("grant_id", ""))
            if g is None or g.agent_id != req["agent_id"]:
                return {"decision": "denied", "reason": "unknown_grant",
                        "agent_id": req["agent_id"],
                        "grant_id": req.get("grant_id")}
            base = {"agent_id": g.agent_id, "publisher": g.publisher,
                    "task": g.task, "grant_id": g.grant_id}
            expired = (at > g.expires_at
                       or (g.single_use and g.used >= 1))
            if expired:
                before, after = self._adjust(g.publisher, -PENALTY_EXPIRED)
                return dict(base, decision="denied", reason="expired",
                            expires_at=g.expires_at,
                            credit_before=before, credit_after=after)
            g.used += 1
            c = self.credit_of(g.publisher)
            return dict(base, decision="granted", reason="within_validity",
                        tier_granted=g.tier, fields_granted=g.fields,
                        expires_at=g.expires_at,
                        credit_before=c, credit_after=c,
                        data=self.view(g.tier, g.fields))
        return self._decide("fetch", req["request_id"], req, decide)
