import yfinance as yf; data = yf.download("NVDA", period="5d", progress=False, auto_adjust=True); print("NVDA :", round(float(data["Close"].iloc[-1].squeeze()), 2))
