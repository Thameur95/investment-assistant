from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ============================================================
# CATALYST SEMANTIC V5.1
# - Prefiltre local gratuit
# - Top evenements uniquement
# - Un seul appel Gemini groupe
# - Arret immediat sur timeout, quota ou erreur API
# - Informatif uniquement, aucun impact sur les trades
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

EVENTS_FILE = REPORTS_DIR / "catalyst_events.csv"
OUTPUT_FILE = REPORTS_DIR / "catalyst_semantic.csv"
SUMMARY_FILE = REPORTS_DIR / "catalyst_semantic_summary.csv"
ERROR_FILE = REPORTS_DIR / "catalyst_semantic_errors.csv"
PREFILTER_FILE = REPORTS_DIR / "catalyst_prefilter.csv"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

REQUEST_TIMEOUT_SECONDS = 75
TOP_EVENTS_PER_RUN = int(os.getenv("CATALYST_TOP_EVENTS_PER_RUN", "12"))
MIN_LOCAL_SCORE = float(os.getenv("CATALYST_MIN_LOCAL_SCORE", "3.0"))
MAX_SUMMARY_CHARS = 700

CATEGORIES = [
    "EARNINGS", "GUIDANCE", "CONTRACT", "PRODUCT", "M&A", "PARTNERSHIP",
    "CAPITAL_RAISE", "DILUTION", "REGULATORY", "LEGAL", "MANAGEMENT",
    "ANALYST", "INSIDER", "DIVIDEND", "BUYBACK", "IPO", "MACRO",
    "GEOPOLITICAL", "SECTOR", "OTHER",
]
DIRECTIONS = [
    "VERY_NEGATIVE", "NEGATIVE", "NEUTRAL", "POSITIVE", "VERY_POSITIVE", "UNKNOWN",
]
IMPORTANCES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

# Poids locaux volontairement simples. Ils servent uniquement a reduire
# les appels LLM, pas a prendre une decision de trading.
KEYWORD_WEIGHTS = {
    "earnings": 5, "results": 4, "revenue": 3, "profit": 3, "eps": 4,
    "guidance": 6, "outlook": 5, "forecast": 4, "raises": 3, "cuts": 4,
    "contract": 5, "order": 4, "awarded": 4, "selected": 3,
    "acquisition": 6, "acquire": 5, "merger": 6, "takeover": 6,
    "partnership": 4, "collaboration": 3, "agreement": 3,
    "fda": 6, "approval": 5, "regulatory": 5, "investigation": 5,
    "lawsuit": 5, "probe": 5, "recall": 5, "ban": 5,
    "offering": 6, "capital raise": 6, "dilution": 7, "secondary offering": 7,
    "buyback": 5, "dividend": 4, "layoffs": 5, "ceo": 4, "resigns": 5,
    "resignation": 5, "bankruptcy": 8, "default": 7, "warning": 5,
}

NOISE_PATTERNS = [
    r"^stock market today", r"^sector update", r"stocks? to watch",
    r"why .* stock", r"top \d+ stocks", r"best stocks", r"market roundup",
    r"closing bell", r"midday update", r"premarket movers", r"after-hours movers",
    r"national medals", r"award ceremony", r"honorary", r"podcast", r"opinion",
]

OUTPUT_COLUMNS = [
    "ClassifiedAtUTC", "Model", "EventID", "Ticker", "Market", "FirstSeenUTC",
    "LastSeenUTC", "ArticleCount", "PublisherCount", "Sources",
    "RepresentativeTitle", "RepresentativeSummary", "RepresentativeURL",
    "LocalPriorityScore", "Category", "Direction", "Importance", "Confidence",
    "IsRelevant", "Freshness", "Reason", "KeyFact", "PotentialHorizon",
    "NeedsHumanReview",
]
PREFILTER_COLUMNS = [
    "EventID", "Ticker", "LastSeenUTC", "ArticleCount", "PublisherCount",
    "Sources", "RepresentativeTitle", "LocalPriorityScore", "LocalReasons",
    "PrefilterStatus",
]
SUMMARY_COLUMNS = [
    "GeneratedAtUTC", "AvailableEvents", "LocallyEligibleEvents", "SentToGemini",
    "ClassifiedEvents", "RelevantEvents", "PositiveEvents", "NegativeEvents",
    "HighOrCriticalEvents", "HumanReviewEvents", "TopCategory", "TopDirection",
    "GeminiStatus",
]
ERROR_COLUMNS = ["OccurredAtUTC", "Stage", "Error"]

BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "direction": {"type": "string", "enum": DIRECTIONS},
                    "importance": {"type": "string", "enum": IMPORTANCES},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "is_relevant": {"type": "boolean"},
                    "freshness": {
                        "type": "string",
                        "enum": ["FRESH", "RECENT", "STALE", "UNKNOWN"],
                    },
                    "reason": {"type": "string", "maxLength": 220},
                    "key_fact": {"type": "string", "maxLength": 220},
                    "potential_horizon": {
                        "type": "string",
                        "enum": ["INTRADAY", "1_2_DAYS", "3_5_DAYS", "LONGER", "UNKNOWN"],
                    },
                    "needs_human_review": {"type": "boolean"},
                },
                "required": [
                    "event_id", "category", "direction", "importance", "confidence",
                    "is_relevant", "freshness", "reason", "key_fact",
                    "potential_horizon", "needs_human_review",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def to_int(value: Any) -> int:
    try:
        return int(float(clean(value) or 0))
    except (TypeError, ValueError):
        return 0


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


def local_priority(event: dict[str, str]) -> tuple[float, list[str]]:
    title = clean(event.get("RepresentativeTitle")).lower()
    summary = clean(event.get("RepresentativeSummary")).lower()
    text = f"{title} {summary}"
    reasons: list[str] = []
    score = 0.0

    article_count = to_int(event.get("ArticleCount"))
    publisher_count = to_int(event.get("PublisherCount"))
    sources = clean(event.get("Sources")).upper()

    if article_count > 1:
        bonus = min(4.0, (article_count - 1) * 1.2)
        score += bonus
        reasons.append(f"multi_articles:+{bonus:.1f}")
    if publisher_count > 1:
        bonus = min(3.0, (publisher_count - 1) * 1.0)
        score += bonus
        reasons.append(f"multi_publishers:+{bonus:.1f}")
    if any(source in sources for source in ["OFFICIAL", "SEC_EDGAR"]):
        score += 6.0
        reasons.append("source_officielle:+6")

    matched_keywords = []
    for keyword, weight in KEYWORD_WEIGHTS.items():
        if keyword in text:
            score += weight
            matched_keywords.append(f"{keyword}:+{weight}")
    if matched_keywords:
        reasons.extend(matched_keywords[:6])

    for pattern in NOISE_PATTERNS:
        if re.search(pattern, title, flags=re.IGNORECASE):
            score -= 8.0
            reasons.append("bruit_generique:-8")
            break

    if not summary:
        score -= 1.0
        reasons.append("resume_absent:-1")

    return round(score, 2), reasons


def prefilter(events: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    ranked: list[tuple[float, dict[str, str], list[str]]] = []
    report: list[dict[str, Any]] = []

    for event in events:
        score, reasons = local_priority(event)
        status = "ELIGIBLE" if score >= MIN_LOCAL_SCORE else "REJECTED_LOCAL"
        row = {
            "EventID": clean(event.get("EventID")),
            "Ticker": clean(event.get("Ticker")),
            "LastSeenUTC": clean(event.get("LastSeenUTC")),
            "ArticleCount": to_int(event.get("ArticleCount")),
            "PublisherCount": to_int(event.get("PublisherCount")),
            "Sources": clean(event.get("Sources")),
            "RepresentativeTitle": clean(event.get("RepresentativeTitle")),
            "LocalPriorityScore": score,
            "LocalReasons": " | ".join(reasons),
            "PrefilterStatus": status,
        }
        report.append(row)
        if status == "ELIGIBLE":
            ranked.append((score, event, reasons))

    ranked.sort(
        key=lambda item: (
            item[0],
            to_int(item[1].get("ArticleCount")),
            clean(item[1].get("LastSeenUTC")),
        ),
        reverse=True,
    )

    selected = []
    for score, event, _ in ranked[:TOP_EVENTS_PER_RUN]:
        copy = dict(event)
        copy["LocalPriorityScore"] = score
        selected.append(copy)

    selected_ids = {clean(event.get("EventID")) for event in selected}
    for row in report:
        if row["EventID"] in selected_ids:
            row["PrefilterStatus"] = "SELECTED_FOR_GEMINI"

    report.sort(
        key=lambda row: (float(row["LocalPriorityScore"]), clean(row["LastSeenUTC"])),
        reverse=True,
    )
    return selected, report


def build_batch_prompt(events: list[dict[str, str]]) -> str:
    compact_events = []
    for event in events:
        compact_events.append({
            "event_id": clean(event.get("EventID")),
            "ticker": clean(event.get("Ticker")),
            "market": clean(event.get("Market")),
            "first_seen_utc": clean(event.get("FirstSeenUTC")),
            "last_seen_utc": clean(event.get("LastSeenUTC")),
            "article_count": to_int(event.get("ArticleCount")),
            "publisher_count": to_int(event.get("PublisherCount")),
            "sources": clean(event.get("Sources")),
            "title": clean(event.get("RepresentativeTitle")),
            "summary": clean(event.get("RepresentativeSummary"))[:MAX_SUMMARY_CHARS],
            "local_priority_score": event.get("LocalPriorityScore", 0),
        })

    return (
        "Tu es l'agent Catalyseur d'un systeme de trading actions Paris et New York. "
        "Classe chaque evenement fourni, sans inventer de faits. L'impact vise un horizon "
        "intraday a cinq seances. La direction mesure l'impact probable sur le titre, pas "
        "le ton du titre. Marque is_relevant=false pour le bruit de marche, les listes de "
        "titres, les opinions sans fait nouveau et les contenus sans lien specifique. "
        "Baisse confidence et active needs_human_review si les donnees sont vagues ou "
        "contradictoires. Retourne exactement une classification par event_id, sans omission.\n\n"
        "EVENEMENTS:\n" + json.dumps(compact_events, ensure_ascii=False)
    )


def gemini_url() -> str:
    return (
        f"{GEMINI_BASE_URL}/{quote(GEMINI_MODEL, safe='-._')}:generateContent"
        f"?key={quote(GEMINI_API_KEY, safe='')}"
    )


def parse_json_tolerant(text: str) -> dict[str, Any]:
    content = clean(text)
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content, flags=re.IGNORECASE).strip()
        content = re.sub(r"```$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def call_gemini_once(events: list[dict[str, str]]) -> dict[str, Any]:
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": build_batch_prompt(events)}],
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 5000,
            "responseMimeType": "application/json",
            "responseJsonSchema": BATCH_SCHEMA,
            "thinkingConfig": {"thinkingLevel": "LOW"},
        },
    }
    request = Request(
        gemini_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "investment-assistant-catalyst-semantic/5.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            outer = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini HTTP {error.code}: {body[:800]}") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Gemini indisponible: {error}") from error

    candidates = outer.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Aucun candidat Gemini: {outer.get('promptFeedback', {})}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(str(part.get("text", "")) for part in parts)
    if not text:
        raise RuntimeError("Reponse Gemini vide")
    return parse_json_tolerant(text)


def normalise_classification(item: dict[str, Any]) -> dict[str, Any]:
    category = clean(item.get("category")).upper()
    direction = clean(item.get("direction")).upper()
    importance = clean(item.get("importance")).upper()
    freshness = clean(item.get("freshness")).upper()
    horizon = clean(item.get("potential_horizon")).upper()
    try:
        confidence = max(0, min(100, int(item.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    return {
        "Category": category if category in CATEGORIES else "OTHER",
        "Direction": direction if direction in DIRECTIONS else "UNKNOWN",
        "Importance": importance if importance in IMPORTANCES else "LOW",
        "Confidence": confidence,
        "IsRelevant": bool(item.get("is_relevant", False)),
        "Freshness": freshness if freshness in {"FRESH", "RECENT", "STALE", "UNKNOWN"} else "UNKNOWN",
        "Reason": clean(item.get("reason"))[:220],
        "KeyFact": clean(item.get("key_fact"))[:220],
        "PotentialHorizon": horizon if horizon in {"INTRADAY", "1_2_DAYS", "3_5_DAYS", "LONGER", "UNKNOWN"} else "UNKNOWN",
        "NeedsHumanReview": bool(item.get("needs_human_review", False)),
    }


def classify_batch(events: list[dict[str, str]]) -> list[dict[str, Any]]:
    response = call_gemini_once(events)
    items = response.get("classifications", [])
    if not isinstance(items, list):
        raise RuntimeError("Champ classifications absent ou invalide")

    by_id = {
        clean(item.get("event_id")): normalise_classification(item)
        for item in items
        if clean(item.get("event_id"))
    }
    rows = []
    for event in events:
        event_id = clean(event.get("EventID"))
        classification = by_id.get(event_id)
        if classification is None:
            classification = {
                "Category": "OTHER", "Direction": "UNKNOWN", "Importance": "LOW",
                "Confidence": 0, "IsRelevant": False, "Freshness": "UNKNOWN",
                "Reason": "Classification absente de la reponse groupee.", "KeyFact": "",
                "PotentialHorizon": "UNKNOWN", "NeedsHumanReview": True,
            }
        rows.append({
            "ClassifiedAtUTC": now_iso(),
            "Model": GEMINI_MODEL,
            "EventID": event_id,
            "Ticker": clean(event.get("Ticker")),
            "Market": clean(event.get("Market")),
            "FirstSeenUTC": clean(event.get("FirstSeenUTC")),
            "LastSeenUTC": clean(event.get("LastSeenUTC")),
            "ArticleCount": to_int(event.get("ArticleCount")),
            "PublisherCount": to_int(event.get("PublisherCount")),
            "Sources": clean(event.get("Sources")),
            "RepresentativeTitle": clean(event.get("RepresentativeTitle")),
            "RepresentativeSummary": clean(event.get("RepresentativeSummary")),
            "RepresentativeURL": clean(event.get("RepresentativeURL")),
            "LocalPriorityScore": event.get("LocalPriorityScore", 0),
            **classification,
        })
    return rows


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return clean(value).lower() in {"true", "1", "yes", "oui"}


def build_summary(
    available_count: int,
    eligible_count: int,
    sent_count: int,
    rows: list[dict[str, Any]],
    status: str,
) -> list[dict[str, Any]]:
    categories = Counter(clean(row.get("Category")) for row in rows)
    directions = Counter(clean(row.get("Direction")) for row in rows)
    return [{
        "GeneratedAtUTC": now_iso(),
        "AvailableEvents": available_count,
        "LocallyEligibleEvents": eligible_count,
        "SentToGemini": sent_count,
        "ClassifiedEvents": len(rows),
        "RelevantEvents": sum(as_bool(row.get("IsRelevant")) for row in rows),
        "PositiveEvents": sum(clean(row.get("Direction")) in {"POSITIVE", "VERY_POSITIVE"} for row in rows),
        "NegativeEvents": sum(clean(row.get("Direction")) in {"NEGATIVE", "VERY_NEGATIVE"} for row in rows),
        "HighOrCriticalEvents": sum(clean(row.get("Importance")) in {"HIGH", "CRITICAL"} for row in rows),
        "HumanReviewEvents": sum(as_bool(row.get("NeedsHumanReview")) for row in rows),
        "TopCategory": categories.most_common(1)[0][0] if categories else "",
        "TopDirection": directions.most_common(1)[0][0] if directions else "",
        "GeminiStatus": status,
    }]


def main() -> int:
    print("=" * 120)
    print("CATALYST SEMANTIC V5.1 - PREFILTRE LOCAL + UN SEUL APPEL GEMINI")
    print("MODE INFORMATIF UNIQUEMENT - AUCUN IMPACT SUR LES TRADES")
    print("=" * 120)

    if not EVENTS_FILE.exists():
        print(f"ERREUR: fichier absent: {EVENTS_FILE}")
        return 1

    events = load_csv(EVENTS_FILE)
    existing = load_csv(OUTPUT_FILE)
    existing_by_id = {
        clean(row.get("EventID")): row
        for row in existing
        if clean(row.get("EventID"))
    }
    unclassified = [
        event for event in events
        if clean(event.get("EventID")) not in existing_by_id
    ]

    selected, prefilter_report = prefilter(unclassified)
    eligible_count = sum(
        row["PrefilterStatus"] in {"ELIGIBLE", "SELECTED_FOR_GEMINI"}
        for row in prefilter_report
    )
    save_csv(PREFILTER_FILE, prefilter_report, PREFILTER_COLUMNS)

    print(f"Modele                    : {GEMINI_MODEL}")
    print(f"Evenements disponibles    : {len(events)}")
    print(f"Deja classes              : {len(existing_by_id)}")
    print(f"Eligibles apres prefiltre  : {eligible_count}")
    print(f"Selectionnes pour Gemini  : {len(selected)}")
    print(f"Nombre d'appels Gemini    : {1 if selected else 0}")

    status = "NOT_CALLED"
    new_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    if selected and not GEMINI_API_KEY:
        status = "SKIPPED_NO_API_KEY"
        errors.append({
            "OccurredAtUTC": now_iso(),
            "Stage": "CONFIGURATION",
            "Error": "GEMINI_API_KEY absent",
        })
        print("Gemini ignore: GEMINI_API_KEY absent.")
    elif selected:
        try:
            new_rows = classify_batch(selected)
            status = "SUCCESS"
            print(f"Classification groupee recue : {len(new_rows)} evenements")
        except Exception as error:
            status = "FAILED_FAST"
            errors.append({
                "OccurredAtUTC": now_iso(),
                "Stage": "GEMINI_BATCH",
                "Error": str(error),
            })
            print(f"Gemini indisponible, arret immediat: {error}")

    combined = dict(existing_by_id)
    for row in new_rows:
        combined[clean(row.get("EventID"))] = row
    combined_rows = list(combined.values())
    combined_rows.sort(
        key=lambda row: (
            as_bool(row.get("IsRelevant")),
            {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(clean(row.get("Importance")), 0),
            to_int(row.get("Confidence")),
            clean(row.get("LastSeenUTC")),
        ),
        reverse=True,
    )

    save_csv(OUTPUT_FILE, combined_rows, OUTPUT_COLUMNS)
    save_csv(
        SUMMARY_FILE,
        build_summary(len(events), eligible_count, len(selected), combined_rows, status),
        SUMMARY_COLUMNS,
    )
    previous_errors = load_csv(ERROR_FILE)
    save_csv(ERROR_FILE, previous_errors + errors, ERROR_COLUMNS)

    print()
    print("=" * 120)
    print("RESUME V5.1")
    print("=" * 120)
    print(f"Statut Gemini               : {status}")
    print(f"Nouveaux evenements classes : {len(new_rows)}")
    print(f"Total historique classe     : {len(combined_rows)}")
    print(f"Erreurs de cette execution  : {len(errors)}")
    print(f"Prefiltre                   : {PREFILTER_FILE}")
    print(f"Rapport                     : {OUTPUT_FILE}")
    print(f"Resume                      : {SUMMARY_FILE}")
    print(f"Erreurs                     : {ERROR_FILE}")

    top = [
        row for row in combined_rows
        if as_bool(row.get("IsRelevant"))
        and clean(row.get("Importance")) in {"HIGH", "CRITICAL"}
    ][:12]
    print()
    print("Evenements HIGH / CRITICAL pertinents :")
    if not top:
        print("- Aucun")
    else:
        for row in top:
            title = clean(row.get("RepresentativeTitle"))[:95]
            print(
                f"- {clean(row.get('Ticker')):<8} {clean(row.get('Importance')):<8} "
                f"{clean(row.get('Direction')):<14} conf={to_int(row.get('Confidence')):>3} | {title}"
            )

    print("=" * 120)
    print("FIN CATALYST SEMANTIC V5.1")
    print("=" * 120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
