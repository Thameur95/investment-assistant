import csv
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
SOURCE_SCRIPT = ROOT / "virtual_portfolio.py"


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


if not SOURCE_SCRIPT.exists():
    raise FileNotFoundError("virtual_portfolio.py introuvable à la racine du dépôt")

now_paris = datetime.now(ZoneInfo("Europe/Paris"))
today = now_paris.strftime("%Y-%m-%d")
decision_time = now_paris.isoformat()

with tempfile.TemporaryDirectory(prefix="e2e_portfolio_") as temp_directory:
    test_dir = Path(temp_directory)

    shutil.copy2(SOURCE_SCRIPT, test_dir / "virtual_portfolio.py")

    write_csv(
        test_dir / "portfolio_cash.csv",
        [
            "initial_capital_eur",
            "available_cash_eur",
            "invested_cost_eur",
            "market_value_eur",
            "realized_pnl_eur",
            "unrealized_pnl_eur",
            "fees_paid_eur",
            "total_equity_eur",
        ],
        [
            {
                "initial_capital_eur": 3000,
                "available_cash_eur": 3000,
                "invested_cost_eur": 0,
                "market_value_eur": 0,
                "realized_pnl_eur": 0,
                "unrealized_pnl_eur": 0,
                "fees_paid_eur": 0,
                "total_equity_eur": 3000,
            }
        ],
    )

    portfolio_fields = [
        "ticker",
        "signal_date",
        "decision",
        "decision_time",
        "entry_date",
        "entry_price",
        "currency",
        "eurusd_entry",
        "quantity",
        "gross_buy_value_local",
        "gross_buy_value_eur",
        "buy_fee_eur",
        "total_buy_cost_eur",
        "target_price",
        "stop_price",
        "status",
        "last_price",
        "market_value_eur",
        "unrealized_pnl_eur",
        "exit_date",
        "exit_price",
        "gross_sell_value_local",
        "gross_sell_value_eur",
        "sell_fee_eur",
        "net_sell_proceeds_eur",
        "realized_pnl_eur",
        "exit_reason",
    ]

    write_csv(
        test_dir / "virtual_portfolio.csv",
        portfolio_fields,
        [],
    )

    write_csv(
        test_dir / "signals_history.csv",
        ["ticker", "date", "score"],
        [
            {
                "ticker": "AIR.PA",
                "date": today,
                "score": 99,
            }
        ],
    )

    write_csv(
        test_dir / "intraday_decisions.csv",
        [
            "ticker",
            "signal_date",
            "session_date",
            "decision",
            "next_state",
            "decision_time",
            "decision_close",
            "decision_volume",
        ],
        [
            {
                "ticker": "AIR.PA",
                "signal_date": today,
                "session_date": today,
                "decision": "ACHAT CONFIRME",
                "next_state": "POSITION A OUVRIR",
                "decision_time": decision_time,
                "decision_close": "100.00",
                "decision_volume": "1000000",
            }
        ],
    )

    print("=" * 80)
    print("TEST E2E ISOLE DU PORTEFEUILLE VIRTUEL")
    print("Aucun fichier réel ne sera modifié")
    print("=" * 80)

    result = subprocess.run(
        [sys.executable, "virtual_portfolio.py"],
        cwd=test_dir,
        text=True,
        capture_output=True,
    )

    print(result.stdout)

    if result.stderr:
        print("SORTIE ERREUR :")
        print(result.stderr)

    if result.returncode != 0:
        raise SystemExit("ECHEC E2E : virtual_portfolio.py a retourné une erreur")

    positions = read_csv(test_dir / "virtual_portfolio.csv")
    cash_rows = read_csv(test_dir / "portfolio_cash.csv")

    open_positions = [
        position
        for position in positions
        if position.get("ticker") == "AIR.PA"
        and position.get("status") == "OPEN"
    ]

    if not open_positions:
        raise SystemExit(
            "ECHEC E2E : aucune position AIR.PA ouverte après ACHAT CONFIRME"
        )

    if not cash_rows:
        raise SystemExit("ECHEC E2E : portfolio_cash.csv est vide")

    remaining_cash = float(cash_rows[0]["available_cash_eur"])
    quantity = int(float(open_positions[0]["quantity"]))
    total_cost = float(open_positions[0]["total_buy_cost_eur"])

    if quantity < 1:
        raise SystemExit("ECHEC E2E : quantité achetée invalide")

    if not 0 <= remaining_cash < 3000:
        raise SystemExit(
            f"ECHEC E2E : cash incohérent après achat, valeur={remaining_cash}"
        )

    if total_cost <= 0:
        raise SystemExit("ECHEC E2E : coût d'achat invalide")

    print("=" * 80)
    print("TEST E2E REUSSI")
    print(f"Position créée : AIR.PA")
    print(f"Quantité       : {quantity}")
    print(f"Coût total     : {total_cost:.2f} EUR")
    print(f"Cash restant   : {remaining_cash:.2f} EUR")
    print("Fichiers V1 réels : NON MODIFIES")
    print("=" * 80)
