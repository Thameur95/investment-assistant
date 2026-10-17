#!/usr/bin/env python3
"""Alertes immédiates Telegram + WhatsApp avec anti-doublon par canal."""
from __future__ import annotations
import csv, html, os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import pandas as pd
import requests

ROOT=Path(__file__).resolve().parent
DECISIONS=ROOT/'intraday_decisions.csv'
V1_PORTFOLIO=ROOT/'virtual_portfolio.csv'
STRUCTURE_EVENTS=ROOT/'historical_lab/results/structure_shadow_portfolio_events.csv'
STATE_FILE=ROOT/'intraday_alert_state.csv'
PARIS=ZoneInfo('Europe/Paris'); NOW_PARIS=datetime.now(PARIS); NOW_UTC=pd.Timestamp.now(tz='UTC')
STATE_FIELDS=['EventID','EventType','Ticker','Source','TelegramSentAtParis','TelegramMessageID','WhatsAppSentAtParis','WhatsAppMessageID','WhatsAppStatus']

def clean(v:Any)->str:return '' if v is None else str(v).strip()
def esc(v:Any)->str:return html.escape(clean(v))
def num(v:Any,d:int=2)->str:
    try:return f'{float(v):.{d}f}'
    except:return 'N/A'
def first(r:Dict[str,str],*names:str)->str:
    low={str(k).lower():v for k,v in r.items()}
    for n in names:
        v=r.get(n,low.get(n.lower(),''))
        if clean(v):return clean(v)
    return ''
def read(path:Path)->List[Dict[str,str]]:
    if not path.exists() or path.stat().st_size==0:return []
    with path.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def write_state(rows):
    with STATE_FILE.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=STATE_FIELDS,extrasaction='ignore');w.writeheader();w.writerows(rows)
def stamp(v:Any,zone='Europe/Paris')->Optional[pd.Timestamp]:
    if not clean(v):return None
    try:
        x=pd.Timestamp(v)
        return x.tz_localize(zone) if x.tzinfo is None else x
    except:return None
def fresh(v:Any,minutes:int,zone='Europe/Paris')->bool:
    x=stamp(v,zone)
    if x is None:return False
    age=(NOW_UTC-x.tz_convert('UTC')).total_seconds()/60
    return 0<=age<=minutes
def paris(v:Any,zone='Europe/Paris')->str:
    x=stamp(v,zone)
    return x.tz_convert('Europe/Paris').strftime('%d/%m/%Y à %H:%M Paris') if x is not None else 'heure inconnue'
def plain(text:str)->str:
    for tag in ('<b>','</b>','<i>','</i>'):text=text.replace(tag,'')
    return html.unescape(text)

def telegram_ready():return bool(clean(os.getenv('TELEGRAM_BOT_TOKEN')) and clean(os.getenv('TELEGRAM_CHAT_ID')))
def whatsapp_ready():return all(clean(os.getenv(x)) for x in ('WHATSAPP_ACCESS_TOKEN','WHATSAPP_PHONE_NUMBER_ID','WHATSAPP_RECIPIENT','WHATSAPP_TEMPLATE_NAME'))
def send_telegram(text:str)->str:
    r=requests.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/sendMessage',data={'chat_id':os.environ['TELEGRAM_CHAT_ID'],'text':text,'parse_mode':'HTML','disable_web_page_preview':True},timeout=30)
    body=r.json() if r.content else {}
    if r.status_code!=200 or not body.get('ok'):raise RuntimeError(r.text)
    return clean(body.get('result',{}).get('message_id'))
def send_whatsapp(text:str)->str:
    version=clean(os.getenv('WHATSAPP_GRAPH_VERSION')) or 'v24.0'
    url=f"https://graph.facebook.com/{version}/{clean(os.environ['WHATSAPP_PHONE_NUMBER_ID'])}/messages"
    payload={'messaging_product':'whatsapp','recipient_type':'individual','to':clean(os.environ['WHATSAPP_RECIPIENT']),'type':'template','template':{'name':clean(os.environ['WHATSAPP_TEMPLATE_NAME']),'language':{'code':clean(os.getenv('WHATSAPP_TEMPLATE_LANGUAGE')) or 'fr'},'components':[{'type':'body','parameters':[{'type':'text','text':plain(text)[:1024]}]}]}}
    r=requests.post(url,headers={'Authorization':'Bearer '+clean(os.environ['WHATSAPP_ACCESS_TOKEN']),'Content-Type':'application/json'},json=payload,timeout=30)
    body=r.json() if r.content else {}; msgs=body.get('messages') or []
    if r.status_code not in (200,201) or not msgs:raise RuntimeError(r.text)
    return clean(msgs[0].get('id'))

def events()->List[Dict[str,str]]:
    out=[]
    allowed={'ACHAT CONFIRME','ACHAT REACTIVE','ACHAT RÉACTIF','SETUP INITIAL INVALIDE','EXPIRE'}
    for r in read(DECISIONS):
        decision=first(r,'decision','Decision'); t=first(r,'ticker','Ticker'); dt=first(r,'decision_time','DecisionTime')
        if decision not in allowed or not fresh(dt,10):continue
        cur='EUR' if t.endswith(('.PA','.AS')) else 'USD'; eid=f"V1_DECISION|{t}|{first(r,'signal_date')}|{first(r,'session_date')}|{decision}"
        text=f"<b>📣 SIGNAL V1 | {esc(decision)}</b>\n\n<b>{esc(t)}</b>\nDécision : {paris(dt)}\nPrix de clôture/décision : <b>{num(first(r,'decision_close'))} {cur}</b>\nVWAP : {num(first(r,'vwap'))}\nOR haut : {num(first(r,'opening_range_high'))} {cur}\nOR bas : {num(first(r,'opening_range_low'))} {cur}\nMotif : {esc(first(r,'reason'))}\n\n<i>Paper trading uniquement.</i>"
        out.append({'EventID':eid,'EventType':'V1_DECISION','Ticker':t,'Source':'V1','Text':text})
    for r in read(V1_PORTFOLIO):
        t=first(r,'ticker','Ticker'); status=first(r,'status','Status').upper(); cur=first(r,'currency','Currency') or ('EUR' if t.endswith(('.PA','.AS')) else 'USD'); et=first(r,'entry_time','EntryTime','entry_date','EntryDate'); ep=first(r,'entry_price','EntryPrice'); q=first(r,'quantity','Quantity'); pid=first(r,'position_id','PositionID') or f'{t}|{et}|{ep}'
        if et and fresh(et,15):
            text=f"<b>🟢 ACHAT V1 EXÉCUTÉ</b>\n\n<b>{esc(t)}</b>\nHeure : {paris(et)}\nPrix : <b>{num(ep)} {esc(cur)}</b>\nQuantité : {esc(q)}\nMontant : {num(first(r,'total_buy_cost_eur','TotalBuyCostEUR'))} EUR\n🎯 TP : {num(first(r,'target_price','TargetPrice'))} {esc(cur)}\n🛡 SL : {num(first(r,'stop_price','StopPrice'))} {esc(cur)}\n\n<i>Exécution virtuelle V1.</i>"
            out.append({'EventID':f'V1_BUY|{pid}|{et}','EventType':'V1_BUY_EXECUTED','Ticker':t,'Source':'V1','Text':text})
        xt=first(r,'exit_time','ExitTime','exit_date','ExitDate')
        if status=='CLOSED' and xt and fresh(xt,15):
            text=f"<b>🔴 VENTE V1 EXÉCUTÉE</b>\n\n<b>{esc(t)}</b>\nHeure : {paris(xt)}\nEntrée : {num(ep)} {esc(cur)}\nSortie : <b>{num(first(r,'exit_price','ExitPrice'))} {esc(cur)}</b>\nMotif : {esc(first(r,'exit_reason','ExitReason'))}\nPnL : <b>{num(first(r,'realized_pnl_eur','RealizedPnLEUR'))} EUR</b>\nRendement : <b>{num(first(r,'return_pct','ReturnPct'))} %</b>\n\n<i>Exécution virtuelle V1.</i>"
            out.append({'EventID':f'V1_SELL|{pid}|{xt}','EventType':'V1_SELL_EXECUTED','Ticker':t,'Source':'V1','Text':text})
    for r in read(STRUCTURE_EVENTS):
        typ=first(r,'EventType'); tm=first(r,'EventTimeUTC')
        if typ not in {'SHADOW_BUY_EXECUTED','SHADOW_SELL_EXECUTED'} or not fresh(tm,15,'UTC'):continue
        t=first(r,'Ticker'); title='🟡 ACHAT VIRTUEL STRUCTURE SHADOW' if typ=='SHADOW_BUY_EXECUTED' else '🟡 VENTE VIRTUELLE STRUCTURE SHADOW'
        text=f"<b>{title}</b>\n\n<b>{esc(t)}</b>\nHeure : {paris(tm,'UTC')}\nPrix : <b>{num(first(r,'Price'))}</b>\nQuantité : {esc(first(r,'Quantity'))}\nMontant : {num(first(r,'AmountEUR'))} EUR\nDétail : {esc(first(r,'Reason'))}\n\n<i>Aucun ordre réel.</i>"
        out.append({'EventID':first(r,'EventID'),'EventType':typ,'Ticker':t,'Source':'STRUCTURE_SHADOW','Text':text})
    return out

def main()->int:
    if not telegram_ready() and not whatsapp_ready():print('Telegram et WhatsApp non configurés.');return 0
    state={}
    for r in read(STATE_FILE):
        eid=first(r,'EventID')
        if eid:state[eid]={'EventID':eid,'EventType':first(r,'EventType'),'Ticker':first(r,'Ticker'),'Source':first(r,'Source'),'TelegramSentAtParis':first(r,'TelegramSentAtParis','SentAtParis'),'TelegramMessageID':first(r,'TelegramMessageID'),'WhatsAppSentAtParis':first(r,'WhatsAppSentAtParis'),'WhatsAppMessageID':first(r,'WhatsAppMessageID'),'WhatsAppStatus':first(r,'WhatsAppStatus')}
    tg=wa=errors=0
    for e in events():
        row=state.setdefault(e['EventID'],{'EventID':e['EventID'],'EventType':e['EventType'],'Ticker':e['Ticker'],'Source':e['Source'],'TelegramSentAtParis':'','TelegramMessageID':'','WhatsAppSentAtParis':'','WhatsAppMessageID':'','WhatsAppStatus':''})
        if telegram_ready() and not clean(row['TelegramMessageID']):
            try:row['TelegramMessageID']=send_telegram(e['Text']);row['TelegramSentAtParis']=NOW_PARIS.isoformat(timespec='seconds');tg+=1
            except Exception as ex:errors+=1;print('TELEGRAM ERROR |',e['Ticker'],'|',ex)
        if whatsapp_ready() and not clean(row['WhatsAppMessageID']):
            try:row['WhatsAppMessageID']=send_whatsapp(e['Text']);row['WhatsAppSentAtParis']=NOW_PARIS.isoformat(timespec='seconds');row['WhatsAppStatus']='SENT';wa+=1
            except Exception as ex:errors+=1;row['WhatsAppStatus']='ERROR';print('WHATSAPP ERROR |',e['Ticker'],'|',ex)
    write_state(list(state.values()))
    print('Alertes Telegram immédiates envoyées :',tg);print('Alertes WhatsApp immédiates envoyées :',wa);print('Erreurs de messagerie :',errors);print('V1ExecutionChanged : False');print('PortfolioV1Changed : False');return 0
if __name__=='__main__':raise SystemExit(main())
