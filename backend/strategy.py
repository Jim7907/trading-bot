"""
Core strategy engine — probability model + signal generation.
Shared between the backtest and the live runner.
"""

import math
import logging
import numpy as np
import pandas as pd
import yfinance as yf
from config import (
    THRESHOLD, MIN_SAMPLES, ATR_MULT, RR_RATIO,
    EMA_PERIOD, USE_EMA, USE_TIME, TRADE_DIR,
)

log = logging.getLogger("strategy")


# ── Indicators ───────────────────────────────────────────────────────────────

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ── Probability Engine ───────────────────────────────────────────────────────

class ProbabilityEngine:
    def __init__(self):
        self.up_counts    = np.zeros(100, dtype=np.int64)
        self.total_counts = np.zeros(100, dtype=np.int64)
        self.warmed       = False
        self.obs_count    = 0

    @staticmethod
    def _feat(o, h, l, c):
        tr = h - l
        if tr == 0:
            return 0, 0
        br = (c - o) / tr
        uw = (h - max(o, c)) / tr
        return max(0, min(9, int(math.floor((br + 1) / 2 * 9)))), \
               max(0, min(9, int(math.floor(uw * 9))))

    def update(self, o, h, l, c, next_mid, prev_mid):
        b, w = self._feat(o, h, l, c)
        flat = b * 10 + w
        self.up_counts[flat]    += 1 if next_mid > prev_mid else 0
        self.total_counts[flat] += 1
        self.obs_count          += 1

    def prob_up(self, o, h, l, c) -> float:
        b, w  = self._feat(o, h, l, c)
        flat  = b * 10 + w
        tot   = int(self.total_counts[flat])
        return float(self.up_counts[flat] / tot) if tot >= MIN_SAMPLES else 0.5

    def active_bins(self) -> int:
        return int((self.total_counts > 0).sum())

    def warm_up(self, ticker: str):
        log.info(f"Warming probability engine for {ticker} (5yr daily)…")
        raw = yf.download(ticker, period="5y", interval="1d",
                          auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        bars = raw[["Open", "High", "Low", "Close"]].dropna()
        prev_mid = None
        for i in range(len(bars)):
            row = bars.iloc[i]
            mid = (float(row["Open"]) + float(row["Close"])) / 2
            if prev_mid is not None:
                pr = bars.iloc[i - 1]
                self.update(float(pr["Open"]), float(pr["High"]),
                            float(pr["Low"]),  float(pr["Close"]),
                            next_mid=mid, prev_mid=prev_mid)
            prev_mid = mid
        self.warmed = True
        log.info(f"  {ticker}: {self.obs_count:,} obs, {self.active_bins()} active bins")


# ── Bar Fetcher ───────────────────────────────────────────────────────────────

def fetch_bars(ticker: str, n: int = 250) -> pd.DataFrame | None:
    try:
        raw = yf.download(ticker, period="60d", interval="15m",
                          auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna().tail(n)
        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert("America/New_York")
        df["atr14"]   = calc_atr(df, 14)
        df["ema200"]  = df["Close"].ewm(span=EMA_PERIOD, adjust=False).mean()
        df["vol_sma"] = df["Volume"].rolling(20).mean()
        df["mid2"]    = (df["Open"] + df["Close"]) / 2
        return df
    except Exception as e:
        log.error(f"fetch_bars({ticker}): {e}")
        return None


# ── Session Helpers ───────────────────────────────────────────────────────────

def _hm(dt) -> int:
    return dt.hour * 100 + dt.minute

def is_rth(dt) -> bool:
    return 945 <= _hm(dt) <= 1545

def is_eod(dt) -> bool:
    return 1550 <= _hm(dt) <= 1600


# ── Signal Generator ─────────────────────────────────────────────────────────

def generate_signal(df: pd.DataFrame, engine: ProbabilityEngine) -> dict | None:
    if len(df) < EMA_PERIOD + 10:
        return None

    bar = df.iloc[-2]
    now = df.index[-2]

    # Feed last completed bar into engine
    if len(df) >= 3:
        p  = df.iloc[-3]
        engine.update(
            float(p["Open"]), float(p["High"]), float(p["Low"]), float(p["Close"]),
            next_mid=float(df.iloc[-2]["mid2"]),
            prev_mid=float(df.iloc[-3]["mid2"]),
        )

    prob  = engine.prob_up(float(bar["Open"]), float(bar["High"]),
                           float(bar["Low"]),  float(bar["Close"]))

    if USE_TIME and not is_rth(now):
        return None

    atr_v  = float(bar["atr14"]) if not np.isnan(bar["atr14"]) else 0
    ema200 = float(bar["ema200"])
    close  = float(bar["Close"])
    mid2   = float(bar["mid2"])
    vsma   = float(bar["vol_sma"]) if not np.isnan(bar["vol_sma"]) else 0
    liquid = float(bar["Volume"]) > vsma if vsma > 0 else True

    trend_up   = (not USE_EMA) or close > ema200
    trend_down = (not USE_EMA) or close < ema200
    long_ok    = TRADE_DIR in ("Both", "Long Only")
    short_ok   = TRADE_DIR in ("Both", "Short Only")

    if prob > THRESHOLD and trend_up and liquid and long_ok:
        sl   = float(bar["Low"]) - atr_v * ATR_MULT
        risk = mid2 - sl
        if risk <= 0:
            return None
        return {"side": "long", "entry": mid2, "sl": sl,
                "tp": mid2 + risk * RR_RATIO, "risk_per_share": risk, "prob": prob}

    if prob < (1 - THRESHOLD) and trend_down and liquid and short_ok:
        sl   = float(bar["High"]) + atr_v * ATR_MULT
        risk = sl - mid2
        if risk <= 0:
            return None
        return {"side": "short", "entry": mid2, "sl": sl,
                "tp": mid2 - risk * RR_RATIO, "risk_per_share": risk, "prob": prob}

    return None
