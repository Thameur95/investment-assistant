#!/usr/bin/env python3
"""
validate_v2_full.py

Validation automatisee Champion V1 contre Challenger V2 RVOL.

Regle officielle figee:
    RVOL_J1 < 0.60 -> rejet avant entree

Le script:
1. charge le dataset de decouverte actuel;
2. decouvre automatiquement les datasets OOS optionnels;
3. reconstruit V1 et V2 chronologiquement;
4. controle la baseline officielle sur le dataset de decouverte;
5. calcule robustesse des seuils, blocs temporels, concentration et bootstrap;
6. genere un verdict automatique sans modifier le moteur reel.

Dossiers OOS reconnus automatiquement:
    historical_lab/data/oos_v2/**/risk_manager_source_trades.csv
    historical_lab/results/oos_v2/**/risk_manager_source_trades.csv

IMPORTANT:
Un resultat obtenu uniquement sur le dataset de decouverte ne peut jamais
promouvoir V2. Sans donnees OOS independantes, le verdict reste A CONFIRMER.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


# ============================================================================
# CONFIGURATION FIGEE
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = BASE_DIR / "results"
DISCOVERY_FILE = RESULTS_DIR / "risk_manager_source_trades.csv"
OFFICIAL_SUMMARY_FILE = RESULTS_DIR / "risk_manager_summary.csv"

OOS_PATTERNS = [
    BASE_DIR / "data" / "oos_v2",
    BASE_DIR / "results" / "oos_v2",
]

OUTPUT_DIR = RESULTS_DIR / "v2_full"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MODEL = "FIXED_1000"
INITIAL_CAPITAL = 3000.0
MAX_POSITIONS = 3
MAX_SHARE_PRICE = 2000.0
FIXED_BUDGET = 1000.0
FEE_SIDE_PCT = 0.05

OFFICIAL_RVOL_THRESHOLD = 0.60
SENSITIVITY_THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70]
BASELINE_TOLERANCE_EUR = 0.02
BOOTSTRAP_ITERATIONS = 5000
BOOTSTRAP_SEED = 20261011

MIN_OOS_EXECUTED_TRADES = 100
MIN_OOS_FILTERED_TRADES = 20
MAX_FILTERED_WIN_RATE_PCT = 35.0
MAX_ALLOWED_BLOCK_DAMAGE_PCT = -2.0
MAX_ALLOWED_DD_DEGRADATION_POINT = -0.5

REQUIRED_COLUMNS = {
    "Ticker", "SessionDate", "Decision", "DecisionTime", "TradeStatus",
    "ExitTime", "ExitReason", "EffectiveEntryPrice", "EffectiveExitPrice",
    "TPPct", "SLPct", "RVOL_J1", "Score_J1", "RSI14_J1", "Var5j_J1",
    "ATR14Pct_J1", "DistanceMM20_J1_Pct",
}

NUMERIC_COLUMNS = [
    "EffectiveEntryPrice", "EffectiveExitPrice", "TPPct", "SLPct",
    "RVOL_J1", "Score_J1", "RSI14_J1", "Var5j_J1",
    "ATR14Pct_J1", "DistanceMM20_J1_Pct",
]


# ============================================================================
# CHARGEMENT
# ============================================================================

def load_candidate_file(path: Path, dataset_name: str, is_oos: bool) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Fichier introuvable: {path}")

    df = pd.read_csv(path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"Colonnes manquantes dans {path}: " + ", ".join(sorted(missing))
        )

    df["DecisionTime"] = pd.to_datetime(df["DecisionTime"], errors="coerce")
    df["ExitTime"] = pd.to_datetime(df["ExitTime"], errors="coerce")
    df["SessionDate"] = df["SessionDate"].astype(str).str[:10]

    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Dataset"] = dataset_name
    df["DatasetPath"] = str(path)
    df["IsOOS"] = bool(is_oos)

    df = df.loc[df["TradeStatus"] == "TERMINE"].copy()
    df = df.dropna(
        subset=[
            "Ticker", "SessionDate", "DecisionTime", "ExitTime",
            "EffectiveEntryPrice", "EffectiveExitPrice",
        ]
    )

    df["Priority"] = (
        df["Decision"]
        .map({"ACHAT CONFIRME": 0, "ACHAT REACTIVE": 1})
        .fillna(2)
    )

    return (
        df.sort_values(["DecisionTime", "Priority", "Ticker"])
        .reset_index(drop=True)
    )


def discover_datasets() -> Tuple[List[Dict], List[str]]:
    datasets: List[Dict] = []
    warnings: List[str] = []

    datasets.append(
        {
            "name": "DISCOVERY_CURRENT",
            "path": DISCOVERY_FILE,
            "is_oos": False,
        }
    )

    seen = {DISCOVERY_FILE.resolve() if DISCOVERY_FILE.exists() else DISCOVERY_FILE}

    for root in OOS_PATTERNS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("risk_manager_source_trades.csv")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            relative = path.relative_to(root)
            label = "OOS_" + "_".join(relative.parts[:-1] or [path.parent.name])
            datasets.append({"name": label, "path": path, "is_oos": True})

    if len(datasets) == 1:
        warnings.append(
            "Aucun dataset OOS independant detecte. Le script executera tous "
            "les diagnostics sur l'echantillon de decouverte, mais le verdict "
            "ne pourra pas promouvoir V2."
        )

    return datasets, warnings


def load_official_baseline() -> Dict[str, float]:
    if not OFFICIAL_SUMMARY_FILE.exists():
        raise FileNotFoundError(f"Resume V1 introuvable: {OFFICIAL_SUMMARY_FILE}")

    summary = pd.read_csv(OFFICIAL_SUMMARY_FILE)
    fixed = summary.loc[summary["AllocationModel"] == MODEL].copy()
    if fixed.empty:
        raise ValueError(f"Modele {MODEL} absent de {OFFICIAL_SUMMARY_FILE}")

    row = fixed.iloc[0]
    return {
        "FinalCapitalEUR": float(row["FinalCapitalEUR"]),
        "ExecutedTrades": int(row["ExecutedTrades"]),
        "RejectedTrades": int(row["RejectedTrades"]),
    }


# ============================================================================
# SIMULATEUR PORTEFEUILLE
# ============================================================================

def equity_at_cost(cash: float, positions: List[Dict]) -> float:
    return cash + sum(float(p["TotalEntryCost"]) for p in positions)


def close_due_positions(
    now: pd.Timestamp,
    cash: float,
    positions: List[Dict],
    ledger: List[Dict],
    equity_points: List[Dict],
) -> Tuple[float, List[Dict]]:
    due = [p for p in positions if p["ExitTime"] <= now]
    remaining = [p for p in positions if p["ExitTime"] > now]

    for position in sorted(due, key=lambda p: (p["ExitTime"], p["Ticker"])):
        exit_gross = position["Quantity"] * position["EffectiveExitPrice"]
        exit_fee = exit_gross * FEE_SIDE_PCT / 100.0
        exit_net = exit_gross - exit_fee
        cash += exit_net
        pnl = exit_net - position["TotalEntryCost"]

        ledger.append(
            {
                **position,
                "ExitGrossEUR": exit_gross,
                "ExitFeeEUR": exit_fee,
                "ExitNetEUR": exit_net,
                "PnLEUR": pnl,
                "ReturnPct": pnl / position["TotalEntryCost"] * 100.0,
                "CashAfterExitEUR": cash,
            }
        )

    if due:
        equity_points.append(
            {"Datetime": now, "EquityEUR": equity_at_cost(cash, remaining)}
        )

    return cash, remaining


def calculate_drawdown(equity_points: List[Dict]) -> Tuple[float, float]:
    if not equity_points:
        return np.nan, np.nan

    eq = pd.DataFrame(equity_points)
    eq["Datetime"] = pd.to_datetime(eq["Datetime"], errors="coerce")
    eq = (
        eq.dropna(subset=["Datetime", "EquityEUR"])
        .sort_values("Datetime")
        .drop_duplicates("Datetime", keep="last")
    )
    if eq.empty:
        return np.nan, np.nan

    eq["PeakEUR"] = eq["EquityEUR"].cummax()
    eq["DrawdownEUR"] = eq["EquityEUR"] - eq["PeakEUR"]
    eq["DrawdownPct"] = eq["DrawdownEUR"] / eq["PeakEUR"] * 100.0
    return float(eq["DrawdownEUR"].min()), float(eq["DrawdownPct"].min())


def simulate_portfolio(
    candidates: pd.DataFrame,
    version: str,
    threshold: float | None,
) -> Tuple[Dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return (
            {
                "Version": version,
                "RVOLThreshold": threshold,
                "InitialCapitalEUR": INITIAL_CAPITAL,
                "FinalCapitalEUR": INITIAL_CAPITAL,
                "PnLEUR": 0.0,
                "ReturnPct": 0.0,
                "CandidateTrades": 0,
                "ExecutedTrades": 0,
                "RejectedTrades": 0,
                "RejectedByRVOL": 0,
                "RejectedByCapacity": 0,
                "WinRatePct": np.nan,
                "ProfitFactor": np.nan,
                "MaxDrawdownEUR": 0.0,
                "MaxDrawdownPct": 0.0,
            },
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        )

    candidates = candidates.sort_values(
        ["DecisionTime", "Priority", "Ticker"]
    ).reset_index(drop=True)

    cash = INITIAL_CAPITAL
    positions: List[Dict] = []
    ledger: List[Dict] = []
    rejections: List[Dict] = []
    decisions: List[Dict] = []
    equity_points = [
        {"Datetime": candidates["DecisionTime"].min(), "EquityEUR": INITIAL_CAPITAL}
    ]

    for _, trade in candidates.iterrows():
        now = trade["DecisionTime"]
        cash, positions = close_due_positions(
            now, cash, positions, ledger, equity_points
        )

        base = {
            "Version": version,
            "Dataset": trade["Dataset"],
            "IsOOS": bool(trade["IsOOS"]),
            "Ticker": trade["Ticker"],
            "SessionDate": trade["SessionDate"],
            "Decision": trade["Decision"],
            "DecisionTime": trade["DecisionTime"],
            "ExitTime": trade["ExitTime"],
            "ExitReason": trade["ExitReason"],
            "RVOL_J1": trade["RVOL_J1"],
            "Score_J1": trade["Score_J1"],
            "RSI14_J1": trade["RSI14_J1"],
            "Var5j_J1": trade["Var5j_J1"],
            "ATR14Pct_J1": trade["ATR14Pct_J1"],
            "DistanceMM20_J1_Pct": trade["DistanceMM20_J1_Pct"],
        }

        if (
            threshold is not None
            and not pd.isna(trade["RVOL_J1"])
            and trade["RVOL_J1"] < threshold
        ):
            row = {**base, "Reason": "RVOL_J1_BELOW_THRESHOLD"}
            rejections.append(row)
            decisions.append(
                {**base, "PortfolioAction": "REJET_RVOL", "Reason": row["Reason"]}
            )
            continue

        if len(positions) >= MAX_POSITIONS:
            row = {**base, "Reason": "MAX_POSITIONS"}
            rejections.append(row)
            decisions.append(
                {**base, "PortfolioAction": "REJET_CAPACITE", "Reason": row["Reason"]}
            )
            continue

        entry_price = float(trade["EffectiveEntryPrice"])
        exit_price = float(trade["EffectiveExitPrice"])

        if entry_price <= 0 or exit_price <= 0 or pd.isna(entry_price) or pd.isna(exit_price):
            row = {**base, "Reason": "PRIX_INVALIDE"}
            rejections.append(row)
            decisions.append(
                {**base, "PortfolioAction": "REJET_PRIX", "Reason": row["Reason"]}
            )
            continue

        if entry_price > MAX_SHARE_PRICE:
            row = {**base, "Reason": "PRICE_ABOVE_2000"}
            rejections.append(row)
            decisions.append(
                {**base, "PortfolioAction": "REJET_PRIX_MAX", "Reason": row["Reason"]}
            )
            continue

        quantity = int(FIXED_BUDGET // entry_price)
        if quantity < 1 and entry_price <= cash:
            quantity = 1

        entry_gross = quantity * entry_price
        entry_fee = entry_gross * FEE_SIDE_PCT / 100.0
        total_cost = entry_gross + entry_fee

        while quantity > 0 and total_cost > cash:
            quantity -= 1
            entry_gross = quantity * entry_price
            entry_fee = entry_gross * FEE_SIDE_PCT / 100.0
            total_cost = entry_gross + entry_fee

        if quantity <= 0:
            row = {**base, "Reason": "BUDGET_OR_CASH"}
            rejections.append(row)
            decisions.append(
                {**base, "PortfolioAction": "REJET_CASH", "Reason": row["Reason"]}
            )
            continue

        cash -= total_cost
        position = {
            **base,
            "Quantity": quantity,
            "EffectiveEntryPrice": entry_price,
            "EffectiveExitPrice": exit_price,
            "EntryGrossEUR": entry_gross,
            "EntryFeeEUR": entry_fee,
            "TotalEntryCost": total_cost,
            "CashAfterEntryEUR": cash,
        }
        positions.append(position)
        decisions.append(
            {**base, "PortfolioAction": "EXECUTE", "Reason": "TRADE_EXECUTE"}
        )
        equity_points.append(
            {"Datetime": now, "EquityEUR": equity_at_cost(cash, positions)}
        )

    if positions:
        final_time = max(p["ExitTime"] for p in positions)
        cash, positions = close_due_positions(
            final_time, cash, positions, ledger, equity_points
        )

    ledger_df = pd.DataFrame(ledger)
    rejected_df = pd.DataFrame(rejections)
    decisions_df = pd.DataFrame(decisions)

    winners = ledger_df.loc[ledger_df["PnLEUR"] > 0] if not ledger_df.empty else ledger_df
    losers = ledger_df.loc[ledger_df["PnLEUR"] < 0] if not ledger_df.empty else ledger_df
    gross_profit = float(winners["PnLEUR"].sum()) if not winners.empty else 0.0
    gross_loss = abs(float(losers["PnLEUR"].sum())) if not losers.empty else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    dd_eur, dd_pct = calculate_drawdown(equity_points)

    rejected_by_rvol = 0
    rejected_by_capacity = 0
    if not rejected_df.empty:
        rejected_by_rvol = int(
            (rejected_df["Reason"] == "RVOL_J1_BELOW_THRESHOLD").sum()
        )
        rejected_by_capacity = int(
            (rejected_df["Reason"] == "MAX_POSITIONS").sum()
        )

    summary = {
        "Version": version,
        "RVOLThreshold": threshold,
        "InitialCapitalEUR": INITIAL_CAPITAL,
        "FinalCapitalEUR": cash,
        "PnLEUR": cash - INITIAL_CAPITAL,
        "ReturnPct": (cash / INITIAL_CAPITAL - 1.0) * 100.0,
        "CandidateTrades": len(candidates),
        "ExecutedTrades": len(ledger_df),
        "RejectedTrades": len(rejected_df),
        "RejectedByRVOL": rejected_by_rvol,
        "RejectedByCapacity": rejected_by_capacity,
        "WinRatePct": (
            len(winners) / len(ledger_df) * 100.0 if len(ledger_df) else np.nan
        ),
        "ProfitFactor": profit_factor,
        "MaxDrawdownEUR": dd_eur,
        "MaxDrawdownPct": dd_pct,
    }
    return summary, ledger_df, rejected_df, decisions_df


# ============================================================================
# ANALYSES AUTOMATIQUES
# ============================================================================

def validate_discovery_baseline(reconstructed: Dict, official: Dict) -> None:
    capital_ok = abs(
        reconstructed["FinalCapitalEUR"] - official["FinalCapitalEUR"]
    ) <= BASELINE_TOLERANCE_EUR
    trades_ok = reconstructed["ExecutedTrades"] == official["ExecutedTrades"]
    rejects_ok = reconstructed["RejectedTrades"] == official["RejectedTrades"]

    if not (capital_ok and trades_ok and rejects_ok):
        raise RuntimeError(
            "La V1 reconstruite ne correspond pas a la baseline officielle. "
            f"Reconstruit: capital={reconstructed['FinalCapitalEUR']:.6f}, "
            f"trades={reconstructed['ExecutedTrades']}, "
            f"rejets={reconstructed['RejectedTrades']}. "
            f"Officiel: capital={official['FinalCapitalEUR']:.6f}, "
            f"trades={official['ExecutedTrades']}, "
            f"rejets={official['RejectedTrades']}."
        )


def run_pair(candidates: pd.DataFrame, label: str) -> Tuple[Dict, Dict, pd.DataFrame, pd.DataFrame]:
    v1, v1_ledger, _, _ = simulate_portfolio(candidates, f"{label}_V1", None)
    v2, v2_ledger, v2_rejections, _ = simulate_portfolio(
        candidates, f"{label}_V2_RVOL060", OFFICIAL_RVOL_THRESHOLD
    )
    return v1, v2, pd.concat([v1_ledger, v2_ledger], ignore_index=True), v2_rejections


def pair_row(scope: str, dataset: str, is_oos: bool, v1: Dict, v2: Dict) -> Dict:
    return {
        "Scope": scope,
        "Dataset": dataset,
        "IsOOS": is_oos,
        "CandidateTrades": v1["CandidateTrades"],
        "V1_FinalCapitalEUR": v1["FinalCapitalEUR"],
        "V2_FinalCapitalEUR": v2["FinalCapitalEUR"],
        "DeltaCapitalEUR": v2["FinalCapitalEUR"] - v1["FinalCapitalEUR"],
        "V1_ReturnPct": v1["ReturnPct"],
        "V2_ReturnPct": v2["ReturnPct"],
        "DeltaReturnPctPoint": v2["ReturnPct"] - v1["ReturnPct"],
        "V1_ExecutedTrades": v1["ExecutedTrades"],
        "V2_ExecutedTrades": v2["ExecutedTrades"],
        "FilteredTrades": v2["RejectedByRVOL"],
        "V1_WinRatePct": v1["WinRatePct"],
        "V2_WinRatePct": v2["WinRatePct"],
        "V1_ProfitFactor": v1["ProfitFactor"],
        "V2_ProfitFactor": v2["ProfitFactor"],
        "V1_MaxDrawdownPct": v1["MaxDrawdownPct"],
        "V2_MaxDrawdownPct": v2["MaxDrawdownPct"],
    }


def temporal_blocks(candidates: pd.DataFrame) -> List[Tuple[str, pd.DataFrame]]:
    if candidates.empty:
        return []
    temp = candidates.copy()
    temp["Block"] = temp["DecisionTime"].dt.to_period("Q").astype(str)
    return [(str(block), group.copy()) for block, group in temp.groupby("Block")]


def sensitivity_analysis(candidates: pd.DataFrame, dataset: str, is_oos: bool) -> pd.DataFrame:
    rows = []
    v1, _, _, _ = simulate_portfolio(candidates, "V1_SENSITIVITY", None)
    for threshold in SENSITIVITY_THRESHOLDS:
        v2, _, rejected, _ = simulate_portfolio(
            candidates, f"V2_RVOL_{threshold:.2f}", threshold
        )
        rows.append(
            {
                "Dataset": dataset,
                "IsOOS": is_oos,
                "Threshold": threshold,
                "OfficialThreshold": threshold == OFFICIAL_RVOL_THRESHOLD,
                "V1_FinalCapitalEUR": v1["FinalCapitalEUR"],
                "V2_FinalCapitalEUR": v2["FinalCapitalEUR"],
                "DeltaCapitalEUR": v2["FinalCapitalEUR"] - v1["FinalCapitalEUR"],
                "V2_ReturnPct": v2["ReturnPct"],
                "V2_ProfitFactor": v2["ProfitFactor"],
                "V2_MaxDrawdownPct": v2["MaxDrawdownPct"],
                "FilteredTrades": int(
                    (rejected["Reason"] == "RVOL_J1_BELOW_THRESHOLD").sum()
                ) if not rejected.empty else 0,
            }
        )
    return pd.DataFrame(rows)


def bootstrap_weekly_delta(
    v1_ledger: pd.DataFrame,
    v2_ledger: pd.DataFrame,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> Dict:
    if v1_ledger.empty or v2_ledger.empty:
        return {
            "BootstrapIterations": 0,
            "ProbabilityV2AboveV1Pct": np.nan,
            "DeltaPnL_CI95_LowEUR": np.nan,
            "DeltaPnL_CI95_HighEUR": np.nan,
        }

    def weekly(ledger: pd.DataFrame, name: str) -> pd.DataFrame:
        x = ledger.copy()
        x["ExitTime"] = pd.to_datetime(x["ExitTime"], errors="coerce")
        iso = x["ExitTime"].dt.isocalendar()
        x["Week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
        return x.groupby("Week", as_index=False)["PnLEUR"].sum().rename(columns={"PnLEUR": name})

    merged = weekly(v1_ledger, "V1").merge(
        weekly(v2_ledger, "V2"), on="Week", how="outer"
    ).fillna(0.0)
    merged["Delta"] = merged["V2"] - merged["V1"]

    deltas = merged["Delta"].to_numpy(dtype=float)
    if len(deltas) < 2:
        return {
            "BootstrapIterations": 0,
            "ProbabilityV2AboveV1Pct": np.nan,
            "DeltaPnL_CI95_LowEUR": np.nan,
            "DeltaPnL_CI95_HighEUR": np.nan,
        }

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    samples = rng.choice(deltas, size=(iterations, len(deltas)), replace=True)
    totals = samples.sum(axis=1)
    return {
        "BootstrapIterations": iterations,
        "ProbabilityV2AboveV1Pct": float((totals > 0).mean() * 100.0),
        "DeltaPnL_CI95_LowEUR": float(np.percentile(totals, 2.5)),
        "DeltaPnL_CI95_HighEUR": float(np.percentile(totals, 97.5)),
    }


def filtered_trade_diagnostics(candidates: pd.DataFrame) -> Dict:
    filtered = candidates.loc[
        candidates["RVOL_J1"].notna()
        & (candidates["RVOL_J1"] < OFFICIAL_RVOL_THRESHOLD)
    ].copy()

    if filtered.empty:
        return {
            "FilteredCandidates": 0,
            "FilteredRawWinRatePct": np.nan,
            "FilteredRawAverageNetReturnPct": np.nan,
            "FilteredUniqueTickers": 0,
            "LargestTickerSharePct": np.nan,
        }

    raw_returns = pd.to_numeric(filtered.get("NetReturnPct"), errors="coerce")
    ticker_counts = filtered["Ticker"].value_counts()
    return {
        "FilteredCandidates": len(filtered),
        "FilteredRawWinRatePct": float((raw_returns > 0).mean() * 100.0),
        "FilteredRawAverageNetReturnPct": float(raw_returns.mean()),
        "FilteredUniqueTickers": int(filtered["Ticker"].nunique()),
        "LargestTickerSharePct": float(ticker_counts.iloc[0] / len(filtered) * 100.0),
    }


def decide_verdict(
    oos_rows: pd.DataFrame,
    oos_candidates: pd.DataFrame,
    oos_v1: Dict | None,
    oos_v2: Dict | None,
    filtered_diag: Dict,
    bootstrap: Dict,
) -> Tuple[str, List[str]]:
    reasons: List[str] = []

    if oos_candidates.empty or oos_v1 is None or oos_v2 is None:
        return (
            "A_CONFIRMER_OOS_MANQUANT",
            ["Aucun dataset hors echantillon independant n'a ete detecte."],
        )

    total_executed = int(oos_v1["ExecutedTrades"])
    filtered_count = int(oos_v2["RejectedByRVOL"])
    delta_pnl = float(oos_v2["PnLEUR"] - oos_v1["PnLEUR"])
    pf_ok = bool(oos_v2["ProfitFactor"] >= oos_v1["ProfitFactor"])
    dd_delta = float(oos_v2["MaxDrawdownPct"] - oos_v1["MaxDrawdownPct"])
    dd_ok = dd_delta >= MAX_ALLOWED_DD_DEGRADATION_POINT

    block_rows = oos_rows.loc[oos_rows["Scope"] == "TEMPORAL_BLOCK"].copy()
    improved_blocks = int((block_rows["DeltaCapitalEUR"] > 0).sum()) if not block_rows.empty else 0
    total_blocks = len(block_rows)
    required_improved = max(1, int(np.ceil(total_blocks * 2 / 3))) if total_blocks else 1
    major_damage = bool(
        (block_rows["DeltaReturnPctPoint"] < MAX_ALLOWED_BLOCK_DAMAGE_PCT).any()
    ) if not block_rows.empty else False

    filtered_expectancy_ok = (
        not pd.isna(filtered_diag["FilteredRawAverageNetReturnPct"])
        and filtered_diag["FilteredRawAverageNetReturnPct"] < 0
    )
    filtered_win_rate_ok = (
        not pd.isna(filtered_diag["FilteredRawWinRatePct"])
        and filtered_diag["FilteredRawWinRatePct"] <= MAX_FILTERED_WIN_RATE_PCT
    )
    concentration_ok = (
        not pd.isna(filtered_diag["LargestTickerSharePct"])
        and filtered_diag["LargestTickerSharePct"] <= 50.0
    )

    if total_executed < MIN_OOS_EXECUTED_TRADES:
        reasons.append(
            f"Seulement {total_executed} trades OOS executes; minimum fixe: {MIN_OOS_EXECUTED_TRADES}."
        )
    if filtered_count < MIN_OOS_FILTERED_TRADES:
        reasons.append(
            f"Seulement {filtered_count} trades OOS filtres; minimum fixe: {MIN_OOS_FILTERED_TRADES}."
        )

    hard_fail = (
        delta_pnl <= 0
        or not pf_ok
        or not dd_ok
        or major_damage
        or not filtered_expectancy_ok
        or not filtered_win_rate_ok
        or not concentration_ok
        or improved_blocks < required_improved
    )

    enough_data = (
        total_executed >= MIN_OOS_EXECUTED_TRADES
        and filtered_count >= MIN_OOS_FILTERED_TRADES
    )

    if hard_fail:
        reasons.append("Au moins un critere de performance ou de stabilite OOS a echoue.")
        return "REJETE", reasons

    if not enough_data:
        reasons.append("Le signal est positif, mais l'echantillon OOS reste insuffisant.")
        return "A_CONFIRMER_DONNEES_INSUFFISANTES", reasons

    probability = bootstrap.get("ProbabilityV2AboveV1Pct", np.nan)
    if pd.isna(probability) or probability < 90.0:
        reasons.append("La confiance bootstrap est inferieure a 90%.")
        return "A_CONFIRMER_BOOTSTRAP", reasons

    reasons.append("Tous les criteres OOS figes sont satisfaits.")
    return "VALIDE_POUR_PAPER_TRADING", reasons


# ============================================================================
# EXECUTION PRINCIPALE
# ============================================================================

def main() -> None:
    print("=" * 120)
    print("HISTORICAL LAB - VALIDATION V2 FULL")
    print("CHAMPION V1 VS CHALLENGER V2 RVOL_J1 < 0.60")
    print("V1 REELLE NON MODIFIEE")
    print("=" * 120)

    dataset_specs, warnings = discover_datasets()
    official = load_official_baseline()

    all_candidates = []
    dataset_inventory = []
    comparison_rows = []
    sensitivity_frames = []
    all_ledgers = []
    all_rejections = []

    discovery_v1 = None
    discovery_v2 = None

    for spec in dataset_specs:
        candidates = load_candidate_file(spec["path"], spec["name"], spec["is_oos"])
        all_candidates.append(candidates)
        dataset_inventory.append(
            {
                "Dataset": spec["name"],
                "Path": str(spec["path"]),
                "IsOOS": spec["is_oos"],
                "CandidateTrades": len(candidates),
                "Start": candidates["DecisionTime"].min() if not candidates.empty else pd.NaT,
                "End": candidates["DecisionTime"].max() if not candidates.empty else pd.NaT,
            }
        )

        v1, v2, ledgers, rejections = run_pair(candidates, spec["name"])
        comparison_rows.append(pair_row("DATASET", spec["name"], spec["is_oos"], v1, v2))
        all_ledgers.append(ledgers)
        if not rejections.empty:
            all_rejections.append(rejections.assign(Dataset=spec["name"]))

        for block_name, block_df in temporal_blocks(candidates):
            bv1, bv2, bledgers, brejections = run_pair(block_df, f"{spec['name']}_{block_name}")
            comparison_rows.append(
                pair_row("TEMPORAL_BLOCK", f"{spec['name']}::{block_name}", spec["is_oos"], bv1, bv2)
            )
            all_ledgers.append(bledgers)
            if not brejections.empty:
                all_rejections.append(
                    brejections.assign(Dataset=f"{spec['name']}::{block_name}")
                )

        sensitivity_frames.append(
            sensitivity_analysis(candidates, spec["name"], spec["is_oos"])
        )

        if not spec["is_oos"]:
            discovery_v1, discovery_v2 = v1, v2
            validate_discovery_baseline(v1, official)

    combined_candidates = pd.concat(all_candidates, ignore_index=True) if all_candidates else pd.DataFrame()
    oos_candidates = combined_candidates.loc[combined_candidates["IsOOS"]].copy()

    oos_v1 = None
    oos_v2 = None
    oos_v1_ledger = pd.DataFrame()
    oos_v2_ledger = pd.DataFrame()

    if not oos_candidates.empty:
        oos_v1, oos_v1_ledger, _, _ = simulate_portfolio(
            oos_candidates, "OOS_CONSOLIDATED_V1", None
        )
        oos_v2, oos_v2_ledger, oos_rejections, _ = simulate_portfolio(
            oos_candidates, "OOS_CONSOLIDATED_V2", OFFICIAL_RVOL_THRESHOLD
        )
        comparison_rows.append(
            pair_row("OOS_CONSOLIDATED", "ALL_OOS", True, oos_v1, oos_v2)
        )
        all_ledgers.append(pd.concat([oos_v1_ledger, oos_v2_ledger], ignore_index=True))
        if not oos_rejections.empty:
            all_rejections.append(oos_rejections.assign(Dataset="ALL_OOS"))

    comparison_df = pd.DataFrame(comparison_rows)
    sensitivity_df = pd.concat(sensitivity_frames, ignore_index=True) if sensitivity_frames else pd.DataFrame()
    inventory_df = pd.DataFrame(dataset_inventory)
    ledgers_df = pd.concat(all_ledgers, ignore_index=True) if all_ledgers else pd.DataFrame()
    rejections_df = pd.concat(all_rejections, ignore_index=True) if all_rejections else pd.DataFrame()

    filtered_diag = filtered_trade_diagnostics(oos_candidates)
    bootstrap = bootstrap_weekly_delta(oos_v1_ledger, oos_v2_ledger)

    oos_rows = comparison_df.loc[comparison_df["IsOOS"]].copy()
    verdict, verdict_reasons = decide_verdict(
        oos_rows, oos_candidates, oos_v1, oos_v2, filtered_diag, bootstrap
    )

    sensitivity_oos = sensitivity_df.loc[sensitivity_df["IsOOS"]].copy() if not sensitivity_df.empty else pd.DataFrame()
    neighbor_positive = None
    if not sensitivity_oos.empty:
        grouped = sensitivity_oos.groupby("Threshold", as_index=False)["DeltaCapitalEUR"].sum()
        neighbor_positive = bool(
            (grouped.loc[grouped["Threshold"].isin([0.55, 0.60, 0.65]), "DeltaCapitalEUR"] > 0).all()
        )

    final_report = {
        "Rule": "RVOL_J1 < 0.60 -> REJET",
        "OfficialThreshold": OFFICIAL_RVOL_THRESHOLD,
        "DiscoveryBaselineControl": "OK",
        "DiscoveryV1": discovery_v1,
        "DiscoveryV2": discovery_v2,
        "OOSDatasetCount": int(inventory_df["IsOOS"].sum()) if not inventory_df.empty else 0,
        "OOSCandidateTrades": int(len(oos_candidates)),
        "OOSV1": oos_v1,
        "OOSV2": oos_v2,
        "FilteredDiagnosticsOOS": filtered_diag,
        "BootstrapOOS": bootstrap,
        "SensitivityNeighborsPositiveOOS": neighbor_positive,
        "Verdict": verdict,
        "VerdictReasons": verdict_reasons,
        "Warnings": warnings,
    }

    inventory_df.to_csv(OUTPUT_DIR / "dataset_inventory.csv", index=False, encoding="utf-8")
    comparison_df.to_csv(OUTPUT_DIR / "champion_vs_challenger.csv", index=False, encoding="utf-8")
    sensitivity_df.to_csv(OUTPUT_DIR / "threshold_sensitivity.csv", index=False, encoding="utf-8")
    ledgers_df.to_csv(OUTPUT_DIR / "all_ledgers.csv", index=False, encoding="utf-8")
    rejections_df.to_csv(OUTPUT_DIR / "all_rejections.csv", index=False, encoding="utf-8")

    with open(OUTPUT_DIR / "final_verdict.json", "w", encoding="utf-8") as handle:
        json.dump(final_report, handle, ensure_ascii=False, indent=2, default=str)

    lines = [
        "HISTORICAL LAB - V2 FULL",
        "",
        "Regle: RVOL_J1 < 0.60 -> REJET",
        f"Controle baseline decouverte: OK",
        f"Datasets OOS detectes: {final_report['OOSDatasetCount']}",
        f"Candidats OOS: {final_report['OOSCandidateTrades']}",
        "",
        f"FINAL VERDICT = {verdict}",
        "",
        "Raisons:",
        *[f"- {reason}" for reason in verdict_reasons],
        "",
        "Avertissements:",
        *([f"- {warning}" for warning in warnings] or ["- Aucun"]),
    ]
    (OUTPUT_DIR / "final_verdict.txt").write_text("\n".join(lines), encoding="utf-8")

    print()
    print("CONTROLE BASELINE DECOUVERTE : OK")
    print(
        f"V1 decouverte: {discovery_v1['FinalCapitalEUR']:.3f} EUR | "
        f"V2 decouverte: {discovery_v2['FinalCapitalEUR']:.3f} EUR"
    )
    print(f"Datasets OOS detectes: {final_report['OOSDatasetCount']}")
    print(f"Candidats OOS: {final_report['OOSCandidateTrades']}")

    if oos_v1 is not None and oos_v2 is not None:
        print(
            f"OOS V1: {oos_v1['FinalCapitalEUR']:.3f} EUR | "
            f"OOS V2: {oos_v2['FinalCapitalEUR']:.3f} EUR | "
            f"Delta: {oos_v2['FinalCapitalEUR'] - oos_v1['FinalCapitalEUR']:+.3f} EUR"
        )
        print(
            f"Bootstrap P(V2 > V1): "
            f"{bootstrap['ProbabilityV2AboveV1Pct']:.2f}%"
        )

    print()
    print("=" * 120)
    print(f"FINAL VERDICT = {verdict}")
    for reason in verdict_reasons:
        print(f"- {reason}")
    for warning in warnings:
        print(f"- AVERTISSEMENT: {warning}")
    print("=" * 120)
    print(f"Rapport: {OUTPUT_DIR / 'final_verdict.txt'}")
    print(f"JSON:    {OUTPUT_DIR / 'final_verdict.json'}")
    print("FIN V2 FULL")


if __name__ == "__main__":
    main()
