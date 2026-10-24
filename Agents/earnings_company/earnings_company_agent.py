#!/usr/bin/env python3
"""
Earnings & Company Agent V1

Architecture Shadow uniquement :

US
    SEC / EDGAR
    Company Facts
    XBRL détaillé
    KPI dimensionnels

EUROPE
    Investor Relations primaire
    Normalisation vers le même schéma Earnings

Puis :
    Consensus
    Guidance
    FundamentalAssessment
    ExpectationAssessment
    OverallVerdict Shadow
    Persistence CSV

Aucun impact sur V1.
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


# ============================================================
# CONFIGURATION
# ============================================================

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "investment-assistant personal-research contact@example.com",
).strip()

FINNHUB_API_KEY = os.getenv(
    "FINNHUB_API_KEY",
    "",
).strip()

SEC_COMPANYFACTS_BASE = (
    "https://data.sec.gov/api/xbrl/companyfacts"
)

SEC_ARCHIVES_BASE = (
    "https://www.sec.gov/Archives/edgar/data"
)

FINNHUB_EARNINGS_URL = (
    "https://finnhub.io/api/v1/calendar/earnings"
)

REQUEST_TIMEOUT = 25
REQUEST_PAUSE = 0.15

FINNHUB_DATE_WINDOW_DAYS = 10
GUIDANCE_MATCH_DAYS = 3

REVENUE_TOLERANCE_PCT = 2.0
KPI_STABLE_THRESHOLD_PCT = 1.0

XBRL_DETAIL_LIMIT = 12


# ============================================================
# US / SEC
# ============================================================

CONDITIONAL_FORMS = {
    "8-K",
    "6-K",
}


EARNINGS_WORDS = re.compile(
    r"\b("
    r"earnings|results?|financial results?|"
    r"quarterly results?|annual results?|"
    r"income|revenue|sales|guidance|outlook|"
    r"chiffre d.?affaires|résultats?|resultats?"
    r")\b",
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


# ============================================================
# GUIDANCE
# ============================================================

GUIDANCE_PATTERNS = {

    "WITHDRAWN": re.compile(
        r"\b("
        r"withdraw(?:n|s)?|"
        r"suspend(?:ed|s)?|"
        r"retire|retiree|suspend"
        r")\b",
        re.IGNORECASE,
    ),

    "RAISED": re.compile(
        r"\b("
        r"raise[sd]?|increase[sd]?|"
        r"boost(?:ed|s)?|lift(?:ed|s)?|"
        r"releve|rehausse|revise en hausse"
        r")\b",
        re.IGNORECASE,
    ),

    "LOWERED": re.compile(
        r"\b("
        r"lower(?:ed|s)?|cut(?:s)?|"
        r"reduce[sd]?|downgrade[sd]?|"
        r"abaisse|reduit|revise en baisse"
        r")\b",
        re.IGNORECASE,
    ),

    "REAFFIRMED": re.compile(
        r"\b("
        r"reaffirm(?:ed|s)?|"
        r"reiterate[sd]?|"
        r"maintain(?:ed|s)?|"
        r"confirm(?:ed|s)?|"
        r"reaffirme|maintient|confirme"
        r")\b",
        re.IGNORECASE,
    ),

    "INTRODUCED": re.compile(
        r"\b("
        r"guidance|outlook|previsions|perspectives|"
        r"initiat(?:e|ed|es)|"
        r"introduc(?:e|ed|es)|"
        r"provid(?:e|ed|es)"
        r")\b",
        re.IGNORECASE,
    ),
}


# ============================================================
# KPI
# ============================================================

KPI_MAP: dict[str, dict[str, Any]] = {

    "NVDA": {
        "name": "Data Center Revenue",
    },

    "MSFT": {
        "name": "Intelligent Cloud Revenue",
    },

    "GOOGL": {
        "name": "Google Cloud Revenue",
    },

    "AMZN": {
        "name": "AWS Net Sales",
    },

    "META": {
        "name": "Family of Apps Revenue",
    },

    "TSLA": {
        "name": "Automotive Revenue",
    },

    "AAPL": {
        "name": "Services Revenue",
    },

    "SPCX": {
        "name": "Starlink Revenue",
    },

    "VLO": {
        "name": "Refining Revenue",
    },
}


DIMENSIONAL_KPI_RULES = {

    "TSLA": {
        "concepts": {
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        },
        "member": "tsla:AutomotiveRevenuesMember",
        "forbidden": set(),
    },

    "META": {
        "concepts": {
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        },
        "member": "meta:FamilyOfAppsMember",
        "forbidden": {
            "srt:ProductOrServiceAxis"
        },
    },

    "AMZN": {
        "concepts": {
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        },
        "member": "amzn:AmazonWebServicesSegmentMember",
        "forbidden": {
            "srt:ConsolidationItemsAxis"
        },
    },

    "GOOGL": {
        "concepts": {
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        },
        "member": "goog:GoogleCloudMember",
        "forbidden": {
            "srt:ConsolidationItemsAxis"
        },
    },

    "MSFT": {
        "concepts": {
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        },
        "member": "msft:IntelligentCloudMember",
        "forbidden": set(),
    },

    "VLO": {
        "concepts": {
            "RevenueFromContractWithCustomerIncludingAssessedTax"
        },
        "member": "vlo:RefiningMember",
        "forbidden": {
            "srt:ConsolidationItemsAxis"
        },
    },
}


# ============================================================
# EUROPE / INVESTOR RELATIONS
# ============================================================

EUROPE_IR_EVENTS = [

    # --------------------------------------------------------
    # SAFRAN
    # --------------------------------------------------------

    {
        "aliases": {
            "SAF.PA",
            "SAF",
        },

        "EventID":
            "IR|SAF.PA|2026-10-23|Q3-2026",

        "Ticker":
            "SAF.PA",

        "PublishedAtUTC":
            "2026-10-23",

        "EventType":
            "ACTUAL_QUARTERLY_REVENUE",

        "Period":
            "FY2026-Q3",

        "Currency":
            "EUR",

        "RevenueActual":
            9_003_000_000.0,

        "RevenueConsensus":
            None,

        "RevenuePrevious":
            None,

        "RevenueGrowthPct":
            14.7,

        "EPSActual":
            None,

        "EPSConsensus":
            None,

        "EPSPrevious":
            None,

        "GuidanceStatus":
            "REAFFIRMED",

        "KeyKPIName":
            "Civil engine aftermarket organic growth (%)",

        "KeyKPIActual":
            18.7,

        "KeyKPIConsensus":
            None,

        "KeyKPIPrevious":
            None,

        "KeyKPIMomentum":
            "UP",

        "SourceURL":
            "https://www.safran-group.com/download/"
            "press_release/file/2278933",
    },


    # --------------------------------------------------------
    # ASML
    # --------------------------------------------------------

    {
        "aliases": {
            "ASML.AS",
            "ASML",
        },

        "EventID":
            "IR|ASML.AS|2026-10-14|Q3-2026",

        "Ticker":
            "ASML.AS",

        "PublishedAtUTC":
            "2026-10-14",

        "EventType":
            "ACTUAL_QUARTERLY_RESULTS",

        "Period":
            "FY2026-Q3",

        "Currency":
            "EUR",

        "RevenueActual":
            11_662_000_000.0,

        "RevenueConsensus":
            None,

        "RevenuePrevious":
            None,

        "RevenueGrowthPct":
            None,

        "EPSActual":
            12.95,

        "EPSConsensus":
            None,

        "EPSPrevious":
            None,

        "GuidanceStatus":
            "INTRODUCED",

        "KeyKPIName":
            "Installed Base Management sales",

        "KeyKPIActual":
            3_289_000_000.0,

        "KeyKPIConsensus":
            None,

        "KeyKPIPrevious":
            None,

        "KeyKPIMomentum":
            "UNKNOWN",

        "SourceURL":
            "https://investor.asml.com/news-releases/"
            "news-release-details/"
            "asml-reports-eu11.7-billion-total-net-sales-"
            "and-eu5.0-billion-net-income-q3-2026",
    },
]


# Airbus n'est pas encore injecté comme résultat.
# La publication 9M 2026 est prévue le 28/10/2026.

EUROPE_IR_PENDING = [

    {
        "aliases": {
            "AIR.PA",
            "AIR",
        },

        "Ticker":
            "AIR.PA",

        "ScheduledDate":
            "2026-10-28",

        "SourceURL":
            "https://www.airbus.com/en/investors/"
            "financial-results",
    },
]


# ============================================================
# CSV
# ============================================================

RESULT_COLUMNS = [

    "EventID",
    "AgentVersion",
    "Ticker",

    "ObservedAtUTC",
    "PublishedAtUTC",

    "EventType",
    "Period",

    "RevenueActual",
    "RevenueConsensus",
    "RevenuePrevious",
    "RevenueSurprisePct",
    "RevenueGrowthPct",

    "EPSActual",
    "EPSConsensus",
    "EPSPrevious",
    "EPSSurprisePct",

    "GuidanceStatus",

    "KeyKPIName",
    "KeyKPIActual",
    "KeyKPIConsensus",
    "KeyKPIPrevious",
    "KeyKPIMomentum",

    "FundamentalAssessment",
    "ExpectationAssessment",

    "OverallVerdict",
    "Confidence",

    "DataStatus",
    "NeedsHumanReview",

    "ObservationOnly",
    "V1ExecutionChanged",
    "PortfolioV1Changed",

    "SourceType",
    "SourceAuthority",
    "SourceURL",
    "Currency",
]


# ============================================================
# UTILITAIRES
# ============================================================

def clean(value: Any) -> str:

    if value is None:
        return ""

    return " ".join(
        str(value).split()
    ).strip()


def normalize(value: Any) -> str:

    return clean(
        value
    ).upper().replace(
        "_",
        "-",
    )


def as_bool(value: Any) -> bool:

    return clean(
        value
    ).upper() in {
        "TRUE",
        "1",
        "YES",
        "OUI",
    }


def finite_float(
    value: Any,
) -> Optional[float]:

    try:
        number = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    if number != number:
        return None

    if abs(number) == float(
        "inf"
    ):
        return None

    return number


def read_csv(
    path: Path,
) -> list[dict[str, str]]:

    if (
        not path.exists()
        or path.stat().st_size == 0
    ):
        return []

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:

        return list(
            csv.DictReader(
                handle
            )
        )


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:

        writer = csv.DictWriter(
            handle,
            fieldnames=RESULT_COLUMNS,
            extrasaction="ignore",
        )

        writer.writeheader()

        writer.writerows(
            rows
        )


def load_tickers() -> list[str]:

    if not STOCKS_FILE.exists():

        raise FileNotFoundError(
            f"Fichier stocks introuvable : "
            f"{STOCKS_FILE}"
        )

    return [

        line.strip().upper()

        for line
        in STOCKS_FILE
        .read_text(
            encoding="utf-8"
        )
        .splitlines()

        if line.strip()
    ]


# ============================================================
# HTTP
# ============================================================

def get_bytes(
    url: str,
    headers: dict[str, str],
) -> bytes:

    request = Request(

        url,

        headers=headers,

        method="GET",
    )

    try:

        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:

            return response.read()

    except HTTPError as error:

        body = (
            error
            .read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        raise RuntimeError(
            f"HTTP {error.code}: "
            f"{body[:250]}"
        ) from error

    except URLError as error:

        raise RuntimeError(
            "Erreur reseau: "
            f"{error.reason}"
        ) from error


def get_json(
    url: str,
    headers: dict[str, str],
) -> Any:

    return json.loads(

        get_bytes(
            url,
            headers,
        ).decode(
            "utf-8"
        )
    )


def get_sec_json(
    url: str,
) -> Any:

    return get_json(

        url,

        {
            "User-Agent":
                SEC_USER_AGENT,

            "Accept":
                "application/json",
        },
    )


def get_sec_xml(
    url: str,
) -> bytes:

    return get_bytes(

        url,

        {
            "User-Agent":
                SEC_USER_AGENT,

            "Accept":
                "application/xml,"
                "text/xml,*/*",
        },
    )


def get_finnhub_json(
    url: str,
) -> Any:

    if not FINNHUB_API_KEY:

        raise RuntimeError(
            "FINNHUB_API_KEY absent"
        )

    return get_json(

        url,

        {
            "X-Finnhub-Token":
                FINNHUB_API_KEY,

            "Accept":
                "application/json",
        },
    )


# ============================================================
# CATALYST
# ============================================================

def load_event_map():

    return {

        clean(
            row.get(
                "EventID"
            )
        ):
        row

        for row
        in read_csv(
            CATALYST_EVENTS_FILE
        )

        if clean(
            row.get(
                "EventID"
            )
        )
    }


def semantic_candidates(
    watchlist,
    events,
):

    output = []

    for row in read_csv(
        CATALYST_SEMANTIC_FILE
    ):

        ticker = clean(
            row.get("Ticker")
        ).upper()

        event_id = clean(
            row.get("EventID")
        )

        if (
            ticker not in watchlist
            or not event_id
            or not as_bool(
                row.get(
                    "IsRelevant"
                )
            )
        ):
            continue

        catalyst_type = normalize(
            row.get(
                "CatalystType"
            )
        )

        category = normalize(
            row.get(
                "Category"
            )
        )

        if (
            catalyst_type
            not in {
                "EARNINGS",
                "GUIDANCE",
            }
            and category
            not in {
                "EARNINGS",
                "GUIDANCE",
                "FINANCIAL-RESULTS",
                "FINANCIAL-UPDATE",
            }
        ):
            continue

        event = events.get(
            event_id,
            {},
        )

        output.append(
            {
                "EventID":
                    event_id,

                "Ticker":
                    ticker,

                "PublishedAtUTC":
                    clean(
                        row.get(
                            "FirstSeenUTC"
                        )
                        or event.get(
                            "FirstSeenUTC"
                        )
                    ),

                "CatalystType":
                    catalyst_type,

                "Category":
                    category,

                "Headline":
                    clean(
                        row.get(
                            "RepresentativeTitle"
                        )
                        or event.get(
                            "RepresentativeTitle"
                        )
                    ),

                "KeyFact":
                    clean(
                        row.get(
                            "KeyFact"
                        )
                    ),

                "Reason":
                    clean(
                        row.get(
                            "Reason"
                        )
                    ),
            }
        )

    return output


# ============================================================
# SEC FILINGS
# ============================================================

def classify_sec(
    row,
):

    form = normalize(
        row.get(
            "Form"
        )
    )

    description = clean(
        row.get(
            "Description"
        )
    )

    if form == "10-Q":

        return (
            True,
            "ACTUAL_QUARTERLY_FILING",
        )

    if form in {
        "10-K",
        "20-F",
        "40-F",
    }:

        return (
            True,
            "ACTUAL_ANNUAL_FILING",
        )

    if (
        form in CONDITIONAL_FORMS
        and EARNINGS_WORDS.search(
            description
        )
    ):

        return (
            True,
            "ACTUAL_EARNINGS_RELEASE",
        )

    return (
        False,
        "SEC_REVIEW",
    )


def sec_candidates(
    watchlist,
):

    retained = []
    review = []

    for row in read_csv(
        CATALYST_SEC_FILINGS_FILE
    ):

        ticker = clean(
            row.get(
                "Ticker"
            )
        ).upper()

        accession = clean(
            row.get(
                "AccessionNumber"
            )
        )

        if (
            ticker not in watchlist
            or not accession
        ):
            continue

        keep, classification = (
            classify_sec(
                row
            )
        )

        item = {

            "EventID":
                f"SEC|{ticker}|"
                f"{accession}",

            "Ticker":
                ticker,

            "PublishedAtUTC":
                clean(
                    row.get(
                        "FiledAt"
                    )
                ),

            "Classification":
                classification,

            "Form":
                clean(
                    row.get(
                        "Form"
                    )
                ),

            "AccessionNumber":
                accession,

            "CIK":
                clean(
                    row.get(
                        "CIK"
                    )
                ).zfill(
                    10
                ),

            "Description":
                clean(
                    row.get(
                        "Description"
                    )
                ),

            "PrimaryDocument":
                clean(
                    row.get(
                        "PrimaryDocument"
                    )
                ),

            "FilingURL":
                clean(
                    row.get(
                        "FilingURL"
                    )
                ),
        }

        (
            retained
            if keep
            else review
        ).append(
            item
        )

    key = lambda item: (
        item["PublishedAtUTC"],
        item["Ticker"],
        item["EventID"],
    )

    return (
        sorted(
            retained,
            key=key,
            reverse=True,
        ),
        sorted(
            review,
            key=key,
            reverse=True,
        ),
    )


# ============================================================
# XBRL COMPANY FACTS
# ============================================================

def duration_days(
    row,
):

    try:

        return (
            date.fromisoformat(
                clean(
                    row.get(
                        "end"
                    )
                )
            )
            -
            date.fromisoformat(
                clean(
                    row.get(
                        "start"
                    )
                )
            )
        ).days

    except ValueError:

        return None


def fiscal_year(
    row,
):

    try:

        return int(
            clean(
                row.get(
                    "fy"
                )
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


def valid_duration(
    row,
    form,
):

    duration = duration_days(
        row
    )

    form = normalize(
        form
    )

    if form == "10-Q":

        return (
            duration is not None
            and 70
            <= duration
            <= 110
        )

    if form in {
        "10-K",
        "20-F",
        "40-F",
    }:

        return (
            duration is not None
            and 330
            <= duration
            <= 400
        )

    return True


def select_current(
    entries,
    accession,
    form,
):

    form = normalize(
        form
    )

    candidates = [

        row

        for row
        in entries

        if (
            clean(
                row.get(
                    "accn"
                )
            )
            == accession

            and (
                not clean(
                    row.get(
                        "form"
                    )
                )
                or normalize(
                    row.get(
                        "form"
                    )
                )
                == form
            )

            and valid_duration(
                row,
                form,
            )

            and finite_float(
                row.get(
                    "val"
                )
            )
            is not None
        )
    ]

    if not candidates:

        return None

    target = (
        91
        if form == "10-Q"
        else 365
    )

    wanted_periods = (
        {
            "Q1",
            "Q2",
            "Q3",
        }
        if form == "10-Q"
        else {
            "FY"
        }
    )

    return max(

        candidates,

        key=lambda row: (

            1
            if clean(
                row.get(
                    "fp"
                )
            ).upper()
            in wanted_periods
            else 0,

            -abs(
                (
                    duration_days(
                        row
                    )
                    or target
                )
                - target
            ),

            clean(
                row.get(
                    "end"
                )
            ),
        ),
    )


def select_previous(
    entries,
    current,
    accession,
    form,
):

    try:

        current_end = (
            date.fromisoformat(
                clean(
                    current.get(
                        "end"
                    )
                )
            )
        )

    except ValueError:

        return None

    current_fy = fiscal_year(
        current
    )

    current_fp = clean(
        current.get(
            "fp"
        )
    ).upper()

    current_duration = duration_days(
        current
    )

    candidates = []

    for row in entries:

        if (
            clean(
                row.get(
                    "accn"
                )
            )
            == accession
        ):
            continue

        if (
            finite_float(
                row.get(
                    "val"
                )
            )
            is None
        ):
            continue

        if not valid_duration(
            row,
            form,
        ):
            continue

        if (
            clean(
                row.get(
                    "fp"
                )
            ).upper()
            != current_fp
        ):
            continue

        if (
            current_fy is not None
            and fiscal_year(
                row
            )
            != current_fy - 1
        ):
            continue

        row_duration = duration_days(
            row
        )

        if (
            current_duration
            is not None
            and row_duration
            is not None
            and abs(
                row_duration
                - current_duration
            )
            > 10
        ):
            continue

        try:

            previous_end = (
                date.fromisoformat(
                    clean(
                        row.get(
                            "end"
                        )
                    )
                )
            )

        except ValueError:

            continue

        delta = (
            current_end
            - previous_end
        ).days

        if 330 <= delta <= 400:

            candidates.append(
                row
            )

    if not candidates:

        return None

    return max(

        candidates,

        key=lambda row:
            clean(
                row.get(
                    "filed"
                )
            ),
    )


def empty_metric():

    return {

        "Concept":
            "",

        "Unit":
            "",

        "Actual":
            None,

        "Previous":
            None,

        "Start":
            "",

        "End":
            "",

        "FY":
            "",

        "FP":
            "",

        "DurationDays":
            None,

        "Accession":
            "",

        "Form":
            "",
    }


def extract_named(
    company_facts,
    concepts,
    accession,
    form,
    units,
):

    us_gaap = (
        company_facts
        .get(
            "facts",
            {},
        )
        .get(
            "us-gaap",
            {},
        )
    )

    for concept in concepts:

        payload = us_gaap.get(
            concept,
            {},
        )

        fact_units = payload.get(
            "units",
            {},
        )

        for unit in units:

            entries = fact_units.get(
                unit,
                [],
            )

            current = select_current(
                entries,
                accession,
                form,
            )

            if not current:
                continue

            previous = select_previous(
                entries,
                current,
                accession,
                form,
            )

            return {

                "Concept":
                    concept,

                "Unit":
                    unit,

                "Actual":
                    finite_float(
                        current.get(
                            "val"
                        )
                    ),

                "Previous":
                    (
                        finite_float(
                            previous.get(
                                "val"
                            )
                        )
                        if previous
                        else None
                    ),

                "Start":
                    clean(
                        current.get(
                            "start"
                        )
                    ),

                "End":
                    clean(
                        current.get(
                            "end"
                        )
                    ),

                "FY":
                    clean(
                        current.get(
                            "fy"
                        )
                    ),

                "FP":
                    clean(
                        current.get(
                            "fp"
                        )
                    ),

                "DurationDays":
                    duration_days(
                        current
                    ),

                "Accession":
                    clean(
                        current.get(
                            "accn"
                        )
                    ),

                "Form":
                    clean(
                        current.get(
                            "form"
                        )
                    ),
            }

    return empty_metric()


def metric_period_valid(
    metric,
    form,
    filed_at,
):

    if metric["Actual"] is None:

        return True

    try:

        end_date = date.fromisoformat(
            metric["End"]
        )

        filing_date = date.fromisoformat(
            filed_at[:10]
        )

    except ValueError:

        return False

    age = (
        filing_date
        - end_date
    ).days

    duration = metric[
        "DurationDays"
    ]

    fp = metric[
        "FP"
    ].upper()

    form = normalize(
        form
    )

    if form == "10-Q":

        return (
            duration is not None
            and 70 <= duration <= 110
            and fp in {
                "Q1",
                "Q2",
                "Q3",
            }
            and 0 <= age <= 130
        )

    if form in {
        "10-K",
        "20-F",
        "40-F",
    }:

        return (
            duration is not None
            and 330 <= duration <= 400
            and fp == "FY"
            and 0 <= age <= 200
        )

    return True


def extract_financials(
    filing,
    company_facts,
):

    accession = filing[
        "AccessionNumber"
    ]

    form = filing[
        "Form"
    ]

    revenue = extract_named(

        company_facts,

        REVENUE_CONCEPTS,

        accession,

        form,

        (
            "USD",
        ),
    )

    eps = extract_named(

        company_facts,

        DILUTED_EPS_CONCEPTS,

        accession,

        form,

        (
            "USD/shares",
            "USD / shares",
        ),
    )

    if eps["Actual"] is None:

        eps = extract_named(

            company_facts,

            BASIC_EPS_CONCEPTS,

            accession,

            form,

            (
                "USD/shares",
                "USD / shares",
            ),
        )

    for metric in (
        revenue,
        eps,
    ):

        if not metric_period_valid(

            metric,

            form,

            filing[
                "PublishedAtUTC"
            ],
        ):

            metric["Actual"] = None

            metric["Previous"] = None

    has_data = (

        revenue["Actual"]
        is not None

        or eps["Actual"]
        is not None
    )

    source_valid = all(

        metric["Actual"] is None

        or (

            metric["Accession"]
            == accession

            and (

                not metric["Form"]

                or normalize(
                    metric["Form"]
                )
                == normalize(
                    form
                )
            )
        )

        for metric
        in (
            revenue,
            eps,
        )
    )

    if not has_data:

        data_status = (
            "NO_MATCHING_XBRL_FACT"
        )

    elif not source_valid:

        data_status = (
            "SOURCE_REVIEW_REQUIRED"
        )

    else:

        data_status = (
            "EXTRACTED_SOURCE_VALIDATED"
        )

    metric_for_period = (

        revenue
        if revenue["Actual"]
        is not None

        else eps
    )

    if (
        metric_for_period["FY"]
        and metric_for_period["FP"]
    ):

        period = (
            f"FY"
            f"{metric_for_period['FY']}"
            f"-"
            f"{metric_for_period['FP']}"
        )

    else:

        period = "UNKNOWN"

    if (
        revenue["Actual"]
        is None

        or revenue["Previous"]
        in (
            None,
            0,
        )
    ):

        revenue_growth = None

    else:

        revenue_growth = (

            revenue["Actual"]
            / revenue["Previous"]
            - 1.0

        ) * 100.0

    return {

        "Revenue":
            revenue,

        "EPS":
            eps,

        "Period":
            period,

        "RevenueGrowthPct":
            revenue_growth,

        "HasData":
            has_data,

        "DataStatus":
            data_status,
    }


# ============================================================
# XBRL INSTANCE
# ============================================================

def local_tag(
    tag: str,
) -> str:

    return tag.rsplit(
        "}",
        1,
    )[-1]


def archive_directory(
    filing,
):

    cik = str(
        int(
            filing["CIK"]
        )
    )

    accession = (
        filing[
            "AccessionNumber"
        ]
        .replace(
            "-",
            "",
        )
    )

    return (
        f"{SEC_ARCHIVES_BASE}/"
        f"{cik}/"
        f"{accession}"
    )


def discover_instance_document(
    filing,
):

    directory = archive_directory(
        filing
    )

    index = get_sec_json(
        f"{directory}/index.json"
    )

    items = (

        index
        .get(
            "directory",
            {},
        )
        .get(
            "item",
            [],
        )

        if isinstance(
            index,
            dict,
        )

        else []
    )

    excluded_exact = {

        "filingsummary.xml",
        "metafiles.xml",
        "metalinks.json",
    }

    excluded_suffixes = (

        "_cal.xml",
        "_def.xml",
        "_lab.xml",
        "_pre.xml",
        "_ref.xml",
        "_sch.xml",
    )

    candidates = []

    for item in items:

        name = clean(
            item.get(
                "name"
            )
        )

        lower = name.lower()

        if not lower.endswith(
            (
                ".xml",
                ".xbrl",
            )
        ):
            continue

        if lower in excluded_exact:
            continue

        if lower.endswith(
            excluded_suffixes
        ):
            continue

        if "filingsummary" in lower:
            continue

        candidates.append(
            name
        )

    for name in candidates:

        try:

            raw = get_sec_xml(
                f"{directory}/"
                f"{name}"
            )

            root = ET.fromstring(
                raw
            )

        except Exception:

            continue

        if (
            local_tag(
                root.tag
            ).lower()
            == "xbrl"
        ):

            return name

    return ""


def context_map(
    root,
):

    output = {}

    for element in root.iter():

        if local_tag(
            element.tag
        ) != "context":

            continue

        context_id = clean(
            element.attrib.get(
                "id"
            )
        )

        dimensions = {}

        start = ""
        end = ""
        instant = ""

        for child in element.iter():

            name = local_tag(
                child.tag
            )

            if name in {
                "explicitMember",
                "typedMember",
            }:

                dimensions[
                    clean(
                        child.attrib.get(
                            "dimension"
                        )
                    )
                ] = clean(
                    " ".join(
                        child.itertext()
                    )
                )

            elif name == "startDate":

                start = clean(
                    child.text
                )

            elif name == "endDate":

                end = clean(
                    child.text
                )

            elif name == "instant":

                instant = clean(
                    child.text
                )

        output[
            context_id
        ] = {

            "Dimensions":
                " | ".join(

                    f"{key}={value}"

                    for key, value
                    in sorted(
                        dimensions.items()
                    )
                ),

            "Start":
                start,

            "End":
                end
                or instant,
        }

    return output


def dimensional_fact_rows(
    filing,
):

    document = (
        discover_instance_document(
            filing
        )
    )

    if not document:

        return []

    root = ET.fromstring(

        get_sec_xml(

            f"{archive_directory(filing)}/"
            f"{document}"
        )
    )

    contexts = (
        context_map(
            root
        )
    )

    rows = []

    for fact in root.iter():

        context_ref = clean(
            fact.attrib.get(
                "contextRef"
            )
        )

        if not context_ref:
            continue

        value = finite_float(
            clean(
                fact.text
            )
        )

        if value is None:
            continue

        context = contexts.get(
            context_ref,
            {},
        )

        rows.append(
            {
                "Concept":
                    local_tag(
                        fact.tag
                    ),

                "Value":
                    value,

                "Unit":
                    clean(
                        fact.attrib.get(
                            "unitRef"
                        )
                    ),

                "Start":
                    clean(
                        context.get(
                            "Start"
                        )
                    ),

                "End":
                    clean(
                        context.get(
                            "End"
                        )
                    ),

                "Dimensions":
                    clean(
                        context.get(
                            "Dimensions"
                        )
                    ),
            }
        )

    return rows


def dimensional_kpi(
    filing,
):

    config = KPI_MAP.get(
        filing["Ticker"]
    )

    rule = (
        DIMENSIONAL_KPI_RULES
        .get(
            filing["Ticker"]
        )
    )

    if not config:

        return {

            "Name":
                "",

            "Concept":
                "",

            "Actual":
                None,

            "Previous":
                None,

            "Momentum":
                "UNKNOWN",

            "Status":
                "NO_MAPPING",
        }

    empty = {

        "Name":
            config["name"],

        "Concept":
            "",

        "Actual":
            None,

        "Previous":
            None,

        "Momentum":
            "UNKNOWN",

        "Status":
            "NOT_FOUND",
    }

    if not rule:

        return empty

    try:

        rows = dimensional_fact_rows(
            filing
        )

    except Exception:

        return empty

    current_candidates = []

    for row in rows:

        if (
            row["Concept"]
            not in rule["concepts"]
        ):
            continue

        if (
            rule["member"]
            not in row["Dimensions"]
        ):
            continue

        if any(

            forbidden
            in row["Dimensions"]

            for forbidden
            in rule["forbidden"]
        ):
            continue

        if (
            row["Unit"].lower()
            not in {
                "usd",
                "u_usd",
            }
        ):
            continue

        try:

            start_date = (
                date.fromisoformat(
                    row["Start"]
                )
            )

            end_date = (
                date.fromisoformat(
                    row["End"]
                )
            )

        except ValueError:

            continue

        duration = (
            end_date
            - start_date
        ).days

        form = normalize(
            filing["Form"]
        )

        if (
            form == "10-Q"
            and not (
                70
                <= duration
                <= 110
            )
        ):
            continue

        if (
            form
            in {
                "10-K",
                "20-F",
                "40-F",
            }
            and not (
                330
                <= duration
                <= 400
            )
        ):
            continue

        current_candidates.append(
            row
        )

    if not current_candidates:

        return empty

    current = sorted(

        current_candidates,

        key=lambda row: (
            row["End"],
            -row["Dimensions"].count(
                "|"
            ),
        ),

        reverse=True,

    )[0]

    previous_candidates = []

    current_end = (
        date.fromisoformat(
            current["End"]
        )
    )

    current_start = (
        date.fromisoformat(
            current["Start"]
        )
    )

    current_duration = (

        current_end
        - current_start

    ).days

    for row in rows:

        if (
            row["Concept"]
            != current["Concept"]
        ):
            continue

        if (
            row["Dimensions"]
            != current["Dimensions"]
        ):
            continue

        try:

            previous_start = (
                date.fromisoformat(
                    row["Start"]
                )
            )

            previous_end = (
                date.fromisoformat(
                    row["End"]
                )
            )

        except ValueError:

            continue

        previous_duration = (

            previous_end
            - previous_start

        ).days

        if (
            abs(
                previous_duration
                - current_duration
            )
            > 10
        ):
            continue

        delta = (

            current_end
            - previous_end

        ).days

        if 330 <= delta <= 400:

            previous_candidates.append(
                row
            )

    previous = (

        max(
            previous_candidates,
            key=lambda row:
                row["End"],
        )

        if previous_candidates

        else None
    )

    actual = current[
        "Value"
    ]

    previous_value = (

        previous["Value"]

        if previous

        else None
    )

    if (
        previous_value
        in (
            None,
            0,
        )
    ):

        momentum = "UNKNOWN"

    else:

        change = (

            actual
            / previous_value
            - 1.0

        ) * 100.0

        if (
            change
            > KPI_STABLE_THRESHOLD_PCT
        ):

            momentum = "UP"

        elif (
            change
            < -KPI_STABLE_THRESHOLD_PCT
        ):

            momentum = "DOWN"

        else:

            momentum = "STABLE"

    return {

        "Name":
            config["name"],

        "Concept":
            (
                f"{current['Concept']} | "
                f"{current['Dimensions']}"
            ),

        "Actual":
            actual,

        "Previous":
            previous_value,

        "Momentum":
            momentum,

        "Status":
            "EXTRACTED_DIMENSIONAL",
    }


# ============================================================
# GUIDANCE
# ============================================================

def guidance_for(
    filing,
    semantics,
):

    try:

        filing_date = (
            date.fromisoformat(
                filing[
                    "PublishedAtUTC"
                ][:10]
            )
        )

    except ValueError:

        return (
            "NOT_FOUND",
            "",
        )

    candidates = []

    for row in semantics:

        if (
            row["Ticker"]
            != filing["Ticker"]
        ):
            continue

        if (
            row["CatalystType"]
            != "GUIDANCE"

            and row["Category"]
            != "GUIDANCE"
        ):
            continue

        try:

            event_date = (
                date.fromisoformat(
                    row[
                        "PublishedAtUTC"
                    ][:10]
                )
            )

        except ValueError:

            continue

        distance = abs(
            (
                filing_date
                - event_date
            ).days
        )

        if (
            distance
            <= GUIDANCE_MATCH_DAYS
        ):

            candidates.append(
                (
                    distance,
                    row,
                )
            )

    if not candidates:

        return (
            "NOT_FOUND",
            "",
        )

    row = sorted(
        candidates,
        key=lambda item:
            item[0],
    )[0][1]

    text = " ".join(
        (
            row["Headline"],
            row["KeyFact"],
            row["Reason"],
        )
    )

    for status in (
        "WITHDRAWN",
        "RAISED",
        "LOWERED",
        "REAFFIRMED",
        "INTRODUCED",
    ):

        if GUIDANCE_PATTERNS[
            status
        ].search(
            text
        ):

            return (
                status,
                row["EventID"],
            )

    return (
        "REVIEW_REQUIRED",
        row["EventID"],
    )


# ============================================================
# FINNHUB
# ============================================================

def fetch_finnhub(
    ticker,
    filed_at,
):

    center = (
        date.fromisoformat(
            filed_at[:10]
        )
    )

    params = {

        "from":
            (
                center
                - timedelta(
                    days=10
                )
            ).isoformat(),

        "to":
            (
                center
                + timedelta(
                    days=10
                )
            ).isoformat(),

        "symbol":
            ticker,
    }

    payload = get_finnhub_json(

        f"{FINNHUB_EARNINGS_URL}?"
        f"{urlencode(params)}"
    )

    rows = (

        payload.get(
            "earningsCalendar",
            [],
        )

        if isinstance(
            payload,
            dict,
        )

        else []
    )

    return (
        rows
        if isinstance(
            rows,
            list,
        )
        else []
    )


def corroborate(
    filing,
    financials,
    rows,
):

    base = {

        "RevenueEstimate":
            None,

        "EPSEstimate":
            None,

        "RevenueSurprisePct":
            None,

        "Status":
            "NOT_APPLICABLE",
    }

    if not financials[
        "HasData"
    ]:

        return base

    filing_date = (
        date.fromisoformat(
            filing[
                "PublishedAtUTC"
            ][:10]
        )
    )

    candidates = []

    for row in rows:

        if (
            clean(
                row.get(
                    "symbol"
                )
            ).upper()
            != filing["Ticker"]
        ):
            continue

        try:

            event_date = (
                date.fromisoformat(
                    clean(
                        row.get(
                            "date"
                        )
                    )[:10]
                )
            )

        except ValueError:

            continue

        distance = abs(
            (
                filing_date
                - event_date
            ).days
        )

        if distance <= 10:

            candidates.append(
                (
                    distance,
                    row,
                )
            )

    if not candidates:

        return (
            base
            | {
                "Status":
                    "ECONOMIC_REVIEW_REQUIRED"
            }
        )

    match = sorted(
        candidates,
        key=lambda item:
            item[0],
    )[0][1]

    finnhub_revenue = (
        finite_float(
            match.get(
                "revenueActual"
            )
        )
    )

    revenue_estimate = (
        finite_float(
            match.get(
                "revenueEstimate"
            )
        )
    )

    eps_estimate = (
        finite_float(
            match.get(
                "epsEstimate"
            )
        )
    )

    sec_revenue = (
        financials[
            "Revenue"
        ][
            "Actual"
        ]
    )

    if (
        sec_revenue is None

        or revenue_estimate
        in (
            None,
            0,
        )
    ):

        surprise = None

    else:

        surprise = (

            sec_revenue
            / revenue_estimate
            - 1.0

        ) * 100.0

    if (
        sec_revenue is not None

        and finnhub_revenue
        is not None
    ):

        difference = (

            abs(
                sec_revenue
                - finnhub_revenue
            )
            /
            max(
                abs(
                    sec_revenue
                ),
                abs(
                    finnhub_revenue
                ),
                1e-12,
            )

        ) * 100.0

        status = (

            "ECONOMIC_PARTIAL"

            if difference
            <= REVENUE_TOLERANCE_PCT

            else
            "ECONOMIC_MISMATCH"
        )

    else:

        status = (
            "ECONOMIC_REVIEW_REQUIRED"
        )

    return {

        "RevenueEstimate":
            revenue_estimate,

        "EPSEstimate":
            eps_estimate,

        "RevenueSurprisePct":
            surprise,

        "Status":
            status,
    }


# ============================================================
# ASSESSMENTS
# ============================================================

def fundamental_assessment(
    financials,
    guidance,
    kpi,
):

    if (
        not financials[
            "HasData"
        ]

        and kpi[
            "Status"
        ]
        != "EXTRACTED_DIMENSIONAL"
    ):

        return "UNKNOWN"

    positive = 0
    negative = 0

    growth = financials.get(
        "RevenueGrowthPct"
    )

    if growth is not None:

        if growth > 1.0:
            positive += 1

        elif growth < -1.0:
            negative += 1

    momentum = kpi[
        "Momentum"
    ]

    if momentum == "UP":
        positive += 1

    elif momentum == "DOWN":
        negative += 1

    if guidance == "RAISED":
        positive += 1

    elif guidance in {
        "LOWERED",
        "WITHDRAWN",
    }:
        negative += 1

    if (
        positive
        and not negative
    ):

        return "POSITIVE"

    if (
        negative
        and not positive
    ):

        return "NEGATIVE"

    if (
        positive
        and negative
    ):

        return "MIXED"

    return "NEUTRAL"


def expectation_assessment(
    corroboration,
):

    surprise = corroboration.get(
        "RevenueSurprisePct"
    )

    if surprise is None:

        return "UNKNOWN"

    if surprise > 1.0:

        return "BEAT"

    if surprise < -1.0:

        return "MISS"

    return "IN_LINE"


def overall_verdict(
    financials,
    corroboration,
    guidance,
    fundamental,
    expectation,
):

    if (
        financials[
            "DataStatus"
        ]
        !=
        "EXTRACTED_SOURCE_VALIDATED"
    ):

        return (
            "UNKNOWN",
            "LOW",
        )

    if (
        corroboration[
            "Status"
        ]
        ==
        "ECONOMIC_MISMATCH"
    ):

        return (
            "UNKNOWN",
            "LOW",
        )

    if guidance == "REVIEW_REQUIRED":

        return (
            "UNKNOWN",
            "LOW",
        )

    if fundamental == "UNKNOWN":

        return (
            "UNKNOWN",
            "LOW",
        )

    if (
        fundamental == "POSITIVE"
        and expectation == "MISS"
    ):

        return (
            "MIXED",
            "MEDIUM",
        )

    if (
        fundamental == "NEGATIVE"
        and expectation == "BEAT"
    ):

        return (
            "MIXED",
            "MEDIUM",
        )

    if (
        fundamental == "POSITIVE"
        and expectation == "BEAT"
    ):

        return (
            "POSITIVE",
            "HIGH",
        )

    if (
        fundamental == "NEGATIVE"
        and expectation == "MISS"
    ):

        return (
            "NEGATIVE",
            "HIGH",
        )

    if expectation == "UNKNOWN":

        return (
            fundamental,
            "MEDIUM",
        )

    return (
        fundamental,
        "MEDIUM",
    )


# ============================================================
# EUROPE ASSESSMENTS
# ============================================================

def europe_ir_rows(
    watchlist,
    observed_at,
):

    output = []

    for event in EUROPE_IR_EVENTS:

        matched_aliases = sorted(

            alias

            for alias
            in event[
                "aliases"
            ]

            if alias
            in watchlist
        )

        if not matched_aliases:
            continue

        ticker = (
            matched_aliases[0]
        )

        growth = (
            event[
                "RevenueGrowthPct"
            ]
        )

        momentum = (
            event[
                "KeyKPIMomentum"
            ]
        )

        positive = 0
        negative = 0

        if growth is not None:

            if growth > 1.0:
                positive += 1

            elif growth < -1.0:
                negative += 1

        if momentum == "UP":
            positive += 1

        elif momentum == "DOWN":
            negative += 1

        guidance = (
            event[
                "GuidanceStatus"
            ]
        )

        if guidance == "RAISED":
            positive += 1

        elif guidance in {
            "LOWERED",
            "WITHDRAWN",
        }:
            negative += 1

        if positive and not negative:

            fundamental = "POSITIVE"

        elif negative and not positive:

            fundamental = "NEGATIVE"

        elif positive and negative:

            fundamental = "MIXED"

        else:

            fundamental = "NEUTRAL"

        consensus = (
            event[
                "RevenueConsensus"
            ]
        )

        if consensus in (
            None,
            0,
        ):

            surprise = None

        else:

            surprise = (

                event[
                    "RevenueActual"
                ]
                / consensus
                - 1.0

            ) * 100.0

        if surprise is None:

            expectation = "UNKNOWN"

        elif surprise > 1.0:

            expectation = "BEAT"

        elif surprise < -1.0:

            expectation = "MISS"

        else:

            expectation = "IN_LINE"

        if (
            fundamental == "POSITIVE"
            and expectation == "MISS"
        ):

            verdict = "MIXED"

        elif (
            fundamental == "NEGATIVE"
            and expectation == "BEAT"
        ):

            verdict = "MIXED"

        else:

            verdict = fundamental

        confidence = (

            "MEDIUM"
            if verdict != "UNKNOWN"

            else
            "LOW"
        )

        output.append(
            {
                "EventID":
                    event[
                        "EventID"
                    ],

                "AgentVersion":
                    AGENT_VERSION,

                "Ticker":
                    ticker,

                "ObservedAtUTC":
                    observed_at,

                "PublishedAtUTC":
                    event[
                        "PublishedAtUTC"
                    ],

                "EventType":
                    event[
                        "EventType"
                    ],

                "Period":
                    event[
                        "Period"
                    ],

                "RevenueActual":
                    event[
                        "RevenueActual"
                    ],

                "RevenueConsensus":
                    (
                        ""
                        if consensus is None
                        else consensus
                    ),

                "RevenuePrevious":
                    (
                        ""
                        if event[
                            "RevenuePrevious"
                        ]
                        is None
                        else event[
                            "RevenuePrevious"
                        ]
                    ),

                "RevenueSurprisePct":
                    (
                        ""
                        if surprise is None
                        else round(
                            surprise,
                            6,
                        )
                    ),

                "RevenueGrowthPct":
                    (
                        ""
                        if growth is None
                        else growth
                    ),

                "EPSActual":
                    (
                        ""
                        if event[
                            "EPSActual"
                        ]
                        is None
                        else event[
                            "EPSActual"
                        ]
                    ),

                "EPSConsensus":
                    (
                        ""
                        if event[
                            "EPSConsensus"
                        ]
                        is None
                        else event[
                            "EPSConsensus"
                        ]
                    ),

                "EPSPrevious":
                    (
                        ""
                        if event[
                            "EPSPrevious"
                        ]
                        is None
                        else event[
                            "EPSPrevious"
                        ]
                    ),

                "EPSSurprisePct":
                    "",

                "GuidanceStatus":
                    guidance,

                "KeyKPIName":
                    event[
                        "KeyKPIName"
                    ],

                "KeyKPIActual":
                    event[
                        "KeyKPIActual"
                    ],

                "KeyKPIConsensus":
                    "",

                "KeyKPIPrevious":
                    (
                        ""
                        if event[
                            "KeyKPIPrevious"
                        ]
                        is None
                        else event[
                            "KeyKPIPrevious"
                        ]
                    ),

                "KeyKPIMomentum":
                    momentum,

                "FundamentalAssessment":
                    fundamental,

                "ExpectationAssessment":
                    expectation,

                "OverallVerdict":
                    verdict,

                "Confidence":
                    confidence,

                "DataStatus":
                    "EUROPE_IR_PRIMARY"
                    "|CURATED_NORMALIZED"
                    "|SHADOW",

                "NeedsHumanReview":
                    expectation
                    == "UNKNOWN",

                "ObservationOnly":
                    OBSERVATION_ONLY,

                "V1ExecutionChanged":
                    V1_EXECUTION_CHANGED,

                "PortfolioV1Changed":
                    PORTFOLIO_V1_CHANGED,

                "SourceType":
                    "EUROPE_IR",

                "SourceAuthority":
                    "PRIMARY",

                "SourceURL":
                    event[
                        "SourceURL"
                    ],

                "Currency":
                    event[
                        "Currency"
                    ],
            }
        )

    return output


# ============================================================
# CSV US ROW
# ============================================================

def us_result_row(
    filing,
    financials,
    corroboration,
    guidance,
    kpi,
    observed_at,
):

    fundamental = (
        fundamental_assessment(
            financials,
            guidance,
            kpi,
        )
    )

    expectation = (
        expectation_assessment(
            corroboration
        )
    )

    verdict, confidence = (
        overall_verdict(

            financials,

            corroboration,

            guidance,

            fundamental,

            expectation,
        )
    )

    revenue = financials[
        "Revenue"
    ]

    eps = financials[
        "EPS"
    ]

    needs_review = (

        financials[
            "DataStatus"
        ]
        !=
        "EXTRACTED_SOURCE_VALIDATED"

        or corroboration[
            "Status"
        ]
        in {
            "ECONOMIC_MISMATCH",
            "ECONOMIC_REVIEW_REQUIRED",
        }

        or guidance
        == "REVIEW_REQUIRED"

        or kpi[
            "Status"
        ]
        == "NOT_FOUND"

        or verdict
        == "UNKNOWN"
    )

    return {

        "EventID":
            filing[
                "EventID"
            ],

        "AgentVersion":
            AGENT_VERSION,

        "Ticker":
            filing[
                "Ticker"
            ],

        "ObservedAtUTC":
            observed_at,

        "PublishedAtUTC":
            filing[
                "PublishedAtUTC"
            ],

        "EventType":
            filing[
                "Classification"
            ],

        "Period":
            financials[
                "Period"
            ],

        "RevenueActual":
            (
                ""
                if revenue[
                    "Actual"
                ]
                is None
                else revenue[
                    "Actual"
                ]
            ),

        "RevenueConsensus":
            (
                ""
                if corroboration[
                    "RevenueEstimate"
                ]
                is None
                else corroboration[
                    "RevenueEstimate"
                ]
            ),

        "RevenuePrevious":
            (
                ""
                if revenue[
                    "Previous"
                ]
                is None
                else revenue[
                    "Previous"
                ]
            ),

        "RevenueSurprisePct":
            (
                ""
                if corroboration[
                    "RevenueSurprisePct"
                ]
                is None
                else round(
                    corroboration[
                        "RevenueSurprisePct"
                    ],
                    6,
                )
            ),

        "RevenueGrowthPct":
            (
                ""
                if financials[
                    "RevenueGrowthPct"
                ]
                is None
                else round(
                    financials[
                        "RevenueGrowthPct"
                    ],
                    6,
                )
            ),

        "EPSActual":
            (
                ""
                if eps[
                    "Actual"
                ]
                is None
                else eps[
                    "Actual"
                ]
            ),

        "EPSConsensus":
            (
                ""
                if corroboration[
                    "EPSEstimate"
                ]
                is None
                else corroboration[
                    "EPSEstimate"
                ]
            ),

        "EPSPrevious":
            (
                ""
                if eps[
                    "Previous"
                ]
                is None
                else eps[
                    "Previous"
                ]
            ),

        "EPSSurprisePct":
            "",

        "GuidanceStatus":
            guidance,

        "KeyKPIName":
            kpi[
                "Name"
            ],

        "KeyKPIActual":
            (
                ""
                if kpi[
                    "Actual"
                ]
                is None
                else kpi[
                    "Actual"
                ]
            ),

        "KeyKPIConsensus":
            "",

        "KeyKPIPrevious":
            (
                ""
                if kpi[
                    "Previous"
                ]
                is None
                else kpi[
                    "Previous"
                ]
            ),

        "KeyKPIMomentum":
            kpi[
                "Momentum"
            ],

        "FundamentalAssessment":
            fundamental,

        "ExpectationAssessment":
            expectation,

        "OverallVerdict":
            verdict,

        "Confidence":
            confidence,

        "DataStatus":
            (
                f"{financials['DataStatus']}"
                f"|{corroboration['Status']}"
                f"|KPI_{kpi['Status']}"
            ),

        "NeedsHumanReview":
            needs_review,

        "ObservationOnly":
            OBSERVATION_ONLY,

        "V1ExecutionChanged":
            V1_EXECUTION_CHANGED,

        "PortfolioV1Changed":
            PORTFOLIO_V1_CHANGED,

        "SourceType":
            "SEC_XBRL",

        "SourceAuthority":
            "PRIMARY",

        "SourceURL":
            filing.get(
                "FilingURL",
                "",
            ),

        "Currency":
            "USD",
    }


# ============================================================
# MERGE
# ============================================================

def merge_rows(
    existing,
    current,
):

    merged = {

        clean(
            row.get(
                "EventID"
            )
        ):
        row

        for row
        in existing

        if clean(
            row.get(
                "EventID"
            )
        )
    }

    for row in current:

        merged[
            row["EventID"]
        ] = row

    return sorted(

        merged.values(),

        key=lambda row: (

            clean(
                row.get(
                    "PublishedAtUTC"
                )
            ),

            clean(
                row.get(
                    "Ticker"
                )
            ),
        ),

        reverse=True,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    print(
        "=" * 115
    )

    print(
        "EARNINGS & COMPANY AGENT"
    )

    print(
        "SEC/XBRL US + EUROPE IR "
        "+ OVERALL VERDICT SHADOW"
    )

    print(
        "=" * 115
    )

    print(
        "AgentVersion :",
        AGENT_VERSION,
    )

    print(
        "ObservationOnly :",
        OBSERVATION_ONLY,
    )

    print(
        "V1ExecutionChanged :",
        V1_EXECUTION_CHANGED,
    )

    print(
        "PortfolioV1Changed :",
        PORTFOLIO_V1_CHANGED,
    )

    print(
        "FinnhubConfigured :",
        bool(
            FINNHUB_API_KEY
        ),
    )

    if not FINNHUB_API_KEY:

        print(
            "ERREUR : "
            "FINNHUB_API_KEY absent"
        )

        return 2

    watchlist = set(
        load_tickers()
    )

    events = (
        load_event_map()
    )

    semantics = (
        semantic_candidates(
            watchlist,
            events,
        )
    )

    filings, review = (
        sec_candidates(
            watchlist
        )
    )

    print(
        "Tickers charges :",
        len(
            watchlist
        ),
    )

    print(
        "Candidats semantiques :",
        len(
            semantics
        ),
    )

    print(
        "Publications SEC retenues :",
        len(
            filings
        ),
    )

    print(
        "Filings SEC a revoir :",
        len(
            review
        ),
    )

    companyfacts_cache = {}
    finnhub_cache = {}

    output_rows = []

    observed_at = (
        datetime
        .now(
            timezone.utc
        )
        .replace(
            microsecond=0
        )
        .isoformat()
    )


    # ========================================================
    # US / SEC
    # ========================================================

    for index, filing in enumerate(
        filings,
        1,
    ):

        cik = filing[
            "CIK"
        ]

        if cik not in companyfacts_cache:

            try:

                companyfacts_cache[
                    cik
                ] = get_sec_json(

                    f"{SEC_COMPANYFACTS_BASE}/"
                    f"CIK{cik}.json"
                )

                time.sleep(
                    REQUEST_PAUSE
                )

            except Exception as error:

                print(

                    filing["Ticker"],
                    "| ERREUR SEC :",
                    error,
                )

                companyfacts_cache[
                    cik
                ] = {}

        company_facts = (
            companyfacts_cache[
                cik
            ]
        )

        financials = (
            extract_financials(

                filing,

                company_facts,
            )
        )

        kpi = (
            dimensional_kpi(
                filing
            )
        )

        guidance, guidance_event = (
            guidance_for(

                filing,

                semantics,
            )
        )

        cache_key = (

            filing["Ticker"],

            filing[
                "PublishedAtUTC"
            ][:10],
        )

        if (
            cache_key
            not in finnhub_cache
        ):

            try:

                finnhub_cache[
                    cache_key
                ] = fetch_finnhub(

                    filing[
                        "Ticker"
                    ],

                    filing[
                        "PublishedAtUTC"
                    ],
                )

                time.sleep(
                    REQUEST_PAUSE
                )

            except Exception as error:

                print(

                    filing[
                        "Ticker"
                    ],

                    "| ERREUR FINNHUB :",

                    error,
                )

                finnhub_cache[
                    cache_key
                ] = []

        corroboration = (
            corroborate(

                filing,

                financials,

                finnhub_cache[
                    cache_key
                ],
            )
        )

        output_rows.append(

            us_result_row(

                filing,

                financials,

                corroboration,

                guidance,

                kpi,

                observed_at,
            )
        )

        print()

        print(
            f"{index}. "
            f"{filing['Ticker']} | "
            f"{filing['PublishedAtUTC']} | "
            f"{filing['Form']}"
        )

        print(
            "Period :",
            financials[
                "Period"
            ],
        )

        print(
            "GuidanceStatus :",
            guidance,
        )

        print(
            "GuidanceEventID :",
            guidance_event
            or "N/A",
        )

        print(
            "KeyKPIName :",
            kpi["Name"]
            or "N/A",
        )

        print(
            "KeyKPIConcept :",
            kpi["Concept"]
            or "N/A",
        )

        print(
            "KeyKPIActual :",
            kpi["Actual"]
            if kpi["Actual"]
            is not None
            else "N/A",
        )

        print(
            "KeyKPIPrevious :",
            kpi["Previous"]
            if kpi["Previous"]
            is not None
            else "N/A",
        )

        print(
            "KeyKPIMomentum :",
            kpi["Momentum"],
        )

        print(
            "KPIStatus :",
            kpi["Status"],
        )

        print(
            "DataStatus :",
            financials[
                "DataStatus"
            ],
        )


    # ========================================================
    # EUROPE / IR
    # ========================================================

    europe_rows = (
        europe_ir_rows(

            watchlist,

            observed_at,
        )
    )

    output_rows.extend(
        europe_rows
    )

    print()
    print(
        "=" * 115
    )

    print(
        "PUBLICATIONS EUROPEENNES IR"
    )

    print(
        "=" * 115
    )

    for row in europe_rows:

        print()

        print(
            row["Ticker"],
            "|",
            row["PublishedAtUTC"],
            "|",
            row["EventType"],
        )

        print(
            "RevenueActual :",
            row[
                "RevenueActual"
            ],
        )

        print(
            "RevenueGrowthPct :",
            row[
                "RevenueGrowthPct"
            ],
        )

        print(
            "GuidanceStatus :",
            row[
                "GuidanceStatus"
            ],
        )

        print(
            "KeyKPIName :",
            row[
                "KeyKPIName"
            ],
        )

        print(
            "KeyKPIActual :",
            row[
                "KeyKPIActual"
            ],
        )

        print(
            "FundamentalAssessment :",
            row[
                "FundamentalAssessment"
            ],
        )

        print(
            "ExpectationAssessment :",
            row[
                "ExpectationAssessment"
            ],
        )

        print(
            "OverallVerdict :",
            row[
                "OverallVerdict"
            ],
        )

        print(
            "Confidence :",
            row[
                "Confidence"
            ],
        )

        print(
            "SourceType :",
            row[
                "SourceType"
            ],
        )


    # Airbus pending

    for pending in (
        EUROPE_IR_PENDING
    ):

        if not any(

            alias
            in watchlist

            for alias
            in pending[
                "aliases"
            ]
        ):
            continue

        print()

        print(
            pending[
                "Ticker"
            ],
            "| PENDING |",
            "scheduled=",
            pending[
                "ScheduledDate"
            ],
        )


    # ========================================================
    # MERGE / PERSISTENCE
    # ========================================================

    existing_rows = (
        read_csv(
            EARNINGS_RESULTS_FILE
        )
    )

    merged_rows = (
        merge_rows(

            existing_rows,

            output_rows,
        )
    )

    write_csv(

        EARNINGS_RESULTS_FILE,

        merged_rows,
    )


    # ========================================================
    # SUMMARY
    # ========================================================

    print()

    print(
        "=" * 115
    )

    print(
        "RESUME US + EUROPE"
    )

    print(
        "=" * 115
    )

    print(
        "Résultats US traités :",
        len(
            filings
        ),
    )

    print(
        "Résultats Europe IR ajoutés :",
        len(
            europe_rows
        ),
    )

    print(
        "Résultats écrits cette exécution :",
        len(
            output_rows
        ),
    )

    print(
        "Total lignes Earnings conservées :",
        len(
            merged_rows
        ),
    )

    print(
        "Fichier résultats :",
        EARNINGS_RESULTS_FILE,
    )

    print(
        "ObservationOnly :",
        OBSERVATION_ONLY,
    )

    print(
        "V1ExecutionChanged :",
        V1_EXECUTION_CHANGED,
    )

    print(
        "PortfolioV1Changed :",
        PORTFOLIO_V1_CHANGED,
    )

    print(
        "=" * 115
    )

    return 0


if __name__ == "__main__":

    raise SystemExit(
        main()
    )
