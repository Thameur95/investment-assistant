import yfinance as yf
 
stocks = []
 
with open("stocks.txt", "r") as f:
stocks = [line.strip() for line in f if line.strip()]
 
print("=== STOCK SCANNER ===")
 
for ticker in stocks:
try:
stock = yf.Ticker(ticker)
price = stock.history(period="1d")["Close"].iloc[-1]
print(f"{ticker}: {price:.2f}")
except Exception as e:
print(f"{ticker}: ERROR")
