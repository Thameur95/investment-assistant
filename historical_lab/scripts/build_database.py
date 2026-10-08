import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("BUILD DATABASE")
print("=" * 50)

BASE_DIR = Path(__file__).resolve().parent.parent

RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"

PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

database = []

for file in RAW_DIR.glob("*.csv"):

    ticker = file.stem

    print(f"Lecture : {ticker}")

    try:

        df = pd.read_csv(file)

        if len(df) == 0:
            continue

        if str(df.iloc[0, 0]).strip() == "":
            df = df.iloc[1:].reset_index(drop=True)

        df["Ticker"] = ticker

        database.append(df)

    except Exception as e:

        print(f"Erreur {ticker} : {e}")

if not database:
    raise Exception("Aucune donnée chargée")

final_df = pd.concat(database, ignore_index=True)

output_file = PROCESSED_DIR / "historical_database.csv"

final_df.to_csv(output_file, index=False)

print()
print("=" * 50)
print("BASE HISTORIQUE CRÉÉE")
print("=" * 50)
print(f"Lignes : {len(final_df)}")
print(f"Fichier : {output_file}")
