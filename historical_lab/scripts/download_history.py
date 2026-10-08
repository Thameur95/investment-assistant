import yfinance as yf
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("DOWNLOAD HISTORY")
print("=" * 50)

tickers_file = Path("../data/tickers.txt")

if not tickers_file.exists():
    raise FileNotFoundError(f"Fichier introuvable : {tickers_file}")

with open(tickers_file, "r", encoding="utf-8") as f:
    tickers = [x.strip() for x in f if x.strip()]

print(f"Nombre de tickers : {len(tickers)}")

for ticker in tickers:
    print(ticker)
