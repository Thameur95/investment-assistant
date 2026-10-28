#!/usr/bin/env python3
"""Construit le journal central Trade Intelligence enrichi Structure Shadow.

Correction audit 2026-10-28 :
- association Catalyst strictement temporelle, sans look-ahead ;
- une seule ligne terminale par Ticker + SessionDate ;
- aucune modification de V1, V2, Catalyst ou Structure Shadow.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "historical_lab" / "results"
OUT = RESULTS / "trade_intelligence_log.csv"
DECISIONS = ROOT / "intraday_decisions.csv"
SHADOW = RESULTS / "v2_shadow_rvol_log.csv"
CATALYST = ROOT / "historical_lab" / "reports" / "catalyst_semantic.csv"
PORTFOLIO = ROOT / "virtual_portfolio.csv"
HIST_TRADES = RESULTS / "risk_manager_source_trades.csv"
STRUCTURE_STATE = RESULTS / "structure_shadow_state.csv"
STRUCTURE_SIGNALS = RESULTS / "structure_shadow_signals.csv"
STRUCTURE_PORTFOLIO = RESULTS / "structure_shadow_portfolio.csv"
STRUCTURE_RESULTS = RESULTS / "structure_shadow_results.csv"

ACTIONABLE_NORMALIZED = {"ACHAT CONFIRME", "ACHAT REACTIVE", "ACHAT REACTIF"}
VERSION = "V1_V2SHADOW_CATALYST_STRUCTURE_3_NO_LOOKAHEAD"


def normalize_label(value) -> str:
    text = "" if value is None else str(value).strip().upper()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def is_actionable(value) -> bool:
    return normalize_label(value) in ACTIONABLE_NORMALIZED


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
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
    return df[c] if c is not None else pd.Series([default] * len(df), index=df.index)


def dates(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True).dt.strftime("%Y-%m-%d")


def times(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    return parsed.astype("string").replace("NaT", pd.NA)


def booleans(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().map({
        "true": True, "1": True, "yes": True, "oui": True,
        "false": False, "0": False, "no": False, "non": False,
    })


def normalise_decisions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ticker = col(df, ["ticker", "Ticker"])
    decision = col(df, ["decision", "Decision"])
    if ticker is None or decision is None:
        raise ValueError("intraday_decisions.csv doit contenir ticker et decision")
    x = df.copy()
    session = values(x, ["session_date", "SessionDate", "date", "Date_Paris"])
    dtime = values(x, ["decision_time", "DecisionTime", "datetime", "Datetime_Paris"])
    session_dates = dates(session)
    session_norm = session_dates.where(session_dates.notna(), dates(dtime))
    out = pd.DataFrame({
        "Ticker": x[ticker].astype(str).str.strip(),
        "SessionDate": session_norm,
        "DecisionTime": times(dtime),
        "V1Decision": x[decision].astype(str),
        "V1Reason": values(x, ["reason", "Reason", "motif", "decision_reason"], ""),
        "V1Status": np.where(x[decision].map(is_actionable), "ACHAT_V1", "REJET_V1"),
        "V1EntryPrice": pd.to_numeric(values(x, ["entry_price", "EntryPrice", "EffectiveEntryPrice", "decision_close"]), errors="coerce"),
    }).dropna(subset=["Ticker", "SessionDate"])
    out["_decision_ts"] = pd.to_datetime(out["DecisionTime"], errors="coerce", utc=True)
    out["_actionable"] = out["V1Decision"].map(is_actionable).astype(int)
    out = out.sort_values(["Ticker", "SessionDate", "_decision_ts", "_actionable"], na_position="first")
    return out.drop_duplicates(["Ticker", "SessionDate"], keep="last").drop(columns=["_decision_ts", "_actionable"])


def normalise_shadow(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or col(df, ["Ticker", "ticker"]) is None:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker"])
    return pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(),
        "SessionDate": dates(values(df, ["SessionDate", "session_date"])),
        "SignalDate": values(df, ["SignalDate"]),
        "RVOL_J1": pd.to_numeric(values(df, ["RVOL_J1"]), errors="coerce"),
        "V2ShadowAction": values(df, ["V2ShadowAction"]),
        "V2ShadowReason": values(df, ["V2ShadowReason"]),
        "WouldV2Reject": values(df, ["WouldV2Reject"]),
        "V2V1ExecutionChanged": values(df, ["V1ExecutionChanged"], False),
    }).drop_duplicates(["Ticker", "SessionDate"], keep="last")


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
    out["_CatalystEventTimestamp"] = pd.to_datetime(out["CatalystEventTime"], errors="coerce", utc=True)
    out = out.dropna(subset=["Ticker", "_CatalystEventTimestamp"])
    out = out.sort_values(["Ticker", "_CatalystEventTimestamp"])
    return out.drop_duplicates(["Ticker", "_CatalystEventTimestamp", "CatalystHeadline"], keep="last")


def merge_catalyst_asof(decisions: pd.DataFrame, catalyst: pd.DataFrame) -> pd.DataFrame:
    """Associe uniquement un Catalyst connu au moment de la décision.

    Sans DecisionTime, le cutoff conservateur est 00:00 UTC de SessionDate.
    """
    if decisions.empty or catalyst.empty:
        return decisions
    left = decisions.copy()
    right = catalyst.copy()
    left["_DecisionTimestamp"] = pd.to_datetime(left["DecisionTime"], errors="coerce", utc=True)
    session_cutoff = pd.to_datetime(left["SessionDate"], errors="coerce", utc=True)
    left["_CatalystCutoff"] = left["_DecisionTimestamp"].where(left["_DecisionTimestamp"].notna(), session_cutoff)
    right["_CatalystEventTimestamp"] = pd.to_datetime(right["_CatalystEventTimestamp"], errors="coerce", utc=True)
    left["_OriginalOrder"] = np.arange(len(left))
    valid_left = left.dropna(subset=["Ticker", "_CatalystCutoff"]).copy()
    invalid_left = left.loc[~left.index.isin(valid_left.index)].copy()
    valid_right = right.dropna(subset=["Ticker", "_CatalystEventTimestamp"]).copy()
    catalyst_columns = ["CatalystPriority", "CatalystSentiment", "CatalystConfidence", "CatalystHeadline", "CatalystSource", "CatalystEventTime"]
    valid_left = valid_left.sort_values(["_CatalystCutoff", "Ticker"])
    valid_right = valid_right.sort_values(["_CatalystEventTimestamp", "Ticker"])
    merged = pd.merge_asof(
        valid_left,
        valid_right[["Ticker", "_CatalystEventTimestamp", *catalyst_columns]],
        left_on="_CatalystCutoff",
        right_on="_CatalystEventTimestamp",
        by="Ticker",
        direction="backward",
        allow_exact_matches=True,
    )
    for column in catalyst_columns:
        if column not in invalid_left.columns:
            invalid_left[column] = np.nan
    combined = pd.concat([merged, invalid_left], ignore_index=True, sort=False)
    combined = combined.sort_values("_OriginalOrder").reset_index(drop=True)
    return combined.drop(columns=["_DecisionTimestamp", "_CatalystCutoff", "_CatalystEventTimestamp", "_OriginalOrder"], errors="ignore")


def normalise_results(df: pd.DataFrame, source_name: str, prefix: str = "") -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker", "Symbol", "symbol"])
    if ticker is None:
        return pd.DataFrame()
    session = values(df, ["entry_date", "EntryDate", "SessionDate", "session_date", "signal_date", "date", "Date"])
    entry_time = values(df, ["EntryTime", "entry_time", "decision_time", "DecisionTime", "entry_date"])
    exit_time = values(df, ["ExitTime", "exit_time", "exit_date", "ExitDate"])
    p = prefix
    result = pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(),
        "SessionDate": dates(session).where(dates(session).notna(), dates(entry_time)),
        f"{p}EntryTime": times(entry_time),
        f"{p}EntryPrice": pd.to_numeric(values(df, ["entry_price", "EntryPrice", "EffectiveEntryPrice", "BuyPrice"]), errors="coerce"),
        f"{p}ExitTime": times(exit_time),
        f"{p}ExitPrice": pd.to_numeric(values(df, ["exit_price", "ExitPrice", "EffectiveExitPrice", "SellPrice"]), errors="coerce"),
        f"{p}PositionStatus": values(df, ["status", "Status"]),
        f"{p}ExitReason": values(df, ["exit_reason", "ExitReason"]),
        f"{p}PnLEUR": pd.to_numeric(values(df, ["realized_pnl_eur", "RealizedPnLEUR", "realized_pnl", "PnLEUR", "pnl_eur", "PnL", "pnl"]), errors="coerce"),
        f"{p}ReturnPct": pd.to_numeric(values(df, ["ReturnPct", "return_pct", "NetReturnPct", "net_return_pct"]), errors="coerce"),
        f"{p}MFEPct": pd.to_numeric(values(df, ["MFEPct", "MFE_Pct", "MFE", "mfe_pct"]), errors="coerce"),
        f"{p}MAEPct": pd.to_numeric(values(df, ["MAEPct", "MAE_Pct", "MAE", "mae_pct"]), errors="coerce"),
        f"{p}ResultSource": source_name,
    })
    result["_complete"] = result.notna().sum(axis=1)
    result = result.sort_values(["Ticker", "SessionDate", "_complete"])
    return result.drop_duplicates(["Ticker", "SessionDate"], keep="last").drop(columns="_complete")


def normalise_structure_state(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or col(df, ["Ticker", "ticker"]) is None:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker"])
    mapping = {
        "StructureObservedAtUTC": (["LastObservedAtUTC", "ObservedAtUTC"], times),
        "StructureLastBarTime": (["LastBarTime", "BarTime"], times),
        "StructureState": (["StructureState"], None), "StructurePreviousState": (["PreviousStructureState"], None),
        "StructureLastPrice": (["LastPrice"], "num"), "StructureLocalSupport12": (["LocalSupport12"], "num"),
        "StructureLocalResistance12": (["LocalResistance12"], "num"), "StructureSessionSupport": (["SessionSupport"], "num"),
        "StructureSessionResistance": (["SessionResistance"], "num"), "StructureDailySupport20": (["DailySupport20"], "num"),
        "StructureDailyResistance20": (["DailyResistance20"], "num"), "StructureDailySupport60": (["DailySupport60"], "num"),
        "StructureDailyResistance60": (["DailyResistance60"], "num"), "StructureNextMajorResistance": (["NextMajorResistance"], "num"),
        "StructureRoomToMajorResistancePct": (["RoomToMajorResistancePct"], "num"), "StructureMajorResistanceTooClose": (["MajorResistanceTooClose"], booleans),
        "StructureVolumeRatio": (["VolumeRatio"], "num"), "StructureVolumeEligible": (["VolumeEligible"], booleans),
        "StructureVolumeConfirmed": (["VolumeConfirmed"], booleans), "StructureBreakoutLocal": (["BreakoutLocal"], booleans),
        "StructurePullbackCandidate": (["PullbackCandidate"], booleans), "StructureBreakoutLevel": (["PersistedBreakoutLevel"], "num"),
        "StructureBreakoutTime": (["PersistedBreakoutTime"], times), "StructureBreakoutConfirmed": (["PersistedBreakoutConfirmed"], booleans),
        "StructureBreakoutAgeSessions": (["BreakoutAgeSessions"], "num"), "StructureInvalidationLevel": (["InvalidationLevel"], "num"),
        "StructureV1StatusCurrentSession": (["V1StatusCurrentSession"], None), "StructureV1ContextSessionAligned": (["V1ContextSessionAligned"], booleans),
    }
    data = {"Ticker": df[ticker].astype(str).str.strip(), "SessionDate": dates(values(df, ["SessionDate", "session_date"]))}
    for name, (candidates, transform) in mapping.items():
        s = values(df, candidates)
        data[name] = pd.to_numeric(s, errors="coerce") if transform == "num" else (transform(s) if callable(transform) else s)
    out = pd.DataFrame(data)
    return out.dropna(subset=["Ticker", "SessionDate"]).drop_duplicates(["Ticker", "SessionDate"], keep="last")


def normalise_structure_signals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or col(df, ["Ticker", "ticker"]) is None:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker"])
    out = pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(), "SessionDate": dates(values(df, ["SessionDate", "session_date"])),
        "StructureSignalID": values(df, ["SignalID"]), "StructureAgentVersion": values(df, ["AgentVersion"]),
        "StructureSignalValidityPolicy": values(df, ["SignalValidityPolicy"]), "StructureSignalTime": times(values(df, ["SignalTime"])),
        "StructureSignalType": values(df, ["SignalType"]), "StructureSignalState": values(df, ["StructureState"]),
        "StructureSignalBreakoutLevel": pd.to_numeric(values(df, ["BreakoutLevel"]), errors="coerce"),
        "StructureSignalReferencePrice": pd.to_numeric(values(df, ["ReferencePrice"]), errors="coerce"),
        "StructureSignalStopPrice": pd.to_numeric(values(df, ["StopPrice"]), errors="coerce"),
        "StructureSignalTargetPrice": pd.to_numeric(values(df, ["TargetPrice"]), errors="coerce"),
        "StructureSignalRiskReward": pd.to_numeric(values(df, ["RiskReward"]), errors="coerce"),
        "StructureSignalVolumeRatio": pd.to_numeric(values(df, ["VolumeRatio"]), errors="coerce"),
        "StructureSignalFrozenBeforeOutcome": booleans(values(df, ["SignalFrozenBeforeOutcome"])),
    })
    out["_time"] = pd.to_datetime(out["StructureSignalTime"], errors="coerce", utc=True)
    out = out.sort_values(["Ticker", "SessionDate", "_time"], na_position="first")
    return out.drop_duplicates(["Ticker", "SessionDate"], keep="last").drop(columns="_time")


def normalise_structure_portfolio(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or col(df, ["Ticker", "ticker"]) is None:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker"])
    out = pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(), "SessionDate": dates(values(df, ["SignalSessionDate", "EntryDate", "SessionDate"])),
        "StructurePositionID": values(df, ["PositionID"]), "StructurePortfolioSignalID": values(df, ["SignalID"]),
        "StructurePortfolioStatus": values(df, ["Status", "status"]), "StructureEntryPolicy": values(df, ["EntryPolicy"]),
        "StructureEntryTime": times(values(df, ["EntryTime"])), "StructureEntryPrice": pd.to_numeric(values(df, ["EntryPrice"]), errors="coerce"),
        "StructureQuantity": pd.to_numeric(values(df, ["Quantity"]), errors="coerce"), "StructureStopPrice": pd.to_numeric(values(df, ["StopPrice"]), errors="coerce"),
        "StructureTargetPrice": pd.to_numeric(values(df, ["TargetPrice"]), errors="coerce"), "StructureInitialRiskReward": pd.to_numeric(values(df, ["InitialRiskReward"]), errors="coerce"),
        "StructureExitTime": times(values(df, ["ExitTime"])), "StructureExitPrice": pd.to_numeric(values(df, ["ExitPrice"]), errors="coerce"),
        "StructureExitReason": values(df, ["ExitReason"]), "StructureRealizedPnLEUR": pd.to_numeric(values(df, ["RealizedPnLEUR"]), errors="coerce"),
        "StructureReturnPct": pd.to_numeric(values(df, ["ReturnPct"]), errors="coerce"), "StructureMFEPct": pd.to_numeric(values(df, ["MFEPct"]), errors="coerce"),
        "StructureMAEPct": pd.to_numeric(values(df, ["MAEPct"]), errors="coerce"), "StructureHoldingSessions": pd.to_numeric(values(df, ["HoldingSessions"]), errors="coerce"),
        "StructureOvernightCount": pd.to_numeric(values(df, ["OvernightCount"]), errors="coerce"), "StructureSameBarAmbiguity": booleans(values(df, ["SameBarAmbiguity"])),
    })
    out["_complete"] = out.notna().sum(axis=1)
    out = out.sort_values(["Ticker", "SessionDate", "_complete"])
    return out.drop_duplicates(["Ticker", "SessionDate"], keep="last").drop(columns="_complete")


def merge_on_trade(base: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    return base if base.empty or extra.empty else base.merge(extra, on=["Ticker", "SessionDate"], how="left", suffixes=("", "_new"))


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


def main() -> None:
    decisions = normalise_decisions(read_csv(DECISIONS))
    if decisions.empty:
        print("TRADE INTELLIGENCE: aucune décision V1 à agréger.")
        if OUT.exists():
            print(f"Journal existant conservé: {OUT}")
        return

    current = merge_on_trade(decisions.copy(), normalise_shadow(read_csv(SHADOW)))
    current = merge_catalyst_asof(current, normalise_catalyst(read_csv(CATALYST)))
    current = merge_on_trade(current, normalise_results(read_csv(PORTFOLIO), "virtual_portfolio", "V1Result"))
    current = merge_on_trade(current, normalise_structure_state(read_csv(STRUCTURE_STATE)))
    current = merge_on_trade(current, normalise_structure_signals(read_csv(STRUCTURE_SIGNALS)))
    current = merge_on_trade(current, normalise_structure_portfolio(read_csv(STRUCTURE_PORTFOLIO)))
    current = merge_on_trade(current, normalise_results(read_csv(STRUCTURE_RESULTS), "structure_shadow_results", "StructureResult"))
    current = coalesce_duplicate_columns(current)
    current = merge_on_trade(current, normalise_results(read_csv(HIST_TRADES), "risk_manager_source_trades", "HistResult"))
    current = coalesce_duplicate_columns(current)

    current["TradeIntelligenceVersion"] = VERSION
    current["ObservationOnly"] = True
    current["V1ExecutionChanged"] = False
    current["StructureExecutionChangedV1"] = False
    current["CatalystDecisionPower"] = False
    current["UpdatedAtUTC"] = pd.Timestamp.now(tz="UTC").isoformat()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    previous = read_csv(OUT)
    combined = pd.concat([previous, current], ignore_index=True, sort=False)
    key = ["Ticker", "SessionDate"]
    for k in key:
        if k not in combined.columns:
            combined[k] = ""
    combined = combined.sort_values("UpdatedAtUTC", na_position="first")
    combined = combined.drop_duplicates(key, keep="last")

    preferred = [
        "TradeIntelligenceVersion", "Ticker", "SessionDate", "DecisionTime", "V1Status", "V1Decision", "V1Reason",
        "RVOL_J1", "V2ShadowAction", "V2ShadowReason", "WouldV2Reject", "CatalystPriority", "CatalystSentiment",
        "CatalystConfidence", "CatalystHeadline", "CatalystSource", "CatalystEventTime", "StructureState",
        "StructurePreviousState", "StructureObservedAtUTC", "StructureLastBarTime", "StructureLastPrice",
        "StructureLocalSupport12", "StructureLocalResistance12", "StructureSessionSupport", "StructureSessionResistance",
        "StructureDailySupport20", "StructureDailyResistance20", "StructureDailySupport60", "StructureDailyResistance60",
        "StructureNextMajorResistance", "StructureRoomToMajorResistancePct", "StructureMajorResistanceTooClose",
        "StructureVolumeRatio", "StructureVolumeEligible", "StructureVolumeConfirmed", "StructureBreakoutLocal",
        "StructurePullbackCandidate", "StructureBreakoutLevel", "StructureBreakoutTime", "StructureBreakoutConfirmed",
        "StructureBreakoutAgeSessions", "StructureInvalidationLevel", "StructureSignalID", "StructureAgentVersion",
        "StructureSignalValidityPolicy", "StructureSignalTime", "StructureSignalType", "StructureSignalState",
        "StructureSignalBreakoutLevel", "StructureSignalReferencePrice", "StructureSignalStopPrice",
        "StructureSignalTargetPrice", "StructureSignalRiskReward", "StructureSignalVolumeRatio",
        "StructureSignalFrozenBeforeOutcome", "StructurePositionID", "StructurePortfolioStatus", "StructureEntryPolicy",
        "StructureEntryTime", "StructureEntryPrice", "StructureQuantity", "StructureStopPrice", "StructureTargetPrice",
        "StructureInitialRiskReward", "StructureExitTime", "StructureExitPrice", "StructureExitReason",
        "StructureRealizedPnLEUR", "StructureReturnPct", "StructureMFEPct", "StructureMAEPct",
        "StructureHoldingSessions", "StructureOvernightCount", "V1ResultEntryPrice", "V1ResultPositionStatus",
        "V1ResultExitTime", "V1ResultExitPrice", "V1ResultExitReason", "V1ResultPnLEUR", "V1ResultReturnPct",
        "V1ResultMFEPct", "V1ResultMAEPct", "ObservationOnly", "V1ExecutionChanged",
        "StructureExecutionChangedV1", "CatalystDecisionPower", "UpdatedAtUTC",
    ]
    ordered = [c for c in preferred if c in combined.columns]
    ordered += [c for c in combined.columns if c not in ordered]
    combined[ordered].to_csv(OUT, index=False, encoding="utf-8")

    def count(name: str, dtype=object) -> int:
        return int(current.get(name, pd.Series(dtype=dtype)).notna().sum())

    print("=" * 100)
    print("TRADE INTELLIGENCE LOG")
    print("V1 + V2 SHADOW + CATALYST + STRUCTURE SHADOW")
    print("MODE OBSERVATION UNIQUEMENT - AUCUN IMPACT V1")
    print("POLITIQUE CATALYST : NO LOOK-AHEAD")
    print("=" * 100)
    print("Décisions V1 agrégées       :", len(current))
    print("Lignes journal total        :", len(combined))
    print("Avec V2 Shadow              :", count("V2ShadowAction"))
    print("Avec Catalyst               :", count("CatalystPriority"))
    print("Avec état Structure         :", count("StructureState"))
    print("Avec signal Structure       :", count("StructureSignalID"))
    print("Avec position Structure     :", count("StructurePositionID"))
    print("Avec résultat Structure/PnL :", count("StructureRealizedPnLEUR", float))
    print("Fichier                      :", OUT)
    print("V1ExecutionChanged           : False")
    print("CatalystDecisionPower        : False")
    print("=" * 100)


if __name__ == "__main__":
    main()
