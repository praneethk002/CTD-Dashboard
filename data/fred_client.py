"""
data/fred_client.py

FRED (Federal Reserve Economic Data) yield curve client.

Fetches daily Treasury yields for 6 maturities:
  DGS3MO  — 3-month
  DGS2    — 2-year
  DGS5    — 5-year
  DGS7    — 7-year
  DGS10   — 10-year
  DGS30   — 30-year

These are used to interpolate a yield for each basket bond
based on its maturity. This is a first-order approximation —
production would use Bloomberg BDS for per-bond yields.

FRED API:
  Base URL: https://api.stlouisfed.org/fred/series/observations
  Free API key: register at https://fred.stlouisfed.org/docs/api/api_key.html
  Rate limit: 120 requests/minute — well within our usage

Caching:
  Results cached in memory for 5 minutes to avoid redundant API calls
  during the same session.
"""

import os
import time
from datetime import date, timedelta
from typing import Optional

import urllib.request
import json

# FRED series IDs mapped to maturity in years
FRED_SERIES = {
    "DGS3MO": 0.25,
    "DGS2":   2.0,
    "DGS5":   5.0,
    "DGS7":   7.0,
    "DGS10":  10.0,
    "DGS30":  30.0,
}

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# simple in-memory cache: {series_id: (timestamp, value)}
_cache: dict[str, tuple[float, float]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


def get_yield_curve(api_key: Optional[str] = None) -> dict[str, float]:
    """
    Fetch the current Treasury yield curve from FRED.

    Returns dict of {maturity_years: yield_decimal} for the 6 series.
    Falls back to a realistic default curve if FRED is unavailable
    (no API key, network error, or FRED returns no data).

    Args:
        api_key: FRED API key. If None, reads from FRED_API_KEY env var.
                 If still None, returns default curve.

    Returns:
        dict of {maturity_years: yield_decimal}
        e.g. {0.25: 0.0532, 2.0: 0.0487, 5.0: 0.0455, ...}
    """
    key = api_key or os.environ.get("FRED_API_KEY")

    if not key:
        return _default_curve()

    curve = {}
    for series_id, maturity_yrs in FRED_SERIES.items():
        yield_val = _fetch_series(series_id, key)
        if yield_val is not None:
            curve[maturity_yrs] = yield_val

    # if we got fewer than 4 points, fall back to default
    if len(curve) < 4:
        return _default_curve()

    return curve


def interpolate_yield(curve: dict[str, float], target_maturity_yrs: float) -> float:
    """
    Linearly interpolate a yield for a bond's specific maturity.

    Uses the two nearest points in the curve that bracket the target maturity.
    Extrapolates flat beyond the curve endpoints.

    Args:
        curve:                output of get_yield_curve()
        target_maturity_yrs:  bond's remaining maturity in years (e.g. 7.3)

    Returns:
        interpolated yield as decimal (e.g. 0.0461)
    """
    maturities = sorted(curve.keys())
    yields     = [curve[m] for m in maturities]

    # extrapolate flat below shortest maturity
    if target_maturity_yrs <= maturities[0]:
        return yields[0]

    # extrapolate flat above longest maturity
    if target_maturity_yrs >= maturities[-1]:
        return yields[-1]

    # find bracketing points and interpolate linearly
    for i in range(len(maturities) - 1):
        m_low, m_high = maturities[i], maturities[i + 1]
        if m_low <= target_maturity_yrs <= m_high:
            t = (target_maturity_yrs - m_low) / (m_high - m_low)
            return yields[i] + t * (yields[i + 1] - yields[i])

    return yields[-1]


def build_basket_yields(
    curve: dict[str, float],
    basket: list[dict],
    settlement: date,
) -> dict[str, float]:
    """
    Interpolate a yield for every bond in the delivery basket.

    Args:
        curve:      output of get_yield_curve()
        basket:     output of core.basket.get_basket()
        settlement: today's settlement date

    Returns:
        dict of {cusip: yield_decimal}
    """
    yields = {}
    for bond in basket:
        remaining_years = (bond["maturity"] - settlement).days / 365.25
        yields[bond["cusip"]] = interpolate_yield(curve, remaining_years)
    return yields


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _fetch_series(series_id: str, api_key: str) -> Optional[float]:
    """
    Fetch the most recent observation for a FRED series.

    Uses in-memory cache to avoid redundant requests within 5 minutes.
    Returns yield as decimal (FRED returns percentage strings e.g. "4.52").
    """
    cached = _cache.get(series_id)
    if cached and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    # request last 5 days to handle weekends/holidays when FRED has no data
    observation_start = (date.today() - timedelta(days=5)).isoformat()

    url = (
        f"{FRED_BASE_URL}"
        f"?series_id={series_id}"
        f"&api_key={api_key}"
        f"&file_type=json"
        f"&sort_order=desc"
        f"&limit=1"
        f"&observation_start={observation_start}"
    )

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())

        observations = data.get("observations", [])
        if not observations:
            return None

        value_str = observations[0]["value"]
        if value_str == ".":
            return None  # FRED uses "." for missing data

        yield_pct = float(value_str)
        yield_dec = yield_pct / 100  # convert from percentage to decimal

        _cache[series_id] = (time.time(), yield_dec)
        return yield_dec

    except Exception:
        return None


def _default_curve() -> dict[str, float]:
    """
    Realistic default yield curve when FRED is unavailable.
    Based on approximate March 2026 market levels.
    """
    return {
        0.25: 0.0532,   # 3M: 5.32%
        2.0:  0.0487,   # 2Y: 4.87%
        5.0:  0.0455,   # 5Y: 4.55%
        7.0:  0.0448,   # 7Y: 4.48%
        10.0: 0.0445,   # 10Y: 4.45%
        30.0: 0.0462,   # 30Y: 4.62%
    }
