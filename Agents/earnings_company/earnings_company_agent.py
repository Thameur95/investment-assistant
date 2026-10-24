#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Lit en lecture seule Catalyst Events, Catalyst Semantic et les filings SEC.
Identifie les publications financières primaires, même sans événement
sémantique Catalyst correspondant.

Cette version n'extrait pas encore Revenue/EPS/KPI et n'écrit aucun résultat.
Aucun impact sur V1 ou sur le portefeuille V1.
"""
from __future__ import annotations

import csv
import re
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

PERIODIC_FORMS = {"10-Q", "10-K", "20-F", "40-F"}
CONDITIONAL_FORMS = {"8-K", "6-K"}
EARNINGS_WORDS = re.compile(
    r"\b(earnings|results?|financial results?|quarterly results?|annual results?|"
    r"income|revenue|sales|guidance|outlook|chiffre d.?affaires|résultats?|resultats?)\b",
    re.IGNORECASE,
)


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def normalize(value: Any) -> str:
    return clean(value).upper().replace("_", "-")


def as_bool(value: Any) -> bool:
    return clean(value).upper() in {"TRUE", "1", "YES", "OUI"}


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


def semantic_candidates(
    watchlist: set[str],
    event_map: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    """Conserve seulement les classifications sémantiques réellement pertinentes."""
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
            "EARNINGS", "GUIDANCE", "FINANCIAL-RESULTS", "FINANCIAL-UPDATE"
        }:
            continue

        event = event_map.get(event_id, {})
        rows.append({
            "SourceKind": "CATALYST_SEMANTIC",
            "EventID": event_id,
            "Ticker": ticker,
            "PublishedAtUTC": clean(
                semantic.get("FirstSeenUTC") or event.get("FirstSeenUTC")
            ),
            "Classification": catalyst_type or category,
            "Form": "",
            "AccessionNumber": "",
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
        })
    return rows


def classify_sec_filing(row: dict[str, str]) -> tuple[bool, str, str]:
    """Classe un filing SEC sans supposer que tout 8-K/6-K est un résultat."""
    form = normalize(row.get("Form"))
    description = clean(row.get("Description"))

    if form in PERIODIC_FORMS:
        period = "QUARTERLY" if form == "10-Q" else "ANNUAL"
        return True, f"ACTUAL_{period}_FILING", "Formulaire périodique SEC"

    if form in CONDITIONAL_FORMS:
        if EARNINGS_WORDS.search(description):
            return True, "ACTUAL_EARNINGS_RELEASE", "Description SEC explicite"
        return False, "SEC_REVIEW", "8-K/6-K sans preuve Earnings dans Description"

    return False, "SEC_EXCLUDED", f"Formulaire non géré : {form}"


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
        item["PublishedAtUTC"], item["Ticker"], item["EventID"]
    )
    retained.sort(key=sort_key, reverse=True)
    review.sort(key=sort_key, reverse=True)
    return retained, review


def deduplicate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        event_id = clean(row.get("EventID"))
        if event_id:
            unique[event_id] = row
    return sorted(
        unique.values(),
        key=lambda item: (
            item["PublishedAtUTC"], item["Ticker"], item["EventID"]
        ),
        reverse=True,
    )


def print_candidate(index: int, row: dict[str, str]) -> None:
    print()
    print(
        f"{index}. {row['Ticker']} | {row['PublishedAtUTC']} | "
        f"{row['Classification']} | {row['SourceKind']}"
    )
    print("EventID :", row["EventID"])
    print("Form :", row.get("Form", ""))
    print("AccessionNumber :", row.get("AccessionNumber", ""))
    print("Description :", row.get("Description", ""))
    print("Headline :", row.get("Headline", ""))
    print("NeedsHumanReview :", row.get("NeedsHumanReview", ""))
    print("URL :", row.get("URL", ""))


def main() -> int:
    print("=" * 110)
    print("EARNINGS & COMPANY AGENT")
    print("INGESTION PRIMAIRE SEC + CONTEXTE CATALYST")
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
    retained = deduplicate(sec_rows + semantic_rows)

    print()
    print("Tickers charges :", len(tickers_list))
    print("Evenements Catalyst indexes :", len(event_map))
    print("Candidats semantiques :", len(semantic_rows))
    print("Publications SEC retenues :", len(sec_rows))
    print("Filings SEC a revoir :", len(sec_review))
    print("Total candidats Earnings :", len(retained))

    print()
    print("=" * 110)
    print("PUBLICATIONS FINANCIERES RETENUES")
    print("=" * 110)
    if not retained:
        print("Aucune publication financiere retenue.")
    else:
        for index, row in enumerate(retained, start=1):
            print_candidate(index, row)

    print()
    print("=" * 110)
    print("FILINGS SEC A REVOIR")
    print("=" * 110)
    if not sec_review:
        print("Aucun filing SEC a revoir.")
    else:
        for index, row in enumerate(sec_review, start=1):
            print()
            print(
                f"{index}. {row['Ticker']} | {row['PublishedAtUTC']} | "
                f"{row['Form']} | {row['FilterReason']}"
            )
            print("EventID :", row["EventID"])
            print("Description :", row["Description"])

    print()
    print("Aucune ecriture dans earnings_company_events.csv")
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("=" * 110)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
