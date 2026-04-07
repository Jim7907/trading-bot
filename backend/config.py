import os
from dotenv import load_dotenv
load_dotenv()

T212_API_KEY   = os.getenv("T212_API_KEY", "")
T212_ENV       = os.getenv("T212_ENV", "demo").lower()
SYMBOLS        = [s.strip().upper() for s in os.getenv("SYMBOLS", "AMC,AAPL").split(",")]
RISK_PCT       = float(os.getenv("RISK_PCT",    "0.01"))
ATR_MULT       = float(os.getenv("ATR_MULT",    "1.5"))
RR_RATIO       = float(os.getenv("RR_RATIO",    "2.0"))
THRESHOLD      = float(os.getenv("THRESHOLD",   "0.60"))
MIN_SAMPLES    = int(os.getenv("MIN_SAMPLES",   "20"))
EMA_PERIOD     = int(os.getenv("EMA_PERIOD",    "200"))
USE_EMA        = os.getenv("USE_EMA_FILTER",  "true").lower() == "true"
USE_TIME       = os.getenv("USE_TIME_FILTER", "true").lower() == "true"
TRADE_DIR      = os.getenv("TRADE_DIR", "Both")
POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "900"))   # 15 min

BASE_URL = (
    "https://live.trading212.com/api/v0"
    if T212_ENV == "live"
    else "https://demo.trading212.com/api/v0"
)
