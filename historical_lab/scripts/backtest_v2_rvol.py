from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = BASE_DIR / "results"

SOURCE_FILE = RESULTS_DIR / "risk_manager_source_trades.csv"
OFFICIAL_SUMMARY_FILE = RESULTS_DIR / "risk_manager_summary.csv"

OUTPUT_DETAIL = RESULTS_DIR / "v2_test02_rvol_detail.csv"
OUTPUT_SUMMARY = RESULTS_DIR / "v2_test02_rvol_summary.csv"
OUTPUT_EXECUTED = RESULTS_DIR / "v2_test02_rvol_executed.csv"
OUTPUT_REJECTIONS = RESULTS_DIR / "v2_test02_rvol_rejections.csv"

MODEL = "FIXED_1000"

INITIAL_CAPITAL = 3000.0
MAX_POSITIONS = 3
MAX_SHARE_PRICE = 2000.0
FIXED_BUDGET = 1000.0

FEE_SIDE_PCT = 0.05

RVOL_THRESHOLD = 0.60

BASELINE_TOLERANCE_EUR = 0.02


# ============================================================
# CHARGEMENT ET VALIDATION
# ============================================================

def load_source_trades():
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(
            f"Fichier source introuvable : {SOURCE_FILE}"
        )

    source = pd.read_csv(SOURCE_FILE)

    required_columns = {
        "Ticker",
        "SessionDate",
        "Decision",
        "DecisionTime",
        "TradeStatus",
        "ExitTime",
        "ExitReason",
        "EffectiveEntryPrice",
        "EffectiveExitPrice",
        "TPPct",
        "SLPct",
        "RVOL_J1",
        "Score_J1",
        "RSI14_J1",
        "Var5j_J1",
        "ATR14Pct_J1",
        "DistanceMM20_J1_Pct",
    }

    missing = required_columns - set(source.columns)

    if missing:
        raise ValueError(
            "Colonnes manquantes dans "
            "risk_manager_source_trades.csv : "
            + ", ".join(sorted(missing))
        )

    datetime_columns = [
        "DecisionTime",
        "ExitTime",
    ]

    numeric_columns = [
        "EffectiveEntryPrice",
        "EffectiveExitPrice",
        "TPPct",
        "SLPct",
        "RVOL_J1",
        "Score_J1",
        "RSI14_J1",
        "Var5j_J1",
        "ATR14Pct_J1",
        "DistanceMM20_J1_Pct",
    ]

    for column in datetime_columns:
        source[column] = pd.to_datetime(
            source[column],
            errors="coerce",
        )

    for column in numeric_columns:
        source[column] = pd.to_numeric(
            source[column],
            errors="coerce",
        )

    completed = source.loc[
        source["TradeStatus"] == "TERMINE"
    ].copy()

    completed = completed.dropna(
        subset=[
            "Ticker",
            "SessionDate",
            "DecisionTime",
            "ExitTime",
            "EffectiveEntryPrice",
            "EffectiveExitPrice",
        ]
    )

    completed["Priority"] = (
        completed["Decision"]
        .map(
            {
                "ACHAT CONFIRME": 0,
                "ACHAT REACTIVE": 1,
            }
        )
        .fillna(2)
    )

    completed = (
        completed
        .sort_values(
            [
                "DecisionTime",
                "Priority",
                "Ticker",
            ]
        )
        .reset_index(drop=True)
    )

    return source, completed


def load_official_baseline():
    if not OFFICIAL_SUMMARY_FILE.exists():
        raise FileNotFoundError(
            f"Résumé V1 introuvable : {OFFICIAL_SUMMARY_FILE}"
        )

    summary = pd.read_csv(OFFICIAL_SUMMARY_FILE)

    required_columns = {
        "AllocationModel",
        "FinalCapitalEUR",
        "PnLEUR",
        "ReturnPct",
        "ExecutedTrades",
        "RejectedTrades",
        "WinRatePct",
        "ProfitFactor",
        "MaxDrawdownEUR",
        "MaxDrawdownPct",
    }

    missing = required_columns - set(summary.columns)

    if missing:
        raise ValueError(
            "Colonnes manquantes dans risk_manager_summary.csv : "
            + ", ".join(sorted(missing))
        )

    fixed = summary.loc[
        summary["AllocationModel"] == MODEL
    ].copy()

    if fixed.empty:
        raise ValueError(
            f"Modèle {MODEL} introuvable dans "
            "risk_manager_summary.csv"
        )

    row = fixed.iloc[0]

    return {
        "FinalCapitalEUR": float(row["FinalCapitalEUR"]),
        "PnLEUR": float(row["PnLEUR"]),
        "ReturnPct": float(row["ReturnPct"]),
        "ExecutedTrades": int(row["ExecutedTrades"]),
        "RejectedTrades": int(row["RejectedTrades"]),
        "WinRatePct": float(row["WinRatePct"]),
        "ProfitFactor": float(row["ProfitFactor"]),
        "MaxDrawdownEUR": float(row["MaxDrawdownEUR"]),
        "MaxDrawdownPct": float(row["MaxDrawdownPct"]),
    }


# ============================================================
# OUTILS PORTEFEUILLE
# ============================================================

def equity_at_cost(cash, positions):
    return cash + sum(
        position["TotalEntryCost"]
        for position in positions
    )


def calculate_drawdown(equity_points):
    if not equity_points:
        return np.nan, np.nan

    equity = pd.DataFrame(equity_points)

    equity["Datetime"] = pd.to_datetime(
        equity["Datetime"],
        errors="coerce",
    )

    equity = (
        equity
        .dropna(subset=["Datetime", "EquityEUR"])
        .sort_values("Datetime")
        .drop_duplicates(
            "Datetime",
            keep="last",
        )
    )

    if equity.empty:
        return np.nan, np.nan

    equity["PeakEUR"] = equity["EquityEUR"].cummax()

    equity["DrawdownEUR"] = (
        equity["EquityEUR"]
        - equity["PeakEUR"]
    )

    equity["DrawdownPct"] = (
        equity["DrawdownEUR"]
        / equity["PeakEUR"]
        * 100.0
    )

    return (
        float(equity["DrawdownEUR"].min()),
        float(equity["DrawdownPct"].min()),
    )


def close_due_positions(
    now,
    cash,
    positions,
    ledger,
    equity_points,
):
    due = [
        position
        for position in positions
        if position["ExitTime"] <= now
    ]

    remaining = [
        position
        for position in positions
        if position["ExitTime"] > now
    ]

    due = sorted(
        due,
        key=lambda position: (
            position["ExitTime"],
            position["Ticker"],
        ),
    )

    for position in due:
        exit_gross = (
            position["Quantity"]
            * position["EffectiveExitPrice"]
        )

        exit_fee = (
            exit_gross
            * FEE_SIDE_PCT
            / 100.0
        )

        exit_net = exit_gross - exit_fee

        cash += exit_net

        pnl = (
            exit_net
            - position["TotalEntryCost"]
        )

        return_pct = (
            pnl
            / position["TotalEntryCost"]
            * 100.0
        )

        ledger.append(
            {
                **position,
                "ExitGrossEUR": exit_gross,
                "ExitFeeEUR": exit_fee,
                "ExitNetEUR": exit_net,
                "PnLEUR": pnl,
                "ReturnPct": return_pct,
                "CashAfterExitEUR": cash,
            }
        )

    if due:
        equity_points.append(
            {
                "Datetime": now,
                "EquityEUR": equity_at_cost(
                    cash,
                    remaining,
                ),
            }
        )

    return cash, remaining


# ============================================================
# SIMULATION CHRONOLOGIQUE
# ============================================================

def simulate_portfolio(
    candidates,
    version_name,
    apply_rvol_filter,
):
    cash = INITIAL_CAPITAL

    positions = []
    ledger = []
    rejections = []
    decisions = []

    if candidates.empty:
        raise ValueError(
            "Aucun trade terminé disponible pour la simulation."
        )

    equity_points = [
        {
            "Datetime": candidates["DecisionTime"].min(),
            "EquityEUR": INITIAL_CAPITAL,
        }
    ]

    for _, trade in candidates.iterrows():
        now = trade["DecisionTime"]

        cash, positions = close_due_positions(
            now,
            cash,
            positions,
            ledger,
            equity_points,
        )

        base_decision = {
            "Version": version_name,
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
            "DistanceMM20_J1_Pct":
                trade["DistanceMM20_J1_Pct"],
            "OpenPositionsBeforeDecision": len(positions),
            "CashBeforeDecisionEUR": cash,
        }

        if (
            apply_rvol_filter
            and not pd.isna(trade["RVOL_J1"])
            and trade["RVOL_J1"] < RVOL_THRESHOLD
        ):
            rejection = {
                **base_decision,
                "Reason": "RVOL_J1_BELOW_THRESHOLD",
                "RVOLThreshold": RVOL_THRESHOLD,
            }

            rejections.append(rejection)

            decisions.append(
                {
                    **base_decision,
                    "PortfolioAction": "REJET_RVOL",
                    "Reason": "RVOL_J1_BELOW_THRESHOLD",
                    "Quantity": 0,
                }
            )

            continue

        if len(positions) >= MAX_POSITIONS:
            rejection = {
                **base_decision,
                "Reason": "MAX_POSITIONS",
                "RVOLThreshold": (
                    RVOL_THRESHOLD
                    if apply_rvol_filter
                    else np.nan
                ),
            }

            rejections.append(rejection)

            decisions.append(
                {
                    **base_decision,
                    "PortfolioAction": "REJET_CAPACITE",
                    "Reason": "MAX_POSITIONS",
                    "Quantity": 0,
                }
            )

            continue

        entry_price = float(
            trade["EffectiveEntryPrice"]
        )

        exit_price = float(
            trade["EffectiveExitPrice"]
        )

        if (
            pd.isna(entry_price)
            or entry_price <= 0
            or pd.isna(exit_price)
            or exit_price <= 0
        ):
            rejection = {
                **base_decision,
                "Reason": "PRIX_INVALIDE",
                "RVOLThreshold": (
                    RVOL_THRESHOLD
                    if apply_rvol_filter
                    else np.nan
                ),
            }

            rejections.append(rejection)

            decisions.append(
                {
                    **base_decision,
                    "PortfolioAction": "REJET_PRIX",
                    "Reason": "PRIX_INVALIDE",
                    "Quantity": 0,
                }
            )

            continue

        if entry_price > MAX_SHARE_PRICE:
            rejection = {
                **base_decision,
                "Reason": "PRICE_ABOVE_2000",
                "RVOLThreshold": (
                    RVOL_THRESHOLD
                    if apply_rvol_filter
                    else np.nan
                ),
            }

            rejections.append(rejection)

            decisions.append(
                {
                    **base_decision,
                    "PortfolioAction": "REJET_PRIX_MAX",
                    "Reason": "PRICE_ABOVE_2000",
                    "Quantity": 0,
                }
            )

            continue

        quantity = int(
            FIXED_BUDGET // entry_price
        )

        if (
            quantity < 1
            and entry_price <= cash
        ):
            quantity = 1

        entry_gross = (
            quantity
            * entry_price
        )

        entry_fee = (
            entry_gross
            * FEE_SIDE_PCT
            / 100.0
        )

        total_entry_cost = (
            entry_gross
            + entry_fee
        )

        while (
            quantity > 0
            and total_entry_cost > cash
        ):
            quantity -= 1

            entry_gross = (
                quantity
                * entry_price
            )

            entry_fee = (
                entry_gross
                * FEE_SIDE_PCT
                / 100.0
            )

            total_entry_cost = (
                entry_gross
                + entry_fee
            )

        if quantity <= 0:
            rejection = {
                **base_decision,
                "Reason": "BUDGET_OR_CASH",
                "RVOLThreshold": (
                    RVOL_THRESHOLD
                    if apply_rvol_filter
                    else np.nan
                ),
            }

            rejections.append(rejection)

            decisions.append(
                {
                    **base_decision,
                    "PortfolioAction": "REJET_CASH",
                    "Reason": "BUDGET_OR_CASH",
                    "Quantity": 0,
                }
            )

            continue

        cash -= total_entry_cost

        sl_pct = float(trade["SLPct"])

        planned_risk = (
            entry_gross
            * sl_pct
            / 100.0
            if not pd.isna(sl_pct)
            else np.nan
        )

        position = {
            "Version": version_name,
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
            "DistanceMM20_J1_Pct":
                trade["DistanceMM20_J1_Pct"],
            "Quantity": quantity,
            "EffectiveEntryPrice": entry_price,
            "EffectiveExitPrice": exit_price,
            "EntryGrossEUR": entry_gross,
            "EntryFeeEUR": entry_fee,
            "TotalEntryCost": total_entry_cost,
            "PlannedRiskEUR": planned_risk,
            "TPPct": trade["TPPct"],
            "SLPct": trade["SLPct"],
            "CashAfterEntryEUR": cash,
        }

        positions.append(position)

        decisions.append(
            {
                **base_decision,
                "PortfolioAction": "EXECUTE",
                "Reason": "TRADE_EXECUTE",
                "Quantity": quantity,
                "EntryGrossEUR": entry_gross,
                "EntryFeeEUR": entry_fee,
                "TotalEntryCost": total_entry_cost,
                "CashAfterEntryEUR": cash,
            }
        )

        equity_points.append(
            {
                "Datetime": now,
                "EquityEUR": equity_at_cost(
                    cash,
                    positions,
                ),
            }
        )

    if positions:
        final_time = max(
            position["ExitTime"]
            for position in positions
        )

        cash, positions = close_due_positions(
            final_time,
            cash,
            positions,
            ledger,
            equity_points,
        )

    ledger_df = pd.DataFrame(ledger)
    rejection_df = pd.DataFrame(rejections)
    decision_df = pd.DataFrame(decisions)

    if ledger_df.empty:
        raise ValueError(
            f"Aucun trade exécuté pour {version_name}."
        )

    winners = ledger_df.loc[
        ledger_df["PnLEUR"] > 0
    ]

    losers = ledger_df.loc[
        ledger_df["PnLEUR"] < 0
    ]

    gross_profit = float(
        winners["PnLEUR"].sum()
    )

    gross_loss = abs(
        float(
            losers["PnLEUR"].sum()
        )
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else np.inf
    )

    max_dd_eur, max_dd_pct = calculate_drawdown(
        equity_points
    )

    summary = {
        "Version": version_name,
        "RVOLThreshold": (
            RVOL_THRESHOLD
            if apply_rvol_filter
            else np.nan
        ),
        "InitialCapitalEUR": INITIAL_CAPITAL,
        "FinalCapitalEUR": cash,
        "PnLEUR": cash - INITIAL_CAPITAL,
        "ReturnPct": (
            cash / INITIAL_CAPITAL - 1.0
        ) * 100.0,
        "CandidateTrades": len(candidates),
        "ExecutedTrades": len(ledger_df),
        "RejectedTrades": len(rejection_df),
        "RejectedByRVOL": (
            int(
                (
                    rejection_df["Reason"]
                    == "RVOL_J1_BELOW_THRESHOLD"
                ).sum()
            )
            if not rejection_df.empty
            else 0
        ),
        "RejectedByCapacity": (
            int(
                (
                    rejection_df["Reason"]
                    == "MAX_POSITIONS"
                ).sum()
            )
            if not rejection_df.empty
            else 0
        ),
        "RejectedByCash": (
            int(
                (
                    rejection_df["Reason"]
                    == "BUDGET_OR_CASH"
                ).sum()
            )
            if not rejection_df.empty
            else 0
        ),
        "WinRatePct": (
            len(winners)
            / len(ledger_df)
            * 100.0
        ),
        "ProfitFactor": profit_factor,
        "MaxDrawdownEUR": max_dd_eur,
        "MaxDrawdownPct": max_dd_pct,
        "LargestWinEUR": float(
            ledger_df["PnLEUR"].max()
        ),
        "LargestLossEUR": float(
            ledger_df["PnLEUR"].min()
        ),
        "AveragePnLEUR": float(
            ledger_df["PnLEUR"].mean()
        ),
        "AverageReturnPct": float(
            ledger_df["ReturnPct"].mean()
        ),
    }

    return (
        summary,
        ledger_df,
        rejection_df,
        decision_df,
    )


# ============================================================
# CONTROLE DE LA BASELINE
# ============================================================

def validate_reconstructed_baseline(
    reconstructed,
    official,
):
    checks = {
        "FinalCapitalEUR": abs(
            reconstructed["FinalCapitalEUR"]
            - official["FinalCapitalEUR"]
        ) <= BASELINE_TOLERANCE_EUR,

        "ExecutedTrades": (
            reconstructed["ExecutedTrades"]
            == official["ExecutedTrades"]
        ),

        "RejectedTrades": (
            reconstructed["RejectedTrades"]
            == official["RejectedTrades"]
        ),
    }

    if not all(checks.values()):
        raise RuntimeError(
            "\nLa baseline reconstruite ne correspond pas "
            "a la V1 officielle.\n"
            f"Capital reconstruit : "
            f"{reconstructed['FinalCapitalEUR']:.6f} EUR\n"
            f"Capital officiel   : "
            f"{official['FinalCapitalEUR']:.6f} EUR\n"
            f"Trades reconstruits : "
            f"{reconstructed['ExecutedTrades']}\n"
            f"Trades officiels    : "
            f"{official['ExecutedTrades']}\n"
            f"Rejets reconstruits : "
            f"{reconstructed['RejectedTrades']}\n"
            f"Rejets officiels    : "
            f"{official['RejectedTrades']}\n"
            "Le test V2 est arrete pour proteger "
            "la comparabilite."
        )


# ============================================================
# EXECUTION
# ============================================================

print("=" * 120)
print("HISTORICAL LAB - V2 TEST 02")
print("FILTRE PRE-ENTREE : RVOL J-1")
print()
print(
    f"REGLE : RVOL_J1 < {RVOL_THRESHOLD:.2f} "
    "-> REJET DU TRADE"
)
print("V1 FIGEE NON MODIFIEE")
print("=" * 120)


source_all, candidates = load_source_trades()
official_v1 = load_official_baseline()


print()
print("Entrees totales detectees :", len(source_all))
print("Trades termines candidats :", len(candidates))
print(
    "Trades avec donnees incompletes :",
    int(
        (
            source_all["TradeStatus"]
            == "DONNEES INCOMPLETES"
        ).sum()
    ),
)


(
    baseline_summary,
    baseline_ledger,
    baseline_rejections,
    baseline_decisions,
) = simulate_portfolio(
    candidates=candidates,
    version_name="V1_RECONSTRUITE",
    apply_rvol_filter=False,
)


validate_reconstructed_baseline(
    baseline_summary,
    official_v1,
)


print()
print("CONTROLE BASELINE V1 : OK")
print(
    "Capital reconstruit :",
    f"{baseline_summary['FinalCapitalEUR']:.3f} EUR",
)
print(
    "Capital officiel   :",
    f"{official_v1['FinalCapitalEUR']:.3f} EUR",
)
print(
    "Trades executes    :",
    baseline_summary["ExecutedTrades"],
)
print(
    "Trades rejetes     :",
    baseline_summary["RejectedTrades"],
)


(
    v2_summary,
    v2_ledger,
    v2_rejections,
    v2_decisions,
) = simulate_portfolio(
    candidates=candidates,
    version_name="V2_TEST02_RVOL",
    apply_rvol_filter=True,
)


# ============================================================
# COMPARAISON DES DECISIONS V1 / V2
# ============================================================

comparison_keys = [
    "Ticker",
    "SessionDate",
    "DecisionTime",
]

v1_actions = baseline_decisions[
    comparison_keys
    + [
        "PortfolioAction",
        "Reason",
        "Quantity",
    ]
].copy()

v1_actions = v1_actions.rename(
    columns={
        "PortfolioAction": "V1_Action",
        "Reason": "V1_Reason",
        "Quantity": "V1_Quantity",
    }
)

v2_actions = v2_decisions.copy()

v2_actions = v2_actions.rename(
    columns={
        "PortfolioAction": "V2_Action",
        "Reason": "V2_Reason",
        "Quantity": "V2_Quantity",
    }
)

detail = v2_actions.merge(
    v1_actions,
    on=comparison_keys,
    how="left",
)

detail["DecisionChanged"] = (
    detail["V1_Action"]
    != detail["V2_Action"]
)

detail = detail.sort_values(
    [
        "DecisionTime",
        "Ticker",
    ]
).reset_index(drop=True)


# ============================================================
# RESUME FINAL
# ============================================================

summary = pd.DataFrame(
    [
        {
            **baseline_summary,
            "OfficialBaselineCapitalEUR":
                official_v1["FinalCapitalEUR"],
            "BaselineControl":
                "OK",
        },
        {
            **v2_summary,
            "OfficialBaselineCapitalEUR":
                official_v1["FinalCapitalEUR"],
            "BaselineControl":
                "OK",
        },
    ]
)

summary["DeltaCapitalVsV1EUR"] = (
    summary["FinalCapitalEUR"]
    - baseline_summary["FinalCapitalEUR"]
)

summary["DeltaPnLVsV1EUR"] = (
    summary["PnLEUR"]
    - baseline_summary["PnLEUR"]
)

summary["DeltaReturnVsV1PctPoint"] = (
    summary["ReturnPct"]
    - baseline_summary["ReturnPct"]
)

summary["DeltaProfitFactorVsV1"] = (
    summary["ProfitFactor"]
    - baseline_summary["ProfitFactor"]
)

summary["DeltaMaxDrawdownPctPoint"] = (
    summary["MaxDrawdownPct"]
    - baseline_summary["MaxDrawdownPct"]
)


# ============================================================
# SAUVEGARDE
# ============================================================

detail.to_csv(
    OUTPUT_DETAIL,
    index=False,
    encoding="utf-8",
)

summary.to_csv(
    OUTPUT_SUMMARY,
    index=False,
    encoding="utf-8",
)

v2_ledger.to_csv(
    OUTPUT_EXECUTED,
    index=False,
    encoding="utf-8",
)

v2_rejections.to_csv(
    OUTPUT_REJECTIONS,
    index=False,
    encoding="utf-8",
)


# ============================================================
# AFFICHAGE
# ============================================================

print()
print("=" * 140)
print("TRADES REJETES PAR RVOL")
print("=" * 140)

rvol_rejections = v2_rejections.loc[
    v2_rejections["Reason"]
    == "RVOL_J1_BELOW_THRESHOLD"
].copy()

if rvol_rejections.empty:
    print("Aucun trade rejete par le filtre RVOL.")

else:
    rejection_columns = [
        "Ticker",
        "SessionDate",
        "Decision",
        "DecisionTime",
        "RVOL_J1",
        "Score_J1",
        "ATR14Pct_J1",
        "ExitReason",
    ]

    print(
        rvol_rejections[
            rejection_columns
        ]
        .round(3)
        .to_string(index=False)
    )


print()
print("=" * 180)
print("COMPARAISON V1 / V2 TEST 02")
print("=" * 180)

display_columns = [
    "Version",
    "RVOLThreshold",
    "FinalCapitalEUR",
    "PnLEUR",
    "ReturnPct",
    "CandidateTrades",
    "ExecutedTrades",
    "RejectedTrades",
    "RejectedByRVOL",
    "RejectedByCapacity",
    "WinRatePct",
    "ProfitFactor",
    "MaxDrawdownEUR",
    "MaxDrawdownPct",
    "DeltaCapitalVsV1EUR",
    "DeltaReturnVsV1PctPoint",
]

print(
    summary[
        display_columns
    ]
    .round(3)
    .to_string(index=False)
)


print()
print("=" * 140)
print("CHANGEMENTS DE DECISION V1 -> V2")
print("=" * 140)

changed = detail.loc[
    detail["DecisionChanged"]
].copy()

if changed.empty:
    print("Aucun changement de decision.")

else:
    changed_columns = [
        "Ticker",
        "SessionDate",
        "RVOL_J1",
        "V1_Action",
        "V1_Reason",
        "V2_Action",
        "V2_Reason",
    ]

    print(
        changed[
            changed_columns
        ]
        .round(3)
        .to_string(index=False)
    )


print()
print("Fichiers generes :")
print("Resume       :", OUTPUT_SUMMARY)
print("Detail       :", OUTPUT_DETAIL)
print("Trades V2    :", OUTPUT_EXECUTED)
print("Rejets V2    :", OUTPUT_REJECTIONS)

print("=" * 180)
print("FIN V2 TEST 02")
print("=" * 180)
