#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Baseline US SEC/XBRL + branche Europe IR additive + intelligence de trajectoire KPI.
Mode Shadow uniquement. Aucun impact sur le moteur ou le portefeuille V1.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import xml.etree.ElementTree as ET
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
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
REQUEST_TIMEOUT = 25
REQUEST_PAUSE = 0.15
REVENUE_TOLERANCE_PCT = 2.0
KPI_STABLE_THRESHOLD_PCT = 1.0
EPS_SECONDARY_STATUS = "SECONDARY_BASIS_UNKNOWN"

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

KPI_MAP = {
    "TSLA": "Automotive Revenue",
    "META": "Family of Apps Revenue",
    "AMZN": "AWS Net Sales",
    "GOOGL": "Google Cloud Revenue",
    "MSFT": "Intelligent Cloud Revenue",
    "VLO": "Refining Revenue",
    "NVDA": "Data Center Revenue",
    "AAPL": "Services Revenue",
    "SPCX": "Starlink Revenue",
}
KPI_RULES = {
    "TSLA": (
        {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "tsla:AutomotiveRevenuesMember",
        set(),
    ),
    "META": (
        {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "meta:FamilyOfAppsMember",
        {"srt:ProductOrServiceAxis"},
    ),
    "AMZN": (
        {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "amzn:AmazonWebServicesSegmentMember",
        {"srt:ConsolidationItemsAxis"},
    ),
    "GOOGL": (
        {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "goog:GoogleCloudMember",
        {"srt:ConsolidationItemsAxis"},
    ),
    "MSFT": (
        {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "msft:IntelligentCloudMember",
        set(),
    ),
    "VLO": (
        {"RevenueFromContractWithCustomerIncludingAssessedTax"},
        "vlo:RefiningMember",
        {"srt:ConsolidationItemsAxis"},
    ),
}

# Les données européennes sont explicitement sourcées et normalisées.
# Pour Safran, le signal de décélération est un signal de marché qualitatif.
# Il ne fabrique aucune valeur historique absente.
EUROPE_IR_EVENTS = [
    {
        "aliases": {"SAF.PA", "SAF"},
        "EventID": "IR|SAF.PA|2026-10-23|Q3-2026",
        "Ticker": "SAF.PA",
        "PublishedAtUTC": "2026-10-23",
        "EventType": "ACTUAL_QUARTERLY_REVENUE",
        "Period": "FY2026-Q3",
        "Currency": "EUR",
        "RevenueActual": 9_003_000_000.0,
        "RevenueConsensus": 8_990_000_000.0,
        "RevenuePrevious": None,
        "RevenueGrowthPct": 14.7,
        "EPSActual": None,
        "EPSConsensus": None,
        "EPSPrevious": None,
        "GuidanceStatus": "REAFFIRMED",
        "KeyKPIName": "Civil engine aftermarket organic growth (%)",
        "KeyKPIActual": 18.7,
        "KeyKPIConsensus": None,
        "KeyKPIPrevious": None,
        "KeyKPILevelStatus": "POSITIVE",
        "KeyKPIHistory": "Q1_2026:32.1|H1_2026:27.7|Q3_2026:18.7",
        "KeyKPITrendBasis": "REPORTED_RATE_SEQUENCE",
        "KeyKPITrend": "DECELERATING_REPORTED_RATES",
        "SparePartsGrowthHistory": "Q1_2026:29.3|H1_2026:27.9|Q3_2026:17.0",
        "ServicesGrowthHistory": "Q1_2026:43.1|H1_2026:40.4|Q3_2026:19.0",
        "KPIExpectationAssessment": "REVIEW_REQUIRED",
        "MarketReactionAssessment": "NEGATIVE",
        "RiskFlags": "AFTERMARKET_REPORTED_RATE_DECELERATION|POSITIVE_RESULTS_NEGATIVE_REACTION",
        "MissingInformation": "KPI_PREVIOUS_COMPARABLE_UNAVAILABLE|KPI_CONSENSUS_UNAVAILABLE",
        "SourceURL": "https://www.safran-group.com/download/press_release/file/2278933",
        "ConsensusURL": "https://www.safran-group.com/fr/download/media/452055",
        "MarketContextURL": "https://www.reuters.com/business/aerospace-defense/safran-posts-slightly-higher-than-expected-q3-sales-2026-10-23/",
    },
    {
        "aliases": {"ASML.AS", "ASML"},
        "EventID": "IR|ASML.AS|2026-10-14|Q3-2026",
        "Ticker": "ASML.AS",
        "PublishedAtUTC": "2026-10-14",
        "EventType": "ACTUAL_QUARTERLY_RESULTS",
        "Period": "FY2026-Q3",
        "Currency": "EUR",
        "RevenueActual": 11_662_000_000.0,
        "RevenueConsensus": None,
        "RevenuePrevious": None,
        "RevenueGrowthPct": None,
        "EPSActual": 12.95,
        "EPSConsensus": None,
        "EPSPrevious": None,
        "GuidanceStatus": "INTRODUCED",
        "KeyKPIName": "Installed Base Management sales",
        "KeyKPIActual": 3_289_000_000.0,
        "KeyKPIConsensus": None,
        "KeyKPIPrevious": None,
        "KeyKPILevelStatus": "POSITIVE",
        "KeyKPIHistory": "",
        "KeyKPITrendBasis": "INSUFFICIENT_HISTORY",
        "KeyKPITrend": "UNKNOWN",
        "SparePartsGrowthHistory": "",
        "ServicesGrowthHistory": "",
        "KPIExpectationAssessment": "UNKNOWN",
        "MarketReactionAssessment": "UNKNOWN",
        "RiskFlags": "",
        "MissingInformation": "KPI_PREVIOUS_COMPARABLE_UNAVAILABLE|KPI_CONSENSUS_UNAVAILABLE|MARKET_REACTION_UNAVAILABLE",
        "SourceURL": "https://investor.asml.com/news-releases/news-release-details/asml-reports-eu11.7-billion-total-net-sales-and-eu5.0-billion-net-income-q3-2026",
        "ConsensusURL": "",
        "MarketContextURL": "",
    },
]
EUROPE_PENDING = [
    {
        "aliases": {"AIR.PA", "AIR"},
        "Ticker": "AIR.PA",
        "ScheduledDate": "2026-10-28",
        "SourceURL": "https://www.airbus.com/en/investors/financial-results",
    }
]

RESULT_COLUMNS = [
    "EventID", "AgentVersion", "Ticker", "ObservedAtUTC", "PublishedAtUTC",
    "EventType", "Period", "RevenueActual", "RevenueConsensus",
    "RevenuePrevious", "RevenueSurprisePct", "RevenueGrowthPct", "EPSActual",
    "EPSConsensus", "EPSPrevious", "EPSSurprisePct", "GuidanceStatus",
    "KeyKPIName", "KeyKPIActual", "KeyKPIConsensus", "KeyKPIPrevious",
    "KeyKPIChange", "KeyKPILevelStatus", "KeyKPIHistory",
    "KeyKPITrendBasis", "KeyKPITrend", "SparePartsGrowthHistory",
    "ServicesGrowthHistory", "KPIExpectationAssessment", "FundamentalAssessment",
    "ExpectationAssessment", "MarketReactionAssessment", "OverallVerdict",
    "OverallVerdictReason", "Confidence", "RiskFlags", "MissingInformation",
    "DataStatus", "NeedsHumanReview", "ObservationOnly", "V1ExecutionChanged",
    "PortfolioV1Changed", "SourceType", "SourceAuthority", "SourceURL",
    "ConsensusURL", "MarketContextURL", "Currency",
]


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def norm(value: Any) -> str:
    return clean(value).upper().replace("_", "-")


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


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_tickers() -> list[str]:
    return [
        line.strip().upper()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def get_bytes(url: str, headers: dict[str, str]) -> bytes:
    try:
        with urlopen(Request(url, headers=headers), timeout=REQUEST_TIMEOUT) as response:
            return response.read()
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body[:200]}") from error
    except URLError as error:
        raise RuntimeError(f"Network: {error.reason}") from error


def sec_json(url: str) -> Any:
    return json.loads(get_bytes(url, {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}).decode())


def sec_xml(url: str) -> bytes:
    return get_bytes(url, {"User-Agent": SEC_USER_AGENT, "Accept": "application/xml,text/xml,*/*"})


def finnhub_json(url: str) -> Any:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY absent")
    return json.loads(get_bytes(url, {"X-Finnhub-Token": FINNHUB_API_KEY, "Accept": "application/json"}).decode())


def duration_days(row: dict[str, Any]) -> Optional[int]:
    try:
        return (date.fromisoformat(clean(row.get("end"))) - date.fromisoformat(clean(row.get("start")))).days
    except ValueError:
        return None


def fiscal_year(row: dict[str, Any]) -> Optional[int]:
    try:
        return int(clean(row.get("fy")))
    except (TypeError, ValueError):
        return None


def valid_duration(row: dict[str, Any], form: str) -> bool:
    duration = duration_days(row)
    form = norm(form)
    if form == "10-Q":
        return duration is not None and 70 <= duration <= 110
    if form in {"10-K", "20-F", "40-F"}:
        return duration is not None and 330 <= duration <= 400
    return True


def select_current(entries: list[dict[str, Any]], accession: str, form: str) -> Optional[dict[str, Any]]:
    form = norm(form)
    rows = [
        row for row in entries
        if clean(row.get("accn")) == accession
        and (not clean(row.get("form")) or norm(row.get("form")) == form)
        and valid_duration(row, form)
        and finite_float(row.get("val")) is not None
    ]
    if not rows:
        return None
    target = 91 if form == "10-Q" else 365
    wanted = {"Q1", "Q2", "Q3"} if form == "10-Q" else {"FY"}
    return max(rows, key=lambda row: (
        1 if clean(row.get("fp")).upper() in wanted else 0,
        -abs((duration_days(row) or target) - target),
        clean(row.get("end")),
    ))


def select_previous(entries: list[dict[str, Any]], current: dict[str, Any], accession: str, form: str) -> Optional[dict[str, Any]]:
    try:
        current_end = date.fromisoformat(clean(current.get("end")))
    except ValueError:
        return None
    rows = []
    for row in entries:
        if clean(row.get("accn")) == accession or finite_float(row.get("val")) is None:
            continue
        if not valid_duration(row, form):
            continue
        if clean(row.get("fp")).upper() != clean(current.get("fp")).upper():
            continue
        current_fy = fiscal_year(current)
        if current_fy is not None and fiscal_year(row) != current_fy - 1:
            continue
        try:
            delta = (current_end - date.fromisoformat(clean(row.get("end")))).days
        except ValueError:
            continue
        if 330 <= delta <= 400:
            rows.append(row)
    return max(rows, key=lambda row: clean(row.get("filed"))) if rows else None


def extract_metric(facts: dict[str, Any], concepts: tuple[str, ...], accession: str, form: str, units: tuple[str, ...]) -> dict[str, Any]:
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for concept in concepts:
        for unit in units:
            entries = gaap.get(concept, {}).get("units", {}).get(unit, [])
            current = select_current(entries, accession, form)
            if current:
                previous = select_previous(entries, current, accession, form)
                return {
                    "Actual": finite_float(current.get("val")),
                    "Previous": finite_float(previous.get("val")) if previous else None,
                    "FY": clean(current.get("fy")),
                    "FP": clean(current.get("fp")),
                    "Accession": clean(current.get("accn")),
                    "Form": clean(current.get("form")),
                }
    return {"Actual": None, "Previous": None, "FY": "", "FP": "", "Accession": "", "Form": ""}


def load_semantics(watchlist: set[str]) -> list[dict[str, str]]:
    events = {clean(row.get("EventID")): row for row in read_csv(CATALYST_EVENTS_FILE)}
    output = []
    for row in read_csv(CATALYST_SEMANTIC_FILE):
        ticker = clean(row.get("Ticker")).upper()
        event_id = clean(row.get("EventID"))
        catalyst_type = norm(row.get("CatalystType"))
        category = norm(row.get("Category"))
        relevant = clean(row.get("IsRelevant")).upper() in {"TRUE", "1", "YES"}
        if ticker not in watchlist or not event_id or not relevant:
            continue
        if catalyst_type not in {"EARNINGS", "GUIDANCE"} and category not in {"EARNINGS", "GUIDANCE"}:
            continue
        event = events.get(event_id, {})
        output.append({
            "Ticker": ticker,
            "EventID": event_id,
            "PublishedAtUTC": clean(row.get("FirstSeenUTC") or event.get("FirstSeenUTC")),
            "CatalystType": catalyst_type,
            "Category": category,
            "Text": " ".join([clean(row.get("RepresentativeTitle")), clean(row.get("KeyFact")), clean(row.get("Reason"))]),
        })
    return output


def load_filings(watchlist: set[str]) -> list[dict[str, str]]:
    output = []
    for row in read_csv(CATALYST_SEC_FILINGS_FILE):
        ticker = clean(row.get("Ticker")).upper()
        accession = clean(row.get("AccessionNumber"))
        form = norm(row.get("Form"))
        description = clean(row.get("Description"))
        if ticker not in watchlist or not accession:
            continue
        if form == "10-Q":
            event_type = "ACTUAL_QUARTERLY_FILING"
        elif form in {"10-K", "20-F", "40-F"}:
            event_type = "ACTUAL_ANNUAL_FILING"
        elif form in {"8-K", "6-K"} and EARNINGS_WORDS.search(description):
            event_type = "ACTUAL_EARNINGS_RELEASE"
        else:
            continue
        output.append({
            "EventID": f"SEC|{ticker}|{accession}",
            "Ticker": ticker,
            "PublishedAtUTC": clean(row.get("FiledAt")),
            "Classification": event_type,
            "Form": clean(row.get("Form")),
            "AccessionNumber": accession,
            "CIK": clean(row.get("CIK")).zfill(10),
            "FilingURL": clean(row.get("FilingURL")),
        })
    return sorted(output, key=lambda item: (item["PublishedAtUTC"], item["Ticker"]), reverse=True)


def extract_financials(filing: dict[str, str], facts: dict[str, Any]) -> dict[str, Any]:
    accession = filing["AccessionNumber"]
    form = filing["Form"]
    revenue = extract_metric(facts, REVENUE_CONCEPTS, accession, form, ("USD",))
    eps = extract_metric(facts, DILUTED_EPS_CONCEPTS, accession, form, ("USD/shares", "USD / shares"))
    if eps["Actual"] is None:
        eps = extract_metric(facts, BASIC_EPS_CONCEPTS, accession, form, ("USD/shares", "USD / shares"))
    has_data = revenue["Actual"] is not None or eps["Actual"] is not None
    source_valid = all(
        metric["Actual"] is None or (
            metric["Accession"] == accession
            and (not metric["Form"] or norm(metric["Form"]) == norm(form))
        )
        for metric in (revenue, eps)
    )
    status = "NO_MATCHING_XBRL_FACT" if not has_data else (
        "EXTRACTED_SOURCE_VALIDATED" if source_valid else "SOURCE_REVIEW_REQUIRED"
    )
    selected = revenue if revenue["Actual"] is not None else eps
    period = f"FY{selected['FY']}-{selected['FP']}" if selected["FY"] and selected["FP"] else "UNKNOWN"
    growth = None if revenue["Actual"] is None or revenue["Previous"] in (None, 0) else (revenue["Actual"] / revenue["Previous"] - 1) * 100
    return {"Revenue": revenue, "EPS": eps, "HasData": has_data, "DataStatus": status, "Period": period, "RevenueGrowthPct": growth}


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def archive_directory(filing: dict[str, str]) -> str:
    return f"{SEC_ARCHIVES_BASE}/{int(filing['CIK'])}/{filing['AccessionNumber'].replace('-', '')}"


def discover_instance_document(filing: dict[str, str]) -> str:
    directory = archive_directory(filing)
    index = sec_json(f"{directory}/index.json")
    names = []
    for item in index.get("directory", {}).get("item", []):
        name = clean(item.get("name"))
        lower = name.lower()
        if not lower.endswith((".xml", ".xbrl")):
            continue
        if lower in {"filingsummary.xml", "metafiles.xml", "metalinks.json"}:
            continue
        if lower.endswith(("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_ref.xml", "_sch.xml")):
            continue
        names.append(name)
    for name in names:
        try:
            root = ET.fromstring(sec_xml(f"{directory}/{name}"))
        except Exception:
            continue
        if local_tag(root.tag).lower() == "xbrl":
            return name
    return ""


def context_map(root: ET.Element) -> dict[str, dict[str, str]]:
    output = {}
    for element in root.iter():
        if local_tag(element.tag) != "context":
            continue
        context_id = clean(element.attrib.get("id"))
        dimensions = {}
        start = end = instant = ""
        for child in element.iter():
            name = local_tag(child.tag)
            if name in {"explicitMember", "typedMember"}:
                dimensions[clean(child.attrib.get("dimension"))] = clean(" ".join(child.itertext()))
            elif name == "startDate":
                start = clean(child.text)
            elif name == "endDate":
                end = clean(child.text)
            elif name == "instant":
                instant = clean(child.text)
        output[context_id] = {
            "Dimensions": " | ".join(f"{key}={value}" for key, value in sorted(dimensions.items())),
            "Start": start,
            "End": end or instant,
        }
    return output


def extract_dimensional_kpi(filing: dict[str, str]) -> dict[str, Any]:
    name = KPI_MAP.get(filing["Ticker"], "")
    rule = KPI_RULES.get(filing["Ticker"])
    if not name:
        return {"Name": "", "Concept": "", "Actual": None, "Previous": None, "Change": None, "LevelStatus": "UNKNOWN", "Trend": "UNKNOWN", "Status": "NO_MAPPING"}
    if not rule:
        return {"Name": name, "Concept": "", "Actual": None, "Previous": None, "Change": None, "LevelStatus": "UNKNOWN", "Trend": "UNKNOWN", "Status": "NOT_FOUND"}
    document = discover_instance_document(filing)
    if not document:
        return {"Name": name, "Concept": "", "Actual": None, "Previous": None, "Change": None, "LevelStatus": "UNKNOWN", "Trend": "UNKNOWN", "Status": "NOT_FOUND"}
    root = ET.fromstring(sec_xml(f"{archive_directory(filing)}/{document}"))
    contexts = context_map(root)
    concepts, member, forbidden = rule
    rows = []
    for fact in root.iter():
        context_ref = clean(fact.attrib.get("contextRef"))
        value = finite_float(clean(fact.text))
        concept = local_tag(fact.tag)
        context = contexts.get(context_ref, {})
        dimensions = clean(context.get("Dimensions"))
        if not context_ref or value is None or concept not in concepts or member not in dimensions:
            continue
        if any(item in dimensions for item in forbidden):
            continue
        try:
            duration = (date.fromisoformat(context["End"]) - date.fromisoformat(context["Start"])).days
        except (KeyError, ValueError):
            continue
        form = norm(filing["Form"])
        if (form == "10-Q" and 70 <= duration <= 110) or (form in {"10-K", "20-F", "40-F"} and 330 <= duration <= 400):
            rows.append({"Value": value, "Start": context["Start"], "End": context["End"], "Dimensions": dimensions, "Concept": concept})
    if not rows:
        return {"Name": name, "Concept": "", "Actual": None, "Previous": None, "Change": None, "LevelStatus": "UNKNOWN", "Trend": "UNKNOWN", "Status": "NOT_FOUND"}
    current = max(rows, key=lambda row: row["End"])
    current_end = date.fromisoformat(current["End"])
    previous_rows = [
        row for row in rows
        if row["Dimensions"] == current["Dimensions"]
        and 330 <= (current_end - date.fromisoformat(row["End"])).days <= 400
    ]
    previous = max(previous_rows, key=lambda row: row["End"]) if previous_rows else None
    previous_value = previous["Value"] if previous else None
    change = None if previous_value in (None, 0) else (current["Value"] / previous_value - 1) * 100
    if change is None:
        trend = "UNKNOWN"
    elif change > KPI_STABLE_THRESHOLD_PCT:
        trend = "GROWING"
    elif change < -KPI_STABLE_THRESHOLD_PCT:
        trend = "DECLINING"
    else:
        trend = "STABLE"
    return {
        "Name": name,
        "Concept": f"{current['Concept']} | {current['Dimensions']}",
        "Actual": current["Value"],
        "Previous": previous_value,
        "Change": change,
        "LevelStatus": "POSITIVE" if current["Value"] > 0 else "NEGATIVE",
        "Trend": trend,
        "Status": "EXTRACTED_DIMENSIONAL",
    }


def guidance_status(filing: dict[str, str], semantics: list[dict[str, str]]) -> str:
    try:
        filing_date = date.fromisoformat(filing["PublishedAtUTC"][:10])
    except ValueError:
        return "NOT_FOUND"
    candidates = []
    for row in semantics:
        if row["Ticker"] != filing["Ticker"]:
            continue
        if row["CatalystType"] != "GUIDANCE" and row["Category"] != "GUIDANCE":
            continue
        try:
            distance = abs((filing_date - date.fromisoformat(row["PublishedAtUTC"][:10])).days)
        except ValueError:
            continue
        if distance <= 3:
            candidates.append((distance, row))
    if not candidates:
        return "NOT_FOUND"
    text = min(candidates, key=lambda item: item[0])[1]["Text"].lower()
    for status, pattern in (
        ("WITHDRAWN", r"withdraw|suspend"),
        ("RAISED", r"raise|increase|boost|lift"),
        ("LOWERED", r"lower|cut|reduce"),
        ("REAFFIRMED", r"reaffirm|reiterate|maintain|confirm"),
    ):
        if re.search(pattern, text):
            return status
    return "INTRODUCED" if re.search(r"guidance|outlook", text) else "REVIEW_REQUIRED"


def fetch_finnhub(ticker: str, published_at: str) -> list[dict[str, Any]]:
    center = date.fromisoformat(published_at[:10])
    query = urlencode({
        "from": (center - timedelta(days=10)).isoformat(),
        "to": (center + timedelta(days=10)).isoformat(),
        "symbol": ticker,
    })
    payload = finnhub_json(f"{FINNHUB_EARNINGS_URL}?{query}")
    return payload.get("earningsCalendar", []) if isinstance(payload, dict) else []


def corroborate(filing: dict[str, str], financials: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not financials["HasData"]:
        return {"RevenueEstimate": None, "EPSEstimate": None, "RevenueSurprisePct": None, "Status": "NOT_APPLICABLE"}
    filing_date = date.fromisoformat(filing["PublishedAtUTC"][:10])
    candidates = []
    for row in rows:
        try:
            distance = abs((filing_date - date.fromisoformat(clean(row.get("date"))[:10])).days)
        except ValueError:
            continue
        if clean(row.get("symbol")).upper() == filing["Ticker"] and distance <= 10:
            candidates.append((distance, row))
    if not candidates:
        return {"RevenueEstimate": None, "EPSEstimate": None, "RevenueSurprisePct": None, "Status": "ECONOMIC_REVIEW_REQUIRED"}
    match = min(candidates, key=lambda item: item[0])[1]
    secondary_actual = finite_float(match.get("revenueActual"))
    revenue_estimate = finite_float(match.get("revenueEstimate"))
    eps_estimate = finite_float(match.get("epsEstimate"))
    primary_actual = financials["Revenue"]["Actual"]
    surprise = None if primary_actual is None or revenue_estimate in (None, 0) else (primary_actual / revenue_estimate - 1) * 100
    if primary_actual is None or secondary_actual is None:
        status = "ECONOMIC_REVIEW_REQUIRED"
    else:
        difference = abs(primary_actual - secondary_actual) / max(abs(primary_actual), abs(secondary_actual), 1e-12) * 100
        status = "ECONOMIC_PARTIAL" if difference <= REVENUE_TOLERANCE_PCT else "ECONOMIC_MISMATCH"
    return {"RevenueEstimate": revenue_estimate, "EPSEstimate": eps_estimate, "RevenueSurprisePct": surprise, "Status": status}


def fundamental_assessment(financials: dict[str, Any], guidance: str, kpi: dict[str, Any]) -> tuple[str, str]:
    if not financials["HasData"] and kpi["Status"] != "EXTRACTED_DIMENSIONAL":
        return "UNKNOWN", "Données fondamentales insuffisantes"
    positive = negative = 0
    evidence = []
    growth = financials.get("RevenueGrowthPct")
    if growth is not None:
        if growth > 1:
            positive += 1; evidence.append("REVENUE_GROWTH_UP")
        elif growth < -1:
            negative += 1; evidence.append("REVENUE_GROWTH_DOWN")
        else:
            evidence.append("REVENUE_GROWTH_STABLE")
    if kpi["LevelStatus"] == "POSITIVE":
        positive += 1; evidence.append("KPI_LEVEL_POSITIVE")
    if kpi["Trend"] in {"DECELERATING_REPORTED_RATES", "DECLINING"}:
        negative += 1; evidence.append("KPI_DECELERATING")
    elif kpi["Trend"] == "GROWING":
        positive += 1; evidence.append("KPI_GROWING")
    if guidance == "RAISED":
        positive += 1; evidence.append("GUIDANCE_RAISED")
    elif guidance in {"LOWERED", "WITHDRAWN"}:
        negative += 1; evidence.append(f"GUIDANCE_{guidance}")
    elif guidance == "REAFFIRMED":
        evidence.append("GUIDANCE_REAFFIRMED")
    status = "POSITIVE" if positive and not negative else (
        "NEGATIVE" if negative and not positive else ("MIXED" if positive and negative else "NEUTRAL")
    )
    return status, "|".join(evidence) if evidence else "Aucun signal directionnel validé"


def expectation_assessment(surprise: Optional[float]) -> tuple[str, str]:
    if surprise is None:
        return "UNKNOWN", "Consensus Revenue comparable indisponible"
    if surprise > 1:
        return "BEAT", f"REVENUE_SURPRISE={surprise:.4f}%"
    if surprise < -1:
        return "MISS", f"REVENUE_SURPRISE={surprise:.4f}%"
    return "IN_LINE", f"REVENUE_SURPRISE={surprise:.4f}%"


def overall_verdict(
    data_status: str,
    economic_status: str,
    guidance: str,
    fundamental: str,
    expectation: str,
    kpi_trend: str,
    market_reaction: str,
) -> tuple[str, str, str]:
    if data_status != "EXTRACTED_SOURCE_VALIDATED" and not data_status.startswith("EUROPE_IR_PRIMARY"):
        return "UNKNOWN", "DATA_STATUS_NOT_FULLY_VALIDATED", "LOW"
    if economic_status == "ECONOMIC_MISMATCH":
        return "UNKNOWN", "ECONOMIC_MISMATCH_REQUIRES_REVIEW", "LOW"
    if guidance == "REVIEW_REQUIRED":
        return "UNKNOWN", "GUIDANCE_REQUIRES_REVIEW", "LOW"
    if fundamental == "UNKNOWN":
        return "UNKNOWN", "FUNDAMENTALS_INSUFFICIENT", "LOW"
    if market_reaction == "NEGATIVE" and fundamental == "POSITIVE":
        return "MIXED", "POSITIVE_RESULTS_NEGATIVE_REACTION", "MEDIUM"
    if kpi_trend in {"DECELERATING_REPORTED_RATES", "DECLINING"} and fundamental == "POSITIVE":
        return "MIXED", "POSITIVE_LEVEL_WITH_KPI_DECELERATION", "MEDIUM"
    if (fundamental, expectation) in {("POSITIVE", "MISS"), ("NEGATIVE", "BEAT")}:
        return "MIXED", f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_{expectation}", "MEDIUM"
    confidence = "HIGH" if expectation != "UNKNOWN" else "MEDIUM"
    return fundamental, f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_{expectation}", confidence


def europe_rows(watchlist: set[str], observed_at: str) -> list[dict[str, Any]]:
    output = []
    for event in EUROPE_IR_EVENTS:
        matches = sorted(alias for alias in event["aliases"] if alias in watchlist)
        if not matches:
            continue
        consensus = event["RevenueConsensus"]
        surprise = None if consensus in (None, 0) else (event["RevenueActual"] / consensus - 1) * 100
        expectation, expectation_reason = expectation_assessment(surprise)
        kpi = {
            "Status": "CURATED_PRIMARY",
            "LevelStatus": event["KeyKPILevelStatus"],
            "Trend": event["KeyKPITrend"],
        }
        fake_financials = {"HasData": True, "RevenueGrowthPct": event["RevenueGrowthPct"]}
        fundamental, fundamental_reason = fundamental_assessment(fake_financials, event["GuidanceStatus"], kpi)
        verdict, verdict_reason, confidence = overall_verdict(
            "EUROPE_IR_PRIMARY|CURATED_NORMALIZED|SHADOW",
            "NOT_APPLICABLE",
            event["GuidanceStatus"],
            fundamental,
            expectation,
            event["KeyKPITrend"],
            event["MarketReactionAssessment"],
        )
        needs_review = bool(event["MissingInformation"]) or verdict == "UNKNOWN"
        output.append({
            "EventID": event["EventID"], "AgentVersion": AGENT_VERSION,
            "Ticker": matches[0], "ObservedAtUTC": observed_at,
            "PublishedAtUTC": event["PublishedAtUTC"], "EventType": event["EventType"],
            "Period": event["Period"], "RevenueActual": event["RevenueActual"],
            "RevenueConsensus": "" if consensus is None else consensus,
            "RevenuePrevious": "" if event["RevenuePrevious"] is None else event["RevenuePrevious"],
            "RevenueSurprisePct": "" if surprise is None else round(surprise, 6),
            "RevenueGrowthPct": "" if event["RevenueGrowthPct"] is None else event["RevenueGrowthPct"],
            "EPSActual": "" if event["EPSActual"] is None else event["EPSActual"],
            "EPSConsensus": "" if event["EPSConsensus"] is None else event["EPSConsensus"],
            "EPSPrevious": "" if event["EPSPrevious"] is None else event["EPSPrevious"],
            "EPSSurprisePct": "", "GuidanceStatus": event["GuidanceStatus"],
            "KeyKPIName": event["KeyKPIName"], "KeyKPIActual": event["KeyKPIActual"],
            "KeyKPIConsensus": "" if event["KeyKPIConsensus"] is None else event["KeyKPIConsensus"],
            "KeyKPIPrevious": "" if event["KeyKPIPrevious"] is None else event["KeyKPIPrevious"],
            "KeyKPIChange": "", "KeyKPILevelStatus": event["KeyKPILevelStatus"],
            "KeyKPIHistory": event["KeyKPIHistory"],
            "KeyKPITrendBasis": event["KeyKPITrendBasis"],
            "KeyKPITrend": event["KeyKPITrend"],
            "SparePartsGrowthHistory": event["SparePartsGrowthHistory"],
            "ServicesGrowthHistory": event["ServicesGrowthHistory"],
            "KPIExpectationAssessment": event["KPIExpectationAssessment"],
            "FundamentalAssessment": fundamental, "ExpectationAssessment": expectation,
            "MarketReactionAssessment": event["MarketReactionAssessment"],
            "OverallVerdict": verdict, "OverallVerdictReason": verdict_reason,
            "Confidence": confidence, "RiskFlags": event["RiskFlags"],
            "MissingInformation": event["MissingInformation"],
            "DataStatus": "EUROPE_IR_PRIMARY|CURATED_NORMALIZED|SHADOW",
            "NeedsHumanReview": needs_review, "ObservationOnly": OBSERVATION_ONLY,
            "V1ExecutionChanged": V1_EXECUTION_CHANGED,
            "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
            "SourceType": "EUROPE_IR", "SourceAuthority": "PRIMARY",
            "SourceURL": event["SourceURL"], "ConsensusURL": event["ConsensusURL"],
            "MarketContextURL": event["MarketContextURL"], "Currency": event["Currency"],
        })
        print("FundamentalReason :", fundamental_reason)
        print("ExpectationReason :", expectation_reason)
    return output


def merge_rows(existing: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {clean(row.get("EventID")): row for row in existing if clean(row.get("EventID"))}
    merged.update({row["EventID"]: row for row in current})
    return sorted(merged.values(), key=lambda row: (clean(row.get("PublishedAtUTC")), clean(row.get("Ticker"))), reverse=True)


def main() -> int:
    print("=" * 115)
    print("EARNINGS & COMPANY AGENT")
    print("US SEC/XBRL + EUROPE IR + KPI MULTI-PERIOD HISTORY + MARKET DIVERGENCE SHADOW")
    print("=" * 115)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)

    watchlist = set(load_tickers())
    semantics = load_semantics(watchlist)
    filings = load_filings(watchlist)
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    facts_cache: dict[str, dict[str, Any]] = {}
    finnhub_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    output = []

    print("Tickers charges :", len(watchlist))
    print("Candidats semantiques :", len(semantics))
    print("Publications SEC retenues :", len(filings))

    for filing in filings:
        cik = filing["CIK"]
        if cik not in facts_cache:
            try:
                facts_cache[cik] = sec_json(f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json")
                time.sleep(REQUEST_PAUSE)
            except Exception as error:
                print(filing["Ticker"], "SEC ERROR", error)
                facts_cache[cik] = {}

        financials = extract_financials(filing, facts_cache[cik])
        kpi = extract_dimensional_kpi(filing)
        guidance = guidance_status(filing, semantics)
        cache_key = (filing["Ticker"], filing["PublishedAtUTC"][:10])
        if cache_key not in finnhub_cache:
            try:
                finnhub_cache[cache_key] = fetch_finnhub(*cache_key)
                time.sleep(REQUEST_PAUSE)
            except Exception:
                finnhub_cache[cache_key] = []
        corroboration = corroborate(filing, financials, finnhub_cache[cache_key])
        fundamental, fundamental_reason = fundamental_assessment(financials, guidance, kpi)
        expectation, expectation_reason = expectation_assessment(corroboration["RevenueSurprisePct"])
        missing = []
        if kpi["Previous"] is None:
            missing.append("KPI_PREVIOUS_COMPARABLE_UNAVAILABLE")
        missing.append("KPI_CONSENSUS_UNAVAILABLE")
        missing.append("MARKET_REACTION_UNAVAILABLE")
        verdict, verdict_reason, confidence = overall_verdict(
            financials["DataStatus"], corroboration["Status"], guidance,
            fundamental, expectation, kpi["Trend"], "UNKNOWN",
        )
        revenue = financials["Revenue"]
        eps = financials["EPS"]
        risk_flags = "KPI_DECLINING" if kpi["Trend"] == "DECLINING" else ""
        output.append({
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
            "EPSSurprisePct": "", "GuidanceStatus": guidance,
            "KeyKPIName": kpi["Name"], "KeyKPIActual": "" if kpi["Actual"] is None else kpi["Actual"],
            "KeyKPIConsensus": "", "KeyKPIPrevious": "" if kpi["Previous"] is None else kpi["Previous"],
            "KeyKPIChange": "" if kpi["Change"] is None else round(kpi["Change"], 6),
            "KeyKPILevelStatus": kpi["LevelStatus"],
            "KeyKPIHistory": "",
            "KeyKPITrendBasis": "YOY_LEVEL_CHANGE" if kpi["Change"] is not None else "INSUFFICIENT_HISTORY",
            "KeyKPITrend": kpi["Trend"],
            "SparePartsGrowthHistory": "",
            "ServicesGrowthHistory": "",
            "KPIExpectationAssessment": "UNKNOWN",
            "FundamentalAssessment": fundamental, "ExpectationAssessment": expectation,
            "MarketReactionAssessment": "UNKNOWN", "OverallVerdict": verdict,
            "OverallVerdictReason": verdict_reason, "Confidence": confidence,
            "RiskFlags": risk_flags, "MissingInformation": "|".join(missing),
            "DataStatus": f"{financials['DataStatus']}|{corroboration['Status']}|KPI_{kpi['Status']}",
            "NeedsHumanReview": verdict == "UNKNOWN" or bool(missing),
            "ObservationOnly": OBSERVATION_ONLY,
            "V1ExecutionChanged": V1_EXECUTION_CHANGED,
            "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
            "SourceType": "SEC_XBRL", "SourceAuthority": "PRIMARY",
            "SourceURL": filing["FilingURL"], "ConsensusURL": "",
            "MarketContextURL": "", "Currency": "USD",
        })

        print("\nUS RESULT", filing["Ticker"], "|", filing["PublishedAtUTC"], "|", filing["Form"])
        print("RevenueActual :", revenue["Actual"] if revenue["Actual"] is not None else "N/A")
        print("KeyKPIName :", kpi["Name"] or "N/A")
        print("KeyKPIActual :", kpi["Actual"] if kpi["Actual"] is not None else "N/A")
        print("KeyKPIPrevious :", kpi["Previous"] if kpi["Previous"] is not None else "N/A")
        print("KeyKPIChange :", kpi["Change"] if kpi["Change"] is not None else "N/A")
        print("KeyKPILevelStatus :", kpi["LevelStatus"])
        print("KeyKPITrend :", kpi["Trend"])
        print("FundamentalAssessment :", fundamental)
        print("FundamentalReason :", fundamental_reason)
        print("ExpectationAssessment :", expectation)
        print("ExpectationReason :", expectation_reason)
        print("OverallVerdict :", verdict)
        print("OverallVerdictReason :", verdict_reason)
        print("Confidence :", confidence)

    europe = europe_rows(watchlist, observed_at)
    output.extend(europe)
    print("\nPUBLICATIONS EUROPEENNES IR")
    for row in europe:
        print(
            row["Ticker"], row["PublishedAtUTC"],
            "Revenue", row["RevenueActual"],
            "Consensus", row["RevenueConsensus"],
            "KPIHistory", row["KeyKPIHistory"],
            "KPITrendBasis", row["KeyKPITrendBasis"],
            "KPITrend", row["KeyKPITrend"],
            "MarketReaction", row["MarketReactionAssessment"],
            "RiskFlags", row["RiskFlags"],
            "Verdict", row["OverallVerdict"],
        )
    for pending in EUROPE_PENDING:
        if any(alias in watchlist for alias in pending["aliases"]):
            print(pending["Ticker"], "PENDING", pending["ScheduledDate"])

    merged = merge_rows(read_csv(EARNINGS_RESULTS_FILE), output)
    write_csv(EARNINGS_RESULTS_FILE, merged)
    print("\n" + "=" * 115)
    print("RESUME EARNINGS INTELLIGENCE")
    print("=" * 115)
    print("US SEC/XBRL :", len(filings))
    print("Europe IR :", len(europe))
    print("Resultats ecrits :", len(output))
    print("Total conserve :", len(merged))
    print("EPSSecondaryStatus :", EPS_SECONDARY_STATUS)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("Fichier :", EARNINGS_RESULTS_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
