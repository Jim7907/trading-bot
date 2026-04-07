"""
FastAPI backend — serves the REST API and the frontend.
"""

import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from runner import bot

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("api")

app = FastAPI(title="OHLC Probability Trading Bot")

FRONTEND = Path(__file__).parent.parent / "frontend"


# ── REST API ─────────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    state = bot.get_state()
    eq_hist = state["equity_history"]
    start_eq = eq_hist[0]["equity"] if eq_hist else config.INITIAL_EQUITY if hasattr(config, "INITIAL_EQUITY") else 10_000
    cur_eq   = eq_hist[-1]["equity"] if eq_hist else start_eq
    closed   = state["trades"]
    wins     = sum(1 for t in closed if t["pnl"] > 0)
    total_tr = len(closed)
    net_pnl  = sum(t["pnl"] for t in closed)
    return {
        "running":       state["running"],
        "env":           config.T212_ENV.upper(),
        "symbols":       config.SYMBOLS,
        "equity":        cur_eq,
        "net_pnl":       round(net_pnl, 2),
        "total_trades":  total_tr,
        "win_rate":      round(wins / total_tr * 100, 1) if total_tr else 0,
        "open_positions": len(state["positions"]),
        "error":         state["error"],
        "config": {
            "risk_pct":    config.RISK_PCT,
            "atr_mult":    config.ATR_MULT,
            "rr_ratio":    config.RR_RATIO,
            "threshold":   config.THRESHOLD,
            "trade_dir":   config.TRADE_DIR,
            "use_ema":     config.USE_EMA,
            "use_time":    config.USE_TIME,
            "poll_secs":   config.POLL_SECONDS,
        },
    }


@app.get("/api/positions")
def get_positions():
    return bot.get_state()["positions"]


@app.get("/api/trades")
def get_trades(limit: int = 100):
    return bot.get_state()["trades"][-limit:]


@app.get("/api/equity")
def get_equity():
    return bot.get_state()["equity_history"]


@app.get("/api/logs")
def get_logs(limit: int = 100):
    return list(bot.logs)[-limit:]


class BotAction(BaseModel):
    action: str  # "start" | "stop"


@app.post("/api/bot")
def control_bot(body: BotAction):
    if body.action == "start":
        try:
            bot.start()
            return {"ok": True, "status": "started"}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif body.action == "stop":
        bot.stop()
        return {"ok": True, "status": "stopped"}
    raise HTTPException(status_code=400, detail="Unknown action")


class ConfigUpdate(BaseModel):
    symbols:    list[str] | None = None
    risk_pct:   float | None     = None
    atr_mult:   float | None     = None
    rr_ratio:   float | None     = None
    threshold:  float | None     = None
    trade_dir:  str | None       = None
    use_ema:    bool | None      = None
    use_time:   bool | None      = None


@app.post("/api/config")
def update_config(body: ConfigUpdate):
    if bot.running:
        raise HTTPException(status_code=400, detail="Stop the bot before changing config")
    if body.symbols   is not None: config.SYMBOLS    = [s.upper() for s in body.symbols]
    if body.risk_pct  is not None: config.RISK_PCT   = body.risk_pct
    if body.atr_mult  is not None: config.ATR_MULT   = body.atr_mult
    if body.rr_ratio  is not None: config.RR_RATIO   = body.rr_ratio
    if body.threshold is not None: config.THRESHOLD  = body.threshold
    if body.trade_dir is not None: config.TRADE_DIR  = body.trade_dir
    if body.use_ema   is not None: config.USE_EMA    = body.use_ema
    if body.use_time  is not None: config.USE_TIME   = body.use_time
    return {"ok": True}


# ── WebSocket (live log stream) ───────────────────────────────────────────────

_ws_clients: list[WebSocket] = []


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        last_sent = 0
        while True:
            logs = list(bot.logs)
            if len(logs) > last_sent:
                for line in logs[last_sent:]:
                    await ws.send_text(line)
                last_sent = len(logs)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        _ws_clients.remove(ws)


# ── Frontend ──────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def root():
    return FileResponse(str(FRONTEND / "index.html"))
