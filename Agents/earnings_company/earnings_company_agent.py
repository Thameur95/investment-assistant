#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Lit en lecture seule Catalyst Events, Catalyst Semantic et les filings SEC,
puis identifie uniquement les publications financières effectivement publiées.

Cette version n'extrait pas encore Revenue/EPS/KPI et n'écrit aucun résultat.
Aucun impact sur V1 ou sur le portefeuille V1.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import Any

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

# Catégories sémantiques compatibles avec une publication financière réelle.
ALLOWED_CATALYST_TYPES = {"EARNINGS", "GUIDANCE"}
ALLOWED_CATEGORIES = {"EARNINGS", "GUIDANCE", "FINANCIAL_RESULTS", "FINANCIAL_UPDATE"}

# Filings réglementaires qui constituent une preuve forte de publication.
EARNINGS_SEC_FORMS = {"10-Q", "10-K", "6-K", "20-F", "40-F", "8-K"}

# Signaux textuels positifs : le contenu décrit des chiffres effectivement publiés.
ACTUAL_PUBLICATION_PATTERNS = (
    r"\breports?\b",
    r"\breported\b",
    r"\bposts?\b",
    r"\bposted\b",
    r"\bannounces?\b",
    r"\bannounced\b",
    r"\bpublishes?\b",
    r"\bpublished\b",
    r"\breleases?\b",
    r"\breleased\b",
    r"\bresults?\b",
    r"\bearnings?\b",
    r"\brevenue\b",
    r"\bsales\b",
    r"\bnet income\b",
    r"\boperating income\b",
    r"\bfree cash flow\b",
    r"\bguidance\b",
    r"\boutlook\b",
    r"\bquarterly\b",
    r"\bfull[ -]year\b",
    r"\bchiffre d.?affaires\b",
    r"\brésultats?\b",
    r"\bresultats?\b",
    r"\bprévisions?\b",
    r"\bprevisions?\b",
    r"\bperspectives?\b",
)

# Signaux d'exclusion : preview, opinion, analyste, rétrospective ou bruit.
EXCLUSION_PATTERNS = (
    r"\bpreview\b",
    r"\bahead of earnings\b",
    r"\bbefore earnings\b",
    r"\bheading into earnings\b",
    r"\bwhat to expect\b",
    r"\bwhat you need to know\b",
    r"\bwhat.?s in the cards\b",
    r"\bexpected to\b",
    r"\bestimates?\b",
    r"\bforecast\b",
    r"\btarget price\b",
    r"\bprice target\b",
    r"\bcuts? target\b",
    r"\braises? target\b",
    r"\banalyst\b",
    r"\bopinion\b",
    r"\bretrospective\b",
    r"\brétrospective\b",
    r"\bhistory says\b",
    r"\bexpected move\b",
    r"\boptions pricing\b",
    r"\bcalendar\b",
    r"\blooms?\b",
    r"\baperçu\b",
    r"\bapercu\b",
    r"\bavant résultats\b",
    r"\bavant resultats\b",
    r"\bobjectif de cours\b",
)

# Catégories incompatibles, sauf preuve réglementaire forte.
EXCLUDED_CATEGORIES = {
    "ANALYST",
    "REGULATORY",
    "MACRO",
    "SECTOR",
    "OTHER",
    "LEGAL",
    "PRODUCT",
    "MANAGEMENT",
    "M&A",
    "PARTNERSHIP",
    "CAPITAL_RAISE",
}


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def normalize(value: Any) -> str:
    return clean(value).upper().replace(" ", "_").replace("-", "_")


def normalize_text(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", clean(value).lower())
        if not unicodedata.combining(char)
    )
    return " ".join(text.split())


def as_bool(value: Any) -> bool:
    return normalize(value) in {"TRUE", "1", "YES", "OUI"}


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


def load_event_map() -> dict[str, dict[str, str]]:
    return {
        clean(row.get("EventID")): row
        for row in read_csv(CATALYST_EVENTS_FILE)
        if clean(row.get("EventID"))
    }


def build_sec_index() -> dict[str, list[dict[str, str]]]:
    index: dict[str, list[dict[str, str]]] = {}
    for row in read_csv(CATALYST_SEC_FILINGS_FILE):
        ticker = clean(row.get("Ticker")).upper()
        form = normalize(row.get("Form"))
        if ticker and form in {normalize(x) for x in EARNINGS_SEC_FORMS}:
            index.setdefault(ticker, []).append(row)
    return index


def combined_text(
    semantic: dict[str, str],
    event: dict[str, str],
) -> str:
    fields = [
        semantic.get("RepresentativeTitle"),
        semantic.get("RepresentativeSummary"),
        semantic.get("Reason"),
        semantic.get("KeyFact"),
        event.get("RepresentativeTitle"),
        event.get("RepresentativeSummary"),
    ]
    return normalize_text(" ".join(clean(value) for value in fields if value))


def matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def has_matching_sec_filing(
    ticker: str,
    semantic: dict[str, str],
    sec_index: dict[str, list[dict[str, str]]],
) -> bool:
    """Cherche un filing du même ticker autour de la date de l'événement.

    La comparaison reste volontairement simple et déterministe : même ticker et
    date FiledAt comprise entre J-1 et J+1 autour de FirstSeenUTC.
    """
    event_date = clean(semantic.get("FirstSeenUTC"))[:10]
    if not event_date:
        return False

    try:
        from datetime import date, timedelta

        center = date.fromisoformat(event_date)
        accepted = {
            (center + timedelta(days=offset)).isoformat()
            for offset in (-1, 0, 1)
        }
    except ValueError:
        return False

    return any(
        clean(row.get("FiledAt"))[:10] in accepted
        for row in sec_index.get(ticker, [])
    )


def classify_candidate(
    semantic: dict[str, str],
    event: dict[str, str],
    sec_index: dict[str, list[dict[str, str]]],
) -> tuple[bool, str, str]:
    """Retourne (retenu, classe, motif)."""
    if not as_bool(semantic.get("IsRelevant")):
        return False, "EXCLUDED", "IsRelevant=False"

    ticker = clean(semantic.get("Ticker")).upper()
    catalyst_type = normalize(semantic.get("CatalystType"))
    category = normalize(semantic.get("Category"))
    text = combined_text(semantic, event)

    semantic_compatible = (
        catalyst_type in ALLOWED_CATALYST_TYPES
        or category in ALLOWED_CATEGORIES
    )
    sec_supported = has_matching_sec_filing(ticker, semantic, sec_index)

    if not semantic_compatible and not sec_supported:
        return False, "EXCLUDED", "Type/categorie non financier"

    if category in EXCLUDED_CATEGORIES and not sec_supported:
        return False, "EXCLUDED", f"Categorie incompatible: {category}"

    if matches_any(text, EXCLUSION_PATTERNS) and not sec_supported:
        return False, "EXCLUDED", "Preview/opinion/analyste/retrospective"

    actual_evidence = matches_any(text, ACTUAL_PUBLICATION_PATTERNS)
    if not actual_evidence and not sec_supported:
        return False, "REVIEW", "Preuve de publication effective insuffisante"

    if sec_supported:
        return True, "ACTUAL_EARNINGS_SEC_SUPPORTED", "Filing SEC correspondant"

    if catalyst_type == "GUIDANCE" or category == "GUIDANCE":
        return True, "ACTUAL_GUIDANCE", "Guidance effectivement publiee"

    return True, "ACTUAL_EARNINGS", "Publication financiere effective detectee"


def build_candidates(
    tickers: set[str],
    event_map: dict[str, dict[str, str]],
    semantic_rows: list[dict[str, str]],
    sec_index: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    retained: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []

    for semantic in semantic_rows:
        event_id = clean(semantic.get("EventID"))
        ticker = clean(semantic.get("Ticker")).upper()
        if not event_id or ticker not in tickers:
            continue

        event = event_map.get(event_id, {})
        keep, classification, filter_reason = classify_candidate(
            semantic,
            event,
            sec_index,
        )

        row = {
            "EventID": event_id,
            "Ticker": ticker,
            "PublishedAtUTC": clean(
                semantic.get("FirstSeenUTC") or event.get("FirstSeenUTC")
            ),
            "Classification": classification,
            "FilterReason": filter_reason,
            "CatalystType": clean(semantic.get("CatalystType")),
            "Category": clean(semantic.get("Category")),
            "Direction": clean(semantic.get("Direction")),
            "Importance": clean(semantic.get("Importance")),
            "Confidence": clean(semantic.get("Confidence")),
            "Freshness": clean(semantic.get("Freshness")),
            "NeedsHumanReview": clean(semantic.get("NeedsHumanReview")),
            "Headline": clean(
                semantic.get("RepresentativeTitle")
                or event.get("RepresentativeTitle")
            ),
            "Reason": clean(semantic.get("Reason")),
            "KeyFact": clean(semantic.get("KeyFact")),
        }

        (retained if keep else rejected).append(row)

    sort_key = lambda row: (
        row["PublishedAtUTC"],
        row["Ticker"],
        row["EventID"],
    )
    retained.sort(key=sort_key, reverse=True)
    rejected.sort(key=sort_key, reverse=True)
    return retained, rejected


def print_event(index: int, row: dict[str, str]) -> None:
    print()
    print(
        f"{index}. {row['Ticker']} | {row['PublishedAtUTC']} | "
        f"{row['Classification']} | {row['Direction']} | "
        f"Importance {row['Importance']} | Confiance {row['Confidence']}"
    )
    print("EventID :", row["EventID"])
    print("FilterReason :", row["FilterReason"])
    print("CatalystType :", row["CatalystType"])
    print("Category :", row["Category"])
    print("Freshness :", row["Freshness"])
    print("NeedsHumanReview :", row["NeedsHumanReview"])
    print("Headline :", row["Headline"])
    print("Reason :", row["Reason"])
    print("KeyFact :", row["KeyFact"])


def main() -> int:
    print("=" * 110)
    print("EARNINGS & COMPANY AGENT")
    print("FILTRE RENFORCE - PUBLICATIONS FINANCIERES EFFECTIVES")
    print("=" * 110)
    print("AgentVersion :", AGENT_VERSION)

    print()
    print("Sources lecture seule :")
    print("CatalystEvents :", CATALYST_EVENTS_FILE)
    print("CatalystSemantic :", CATALYST_SEMANTIC_FILE)
    print("CatalystSEC :", CATALYST_SEC_FILINGS_FILE)

    print()
    print("Securite :")
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)

    required_files = [CATALYST_EVENTS_FILE, CATALYST_SEMANTIC_FILE]
    missing = [path for path in required_files if not path.exists()]
    if missing:
        for path in missing:
            print("ERREUR : fichier source introuvable :", path)
        return 2

    tickers_list = load_tickers()
    tickers = set(tickers_list)
    event_map = load_event_map()
    semantic_rows = read_csv(CATALYST_SEMANTIC_FILE)
    sec_index = build_sec_index()

    retained, rejected = build_candidates(
        tickers=tickers,
        event_map=event_map,
        semantic_rows=semantic_rows,
        sec_index=sec_index,
    )

    relevant_count = sum(
        1 for row in semantic_rows if as_bool(row.get("IsRelevant"))
    )

    print()
    print("Tickers charges :", len(tickers_list))
    print("Univers :", ", ".join(tickers_list))
    print("Evenements Catalyst indexes :", len(event_map))
    print("Lignes semantiques lues :", len(semantic_rows))
    print("Lignes semantiques pertinentes :", relevant_count)
    print("Tickers avec filings SEC indexes :", len(sec_index))
    print("Publications Earnings retenues :", len(retained))
    print("Evenements examines mais exclus :", len(rejected))

    print()
    print("=" * 110)
    print("PUBLICATIONS FINANCIERES RETENUES")
    print("=" * 110)
    if not retained:
        print("Aucune publication financiere effective detectee.")
    else:
        for index, row in enumerate(retained, start=1):
            print_event(index, row)

    print()
    print("=" * 110)
    print("EXCLUSIONS DU FILTRE EARNINGS")
    print("=" * 110)
    if not rejected:
        print("Aucune exclusion.")
    else:
        for index, row in enumerate(rejected, start=1):
            print()
            print(
                f"{index}. {row['Ticker']} | {row['Classification']} | "
                f"{row['FilterReason']} | {row['Headline']}"
            )

    print()
    print("Aucune ecriture dans earnings_company_events.csv")
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("=" * 110)
    print("FIN DU FILTRAGE EARNINGS RENFORCE")
    print("=" * 110)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
