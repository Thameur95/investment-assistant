from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ============================================================
# CATALYST AGENT V4.1
# US: Finnhub + SEC EDGAR
# Europe renforcee: alias Yahoo + diagnostic 7 jours + sources officielles Airbus/ASML/Safran
# Clustering local, sans LLM et sans impact sur les trades
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

RAW_FILE = REPORTS_DIR / "catalyst_raw.csv"
EVENTS_FILE = REPORTS_DIR / "catalyst_events.csv"
SEC_FILE = REPORTS_DIR / "catalyst_sec_filings.csv"

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "investment-assistant personal-research contact@example.com",
).strip()
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
EUROPE_DIAGNOSTIC_FILE = REPORTS_DIR / "catalyst_europe_diagnostic.csv"
EUROPE_DIAGNOSTIC_DAYS = 7
EUROPE_QUERIES = {
    "AIR.PA": ["AIR.PA", "Airbus", "Airbus SE"],
    "ASML.AS": ["ASML.AS", "ASML", "ASML Holding"],
    "SAF.PA": ["SAF.PA", "Safran", "Safran SA"],
}
OFFICIAL_EU_PAGES = {
    "ASML.AS": "https://www.investor.asml.com/news/press-releases-and-announcements",
    "SAF.PA": "https://www.safran-group.com/pressroom",
}

LOOKBACK_HOURS = 24
MAX_NEWS_PER_TICKER = 20
REQUEST_TIMEOUT = 20
REQUEST_PAUSE = 0.15
SEC_FORMS = {"8-K", "10-Q", "10-K", "6-K", "20-F", "40-F"}
EU_SUFFIXES = (".PA", ".AS", ".DE", ".L", ".MI", ".MC", ".BR", ".SW")
EUROPE_TICKERS = {"AIR.PA", "ASML.AS", "SAF.PA"}
OFFICIAL_EU_RSS = {
    "AIR.PA": "https://www.airbus.com/en/rss-press-releases-feeds",
}
CLUSTER_MAX_HOURS = 12
TITLE_SIMILARITY = 0.64
TOKEN_JACCARD = 0.42
MIN_SHARED_TOKENS = 3

FALLBACK_TICKERS = [
    "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA",
    "SPCX", "STDN", "SU", "TTE", "VLO", "AIR.PA", "ASML.AS", "SAF.PA",
]

STOP_WORDS = {
    "a", "about", "after", "all", "also", "an", "and", "are", "as", "at",
    "be", "been", "before", "by", "for", "from", "has", "have", "in", "is",
    "it", "its", "may", "more", "new", "news", "of", "on", "or", "said",
    "says", "that", "the", "their", "this", "to", "up", "was", "will",
    "with", "stock", "stocks", "shares", "market", "markets", "company", "why",
    "au", "aux", "avec", "ce", "ces", "dans", "de", "des", "du", "en", "et",
    "est", "la", "le", "les", "par", "plus", "pour", "sur", "un", "une",
}

RAW_COLUMNS = [
    "RetrievedAtUTC", "PublishedAtUTC", "Ticker", "Market", "Source",
    "SourceType", "ExternalID", "Publisher", "CategoryRaw", "Title",
    "Summary", "URL", "Related", "EventFingerprint", "ClusterEventID",
]
EVENT_COLUMNS = [
    "EventID", "Ticker", "Market", "FirstSeenUTC", "LastSeenUTC",
    "ArticleCount", "PublisherCount", "Publishers", "Sources",
    "RepresentativeTitle", "RepresentativeSummary", "RepresentativeURL",
    "RepresentativeExternalID", "CategoryRaw", "Related", "ClusterKey",
    "MaxTitleSimilarity",
]
EUROPE_DIAGNOSTIC_COLUMNS = [
    "RetrievedAtUTC", "Ticker", "Source", "Query", "WindowHours",
    "RawItems", "RecentItems", "Status", "Message",
]
SEC_COLUMNS = [
    "RetrievedAtUTC", "FiledAt", "Ticker", "Market", "Source", "SourceType",
    "CIK", "CompanyName", "Form", "AccessionNumber", "PrimaryDocument",
    "Description", "FilingURL", "EventFingerprint",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(clean(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_title(value: str) -> str:
    text = "".join(
        c for c in unicodedata.normalize("NFKD", clean(value).lower())
        if not unicodedata.combining(c)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def tokens(value: str) -> set[str]:
    return {
        token for token in normalize_title(value).split()
        if len(token) >= 3 and token not in STOP_WORDS and not token.isdigit()
    }


def fingerprint(ticker: str, title: str, published: str) -> str:
    payload = f"{ticker}|{published[:10]}|{normalize_title(title)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def market(ticker: str) -> str:
    return "EUROPE" if ticker.endswith(EU_SUFFIXES) else "US"


def get_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    final_headers = {
        "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*",
        "User-Agent": "investment-assistant-catalyst/4.0",
    }
    if headers:
        final_headers.update(headers)
    request = Request(url, headers=final_headers, method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body[:300]}") from error
    except URLError as error:
        raise RuntimeError(f"Erreur reseau: {error.reason}") from error


def get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    return json.loads(get_bytes(url, headers).decode("utf-8"))


def extract_tickers(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = {x.lower().strip(): x for x in (reader.fieldnames or [])}
            column = fields.get("ticker") or fields.get("symbol")
            if not column:
                return []
            result = []
            for row in reader:
                ticker = clean(row.get(column)).upper()
                if ticker and ticker not in result:
                    result.append(ticker)
            return result
    except Exception:
        return []


def discover_watchlist() -> tuple[list[str], str]:
    preferred = ["watchlist.csv", "scanner_watchlist.csv", "historical_indicators.csv"]
    paths: list[Path] = []
    for name in preferred:
        paths.extend(DATA_DIR.rglob(name))
    seen = {p.resolve() for p in paths}
    for path in DATA_DIR.rglob("*.csv"):
        if path.resolve() not in seen:
            paths.append(path)
            seen.add(path.resolve())
    for path in paths:
        tickers = extract_tickers(path)
        if tickers:
            return tickers, str(path)
    return FALLBACK_TICKERS.copy(), "FALLBACK_TICKERS"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception as error:
        print(f"ATTENTION lecture {path.name}: {error}")
        return []


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def news_row(
    ticker: str,
    published: datetime,
    source: str,
    source_type: str,
    external_id: str,
    publisher: str,
    category: str,
    title: str,
    summary: str,
    url: str,
    retrieved: datetime,
) -> dict[str, str]:
    published_at = iso_utc(published)
    return {
        "RetrievedAtUTC": iso_utc(retrieved),
        "PublishedAtUTC": published_at,
        "Ticker": ticker,
        "Market": market(ticker),
        "Source": source,
        "SourceType": source_type,
        "ExternalID": clean(external_id),
        "Publisher": clean(publisher),
        "CategoryRaw": clean(category),
        "Title": clean(title),
        "Summary": clean(summary),
        "URL": clean(url),
        "Related": ticker,
        "EventFingerprint": fingerprint(ticker, title, published_at),
        "ClusterEventID": "",
    }


# ============================================================
# FINNHUB US
# ============================================================

def fetch_finnhub(ticker: str, retrieved: datetime) -> list[dict[str, str]]:
    params = {
        "symbol": ticker,
        "from": (retrieved - timedelta(hours=LOOKBACK_HOURS)).date().isoformat(),
        "to": retrieved.date().isoformat(),
    }
    url = f"{FINNHUB_BASE_URL}/company-news?{urlencode(params)}"
    payload = get_json(url, {"X-Finnhub-Token": FINNHUB_API_KEY})
    if not isinstance(payload, list):
        raise RuntimeError("Format Finnhub inattendu")
    threshold = retrieved - timedelta(hours=LOOKBACK_HOURS)
    rows = []
    for item in payload:
        try:
            published = datetime.fromtimestamp(float(item.get("datetime")), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        title = clean(item.get("headline"))
        if published < threshold or not title:
            continue
        rows.append(news_row(
            ticker, published, "FINNHUB", "COMPANY_NEWS", clean(item.get("id")),
            clean(item.get("source")), clean(item.get("category")), title,
            clean(item.get("summary")), clean(item.get("url")), retrieved,
        ))
    rows.sort(key=lambda x: x["PublishedAtUTC"], reverse=True)
    return rows[:MAX_NEWS_PER_TICKER]


# ============================================================
# EUROPE: YAHOO SEARCH + AIRBUS OFFICIEL
# ============================================================

def fetch_yahoo_query(
    ticker: str,
    query: str,
    retrieved: datetime,
    lookback_hours: int,
) -> tuple[list[dict[str, str]], int]:
    params = {"q": query, "quotesCount": 1, "newsCount": 20}
    payload = get_json(f"{YAHOO_SEARCH_URL}?{urlencode(params)}")
    raw_news = payload.get("news", []) if isinstance(payload, dict) else []
    threshold = retrieved - timedelta(hours=lookback_hours)
    rows = []
    for item in raw_news:
        try:
            published = datetime.fromtimestamp(
                float(item.get("providerPublishTime")),
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OSError):
            continue
        title = clean(item.get("title"))
        if published < threshold or not title:
            continue
        rows.append(news_row(
            ticker, published, "YAHOO_FINANCE", "COMPANY_NEWS",
            clean(item.get("uuid")), clean(item.get("publisher")),
            "EUROPE_NEWS", title, clean(item.get("summary")),
            clean(item.get("link")), retrieved,
        ))
    return rows[:MAX_NEWS_PER_TICKER], len(raw_news)


def fetch_yahoo_europe_aliases(
    ticker: str,
    retrieved: datetime,
    lookback_hours: int,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    combined: list[dict[str, str]] = []
    diagnostics: list[dict[str, Any]] = []
    for query in EUROPE_QUERIES.get(ticker, [ticker]):
        try:
            rows, raw_count = fetch_yahoo_query(
                ticker, query, retrieved, lookback_hours
            )
            combined.extend(rows)
            diagnostics.append({
                "RetrievedAtUTC": iso_utc(retrieved),
                "Ticker": ticker,
                "Source": "YAHOO_FINANCE",
                "Query": query,
                "WindowHours": lookback_hours,
                "RawItems": raw_count,
                "RecentItems": len(rows),
                "Status": "OK",
                "Message": "",
            })
        except Exception as error:
            diagnostics.append({
                "RetrievedAtUTC": iso_utc(retrieved),
                "Ticker": ticker,
                "Source": "YAHOO_FINANCE",
                "Query": query,
                "WindowHours": lookback_hours,
                "RawItems": 0,
                "RecentItems": 0,
                "Status": "ERROR",
                "Message": str(error),
            })
    return dedup_articles(combined), diagnostics


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child(parent: ET.Element, names: set[str]) -> ET.Element | None:
    return next((x for x in list(parent) if local_name(x.tag) in names), None)


def element_text(element: ET.Element | None) -> str:
    return "" if element is None else clean(" ".join(element.itertext()))


def feed_date(value: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(clean(value))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return parse_iso(value)


def fetch_official_rss(ticker: str, url: str, retrieved: datetime) -> list[dict[str, str]]:
    root = ET.fromstring(get_bytes(url))
    threshold = retrieved - timedelta(hours=LOOKBACK_HOURS)
    rows = []
    for item in [x for x in root.iter() if local_name(x.tag) in {"item", "entry"}]:
        title = element_text(child(item, {"title"}))
        published = feed_date(element_text(child(item, {"pubdate", "published", "updated", "date"})))
        if not title or published is None or published < threshold:
            continue
        link_el = child(item, {"link"})
        link = clean(link_el.attrib.get("href")) if link_el is not None else ""
        link = link or element_text(link_el)
        summary = element_text(child(item, {"description", "summary", "content"}))
        guid = element_text(child(item, {"guid", "id"})) or link
        rows.append(news_row(
            ticker, published, "AIRBUS_OFFICIAL_RSS", "PRESS_RELEASE", guid,
            "Airbus", "OFFICIAL_PRESS_RELEASE", title, summary, link, retrieved,
        ))
    return rows[:MAX_NEWS_PER_TICKER]



def iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child_value in value.values():
            yield from iter_json_objects(child_value)
    elif isinstance(value, list):
        for child_value in value:
            yield from iter_json_objects(child_value)


def fetch_official_html(
    ticker: str,
    url: str,
    retrieved: datetime,
    lookback_hours: int,
) -> tuple[list[dict[str, str]], int]:
    html = get_bytes(url).decode("utf-8", errors="replace")
    scripts = re.findall(
        r'<script[^>]+type=["\\\']application/ld\\+json["\\\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    threshold = retrieved - timedelta(hours=lookback_hours)
    candidates: list[dict[str, Any]] = []
    for script in scripts:
        try:
            payload = json.loads(script.strip())
        except json.JSONDecodeError:
            continue
        for obj in iter_json_objects(payload):
            obj_type = clean(obj.get("@type")).lower()
            if obj_type in {
                "newsarticle", "article", "pressrelease", "reportagearticle"
            } or obj.get("datePublished"):
                candidates.append(obj)

    rows: list[dict[str, str]] = []
    for item in candidates:
        title = clean(item.get("headline") or item.get("name"))
        published = feed_date(
            clean(item.get("datePublished") or item.get("dateModified"))
        )
        if not title or published is None or published < threshold:
            continue
        item_url = item.get("url") or item.get("mainEntityOfPage") or ""
        if isinstance(item_url, dict):
            item_url = item_url.get("@id") or item_url.get("url") or ""
        publisher = item.get("publisher") or ""
        if isinstance(publisher, dict):
            publisher = publisher.get("name") or ""
        rows.append(news_row(
            ticker, published,
            "ASML_OFFICIAL" if ticker == "ASML.AS" else "SAFRAN_OFFICIAL",
            "PRESS_RELEASE", clean(item_url), clean(publisher) or ticker,
            "OFFICIAL_PRESS_RELEASE", title,
            clean(item.get("description")), clean(item_url), retrieved,
        ))
    return dedup_articles(rows), len(candidates)


# ============================================================
# SEC EDGAR
# ============================================================

def sec_headers() -> dict[str, str]:
    return {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}


def load_sec_map() -> dict[str, dict[str, str]]:
    payload = get_json(SEC_TICKER_URL, sec_headers())
    result = {}
    for item in payload.values() if isinstance(payload, dict) else []:
        ticker = clean(item.get("ticker")).upper()
        if ticker and item.get("cik_str") is not None:
            result[ticker] = {
                "cik": str(item["cik_str"]).zfill(10),
                "name": clean(item.get("title")),
            }
    return result


def filing_url(cik: str, accession: str, document: str) -> str:
    if not document:
        return ""
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{document}"
    )


def fetch_sec(ticker: str, info: dict[str, str], retrieved: datetime) -> list[dict[str, str]]:
    cik = info["cik"]
    payload = get_json(f"{SEC_SUBMISSIONS_BASE}/CIK{cik}.json", sec_headers())
    company = clean(payload.get("name")) or info["name"]
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    cutoff = (retrieved - timedelta(hours=LOOKBACK_HOURS)).date() - timedelta(days=1)
    rows = []
    for index in range(min(len(forms), len(accessions), len(dates))):
        form = clean(forms[index]).upper()
        try:
            filed_date = datetime.strptime(clean(dates[index]), "%Y-%m-%d").date()
        except ValueError:
            continue
        if form not in SEC_FORMS or filed_date < cutoff:
            continue
        accession = clean(accessions[index])
        document = clean(docs[index]) if index < len(docs) else ""
        description = clean(descriptions[index]) if index < len(descriptions) else ""
        rows.append({
            "RetrievedAtUTC": iso_utc(retrieved), "FiledAt": filed_date.isoformat(),
            "Ticker": ticker, "Market": "US", "Source": "SEC_EDGAR",
            "SourceType": "FILING", "CIK": cik, "CompanyName": company,
            "Form": form, "AccessionNumber": accession, "PrimaryDocument": document,
            "Description": description, "FilingURL": filing_url(cik, accession, document),
            "EventFingerprint": hashlib.sha256(
                f"SEC|{ticker}|{accession}|{form}".encode()
            ).hexdigest()[:24],
        })
    return rows


def dedup_sec(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = {}
    for row in rows:
        key = (clean(row.get("Ticker")).upper(), clean(row.get("AccessionNumber")))
        if key[1]:
            unique[key] = row
    return sorted(unique.values(), key=lambda x: (clean(x.get("FiledAt")), clean(x.get("Ticker"))), reverse=True)


# ============================================================
# DEDUPLICATION ET CLUSTERING
# ============================================================

def dedup_articles(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = {}
    for row in rows:
        identifier = clean(row.get("ExternalID")) or clean(row.get("EventFingerprint"))
        key = (clean(row.get("Ticker")).upper(), clean(row.get("Source")), identifier)
        if identifier:
            unique[key] = row
    return sorted(unique.values(), key=lambda x: (clean(x.get("PublishedAtUTC")), clean(x.get("Ticker"))), reverse=True)


def similarities(a: str, b: str) -> tuple[float, float, int]:
    na, nb = normalize_title(a), normalize_title(b)
    seq = SequenceMatcher(None, na, nb).ratio() if na and nb else 0.0
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return seq, jac, len(ta & tb)


def same_event(article: dict[str, str], representative: dict[str, str]) -> tuple[bool, float]:
    a = parse_iso(article.get("PublishedAtUTC", ""))
    b = parse_iso(representative.get("PublishedAtUTC", ""))
    if not a or not b or abs((a - b).total_seconds()) > CLUSTER_MAX_HOURS * 3600:
        return False, 0.0
    seq, jac, shared = similarities(article.get("Title", ""), representative.get("Title", ""))
    return (
        seq >= TITLE_SIMILARITY or (jac >= TOKEN_JACCARD and shared >= MIN_SHARED_TOKENS),
        max(seq, jac),
    )


def cluster(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[clean(row.get("Ticker")).upper()].append(row)
    events = []
    for ticker, articles in grouped.items():
        articles.sort(key=lambda x: clean(x.get("PublishedAtUTC")))
        clusters = []
        for article in articles:
            best = None
            best_score = -1.0
            for idx, group in enumerate(clusters):
                match, score = same_event(article, group["representative"])
                if match and score > best_score:
                    best, best_score = idx, score
            if best is None:
                clusters.append({"representative": article, "articles": [article], "score": 1.0})
            else:
                clusters[best]["articles"].append(article)
                clusters[best]["score"] = max(clusters[best]["score"], best_score)
        for group in clusters:
            items = group["articles"]
            representative = max(items, key=lambda x: (len(clean(x.get("Summary"))), len(clean(x.get("Title")))))
            first = min(clean(x.get("PublishedAtUTC")) for x in items)
            last = max(clean(x.get("PublishedAtUTC")) for x in items)
            digest = hashlib.sha1(
                f"{ticker}|{first[:10]}|{normalize_title(representative['Title'])}".encode()
            ).hexdigest()[:10].upper()
            event_id = f"EVT_{ticker.replace('.', '_')}_{first[:10].replace('-', '')}_{digest}"
            publishers = sorted({clean(x.get("Publisher")) for x in items if clean(x.get("Publisher"))})
            sources = sorted({clean(x.get("Source")) for x in items if clean(x.get("Source"))})
            for item in items:
                item["ClusterEventID"] = event_id
            events.append({
                "EventID": event_id, "Ticker": ticker, "Market": market(ticker),
                "FirstSeenUTC": first, "LastSeenUTC": last, "ArticleCount": len(items),
                "PublisherCount": len(publishers), "Publishers": " | ".join(publishers),
                "Sources": " | ".join(sources),
                "RepresentativeTitle": clean(representative.get("Title")),
                "RepresentativeSummary": clean(representative.get("Summary")),
                "RepresentativeURL": clean(representative.get("URL")),
                "RepresentativeExternalID": clean(representative.get("ExternalID")),
                "CategoryRaw": clean(representative.get("CategoryRaw")),
                "Related": clean(representative.get("Related")),
                "ClusterKey": " ".join(sorted(tokens(representative.get("Title", "")))[:10]),
                "MaxTitleSimilarity": round(float(group["score"]), 4),
            })
    events.sort(key=lambda x: (x["LastSeenUTC"], x["Ticker"]), reverse=True)
    rows.sort(key=lambda x: (x["PublishedAtUTC"], x["Ticker"]), reverse=True)
    return rows, events


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    print("=" * 120)
    print("CATALYST AGENT V4.1 - EUROPE RENFORCEE + US + SEC EDGAR")
    print("MODE INFORMATIF UNIQUEMENT - AUCUN IMPACT SUR LES TRADES")
    print("=" * 120)

    if not FINNHUB_API_KEY:
        print("ERREUR: FINNHUB_API_KEY absent de l'environnement.")
        return 1

    watchlist, source = discover_watchlist()
    us_tickers = [x for x in watchlist if not x.endswith(EU_SUFFIXES)]
    eu_tickers = [x for x in watchlist if x.endswith(EU_SUFFIXES)]
    print(f"Watchlist source : {source}")
    print(f"Tickers detectes : {len(watchlist)}")
    print(f"Tickers US       : {len(us_tickers)}")
    print(f"Tickers Europe   : {len(eu_tickers)}")
    if eu_tickers:
        print("Europe :", ", ".join(eu_tickers))

    now = now_utc()
    new_articles = []
    finnhub_errors = []
    europe_errors = []

    for ticker in us_tickers:
        try:
            rows = fetch_finnhub(ticker, now)
            new_articles.extend(rows)
            print(f"US Finnhub {ticker:<8} {len(rows):>3} news recentes")
        except Exception as error:
            finnhub_errors.append((ticker, str(error)))
            print(f"US Finnhub {ticker:<8} ERREUR: {error}")
        time.sleep(REQUEST_PAUSE)

    europe_diagnostics = []
    for ticker in eu_tickers:
        if ticker not in EUROPE_TICKERS:
            continue

        # Fenetre operationnelle 24 h, fusion de plusieurs alias.
        rows_24h, diagnostics_24h = fetch_yahoo_europe_aliases(
            ticker, now, LOOKBACK_HOURS
        )
        new_articles.extend(rows_24h)
        europe_diagnostics.extend(diagnostics_24h)
        print(f"EU Yahoo aliases {ticker:<7} {len(rows_24h):>3} news 24 h")

        # Diagnostic 7 jours, NON injecte dans le moteur operationnel.
        rows_7d, diagnostics_7d = fetch_yahoo_europe_aliases(
            ticker, now, EUROPE_DIAGNOSTIC_DAYS * 24
        )
        europe_diagnostics.extend(diagnostics_7d)
        print(f"EU Yahoo diag 7j {ticker:<7} {len(rows_7d):>3} news")

        official_rss = OFFICIAL_EU_RSS.get(ticker)
        if official_rss:
            try:
                official_24h = fetch_official_rss(ticker, official_rss, now)
                new_articles.extend(official_24h)
                print(
                    f"EU RSS officiel {ticker:<7} "
                    f"{len(official_24h):>3} communiques 24 h"
                )
                europe_diagnostics.append({
                    "RetrievedAtUTC": iso_utc(now), "Ticker": ticker,
                    "Source": "AIRBUS_OFFICIAL_RSS", "Query": official_rss,
                    "WindowHours": LOOKBACK_HOURS, "RawItems": len(official_24h),
                    "RecentItems": len(official_24h), "Status": "OK", "Message": "",
                })
            except Exception as error:
                europe_errors.append((f"RSS {ticker}", str(error)))
                print(f"EU RSS officiel {ticker:<7} ERREUR: {error}")

        official_page = OFFICIAL_EU_PAGES.get(ticker)
        if official_page:
            try:
                official_24h, raw_official = fetch_official_html(
                    ticker, official_page, now, LOOKBACK_HOURS
                )
                new_articles.extend(official_24h)
                print(
                    f"EU HTML officiel {ticker:<7} "
                    f"{len(official_24h):>3} communiques 24 h"
                )
                official_7d, raw_7d = fetch_official_html(
                    ticker, official_page, now, EUROPE_DIAGNOSTIC_DAYS * 24
                )
                print(
                    f"EU HTML diag 7j {ticker:<7} "
                    f"{len(official_7d):>3} communiques"
                )
                europe_diagnostics.extend([
                    {
                        "RetrievedAtUTC": iso_utc(now), "Ticker": ticker,
                        "Source": "OFFICIAL_HTML", "Query": official_page,
                        "WindowHours": LOOKBACK_HOURS, "RawItems": raw_official,
                        "RecentItems": len(official_24h), "Status": "OK", "Message": "",
                    },
                    {
                        "RetrievedAtUTC": iso_utc(now), "Ticker": ticker,
                        "Source": "OFFICIAL_HTML", "Query": official_page,
                        "WindowHours": EUROPE_DIAGNOSTIC_DAYS * 24,
                        "RawItems": raw_7d, "RecentItems": len(official_7d),
                        "Status": "OK", "Message": "",
                    },
                ])
            except Exception as error:
                europe_errors.append((f"HTML {ticker}", str(error)))
                print(f"EU HTML officiel {ticker:<7} ERREUR: {error}")
                europe_diagnostics.append({
                    "RetrievedAtUTC": iso_utc(now), "Ticker": ticker,
                    "Source": "OFFICIAL_HTML", "Query": official_page,
                    "WindowHours": LOOKBACK_HOURS, "RawItems": 0,
                    "RecentItems": 0, "Status": "ERROR", "Message": str(error),
                })

        time.sleep(REQUEST_PAUSE)

    write_csv(
        EUROPE_DIAGNOSTIC_FILE,
        europe_diagnostics,
        EUROPE_DIAGNOSTIC_COLUMNS,
    )

    sec_rows = []
    sec_errors = []
    try:
        sec_map = load_sec_map()
        print(f"Mapping SEC charge : {len(sec_map)} symboles")
    except Exception as error:
        sec_map = {}
        sec_errors.append(("MAPPING", str(error)))
        print(f"SEC mapping ERREUR non bloquante : {error}")

    for ticker in us_tickers:
        if ticker not in sec_map:
            continue
        try:
            rows = fetch_sec(ticker, sec_map[ticker], now)
            sec_rows.extend(rows)
            print(f"SEC {ticker:<12} {len(rows):>3} filings recents")
        except Exception as error:
            sec_errors.append((ticker, str(error)))
            print(f"SEC {ticker:<12} ERREUR: {error}")
        time.sleep(REQUEST_PAUSE)

    all_sec = dedup_sec(read_csv(SEC_FILE) + sec_rows)
    write_csv(SEC_FILE, all_sec, SEC_COLUMNS)

    all_articles = dedup_articles(read_csv(RAW_FILE) + new_articles)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    recent = [row for row in all_articles if (parse_iso(row.get("PublishedAtUTC", "")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    recent, events = cluster(recent)
    event_ids = {row["EventFingerprint"]: row["ClusterEventID"] for row in recent}
    for row in all_articles:
        row["ClusterEventID"] = event_ids.get(clean(row.get("EventFingerprint")), clean(row.get("ClusterEventID")))

    write_csv(RAW_FILE, all_articles, RAW_COLUMNS)
    write_csv(EVENTS_FILE, events, EVENT_COLUMNS)

    multi = [x for x in events if int(x["ArticleCount"]) > 1]
    print()
    print("=" * 120)
    print("RESUME V4.1")
    print("=" * 120)
    print(f"Articles cette execution         : {len(new_articles)}")
    print(f"Articles Europe                  : {sum(1 for x in new_articles if x['Market'] == 'EUROPE')}")
    print(f"Articles RAW uniques conserves   : {len(all_articles)}")
    print(f"Articles clusterises (24 h)      : {len(recent)}")
    print(f"Evenements uniques               : {len(events)}")
    print(f"Evenements multi-articles        : {len(multi)}")
    print(f"Filings SEC cette execution      : {len(sec_rows)}")
    print(f"Filings SEC uniques conserves    : {len(all_sec)}")
    print(f"Erreurs Finnhub                  : {len(finnhub_errors)}")
    print(f"Erreurs Europe                   : {len(europe_errors)}")
    print(f"Erreurs SEC                      : {len(sec_errors)}")
    print(f"RAW                              : {RAW_FILE}")
    print(f"EVENTS                           : {EVENTS_FILE}")
    print(f"SEC                              : {SEC_FILE}")
    print(f"DIAGNOSTIC EUROPE                : {EUROPE_DIAGNOSTIC_FILE}")

    if multi:
        print()
        print("Top clusters multi-sources / multi-articles :")
        for event in sorted(multi, key=lambda x: int(x["ArticleCount"]), reverse=True)[:10]:
            print(f"- {event['Ticker']:<8} {event['ArticleCount']:>2} articles | {event['Sources']} | {event['RepresentativeTitle'][:100]}")

    if finnhub_errors or europe_errors or sec_errors:
        print()
        print("Erreurs non bloquantes :")
        for name, message in finnhub_errors:
            print(f"- Finnhub {name}: {message}")
        for name, message in europe_errors:
            print(f"- Europe {name}: {message}")
        for name, message in sec_errors:
            print(f"- SEC {name}: {message}")

    print("=" * 120)
    print("FIN CATALYST AGENT V4.1")
    print("=" * 120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
