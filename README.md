# Utility-Billing-System
### System Overview
<pre> ```             
       ┌──────────────────────────┐
       │        CSV Inputs        │
       │ customers / meters /     │
       │ readings / rates         │
       └──────────┬───────────────┘
                  │
                  ▼
      ┌───────────────────────────┐
      │        ETL Load           │
      │ load_csvs() + insert_*    │
      └──────────┬────────────────┘
                  │
                  ▼
   ┌──────────────────────────────┐
   │ Derive Period Usage          │
   │ compute_periods()            │
   │ groupby + shift on readings  │
   └──────────┬───────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │  Rate Selection (Rates v2)   │
   │ get_rate_for_period_end()    │
   │ pick latest rate <= period   │
   └──────────┬───────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │ Tiered Pricing               │
   │ price_usage()               │
   │ fixed + tier1/2/3 + tax      │
   └──────────┬───────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │ Build Bills                  │
   │ build_bills()               │
   │ creates `bills` table        │
   └──────────┬───────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │ Payments + Aging Views      │
   │ payments, aging_rollup     │
   │ tracks overdue balances    │
   └──────────┬──────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │ Rate Audit Views            │
   │ v_rate_applied, coverage   │
   │ QA check for rates        │
   └──────────┬──────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │ Data Quality (Gaps/Outliers) │
   │ dq_findings, dq_status       │
   └──────────┬──────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │ BI Semantic Layer            │
   │ Fact & Dim tables           │
   │ for Power BI / Tableau      │
   └──────────┬──────────────────┘
              │
              ▼
   ┌──────────────────────────────┐
   │ Streamlit Dashboard         │
   │ KPIs, trends, aging chart  │
   │ drill through              │
   └────────────────────────────┘
``` </pre>


<img width="1538" height="599" alt="Screenshot 2025-10-19 172716" src="https://github.com/user-attachments/assets/29cf3847-b426-4c93-aaa9-939ad06ddf1d" />
