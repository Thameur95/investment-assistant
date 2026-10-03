import yfinance as yf

tickers = [x.strip() for x in open("stocks.txt", encoding="utf-8") if x.strip()]

print("===== INTRADAY MONITOR =====")

for ticker in tickers:
    try:
        data = yf.download(
            ticker,
            period="1d",
            interval="5m",
            progress=False,
            auto_adjust=True,
            prepost=False,
        )

        if len(data) == 0:
            print(f"{ticker}: aucune donnée intraday")
            continue

        print(f"\n{ticker}")

        first4 = data.head(4)

        for idx, row in first4.iterrows():
            print(
                f"{idx} | O={float(row['Open']):.2f} "
                f"H={float(row['High']):.2f} "
                f"L={float(row['Low']):.2f} "
                f"C={float(row['Close']):.2f} "
                f"V={int(row['Volume'])}"
            )

    except Exception as e:
        print(f"{ticker}: erreur {e}")
