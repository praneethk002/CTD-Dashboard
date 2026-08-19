"""
core/commentary.py

Turns an already-computed report object (core/reporting.py) into a short
natural-language commentary — either via the Anthropic API, or via a
deterministic template if no API key is available.

Hard rule: the model is NEVER asked to calculate anything. It is given
already-computed numbers and asked only to phrase them. If ANTHROPIC_API_KEY
is missing or the API call fails, generate_commentary() falls back to
template_fallback() rather than raising — the rest of the application must
keep working without an API key.

Every response is labeled with its source ("ai_generated" or
"template_fallback") so the caller/UI never has to guess which one it got.
"""

import anthropic

MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are a fixed-income performance-reporting assistant.
You will be given a JSON object of ALREADY-CALCULATED portfolio performance
metrics (returns, contributors, attribution, risk metrics). Your only job is
to write a concise 2-4 sentence commentary suitable for a morning
performance report.

Rules:
- Use ONLY the numbers provided. Never invent, estimate, or restate a
  number differently than given.
- Lead with the active return and its sign.
- Name the top contributor and top detractor by label if present.
- Mention the maturity-bucket driver if the attribution data supports it.
- Do not mention that you are an AI or that this is generated commentary.
- Plain, desk-ready prose. No bullet points, no markdown.
"""


def build_commentary_prompt(report: dict) -> str:
    """Serialize the relevant slice of a report object into a compact prompt."""
    import json
    payload = {
        "horizon": report["horizon"],
        "as_of": report["as_of"],
        "performance": report["performance"],
        "risk_metrics": report["risk_metrics"],
        "top_positive_contributors": report["contributors"]["top_positive"],
        "top_negative_contributors": report["contributors"]["top_negative"],
        "bucket_attribution": [
            {k: v for k, v in b.items() if k != "securities"}
            for b in report["attribution_summary"]["buckets"]
        ],
        "data_quality_status": report["data_quality"]["status"],
    }
    return (
        "Here are the calculated metrics for this period. Write the commentary "
        "described in your system instructions, using only these numbers:\n\n"
        + json.dumps(payload, indent=2, default=str)
    )


def template_fallback(report: dict) -> str:
    """Deterministic, no-API-key commentary built from the same structured report."""
    perf = report["performance"]
    top_pos = report["contributors"]["top_positive"]
    top_neg = report["contributors"]["top_negative"]

    direction = "outperformed" if perf["active_return_bps"] >= 0 else "underperformed"
    sentence = (
        f"Portfolio {direction} the benchmark by {abs(perf['active_return_bps']):.1f} bps "
        f"over {report['horizon']} ({perf['portfolio_return'] * 100:.2f}% vs. "
        f"{perf['benchmark_return'] * 100:.2f}%)."
    )

    if top_pos:
        sentence += f" Top contributor: {top_pos[0]['label']} " \
                    f"({top_pos[0]['contribution_to_portfolio'] * 10_000:+.1f} bps)."
    if top_neg and top_neg[0]["contribution_to_portfolio"] < 0:
        sentence += f" Top detractor: {top_neg[0]['label']} " \
                    f"({top_neg[0]['contribution_to_portfolio'] * 10_000:+.1f} bps)."

    sentence += f" Data quality: {report['data_quality']['status']}."
    return sentence


def generate_commentary(report: dict, api_key: str | None) -> dict:
    """
    Returns {"text": str, "source": "ai_generated" | "template_fallback"}.
    Never raises for a missing/invalid key — falls back to a template instead.
    """
    if not api_key:
        return {"text": template_fallback(report), "source": "template_fallback"}

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_commentary_prompt(report)}],
        )
        text = " ".join(b.text for b in response.content if hasattr(b, "text")).strip()
        if not text:
            return {"text": template_fallback(report), "source": "template_fallback"}
        return {"text": text, "source": "ai_generated"}
    except Exception:
        return {"text": template_fallback(report), "source": "template_fallback"}
