#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Mode Shadow uniquement.
- Lit Catalyst et SEC EDGAR en lecture seule.
- Détecte les publications financières primaires.
- Extrait Revenue/EPS via SEC Company Facts.
- Contrôle les périodes XBRL et la période N-1 comparable.
- Corrobore Revenue avec Finnhub.
- Conserve EPS Finnhub/consensus à titre informatif tant que sa base
  GAAP/ajustée n'est pas explicitée.
- Persiste les observations par EventID sans doublons.

Aucun impact sur V1 ou sur le portefeuille V1.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import (
    AGENT_VERSION,
    CATALYST_EVENTS_FILE,
    CATALYST_SEC_FILINGS_FILE,
    CATALYST_SEMANTIC_FILE,
    EARNINGS_RESULTS_FILE,
    OBSERVATION_ONLY,
    PORTFOLIO_V1_CHANGED,
    STOCKS_FILE,
    V1_EXECUTION_CHANGED,
)

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "investment-assistant personal-research contact@example.com",
).strip()
SEC_COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_EARNINGS_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/earnings"
FINNHUB_DATE_WINDOW_DAYS = 10
REVENUE_TOLERANCE_PCT = 2.0
REQUEST_TIMEOUT = 25
REQUEST_PAUSE = 0.15

CONDITIONAL_FORMS = {"8-K", "6-K"}
EARNINGS_WORDS = re.compile(
    r"\b(earnings|results?|financial results?|quarterly results?|annual results?|"
    r"income|revenue|sales|guidance|outlook|chiffre d.?affaires|résultats?|resultats?)\b",
    re.IGNORECASE,
)
REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
DILUTED_EPS_CONCEPTS = (
    "EarningsPerShareDiluted",
    "IncomeLossFromContinuingOperationsPerDilutedShare",
)
BASIC_EPS_CONCEPTS = (
    "EarningsPerShareBasic",
    "IncomeLossFromContinuingOperationsPerBasicShare",
)
RESULT_COLUMNS = [
    "EventID", "AgentVersion", "Ticker", "ObservedAtUTC", "PublishedAtUTC",
    "EventType", "Period", "RevenueActual", "RevenueConsensus",
    "RevenuePrevious", "RevenueSurprisePct", "RevenueGrowthPct", "EPSActual",
    "EPSConsensus", "EPSPrevious", "EPSSurprisePct", "GuidanceStatus",
    "KeyKPIName", "KeyKPIActual", "KeyKPIConsensus", "KeyKPIPrevious",
    "KeyKPIMomentum", "FundamentalAssessment", "ExpectationAssessment",
    "OverallVerdict", "Confidence", "DataStatus", "NeedsHumanReview",
    "ObservationOnly", "V1ExecutionChanged", "PortfolioV1Changed",
]


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def normalize(value: Any) -> str:
    return clean(value).upper().replace("_", "-")


def as_bool(value: Any) -> bool:
    return clean(value).upper() in {"TRUE", "1", "YES", "OUI"}


def finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_results_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_tickers() -> list[str]:
    if not STOCKS_FILE.exists():
        raise FileNotFoundError(f"Fichier stocks introuvable : {STOCKS_FILE}")
    return [
        line.strip().upper()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def get_json(url: str, headers: dict[str, str]) -> Any:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body[:250]}") from error
    except URLError as error:
        raise RuntimeError(f"Erreur reseau: {error.reason}") from error


def get_sec_json(url: str) -> Any:
    return get_json(url, {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"})


def get_finnhub_json(url: str) -> Any:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY absent de l'environnement")
    return get_json(
        url,
        {"X-Finnhub-Token": FINNHUB_API_KEY, "Accept": "application/json"},
    )


def load_event_map() -> dict[str, dict[str, str]]:
    return {
        clean(row.get("EventID")): row
        for row in read_csv(CATALYST_EVENTS_FILE)
        if clean(row.get("EventID"))
    }


def semantic_candidates(
    watchlist: set[str], event_map: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for semantic in read_csv(CATALYST_SEMANTIC_FILE):
        ticker = clean(semantic.get("Ticker")).upper()
        event_id = clean(semantic.get("EventID"))
        if ticker not in watchlist or not event_id or not as_bool(semantic.get("IsRelevant")):
            continue
        catalyst_type = normalize(semantic.get("CatalystType"))
        category = normalize(semantic.get("Category"))
        if catalyst_type not in {"EARNINGS", "GUIDANCE"} and category not in {
            "EARNINGS", "GUIDANCE", "FINANCIAL-RESULTS", "FINANCIAL-UPDATE"
        }:
            continue
        event = event_map.get(event_id, {})
        rows.append({
            "EventID": event_id,
            "Ticker": ticker,
            "PublishedAtUTC": clean(semantic.get("FirstSeenUTC") or event.get("FirstSeenUTC")),
        })
    return rows


def classify_sec_filing(row: dict[str, str]) -> tuple[bool, str, str]:
    form = normalize(row.get("Form"))
    description = clean(row.get("Description"))
    if form == "10-Q":
        return True, "ACTUAL_QUARTERLY_FILING", "Formulaire trimestriel SEC"
    if form in {"10-K", "20-F", "40-F"}:
        return True, "ACTUAL_ANNUAL_FILING", "Formulaire annuel SEC"
    if form in CONDITIONAL_FORMS:
        if EARNINGS_WORDS.search(description):
            return True, "ACTUAL_EARNINGS_RELEASE", "Description SEC explicite"
        return False, "SEC_REVIEW", "8-K/6-K sans preuve Earnings dans Description"
    return False, "SEC_EXCLUDED", f"Formulaire non gere : {form}"


def sec_candidates(watchlist: set[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    retained: list[dict[str, str]] = []
    review: list[dict[str, str]] = []
    for filing in read_csv(CATALYST_SEC_FILINGS_FILE):
        ticker = clean(filing.get("Ticker")).upper()
        accession = clean(filing.get("AccessionNumber"))
        if ticker not in watchlist or not accession:
            continue
        keep, classification, reason = classify_sec_filing(filing)
        row = {
            "EventID": f"SEC|{ticker}|{accession}",
            "Ticker": ticker,
            "PublishedAtUTC": clean(filing.get("FiledAt")),
            "Classification": classification,
            "FilterReason": reason,
            "Form": clean(filing.get("Form")),
            "AccessionNumber": accession,
            "CIK": clean(filing.get("CIK")).zfill(10),
            "Description": clean(filing.get("Description")),
            "URL": clean(filing.get("FilingURL")),
        }
        (retained if keep else review).append(row)
    key = lambda x: (x["PublishedAtUTC"], x["Ticker"], x["EventID"])
    retained.sort(key=key, reverse=True)
    review.sort(key=key, reverse=True)
    return retained, review


def fact_units(
    companyfacts: dict[str, Any], concepts: tuple[str, ...], units: tuple[str, ...]
) -> tuple[str, str, list[dict[str, Any]]]:
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    for concept in concepts:
        fact = us_gaap.get(concept)
        if not isinstance(fact, dict):
            continue
        fact_units_map = fact.get("units", {})
        if not isinstance(fact_units_map, dict):
            continue
        for unit in units:
            entries = fact_units_map.get(unit)
            if isinstance(entries, list) and entries:
                return concept, unit, entries
    return "", "", []


def duration_days(row: dict[str, Any]) -> Optional[int]:
    try:
        return (
            date.fromisoformat(clean(row.get("end")))
            - date.fromisoformat(clean(row.get("start")))
        ).days
    except ValueError:
        return None


def fiscal_year_int(row: dict[str, Any]) -> Optional[int]:
    try:
        return int(clean(row.get("fy")))
    except (TypeError, ValueError):
        return None


def valid_duration_for_form(row: dict[str, Any], form: str) -> bool:
    duration = duration_days(row)
    form = normalize(form)
    if form == "10-Q":
        return duration is not None and 70 <= duration <= 110
    if form in {"10-K", "20-F", "40-F"}:
        return duration is not None and 330 <= duration <= 400
    return True


def select_current_fact(
    entries: list[dict[str, Any]], accession: str, form: str
) -> Optional[dict[str, Any]]:
    form = normalize(form)
    candidates = [
        row for row in entries
        if clean(row.get("accn")) == accession
        and (not clean(row.get("form")) or normalize(row.get("form")) == form)
        and valid_duration_for_form(row, form)
        and finite_float(row.get("val")) is not None
    ]
    if not candidates:
        return None

    def score(row: dict[str, Any]) -> tuple[int, int, str]:
        fp = clean(row.get("fp")).upper()
        duration = duration_days(row)
        if form == "10-Q":
            return (3 if fp in {"Q1", "Q2", "Q3"} else 0, -abs((duration or 91)-91), clean(row.get("end")))
        return (3 if fp == "FY" else 0, -abs((duration or 365)-365), clean(row.get("end")))

    return max(candidates, key=score)


def comparable_previous_fact(
    entries: list[dict[str, Any]], current: dict[str, Any], accession: str, form: str
) -> Optional[dict[str, Any]]:
    try:
        current_end = date.fromisoformat(clean(current.get("end")))
    except ValueError:
        return None
    current_fp = clean(current.get("fp")).upper()
    current_fy = fiscal_year_int(current)
    current_duration = duration_days(current)
    candidates: list[dict[str, Any]] = []
    for row in entries:
        if clean(row.get("accn")) == accession or finite_float(row.get("val")) is None:
            continue
        if not valid_duration_for_form(row, form):
            continue
        if clean(row.get("fp")).upper() != current_fp:
            continue
        if current_fy is not None and fiscal_year_int(row) != current_fy - 1:
            continue
        row_duration = duration_days(row)
        if current_duration is not None and row_duration is not None and abs(row_duration-current_duration) > 10:
            continue
        try:
            previous_end = date.fromisoformat(clean(row.get("end")))
        except ValueError:
            continue
        if 330 <= (current_end-previous_end).days <= 400:
            candidates.append(row)
    return max(candidates, key=lambda x: clean(x.get("filed"))) if candidates else None


def empty_metric(concept: str, unit: str) -> dict[str, Any]:
    return {
        "Concept": concept, "Unit": unit, "Actual": None, "Previous": None,
        "Start": "", "End": "", "FY": "", "FP": "", "DurationDays": None,
        "PreviousEnd": "", "PreviousFY": "", "PreviousFP": "",
        "Frame": "", "Accession": "", "Filed": "", "Form": "",
    }


def extract_metric(
    companyfacts: dict[str, Any], concepts: tuple[str, ...], units: tuple[str, ...],
    accession: str, form: str
) -> dict[str, Any]:
    concept, unit, entries = fact_units(companyfacts, concepts, units)
    current = select_current_fact(entries, accession, form)
    if current is None:
        return empty_metric(concept, unit)
    previous = comparable_previous_fact(entries, current, accession, form)
    return {
        "Concept": concept, "Unit": unit,
        "Actual": finite_float(current.get("val")),
        "Previous": finite_float(previous.get("val")) if previous else None,
        "Start": clean(current.get("start")), "End": clean(current.get("end")),
        "FY": clean(current.get("fy")), "FP": clean(current.get("fp")),
        "DurationDays": duration_days(current),
        "PreviousEnd": clean(previous.get("end")) if previous else "",
        "PreviousFY": clean(previous.get("fy")) if previous else "",
        "PreviousFP": clean(previous.get("fp")) if previous else "",
        "Frame": clean(current.get("frame")), "Accession": clean(current.get("accn")),
        "Filed": clean(current.get("filed")), "Form": clean(current.get("form")),
    }


def metric_period_is_current(metric: dict[str, Any], form: str, filed_at: str) -> bool:
    if metric["Actual"] is None:
        return True
    try:
        end_date = date.fromisoformat(clean(metric["End"]))
        filing_date = date.fromisoformat(clean(filed_at)[:10])
    except ValueError:
        return False
    age = (filing_date-end_date).days
    form = normalize(form)
    duration = metric["DurationDays"]
    fp = clean(metric["FP"]).upper()
    if form == "10-Q":
        return duration is not None and 70 <= duration <= 110 and fp in {"Q1","Q2","Q3"} and 0 <= age <= 130
    if form in {"10-K","20-F","40-F"}:
        return duration is not None and 330 <= duration <= 400 and fp == "FY" and 0 <= age <= 200
    return True


def previous_period_is_comparable(metric: dict[str, Any]) -> bool:
    if metric["Previous"] is None:
        return True
    try:
        current_end = date.fromisoformat(clean(metric["End"]))
        previous_end = date.fromisoformat(clean(metric["PreviousEnd"]))
        current_fy = int(clean(metric["FY"]))
        previous_fy = int(clean(metric["PreviousFY"]))
    except (TypeError, ValueError):
        return False
    return (
        330 <= (current_end-previous_end).days <= 400
        and previous_fy == current_fy-1
        and clean(metric["PreviousFP"]).upper() == clean(metric["FP"]).upper()
    )


def invalidate_metric(metric: dict[str, Any]) -> None:
    metric["Actual"] = metric["Previous"] = None
    metric["PreviousEnd"] = metric["PreviousFY"] = metric["PreviousFP"] = ""


def invalidate_previous(metric: dict[str, Any]) -> None:
    metric["Previous"] = None
    metric["PreviousEnd"] = metric["PreviousFY"] = metric["PreviousFP"] = ""


def metric_source_validation(metric: dict[str, Any], filing: dict[str, str]) -> tuple[str, str]:
    if metric["Actual"] is None:
        return "NOT_AVAILABLE", "Aucune valeur XBRL retenue"
    if clean(metric["Accession"]) != clean(filing["AccessionNumber"]):
        return "SOURCE_REVIEW_REQUIRED", "Accession XBRL non identique au filing"
    if metric["Form"] and normalize(metric["Form"]) != normalize(filing["Form"]):
        return "SOURCE_REVIEW_REQUIRED", "Formulaire XBRL non identique au filing"
    if not metric["End"] or metric["DurationDays"] is None:
        return "SOURCE_REVIEW_REQUIRED", "Période XBRL incomplète"
    return "SOURCE_VALIDATED", "Accession, formulaire et période cohérents"


def percentage_change(actual: Optional[float], previous: Optional[float]) -> Optional[float]:
    if actual is None or previous is None or previous == 0:
        return None
    return (actual/previous - 1.0) * 100.0


def infer_period(metric: dict[str, Any], form: str) -> str:
    fy, fp = clean(metric.get("FY")), clean(metric.get("FP")).upper()
    if fy and fp:
        return f"FY{fy}-{fp}"
    if normalize(form) == "10-Q":
        return "QUARTERLY"
    if normalize(form) in {"10-K","20-F","40-F"}:
        return "ANNUAL"
    return "UNKNOWN"


def extract_financials(filing: dict[str, str], companyfacts: dict[str, Any]) -> dict[str, Any]:
    accession, form = filing["AccessionNumber"], filing["Form"]
    revenue = extract_metric(companyfacts, REVENUE_CONCEPTS, ("USD",), accession, form)
    eps = extract_metric(companyfacts, DILUTED_EPS_CONCEPTS, ("USD/shares", "USD / shares"), accession, form)
    if eps["Actual"] is None:
        eps = extract_metric(companyfacts, BASIC_EPS_CONCEPTS, ("USD/shares", "USD / shares"), accession, form)

    invalid_period: list[str] = []
    invalid_previous: list[str] = []
    for name, metric in (("REVENUE", revenue), ("EPS", eps)):
        if not metric_period_is_current(metric, form, filing["PublishedAtUTC"]):
            invalid_period.append(name)
            invalidate_metric(metric)
            continue
        if not previous_period_is_comparable(metric):
            invalid_previous.append(name)
            invalidate_previous(metric)

    rev_status, rev_reason = metric_source_validation(revenue, filing)
    eps_status, eps_reason = metric_source_validation(eps, filing)
    has_data = revenue["Actual"] is not None or eps["Actual"] is not None
    if not has_data:
        data_status = "NO_MATCHING_XBRL_FACT"
    elif invalid_period:
        data_status = "EXTRACTED_PARTIAL_INVALID_PERIOD"
    elif invalid_previous:
        data_status = "EXTRACTED_WITHOUT_VALID_PREVIOUS"
    elif rev_status == "SOURCE_REVIEW_REQUIRED" or eps_status == "SOURCE_REVIEW_REQUIRED":
        data_status = "SOURCE_REVIEW_REQUIRED"
    else:
        data_status = "EXTRACTED_SOURCE_VALIDATED"

    metric_for_period = revenue if revenue["Actual"] is not None else eps
    return {
        "Period": infer_period(metric_for_period, form),
        "Revenue": revenue, "EPS": eps,
        "RevenueGrowthPct": percentage_change(revenue["Actual"], revenue["Previous"]),
        "RevenueSourceStatus": rev_status, "RevenueSourceReason": rev_reason,
        "EPSSourceStatus": eps_status, "EPSSourceReason": eps_reason,
        "InvalidPeriodFields": "|".join(invalid_period),
        "InvalidPreviousFields": "|".join(invalid_previous),
        "DataStatus": data_status, "HasData": has_data,
    }


def fetch_finnhub_earnings(ticker: str, filed_at: str) -> list[dict[str, Any]]:
    try:
        center = date.fromisoformat(clean(filed_at)[:10])
    except ValueError:
        return []
    params = {
        "from": (center-timedelta(days=FINNHUB_DATE_WINDOW_DAYS)).isoformat(),
        "to": (center+timedelta(days=FINNHUB_DATE_WINDOW_DAYS)).isoformat(),
        "symbol": ticker,
    }
    payload = get_finnhub_json(f"{FINNHUB_EARNINGS_CALENDAR_URL}?{urlencode(params)}")
    rows = payload.get("earningsCalendar", []) if isinstance(payload, dict) else []
    return rows if isinstance(rows, list) else []


def closest_finnhub_earning(
    rows: list[dict[str, Any]], ticker: str, filed_at: str
) -> Optional[dict[str, Any]]:
    try:
        filing_date = date.fromisoformat(clean(filed_at)[:10])
    except ValueError:
        return None
    candidates = []
    for row in rows:
        if clean(row.get("symbol")).upper() != ticker.upper():
            continue
        try:
            earnings_date = date.fromisoformat(clean(row.get("date"))[:10])
        except ValueError:
            continue
        distance = abs((filing_date-earnings_date).days)
        if distance <= FINNHUB_DATE_WINDOW_DAYS:
            candidates.append((distance, earnings_date, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def normalized_difference_pct(primary: float, secondary: float) -> float:
    return abs(primary-secondary) / max(abs(primary), abs(secondary), 1e-12) * 100.0


def surprise_pct(actual: Optional[float], consensus: Optional[float]) -> Optional[float]:
    if actual is None or consensus is None or consensus == 0:
        return None
    return (actual/consensus - 1.0) * 100.0


def corroborate_with_finnhub(
    filing: dict[str, str], financials: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    base = {
        "Date": "", "RevenueActual": None, "EPSActual": None,
        "RevenueEstimate": None, "EPSEstimate": None,
        "RevenueSurprisePct": None, "EPSSurprisePct": None,
        "RevenueDiffPct": None, "EPSDiffPct": None,
    }
    if not financials["HasData"]:
        return base | {
            "Status": "NOT_APPLICABLE",
            "Reason": "Aucune donnée financière SEC/XBRL extraite",
            "RevenueCheck": "NOT_APPLICABLE", "EPSCheck": "NOT_APPLICABLE",
            "EPSSurpriseStatus": "NOT_APPLICABLE",
        }
    match = closest_finnhub_earning(rows, filing["Ticker"], filing["PublishedAtUTC"])
    if not match:
        return base | {
            "Status": "ECONOMIC_REVIEW_REQUIRED",
            "Reason": "Aucune publication Finnhub correspondante dans la fenêtre",
            "RevenueCheck": "SECONDARY_NOT_AVAILABLE",
            "EPSCheck": "SECONDARY_NOT_AVAILABLE" if financials["EPS"]["Actual"] is not None else "NOT_APPLICABLE",
            "EPSSurpriseStatus": "SECONDARY_NOT_AVAILABLE",
        }

    fh_revenue = finite_float(match.get("revenueActual"))
    fh_eps = finite_float(match.get("epsActual"))
    revenue_estimate = finite_float(match.get("revenueEstimate"))
    eps_estimate = finite_float(match.get("epsEstimate"))
    sec_revenue = financials["Revenue"]["Actual"]
    sec_eps = financials["EPS"]["Actual"]

    if sec_revenue is None:
        revenue_check, revenue_reason, revenue_diff = "NOT_APPLICABLE", "Revenue SEC absent", None
    elif fh_revenue is None:
        revenue_check, revenue_reason, revenue_diff = "SECONDARY_NOT_AVAILABLE", "Revenue Finnhub absent", None
    else:
        revenue_diff = normalized_difference_pct(sec_revenue, fh_revenue)
        revenue_check = "MATCH" if revenue_diff <= REVENUE_TOLERANCE_PCT else "MISMATCH"
        revenue_reason = "Valeurs compatibles" if revenue_check == "MATCH" else "Écart supérieur au seuil"

    if sec_eps is None:
        eps_check, eps_diff = "NOT_APPLICABLE", None
    elif fh_eps is None:
        eps_check, eps_diff = "SECONDARY_NOT_AVAILABLE", None
    else:
        eps_check = "SECONDARY_BASIS_UNKNOWN"
        eps_diff = normalized_difference_pct(sec_eps, fh_eps)

    revenue_surprise = surprise_pct(sec_revenue, revenue_estimate)
    # EPS consensus conservé, mais surprise laissée vide : base non explicitée.
    eps_surprise = None
    if sec_eps is None:
        eps_surprise_status = "NOT_APPLICABLE"
    elif eps_estimate is None:
        eps_surprise_status = "SECONDARY_NOT_AVAILABLE"
    else:
        eps_surprise_status = "SECONDARY_BASIS_UNKNOWN"

    if revenue_check == "MISMATCH":
        status, reason = "ECONOMIC_MISMATCH", revenue_reason
    elif revenue_check == "MATCH":
        status = "ECONOMIC_PARTIAL"
        reason = "Revenue corroboré; EPS informatif, base secondaire inconnue"
    else:
        status = "ECONOMIC_REVIEW_REQUIRED"
        reason = "Aucune métrique Revenue comparable; EPS non décisionnel"

    return {
        "Status": status, "Reason": reason, "Date": clean(match.get("date")),
        "RevenueActual": fh_revenue, "EPSActual": fh_eps,
        "RevenueEstimate": revenue_estimate, "EPSEstimate": eps_estimate,
        "RevenueSurprisePct": revenue_surprise,
        "EPSSurprisePct": eps_surprise,
        "EPSSurpriseStatus": eps_surprise_status,
        "RevenueCheck": revenue_check, "EPSCheck": eps_check,
        "RevenueDiffPct": revenue_diff, "EPSDiffPct": eps_diff,
    }


def result_row(
    filing: dict[str, str], financials: dict[str, Any], corroboration: dict[str, Any],
    observed_at: str
) -> dict[str, Any]:
    revenue, eps = financials["Revenue"], financials["EPS"]
    needs_review = (
        financials["DataStatus"] != "EXTRACTED_SOURCE_VALIDATED"
        or corroboration["Status"] in {"ECONOMIC_MISMATCH", "ECONOMIC_REVIEW_REQUIRED"}
    )
    return {
        "EventID": filing["EventID"], "AgentVersion": AGENT_VERSION,
        "Ticker": filing["Ticker"], "ObservedAtUTC": observed_at,
        "PublishedAtUTC": filing["PublishedAtUTC"],
        "EventType": filing["Classification"], "Period": financials["Period"],
        "RevenueActual": "" if revenue["Actual"] is None else revenue["Actual"],
        "RevenueConsensus": "" if corroboration["RevenueEstimate"] is None else corroboration["RevenueEstimate"],
        "RevenuePrevious": "" if revenue["Previous"] is None else revenue["Previous"],
        "RevenueSurprisePct": "" if corroboration["RevenueSurprisePct"] is None else round(corroboration["RevenueSurprisePct"], 6),
        "RevenueGrowthPct": "" if financials["RevenueGrowthPct"] is None else round(financials["RevenueGrowthPct"], 6),
        "EPSActual": "" if eps["Actual"] is None else eps["Actual"],
        "EPSConsensus": "" if corroboration["EPSEstimate"] is None else corroboration["EPSEstimate"],
        "EPSPrevious": "" if eps["Previous"] is None else eps["Previous"],
        "EPSSurprisePct": "" if corroboration["EPSSurprisePct"] is None else round(corroboration["EPSSurprisePct"], 6),
        "GuidanceStatus": "", "KeyKPIName": "", "KeyKPIActual": "",
        "KeyKPIConsensus": "", "KeyKPIPrevious": "", "KeyKPIMomentum": "",
        "FundamentalAssessment": "", "ExpectationAssessment": "",
        "OverallVerdict": "", "Confidence": "",
        "DataStatus": f"{financials['DataStatus']}|{corroboration['Status']}",
        "NeedsHumanReview": needs_review, "ObservationOnly": OBSERVATION_ONLY,
        "V1ExecutionChanged": V1_EXECUTION_CHANGED,
        "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
    }


def merge_result_rows(
    existing: list[dict[str, str]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in existing:
        if clean(row.get("EventID")):
            merged[clean(row["EventID"])] = row
    for row in current:
        if clean(row.get("EventID")):
            merged[clean(row["EventID"])] = row
    return sorted(
        merged.values(),
        key=lambda row: (clean(row.get("PublishedAtUTC")), clean(row.get("Ticker")), clean(row.get("EventID"))),
        reverse=True,
    )


def display(value: Optional[float], decimals: int = 4) -> str:
    return "N/A" if value is None else f"{value:,.{decimals}f}"


def main() -> int:
    print("="*115)
    print("EARNINGS & COMPANY AGENT")
    print("EXTRACTION SEC/XBRL + CONSENSUS & SURPRISE FINNHUB")
    print("="*115)
    print("AgentVersion :", AGENT_VERSION)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("FinnhubConfigured :", bool(FINNHUB_API_KEY))
    if not FINNHUB_API_KEY:
        print("ERREUR : FINNHUB_API_KEY absent de l'environnement")
        return 2

    required = [CATALYST_EVENTS_FILE, CATALYST_SEMANTIC_FILE, CATALYST_SEC_FILINGS_FILE]
    missing = [path for path in required if not path.exists()]
    if missing:
        for path in missing:
            print("ERREUR : fichier source introuvable :", path)
        return 2

    tickers_list = load_tickers()
    watchlist = set(tickers_list)
    semantic_rows = semantic_candidates(watchlist, load_event_map())
    sec_rows, sec_review = sec_candidates(watchlist)
    print("\nTickers charges :", len(tickers_list))
    print("Candidats semantiques :", len(semantic_rows))
    print("Publications SEC retenues :", len(sec_rows))
    print("Filings SEC a revoir :", len(sec_review))

    companyfacts_cache: dict[str, dict[str, Any]] = {}
    finnhub_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    output_rows: list[dict[str, Any]] = []
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    errors = finnhub_errors = extracted = no_data = 0
    economic_counts: dict[str, int] = {}

    print("\n" + "="*115)
    print("EXTRACTION DES CHIFFRES PUBLIES")
    print("="*115)
    for index, filing in enumerate(sec_rows, 1):
        cik = filing["CIK"]
        if cik not in companyfacts_cache:
            try:
                companyfacts_cache[cik] = get_sec_json(f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json")
                time.sleep(REQUEST_PAUSE)
            except Exception as exc:
                errors += 1
                companyfacts_cache[cik] = {}
                print(f"\n{index}. {filing['Ticker']} | ERREUR COMPANY FACTS : {exc}")
                continue

        financials = extract_financials(filing, companyfacts_cache[cik])
        finnhub_key = (filing["Ticker"], filing["PublishedAtUTC"][:10])
        if finnhub_key not in finnhub_cache:
            try:
                finnhub_cache[finnhub_key] = fetch_finnhub_earnings(
                    filing["Ticker"], filing["PublishedAtUTC"]
                )
                time.sleep(REQUEST_PAUSE)
            except Exception as exc:
                finnhub_errors += 1
                finnhub_cache[finnhub_key] = []
                print("FinnhubCorroborationError :", exc)

        corroboration = corroborate_with_finnhub(
            filing, financials, finnhub_cache[finnhub_key]
        )
        economic_counts[corroboration["Status"]] = economic_counts.get(corroboration["Status"], 0) + 1
        if financials["HasData"]:
            extracted += 1
        else:
            no_data += 1
        output_rows.append(result_row(filing, financials, corroboration, observed_at))

        print(f"\n{index}. {filing['Ticker']} | {filing['PublishedAtUTC']} | {filing['Classification']} | {filing['Form']}")
        print("EventID :", filing["EventID"])
        print("Period :", financials["Period"])
        print("RevenueActual :", display(financials["Revenue"]["Actual"], 4))
        print("RevenuePrevious :", display(financials["Revenue"]["Previous"], 4))
        print("RevenueGrowthPct :", display(financials["RevenueGrowthPct"], 4))
        print("FinnhubRevenueActual :", display(corroboration["RevenueActual"], 4))
        print("FinnhubRevenueEstimate :", display(corroboration["RevenueEstimate"], 4))
        print("RevenueSurprisePct :", display(corroboration["RevenueSurprisePct"], 4))
        print("EPSActual :", display(financials["EPS"]["Actual"], 4))
        print("EPSPrevious :", display(financials["EPS"]["Previous"], 4))
        print("FinnhubEPSActual :", display(corroboration["EPSActual"], 4))
        print("FinnhubEPSEstimate :", display(corroboration["EPSEstimate"], 4))
        print("EPSSurprisePct :", display(corroboration["EPSSurprisePct"], 4))
        print("EPSSurpriseStatus :", corroboration["EPSSurpriseStatus"])
        print("EconomicValidationStatus :", corroboration["Status"])
        print("DataStatus :", financials["DataStatus"])

    merged_results = merge_result_rows(read_csv(EARNINGS_RESULTS_FILE), output_rows)
    write_results_csv(EARNINGS_RESULTS_FILE, merged_results)

    print("\n" + "="*115)
    print("RESUME EXTRACTION")
    print("="*115)
    print("Publications SEC examinees :", len(sec_rows))
    print("Publications avec donnees :", extracted)
    print("Publications sans correspondance XBRL :", no_data)
    print("Erreurs SEC Company Facts :", errors)
    print("Erreurs Finnhub :", finnhub_errors)
    print("Corroborations economiques :")
    for status, count in sorted(economic_counts.items()):
        print(status, ":", count)
    print("Resultats ecrits cette execution :", len(output_rows))
    print("Total lignes Earnings conservees :", len(merged_results))
    print("Fichier resultats :", EARNINGS_RESULTS_FILE)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("="*115)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
