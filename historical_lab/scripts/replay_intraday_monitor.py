import pandas as pd
from pathlib import Path

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
    daily_all["Date"]
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

    df = pd.read_csv(
        intraday_file
    )

    daily = daily_all[
        daily_all["Ticker"] == ticker
    ].copy()

    if daily.empty:

        print(
            "Aucune donnée quotidienne."
        )

        continue

    daily = (
        daily
        .sort_values("Date")
        .reset_index(drop=True)
    )


    # ========================================================
    # LE SIGNAL ACHAT DU JOUR J-1 DEVIENT CANDIDAT LE JOUR J
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
        .dropna(subset=["SessionDate"])
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
    # DONNÉES DAILY POUR J+1 / J+2
    # ========================================================

    daily["DateString"] = (
        daily["Date"]
        .dt.strftime("%Y-%m-%d")
    )

    daily_close_by_date = (
        daily
        .set_index("DateString")["Close"]
        .to_dict()
    )

    daily_dates = (
        daily["DateString"]
        .tolist()
    )

    daily_date_positions = {
        date: position
        for position, date
        in enumerate(daily_dates)
    }


    # ========================================================
    # DONNÉES INTRADAY
    # ========================================================

    df["Datetime_Paris"] = pd.to_datetime(
        df["Datetime_Paris"]
    )

    df["Date_Paris"] = (
        df["Date_Paris"]
        .astype(str)
    )

    sessions = []


    # ========================================================
    # REPLAY JOUR PAR JOUR
    # ========================================================

    for date, day in df.groupby(
        "Date_Paris"
    ):

        if date not in candidate_sessions:
            continue

        day = (
            day
            .sort_values(
                "Datetime_Paris"
            )
            .reset_index(drop=True)
        )

        # Le moteur réel attend au moins
        # les 4 premières bougies.
        if len(day) < 4:
            continue


        # ====================================================
        # PRIX DU SIGNAL J-1
        # ====================================================

        signal_price = (
            signal_price_by_session
            .get(date)
        )

        if signal_price is None:
            continue

        signal_price = float(
            signal_price
        )


        # ====================================================
        # OPENING + GAP
        # ====================================================

        opening = float(
            day["Open"].iloc[0]
        )

        gap_pct = (
            (
                opening
                / signal_price
            )
            - 1
        ) * 100


        # ====================================================
        # OPENING RANGE = 4 BOUGIES = 20 MIN
        # EXACTEMENT COMME LE MOTEUR RÉEL
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

        valid_initial_volume = (
            first4.loc[
                first4["Volume"] > 0,
                "Volume"
            ]
        )

        if len(valid_initial_volume) > 0:

            vol4 = float(
                valid_initial_volume.mean()
            )

        else:

            vol4 = 0.0


        # ====================================================
        # VWAP COMPLET
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
            (
                typical_price
                * day["Volume"]
            ).cumsum()
        )

        safe_volume = (
            cumulative_volume
            .replace(0, float("nan"))
        )

        day["VWAP"] = (
            cumulative_vwap_value
            / safe_volume
        )


        # ====================================================
        # VARIABLES DE DÉCISION
        # ====================================================

        decision = "EN ATTENTE"

        reason = (
            "Prix dans Opening Range"
        )

        decision_time = None
        decision_price = None
        decision_volume = None
        decision_vwap = None

        invalid_pos = 3


        # ====================================================
        # FILTRES INITIAUX
        # ====================================================

        if gap_pct <= -3:

            decision = (
                "SETUP INITIAL INVALIDE"
            )

            reason = (
                "Gap baissier >= 3%"
            )


        elif close4 <= opening * 0.98:

            decision = (
                "SETUP INITIAL INVALIDE"
            )

            reason = (
                "Baisse >= 2% "
                "apres 20 minutes"
            )


        elif (
            red4 >= 3
            and close4 < opening
        ):

            decision = (
                "SETUP INITIAL INVALIDE"
            )

            reason = (
                "Au moins 3 bougies "
                "rouges sur 4"
            )


        # ====================================================
        # RECHERCHE DE L'ACHAT CONFIRMÉ
        # UNIQUEMENT PENDANT LA PREMIÈRE HEURE
        # ====================================================

        else:

            first_hour = (
                day.iloc[:12].copy()
            )

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

                current_vwap = float(
                    row["VWAP"]
                )


                # ============================================
                # INVALIDATION DURANT LA PREMIÈRE HEURE
                # ============================================

                if (
                    current_low < or_low
                    or
                    current_close
                    <= opening * 0.98
                ):

                    decision = (
                        "SETUP INITIAL INVALIDE"
                    )

                    reason = (
                        "Cassure baissiere "
                        "Opening Range"
                    )

                    decision_time = (
                        row[
                            "Datetime_Paris"
                        ]
                    )

                    invalid_pos = pos

                    break


                # ============================================
                # ACHAT CONFIRMÉ
                # ============================================

                if (
                    current_close > or_high
                    and
                    current_close > current_vwap
                    and
                    current_volume >= vol4
                ):

                    decision = (
                        "ACHAT CONFIRME"
                    )

                    reason = (
                        "Cassure haussiere "
                        "+ VWAP + volume"
                    )

                    decision_time = (
                        row[
                            "Datetime_Paris"
                        ]
                    )

                    decision_price = (
                        current_close
                    )

                    decision_volume = (
                        current_volume
                    )

                    decision_vwap = (
                        current_vwap
                    )

                    break


            # ================================================
            # AUCUNE CONFIRMATION PENDANT UNE HEURE
            # ================================================

            if (
                decision == "EN ATTENTE"
                and len(day) >= 12
            ):

                decision = (
                    "SETUP INITIAL INVALIDE"
                )

                reason = (
                    "Aucune confirmation "
                    "pendant une heure"
                )

                invalid_pos = 11


        # ====================================================
        # ACHAT RÉACTIF
        # EXACTEMENT COMME LE MOTEUR DE PRODUCTION
        # ====================================================

        if (
            decision
            == "SETUP INITIAL INVALIDE"
        ):

            reactive_start = (
                invalid_pos + 1
            )

            for _, row in (
                day.iloc[
                    reactive_start:
                ].iterrows()
            ):

                current_close = float(
                    row["Close"]
                )

                current_volume = float(
                    row["Volume"]
                )

                current_vwap = float(
                    row["VWAP"]
                )

                if (
                    current_close > or_high
                    and
                    current_close > current_vwap
                    and
                    current_volume >= vol4 * 1.5
                ):

                    decision = (
                        "ACHAT REACTIVE"
                    )

                    reason = (
                        "Retournement confirme : "
                        "OR High + VWAP + volume"
                    )

                    decision_time = (
                        row[
                            "Datetime_Paris"
                        ]
                    )

                    decision_price = (
                        current_close
                    )

                    decision_volume = (
                        current_volume
                    )

                    decision_vwap = (
                        current_vwap
                    )

                    break


        # ====================================================
        # PERFORMANCE
        # ====================================================

        close_final = float(
            day["Close"].iloc[-1]
        )

        intraday_return_pct = None
        return_j1_pct = None
        return_j2_pct = None

        is_buy = (
            decision
            in [
                "ACHAT CONFIRME",
                "ACHAT REACTIVE"
            ]
        )


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


            if date in daily_date_positions:

                current_position = (
                    daily_date_positions[
                        date
                    ]
                )

                for horizon in [1, 2]:

                    uture_position = (
                        current_position
                        + horizon
                    )

                    if (
                        future_position
                        < len(daily_dates)
                    ):

                        future_date = (
                            daily_dates[
                                future_position
                            ]
                        )

                        future_close = (
                            daily_close_by_date[
                                future_date
                            ]
                        )

                        future_return = (
                            (
                                future_close
                                / decision_price
                            )
                            - 1
                        ) * 100

                        if horizon == 1:

                            return_j1_pct = (
                                future_return
                            )

                        elif horizon == 2:

                            return_j2_pct = (
                                future_return
                            )


        # ====================================================
        # IMPORTANT :
        # PERFORMANCE DES SETUPS REJETÉS
        #
        # On mesure aussi ce qu'aurait fait le titre depuis
        # l'ouverture, afin de voir si le moteur rejette
        # des titres qui montent ensuite.
        # ====================================================

        rejected_intraday_pct = None
        rejected_j1_pct = None
        rejected_j2_pct = None

        if not is_buy:

            rejected_intraday_pct = (
                (
                    close_final
                    / opening
                )
                - 1
            ) * 100

            if date in daily_date_positions:

                current_position = (
                    daily_date_positions[
                        date
                    ]
                )

                for horizon in [1, 2]:

                    future_position = (
                        current_position
                        + horizon
                    )

                    if (
                        future_position
                        < len(daily_dates)
                    ):

                        future_date = (
                            daily_dates[
                                future_position
                            ]
                        )

                        future_close = (
                            daily_close_by_date[
                                future_date
                            ]
                        )

                        rejected_return = (
                            (
                                future_close
                                / opening
                            )
                            - 1
                        ) * 100

                        if horizon == 1:

                            rejected_j1_pct = (
                                rejected_return
                            )

                        elif horizon == 2:

                            rejected_j2_pct = (
                                rejected_return
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

                "IntradayReturnPct":
                    intraday_return_pct,

                "ReturnJ1Pct":
                    return_j1_pct,

                "ReturnJ2Pct":
                    return_j2_pct,

                "RejectedIntradayPct":
                    rejected_intraday_pct,

                "RejectedJ1Pct":
                    rejected_j1_pct,

                "RejectedJ2Pct":
                    rejected_j2_pct,

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
    print(
        "Aucun résultat trouvé."
    )

    raise SystemExit(0)


result = pd.concat(
    all_results,
    ignore_index=True
)


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
# TAUX D'ACHAT
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
# PERFORMANCE DES ACHATS
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

    valid = (
        buy_result[column]
        .dropna()
    )

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
# ACHAT CONFIRME VS ACHAT REACTIVE
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
        result["Decision"]
        == decision_type
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

        valid = (
            subset[column]
            .dropna()
        )

        if valid.empty:
            continue

        print(
            f"{label} | "
            f"Moyenne "
            f"{valid.mean():+.3f}% | "
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

    valid = (
        rejected_result[column]
        .dropna()
    )

    if valid.empty:
        continue

    print()
    print(label)

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

if not rejected_result.empty:

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
        rejection_stats
        .round(3)
    )


# ============================================================
# PERFORMANCE PAR TICKER
# ============================================================

print()
print("=" * 70)
print("PERFORMANCE DES ACHATS PAR TICKER")
print("=" * 70)

if not buy_result.empty:

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
        ticker_stats
        .round(3)
    )


# ============================================================
# CONCLUSION TECHNIQUE
# ============================================================

print()
print("=" * 70)
print("FIN DU REPLAY EXACT")
print("=" * 70)
