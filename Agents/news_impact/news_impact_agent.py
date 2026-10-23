#!/usr/bin/env python3
"""News Impact Agent.

Collecte et journalise les actualités associées aux actions surveillées.

Mode Shadow / observation uniquement.
Aucun impact sur V1 ou sur le portefeuille V1.
"""

from __future__ import annotations

from config import (
    AGENT_VERSION,
    EVENTS_FILE,
    OBSERVATION_ONLY,
    PORTFOLIO_V1_CHANGED,
    STOCKS_FILE,
    V1_EXECUTION_CHANGED,
)


def load_tickers() -> list[str]:
    """Charge l'univers des actions depuis le fichier stocks.txt."""

    if not STOCKS_FILE.exists():
        raise FileNotFoundError(
            f"Fichier stocks introuvable : {STOCKS_FILE}"
        )

    return [
        line.strip()
        for line in STOCKS_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def main() -> int:
    print("=" * 70)
    print("NEWS IMPACT AGENT")
    print("=" * 70)

    print("AgentVersion :", AGENT_VERSION)
    print("StocksFile :", STOCKS_FILE)
    print("EventsFile :", EVENTS_FILE)

    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)

    tickers = load_tickers()

    print()
    print("Tickers charges :", len(tickers))
    print("Univers :", ", ".join(tickers))

    print()
    print("=" * 70)
    print("NEWS IMPACT AGENT - INITIALISATION OK")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
