#!/usr/bin/env python3
"""Construit le journal central Trade Intelligence enrichi Structure Shadow.

Sources optionnelles :
- intraday_decisions.csv
- historical_lab/results/v2_shadow_rvol_log.csv
- historical_lab/reports/catalyst_semantic.csv
- virtual_portfolio.csv
- historical_lab/results/risk_manager_source_trades.csv
- historical_lab/results/structure_shadow_state.csv
- historical_lab/results/structure_shadow_signals.csv
- historical_lab/results/structure_shadow_portfolio.csv
- historical_lab/results/structure_shadow_results.csv

Le script est tolerant aux fichiers et colonnes absents, idempotent et
strictement observationnel. Il ne modifie aucune source, décision ou exécution.
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

ACTIONABLE_NORMALIZED = {
    "ACHAT CONFIRME",
    "ACHAT REACTIVE",
    "ACHAT REACTIF",
}
VERSION = "V1_V2SHADOW_CATALYST_STRUCTURE_2"


def normalize_label(value) -> str:
    text = "" if value is None else str(value).strip().upper()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
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
    if c is None:
        return pd.Series([default] * len(df), index=df.index)
    return df[c]


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
    session_norm = dates(session).where(dates(session).notna(), dates(dtime))

    out = pd.DataFrame({
        "Ticker": x[ticker].astype(str).str.strip(),
        "SessionDate": session_norm,
        "DecisionTime": times(dtime),
        "V1Decision": x[decision].astype(str),
        "V1Reason": values(x, ["reason", "Reason", "motif", "decision_reason"], ""),
        "V1Status": np.where(x[decision].map(is_actionable), "ACHAT_V1", "REJET_V1"),
        "V1EntryPrice": pd.to_numeric(values(x, ["entry_price", "EntryPrice", "EffectiveEntryPrice", "decision_close"]), errors="coerce"),
    })
    out = out.dropna(subset=["Ticker", "SessionDate"])
    out = out.sort_values(["Ticker", "SessionDate", "DecisionTime"], na_position="first")
    return out.drop_duplicates(["Ticker", "SessionDate"], keep="last")


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
    rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    out["_priority"] = out["CatalystPriority"].astype(str).str.upper().map(rank).fillna(0)
    out["_confidence"] = out["CatalystConfidence"].fillna(-1)
    out = out.sort_values(["Ticker", "_priority", "_confidence", "CatalystEventTime"], ascending=[True, False, False, False], na_position="last")
    return out.drop_duplicates("Ticker", keep="first").drop(columns=["_priority", "_confidence"])


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
    result = result.sort_values(["Ticker", "SessionDate", "_complete"]).drop_duplicates(["Ticker", "SessionDate"], keep="last")
    return result.drop(columns="_complete")


def normalise_structure_state(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or col(df, ["Ticker", "ticker"]) is None:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker"])
    out = pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(),
        "SessionDate": dates(values(df, ["SessionDate", "session_date"])),
        "StructureObservedAtUTC": times(values(df, ["LastObservedAtUTC", "ObservedAtUTC"])),
        "StructureLastBarTime": times(values(df, ["LastBarTime", "BarTime"])),
        "StructureState": values(df, ["StructureState"]),
        "StructurePreviousState": values(df, ["PreviousStructureState"]),
        "StructureLastPrice": pd.to_numeric(values(df, ["LastPrice"]), errors="coerce"),
        "StructureLocalSupport12": pd.to_numeric(values(df, ["LocalSupport12"]), errors="coerce"),
        "StructureLocalResistance12": pd.to_numeric(values(df, ["LocalResistance12"]), errors="coerce"),
        "StructureSessionSupport": pd.to_numeric(values(df, ["SessionSupport"]), errors="coerce"),
        "StructureSessionResistance": pd.to_numeric(values(df, ["SessionResistance"]), errors="coerce"),
        "StructureDailySupport20": pd.to_numeric(values(df, ["DailySupport20"]), errors="coerce"),
        "StructureDailyResistance20": pd.to_numeric(values(df, ["DailyResistance20"]), errors="coerce"),
        "StructureDailySupport60": pd.to_numeric(values(df, ["DailySupport60"]), errors="coerce"),
        "StructureDailyResistance60": pd.to_numeric(values(df, ["DailyResistance60"]), errors="coerce"),
        "StructureNextMajorResistance": pd.to_numeric(values(df, ["NextMajorResistance"]), errors="coerce"),
        "StructureRoomToMajorResistancePct": pd.to_numeric(values(df, ["RoomToMajorResistancePct"]), errors="coerce"),
        "StructureMajorResistanceTooClose": booleans(values(df, ["MajorResistanceTooClose"])),
        "StructureVolumeRatio": pd.to_numeric(values(df, ["VolumeRatio"]), errors="coerce"),
        "StructureVolumeEligible": booleans(values(df, ["VolumeEligible"])),
        "StructureVolumeConfirmed": booleans(values(df, ["VolumeConfirmed"])),
        "StructureBreakoutLocal": booleans(values(df, ["BreakoutLocal"])),
        "StructurePullbackCandidate": booleans(values(df, ["PullbackCandidate"])),
        "StructureBreakoutLevel": pd.to_numeric(values(df, ["PersistedBreakoutLevel"]), errors="coerce"),
        "StructureBreakoutTime": times(values(df, ["PersistedBreakoutTime"])),
        "StructureBreakoutConfirmed": booleans(values(df, ["PersistedBreakoutConfirmed"])),
        "StructureBreakoutAgeSessions": pd.to_numeric(values(df, ["BreakoutAgeSessions"]), errors="coerce"),
        "StructureInvalidationLevel": pd.to_numeric(values(df, ["InvalidationLevel"]), errors="coerce"),
        "StructureV1StatusCurrentSession": values(df, ["V1StatusCurrentSession"]),
        "StructureV1ContextSessionAligned": booleans(values(df, ["V1ContextSessionAligned"])),
    })
    return out.dropna(subset=["Ticker", "SessionDate"]).drop_duplicates(["Ticker", "SessionDate"], keep="last")


def normalise_structure_signals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or col(df, ["Ticker", "ticker"]) is None:
        return pd.DataFrame()
    ticker = col(df, ["Ticker", "ticker"])
    out = pd.DataFrame({
        "Ticker": df[ticker].astype(str).str.strip(),
        "SessionDate": dates(values(df, ["SessionDate", "session_date"])),
        "StructureSignalID": values(df, ["SignalID"]),
        "StructureAgentVersion": values(df, ["AgentVersion"]),
        "StructureSignalValidityPolicy": values(df, ["SignalValidityPolicy"]),
        "StructureSignalTime": times(values(df, ["SignalTime"])),
        "StructureSignalType": values(df, ["SignalType"]),
        "StructureSignalState": values(df, ["StructureState"]),
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
        "Ticker": df[ticker].astype(str).str.strip(),
        "SessionDate": dates(values(df, ["SignalSessionDate", "EntryDate", "SessionDate"])),
        "StructurePositionID": values(df, ["PositionID"]),
        "StructurePortfolioSignalID": values(df, ["SignalID"]),
        "StructurePortfolioStatus": values(df, ["Status", "status"]),
        "StructureEntryPolicy": values(df, ["EntryPolicy"]),
        "StructureEntryTime": times(values(df, ["EntryTime"])),
        "StructureEntryPrice": pd.to_numeric(values(df, ["EntryPrice"]), errors="coerce"),
        "StructureQuantity": pd.to_numeric(values(df, ["Quantity"]), errors="coerce"),
        "StructureStopPrice": pd.to_numeric(values(df, ["StopPrice"]), errors="coerce"),
        "StructureTargetPrice": pd.to_numeric(values(df, ["TargetPrice"]), errors="coerce"),
        "StructureInitialRiskReward": pd.to_numeric(values(df, ["InitialRiskReward"]), errors="coerce"),
        "StructureExitTime": times(values(df, ["ExitTime"])),
        "StructureExitPrice": pd.to_numeric(values(df, ["ExitPrice"]), errors="coerce"),
        "StructureExitReason": values(df, ["ExitReason"]),
        "StructureRealizedPnLEUR": pd.to_numeric(values(df, ["RealizedPnLEUR"]), errors="coerce"),
        "StructureReturnPct": pd.to_numeric(values(df, ["ReturnPct"]), errors="coerce"),
        "StructureMFEPct": pd.to_numeric(values(df, ["MFEPct"]), errors="coerce"),
        "StructureMAEPct": pd.to_numeric(values(df, ["MAEPct"]), errors="coerce"),
        "StructureHoldingSessions": pd.to_numeric(values(df, ["HoldingSessions"]), errors="coerce"),
        "StructureOvernightCount": pd.to_numeric(values(df, ["OvernightCount"]), errors="coerce"),
        "StructureSameBarAmbiguity": booleans(values(df, ["SameBarAmbiguity"])),
    })
    out["_complete"] = out.notna().sum(axis=1)
    out = out.sort_values(["Ticker", "SessionDate", "_complete"])
    return out.drop_duplicates(["Ticker", "SessionDate"], keep="last").drop(columns="_complete")


def merge_on_trade(base: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    if base.empty or extra.empty:
        return base
    return base.merge(extra, on=["Ticker", "SessionDate"], how="left", suffixes=("", "_new"))


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

    current = decisions.copy()
    current = merge_on_trade(current, normalise_shadow(read_csv(SHADOW)))

    catalyst = normalise_catalyst(read_csv(CATALYST))
    if not catalyst.empty:
        current = current.merge(catalyst, on="Ticker", how="left")

    current = merge_on_trade(current, normalise_results(read_csv(PORTFOLIO), "virtual_portfolio", "V1Result"))
    current = merge_on_trade(current, normalise_structure_state(read_csv(STRUCTURE_STATE)))
    current = merge_on_trade(current, normalise_structure_signals(read_csv(STRUCTURE_SIGNALS)))
    current = merge_on_trade(current, normalise_structure_portfolio(read_csv(STRUCTURE_PORTFOLIO)))
    current = merge_on_trade(current, normalise_results(read_csv(STRUCTURE_RESULTS), "structure_shadow_results", "StructureResult"))
    current = coalesce_duplicate_columns(current)

    historical = normalise_results(read_csv(HIST_TRADES), "risk_manager_source_trades", "HistResult")
    current = merge_on_trade(current, historical)
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

    key = ["Ticker", "SessionDate", "V1Decision"]
    for k in key:
        if k not in combined.columns:
            combined[k] = ""
    combined = combined.sort_values("UpdatedAtUTC", na_position="first")
    combined = combined.drop_duplicates(key, keep="last")

    preferred = [
        "TradeIntelligenceVersion", "Ticker", "SessionDate", "DecisionTime",
        "V1Status", "V1Decision", "V1Reason", "RVOL_J1", "V2ShadowAction",
        "V2ShadowReason", "WouldV2Reject", "CatalystPriority", "CatalystSentiment",
        "CatalystConfidence", "CatalystHeadline", "CatalystSource", "CatalystEventTime",
        "StructureState", "StructurePreviousState", "StructureObservedAtUTC",
        "StructureLastBarTime", "StructureLastPrice", "StructureLocalSupport12",
        "StructureLocalResistance12", "StructureSessionSupport", "StructureSessionResistance",
        "StructureDailySupport20", "StructureDailyResistance20", "StructureDailySupport60",
        "StructureDailyResistance60", "StructureNextMajorResistance",
        "StructureRoomToMajorResistancePct", "StructureMajorResistanceTooClose",
        "StructureVolumeRatio", "StructureVolumeEligible", "StructureVolumeConfirmed",
        "StructureBreakoutLocal", "StructurePullbackCandidate", "StructureBreakoutLevel",
        "StructureBreakoutTime", "StructureBreakoutConfirmed", "StructureBreakoutAgeSessions",
        "StructureInvalidationLevel", "StructureSignalID", "StructureAgentVersion",
        "StructureSignalTime", "StructureSignalType", "StructureSignalReferencePrice",
        "StructureSignalStopPrice", "StructureSignalTargetPrice", "StructureSignalRiskReward",
        "StructurePositionID", "StructurePortfolioStatus", "StructureEntryPolicy",
        "StructureEntryTime", "StructureEntryPrice", "StructureQuantity",
        "StructureStopPrice", "StructureTargetPrice", "StructureInitialRiskReward",
        "StructureExitTime", "StructureExitPrice", "StructureExitReason",
        "StructureRealizedPnLEUR", "StructureReturnPct", "StructureMFEPct",
        "StructureMAEPct", "StructureHoldingSessions", "StructureOvernightCount",
        "V1ResultEntryPrice", "V1ResultPositionStatus", "V1ResultExitTime",
        "V1ResultExitPrice", "V1ResultExitReason", "V1ResultPnLEUR",
        "V1ResultReturnPct", "V1ResultMFEPct", "V1ResultMAEPct",
        "ObservationOnly", "V1ExecutionChanged", "StructureExecutionChangedV1",
        "CatalystDecisionPower", "UpdatedAtUTC",
    ]
    ordered = [c for c in preferred if c in combined.columns]
    ordered += [c for c in combined.columns if c not in ordered]
    combined = combined[ordered]
    combined.to_csv(OUT, index=False, encoding="utf-8")

    structure_state_count = int(current.get("StructureState", pd.Series(dtype=object)).notna().sum())
    structure_signal_count = int(current.get("StructureSignalID", pd.Series(dtype=object)).notna().sum())
    structure_position_count = int(current.get("StructurePositionID", pd.Series(dtype=object)).notna().sum())
    structure_result_count = int(current.get("StructureRealizedPnLEUR", pd.Series(dtype=float)).notna().sum())

    print("=" * 100)
    print("TRADE INTELLIGENCE LOG")
    print("V1 + V2 SHADOW + CATALYST + STRUCTURE SHADOW")
    print("MODE OBSERVATION UNIQUEMENT - AUCUN IMPACT V1")
    print("=" * 100)
    print("Décisions V1 agrégées       :", len(current))
    print("Lignes journal total        :", len(combined))
    print("Avec V2 Shadow              :", int(current.get("V2ShadowAction", pd.Series(dtype=object)).notna().sum()))
    print("Avec Catalyst               :", int(current.get("CatalystPriority", pd.Series(dtype=object)).notna().sum()))
    print("Avec état Structure         :", structure_state_count)
    print("Avec signal Structure       :", structure_signal_count)
    print("Avec position Structure     :", structure_position_count)
    print("Avec résultat Structure/PnL :", structure_result_count)
    print("Fichier                      :", OUT)
    print("V1ExecutionChanged           : False")
    print("CatalystDecisionPower        : False")
    print("=" * 100)


if __name__ == "__main__":
    main()
