"""
Build & update a mini utility CIS database from 4 CSVs, then compute monthly bills.
- No extra files are generated. All results are written into cis_demo.sqlite.
- Assumes cumulative monthly readings and a tiered rate schedule.
- NOW: Rates v2 — for each bill period, pick the latest rate whose effective_date <= period_end (no proration).

Tables expected in CSVs:
  customers.csv: customer_id,account_number,name,address,postal_code,service_type,meter_id,start_date
  meters.csv:    meter_id,install_date,meter_type,status
  readings.csv:  (optional reading_id),meter_id,reading_date,reading_value   # cumulative reads
  rates.csv:     rate_id,effective_date,fixed_charge,tier1_limit,tier1_rate,tier2_limit,tier2_rate,tier3_rate
"""

from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime
import pandas as pd
import numpy as np

# -----------------------
# Configuration
# -----------------------
CSV_DIR = Path("C:/Users/jinyu/OneDrive/Desktop/Wyse_project/")  
DB_PATH = Path("C:/Users/jinyu/OneDrive/Desktop/Wyse_project/cis_demo.sqlite")

PATH_CUSTOMERS = CSV_DIR / "customers.csv"
PATH_METERS    = CSV_DIR / "meters.csv"
PATH_READINGS  = CSV_DIR / "readings.csv"
PATH_RATES     = CSV_DIR / "rates.csv"

# Billing periods to produce (period_end dates)
# Example: if readings exist on 2025-06-01, 2025-07-01, 2025-08-01, 2025-09-01,
#          then billed periods are [Jun, Jul, Aug] ending on the latter three dates.
TARGET_PERIOD_ENDS = {"2025-07-01", "2025-08-01", "2025-09-01"}

TAX_RATE = 0.13  # demo tax


# -----------------------
# DB Schema
# -----------------------
SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS bills;
DROP TABLE IF EXISTS readings;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS meters;
DROP TABLE IF EXISTS rates;

CREATE TABLE customers (
    customer_id    INTEGER PRIMARY KEY,
    account_number TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    address        TEXT NOT NULL,
    postal_code    TEXT NOT NULL,
    service_type   TEXT NOT NULL CHECK (service_type IN ('water','electric','gas','sewer')),
    meter_id       TEXT NOT NULL,
    start_date     DATE NOT NULL
);

CREATE TABLE meters (
    meter_id     TEXT PRIMARY KEY,
    install_date DATE NOT NULL,
    meter_type   TEXT NOT NULL CHECK (meter_type IN ('AMR','AMI','Manual')),
    status       TEXT NOT NULL CHECK (status IN ('active','inactive','retired'))
);

CREATE TABLE readings (
    reading_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id      TEXT NOT NULL,
    reading_date  DATE NOT NULL,
    reading_value REAL NOT NULL
    -- (cumulative)
);

CREATE TABLE rates (
    rate_id        INTEGER PRIMARY KEY,
    effective_date DATE NOT NULL,
    fixed_charge   REAL NOT NULL,
    tier1_limit    REAL NOT NULL,
    tier1_rate     REAL NOT NULL,
    tier2_limit    REAL NOT NULL,
    tier2_rate     REAL NOT NULL,
    tier3_rate     REAL NOT NULL
);
"""

CREATE_BILLS_SQL = """
CREATE TABLE bills (
    bill_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number TEXT NOT NULL,
    customer_id    INTEGER,
    meter_id       TEXT NOT NULL,
    period_start   DATE NOT NULL,
    period_end     DATE NOT NULL,
    usage_m3       REAL NOT NULL,
    -- rate used for this bill (Rates v2)
    rate_id_applied INTEGER,
    fixed_charge   REAL NOT NULL,
    tier1_units    REAL NOT NULL,
    tier1_amount   REAL NOT NULL,
    tier2_units    REAL NOT NULL,
    tier2_amount   REAL NOT NULL,
    tier3_units    REAL NOT NULL,
    tier3_amount   REAL NOT NULL,
    subtotal       REAL NOT NULL,
    tax_rate       REAL NOT NULL,
    tax_amount     REAL NOT NULL,
    total_due      REAL NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('open','partial','paid','void')),
    generated_at   TEXT NOT NULL,
    issue_flag     TEXT
);
"""

CREATE_BASE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_customers_meter ON customers(meter_id);
CREATE INDEX IF NOT EXISTS idx_readings_meter_date ON readings(meter_id, reading_date);
"""

CREATE_BILLS_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_bills_period ON bills(period_end);
CREATE INDEX IF NOT EXISTS idx_bills_rate ON bills(rate_id_applied);
"""

# -----------------------
# Load & init
# -----------------------
def load_csvs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    customers = pd.read_csv(PATH_CUSTOMERS)
    meters    = pd.read_csv(PATH_METERS)
    readings  = pd.read_csv(PATH_READINGS)
    rates     = pd.read_csv(PATH_RATES)
    readings["reading_date"] = pd.to_datetime(readings["reading_date"])
    rates["effective_date"] = pd.to_datetime(rates["effective_date"])
    return customers, meters, readings, rates

def init_db(conn: sqlite3.Connection) -> None:
    """
    Reset and create base tables in a FK-safe order.
    - Drop views first (they may depend on tables)
    - Drop child tables before parents (payments -> bills, etc.)
    - Then run the base schema creation
    """
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=OFF;")

    # Drop dependent views (ignore if missing)
    conn.executescript("""
    DROP VIEW IF EXISTS v_rate_coverage_gaps;
    DROP VIEW IF EXISTS v_rate_applied;
    DROP VIEW IF EXISTS v_bi_fact_payments;
    DROP VIEW IF EXISTS v_bi_fact_bills;
    DROP VIEW IF EXISTS v_bi_dim_meters;
    DROP VIEW IF EXISTS v_bi_dim_customers;
    DROP VIEW IF EXISTS v_bi_dim_dates;
    DROP VIEW IF EXISTS v_dq_bill_status;
    DROP VIEW IF EXISTS v_dq_summary;
    DROP VIEW IF EXISTS v_aging_by_account;
    DROP VIEW IF EXISTS v_aging_rollup;
    DROP VIEW IF EXISTS v_aging_detail;
    DROP VIEW IF EXISTS v_bill_balances;
    DROP VIEW IF EXISTS v_usage_stats;
    DROP VIEW IF EXISTS v_revenue_by_month;
    """)

    # Drop auxiliary/snapshot tables
    conn.executescript("""
    DROP TABLE IF EXISTS aging_snapshot;
    DROP TABLE IF EXISTS payments;
    DROP TABLE IF EXISTS dq_findings;
    """)

    # Now run the base schema (drops/creates customers, meters, readings, rates, bills)
    conn.executescript(SCHEMA_SQL)

    cur.execute("PRAGMA foreign_keys=ON;")
    conn.commit()

def insert_base_tables(conn: sqlite3.Connection,
                       customers: pd.DataFrame,
                       meters: pd.DataFrame,
                       readings: pd.DataFrame,
                       rates: pd.DataFrame) -> None:
    readings_to_insert = readings[["meter_id", "reading_date", "reading_value"]].copy()
    readings_to_insert["reading_date"] = readings_to_insert["reading_date"].dt.date.astype(str)

    customers.to_sql("customers", conn, if_exists="append", index=False)
    meters.to_sql("meters", conn, if_exists="append", index=False)
    readings_to_insert.to_sql("readings", conn, if_exists="append", index=False)
    rates_to_insert = rates.copy()
    rates_to_insert["effective_date"] = rates_to_insert["effective_date"].dt.date.astype(str)
    rates_to_insert.to_sql("rates", conn, if_exists="append", index=False)

    conn.executescript(CREATE_BASE_INDEXES_SQL)
    conn.commit()

# -----------------------
# Billing (Rates v2)
# -----------------------
def get_rate_for_period_end(conn: sqlite3.Connection, period_end: str) -> dict | None:
    """
    Returns the latest rate row (as dict) whose effective_date <= period_end.
    If none exists, returns None.
    """
    q = """
        SELECT *
        FROM rates
        WHERE date(effective_date) <= date(?)
        ORDER BY date(effective_date) DESC
        LIMIT 1
    """
    df = pd.read_sql_query(q, conn, params=(period_end,))
    if df.empty:
        return None
    r = df.iloc[0]
    return {
        "rate_id":     int(r["rate_id"]),
        "fixed_charge": float(r["fixed_charge"]),
        "tier1_limit":  float(r["tier1_limit"]),
        "tier1_rate":   float(r["tier1_rate"]),
        "tier2_limit":  float(r["tier2_limit"]),
        "tier2_rate":   float(r["tier2_rate"]),
        "tier3_rate":   float(r["tier3_rate"]),
    }

def compute_periods(conn: sqlite3.Connection) -> pd.DataFrame:
    customers = pd.read_sql_query("SELECT customer_id, account_number, meter_id FROM customers", conn)
    readings  = pd.read_sql_query(
        "SELECT meter_id, reading_date, reading_value FROM readings",
        conn, parse_dates=["reading_date"]
    )

    readings = readings.sort_values(["meter_id", "reading_date"]).reset_index(drop=True)
    readings["prev_value"] = readings.groupby("meter_id")["reading_value"].shift(1)
    readings["prev_date"]  = readings.groupby("meter_id")["reading_date"].shift(1)

    periods = readings.dropna(subset=["prev_value"]).copy()
    periods["usage_m3"]     = (periods["reading_value"] - periods["prev_value"]).round(2)
    periods["period_start"] = periods["prev_date"].dt.date.astype(str)
    periods["period_end"]   = periods["reading_date"].dt.date.astype(str)

    periods = periods[periods["period_end"].isin(TARGET_PERIOD_ENDS)].copy()

    out = periods.merge(customers, on="meter_id", how="left")
    out["issue_flag"] = np.where(out["usage_m3"] <= 0, "nonpositive_usage", "ok")
    return out[[
        "account_number", "customer_id", "meter_id", "period_start", "period_end", "usage_m3", "issue_flag"
    ]]

def price_usage(usage: float, r: dict) -> dict:
    u = max(0.0, float(usage))
    t1_cap, t2_cap = r["tier1_limit"], r["tier2_limit"]

    t1_units = min(u, t1_cap)
    t2_units = min(max(u - t1_cap, 0.0), t2_cap - t1_cap)
    t3_units = max(u - t2_cap, 0.0)

    t1_amt = t1_units * r["tier1_rate"]
    t2_amt = t2_units * r["tier2_rate"]
    t3_amt = t3_units * r["tier3_rate"]

    subtotal = r["fixed_charge"] + t1_amt + t2_amt + t3_amt
    return {
        "fixed_charge": r["fixed_charge"],
        "tier1_units": round(t1_units, 2), "tier1_amount": round(t1_amt, 2),
        "tier2_units": round(t2_units, 2), "tier2_amount": round(t2_amt, 2),
        "tier3_units": round(t3_units, 2), "tier3_amount": round(t3_amt, 2),
        "subtotal": round(subtotal, 2)
    }

def build_bills(conn: sqlite3.Connection) -> int:
    """
    Compute bills from periods with per-period rate selection (Rates v2).
    - If no rate is found for a period_end, we still create the row with zeros and flag dq later.
    """
    periods = compute_periods(conn)

    rows = []
    for _, row in periods.iterrows():
        rate = get_rate_for_period_end(conn, row["period_end"])
        if rate is None:
            # No valid rate — create a skeleton bill for audit
            priced = {
                "fixed_charge": 0.0,
                "tier1_units": 0.0, "tier1_amount": 0.0,
                "tier2_units": 0.0, "tier2_amount": 0.0,
                "tier3_units": 0.0, "tier3_amount": 0.0,
                "subtotal": 0.0
            }
            rate_id_applied = None
        else:
            priced = price_usage(row["usage_m3"], rate)
            rate_id_applied = rate["rate_id"]

        tax_amount = round(priced["subtotal"] * TAX_RATE, 2)
        total_due  = round(priced["subtotal"] + tax_amount, 2)
        rows.append({
            "account_number": row["account_number"],
            "customer_id": None if pd.isna(row["customer_id"]) else int(row["customer_id"]),
            "meter_id": row["meter_id"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "usage_m3": round(float(row["usage_m3"]), 2),
            "rate_id_applied": rate_id_applied,
            **priced,
            "tax_rate": TAX_RATE,
            "tax_amount": tax_amount,
            "total_due": total_due,
            "status": "open",
            "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
            "issue_flag": row["issue_flag"]
        })

    conn.executescript("DROP TABLE IF EXISTS bills;")
    conn.executescript(CREATE_BILLS_SQL)
    pd.DataFrame(rows).to_sql("bills", conn, if_exists="append", index=False)

    conn.executescript(CREATE_BILLS_INDEXES_SQL)
    conn.commit()
    return len(rows)

# -----------------------
# Payments & Aging
# -----------------------
CREATE_PAYMENTS_SQL = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id        INTEGER NOT NULL,
    account_number TEXT NOT NULL,
    payment_date   DATE NOT NULL,
    amount         REAL NOT NULL CHECK (amount >= 0),
    method         TEXT,
    note           TEXT,
    FOREIGN KEY (bill_id) REFERENCES bills(bill_id)
);
"""

CREATE_PAYMENTS_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_payments_bill    ON payments(bill_id);
CREATE INDEX IF NOT EXISTS idx_payments_account ON payments(account_number);
CREATE INDEX IF NOT EXISTS idx_payments_date    ON payments(payment_date);
"""

def ensure_due_dates(conn: sqlite3.Connection, net_days: int = 21) -> None:
    cols = pd.read_sql_query("PRAGMA table_info(bills);", conn)
    if "due_date" not in cols["name"].tolist():
        conn.execute("ALTER TABLE bills ADD COLUMN due_date DATE;")
        conn.commit()
    conn.execute(f"""
        UPDATE bills
        SET due_date = date(period_end, '+{net_days} day')
        WHERE due_date IS NULL;
    """)
    conn.commit()

def ensure_payments_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CREATE_PAYMENTS_SQL)
    conn.executescript(CREATE_PAYMENTS_INDEXES_SQL)
    conn.commit()

def seed_demo_payments(conn: sqlite3.Connection, seed: int = 42, pay_fraction: float = 0.6) -> int:
    import random
    random.seed(seed)
    bills = pd.read_sql_query("""
        SELECT bill_id, account_number, total_due, due_date
        FROM bills
        WHERE status = 'open'
    """, conn)

    rows = []
    for _, r in bills.iterrows():
        if random.random() <= pay_fraction:
            frac = random.choice([1.0, 0.9, 0.8, 0.7])
            amt  = round(float(r["total_due"]) * frac, 2)
            pay_date = pd.read_sql_query(
                "SELECT date(?, '+'||?||' day') AS d",
                conn,
                params=(str(r["due_date"]), random.choice([0, 5, 10, 20, 35, 70])),
            )["d"].iat[0]
            rows.append({
                "bill_id": int(r["bill_id"]),
                "account_number": r["account_number"],
                "payment_date": pay_date,
                "amount": amt,
                "method": random.choice(["card","bank","cash","online"]),
                "note": "demo"
            })
    if rows:
        pd.DataFrame(rows).to_sql("payments", conn, if_exists="append", index=False)
        conn.commit()
        return len(rows)
    return 0

def create_balance_and_aging_views(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    DROP VIEW IF EXISTS v_bill_balances;
    CREATE VIEW v_bill_balances AS
    WITH pay AS (
        SELECT bill_id, SUM(amount) AS paid_amount
        FROM payments
        GROUP BY bill_id
    )
    SELECT
        b.bill_id,
        b.account_number,
        b.period_start,
        b.period_end,
        b.due_date,
        b.total_due,
        IFNULL(p.paid_amount, 0) AS paid_amount,
        ROUND(b.total_due - IFNULL(p.paid_amount, 0), 2) AS balance,
        CAST(julianday(DATE('now')) - julianday(b.due_date) AS INT) AS days_past_due
    FROM bills b
    LEFT JOIN pay p ON p.bill_id = b.bill_id;

    DROP VIEW IF EXISTS v_aging_detail;
    CREATE VIEW v_aging_detail AS
    SELECT
        bill_id,
        account_number,
        due_date,
        total_due,
        paid_amount,
        balance,
        CASE
            WHEN balance <= 0                           THEN 'paid'
            WHEN days_past_due <= 0                     THEN 'current'
            WHEN days_past_due BETWEEN 1  AND 30        THEN '1-30'
            WHEN days_past_due BETWEEN 31 AND 60        THEN '31-60'
            WHEN days_past_due BETWEEN 61 AND 90        THEN '61-90'
            ELSE '90+'
        END AS bucket,
        days_past_due
    FROM v_bill_balances;

    DROP VIEW IF EXISTS v_aging_rollup;
    CREATE VIEW v_aging_rollup AS
    SELECT bucket, COUNT(*) AS bills, ROUND(SUM(balance), 2) AS balance_total
    FROM v_aging_detail
    GROUP BY bucket
    ORDER BY
        CASE bucket
            WHEN 'paid' THEN 0
            WHEN 'current' THEN 1
            WHEN '1-30' THEN 2
            WHEN '31-60' THEN 3
            WHEN '61-90' THEN 4
            ELSE 5
        END;

    DROP VIEW IF EXISTS v_aging_by_account;
    CREATE VIEW v_aging_by_account AS
    SELECT account_number, bucket, COUNT(*) AS bills, ROUND(SUM(balance),2) AS balance_total
    FROM v_aging_detail
    GROUP BY account_number, bucket
    ORDER BY account_number,
        CASE bucket
            WHEN 'paid' THEN 0
            WHEN 'current' THEN 1
            WHEN '1-30' THEN 2
            WHEN '31-60' THEN 3
            WHEN '61-90' THEN 4
            ELSE 5
        END;
    """)
    conn.commit()

# -----------------------
# Data Quality v2: gaps & outliers
# -----------------------
CREATE_DQ_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dq_findings (
    finding_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_type   TEXT NOT NULL,                  -- 'gap' | 'outlier' | 'nonpositive' | 'orphan'
    severity       TEXT NOT NULL,                  -- 'low' | 'medium' | 'high'
    meter_id       TEXT,
    account_number TEXT,
    reading_date   DATE,                           -- for raw reading based findings
    period_start   DATE,                           -- for period/bill based findings
    period_end     DATE,
    observed_value REAL,
    threshold      REAL,
    message        TEXT,
    created_at     TEXT NOT NULL
);
"""

def ensure_dq_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CREATE_DQ_TABLE_SQL)
    conn.commit()

def _dq_link_accounts(conn: sqlite3.Connection, df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    acct = pd.read_sql_query("SELECT account_number, meter_id FROM customers", conn)
    return df.merge(acct, on="meter_id", how="left")

def dq_detect_gaps(conn: sqlite3.Connection) -> pd.DataFrame:
    reads = pd.read_sql_query(
        "SELECT meter_id, reading_date FROM readings ORDER BY meter_id, reading_date",
        conn, parse_dates=["reading_date"]
    )
    if reads.empty:
        return pd.DataFrame(columns=["finding_type","severity","meter_id","reading_date","message","created_at","account_number"])

    reads["prev_date"] = reads.groupby("meter_id")["reading_date"].shift(1)

    def month_delta(a, b):
        return (a.year - b.year)*12 + (a.month - b.month)

    mask = reads["prev_date"].notna() & (reads.apply(lambda r: month_delta(r["reading_date"], r["prev_date"]), axis=1) != 1)
    gaps = reads.loc[mask].copy()
    if gaps.empty:
        return pd.DataFrame(columns=["finding_type","severity","meter_id","reading_date","message","created_at","account_number"])

    gaps["finding_type"] = "gap"
    gaps["severity"] = "high"
    gaps["message"] = gaps.apply(
        lambda r: f"Gap detected between {r['prev_date'].date()} and {r['reading_date'].date()} (month jump != 1).",
        axis=1
    )
    gaps["created_at"] = datetime.utcnow().isoformat(timespec="seconds")
    out = gaps[["finding_type","severity","meter_id","reading_date","message","created_at"]].copy()
    out = _dq_link_accounts(conn, out)
    return out

def dq_compute_period_usage(conn: sqlite3.Connection) -> pd.DataFrame:
    reads = pd.read_sql_query(
        "SELECT meter_id, reading_date, reading_value FROM readings",
        conn, parse_dates=["reading_date"]
    ).sort_values(["meter_id","reading_date"])

    reads["prev_value"] = reads.groupby("meter_id")["reading_value"].shift(1)
    reads["prev_date"]  = reads.groupby("meter_id")["reading_date"].shift(1)
    per = reads.dropna(subset=["prev_value"]).copy()
    per["usage_m3"] = (per["reading_value"] - per["prev_value"]).astype(float)
    per["period_start"] = per["prev_date"].dt.date.astype(str)
    per["period_end"]   = per["reading_date"].dt.date.astype(str)
    return per[["meter_id","period_start","period_end","usage_m3"]]

def dq_detect_outliers(conn: sqlite3.Connection, z_thresh: float = 3.5) -> pd.DataFrame:
    per = dq_compute_period_usage(conn)
    if per.empty:
        return pd.DataFrame(columns=["finding_type","severity","meter_id","period_start","period_end","observed_value","threshold","message","created_at","account_number"])

    findings = []
    for mid, grp in per.groupby("meter_id"):
        x = grp["usage_m3"].astype(float).values
        med = np.median(x)
        mad = np.median(np.abs(x - med))  # raw MAD
        if mad > 0:
            scale = 1.4826 * mad
            z = np.abs((x - med) / scale)
            out_idx = np.where(z > z_thresh)[0]
            for i in out_idx:
                findings.append({
                    "finding_type": "outlier",
                    "severity": "medium",
                    "meter_id": mid,
                    "period_start": grp.iloc[i]["period_start"],
                    "period_end":   grp.iloc[i]["period_end"],
                    "observed_value": float(grp.iloc[i]["usage_m3"]),
                    "threshold": float(med + z_thresh*scale),
                    "message": f"Usage {grp.iloc[i]['usage_m3']:.2f} m3 is a robust-Z>{z_thresh} outlier for meter {mid} (median {med:.2f}, MAD {mad:.2f}).",
                    "created_at": datetime.utcnow().isoformat(timespec="seconds")
                })
        else:
            q1, q3 = np.percentile(x, [25, 75])
            iqr = q3 - q1
            lo, hi = q1 - 1.5*iqr, q3 + 1.5*iqr
            out_mask = (x < lo) | (x > hi)
            for i, flag in enumerate(out_mask):
                if flag:
                    findings.append({
                        "finding_type": "outlier",
                        "severity": "medium",
                        "meter_id": mid,
                        "period_start": grp.iloc[i]["period_start"],
                        "period_end":   grp.iloc[i]["period_end"],
                        "observed_value": float(grp.iloc[i]["usage_m3"]),
                        "threshold": float(hi),
                        "message": f"Usage {grp.iloc[i]['usage_m3']:.2f} m3 outside Tukey fences [{lo:.2f}, {hi:.2f}] for meter {mid}.",
                        "created_at": datetime.utcnow().isoformat(timespec="seconds")
                    })

    out = pd.DataFrame(findings)
    if out.empty:
        return out
    out = _dq_link_accounts(conn, out)
    return out

def dq_write_findings(conn: sqlite3.Connection, *frames: pd.DataFrame) -> int:
    ensure_dq_schema(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM dq_findings;")
    conn.commit()
    df = pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return 0
    df.to_sql("dq_findings", conn, if_exists="append", index=False)
    conn.commit()
    return len(df)

def dq_update_bill_status(conn: sqlite3.Connection) -> None:
    cols = pd.read_sql_query("PRAGMA table_info(bills);", conn)
    if "dq_status" not in cols["name"].tolist():
        conn.execute("ALTER TABLE bills ADD COLUMN dq_status TEXT;")
        conn.commit()

    conn.execute("UPDATE bills SET dq_status = 'ok';")
    # Review if nonpositive usage
    conn.execute("UPDATE bills SET dq_status = 'review' WHERE IFNULL(issue_flag,'ok') <> 'ok';")
    # Review if rate missing (Rates v2 coverage gap)
    conn.execute("UPDATE bills SET dq_status = 'review' WHERE rate_id_applied IS NULL;")
    # Review if outlier flagged
    conn.execute("""
        UPDATE bills
        SET dq_status = 'review'
        WHERE EXISTS (
            SELECT 1
            FROM dq_findings f
            WHERE f.finding_type = 'outlier'
              AND f.meter_id = bills.meter_id
              AND f.period_end = bills.period_end
        );
    """)
    conn.commit()

def dq_create_summary_views(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    DROP VIEW IF EXISTS v_dq_summary;
    CREATE VIEW v_dq_summary AS
    SELECT finding_type, severity, COUNT(*) AS findings
    FROM dq_findings
    GROUP BY finding_type, severity
    ORDER BY finding_type, severity;

    DROP VIEW IF EXISTS v_dq_bill_status;
    CREATE VIEW v_dq_bill_status AS
    SELECT dq_status, COUNT(*) AS bills
    FROM bills
    GROUP BY dq_status;
    """)
    conn.commit()

def run_dq_checks(conn: sqlite3.Connection) -> None:
    gaps = dq_detect_gaps(conn)
    outs = dq_detect_outliers(conn)
    dq_write_findings(conn, gaps, outs)
    dq_update_bill_status(conn)
    dq_create_summary_views(conn)

# -----------------------
# Rate audit views (NEW)
# -----------------------
def create_rate_audit_views(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    DROP VIEW IF EXISTS v_rate_applied;
    CREATE VIEW v_rate_applied AS
    SELECT
        b.bill_id,
        b.account_number,
        b.meter_id,
        b.period_start,
        b.period_end,
        b.rate_id_applied,
        r.effective_date AS rate_effective_date,
        r.fixed_charge,
        r.tier1_limit, r.tier1_rate,
        r.tier2_limit, r.tier2_rate,
        r.tier3_rate
    FROM bills b
    LEFT JOIN rates r ON r.rate_id = b.rate_id_applied;

    DROP VIEW IF EXISTS v_rate_coverage_gaps;
    CREATE VIEW v_rate_coverage_gaps AS
    SELECT
        bill_id,
        account_number,
        meter_id,
        period_start,
        period_end
    FROM bills
    WHERE rate_id_applied IS NULL;
    """)
    conn.commit()

# -----------------------
# Auto-status from balances + Aging snapshots + BI model
# -----------------------
def update_bill_status_from_balances(conn: sqlite3.Connection) -> None:
    """
    Uses v_bill_balances to set bills.status:
      - 'paid'    when balance <= 0
      - 'partial' when 0 < balance < total_due
      - 'open'    when balance >= total_due (no or zero payments)
    """
    conn.execute("SELECT 1 FROM v_bill_balances LIMIT 1;")
    conn.executescript("""
    UPDATE bills
    SET status = CASE
        WHEN (SELECT balance FROM v_bill_balances vb WHERE vb.bill_id = bills.bill_id) <= 0 THEN 'paid'
        WHEN (SELECT balance FROM v_bill_balances vb WHERE vb.bill_id = bills.bill_id) < total_due THEN 'partial'
        ELSE 'open'
    END;
    """)
    conn.commit()

CREATE_AGING_SNAPSHOT_SQL = """
CREATE TABLE IF NOT EXISTS aging_snapshot (
    snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date DATE NOT NULL,
    bucket        TEXT NOT NULL,     -- paid/current/1-30/31-60/61-90/90+
    bills         INTEGER NOT NULL,
    balance_total REAL NOT NULL
);
"""

def create_aging_snapshot_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CREATE_AGING_SNAPSHOT_SQL)
    conn.commit()

def take_aging_snapshot(conn: sqlite3.Connection, as_of_sql: str = "DATE('now')") -> int:
    conn.execute("SELECT 1 FROM v_aging_rollup LIMIT 1;")
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO aging_snapshot (snapshot_date, bucket, bills, balance_total)
        SELECT {as_of_sql}, bucket, bills, balance_total
        FROM v_aging_rollup;
    """)
    conn.commit()
    return cur.rowcount

def create_bi_model(conn: sqlite3.Connection) -> None:
    """
    Creates lightweight BI semantic layer views:
      - v_bi_dim_dates: calendar view spanning min(period_start) .. max(period_end)
      - v_bi_dim_customers: customer dimension
      - v_bi_dim_meters: meter dimension
      - v_bi_fact_bills: fact table for bills (with date keys)
      - v_bi_fact_payments: fact table for payments (with date keys)
    """
    conn.executescript("""
    -- Dates dimension using a recursive CTE between min(period_start) and max(period_end)
    WITH RECURSIVE
      bounds AS (
        SELECT MIN(date(period_start)) AS dmin, MAX(date(period_end)) AS dmax FROM bills
      ),
      cal(d) AS (
        SELECT dmin FROM bounds
        UNION ALL
        SELECT date(d, '+1 day') FROM cal, bounds WHERE d < dmax
      )
    SELECT 1;

    DROP VIEW IF EXISTS v_bi_dim_dates;
    CREATE VIEW v_bi_dim_dates AS
    WITH RECURSIVE
      bounds AS (
        SELECT MIN(date(period_start)) AS dmin, MAX(date(period_end)) AS dmax FROM bills
      ),
      cal(d) AS (
        SELECT dmin FROM bounds
        UNION ALL
        SELECT date(d, '+1 day') FROM cal, bounds WHERE d < dmax
      )
    SELECT
        d AS date_key,
        CAST(STRFTIME('%Y%m%d', d) AS INTEGER) AS yyyymmdd,
        CAST(STRFTIME('%Y', d) AS INTEGER) AS year,
        CAST(STRFTIME('%m', d) AS INTEGER) AS month,
        CAST(STRFTIME('%d', d) AS INTEGER) AS day,
        STRFTIME('%Y-%m', d) AS year_month,
        CAST((CAST(STRFTIME('%m', d) AS INTEGER)+2)/3 AS INTEGER) AS quarter
    FROM cal;

    -- Customer dimension
    DROP VIEW IF EXISTS v_bi_dim_customers;
    CREATE VIEW v_bi_dim_customers AS
    SELECT
        customer_id,
        account_number,
        name,
        address,
        postal_code,
        service_type,
        meter_id,
        start_date
    FROM customers;

    -- Meter dimension
    DROP VIEW IF EXISTS v_bi_dim_meters;
    CREATE VIEW v_bi_dim_meters AS
    SELECT
        meter_id,
        install_date,
        meter_type,
        status
    FROM meters;

    -- Bills fact (include rate_id_applied)
    DROP VIEW IF EXISTS v_bi_fact_bills;
    CREATE VIEW v_bi_fact_bills AS
    SELECT
        b.bill_id,
        b.account_number,
        b.customer_id,
        b.meter_id,
        b.period_start,
        b.period_end,
        CAST(STRFTIME('%Y%m%d', b.period_end) AS INTEGER) AS period_end_key,
        b.usage_m3,
        b.rate_id_applied,
        b.fixed_charge,
        b.tier1_units, b.tier1_amount,
        b.tier2_units, b.tier2_amount,
        b.tier3_units, b.tier3_amount,
        b.subtotal, b.tax_rate, b.tax_amount, b.total_due,
        b.status,
        b.due_date,
        b.dq_status
    FROM bills b;

    -- Payments fact
    DROP VIEW IF EXISTS v_bi_fact_payments;
    CREATE VIEW v_bi_fact_payments AS
    SELECT
        p.payment_id,
        p.bill_id,
        p.account_number,
        p.payment_date,
        CAST(STRFTIME('%Y%m%d', p.payment_date) AS INTEGER) AS payment_date_key,
        p.amount,
        p.method
    FROM payments p;
    """)
    conn.commit()

# -----------------------
# Main
# -----------------------
def main() -> None:
    customers, meters, readings, rates = load_csvs()

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        insert_base_tables(conn, customers, meters, readings, rates)

        n = build_bills(conn)

        # Summary views
        conn.executescript("""
        DROP VIEW IF EXISTS v_revenue_by_month;
        CREATE VIEW v_revenue_by_month AS
        SELECT substr(period_end, 1, 7) AS month, SUM(total_due) AS revenue
        FROM bills
        GROUP BY substr(period_end, 1, 7)
        ORDER BY month;

        DROP VIEW IF EXISTS v_usage_stats;
        CREATE VIEW v_usage_stats AS
        SELECT substr(period_end,1,7) AS month,
               AVG(usage_m3) AS avg_usage_m3,
               MIN(usage_m3) AS min_usage_m3,
               MAX(usage_m3) AS max_usage_m3
        FROM bills
        GROUP BY substr(period_end,1,7)
        ORDER BY month;
        """)
        conn.commit()

        # Payments + aging
        ensure_due_dates(conn, net_days=21)
        ensure_payments_schema(conn)
        seed_demo_payments(conn)          # optional; comment out to skip seeding
        create_balance_and_aging_views(conn)

        # Auto-update bill status from balances
        update_bill_status_from_balances(conn)

        # Rate audit views (shows which rate was used or missing)
        create_rate_audit_views(conn)

        # Data Quality v2 (gaps + outliers + dq_status + summary views)
        run_dq_checks(conn)

        # Aging snapshots (point-in-time delinquency totals)
        create_aging_snapshot_schema(conn)
        take_aging_snapshot(conn)         # snapshot as of today

        # BI semantic layer views
        create_bi_model(conn)

        # Optional preview
        df_customers = pd.read_sql_query("SELECT * FROM customers LIMIT 5;", conn)
        print(df_customers)
        df_bills = pd.read_sql_query("SELECT * FROM bills LIMIT 5;", conn)
        print(df_bills)

    print(f"Database updated: {DB_PATH}")
    print("Tables: customers, meters, readings, rates, bills, payments, dq_findings, aging_snapshot")
    print("Views:  v_revenue_by_month, v_usage_stats, v_bill_balances, v_aging_detail, v_aging_rollup, "
          "v_aging_by_account, v_dq_summary, v_dq_bill_status, v_rate_applied, v_rate_coverage_gaps, "
          "v_bi_dim_dates, v_bi_dim_customers, v_bi_dim_meters, v_bi_fact_bills, v_bi_fact_payments")
    print(f"Billed rows inserted: {n}")

if __name__ == "__main__":
    main()
