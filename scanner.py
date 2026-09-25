import yfinance as yf
 
with open("stocks.txt", "r") as file:tickers = [line.strip() for line in file if line.strip()]
 
for ticker in tickers:
  try:
  data = yf.download(ticker,period="5d",progress=False,auto_adjust=True)
  price = float(data["Close"].iloc[-1].squeeze())
  print(f"{ticker}: {price:.2f}")
  except Exception:
print(f"{ticker}: ERROR")
