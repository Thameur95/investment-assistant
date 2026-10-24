#!/usr/bin/env python3
"""Earnings & Company Agent V1 - Shadow / observation uniquement.

SEC/XBRL + consensus Finnhub + Guidance Catalyst + cartographie KPI métier.
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
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
SEC_COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
REQUEST_TIMEOUT = 25
REQUEST_PAUSE = 0.15
FINNHUB_DATE_WINDOW_DAYS = 10
GUIDANCE_MATCH_DAYS = 3
REVENUE_TOLERANCE_PCT = 2.0
KPI_STABLE_THRESHOLD_PCT = 1.0
KPI_DIAGNOSTIC_LIMIT = 8

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

GUIDANCE_PATTERNS = {
    "WITHDRAWN": re.compile(r"\b(withdraw(?:n|s)?|suspend(?:ed|s)?|retire|retiree|suspend)\b", re.I),
    "RAISED": re.compile(r"\b(raise[sd]?|increase[sd]?|boost(?:ed|s)?|lift(?:ed|s)?|releve|rehausse|revise en hausse)\b", re.I),
    "LOWERED": re.compile(r"\b(lower(?:ed|s)?|cut(?:s)?|reduce[sd]?|downgrade[sd]?|abaisse|reduit|revise en baisse)\b", re.I),
    "REAFFIRMED": re.compile(r"\b(reaffirm(?:ed|s)?|reiterate[sd]?|maintain(?:ed|s)?|confirm(?:ed|s)?|reaffirme|maintient|confirme)\b", re.I),
    "INTRODUCED": re.compile(r"\b(guidance|outlook|previsions|perspectives|initiat(?:e|ed|es)|introduc(?:e|ed|es)|provid(?:e|ed|es))\b", re.I),
}

# V1 déterministe. Les motifs sont appliqués aux noms de concepts XBRL,
# dans toutes les taxonomies présentes dans SEC Company Facts.
KPI_MAP: dict[str, dict[str, Any]] = {
    "NVDA": {"name": "Data Center Revenue", "patterns": [r"datacenter.*revenue", r"revenue.*datacenter"]},
    "MSFT": {"name": "Intelligent Cloud Revenue", "patterns": [r"intelligentcloud.*revenue", r"azure.*revenue"]},
    "GOOGL": {"name": "Google Cloud Revenue", "patterns": [r"googlecloud.*revenue", r"cloud.*revenue"]},
    "AMZN": {"name": "AWS Net Sales", "patterns": [r"amazonwebservices.*(sales|revenue)", r"aws.*(sales|revenue)"]},
    "META": {"name": "Family of Apps Revenue", "patterns": [r"familyofapps.*revenue"]},
    "TSLA": {"name": "Automotive Revenue", "patterns": [r"automotive.*revenue"]},
    "AAPL": {"name": "Services Revenue", "patterns": [r"services.*(netsales|revenue)"]},
    "SPCX": {"name": "Starlink Revenue", "patterns": [r"starlink.*revenue"]},
    "VLO": {"name": "Refining Revenue", "patterns": [r"refining.*revenue"]},
    "SU": {"name": "Oil Sands Revenue", "patterns": [r"oilsands.*revenue"]},
    "TTE": {"name": "LNG Revenue", "patterns": [r"lng.*revenue", r"integratedlng.*revenue"]},
}

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
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_tickers() -> list[str]:
    if not STOCKS_FILE.exists():
        raise FileNotFoundError(f"Fichier stocks introuvable : {STOCKS_FILE}")
    return [x.strip().upper() for x in STOCKS_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]


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
    return get_json(url, {"X-Finnhub-Token": FINNHUB_API_KEY, "Accept": "application/json"})


def load_event_map() -> dict[str, dict[str, str]]:
    return {clean(r.get("EventID")): r for r in read_csv(CATALYST_EVENTS_FILE) if clean(r.get("EventID"))}


def semantic_candidates(watchlist: set[str], events: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    output = []
    for row in read_csv(CATALYST_SEMANTIC_FILE):
        ticker, event_id = clean(row.get("Ticker")).upper(), clean(row.get("EventID"))
        if ticker not in watchlist or not event_id or not as_bool(row.get("IsRelevant")):
            continue
        ctype, category = normalize(row.get("CatalystType")), normalize(row.get("Category"))
        if ctype not in {"EARNINGS", "GUIDANCE"} and category not in {
            "EARNINGS", "GUIDANCE", "FINANCIAL-RESULTS", "FINANCIAL-UPDATE"
        }:
            continue
        event = events.get(event_id, {})
        output.append({
            "EventID": event_id, "Ticker": ticker,
            "PublishedAtUTC": clean(row.get("FirstSeenUTC") or event.get("FirstSeenUTC")),
            "CatalystType": ctype, "Category": category,
            "Headline": clean(row.get("RepresentativeTitle") or event.get("RepresentativeTitle")),
            "KeyFact": clean(row.get("KeyFact")), "Reason": clean(row.get("Reason")),
        })
    return output


def classify_sec(row: dict[str, str]) -> tuple[bool, str]:
    form, description = normalize(row.get("Form")), clean(row.get("Description"))
    if form == "10-Q":
        return True, "ACTUAL_QUARTERLY_FILING"
    if form in {"10-K", "20-F", "40-F"}:
        return True, "ACTUAL_ANNUAL_FILING"
    if form in CONDITIONAL_FORMS and EARNINGS_WORDS.search(description):
        return True, "ACTUAL_EARNINGS_RELEASE"
    return False, "SEC_REVIEW"


def sec_candidates(watchlist: set[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    retained, review = [], []
    for row in read_csv(CATALYST_SEC_FILINGS_FILE):
        ticker, accession = clean(row.get("Ticker")).upper(), clean(row.get("AccessionNumber"))
        if ticker not in watchlist or not accession:
            continue
        keep, classification = classify_sec(row)
        item = {
            "EventID": f"SEC|{ticker}|{accession}", "Ticker": ticker,
            "PublishedAtUTC": clean(row.get("FiledAt")), "Classification": classification,
            "Form": clean(row.get("Form")), "AccessionNumber": accession,
            "CIK": clean(row.get("CIK")).zfill(10), "Description": clean(row.get("Description")),
        }
        (retained if keep else review).append(item)
    key = lambda x: (x["PublishedAtUTC"], x["Ticker"], x["EventID"])
    return sorted(retained, key=key, reverse=True), sorted(review, key=key, reverse=True)


def duration_days(row: dict[str, Any]) -> Optional[int]:
    try:
        return (date.fromisoformat(clean(row.get("end"))) - date.fromisoformat(clean(row.get("start")))).days
    except ValueError:
        return None


def valid_duration(row: dict[str, Any], form: str) -> bool:
    duration, form = duration_days(row), normalize(form)
    if form == "10-Q":
        return duration is not None and 70 <= duration <= 110
    if form in {"10-K", "20-F", "40-F"}:
        return duration is not None and 330 <= duration <= 400
    return True


def fiscal_year(row: dict[str, Any]) -> Optional[int]:
    try:
        return int(clean(row.get("fy")))
    except (TypeError, ValueError):
        return None


def select_current(entries: list[dict[str, Any]], accession: str, form: str) -> Optional[dict[str, Any]]:
    form = normalize(form)
    rows = [
        row for row in entries
        if clean(row.get("accn")) == accession
        and (not clean(row.get("form")) or normalize(row.get("form")) == form)
        and valid_duration(row, form)
        and finite_float(row.get("val")) is not None
    ]
    if not rows:
        return None
    target = 91 if form == "10-Q" else 365
    wanted = {"Q1", "Q2", "Q3"} if form == "10-Q" else {"FY"}
    return max(rows, key=lambda row: (
        1 if clean(row.get("fp")).upper() in wanted else 0,
        -abs((duration_days(row) or target)-target), clean(row.get("end")),
    ))


def select_previous(entries: list[dict[str, Any]], current: dict[str, Any], accession: str, form: str) -> Optional[dict[str, Any]]:
    try:
        current_end = date.fromisoformat(clean(current.get("end")))
    except ValueError:
        return None
    current_fy, current_fp, current_duration = fiscal_year(current), clean(current.get("fp")).upper(), duration_days(current)
    rows = []
    for row in entries:
        if clean(row.get("accn")) == accession or finite_float(row.get("val")) is None:
            continue
        if not valid_duration(row, form) or clean(row.get("fp")).upper() != current_fp:
            continue
        if current_fy is not None and fiscal_year(row) != current_fy-1:
            continue
        if current_duration is not None and duration_days(row) is not None and abs(duration_days(row)-current_duration) > 10:
            continue
        try:
            delta = (current_end-date.fromisoformat(clean(row.get("end")))).days
        except ValueError:
            continue
        if 330 <= delta <= 400:
            rows.append(row)
    return max(rows, key=lambda row: clean(row.get("filed"))) if rows else None


def all_fact_concepts(companyfacts: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    output = []
    for namespace, concepts in companyfacts.get("facts", {}).items():
        if not isinstance(concepts, dict):
            continue
        for concept, payload in concepts.items():
            if isinstance(payload, dict):
                output.append((namespace, concept, payload))
    return output


def extract_named_metric(
    companyfacts: dict[str, Any], concepts: tuple[str, ...], accession: str, form: str,
    preferred_units: tuple[str, ...]
) -> dict[str, Any]:
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    for concept in concepts:
        payload = us_gaap.get(concept, {})
        units = payload.get("units", {}) if isinstance(payload, dict) else {}
        for unit in preferred_units:
            entries = units.get(unit, [])
            current = select_current(entries, accession, form)
            if current:
                previous = select_previous(entries, current, accession, form)
                return metric_payload(concept, unit, current, previous)
    return empty_metric()


def empty_metric() -> dict[str, Any]:
    return {"Name": "", "Concept": "", "Unit": "", "Actual": None, "Previous": None,
            "Start": "", "End": "", "FY": "", "FP": "", "DurationDays": None,
            "Accession": "", "Form": ""}


def metric_payload(concept: str, unit: str, current: dict[str, Any], previous: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "Name": "", "Concept": concept, "Unit": unit,
        "Actual": finite_float(current.get("val")),
        "Previous": finite_float(previous.get("val")) if previous else None,
        "Start": clean(current.get("start")), "End": clean(current.get("end")),
        "FY": clean(current.get("fy")), "FP": clean(current.get("fp")),
        "DurationDays": duration_days(current), "Accession": clean(current.get("accn")),
        "Form": clean(current.get("form")),
    }


def kpi_search_tokens(config: dict[str, Any]) -> set[str]:
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", clean(config.get("name")))
        if len(token) >= 3 and token.lower() not in {"revenue", "sales", "net"}
    }
    for pattern in config.get("patterns", []):
        tokens.update(
            token.lower()
            for token in re.findall(r"[A-Za-z0-9]+", pattern)
            if len(token) >= 3
            and token.lower() not in {"revenue", "sales", "netsales"}
        )
    return tokens


def diagnose_kpi_concepts(
    companyfacts: dict[str, Any],
    filing: dict[str, str],
) -> list[dict[str, Any]]:
    """Inventorie les concepts plausibles rattachés au même accession.

    Ce diagnostic n'alimente pas KeyKPIActual. Il sert uniquement à découvrir
    les vrais concepts et unités publiés par l'entreprise, sans élargir les
    règles d'extraction au hasard.
    """
    config = KPI_MAP.get(filing["Ticker"])
    if not config:
        return []
    search_tokens = kpi_search_tokens(config)
    results: list[dict[str, Any]] = []

    for namespace, concept, payload in all_fact_concepts(companyfacts):
        label = clean(payload.get("label"))
        description = clean(payload.get("description"))
        haystack = f"{concept} {label} {description}".lower()
        token_hits = sorted(token for token in search_tokens if token in haystack)
        if not token_hits:
            continue

        units = payload.get("units", {})
        if not isinstance(units, dict):
            continue
        for unit, entries in units.items():
            if not isinstance(entries, list):
                continue
            filing_entries = [
                row
                for row in entries
                if clean(row.get("accn")) == filing["AccessionNumber"]
            ]
            if not filing_entries:
                continue
            current = select_current(entries, filing["AccessionNumber"], filing["Form"])
            results.append({
                "Score": len(token_hits),
                "Namespace": namespace,
                "Concept": concept,
                "Label": label,
                "Description": description,
                "Unit": unit,
                "TokenHits": ",".join(token_hits),
                "AccessionEntryCount": len(filing_entries),
                "SelectedValue": (
                    finite_float(current.get("val")) if current else None
                ),
                "SelectedStart": clean(current.get("start")) if current else "",
                "SelectedEnd": clean(current.get("end")) if current else "",
                "SelectedFP": clean(current.get("fp")) if current else "",
            })

    return sorted(
        results,
        key=lambda row: (
            row["Score"],
            row["AccessionEntryCount"],
            row["Concept"],
        ),
        reverse=True,
    )[:KPI_DIAGNOSTIC_LIMIT]


def print_kpi_diagnostic(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("KPIDiagnostic : aucun concept candidat rattache au filing")
        return
    print("KPIDiagnosticCandidates :", len(rows))
    for index, row in enumerate(rows, 1):
        print(
            f"KPIDiagnostic{index} : "
            f"{row['Namespace']}:{row['Concept']} | "
            f"label={row['Label'] or 'N/A'} | unit={row['Unit']} | "
            f"hits={row['TokenHits']} | entries={row['AccessionEntryCount']} | "
            f"value={row['SelectedValue'] if row['SelectedValue'] is not None else 'N/A'} | "
            f"period={row['SelectedStart'] or 'N/A'}->{row['SelectedEnd'] or 'N/A'} | "
            f"fp={row['SelectedFP'] or 'N/A'}"
        )


def extract_kpi(companyfacts: dict[str, Any], filing: dict[str, str]) -> dict[str, Any]:
    config = KPI_MAP.get(filing["Ticker"])
    if not config:
        result = empty_metric()
        result.update({"Status": "NO_MAPPING", "Momentum": "UNKNOWN"})
        return result

    compiled = [re.compile(pattern, re.I) for pattern in config["patterns"]]
    candidates: list[tuple[int, str, str, str, list[dict[str, Any]]]] = []
    for namespace, concept, payload in all_fact_concepts(companyfacts):
        normalized = re.sub(r"[^a-z0-9]", "", concept.lower())
        matches = [p for p in compiled if p.search(normalized)]
        if not matches:
            continue
        units = payload.get("units", {})
        if not isinstance(units, dict):
            continue
        for unit, entries in units.items():
            if unit not in {"USD", "shares", "pure"} or not isinstance(entries, list):
                continue
            score = max(len(match.pattern) for match in matches)
            candidates.append((score, namespace, concept, unit, entries))

    candidates.sort(key=lambda item: (item[0], item[2]), reverse=True)
    for _, namespace, concept, unit, entries in candidates:
        current = select_current(entries, filing["AccessionNumber"], filing["Form"])
        if not current:
            continue
        previous = select_previous(entries, current, filing["AccessionNumber"], filing["Form"])
        result = metric_payload(f"{namespace}:{concept}", unit, current, previous)
        result["Name"] = config["name"]
        result["Status"] = "EXTRACTED"
        actual, prev = result["Actual"], result["Previous"]
        if actual is None or prev is None or prev == 0:
            result["Momentum"] = "UNKNOWN"
        else:
            change = (actual/prev-1.0)*100.0
            if change > KPI_STABLE_THRESHOLD_PCT:
                result["Momentum"] = "UP"
            elif change < -KPI_STABLE_THRESHOLD_PCT:
                result["Momentum"] = "DOWN"
            else:
                result["Momentum"] = "STABLE"
        return result

    result = empty_metric()
    result.update({"Name": config["name"], "Status": "NOT_FOUND", "Momentum": "UNKNOWN"})
    return result


def metric_period_valid(metric: dict[str, Any], form: str, filed_at: str) -> bool:
    if metric["Actual"] is None:
        return True
    try:
        end = date.fromisoformat(metric["End"])
        filed = date.fromisoformat(filed_at[:10])
    except ValueError:
        return False
    age = (filed-end).days
    fp, duration, form = metric["FP"].upper(), metric["DurationDays"], normalize(form)
    if form == "10-Q":
        return duration is not None and 70 <= duration <= 110 and fp in {"Q1", "Q2", "Q3"} and 0 <= age <= 130
    if form in {"10-K", "20-F", "40-F"}:
        return duration is not None and 330 <= duration <= 400 and fp == "FY" and 0 <= age <= 200
    return True


def extract_financials(filing: dict[str, str], facts: dict[str, Any]) -> dict[str, Any]:
    accession, form = filing["AccessionNumber"], filing["Form"]
    revenue = extract_named_metric(facts, REVENUE_CONCEPTS, accession, form, ("USD",))
    eps = extract_named_metric(facts, DILUTED_EPS_CONCEPTS, accession, form, ("USD/shares", "USD / shares"))
    if eps["Actual"] is None:
        eps = extract_named_metric(facts, BASIC_EPS_CONCEPTS, accession, form, ("USD/shares", "USD / shares"))
    for metric in (revenue, eps):
        if not metric_period_valid(metric, form, filing["PublishedAtUTC"]):
            metric["Actual"] = metric["Previous"] = None
    has_data = revenue["Actual"] is not None or eps["Actual"] is not None
    source_ok = all(
        metric["Actual"] is None or (
            metric["Accession"] == accession
            and (not metric["Form"] or normalize(metric["Form"]) == normalize(form))
        )
        for metric in (revenue, eps)
    )
    if not has_data:
        status = "NO_MATCHING_XBRL_FACT"
    elif not source_ok:
        status = "SOURCE_REVIEW_REQUIRED"
    else:
        status = "EXTRACTED_SOURCE_VALIDATED"
    period_metric = revenue if revenue["Actual"] is not None else eps
    period = f"FY{period_metric['FY']}-{period_metric['FP']}" if period_metric["FY"] and period_metric["FP"] else "UNKNOWN"
    growth = None if revenue["Actual"] is None or revenue["Previous"] in (None, 0) else (revenue["Actual"]/revenue["Previous"]-1)*100
    return {"Revenue": revenue, "EPS": eps, "Period": period, "RevenueGrowthPct": growth,
            "HasData": has_data, "DataStatus": status}


def guidance_for_filing(filing: dict[str, str], semantic_rows: list[dict[str, str]]) -> tuple[str, str]:
    try:
        filing_date = date.fromisoformat(filing["PublishedAtUTC"][:10])
    except ValueError:
        return "NOT_FOUND", ""
    candidates = []
    for row in semantic_rows:
        if row["Ticker"] != filing["Ticker"]:
            continue
        if row["CatalystType"] != "GUIDANCE" and row["Category"] != "GUIDANCE":
            continue
        try:
            event_date = date.fromisoformat(row["PublishedAtUTC"][:10])
        except ValueError:
            continue
        distance = abs((filing_date-event_date).days)
        if distance <= GUIDANCE_MATCH_DAYS:
            candidates.append((distance, row))
    if not candidates:
        return "NOT_FOUND", ""
    row = sorted(candidates, key=lambda item: item[0])[0][1]
    text = " ".join((row["Headline"], row["KeyFact"], row["Reason"]))
    for status in ("WITHDRAWN", "RAISED", "LOWERED", "REAFFIRMED", "INTRODUCED"):
        if GUIDANCE_PATTERNS[status].search(text):
            return status, row["EventID"]
    return "REVIEW_REQUIRED", row["EventID"]


def fetch_finnhub(ticker: str, filed_at: str) -> list[dict[str, Any]]:
    center = date.fromisoformat(filed_at[:10])
    params = {"from": (center-timedelta(days=10)).isoformat(), "to": (center+timedelta(days=10)).isoformat(), "symbol": ticker}
    payload = get_finnhub_json(f"{FINNHUB_EARNINGS_URL}?{urlencode(params)}")
    rows = payload.get("earningsCalendar", []) if isinstance(payload, dict) else []
    return rows if isinstance(rows, list) else []


def corroborate(filing: dict[str, str], financials: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = {"RevenueEstimate": None, "EPSEstimate": None, "RevenueSurprisePct": None,
            "EPSSurprisePct": None, "Status": "NOT_APPLICABLE"}
    if not financials["HasData"]:
        return base
    filing_date = date.fromisoformat(filing["PublishedAtUTC"][:10])
    candidates = []
    for row in rows:
        if clean(row.get("symbol")).upper() != filing["Ticker"]:
            continue
        try:
            event_date = date.fromisoformat(clean(row.get("date"))[:10])
        except ValueError:
            continue
        distance = abs((filing_date-event_date).days)
        if distance <= 10:
            candidates.append((distance, row))
    if not candidates:
        return base | {"Status": "ECONOMIC_REVIEW_REQUIRED"}
    match = sorted(candidates, key=lambda item: item[0])[0][1]
    revenue_actual = finite_float(match.get("revenueActual"))
    revenue_estimate = finite_float(match.get("revenueEstimate"))
    eps_estimate = finite_float(match.get("epsEstimate"))
    sec_revenue = financials["Revenue"]["Actual"]
    surprise = None if sec_revenue is None or revenue_estimate in (None, 0) else (sec_revenue/revenue_estimate-1)*100
    if sec_revenue is not None and revenue_actual is not None:
        diff = abs(sec_revenue-revenue_actual)/max(abs(sec_revenue), abs(revenue_actual), 1e-12)*100
        status = "ECONOMIC_PARTIAL" if diff <= REVENUE_TOLERANCE_PCT else "ECONOMIC_MISMATCH"
    else:
        status = "ECONOMIC_REVIEW_REQUIRED"
    return {"RevenueEstimate": revenue_estimate, "EPSEstimate": eps_estimate,
            "RevenueSurprisePct": surprise, "EPSSurprisePct": None, "Status": status}


def result_row(filing, financials, corroboration, guidance_status, kpi, observed_at):
    revenue, eps = financials["Revenue"], financials["EPS"]
    needs_review = (
        financials["DataStatus"] != "EXTRACTED_SOURCE_VALIDATED"
        or corroboration["Status"] in {"ECONOMIC_MISMATCH", "ECONOMIC_REVIEW_REQUIRED"}
        or guidance_status == "REVIEW_REQUIRED"
        or kpi["Status"] == "NOT_FOUND"
    )
    return {
        "EventID": filing["EventID"], "AgentVersion": AGENT_VERSION,
        "Ticker": filing["Ticker"], "ObservedAtUTC": observed_at,
        "PublishedAtUTC": filing["PublishedAtUTC"], "EventType": filing["Classification"],
        "Period": financials["Period"],
        "RevenueActual": "" if revenue["Actual"] is None else revenue["Actual"],
        "RevenueConsensus": "" if corroboration["RevenueEstimate"] is None else corroboration["RevenueEstimate"],
        "RevenuePrevious": "" if revenue["Previous"] is None else revenue["Previous"],
        "RevenueSurprisePct": "" if corroboration["RevenueSurprisePct"] is None else round(corroboration["RevenueSurprisePct"], 6),
        "RevenueGrowthPct": "" if financials["RevenueGrowthPct"] is None else round(financials["RevenueGrowthPct"], 6),
        "EPSActual": "" if eps["Actual"] is None else eps["Actual"],
        "EPSConsensus": "" if corroboration["EPSEstimate"] is None else corroboration["EPSEstimate"],
        "EPSPrevious": "" if eps["Previous"] is None else eps["Previous"],
        "EPSSurprisePct": "",
        "GuidanceStatus": guidance_status,
        "KeyKPIName": kpi["Name"],
        "KeyKPIActual": "" if kpi["Actual"] is None else kpi["Actual"],
        "KeyKPIConsensus": "",
        "KeyKPIPrevious": "" if kpi["Previous"] is None else kpi["Previous"],
        "KeyKPIMomentum": kpi["Momentum"],
        "FundamentalAssessment": "", "ExpectationAssessment": "",
        "OverallVerdict": "", "Confidence": "",
        "DataStatus": f"{financials['DataStatus']}|{corroboration['Status']}|KPI_{kpi['Status']}",
        "NeedsHumanReview": needs_review,
        "ObservationOnly": OBSERVATION_ONLY,
        "V1ExecutionChanged": V1_EXECUTION_CHANGED,
        "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
    }


def merge_rows(existing, current):
    merged = {clean(row.get("EventID")): row for row in existing if clean(row.get("EventID"))}
    merged.update({row["EventID"]: row for row in current})
    return sorted(merged.values(), key=lambda row: (clean(row.get("PublishedAtUTC")), clean(row.get("Ticker"))), reverse=True)


def display(value: Optional[float], decimals: int = 4) -> str:
    return "N/A" if value is None else f"{value:,.{decimals}f}"


def main() -> int:
    print("="*115)
    print("EARNINGS & COMPANY AGENT")
    print("SEC/XBRL + CONSENSUS + GUIDANCE + KPI + DIAGNOSTIC XBRL")
    print("="*115)
    print("AgentVersion :", AGENT_VERSION)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("FinnhubConfigured :", bool(FINNHUB_API_KEY))
    if not FINNHUB_API_KEY:
        print("ERREUR : FINNHUB_API_KEY absent")
        return 2

    watchlist = set(load_tickers())
    semantics = semantic_candidates(watchlist, load_event_map())
    filings, review = sec_candidates(watchlist)
    print("Tickers charges :", len(watchlist))
    print("Candidats semantiques :", len(semantics))
    print("Publications SEC retenues :", len(filings))
    print("Filings SEC a revoir :", len(review))

    facts_cache, finnhub_cache = {}, {}
    output, kpi_counts = [], {}
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for index, filing in enumerate(filings, 1):
        cik = filing["CIK"]
        if cik not in facts_cache:
            try:
                facts_cache[cik] = get_sec_json(f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json")
                time.sleep(REQUEST_PAUSE)
            except Exception as exc:
                print(f"{filing['Ticker']} | ERREUR SEC : {exc}")
                facts_cache[cik] = {}
        facts = facts_cache[cik]
        financials = extract_financials(filing, facts)
        kpi = extract_kpi(facts, filing)
        guidance_status, guidance_event = guidance_for_filing(filing, semantics)
        key = (filing["Ticker"], filing["PublishedAtUTC"][:10])
        if key not in finnhub_cache:
            try:
                finnhub_cache[key] = fetch_finnhub(*key)
                time.sleep(REQUEST_PAUSE)
            except Exception as exc:
                print(f"{filing['Ticker']} | ERREUR FINNHUB : {exc}")
                finnhub_cache[key] = []
        corroboration = corroborate(filing, financials, finnhub_cache[key])
        output.append(result_row(filing, financials, corroboration, guidance_status, kpi, observed_at))
        kpi_counts[kpi["Status"]] = kpi_counts.get(kpi["Status"], 0)+1

        print(f"\n{index}. {filing['Ticker']} | {filing['PublishedAtUTC']} | {filing['Form']}")
        print("Period :", financials["Period"])
        print("GuidanceStatus :", guidance_status)
        print("GuidanceEventID :", guidance_event or "N/A")
        print("KeyKPIName :", kpi["Name"] or "N/A")
        print("KeyKPIConcept :", kpi["Concept"] or "N/A")
        print("KeyKPIActual :", display(kpi["Actual"]))
        print("KeyKPIPrevious :", display(kpi["Previous"]))
        print("KeyKPIMomentum :", kpi["Momentum"])
        print("KPIStatus :", kpi["Status"])
        if kpi["Status"] == "NOT_FOUND":
            print_kpi_diagnostic(diagnose_kpi_concepts(facts, filing))
        print("DataStatus :", financials["DataStatus"])

    merged = merge_rows(read_csv(EARNINGS_RESULTS_FILE), output)
    write_csv(EARNINGS_RESULTS_FILE, merged)

    print("\n"+"="*115)
    print("RESUME KPI")
    print("="*115)
    print("Resultats ecrits cette execution :", len(output))
    print("Total lignes Earnings conservees :", len(merged))
    print("Statuts KPI :")
    for status, count in sorted(kpi_counts.items()):
        print(status, ":", count)
    print("Fichier resultats :", EARNINGS_RESULTS_FILE)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("="*115)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
