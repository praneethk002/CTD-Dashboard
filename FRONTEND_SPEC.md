# Frontend Spec — Fixed Income Performance & Analytics Workbench

For the Lovable rebuild. Backend is a Flask API at `http://localhost:5001`
(update the base URL once deployed). All portfolio data is synthetic and
should be labeled as such somewhere visible in the UI (e.g. a small
"synthetic demo portfolio" badge near the header) — do not imply this is
real account data.

**Visual tone:** institutional, restrained, data-dense only where it earns
it. No RBC branding (this is a personal portfolio piece, not an RBC
product). No fake-Bloomberg gradients/neon. Think: a clean internal
analytics tool, not a marketing page. Very little explanatory copy — the
numbers and structure should carry the meaning.

**Progressive disclosure principle:** default view answers "what
happened?" A user should never need to understand Treasury futures basis
mechanics to use the first three tabs. The CTD/basis engine is available
but lives behind an explicit "Advanced" entry point, not the landing view.

---

## Global

- **Horizon selector**: 1D / MTD / QTD / YTD, persists across all four
  tabs below (single global control, not per-tab).
- **Data-quality status chip**: always visible somewhere global (header or
  persistent sidebar) — shows `PASS` (quiet, green/neutral) or `N items
  need review` (attention color). Clicking it jumps to the Data Quality tab.
  Source: `GET /api/portfolio/controls` → `{status, issues}`.

---

## A. Portfolio Overview — "What happened?"

**Endpoint:** `GET /api/portfolio/performance?horizon={h}`

```json
{
  "horizon": "YTD",
  "as_of": "2026-08-19",
  "performance": {
    "portfolio_return": 0.0193,
    "benchmark_return": 0.0202,
    "active_return_bps": -8.44,
    "portfolio_mv_begin": 10075849.25,
    "portfolio_mv_end": 10059344.15,
    "benchmark_mv_begin": 10036399.10,
    "benchmark_mv_end": 10029319.80,
    "portfolio_coupon_cash": 211406.25,
    "benchmark_coupon_cash": 209687.50
  },
  "risk_metrics": {
    "weighted_modified_duration": 6.81,
    "weighted_yield": 0.0426,
    "dv01_usd": 6742.75
  }
}
```

**Layout:** a hero row of stat tiles — Portfolio Return, Benchmark Return,
Active Return (bps, signed, colored by sign), Market Value, Weighted
Duration, DV01 ($). Active return is the single most important number on
this screen — give it the most visual weight. One line of text max, e.g.
the `executive_summary.headline` string from `GET /api/portfolio/report`
if you want a plain-language sentence instead of composing your own.

**Also useful here:** `GET /api/portfolio/summary` for the holdings table
(security, classification, weight, price, yield, duration, DV01) if the
Overview tab includes a compact holdings list below the hero tiles.

---

## B. Performance Drivers — "Why did it happen?"

**Endpoints:**
- `GET /api/portfolio/contributors?horizon={h}` → top 3 positive / top 3
  negative security contributors.
- `GET /api/portfolio/attribution?horizon={h}&level=bucket` → bucket-level
  breakdown + portfolio-vs-benchmark comparison.
- `GET /api/portfolio/security/{id}?horizon={h}` → single-security detail
  when a user drills in.

```json
// contributors
{
  "horizon": "YTD",
  "contributors": {
    "top_positive": [
      {"security_id": "DEMO-UST-10Y", "label": "UST 10Y", "weight": 0.205,
       "total_return": 0.0202, "carry": 0.0273, "price_effect_approx": -0.0071,
       "residual": 0.00008, "contribution_to_portfolio": 0.00415}
    ],
    "top_negative": [ /* same shape */ ]
  }
}

// attribution (level=bucket)
{
  "attribution": {
    "buckets": [
      {"bucket": "Long (10Y+)", "weight": 0.10, "contribution_to_portfolio": 0.00081,
       "carry": 0.0295, "price_effect_approx": -0.0218, "residual": 0.00046,
       "securities": [ /* per-security contribution dicts, same shape as above */ ]}
    ],
    "portfolio_vs_benchmark": [
      {"bucket": "Long (10Y+)", "portfolio_contribution": 0.00081,
       "benchmark_contribution": 0.00057, "difference": 0.00024}
    ]
  }
}
```

**Layout:** drilldown portfolio → bucket → security.
1. Bucket-level bar chart: portfolio contribution vs. benchmark
   contribution, side by side per bucket (from `portfolio_vs_benchmark`).
2. Within a selected bucket, a stacked/grouped bar per security showing
   carry vs. price_effect_approx vs. residual (all in bps — multiply by
   10,000 for display). **Label `price_effect_approx` as an approximation**
   somewhere visible (tooltip is fine) — don't present it as exact.
3. Top contributors / detractors list (from `contributors`), each row
   clickable → security detail view using `/api/portfolio/security/{id}`.

The `residual` should be visually present, not hidden — a thin gray
segment is fine, but don't drop it from the stack. If a user asks "why
doesn't carry + price effect equal the total," the residual bar is the
honest answer, and the security-detail view can show the exact
reconciliation (`total_return == carry + price_effect_approx + residual`).

**Note on the "Unclassified" bucket:** because the demo data seeds one
security (`DEMO-UST-20Y`) with a missing classification (a deliberate
data-quality issue, not a display bug), it appears under `bucket:
"Unclassified"` rather than under `"Long (10Y+)"`. This is intentional —
it's a live example of why the Data Quality tab matters for attribution.
Don't silently reclassify it in the frontend; if anything, a small note
("this bucket exists because of a flagged data-quality issue — see Data
Quality") makes the point for you.

---

## C. Data Quality — "Can I trust the numbers?"

**Endpoint:** `GET /api/portfolio/controls`

```json
{
  "status": "REVIEW",
  "issues": [
    {"issue": "Portfolio Weights do not sum to 100%", "security_id": null,
     "reason": "portfolio_weight totals 100.50%...", "severity": "REVIEW",
     "suggested_action": "Reconcile weights against the source holdings file..."},
    {"issue": "Stale price", "security_id": "DEMO-UST-05YB",
     "reason": "Price (98.734) and yield (0.04074) unchanged from prior snapshot...",
     "severity": "REVIEW", "suggested_action": "Confirm the price feed refreshed..."},
    {"issue": "Missing classification", "security_id": "DEMO-UST-20Y",
     "reason": "No maturity-bucket / asset-class classification on file.",
     "severity": "REVIEW", "suggested_action": "Assign a classification before..."}
  ]
}
```

**Layout:** exception-oriented, per the UX principle — never render a wall
of 20 green checkmarks.
- **Clean state:** a single quiet confirmation, "All controls passed."
- **Issues present:** a single line, "3 items need review," expandable
  into a list. Each row: issue name, affected security (or "portfolio-
  level" if `security_id` is null), one-line reason, severity badge,
  suggested action. `severity: "INFO"` items (e.g. upcoming coupon events)
  should read as informational, not alarming — visually distinct from
  `"REVIEW"`.
- Clicking an issue with a `security_id` should deep-link to that
  security's detail view (tab B).

---

## D. Report / Commentary — "How do I communicate it?"

**Endpoints:**
- `GET /api/portfolio/report?horizon={h}` → the full structured report
  (performance + risk_metrics + contributors + attribution_summary +
  data_quality + executive_summary), everything the other three tabs show,
  in one payload — useful if you want to render a single-page "report
  view" rather than re-fetching per section.
- `POST /api/portfolio/commentary` with body `{"horizon": "YTD"}` →
  `{"text": "...", "source": "ai_generated" | "template_fallback"}`.

**Layout:** a clean, printable-feeling report preview — executive summary
headline, key stats, top contributors/detractors, data-quality status —
followed by the commentary text. **Always show which kind of commentary it
is** (e.g. a small "AI-drafted" or "template" tag next to the text) — never
present template commentary as if it came from the model, or vice versa.
If straightforward, a CSV/text export button is a nice-to-have; don't
over-invest in PDF generation.

---

## Advanced: Treasury Futures & CTD Risk

Not one of the four main tabs — a separate, clearly-secondary entry point
(e.g. a link/tab labeled "Advanced Fixed Income Analytics" that a curious
reviewer can find, not something competing with the main flow).

Reframe the existing CTD dashboard's headline concepts in plain English
before showing the technical detail:
- Instead of "CTD transition threshold F*": *"Which Treasury is cheapest
  to deliver, and how close are we to that changing?"*
- Instead of "implied repo ranking": *"Which bond is most profitable to
  buy, finance, and deliver into the futures contract?"*

Existing endpoints (`/api/basket`, `/api/history/<cusip>`, `/api/percentile`,
`/api/transitions`, `/api/proximity`, `/api/scenarios`, `/api/threshold`,
`/api/carry/<cusip>`) are all unchanged and can back this section directly
— reuse the panel logic from `ui/index.html` as a reference for what each
one renders, then let the technical details (net basis ticks, conversion
factors, F* formula) expand underneath the plain-English framing rather
than leading with them.
