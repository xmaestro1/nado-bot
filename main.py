"""
Nado.xyz Grid Trading Bot — Long + Short
==========================================
LONG Grid: preis fällt → kaufen (5 levels), preis steigt → verkaufen (5 levels)
SHORT Grid: preis steigt → shorten (5 levels), preis fällt → zurückkaufen (5 levels)

Start:     4/7 Indikatoren gleiche Richtung → Grid starten
Wechsel:   Alle 7 Indikatoren andere Richtung → Grid schließen + neue Richtung
SL:        Alle 5 Levels voll + 1% weiter → alles schließen

7 Indikatoren: RSI, MACD, EMA9/21, Bollinger Bands, VWAP, Stochastic RSI, OBV

Einrichten: SIGNER_KEY = 1-Click Trading Key (app.nado.xyz → Settings)
"""

import time, random, requests, sys, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN; M=Fore.MAGENTA
    X=Style.RESET_ALL; B=Style.BRIGHT
except:
    G=R=Y=C=M=X=B=""

# ═══════════════════════════════════════════════════════════
WALLET_ADDR = "0x14A26C3F3fF2C7A5bC4a1E5E5B15972628288ab7"
SIGNER_KEY  = "0xf7bbe14bbca148872730e063ca969d37f2d7006b22e4486877adaef9206192f9"
SUBACCOUNT  = "0x14a26c3f3ff2c7a5bc4a1e5e5b15972628288ab764656661756c740000000000"

PRODUCT_ID  = 2
CHAIN_ID    = 57073
GATEWAY     = "https://gateway.prod.nado.xyz/v1"
ARCHIVE     = "https://archive.prod.nado.xyz/v1"
HEADERS     = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0015  # BTC pro Level
GRID_LEVELS  = 5       # Anzahl Levels
GRID_STEP    = 0.2     # % Abstand zwischen Levels
GRID_PROFIT  = 0.2     # % Gewinn pro Level
SL_PCT       = 1.0     # % außerhalb letztem Level → SL
MIN_SIGNAL   = 4       # Min Indikatoren für ersten Start
SYNC_WAIT    = 180     # Sek nach Order kein Sync
INTERVAL     = 30      # Sek pro Tick
DRY_RUN      = False
# ═══════════════════════════════════════════════════════════

# State
grid_mode     = None    # "LONG" oder "SHORT"
grid          = []      # [{entry_price, exit_price, filled, open_time}]
wins          = 0
total_pnl     = 0.0
prev_preis    = None
last_order_t  = 0.0
just_acted    = False   # Verhindert Buy+Sell im gleichen Tick


def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(m, c=""):
    print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}")
    sys.stdout.flush()

def fmt(x):
    try:    return f"${float(x):,.2f}"
    except: return "?"

def filled_count():
    return sum(1 for lv in grid if lv["filled"])

def total_long_size():
    """Gesamte LONG Position in BTC."""
    if grid_mode == "LONG":
        return round(filled_count() * ORDER_SIZE, 4)
    return 0.0

def total_short_size():
    """Gesamte SHORT Position in BTC."""
    if grid_mode == "SHORT":
        return round(filled_count() * ORDER_SIZE, 4)
    return 0.0


# ─── API ──────────────────────────────────────────────────

def get_preis():
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=all_products",
            headers={"Accept-Encoding": "gzip"},
            timeout=15, verify=False
        )
        data = r.json().get("data", r.json())
        for p in data.get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e:
        log(f"Preis Fehler: {e}", Y)
    return None


def get_kerzen(limit=100):
    """5-Min Kerzen, älteste zuerst, mit open/high/low/close/volume."""
    try:
        r = requests.post(
            ARCHIVE,
            json={"candlesticks": {"product_id": PRODUCT_ID, "granularity": 300, "limit": limit}},
            headers=HEADERS, timeout=15, verify=False
        )
        cs = r.json().get("candlesticks", [])
        if not cs: return None
        candles = [{
            "o": float(c.get("open_x18",  0)) / 1e18,
            "h": float(c.get("high_x18",  0)) / 1e18,
            "l": float(c.get("low_x18",   0)) / 1e18,
            "c": float(c.get("close_x18", 0)) / 1e18,
            "v": float(c.get("volume",    0)) / 1e18,
        } for c in cs]
        return list(reversed(candles))  # älteste zuerst, neueste zuletzt
    except Exception as e:
        log(f"Kerzen Fehler: {e}", Y)
    return None


def get_nado_position():
    """Echte Position von Nado: positiv=LONG, negativ=SHORT, 0=keine."""
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}",
            headers={"Accept-Encoding": "gzip"},
            timeout=15, verify=False
        )
        data = r.json().get("data", {})
        for pb in data.get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                return float(pb["balance"]["amount"]) / 1e18
    except Exception as e:
        log(f"Nado API Fehler: {e}", Y)
    return None


# ─── INDIKATOREN ──────────────────────────────────────────

def calc_ema(closes, n):
    if len(closes) < n: return None
    k = 2 / (n + 1)
    e = sum(closes[:n]) / n
    for x in closes[n:]:
        e = x * k + e * (1 - k)
    return e


def calc_rsi(closes, n=14):
    if len(closes) < n + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n-1) + gains[i]) / n
        al = (al * (n-1) + losses[i]) / n
    return 100 if al == 0 else 100 - (100 / (1 + ag / al))


def calc_macd(closes):
    """MACD Signal: positiv=LONG, negativ=SHORT."""
    if len(closes) < 26: return None
    e12 = calc_ema(closes, 12)
    e26 = calc_ema(closes, 26)
    if e12 is None or e26 is None: return None
    macd_line = e12 - e26
    # Signal line = EMA9 von MACD
    macd_vals = []
    for i in range(26, len(closes) + 1):
        e12i = calc_ema(closes[:i], 12)
        e26i = calc_ema(closes[:i], 26)
        if e12i and e26i: macd_vals.append(e12i - e26i)
    if len(macd_vals) < 9: return None
    signal = calc_ema(macd_vals, 9)
    if signal is None: return None
    return macd_line - signal  # positiv=LONG, negativ=SHORT


def calc_bb(closes, n=20):
    """Bollinger Bands: gibt position zurück. >0=über Mitte=bullish, <0=unter Mitte=bearish."""
    if len(closes) < n: return None
    sma = sum(closes[-n:]) / n
    std = (sum((x - sma) ** 2 for x in closes[-n:]) / n) ** 0.5
    upper = sma + 2 * std
    lower = sma - 2 * std
    cur = closes[-1]
    # Normalisiert: +1=über Mitte, -1=unter Mitte
    mid = (upper + lower) / 2
    return cur - mid  # positiv=über Mitte=bullish


def calc_vwap(candles):
    """VWAP: Preis über VWAP=bullish, darunter=bearish."""
    if not candles: return None
    tvp = sum(c["v"] * (c["h"] + c["l"] + c["c"]) / 3 for c in candles)
    tv  = sum(c["v"] for c in candles)
    if tv == 0: return None
    vwap = tvp / tv
    return candles[-1]["c"] - vwap  # positiv=über VWAP=bullish


def calc_stoch_rsi(closes, n=14, k=3):
    """Stochastic RSI: >0.5=bullish, <0.5=bearish."""
    if len(closes) < n * 2: return None
    rsi_vals = []
    for i in range(n, len(closes) + 1):
        r = calc_rsi(closes[:i], n)
        if r is not None: rsi_vals.append(r)
    if len(rsi_vals) < n: return None
    recent = rsi_vals[-n:]
    low_r  = min(recent)
    high_r = max(recent)
    if high_r == low_r: return 0.5
    stoch = (rsi_vals[-1] - low_r) / (high_r - low_r)
    return stoch - 0.5  # positiv=bullish, negativ=bearish


def calc_obv(candles):
    """On-Balance Volume: steigend=bullish, fallend=bearish."""
    if len(candles) < 2: return None
    obv = 0.0
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i-1]["c"]:
            obv += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]:
            obv -= candles[i]["v"]
    # OBV Trend: vergleiche letzten mit vorherigem
    obv_prev = 0.0
    for i in range(1, len(candles) - 1):
        if candles[i]["c"] > candles[i-1]["c"]:
            obv_prev += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]:
            obv_prev -= candles[i]["v"]
    return obv - obv_prev  # positiv=steigend=bullish


def get_signal(candles):
    """
    Berechnet alle 7 Indikatoren.
    Gibt zurück: (long_count, short_count, details)
    """
    if not candles or len(candles) < 30:
        return 0, 0, {}

    closes = [c["c"] for c in candles]

    # Alle 7 berechnen
    rsi      = calc_rsi(closes)
    macd     = calc_macd(closes)
    ema_diff = (calc_ema(closes, 9) or 0) - (calc_ema(closes, 21) or 0)
    bb       = calc_bb(closes)
    vwap     = calc_vwap(candles)
    stoch    = calc_stoch_rsi(closes)
    obv      = calc_obv(candles)

    if any(x is None for x in [rsi, macd, bb, vwap, stoch, obv]):
        return 0, 0, {}

    # Jeder Indikator gibt LONG oder SHORT
    signals = {
        "RSI":       "LONG" if rsi > 50      else "SHORT",
        "MACD":      "LONG" if macd > 0      else "SHORT",
        "EMA":       "LONG" if ema_diff > 0  else "SHORT",
        "BB":        "LONG" if bb > 0        else "SHORT",
        "VWAP":      "LONG" if vwap > 0      else "SHORT",
        "StochRSI":  "LONG" if stoch > 0     else "SHORT",
        "OBV":       "LONG" if obv > 0       else "SHORT",
    }

    long_count  = sum(1 for v in signals.values() if v == "LONG")
    short_count = sum(1 for v in signals.values() if v == "SHORT")

    details = {
        "RSI": round(rsi, 1),
        "L": long_count,
        "S": short_count,
    }

    return long_count, short_count, details


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x", ""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def place_order(is_buy, price, size):
    """
    is_buy=True  → positive Menge → LONG öffnen ODER SHORT schließen
    is_buy=False → negative Menge → SHORT öffnen ODER LONG schließen
    Kein reduce_only. Preis mit 0.2% Slippage.
    """
    global last_order_t
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
        last_order_t = time.time()
        return True
    try:
        from eth_account import Account
        # Limit Order nahe am Marktpreis — 0.05% Slippage statt 0.2%
        px    = round(price * (1.0005 if is_buy else 0.9995)) * int(1e18)
        amt   = int(size * 1e18) if is_buy else -int(size * 1e18)
        exp   = int(time.time()) + 60
        nonce = ((int(time.time() * 1000) + 5000) << 20) + random.randint(0, 99999)
        apx   = 1
        sndr  = sender_hex()
        dom = {
            "name": "Nado", "version": "0.0.1",
            "chainId": CHAIN_ID,
            "verifyingContract": f"0x{PRODUCT_ID:040x}"
        }
        typ = {"Order": [
            {"name": "sender",     "type": "bytes32"},
            {"name": "priceX18",   "type": "int128"},
            {"name": "amount",     "type": "int128"},
            {"name": "expiration", "type": "uint64"},
            {"name": "nonce",      "type": "uint64"},
            {"name": "appendix",   "type": "uint128"},
        ]}
        msg = {
            "sender": sndr, "priceX18": px, "amount": amt,
            "expiration": exp, "nonce": nonce, "appendix": apx
        }
        acc = Account.from_key(SIGNER_KEY)
        sig = acc.sign_typed_data(
            domain_data=dom, message_types=typ, message_data=msg
        ).signature.hex()
        if not sig.startswith("0x"): sig = "0x" + sig
        pld = {"place_order": {"product_id": PRODUCT_ID, "order": {
            "sender": sndr, "priceX18": str(px), "amount": str(amt),
            "expiration": str(exp), "nonce": str(nonce), "appendix": str(apx)
        }, "signature": sig}}
        r = requests.post(
            f"{GATEWAY}/execute", json=pld,
            headers=HEADERS, timeout=15, verify=False
        )
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G)
            last_order_t = time.time()
            # Warte kurz und prüfe ob Order wirklich auf Nado gefüllt wurde
            time.sleep(5)
            nado = get_nado_position()
            if nado is not None and abs(nado) < 0.0001 and not is_buy:
                log("⚠️ Order wurde nicht gefüllt — State zurücksetzen", Y)
                return "NOT_FILLED"
            return True
        code = d.get("error_code", 0)
        err  = d.get("error", "")
        if code == 2006:
            log("⚠️ Kein Kapital (2064) — Level überspringen", Y)
            return "NO_MARGIN"
        log(f"❌ {err} (Code:{code})", R)
        return False
    except Exception as e:
        log(f"Order Exception: {e}", R)
        return False


# ─── GRID AUFBAUEN ────────────────────────────────────────

def build_grid(preis, modus):
    """
    Baut Grid auf.
    LONG: 5 Levels UNTER aktuellem Preis
    SHORT: 5 Levels ÜBER aktuellem Preis
    Sofort ersten Trade beim Start.
    """
    global grid, grid_mode, wins, total_pnl
    grid_mode = modus
    grid = []

    for i in range(1, GRID_LEVELS + 1):
        if modus == "LONG":
            entry_p = round(preis * (1 - i * GRID_STEP / 100))
            exit_p  = round(entry_p * (1 + GRID_PROFIT / 100))
        else:  # SHORT
            entry_p = round(preis * (1 + i * GRID_STEP / 100))
            exit_p  = round(entry_p * (1 - GRID_PROFIT / 100))
        grid.append({
            "entry_price": entry_p,
            "exit_price":  exit_p,
            "filled":      False,
            "open_time":   0.0,
        })

    lvls = " | ".join(fmt(lv["entry_price"]) for lv in grid)
    log(f"{G if modus=='LONG' else R}{modus} Grid @ {fmt(preis)} | Levels: {lvls}{X}", C)

    if modus == "LONG":
        sl_p = grid[-1]["entry_price"] * (1 - SL_PCT / 100)
    else:
        sl_p = grid[-1]["entry_price"] * (1 + SL_PCT / 100)
    log(f"SL @ {fmt(sl_p)}", Y)

    # Sofort ersten Trade beim Start
    log(f"{'🟢 LONG' if modus == 'LONG' else '🔴 SHORT'} Soforteinstieg @ {fmt(preis)}", G if modus == "LONG" else R)
    is_buy = (modus == "LONG")
    ok = place_order(is_buy, preis, ORDER_SIZE)
    if ok is True:
        exit_p = round(preis * (1 + GRID_PROFIT / 100)) if modus == "LONG" else round(preis * (1 - GRID_PROFIT / 100))
        grid.insert(0, {
            "entry_price": round(preis),
            "exit_price":  exit_p,
            "filled":      True,
            "open_time":   time.time(),
        })


def close_all(preis, reason=""):
    """Schließt alle offenen Positionen auf einmal."""
    global grid, grid_mode
    n = filled_count()
    if n == 0:
        grid = []
        grid_mode = None
        return
    size = round(n * ORDER_SIZE, 4)
    log(f"⛔ {reason} — Schließe {n} Levels ({size} BTC)", R)
    # LONG schließen = SELL, SHORT schließen = BUY
    is_buy = (grid_mode == "SHORT")
    ok = place_order(is_buy, preis, size)
    if ok is True or DRY_RUN:
        grid = []
        grid_mode = None
        log("Alle Positionen geschlossen.", Y)


# ─── SYNC ─────────────────────────────────────────────────

def sync_nado(preis):
    """Prüft ob Bot-State mit Nado übereinstimmt."""
    # Warte nur 10 Sekunden nach Order bevor wir syncen
    if (time.time() - last_order_t) < 10:
        return
    nado = get_nado_position()
    if nado is None: return

    bot_long  = total_long_size()
    bot_short = total_short_size()

    # LONG Sync
    if grid_mode == "LONG":
        nado_size = max(0.0, nado)
        if abs(nado_size - bot_long) > 0.0001:
            log(f"Sync LONG: Bot={bot_long:.4f} | Nado={nado_size:.4f}", Y)
            if nado_size == 0:
                for lv in grid: lv["filled"] = False; lv["open_time"] = 0.0
            elif nado_size < bot_long:
                diff = max(1, round((bot_long - nado_size) / ORDER_SIZE))
                count = 0
                for lv in reversed(grid):
                    if lv["filled"] and count < diff:
                        lv["filled"] = False; lv["open_time"] = 0.0; count += 1

    # SHORT Sync
    elif grid_mode == "SHORT":
        nado_size = max(0.0, -nado)  # SHORT ist negativ
        if abs(nado_size - bot_short) > 0.0001:
            log(f"Sync SHORT: Bot={bot_short:.4f} | Nado={nado_size:.4f}", Y)
            if nado_size == 0:
                for lv in grid: lv["filled"] = False; lv["open_time"] = 0.0
            elif nado_size < bot_short:
                diff = max(1, round((bot_short - nado_size) / ORDER_SIZE))
                count = 0
                for lv in reversed(grid):
                    if lv["filled"] and count < diff:
                        lv["filled"] = False; lv["open_time"] = 0.0; count += 1


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global prev_preis, just_acted, wins, total_pnl, grid_mode, grid

    tick = 0
    log(f"Bot | LONG+SHORT Grid | 7 Indikatoren | {'DRY' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick      += 1
            just_acted = False

            preis = get_preis()
            if not preis:
                log("Kein Preis — warte...", Y)
                time.sleep(INTERVAL)
                continue

            # Kerzen holen (für Indikatoren)
            candles = get_kerzen(100)
            if not candles:
                log("Keine Kerzen — warte...", Y)
                time.sleep(INTERVAL)
                continue

            # Indikatoren berechnen
            long_c, short_c, details = get_signal(candles)

            # Sync alle 4 Ticks
            if tick % 4 == 0 and grid_mode:
                sync_nado(preis)

            # ── KEIN AKTIVES GRID ─────────────────────────
            if grid_mode is None:
                if long_c >= MIN_SIGNAL and long_c > short_c:
                    log(f"🎯 {long_c}/7 LONG Signal — LONG Grid starten", G)
                    build_grid(preis, "LONG")
                elif short_c >= MIN_SIGNAL and short_c > long_c:
                    log(f"🎯 {short_c}/7 SHORT Signal — SHORT Grid starten", R)
                    build_grid(preis, "SHORT")
                else:
                    if tick % 2 == 0:
                        log(f"BTC {fmt(preis)} | L:{long_c}/7 S:{short_c}/7 RSI:{details.get('RSI','?')} | Warte auf Signal...", Y)
                time.sleep(INTERVAL)
                prev_preis = preis
                continue

            # ── AKTIVES GRID ──────────────────────────────

            # Richtungswechsel: alle 7 andere Richtung → aber nur wenn KEINE offenen Positionen
            if filled_count() == 0:
                if grid_mode == "LONG" and short_c == 7:
                    log("🔄 Alle 7 SHORT + keine offenen Positionen → SHORT Grid starten", M)
                    build_grid(preis, "SHORT")
                    time.sleep(INTERVAL)
                    prev_preis = preis
                    continue
                elif grid_mode == "SHORT" and long_c == 7:
                    log("🔄 Alle 7 LONG + keine offenen Positionen → LONG Grid starten", M)
                    build_grid(preis, "LONG")
                    time.sleep(INTERVAL)
                    prev_preis = preis
                    continue

            # SL prüfen
            if filled_count() == GRID_LEVELS:
                if grid_mode == "LONG":
                    sl_p = grid[-1]["entry_price"] * (1 - SL_PCT / 100)
                    if preis <= sl_p:
                        close_all(preis, "STOP LOSS")
                        time.sleep(INTERVAL)
                        prev_preis = preis
                        continue
                else:  # SHORT
                    sl_p = grid[-1]["entry_price"] * (1 + SL_PCT / 100)
                    if preis >= sl_p:
                        close_all(preis, "STOP LOSS")
                        time.sleep(INTERVAL)
                        prev_preis = preis
                        continue

            # Grid neu aufbauen NUR wenn Preis über alle LONG Levels gestiegen
            # oder unter alle SHORT Levels gefallen ist — dann Signal-Check (4/7)
            if grid and filled_count() == 0:
                if grid_mode == "LONG":
                    highest = max(lv["entry_price"] for lv in grid)
                    if preis > highest * 1.002:
                        if long_c >= MIN_SIGNAL:
                            log(f"Preis über Grid — LONG Grid neu @ {fmt(preis)} ({long_c}/7)", Y)
                            build_grid(preis, "LONG")
                            time.sleep(INTERVAL)
                            prev_preis = preis
                            continue
                        else:
                            log(f"Preis über Grid — warte auf Signal ({long_c}/7 L {short_c}/7 S)", Y)
                elif grid_mode == "SHORT":
                    lowest = min(lv["entry_price"] for lv in grid)
                    if preis < lowest * 0.998:
                        if short_c >= MIN_SIGNAL:
                            log(f"Preis unter Grid — SHORT Grid neu @ {fmt(preis)} ({short_c}/7)", Y)
                            build_grid(preis, "SHORT")
                            time.sleep(INTERVAL)
                            prev_preis = preis
                            continue
                        else:
                            log(f"Preis unter Grid — warte auf Signal ({long_c}/7 L {short_c}/7 S)", Y)

            rising  = prev_preis is not None and preis > prev_preis
            falling = prev_preis is not None and preis < prev_preis

            # ── LONG GRID ─────────────────────────────────
            if grid_mode == "LONG":
                # BUY: preis fällt auf entry level
                if falling:
                    for lv in grid:
                        if not lv["filled"] and preis <= lv["entry_price"] * 1.001:
                            log(f"🟢 LONG BUY @ {fmt(lv['entry_price'])} | TP: {fmt(lv['exit_price'])}", G)
                            ok = place_order(True, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]   = True
                                lv["open_time"] = time.time()
                                just_acted     = True
                            elif ok == "NO_MARGIN":
                                lv["filled"]   = True
                                lv["open_time"] = -1
                            break

                # SELL: preis steigt auf exit level
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"]: continue
                        if lv["open_time"] < 0: continue  # NO_MARGIN Level
                        if (time.time() - lv["open_time"]) < 60: continue
                        if preis >= lv["exit_price"]:
                            log(f"🔴 LONG SELL @ {fmt(lv['exit_price'])} | Einstieg: {fmt(lv['entry_price'])}", R)
                            ok = place_order(False, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]   = False
                                lv["open_time"] = 0.0
                                total_pnl     += GRID_PROFIT
                                wins          += 1
                                just_acted    = True
                                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                            break

            # ── SHORT GRID ────────────────────────────────
            elif grid_mode == "SHORT":
                # SHORT: preis steigt auf entry level
                if rising:
                    for lv in grid:
                        if not lv["filled"] and preis >= lv["entry_price"] * 0.999:
                            log(f"🔴 SHORT @ {fmt(lv['entry_price'])} | TP: {fmt(lv['exit_price'])}", R)
                            ok = place_order(False, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]   = True
                                lv["open_time"] = time.time()
                                just_acted     = True
                            elif ok == "NO_MARGIN":
                                lv["filled"]   = True
                                lv["open_time"] = -1
                            break

                # SHORT schließen: preis fällt auf exit level
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"]: continue
                        if lv["open_time"] < 0: continue
                        if (time.time() - lv["open_time"]) < 60: continue
                        if preis <= lv["exit_price"]:
                            log(f"🟢 SHORT CLOSE @ {fmt(lv['exit_price'])} | Einstieg: {fmt(lv['entry_price'])}", G)
                            ok = place_order(True, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]   = False
                                lv["open_time"] = 0.0
                                total_pnl     += GRID_PROFIT
                                wins          += 1
                                just_acted    = True
                                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                            break

            prev_preis = preis

            # Status
            if tick % 2 == 0:
                n = filled_count()
                modus_txt = f"{G}LONG{X}" if grid_mode == "LONG" else f"{R}SHORT{X}" if grid_mode == "SHORT" else "KEIN"
                log(f"BTC {fmt(preis)} | {modus_txt} | Offen:{n}/{GRID_LEVELS} | L:{long_c}/7 S:{short_c}/7 | {wins}W P&L:{total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if grid_mode and filled_count() > 0:
                log(f"⚠️ {filled_count()} offene {grid_mode} Positionen — manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R)
            time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   Nado.xyz — Long + Short Grid Bot       ║")
    print(f"  ║   7 Indikatoren | SL | Auto-Richtung     ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:    {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:      {GRID_STEP}% | Levels: {GRID_LEVELS} | Profit: +{GRID_PROFIT}%")
    print(f"  Start:     {MIN_SIGNAL}/7 Indikatoren gleiche Richtung")
    print(f"  Wechsel:   7/7 Indikatoren andere Richtung")
    print(f"  SL:        {SL_PCT}% außerhalb letztem Level")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:     {modus}\n")
    loop()


if __name__ == "__main__":
    main()
