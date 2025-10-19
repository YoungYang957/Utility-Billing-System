# Utility-Billing-System

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
