#!/usr/bin/env python3
"""Challenger Factory V1.1 - hypothèses Shadow/OOS depuis Analyst Agent.

Sépare les audits de qualité de données des challengers trading.
Ne modifie jamais Champion V1, l'exécution ou le portefeuille.
"""
from __future__ import annotations
import csv, json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[2]
BASE=Path(__file__).resolve().parent
RESULTS=BASE/'results'; RESULTS.mkdir(parents=True,exist_ok=True)
ANALYST=ROOT/'Agents'/'analyst'/'results'/'analyst_observations.csv'
OUT=RESULTS/'challenger_hypotheses.csv'
SUMMARY=RESULTS/'challenger_factory_summary.json'
VERSION='CHALLENGER_FACTORY_V1_1'
OBSERVATION_ONLY=True; V1_EXECUTION_CHANGED=False; PORTFOLIO_V1_CHANGED=False
MIN_SAMPLE_FOR_TEST=5; MIN_SAMPLE_FOR_PROMOTION_REVIEW=20
COLUMNS=['FactoryVersion','GeneratedAtUTC','HypothesisID','Ticker','TriggerType','TriggerEvidence','Hypothesis','VariableUnderTest','ChampionRuleChanged','ExperimentMode','RequiredSample','CurrentSample','ExperimentStatus','EvaluationMetric','RiskFlags','MissingInformation','AnalystAssessment','AnalystConfidence','NeedsReview','ObservationOnly','V1ExecutionChanged','PortfolioV1Changed']

def clean(v:Any)->str:return '' if v is None else ' '.join(str(v).split()).strip()
def read_csv(p:Path):
 if not p.exists() or p.stat().st_size==0:return []
 with p.open('r',encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def tok(v):return {x for x in clean(v).split('|') if x}
def unique_join(items):return '|'.join(sorted({x for item in items for x in tok(item)}))
def slug(v):return '_'.join(''.join(c if c.isalnum() else ' ' for c in v.upper()).split())

def proposals(row):
 t=clean(row.get('Ticker')).upper();b=tok(row.get('BearFactors'));r=tok(row.get('RiskFlags'));m=tok(row.get('MissingInformation'));c=tok(row.get('Contradictions'));out=[]
 def add(trigger,evidence,hypothesis,var,metric,kind='TRADING_CHALLENGER'):
  out.append({'Ticker':t,'TriggerType':trigger,'TriggerEvidence':evidence,'Hypothesis':hypothesis,'VariableUnderTest':var,'EvaluationMetric':metric,'Kind':kind,'RiskFlags':clean(row.get('RiskFlags')),'MissingInformation':clean(row.get('MissingInformation')),'AnalystAssessment':clean(row.get('OverallAssessment')),'AnalystConfidence':clean(row.get('Confidence'))})
 if 'KPI_DECLINING' in r or 'KPI_WEAKENING' in b:add('KPI_WEAKENING','KPI_DECLINING|KPI_WEAKENING','Tester si un KPI métier en baisse ou en ralentissement dégrade le rendement post-signal par rapport aux cas sans affaiblissement KPI.','KPI_TREND_FILTER','ForwardReturn|WinRate|MaxAdverseExcursion')
 if 'AFTERMARKET_REPORTED_RATE_DECELERATION' in r:add('REPORTED_RATE_DECELERATION','AFTERMARKET_REPORTED_RATE_DECELERATION','Tester si une séquence décroissante de taux KPI publiés améliore l’identification des publications dont la réaction de marché est défavorable malgré des fondamentaux solides.','REPORTED_RATE_DECELERATION_FLAG','PostEarningsReturn|RelativeReturn|VolumeReaction')
 if 'POSITIVE_RESULTS_NEGATIVE_REACTION' in r or 'POSITIVE_FUNDAMENTALS_VS_NEGATIVE_MARKET' in c:add('MARKET_DIVERGENCE','POSITIVE_RESULTS_NEGATIVE_REACTION','Tester si une divergence fondamentaux positifs / réaction marché négative prédit une sous-performance à court terme.','MARKET_DIVERGENCE_FLAG','ForwardReturn1D|ForwardReturn3D|RelativeReturn')
 if 'REVENUE_MISS' in b:add('REVENUE_MISS','REVENUE_MISS','Tester si un Revenue MISS combiné au contexte technique actuel réduit la qualité des entrées V1.','REVENUE_MISS_CONTEXT','TradePnL|WinRate|MAE|MFE')
 if clean(row.get('OverallAssessment')).upper()=='UNKNOWN' and m:add('INSUFFICIENT_INFORMATION','|'.join(sorted(m)),'Mesurer si les verdicts UNKNOWN sont principalement dus à des sources absentes plutôt qu’à des signaux réellement neutres.','MISSING_INFORMATION_COVERAGE','CoverageRate|UnknownRate','DATA_QUALITY_AUDIT')
 return out

def main():
 now=datetime.now(timezone.utc).replace(microsecond=0).isoformat();analyst=read_csv(ANALYST)
 if not analyst:raise RuntimeError(f'Aucune observation Analyste disponible: {ANALYST}')
 raw=[]
 for row in analyst:raw.extend(proposals(row))
 groups={}
 for x in raw:groups.setdefault((x['Kind'],x['TriggerType'],x['VariableUnderTest']),[]).append(x)
 output=[]
 for i,((kind,trigger,var),items) in enumerate(sorted(groups.items()),1):
  sample=len(items);tickers=','.join(sorted({x['Ticker'] for x in items}))
  if kind=='DATA_QUALITY_AUDIT':status='DATA_QUALITY_AUDIT'; required=MIN_SAMPLE_FOR_TEST
  elif sample>=MIN_SAMPLE_FOR_PROMOTION_REVIEW:status='PROMOTION_REVIEW_ELIGIBLE';required=MIN_SAMPLE_FOR_TEST
  elif sample>=MIN_SAMPLE_FOR_TEST:status='TESTABLE';required=MIN_SAMPLE_FOR_TEST
  else:status='MORE_DATA';required=MIN_SAMPLE_FOR_TEST
  evidence=unique_join(x['TriggerEvidence'] for x in items)
  missing=unique_join(x['MissingInformation'] for x in items)
  risks=unique_join(x['RiskFlags'] for x in items)
  assessments={x['AnalystAssessment'] for x in items};conf={x['AnalystConfidence'] for x in items}
  output.append({'FactoryVersion':VERSION,'GeneratedAtUTC':now,'HypothesisID':f'CFV1-{i:03d}-{slug(var)}','Ticker':tickers,'TriggerType':trigger,'TriggerEvidence':evidence,'Hypothesis':items[0]['Hypothesis'],'VariableUnderTest':var,'ChampionRuleChanged':False,'ExperimentMode':'SHADOW_OOS' if kind=='TRADING_CHALLENGER' else 'DATA_QUALITY_SHADOW','RequiredSample':required,'CurrentSample':sample,'ExperimentStatus':status,'EvaluationMetric':items[0]['EvaluationMetric'],'RiskFlags':risks,'MissingInformation':missing,'AnalystAssessment':next(iter(assessments)) if len(assessments)==1 else 'MULTI','AnalystConfidence':next(iter(conf)) if len(conf)==1 else 'MULTI','NeedsReview':True,'ObservationOnly':OBSERVATION_ONLY,'V1ExecutionChanged':V1_EXECUTION_CHANGED,'PortfolioV1Changed':PORTFOLIO_V1_CHANGED})
 with OUT.open('w',encoding='utf-8',newline='') as f:w=csv.DictWriter(f,fieldnames=COLUMNS);w.writeheader();w.writerows(output)
 counts={}
 for x in output:counts[x['ExperimentStatus']]=counts.get(x['ExperimentStatus'],0)+1
 SUMMARY.write_text(json.dumps({'GeneratedAtUTC':now,'FactoryVersion':VERSION,'AnalystRowsRead':len(analyst),'HypothesesGenerated':len(output),'StatusCounts':counts,'MinimumSampleForTest':MIN_SAMPLE_FOR_TEST,'MinimumSampleForPromotionReview':MIN_SAMPLE_FOR_PROMOTION_REVIEW,'ObservationOnly':True,'V1ExecutionChanged':False,'PortfolioV1Changed':False},indent=2,ensure_ascii=False),encoding='utf-8')
 print('CHALLENGER FACTORY V1.1');print('Analyst rows read :',len(analyst));print('Hypotheses generated :',len(output))
 for x in output:print(x['HypothesisID'],'|',x['TriggerType'],'| sample=',x['CurrentSample'],'|',x['ExperimentStatus'])
 print('ObservationOnly :',OBSERVATION_ONLY);print('V1ExecutionChanged :',V1_EXECUTION_CHANGED);print('PortfolioV1Changed :',PORTFOLIO_V1_CHANGED);print('Results :',OUT);return 0
if __name__=='__main__':raise SystemExit(main())
