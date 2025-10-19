# app.py
import sqlite3
from contextlib import closing
from pathlib import Path
import pandas as pd
import streamlit as st
import altair as alt

DB_PATH = Path("C:/Users/jinyu/OneDrive/Desktop/Wyse_project/cis_demo.sqlite")

@st.cache_data(show_spinner=False)
def run_sql(query: str, params: tuple | None = None) -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return pd.read_sql_query(query, conn, params=params)

st.set_page_config(page_title="Utility CIS Demo", layout="wide")

st.title("Utility CIS Demo Dashboard")

# -------------------- Filters --------------------
with st.sidebar:
    st.header("Filters")
    # dynamic months from bills
    months = run_sql("SELECT DISTINCT substr(period_end,1,7) AS m FROM bills ORDER BY m;")["m"].tolist()
    sel_months = st.multiselect("Billing Months (YYYY-MM)", months, default=months)
    accts = run_sql("SELECT DISTINCT account_number FROM customers ORDER BY account_number;")["account_number"].tolist()
    sel_acct = st.selectbox("Account (optional)", ["(All)"] + accts, index=0)

# Build WHERE clause
where = []
params = []
if sel_months:
    where.append(f"substr(period_end,1,7) IN ({','.join(['?']*len(sel_months))})")
    params += sel_months
if sel_acct and sel_acct != "(All)":
    where.append("account_number = ?")
    params.append(sel_acct)
where_sql = ("WHERE " + " AND ".join(where)) if where else ""

# -------------------- KPIs --------------------
kpi = run_sql(f"""
SELECT
  ROUND(SUM(total_due),2)  AS revenue,
  ROUND(AVG(total_due),2)  AS avg_bill,
  SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS paid_bills,
  SUM(CASE WHEN status='open' OR status='partial' THEN 1 ELSE 0 END) AS unpaid_bills
FROM bills
{where_sql};
""", tuple(params))

col1, col2, col3, col4 = st.columns(4)
col1.metric("Revenue (selected)", f"${kpi.at[0,'revenue'] or 0:,.2f}")
col2.metric("Avg Bill", f"${kpi.at[0,'avg_bill'] or 0:,.2f}")
col3.metric("Paid Bills", int(kpi.at[0,'paid_bills'] or 0))
col4.metric("Unpaid/Partial Bills", int(kpi.at[0,'unpaid_bills'] or 0))

# -------------------- Trends --------------------
rev_trend = run_sql(f"""
SELECT substr(period_end,1,7) AS month, SUM(total_due) AS revenue
FROM bills
{where_sql}
GROUP BY substr(period_end,1,7)
ORDER BY month;
""", tuple(params))

usage_trend = run_sql(f"""
SELECT substr(period_end,1,7) AS month, AVG(usage_m3) AS avg_usage
FROM bills
{where_sql}
GROUP BY substr(period_end,1,7)
ORDER BY month;
""", tuple(params))

c1, c2 = st.columns(2)

c1.subheader("Revenue by Month")
if not rev_trend.empty:
    chart_rev = alt.Chart(rev_trend).mark_line(point=True).encode(
        x=alt.X('month:N', sort=None, title='Month'),
        y=alt.Y('revenue:Q', title='Revenue ($)'),
        tooltip=['month','revenue']
    )
    c1.altair_chart(chart_rev, use_container_width=True)
else:
    c1.info("No data for selected filters.")

c2.subheader("Average Usage by Month")
if not usage_trend.empty:
    chart_usage = alt.Chart(usage_trend).mark_line(point=True).encode(
        x=alt.X('month:N', sort=None, title='Month'),
        y=alt.Y('avg_usage:Q', title='Avg Usage (m³)'),
        tooltip=['month','avg_usage']
    )
    c2.altair_chart(chart_usage, use_container_width=True)
else:
    c2.info("No data for selected filters.")

# -------------------- Aging --------------------
st.subheader("Aging Buckets")
aging = run_sql("""
SELECT bucket, bills, balance_total
FROM v_aging_rollup
ORDER BY CASE bucket
  WHEN 'paid' THEN 0 WHEN 'current' THEN 1 WHEN '1-30' THEN 2
  WHEN '31-60' THEN 3 WHEN '61-90' THEN 4 ELSE 5 END;
""")
aging_display = aging.copy()
aging_display["balance_total"] = aging_display["balance_total"].fillna(0.0)
st.dataframe(aging_display, use_container_width=True)
if not aging.empty:
    chart_age = alt.Chart(aging_display).mark_bar().encode(
        x=alt.X("bucket:N", sort=['paid','current','1-30','31-60','61-90','90+']),
        y=alt.Y("balance_total:Q", title="Balance"),
        tooltip=['bucket','bills','balance_total']
    )
    st.altair_chart(chart_age, use_container_width=True)

# -------------------- Segmentation --------------------
st.subheader("Revenue by Postal Code")
rev_postal = run_sql(f"""
SELECT c.postal_code, ROUND(SUM(b.total_due),2) AS revenue
FROM bills b
LEFT JOIN customers c USING(account_number)
{where_sql.replace('period_end','b.period_end').replace('account_number','b.account_number')}
GROUP BY c.postal_code
ORDER BY revenue DESC;
""", tuple(params))
if not rev_postal.empty:
    chart_pc = alt.Chart(rev_postal).mark_bar().encode(
        x=alt.X('postal_code:N', sort='-y', title='Postal Code'),
        y=alt.Y('revenue:Q', title='Revenue ($)'),
        tooltip=['postal_code','revenue']
    )
    st.altair_chart(chart_pc, use_container_width=True)
st.dataframe(rev_postal, use_container_width=True)

# -------------------- Rate Audit --------------------
st.subheader("Rate Audit (applied vs gaps)")
rate_applied = run_sql("SELECT * FROM v_rate_applied ORDER BY period_end, bill_id;")
st.dataframe(rate_applied, use_container_width=True, height=250)
rate_gaps = run_sql("SELECT * FROM v_rate_coverage_gaps ORDER BY period_end, bill_id;")
if not rate_gaps.empty:
    st.warning("Some bills are missing a valid rate for their period_end.")
    st.dataframe(rate_gaps, use_container_width=True, height=200)

# -------------------- Drill-through --------------------
st.subheader("Bill Drill-through")
bill_ids = run_sql("SELECT bill_id FROM bills ORDER BY bill_id;")["bill_id"].tolist()
sel_bill = st.selectbox("Select bill_id", bill_ids if bill_ids else [None])
if sel_bill:
    b = run_sql("SELECT * FROM bills WHERE bill_id = ?;", (int(sel_bill),))
    st.dataframe(b, use_container_width=True)
    pays = run_sql("SELECT * FROM payments WHERE bill_id = ? ORDER BY payment_date;", (int(sel_bill),))
    st.write("Payments")
    st.dataframe(pays if not pays.empty else pd.DataFrame(columns=["(none)"]), use_container_width=True)
