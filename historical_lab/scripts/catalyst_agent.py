from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ============================================================
# CATALYST AGENT V3
# - Collecte Finnhub V1 conservee
# - Deduplication exacte
# - Clustering local des articles similaires en evenements
# - Sorties RAW + EVENTS
# - Ajout SEC EDGAR comme source primaire US
# - Aucun LLM et aucun impact sur les trades
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

RAW_OUTPUT_FILE = REPORTS_DIR / "catalyst_raw.csv"
EVENTS_OUTPUT_FILE = REPORTS_DIR / "catalyst_events.csv"
SEC_OUTPUT_FILE = REPORTS_DIR / "catalyst_sec_filings.csv"

SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "investment-assistant personal-research contact@example.com").strip()
SEC_FORMS = {"8-K", "10-Q", "10-K", "6-K", "20-F", "40-F"}
SEC_REQUEST_PAUSE_SECONDS = 0.15

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

LOOKBACK_HOURS = 24
MAX_NEWS_PER_TICKER = 20
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_PAUSE_SECONDS = 0.15

# Clustering V2. Ce sont des regles d'ingenierie, pas des seuils de trading.
CLUSTER_MAX_HOURS = 12
TITLE_SIMILARITY_THRESHOLD = 0.64
TOKEN_JACCARD_THRESHOLD = 0.42
MIN_SHARED_SIGNIFICANT_TOKENS = 3

FALLBACK_TICKERS = [
    "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA",
    "ASML.AS", "AIR.PA", "SAF.PA", "TTE", "SU", "SPCX", "STDN", "VLO",
]

EU_SUFFIXES = (".PA", ".AS", ".DE", ".L", ".MI", ".MC", ".BR", ".SW")

STOP_WORDS = {
    "a", "about", "after", "again", "against", "all", "also", "an", "and",
    "are", "as", "at", "be", "been", "before", "being", "by", "can", "could",
    "for", "from", "has", "have", "in", "into", "is", "it", "its", "may",
    "more", "new", "news", "of", "on", "or", "said", "says", "that", "the",
    "their", "this", "to", "up", "was", "will", "with", "would", "stock",
    "stocks", "shares", "market", "markets", "company", "companies", "why",
    "ce", "ces", "dans", "de", "des", "du", "en", "et", "est", "la", "le",
    "les", "pour", "sur", "un", "une", "avec", "aux", "au", "par", "plus",
}

RAW_COLUMNS = [
    "RetrievedAtUTC", "PublishedAtUTC", "Ticker", "Market", "Source",
    "SourceType", "ExternalID", "Publisher", "CategoryRaw", "Title",
    "Summary", "URL", "Related", "EventFingerprint", "ClusterEventID",
]

SEC_COLUMNS = [
    "RetrievedAtUTC", "FiledAt", "Ticker", "Market", "Source", "SourceType",
    "CIK", "CompanyName", "Form", "AccessionNumber", "PrimaryDocument",
    "Description", "FilingURL", "EventFingerprint",
]

EVENT_COLUMNS = [
    "EventID", "Ticker", "Market", "FirstSeenUTC", "LastSeenUTC",
    "ArticleCount", "PublisherCount", "Publishers", "Sources", "RepresentativeTitle",
    "RepresentativeSummary", "RepresentativeURL", "RepresentativeExternalID",
    "CategoryRaw", "Related", "ClusterKey", "MaxTitleSimilarity",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def strip_accents(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )


def normalize_title(value: str) -> str:
    text = strip_accents(clean_text(value).lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def significant_tokens(value: str) -> set[str]:
    tokens = normalize_title(value).split()
    return {
        token for token in tokens
        if len(token) >= 3 and token not in STOP_WORDS and not token.isdigit()
    }


def event_fingerprint(ticker: str, title: str, published_at: str) -> str:
    date_part = published_at[:10] if published_at else "unknown-date"
    payload = f"{ticker}|{date_part}|{normalize_title(title)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def infer_market(ticker: str) -> str:
    return "EUROPE" if ticker.upper().endswith(EU_SUFFIXES) else "US"


def is_finnhub_v2_symbol(ticker: str) -> bool:
    return not ticker.upper().endswith(EU_SUFFIXES)


def extract_tickers_from_csv(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return []
            columns = {name.lower().strip(): name for name in reader.fieldnames if name}
            ticker_column = columns.get("ticker") or columns.get("symbol")
            if not ticker_column:
                return []
            tickers: list[str] = []
            for row in reader:
                ticker = clean_text(row.get(ticker_column, "")).upper()
                if ticker and ticker not in tickers:
                    tickers.append(ticker)
            return tickers
    except Exception:
        return []


def discover_watchlist() -> tuple[list[str], str]:
    preferred_names = ["watchlist.csv", "scanner_watchlist.csv", "historical_indicators.csv"]
    paths: list[Path] = []
    for name in preferred_names:
        paths.extend(DATA_DIR.rglob(name))
    known = {path.resolve() for path in paths}
    for path in DATA_DIR.rglob("*.csv"):
        if path.resolve() not in known:
            paths.append(path)
            known.add(path.resolve())
    for path in paths:
        tickers = extract_tickers_from_csv(path)
        if tickers:
            return tickers, str(path)
    return list(FALLBACK_TICKERS), "FALLBACK_TICKERS"


def finnhub_get(path: str, params: dict[str, Any]) -> Any:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY absent de l'environnement.")
    url = f"{FINNHUB_BASE_URL}{path}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "investment-assistant-catalyst/2.0",
            "X-Finnhub-Token": FINNHUB_API_KEY,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Finnhub HTTP {error.code}: {body[:300]}") from error
    except URLError as error:
        raise RuntimeError(f"Erreur reseau Finnhub: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Reponse Finnhub non JSON.") from error



def sec_get_json(url: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": SEC_USER_AGENT,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SEC HTTP {error.code}: {body[:300]}") from error
    except URLError as error:
        raise RuntimeError(f"Erreur reseau SEC: {error.reason}") from error


def load_sec_ticker_map() -> dict[str, dict[str, str]]:
    payload = sec_get_json(SEC_TICKER_URL)
    mapping: dict[str, dict[str, str]] = {}
    if not isinstance(payload, dict):
        return mapping
    for item in payload.values():
        ticker = clean_text(item.get("ticker")).upper()
        cik = item.get("cik_str")
        if not ticker or cik is None:
            continue
        mapping[ticker] = {
            "cik": str(cik).zfill(10),
            "name": clean_text(item.get("title")),
        }
    return mapping


def sec_filing_url(cik: str, accession: str, primary_document: str) -> str:
    cik_plain = str(int(cik))
    accession_plain = accession.replace("-", "")
    if not primary_document:
        return ""
    return f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{accession_plain}/{primary_document}"


def fetch_sec_filings(
    ticker: str,
    cik: str,
    fallback_name: str,
    retrieved_at: datetime,
) -> list[dict[str, str]]:
    payload = sec_get_json(f"{SEC_SUBMISSIONS_BASE}/CIK{cik}.json")
    company_name = clean_text(payload.get("name")) or fallback_name
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_documents = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    cutoff_date = (retrieved_at - timedelta(hours=LOOKBACK_HOURS)).date()
    rows: list[dict[str, str]] = []
    count = min(len(forms), len(accessions), len(filing_dates))

    for index in range(count):
        form = clean_text(forms[index]).upper()
        if form not in SEC_FORMS:
            continue
        filed_at = clean_text(filing_dates[index])
        try:
            filed_date = datetime.strptime(filed_at, "%Y-%m-%d").date()
        except ValueError:
            continue
        # EDGAR recent submissions expose filingDate rather than publication time.
        # A one-day cushion avoids losing a relevant filing around UTC/session boundaries.
        if filed_date < cutoff_date - timedelta(days=1):
            continue
        accession = clean_text(accessions[index])
        primary_document = clean_text(primary_documents[index]) if index < len(primary_documents) else ""
        description = clean_text(descriptions[index]) if index < len(descriptions) else ""
        fingerprint_payload = f"SEC|{ticker}|{accession}|{form}"
        fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()[:24]
        rows.append({
            "RetrievedAtUTC": iso_utc(retrieved_at),
            "FiledAt": filed_at,
            "Ticker": ticker,
            "Market": "US",
            "Source": "SEC_EDGAR",
            "SourceType": "FILING",
            "CIK": cik,
            "CompanyName": company_name,
            "Form": form,
            "AccessionNumber": accession,
            "PrimaryDocument": primary_document,
            "Description": description,
            "FilingURL": sec_filing_url(cik, accession, primary_document),
            "EventFingerprint": fingerprint,
        })
    return rows


def deduplicate_sec_filings(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (clean_text(row.get("Ticker")).upper(), clean_text(row.get("AccessionNumber")))
        if key[1]:
            unique[key] = row
    result = list(unique.values())
    result.sort(key=lambda row: (clean_text(row.get("FiledAt")), clean_text(row.get("Ticker"))), reverse=True)
    return result

def fetch_company_news(ticker: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    payload = finnhub_get(
        "/company-news",
        {"symbol": ticker, "from": start_date, "to": end_date},
    )
    if not isinstance(payload, list):
        raise RuntimeError(f"Format company-news inattendu pour {ticker}: {type(payload).__name__}")
    return payload


def normalize_news(ticker: str, items: list[dict[str, Any]], retrieved_at: datetime) -> list[dict[str, str]]:
    threshold = retrieved_at - timedelta(hours=LOOKBACK_HOURS)
    rows: list[dict[str, str]] = []
    for item in items:
        try:
            published = datetime.fromtimestamp(float(item.get("datetime")), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        if published < threshold:
            continue
        title = clean_text(item.get("headline"))
        if not title:
            continue
        published_at = iso_utc(published)
        rows.append({
            "RetrievedAtUTC": iso_utc(retrieved_at),
            "PublishedAtUTC": published_at,
            "Ticker": ticker,
            "Market": infer_market(ticker),
            "Source": "FINNHUB",
            "SourceType": "COMPANY_NEWS",
            "ExternalID": clean_text(item.get("id")),
            "Publisher": clean_text(item.get("source")),
            "CategoryRaw": clean_text(item.get("category")),
            "Title": title,
            "Summary": clean_text(item.get("summary")),
            "URL": clean_text(item.get("url")),
            "Related": clean_text(item.get("related")),
            "EventFingerprint": event_fingerprint(ticker, title, published_at),
            "ClusterEventID": "",
        })
    rows.sort(key=lambda row: row["PublishedAtUTC"], reverse=True)
    return rows[:MAX_NEWS_PER_TICKER]


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception as error:
        print(f"ATTENTION: lecture impossible {path.name}: {error}")
        return []


def deduplicate_articles(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        ticker = clean_text(row.get("Ticker")).upper()
        source = clean_text(row.get("Source"))
        external_id = clean_text(row.get("ExternalID"))
        fingerprint = clean_text(row.get("EventFingerprint"))
        identifier = external_id or fingerprint or normalize_title(clean_text(row.get("Title")))
        key = (ticker, source, identifier)
        previous = unique.get(key)
        if previous is None or clean_text(row.get("PublishedAtUTC")) > clean_text(previous.get("PublishedAtUTC")):
            unique[key] = row
    result = list(unique.values())
    result.sort(key=lambda row: (clean_text(row.get("PublishedAtUTC")), clean_text(row.get("Ticker"))), reverse=True)
    return result


def jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def title_similarity(title_a: str, title_b: str) -> tuple[float, float, int]:
    norm_a = normalize_title(title_a)
    norm_b = normalize_title(title_b)
    sequence = SequenceMatcher(None, norm_a, norm_b).ratio() if norm_a and norm_b else 0.0
    tokens_a = significant_tokens(title_a)
    tokens_b = significant_tokens(title_b)
    shared = len(tokens_a & tokens_b)
    return sequence, jaccard(tokens_a, tokens_b), shared


def same_event(article: dict[str, str], representative: dict[str, str]) -> tuple[bool, float]:
    dt_a = parse_iso(article.get("PublishedAtUTC", ""))
    dt_b = parse_iso(representative.get("PublishedAtUTC", ""))
    if dt_a is None or dt_b is None:
        return False, 0.0
    hours = abs((dt_a - dt_b).total_seconds()) / 3600.0
    if hours > CLUSTER_MAX_HOURS:
        return False, 0.0
    seq, jac, shared = title_similarity(article.get("Title", ""), representative.get("Title", ""))
    match = (
        seq >= TITLE_SIMILARITY_THRESHOLD
        or (jac >= TOKEN_JACCARD_THRESHOLD and shared >= MIN_SHARED_SIGNIFICANT_TOKENS)
    )
    score = max(seq, jac)
    return match, score


def make_event_id(ticker: str, first_seen: str, representative_title: str) -> str:
    day = first_seen[:10].replace("-", "") if first_seen else "UNKNOWN"
    digest = hashlib.sha1(
        f"{ticker}|{day}|{normalize_title(representative_title)}".encode("utf-8")
    ).hexdigest()[:10].upper()
    return f"EVT_{ticker.replace('.', '_')}_{day}_{digest}"


def cluster_articles(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    by_ticker: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_ticker[clean_text(row.get("Ticker")).upper()].append(row)

    event_rows: list[dict[str, Any]] = []

    for ticker, ticker_rows in by_ticker.items():
        ticker_rows.sort(key=lambda row: clean_text(row.get("PublishedAtUTC")))
        clusters: list[dict[str, Any]] = []

        for article in ticker_rows:
            best_index = None
            best_score = -1.0
            for index, cluster in enumerate(clusters):
                match, score = same_event(article, cluster["representative"])
                if match and score > best_score:
                    best_index = index
                    best_score = score

            if best_index is None:
                clusters.append({
                    "representative": article,
                    "articles": [article],
                    "max_similarity": 1.0,
                })
            else:
                clusters[best_index]["articles"].append(article)
                clusters[best_index]["max_similarity"] = max(
                    float(clusters[best_index]["max_similarity"]), best_score
                )

        for cluster in clusters:
            articles = cluster["articles"]
            articles.sort(key=lambda row: clean_text(row.get("PublishedAtUTC")))
            representative = max(
                articles,
                key=lambda row: (
                    len(clean_text(row.get("Summary"))),
                    len(clean_text(row.get("Title"))),
                ),
            )
            first_seen = min(clean_text(row.get("PublishedAtUTC")) for row in articles)
            last_seen = max(clean_text(row.get("PublishedAtUTC")) for row in articles)
            event_id = make_event_id(ticker, first_seen, representative.get("Title", ""))

            publishers = sorted({clean_text(row.get("Publisher")) for row in articles if clean_text(row.get("Publisher"))})
            sources = sorted({clean_text(row.get("Source")) for row in articles if clean_text(row.get("Source"))})

            for article in articles:
                article["ClusterEventID"] = event_id

            tokens = significant_tokens(representative.get("Title", ""))
            cluster_key = " ".join(sorted(tokens)[:10])

            event_rows.append({
                "EventID": event_id,
                "Ticker": ticker,
                "Market": clean_text(representative.get("Market")),
                "FirstSeenUTC": first_seen,
                "LastSeenUTC": last_seen,
                "ArticleCount": len(articles),
                "PublisherCount": len(publishers),
                "Publishers": " | ".join(publishers),
                "Sources": " | ".join(sources),
                "RepresentativeTitle": clean_text(representative.get("Title")),
                "RepresentativeSummary": clean_text(representative.get("Summary")),
                "RepresentativeURL": clean_text(representative.get("URL")),
                "RepresentativeExternalID": clean_text(representative.get("ExternalID")),
                "CategoryRaw": clean_text(representative.get("CategoryRaw")),
                "Related": clean_text(representative.get("Related")),
                "ClusterKey": cluster_key,
                "MaxTitleSimilarity": round(float(cluster["max_similarity"]), 4),
            })

    event_rows.sort(key=lambda row: (clean_text(row.get("LastSeenUTC")), clean_text(row.get("Ticker"))), reverse=True)
    rows.sort(key=lambda row: (clean_text(row.get("PublishedAtUTC")), clean_text(row.get("Ticker"))), reverse=True)
    return rows, event_rows


def save_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_event_stats(events: list[dict[str, Any]]) -> None:
    if not events:
        print("Aucun evenement clusterise.")
        return
    multi = [event for event in events if int(event["ArticleCount"]) > 1]
    max_articles = max(int(event["ArticleCount"]) for event in events)
    average_articles = sum(int(event["ArticleCount"]) for event in events) / len(events)
    print(f"Evenements uniques               : {len(events)}")
    print(f"Evenements multi-articles        : {len(multi)}")
    print(f"Articles moyens / evenement      : {average_articles:.2f}")
    print(f"Plus gros cluster                : {max_articles} articles")

    print()
    print("Top clusters multi-articles :")
    top = sorted(multi, key=lambda event: int(event["ArticleCount"]), reverse=True)[:10]
    if not top:
        print("- Aucun cluster multi-article detecte")
    else:
        for event in top:
            title = clean_text(event["RepresentativeTitle"])
            if len(title) > 100:
                title = title[:97] + "..."
            print(f"- {event['Ticker']:<8} {int(event['ArticleCount']):>2} articles | {title}")


def main() -> int:
    print("=" * 120)
    print("CATALYST AGENT V3 - FINNHUB + CLUSTERING + SEC EDGAR")
    print("MODE INFORMATIF UNIQUEMENT - AUCUN IMPACT SUR LES TRADES")
    print("=" * 120)

    if not FINNHUB_API_KEY:
        print("ERREUR: FINNHUB_API_KEY n'est pas disponible dans l'environnement.")
        print("Passe le secret au step GitHub Actions avec env: FINNHUB_API_KEY: ${{ secrets.FINNHUB_API_KEY }}")
        return 1

    tickers, watchlist_source = discover_watchlist()
    print(f"Watchlist source : {watchlist_source}")
    print(f"Tickers detectes : {len(tickers)}")

    finnhub_tickers = [ticker for ticker in tickers if is_finnhub_v2_symbol(ticker)]
    deferred = [ticker for ticker in tickers if ticker not in finnhub_tickers]
    print(f"Tickers Finnhub V2 : {len(finnhub_tickers)}")
    print(f"Tickers Europe reportes : {len(deferred)}")
    if deferred:
        print("Europe :", ", ".join(deferred))

    now = utc_now()
    start_date = (now - timedelta(hours=LOOKBACK_HOURS)).date().isoformat()
    end_date = now.date().isoformat()

    new_rows: list[dict[str, str]] = []
    errors: list[tuple[str, str]] = []

    for ticker in finnhub_tickers:
        try:
            raw_news = fetch_company_news(ticker, start_date, end_date)
            normalized = normalize_news(ticker, raw_news, now)
            new_rows.extend(normalized)
            print(f"{ticker:<12} {len(normalized):>3} news recentes")
        except Exception as error:
            errors.append((ticker, str(error)))
            print(f"{ticker:<12} ERREUR: {error}")
        time.sleep(REQUEST_PAUSE_SECONDS)

    # ------------------------------------------------------------
    # SEC EDGAR : source primaire pour les tickers US reconnus.
    # Une panne SEC reste non bloquante.
    # ------------------------------------------------------------
    sec_rows_new: list[dict[str, str]] = []
    sec_errors: list[tuple[str, str]] = []
    sec_map: dict[str, dict[str, str]] = {}

    try:
        sec_map = load_sec_ticker_map()
        print()
        print(f"Mapping SEC charge : {len(sec_map)} symboles")
    except Exception as error:
        print(f"SEC mapping ERREUR non bloquante : {error}")

    for ticker in finnhub_tickers:
        company = sec_map.get(ticker)
        if not company:
            continue
        try:
            filings = fetch_sec_filings(
                ticker,
                company["cik"],
                company["name"],
                now,
            )
            sec_rows_new.extend(filings)
            print(f"SEC {ticker:<8} {len(filings):>3} filings recents")
        except Exception as error:
            sec_errors.append((ticker, str(error)))
            print(f"SEC {ticker:<8} ERREUR: {error}")
        time.sleep(SEC_REQUEST_PAUSE_SECONDS)

    existing_sec = load_csv_rows(SEC_OUTPUT_FILE)
    all_sec_filings = deduplicate_sec_filings(existing_sec + sec_rows_new)
    save_csv(SEC_OUTPUT_FILE, all_sec_filings, SEC_COLUMNS)

    existing_rows = load_csv_rows(RAW_OUTPUT_FILE)
    all_articles = deduplicate_articles(existing_rows + new_rows)

    # Conserver l'historique RAW, mais ne clusteriser que la fenetre utile V2.
    cluster_cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    recent_articles = []
    for row in all_articles:
        published = parse_iso(row.get("PublishedAtUTC", ""))
        if published is not None and published >= cluster_cutoff:
            recent_articles.append(row)

    recent_articles, events = cluster_articles(recent_articles)
    event_map = {
        clean_text(row.get("EventFingerprint")): clean_text(row.get("ClusterEventID"))
        for row in recent_articles
        if clean_text(row.get("EventFingerprint"))
    }
    for row in all_articles:
        fingerprint = clean_text(row.get("EventFingerprint"))
        if fingerprint in event_map:
            row["ClusterEventID"] = event_map[fingerprint]
        elif "ClusterEventID" not in row:
            row["ClusterEventID"] = ""

    save_csv(RAW_OUTPUT_FILE, all_articles, RAW_COLUMNS)
    save_csv(EVENTS_OUTPUT_FILE, events, EVENT_COLUMNS)

    print()
    print("=" * 120)
    print("RESUME V3")
    print("=" * 120)
    print(f"News collectees cette execution : {len(new_rows)}")
    print(f"Articles RAW uniques conserves   : {len(all_articles)}")
    print(f"Articles clusterises (24 h)      : {len(recent_articles)}")
    print_event_stats(events)
    print(f"Filings SEC cette execution      : {len(sec_rows_new)}")
    print(f"Filings SEC uniques conserves    : {len(all_sec_filings)}")
    print(f"Erreurs Finnhub                  : {len(errors)}")
    print(f"Erreurs SEC                      : {len(sec_errors)}")
    print(f"RAW                              : {RAW_OUTPUT_FILE}")
    print(f"SEC                              : {SEC_OUTPUT_FILE}")
    print(f"EVENTS                           : {EVENTS_OUTPUT_FILE}")

    if errors or sec_errors:
        print()
        print("Erreurs non bloquantes :")
        for ticker, message in errors:
            print(f"- Finnhub {ticker}: {message}")
        for ticker, message in sec_errors:
            print(f"- SEC {ticker}: {message}")

    print("=" * 120)
    print("FIN CATALYST AGENT V3")
    print("=" * 120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
