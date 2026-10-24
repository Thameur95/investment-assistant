#!/usr/bin/env python3
"""Earnings & Company Agent V1.

Analyse en mode Shadow les résultats financiers et les informations
propres aux entreprises surveillées.

Aucun impact sur V1 ou sur le portefeuille V1.
"""

from __future__ import annotations

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


def load_tickers() -> list[str]:
    """Charge l'univers surveillé depuis stocks.txt."""

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
    print("=" * 80)
    print("EARNINGS & COMPANY AGENT")
    print("=" * 80)

    print("AgentVersion :", AGENT_VERSION)

    print()
    print("Sources lecture seule :")
    print("CatalystEvents :", CATALYST_EVENTS_FILE)
    print("CatalystSemantic :", CATALYST_SEMANTIC_FILE)
    print("CatalystSEC :", CATALYST_SEC_FILINGS_FILE)

    print()
    print("Sécurité :")
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)

    oad_tickers()

    print()
    print("Tickers chargés :", len(tickers))
    print("Univers :", ", ".join(tickers))

    print()
    print("=" * 80)
    print("EARNINGS & COMPANY AGENT - INITIALISATION OK")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
