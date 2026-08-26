"""Proactive issue detection (Client Problem 1).

A reactive chatbot only helps once somebody asks. This module runs a set of
detectors across all support activity and produces a ranked board of what
deserves attention right now.

Every detector is deterministic and explainable. That is a product decision, not
a shortcut: an operations board that reshuffles between refreshes because a
model sampled differently is a board nobody trusts. Each signal carries the
evidence that produced it, so an agent can check the reasoning rather than take
it on faith, and each carries a recommended action that routes through the same
human-confirmation gate as everything else.

Detectors
  1. SLA radar          -- open tickets breaching or approaching their target,
                           measured on the correct clock and the correct
                           (contract or policy) target.
  2. Complaint clusters -- groups of similar tickets, with a rising-rate test
                           against the preceding baseline.
  3. Known-issue links  -- clusters mapped to documented known issues.
  4. Cross-account      -- one problem hitting several customers at once.
  5. Order anomalies    -- carrier fault rates, pickup overruns, cancellations.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from app.core import clock
from app.core.models import Severity, Ticket
from app.core.principal import Principal
from app.data.repository import Repository
from app.ingest.corpus import all_chunks
from app.policy import engine
from app.policy.triage import triage
from app.retrieval.index import tokenize

SEV_WEIGHT = {Severity.P1: 3.0, Severity.P2: 2.0, Severity.P3: 1.0}

# Tuned against the pack: high enough that unrelated how-to questions stay
# apart, low enough that the same complaint phrased differently still groups.
_CLUSTER_THRESHOLD = 0.33


@dataclass
class Signal:
    signal_id: str
    kind: str
    title: str
    why_it_matters: str
    score: float
    severity: str                      # critical | high | medium | low
    accounts: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    recommended_action: dict | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id, "kind": self.kind, "title": self.title,
            "why_it_matters": self.why_it_matters, "score": round(self.score, 2),
            "severity": self.severity, "accounts": self.accounts,
            "evidence": self.evidence, "recommended_action": self.recommended_action,
            "sources": self.sources,
        }


@dataclass
class SignalBoard:
    generated_at: str
    signals: list[Signal] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"generated_at": self.generated_at,
                "reference_time_note": (
                    "All timings are measured against the dataset snapshot, not "
                    "real wall-clock time."),
                "stats": self.stats,
                "signals": [s.to_dict() for s in self.signals]}


def _band(score: float) -> str:
    if score >= 8:
        return "critical"
    if score >= 5:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


# --------------------------------------------------------------------------
# 1. SLA radar
# --------------------------------------------------------------------------

def _sla_signals(repo: Repository) -> tuple[list[Signal], dict]:
    """Open tickets against their true target.

    ASSUMPTION, stated because it changes the numbers: the dataset records no
    agent-response timestamp, so "elapsed" is measured from ticket creation to
    the snapshot. That is the right proxy for a first-response target on a
    ticket that is still open, and it is why only OPEN tickets are assessed --
    computing a first-response breach on a closed ticket would be meaningless.
    """
    out: list[Signal] = []
    breached = at_risk = 0
    for t in repo.list_tickets(only_open=True):
        acct = repo.get_account(t.account_id)
        tr = triage(t)
        s = engine.resolve_sla(acct, tr.severity, t.created_at, subject=t.ticket_id)

        if s.breached:
            breached += 1
            over_ratio = s.minutes_over / max(s.target_minutes, 1)
            score = SEV_WEIGHT[tr.severity] * 2.0 + min(over_ratio, 3.0) * 1.5
            out.append(Signal(
                signal_id=f"SLA-{t.ticket_id}", kind="sla_breach",
                title=f"{tr.severity.value} SLA breached — {t.ticket_id} ({acct.account_name})",
                why_it_matters=(
                    f"First response was due {s.deadline} under a "
                    f"{s.target} target ({'the signed agreement' if s.authority_used == 'contract' else 'Support Policy v3'}). "
                    f"It is now {clock.humanise_minutes(s.minutes_over)} overdue. "
                    f"Support Policy v3 §4 requires the breach to be stated and escalated "
                    f"rather than hidden."),
                score=score, severity=_band(score), accounts=[t.account_id],
                evidence=[{"ticket_id": t.ticket_id, "subject": t.subject,
                           "severity": tr.severity.value, "triage_rationale": tr.rationale,
                           "created_at": s.created_at, "due": s.deadline,
                           "overdue_by": clock.humanise_minutes(s.minutes_over),
                           "clock": s.clock_type, "target": s.target,
                           "target_from": s.authority_used}],
                sources=[s.source, tr.policy_source],
                recommended_action={"action_type": "create_escalation",
                                    "ticket_id": t.ticket_id,
                                    "severity": tr.severity.value,
                                    "reason": f"{tr.severity.value} first-response SLA "
                                              f"breached by {clock.humanise_minutes(s.minutes_over)}."}))
        elif s.clock_running and s.minutes_remaining <= max(0.25 * s.target_minutes, 15):
            at_risk += 1
            score = SEV_WEIGHT[tr.severity] * 1.5
            out.append(Signal(
                signal_id=f"SLA-RISK-{t.ticket_id}", kind="sla_at_risk",
                title=f"{tr.severity.value} approaching SLA — {t.ticket_id} ({acct.account_name})",
                why_it_matters=(f"Only {clock.humanise_minutes(s.minutes_remaining)} left "
                                f"against a {s.target} target, due {s.deadline}."),
                score=score, severity=_band(score), accounts=[t.account_id],
                evidence=[{"ticket_id": t.ticket_id, "subject": t.subject,
                           "remaining": clock.humanise_minutes(s.minutes_remaining),
                           "due": s.deadline, "target": s.target}],
                sources=[s.source]))
    return out, {"open_tickets": len(repo.list_tickets(only_open=True)),
                 "sla_breached": breached, "sla_at_risk": at_risk}


# --------------------------------------------------------------------------
# 2. Complaint clusters
# --------------------------------------------------------------------------

_GENERIC = {"customer", "ticket", "issue", "shipment", "parcelpilot", "order",
            "orders", "still", "shows", "reports", "asks", "wants", "page",
            "into", "through", "each", "same", "some", "then", "than", "this",
            "that", "with", "from", "they", "them", "when", "what", "been"}

# Short tokens that carry real domain meaning and must survive the length
# filter -- "csv" is three characters and is the single most discriminative
# term in the bulk-upload cluster.
_DOMAIN_SHORT = {"csv", "api", "sla", "p1", "p2", "p3", "ki", "cod", "500"}


def _fingerprint(t: Ticket) -> set[str]:
    toks = set(tokenize(f"{t.subject} {t.subject} {t.description}"))
    return {w for w in toks
            if w not in _GENERIC and (len(w) > 3 or w in _DOMAIN_SHORT)}


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _cluster_signals(repo: Repository) -> list[Signal]:
    tickets = [t for t in repo.list_tickets()
               if t.created_at and t.created_at >= clock.now() - timedelta(days=21)]
    fps = {t.ticket_id: _fingerprint(t) for t in tickets}

    # Greedy single-link clustering, seeded from the most recent tickets so a
    # live problem forms the cluster rather than an old one absorbing it.
    # Single-link agglomeration. A candidate joins if it is similar to ANY
    # existing member, not merely to the seed. Seed-only comparison fragments
    # a genuine cluster whose members drift in wording -- six bulk-upload
    # complaints phrased six different ways split into two groups and stopped
    # matching the known issue that explains all of them.
    order = sorted(tickets, key=lambda t: t.created_at, reverse=True)
    clusters: list[list[Ticket]] = []
    assigned: set[str] = set()
    for t in order:
        if t.ticket_id in assigned:
            continue
        group = [t]
        assigned.add(t.ticket_id)
        grew = True
        while grew:
            grew = False
            for other in order:
                if other.ticket_id in assigned:
                    continue
                fo = fps[other.ticket_id]
                if any(_similar(fps[m.ticket_id], fo) >= _CLUSTER_THRESHOLD
                       and len(fps[m.ticket_id] & fo) >= 3 for m in group):
                    group.append(other)
                    assigned.add(other.ticket_id)
                    grew = True
        if len(group) >= 3:
            clusters.append(group)

    known_issues = [c for c in all_chunks()
                    if c.section.startswith("Known issue") and c.status != "RESOLVED"]

    out: list[Signal] = []
    for group in clusters:
        group.sort(key=lambda t: t.created_at, reverse=True)
        accounts = sorted({t.account_id for t in group})
        recent = [t for t in group if t.created_at >= clock.now() - timedelta(days=7)]
        prior = [t for t in group if t.created_at < clock.now() - timedelta(days=7)]

        # Rising-rate test: last 7 days against the 14 before, normalised.
        rate_now = len(recent) / 7.0
        rate_before = len(prior) / 14.0
        rising = rate_now > max(rate_before * 2.0, 0.28)

        shared = sorted(set.intersection(*[fps[t.ticket_id] for t in group]) or
                        {w for t in group for w in fps[t.ticket_id]})[:6]
        theme = ", ".join(shared[:4]) or "related reports"

        # Map the cluster to a documented known issue where one matches.
        matched_ki, ki_source = None, None
        blob = " ".join(f"{t.subject} {t.description}" for t in group).lower()
        for ki in known_issues:
            ki_id = re.search(r"KI-\d+", ki.citation)
            ki_terms = _fingerprint(Ticket(ticket_id="x", account_id="x",
                                           subject=ki.citation, description=ki.text))
            if len(ki_terms & set(tokenize(blob))) >= 4:
                matched_ki, ki_source = ki_id.group(0) if ki_id else ki.citation, ki.citation
                break

        score = len(group) * 0.9 + len(accounts) * 1.2 + (2.5 if rising else 0)
        if matched_ki:
            score += 1.0

        why = (f"{len(group)} tickets share this theme"
               f"{f', {len(recent)} of them in the last 7 days' if recent else ''}. ")
        if rising:
            why += (f"Volume is rising: {rate_now:.1f}/day now versus "
                    f"{rate_before:.1f}/day over the preceding fortnight. ")
        if len(accounts) > 1:
            why += f"It affects {len(accounts)} different accounts, so it is unlikely to be customer-specific. "
        if matched_ki:
            why += (f"It matches documented known issue {matched_ki}, so a workaround "
                    f"already exists and can be sent proactively.")
        else:
            why += "No documented known issue matches, so this may be undocumented."

        out.append(Signal(
            signal_id=f"CLUSTER-{group[0].ticket_id}", kind="complaint_cluster",
            title=f"{len(group)} similar reports: {theme}"
                  + (f" (matches {matched_ki})" if matched_ki else " (undocumented)"),
            why_it_matters=why, score=score, severity=_band(score), accounts=accounts,
            evidence=[{"ticket_id": t.ticket_id, "account_id": t.account_id,
                       "created_at": clock.fmt(t.created_at), "subject": t.subject,
                       "status": t.status} for t in group[:8]],
            sources=[ki_source] if ki_source else [],
            recommended_action={
                "action_type": "create_followup_task",
                "details": (f"Investigate cluster of {len(group)} reports ({theme})"
                            + (f" against {matched_ki}" if matched_ki else "")),
                "reason": why[:300]}))
    return sorted(out, key=lambda s: -s.score)


# --------------------------------------------------------------------------
# 3. Cross-account impact
# --------------------------------------------------------------------------

def _cross_account_signals(repo: Repository) -> list[Signal]:
    by_carrier: dict[str, set[str]] = defaultdict(set)
    problem_orders: dict[str, list] = defaultdict(list)
    for o in repo.list_orders():
        by_carrier[o.carrier].add(o.account_id)
        late = o.pickup_delay_minutes() or 0
        if o.carrier_fault or late > 120:
            problem_orders[o.carrier].append(o)

    out = []
    for carrier, orders in problem_orders.items():
        accounts = sorted({o.account_id for o in orders})
        total = len([o for o in repo.list_orders() if o.carrier == carrier])
        if not orders:
            continue
        rate = len(orders) / max(total, 1)
        score = len(accounts) * 1.8 + rate * 3.0
        out.append(Signal(
            signal_id=f"CARRIER-{carrier.replace(' ', '')}", kind="carrier_performance",
            title=f"{carrier}: {len(orders)} of {total} orders with carrier fault or major delay",
            why_it_matters=(
                f"{rate*100:.0f}% of {carrier} orders in the dataset show carrier fault or "
                f"a pickup more than 2 hours past window, across {len(accounts)} account(s). "
                f"Concentrated carrier failure is a supplier-management problem, not a "
                f"series of unrelated support tickets."),
            score=score, severity=_band(score), accounts=accounts,
            evidence=[{"order_id": o.order_id, "account_id": o.account_id,
                       "carrier_fault": o.carrier_fault,
                       "late_by": clock.humanise_minutes(o.pickup_delay_minutes() or 0),
                       "status": o.status.value} for o in orders[:8]],
            recommended_action={
                "action_type": "create_followup_task",
                "details": f"Review {carrier} pickup performance with carrier partner team",
                "reason": f"{len(orders)}/{total} orders affected across {len(accounts)} accounts."}))
    return out


# --------------------------------------------------------------------------
# 4. Order anomalies
# --------------------------------------------------------------------------

def _order_signals(repo: Repository) -> list[Signal]:
    out = []
    stuck = [o for o in repo.list_orders()
             if not o.pickup_occurred and (o.pickup_delay_minutes() or 0) > 120]
    if stuck:
        accounts = sorted({o.account_id for o in stuck})
        score = 2.0 + len(stuck) * 1.4 + len(accounts) * 0.8
        out.append(Signal(
            signal_id="ORD-STUCK", kind="order_anomaly",
            title=f"{len(stuck)} shipment(s) never collected, more than 2 hours past window",
            why_it_matters=(
                "These are still uncollected at the snapshot and are accruing delay. "
                "Each is a probable service-credit case that the customer has not "
                "necessarily raised yet — reaching out first is cheaper than a complaint."),
            score=score, severity=_band(score), accounts=accounts,
            evidence=[{"order_id": o.order_id, "account_id": o.account_id,
                       "carrier": o.carrier,
                       "window_ended": clock.fmt(o.pickup_window_end),
                       "late_by": clock.humanise_minutes(o.pickup_delay_minutes() or 0),
                       "carrier_fault": o.carrier_fault} for o in stuck],
            recommended_action={
                "action_type": "create_followup_task",
                "details": "Proactively contact affected customers and assess service credits",
                "reason": f"{len(stuck)} uncollected shipment(s) past the 2-hour mark."}))

    cancels = [o for o in repo.list_orders() if o.cancellation_requested_at]
    total = len(repo.list_orders())
    if total and len(cancels) / total >= 0.4:
        score = 3.0 + (len(cancels) / total) * 3.0
        out.append(Signal(
            signal_id="ORD-CANCELRATE", kind="order_anomaly",
            title=f"Elevated cancellation requests: {len(cancels)} of {total} orders",
            why_it_matters=(
                f"{len(cancels)/total*100:.0f}% of orders in scope have a cancellation "
                f"request. A rate this high usually points at a booking-flow or pricing "
                f"problem rather than at individual customer changes of mind."),
            score=score, severity=_band(score),
            accounts=sorted({o.account_id for o in cancels}),
            evidence=[{"order_id": o.order_id, "account_id": o.account_id,
                       "requested_at": clock.fmt(o.cancellation_requested_at),
                       "status": o.status.value} for o in cancels]))
    return out


# --------------------------------------------------------------------------

def build_signals(principal: Principal) -> SignalBoard:
    repo = Repository(principal)
    sla, stats = _sla_signals(repo)
    signals = sla + _cluster_signals(repo) + _cross_account_signals(repo) + _order_signals(repo)
    signals.sort(key=lambda s: -s.score)
    stats.update({
        "total_signals": len(signals),
        "critical": sum(1 for s in signals if s.severity == "critical"),
        "accounts_affected": len({a for s in signals for a in s.accounts}),
    })
    return SignalBoard(generated_at=clock.fmt(clock.now()), signals=signals, stats=stats)
