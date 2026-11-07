#!/usr/bin/env bash

set -uo pipefail

cd /opt/investment-assistant

source .venv/bin/activate

set -a
source .env
set +a

echo "============================================================"
echo "VPS INTRADAY CYCLE - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "============================================================"

critical_failed=0
shadow_errors=0


run_critical() {
    local label="$1"
    shift

    echo
    echo "[CRITICAL] $label"

    if "$@"; then
        echo "[CRITICAL OK] $label"
        return 0
    fi

    rc=$?
    echo "[CRITICAL ERROR] $label | exit=$rc"
    critical_failed=1
    return "$rc"
}


run_shadow() {
    local label="$1"
    shift

    echo
    echo "[SHADOW] $label"

    if "$@"; then
        echo "[SHADOW OK] $label"
        return 0
    fi

    rc=$?
    echo "[SHADOW ERROR] $label | exit=$rc"
    shadow_errors=$((shadow_errors + 1))

    # Une erreur Shadow ne doit jamais interrompre Champion V1.
    return 0
}


# ============================================================
# BLOC CRITIQUE CHAMPION V1
# ============================================================

run_critical "Intraday Monitor V1" \
    python intraday_monitor.py || exit $?

run_critical "Virtual Portfolio V1" \
    python virtual_portfolio.py || exit $?

run_critical "Immediate Alerts V1" \
    python intraday_alert.py || exit $?


# ============================================================
# BLOC OBSERVATION / SHADOW
# Aucune erreur de ce bloc ne peut modifier ou interrompre V1.
# ============================================================

run_shadow "V2 Shadow RVOL" \
    python v2_shadow_live.py

run_shadow "Structure Shadow Agent" \
    python structure_shadow_agent.py

run_shadow "Structure Shadow Portfolio" \
    python structure_shadow_portfolio.py

run_shadow "Trade Intelligence" \
    python trade_intelligence_log.py


echo
echo "============================================================"
echo "VPS INTRADAY CYCLE FINISHED - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "CriticalFailed : $critical_failed"
echo "ShadowErrors   : $shadow_errors"
echo "V1ExecutionChanged : False"
echo "============================================================"

exit "$critical_failed"
