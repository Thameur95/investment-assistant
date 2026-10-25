#!/usr/bin/env python3
"""Analyst Agent V1.4 - multi-agent synthesis, Shadow only."""
from __future__ import annotations
import csv, json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT=Path(__file__).resolve().parents[2]
RESULTS=Path(__file__).resolve().parent/'results'; RESULTS.mkdir(parents=True,exist_ok=True)
OUT=RESULTS/'analyst_observations.csv'; SUMMARY=RESULTS/'analyst_summary.json'
VERSION='ANALYST_V1_4'; OBSERVATION_ONLY=True; V1_EXECUTION_CHANGED=False; PORTFOLIO_V1_CHANGED=False
COLS=['AnalystVersion','ObservedAtUTC','Ticker','EarningsAssessment','NewsAssessment','CatalystAssessment','TechnicalAssessment','V1Context','BullFactors','BearFactors','RiskFlags','MissingInformation','Contradictions','OverallAssessment','Confidence','NeedsReview','ObservationOnly','V1ExecutionChanged','PortfolioV1Changed']
CANDIDATES={
 'earnings':['Agents/earnings_company/results/earnings_company_events.csv'],
 'news':['Agents/news_impact/results/news_impact_events.csv','Agents/news_impact/results/news_impact.csv'],
 'trade':['historical_lab/results/trade_intelligence_log.csv','trade_intelligence_log.csv','results/trade_intelligence_log.csv'],
 'structure':['historical_lab/results/structure_shadow_signals.csv','Agents/structure/results/structure_shadow_signals.csv','Agents/structure_shadow/results/structure_shadow_signals.csv'],
 'catalyst':['catalyst_semantic.csv','historical_lab/reports/catalyst_semantic.csv','historical_lab/results/catalyst_semantic.csv','Agents/catalyst/results/catalyst_semantic.csv'],
}
TIME_FIELDS=('PublishedAtUTC','SessionDate','SignalDate','DecisionTime','FirstSeenUTC','StructureObservedAtUTC','UpdatedAtUTC','EvaluatedAtUTC','ObservedAtUTC','Date')

def clean(v:Any)->str:return '' if v is None else ' '.join(str(v).split()).strip()
def read_csv(path:Path|None):
 if path is None or not path.exists() or path.stat().st_size==0:return []
 with path.open('r',encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def locate(kind):
 for rel in CANDIDATES[kind]:
  p=ROOT/rel
  if p.exists() and p.is_file():return p
 patterns={'earnings':'*earnings*events*.csv','news':'*news*impact*.csv','trade':'*trade*intelligence*.csv','structure':'*structure*shadow*signals*.csv','catalyst':'*catalyst*semantic*.csv'}
 matches=sorted(p for p in ROOT.rglob(patterns[kind]) if p.is_file() and '.git' not in p.parts and 'analyst' not in p.parts)
 return matches[0] if matches else None
def row_time(r):
 for field in TIME_FIELDS:
  value=clean(r.get(field))
  if value:return value
 return ''
def latest(data):
 out={}
 for r in data:
  t=clean(r.get('Ticker') or r.get('ticker') or r.get('Symbol')).upper()
  if t and (t not in out or row_time(r)>=row_time(out[t])):out[t]=r
 return out
def uniq(xs):return list(dict.fromkeys(x for x in xs if x))
def flags(v):return [x for x in clean(v).split('|') if x]
def first(r,keys,default='UNKNOWN'):
 for k in keys:
  v=clean(r.get(k))
  if v:return v
 return default

def earnings_factors(e):
 bull=[];bear=[];risk=flags(e.get('RiskFlags'));missing=flags(e.get('MissingInformation'));contr=[]
 verdict=clean(e.get('OverallVerdict')).upper();expect=clean(e.get('ExpectationAssessment')).upper();trend=clean(e.get('KeyKPITrend')).upper();market=clean(e.get('MarketReactionAssessment')).upper()
 if verdict=='POSITIVE':bull.append('EARNINGS_POSITIVE')
 elif verdict=='NEGATIVE':bear.append('EARNINGS_NEGATIVE')
 if expect=='BEAT':bull.append('REVENUE_BEAT')
 elif expect=='MISS':bear.append('REVENUE_MISS')
 if trend=='GROWING':bull.append('KPI_GROWING')
 elif trend in {'DECLINING','DECELERATING_REPORTED_RATES'}:bear.append('KPI_WEAKENING')
 if market=='POSITIVE':bull.append('MARKET_REACTION_POSITIVE')
 elif market=='NEGATIVE':bear.append('MARKET_REACTION_NEGATIVE')
 if verdict=='POSITIVE' and market=='NEGATIVE':contr.append('POSITIVE_FUNDAMENTALS_VS_NEGATIVE_MARKET')
 return bull,bear,risk,missing,contr

def news_assessment(n):
 if not n:return 'UNKNOWN','SOURCE_UNAVAILABLE'
 direct=first(n,('OverallVerdict','ImpactAssessment','NewsImpact','Impact','Sentiment','NewsSentiment','Direction','SemanticSentiment','ImpactDirection','MarketImpact','FinalSentiment'),default='')
 aliases={'BULLISH':'POSITIVE','BEARISH':'NEGATIVE','UP':'POSITIVE','DOWN':'NEGATIVE','POS':'POSITIVE','NEG':'NEGATIVE','MIXED':'MIXED','NEUTRAL':'NEUTRAL','POSITIVE':'POSITIVE','NEGATIVE':'NEGATIVE'}
 normalized=direct.upper()
 if normalized in aliases:return aliases[normalized],'OK'
 for key in ('AbnormalReturnPct','PriceReactionPct','ReactionPct','Return1DPct','ImpactScore'):
  raw=clean(n.get(key))
  if not raw:continue
  try:value=float(raw)
  except ValueError:continue
  if value>0:return 'POSITIVE','NUMERIC_FALLBACK'
  if value<0:return 'NEGATIVE','NUMERIC_FALLBACK'
  return 'NEUTRAL','NUMERIC_FALLBACK'
 return 'UNKNOWN','SCHEMA_UNMAPPED'

def catalyst_assessment(c,tr):
 source=c or tr
 if not source:return 'UNKNOWN','SOURCE_UNAVAILABLE'
 sentiment=first(source,('CatalystSentiment','Sentiment','SemanticSentiment'),default='')
 priority=first(source,('CatalystPriority','Priority'),default='')
 ctype=first(source,('CatalystType','Category'),default='')
 parts=[x for x in (sentiment,priority,ctype) if x]
 return ('|'.join(parts),'OK') if parts else ('UNKNOWN','SCHEMA_UNMAPPED')

def technical_assessment(st,tr):
 source=st or tr
 if not source:return 'UNKNOWN','SOURCE_UNAVAILABLE'
 value=first(source,('StructureState','StructureSignalType','StructureSignalState','OverallAssessment','StructureStatus'),default='')
 return (value,'OK') if value else ('UNKNOWN','NO_STRUCTURE_FOR_LATEST_ROW')

def v1_context(tr):
 if not tr:return 'UNKNOWN','SOURCE_UNAVAILABLE'
 value=first(tr,('V1Decision','V1Status','Decision','Action'),default='')
 return (value,'OK') if value else ('UNKNOWN','SCHEMA_UNMAPPED')

def main():
 observed=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
 paths={k:locate(k) for k in CANDIDATES};data={k:latest(read_csv(p)) for k,p in paths.items()}
 print('ANALYST AGENT V1.4');print('SOURCE COVERAGE')
 for k,p in paths.items():print(f"{k}: {p if p else 'NOT_FOUND'} | tickers={len(data[k])}")
 print(f"catalyst_embedded_in_trade: {paths['trade'] if paths['trade'] else 'NOT_FOUND'} | tickers={len(data['trade'])}")
 print(f"structure_embedded_in_trade: {paths['trade'] if paths['trade'] else 'NOT_FOUND'} | tickers={sum(1 for r in data['trade'].values() if clean(r.get('StructureState') or r.get('StructureSignalType')))}")
 for ticker in ('TSLA','META','SAF.PA'):
  erow=data['earnings'].get(ticker)
  if erow:print(f"latest_earnings[{ticker}]: PublishedAtUTC={clean(erow.get('PublishedAtUTC'))} | Period={clean(erow.get('Period'))} | Verdict={clean(erow.get('OverallVerdict'))}")
 if paths['news']:
  sample=next(iter(read_csv(paths['news'])),{})
  print('news_columns :','|'.join(sample.keys()))
 tickers=sorted(set().union(*(set(v) for v in data.values())));out=[]
 for t in tickers:
  e=data['earnings'].get(t);n=data['news'].get(t);c=data['catalyst'].get(t);tr=data['trade'].get(t);st=data['structure'].get(t)
  if e:bull,bear,risk,missing,contr=earnings_factors(e);earn=first(e,('OverallVerdict',))
  else:bull,bear,risk,missing,contr=[],[],[],['EARNINGS_UNAVAILABLE'],[];earn='UNKNOWN'
  news,news_status=news_assessment(n); catalyst,cat_status=catalyst_assessment(c,tr); technical,tech_status=technical_assessment(st,tr); v1,v1_status=v1_context(tr)
  if news=='POSITIVE':bull.append('NEWS_POSITIVE')
  elif news=='NEGATIVE':bear.append('NEWS_NEGATIVE')
  if news_status=='SOURCE_UNAVAILABLE':missing.append('NEWS_SOURCE_UNAVAILABLE')
  elif news_status=='SCHEMA_UNMAPPED':missing.append('NEWS_ASSESSMENT_UNMAPPED')
  if cat_status=='SOURCE_UNAVAILABLE':missing.append('CATALYST_SOURCE_UNAVAILABLE')
  elif cat_status=='SCHEMA_UNMAPPED':missing.append('CATALYST_ASSESSMENT_UNMAPPED')
  if tech_status=='SOURCE_UNAVAILABLE':missing.append('TECHNICAL_SOURCE_UNAVAILABLE')
  elif tech_status=='NO_STRUCTURE_FOR_LATEST_ROW':missing.append('TECHNICAL_CONTEXT_UNAVAILABLE_FOR_LATEST_ROW')
  if v1_status=='SOURCE_UNAVAILABLE':missing.append('TRADE_INTELLIGENCE_SOURCE_UNAVAILABLE')
  elif v1_status=='SCHEMA_UNMAPPED':missing.append('V1_CONTEXT_UNMAPPED')
  bull,bear,risk,missing,contr=map(uniq,(bull,bear,risk,missing,contr))
  independent_bull=any(x.startswith('NEWS_') or x.startswith('TECHNICAL_') for x in bull)
  independent_bear=any(x.startswith('NEWS_') or x.startswith('TECHNICAL_') for x in bear)
  earn_upper=earn.upper()
  if earn_upper=='MIXED' and not (independent_bull or independent_bear):overall='MIXED'
  elif earn_upper in {'UNKNOWN','NEUTRAL'} and not (independent_bull or independent_bear):overall='UNKNOWN'
  elif bull and bear:overall='MIXED'
  elif bull:overall='POSITIVE'
  elif bear:overall='NEGATIVE'
  else:overall='UNKNOWN'
  independent=sum(1 for src in (n,c or tr,st or tr) if src)
  confidence='LOW' if overall=='UNKNOWN' else ('HIGH' if independent>=3 and not missing else 'MEDIUM')
  out.append({'AnalystVersion':VERSION,'ObservedAtUTC':observed,'Ticker':t,'EarningsAssessment':earn,'NewsAssessment':news,'CatalystAssessment':catalyst,'TechnicalAssessment':technical,'V1Context':v1,'BullFactors':'|'.join(bull),'BearFactors':'|'.join(bear),'RiskFlags':'|'.join(risk),'MissingInformation':'|'.join(missing),'Contradictions':'|'.join(contr),'OverallAssessment':overall,'Confidence':confidence,'NeedsReview':bool(missing or contr),'ObservationOnly':OBSERVATION_ONLY,'V1ExecutionChanged':V1_EXECUTION_CHANGED,'PortfolioV1Changed':PORTFOLIO_V1_CHANGED})
 with OUT.open('w',encoding='utf-8',newline='') as f:w=csv.DictWriter(f,fieldnames=COLS);w.writeheader();w.writerows(out)
 SUMMARY.write_text(json.dumps({'GeneratedAtUTC':observed,'TickersAnalyzed':len(out),'Sources':{k:(str(p.relative_to(ROOT)) if p else 'NOT_FOUND') for k,p in paths.items()},'SourceTickerCounts':{k:len(v) for k,v in data.items()},'ObservationOnly':True,'V1ExecutionChanged':False,'PortfolioV1Changed':False},indent=2,ensure_ascii=False),encoding='utf-8')
 print('Tickers analyses :',len(out));print('Resultats :',OUT);print('ObservationOnly :',OBSERVATION_ONLY);return 0
if __name__=='__main__':raise SystemExit(main())
