import pandas as pd
import yfinance as yf
from pathlib import Path

print("=" * 60)
print("HISTORICAL LAB")
print("DOWNLOAD INTRADAY 5 MINUTES")
print("=" * 60)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "data" / "intraday"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ticker = "MSFT"

print()
print(f"Téléchargement de {ticker} en bougies 5 minutes...")

data = yf.download(
    ticker,
    period="60d",
    interval="5m",
    auto_adjust=True,
    prepost=False,
    progress=False,
    threads=False
)

if data.empty:
    raise RuntimeError(
        f"Aucune donnée intraday disponible pour {ticker}"
    )

print()
print("Colonnes reçues :")
print(data.columns)

if isinstance(data.columns, pd.MultiIndex):
    data.columns = data.columns.get_level_values(0)

data = data.reset_index()

datetime_column = data.columns[0]

data = data.rename(
    columns={
        datetime_column: "Datetime"
    }
)

columns_to_keep = [
    "Datetime",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume"
]

missing_columns = [
    column
    for column in columns_to_keep
    if column not in data.columns
]

if missing_columns:
    raise RuntimeError(
        "Colonnes manquantes : "
        + ", ".join(missing_columns)
    )

data = data[columns_to_keep].copy()

data["Datetime"] = pd.to_datetime(
    data["Datetime"],
    errors="coerce",
    utc=True
)

data = data.dropna(
    subset=["Datetime"]
)

data["Datetime_Paris"] = (
    data["Datetime"]
    .dt.tz_convert("Europe/Paris")
)

data["Date_Paris"] = (
    data["Datetime_Paris"]
    .dt.strftime("%Y-%m-%d")
)

data["Time_Paris"] = (
    data["Datetime_Paris"]
    .dt.strftime("%H:%M:%S")
)

data["Ticker"] = ticker

data = data[
    [
        "Ticker",
        "Datetime",
        "Datetime_Paris",
        "Date_Paris",
        "Time_Paris",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume"
    ]
]

output_file = OUTPUT_DIR / f"{ticker}_5m.csv"

data.to_csv(
    output_file,
    index=False
)

print()
print("=" * 60)
print("DONNÉES INTRADAY SAUVEGARDÉES")
print("=" * 60)

print(f"Ticker : {ticker}")
print(f"Nombre de bougies : {len(data)}")
print(
    "Première bougie Paris :",
    data["Datetime_Paris"].min()
)
print(
    "Dernière bougie Paris :",
    data["Datetime_Paris"].max()
)
print(
    "Nombre de séances :",
    data["Date_Paris"].nunique()
)
print(f"Fichier : {output_file}")

print()
print("Premières lignes :")
print(data.head())
