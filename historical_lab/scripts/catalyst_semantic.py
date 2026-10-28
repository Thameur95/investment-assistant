from __future__ import annotations

import csv
import json
import os
import re
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
# CATALYST SEMANTIC V5.2
# - Validation stricte de la pertinence ticker
# - Filtre anti-bruit editorial
# - Scores separes: RelevanceScore + CatalystScore
# - TOP N = plafond, jamais objectif
# - Un seul appel Gemini groupe
# - Retry borne sur erreurs transitoires uniquement
# - INFORMATIF UNIQUEMENT - AUCUN IMPACT SUR LES TRADES
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
GEMINI_MAX_ATTEMPTS = 3
GEMINI_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
GEMINI_RETRY_DELAYS_SECONDS = (5, 15)
TOP_EVENTS_PER_RUN = int(os.getenv("CATALYST_TOP_EVENTS_PER_RUN", "8"))
MIN_PRIORITY_SCORE = float(os.getenv("CATALYST_MIN_PRIORITY_SCORE", "9.0"))
MAX_SUMMARY_CHARS = 700

COMPANY_ALIASES: dict[str, list[str]] = {
    "AAPL": ["apple", "applecare", "iphone", "ipad", "macbook"],
    "AMZN": ["amazon", "amazon.com", "aws", "amazon web services"],
    "GOOGL": ["google", "alphabet", "youtube", "google cloud"],
    "META": ["meta", "facebook", "instagram", "whatsapp"],
    "MSFT": ["microsoft", "azure", "github", "linkedin"],
    "NVDA": ["nvidia", "geforce", "cuda"],
    "TSLA": ["tesla"],
    "VLO": ["valero", "valero energy"],
    "AIR.PA": ["airbus", "airbus se"],
    "ASML.AS": ["asml", "asml holding"],
    "SAF.PA": ["safran", "safran sa"],
    "SU": ["suncor", "suncor energy"],
    "TTE": ["totalenergies", "total energies"],
}

RELATED_PERSON_ENTITY_BLOCKLIST: dict[str, list[str]] = {"AMZN": ["blue origin"]}

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

CATALYST_RULES: list[tuple[str, list[str], float]] = [
    ("M&A", ["acquisition", "acquire", "merger", "takeover", "buyout"], 8.0),
    ("CAPITAL", ["capital raise", "secondary offering", "public offering", "share offering"], 8.0),
    ("DILUTION", ["dilution", "dilutive"], 8.0),
    ("GUIDANCE", ["guidance", "raises outlook", "raised outlook", "cuts outlook", "cut outlook", "forecast"], 7.0),
    ("REGULATORY", ["fda", "regulatory approval", "antitrust", "regulator", "approval"], 7.0),
    ("EARNINGS", ["earnings", "quarterly results", "financial results", "eps", "revenue"], 6.0),
    ("CONTRACT", ["contract", "order", "awarded", "selected to supply", "wins deal"], 6.0),
    ("LEGAL", ["lawsuit", "investigation", "probe", "subpoena", "recall", "ban"], 6.0),
    ("BUYBACK", ["buyback", "share repurchase", "repurchase program"], 5.0),
    ("MANAGEMENT", ["ceo resigns", "ceo resignation", "new ceo", "appoints ceo", "layoffs", "restructuring"], 5.0),
    ("DIVIDEND", ["dividend"], 4.0),
    ("PARTNERSHIP", ["partnership", "collaboration", "strategic agreement"], 4.0),
    ("PRODUCT", ["launches", "unveils", "new product", "product launch"], 3.0),
]

NOISE_RULES: list[tuple[str, str, float]] = [
    ("MARKET_ROUNDUP", r"stock market today|market roundup|closing bell|end week higher|midday update", -7.0),
    ("LISTICLE", r"stocks? to watch|top \d+ stocks|best stocks|top research reports", -6.0),
    ("MOVERS", r"premarket movers|after-hours movers|biggest movers", -6.0),
    ("OPINION", r"here'?s why i'?d|opinion|podcast|are markets", -5.0),
    ("PROMOTIONAL", r"onsite at|will be onsite|conference coverage|fireside interviews", -6.0),
    ("HONORIFIC", r"national medals|award ceremony|honorary", -8.0),
]

OUTPUT_COLUMNS = [
    "ClassifiedAtUTC", "Model", "EventID", "Ticker", "Market", "FirstSeenUTC",
    "LastSeenUTC", "ArticleCount", "PublisherCount", "Sources",
    "RepresentativeTitle", "RepresentativeSummary", "RepresentativeURL",
    "TickerRelation", "MatchedAliases", "RelevanceScore", "CatalystType",
    "CatalystScore", "NoiseDetected", "NoiseReason", "PriorityScore",
    "Category", "Direction", "Importance", "Confidence", "IsRelevant",
    "Freshness", "Reason", "KeyFact", "PotentialHorizon", "NeedsHumanReview",
]
PREFILTER_COLUMNS = [
    "EventID", "Ticker", "LastSeenUTC", "ArticleCount", "PublisherCount",
    "Sources", "RepresentativeTitle", "TickerRelation", "MatchedAliases",
    "RelevanceScore", "CatalystType", "CatalystScore", "NoiseDetected",
    "NoiseReason", "PriorityScore", "PrefilterDecision", "DecisionReason",
]
SUMMARY_COLUMNS = [
    "GeneratedAtUTC", "AvailableEvents", "UnclassifiedEvents",
    "TickerMismatchRejected", "NoiseRejected", "LowPriorityRejected",
    "EligibleEvents", "SentToGemini", "ClassifiedEvents", "RelevantEvents",
    "PositiveEvents", "NegativeEvents", "HighOrCriticalEvents",
    "HumanReviewEvents", "TopCategory", "TopDirection", "GeminiStatus",
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
                    "freshness": {"type": "string", "enum": ["FRESH", "RECENT", "STALE", "UNKNOWN"]},
                    "reason": {"type": "string", "maxLength": 220},
                    "key_fact": {"type": "string", "maxLength": 220},
                    "potential_horizon": {"type": "string", "enum": ["INTRADAY", "1_2_DAYS", "3_5_DAYS", "LONGER", "UNKNOWN"]},
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


def norm(value: Any) -> str:
    return clean(value).lower().replace("’", "'").replace("–", "-").replace("—", "-")


def to_int(value: Any) -> int:
    try:
        return int(float(clean(value) or 0))
    except (TypeError, ValueError):
        return 0


def as_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else clean(value).lower() in {"true", "1", "yes", "oui"}


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


def text_blob(event: dict[str, str]) -> tuple[str, str, str]:
    title = norm(event.get("RepresentativeTitle"))
    summary = norm(event.get("RepresentativeSummary"))
    return title, summary, f"{title} {summary}".strip()


def contains_phrase(text: str, phrase: str) -> bool:
    phrase = norm(phrase)
    if not phrase:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text, re.IGNORECASE) is not None


def ticker_base(ticker: str) -> str:
    return clean(ticker).upper().split(".")[0]


def evaluate_ticker_relation(event: dict[str, str]) -> dict[str, Any]:
    ticker = clean(event.get("Ticker")).upper()
    base = ticker_base(ticker).lower()
    title, _, blob = text_blob(event)
    aliases = COMPANY_ALIASES.get(ticker, [])
    matched = sorted({alias for alias in aliases if contains_phrase(blob, alias)})
    ticker_mentioned = contains_phrase(blob, base) if len(base) >= 3 else False
    blocked = [entity for entity in RELATED_PERSON_ENTITY_BLOCKLIST.get(ticker, []) if contains_phrase(blob, entity)]
    if blocked and not matched:
        return {"relation": "RELATED_PERSON_NOT_COMPANY", "matched": blocked, "score": 0.0, "reason": "Entite liee a une personne, mais distincte de la societe cotee."}
    if matched:
        score = 9.0 if any(contains_phrase(title, alias) for alias in matched) else 7.0
        return {"relation": "DIRECT", "matched": matched, "score": score, "reason": "Nom, marque ou filiale de la societe detecte dans le contenu."}
    if ticker_mentioned:
        score = 7.0 if contains_phrase(title, base) else 5.0
        return {"relation": "TICKER_MENTION", "matched": [base.upper()], "score": score, "reason": "Ticker explicitement mentionne dans le contenu."}
    return {"relation": "NONE", "matched": [], "score": 0.0, "reason": "Aucun lien direct detecte entre le contenu et le ticker."}


def evaluate_noise(event: dict[str, str]) -> dict[str, Any]:
    title, _, _ = text_blob(event)
    reasons, penalty = [], 0.0
    for name, pattern, weight in NOISE_RULES:
        if re.search(pattern, title, re.IGNORECASE):
            reasons.append(name)
            penalty += weight
    return {"detected": bool(reasons), "reason": "|".join(reasons), "penalty": round(penalty, 2)}


def evaluate_catalyst(event: dict[str, str], relation_score: float) -> dict[str, Any]:
    if relation_score <= 0:
        return {"type": "NONE", "score": 0.0, "matches": []}
    _, _, blob = text_blob(event)
    matches, scored = [], []
    for catalyst_type, keywords, weight in CATALYST_RULES:
        found = [keyword for keyword in keywords if contains_phrase(blob, keyword)]
        if found:
            scored.append((catalyst_type, weight))
            matches.extend(found)
    if not scored:
        return {"type": "NONE", "score": 0.0, "matches": []}
    scored.sort(key=lambda item: item[1], reverse=True)
    primary_type, primary_score = scored[0]
    bonus = 1.5 if len({item[0] for item in scored}) > 1 else 0.0
    return {"type": primary_type, "score": round(primary_score + bonus, 2), "matches": sorted(set(matches))[:8]}


def source_bonus(event: dict[str, str]) -> tuple[float, list[str]]:
    sources, bonus, reasons = clean(event.get("Sources")).upper(), 0.0, []
    if "SEC" in sources:
        bonus += 8.0
        reasons.append("SEC:+8")
    if "OFFICIAL" in sources:
        bonus += 6.0
        reasons.append("OFFICIAL:+6")
    return bonus, reasons


def corroboration_bonus(event: dict[str, str]) -> tuple[float, list[str]]:
    articles, publishers = to_int(event.get("ArticleCount")), to_int(event.get("PublisherCount"))
    bonus, reasons = 0.0, []
    if articles > 1:
        value = min(4.0, (articles - 1) * 1.2)
        bonus += value
        reasons.append(f"multi_articles:+{value:.1f}")
    if publishers > 1:
        value = min(3.0, (publishers - 1) * 1.0)
        bonus += value
        reasons.append(f"multi_publishers:+{value:.1f}")
    return bonus, reasons


def evaluate_event(event: dict[str, str]) -> dict[str, Any]:
    relation, noise = evaluate_ticker_relation(event), evaluate_noise(event)
    catalyst = evaluate_catalyst(event, relation["score"])
    src_bonus, src_reasons = source_bonus(event)
    corr_bonus, corr_reasons = corroboration_bonus(event)
    relevance_score, catalyst_score = relation["score"], catalyst["score"]
    priority = relevance_score + catalyst_score + src_bonus + corr_bonus + noise["penalty"]
    if relation["relation"] == "RELATED_PERSON_NOT_COMPANY":
        decision, reason = "REJECT_RELATED_PERSON_NOT_COMPANY", relation["reason"]
    elif relevance_score <= 0:
        decision, reason = "REJECT_TICKER_MISMATCH", relation["reason"]
    elif noise["detected"] and catalyst_score <= 0 and src_bonus <= 0:
        decision, reason = "REJECT_NOISE", f"Bruit editorial sans catalyseur direct: {noise['reason']}"
    elif catalyst_score <= 0 and src_bonus <= 0 and corr_bonus <= 0:
        decision, reason = "REJECT_LOW_CATALYST", "Lien ticker confirme, mais aucun catalyseur materiel detecte."
    elif priority < MIN_PRIORITY_SCORE:
        decision, reason = "REJECT_LOW_PRIORITY", f"PriorityScore {priority:.1f} < seuil {MIN_PRIORITY_SCORE:.1f}."
    else:
        decision = "ELIGIBLE"
        reason = " | ".join(src_reasons + corr_reasons) or "Pertinence ticker et catalyseur local confirmes."
    return {
        "TickerRelation": relation["relation"], "MatchedAliases": "|".join(relation["matched"]),
        "RelevanceScore": round(relevance_score, 2), "CatalystType": catalyst["type"],
        "CatalystScore": round(catalyst_score, 2), "NoiseDetected": noise["detected"],
        "NoiseReason": noise["reason"], "PriorityScore": round(priority, 2),
        "PrefilterDecision": decision, "DecisionReason": reason,
    }


def prefilter(events: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enriched, report = [], []
    for event in events:
        evaluation = evaluate_event(event)
        enriched_event = {**event, **evaluation}
        enriched.append(enriched_event)
        report.append({
            "EventID": clean(event.get("EventID")), "Ticker": clean(event.get("Ticker")),
            "LastSeenUTC": clean(event.get("LastSeenUTC")), "ArticleCount": to_int(event.get("ArticleCount")),
            "PublisherCount": to_int(event.get("PublisherCount")), "Sources": clean(event.get("Sources")),
            "RepresentativeTitle": clean(event.get("RepresentativeTitle")), **evaluation,
        })
    eligible = [row for row in enriched if row["PrefilterDecision"] == "ELIGIBLE"]
    eligible.sort(key=lambda e: (float(e.get("PriorityScore", 0)), float(e.get("RelevanceScore", 0)), to_int(e.get("ArticleCount")), clean(e.get("LastSeenUTC"))), reverse=True)
    selected = eligible[:TOP_EVENTS_PER_RUN]
    selected_ids = {clean(row.get("EventID")) for row in selected}
    for row in report:
        if row["EventID"] in selected_ids:
            row["PrefilterDecision"] = "SEND_TO_GEMINI"
            row["DecisionReason"] = "Classe parmi les meilleurs evenements eligibles de cette execution."
    report.sort(key=lambda r: (float(r.get("PriorityScore", 0)), clean(r.get("LastSeenUTC"))), reverse=True)
    return selected, report


def build_batch_prompt(events: list[dict[str, Any]]) -> str:
    compact = [{
        "event_id": clean(e.get("EventID")), "ticker": clean(e.get("Ticker")), "market": clean(e.get("Market")),
        "first_seen_utc": clean(e.get("FirstSeenUTC")), "last_seen_utc": clean(e.get("LastSeenUTC")),
        "article_count": to_int(e.get("ArticleCount")), "publisher_count": to_int(e.get("PublisherCount")),
        "sources": clean(e.get("Sources")), "title": clean(e.get("RepresentativeTitle")),
        "summary": clean(e.get("RepresentativeSummary"))[:MAX_SUMMARY_CHARS],
        "ticker_relation": clean(e.get("TickerRelation")), "matched_aliases": clean(e.get("MatchedAliases")),
        "relevance_score": e.get("RelevanceScore", 0), "local_catalyst_type": clean(e.get("CatalystType")),
        "catalyst_score": e.get("CatalystScore", 0), "priority_score": e.get("PriorityScore", 0),
    } for e in events]
    return (
        "Tu es l'agent Catalyseur d'un systeme de trading actions Paris et New York. "
        "Classe uniquement les evenements fournis, sans inventer de faits. L'horizon cible est intraday a cinq seances. "
        "La direction mesure l'impact probable sur le ticker, pas le ton journalistique. Marque is_relevant=false si, "
        "malgre le prefiltre, le fait nouveau n'a pas d'impact specifique ou exploitable pour le ticker. "
        "Ne transforme pas une opinion, une citation ou une analyse retrospective en fait nouveau. "
        "Baisse confidence et active needs_human_review si l'information est incomplete ou ambigue. "
        "Retourne exactement une classification par event_id.\n\nEVENEMENTS:\n" + json.dumps(compact, ensure_ascii=False)
    )


def gemini_url() -> str:
    return f"{GEMINI_BASE_URL}/{quote(GEMINI_MODEL, safe='-._')}:generateContent?key={quote(GEMINI_API_KEY, safe='')}"


def parse_json_tolerant(text: str) -> dict[str, Any]:
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content, flags=re.IGNORECASE).strip()
        content = re.sub(r"```$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def call_gemini_once(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Appel groupe avec retry borne pour erreurs transitoires uniquement."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": build_batch_prompt(events)}]}],
        "generationConfig": {
            "temperature": 0.0, "maxOutputTokens": 4000,
            "responseMimeType": "application/json", "responseJsonSchema": BATCH_SCHEMA,
            "thinkingConfig": {"thinkingLevel": "LOW"},
        },
    }
    encoded_payload = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
        request = Request(
            gemini_url(), data=encoded_payload,
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "investment-assistant-catalyst-semantic/5.2"},
            method="POST",
        )
        try:
            print(f"Appel Gemini {attempt}/{GEMINI_MAX_ATTEMPTS}")
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                outer = json.loads(response.read().decode("utf-8"))
            candidates = outer.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"Aucun candidat Gemini: {outer.get('promptFeedback', {})}")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(str(part.get("text", "")) for part in parts)
            if not text:
                raise RuntimeError("Reponse Gemini vide")
            if attempt > 1:
                print(f"Appel Gemini reussi a la tentative {attempt}.")
            return parse_json_tolerant(text)
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            message = f"Gemini HTTP {error.code}: {body[:800]}"
            last_error = RuntimeError(message)
            if error.code not in GEMINI_RETRYABLE_HTTP_CODES or attempt >= GEMINI_MAX_ATTEMPTS:
                raise last_error from error
            delay = GEMINI_RETRY_DELAYS_SECONDS[attempt - 1]
            print(message)
            print(f"Erreur Gemini transitoire. Nouvelle tentative apres {delay} seconde(s).")
            time.sleep(delay)
        except (URLError, TimeoutError) as error:
            message = f"Gemini indisponible: {error}"
            last_error = RuntimeError(message)
            if attempt >= GEMINI_MAX_ATTEMPTS:
                raise last_error from error
            delay = GEMINI_RETRY_DELAYS_SECONDS[attempt - 1]
            print(message)
            print(f"Erreur reseau transitoire. Nouvelle tentative apres {delay} seconde(s).")
            time.sleep(delay)
    raise RuntimeError(f"Gemini indisponible apres {GEMINI_MAX_ATTEMPTS} tentatives: {last_error}")


def normalise_classification(item: dict[str, Any]) -> dict[str, Any]:
    category, direction = clean(item.get("category")).upper(), clean(item.get("direction")).upper()
    importance, freshness = clean(item.get("importance")).upper(), clean(item.get("freshness")).upper()
    horizon = clean(item.get("potential_horizon")).upper()
    try:
        confidence = max(0, min(100, int(item.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    return {
        "Category": category if category in CATEGORIES else "OTHER",
        "Direction": direction if direction in DIRECTIONS else "UNKNOWN",
        "Importance": importance if importance in IMPORTANCES else "LOW",
        "Confidence": confidence, "IsRelevant": bool(item.get("is_relevant", False)),
        "Freshness": freshness if freshness in {"FRESH", "RECENT", "STALE", "UNKNOWN"} else "UNKNOWN",
        "Reason": clean(item.get("reason"))[:220], "KeyFact": clean(item.get("key_fact"))[:220],
        "PotentialHorizon": horizon if horizon in {"INTRADAY", "1_2_DAYS", "3_5_DAYS", "LONGER", "UNKNOWN"} else "UNKNOWN",
        "NeedsHumanReview": bool(item.get("needs_human_review", False)),
    }


def classify_batch(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    response = call_gemini_once(events)
    items = response.get("classifications", [])
    if not isinstance(items, list):
        raise RuntimeError("Champ classifications absent ou invalide")
    by_id = {clean(item.get("event_id")): normalise_classification(item) for item in items if clean(item.get("event_id"))}
    rows = []
    for event in events:
        event_id = clean(event.get("EventID"))
        classification = by_id.get(event_id) or {
            "Category": "OTHER", "Direction": "UNKNOWN", "Importance": "LOW", "Confidence": 0,
            "IsRelevant": False, "Freshness": "UNKNOWN", "Reason": "Classification absente de la reponse groupee.",
            "KeyFact": "", "PotentialHorizon": "UNKNOWN", "NeedsHumanReview": True,
        }
        rows.append({
            "ClassifiedAtUTC": now_iso(), "Model": GEMINI_MODEL, "EventID": event_id,
            "Ticker": clean(event.get("Ticker")), "Market": clean(event.get("Market")),
            "FirstSeenUTC": clean(event.get("FirstSeenUTC")), "LastSeenUTC": clean(event.get("LastSeenUTC")),
            "ArticleCount": to_int(event.get("ArticleCount")), "PublisherCount": to_int(event.get("PublisherCount")),
            "Sources": clean(event.get("Sources")), "RepresentativeTitle": clean(event.get("RepresentativeTitle")),
            "RepresentativeSummary": clean(event.get("RepresentativeSummary")), "RepresentativeURL": clean(event.get("RepresentativeURL")),
            "TickerRelation": clean(event.get("TickerRelation")), "MatchedAliases": clean(event.get("MatchedAliases")),
            "RelevanceScore": event.get("RelevanceScore", 0), "CatalystType": clean(event.get("CatalystType")),
            "CatalystScore": event.get("CatalystScore", 0), "NoiseDetected": event.get("NoiseDetected", False),
            "NoiseReason": clean(event.get("NoiseReason")), "PriorityScore": event.get("PriorityScore", 0), **classification,
        })
    return rows


def build_summary(available: int, unclassified: int, report: list[dict[str, Any]], sent: int, rows: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    decisions = Counter(clean(row.get("PrefilterDecision")) for row in report)
    categories = Counter(clean(row.get("Category")) for row in rows)
    directions = Counter(clean(row.get("Direction")) for row in rows)
    return [{
        "GeneratedAtUTC": now_iso(), "AvailableEvents": available, "UnclassifiedEvents": unclassified,
        "TickerMismatchRejected": decisions.get("REJECT_TICKER_MISMATCH", 0) + decisions.get("REJECT_RELATED_PERSON_NOT_COMPANY", 0),
        "NoiseRejected": decisions.get("REJECT_NOISE", 0),
        "LowPriorityRejected": decisions.get("REJECT_LOW_CATALYST", 0) + decisions.get("REJECT_LOW_PRIORITY", 0),
        "EligibleEvents": decisions.get("ELIGIBLE", 0) + decisions.get("SEND_TO_GEMINI", 0),
        "SentToGemini": sent, "ClassifiedEvents": len(rows), "RelevantEvents": sum(as_bool(r.get("IsRelevant")) for r in rows),
        "PositiveEvents": sum(clean(r.get("Direction")) in {"POSITIVE", "VERY_POSITIVE"} for r in rows),
        "NegativeEvents": sum(clean(r.get("Direction")) in {"NEGATIVE", "VERY_NEGATIVE"} for r in rows),
        "HighOrCriticalEvents": sum(clean(r.get("Importance")) in {"HIGH", "CRITICAL"} for r in rows),
        "HumanReviewEvents": sum(as_bool(r.get("NeedsHumanReview")) for r in rows),
        "TopCategory": categories.most_common(1)[0][0] if categories else "",
        "TopDirection": directions.most_common(1)[0][0] if directions else "", "GeminiStatus": status,
    }]


def main() -> int:
    print("=" * 120)
    print("CATALYST SEMANTIC V5.2 - TICKER VALIDATION + ANTI-BRUIT + RETRY GEMINI BORNE")
    print("MODE INFORMATIF UNIQUEMENT - AUCUN IMPACT SUR LES TRADES")
    print("=" * 120)
    if not EVENTS_FILE.exists():
        print(f"ERREUR: fichier absent: {EVENTS_FILE}")
        return 1
    events, existing = load_csv(EVENTS_FILE), load_csv(OUTPUT_FILE)
    existing_by_id = {clean(row.get("EventID")): row for row in existing if clean(row.get("EventID"))}
    unclassified_events = [event for event in events if clean(event.get("EventID")) not in existing_by_id]
    selected, prefilter_report = prefilter(unclassified_events)
    save_csv(PREFILTER_FILE, prefilter_report, PREFILTER_COLUMNS)
    decisions = Counter(clean(row.get("PrefilterDecision")) for row in prefilter_report)
    print(f"Modele                         : {GEMINI_MODEL}")
    print(f"Evenements disponibles         : {len(events)}")
    print(f"Deja classes                   : {len(existing_by_id)}")
    print(f"A evaluer localement           : {len(unclassified_events)}")
    print(f"Rejets mismatch ticker         : {decisions.get('REJECT_TICKER_MISMATCH', 0) + decisions.get('REJECT_RELATED_PERSON_NOT_COMPANY', 0)}")
    print(f"Rejets bruit editorial         : {decisions.get('REJECT_NOISE', 0)}")
    print(f"Rejets catalyseur/priorite     : {decisions.get('REJECT_LOW_CATALYST', 0) + decisions.get('REJECT_LOW_PRIORITY', 0)}")
    print(f"Selectionnes pour Gemini       : {len(selected)}")
    print(f"Nombre maximal de tentatives   : {GEMINI_MAX_ATTEMPTS if selected else 0}")
    status, new_rows, errors = "NOT_CALLED", [], []
    if selected and not GEMINI_API_KEY:
        status = "SKIPPED_NO_API_KEY"
        errors.append({"OccurredAtUTC": now_iso(), "Stage": "CONFIGURATION", "Error": "GEMINI_API_KEY absent"})
        print("Gemini ignore: GEMINI_API_KEY absent.")
    elif selected:
        try:
            new_rows = classify_batch(selected)
            status = "SUCCESS"
            print(f"Classification groupee recue   : {len(new_rows)} evenements")
        except Exception as error:
            status = "FAILED_FAST"
            errors.append({"OccurredAtUTC": now_iso(), "Stage": "GEMINI_BATCH", "Error": str(error)})
            print(f"Gemini indisponible apres retries, arret: {error}")
    combined = dict(existing_by_id)
    for row in new_rows:
        combined[clean(row.get("EventID"))] = row
    combined_rows = list(combined.values())
    combined_rows.sort(key=lambda r: (as_bool(r.get("IsRelevant")), {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(clean(r.get("Importance")), 0), to_int(r.get("Confidence")), clean(r.get("LastSeenUTC"))), reverse=True)
    save_csv(OUTPUT_FILE, combined_rows, OUTPUT_COLUMNS)
    save_csv(SUMMARY_FILE, build_summary(len(events), len(unclassified_events), prefilter_report, len(selected), combined_rows, status), SUMMARY_COLUMNS)
    save_csv(ERROR_FILE, load_csv(ERROR_FILE) + errors, ERROR_COLUMNS)
    print("\n" + "=" * 120)
    print("RESUME V5.2")
    print("=" * 120)
    print(f"Statut Gemini               : {status}")
    print(f"Nouveaux evenements classes : {len(new_rows)}")
    print(f"Total historique classe     : {len(combined_rows)}")
    print(f"Erreurs de cette execution  : {len(errors)}")
    print(f"Prefiltre                   : {PREFILTER_FILE}")
    print(f"Rapport                     : {OUTPUT_FILE}")
    print(f"Resume                      : {SUMMARY_FILE}")
    print(f"Erreurs                     : {ERROR_FILE}")
    print("\nSelection locale envoyee a Gemini :")
    if not selected:
        print("- Aucun evenement suffisamment prioritaire")
    else:
        for row in selected:
            title = clean(row.get("RepresentativeTitle"))[:88]
            print(f"- {clean(row.get('Ticker')):<8} rel={float(row.get('RelevanceScore', 0)):>4.1f} cat={float(row.get('CatalystScore', 0)):>4.1f} pri={float(row.get('PriorityScore', 0)):>5.1f} {clean(row.get('CatalystType')):<12} | {title}")
    print("\nEvenements HIGH / CRITICAL pertinents :")
    top = [row for row in combined_rows if as_bool(row.get("IsRelevant")) and clean(row.get("Importance")) in {"HIGH", "CRITICAL"}][:8]
    if not top:
        print("- Aucun")
    else:
        for row in top:
            title = clean(row.get("RepresentativeTitle"))[:88]
            print(f"- {clean(row.get('Ticker')):<8} {clean(row.get('Importance')):<8} {clean(row.get('Direction')):<14} conf={to_int(row.get('Confidence')):>3} | {title}")
    print("=" * 120)
    print("FIN CATALYST SEMANTIC V5.2")
    print("=" * 120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
