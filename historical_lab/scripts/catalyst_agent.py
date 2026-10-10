from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ============================================================
# HISTORICAL LAB - CATALYST AGENT V1
# Collecte Finnhub uniquement, sans impact sur les trades.
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = REPORTS_DIR / "catalyst_raw.csv"

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

# Fenetre volontairement courte pour cette V1 de collecte.
LOOKBACK_HOURS = 24
MAX_NEWS_PER_TICKER = 20
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_PAUSE_SECONDS = 0.15

# Watchlist de secours uniquement si aucun fichier de tickers exploitable
# n'est trouve dans historical_lab/data.
FALLBACK_TICKERS = [
    "AAPL",
    "AMZN",
    "GOOGL",
    "META",
    "MSFT",
    "NVDA",
    "TSLA",
    "ASML.AS",
    "AIR.PA",
    "SAF.PA",
    "TTE",
    "SU",
    "SPXC",
]

# V1 Finnhub : on commence avec les symboles US simples.
# Les symboles Europe seront conserves dans la watchlist mais marques
# comme non interroges jusqu'a la couche de mapping Europe.
EU_SUFFIXES = (".PA", ".AS", ".DE", ".L", ".MI", ".MC", ".BR", ".SW")

OUTPUT_COLUMNS = [
    "RetrievedAtUTC",
    "PublishedAtUTC",
    "Ticker",
    "Market",
    "Source",
    "SourceType",
    "ExternalID",
    "Publisher",
    "CategoryRaw",
    "Title",
    "Summary",
    "URL",
    "Related",
    "EventFingerprint",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def normalize_title(value: str) -> str:
    text = clean_text(value).lower()
    chars = []
    previous_space = False
    for char in text:
        if char.isalnum():
            chars.append(char)
            previous_space = False
        elif not previous_space:
            chars.append(" ")
            previous_space = True
    return " ".join("".join(chars).split())


def event_fingerprint(ticker: str, title: str, published_at: str) -> str:
    # V1: empreinte deterministe article/titre. Une vraie fusion semantique
    # multi-source sera ajoutee lors de l'etape deduplication avancee.
    date_part = published_at[:10] if published_at else "unknown-date"
    payload = f"{ticker}|{date_part}|{normalize_title(title)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def infer_market(ticker: str) -> str:
    if ticker.upper().endswith(EU_SUFFIXES):
        return "EUROPE"
    return "US"


def is_finnhub_v1_symbol(ticker: str) -> bool:
    # Pour ne pas inventer maintenant le mapping des places europeennes,
    # la V1 Finnhub interroge uniquement les tickers US sans suffixe Yahoo.
    return not ticker.upper().endswith(EU_SUFFIXES)


def extract_tickers_from_csv(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return []

            candidates = {
                name.lower().strip(): name
                for name in reader.fieldnames
                if name
            }

            ticker_column = None
            for key in ("ticker", "symbol"):
                if key in candidates:
                    ticker_column = candidates[key]
                    break

            if ticker_column is None:
                return []

            tickers = []
            for row in reader:
                ticker = clean_text(row.get(ticker_column, "")).upper()
                if ticker and ticker not in tickers:
                    tickers.append(ticker)
            return tickers
    except Exception:
        return []


def discover_watchlist() -> tuple[list[str], str]:
    # On reutilise d'abord les donnees existantes du projet.
    preferred_names = [
        "watchlist.csv",
        "scanner_watchlist.csv",
        "historical_indicators.csv",
    ]

    csv_paths = []
    for name in preferred_names:
        csv_paths.extend(DATA_DIR.rglob(name))

    # Puis n'importe quel CSV de data contenant une colonne Ticker/Symbol.
    seen = {path.resolve() for path in csv_paths}
    for path in DATA_DIR.rglob("*.csv"):
        if path.resolve() not in seen:
            csv_paths.append(path)
            seen.add(path.resolve())

    for path in csv_paths:
        tickers = extract_tickers_from_csv(path)
        if tickers:
            return tickers, str(path)

    return list(FALLBACK_TICKERS), "FALLBACK_TICKERS"


def finnhub_get(path: str, params: dict[str, Any]) -> Any:
    if not FINNHUB_API_KEY:
        raise RuntimeError(
            "FINNHUB_API_KEY absent de l'environnement. "
            "Ajoute le secret GitHub au workflow via env."
        )

    query = dict(params)
    url = f"{FINNHUB_BASE_URL}{path}?{urlencode(query)}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "investment-assistant-catalyst/1.0",
            "X-Finnhub-Token": FINNHUB_API_KEY,
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Finnhub HTTP {error.code}: {body[:300]}"
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Erreur reseau Finnhub: {error.reason}"
        ) from error
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Reponse Finnhub non JSON."
        ) from error


def fetch_company_news(ticker: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    payload = finnhub_get(
        "/company-news",
        {
            "symbol": ticker,
            "from": start_date,
            "to": end_date,
        },
    )

    if not isinstance(payload, list):
        raise RuntimeError(
            f"Format company-news inattendu pour {ticker}: {type(payload).__name__}"
        )

    return payload


def normalize_news(ticker: str, items: list[dict[str, Any]], retrieved_at: datetime) -> list[dict[str, str]]:
    threshold = retrieved_at - timedelta(hours=LOOKBACK_HOURS)
    rows = []

    for item in items:
        unix_time = item.get("datetime")
        try:
            published = datetime.fromtimestamp(float(unix_time), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue

        if published < threshold:
            continue

        title = clean_text(item.get("headline"))
        if not title:
            continue

        published_at = iso_utc(published)
        row = {
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
        }
        rows.append(row)

    rows.sort(key=lambda row: row["PublishedAtUTC"], reverse=True)
    return rows[:MAX_NEWS_PER_TICKER]


def load_existing_rows() -> list[dict[str, str]]:
    if not OUTPUT_FILE.exists():
        return []

    try:
        with OUTPUT_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception as error:
        print(f"ATTENTION: lecture historique impossible: {error}")
        return []


def deduplicate_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    # ExternalID Finnhub prioritaire si present, sinon empreinte locale.
    unique: dict[tuple[str, str, str], dict[str, str]] = {}

    for row in rows:
        ticker = clean_text(row.get("Ticker")).upper()
        external_id = clean_text(row.get("ExternalID"))
        fingerprint = clean_text(row.get("EventFingerprint"))
        identifier = external_id or fingerprint
        key = (ticker, clean_text(row.get("Source")), identifier)

        if not identifier:
            key = (
                ticker,
                clean_text(row.get("Source")),
                normalize_title(clean_text(row.get("Title"))),
            )

        existing = unique.get(key)
        if existing is None:
            unique[key] = row
            continue

        if clean_text(row.get("PublishedAtUTC")) > clean_text(existing.get("PublishedAtUTC")):
            unique[key] = row

    result = list(unique.values())
    result.sort(
        key=lambda row: (
            clean_text(row.get("PublishedAtUTC")),
            clean_text(row.get("Ticker")),
        ),
        reverse=True,
    )
    return result


def save_rows(rows: list[dict[str, str]]) -> None:
    with OUTPUT_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=OUTPUT_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    print("=" * 120)
    print("CATALYST AGENT V1 - COLLECTE FINNHUB")
    print("MODE INFORMATIF UNIQUEMENT - AUCUN IMPACT SUR LES TRADES")
    print("=" * 120)

    if not FINNHUB_API_KEY:
        print("ERREUR: FINNHUB_API_KEY n'est pas disponible dans l'environnement.")
        print("Le secret GitHub doit etre passe a l'etape via env: FINNHUB_API_KEY: ${{ secrets.FINNHUB_API_KEY }}")
        return 1

    tickers, watchlist_source = discover_watchlist()
    print(f"Watchlist source : {watchlist_source}")
    print(f"Tickers detectes : {len(tickers)}")

    us_tickers = [ticker for ticker in tickers if is_finnhub_v1_symbol(ticker)]
    deferred_tickers = [ticker for ticker in tickers if ticker not in us_tickers]

    print(f"Tickers Finnhub V1 : {len(us_tickers)}")
    print(f"Tickers Europe reportes a la prochaine couche : {len(deferred_tickers)}")
    if deferred_tickers:
        print("Europe :", ", ".join(deferred_tickers))

    now = utc_now()
    start_date = (now - timedelta(hours=LOOKBACK_HOURS)).date().isoformat()
    end_date = now.date().isoformat()

    new_rows: list[dict[str, str]] = []
    errors = []

    for ticker in us_tickers:
        try:
            raw_news = fetch_company_news(ticker, start_date, end_date)
            normalized = normalize_news(ticker, raw_news, now)
            new_rows.extend(normalized)
            print(f"{ticker:<12} {len(normalized):>3} news recentes")
        except Exception as error:
            errors.append((ticker, str(error)))
            print(f"{ticker:<12} ERREUR: {error}")

        time.sleep(REQUEST_PAUSE_SECONDS)

    existing_rows = load_existing_rows()
    combined = deduplicate_rows(existing_rows + new_rows)
    save_rows(combined)

    print()
    print("=" * 120)
    print("RESUME")
    print("=" * 120)
    print(f"News collectees cette execution : {len(new_rows)}")
    print(f"News uniques conservees         : {len(combined)}")
    print(f"Erreurs ticker                   : {len(errors)}")
    print(f"Rapport                          : {OUTPUT_FILE}")

    if errors:
        print()
        print("Erreurs non bloquantes :")
        for ticker, message in errors:
            print(f"- {ticker}: {message}")

    print("=" * 120)
    print("FIN CATALYST AGENT V1")
    print("=" * 120)

    # Une panne ponctuelle sur un ticker ne doit pas casser le pipeline.
    # L'absence totale de collecte n'est pas consideree comme une erreur ici.
    return 0


if __name__ == "__main__":
    sys.exit(main())
