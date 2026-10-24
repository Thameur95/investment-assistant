#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Lecture seule de Catalyst et SEC EDGAR.

Fonctions de cette version :
- détecter les publications financières primaires ;
- interroger SEC Company Facts (XBRL JSON) ;
- extraire, quand disponibles, période, chiffre d'affaires et EPS ;
- comparer la valeur publiée à la période précédente comparable ;
- afficher un diagnostic sans écrire earnings_company_events.csv.

Mode Shadow uniquement. Aucun impact sur V1 ou le portefeuille V1.
"""
from __future__ import annotations

import csv
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
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

SEC_USER_AGENT = (
    __import__("os")
    .getenv(
        "SEC_USER_AGENT",
        "investment-assistant personal-research contact@example.com",
    )
    .strip()
)
SEC_COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
REQUEST_TIMEOUT = 25
REQUEST_PAUSE = 0.15

PERIODIC_FORMS = {"10-Q", "10-K", "20-F", "40-F"}
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
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept": "application/json",
        },
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


def load_event_map() -> dict[str, dict[str, str]]:
    return {
        clean(row.get("EventID")): row
        for row in read_csv(CATALYST_EVENTS_FILE)
        if clean(row.get("EventID"))
    }


def semantic_candidates(
    watchlist: set[str],
    event_map: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for semantic in read_csv(CATALYST_SEMANTIC_FILE):
        ticker = clean(semantic.get("Ticker")).upper()
        event_id = clean(semantic.get("EventID"))
        if ticker not in watchlist or not event_id:
            continue
        if not as_bool(semantic.get("IsRelevant")):
            continue

        catalyst_type = normalize(semantic.get("CatalystType"))
        category = normalize(semantic.get("Category"))
        if catalyst_type not in {"EARNINGS", "GUIDANCE"} and category not in {
            "EARNINGS",
            "GUIDANCE",
            "FINANCIAL-RESULTS",
            "FINANCIAL-UPDATE",
        }:
            continue

        event = event_map.get(event_id, {})
        rows.append(
            {
                "SourceKind": "CATALYST_SEMANTIC",
                "EventID": event_id,
                "Ticker": ticker,
                "PublishedAtUTC": clean(
                    semantic.get("FirstSeenUTC") or event.get("FirstSeenUTC")
                ),
                "Classification": catalyst_type or category,
                "Form": "",
                "AccessionNumber": "",
                "CIK": "",
                "Description": clean(semantic.get("KeyFact")),
                "Headline": clean(
                    semantic.get("RepresentativeTitle")
                    or event.get("RepresentativeTitle")
                ),
                "Direction": clean(semantic.get("Direction")),
                "Importance": clean(semantic.get("Importance")),
                "Confidence": clean(semantic.get("Confidence")),
                "NeedsHumanReview": clean(semantic.get("NeedsHumanReview")),
                "URL": clean(
                    semantic.get("RepresentativeURL")
                    or event.get("RepresentativeURL")
                ),
            }
        )
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


def sec_candidates(
    watchlist: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    retained: list[dict[str, str]] = []
    review: list[dict[str, str]] = []

    for filing in read_csv(CATALYST_SEC_FILINGS_FILE):
        ticker = clean(filing.get("Ticker")).upper()
        accession = clean(filing.get("AccessionNumber"))
        if ticker not in watchlist or not accession:
            continue

        keep, classification, reason = classify_sec_filing(filing)
        row = {
            "SourceKind": "SEC_EDGAR",
            "EventID": f"SEC|{ticker}|{accession}",
            "Ticker": ticker,
            "PublishedAtUTC": clean(filing.get("FiledAt")),
            "Classification": classification,
            "FilterReason": reason,
            "Form": clean(filing.get("Form")),
            "AccessionNumber": accession,
            "CIK": clean(filing.get("CIK")).zfill(10),
            "Description": clean(filing.get("Description")),
            "Headline": clean(filing.get("Description")) or clean(filing.get("Form")),
            "Direction": "",
            "Importance": "",
            "Confidence": "",
            "NeedsHumanReview": "False" if keep else "True",
            "URL": clean(filing.get("FilingURL")),
        }
        (retained if keep else review).append(row)

    sort_key = lambda item: (
        item["PublishedAtUTC"],
        item["Ticker"],
        item["EventID"],
    )
    retained.sort(key=sort_key, reverse=True)
    review.sort(key=sort_key, reverse=True)
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


def accession_facts(
    entries: list[dict[str, Any]],
    accession: str,
) -> list[dict[str, Any]]:
    matches = [row for row in entries if clean(row.get("accn")) == accession]
    return sorted(
        matches,
        key=lambda row: (
            clean(row.get("end")),
            clean(row.get("start")),
            clean(row.get("filed")),
        ),
        reverse=True,
    )


def duration_days(row: dict[str, Any]) -> Optional[int]:
    try:
        start = date.fromisoformat(clean(row.get("start")))
        end = date.fromisoformat(clean(row.get("end")))
        return (end - start).days
    except ValueError:
        return None


def fiscal_year_int(row: dict[str, Any]) -> Optional[int]:
    try:
        return int(clean(row.get("fy")))
    except (TypeError, ValueError):
        return None


def valid_duration_for_form(row: dict[str, Any], form: str) -> bool:
    """Valide strictement la durée du fait selon le formulaire.

    10-Q : uniquement une durée trimestrielle comprise entre 70 et 110 jours.
    10-K/20-F/40-F : durée annuelle comprise entre 330 et 400 jours.
    8-K/6-K : aucune contrainte stricte car les facts peuvent provenir d'un
    communiqué joint et ne sont pas toujours présents dans Company Facts.
    """
    duration = duration_days(row)
    normalized_form = normalize(form)

    if normalized_form == "10-Q":
        return duration is not None and 70 <= duration <= 110
    if normalized_form in {"10-K", "20-F", "40-F"}:
        return duration is not None and 330 <= duration <= 400
    return True


def select_current_fact(
    matches: list[dict[str, Any]],
    form: str,
) -> Optional[dict[str, Any]]:
    if not matches:
        return None

    normalized_form = normalize(form)
    same_form = [
        row
        for row in matches
        if not clean(row.get("form"))
        or normalize(row.get("form")) == normalized_form
    ]
    candidates = same_form or matches

    # Pour un 10-Q, on refuse explicitement les valeurs YTD de 6 ou 9 mois.
    # Pour un dépôt annuel, on refuse les durées non annuelles.
    candidates = [
        row for row in candidates if valid_duration_for_form(row, normalized_form)
    ]
    if not candidates:
        return None

    def score(row: dict[str, Any]) -> tuple[int, int, int, str]:
        duration = duration_days(row)
        fiscal_period = clean(row.get("fp")).upper()
        frame = clean(row.get("frame"))

        if normalized_form == "10-Q":
            fp_score = 3 if fiscal_period in {"Q1", "Q2", "Q3"} else 0
            duration_score = -abs((duration or 91) - 91)
        else:
            fp_score = 3 if fiscal_period == "FY" else 0
            duration_score = -abs((duration or 365) - 365)

        frame_score = 1 if frame else 0
        has_value = 1 if finite_float(row.get("val")) is not None else 0
        return has_value, fp_score, duration_score + frame_score, clean(row.get("end"))

    return max(candidates, key=score)


def comparable_previous_fact(
    entries: list[dict[str, Any]],
    current: dict[str, Any],
    accession: str,
    form: str,
) -> Optional[dict[str, Any]]:
    """Sélectionne la même période fiscale de l'exercice précédent.

    Priorités :
    1. même FP (Q1/Q2/Q3 ou FY) ;
    2. FY = FY courant - 1 ;
    3. durée comparable et valide pour le même formulaire ;
    4. date de fin située approximativement un an avant la période courante.
    """
    current_end_text = clean(current.get("end"))
    current_fp = clean(current.get("fp")).upper()
    current_fy = fiscal_year_int(current)
    current_duration = duration_days(current)

    try:
        current_end = date.fromisoformat(current_end_text)
    except ValueError:
        return None

    candidates: list[dict[str, Any]] = []
    for row in entries:
        if clean(row.get("accn")) == accession:
            continue
        if finite_float(row.get("val")) is None:
            continue
        if not valid_duration_for_form(row, form):
            continue

        row_fp = clean(row.get("fp")).upper()
        if current_fp and row_fp != current_fp:
            continue

        row_fy = fiscal_year_int(row)
        if current_fy is not None and row_fy != current_fy - 1:
            continue

        row_duration = duration_days(row)
        if current_duration is not None and row_duration is not None:
            if abs(row_duration - current_duration) > 10:
                continue

        try:
            row_end = date.fromisoformat(clean(row.get("end")))
        except ValueError:
            continue

        days_apart = (current_end - row_end).days
        if not 330 <= days_apart <= 400:
            continue

        candidates.append(row)

    if not candidates:
        return None

    def previous_score(row: dict[str, Any]) -> tuple[int, int, str]:
        row_duration = duration_days(row)
        duration_delta = abs((row_duration or 0) - (current_duration or 0))
        try:
            row_end = date.fromisoformat(clean(row.get("end")))
            year_delta = abs((current_end - row_end).days - 365)
        except ValueError:
            year_delta = 9999
        return -duration_delta, -year_delta, clean(row.get("filed"))

    return max(candidates, key=previous_score)

def extract_metric(
    companyfacts: dict[str, Any],
    concepts: tuple[str, ...],
    units: tuple[str, ...],
    accession: str,
    form: str,
) -> dict[str, Any]:
    concept, unit, entries = fact_units(companyfacts, concepts, units)
    matches = accession_facts(entries, accession)
    current = select_current_fact(matches, form)
    if current is None:
        return {
            "Concept": concept,
            "Unit": unit,
            "Actual": None,
            "Previous": None,
            "Start": "",
            "End": "",
            "FY": "",
            "FP": "",
            "DurationDays": None,
            "PreviousEnd": "",
            "PreviousFY": "",
            "PreviousFP": "",
            "Frame": "",
            "Accession": "",
            "Filed": "",
            "Form": "",
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


def percentage_change(actual: Optional[float], previous: Optional[float]) -> Optional[float]:
    if actual is None or previous is None or previous == 0:
        return None
    return (actual / previous - 1.0) * 100.0


def infer_period(metric: dict[str, Any], form: str) -> str:
    fy = clean(metric.get("FY"))
    fp = clean(metric.get("FP")).upper()
    if fy and fp:
        return f"FY{fy}-{fp}"
    if fy:
        return f"FY{fy}"
    if fp:
        return fp
    normalized_form = normalize(form)
    if normalized_form == "10-Q":
        return "QUARTERLY"
    if normalized_form in {"10-K", "20-F", "40-F"}:
        return "ANNUAL"
    return "UNKNOWN"


def metric_period_is_current(
    metric: dict[str, Any],
    form: str,
    filed_at: str,
) -> bool:
    """Vérifie que la période XBRL correspond réellement au filing examiné."""
    if metric.get("Actual") is None:
        return True

    try:
        end_date = date.fromisoformat(clean(metric.get("End")))
        filing_date = date.fromisoformat(clean(filed_at)[:10])
    except ValueError:
        return False

    age_days = (filing_date - end_date).days
    normalized_form = normalize(form)
    fp = clean(metric.get("FP")).upper()
    duration = metric.get("DurationDays")

    if age_days < -10:
        return False
    if normalized_form == "10-Q":
        return (
            duration is not None
            and 70 <= int(duration) <= 110
            and fp in {"Q1", "Q2", "Q3"}
            and 0 <= age_days <= 130
        )
    if normalized_form in {"10-K", "20-F", "40-F"}:
        return (
            duration is not None
            and 330 <= int(duration) <= 400
            and fp == "FY"
            and 0 <= age_days <= 200
        )
    return True


def previous_period_is_comparable(metric: dict[str, Any]) -> bool:
    """Contrôle final de la période précédente sélectionnée."""
    if metric.get("Previous") is None:
        return True
    try:
        current_end = date.fromisoformat(clean(metric.get("End")))
        previous_end = date.fromisoformat(clean(metric.get("PreviousEnd")))
        current_fy = int(clean(metric.get("FY")))
        previous_fy = int(clean(metric.get("PreviousFY")))
    except (TypeError, ValueError):
        return False

    return (
        330 <= (current_end - previous_end).days <= 400
        and previous_fy == current_fy - 1
        and clean(metric.get("PreviousFP")).upper()
        == clean(metric.get("FP")).upper()
    )


def invalidate_metric(metric: dict[str, Any]) -> None:
    metric["Actual"] = None
    metric["Previous"] = None
    metric["PreviousEnd"] = ""
    metric["PreviousFY"] = ""
    metric["PreviousFP"] = ""


def invalidate_previous(metric: dict[str, Any]) -> None:
    metric["Previous"] = None
    metric["PreviousEnd"] = ""
    metric["PreviousFY"] = ""
    metric["PreviousFP"] = ""


def metric_source_validation(
    metric: dict[str, Any],
    filing: dict[str, str],
) -> tuple[str, str]:
    """Valide la traçabilité du fact XBRL vers le filing primaire."""
    if metric.get("Actual") is None:
        return "NOT_AVAILABLE", "Aucune valeur XBRL retenue"

    expected_accession = clean(filing.get("AccessionNumber"))
    expected_form = normalize(filing.get("Form"))
    fact_accession = clean(metric.get("Accession"))
    fact_form = normalize(metric.get("Form"))

    if not fact_accession or fact_accession != expected_accession:
        return "SOURCE_REVIEW_REQUIRED", "Accession XBRL non identique au filing"
    if fact_form and fact_form != expected_form:
        return "SOURCE_REVIEW_REQUIRED", "Formulaire XBRL non identique au filing"
    if not clean(metric.get("End")):
        return "SOURCE_REVIEW_REQUIRED", "Date de fin XBRL absente"
    if metric.get("DurationDays") is None:
        return "SOURCE_REVIEW_REQUIRED", "Durée XBRL absente"

    # Le frame SEC n'est pas toujours fourni. Son absence n'invalide pas le fact,
    # mais reste visible dans le diagnostic pour audit.
    return "SOURCE_VALIDATED", "Accession, formulaire et période cohérents"


def extract_financials(
    filing: dict[str, str],
    companyfacts: dict[str, Any],
) -> dict[str, Any]:
    accession = filing["AccessionNumber"]
    form = filing["Form"]

    revenue = extract_metric(
        companyfacts,
        REVENUE_CONCEPTS,
        ("USD",),
        accession,
        form,
    )

    eps = extract_metric(
        companyfacts,
        DILUTED_EPS_CONCEPTS,
        ("USD/shares", "USD / shares"),
        accession,
        form,
    )
    if eps["Actual"] is None:
        eps = extract_metric(
            companyfacts,
            BASIC_EPS_CONCEPTS,
            ("USD/shares", "USD / shares"),
            accession,
            form,
        )

    # Validation finale indépendante de la sélection XBRL.
    invalid_period_fields: list[str] = []
    invalid_previous_fields: list[str] = []
    filed_at = clean(filing.get("PublishedAtUTC"))

    for field_name, metric in (("REVENUE", revenue), ("EPS", eps)):
        if not metric_period_is_current(metric, form, filed_at):
            invalid_period_fields.append(field_name)
            invalidate_metric(metric)
            continue
        if not previous_period_is_comparable(metric):
            invalid_previous_fields.append(field_name)
            invalidate_previous(metric)

    revenue_source_status, revenue_source_reason = metric_source_validation(
        revenue, filing
    )
    eps_source_status, eps_source_reason = metric_source_validation(eps, filing)

    return {
        "Period": infer_period(revenue if revenue["Actual"] is not None else eps, form),
        "RevenueConcept": revenue["Concept"],
        "RevenueUnit": revenue["Unit"],
        "RevenueActual": revenue["Actual"],
        "RevenuePrevious": revenue["Previous"],
        "RevenueGrowthPct": percentage_change(revenue["Actual"], revenue["Previous"]),
        "RevenueDurationDays": revenue["DurationDays"],
        "RevenuePreviousEnd": revenue["PreviousEnd"],
        "RevenuePreviousFY": revenue["PreviousFY"],
        "RevenuePreviousFP": revenue["PreviousFP"],
        "RevenueStart": revenue["Start"],
        "RevenueEnd": revenue["End"],
        "RevenueFrame": revenue["Frame"],
        "RevenueAccession": revenue["Accession"],
        "RevenueFiled": revenue["Filed"],
        "RevenueSourceStatus": revenue_source_status,
        "RevenueSourceReason": revenue_source_reason,
        "EPSConcept": eps["Concept"],
        "EPSUnit": eps["Unit"],
        "EPSActual": eps["Actual"],
        "EPSPrevious": eps["Previous"],
        "EPSDurationDays": eps["DurationDays"],
        "EPSPreviousEnd": eps["PreviousEnd"],
        "EPSPreviousFY": eps["PreviousFY"],
        "EPSPreviousFP": eps["PreviousFP"],
        "EPSStart": eps["Start"],
        "EPSEnd": eps["End"],
        "EPSFrame": eps["Frame"],
        "EPSAccession": eps["Accession"],
        "EPSFiled": eps["Filed"],
        "EPSSourceStatus": eps_source_status,
        "EPSSourceReason": eps_source_reason,
        "GuidanceStatus": "UNKNOWN",
        "InvalidPeriodFields": "|".join(invalid_period_fields),
        "InvalidPreviousFields": "|".join(invalid_previous_fields),
    }


def display_number(value: Optional[float], decimals: int = 4) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.{decimals}f}"


def main() -> int:
    print("=" * 115)
    print("EARNINGS & COMPANY AGENT")
    print("EXTRACTION SEC COMPANY FACTS - DIAGNOSTIC UNIQUEMENT")
    print("=" * 115)
    print("AgentVersion :", AGENT_VERSION)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)

    required = [
        CATALYST_EVENTS_FILE,
        CATALYST_SEMANTIC_FILE,
        CATALYST_SEC_FILINGS_FILE,
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        for path in missing:
            print("ERREUR : fichier source introuvable :", path)
        return 2

    tickers_list = load_tickers()
    watchlist = set(tickers_list)
    event_map = load_event_map()
    semantic_rows = semantic_candidates(watchlist, event_map)
    sec_rows, sec_review = sec_candidates(watchlist)

    print()
    print("Tickers charges :", len(tickers_list))
    print("Candidats semantiques :", len(semantic_rows))
    print("Publications SEC retenues :", len(sec_rows))
    print("Filings SEC a revoir :", len(sec_review))

    companyfacts_cache: dict[str, dict[str, Any]] = {}
    extraction_errors = 0
    extracted = 0
    no_financial_data = 0

    print()
    print("=" * 115)
    print("EXTRACTION DES CHIFFRES PUBLIES")
    print("=" * 115)

    for index, filing in enumerate(sec_rows, start=1):
        cik = filing["CIK"]
        ticker = filing["Ticker"]

        if cik not in companyfacts_cache:
            try:
                url = f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json"
                companyfacts_cache[cik] = get_json(url)
                time.sleep(REQUEST_PAUSE)
            except Exception as error:
                extraction_errors += 1
                companyfacts_cache[cik] = {}
                print()
                print(f"{index}. {ticker} | ERREUR COMPANY FACTS : {error}")
                continue

        financials = extract_financials(filing, companyfacts_cache[cik])
        has_data = (
            financials["RevenueActual"] is not None
            or financials["EPSActual"] is not None
        )
        invalid_period = bool(financials["InvalidPeriodFields"])
        invalid_previous = bool(financials["InvalidPreviousFields"])
        if has_data:
            extracted += 1
        else:
            no_financial_data += 1

        print()
        print(
            f"{index}. {ticker} | {filing['PublishedAtUTC']} | "
            f"{filing['Classification']} | {filing['Form']}"
        )
        print("EventID :", filing["EventID"])
        print("AccessionNumber :", filing["AccessionNumber"])
        print("Period :", financials["Period"])
        print(
            "Revenue :",
            display_number(financials["RevenueActual"], 2),
            financials["RevenueUnit"] or "",
        )
        print(
            "RevenuePrevious :",
            display_number(financials["RevenuePrevious"], 2),
        )
        print(
            "RevenueGrowthPct :",
            display_number(financials["RevenueGrowthPct"], 4),
        )
        print("RevenueConcept :", financials["RevenueConcept"] or "N/A")
        print("RevenueStart :", financials["RevenueStart"] or "N/A")
        print("RevenueEnd :", financials["RevenueEnd"] or "N/A")
        print("RevenueFrame :", financials["RevenueFrame"] or "N/A")
        print("RevenueAccession :", financials["RevenueAccession"] or "N/A")
        print("RevenueFiled :", financials["RevenueFiled"] or "N/A")
        print("RevenueSourceStatus :", financials["RevenueSourceStatus"])
        print("RevenueSourceReason :", financials["RevenueSourceReason"])
        print("RevenueDurationDays :", financials["RevenueDurationDays"] if financials["RevenueDurationDays"] is not None else "N/A")
        print("RevenuePreviousPeriod :", financials["RevenuePreviousFY"] or "N/A", financials["RevenuePreviousFP"] or "", financials["RevenuePreviousEnd"] or "")
        print("EPS :", display_number(financials["EPSActual"], 4))
        print("EPSPrevious :", display_number(financials["EPSPrevious"], 4))
        print("EPSConcept :", financials["EPSConcept"] or "N/A")
        print("EPSStart :", financials["EPSStart"] or "N/A")
        print("EPSEnd :", financials["EPSEnd"] or "N/A")
        print("EPSFrame :", financials["EPSFrame"] or "N/A")
        print("EPSAccession :", financials["EPSAccession"] or "N/A")
        print("EPSFiled :", financials["EPSFiled"] or "N/A")
        print("EPSSourceStatus :", financials["EPSSourceStatus"])
        print("EPSSourceReason :", financials["EPSSourceReason"])
        print("EPSDurationDays :", financials["EPSDurationDays"] if financials["EPSDurationDays"] is not None else "N/A")
        print("EPSPreviousPeriod :", financials["EPSPreviousFY"] or "N/A", financials["EPSPreviousFP"] or "", financials["EPSPreviousEnd"] or "")
        print("GuidanceStatus :", financials["GuidanceStatus"])
        print("InvalidPeriodFields :", financials["InvalidPeriodFields"] or "NONE")
        print("InvalidPreviousFields :", financials["InvalidPreviousFields"] or "NONE")
        if invalid_period and not has_data:
            data_status = "INVALID_XBRL_PERIOD"
        elif invalid_period and has_data:
            data_status = "EXTRACTED_PARTIAL_INVALID_PERIOD"
        elif invalid_previous and has_data:
            data_status = "EXTRACTED_WITHOUT_VALID_PREVIOUS"
        elif has_data and (
            financials["RevenueSourceStatus"] == "SOURCE_REVIEW_REQUIRED"
            or financials["EPSSourceStatus"] == "SOURCE_REVIEW_REQUIRED"
        ):
            data_status = "SOURCE_REVIEW_REQUIRED"
        elif has_data:
            data_status = "EXTRACTED_SOURCE_VALIDATED"
        else:
            data_status = "NO_MATCHING_XBRL_FACT"
        print("DataStatus :", data_status)

    print()
    print("=" * 115)
    print("RESUME EXTRACTION")
    print("=" * 115)
    print("Publications SEC examinees :", len(sec_rows))
    print("Publications avec donnees :", extracted)
    print("Publications sans correspondance XBRL :", no_financial_data)
    print("Erreurs SEC Company Facts :", extraction_errors)
    print("CIK Company Facts charges :", len(companyfacts_cache))
    print("GuidanceStatus : UNKNOWN dans cette version")
    print("Aucune ecriture dans earnings_company_events.csv")
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("=" * 115)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
