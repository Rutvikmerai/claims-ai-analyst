"""
create_db.py
Builds a realistic (synthetic) healthcare claims SQLite database.
Run this once to generate db/claims.db before starting the app.
"""

import sqlite3
import random
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "claims.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    provider_id     INTEGER PRIMARY KEY,
    provider_name   TEXT NOT NULL,
    specialty       TEXT NOT NULL,
    state           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    member_id       INTEGER PRIMARY KEY,
    age             INTEGER NOT NULL,
    gender          TEXT NOT NULL,
    plan_type       TEXT NOT NULL   -- HMO, PPO, EPO, Medicare Advantage
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id            INTEGER PRIMARY KEY,
    member_id           INTEGER NOT NULL REFERENCES members(member_id),
    provider_id         INTEGER NOT NULL REFERENCES providers(provider_id),
    claim_type          TEXT NOT NULL,      -- Inpatient, Outpatient, Pharmacy, Dental, Vision
    submitted_date      TEXT NOT NULL,      -- ISO date
    billed_amount       REAL NOT NULL,
    allowed_amount       REAL NOT NULL,
    paid_amount          REAL NOT NULL,
    status               TEXT NOT NULL,      -- Paid, Rejected, Pending, Under Review
    rejection_reason     TEXT                -- NULL unless status = Rejected
);
"""

CLAIM_TYPES = ["Inpatient", "Outpatient", "Pharmacy", "Dental", "Vision"]
PLAN_TYPES = ["HMO", "PPO", "EPO", "Medicare Advantage"]
SPECIALTIES = ["Cardiology", "Orthopedics", "Primary Care", "Dermatology",
               "Radiology", "Oncology", "Pediatrics", "Psychiatry"]
STATES = ["OH", "NY", "CA", "TX", "FL", "PA", "IL"]
STATUSES = ["Paid", "Rejected", "Pending", "Under Review"]
REJECTION_REASONS = [
    "Missing prior authorization",
    "Duplicate claim",
    "Service not covered under plan",
    "Incorrect member ID",
    "Filed past deadline",
    "Incomplete documentation",
]

# Different claim types get different rejection likelihoods on purpose,
# so the demo query ("which claim type has the highest rejection rate")
# has a real, non-random answer to point to.
REJECTION_WEIGHT = {
    "Inpatient": 0.06,
    "Outpatient": 0.10,
    "Pharmacy": 0.22,   # intentionally the highest - good demo answer
    "Dental": 0.14,
    "Vision": 0.08,
}


def random_date_within_last_year():
    start = date.today() - timedelta(days=365)
    offset = random.randint(0, 365)
    return (start + timedelta(days=offset)).isoformat()


def build_database(n_providers=25, n_members=400, n_claims=3000, seed=42):
    random.seed(seed)
    DB_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    # Providers
    for pid in range(1, n_providers + 1):
        cur.execute(
            "INSERT INTO providers VALUES (?, ?, ?, ?)",
            (pid, f"Provider Group {pid}", random.choice(SPECIALTIES), random.choice(STATES)),
        )

    # Members
    for mid in range(1, n_members + 1):
        cur.execute(
            "INSERT INTO members VALUES (?, ?, ?, ?)",
            (mid, random.randint(1, 90), random.choice(["M", "F"]), random.choice(PLAN_TYPES)),
        )

    # Claims
    for cid in range(1, n_claims + 1):
        claim_type = random.choice(CLAIM_TYPES)
        billed = round(random.uniform(80, 15000), 2)
        is_rejected = random.random() < REJECTION_WEIGHT[claim_type]

        if is_rejected:
            status = "Rejected"
            allowed = 0.0
            paid = 0.0
            reason = random.choice(REJECTION_REASONS)
        else:
            status = random.choices(
                ["Paid", "Pending", "Under Review"], weights=[0.82, 0.10, 0.08]
            )[0]
            allowed = round(billed * random.uniform(0.6, 0.95), 2)
            paid = allowed if status == "Paid" else 0.0
            reason = None

        cur.execute(
            """INSERT INTO claims
               (claim_id, member_id, provider_id, claim_type, submitted_date,
                billed_amount, allowed_amount, paid_amount, status, rejection_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid,
                random.randint(1, n_members),
                random.randint(1, n_providers),
                claim_type,
                random_date_within_last_year(),
                billed,
                allowed,
                paid,
                status,
                reason,
            ),
        )

    conn.commit()
    conn.close()
    print(f"Database built at {DB_PATH} ({n_claims} claims, {n_members} members, {n_providers} providers).")


if __name__ == "__main__":
    build_database()
