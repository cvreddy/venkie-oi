"""
====================================================
  NIFTY50 OI Middleware Server — Upstox API
  UPGRADED:
    - Strike prices fixed (ATM ±5 only)
    - OI Change tracking (5min + day)
    - RSI + ADX from candle data
    - Auto snapshot saved every 5min (JSON + CSV)
    - /get_token for SENSEX sharing
====================================================
FOLDER:
  Nifty OI/
    upstox_oi_server.py
    templates/dashboard.html
    snapshots/           ← auto-created, stores CSV logs
"""

import os
import csv
import time
import threading
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, request, redirect, render_template
from flask_cors import CORS
import requests

app  = Flask(__name__)
CORS(app)

# ── CONFIG ─────────────────────────────────────────
API_KEY      = "dc927c0f-918a-4c21-ae03-493acaa0608a"
API_SECRET   = "21ebqgxrft"
REDIRECT_URI = "https://venkie-oi.onrender.com/callback"
NIFTY_KEY    = "NSE_INDEX|Nifty 50"
CACHE_TTL    = 300          # 5 minutes
STRIKE_STEP  = 50
ATM_RANGE    = 5            # ATM ± 5 strikes

SNAPSHOT_DIR = "snapshots"  # folder to save CSV data

# ── STORES ─────────────────────────────────────────
token_store = {"access_token": None}
oi_cache    = {"data": None}
prev_oi     = {}        # previous cycle OI for change calc
baseline_oi = {}        # day start OI for daily change calc
candle_history = []     # list of OHLCV dicts for RSI/ADX


# ══════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════

@app.route("/login")
def login():
    url = (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"
    )
    return redirect(url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Error: No auth code", 400
    resp = requests.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data={"code": code, "client_id": API_KEY, "client_secret": API_SECRET,
              "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"},
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    )
    data = resp.json()
    token_store["access_token"] = data.get("access_token")
    print("[LOGIN] Token received:", (token_store["access_token"] or "")[:20])
    refresh()
    return """<html><body style="font-family:sans-serif;background:#0a0c10;color:#00e676;padding:40px">
    <h2>✅ Login Successful!</h2>
    <p><a href="/" style="color:#40c4ff">→ Open Dashboard</a></p>
    </body></html>"""


@app.route("/get_token")
def get_token():
    """Share token with SENSEX server."""
    if not token_store["access_token"]:
        return jsonify({"error": "No token — login first"})
    return jsonify({"token": token_store["access_token"]})


def hdrs():
    return {"Authorization": f"Bearer {token_store['access_token']}", "Accept": "application/json"}


# ══════════════════════════════════════════════════
#  FETCHERS
# ══════════════════════════════════════════════════

def fetch_spot():
    try:
        r = requests.get("https://api.upstox.com/v2/market-quote/ltp",
                         params={"symbol": NIFTY_KEY}, headers=hdrs(), timeout=10)
        d = r.json().get("data", {})
        if not d: return 0
        key = list(d.keys())[0]
        return float(d[key].get("last_price", 0))
    except Exception as e:
        print("[SPOT ERROR]", e); return 0


def fetch_futures(spot):
    try:
        r = requests.get("https://api.upstox.com/v2/market-quote/ltp",
                         params={"symbol": "NSE_FO|NIFTY25APRFUT"},
                         headers=hdrs(), timeout=10)
        if r.status_code == 200:
            d = r.json().get("data", {})
            if d:
                key = list(d.keys())[0]
                p = d[key].get("last_price") or d[key].get("ltp") or 0
                if p: return float(p)
    except Exception as e:
        print("[FUT ERROR]", e)
    return round(spot * 1.005, 2)


def fetch_vix():
    try:
        r = requests.get("https://api.upstox.com/v2/market-quote/ltp",
                         params={"symbol": "NSE_INDEX|India VIX"},
                         headers=hdrs(), timeout=10)
        if r.status_code == 200:
            d = r.json().get("data", {})
            if d:
                key = list(d.keys())[0]
                return float(d[key].get("last_price") or d[key].get("ltp") or 0)
    except Exception as e:
        print("[VIX ERROR]", e)
    return 0


def fetch_candles():
    """
    Fetch last 30 x 5-min candles for NIFTY from Upstox historical API.
    Used for RSI and ADX calculation.
    """
    try:
        today = date.today().strftime("%Y-%m-%d")
        r = requests.get(
            f"https://api.upstox.com/v2/historical-candle/{NIFTY_KEY}/5minute/{today}/{today}",
            headers=hdrs(), timeout=10
        )
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            # Each candle: [timestamp, open, high, low, close, volume, oi]
            result = []
            for c in candles[-30:]:  # last 30 candles = 150 min
                if len(c) >= 5:
                    result.append({
                        "time":   c[0],
                        "open":   float(c[1]),
                        "high":   float(c[2]),
                        "low":    float(c[3]),
                        "close":  float(c[4]),
                        "volume": float(c[5]) if len(c) > 5 else 0
                    })
            print(f"[CANDLES] Fetched {len(result)} candles")
            return result
    except Exception as e:
        print("[CANDLES ERROR]", e)
    return []


def get_expiry():
    try:
        r = requests.get("https://api.upstox.com/v2/option/contract",
                         params={"instrument_key": NIFTY_KEY},
                         headers=hdrs(), timeout=10)
        if r.status_code == 200:
            items = r.json().get("data", [])
            expiries = []
            for item in items:
                if isinstance(item, str): expiries.append(item)
                elif isinstance(item, dict):
                    e = item.get("expiry") or item.get("expiry_date")
                    if e: expiries.append(e)
            today = datetime.today().strftime("%Y-%m-%d")
            for exp in sorted(expiries):
                if exp >= today:
                    print("[EXPIRY] Using:", exp)
                    return exp
    except Exception as e:
        print("[EXPIRY ERROR]", e)
    today = date.today()
    days  = (3 - today.weekday()) % 7
    if days == 0: days = 7
    fb = (today + timedelta(days=days)).strftime("%Y-%m-%d")
    print("[EXPIRY FALLBACK]", fb)
    return fb


def fetch_chain(expiry):
    try:
        r = requests.get("https://api.upstox.com/v2/option/chain",
                         params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
                         headers=hdrs(), timeout=15)
        data = r.json().get("data", [])
        print(f"[CHAIN] Expiry={expiry} Records={len(data)}")
        return data
    except Exception as e:
        print("[CHAIN ERROR]", e); return []


# ══════════════════════════════════════════════════
#  RSI + ADX CALCULATION
# ══════════════════════════════════════════════════

def calc_rsi(closes, period=14):
    """Standard RSI from close prices."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    # Initial avg
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def calc_adx(candles, period=14):
    """
    Standard ADX from OHLC candles.
    Returns (ADX, +DI, -DI).
    """
    if len(candles) < period + 1:
        return None, None, None
    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i-1]["close"]
        ph = candles[i-1]["high"]
        pl = candles[i-1]["low"]
        tr  = max(h - l, abs(h - pc), abs(l - pc))
        pdm = max(h - ph, 0) if (h - ph) > (pl - l) else 0
        ndm = max(pl - l, 0) if (pl - l) > (h - ph) else 0
        tr_list.append(tr)
        pdm_list.append(pdm)
        ndm_list.append(ndm)

    def smooth(lst, p):
        s = sum(lst[:p])
        result = [s]
        for i in range(p, len(lst)):
            s = s - (s / p) + lst[i]
            result.append(s)
        return result

    atr  = smooth(tr_list,  period)
    pDM  = smooth(pdm_list, period)
    nDM  = smooth(ndm_list, period)
    dx_list = []
    for i in range(len(atr)):
        if atr[i] == 0: continue
        pDI = 100 * pDM[i] / atr[i]
        nDI = 100 * nDM[i] / atr[i]
        dx  = 100 * abs(pDI - nDI) / (pDI + nDI) if (pDI + nDI) else 0
        dx_list.append((dx, pDI, nDI))

    if not dx_list: return None, None, None
    # ADX = smoothed average of DX
    dx_vals  = [x[0] for x in dx_list]
    adx_val  = sum(dx_vals[-period:]) / min(period, len(dx_vals))
    last_pDI = dx_list[-1][1]
    last_nDI = dx_list[-1][2]
    return round(adx_val, 2), round(last_pDI, 2), round(last_nDI, 2)


def get_indicators(candles):
    """Returns RSI, ADX, +DI, -DI and signal strings."""
    if not candles or len(candles) < 15:
        return {"rsi": None, "adx": None, "pdi": None, "ndi": None,
                "rsi_signal": "N/A", "adx_signal": "N/A", "adx_trend": "N/A"}

    closes = [c["close"] for c in candles]
    rsi = calc_rsi(closes, 14)
    adx, pdi, ndi = calc_adx(candles, 14)

    # RSI interpretation
    if rsi is None:
        rsi_sig = "N/A"
    elif rsi >= 70:
        rsi_sig = "OVERBOUGHT"
    elif rsi <= 30:
        rsi_sig = "OVERSOLD"
    elif rsi >= 60:
        rsi_sig = "BULLISH"
    elif rsi <= 40:
        rsi_sig = "BEARISH"
    else:
        rsi_sig = "NEUTRAL"

    # ADX interpretation
    if adx is None:
        adx_sig, adx_trend = "N/A", "N/A"
    elif adx >= 25:
        adx_sig = "STRONG TREND"
        adx_trend = "BULLISH" if (pdi or 0) > (ndi or 0) else "BEARISH"
    elif adx >= 20:
        adx_sig = "DEVELOPING"
        adx_trend = "BULLISH" if (pdi or 0) > (ndi or 0) else "BEARISH"
    else:
        adx_sig   = "SIDEWAYS"
        adx_trend = "RANGING"

    return {
        "rsi":        rsi,
        "adx":        adx,
        "pdi":        pdi,
        "ndi":        ndi,
        "rsi_signal": rsi_sig,
        "adx_signal": adx_sig,
        "adx_trend":  adx_trend
    }


# ══════════════════════════════════════════════════
#  CHAIN PROCESSING
# ══════════════════════════════════════════════════

def round_to_strike(price, step=50):
    return round(round(price / step) * step, 2)


def process_chain(raw):
    global prev_oi, baseline_oi
    result = {}
    is_first = len(prev_oi) == 0

    for item in raw:
        strike = float(item.get("strike_price", 0))
        if not strike: continue

        ce    = item.get("call_options", {})
        pe    = item.get("put_options",  {})
        ce_md = ce.get("market_data",   {})
        pe_md = pe.get("market_data",   {})
        ce_gk = ce.get("option_greeks", {})
        pe_gk = pe.get("option_greeks", {})

        call_oi  = float(ce_md.get("oi",     0) or 0)
        put_oi   = float(pe_md.get("oi",     0) or 0)
        call_vol = float(ce_md.get("volume", 0) or 0)
        put_vol  = float(pe_md.get("volume", 0) or 0)
        call_ltp = float(ce_md.get("ltp", 0) or ce_md.get("last_price", 0) or 0)
        put_ltp  = float(pe_md.get("ltp", 0) or pe_md.get("last_price", 0) or 0)
        call_iv  = float(ce_gk.get("iv", 0) or 0) * 100
        put_iv   = float(pe_gk.get("iv", 0) or 0) * 100

        prev = prev_oi.get(strike, {})
        call_oi_chg = call_oi - prev.get("call_oi", call_oi) if prev else 0
        put_oi_chg  = put_oi  - prev.get("put_oi",  put_oi)  if prev else 0

        base = baseline_oi.get(strike, {})
        call_oi_chg_day = call_oi - base.get("call_oi", call_oi) if base else 0
        put_oi_chg_day  = put_oi  - base.get("put_oi",  put_oi)  if base else 0

        result[strike] = {
            "strike":           strike,
            "call_oi":          call_oi,
            "call_oi_chg":      call_oi_chg,
            "call_oi_chg_day":  call_oi_chg_day,
            "call_vol":         call_vol,
            "call_vol_oi":      round(call_vol / call_oi, 2) if call_oi else 0,
            "call_iv":          round(call_iv,  2),
            "call_ltp":         call_ltp,
            "put_oi":           put_oi,
            "put_oi_chg":       put_oi_chg,
            "put_oi_chg_day":   put_oi_chg_day,
            "put_vol":          put_vol,
            "put_vol_oi":       round(put_vol / put_oi, 2) if put_oi else 0,
            "put_iv":           round(put_iv,  2),
            "put_ltp":          put_ltp,
            "pcr":              round(put_oi / call_oi, 2) if call_oi else 0,
            "net_oi":           put_oi - call_oi,
        }

    if is_first and result:
        baseline_oi = {s: {"call_oi": v["call_oi"], "put_oi": v["put_oi"]} for s, v in result.items()}
        print(f"[OI] Baseline set — {len(baseline_oi)} strikes")

    return result


def compute_max_pain(chain):
    strikes = sorted(chain.keys())
    if not strikes: return 0
    min_loss, mp = float("inf"), strikes[0]
    for s in strikes:
        loss = sum(
            v["call_oi"]*(s-k) if k < s else v["put_oi"]*(k-s) if k > s else 0
            for k, v in chain.items()
        )
        if loss < min_loss: min_loss = loss; mp = s
    return mp


def analyse_trend(atm_strikes, atm):
    if not atm_strikes: return "NEUTRAL", "Insufficient data", 50
    calls = [(s, v) for s, v in atm_strikes.items() if s > atm]
    puts  = [(s, v) for s, v in atm_strikes.items() if s < atm]
    tc = sum(v["call_oi"] for _, v in calls)
    tp = sum(v["put_oi"]  for _, v in puts)
    ca = sum(v["call_oi_chg"] for _, v in calls if v["call_oi_chg"] > 0)
    pa = sum(v["put_oi_chg"]  for _, v in puts  if v["put_oi_chg"]  > 0)
    ce = abs(sum(v["call_oi_chg"] for _, v in calls if v["call_oi_chg"] < 0))
    pe = abs(sum(v["put_oi_chg"]  for _, v in puts  if v["put_oi_chg"]  < 0))
    pcr_atm = tp / tc if tc else 1.0
    score, reasons = 0, []
    if tc > tp * 1.2:   score -= 2; reasons.append("Call OI dominates — strong resistance above")
    elif tp > tc * 1.2: score += 2; reasons.append("Put OI dominates — strong support below")
    if ca > pa * 1.3:   score -= 2; reasons.append("Fresh call writing — sellers adding resistance")
    elif pa > ca * 1.3: score += 2; reasons.append("Fresh put writing — support being built")
    if ce > pe * 1.3:   score += 1; reasons.append("Call unwinding — resistance easing")
    elif pe > ce * 1.3: score -= 1; reasons.append("Put unwinding — support easing")
    if pcr_atm > 1.2:   score += 1; reasons.append(f"PCR {pcr_atm:.2f} — bullish near ATM")
    elif pcr_atm < 0.8: score -= 1; reasons.append(f"PCR {pcr_atm:.2f} — bearish near ATM")
    if   score >=  3: t, s = "STRONGLY BULLISH", 90
    elif score ==  2: t, s = "BULLISH",           70
    elif score ==  1: t, s = "MILD BULLISH",      60
    elif score == -1: t, s = "MILD BEARISH",      40
    elif score == -2: t, s = "BEARISH",           30
    elif score <= -3: t, s = "STRONGLY BEARISH",  10
    else:             t, s = "NEUTRAL / SIDEWAYS", 50
    return t, " | ".join(reasons) if reasons else "OI balanced — range-bound expected", s


# ══════════════════════════════════════════════════
#  SNAPSHOT — save CSV every refresh
# ══════════════════════════════════════════════════

def save_snapshot(data, atm_strikes):
    """
    Saves two files every 5 minutes:
    1. snapshots/YYYY-MM-DD.csv  — daily log of all refreshes
    2. snapshots/latest.csv      — always the latest snapshot
    """
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        now       = datetime.now()
        ts        = now.strftime("%Y-%m-%d %H:%M:%S")
        date_str  = now.strftime("%Y-%m-%d")
        daily_file  = os.path.join(SNAPSHOT_DIR, f"nifty_oi_{date_str}.csv")
        latest_file = os.path.join(SNAPSHOT_DIR, "latest.csv")

        ind = data.get("indicators", {})

        # Header for full snapshot file
        fieldnames = [
            "timestamp", "spot", "futures", "premium", "atm", "pcr",
            "vix", "max_pain", "trend", "trend_strength",
            "rsi", "rsi_signal", "adx", "adx_signal", "adx_trend", "pdi", "ndi",
            "strike",
            "call_oi", "call_oi_chg", "call_oi_chg_day", "call_vol", "call_vol_oi", "call_iv", "call_ltp",
            "put_oi",  "put_oi_chg",  "put_oi_chg_day",  "put_vol",  "put_vol_oi",  "put_iv",  "put_ltp",
            "pcr_strike", "net_oi"
        ]

        rows = []
        for strike in sorted(atm_strikes.keys()):
            v = atm_strikes[strike]
            rows.append({
                "timestamp":        ts,
                "spot":             data.get("spot"),
                "futures":          data.get("futures"),
                "premium":          data.get("premium"),
                "atm":              data.get("atm"),
                "pcr":              data.get("pcr"),
                "vix":              data.get("vix"),
                "max_pain":         data.get("max_pain"),
                "trend":            data.get("trend"),
                "trend_strength":   data.get("trend_strength"),
                "rsi":              ind.get("rsi"),
                "rsi_signal":       ind.get("rsi_signal"),
                "adx":              ind.get("adx"),
                "adx_signal":       ind.get("adx_signal"),
                "adx_trend":        ind.get("adx_trend"),
                "pdi":              ind.get("pdi"),
                "ndi":              ind.get("ndi"),
                "strike":           int(strike),
                "call_oi":          v["call_oi"],
                "call_oi_chg":      v["call_oi_chg"],
                "call_oi_chg_day":  v["call_oi_chg_day"],
                "call_vol":         v["call_vol"],
                "call_vol_oi":      v["call_vol_oi"],
                "call_iv":          v["call_iv"],
                "call_ltp":         v["call_ltp"],
                "put_oi":           v["put_oi"],
                "put_oi_chg":       v["put_oi_chg"],
                "put_oi_chg_day":   v["put_oi_chg_day"],
                "put_vol":          v["put_vol"],
                "put_vol_oi":       v["put_vol_oi"],
                "put_iv":           v["put_iv"],
                "put_ltp":          v["put_ltp"],
                "pcr_strike":       v["pcr"],
                "net_oi":           v["net_oi"],
            })

        # Append to daily CSV
        write_header = not os.path.exists(daily_file)
        with open(daily_file, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header: w.writeheader()
            w.writerows(rows)

        # Overwrite latest CSV
        with open(latest_file, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)

        print(f"[SNAPSHOT] Saved {len(rows)} rows → {daily_file}")
    except Exception as e:
        print("[SNAPSHOT ERROR]", e)


# ══════════════════════════════════════════════════
#  MAIN REFRESH
# ══════════════════════════════════════════════════

def refresh():
    global prev_oi, candle_history

    if not token_store["access_token"]:
        print("[REFRESH] No token"); return

    try:
        spot    = fetch_spot()
        expiry  = get_expiry()
        raw     = fetch_chain(expiry)
        if not raw: print("[REFRESH] Empty chain"); return

        atm   = round_to_strike(spot, STRIKE_STEP)
        chain = process_chain(raw)
        if not chain: return

        # ATM ± 5 strikes only
        atm_strikes = {
            s: v for s, v in chain.items()
            if abs(s - atm) <= ATM_RANGE * STRIKE_STEP
        }

        total_call = sum(v["call_oi"] for v in chain.values())
        total_put  = sum(v["put_oi"]  for v in chain.values())
        pcr        = round(total_put / total_call, 2) if total_call else 0
        max_pain   = compute_max_pain(chain)
        futures    = fetch_futures(spot)
        vix        = fetch_vix()

        # RSI + ADX
        candles = fetch_candles()
        if candles: candle_history = candles
        indicators = get_indicators(candle_history)

        trend, trend_reason, trend_strength = analyse_trend(atm_strikes, atm)

        data = {
            "spot":           spot,
            "futures":        futures,
            "premium":        round(futures - spot, 2),
            "atm":            atm,
            "pcr":            pcr,
            "vix":            vix,
            "max_pain":       max_pain,
            "expiry":         expiry,
            "trend":          trend,
            "trend_reason":   trend_reason,
            "trend_strength": trend_strength,
            "indicators":     indicators,
            "resistance":     sorted([(s, v["call_oi"]) for s, v in atm_strikes.items() if s >= atm], key=lambda x: x[1], reverse=True)[:3],
            "support":        sorted([(s, v["put_oi"])  for s, v in atm_strikes.items() if s <= atm], key=lambda x: x[1], reverse=True)[:3],
            "atm_strikes":    atm_strikes,
            "chain":          chain,
            "timestamp":      datetime.now().isoformat()
        }

        oi_cache["data"] = data

        # Save snapshot CSV
        save_snapshot(data, atm_strikes)

        # Update prev for next cycle
        prev_oi = {s: {"call_oi": v["call_oi"], "put_oi": v["put_oi"]} for s, v in chain.items()}

        print(f"[OI] ✅ Spot={spot} | Futures={futures} | VIX={vix} | PCR={pcr} | ATM={atm} | MaxPain={max_pain}")
        print(f"[IND] RSI={indicators['rsi']} ({indicators['rsi_signal']}) | ADX={indicators['adx']} ({indicators['adx_signal']}) | Trend={trend}")

    except Exception as e:
        import traceback; print("[REFRESH ERROR]", e); traceback.print_exc()


def loop():
    while True:
        time.sleep(CACHE_TTL)
        refresh()


# ══════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/oi/json")
def oi_json():
    if not oi_cache["data"]:
        return jsonify({"error": "No data yet — login at /login"})
    return jsonify(oi_cache["data"])


@app.route("/oi/histogram")
def histogram():
    if not oi_cache["data"]: return jsonify([])
    chain = oi_cache["data"]["chain"]
    atm   = oi_cache["data"]["atm"]
    return jsonify(sorted(
        [v for s, v in chain.items() if abs(s - atm) <= ATM_RANGE * STRIKE_STEP],
        key=lambda x: x["strike"]
    ))


@app.route("/oi/snapshots")
def list_snapshots():
    """List all saved snapshot CSV files."""
    try:
        files = sorted(os.listdir(SNAPSHOT_DIR), reverse=True)
        return jsonify({"files": files, "dir": os.path.abspath(SNAPSHOT_DIR)})
    except:
        return jsonify({"files": [], "dir": SNAPSHOT_DIR})


@app.route("/oi/status")
def oi_status():
    return jsonify({
        "token":    bool(token_store["access_token"]),
        "has_data": oi_cache["data"] is not None,
        "spot":     oi_cache["data"]["spot"]      if oi_cache["data"] else None,
        "pcr":      oi_cache["data"]["pcr"]       if oi_cache["data"] else None,
        "trend":    oi_cache["data"]["trend"]     if oi_cache["data"] else None,
        "rsi":      oi_cache["data"]["indicators"]["rsi"] if oi_cache["data"] else None,
        "adx":      oi_cache["data"]["indicators"]["adx"] if oi_cache["data"] else None,
        "updated":  oi_cache["data"]["timestamp"] if oi_cache["data"] else None,
    })


# ══════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  NIFTY OI Server  (Upgraded)")
    print("  Step 1: http://localhost:5000/login")
    print("  Step 2: http://localhost:5000")
    print(f"  Snapshots saved to: ./{SNAPSHOT_DIR}/")
    print("=" * 55)
    threading.Thread(target=loop, daemon=True).start()
    app.run(port=5000, debug=False)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)