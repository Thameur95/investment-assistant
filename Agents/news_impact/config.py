from pathlib import Path

# ============================================================
# NEWS IMPACT AGENT - CONFIGURATION
# ============================================================

AGENT_VERSION = "NEWS_IMPACT_V1"

# Dossier de l'agent
ROOT = Path(__file__).resolve().parent

# Racine du repository investment-assistant
REPOSITORY_ROOT = ROOT.parent.parent

# Dossiers propres à News Impact
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"

# Univers de surveillance existant
STOCKS_FILE = REPOSITORY_ROOT / "stocks.txt"

# ============================================================
# SOURCE INFORMATIONNELLE
# ============================================================

# Source consolidée par Catalyst.
# News Impact la lit uniquement.
# News Impact ne modifie jamais ce fichier.
CATALYST_EVENTS_FILE = (
    REPOSITORY_ROOT
    / "historical_lab"
    / "reports"
    / "catalyst_events.csv"
)


# Classification sémantique produite par Catalyst.
# News Impact lit uniquement ce fichier.
# News Impact ne le modifie jamais.
CATALYST_SEMANTIC_FILE = (
    REPOSITORY_ROOT
    / "historical_lab"
    / "reports"
    / "catalyst_semantic.csv"
)

# ============================================================
# SORTIE NEWS IMPACT
# ============================================================

EVENTS_FILE = (
    RESULTS_DIR
    / "news_impact_events.csv"
)

# ============================================================
# SECURITE / ISOLATION V1
# ============================================================

OBSERVATION_ONLY = True

V1_EXECUTION_CHANGED = False

PORTFOLIO_V1_CHANGED = False
