import yfinance as yf
import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("DOWNLOAD HISTORY")
print("=" * 50)

ticker = "MSFT"

print(f"Téléchargement de {ticker}...")

data = yf.download(
    ticker,
    start="2019-01-01",
    auto_adjust=True,
    progress=False
)

print(data.head())
print()
print(f"Nombre de lignes : {len(data)}")
