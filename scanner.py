import yfinance as yf
 
with open("stocks.txt", "r", encoding="utf-8") as file:
tickers = [line.strip() for line in file if line.strip()]
 
print("=== INVESTMENT ASSISTANT V1 ===")
 
for ticker in tickers:
try:
data = yf.download(
ticker,
period="3mo",
interval="1d",
progress=False,
auto_adjust=True
)
 
if len(data) < 21:
print(f"{ticker}: donnees insuffisantes")
continue
 
close = data["Close"].squeeze()
volume = data["Volume"].squeeze()
 
current_price = float(close.iloc[-1])
price_5_days_ago = float(close.iloc[-6])
moving_average_20 = float(close.tail(20).mean())
average_volume_20 = float(volume.iloc[-21:-1].mean())
current_volume = float(volume.iloc[-1])
 
performance_5_days = (
(current_price / price_5_days_ago) - 1
) * 100
 
score = 0
reasons = []
 
if current_price > moving_average_20:
score += 40
reasons.append("Prix superieur a la MM20")
 
if current_volume > average_volume_20:
score += 30
reasons.append("Volume superieur a la moyenne")
 
if performance_5_days > 3:
score += 30
reasons.append("Performance 5 jours superieure a 3 %")
 
signal = "ACHAT" if score >= 70 else "SURVEILLANCE"
 
print("")
print(f"Action : {ticker}")
print(f"Prix : {current_price:.2f}")
print(f"Performance 5 jours : {performance_5_days:.2f} %")
print(f"Score : {score}/100")
print(f"Signal : {signal}")
 
if reasons:
print("Motifs :")
for reason in reasons:
print(f"- {reason}")
 
print("-" * 40)
 
except Exception as error:
print(f"{ticker}: ERREUR - {error}")
