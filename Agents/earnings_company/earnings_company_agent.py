#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Lecture seule de Catalyst et SEC EDGAR.
Détecte les publications financières primaires, extrait Revenue/EPS via
SEC Company Facts, valide les périodes XBRL et conserve une validation
économique séparée.

Diagnostic uniquement : aucune écriture dans earnings_company_events.csv.
Aucun impact sur V1 ou sur le portefeuille V1.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import date
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
EPS_TOLERANCE_PCT = 5.0
EPS_TOLERANCE_ABS = 0.05
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


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def normalize(value: Any) -> str:
    return clean(value).upper().replace("_", "-")


def as_bool(value: Any) -> bool:
    return clean(value).upper() in {"TRUE", "1", "YES", "OUI"}


def finite_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_tickers() -> list[str]:
    if not STOCKS_FILE.exists():
        raise FileNotFoundError(f"Fichier stocks introuvable : {STOCKS_FILE}")
    return [
        line.strip().upper()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def get_json(url: str) -> Any:
    request = Request(
        url,
        headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body[:250]}") from error
    except URLError as error:
        raise RuntimeError(f"Erreur reseau SEC: {error.reason}") from error


def get_finnhub_json(url: str) -> Any:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY absent de l'environnement")
    request = Request(
        url,
        headers={"X-Finnhub-Token": FINNHUB_API_KEY, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Finnhub HTTP {error.code}: {body[:250]}") from error
    except URLError as error:
        raise RuntimeError(f"Erreur reseau Finnhub: {error.reason}") from error


def fetch_finnhub_earnings(ticker: str, filed_at: str) -> list[dict[str, Any]]:
    try:
        center = date.fromisoformat(clean(filed_at)[:10])
    except ValueError:
        return []
    from datetime import timedelta
    params = {
        "from": (center - timedelta(days=FINNHUB_DATE_WINDOW_DAYS)).isoformat(),
        "to": (center + timedelta(days=FINNHUB_DATE_WINDOW_DAYS)).isoformat(),
        "symbol": ticker,
    }
    payload = get_finnhub_json(
        f"{FINNHUB_EARNINGS_CALENDAR_URL}?{urlencode(params)}"
    )
    if isinstance(payload, dict):
        rows = payload.get("earningsCalendar", [])
        return rows if isinstance(rows, list) else []
    return []


def closest_finnhub_earning(
    rows: list[dict[str, Any]],
    ticker: str,
    filed_at: str,
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
        distance = abs((filing_date - earnings_date).days)
        if distance <= FINNHUB_DATE_WINDOW_DAYS:
            candidates.append((distance, earnings_date, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=False)
    return candidates[0][2]


def normalized_difference_pct(primary: float, secondary: float) -> float:
    denominator = max(abs(primary), abs(secondary), 1e-12)
    return abs(primary - secondary) / denominator * 100.0


def compare_metric(
    primary: Optional[float],
    secondary: Optional[float],
    kind: str,
) -> tuple[str, str, Optional[float]]:
    if primary is None:
        return "NOT_APPLICABLE", "Métrique SEC/XBRL absente", None
    if secondary is None:
        return "SECONDARY_NOT_AVAILABLE", "Valeur Finnhub absente", None

    diff_pct = normalized_difference_pct(primary, secondary)
    if kind == "REVENUE":
        matched = diff_pct <= REVENUE_TOLERANCE_PCT
    else:
        matched = (
            abs(primary - secondary) <= EPS_TOLERANCE_ABS
            or diff_pct <= EPS_TOLERANCE_PCT
        )
    return (
        ("MATCH", "Valeurs compatibles")
        if matched
        else ("MISMATCH", "Écart supérieur au seuil")
    ) + (diff_pct,)


def corroborate_with_finnhub(
    filing: dict[str, str],
    financials: dict[str, Any],
    finnhub_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not financials["HasData"]:
        return {
            "Status": "NOT_APPLICABLE",
            "Reason": "Aucune donnée financière SEC/XBRL extraite",
            "Date": "",
            "RevenueActual": None,
            "EPSActual": None,
            "RevenueCheck": "NOT_APPLICABLE",
            "EPSCheck": "NOT_APPLICABLE",
            "RevenueDiffPct": None,
            "EPSDiffPct": None,
        }

    match = closest_finnhub_earning(
        finnhub_rows, filing["Ticker"], filing["PublishedAtUTC"]
    )
    if not match:
        return {
            "Status": "ECONOMIC_REVIEW_REQUIRED",
            "Reason": "Aucune publication Finnhub correspondante dans la fenêtre",
            "Date": "",
            "RevenueActual": None,
            "EPSActual": None,
            "RevenueCheck": "SECONDARY_NOT_AVAILABLE",
            "EPSCheck": (
                "NOT_APPLICABLE"
                if financials["EPS"]["Actual"] is None
                else "SECONDARY_NOT_AVAILABLE"
            ),
            "RevenueDiffPct": None,
            "EPSDiffPct": None,
        }

    finnhub_revenue = finite_float(match.get("revenueActual"))
    finnhub_eps = finite_float(match.get("epsActual"))
    revenue_check, revenue_reason, revenue_diff = compare_metric(
        financials["Revenue"]["Actual"], finnhub_revenue, "REVENUE"
    )
    # Finnhub ne précise pas ici si epsActual est GAAP dilué, ajusté ou
    # normalisé. L'EPS secondaire reste donc informatif et ne peut jamais,
    # à lui seul, provoquer ECONOMIC_MISMATCH.
    if financials["EPS"]["Actual"] is None:
        eps_check = "NOT_APPLICABLE"
        eps_reason = "EPS SEC/XBRL absent"
        eps_diff = None
    elif finnhub_eps is None:
        eps_check = "SECONDARY_NOT_AVAILABLE"
        eps_reason = "EPS Finnhub absent"
        eps_diff = None
    else:
        eps_check = "SECONDARY_BASIS_UNKNOWN"
        eps_reason = "Base Finnhub GAAP/ajustée non explicitée"
        eps_diff = normalized_difference_pct(
            financials["EPS"]["Actual"], finnhub_eps
        )

    # Seul Revenue peut produire un mismatch économique dans cette version.
    if revenue_check == "MISMATCH":
        status = "ECONOMIC_MISMATCH"
        reason = revenue_reason
    elif revenue_check == "MATCH":
        status = "ECONOMIC_PARTIAL"
        reason = (
            "Revenue corroboré par Finnhub; EPS informatif, base secondaire inconnue"
        )
    elif revenue_check in {"NOT_APPLICABLE", "SECONDARY_NOT_AVAILABLE"}:
        status = "ECONOMIC_REVIEW_REQUIRED"
        reason = (
            "Aucune métrique Revenue comparable; EPS non décisionnel dans cette version"
        )
    else:
        status = "ECONOMIC_REVIEW_REQUIRED"
        reason = "Corroboration économique insuffisante"

    return {
        "Status": status,
        "Reason": reason,
        "Date": clean(match.get("date")),
        "RevenueActual": finnhub_revenue,
        "EPSActual": finnhub_eps,
        "RevenueCheck": revenue_check,
        "EPSCheck": eps_check,
        "RevenueDiffPct": revenue_diff,
        "EPSDiffPct": eps_diff,
    }


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
            "Headline": clean(semantic.get("RepresentativeTitle") or event.get("RepresentativeTitle")),
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
    retained.sort(key=lambda x: (x["PublishedAtUTC"], x["Ticker"]), reverse=True)
    review.sort(key=lambda x: (x["PublishedAtUTC"], x["Ticker"]), reverse=True)
    return retained, review


def fact_units(
    companyfacts: dict[str, Any],
    concepts: tuple[str, ...],
    preferred_units: tuple[str, ...],
) -> tuple[str, str, list[dict[str, Any]]]:
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    for concept in concepts:
        fact = us_gaap.get(concept)
        if not isinstance(fact, dict):
            continue
        units = fact.get("units", {})
        if not isinstance(units, dict):
            continue
        for unit in preferred_units:
            entries = units.get(unit)
            if isinstance(entries, list) and entries:
                return concept, unit, entries
    return "", "", []


def accession_facts(entries: list[dict[str, Any]], accession: str) -> list[dict[str, Any]]:
    return [row for row in entries if clean(row.get("accn")) == accession]


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


def select_current_fact(matches: list[dict[str, Any]], form: str) -> Optional[dict[str, Any]]:
    form = normalize(form)
    candidates = [
        row for row in matches
        if (not clean(row.get("form")) or normalize(row.get("form")) == form)
        and valid_duration_for_form(row, form)
        and finite_float(row.get("val")) is not None
    ]
    if not candidates:
        return None

    def score(row: dict[str, Any]) -> tuple[int, int, str]:
        fp = clean(row.get("fp")).upper()
        duration = duration_days(row)
        if form == "10-Q":
            fp_score = 3 if fp in {"Q1", "Q2", "Q3"} else 0
            duration_score = -abs((duration or 91) - 91)
        else:
            fp_score = 3 if fp == "FY" else 0
            duration_score = -abs((duration or 365) - 365)
        return fp_score, duration_score, clean(row.get("end"))

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
    candidates = []
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
        if not 330 <= (current_end - previous_end).days <= 400:
            continue
        candidates.append(row)
    return max(candidates, key=lambda x: clean(x.get("filed"))) if candidates else None


def extract_metric(
    companyfacts: dict[str, Any], concepts: tuple[str, ...], units: tuple[str, ...],
    accession: str, form: str
) -> dict[str, Any]:
    concept, unit, entries = fact_units(companyfacts, concepts, units)
    current = select_current_fact(accession_facts(entries, accession), form)
    if current is None:
        return {
            "Concept": concept, "Unit": unit, "Actual": None, "Previous": None,
            "Start": "", "End": "", "FY": "", "FP": "", "DurationDays": None,
            "PreviousEnd": "", "PreviousFY": "", "PreviousFP": "",
            "Frame": "", "Accession": "", "Filed": "", "Form": "",
        }
    previous = comparable_previous_fact(entries, current, accession, form)
    return {
        "Concept": concept,
        "Unit": unit,
        "Actual": finite_float(current.get("val")),
        "Previous": finite_float(previous.get("val")) if previous else None,
        "Start": clean(current.get("start")),
        "End": clean(current.get("end")),
        "FY": clean(current.get("fy")),
        "FP": clean(current.get("fp")),
        "DurationDays": duration_days(current),
        "PreviousEnd": clean(previous.get("end")) if previous else "",
        "PreviousFY": clean(previous.get("fy")) if previous else "",
        "PreviousFP": clean(previous.get("fp")) if previous else "",
        "Frame": clean(current.get("frame")),
        "Accession": clean(current.get("accn")),
        "Filed": clean(current.get("filed")),
        "Form": clean(current.get("form")),
    }


def metric_period_is_current(metric: dict[str, Any], form: str, filed_at: str) -> bool:
    if metric["Actual"] is None:
        return True
    try:
        end_date = date.fromisoformat(clean(metric["End"]))
        filing_date = date.fromisoformat(clean(filed_at)[:10])
    except ValueError:
        return False
    age = (filing_date - end_date).days
    form = normalize(form)
    fp = clean(metric["FP"]).upper()
    duration = metric["DurationDays"]
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
    metric["Actual"] = None
    metric["Previous"] = None
    metric["PreviousEnd"] = metric["PreviousFY"] = metric["PreviousFP"] = ""


def invalidate_previous(metric: dict[str, Any]) -> None:
    metric["Previous"] = None
    metric["PreviousEnd"] = metric["PreviousFY"] = metric["PreviousFP"] = ""


def metric_source_validation(metric: dict[str, Any], filing: dict[str, str]) -> tuple[str,str]:
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


def extract_financials(filing: dict[str,str], companyfacts: dict[str,Any]) -> dict[str,Any]:
    accession, form = filing["AccessionNumber"], filing["Form"]
    revenue = extract_metric(companyfacts, REVENUE_CONCEPTS, ("USD",), accession, form)
    eps = extract_metric(companyfacts, DILUTED_EPS_CONCEPTS, ("USD/shares","USD / shares"), accession, form)
    if eps["Actual"] is None:
        eps = extract_metric(companyfacts, BASIC_EPS_CONCEPTS, ("USD/shares","USD / shares"), accession, form)

    invalid_period_fields: list[str] = []
    invalid_previous_fields: list[str] = []
    for name, metric in (("REVENUE",revenue),("EPS",eps)):
        if not metric_period_is_current(metric, form, filing["PublishedAtUTC"]):
            invalid_period_fields.append(name)
            invalidate_metric(metric)
            continue
        if not previous_period_is_comparable(metric):
            invalid_previous_fields.append(name)
            invalidate_previous(metric)

    rev_status, rev_reason = metric_source_validation(revenue, filing)
    eps_status, eps_reason = metric_source_validation(eps, filing)
    has_data = revenue["Actual"] is not None or eps["Actual"] is not None

    if not has_data:
        economic_status = "NOT_APPLICABLE"
        economic_reason = "Aucune donnée financière extraite"
    else:
        economic_status = "ECONOMIC_REVIEW_REQUIRED"
        economic_reason = "Validation technique acquise; corroboration économique absente"

    if not has_data:
        data_status = "NO_MATCHING_XBRL_FACT"
    elif invalid_period_fields:
        data_status = "EXTRACTED_PARTIAL_INVALID_PERIOD"
    elif invalid_previous_fields:
        data_status = "EXTRACTED_WITHOUT_VALID_PREVIOUS"
    elif rev_status == "SOURCE_REVIEW_REQUIRED" or eps_status == "SOURCE_REVIEW_REQUIRED":
        data_status = "SOURCE_REVIEW_REQUIRED"
    else:
        data_status = "EXTRACTED_SOURCE_VALIDATED"

    metric_for_period = revenue if revenue["Actual"] is not None else eps
    return {
        "Period": infer_period(metric_for_period, form),
        "Revenue": revenue,
        "EPS": eps,
        "RevenueGrowthPct": percentage_change(revenue["Actual"], revenue["Previous"]),
        "RevenueSourceStatus": rev_status,
        "RevenueSourceReason": rev_reason,
        "EPSSourceStatus": eps_status,
        "EPSSourceReason": eps_reason,
        "InvalidPeriodFields": "|".join(invalid_period_fields),
        "InvalidPreviousFields": "|".join(invalid_previous_fields),
        "EconomicValidationStatus": economic_status,
        "EconomicValidationReason": economic_reason,
        "DataStatus": data_status,
        "HasData": has_data,
    }


def display(value: Optional[float], decimals: int=4) -> str:
    return "N/A" if value is None else f"{value:,.{decimals}f}"


def print_metric(prefix: str, metric: dict[str,Any], source_status: str, source_reason: str) -> None:
    print(f"{prefix} :", display(metric["Actual"], 4), metric["Unit"] or "")
    print(f"{prefix}Previous :", display(metric["Previous"], 4))
    print(f"{prefix}Concept :", metric["Concept"] or "N/A")
    print(f"{prefix}Start :", metric["Start"] or "N/A")
    print(f"{prefix}End :", metric["End"] or "N/A")
    print(f"{prefix}Frame :", metric["Frame"] or "N/A")
    print(f"{prefix}Accession :", metric["Accession"] or "N/A")
    print(f"{prefix}Filed :", metric["Filed"] or "N/A")
    print(f"{prefix}SourceStatus :", source_status)
    print(f"{prefix}SourceReason :", source_reason)
    print(f"{prefix}DurationDays :", metric["DurationDays"] if metric["DurationDays"] is not None else "N/A")
    print(f"{prefix}PreviousPeriod :", metric["PreviousFY"] or "N/A", metric["PreviousFP"] or "", metric["PreviousEnd"] or "")


def main() -> int:
    print("="*115)
    print("EARNINGS & COMPANY AGENT")
    print("EXTRACTION SEC COMPANY FACTS - DIAGNOSTIC UNIQUEMENT")
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
    missing = [p for p in required if not p.exists()]
    if missing:
        for p in missing: print("ERREUR : fichier source introuvable :", p)
        return 2

    tickers_list = load_tickers()
    watchlist = set(tickers_list)
    semantic_rows = semantic_candidates(watchlist, load_event_map())
    sec_rows, sec_review = sec_candidates(watchlist)
    print("\nTickers charges :", len(tickers_list))
    print("Candidats semantiques :", len(semantic_rows))
    print("Publications SEC retenues :", len(sec_rows))
    print("Filings SEC a revoir :", len(sec_review))

    cache: dict[str,dict[str,Any]] = {}
    finnhub_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    errors = extracted = no_data = 0
    finnhub_errors = 0
    economic_counts: dict[str, int] = {}
    print("\n"+"="*115)
    print("EXTRACTION DES CHIFFRES PUBLIES")
    print("="*115)
    for i, filing in enumerate(sec_rows,1):
        cik = filing["CIK"]
        if cik not in cache:
            try:
                cache[cik] = get_json(f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json")
                time.sleep(REQUEST_PAUSE)
            except Exception as exc:
                errors += 1
                cache[cik] = {}
                print(f"\n{i}. {filing['Ticker']} | ERREUR COMPANY FACTS : {exc}")
                continue
        f = extract_financials(filing, cache[cik])
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
            filing, f, finnhub_cache[finnhub_key]
        )
        economic_counts[corroboration["Status"]] = (
            economic_counts.get(corroboration["Status"], 0) + 1
        )
        if f["HasData"]: extracted += 1
        else: no_data += 1
        print(f"\n{i}. {filing['Ticker']} | {filing['PublishedAtUTC']} | {filing['Classification']} | {filing['Form']}")
        print("EventID :", filing["EventID"])
        print("AccessionNumber :", filing["AccessionNumber"])
        print("Period :", f["Period"])
        print_metric("Revenue", f["Revenue"], f["RevenueSourceStatus"], f["RevenueSourceReason"])
        print("RevenueGrowthPct :", display(f["RevenueGrowthPct"],4))
        print_metric("EPS", f["EPS"], f["EPSSourceStatus"], f["EPSSourceReason"])
        print("InvalidPeriodFields :", f["InvalidPeriodFields"] or "NONE")
        print("InvalidPreviousFields :", f["InvalidPreviousFields"] or "NONE")
        print("TechnicalEconomicStatus :", f["EconomicValidationStatus"])
        print("TechnicalEconomicReason :", f["EconomicValidationReason"])
        print("FinnhubEarningsDate :", corroboration["Date"] or "N/A")
        print("FinnhubRevenueActual :", display(corroboration["RevenueActual"], 4))
        print("FinnhubEPSActual :", display(corroboration["EPSActual"], 4))
        print("RevenueCorroboration :", corroboration["RevenueCheck"])
        print("RevenueDifferencePct :", display(corroboration["RevenueDiffPct"], 4))
        print("EPSCorroboration :", corroboration["EPSCheck"])
        print("EPSDifferencePct :", display(corroboration["EPSDiffPct"], 4))
        print("EconomicValidationStatus :", corroboration["Status"])
        print("EconomicValidationReason :", corroboration["Reason"])
        print("DataStatus :", f["DataStatus"])

    print("\n"+"="*115)
    print("RESUME EXTRACTION")
    print("="*115)
    print("Publications SEC examinees :", len(sec_rows))
    print("Publications avec donnees :", extracted)
    print("Publications sans correspondance XBRL :", no_data)
    print("Erreurs SEC Company Facts :", errors)
    print("Erreurs Finnhub :", finnhub_errors)
    print("CIK Company Facts charges :", len(cache))
    print("Corroborations economiques :")
    for status, count in sorted(economic_counts.items()):
        print(status, ":", count)
    print("Aucune ecriture dans earnings_company_events.csv")
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("="*115)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
