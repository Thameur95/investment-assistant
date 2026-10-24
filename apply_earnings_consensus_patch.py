#!/usr/bin/env python3
"""Applique le patch Consensus & Surprise à earnings_company_agent.py.

Usage depuis la racine du dépôt :
    python apply_earnings_consensus_patch.py
"""
from pathlib import Path

TARGET = Path("Agents/earnings_company/earnings_company_agent.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"Patch interrompu ({label}) : bloc attendu {count} fois au lieu de 1."
        )
    return text.replace(old, new, 1)


def main() -> int:
    if not TARGET.exists():
        raise FileNotFoundError(f"Fichier introuvable : {TARGET}")

    source = TARGET.read_text(encoding="utf-8")

    # 1. Fonction de calcul de surprise, avec protection contre consensus nul.
    marker = '''def normalized_difference_pct(primary: float, secondary: float) -> float:\n    denominator = max(abs(primary), abs(secondary), 1e-12)\n    return abs(primary - secondary) / denominator * 100.0\n\n\n'''
    addition = marker + '''def surprise_pct(\n    actual: Optional[float],\n    consensus: Optional[float],\n) -> Optional[float]:\n    if actual is None or consensus is None or consensus == 0:\n        return None\n    return (actual / consensus - 1.0) * 100.0\n\n\n'''
    source = replace_once(source, marker, addition, "fonction surprise_pct")

    # 2. Cas sans donnée SEC/XBRL : consensus et surprises non applicables.
    old = '''            "RevenueDiffPct": None,\n            "EPSDiffPct": None,\n        }\n\n    match = closest_finnhub_earning('''
    new = '''            "RevenueDiffPct": None,\n            "EPSDiffPct": None,\n            "RevenueEstimate": None,\n            "EPSEstimate": None,\n            "RevenueSurprisePct": None,\n            "EPSSurprisePct": None,\n            "EPSSurpriseStatus": "NOT_APPLICABLE",\n        }\n\n    match = closest_finnhub_earning('''
    source = replace_once(source, old, new, "retour NOT_APPLICABLE")

    # 3. Cas sans publication Finnhub correspondante.
    old = '''            "RevenueDiffPct": None,\n            "EPSDiffPct": None,\n        }\n\n    finnhub_revenue = finite_float(match.get("revenueActual"))'''
    new = '''            "RevenueDiffPct": None,\n            "EPSDiffPct": None,\n            "RevenueEstimate": None,\n            "EPSEstimate": None,\n            "RevenueSurprisePct": None,\n            "EPSSurprisePct": None,\n            "EPSSurpriseStatus": "SECONDARY_NOT_AVAILABLE",\n        }\n\n    finnhub_revenue = finite_float(match.get("revenueActual"))'''
    source = replace_once(source, old, new, "retour Finnhub absent")

    # 4. Lecture des consensus Finnhub.
    old = '''    finnhub_revenue = finite_float(match.get("revenueActual"))\n    finnhub_eps = finite_float(match.get("epsActual"))\n    revenue_check, revenue_reason, revenue_diff = compare_metric('''
    new = '''    finnhub_revenue = finite_float(match.get("revenueActual"))\n    finnhub_eps = finite_float(match.get("epsActual"))\n    revenue_estimate = finite_float(match.get("revenueEstimate"))\n    eps_estimate = finite_float(match.get("epsEstimate"))\n\n    revenue_surprise = surprise_pct(\n        financials["Revenue"]["Actual"],\n        revenue_estimate,\n    )\n\n    # La base de l'EPS Finnhub n'est pas explicitée. EPSConsensus est conservé\n    # à titre informatif, mais EPSSurprisePct reste vide pour éviter de comparer\n    # un EPS GAAP dilué SEC à un consensus potentiellement ajusté.\n    eps_surprise = None\n    if financials["EPS"]["Actual"] is None:\n        eps_surprise_status = "NOT_APPLICABLE"\n    elif eps_estimate is None:\n        eps_surprise_status = "SECONDARY_NOT_AVAILABLE"\n    else:\n        eps_surprise_status = "SECONDARY_BASIS_UNKNOWN"\n\n    revenue_check, revenue_reason, revenue_diff = compare_metric('''
    source = replace_once(source, old, new, "lecture consensus Finnhub")

    # 5. Ajout des consensus et surprises au retour Finnhub.
    old = '''        "RevenueDiffPct": revenue_diff,\n        "EPSDiffPct": eps_diff,\n    }\n\n\ndef load_event_map()'''
    new = '''        "RevenueDiffPct": revenue_diff,\n        "EPSDiffPct": eps_diff,\n        "RevenueEstimate": revenue_estimate,\n        "EPSEstimate": eps_estimate,\n        "RevenueSurprisePct": revenue_surprise,\n        "EPSSurprisePct": eps_surprise,\n        "EPSSurpriseStatus": eps_surprise_status,\n    }\n\n\ndef load_event_map()'''
    source = replace_once(source, old, new, "retour consensus Finnhub")

    # 6. Persistance dans earnings_company_events.csv.
    old = '''        "RevenueActual": "" if revenue["Actual"] is None else revenue["Actual"],\n        "RevenueConsensus": "",\n        "RevenuePrevious": "" if revenue["Previous"] is None else revenue["Previous"],\n        "RevenueSurprisePct": "",\n        "RevenueGrowthPct": "" if financials["RevenueGrowthPct"] is None else round(financials["RevenueGrowthPct"], 6),\n        "EPSActual": "" if eps["Actual"] is None else eps["Actual"],\n        "EPSConsensus": "",\n        "EPSPrevious": "" if eps["Previous"] is None else eps["Previous"],\n        "EPSSurprisePct": "",'''
    new = '''        "RevenueActual": "" if revenue["Actual"] is None else revenue["Actual"],\n        "RevenueConsensus": (\n            "" if corroboration["RevenueEstimate"] is None\n            else corroboration["RevenueEstimate"]\n        ),\n        "RevenuePrevious": "" if revenue["Previous"] is None else revenue["Previous"],\n        "RevenueSurprisePct": (\n            "" if corroboration["RevenueSurprisePct"] is None\n            else round(corroboration["RevenueSurprisePct"], 6)\n        ),\n        "RevenueGrowthPct": "" if financials["RevenueGrowthPct"] is None else round(financials["RevenueGrowthPct"], 6),\n        "EPSActual": "" if eps["Actual"] is None else eps["Actual"],\n        "EPSConsensus": (\n            "" if corroboration["EPSEstimate"] is None\n            else corroboration["EPSEstimate"]\n        ),\n        "EPSPrevious": "" if eps["Previous"] is None else eps["Previous"],\n        "EPSSurprisePct": (\n            "" if corroboration["EPSSurprisePct"] is None\n            else round(corroboration["EPSSurprisePct"], 6)\n        ),'''
    source = replace_once(source, old, new, "persistance consensus")

    # 7. Diagnostic détaillé dans le log GitHub Actions.
    old = '''        print("FinnhubRevenueActual :", display(corroboration["RevenueActual"], 4))\n        print("FinnhubEPSActual :", display(corroboration["EPSActual"], 4))\n        print("RevenueCorroboration :", corroboration["RevenueCheck"])'''
    new = '''        print("FinnhubRevenueActual :", display(corroboration["RevenueActual"], 4))\n        print("FinnhubRevenueEstimate :", display(corroboration["RevenueEstimate"], 4))\n        print("RevenueSurprisePct :", display(corroboration["RevenueSurprisePct"], 4))\n        print("FinnhubEPSActual :", display(corroboration["EPSActual"], 4))\n        print("FinnhubEPSEstimate :", display(corroboration["EPSEstimate"], 4))\n        print("EPSSurprisePct :", display(corroboration["EPSSurprisePct"], 4))\n        print("EPSSurpriseStatus :", corroboration["EPSSurpriseStatus"])\n        print("RevenueCorroboration :", corroboration["RevenueCheck"])'''
    source = replace_once(source, old, new, "diagnostic consensus")

    # 8. Marqueur de version fonctionnelle visible au démarrage.
    old = 'print("EXTRACTION SEC COMPANY FACTS - DIAGNOSTIC UNIQUEMENT")'
    new = 'print("EXTRACTION SEC/XBRL + CONSENSUS & SURPRISE FINNHUB")'
    source = replace_once(source, old, new, "titre du moteur")

    TARGET.write_text(source, encoding="utf-8")
    compile(source, str(TARGET), "exec")
    print(f"Patch Consensus & Surprise appliqué : {TARGET}")
    print("Syntaxe Python vérifiée : OK")
    print("EPSSurprisePct reste vide tant que la base Finnhub n'est pas explicitée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
