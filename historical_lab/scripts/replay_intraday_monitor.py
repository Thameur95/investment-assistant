import pandas as pd
from pathlib import Path


def calculate_future_return(
    daily_prices,
    session_date,
    entry_price,
    horizon
):
    """
    Calcule la performance entre le prix d'entrée et la clôture
    de la horizon-ième séance de marché suivant la séance d'entrée.

    horizon=1 : clôture de J+1
    horizon=2 : clôture de J+2
    """

    if entry_price is None:
        return None

    if pd.isna(entry_price):
        return None

    session_timestamp = pd.Timestamp(
        session_date
    ).normalize()

    future_sessions = daily_prices.loc[
        daily_prices.index > session_timestamp
    ]

    if len(future_sessions) < horizon:
        return None

    future_close = float(
        future_sessions.iloc[horizon - 1]
    )

    return (
        (future_close / float(entry_price)) - 1
    ) * 100


print("=" * 70)
print("HISTORICAL LAB")
print("REPLAY EXACT DU MOTEUR INTRADAY DE PRODUCTION")
print("=" * 70)

BASE_DIR = Path(__file__).resolve().parent.parent

INTRADAY_DIR = (
    BASE_DIR
    / "data"
    / "intraday"
)

DAILY_FILE = (
    BASE_DIR
    / "data"
    / "processed"
    / "historical_indicators.csv"
)

daily_all = pd.read_csv(DAILY_FILE)

daily_all["Date"] = pd.to_datetime(
    daily_all["Date"],
    errors="coerce"
)

daily_all["Close"] = pd.to_numeric(
    daily_all["Close"],
    errors="coerce"
)

all_results = []


# ============================================================
# BOUCLE MULTI-TICKERS
# ============================================================

for intraday_file in sorted(
    INTRADAY_DIR.glob("*_5m.csv")
):

    ticker = (
        intraday_file.stem
        .replace("_5m", "")
    )

    print()
    print("=" * 50)
    print(f"Traitement : {ticker}")
    print("=" * 50)

    df = pd.read_csv(intraday_file)

    required_intraday_columns = {
        "Datetime_Paris",
        "Date_Paris",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume"
    }

    missing_columns = (
        required_intraday_columns
        - set(df.columns)
    )

    if missing_columns:
        print(
            "Colonnes intraday manquantes :",
            sorted(missing_columns)
        )
        continue

    daily = daily_all[
        daily_all["Ticker"] == ticker
    ].copy()

    if daily.empty:
        print("Aucune donnée quotidienne.")
        continue

    daily = (
        daily
        .dropna(
            subset=[
                "Date",
                "Close"
            ]
        )
        .sort_values("Date")
        .reset_index(drop=True)
    )

    if daily.empty:
        print("Aucune donnée quotidienne exploitable.")
        continue


    # ========================================================
    # SIGNAL ACHAT J-1 VERS SESSION CANDIDATE J
    # ========================================================

    daily["SessionDate"] = (
        daily["Date"]
        .shift(-1)
        .dt.strftime("%Y-%m-%d")
    )

    signal_rows = daily[
        daily["Signal"] == "ACHAT"
    ].copy()

    candidate_sessions = set(
        signal_rows[
            "SessionDate"
        ].dropna()
    )

    signal_price_by_session = (
        signal_rows
        .dropna(
            subset=[
                "SessionDate",
                "Close"
            ]
        )
        .drop_duplicates(
            subset="SessionDate",
            keep="last"
        )
        .set_index("SessionDate")["Close"]
        .to_dict()
    )

    print(
        "Signaux ACHAT :",
        len(signal_rows)
    )

    print(
        "Séances candidates :",
        len(candidate_sessions)
    )


    # ========================================================
    # SÉRIE DAILY UNIQUE POUR J+1 ET J+2
    # ========================================================

    daily_prices = (
        daily[
            [
                "Date",
                "Close"
            ]
        ]
        .dropna(
            subset=[
                "Date",
                "Close"
            ]
        )
        .sort_values("Date")
        .drop_duplicates(
            subset="Date",
            keep="last"
        )
        .set_index("Date")["Close"]
    )

    daily_prices.index = pd.to_datetime(
        daily_prices.index
    ).normalize()

    daily_prices = daily_prices.sort_index()

    print(
        "Séances daily uniques :",
        len(daily_prices)
    )


    # ========================================================
    # PRÉPARATION DES DONNÉES INTRADAY
    # ========================================================

    df["Datetime_Paris"] = pd.to_datetime(
        df["Datetime_Paris"],
        errors="coerce"
    )

    df["Date_Paris"] = (
        df["Date_Paris"]
        .astype(str)
        .str[:10]
    )

    numeric_columns = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume"
    ]

    for column in numeric_columns:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(
        subset=[
            "Datetime_Paris",
            "Date_Paris",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]
    )

    sessions = []


    # ========================================================
    # REPLAY JOUR PAR JOUR
    # ========================================================

    for date, day in df.groupby("Date_Paris"):

        date = str(date)[:10]

        if date not in candidate_sessions:
            continue

        day = (
            day
            .sort_values("Datetime_Paris")
            .reset_index(drop=True)
        )

        if len(day) < 4:
            continue


        # ====================================================
        # PRIX DU SIGNAL DE J-1
        # ====================================================

        signal_price = signal_price_by_session.get(
            date
        )

        if signal_price is None:
            continue

        signal_price = float(signal_price)

        if signal_price <= 0:
            continue


        # ====================================================
        # OUVERTURE ET GAP
        # ====================================================

        opening = float(
            day["Open"].iloc[0]
        )

        if opening <= 0:
            continue

        gap_pct = (
            (opening / signal_price) - 1
        ) * 100


        # ====================================================
        # OPENING RANGE DE 20 MINUTES
        # 4 BOUGIES DE 5 MINUTES
        # ====================================================

        first4 = day.iloc[:4].copy()

        or_high = float(
            first4["High"].max()
        )

        or_low = float(
            first4["Low"].min()
        )

        close4 = float(
            first4["Close"].iloc[-1]
        )

        red4 = int(
            (
                first4["Close"]
                < first4["Open"]
            ).sum()
        )


        # ====================================================
        # VOLUME MOYEN DES 4 PREMIÈRES BOUGIES
        # ====================================================

        valid_initial_volume = first4.loc[
            first4["Volume"] > 0,
            "Volume"
        ]

        if valid_initial_volume.empty:
            vol4 = 0.0
        else:
            vol4 = float(
                valid_initial_volume.mean()
            )


        # ====================================================
        # VWAP CUMULÉ
        # ====================================================

        typical_price = (
            day["High"]
            + day["Low"]
            + day["Close"]
        ) / 3

        cumulative_volume = (
            day["Volume"].cumsum()
        )

        cumulative_vwap_value = (
            typical_price
            * day["Volume"]
        ).cumsum()

        safe_volume = cumulative_volume.replace(
            0,
            float("nan")
        )

        day["VWAP"] = (
            cumulative_vwap_value
            / safe_volume
        )


        # ====================================================
        # VARIABLES DE DÉCISION
        # ====================================================

        decision = "EN ATTENTE"
        reason = "Prix dans Opening Range"

        decision_time = None
        decision_price = None
        decision_volume = None
        decision_vwap = None

        invalid_pos = 3


        # ====================================================
        # FILTRES INITIAUX
        # ====================================================

        if gap_pct <= -3:

            decision = "SETUP INITIAL INVALIDE"
            reason = "Gap baissier >= 3%"

        elif close4 <= opening * 0.98:

            decision = "SETUP INITIAL INVALIDE"
            reason = "Baisse >= 2% apres 20 minutes"

        elif (
            red4 >= 3
            and close4 < opening
        ):

            decision = "SETUP INITIAL INVALIDE"
            reason = "Au moins 3 bougies rouges sur 4"

        else:

            # =================================================
            # RECHERCHE ACHAT CONFIRMÉ
            # PREMIÈRE HEURE, SOIT 12 BOUGIES
            # =================================================

            first_hour = day.iloc[:12].copy()

            for pos, row in (
                first_hour
                .iloc[4:]
                .iterrows()
            ):

                current_low = float(
                    row["Low"]
                )

                current_close = float(
                    row["Close"]
                )

                current_volume = float(
                    row["Volume"]
                )

                current_vwap = row["VWAP"]

                if pd.isna(current_vwap):
                    continue

                current_vwap = float(
                    current_vwap
                )


                # =============================================
                # INVALIDATION DURANT LA PREMIÈRE HEURE
                # =============================================

                if (
                    current_low < or_low
                    or current_close <= opening * 0.98
                ):

                    decision = "SETUP INITIAL INVALIDE"
                    reason = "Cassure baissiere Opening Range"

                    decision_time = row[
                        "Datetime_Paris"
                    ]

                    invalid_pos = int(pos)
                    break


                # =============================================
                # ACHAT CONFIRMÉ
                # =============================================

                if (
                    current_close > or_high
                    and current_close > current_vwap
                    and current_volume >= vol4
                ):

                    decision = "ACHAT CONFIRME"

                    reason = (
                        "Cassure haussiere "
                        "+ VWAP + volume"
                    )

                    decision_time = row[
                        "Datetime_Paris"
                    ]

                    decision_price = current_close
                    decision_volume = current_volume
                    decision_vwap = current_vwap

                    break


            # =================================================
            # PAS DE CONFIRMATION APRÈS UNE HEURE
            # =================================================

            if (
                decision == "EN ATTENTE"
                and len(day) >= 12
            ):

                decision = "SETUP INITIAL INVALIDE"

                reason = (
                    "Aucune confirmation "
                    "pendant une heure"
                )

                invalid_pos = 11


        # ====================================================
        # RECHERCHE ACHAT RÉACTIF
        # ====================================================

        if decision == "SETUP INITIAL INVALIDE":

            reactive_start = invalid_pos + 1

            for _, row in (
                day
                .iloc[reactive_start:]
                .iterrows()
            ):

                current_close = float(
                    row["Close"]
                )

                current_volume = float(
                    row["Volume"]
                )

                current_vwap = row["VWAP"]

                if pd.isna(current_vwap):
                    continue

                current_vwap = float(
                    current_vwap
                )

                if (
                    current_close > or_high
                    and current_close > current_vwap
                    and current_volume >= vol4 * 1.5
                ):

                    decision = "ACHAT REACTIVE"

                    reason = (
                        "Retournement confirme : "
                        "OR High + VWAP + volume"
                    )

                    decision_time = row[
                        "Datetime_Paris"
                    ]

                    decision_price = current_close
                    decision_volume = current_volume
                    decision_vwap = current_vwap

                    break


        # ====================================================
        # PERFORMANCE DES ACHATS
        # ====================================================

        close_final = float(
            day["Close"].iloc[-1]
        )

        intraday_return_pct = None
        return_j1_pct = None
        return_j2_pct = None

        is_buy = decision in [
            "ACHAT CONFIRME",
            "ACHAT REACTIVE"
        ]

        if (
            is_buy
            and decision_price is not None
        ):

            intraday_return_pct = (
                (
                    close_final
                    / decision_price
                )
                - 1
            ) * 100

            return_j1_pct = calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=decision_price,
                horizon=1
            )

            return_j2_pct = calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=decision_price,
                horizon=2
            )


        # ====================================================
        # PERFORMANCE DES SETUPS REJETÉS
        # ====================================================

        rejected_intraday_pct = None
        rejected_j1_pct = None
        rejected_j2_pct = None

        if not is_buy:

            rejected_intraday_pct = (
                (close_final / opening) - 1
            ) * 100

            rejected_j1_pct = calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=opening,
                horizon=1
            )

            rejected_j2_pct = calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=opening,
                horizon=2
            )


        # ====================================================
        # ENREGISTREMENT
        # ====================================================

        sessions.append(
            {
                "Ticker": ticker,
                "Date": date,
                "SignalPrice": signal_price,
                "OpeningPrice": opening,
                "GapPct": gap_pct,
                "OR_High": or_high,
                "OR_Low": or_low,
                "Close20Min": close4,
                "RedCandles4": red4,
                "InitialAvgVolume": vol4,
                "Decision": decision,
                "Reason": reason,
                "DecisionTime": decision_time,
                "DecisionPrice": decision_price,
                "DecisionVolume": decision_volume,
                "DecisionVWAP": decision_vwap,
                "CloseFinal": close_final,
                "IntradayReturnPct": intraday_return_pct,
                "ReturnJ1Pct": return_j1_pct,
                "ReturnJ2Pct": return_j2_pct,
                "RejectedIntradayPct": rejected_intraday_pct,
                "RejectedJ1Pct": rejected_j1_pct,
                "RejectedJ2Pct": rejected_j2_pct,
                "Bars": len(day)
            }
        )


    # ========================================================
    # FIN DU TICKER
    # ========================================================

    if sessions:

        ticker_result = pd.DataFrame(
            sessions
        )

        all_results.append(
            ticker_result
        )

        print(
            f"Résultats conservés pour "
            f"{ticker} : "
            f"{len(ticker_result)}"
        )

        print(
            ticker_result[
                "Decision"
            ].value_counts()
        )

    else:

        print(
            f"Aucune séance exploitable "
            f"pour {ticker}"
        )


# ============================================================
# RÉSULTATS GLOBAUX
# ============================================================

if not all_results:

    print()
    print("Aucun résultat trouvé.")

    raise SystemExit(0)

result = pd.concat(
    all_results,
    ignore_index=True
)


# ============================================================
# FUNNEL GLOBAL
# ============================================================

print()
print("=" * 70)
print("FUNNEL GLOBAL DU MOTEUR")
print("=" * 70)

print(
    "Nombre total de séances :",
    len(result)
)

print()

decision_counts = (
    result["Decision"]
    .value_counts()
)

print(decision_counts)


# ============================================================
# ACHATS ET REJETS
# ============================================================

buy_mask = result[
    "Decision"
].isin(
    [
        "ACHAT CONFIRME",
        "ACHAT REACTIVE"
    ]
)

buy_result = result[
    buy_mask
].copy()

rejected_result = result[
    ~buy_mask
].copy()

print()

print(
    "Nombre total d'achats :",
    len(buy_result)
)

print(
    "Taux de déclenchement achat :",
    round(
        len(buy_result)
        / len(result)
        * 100,
        1
    ),
    "%"
)


# ============================================================
# PERFORMANCE GLOBALE DES ACHATS
# ============================================================

print()
print("=" * 70)
print("PERFORMANCE DES ACHATS")
print("=" * 70)

for column, label in [
    (
        "IntradayReturnPct",
        "J / clôture"
    ),
    (
        "ReturnJ1Pct",
        "J+1"
    ),
    (
        "ReturnJ2Pct",
        "J+2"
    )
]:

    valid = buy_result[
        column
    ].dropna()

    if valid.empty:
        continue

    print()
    print(label)

    print(
        "Nombre de cas :",
        len(valid)
    )

    print(
        "Performance moyenne :",
        round(
            valid.mean(),
            3
        ),
        "%"
    )

    print(
        "Performance médiane :",
        round(
            valid.median(),
            3
        ),
        "%"
    )

    print(
        "Taux positif :",
        round(
            (
                valid > 0
            ).mean()
            * 100,
            1
        ),
        "%"
    )


# ============================================================
# ACHAT CONFIRMÉ VS ACHAT RÉACTIF
# ============================================================

print()
print("=" * 70)
print("PERFORMANCE PAR TYPE D'ACHAT")
print("=" * 70)

for decision_type in [
    "ACHAT CONFIRME",
    "ACHAT REACTIVE"
]:

    subset = result[
        result["Decision"] == decision_type
    ].copy()

    if subset.empty:
        continue

    print()
    print(decision_type)

    print(
        "Nombre de trades :",
        len(subset)
    )

    for column, label in [
        (
            "IntradayReturnPct",
            "J"
        ),
        (
            "ReturnJ1Pct",
            "J+1"
        ),
        (
            "ReturnJ2Pct",
            "J+2"
        )
    ]:

        valid = subset[
            column
        ].dropna()

        if valid.empty:
            continue

        print(
            f"{label} | "
            f"Moyenne {valid.mean():+.3f}% | "
            f"Médiane {valid.median():+.3f}% | "
            f"Win rate "
            f"{(valid > 0).mean() * 100:.1f}%"
        )


# ============================================================
# AUTOPSIE DES SETUPS REJETÉS
# ============================================================

print()
print("=" * 70)
print("AUTOPSIE DES SETUPS REJETES")
print("=" * 70)

print(
    "Nombre de setups rejetés :",
    len(rejected_result)
)

for column, label in [
    (
        "RejectedIntradayPct",
        "J depuis ouverture"
    ),
    (
        "RejectedJ1Pct",
        "J+1 depuis ouverture"
    ),
    (
        "RejectedJ2Pct",
        "J+2 depuis ouverture"
    )
]:

    valid = rejected_result[
        column
    ].dropna()

    if valid.empty:
        continue

    print()
    print(label)

    print(
        "Nombre de cas :",
        len(valid)
    )

    print(
        "Performance moyenne :",
        round(
            valid.mean(),
            3
        ),
        "%"
    )

    print(
        "Performance médiane :",
        round(
            valid.median(),
            3
        ),
        "%"
    )

    print(
        "Taux positif :",
        round(
            (
                valid > 0
            ).mean()
            * 100,
            1
        ),
        "%"
    )


# ============================================================
# RAISONS DE REJET
# ============================================================

print()
print("=" * 70)
print("RAISONS DE REJET")
print("=" * 70)

if rejected_result.empty:

    print("Aucun setup rejeté.")

else:

    rejection_stats = (
        rejected_result
        .groupby("Reason")
        .agg(
            Cas=(
                "Ticker",
                "count"
            ),
            J=(
                "RejectedIntradayPct",
                "mean"
            ),
            J1=(
                "RejectedJ1Pct",
                "mean"
            ),
            J2=(
                "RejectedJ2Pct",
                "mean"
            )
        )
        .sort_values(
            "Cas",
            ascending=False
        )
    )

    print(
        rejection_stats.round(3)
    )


# ============================================================
# PERFORMANCE DES ACHATS PAR TICKER
# ============================================================

print()
print("=" * 70)
print("PERFORMANCE DES ACHATS PAR TICKER")
print("=" * 70)

if buy_result.empty:

    print("Aucun achat déclenché.")

else:

    ticker_stats = (
        buy_result
        .groupby("Ticker")
        .agg(
            Trades=(
                "Ticker",
                "count"
            ),
            J=(
                "IntradayReturnPct",
                "mean"
            ),
            J1=(
                "ReturnJ1Pct",
                "mean"
            ),
            J2=(
                "ReturnJ2Pct",
                "mean"
            )
        )
        .sort_values(
            "J2",
            ascending=False
        )
    )

    print(
        ticker_stats.round(3)
    )


# ============================================================
# CONTRÔLE AUTOMATIQUE J+1 / J+2
# ============================================================

print()
print("=" * 70)
print("CONTROLE J+1 / J+2")
print("=" * 70)

comparable = result[
    result["ReturnJ1Pct"].notna()
    & result["ReturnJ2Pct"].notna()
].copy()

if comparable.empty:

    print(
        "Aucun achat ne dispose simultanément "
        "de J+1 et J+2."
    )

else:

    comparable["DifferenceJ1J2"] = (
        comparable["ReturnJ1Pct"]
        - comparable["ReturnJ2Pct"]
    ).abs()

    identical_mask = (
        comparable["DifferenceJ1J2"]
        < 0.000001
    )

    identical_count = int(
        identical_mask.sum()
    )

    print(
        "Trades comparables :",
        len(comparable)
    )

    print(
        "J+1 exactement égal à J+2 :",
        identical_count
    )

    if identical_count > 0:

        print()
        print(
            comparable.loc[
                identical_mask,
                [
                    "Ticker",
                    "Date",
                    "DecisionPrice",
                    "ReturnJ1Pct",
                    "ReturnJ2Pct"
                ]
            ].head(20)
        )


# ============================================================
# CONTRÔLE DES REJETS J+1 / J+2
# ============================================================

print()
print("=" * 70)
print("CONTROLE DES REJETS J+1 / J+2")
print("=" * 70)

rejected_comparable = rejected_result[
    rejected_result["RejectedJ1Pct"].notna()
    & rejected_result["RejectedJ2Pct"].notna()
].copy()

if rejected_comparable.empty:

    print(
        "Aucun rejet ne dispose simultanément "
        "de J+1 et J+2."
    )

else:

    rejected_comparable["DifferenceJ1J2"] = (
        rejected_comparable["RejectedJ1Pct"]
        - rejected_comparable["RejectedJ2Pct"]
    ).abs()

    rejected_identical = int(
        (
            rejected_comparable[
                "DifferenceJ1J2"
            ]
            < 0.000001
        ).sum()
    )

    print(
        "Rejets comparables :",
        len(rejected_comparable)
    )

    print(
        "J+1 exactement égal à J+2 :",
        rejected_identical
    )


print()
print("=" * 70)
print("FIN DU REPLAY EXACT")
print("=" * 70)
