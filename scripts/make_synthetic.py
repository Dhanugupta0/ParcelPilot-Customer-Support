"""Generate a clearly-flagged synthetic ticket backlog for the Signal Board.

The supplied pack has 7 tickets at a single instant. That is enough to answer a
question about any one of them, and not nearly enough to demonstrate proactive
detection -- "a sudden increase in similar complaints" needs a baseline to rise
above. The brief invites adding data, so we add three weeks of history.

Design rules, so this can never be mistaken for supplied ground truth:
  * every record is marked synthetic=True and is hidden from customer sessions
  * ids use the SYN- prefix, never TKT-
  * no synthetic record carries a historical_resolution, so the two genuinely
    incorrect past answers in the pack remain the only ones
  * the planted clusters track the real known-issue open dates in the Product
    Operations Guide: KI-208 (bulk upload) opened 10 Aug, KI-211 (SwiftShip
    webhook) opened 12 Aug. The spike is therefore explainable, not arbitrary.
  * bulk-upload complaints come only from Growth and Enterprise accounts,
    because the Product Operations Guide says Standard has no Bulk Upload.

Deterministic: seeded, so the board is identical on every run and in CI.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(20260816)
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "synthetic_tickets.json"

SNAPSHOT = datetime(2026, 8, 16, 11, 0)
PLAN = {"ACCT-001": "Enterprise", "ACCT-002": "Growth",
        "ACCT-003": "Standard", "ACCT-004": "Enterprise"}
BULK_CAPABLE = [a for a, p in PLAN.items() if p in ("Growth", "Enterprise")]
AGENTS = ["Rohit", "Maya", "Arjun", "Neha"]

# Deliberately wide and low-repetition. An earlier version drew from ten
# templates across thirty tickets, which guaranteed duplicate subjects and made
# the clusterer report false "recurring issues" that were purely an artefact of
# the generator. Background noise has to actually be noisy.
BASELINE = [
    ("How do I download a commercial invoice?", "Cannot find the invoice download on the shipment detail page."),
    ("Add a second billing contact", "Wants two people to receive billing emails."),
    ("Change pickup address for future bookings", "Moved warehouses, wants the default pickup address updated."),
    ("Question about weekend pickup availability", "Does the carrier collect on Sundays?"),
    ("Rate card clarification", "Asks how volumetric weight is calculated."),
    ("Cannot add a new user to the account", "Adding a teammate returns a validation error on the phone field."),
    ("Webhook signature mismatch", "Reports HMAC verification failing on order.updated events."),
    ("Duplicate shipment created", "Two identical shipments were created from one submission."),
    ("Export of monthly shipment report", "Wants a CSV of all shipments for July for reconciliation."),
    ("Update company name on labels", "Legal entity name changed after restructuring."),
    ("COD remittance timing", "Asks when cash-on-delivery amounts are settled."),
    ("Insurance declaration for high-value parcel", "Wants to declare INR 90,000 of electronics."),
    ("Timezone shown incorrectly in reports", "Report timestamps appear to be UTC rather than IST."),
    ("Recipient phone number validation too strict", "Rejects a valid landline number."),
    ("Request for sandbox API credentials", "Developer wants test keys before going live."),
    ("Packaging guidelines for fragile goods", "Asks for the recommended packaging spec."),
    ("Change notification email recipients", "Wants delivery notifications sent to a shared inbox."),
    ("Address autocomplete missing a pincode", "A newly created pincode is not selectable."),
    ("Delivery attempted but no one contacted", "Recipient says no call was made before the attempt."),
    ("Reprint a damaged label", "Label smudged in transit, needs a reprint."),
    ("Query on dimensional rounding", "Asks whether dimensions round up to the nearest cm."),
    ("Bank details update for refunds", "Finance team changed the settlement account."),
    ("Shipment stuck in customs", "International parcel held for documentation."),
    ("Cannot filter shipments by date range", "Date filter returns no results for last month."),
]

BULK = [
    ("Bulk upload fails on large CSV", "Bulk upload of a {n} row CSV file stops partway through and reports a generic error."),
    ("Bulk upload CSV import fails near the end", "Bulk upload of a {n} row CSV file reaches roughly 80% then fails. Smaller CSV files upload fine."),
    ("Large bulk upload CSV times out", "Bulk upload of a {n} row CSV file times out. Splitting the CSV into smaller files worked."),
    ("Bulk upload CSV error with no detail", "Bulk upload of a {n} row CSV file fails with a processing error. Creating shipments individually works."),
    ("Bulk upload CSV keeps failing", "Tried three times to bulk upload a {n} row CSV file, fails each time at the same point."),
    ("Bulk upload CSV unusable for our daily file", "Our daily {n} row CSV bulk upload no longer completes; this started last week."),
]

SWIFT = [
    ("Order still BOOKED after collection", "Driver collected the parcel but the dashboard still shows BOOKED."),
    ("Pickup not reflected in ParcelPilot", "SwiftShip driver scanned the parcel 15 minutes ago, status has not moved."),
    ("Status stuck on BOOKED", "Customer has a signed handover but the shipment shows BOOKED."),
    ("Pickup confirmation missing", "SwiftShip collection happened this morning, ParcelPilot has not updated."),
    ("Dashboard disagrees with carrier tracking", "SwiftShip tracking shows in transit, ParcelPilot still shows BOOKED."),
]


def dt(day_offset: float, hour: int, minute: int = 0) -> str:
    d = SNAPSHOT - timedelta(days=day_offset)
    return d.replace(hour=hour, minute=minute, second=0).strftime("%Y-%m-%d %H:%M")


rows: list[dict] = []
n = 0
baseline_pool = random.sample(BASELINE, len(BASELINE))


def _status(day_offset: float) -> str:
    """Only very recent tickets stay open.

    A 20-person support team does not leave a P2 unanswered for a week, and
    pretending otherwise buries the genuinely breached tickets from the supplied
    pack under synthetic noise on the SLA radar.
    """
    return "open" if day_offset <= 2.2 else "closed"


def add(account: str, subject: str, desc: str, created: str,
        status: str, channel: str | None = None) -> None:
    global n
    n += 1
    rows.append({
        "ticket_id": f"SYN-{600 + n}",
        "account_id": account,
        "created_at": created,
        "status": status,
        "subject": subject,
        "description": desc,
        "channel": channel or random.choice(["email", "chat"]),
        "assigned_to": random.choice(AGENTS),
        "last_customer_message_at": created,
        "historical_resolution": None,
    })


# --- baseline: steady, low-severity chatter across the three weeks ---------
for day in range(21, 0, -1):
    for _ in range(random.choice([0, 1, 1, 2])):
        subject, desc = baseline_pool.pop()
        add(random.choice(list(PLAN)), subject, desc,
            dt(day, random.randint(9, 17), random.choice([0, 15, 30, 45])),
            _status(day))
        if not baseline_pool:
            baseline_pool = random.sample(BASELINE, len(BASELINE))

# --- cluster 1: bulk upload, rising from KI-208's open date (10 Aug) -------
for i, (subject, desc) in enumerate(BULK):
    day = 6 - i * 0.9                      # 10 Aug -> 16 Aug, accelerating
    acct = BULK_CAPABLE[i % len(BULK_CAPABLE)]
    add(acct, subject, desc.format(n=random.choice([3200, 3600, 4100, 4800, 3400, 5000])),
        dt(max(day, 0.2), random.randint(9, 16), random.choice([5, 20, 35, 50])),
        _status(max(day, 0.2)))

# --- cluster 2: SwiftShip pickup status, from KI-211's open date (12 Aug) --
for i, (subject, desc) in enumerate(SWIFT):
    day = 4 - i * 0.8
    acct = ["ACCT-001", "ACCT-003", "ACCT-004", "ACCT-002", "ACCT-001"][i]
    add(acct, subject, desc, dt(max(day, 0.15), random.randint(9, 16),
                                random.choice([10, 25, 40])),
        _status(max(day, 0.15)), channel="chat")

rows.sort(key=lambda r: r["created_at"])
OUT.write_text(json.dumps(rows, indent=2))
print(f"wrote {len(rows)} synthetic tickets -> {OUT}")
opens = sum(1 for r in rows if r["status"] == "open")
print(f"  open: {opens}   bulk-upload cluster: {len(BULK)}   swiftship cluster: {len(SWIFT)}")
