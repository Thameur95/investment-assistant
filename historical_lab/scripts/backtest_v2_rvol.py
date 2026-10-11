from pathlib import Path
import numpy as np
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = BASE_DIR / "results"

SOURCE_FILE = RESULTS_DIR / "risk_manager_source_trades.csv"
LEDGER_FILE = RESULTS_DIR / "risk_manager_ledger.csv"

OUTPUT_DETAIL = RESULTS_DIR / "v2_test02_rvol_detail.csv"
OUTPUT_SUMMARY = RESULTS_DIR / "v2_test02_rvol_summary.csv"

MODEL = "FIXED_1000"

INITIAL_CAPITAL = 3000.0
MAX_POSITIONS = 3
FIXED_BUDGET = 1000.0

FEE_SIDE_PCT = 0.05

RVOL_THRESHOLD = 0.60


# ============================================================
# HELPERS
# ============================================================

def load_data():

    if not SOURCE_FILE.exists():
        raise FileNotFoundError(
            f"Source introuvable : {SOURCE_FILE}"
        )

    if not LEDGER_FILE.exists():
        raise FileNotFoundError(
            f"Ledger introuvable : {LEDGER_FILE}"
        )

    source = pd.read_csv(SOURCE_FILE)
    ledger = pd.read_csv(LEDGER_FILE)

    required_source = [
        "Ticker",
        "SessionDate",
        "Decision",
        "DecisionTime",
        "ExitTime",
        "ExitReason",
        "EffectiveEntryPrice",
        "EffectiveExitPrice",
        "SLPct",
        "RVOL_J1",
        "Score_J1",
        "RSI14_J1",
        "Var5j_J1",
        "ATR14Pct_J1",
        "DistanceMM20_J1_Pct",
    ]

    missing = [
        col
        for col in required_source
        if col not in source.columns
    ]

    if missing:
        raise ValueError(
            "Colonnes manquantes dans "
            "risk_manager_source_trades.csv : "
            + ", ".join(missing)
        )

    if "Model" not in ledger.columns:
        raise ValueError(
            "Colonne Model absente de "
            "risk_manager_ledger.csv"
        )

    source["DecisionTime"] = pd.to_datetime(
        source["DecisionTime"],
        errors="coerce",
    )

    source["ExitTime"] = pd.to_datetime(
        source["ExitTime"],
        errors="coerce",
    )

    ledger["DecisionTime"] = pd.to_datetime(
        ledger["DecisionTime"],
        errors="coerce",
    )

    ledger["ExitTime"] = pd.to_datetime(
        ledger["ExitTime"],
        errors="coerce",
    )

    source["RVOL_J1"] = pd.to_numeric(
        source["RVOL_J1"],
        errors="coerce",
    )

    source["EffectiveEntryPrice"] = pd.to_numeric(
        source["EffectiveEntryPrice"],
        errors="coerce",
    )

    source["EffectiveExitPrice"] = pd.to_numeric(
        source["EffectiveExitPrice"],
        errors="coerce",
    )

    source["SLPct"] = pd.to_numeric(
        source["SLPct"],
        errors="coerce",
    )

    return source, ledger


def get_v1_executed_trades(source, ledger):

    fixed_ledger = ledger.loc[
        ledger["Model"] == MODEL
    ].copy()

    if fixed_ledger.empty:
        raise ValueError(
            "Aucun trade FIXED_1000 trouve "
            "dans risk_manager_ledger.csv"
        )

    keys = fixed_ledger[
        [
            "Ticker",
            "SessionDate",
            "DecisionTime",
        ]
    ].drop_duplicates()

    executed = source.merge(
        keys,
        on=[
            "Ticker",
            "SessionDate",
            "DecisionTime",
        ],
        how="inner",
    )

    executed = executed.loc[
        executed["TradeStatus"] == "TERMINE"
    ].copy()

    executed = (
        executed
        .sort_values(
            [
                "DecisionTime",
                "Ticker",
            ]
        )
        .reset_index(drop)
    )
