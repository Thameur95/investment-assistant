from pathlib import Path

AGENT_VERSION = "NEWS_IMPACT_V1"

ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parent.parent

DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"

STOCKS_FILE = REPOSITORY_ROOT / "stocks.txt"

EVENTS_FILE = RESULTS_DIR / "news_impact_events.csv"

OBSERVATION_ONLY = True
V1_EXECUTION_CHANGED = False
PORTFOLIO_V1_CHANGED = False
