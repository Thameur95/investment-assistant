import pandas as pd
from pathlib import Path


# ============================================================
# PARAMETRES
# ============================================================

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

# 12 bougies = 60 min
# 18 bougies = 90 min
# 24 bougies = 120 min
# None = toute la séance
WINDOWS = {
    "60 min": 12,
    "90 min": 18,
    "120 min": 24,
    "Toute seance": None,
}


# ============================================================
# FONCTIONS
# ============================================================

def calculate_future_return(
    daily_prices,
    session_date,
    entry_price,
    horizon
):
    """
    Performance entre le prix d'entrée et la clôture
    de la horizon-ième séance boursière suivant J.

    horizon=1 -> clôture J+1
    horizon=2 -> clôture J+2
    """

    if entry_price is None:
        return None

    if pd.isna(entry_price):
        return None

    entry_price = float(entry_price)

    if entry_price <= 0:
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
        (
            future_close
            / entry_price
        )
        - 1
    ) * 100


def prepare_daily_prices(daily):
    """
    Construit une série Date -> Close avec une seule ligne
    par séance de cotation.
    """

    prices = (
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

    prices.index = pd.to_datetime(
        prices.index
    ).normalize()

    return prices.sort_index()


def calculate_stats(values):
    """
    Retourne nombre de cas, moyenne, médiane et win rate.
    """

    valid = pd.Series(
        values,
        dtype="float64"
    ).dropna()

    if valid.empty:
        return {
            "Cases": 0,
            "Mean": None,
            "Median": None,
            "WinRate": None,
        }

    return {
        "Cases": len(valid),
        "Mean": valid.mean(),
        "Median": valid.median(),
        "WinRate": (
            valid > 0
        ).mean() * 100,
    }


def replay_session(
    ticker,
    date,
    day,
    signal_price,
    daily_prices,
    max_bars
):
    """
    Rejoue une séance avec les règles du moteur de production.

    max_bars :
        12   -> 60 min
        18   -> 90 min
        24   -> 120 min
        None -> toute la séance
    """

    day = (
        day
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
        .copy()
    )

    if len(day) < 4:
        return None

    signal_price = float(signal_price)

    if signal_price <= 0:
        return None


    # ========================================================
    # OUVERTURE + GAP
    # ========================================================

    opening = float(
        day["Open"].iloc[0]
    )

    if opening <= 0:
        return None

    gap_pct = (
        (
            opening
            / signal_price
        )
        - 1
    ) * 100


    # ========================================================
    # OPENING RANGE 20 MIN
    # ========================================================

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


    # ========================================================
    # VOLUME INITIAL
    # ========================================================

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


    # ========================================================
    # VWAP
    # ========================================================

    typical_price = (
        day["High"]
        + day["Low"]
        + day["Close"]
    ) / 3

    cumulative_volume = (
        day["Volume"].cumsum()
    )

    cumulative_value = (
        typical_price
        * day["Volume"]
    ).cumsum()

    safe_volume = cumulative_volume.replace(
        0,
        float("nan")
    )

    day["VWAP"] = (
        cumulative_value
        / safe_volume
    )


    # ========================================================
    # ETAT INITIAL
    # ========================================================

    decision = "EN ATTENTE"
    reason = "Prix dans Opening Range"

    decision_time = None
    decision_price = None
    decision_volume = None
    decision_vwap = None

    invalid_pos = 3


    # ========================================================
    # PROTECTIONS INITIALES
    # ========================================================

    if gap_pct <= -3:

        decision = "SETUP INITIAL INVALIDE"
        reason = "Gap baissier >= 3%"

    elif close4 <= opening * 0.98:

        decision = "SETUP INITIAL INVALIDE"

        reason = (
            "Baisse >= 2% apres 20 minutes"
        )

    elif (
        red4 >= 3
        and close4 < opening
    ):

        decision = "SETUP INITIAL INVALIDE"

        reason = (
            "Au moins 3 bougies rouges sur 4"
        )


    # ========================================================
    # ACHAT CONFIRME
    # ========================================================

    else:

        if max_bars is None:

            confirmation_window = day.copy()

        else:

            confirmation_window = (
                day.iloc[:max_bars].copy()
            )


        for pos, row in (
            confirmation_window
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


            # =================================================
            # INVALIDATION OR LOW / -2 %
            # =================================================

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

                decision_time = row[
                    "Datetime_Paris"
                ]

                invalid_pos = int(pos)

                break


            # =================================================
            # CONFIRMATION ACHAT
            # =================================================

            if (
                current_close > or_high
                and
                current_close > current_vwap
                and
                current_volume >= vol4
            ):

                decision = "ACHAT CONFIRME"

                reason = (
                    "Cassure haussiere "
                    "+ VWAP + volume"
                )

                decision_time = row[
                    "Datetime_Paris"
                ]

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
        # FIN DE LA FENETRE SANS CONFIRMATION
        # ====================================================

        if decision == "EN ATTENTE":

            enough_bars = (
                max_bars is None
                or len(day) >= max_bars
            )

            if enough_bars:

                decision = (
                    "SETUP INITIAL INVALIDE"
                )

                if max_bars is None:

                    reason = (
                        "Aucune confirmation "
                        "pendant la seance"
                    )

                    invalid_pos = (
                        len(day) - 1
                    )

                else:

                    minutes = max_bars * 5

                    reason = (
                        "Aucune confirmation "
                        f"pendant {minutes} minutes"
                    )

                    invalid_pos = (
                        max_bars - 1
                    )


    # ========================================================
    # ACHAT REACTIF
    # ========================================================

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

            current_vwap = row["VWAP"]

            if pd.isna(current_vwap):
                continue

            current_vwap = float(
                current_vwap
            )

            if (
                current_close > or_high
                and
                current_close > current_vwap
                and
                current_volume
                >= vol4 * 1.5
            ):

                decision = "ACHAT REACTIVE"

                reason = (
                    "Retournement confirme : "
                    "OR High + VWAP + volume"
                )

                decision_time = row[
                    "Datetime_Paris"
                ]

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


    # ========================================================
    # PERFORMANCES
    # ========================================================

    close_final = float(
        day["Close"].iloc[-1]
    )

    is_buy = decision in [
        "ACHAT CONFIRME",
        "ACHAT REACTIVE"
    ]

    intraday_return_pct = None
    return_j1_pct = None
    return_j2_pct = None

    rejected_intraday_pct = None
    rejected_j1_pct = None
    rejected_j2_pct = None


    # ========================================================
    # PERFORMANCE DES ACHATS
    # ========================================================

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

        return_j1_pct = (
            calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=decision_price,
                horizon=1
            )
        )

        return_j2_pct = (
            calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=decision_price,
                horizon=2
            )
        )


    # ========================================================
    # PERFORMANCE DES REJETS
    # ========================================================

    else:

        rejected_intraday_pct = (
            (
                close_final
                / opening
            )
            - 1
        ) * 100

        rejected_j1_pct = (
            calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=opening,
                horizon=1
            )
        )

        rejected_j2_pct = (
            calculate_future_return(
                daily_prices=daily_prices,
                session_date=date,
                entry_price=opening,
                horizon=2
            )
        )


    return {
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

        "Bars": len(day),
    }


# ============================================================
# CHARGEMENT DAILY
# ============================================================

print("=" * 70)
print("HISTORICAL LAB")
print("TEST DES FENETRES DE CONFIRMATION")
print("=" * 70)

daily_all = pd.read_csv(
    DAILY_FILE
)

daily_all["Date"] = pd.to_datetime(
    daily_all["Date"],
    errors="coerce"
)

daily_all["Close"] = pd.to_numeric(
    daily_all["Close"],
    errors="coerce"
)


# ============================================================
# RESULTATS PAR VARIANTE
# ============================================================

results_by_window = {}


# ============================================================
# TEST DES 4 FENETRES
# ============================================================

for window_name, max_bars in WINDOWS.items():

    print()
    print("=" * 70)
    print(
        f"TEST FENETRE : {window_name}"
    )
    print("=" * 70)

    all_results = []


    # ========================================================
    # BOUCLE TICKERS
    # ========================================================

    for intraday_file in sorted(
        INTRADAY_DIR.glob("*_5m.csv")
    ):

        ticker = (
            intraday_file.stem
            .replace("_5m", "")
        )

        df = pd.read_csv(
            intraday_file
        )

        required_columns = {
            "Datetime_Paris",
            "Date_Paris",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
        }

        missing_columns = (
            required_columns
            - set(df.columns)
        )

        if missing_columns:

            print(
                ticker,
                "| colonnes manquantes :",
                sorted(missing_columns)
            )

            continue


        # ====================================================
        # DAILY DU TICKER
        # ====================================================

        daily = daily_all[
            daily_all["Ticker"] == ticker
        ].copy()

        if daily.empty:
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
            continue


        # ====================================================
        # SESSION J ASSOCIEE AU SIGNAL J-1
        # ====================================================

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
            .set_index(
                "SessionDate"
            )["Close"]
            .to_dict()
        )

        daily_prices = (
            prepare_daily_prices(
                daily
            )
        )


        # ====================================================
        # PREPARATION INTRADAY
        # ====================================================

        df["Datetime_Paris"] = (
            pd.to_datetime(
                df["Datetime_Paris"],
                errors="coerce"
            )
        )

        df["Date_Paris"] = (
            df["Date_Paris"]
            .astype(str)
            .str[:10]
        )

        for column in [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]:

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
                "Volume",
            ]
        )

        ticker_results = []


        # ====================================================
        # REPLAY DES SEANCES
        # ====================================================

        for date, day in df.groupby(
            "Date_Paris"
        ):

            date = str(date)[:10]

            if date not in candidate_sessions:
                continue

            signal_price = (
                signal_price_by_session
                .get(date)
            )

            if signal_price is None:
                continue

            row_result = replay_session(
                ticker=ticker,
                date=date,
                day=day,
                signal_price=signal_price,
                daily_prices=daily_prices,
                max_bars=max_bars
            )

            if row_result is not None:
                ticker_results.append(
                    row_result
                )


        if ticker_results:

            ticker_df = pd.DataFrame(
                ticker_results
            )

            all_results.append(
                ticker_df
            )

            buys = ticker_df[
                "Decision"
            ].isin(
                [
                    "ACHAT CONFIRME",
                    "ACHAT REACTIVE"
                ]
            ).sum()

            print(
                f"{ticker:8s} | "
                f"seances {len(ticker_df):3d} | "
                f"achats {buys:3d}"
            )


    # ========================================================
    # CONSOLIDATION DE LA VARIANTE
    # ========================================================

    if not all_results:

        print(
            "Aucun résultat pour cette fenêtre."
        )

        continue

    result = pd.concat(
        all_results,
        ignore_index=True
    )

    results_by_window[
        window_name
    ] = result


# ============================================================
# RAPPORT DETAILLE PAR FENETRE
# ============================================================

summary_rows = []

for window_name, result in (
    results_by_window.items()
):

    print()
    print("=" * 70)
    print(
        f"RESULTATS : {window_name}"
    )
    print("=" * 70)

    buy_mask = result[
        "Decision"
    ].isin(
        [
            "ACHAT CONFIRME",
            "ACHAT REACTIVE"
        ]
    )

    buys = result[
        buy_mask
    ].copy()

    rejects = result[
        ~buy_mask
    ].copy()


    confirmed_count = int(
        (
            result["Decision"]
            == "ACHAT CONFIRME"
        ).sum()
    )

    reactive_count = int(
        (
            result["Decision"]
            == "ACHAT REACTIVE"
        ).sum()
    )


    print(
        "Séances analysées :",
        len(result)
    )

    print(
        "ACHAT CONFIRME :",
        confirmed_count
    )

    print(
        "ACHAT REACTIVE :",
        reactive_count
    )

    print(
        "Total achats :",
        len(buys)
    )

    print(
        "Setups rejetés :",
        len(rejects)
    )


    if len(result) > 0:

        trigger_rate = (
            len(buys)
            / len(result)
            * 100
        )

    else:

        trigger_rate = 0.0


    print(
        "Taux de déclenchement :",
        round(
            trigger_rate,
            1
        ),
        "%"
    )


    # ========================================================
    # STATS DES ACHATS
    # ========================================================

    j_stats = calculate_stats(
        buys["IntradayReturnPct"]
    )

    j1_stats = calculate_stats(
        buys["ReturnJ1Pct"]
    )

    j2_stats = calculate_stats(
        buys["ReturnJ2Pct"]
    )


    for label, stats in [
        ("J", j_stats),
        ("J+1", j1_stats),
        ("J+2", j2_stats),
    ]:

        print()
        print(label)

        print(
            "Nombre de cas :",
            stats["Cases"]
        )

        if stats["Mean"] is not None:

            print(
                "Performance moyenne :",
                round(
                    stats["Mean"],
                    3
                ),
                "%"
            )

            print(
                "Performance médiane :",
                round(
                    stats["Median"],
                    3
                ),
                "%"
            )

            print(
                "Taux positif :",
                round(
                    stats["WinRate"],
                    1
                ),
                "%"
            )


    # ========================================================
    # CONTROLE J+1 / J+2
    # ========================================================

    comparable = buys[
        buys["ReturnJ1Pct"].notna()
        & buys["ReturnJ2Pct"].notna()
    ].copy()

    identical_count = 0

    if not comparable.empty:

        identical_count = int(
            (
                (
                    comparable["ReturnJ1Pct"]
                    - comparable["ReturnJ2Pct"]
                ).abs()
                < 0.000001
            ).sum()
        )


    print()
    print(
        "Trades comparables J+1/J+2 :",
        len(comparable)
    )

    print(
        "J+1 exactement égal à J+2 :",
        identical_count
    )


    # ========================================================
    # PERFORMANCE DES REJETS
    # ========================================================

    rejected_j2_stats = (
        calculate_stats(
            rejects[
                "RejectedJ2Pct"
            ]
        )
    )

    print()
    print(
        "Performance moyenne J+2 "
        "des rejets :",
        (
            round(
                rejected_j2_stats[
                    "Mean"
                ],
                3
            )
            if rejected_j2_stats[
                "Mean"
            ] is not None
            else "N/A"
        ),
        "%"
    )


    # ========================================================
    # LIGNE DU TABLEAU COMPARATIF
    # ========================================================

    summary_rows.append(
        {
            "Fenetre": window_name,

            "Seances":
                len(result),

            "Achats":
                len(buys),

            "Confirmes":
                confirmed_count,

            "Reactifs":
                reactive_count,

            "TauxAchatPct":
                trigger_rate,

            "PerfJ":
                j_stats["Mean"],

            "WinJ":
                j_stats["WinRate"],

            "PerfJ1":
                j1_stats["Mean"],

            "WinJ1":
                j1_stats["WinRate"],

            "PerfJ2":
                j2_stats["Mean"],

            "WinJ2":
                j2_stats["WinRate"],

            "MedianJ2":
                j2_stats["Median"],

            "Rejets":
                len(rejects),

            "PerfRejetsJ2":
                rejected_j2_stats["Mean"],
        }
    )


# ============================================================
# COMPARAISON FINALE
# ============================================================

print()
print("=" * 90)
print("COMPARAISON DES FENETRES DE CONFIRMATION")
print("=" * 90)

if not summary_rows:

    print(
        "Aucune variante n'a produit "
        "de résultat."
    )

    raise SystemExit(0)

summary = pd.DataFrame(
    summary_rows
)

numeric_columns = [
    "TauxAchatPct",
    "PerfJ",
    "WinJ",
    "PerfJ1",
    "WinJ1",
    "PerfJ2",
    "WinJ2",
    "MedianJ2",
    "PerfRejetsJ2",
]

for column in numeric_columns:

    if column in summary.columns:

        summary[column] = (
            summary[column]
            .round(3)
        )

pd.set_option(
    "display.max_columns",
    None
)

pd.set_option(
    "display.width",
    200
)

print()

print(
    summary[
        [
            "Fenetre",
            "Seances",
            "Achats",
            "Confirmes",
            "Reactifs",
            "TauxAchatPct",
            "PerfJ",
            "WinJ",
            "PerfJ1",
            "WinJ1",
            "PerfJ2",
            "WinJ2",
            "MedianJ2",
            "Rejets",
            "PerfRejetsJ2",
        ]
    ].to_string(
        index=False
    )
)


# ============================================================
# PERFORMANCE PAR TYPE D'ACHAT POUR CHAQUE FENETRE
# ============================================================

print()
print("=" * 90)
print("DETAIL ACHAT CONFIRME VS ACHAT REACTIVE")
print("=" * 90)

for window_name, result in (
    results_by_window.items()
):

    print()
    print(
        f"--- {window_name} ---"
    )

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
        print(
            decision_type,
            "| Trades :",
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
            ),
        ]:

            stats = calculate_stats(
                subset[column]
            )

            if stats["Cases"] == 0:
                continue

            print(
                f"{label} | "
                f"Moyenne "
                f"{stats['Mean']:+.3f}% | "
                f"Mediane "
                f"{stats['Median']:+.3f}% | "
                f"Win rate "
                f"{stats['WinRate']:.1f}%"
            )


# ============================================================
# FIN
# ============================================================

print()
print("=" * 90)
print("FIN DU TEST DES FENETRES")
print("=" * 90)
