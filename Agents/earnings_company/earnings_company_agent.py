#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Lit en lecture seule les événements consolidés et sémantiques de Catalyst,
puis identifie les événements réellement pertinents pour l'analyse des
résultats d'entreprise.

Cette version ne collecte aucune donnée supplémentaire et n'écrit aucun
résultat. Elle affiche uniquement les candidats retenus.

Aucun impact sur V1 ou sur le portefeuille V1.
"""
from __future__ import annotations

import csv
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

# Types sémantiques directement liés aux publications financières.
EARNINGS_CATALYST_TYPES = {
    "EARNINGS",
    "GUIDANCE",
}

# Catégories sémantiques acceptées lorsqu'elles représentent réellement
# une publication financière. CatalystType reste le filtre principal.
EARNINGS_CATEGORIES = {
    "EARNINGS",
    "GUIDANCE",
    "FINANCIAL_RESULTS",
    "FINANCIAL_UPDATE",
}


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize(value: Any) -> str:
    return clean(value).upper().replace(" ", "_").replace("-", "_")


def as_bool(value: Any) -> bool:
    return normalize(value) in {"TRUE", "1", "YES", "OUI"}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_tickers() -> list[str]:
    """Charge l'univers surveillé depuis stocks.txt."""
    if not STOCKS_FILE.exists():
        raise FileNotFoundError(
            f"Fichier stocks introuvable : {STOCKS_FILE}"
        )

    return [
        line.strip().upper()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_event_map() -> dict[str, dict[str, str]]:
    """Indexe catalyst_events.csv en lecture seule par EventID."""
    result: dict[str, dict[str, str]] = {}
    for row in read_csv(CATALYST_EVENTS_FILE):
        event_id = clean(row.get("EventID"))
        if event_id:
            result[event_id] = row
    return result


def is_earnings_candidate(semantic: dict[str, str]) -> bool:
    """Retient uniquement les événements financiers pertinents.

    Règles V1 du filtre :
    - l'événement doit être marqué IsRelevant=True par Catalyst Semantic ;
    - le type ou la catégorie doit être directement lié aux résultats ou à
      la guidance ;
    - les articles de bruit déjà rejetés par Catalyst restent exclus.
    """
    if not as_bool(semantic.get("IsRelevant")):
        return False

    catalyst_type = normalize(semantic.get("CatalystType"))
    category = normalize(semantic.get("Category"))

    return (
        catalyst_type in EARNINGS_CATALYST_TYPES
        or category in EARNINGS_CATEGORIES
    )


def build_candidates(
    tickers: set[str],
    event_map: dict[str, dict[str, str]],
    semantic_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []

    for semantic in semantic_rows:
        event_id = clean(semantic.get("EventID"))
        ticker = clean(semantic.get("Ticker")).upper()

        if not event_id or ticker not in tickers:
            continue
        if not is_earnings_candidate(semantic):
            continue

        event = event_map.get(event_id, {})

        candidates.append(
            {
                "EventID": event_id,
                "Ticker": ticker,
                "PublishedAtUTC": clean(
                    semantic.get("FirstSeenUTC")
                    or event.get("FirstSeenUTC")
                ),
                "CatalystType": clean(semantic.get("CatalystType")),
                "Category": clean(semantic.get("Category")),
                "Direction": clean(semantic.get("Direction")),
                "Importance": clean(semantic.get("Importance")),
                "Confidence": clean(semantic.get("Confidence")),
                "Freshness": clean(semantic.get("Freshness")),
                "NeedsHumanReview": clean(
                    semantic.get("NeedsHumanReview")
                ),
                "Headline": clean(
                    semantic.get("RepresentativeTitle")
                    or event.get("RepresentativeTitle")
                ),
                "Reason": clean(semantic.get("Reason")),
                "KeyFact": clean(semantic.get("KeyFact")),
            }
        )

    return sorted(
        candidates,
        key=lambda row: (
            row["PublishedAtUTC"],
            row["Ticker"],
            row["EventID"],
        ),
        reverse=True,
    )


def main() -> int:
    print("=" * 100)
    print("EARNINGS & COMPANY AGENT")
    print("LECTURE ET FILTRAGE DES EVENEMENTS EARNINGS")
    print("=" * 100)

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

    required_files = [
        CATALYST_EVENTS_FILE,
        CATALYST_SEMANTIC_FILE,
    ]
    missing_files = [path for path in required_files if not path.exists()]
    if missing_files:
        for path in missing_files:
            print("ERREUR : fichier source introuvable :", path)
        return 2

    tickers_list = load_tickers()
    tickers = set(tickers_list)
    event_map = load_event_map()
    semantic_rows = read_csv(CATALYST_SEMANTIC_FILE)

    candidates = build_candidates(
        tickers=tickers,
        event_map=event_map,
        semantic_rows=semantic_rows,
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
    print("Candidats Earnings retenus :", len(candidates))

    print()
    print("=" * 100)
    print("CANDIDATS EARNINGS")
    print("=" * 100)

    if not candidates:
        print("Aucun evenement Earnings pertinent detecte.")
    else:
        for index, candidate in enumerate(candidates, start=1):
            print()
            print(
                f"{index}. {candidate['Ticker']} | "
                f"{candidate['PublishedAtUTC']} | "
                f"{candidate['CatalystType']} | "
                f"{candidate['Direction']} | "
                f"Importance {candidate['Importance']} | "
                f"Confiance {candidate['Confidence']}"
            )
            print("EventID :", candidate["EventID"])
            print("Category :", candidate["Category"])
            print("Freshness :", candidate["Freshness"])
            print("NeedsHumanReview :", candidate["NeedsHumanReview"])
            print("Headline :", candidate["Headline"])
            print("Reason :", candidate["Reason"])
            print("KeyFact :", candidate["KeyFact"])

    print()
    print("Aucune ecriture dans earnings_company_events.csv")
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("=" * 100)
    print("FIN DU FILTRAGE EARNINGS")
    print("=" * 100)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
