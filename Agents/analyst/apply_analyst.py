#!/usr/bin/env python3
"""Applique les corrections Analyst V1.4 sur Agents/analyst/analyst_agent.py."""
from pathlib import Path
import re

TARGET=Path('Agents/analyst/analyst_agent.py')
s=TARGET.read_text(encoding='utf-8')
backup=TARGET.with_name('analyst_agent.before_v14.py')
backup.write_text(s,encoding='utf-8')

s=s.replace("VERSION='ANALYST_V1_3'","VERSION='ANALYST_V1_4'",1)
s=s.replace("print('ANALYST AGENT V1.3')","print('ANALYST AGENT V1.4')",1)

# News Impact: mappe les classifications connues et utilise un fallback numérique explicite.
pattern=r"def news_assessment\(n\):.*?\ndef catalyst_assessment"
replacement='''def news_assessment(n):
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

def catalyst_assessment'''
s,count=re.subn(pattern,replacement,s,count=1,flags=re.S)
if count!=1:raise RuntimeError('Bloc news_assessment introuvable')

# Anti-double-comptage: UNKNOWN/NEUTRAL Earnings ne peut devenir directionnel
# uniquement grâce à ses propres sous-facteurs KPI/Revenue.
pattern=r"  # Preserve an upstream MIXED verdict.*?  else:overall='UNKNOWN'"
replacement='''  # Anti-double-counting entre verdict Earnings et sous-facteurs Earnings.
  independent_bull=any(x.startswith('NEWS_') or x.startswith('TECHNICAL_') for x in bull)
  independent_bear=any(x.startswith('NEWS_') or x.startswith('TECHNICAL_') for x in bear)
  earn_upper=earn.upper()
  if earn_upper=='MIXED' and not (independent_bull or independent_bear):overall='MIXED'
  elif earn_upper in {'UNKNOWN','NEUTRAL'} and not (independent_bull or independent_bear):overall='UNKNOWN'
  elif bull and bear:overall='MIXED'
  elif bull:overall='POSITIVE'
  elif bear:overall='NEGATIVE'
  else:overall='UNKNOWN' '''
s,count=re.subn(pattern,replacement,s,count=1,flags=re.S)
if count!=1:raise RuntimeError('Bloc OverallAssessment introuvable')

# Diagnostics du schéma News et des sélections Earnings critiques.
needle=" print(f\"structure_embedded_in_trade: {paths['trade'] if paths['trade'] else 'NOT_FOUND'} | tickers={sum(1 for r in data['trade'].values() if clean(r.get('StructureState') or r.get('StructureSignalType')))}\")"
addition=needle+"\n for ticker in ('TSLA','META','SAF.PA'):\n  erow=data['earnings'].get(ticker)\n  if erow:print(f\"latest_earnings[{ticker}]: PublishedAtUTC={clean(erow.get('PublishedAtUTC'))} | Period={clean(erow.get('Period'))} | Verdict={clean(erow.get('OverallVerdict'))}\")\n if paths['news']:\n  sample=next(iter(read_csv(paths['news'])),{})\n  print('news_columns :','|'.join(sample.keys()))"
if needle not in s:raise RuntimeError('Point diagnostic SOURCE COVERAGE introuvable')
s=s.replace(needle,addition,1)

compile(s,str(TARGET),'exec')
TARGET.write_text(s,encoding='utf-8')
print('Analyst V1.4 applique:',TARGET)
print('Backup:',backup)
print('Syntaxe: OK')
