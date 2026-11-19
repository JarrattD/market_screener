"""Manual-only listing, location and ranking pipeline. No public data artifacts."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys

from screenerV3.negative_ev_provider import FMPClient
from screenerV3.uk_ci_fmp import fetch_financials, fetch_listing_profiles
from screenerV3.uk_ci_output import RATIO_KEYS, open_destination, publish, verify_destination, read_ownership
from screenerV3.uk_ci_score import score_company
from uk_ci_universe import OfficialSourceError, classify_hq, load_hq_overrides, load_universe

MARKETS = ("Main Market", "AIM", "Aquis")
OVERRIDES = Path(__file__).parent / "data" / "uk_ci_hq_overrides.json"


def _note_list(raw):
    if isinstance(raw, str):
        return [raw] if raw else []
    return [str(item) for item in (raw or ())]


def make_rows(listings, profiles, decisions, financials, ran_at, ownership=None):
    """Build stable, numeric rankings and keep unresolved HQs separately visible."""
    ownership = ownership or {}
    ranked, review = [], []
    by_market = {market: Counter() for market in MARKETS}
    snapshots = {market: set() for market in MARKETS}
    score_counts = Counter()
    usable_financial = ownership_missing = matched = 0
    for item in listings:
        profile = profiles.get(item.isin) or {}
        matched += bool(profile)
        hq = decisions[item.isin]
        counts = by_market.setdefault(item.market, Counter())
        counts[hq.status] += 1
        if getattr(item, "as_of", ""):
            snapshots.setdefault(item.market, set()).add(item.as_of)
        if hq.status == "out_of_scope":
            continue
        inputs = financials.get(item.isin) or {}
        symbol = inputs.get("symbol") or profile.get("symbol") or ""
        if hq.status == "review":
            review.append((item.ticker, item.company, item.market, item.isin, item.kind,
                           hq.country, hq.evidence, symbol, item.source_url))
            continue
        if hq.status != "in_scope":
            raise ValueError("Unknown HQ classification")
        score_inputs = dict(inputs.get("score_inputs") or {})
        owned = ownership.get(item.isin)
        # A private, evidence-backed override is not a guess from FMP free float.
        if owned and owned["ticker"] == item.ticker.upper():
            score_inputs["ownership_pct"] = owned["pct"]
        else:
            owned = None
        if inputs.get("status") == "Inactive" or getattr(item, "status", "") == "Suspended":
            score_inputs["market_status"] = "Inactive"
        assessed = score_company(**score_inputs)
        ratio_notes = assessed["ratio_notes"]
        has_financial = any(v is not None for v in assessed["ratios"].values())
        usable_financial += has_financial
        ownership_missing += assessed["ownership_status"] == "Not found"
        has_score = has_financial or assessed["ownership_status"] in ("Owner", "FF")
        result_score = assessed["score"] if has_score else None
        score_counts[result_score] += 1
        notes = _note_list(inputs.get("notes") or inputs.get("issues"))
        if owned:
            notes.insert(0, f"Largest holder: {owned['holder']} {owned['pct']}% "
                           f"({owned['as_of']}); {owned['source']}")
        notes.extend(f"{key}: {value}" for key, value in ratio_notes.items() if value)
        notes = "; ".join(dict.fromkeys(notes))[:650]
        raw_status = inputs.get("status") or "FMP data unavailable"
        if not has_financial:
            status = str(raw_status) if not has_score else "Ownership only"
        elif not all(v is not None for v in assessed["ratios"].values()):
            status = "Partial financial data"
        else:
            status = "Six ratios available"
        if inputs.get("notes") and has_financial:
            status += "; see notes"
        if assessed["ownership_status"] == "Not found" and has_financial:
            status += "; ownership not found"
        market_status = score_inputs.get("market_status")
        display_ratios = tuple(
            assessed["ratios"][k] if assessed["ratios"][k] is not None else
            (market_status if market_status in ("Check", "Inactive") else "")
            for k in RATIO_KEYS
        )
        row = (result_score, item.company, item.ticker, item.market, item.isin, item.kind,
               hq.country, hq.evidence, assessed["financial_points"], assessed["ownership_point"],
               assessed["ownership_status"], *display_ratios, symbol, status, notes, item.source_url)
        bands = {12 + i: assessed["ratio_bands"][key].lower() for i, key in enumerate(RATIO_KEYS)}
        bands[11] = assessed["ownership_band"].lower()
        ranked.append((row, bands))
    ranked.sort(key=lambda pair: (
        pair[0][0] is None, -(pair[0][0] or 0), pair[0][1].casefold(),
        pair[0][2].casefold(), pair[0][4],
    ))
    review.sort(key=lambda row: (row[1].casefold(), row[0].casefold(), row[3]))
    ranked_rows, bands = [], []
    for row, fills in ranked:
        rank_num = len(ranked_rows) + 1 if row[0] is not None else None
        ranked_rows.append((rank_num, *row))
        bands.append(fills)
    coverage = [
        ("Last completed run (UTC)", ran_at),
        ("Official issuer universe", len(listings)),
        ("FMP profiles matched", matched),
        ("In-scope head office", len(ranked_rows)),
        ("HQ needs review", len(review)),
        ("Out of scope", sum(c["out_of_scope"] for c in by_market.values())),
        ("At least one financial ratio available", usable_financial),
        ("In-scope ownership not found", ownership_missing),
        ("Not scored", score_counts[None]),
        *[(f"Score {score}/7", score_counts[score]) for score in range(7, -1, -1)],
        *[(f"{market} | {label}", by_market[market][state])
          for market in MARKETS
          for state, label in (("in_scope", "in scope"), ("review", "HQ review"),
                               ("out_of_scope", "out of scope"))],
        *[(f"{market} | listing source date", ", ".join(sorted(snapshots[market]))) for market in MARKETS],
        ("HQ criterion", "Real group headquarters in UK, Jersey or Guernsey; not just registered office."),
        ("Listing criterion", "LSE Main Market, AIM or Aquis equity. Funds and REITs included; ETFs and debt excluded."),
        ("Score", "Six financial ratio points plus verified ownership. Missing ownership gets no point."),
        ("Ownership Evidence", "Private reviewed evidence; supply current largest beneficial holder, not a nominee."),
        ("Fast flow", "Rank matched usable data provisionally; warnings stay visible for deeper research."),
        ("Blank score", "No supported ratio or ownership evidence; not a low score."),
        ("Published data", "Refresh replaces the generated tables. Keep personal review notes in a separate tab."),
    ]
    return ranked_rows, review, coverage, bands


async def build(ownership=None):
    listings = await asyncio.to_thread(load_universe)
    if not listings:
        raise RuntimeError("No official listings; prior workbook left alone")
    overrides = load_hq_overrides(OVERRIDES)
    async with FMPClient() as client:
        profiles = await fetch_listing_profiles(client, listings)
        if sum(bool(p) for p in profiles.values()) < len(listings) * .5:
            raise RuntimeError("FMP profile coverage fell below 50%; prior workbook left alone")
        decisions = {row.isin: classify_hq(row, profiles.get(row.isin), overrides) for row in listings}
        in_scope = {row.isin: profiles[row.isin] for row in listings
                    if decisions[row.isin].status == "in_scope" and profiles.get(row.isin)}
        financials = await fetch_financials(client, in_scope)
    return make_rows(listings, profiles, decisions, financials,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), ownership)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Do not connect to or write to Google Sheets")
    args = parser.parse_args(argv)
    try:
        # Never put security-level research, data or raw service errors in this public repo's run logs.
        logging.disable(logging.CRITICAL)
        ownership = {}
        if not args.dry_run:
            sheet = open_destination()
            verify_destination(sheet)
            ownership = read_ownership(sheet)
        ranked, review, coverage, bands = asyncio.run(build(ownership))
        if not ranked or not any(row[1] is not None for row in ranked):
            raise RuntimeError("No rankable in-scope issuers; prior workbook left alone")
        if not args.dry_run:
            publish(sheet, ranked, review, coverage, bands)
        print("Dry run completed." if args.dry_run else "Private ranking and coverage verified.")
        return 0
    except OfficialSourceError as exc:
        # This exception includes only a fixed source label and a safe failure category.
        print(f"UK & CI screen stopped: {exc}. No results published.", file=sys.stderr)
        return 1
    except Exception as exc:
        # FMP/GSpread errors may include credentials, symbols, or Sheet metadata.
        print(f"UK & CI screen stopped ({type(exc).__name__}). Check the workflow setup and data sources.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
