"""
Bot runner — background thread that ticks every POLL_SECONDS.
Exposes state for the API layer to read.
"""

import time
import threading
import logging
from datetime import datetime, timezone, timedelta
from collections import deque

from config import (
    T212_API_KEY, BASE_URL, SYMBOLS, RISK_PCT, POLL_SECONDS, USE_TIME,
)
from strategy import ProbabilityEngine, fetch_bars, generate_signal, is_eod
from t212 import T212Client

log = logging.getLogger("runner")

ET = timezone(timedelta(hours=-4))   # Eastern (EDT)


class BotRunner:
    def __init__(self):
        self.running    = False
        self._thread    = None
        self._lock      = threading.Lock()

        self.client     = None
        self.engines: dict[str, ProbabilityEngine] = {}
        self.positions: dict[str, dict] = {}    # symbol -> bracket state
        self.trades:    list[dict]       = []    # completed trades
        self.logs:      deque            = deque(maxlen=200)
        self.equity_history: list[dict]  = []
        self.account_info:   dict        = {}
        self.error:          str | None  = None

    # ── Public control ────────────────────────────────────────────────────────

    def start(self):
        with self._lock:
            if self.running:
                return
            if not T212_API_KEY:
                raise ValueError("T212_API_KEY not configured")
            self.running = True
            self.error   = None
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            self._log("Bot started")

    def stop(self):
        with self._lock:
            self.running = False
            self._log("Bot stopped")

    def get_state(self) -> dict:
        return {
            "running":        self.running,
            "symbols":        SYMBOLS,
            "positions":      self.positions,
            "trades":         self.trades[-50:],
            "equity_history": self.equity_history[-200:],
            "account":        self.account_info,
            "logs":           list(self.logs)[-50:],
            "error":          self.error,
        }

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self):
        try:
            self.client = T212Client(T212_API_KEY, BASE_URL)
            info = self.client.get_account_info()
            self.account_info = info
            self._log(f"Connected: {info.get('id')} ({info.get('currencyCode')})")

            # Warm up engines
            for sym in SYMBOLS:
                eng = ProbabilityEngine()
                eng.warm_up(sym)
                self.engines[sym] = eng
                self._log(f"{sym} engine ready: {eng.obs_count:,} obs")

            while self.running:
                self._tick()
                for _ in range(POLL_SECONDS):
                    if not self.running:
                        break
                    time.sleep(1)

        except Exception as e:
            self.error = str(e)
            self.running = False
            log.error(f"Bot loop error: {e}", exc_info=True)
            self._log(f"ERROR: {e}")

    def _tick(self):
        now = datetime.now(tz=ET)
        self._log(f"── Tick {now.strftime('%H:%M ET')} ──────────────────")

        try:
            cash = self.client.get_cash()
            equity = float(cash.get("total", 0))
            self.account_info["equity"] = equity
            self.equity_history.append({
                "ts":     now.isoformat(),
                "equity": round(equity, 2),
            })
        except Exception as e:
            self._log(f"Cash fetch error: {e}")
            equity = 10_000.0

        for sym in SYMBOLS:
            try:
                self._process(sym, equity, now)
            except Exception as e:
                self._log(f"{sym} error: {e}")
                log.error(f"{sym} tick error", exc_info=True)

    def _process(self, sym: str, equity: float, now: datetime):
        # Sync open bracket
        self._sync_bracket(sym)

        # EOD close
        if USE_TIME and is_eod(now) and sym in self.positions:
            self._force_close(sym)
            return

        if sym in self.positions:
            st = self.positions[sym]
            self._log(f"{sym}: in trade ({st['side']}) entry={st['entry']:.4f}")
            return

        df = fetch_bars(sym)
        if df is None or len(df) < 210:
            self._log(f"{sym}: insufficient bars")
            return

        sig = generate_signal(df, self.engines[sym])

        if sig is None:
            prob = self.engines[sym].prob_up(
                float(df.iloc[-2]["Open"]), float(df.iloc[-2]["High"]),
                float(df.iloc[-2]["Low"]),  float(df.iloc[-2]["Close"]),
            )
            self._log(f"{sym}: no signal (prob={prob:.1%})")
            return

        risk_amt = equity * RISK_PCT
        shares   = max(1.0, round(risk_amt / sig["risk_per_share"], 4))
        self._log(
            f"{sym} SIGNAL {sig['side'].upper()} entry={sig['entry']:.4f} "
            f"sl={sig['sl']:.4f} tp={sig['tp']:.4f} qty={shares} prob={sig['prob']:.1%}"
        )
        self._open_bracket(sym, sig, shares, now)

    def _open_bracket(self, sym: str, sig: dict, shares: float, now: datetime):
        qty = shares if sig["side"] == "long" else -shares
        entry_ord = self.client.place_limit(sym, qty, sig["entry"], "DAY")
        self.positions[sym] = {
            "side":           sig["side"],
            "entry":          sig["entry"],
            "sl":             sig["sl"],
            "tp":             sig["tp"],
            "shares":         shares,
            "prob":           sig["prob"],
            "entry_order_id": entry_ord.get("id"),
            "sl_order_id":    None,
            "tp_order_id":    None,
            "status":         "ENTRY_PENDING",
            "opened_at":      now.isoformat(),
            "symbol":         sym,
        }

    def _sync_bracket(self, sym: str):
        if sym not in self.positions:
            return
        st = self.positions[sym]

        if st["status"] == "ENTRY_PENDING":
            try:
                o = self.client.get_order(st["entry_order_id"])
            except Exception:
                return
            if o.get("status") == "FILLED":
                self._log(f"{sym} entry FILLED — placing SL + TP")
                qty = -st["shares"] if st["side"] == "long" else st["shares"]
                sl_ord = self.client.place_stop(sym, qty, st["sl"])
                tp_ord = self.client.place_limit(sym, qty, st["tp"], "GOOD_TILL_CANCEL")
                st["sl_order_id"] = sl_ord.get("id")
                st["tp_order_id"] = tp_ord.get("id")
                st["status"] = "OPEN"
            elif o.get("status") in ("CANCELLED", "REJECTED"):
                self._log(f"{sym} entry {o.get('status')} — cleared")
                del self.positions[sym]

        elif st["status"] == "OPEN":
            tp_hit = self._is_filled(st["tp_order_id"])
            sl_hit = self._is_filled(st["sl_order_id"])
            if tp_hit or sl_hit:
                result = "TP" if tp_hit else "SL"
                self._log(f"{sym} {result} hit — closing bracket")
                cancel_id = st["sl_order_id"] if tp_hit else st["tp_order_id"]
                if cancel_id:
                    try:
                        self.client.cancel_order(cancel_id)
                    except Exception:
                        pass
                exit_price = st["tp"] if tp_hit else st["sl"]
                d = 1 if st["side"] == "long" else -1
                pnl = (exit_price - st["entry"]) * st["shares"] * d
                trade = {**st, "exit": exit_price, "pnl": round(pnl, 2),
                         "result": result, "closed_at": datetime.now(ET).isoformat()}
                self.trades.append(trade)
                self._log(
                    f"{sym} CLOSED {result}  pnl={pnl:+.2f}  "
                    f"entry={st['entry']:.4f} exit={exit_price:.4f}"
                )
                del self.positions[sym]

    def _force_close(self, sym: str):
        st = self.positions.get(sym)
        if not st:
            return
        for key in ("entry_order_id", "sl_order_id", "tp_order_id"):
            if st.get(key):
                try:
                    self.client.cancel_order(st[key])
                except Exception:
                    pass
        pos = self.client.get_position(sym)
        if pos:
            qty = float(pos.get("quantity", 0))
            if qty != 0:
                self.client.place_market(sym, -qty)
        exit_price = float(pos["currentPrice"]) if pos else st["entry"]
        d = 1 if st["side"] == "long" else -1
        pnl = (exit_price - st["entry"]) * st["shares"] * d
        self.trades.append({**st, "exit": exit_price, "pnl": round(pnl, 2),
                            "result": "EOD", "closed_at": datetime.now(ET).isoformat()})
        del self.positions[sym]
        self._log(f"{sym} EOD force-closed pnl={pnl:+.2f}")

    def _is_filled(self, oid) -> bool:
        if not oid:
            return False
        try:
            return self.client.get_order(oid).get("status") == "FILLED"
        except Exception:
            return False

    def _log(self, msg: str):
        ts  = datetime.now(ET).strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        log.info(msg)


# Singleton
bot = BotRunner()
