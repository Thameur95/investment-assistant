#!/usr/bin/env python3
"""Analyst Agent V1 - synthèse multi-agents, Shadow only.

Lit les sorties persistées des agents existants et produit une synthèse
explicable par ticker. Ne modifie ni V1, ni portefeuille, ni décisions.
"""
from __future__ import annotations
import csv, json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[2]
RESULTS_DIR=Path(__file__).resolve().parent/'results'; RESULTS_DIR.mkdir(parents=True,exist_ok=True)
OUT=RESULTS_DIR/'analyst_observations.csv'
SUMMARY=RESULTS_DIR/'analyst_summary.json'
EARNINGS=ROOT/'Agents'/'earnings_company'/'results'/'earnings_company_events.csv'
NEWS=ROOT/'Agents'/'news_impact'/'results'/'news_impact_events.csv'
TRADE=ROOT/'trade_intelligence_log.csv'
STRUCTURE=ROOT/'historical_lab'/'results'/'structure_shadow_latest.json'
CATALYST=ROOT/'catalyst_semantic.csv'
VERSION='ANALYST_V1'; OBSERVATION_ONLY=True; V1_EXECUTION_CHANGED=False; PORTFOLIO_V1_CHANGED=False
COLS=['AnalystVersion','ObservedAtUTC','Ticker','EarningsAssessment','NewsAssessment','CatalystAssessment','TechnicalAssessment','V1Context','BullFactors','BearFactors','RiskFlags','MissingInformation','Contradictions','OverallAssessment','Confidence','NeedsReview','ObservationOnly','V1ExecutionChanged','PortfolioV1Changed']

def clean(v:Any)->str:return '' if v is None else ' '.join(str(v).split()).strip()
def rows(path:Path):
 if not path.exists() or path.stat().st_size==0:return []
 with path.open('r',encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def latest_by_ticker(data):
 out={}
 for r in data:
  t=clean(r.get('Ticker')).upper()
  if t:out[t]=r
 return out
def split_flags(v):return [x for x in clean(v).split('|') if x]
def uniq(xs):return list(dict.fromkeys(x for x in xs if x))
def classify(row):
 bull=[];bear=[];risk=[];missing=[];contr=[]
 earn=clean(row.get('OverallVerdict')).upper(); exp=clean(row.get('ExpectationAssessment')).upper(); trend=clean(row.get('KeyKPITrend')).upper(); market=clean(row.get('MarketReactionAssessment')).upper()
 risk+=split_flags(row.get('RiskFlags'));missing+=split_flags(row.get('MissingInformation'))
 if earn=='POSITIVE':bull.append('EARNINGS_POSITIVE')
 elif earn=='NEGATIVE':bear.append('EARNINGS_NEGATIVE')
 elif earn=='MIXED':bear.append('EARNINGS_MIXED')
 if exp=='BEAT':bull.append('REVENUE_BEAT')
 elif exp=='MISS':bear.append('REVENUE_MISS')
 if trend in {'GROWING'}:bull.append('KPI_GROWING')
 elif trend in {'DECLINING','DECELERATING_REPORTED_RATES'}:bear.append('KPI_WEAKENING')
 if market=='NEGATIVE':bear.append('MARKET_REACTION_NEGATIVE')
 if earn=='POSITIVE' and market=='NEGATIVE':contr.append('POSITIVE_FUNDAMENTALS_VS_NEGATIVE_MARKET')
 return bull,bear,risk,missing,contr

def main():
 observed=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
 earnings=latest_by_ticker(rows(EARNINGS));news=latest_by_ticker(rows(NEWS));trade=latest_by_ticker(rows(TRADE));cats=latest_by_ticker(rows(CATALYST))
 tickers=sorted(set(earnings)|set(news)|set(trade)|set(cats)); out=[]
 for t in tickers:
  e=earnings.get(t,{});n=news.get(t,{});tr=trade.get(t,{});c=cats.get(t,{})
  bull,bear,risk,missing,contr=classify(e) if e else ([],[],[],['EARNINGS_UNAVAILABLE'],[])
  news_ass=clean(n.get('ImpactAssessment') or n.get('OverallVerdict') or n.get('Sentiment')) or 'UNKNOWN'
  cat_ass=clean(c.get('CatalystType') or c.get('Category') or c.get('Sentiment')) or 'UNKNOWN'
  tech=clean(tr.get('StructureState') or tr.get('StructureSignalType')) or 'UNKNOWN'
  v1=clean(tr.get('V1Decision') or tr.get('V1Status')) or 'UNKNOWN'
  if news_ass.upper() in {'POSITIVE','BULLISH'}:bull.append('NEWS_POSITIVE')
  if news_ass.upper() in {'NEGATIVE','BEARISH'}:bear.append('NEWS_NEGATIVE')
  if tech=='UNKNOWN':missing.append('TECHNICAL_CONTEXT_UNAVAILABLE')
  if news_ass=='UNKNOWN':missing.append('NEWS_CONTEXT_UNAVAILABLE')
  if cat_ass=='UNKNOWN':missing.append('CATALYST_CONTEXT_UNAVAILABLE')
  bull,bear,risk,missing,contr=map(uniq,(bull,bear,risk,missing,contr))
  if bull and bear:assessment='MIXED'
  elif bull:assessment='POSITIVE'
  elif bear:assessment='NEGATIVE'
  else:assessment='UNKNOWN'
  confidence='LOW' if assessment=='UNKNOWN' else ('MEDIUM' if missing else 'HIGH')
  out.append({'AnalystVersion':VERSION,'ObservedAtUTC':observed,'Ticker':t,'EarningsAssessment':clean(e.get('OverallVerdict')) or 'UNKNOWN','NewsAssessment':news_ass,'CatalystAssessment':cat_ass,'TechnicalAssessment':tech,'V1Context':v1,'BullFactors':'|'.join(bull),'BearFactors':'|'.join(bear),'RiskFlags':'|'.join(risk),'MissingInformation':'|'.join(missing),'Contradictions':'|'.join(contr),'OverallAssessment':assessment,'Confidence':confidence,'NeedsReview':bool(missing or contr),'ObservationOnly':OBSERVATION_ONLY,'V1ExecutionChanged':V1_EXECUTION_CHANGED,'PortfolioV1Changed':PORTFOLIO_V1_CHANGED})
 with OUT.open('w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=COLS);w.writeheader();w.writerows(out)
 SUMMARY.write_text(json.dumps({'GeneratedAtUTC':observed,'TickersAnalyzed':len(out),'ObservationOnly':True,'V1ExecutionChanged':False,'PortfolioV1Changed':False},indent=2),encoding='utf-8')
 print('ANALYST AGENT V1');print('Tickers analyses :',len(out));print('Resultats :',OUT);print('ObservationOnly :',OBSERVATION_ONLY);return 0
if __name__=='__main__':raise SystemExit(main())
