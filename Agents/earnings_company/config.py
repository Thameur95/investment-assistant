from pathlib import Path

# ============================================================
# EARNINGS & COMPANY AGENT - CONFIGURATION
# ============================================================

AGENT_VERSION = "EARNINGS_COMPANY_V1"

# Dossier propre à l'agent
ROOT = Path(__file__).resolve().parent

# Racine du repository investment-assistant
REPOSITORY_ROOT = ROOT.parent.parent

# Dossiers propres à l'agent
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"

# Univers de surveillance existant
STOCKS_FILE = REPOSITORY_ROOT / "stocks.txt"

# ============================================================
# SOURCES CATALYST - LECTURE SEULE
# ============================================================

CATALYST_EVENTS_FILE = (
    REPOSITORY_ROOT
    / "historical_lab"
    / "reports"
    / "catalyst_events.csv"
)

CATALYST_SEMANTIC_FILE = (
    REPOSITORY_ROOT
    / "historical_lab"
    / "reports"
    / "catalyst_semantic.csv"
)

CATALYST_SEC_FILINGS_FILE = (
    REPOSITORY_ROOT
    / "historical_lab"
    / "reports"
    / "catalyst_sec_filings.csv"
)

# ============================================================
# SECURITE / ISOLATION V1
# ============================================================

OBSERVATION_ONLY = True
V1_EXECUTION_CHANGED = False
PORTFOLIO_V1_CHANGED = False

# ============================================================
# SORTIE EARNINGS & COMPANY
# ============================================================

EARNINGS_RESULTS_FILE = (
    RESULTS_DIR
    / "earnings_company_events.csv"
)
