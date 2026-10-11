#!/usr/bin/env python3
"""Construit le journal central Trade Intelligence.

Sources optionnelles:
- intraday_decisions.csv
- historical_lab/results/v2_shadow_rvol_log.csv
- historical_lab/reports/catalyst_semantic.csv
- virtual_portfolio.csv
- historical_lab/results/risk_manager_source_trades.csv

Le script est tolerant aux colonnes absentes et idempotent. Il ne modifie
aucune source et n'a aucun impact sur les decisions ou l'execution V1.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "historical_lab" / "results" / "trade_intelligence_log.csv"

DECISIONS = ROOT / "intraday_decisions.csv"
SHADOW = ROOT / "historical_lab" / "results" / "v2_shadow_rvol_log.csv"
CATALYST = ROOT / "historical_lab" / "reports" / "catalyst_semantic.csv"
PORTFOLIO = ROOT / "virtual_portfolio.csv"
HIST_TRADES = ROOT / "historical_lab" / "results" / "risk_manager_source_trades.csv"

ACTIONABLE = {"ACHAT CONFIRME", "ACHAT REACTIVE"}


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    lookup = {str(c).lower(): c for c in df.columns}
    for name in candidates:
        if name in df.columns:
            return name
        found = lookup.get(name.lower())
        if found is not None:
            return found
    return None


def values(df: pd.DataFrame, candidates: Iterable[str], default=np.nan) -> pd.Series:
    c = col(df, candidates)
    if c is None:
        return pd.Series([default] * len(df), index=df.index)
    return df[c]


def dates(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True).dt.strftime("%Y-%m-%d")


def times(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True).astype("string")


def normalise_decisions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ticker = col(df, ["ticker", "Ticker"])
    decision = col(df, ["decision", "Decision"])
    if ticker is None or decision is None:
        raise ValueError("intraday_decisions.csv doit contenir ticker et decision")

    x = df.copy()
    x = x.loc[x[decision].astype(str).isin(ACTIONABLE)].copy()
    if x.empty:
        return pd.DataFrame()

    session = values(x, ["session_date", "SessionDate", "date", "Date_Paris"])
    dtime = values(x, ["decision_time", "DecisionTime", "datetime", "Datetime_Paris"])
    session_norm = dates(session)
    # si la date de session n'existe pas, la deriver de DecisionTime
    fallback = dates(dtime)
    session_norm = session_norm.where(session_norm.notna(), fallback)

    return pd.DataFrame({
        "Ticker": x[ticker].astype(str).str.strip(),
        "SessionDate": session_norm,
        "DecisionTime": times(dtime),
        "V1Decision": x[decision].astype(str),
        "V1Reason": values(x, ["reason", "Reason", "motif", "decision_reason"], ""),
        "V1EntryPrice": pd.to_numeric(values(x, ["entry_price", "EntryPrice", "EffectiveEntryPrice"]), errors="coerce"),
    })


def normalise_shadow(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker"])
    if ticker is None:
        return pd.DataFrame()
    return pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(),
        "SessionDate": dates(values(df, ["SessionDate", "session_date"])),
        "DecisionTime": times(values(df, ["DecisionTime", "decision_time"])),
        "SignalDate": values(df, ["SignalDate"]),
        "RVOL_J1": pd.to_numeric(values(df, ["RVOL_J1"]), errors="coerce"),
        "V2ShadowAction": values(df, ["V2ShadowAction"]),
        "V2ShadowReason": values(df, ["V2ShadowReason"]),
        "WouldV2Reject": values(df, ["WouldV2Reject"]),
        "V1ExecutionChanged": values(df, ["V1ExecutionChanged"], False),
    })


def normalise_catalyst(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker", "symbol", "Symbol"])
    if ticker is None:
        return pd.DataFrame()

    out = pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(),
        "CatalystPriority": values(df, ["Importance", "importance", "Priority", "priority", "CatalystPriority"]),
        "CatalystSentiment": values(df, ["Direction", "direction", "Sentiment", "sentiment", "CatalystSentiment"]),
        "CatalystConfidence": pd.to_numeric(values(df, ["Confidence", "confidence", "CatalystConfidence"]), errors="coerce"),
        "CatalystHeadline": values(df, ["RepresentativeTitle", "Headline", "headline", "title", "Title", "CatalystHeadline"]),
        "CatalystSource": values(df, ["Sources", "Source", "source", "provider", "Provider"]),
        "CatalystEventTime": times(values(df, ["LastSeenUTC", "FirstSeenUTC", "ClassifiedAtUTC", "PublishedAt", "published_at", "datetime", "Datetime", "Date", "date"])),
    })

    # Une seule observation informative par ticker: priorite la plus forte,
    # puis confiance, puis evenement le plus recent.
    rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    out["_PriorityRank"] = out["CatalystPriority"].astype(str).str.upper().map(rank).fillna(0)
    out["_ConfidenceRank"] = out["CatalystConfidence"].fillna(-1)
    out = out.sort_values(
        ["Ticker", "_PriorityRank", "_ConfidenceRank", "CatalystEventTime"],
        ascending=[True, False, False, False],
        na_position="last",
    ).drop_duplicates("Ticker", keep="first")
    return out.drop(columns=["_PriorityRank", "_ConfidenceRank"])


def normalise_results(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker", "Symbol", "symbol"])
    if ticker is None:
        return pd.DataFrame()

    session = values(df, ["entry_date", "SessionDate", "session_date", "signal_date", "date", "Date"])
    decision_time = values(df, ["decision_time", "DecisionTime", "entry_date", "EntryTime", "entry_time"])
    result = pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(),
        "SessionDate": dates(session).where(dates(session).notna(), dates(decision_time)),
        "ResultEntryPrice": pd.to_numeric(values(df, ["entry_price", "EffectiveEntryPrice", "EntryPrice", "BuyPrice"]), errors="coerce"),
        "ExitPrice": pd.to_numeric(values(df, ["exit_price", "EffectiveExitPrice", "ExitPrice", "SellPrice"]), errors="coerce"),
        "PositionStatus": values(df, ["status", "Status"]),
        "ExitDate": values(df, ["exit_date", "ExitDate"]),
        "ExitReason": values(df, ["exit_reason", "ExitReason"]),
        "PnLEUR": pd.to_numeric(values(df, ["realized_pnl_eur", "realized_pnl", "PnLEUR", "pnl_eur", "PnL", "pnl"]), errors="coerce"),
        "ReturnPct": pd.to_numeric(values(df, ["ReturnPct", "return_pct", "NetReturnPct", "net_return_pct"]), errors="coerce"),
        "MFEPct": pd.to_numeric(values(df, ["MFEPct", "MFE_Pct", "MFE", "mfe_pct"]), errors="coerce"),
        "MAEPct": pd.to_numeric(values(df, ["MAEPct", "MAE_Pct", "MAE", "mae_pct"]), errors="coerce"),
        "ResultSource": source_name,
    })
    # Ne considerer le PnL realise que pour une position effectivement cloturee.
    status_upper = result["PositionStatus"].astype(str).str.upper()
    closed_mask = (
        result["ExitDate"].notna()
        | result["ExitPrice"].notna()
        | result["PnLEUR"].notna()
        | status_upper.isin({"CLOSED", "CLOSE", "FERME", "FERMEE", "TERMINE", "EXITED", "SOLD"})
    )
    result.loc[~closed_mask, ["ExitPrice", "ExitReason", "PnLEUR", "ReturnPct", "MFEPct", "MAEPct"]] = np.nan

    # Si le portefeuille ne fournit pas ReturnPct, calculer le rendement prix
    # seulement lorsqu'entree et sortie sont connues. Le PnL EUR reste celui
    # fourni par virtual_portfolio.py afin de conserver frais/FX du moteur.
    missing_return = result["ReturnPct"].isna() & result["ResultEntryPrice"].notna() & result["ExitPrice"].notna()
    result.loc[missing_return, "ReturnPct"] = (
        result.loc[missing_return, "ExitPrice"] / result.loc[missing_return, "ResultEntryPrice"] - 1.0
    ) * 100.0

    # conserver la ligne la plus complete par ticker/session
    result["_complete"] = result.notna().sum(axis=1)
    result = result.sort_values(["Ticker", "SessionDate", "_complete"]).drop_duplicates(
        ["Ticker", "SessionDate"], keep="last"
    )
    return result.drop(columns="_complete")


def merge_by_trade(base: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    if base.empty or extra.empty:
        return base
    keys = ["Ticker", "SessionDate"]
    # Si DecisionTime est disponible des deux cotes, l'utiliser aussi.
    if "DecisionTime" in extra.columns and extra["DecisionTime"].notna().any():
        keys.append("DecisionTime")
    return base.merge(extra, on=keys, how="left", suffixes=("", "_new"))


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    for c in list(df.columns):
        if not c.endswith("_new"):
            continue
        base = c[:-4]
        if base in df.columns:
            df[base] = df[base].where(df[base].notna(), df[c])
            df = df.drop(columns=c)
        else:
            df = df.rename(columns={c: base})
    return df


def main():
    decisions = normalise_decisions(read_csv(DECISIONS))
    if decisions.empty:
        print("TRADE INTELLIGENCE: aucune decision V1 actionnable a agreger.")
        if OUT.exists():
            print(f"Journal existant conserve: {OUT}")
        return

    shadow = normalise_shadow(read_csv(SHADOW))
    catalyst = normalise_catalyst(read_csv(CATALYST))
    portfolio = normalise_results(read_csv(PORTFOLIO), "virtual_portfolio")
    historical = normalise_results(read_csv(HIST_TRADES), "risk_manager_source_trades")

    current = decisions.copy()
    current = merge_by_trade(current, shadow)
    if not catalyst.empty:
        current = current.merge(catalyst, on="Ticker", how="left")
    current = merge_by_trade(current, portfolio)
    current = coalesce_duplicate_columns(current)

    # Le fichier historique sert de complement diagnostique si une info de sortie
    # est disponible pour le meme ticker/session.
    if not historical.empty:
        h = historical.rename(columns={
            "ResultEntryPrice": "HistEntryPrice",
            "ExitPrice": "HistExitPrice",
            "ExitReason": "HistExitReason",
            "PnLEUR": "HistPnLEUR",
            "ReturnPct": "HistReturnPct",
            "MFEPct": "HistMFEPct",
            "MAEPct": "HistMAEPct",
            "ResultSource": "HistResultSource",
            "PositionStatus": "HistPositionStatus",
            "ExitDate": "HistExitDate",
        })
        current = current.merge(h, on=["Ticker", "SessionDate"], how="left")
        fill_map = {
            "ResultEntryPrice": "HistEntryPrice", "ExitPrice": "HistExitPrice",
            "ExitReason": "HistExitReason", "PnLEUR": "HistPnLEUR",
            "ReturnPct": "HistReturnPct", "MFEPct": "HistMFEPct",
            "MAEPct": "HistMAEPct", "ResultSource": "HistResultSource",
            "PositionStatus": "HistPositionStatus", "ExitDate": "HistExitDate",
            "PositionStatus": "HistPositionStatus",
            "ExitDate": "HistExitDate",
        }
        for target, source in fill_map.items():
            if target not in current.columns:
                current[target] = np.nan
            current[target] = current[target].where(current[target].notna(), current[source])
        current = current.drop(columns=list(fill_map.values()), errors="ignore")

    if "ResultEntryPrice" not in current.columns:
        current["ResultEntryPrice"] = np.nan
    current["EntryPrice"] = current["ResultEntryPrice"].where(
        current["ResultEntryPrice"].notna(), current["V1EntryPrice"]
    )
    current = current.drop(columns=["ResultEntryPrice", "V1EntryPrice"], errors="ignore")

    current["TradeIntelligenceVersion"] = "V1_V2SHADOW_CATALYST_1"
    current["ObservationOnly"] = True
    current["UpdatedAtUTC"] = pd.Timestamp.now(tz="UTC").isoformat()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    previous = read_csv(OUT)
    combined = pd.concat([previous, current], ignore_index=True, sort=False)

    # Cle stable: ne jamais creer plusieurs lignes pour le meme signal.
    key = ["Ticker", "SessionDate", "DecisionTime", "V1Decision"]
    for k in key:
        if k not in combined.columns:
            combined[k] = ""
    combined = combined.sort_values("UpdatedAtUTC", na_position="first")
    combined = combined.drop_duplicates(key, keep="last")

    preferred = [
        "TradeIntelligenceVersion", "Ticker", "SessionDate", "SignalDate",
        "DecisionTime", "V1Decision", "V1Reason", "RVOL_J1",
        "V2ShadowAction", "V2ShadowReason", "WouldV2Reject",
        "V1ExecutionChanged", "CatalystPriority", "CatalystSentiment",
        "CatalystConfidence", "CatalystHeadline", "CatalystSource",
        "CatalystEventTime", "EntryPrice", "PositionStatus", "ExitDate", "ExitPrice", "ExitReason",
        "PnLEUR", "ReturnPct", "MFEPct", "MAEPct", "ResultSource",
        "ObservationOnly", "UpdatedAtUTC",
    ]
    ordered = [c for c in preferred if c in combined.columns]
    ordered += [c for c in combined.columns if c not in ordered]
    combined = combined[ordered]
    combined.to_csv(OUT, index=False, encoding="utf-8")

    print("=" * 100)
    print("TRADE INTELLIGENCE LOG")
    print("MODE OBSERVATION UNIQUEMENT - AUCUN IMPACT V1")
    print("=" * 100)
    print("Decisions V1 agregees :", len(current))
    print("Lignes journal total  :", len(combined))
    print("Avec V2 Shadow        :", int(current.get("V2ShadowAction", pd.Series(dtype=object)).notna().sum()))
    print("Avec catalyseur       :", int(current.get("CatalystPriority", pd.Series(dtype=object)).notna().sum()))
    print("Avec resultat/PnL     :", int(current.get("PnLEUR", pd.Series(dtype=float)).notna().sum()))
    print("Fichier                :", OUT)
    print("V1ExecutionChanged     : False")
    print("=" * 100)


if __name__ == "__main__":
    main()
