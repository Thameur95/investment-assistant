#!/usr/bin/env python3
"""Earnings & Company Agent V1, observation uniquement.

SEC/XBRL + consensus Finnhub + Guidance Catalyst + diagnostic KPI métier.
Le diagnostic KPI explore Company Facts puis le véritable document d'instance
XBRL. FilingSummary.xml et les linkbases de présentation sont exclus.
"""
from __future__ import annotations

import csv, json, os, re, time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import (
    AGENT_VERSION, CATALYST_EVENTS_FILE, CATALYST_SEC_FILINGS_FILE,
    CATALYST_SEMANTIC_FILE, EARNINGS_RESULTS_FILE, OBSERVATION_ONLY,
    PORTFOLIO_V1_CHANGED, STOCKS_FILE, V1_EXECUTION_CHANGED,
)

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "investment-assistant personal-research contact@example.com").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
SEC_COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
REQUEST_TIMEOUT, REQUEST_PAUSE = 25, 0.15
FINNHUB_DATE_WINDOW_DAYS, GUIDANCE_MATCH_DAYS = 10, 3
REVENUE_TOLERANCE_PCT, KPI_STABLE_THRESHOLD_PCT = 2.0, 1.0
KPI_DIAGNOSTIC_LIMIT, XBRL_DETAIL_LIMIT = 8, 12

CONDITIONAL_FORMS = {"8-K", "6-K"}
EARNINGS_WORDS = re.compile(r"\b(earnings|results?|financial results?|quarterly results?|annual results?|income|revenue|sales|guidance|outlook|chiffre d.?affaires|résultats?|resultats?)\b", re.I)
REVENUE_CONCEPTS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet")
DILUTED_EPS_CONCEPTS = ("EarningsPerShareDiluted", "IncomeLossFromContinuingOperationsPerDilutedShare")
BASIC_EPS_CONCEPTS = ("EarningsPerShareBasic", "IncomeLossFromContinuingOperationsPerBasicShare")
GUIDANCE_PATTERNS = {
    "WITHDRAWN": re.compile(r"\b(withdraw(?:n|s)?|suspend(?:ed|s)?|retire|retiree|suspend)\b", re.I),
    "RAISED": re.compile(r"\b(raise[sd]?|increase[sd]?|boost(?:ed|s)?|lift(?:ed|s)?|releve|rehausse|revise en hausse)\b", re.I),
    "LOWERED": re.compile(r"\b(lower(?:ed|s)?|cut(?:s)?|reduce[sd]?|downgrade[sd]?|abaisse|reduit|revise en baisse)\b", re.I),
    "REAFFIRMED": re.compile(r"\b(reaffirm(?:ed|s)?|reiterate[sd]?|maintain(?:ed|s)?|confirm(?:ed|s)?|reaffirme|maintient|confirme)\b", re.I),
    "INTRODUCED": re.compile(r"\b(guidance|outlook|previsions|perspectives|initiat(?:e|ed|es)|introduc(?:e|ed|es)|provid(?:e|ed|es))\b", re.I),
}
KPI_MAP: dict[str, dict[str, Any]] = {
    "NVDA": {"name": "Data Center Revenue", "patterns": [r"datacenter.*revenue", r"revenue.*datacenter"]},
    "MSFT": {"name": "Intelligent Cloud Revenue", "patterns": [r"intelligentcloud.*revenue", r"azure.*revenue"]},
    "GOOGL": {"name": "Google Cloud Revenue", "patterns": [r"googlecloud.*revenue", r"cloud.*revenue"]},
    "AMZN": {"name": "AWS Net Sales", "patterns": [r"amazonwebservices.*(sales|revenue)", r"aws.*(sales|revenue)"]},
    "META": {"name": "Family of Apps Revenue", "patterns": [r"familyofapps.*revenue"]},
    "TSLA": {"name": "Automotive Revenue", "patterns": [r"automotive.*revenue"]},
    "AAPL": {"name": "Services Revenue", "patterns": [r"services.*(netsales|revenue)"]},
    "SPCX": {"name": "Starlink Revenue", "patterns": [r"starlink.*revenue"]},
    "VLO": {"name": "Refining Revenue", "patterns": [r"refining.*revenue"]},
    "SU": {"name": "Oil Sands Revenue", "patterns": [r"oilsands.*revenue"]},
    "TTE": {"name": "LNG Revenue", "patterns": [r"lng.*revenue", r"integratedlng.*revenue"]},
}
DIMENSIONAL_KPI_RULES: dict[str, dict[str, Any]] = {
    "TSLA": {
        "concepts": {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "member": "tsla:AutomotiveRevenuesMember",
        "forbidden": set(),
    },
    "META": {
        "concepts": {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "member": "meta:FamilyOfAppsMember",
        "forbidden": {"srt:ProductOrServiceAxis"},
    },
    "AMZN": {
        "concepts": {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "member": "amzn:AmazonWebServicesSegmentMember",
        "forbidden": {"srt:ConsolidationItemsAxis"},
    },
    "GOOGL": {
        "concepts": {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "member": "goog:GoogleCloudMember",
        "forbidden": {"srt:ConsolidationItemsAxis"},
    },
    "MSFT": {
        "concepts": {"RevenueFromContractWithCustomerExcludingAssessedTax"},
        "member": "msft:IntelligentCloudMember",
        "forbidden": set(),
    },
    "VLO": {
        "concepts": {"RevenueFromContractWithCustomerIncludingAssessedTax"},
        "member": "vlo:RefiningMember",
        "forbidden": {"srt:ConsolidationItemsAxis"},
    },
}

RESULT_COLUMNS = ["EventID","AgentVersion","Ticker","ObservedAtUTC","PublishedAtUTC","EventType","Period","RevenueActual","RevenueConsensus","RevenuePrevious","RevenueSurprisePct","RevenueGrowthPct","EPSActual","EPSConsensus","EPSPrevious","EPSSurprisePct","GuidanceStatus","KeyKPIName","KeyKPIActual","KeyKPIConsensus","KeyKPIPrevious","KeyKPIMomentum","FundamentalAssessment","ExpectationAssessment","OverallVerdict","Confidence","DataStatus","NeedsHumanReview","ObservationOnly","V1ExecutionChanged","PortfolioV1Changed"]


def clean(v: Any) -> str: return "" if v is None else " ".join(str(v).split()).strip()
def normalize(v: Any) -> str: return clean(v).upper().replace("_", "-")
def as_bool(v: Any) -> bool: return clean(v).upper() in {"TRUE","1","YES","OUI"}
def finite_float(v: Any) -> Optional[float]:
    try: x=float(v); return x if x==x and abs(x)!=float("inf") else None
    except (TypeError, ValueError): return None

def read_csv(path: Path) -> list[dict[str,str]]:
    if not path.exists() or path.stat().st_size==0: return []
    with path.open("r",encoding="utf-8-sig",newline="") as f: return list(csv.DictReader(f))
def write_csv(path: Path, rows: list[dict[str,Any]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=RESULT_COLUMNS,extrasaction="ignore"); w.writeheader(); w.writerows(rows)
def load_tickers() -> list[str]:
    return [x.strip().upper() for x in STOCKS_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]

def get_bytes(url: str, headers: dict[str,str]) -> bytes:
    request=Request(url,headers=headers,method="GET")
    try:
        with urlopen(request,timeout=REQUEST_TIMEOUT) as response: return response.read()
    except HTTPError as e: raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8',errors='replace')[:250]}") from e
    except URLError as e: raise RuntimeError(f"Erreur reseau: {e.reason}") from e
def get_json(url: str, headers: dict[str,str]) -> Any: return json.loads(get_bytes(url,headers).decode("utf-8"))
def get_sec_json(url: str) -> Any: return get_json(url,{"User-Agent":SEC_USER_AGENT,"Accept":"application/json"})
def get_sec_xml(url: str) -> bytes: return get_bytes(url,{"User-Agent":SEC_USER_AGENT,"Accept":"application/xml,text/xml,*/*"})
def get_finnhub_json(url: str) -> Any:
    if not FINNHUB_API_KEY: raise RuntimeError("FINNHUB_API_KEY absent")
    return get_json(url,{"X-Finnhub-Token":FINNHUB_API_KEY,"Accept":"application/json"})

def load_event_map(): return {clean(r.get("EventID")):r for r in read_csv(CATALYST_EVENTS_FILE) if clean(r.get("EventID"))}
def semantic_candidates(watchlist, events):
    out=[]
    for r in read_csv(CATALYST_SEMANTIC_FILE):
        ticker,eid=clean(r.get("Ticker")).upper(),clean(r.get("EventID"))
        if ticker not in watchlist or not eid or not as_bool(r.get("IsRelevant")): continue
        ctype,cat=normalize(r.get("CatalystType")),normalize(r.get("Category"))
        if ctype not in {"EARNINGS","GUIDANCE"} and cat not in {"EARNINGS","GUIDANCE","FINANCIAL-RESULTS","FINANCIAL-UPDATE"}: continue
        e=events.get(eid,{})
        out.append({"EventID":eid,"Ticker":ticker,"PublishedAtUTC":clean(r.get("FirstSeenUTC") or e.get("FirstSeenUTC")),"CatalystType":ctype,"Category":cat,"Headline":clean(r.get("RepresentativeTitle") or e.get("RepresentativeTitle")),"KeyFact":clean(r.get("KeyFact")),"Reason":clean(r.get("Reason"))})
    return out

def classify_sec(r):
    form,desc=normalize(r.get("Form")),clean(r.get("Description"))
    if form=="10-Q": return True,"ACTUAL_QUARTERLY_FILING"
    if form in {"10-K","20-F","40-F"}: return True,"ACTUAL_ANNUAL_FILING"
    if form in CONDITIONAL_FORMS and EARNINGS_WORDS.search(desc): return True,"ACTUAL_EARNINGS_RELEASE"
    return False,"SEC_REVIEW"
def sec_candidates(watchlist):
    kept,review=[],[]
    for r in read_csv(CATALYST_SEC_FILINGS_FILE):
        ticker,accn=clean(r.get("Ticker")).upper(),clean(r.get("AccessionNumber"))
        if ticker not in watchlist or not accn: continue
        keep,kind=classify_sec(r)
        item={"EventID":f"SEC|{ticker}|{accn}","Ticker":ticker,"PublishedAtUTC":clean(r.get("FiledAt")),"Classification":kind,"Form":clean(r.get("Form")),"AccessionNumber":accn,"CIK":clean(r.get("CIK")).zfill(10),"Description":clean(r.get("Description")),"PrimaryDocument":clean(r.get("PrimaryDocument")),"FilingURL":clean(r.get("FilingURL"))}
        (kept if keep else review).append(item)
    key=lambda x:(x["PublishedAtUTC"],x["Ticker"],x["EventID"])
    return sorted(kept,key=key,reverse=True),sorted(review,key=key,reverse=True)

def duration_days(r):
    try: return (date.fromisoformat(clean(r.get("end")))-date.fromisoformat(clean(r.get("start")))).days
    except ValueError: return None
def fiscal_year(r):
    try: return int(clean(r.get("fy")))
    except (TypeError,ValueError): return None
def valid_duration(r,form):
    d,form=duration_days(r),normalize(form)
    if form=="10-Q": return d is not None and 70<=d<=110
    if form in {"10-K","20-F","40-F"}: return d is not None and 330<=d<=400
    return True
def select_current(entries,accn,form):
    form=normalize(form); rows=[r for r in entries if clean(r.get("accn"))==accn and (not clean(r.get("form")) or normalize(r.get("form"))==form) and valid_duration(r,form) and finite_float(r.get("val")) is not None]
    if not rows:return None
    target=91 if form=="10-Q" else 365; wanted={"Q1","Q2","Q3"} if form=="10-Q" else {"FY"}
    return max(rows,key=lambda r:(1 if clean(r.get("fp")).upper() in wanted else 0,-abs((duration_days(r) or target)-target),clean(r.get("end"))))
def select_previous(entries,current,accn,form):
    try: current_end=date.fromisoformat(clean(current.get("end")))
    except ValueError:return None
    fy,fp,dur=fiscal_year(current),clean(current.get("fp")).upper(),duration_days(current); rows=[]
    for r in entries:
        if clean(r.get("accn"))==accn or finite_float(r.get("val")) is None or not valid_duration(r,form):continue
        if clean(r.get("fp")).upper()!=fp or fiscal_year(r)!=(fy-1 if fy is not None else None):continue
        if dur is not None and duration_days(r) is not None and abs(duration_days(r)-dur)>10:continue
        try: delta=(current_end-date.fromisoformat(clean(r.get("end")))).days
        except ValueError:continue
        if 330<=delta<=400:rows.append(r)
    return max(rows,key=lambda r:clean(r.get("filed"))) if rows else None

def all_concepts(facts):
    for ns,concepts in facts.get("facts",{}).items():
        if isinstance(concepts,dict):
            for concept,payload in concepts.items():
                if isinstance(payload,dict):yield ns,concept,payload
def empty_metric():return {"Concept":"","Unit":"","Actual":None,"Previous":None,"Start":"","End":"","FY":"","FP":"","DurationDays":None,"Accession":"","Form":""}
def metric_payload(concept,unit,current,previous):return {"Concept":concept,"Unit":unit,"Actual":finite_float(current.get("val")),"Previous":finite_float(previous.get("val")) if previous else None,"Start":clean(current.get("start")),"End":clean(current.get("end")),"FY":clean(current.get("fy")),"FP":clean(current.get("fp")),"DurationDays":duration_days(current),"Accession":clean(current.get("accn")),"Form":clean(current.get("form"))}
def extract_named(facts,concepts,accn,form,units):
    gaap=facts.get("facts",{}).get("us-gaap",{})
    for concept in concepts:
        for unit in units:
            entries=gaap.get(concept,{}).get("units",{}).get(unit,[])
            current=select_current(entries,accn,form)
            if current:return metric_payload(concept,unit,current,select_previous(entries,current,accn,form))
    return empty_metric()
def period_valid(m,form,filed_at):
    if m["Actual"] is None:return True
    try:end,filed=date.fromisoformat(m["End"]),date.fromisoformat(filed_at[:10])
    except ValueError:return False
    age=(filed-end).days; form=normalize(form); d=m["DurationDays"]; fp=m["FP"].upper()
    if form=="10-Q":return d is not None and 70<=d<=110 and fp in {"Q1","Q2","Q3"} and 0<=age<=130
    if form in {"10-K","20-F","40-F"}:return d is not None and 330<=d<=400 and fp=="FY" and 0<=age<=200
    return True
def extract_financials(filing,facts):
    accn,form=filing["AccessionNumber"],filing["Form"]
    rev=extract_named(facts,REVENUE_CONCEPTS,accn,form,("USD",)); eps=extract_named(facts,DILUTED_EPS_CONCEPTS,accn,form,("USD/shares","USD / shares"))
    if eps["Actual"] is None:eps=extract_named(facts,BASIC_EPS_CONCEPTS,accn,form,("USD/shares","USD / shares"))
    for m in (rev,eps):
        if not period_valid(m,form,filing["PublishedAtUTC"]):m["Actual"]=m["Previous"]=None
    has=rev["Actual"] is not None or eps["Actual"] is not None
    source_ok=all(m["Actual"] is None or (m["Accession"]==accn and (not m["Form"] or normalize(m["Form"])==normalize(form))) for m in (rev,eps))
    status="NO_MATCHING_XBRL_FACT" if not has else ("EXTRACTED_SOURCE_VALIDATED" if source_ok else "SOURCE_REVIEW_REQUIRED")
    pm=rev if rev["Actual"] is not None else eps; period=f"FY{pm['FY']}-{pm['FP']}" if pm["FY"] and pm["FP"] else "UNKNOWN"
    growth=None if rev["Actual"] is None or rev["Previous"] in (None,0) else (rev["Actual"]/rev["Previous"]-1)*100
    return {"Revenue":rev,"EPS":eps,"Period":period,"RevenueGrowthPct":growth,"HasData":has,"DataStatus":status}

def kpi_tokens(config):return {t.lower() for t in re.findall(r"[A-Za-z0-9]+",config["name"]+" "+" ".join(config["patterns"])) if len(t)>=3 and t.lower() not in {"revenue","sales","net","netsales"}}
def extract_kpi(facts,filing):
    cfg=KPI_MAP.get(filing["Ticker"])
    if not cfg:return {"Name":"","Concept":"","Actual":None,"Previous":None,"Momentum":"UNKNOWN","Status":"NO_MAPPING"}
    patterns=[re.compile(p,re.I) for p in cfg["patterns"]]
    for ns,concept,payload in all_concepts(facts):
        normalized=re.sub(r"[^a-z0-9]","",concept.lower())
        if not any(p.search(normalized) for p in patterns):continue
        for unit,entries in payload.get("units",{}).items():
            current=select_current(entries,filing["AccessionNumber"],filing["Form"])
            if not current:continue
            prev=select_previous(entries,current,filing["AccessionNumber"],filing["Form"]); actual=finite_float(current.get("val")); previous=finite_float(prev.get("val")) if prev else None
            momentum="UNKNOWN" if actual is None or previous in (None,0) else ("UP" if (actual/previous-1)*100>1 else ("DOWN" if (actual/previous-1)*100<-1 else "STABLE"))
            return {"Name":cfg["name"],"Concept":f"{ns}:{concept}","Actual":actual,"Previous":previous,"Momentum":momentum,"Status":"EXTRACTED"}
    return {"Name":cfg["name"],"Concept":"","Actual":None,"Previous":None,"Momentum":"UNKNOWN","Status":"NOT_FOUND"}

def local_tag(tag):return tag.rsplit("}",1)[-1]
def archive_dir(filing):return f"{SEC_ARCHIVES_BASE}/{int(filing['CIK'])}/{filing['AccessionNumber'].replace('-','')}"
def discover_instance_document(filing):
    """Exclut les fichiers de rendu et valide impérativement une racine xbrl."""
    directory=archive_dir(filing); payload=get_sec_json(f"{directory}/index.json"); items=payload.get("directory",{}).get("item",[]) if isinstance(payload,dict) else []
    excluded_exact={"filingsummary.xml","metafiles.xml","metalinks.json"}; excluded_suffixes=("_cal.xml","_def.xml","_lab.xml","_pre.xml","_ref.xml","_sch.xml"); excluded_fragments=("filingsummary","report_r","report_f","report_g","report_p")
    candidates=[]
    for item in items:
        name=clean(item.get("name")); lower=name.lower()
        if not lower.endswith((".xml",".xbrl")) or lower in excluded_exact or lower.endswith(excluded_suffixes) or any(x in lower for x in excluded_fragments):continue
        candidates.append(name)
    primary_stem=Path(clean(filing.get("PrimaryDocument"))).stem.lower()
    candidates.sort(key=lambda name:(0 if name.lower().endswith(".xbrl") else 1,0 if primary_stem and Path(name).stem.lower()==primary_stem else 1,len(name),name))
    for name in candidates:
        try:root=ET.fromstring(get_sec_xml(f"{directory}/{name}"))
        except Exception:continue
        if local_tag(root.tag).lower()=="xbrl":return name
    return ""
def context_map(root):
    out={}
    for el in root.iter():
        if local_tag(el.tag)!="context":continue
        cid=clean(el.attrib.get("id")); dims={}; start=end=instant=""
        for child in el.iter():
            name=local_tag(child.tag)
            if name in {"explicitMember","typedMember"}:dims[clean(child.attrib.get("dimension"))]=clean(" ".join(child.itertext()))
            elif name=="startDate":start=clean(child.text)
            elif name=="endDate":end=clean(child.text)
            elif name=="instant":instant=clean(child.text)
        out[cid]={"Dimensions":" | ".join(f"{k}={v}" for k,v in sorted(dims.items())),"Start":start,"End":end or instant}
    return out
def detailed_diagnostic(filing,config):
    document=discover_instance_document(filing)
    if not document:return "",[]
    root=ET.fromstring(get_sec_xml(f"{archive_dir(filing)}/{document}")); contexts=context_map(root); tokens=kpi_tokens(config); rows=[]
    for fact in root.iter():
        cref=clean(fact.attrib.get("contextRef"))
        if not cref:continue
        concept=local_tag(fact.tag); context=contexts.get(cref,{}); dims=clean(context.get("Dimensions")); haystack=f"{concept} {dims}".lower(); hits=sorted(t for t in tokens if t in haystack); value=clean(fact.text)
        if hits and value:rows.append({"Concept":concept,"Value":value,"Unit":clean(fact.attrib.get("unitRef")),"Start":clean(context.get("Start")),"End":clean(context.get("End")),"Dimensions":dims,"Hits":",".join(hits)})
    rows.sort(key=lambda r:(len(r["Hits"].split(",")),1 if r["Dimensions"] else 0,r["End"]),reverse=True)
    return document,rows[:XBRL_DETAIL_LIMIT]

def dimensional_fact_rows(filing: dict[str, str]) -> list[dict[str, Any]]:
    document = discover_instance_document(filing)
    if not document:
        return []
    root = ET.fromstring(get_sec_xml(f"{archive_dir(filing)}/{document}"))
    contexts = context_map(root)
    rows: list[dict[str, Any]] = []
    for fact in root.iter():
        context_ref = clean(fact.attrib.get("contextRef"))
        if not context_ref:
            continue
        context = contexts.get(context_ref, {})
        value = finite_float(clean(fact.text))
        if value is None:
            continue
        rows.append({
            "Concept": local_tag(fact.tag),
            "Value": value,
            "Unit": clean(fact.attrib.get("unitRef")),
            "Start": clean(context.get("Start")),
            "End": clean(context.get("End")),
            "Dimensions": clean(context.get("Dimensions")),
        })
    return rows


def period_days(start: str, end: str) -> Optional[int]:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def exact_dimensional_candidate(
    rows: list[dict[str, Any]],
    rule: dict[str, Any],
    form: str,
    filing_date_text: str,
) -> Optional[dict[str, Any]]:
    try:
        filing_date = date.fromisoformat(filing_date_text[:10])
    except ValueError:
        return None
    form = normalize(form)
    candidates = []
    for row in rows:
        dimensions = row["Dimensions"]
        if row["Concept"] not in rule["concepts"]:
            continue
        if rule["member"] not in dimensions:
            continue
        if any(forbidden in dimensions for forbidden in rule["forbidden"]):
            continue
        if row["Unit"].lower() not in {"usd", "u_usd"}:
            continue
        duration = period_days(row["Start"], row["End"])
        if form == "10-Q" and (duration is None or not 70 <= duration <= 110):
            continue
        if form in {"10-K", "20-F", "40-F"} and (
            duration is None or not 330 <= duration <= 400
        ):
            continue
        try:
            end_date = date.fromisoformat(row["End"])
        except ValueError:
            continue
        age = (filing_date - end_date).days
        if form == "10-Q" and not 0 <= age <= 130:
            continue
        if form in {"10-K", "20-F", "40-F"} and not 0 <= age <= 200:
            continue
        candidates.append(row)
    if not candidates:
        return None
    # Déduplique les contextes identiques et privilégie le moins dimensionné.
    unique = {}
    for row in candidates:
        key = (row["Concept"], row["Value"], row["Start"], row["End"])
        previous = unique.get(key)
        if previous is None or row["Dimensions"].count("|") < previous["Dimensions"].count("|"):
            unique[key] = row
    return sorted(
        unique.values(),
        key=lambda row: (
            row["End"],
            -row["Dimensions"].count("|"),
            row["Value"],
        ),
        reverse=True,
    )[0]


def previous_dimensional_candidate(
    rows: list[dict[str, Any]],
    current: dict[str, Any],
    rule: dict[str, Any],
    form: str,
) -> Optional[dict[str, Any]]:
    try:
        current_start = date.fromisoformat(current["Start"])
        current_end = date.fromisoformat(current["End"])
    except ValueError:
        return None
    current_duration = (current_end - current_start).days
    matches = []
    for row in rows:
        if row["Concept"] not in rule["concepts"]:
            continue
        if rule["member"] not in row["Dimensions"]:
            continue
        if any(forbidden in row["Dimensions"] for forbidden in rule["forbidden"]):
            continue
        if row["Dimensions"] != current["Dimensions"]:
            continue
        duration = period_days(row["Start"], row["End"])
        if duration is None or abs(duration-current_duration) > 10:
            continue
        try:
            prior_end = date.fromisoformat(row["End"])
        except ValueError:
            continue
        if 330 <= (current_end-prior_end).days <= 400:
            matches.append(row)
    return max(matches, key=lambda row: row["End"]) if matches else None


def promote_dimensional_kpi(
    filing: dict[str, str],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    rule = DIMENSIONAL_KPI_RULES.get(filing["Ticker"])
    if not rule:
        return fallback
    try:
        rows = dimensional_fact_rows(filing)
    except Exception:
        return fallback
    current = exact_dimensional_candidate(
        rows, rule, filing["Form"], filing["PublishedAtUTC"]
    )
    if not current:
        return fallback
    previous = previous_dimensional_candidate(
        rows, current, rule, filing["Form"]
    )
    actual = current["Value"]
    previous_value = previous["Value"] if previous else None
    if previous_value in (None, 0):
        momentum = "UNKNOWN"
    else:
        change = (actual/previous_value - 1.0) * 100.0
        if change > KPI_STABLE_THRESHOLD_PCT:
            momentum = "UP"
        elif change < -KPI_STABLE_THRESHOLD_PCT:
            momentum = "DOWN"
        else:
            momentum = "STABLE"
    return {
        "Name": KPI_MAP[filing["Ticker"]]["name"],
        "Concept": (
            f"{current['Concept']} | {current['Dimensions']}"
        ),
        "Actual": actual,
        "Previous": previous_value,
        "Momentum": momentum,
        "Status": "EXTRACTED_DIMENSIONAL",
    }


def guidance_for(filing,semantics):
    fdate=date.fromisoformat(filing["PublishedAtUTC"][:10]); candidates=[]
    for row in semantics:
        if row["Ticker"]!=filing["Ticker"] or (row["CatalystType"]!="GUIDANCE" and row["Category"]!="GUIDANCE"):continue
        try:distance=abs((fdate-date.fromisoformat(row["PublishedAtUTC"][:10])).days)
        except ValueError:continue
        if distance<=GUIDANCE_MATCH_DAYS:candidates.append((distance,row))
    if not candidates:return "NOT_FOUND",""
    row=sorted(candidates,key=lambda x:x[0])[0][1]; text=" ".join((row["Headline"],row["KeyFact"],row["Reason"]))
    for status in ("WITHDRAWN","RAISED","LOWERED","REAFFIRMED","INTRODUCED"):
        if GUIDANCE_PATTERNS[status].search(text):return status,row["EventID"]
    return "REVIEW_REQUIRED",row["EventID"]
def fetch_finnhub(ticker,filed_at):
    center=date.fromisoformat(filed_at[:10]); params={"from":(center-timedelta(days=10)).isoformat(),"to":(center+timedelta(days=10)).isoformat(),"symbol":ticker}; payload=get_finnhub_json(f"{FINNHUB_EARNINGS_URL}?{urlencode(params)}"); rows=payload.get("earningsCalendar",[]) if isinstance(payload,dict) else []; return rows if isinstance(rows,list) else []
def corroborate(filing,financials,rows):
    base={"RevenueEstimate":None,"EPSEstimate":None,"RevenueSurprisePct":None,"Status":"NOT_APPLICABLE"}
    if not financials["HasData"]:return base
    fdate=date.fromisoformat(filing["PublishedAtUTC"][:10]); candidates=[]
    for row in rows:
        if clean(row.get("symbol")).upper()!=filing["Ticker"]:continue
        try:d=abs((fdate-date.fromisoformat(clean(row.get("date"))[:10])).days)
        except ValueError:continue
        if d<=10:candidates.append((d,row))
    if not candidates:return base|{"Status":"ECONOMIC_REVIEW_REQUIRED"}
    match=sorted(candidates,key=lambda x:x[0])[0][1]; ra=finite_float(match.get("revenueActual")); re_=finite_float(match.get("revenueEstimate")); ee=finite_float(match.get("epsEstimate")); sr=financials["Revenue"]["Actual"]; surprise=None if sr is None or re_ in (None,0) else (sr/re_-1)*100
    if sr is not None and ra is not None:diff=abs(sr-ra)/max(abs(sr),abs(ra),1e-12)*100; status="ECONOMIC_PARTIAL" if diff<=REVENUE_TOLERANCE_PCT else "ECONOMIC_MISMATCH"
    else:status="ECONOMIC_REVIEW_REQUIRED"
    return {"RevenueEstimate":re_,"EPSEstimate":ee,"RevenueSurprisePct":surprise,"Status":status}
def fundamental_assessment(fin: dict[str, Any], guidance: str, kpi: dict[str, Any]) -> tuple[str, str]:
    """Évalue uniquement la dynamique fondamentale observée, sans signal de trading."""
    if not fin["HasData"] and kpi["Status"] != "EXTRACTED_DIMENSIONAL":
        return "UNKNOWN", "Données fondamentales insuffisantes"

    positive = 0
    negative = 0
    evidence: list[str] = []

    growth = fin.get("RevenueGrowthPct")
    if growth is not None:
        if growth > 1.0:
            positive += 1
            evidence.append("REVENUE_GROWTH_UP")
        elif growth < -1.0:
            negative += 1
            evidence.append("REVENUE_GROWTH_DOWN")
        else:
            evidence.append("REVENUE_GROWTH_STABLE")

    momentum = clean(kpi.get("Momentum")).upper()
    if momentum == "UP":
        positive += 1
        evidence.append("KPI_UP")
    elif momentum == "DOWN":
        negative += 1
        evidence.append("KPI_DOWN")
    elif momentum == "STABLE":
        evidence.append("KPI_STABLE")

    if guidance == "RAISED":
        positive += 1
        evidence.append("GUIDANCE_RAISED")
    elif guidance in {"LOWERED", "WITHDRAWN"}:
        negative += 1
        evidence.append(f"GUIDANCE_{guidance}")
    elif guidance == "REAFFIRMED":
        evidence.append("GUIDANCE_REAFFIRMED")

    if positive and not negative:
        return "POSITIVE", "|".join(evidence)
    if negative and not positive:
        return "NEGATIVE", "|".join(evidence)
    if positive and negative:
        return "MIXED", "|".join(evidence)
    return "NEUTRAL", "|".join(evidence) if evidence else "Aucun signal directionnel validé"


def expectation_assessment(corr: dict[str, Any]) -> tuple[str, str]:
    """Évalue uniquement la surprise Revenue; l'EPS reste non décisionnel."""
    surprise = corr.get("RevenueSurprisePct")
    if surprise is None:
        return "UNKNOWN", "Consensus Revenue comparable indisponible"
    if surprise > 1.0:
        return "BEAT", f"REVENUE_SURPRISE={surprise:.4f}%"
    if surprise < -1.0:
        return "MISS", f"REVENUE_SURPRISE={surprise:.4f}%"
    return "IN_LINE", f"REVENUE_SURPRISE={surprise:.4f}%"


def overall_verdict(
    fin: dict[str, Any],
    corr: dict[str, Any],
    kpi: dict[str, Any],
    guidance: str,
    fundamental: str,
    expectation: str,
) -> tuple[str, str]:
    """Synthèse observation-only. Aucun lien avec V1 ou l'exécution."""
    if fin["DataStatus"] != "EXTRACTED_SOURCE_VALIDATED":
        return "UNKNOWN", "DATA_STATUS_NOT_FULLY_VALIDATED"
    if corr["Status"] == "ECONOMIC_MISMATCH":
        return "UNKNOWN", "ECONOMIC_MISMATCH_REQUIRES_REVIEW"
    if guidance == "REVIEW_REQUIRED":
        return "UNKNOWN", "GUIDANCE_REQUIRES_REVIEW"
    if fundamental == "UNKNOWN":
        return "UNKNOWN", "FUNDAMENTALS_INSUFFICIENT"

    # Les attentes connues renforcent ou contredisent les fondamentaux.
    if expectation == "BEAT":
        if fundamental == "POSITIVE":
            return "POSITIVE", "FUNDAMENTALS_POSITIVE|EXPECTATIONS_BEAT"
        if fundamental == "NEGATIVE":
            return "MIXED", "FUNDAMENTALS_NEGATIVE|EXPECTATIONS_BEAT"
        if fundamental in {"MIXED", "NEUTRAL"}:
            return "MIXED", f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_BEAT"

    if expectation == "MISS":
        if fundamental == "NEGATIVE":
            return "NEGATIVE", "FUNDAMENTALS_NEGATIVE|EXPECTATIONS_MISS"
        if fundamental == "POSITIVE":
            return "MIXED", "FUNDAMENTALS_POSITIVE|EXPECTATIONS_MISS"
        if fundamental in {"MIXED", "NEUTRAL"}:
            return "MIXED", f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_MISS"

    if expectation == "IN_LINE":
        if fundamental in {"POSITIVE", "NEGATIVE"}:
            return fundamental, f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_IN_LINE"
        return fundamental, f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_IN_LINE"

    # Consensus absent : le verdict peut rester fondamental, mais il est
    # explicitement qualifié comme non confirmé par les attentes.
    if expectation == "UNKNOWN":
        if fundamental in {"POSITIVE", "NEGATIVE", "MIXED", "NEUTRAL"}:
            return fundamental, f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_UNKNOWN"

    return "UNKNOWN", "INSUFFICIENT_COMBINED_EVIDENCE"


def verdict_confidence(
    fin: dict[str, Any],
    corr: dict[str, Any],
    kpi: dict[str, Any],
    guidance: str,
    fundamental: str,
    expectation: str,
    verdict: str,
) -> str:
    if verdict == "UNKNOWN":
        return "LOW"
    score = 0
    if fin["DataStatus"] == "EXTRACTED_SOURCE_VALIDATED": score += 1
    if kpi["Status"] == "EXTRACTED_DIMENSIONAL": score += 1
    if expectation != "UNKNOWN": score += 1
    if guidance in {"RAISED", "LOWERED", "REAFFIRMED", "WITHDRAWN"}: score += 1
    if corr["Status"] == "ECONOMIC_PARTIAL": score += 1
    return "HIGH" if score >= 4 else ("MEDIUM" if score >= 2 else "LOW")


def result_row(filing,fin,corr,guidance,kpi,observed):
    rev,eps=fin["Revenue"],fin["EPS"]
    fundamental, fundamental_reason = fundamental_assessment(fin, guidance, kpi)
    expectation, expectation_reason = expectation_assessment(corr)
    verdict, verdict_reason = overall_verdict(
        fin, corr, kpi, guidance, fundamental, expectation
    )
    confidence = verdict_confidence(
        fin, corr, kpi, guidance, fundamental, expectation, verdict
    )
    review=fin["DataStatus"]!="EXTRACTED_SOURCE_VALIDATED" or corr["Status"] in {"ECONOMIC_MISMATCH","ECONOMIC_REVIEW_REQUIRED"} or guidance=="REVIEW_REQUIRED" or kpi["Status"]=="NOT_FOUND" or fundamental=="UNKNOWN" or verdict=="UNKNOWN"
    return {"EventID":filing["EventID"],"AgentVersion":AGENT_VERSION,"Ticker":filing["Ticker"],"ObservedAtUTC":observed,"PublishedAtUTC":filing["PublishedAtUTC"],"EventType":filing["Classification"],"Period":fin["Period"],"RevenueActual":"" if rev["Actual"] is None else rev["Actual"],"RevenueConsensus":"" if corr["RevenueEstimate"] is None else corr["RevenueEstimate"],"RevenuePrevious":"" if rev["Previous"] is None else rev["Previous"],"RevenueSurprisePct":"" if corr["RevenueSurprisePct"] is None else round(corr["RevenueSurprisePct"],6),"RevenueGrowthPct":"" if fin["RevenueGrowthPct"] is None else round(fin["RevenueGrowthPct"],6),"EPSActual":"" if eps["Actual"] is None else eps["Actual"],"EPSConsensus":"" if corr["EPSEstimate"] is None else corr["EPSEstimate"],"EPSPrevious":"" if eps["Previous"] is None else eps["Previous"],"EPSSurprisePct":"","GuidanceStatus":guidance,"KeyKPIName":kpi["Name"],"KeyKPIActual":"" if kpi["Actual"] is None else kpi["Actual"],"KeyKPIConsensus":"","KeyKPIPrevious":"" if kpi["Previous"] is None else kpi["Previous"],"KeyKPIMomentum":kpi["Momentum"],"FundamentalAssessment":fundamental,"ExpectationAssessment":expectation,"OverallVerdict":verdict,"Confidence":confidence,"DataStatus":f"{fin['DataStatus']}|{corr['Status']}|KPI_{kpi['Status']}","NeedsHumanReview":review,"ObservationOnly":OBSERVATION_ONLY,"V1ExecutionChanged":V1_EXECUTION_CHANGED,"PortfolioV1Changed":PORTFOLIO_V1_CHANGED}
def merge_rows(existing,current):
    merged={clean(r.get("EventID")):r for r in existing if clean(r.get("EventID"))}; merged.update({r["EventID"]:r for r in current}); return sorted(merged.values(),key=lambda r:(clean(r.get("PublishedAtUTC")),clean(r.get("Ticker"))),reverse=True)

def main()->int:
    print("="*115);print("EARNINGS & COMPANY AGENT");print("SEC/XBRL + CONSENSUS + GUIDANCE + KPI + OVERALL VERDICT SHADOW");print("="*115)
    print("AgentVersion :",AGENT_VERSION);print("ObservationOnly :",OBSERVATION_ONLY);print("V1ExecutionChanged :",V1_EXECUTION_CHANGED);print("PortfolioV1Changed :",PORTFOLIO_V1_CHANGED);print("FinnhubConfigured :",bool(FINNHUB_API_KEY))
    watch=set(load_tickers()); semantics=semantic_candidates(watch,load_event_map()); filings,review=sec_candidates(watch); print("Tickers charges :",len(watch));print("Candidats semantiques :",len(semantics));print("Publications SEC retenues :",len(filings));print("Filings SEC a revoir :",len(review))
    facts_cache={}; fh_cache={}; output=[]; counts={}; observed=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for i,filing in enumerate(filings,1):
        cik=filing["CIK"]
        if cik not in facts_cache:
            try:facts_cache[cik]=get_sec_json(f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json");time.sleep(REQUEST_PAUSE)
            except Exception as exc:print(f"{filing['Ticker']} | ERREUR SEC : {exc}");facts_cache[cik]={}
        facts=facts_cache[cik]; fin=extract_financials(filing,facts); kpi=extract_kpi(facts,filing); kpi=promote_dimensional_kpi(filing,kpi); guidance,gid=guidance_for(filing,semantics); key=(filing["Ticker"],filing["PublishedAtUTC"][:10])
        if key not in fh_cache:
            try:fh_cache[key]=fetch_finnhub(*key);time.sleep(REQUEST_PAUSE)
            except Exception as exc:print(f"{filing['Ticker']} | ERREUR FINNHUB : {exc}");fh_cache[key]=[]
        corr=corroborate(filing,fin,fh_cache[key]);fundamental,fundamental_reason=fundamental_assessment(fin,guidance,kpi);expectation,expectation_reason=expectation_assessment(corr);verdict,verdict_reason=overall_verdict(fin,corr,kpi,guidance,fundamental,expectation);confidence=verdict_confidence(fin,corr,kpi,guidance,fundamental,expectation,verdict);output.append(result_row(filing,fin,corr,guidance,kpi,observed));counts[kpi["Status"]]=counts.get(kpi["Status"],0)+1
        print(f"\n{i}. {filing['Ticker']} | {filing['PublishedAtUTC']} | {filing['Form']}");print("Period :",fin["Period"]);print("GuidanceStatus :",guidance);print("GuidanceEventID :",gid or "N/A");print("KeyKPIName :",kpi["Name"] or "N/A");print("KeyKPIConcept :",kpi["Concept"] or "N/A");print("KeyKPIActual :",kpi["Actual"] if kpi["Actual"] is not None else "N/A");print("KeyKPIPrevious :",kpi["Previous"] if kpi["Previous"] is not None else "N/A");print("KeyKPIMomentum :",kpi["Momentum"]);print("KPIStatus :",kpi["Status"])
        if kpi["Status"]=="NOT_FOUND" and filing["Ticker"] in KPI_MAP:
            try:
                document,rows=detailed_diagnostic(filing,KPI_MAP[filing["Ticker"]]);print("XBRLInstanceDocument :",document or "NOT_FOUND")
                if not rows:print("XBRLDimensionDiagnostic : aucun fact dimensionnel candidat")
                for j,row in enumerate(rows,1):print(f"XBRLDimension{j} : concept={row['Concept']} | value={row['Value']} | unit={row['Unit'] or 'N/A'} | period={row['Start'] or 'N/A'}->{row['End'] or 'N/A'} | dimensions={row['Dimensions'] or 'NONE'} | hits={row['Hits']}")
            except Exception as exc:print("XBRLDimensionDiagnosticError :",exc)
        print("FundamentalAssessment :", fundamental)
        print("FundamentalReason :", fundamental_reason)
        print("ExpectationAssessment :", expectation)
        print("ExpectationReason :", expectation_reason)
        print("OverallVerdict :", verdict)
        print("OverallVerdictReason :", verdict_reason)
        print("Confidence :", confidence)
        print("DataStatus :",fin["DataStatus"])
    merged=merge_rows(read_csv(EARNINGS_RESULTS_FILE),output);write_csv(EARNINGS_RESULTS_FILE,merged);print("\n"+"="*115);print("RESUME KPI");print("="*115);print("Resultats ecrits cette execution :",len(output));print("Total lignes Earnings conservees :",len(merged));print("Statuts KPI :");[print(k,":",v) for k,v in sorted(counts.items())];print("Fichier resultats :",EARNINGS_RESULTS_FILE);print("ObservationOnly :",OBSERVATION_ONLY);print("V1ExecutionChanged :",V1_EXECUTION_CHANGED);print("PortfolioV1Changed :",PORTFOLIO_V1_CHANGED);print("="*115);return 0
if __name__=="__main__":raise SystemExit(main())
