from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


# ============================================================
# CATALYST SEMANTIC V5
# Classe les evenements de catalyst_events.csv avec Gemini.
# Informatif uniquement. Aucun filtre et aucun ordre de trading.
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

EVENTS_FILE = REPORTS_DIR / "catalyst_events.csv"
OUTPUT_FILE = REPORTS_DIR / "catalyst_semantic.csv"
SUMMARY_FILE = REPORTS_DIR / "catalyst_semantic_summary.csv"
ERROR_FILE = REPORTS_DIR / "catalyst_semantic_errors.csv"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

REQUEST_TIMEOUT_SECONDS = 45
REQUEST_PAUSE_SECONDS = 0.8
MAX_RETRIES = 2
MAX_EVENTS_PER_RUN = int(os.getenv("CATALYST_MAX_EVENTS_PER_RUN", "60"))

CATEGORIES = [
    "EARNINGS",
    "GUIDANCE",
    "CONTRACT",
    "PRODUCT",
    "M&A",
    "PARTNERSHIP",
    "CAPITAL_RAISE",
    "DILUTION",
    "REGULATORY",
    "LEGAL",
    "MANAGEMENT",
    "ANALYST",
    "INSIDER",
    "DIVIDEND",
    "BUYBACK",
    "IPO",
    "MACRO",
    "GEOPOLITICAL",
    "SECTOR",
    "OTHER",
]

DIRECTIONS = [
    "VERY_NEGATIVE",
    "NEGATIVE",
    "NEUTRAL",
    "POSITIVE",
    "VERY_POSITIVE",
    "UNKNOWN",
]

IMPORTANCES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

OUTPUT_COLUMNS = [
    "ClassifiedAtUTC",
    "Model",
    "EventID",
    "Ticker",
    "Market",
    "FirstSeenUTC",
    "LastSeenUTC",
    "ArticleCount",
    "PublisherCount",
    "Sources",
    "RepresentativeTitle",
    "RepresentativeSummary",
    "RepresentativeURL",
    "Category",
    "Direction",
    "Importance",
    "Confidence",
    "IsRelevant",
    "Freshness",
    "Reason",
    "KeyFact",
    "PotentialHorizon",
    "NeedsHumanReview",
]

SUMMARY_COLUMNS = [
    "GeneratedAtUTC",
    "TotalEvents",
    "RelevantEvents",
    "PositiveEvents",
    "NegativeEvents",
    "HighOrCriticalEvents",
    "HumanReviewEvents",
    "TopCategory",
    "TopDirection",
]

ERROR_COLUMNS = [
    "OccurredAtUTC",
    "EventID",
    "Ticker",
    "Error",
]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": CATEGORIES,
        },
        "direction": {
            "type": "string",
            "enum": DIRECTIONS,
        },
        "importance": {
            "type": "string",
            "enum": IMPORTANCES,
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "is_relevant": {"type": "boolean"},
        "freshness": {
            "type": "string",
            "enum": ["FRESH", "RECENT", "STALE", "UNKNOWN"],
        },
        "reason": {
            "type": "string",
            "maxLength": 240,
        },
        "key_fact": {
            "type": "string",
            "maxLength": 240,
        },
        "potential_horizon": {
            "type": "string",
            "enum": ["INTRADAY", "1_2_DAYS", "3_5_DAYS", "LONGER", "UNKNOWN"],
        },
        "needs_human_review": {"type": "boolean"},
    },
    "required": [
        "category",
        "direction",
        "importance",
        "confidence",
        "is_relevant",
        "freshness",
        "reason",
        "key_fact",
        "potential_horizon",
        "needs_human_review",
    ],
    "additionalProperties": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def save_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def event_prompt(event: dict[str, str]) -> str:
    title = clean(event.get("RepresentativeTitle"))
    summary = clean(event.get("RepresentativeSummary"))

    if len(summary) > 1600:
        summary = summary[:1600]

    return f"""
Tu es l'agent Catalyseur d'un systeme de trading actions Paris et New York.
Analyse uniquement les informations fournies. N'invente aucun fait et ne
suppose pas le contenu d'un article inaccessible.

Objectif: evaluer si cet evenement peut influencer le titre sur un horizon
intraday a cinq seances. Le resultat reste informatif et ne constitue pas
un ordre d'achat ou de vente.

Regles:
- CATEGORY doit utiliser l'une des categories imposees par le schema.
- DIRECTION mesure l'impact probable sur le titre, pas le ton journalistique.
- IMPORTANCE mesure la materialite potentielle pour l'entreprise ou le titre.
- CONFIDENCE doit baisser si le titre/resume est vague, promotionnel ou ambigu.
- IS_RELEVANT=false pour les articles generiques, listes de titres, bruits de
  marche, contenus sans fait nouveau ou sans lien specifique avec le ticker.
- NEEDS_HUMAN_REVIEW=true si les informations sont contradictoires, insuffisantes
  ou si l'identification du catalyseur est incertaine.
- REASON et KEY_FACT doivent etre tres courts et factuels.

Ticker: {clean(event.get('Ticker'))}
Marche: {clean(event.get('Market'))}
Premiere publication UTC: {clean(event.get('FirstSeenUTC'))}
Derniere publication UTC: {clean(event.get('LastSeenUTC'))}
Nombre d'articles: {clean(event.get('ArticleCount'))}
Nombre d'editeurs: {clean(event.get('PublisherCount'))}
Sources: {clean(event.get('Sources'))}
Categorie source: {clean(event.get('CategoryRaw'))}
Titre: {title}
Resume: {summary or 'Resume absent'}
""".strip()


def gemini_url() -> str:
    model = quote(GEMINI_MODEL, safe="-._")
    key = quote(GEMINI_API_KEY, safe="")
    return f"{GEMINI_BASE_URL}/{model}:generateContent?key={key}"


def call_gemini(event: dict[str, str]) -> dict[str, Any]:
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": event_prompt(event)}],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 500,
            "responseMimeType": "application/json",
            "responseJsonSchema": RESPONSE_SCHEMA,
        },
    }

    request = Request(
        gemini_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "investment-assistant-catalyst-semantic/5.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            outer = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini HTTP {error.code}: {body[:500]}") from error
    except URLError as error:
        raise RuntimeError(f"Erreur reseau Gemini: {error.reason}") from error

    candidates = outer.get("candidates", [])
    if not candidates:
        raise RuntimeError(
            "Gemini n'a retourne aucun candidat: "
            + clean(outer.get("promptFeedback"))
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(clean(part.get("text")) for part in parts)
    if not text:
        raise RuntimeError("Reponse Gemini vide")

    try:
        result = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"JSON Gemini invalide: {text[:500]}") from error

    return validate_result(result)


def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    category = clean(result.get("category")).upper()
    direction = clean(result.get("direction")).upper()
    importance = clean(result.get("importance")).upper()
    freshness = clean(result.get("freshness")).upper()
    horizon = clean(result.get("potential_horizon")).upper()

    if category not in CATEGORIES:
        category = "OTHER"
    if direction not in DIRECTIONS:
        direction = "UNKNOWN"
    if importance not in IMPORTANCES:
        importance = "LOW"
    if freshness not in {"FRESH", "RECENT", "STALE", "UNKNOWN"}:
        freshness = "UNKNOWN"
    if horizon not in {"INTRADAY", "1_2_DAYS", "3_5_DAYS", "LONGER", "UNKNOWN"}:
        horizon = "UNKNOWN"

    try:
        confidence = int(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = min(100, max(0, confidence))

    return {
        "Category": category,
        "Direction": direction,
        "Importance": importance,
        "Confidence": confidence,
        "IsRelevant": bool(result.get("is_relevant", False)),
        "Freshness": freshness,
        "Reason": clean(result.get("reason"))[:240],
        "KeyFact": clean(result.get("key_fact"))[:240],
        "PotentialHorizon": horizon,
        "NeedsHumanReview": bool(result.get("needs_human_review", False)),
    }


def classify_with_retry(event: dict[str, str]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return call_gemini(event)
        except Exception as error:
            last_error = error
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(str(last_error))


def build_output_row(
    event: dict[str, str],
    classification: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ClassifiedAtUTC": now_iso(),
        "Model": GEMINI_MODEL,
        "EventID": clean(event.get("EventID")),
        "Ticker": clean(event.get("Ticker")),
        "Market": clean(event.get("Market")),
        "FirstSeenUTC": clean(event.get("FirstSeenUTC")),
        "LastSeenUTC": clean(event.get("LastSeenUTC")),
        "ArticleCount": clean(event.get("ArticleCount")),
        "PublisherCount": clean(event.get("PublisherCount")),
        "Sources": clean(event.get("Sources")),
        "RepresentativeTitle": clean(event.get("RepresentativeTitle")),
        "RepresentativeSummary": clean(event.get("RepresentativeSummary")),
        "RepresentativeURL": clean(event.get("RepresentativeURL")),
        **classification,
    }


def sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    importance_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    direction_rank = {
        "VERY_NEGATIVE": 5,
        "VERY_POSITIVE": 5,
        "NEGATIVE": 4,
        "POSITIVE": 4,
        "NEUTRAL": 1,
        "UNKNOWN": 0,
    }
    return sorted(
        rows,
        key=lambda row: (
            bool(row.get("IsRelevant")),
            importance_rank.get(clean(row.get("Importance")), 0),
            direction_rank.get(clean(row.get("Direction")), 0),
            int(row.get("Confidence") or 0),
            clean(row.get("LastSeenUTC")),
        ),
        reverse=True,
    )


def build_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []

    categories = Counter(clean(row.get("Category")) for row in rows)
    directions = Counter(clean(row.get("Direction")) for row in rows)

    return [{
        "GeneratedAtUTC": now_iso(),
        "TotalEvents": len(rows),
        "RelevantEvents": sum(bool(row.get("IsRelevant")) for row in rows),
        "PositiveEvents": sum(
            clean(row.get("Direction")) in {"POSITIVE", "VERY_POSITIVE"}
            for row in rows
        ),
        "NegativeEvents": sum(
            clean(row.get("Direction")) in {"NEGATIVE", "VERY_NEGATIVE"}
            for row in rows
        ),
        "HighOrCriticalEvents": sum(
            clean(row.get("Importance")) in {"HIGH", "CRITICAL"}
            for row in rows
        ),
        "HumanReviewEvents": sum(bool(row.get("NeedsHumanReview")) for row in rows),
        "TopCategory": categories.most_common(1)[0][0] if categories else "",
        "TopDirection": directions.most_common(1)[0][0] if directions else "",
    }]


def print_top_events(rows: list[dict[str, Any]]) -> None:
    top = [
        row for row in rows
        if bool(row.get("IsRelevant"))
        and clean(row.get("Importance")) in {"HIGH", "CRITICAL"}
    ][:15]

    print()
    print("Evenements HIGH / CRITICAL pertinents :")
    if not top:
        print("- Aucun")
        return

    for row in top:
        title = clean(row.get("RepresentativeTitle"))
        if len(title) > 90:
            title = title[:87] + "..."
        print(
            f"- {clean(row.get('Ticker')):<8} "
            f"{clean(row.get('Importance')):<8} "
            f"{clean(row.get('Direction')):<14} "
            f"conf={int(row.get('Confidence') or 0):>3} | {title}"
        )


def main() -> int:
    print("=" * 120)
    print("CATALYST SEMANTIC V5 - GEMINI STRUCTURED OUTPUT")
    print("MODE INFORMATIF UNIQUEMENT - AUCUN IMPACT SUR LES TRADES")
    print("=" * 120)

    if not EVENTS_FILE.exists():
        print(f"ERREUR: fichier absent: {EVENTS_FILE}")
        return 1

    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY absent. Classification semantique ignoree sans casser le pipeline.")
        print("Ajoute le secret GitHub GEMINI_API_KEY puis relance le workflow.")
        return 0

    events = load_csv(EVENTS_FILE)
    existing = load_csv(OUTPUT_FILE)
    existing_by_id = {
        clean(row.get("EventID")): row
        for row in existing
        if clean(row.get("EventID"))
    }

    pending = [
        event for event in events
        if clean(event.get("EventID"))
        and clean(event.get("EventID")) not in existing_by_id
    ]

    # Priorite aux evenements avec plusieurs articles, puis aux plus recents.
    pending.sort(
        key=lambda event: (
            int(clean(event.get("ArticleCount")) or 0),
            clean(event.get("LastSeenUTC")),
        ),
        reverse=True,
    )
    pending = pending[:MAX_EVENTS_PER_RUN]

    print(f"Modele                 : {GEMINI_MODEL}")
    print(f"Evenements disponibles : {len(events)}")
    print(f"Deja classes           : {len(existing_by_id)}")
    print(f"A classer maintenant   : {len(pending)}")
    print(f"Limite par execution   : {MAX_EVENTS_PER_RUN}")

    new_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for index, event in enumerate(pending, start=1):
        event_id = clean(event.get("EventID"))
        ticker = clean(event.get("Ticker"))
        try:
            classification = classify_with_retry(event)
            row = build_output_row(event, classification)
            new_rows.append(row)
            print(
                f"[{index:>2}/{len(pending)}] {ticker:<8} "
                f"{classification['Category']:<14} "
                f"{classification['Direction']:<14} "
                f"{classification['Importance']:<8} "
                f"conf={classification['Confidence']:>3}"
            )
        except Exception as error:
            errors.append({
                "OccurredAtUTC": now_iso(),
                "EventID": event_id,
                "Ticker": ticker,
                "Error": str(error),
            })
            print(f"[{index:>2}/{len(pending)}] {ticker:<8} ERREUR: {error}")

        time.sleep(REQUEST_PAUSE_SECONDS)

    combined_by_id = dict(existing_by_id)
    for row in new_rows:
        combined_by_id[clean(row.get("EventID"))] = row

    combined = sort_rows(list(combined_by_id.values()))
    save_csv(OUTPUT_FILE, combined, OUTPUT_COLUMNS)
    save_csv(SUMMARY_FILE, build_summary(combined), SUMMARY_COLUMNS)

    previous_errors = load_csv(ERROR_FILE)
    save_csv(ERROR_FILE, previous_errors + errors, ERROR_COLUMNS)

    print()
    print("=" * 120)
    print("RESUME V5")
    print("=" * 120)
    print(f"Nouveaux evenements classes : {len(new_rows)}")
    print(f"Erreurs                     : {len(errors)}")
    print(f"Total historique classe     : {len(combined)}")
    print(f"Pertinents                   : {sum(bool(row.get('IsRelevant')) for row in combined)}")
    print(f"HIGH / CRITICAL             : {sum(clean(row.get('Importance')) in {'HIGH', 'CRITICAL'} for row in combined)}")
    print(f"Revue humaine               : {sum(bool(row.get('NeedsHumanReview')) for row in combined)}")
    print(f"Rapport                     : {OUTPUT_FILE}")
    print(f"Resume                      : {SUMMARY_FILE}")
    print(f"Erreurs                     : {ERROR_FILE}")

    print_top_events(combined)

    print("=" * 120)
    print("FIN CATALYST SEMANTIC V5")
    print("=" * 120)

    # Les erreurs individuelles ne cassent pas le pipeline de trading.
    return 0


if __name__ == "__main__":
    sys.exit(main())
