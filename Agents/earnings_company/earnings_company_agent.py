#!/usr/bin/env python3
"""Earnings & Company Agent V1 - diagnostic SEC/XBRL, Shadow only."""
from __future__ import annotations

import csv, json, math, os, re, time
from datetime import date
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import (
    AGENT_VERSION, CATALYST_EVENTS_FILE, CATALYST_SEC_FILINGS_FILE,
    CATALYST_SEMANTIC_FILE, OBSERVATION_ONLY, PORTFOLIO_V1_CHANGED,
    STOCKS_FILE, V1_EXECUTION_CHANGED,
)

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "investment-assistant personal-research contact@example.com"
).strip()
SEC_COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
REQUEST_TIMEOUT = 25
REQUEST_PAUSE = 0.15
CONDITIONAL_FORMS = {"8-K", "6-K"}
EARNINGS_WORDS = re.compile(
    r"\b(earnings|results?|financial results?|quarterly results?|annual results?|"
    r"income|revenue|sales|guidance|outlook|chiffre d.?affaires|résultats?|resultats?)\b",
    re.I,
)
REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
    "SalesRevenueNet", "SalesRevenueGoodsNet",
)
DILUTED_EPS_CONCEPTS = (
    "EarningsPerShareDiluted", "IncomeLossFromContinuingOperationsPerDilutedShare",
)
BASIC_EPS_CONCEPTS = (
    "EarningsPerShareBasic", "IncomeLossFromContinuingOperationsPerBasicShare",
)


def clean(v: Any) -> str:
    return "" if v is None else " ".join(str(v).split()).strip()


def normalize(v: Any) -> str:
    return clean(v).upper().replace("_", "-")


def as_bool(v: Any) -> bool:
    return clean(v).upper() in {"TRUE", "1", "YES", "OUI"}


def finite_float(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_tickers() -> list[str]:
    if not STOCKS_FILE.exists():
        raise FileNotFoundError(f"Fichier stocks introuvable : {STOCKS_FILE}")
    return [x.strip().upper() for x in STOCKS_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]


def get_json(url: str) -> Any:
    req = Request(url, headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:250]}") from e
    except URLError as e:
        raise RuntimeError(f"Erreur reseau SEC: {e.reason}") from e


def load_event_map() -> dict[str, dict[str, str]]:
    return {clean(r.get("EventID")): r for r in read_csv(CATALYST_EVENTS_FILE) if clean(r.get("EventID"))}


def semantic_candidates(watchlist: set[str], events: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    out = []
    for s in read_csv(CATALYST_SEMANTIC_FILE):
        ticker, event_id = clean(s.get("Ticker")).upper(), clean(s.get("EventID"))
        if ticker not in watchlist or not event_id or not as_bool(s.get("IsRelevant")):
            continue
        ctype, category = normalize(s.get("CatalystType")), normalize(s.get("Category"))
        if ctype not in {"EARNINGS", "GUIDANCE"} and category not in {
            "EARNINGS", "GUIDANCE", "FINANCIAL-RESULTS", "FINANCIAL-UPDATE"
        }:
            continue
        e = events.get(event_id, {})
        out.append({"EventID": event_id, "Ticker": ticker,
                    "PublishedAtUTC": clean(s.get("FirstSeenUTC") or e.get("FirstSeenUTC"))})
    return out


def classify_sec_filing(r: dict[str, str]) -> tuple[bool, str, str]:
    form, desc = normalize(r.get("Form")), clean(r.get("Description"))
    if form == "10-Q": return True, "ACTUAL_QUARTERLY_FILING", "Formulaire trimestriel SEC"
    if form in {"10-K", "20-F", "40-F"}: return True, "ACTUAL_ANNUAL_FILING", "Formulaire annuel SEC"
    if form in CONDITIONAL_FORMS:
        if EARNINGS_WORDS.search(desc): return True, "ACTUAL_EARNINGS_RELEASE", "Description SEC explicite"
        return False, "SEC_REVIEW", "8-K/6-K sans preuve Earnings dans Description"
    return False, "SEC_EXCLUDED", f"Formulaire non gere : {form}"


def sec_candidates(watchlist: set[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    kept, review = [], []
    for f in read_csv(CATALYST_SEC_FILINGS_FILE):
        ticker, accn = clean(f.get("Ticker")).upper(), clean(f.get("AccessionNumber"))
        if ticker not in watchlist or not accn: continue
        keep, classification, reason = classify_sec_filing(f)
        row = {
            "EventID": f"SEC|{ticker}|{accn}", "Ticker": ticker,
            "PublishedAtUTC": clean(f.get("FiledAt")), "Classification": classification,
            "FilterReason": reason, "Form": clean(f.get("Form")),
            "AccessionNumber": accn, "CIK": clean(f.get("CIK")).zfill(10),
            "Description": clean(f.get("Description")), "URL": clean(f.get("FilingURL")),
        }
        (kept if keep else review).append(row)
    key = lambda r: (r["PublishedAtUTC"], r["Ticker"], r["EventID"])
    return sorted(kept, key=key, reverse=True), sorted(review, key=key, reverse=True)


def fact_units(cf: dict[str, Any], concepts: tuple[str, ...], units: tuple[str, ...]):
    facts = cf.get("facts", {}).get("us-gaap", {})
    for concept in concepts:
        payload = facts.get(concept, {})
        for unit in units:
            rows = payload.get("units", {}).get(unit)
            if isinstance(rows, list) and rows: return concept, unit, rows
    return "", "", []


def duration_days(r: dict[str, Any]) -> Optional[int]:
    try: return (date.fromisoformat(clean(r.get("end"))) - date.fromisoformat(clean(r.get("start")))).days
    except ValueError: return None


def fiscal_year(r: dict[str, Any]) -> Optional[int]:
    try: return int(clean(r.get("fy")))
    except (TypeError, ValueError): return None


def valid_duration(r: dict[str, Any], form: str) -> bool:
    d, form = duration_days(r), normalize(form)
    if form == "10-Q": return d is not None and 70 <= d <= 110
    if form in {"10-K", "20-F", "40-F"}: return d is not None and 330 <= d <= 400
    return True


def select_current(rows: list[dict[str, Any]], accn: str, form: str) -> Optional[dict[str, Any]]:
    form = normalize(form)
    candidates = [r for r in rows if clean(r.get("accn")) == accn]
    candidates = [r for r in candidates if not clean(r.get("form")) or normalize(r.get("form")) == form]
    candidates = [r for r in candidates if valid_duration(r, form) and finite_float(r.get("val")) is not None]
    if not candidates: return None
    target = 91 if form == "10-Q" else 365
    wanted_fp = {"Q1", "Q2", "Q3"} if form == "10-Q" else {"FY"}
    return max(candidates, key=lambda r: (
        1 if clean(r.get("fp")).upper() in wanted_fp else 0,
        -abs((duration_days(r) or target) - target),
        1 if clean(r.get("frame")) else 0,
        clean(r.get("end")),
    ))


def select_previous(rows: list[dict[str, Any]], current: dict[str, Any], accn: str, form: str):
    try: current_end = date.fromisoformat(clean(current.get("end")))
    except ValueError: return None
    fy, fp, dur = fiscal_year(current), clean(current.get("fp")).upper(), duration_days(current)
    candidates = []
    for r in rows:
        if clean(r.get("accn")) == accn or finite_float(r.get("val")) is None or not valid_duration(r, form): continue
        if clean(r.get("fp")).upper() != fp or fiscal_year(r) != (fy - 1 if fy is not None else None): continue
        if dur is not None and duration_days(r) is not None and abs(duration_days(r) - dur) > 10: continue
        try: delta = (current_end - date.fromisoformat(clean(r.get("end")))).days
        except ValueError: continue
        if 330 <= delta <= 400: candidates.append(r)
    return max(candidates, key=lambda r: clean(r.get("filed"))) if candidates else None


def extract_metric(cf, concepts, units, accn, form):
    concept, unit, rows = fact_units(cf, concepts, units)
    current = select_current(rows, accn, form)
    if current is None:
        return {"Concept": concept, "Unit": unit, "Actual": None, "Previous": None,
                "Start": "", "End": "", "FY": "", "FP": "", "DurationDays": None,
                "PreviousEnd": "", "PreviousFY": "", "PreviousFP": "", "Frame": "",
                "Accession": "", "Filed": "", "Form": ""}
    previous = select_previous(rows, current, accn, form)
    return {"Concept": concept, "Unit": unit, "Actual": finite_float(current.get("val")),
            "Previous": finite_float(previous.get("val")) if previous else None,
            "Start": clean(current.get("start")), "End": clean(current.get("end")),
            "FY": clean(current.get("fy")), "FP": clean(current.get("fp")),
            "DurationDays": duration_days(current),
            "PreviousEnd": clean(previous.get("end")) if previous else "",
            "PreviousFY": clean(previous.get("fy")) if previous else "",
            "PreviousFP": clean(previous.get("fp")) if previous else "",
            "Frame": clean(current.get("frame")), "Accession": clean(current.get("accn")),
            "Filed": clean(current.get("filed")), "Form": clean(current.get("form"))}


def period_valid(m, form, filed_at):
    if m["Actual"] is None: return True
    try:
        end, filed = date.fromisoformat(m["End"]), date.fromisoformat(filed_at[:10])
    except ValueError: return False
    age, form, fp, d = (filed-end).days, normalize(form), m["FP"].upper(), m["DurationDays"]
    if form == "10-Q": return d is not None and 70 <= d <= 110 and fp in {"Q1","Q2","Q3"} and 0 <= age <= 130
    if form in {"10-K","20-F","40-F"}: return d is not None and 330 <= d <= 400 and fp == "FY" and 0 <= age <= 200
    return True


def previous_valid(m):
    if m["Previous"] is None: return True
    try:
        cur, prev = date.fromisoformat(m["End"]), date.fromisoformat(m["PreviousEnd"])
        return 330 <= (cur-prev).days <= 400 and int(m["PreviousFY"]) == int(m["FY"])-1 and m["PreviousFP"].upper() == m["FP"].upper()
    except (TypeError, ValueError): return False


def source_validation(m, filing):
    if m["Actual"] is None: return "NOT_AVAILABLE", "Aucune valeur XBRL retenue"
    if m["Accession"] != filing["AccessionNumber"]: return "SOURCE_REVIEW_REQUIRED", "Accession non identique"
    if m["Form"] and normalize(m["Form"]) != normalize(filing["Form"]): return "SOURCE_REVIEW_REQUIRED", "Formulaire non identique"
    if not m["End"] or m["DurationDays"] is None: return "SOURCE_REVIEW_REQUIRED", "Période incomplète"
    return "SOURCE_VALIDATED", "Accession, formulaire et période cohérents"


def invalidate(m, with_actual=True):
    if with_actual: m["Actual"] = None
    m["Previous"] = None; m["PreviousEnd"] = ""; m["PreviousFY"] = ""; m["PreviousFP"] = ""


def pct(a, b):
    return None if a is None or b in (None, 0) else (a/b-1)*100


def extract_financials(filing, cf):
    accn, form = filing["AccessionNumber"], filing["Form"]
    rev = extract_metric(cf, REVENUE_CONCEPTS, ("USD",), accn, form)
    eps = extract_metric(cf, DILUTED_EPS_CONCEPTS, ("USD/shares", "USD / shares"), accn, form)
    if eps["Actual"] is None:
        eps = extract_metric(cf, BASIC_EPS_CONCEPTS, ("USD/shares", "USD / shares"), accn, form)
    invalid_period, invalid_previous = [], []
    for name, metric in (("REVENUE", rev), ("EPS", eps)):
        if not period_valid(metric, form, filing["PublishedAtUTC"]): invalid_period.append(name); invalidate(metric)
        elif not previous_valid(metric): invalid_previous.append(name); invalidate(metric, False)
    rs, rr = source_validation(rev, filing); es, er = source_validation(eps, filing)
    metric = rev if rev["Actual"] is not None else eps
    period = f"FY{metric['FY']}-{metric['FP']}" if metric["FY"] and metric["FP"] else "UNKNOWN"
    return {"Period": period,
            "Revenue": rev, "RevenueGrowthPct": pct(rev["Actual"],rev["Previous"]),
            "EPS": eps, "RevenueSourceStatus": rs, "RevenueSourceReason": rr,
            "EPSSourceStatus": es, "EPSSourceReason": er,
            "InvalidPeriodFields": "|".join(invalid_period),
            "InvalidPreviousFields": "|".join(invalid_previous)}


def display(v, n=4): return "N/A" if v is None else f"{v:,.{n}f}"


def economic_review_status(financials):
    has = financials["Revenue"]["Actual"] is not None or financials["EPS"]["Actual"] is not None
    return (("ECONOMIC_REVIEW_REQUIRED", "Validation technique acquise; corroboration économique absente")
            if has else ("NOT_APPLICABLE", "Aucune métrique financière disponible"))


def main() -> int:
    print("="*115); print("EARNINGS & COMPANY AGENT"); print("EXTRACTION SEC COMPANY FACTS - DIAGNOSTIC UNIQUEMENT"); print("="*115)
    print("AgentVersion :", AGENT_VERSION); print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED); print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    missing=[p for p in (CATALYST_EVENTS_FILE,CATALYST_SEMANTIC_FILE,CATALYST_SEC_FILINGS_FILE) if not p.exists()]
    if missing:
        for p in missing: print("ERREUR : fichier source introuvable :",p)
        return 2
    tickers=load_tickers(); watch=set(tickers); events=load_event_map(); sem=semantic_candidates(watch,events); sec,review=sec_candidates(watch)
    print("\nTickers charges :",len(tickers)); print("Candidats semantiques :",len(sem)); print("Publications SEC retenues :",len(sec)); print("Filings SEC a revoir :",len(review))
    cache={}; errors=extracted=no_data=0
    print("\n"+"="*115+"\nEXTRACTION DES CHIFFRES PUBLIES\n"+"="*115)
    for i,f in enumerate(sec,1):
        cik=f["CIK"]
        if cik not in cache:
            try: cache[cik]=get_json(f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json"); time.sleep(REQUEST_PAUSE)
            except Exception as e: errors+=1; cache[cik]={}; print(f"\n{i}. {f['Ticker']} | ERREUR COMPANY FACTS : {e}"); continue
        x=extract_financials(f,cache[cik]); rev,eps=x["Revenue"],x["EPS"]
        has=rev["Actual"] is not None or eps["Actual"] is not None
        extracted += int(has); no_data += int(not has)
        econ,econ_reason=economic_review_status(x)
        invalid_p=bool(x["InvalidPeriodFields"]); invalid_prev=bool(x["InvalidPreviousFields"])
        if not has: status="INVALID_XBRL_PERIOD" if invalid_p else "NO_MATCHING_XBRL_FACT"
        elif invalid_p: status="EXTRACTED_PARTIAL_INVALID_PERIOD"
        elif invalid_prev: status="EXTRACTED_WITHOUT_VALID_PREVIOUS"
        elif x["RevenueSourceStatus"]=="SOURCE_REVIEW_REQUIRED" or x["EPSSourceStatus"]=="SOURCE_REVIEW_REQUIRED": status="SOURCE_REVIEW_REQUIRED"
        else: status="EXTRACTED_SOURCE_VALIDATED"
        print(f"\n{i}. {f['Ticker']} | {f['PublishedAtUTC']} | {f['Classification']} | {f['Form']}")
        print("EventID :",f["EventID"]); print("Period :",x["Period"])
        for name,m,source_status,source_reason in (("Revenue",rev,x["RevenueSourceStatus"],x["RevenueSourceReason"]),("EPS",eps,x["EPSSourceStatus"],x["EPSSourceReason"])):
            print(f"{name} :",display(m["Actual"],2 if name=="Revenue" else 4),m["Unit"])
            print(f"{name}Previous :",display(m["Previous"],2 if name=="Revenue" else 4))
            print(f"{name}Concept :",m["Concept"] or "N/A"); print(f"{name}Start :",m["Start"] or "N/A"); print(f"{name}End :",m["End"] or "N/A")
            print(f"{name}Frame :",m["Frame"] or "N/A"); print(f"{name}Accession :",m["Accession"] or "N/A"); print(f"{name}Filed :",m["Filed"] or "N/A")
            print(f"{name}SourceStatus :",source_status); print(f"{name}SourceReason :",source_reason); print(f"{name}DurationDays :",m["DurationDays"] if m["DurationDays"] is not None else "N/A")
            print(f"{name}PreviousPeriod :",m["PreviousFY"] or "N/A",m["PreviousFP"],m["PreviousEnd"])
        print("RevenueGrowthPct :",display(x["RevenueGrowthPct"])); print("InvalidPeriodFields :",x["InvalidPeriodFields"] or "NONE")
        print("InvalidPreviousFields :",x["InvalidPreviousFields"] or "NONE"); print("EconomicValidationStatus :",econ); print("EconomicValidationReason :",econ_reason); print("DataStatus :",status)
    print("\n"+"="*115+"\nRESUME EXTRACTION\n"+"="*115)
    print("Publications SEC examinees :",len(sec)); print("Publications avec donnees :",extracted); print("Publications sans correspondance XBRL :",no_data)
    print("Erreurs SEC Company Facts :",errors); print("CIK Company Facts charges :",len(cache)); print("Aucune ecriture dans earnings_company_events.csv")
    print("ObservationOnly :",OBSERVATION_ONLY); print("V1ExecutionChanged :",V1_EXECUTION_CHANGED); print("PortfolioV1Changed :",PORTFOLIO_V1_CHANGED); print("="*115)
    return 0


if __name__ == "__main__": raise SystemExit(main())
