#!/usr/bin/env python3
"""Earnings & Company Agent V1 - US SEC/XBRL + Europe IR, Shadow only."""
from __future__ import annotations
import csv, json, os, re, time, xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from config import (AGENT_VERSION,CATALYST_EVENTS_FILE,CATALYST_SEC_FILINGS_FILE,CATALYST_SEMANTIC_FILE,EARNINGS_RESULTS_FILE,OBSERVATION_ONLY,PORTFOLIO_V1_CHANGED,STOCKS_FILE,V1_EXECUTION_CHANGED)

SEC_USER_AGENT=os.getenv("SEC_USER_AGENT","investment-assistant personal-research contact@example.com").strip()
FINNHUB_API_KEY=os.getenv("FINNHUB_API_KEY","").strip()
SEC_COMPANYFACTS_BASE="https://data.sec.gov/api/xbrl/companyfacts"
SEC_ARCHIVES_BASE="https://www.sec.gov/Archives/edgar/data"
FINNHUB_EARNINGS_URL="https://finnhub.io/api/v1/calendar/earnings"
REQUEST_TIMEOUT=25
REQUEST_PAUSE=0.15
REVENUE_TOLERANCE_PCT=2.0
KPI_STABLE_THRESHOLD_PCT=1.0
EPS_SECONDARY_STATUS="SECONDARY_BASIS_UNKNOWN"
EARNINGS_WORDS=re.compile(r"\b(earnings|results?|financial results?|quarterly results?|annual results?|income|revenue|sales|guidance|outlook|chiffre d.?affaires|résultats?|resultats?)\b",re.I)
REVENUE_CONCEPTS=("RevenueFromContractWithCustomerExcludingAssessedTax","Revenues","SalesRevenueNet","SalesRevenueGoodsNet")
DILUTED_EPS_CONCEPTS=("EarningsPerShareDiluted","IncomeLossFromContinuingOperationsPerDilutedShare")
BASIC_EPS_CONCEPTS=("EarningsPerShareBasic","IncomeLossFromContinuingOperationsPerBasicShare")
KPI_MAP={"TSLA":"Automotive Revenue","META":"Family of Apps Revenue","AMZN":"AWS Net Sales","GOOGL":"Google Cloud Revenue","MSFT":"Intelligent Cloud Revenue","VLO":"Refining Revenue","NVDA":"Data Center Revenue","AAPL":"Services Revenue","SPCX":"Starlink Revenue"}
KPI_RULES={
"TSLA":({"RevenueFromContractWithCustomerExcludingAssessedTax"},"tsla:AutomotiveRevenuesMember",set()),
"META":({"RevenueFromContractWithCustomerExcludingAssessedTax"},"meta:FamilyOfAppsMember",{"srt:ProductOrServiceAxis"}),
"AMZN":({"RevenueFromContractWithCustomerExcludingAssessedTax"},"amzn:AmazonWebServicesSegmentMember",{"srt:ConsolidationItemsAxis"}),
"GOOGL":({"RevenueFromContractWithCustomerExcludingAssessedTax"},"goog:GoogleCloudMember",{"srt:ConsolidationItemsAxis"}),
"MSFT":({"RevenueFromContractWithCustomerExcludingAssessedTax"},"msft:IntelligentCloudMember",set()),
"VLO":({"RevenueFromContractWithCustomerIncludingAssessedTax"},"vlo:RefiningMember",{"srt:ConsolidationItemsAxis"}),}

EUROPE_IR_EVENTS=[
{"aliases":{"SAF.PA","SAF"},"EventID":"IR|SAF.PA|2026-10-23|Q3-2026","Ticker":"SAF.PA","PublishedAtUTC":"2026-10-23","EventType":"ACTUAL_QUARTERLY_REVENUE","Period":"FY2026-Q3","Currency":"EUR","RevenueActual":9_003_000_000.0,"RevenueConsensus":8_990_000_000.0,"RevenuePrevious":None,"RevenueGrowthPct":14.7,"EPSActual":None,"EPSConsensus":None,"EPSPrevious":None,"GuidanceStatus":"REAFFIRMED","KeyKPIName":"Civil engine aftermarket organic growth (%)","KeyKPIActual":18.7,"KeyKPIPrevious":None,"KeyKPIMomentum":"UP","SourceURL":"https://www.safran-group.com/download/press_release/file/2278933","ConsensusURL":"https://www.safran-group.com/fr/download/media/452055"},
{"aliases":{"ASML.AS","ASML"},"EventID":"IR|ASML.AS|2026-10-14|Q3-2026","Ticker":"ASML.AS","PublishedAtUTC":"2026-10-14","EventType":"ACTUAL_QUARTERLY_RESULTS","Period":"FY2026-Q3","Currency":"EUR","RevenueActual":11_662_000_000.0,"RevenueConsensus":None,"RevenuePrevious":None,"RevenueGrowthPct":None,"EPSActual":12.95,"EPSConsensus":None,"EPSPrevious":None,"GuidanceStatus":"INTRODUCED","KeyKPIName":"Installed Base Management sales","KeyKPIActual":3_289_000_000.0,"KeyKPIPrevious":None,"KeyKPIMomentum":"UNKNOWN","SourceURL":"https://investor.asml.com/news-releases/news-release-details/asml-reports-eu11.7-billion-total-net-sales-and-eu5.0-billion-net-income-q3-2026","ConsensusURL":""},]
EUROPE_PENDING=[{"aliases":{"AIR.PA","AIR"},"Ticker":"AIR.PA","ScheduledDate":"2026-10-28","SourceURL":"https://www.airbus.com/en/investors/financial-results"}]
RESULT_COLUMNS=["EventID","AgentVersion","Ticker","ObservedAtUTC","PublishedAtUTC","EventType","Period","RevenueActual","RevenueConsensus","RevenuePrevious","RevenueSurprisePct","RevenueGrowthPct","EPSActual","EPSConsensus","EPSPrevious","EPSSurprisePct","GuidanceStatus","KeyKPIName","KeyKPIActual","KeyKPIConsensus","KeyKPIPrevious","KeyKPIMomentum","FundamentalAssessment","ExpectationAssessment","OverallVerdict","Confidence","DataStatus","NeedsHumanReview","ObservationOnly","V1ExecutionChanged","PortfolioV1Changed","SourceType","SourceAuthority","SourceURL","Currency"]

def clean(v): return "" if v is None else " ".join(str(v).split()).strip()
def norm(v): return clean(v).upper().replace("_","-")
def ff(v):
 try: x=float(v); return x if x==x and abs(x)!=float("inf") else None
 except (TypeError,ValueError): return None
def read_csv(p):
 if not p.exists() or p.stat().st_size==0:return []
 with p.open("r",encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))
def write_csv(p,rows):
 p.parent.mkdir(parents=True,exist_ok=True)
 with p.open("w",encoding="utf-8",newline="") as f:w=csv.DictWriter(f,fieldnames=RESULT_COLUMNS,extrasaction="ignore");w.writeheader();w.writerows(rows)
def load_tickers(): return [x.strip().upper() for x in STOCKS_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
def get_bytes(url,headers):
 try:
  with urlopen(Request(url,headers=headers),timeout=REQUEST_TIMEOUT) as r:return r.read()
 except HTTPError as e: raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8',errors='replace')[:200]}") from e
 except URLError as e: raise RuntimeError(f"Network: {e.reason}") from e
def sec_json(url):return json.loads(get_bytes(url,{"User-Agent":SEC_USER_AGENT,"Accept":"application/json"}).decode())
def sec_xml(url):return get_bytes(url,{"User-Agent":SEC_USER_AGENT,"Accept":"application/xml,text/xml,*/*"})
def fh_json(url):
 if not FINNHUB_API_KEY: raise RuntimeError("FINNHUB_API_KEY absent")
 return json.loads(get_bytes(url,{"X-Finnhub-Token":FINNHUB_API_KEY,"Accept":"application/json"}).decode())
def dur(r):
 try:return (date.fromisoformat(clean(r.get("end")))-date.fromisoformat(clean(r.get("start")))).days
 except ValueError:return None
def fy(r):
 try:return int(clean(r.get("fy")))
 except:return None
def valid_dur(r,form):
 d=dur(r);form=norm(form)
 return (d is not None and 70<=d<=110) if form=="10-Q" else ((d is not None and 330<=d<=400) if form in {"10-K","20-F","40-F"} else True)
def current(entries,accn,form):
 f=norm(form); rows=[r for r in entries if clean(r.get("accn"))==accn and (not clean(r.get("form")) or norm(r.get("form"))==f) and valid_dur(r,f) and ff(r.get("val")) is not None]
 if not rows:return None
 target=91 if f=="10-Q" else 365; wanted={"Q1","Q2","Q3"} if f=="10-Q" else {"FY"}
 return max(rows,key=lambda r:(1 if clean(r.get("fp")).upper() in wanted else 0,-abs((dur(r) or target)-target),clean(r.get("end"))))
def previous(entries,cur,accn,form):
 try:ce=date.fromisoformat(clean(cur.get("end")))
 except:return None
 out=[]
 for r in entries:
  if clean(r.get("accn"))==accn or ff(r.get("val")) is None or not valid_dur(r,form):continue
  if clean(r.get("fp")).upper()!=clean(cur.get("fp")).upper() or fy(r)!=(fy(cur)-1 if fy(cur)!=None else None):continue
  try:delta=(ce-date.fromisoformat(clean(r.get("end")))).days
  except:continue
  if 330<=delta<=400:out.append(r)
 return max(out,key=lambda r:clean(r.get("filed"))) if out else None
def metric(facts,concepts,accn,form,units):
 gaap=facts.get("facts",{}).get("us-gaap",{})
 for c in concepts:
  for u in units:
   es=gaap.get(c,{}).get("units",{}).get(u,[]);cur=current(es,accn,form)
   if cur:
    prev=previous(es,cur,accn,form);return {"Actual":ff(cur.get("val")),"Previous":ff(prev.get("val")) if prev else None,"FY":clean(cur.get("fy")),"FP":clean(cur.get("fp")),"End":clean(cur.get("end")),"Accession":clean(cur.get("accn")),"Form":clean(cur.get("form"))}
 return {"Actual":None,"Previous":None,"FY":"","FP":"","End":"","Accession":"","Form":""}
def load_semantics(watch):
 events={clean(r.get("EventID")):r for r in read_csv(CATALYST_EVENTS_FILE)}; out=[]
 for r in read_csv(CATALYST_SEMANTIC_FILE):
  t=clean(r.get("Ticker")).upper();eid=clean(r.get("EventID"));ctype=norm(r.get("CatalystType"));cat=norm(r.get("Category"))
  if t in watch and eid and clean(r.get("IsRelevant")).upper() in {"TRUE","1","YES"} and (ctype in {"EARNINGS","GUIDANCE"} or cat in {"EARNINGS","GUIDANCE"}):
   e=events.get(eid,{});out.append({"Ticker":t,"EventID":eid,"PublishedAtUTC":clean(r.get("FirstSeenUTC") or e.get("FirstSeenUTC")),"CatalystType":ctype,"Category":cat,"Text":" ".join([clean(r.get("RepresentativeTitle")),clean(r.get("KeyFact")),clean(r.get("Reason"))])})
 return out
def filings(watch):
 out=[]
 for r in read_csv(CATALYST_SEC_FILINGS_FILE):
  t=clean(r.get("Ticker")).upper();a=clean(r.get("AccessionNumber"));form=norm(r.get("Form"));desc=clean(r.get("Description"))
  if t not in watch or not a:continue
  kind="ACTUAL_QUARTERLY_FILING" if form=="10-Q" else ("ACTUAL_ANNUAL_FILING" if form in {"10-K","20-F","40-F"} else ("ACTUAL_EARNINGS_RELEASE" if form in {"8-K","6-K"} and EARNINGS_WORDS.search(desc) else ""))
  if not kind:continue
  out.append({"EventID":f"SEC|{t}|{a}","Ticker":t,"PublishedAtUTC":clean(r.get("FiledAt")),"Classification":kind,"Form":clean(r.get("Form")),"AccessionNumber":a,"CIK":clean(r.get("CIK")).zfill(10),"FilingURL":clean(r.get("FilingURL"))})
 return sorted(out,key=lambda x:(x["PublishedAtUTC"],x["Ticker"]),reverse=True)
def financials(filing,facts):
 rev=metric(facts,REVENUE_CONCEPTS,filing["AccessionNumber"],filing["Form"],("USD",));eps=metric(facts,DILUTED_EPS_CONCEPTS,filing["AccessionNumber"],filing["Form"],("USD/shares","USD / shares"))
 if eps["Actual"] is None:eps=metric(facts,BASIC_EPS_CONCEPTS,filing["AccessionNumber"],filing["Form"],("USD/shares","USD / shares"))
 has=rev["Actual"] is not None or eps["Actual"] is not None;status="EXTRACTED_SOURCE_VALIDATED" if has else "NO_MATCHING_XBRL_FACT";m=rev if rev["Actual"] is not None else eps;period=f"FY{m['FY']}-{m['FP']}" if m["FY"] and m["FP"] else "UNKNOWN";growth=None if rev["Actual"] is None or rev["Previous"] in (None,0) else (rev["Actual"]/rev["Previous"]-1)*100
 return {"Revenue":rev,"EPS":eps,"HasData":has,"DataStatus":status,"Period":period,"RevenueGrowthPct":growth}
def local(tag):return tag.rsplit("}",1)[-1]
def archive(f):return f"{SEC_ARCHIVES_BASE}/{int(f['CIK'])}/{f['AccessionNumber'].replace('-','')}"
def instance_doc(f):
 idx=sec_json(f"{archive(f)}/index.json");items=idx.get("directory",{}).get("item",[]);names=[]
 for i in items:
  n=clean(i.get("name"));l=n.lower()
  if l.endswith((".xml",".xbrl")) and l not in {"filingsummary.xml","metafiles.xml"} and not l.endswith(("_cal.xml","_def.xml","_lab.xml","_pre.xml","_ref.xml","_sch.xml")):names.append(n)
 for n in names:
  try:r=ET.fromstring(sec_xml(f"{archive(f)}/{n}"))
  except:continue
  if local(r.tag).lower()=="xbrl":return n
 return ""
def contexts(root):
 out={}
 for e in root.iter():
  if local(e.tag)!="context":continue
  cid=clean(e.attrib.get("id"));dims={};st=en=ins=""
  for c in e.iter():
   n=local(c.tag)
   if n in {"explicitMember","typedMember"}:dims[clean(c.attrib.get("dimension"))]=clean(" ".join(c.itertext()))
   elif n=="startDate":st=clean(c.text)
   elif n=="endDate":en=clean(c.text)
   elif n=="instant":ins=clean(c.text)
  out[cid]={"D":" | ".join(f"{k}={v}" for k,v in sorted(dims.items())),"S":st,"E":en or ins}
 return out
def kpi(f):
 name=KPI_MAP.get(f["Ticker"],"");rule=KPI_RULES.get(f["Ticker"])
 if not name:return {"Name":"","Concept":"","Actual":None,"Previous":None,"Momentum":"UNKNOWN","Status":"NO_MAPPING"}
 if not rule:return {"Name":name,"Concept":"","Actual":None,"Previous":None,"Momentum":"UNKNOWN","Status":"NOT_FOUND"}
 doc=instance_doc(f)
 if not doc:return {"Name":name,"Concept":"","Actual":None,"Previous":None,"Momentum":"UNKNOWN","Status":"NOT_FOUND"}
 root=ET.fromstring(sec_xml(f"{archive(f)}/{doc}"));ctx=contexts(root); concepts,member,forbidden=rule;rows=[]
 for fact in root.iter():
  cr=clean(fact.attrib.get("contextRef"));v=ff(clean(fact.text));co=local(fact.tag);c=ctx.get(cr,{})
  if cr and v is not None and co in concepts and member in c.get("D","") and not any(x in c.get("D","") for x in forbidden):
   try:d=(date.fromisoformat(c["E"])-date.fromisoformat(c["S"])).days
   except:continue
   form=norm(f["Form"])
   if (form=="10-Q" and 70<=d<=110) or (form in {"10-K","20-F","40-F"} and 330<=d<=400):rows.append({"V":v,"S":c["S"],"E":c["E"],"D":c["D"],"C":co})
 if not rows:return {"Name":name,"Concept":"","Actual":None,"Previous":None,"Momentum":"UNKNOWN","Status":"NOT_FOUND"}
 cur=max(rows,key=lambda r:r["E"]);ce=date.fromisoformat(cur["E"]);prevs=[r for r in rows if r["D"]==cur["D"] and 330<=(ce-date.fromisoformat(r["E"])).days<=400];pv=max(prevs,key=lambda r:r["E"])["V"] if prevs else None;mom="UNKNOWN" if pv in (None,0) else ("UP" if (cur["V"]/pv-1)*100>1 else ("DOWN" if (cur["V"]/pv-1)*100<-1 else "STABLE"))
 return {"Name":name,"Concept":f"{cur['C']} | {cur['D']}","Actual":cur["V"],"Previous":pv,"Momentum":mom,"Status":"EXTRACTED_DIMENSIONAL"}
def guidance(f,sem):
 try:fd=date.fromisoformat(f["PublishedAtUTC"][:10])
 except:return "NOT_FOUND"
 c=[r for r in sem if r["Ticker"]==f["Ticker"] and (r["CatalystType"]=="GUIDANCE" or r["Category"]=="GUIDANCE") and clean(r["PublishedAtUTC"])]
 near=[]
 for r in c:
  try:d=abs((fd-date.fromisoformat(r["PublishedAtUTC"][:10])).days)
  except:continue
  if d<=3:near.append((d,r))
 if not near:return "NOT_FOUND"
 text=min(near,key=lambda x:x[0])[1]["Text"].lower()
 for status,pat in [("WITHDRAWN",r"withdraw|suspend"),("RAISED",r"raise|increase|boost|lift"),("LOWERED",r"lower|cut|reduce"),("REAFFIRMED",r"reaffirm|reiterate|maintain|confirm")]:
  if re.search(pat,text):return status
 return "INTRODUCED" if re.search(r"guidance|outlook",text) else "REVIEW_REQUIRED"
def fh(ticker,dt):
 d=date.fromisoformat(dt[:10]);q=urlencode({"from":(d-timedelta(days=10)).isoformat(),"to":(d+timedelta(days=10)).isoformat(),"symbol":ticker});p=fh_json(f"{FINNHUB_EARNINGS_URL}?{q}");return p.get("earningsCalendar",[]) if isinstance(p,dict) else []
def corr(f,fin,rows):
 if not fin["HasData"]:return {"RevenueEstimate":None,"EPSEstimate":None,"RevenueSurprisePct":None,"Status":"NOT_APPLICABLE"}
 fd=date.fromisoformat(f["PublishedAtUTC"][:10]);cs=[]
 for r in rows:
  try:dist=abs((fd-date.fromisoformat(clean(r.get("date"))[:10])).days)
  except:continue
  if clean(r.get("symbol")).upper()==f["Ticker"] and dist<=10:cs.append((dist,r))
 if not cs:return {"RevenueEstimate":None,"EPSEstimate":None,"RevenueSurprisePct":None,"Status":"ECONOMIC_REVIEW_REQUIRED"}
 m=min(cs,key=lambda x:x[0])[1];ra=ff(m.get("revenueActual"));re_=ff(m.get("revenueEstimate"));ee=ff(m.get("epsEstimate"));sr=fin["Revenue"]["Actual"];sur=None if sr is None or re_ in (None,0) else (sr/re_-1)*100
 status="ECONOMIC_REVIEW_REQUIRED" if sr is None or ra is None else ("ECONOMIC_PARTIAL" if abs(sr-ra)/max(abs(sr),abs(ra),1e-12)*100<=2 else "ECONOMIC_MISMATCH")
 return {"RevenueEstimate":re_,"EPSEstimate":ee,"RevenueSurprisePct":sur,"Status":status}
def assessments(fin,c,k,g):
 if not fin["HasData"] and k["Status"]!="EXTRACTED_DIMENSIONAL":fund="UNKNOWN"
 else:
  pos=neg=0;gr=fin["RevenueGrowthPct"]
  if gr is not None:pos+=gr>1;neg+=gr<-1
  pos+=k["Momentum"]=="UP";neg+=k["Momentum"]=="DOWN";pos+=g=="RAISED";neg+=g in {"LOWERED","WITHDRAWN"};fund="POSITIVE" if pos and not neg else ("NEGATIVE" if neg and not pos else ("MIXED" if pos and neg else "NEUTRAL"))
 sur=c["RevenueSurprisePct"];exp="UNKNOWN" if sur is None else ("BEAT" if sur>1 else ("MISS" if sur<-1 else "IN_LINE"))
 if fin["DataStatus"]!="EXTRACTED_SOURCE_VALIDATED" or c["Status"]=="ECONOMIC_MISMATCH" or fund=="UNKNOWN":ver,conf="UNKNOWN","LOW"
 elif (fund,exp) in {("POSITIVE","MISS"),("NEGATIVE","BEAT")}:ver,conf="MIXED","MEDIUM"
 else:ver,conf=fund,("HIGH" if (fund,exp) in {("POSITIVE","BEAT"),("NEGATIVE","MISS")} else "MEDIUM")
 return fund,exp,ver,conf
def detailed_xbrl_kpi_diagnostic(filing: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Diagnostic non persistant des faits dimensionnels candidats."""
    document = instance_doc(filing)
    if not document:
        return "", []
    root = ET.fromstring(sec_xml(f"{archive(filing)}/{document}"))
    ctx = contexts(root)
    name = KPI_MAP.get(filing["Ticker"], "")
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", name)
        if len(token) >= 3 and token.lower() not in {"revenue", "sales", "net"}
    }
    rows = []
    for fact in root.iter():
        context_ref = clean(fact.attrib.get("contextRef"))
        if not context_ref:
            continue
        value = ff(clean(fact.text))
        if value is None:
            continue
        context = ctx.get(context_ref, {})
        concept = local(fact.tag)
        dimensions = clean(context.get("D"))
        hits = sorted(token for token in tokens if token in f"{concept} {dimensions}".lower())
        if hits:
            rows.append({
                "Concept": concept,
                "Value": value,
                "Unit": clean(fact.attrib.get("unitRef")),
                "Start": clean(context.get("S")),
                "End": clean(context.get("E")),
                "Dimensions": dimensions,
                "Hits": ",".join(hits),
            })
    rows.sort(key=lambda row: (len(row["Hits"].split(",")), bool(row["Dimensions"]), row["End"]), reverse=True)
    return document, rows[:12]


def promote_dimensional_kpi(filing: dict[str, Any]) -> dict[str, Any]:
    """Interface explicite de la baseline KPI dimensionnelle."""
    return kpi(filing)


def fundamental_assessment(fin: dict[str, Any], guidance_status: str, key_kpi: dict[str, Any]) -> tuple[str, str]:
    if not fin["HasData"] and key_kpi["Status"] != "EXTRACTED_DIMENSIONAL":
        return "UNKNOWN", "Données fondamentales insuffisantes"
    positive = 0
    negative = 0
    evidence = []
    growth = fin.get("RevenueGrowthPct")
    if growth is not None:
        if growth > 1:
            positive += 1; evidence.append("REVENUE_GROWTH_UP")
        elif growth < -1:
            negative += 1; evidence.append("REVENUE_GROWTH_DOWN")
        else:
            evidence.append("REVENUE_GROWTH_STABLE")
    momentum = key_kpi.get("Momentum", "UNKNOWN")
    if momentum == "UP": positive += 1; evidence.append("KPI_UP")
    elif momentum == "DOWN": negative += 1; evidence.append("KPI_DOWN")
    elif momentum == "STABLE": evidence.append("KPI_STABLE")
    if guidance_status == "RAISED": positive += 1; evidence.append("GUIDANCE_RAISED")
    elif guidance_status in {"LOWERED", "WITHDRAWN"}: negative += 1; evidence.append(f"GUIDANCE_{guidance_status}")
    elif guidance_status == "REAFFIRMED": evidence.append("GUIDANCE_REAFFIRMED")
    if positive and not negative: status = "POSITIVE"
    elif negative and not positive: status = "NEGATIVE"
    elif positive and negative: status = "MIXED"
    else: status = "NEUTRAL"
    return status, "|".join(evidence) if evidence else "Aucun signal directionnel validé"


def expectation_assessment(corroboration: dict[str, Any]) -> tuple[str, str]:
    surprise = corroboration.get("RevenueSurprisePct")
    if surprise is None:
        return "UNKNOWN", "Consensus Revenue comparable indisponible"
    if surprise > 1: return "BEAT", f"REVENUE_SURPRISE={surprise:.4f}%"
    if surprise < -1: return "MISS", f"REVENUE_SURPRISE={surprise:.4f}%"
    return "IN_LINE", f"REVENUE_SURPRISE={surprise:.4f}%"


def overall_verdict(fin: dict[str, Any], corroboration: dict[str, Any], guidance_status: str, fundamental: str, expectation: str) -> tuple[str, str, str]:
    if fin["DataStatus"] != "EXTRACTED_SOURCE_VALIDATED":
        return "UNKNOWN", "DATA_STATUS_NOT_FULLY_VALIDATED", "LOW"
    if corroboration["Status"] == "ECONOMIC_MISMATCH":
        return "UNKNOWN", "ECONOMIC_MISMATCH_REQUIRES_REVIEW", "LOW"
    if guidance_status == "REVIEW_REQUIRED":
        return "UNKNOWN", "GUIDANCE_REQUIRES_REVIEW", "LOW"
    if fundamental == "UNKNOWN":
        return "UNKNOWN", "FUNDAMENTALS_INSUFFICIENT", "LOW"
    if (fundamental, expectation) in {("POSITIVE", "MISS"), ("NEGATIVE", "BEAT")}:
        return "MIXED", f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_{expectation}", "MEDIUM"
    if (fundamental, expectation) in {("POSITIVE", "BEAT"), ("NEGATIVE", "MISS")}:
        return fundamental, f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_{expectation}", "HIGH"
    return fundamental, f"FUNDAMENTALS_{fundamental}|EXPECTATIONS_{expectation}", "MEDIUM"


def europe_rows(watch,observed):
 out=[]
 for e in EUROPE_IR_EVENTS:
  matches=sorted(a for a in e["aliases"] if a in watch)
  if not matches:continue
  con=e["RevenueConsensus"];sur=None if con in (None,0) else (e["RevenueActual"]/con-1)*100;exp="UNKNOWN" if sur is None else ("BEAT" if sur>1 else ("MISS" if sur<-1 else "IN_LINE"));pos=int(e["RevenueGrowthPct"] is not None and e["RevenueGrowthPct"]>1)+int(e["KeyKPIMomentum"]=="UP");neg=int(e["RevenueGrowthPct"] is not None and e["RevenueGrowthPct"]<-1)+int(e["KeyKPIMomentum"]=="DOWN");fund="POSITIVE" if pos and not neg else ("NEGATIVE" if neg and not pos else ("MIXED" if pos and neg else "NEUTRAL"));ver="MIXED" if (fund,exp) in {("POSITIVE","MISS"),("NEGATIVE","BEAT")} else fund;conf="HIGH" if exp!="UNKNOWN" else "MEDIUM"
  out.append({"EventID":e["EventID"],"AgentVersion":AGENT_VERSION,"Ticker":matches[0],"ObservedAtUTC":observed,"PublishedAtUTC":e["PublishedAtUTC"],"EventType":e["EventType"],"Period":e["Period"],"RevenueActual":e["RevenueActual"],"RevenueConsensus":"" if con is None else con,"RevenuePrevious":"","RevenueSurprisePct":"" if sur is None else round(sur,6),"RevenueGrowthPct":"" if e["RevenueGrowthPct"] is None else e["RevenueGrowthPct"],"EPSActual":"" if e["EPSActual"] is None else e["EPSActual"],"EPSConsensus":"","EPSPrevious":"","EPSSurprisePct":"","GuidanceStatus":e["GuidanceStatus"],"KeyKPIName":e["KeyKPIName"],"KeyKPIActual":e["KeyKPIActual"],"KeyKPIConsensus":"","KeyKPIPrevious":"","KeyKPIMomentum":e["KeyKPIMomentum"],"FundamentalAssessment":fund,"ExpectationAssessment":exp,"OverallVerdict":ver,"Confidence":conf,"DataStatus":"EUROPE_IR_PRIMARY|CURATED_NORMALIZED|SHADOW","NeedsHumanReview":exp=="UNKNOWN","ObservationOnly":OBSERVATION_ONLY,"V1ExecutionChanged":V1_EXECUTION_CHANGED,"PortfolioV1Changed":PORTFOLIO_V1_CHANGED,"SourceType":"EUROPE_IR","SourceAuthority":"PRIMARY","SourceURL":e["SourceURL"],"Currency":e["Currency"]})
 return out
def merge(existing,current):
 d={clean(r.get("EventID")):r for r in existing if clean(r.get("EventID"))};d.update({r["EventID"]:r for r in current});return sorted(d.values(),key=lambda r:(clean(r.get("PublishedAtUTC")),clean(r.get("Ticker"))),reverse=True)
def main():
 required_markers = (
  "SECONDARY_BASIS_UNKNOWN",
  "EXTRACTED_DIMENSIONAL",
  "EUROPE_IR_EVENTS",
 )
 if not all(required_markers):
  raise RuntimeError("Baseline Earnings invalide")
 print("="*110,"\nEARNINGS & COMPANY AGENT\nSEC/XBRL US BASELINE COMPLETE + EUROPE IR ADDITIVE + VERDICT SHADOW\n"+"="*110);print("ObservationOnly :",OBSERVATION_ONLY,"\nV1ExecutionChanged :",V1_EXECUTION_CHANGED,"\nPortfolioV1Changed :",PORTFOLIO_V1_CHANGED)
 watch=set(load_tickers());sem=load_semantics(watch);fs=filings(watch);facts_cache={};fh_cache={};observed=datetime.now(timezone.utc).replace(microsecond=0).isoformat();out=[]
 for f in fs:
  cik=f["CIK"]
  if cik not in facts_cache:
   try:facts_cache[cik]=sec_json(f"{SEC_COMPANYFACTS_BASE}/CIK{cik}.json");time.sleep(REQUEST_PAUSE)
   except Exception as e:print(f["Ticker"],"SEC ERROR",e);facts_cache[cik]={}
  fin=financials(f,facts_cache[cik]);kp=promote_dimensional_kpi(f);g=guidance(f,sem);key=(f["Ticker"],f["PublishedAtUTC"][:10])
  if key not in fh_cache:
   try:fh_cache[key]=fh(*key);time.sleep(REQUEST_PAUSE)
   except Exception:fh_cache[key]=[]
  c=corr(f,fin,fh_cache[key])
  fund,fund_reason=fundamental_assessment(fin,g,kp)
  exp,exp_reason=expectation_assessment(c)
  ver,ver_reason,conf=overall_verdict(fin,c,g,fund,exp)
  rev,eps=fin["Revenue"],fin["EPS"]
  out.append({"EventID":f["EventID"],"AgentVersion":AGENT_VERSION,"Ticker":f["Ticker"],"ObservedAtUTC":observed,"PublishedAtUTC":f["PublishedAtUTC"],"EventType":f["Classification"],"Period":fin["Period"],"RevenueActual":"" if rev["Actual"] is None else rev["Actual"],"RevenueConsensus":"" if c["RevenueEstimate"] is None else c["RevenueEstimate"],"RevenuePrevious":"" if rev["Previous"] is None else rev["Previous"],"RevenueSurprisePct":"" if c["RevenueSurprisePct"] is None else round(c["RevenueSurprisePct"],6),"RevenueGrowthPct":"" if fin["RevenueGrowthPct"] is None else round(fin["RevenueGrowthPct"],6),"EPSActual":"" if eps["Actual"] is None else eps["Actual"],"EPSConsensus":"" if c["EPSEstimate"] is None else c["EPSEstimate"],"EPSPrevious":"" if eps["Previous"] is None else eps["Previous"],"EPSSurprisePct":"","GuidanceStatus":g,"KeyKPIName":kp["Name"],"KeyKPIActual":"" if kp["Actual"] is None else kp["Actual"],"KeyKPIConsensus":"","KeyKPIPrevious":"" if kp["Previous"] is None else kp["Previous"],"KeyKPIMomentum":kp["Momentum"],"FundamentalAssessment":fund,"ExpectationAssessment":exp,"OverallVerdict":ver,"Confidence":conf,"DataStatus":f"{fin['DataStatus']}|{c['Status']}|KPI_{kp['Status']}|FUND_{fund}|EXP_{exp}|VERDICT_{ver}","NeedsHumanReview":ver=="UNKNOWN","ObservationOnly":OBSERVATION_ONLY,"V1ExecutionChanged":V1_EXECUTION_CHANGED,"PortfolioV1Changed":PORTFOLIO_V1_CHANGED,"SourceType":"SEC_XBRL","SourceAuthority":"PRIMARY","SourceURL":f["FilingURL"],"Currency":"USD"})
 euro=europe_rows(watch,observed);out.extend(euro);print("\nPUBLICATIONS EUROPEENNES IR")
 for r in euro:print(r["Ticker"],r["PublishedAtUTC"],"Revenue",r["RevenueActual"],"Consensus",r["RevenueConsensus"],"SurprisePct",r["RevenueSurprisePct"],"Expectation",r["ExpectationAssessment"],"Verdict",r["OverallVerdict"])
 for p in EUROPE_PENDING:
  if any(a in watch for a in p["aliases"]):print(p["Ticker"],"PENDING",p["ScheduledDate"])
 merged=merge(read_csv(EARNINGS_RESULTS_FILE),out);write_csv(EARNINGS_RESULTS_FILE,merged);print("\nUS:",len(fs),"Europe:",len(euro),"Written:",len(out),"Total:",len(merged));print("Fichier :",EARNINGS_RESULTS_FILE);return 0
if __name__=="__main__":raise SystemExit(main())
