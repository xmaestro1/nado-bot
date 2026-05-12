"""
Nado.xyz Grid Trading Bot — Long + Short
==========================================
LONG Grid: preis fällt → kaufen, preis steigt → verkaufen
SHORT Grid: preis steigt → shorten, preis fällt → zurückkaufen

Start:     5/7 Indikatoren gleiche Richtung → Grid starten
Schließen: 7/7 Indikatoren andere Richtung + Verlust → alles schließen
SL:        0.3% gegen Einstiegspreis (ab erstem Level)
Trailing:  0.35% hinter laufendem Preis — zieht immer nach

7 Indikatoren: EMA 9/21, ATR, OBV, VWAP, Supertrend, ADX, Parabolic SAR

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
WALLET_ADDR  = "0x14A26C3F3fF2C7A5bC4a1E5E5B15972628288ab7"
SIGNER_KEY   = "0xf7bbe14bbca148872730e063ca969d37f2d7006b22e4486877adaef9206192f9"
SUBACCOUNT   = "0x14a26c3f3ff2c7a5bc4a1e5e5b15972628288ab764656661756c740000000000"

PRODUCT_ID   = 2
CHAIN_ID     = 57073
GATEWAY      = "https://gateway.prod.nado.xyz/v1"
ARCHIVE      = "https://archive.prod.nado.xyz/v1"
HEADERS      = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0015  # BTC pro Level
GRID_LEVELS  = 5       # Anzahl Levels
GRID_STEP    = 0.1     # % Abstand zwischen Levels
GRID_PROFIT  = 0.30     # % Gewinn pro Level
SL_PCT       = 0.4     # % gegen Einstieg → SL
TRAIL_PCT    = 0.2    # % Trailing SL hinter laufendem Preis
MIN_SIGNAL   = 5       # Min 5/7 für Trade öffnen
SYNC_WAIT    = 180     # Sek nach Order kein Sync
INTERVAL     = 30      # Sek pro Tick
DRY_RUN      = False
# ═══════════════════════════════════════════════════════════

# State
grid_mode      = None
grid           = []
wins           = 0
total_pnl      = 0.0
prev_preis     = None
last_order_t   = 0.0
just_acted     = False
trail_sl       = None   # Aktueller Trailing SL Preis
trail_best     = None   # Bester Preis seit Trade-Öffnung
entry_preis    = None   # Einstiegspreis für SL Berechnung


def ts():    return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try:    return f"${float(x):,.2f}"
    except: return "?"

def real_filled_count():
    return sum(1 for lv in grid if lv["filled"] and lv["open_time"] > 0)

def total_long_size():
    return round(real_filled_count() * ORDER_SIZE, 4) if grid_mode == "LONG" else 0.0

def total_short_size():
    return round(real_filled_count() * ORDER_SIZE, 4) if grid_mode == "SHORT" else 0.0


# ─── API ──────────────────────────────────────────────────

def get_preis():
    try:
        r = requests.get(f"{GATEWAY}/query?type=all_products",
                         headers={"Accept-Encoding":"gzip"}, timeout=15, verify=False)
        data = r.json().get("data", r.json())
        for p in data.get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e: log(f"Preis Fehler: {e}", Y)
    return None


def get_kerzen(limit=100):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": limit},
            timeout=15
        )
        data = r.json()
        if not data: return None
        candles = [{
            "o": float(c[1]), "h": float(c[2]),
            "l": float(c[3]), "c": float(c[4]),
            "v": float(c[5])
        } for c in data]
        return candles
    except Exception as e:
        log(f"Kerzen Fehler: {e}", Y)
    return None


def get_nado_position():
    try:
        r = requests.get(f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}",
                         headers={"Accept-Encoding":"gzip"}, timeout=15, verify=False)
        for pb in r.json().get("data", {}).get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                return float(pb["balance"]["amount"]) / 1e18
    except Exception as e: log(f"Nado API Fehler: {e}", Y)
    return None


# ─── INDIKATOREN ──────────────────────────────────────────

def calc_ema(closes, n):
    if len(closes) < n: return None
    k = 2/(n+1); e = sum(closes[:n])/n
    for x in closes[n:]: e = x*k + e*(1-k)
    return e


def calc_atr(candles, n=14):
    """ATR — misst Volatilität und Trendstärke."""
    if len(candles) < n+1: return None
    trs = []
    for i in range(1, len(candles)):
        h=candles[i]["h"]; l=candles[i]["l"]; pc=candles[i-1]["c"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(trs[:n])/n
    for tr in trs[n:]: atr = (atr*(n-1)+tr)/n
    return atr


def calc_supertrend(candles, n=10, mult=3.0):
    """Supertrend — LONG wenn Preis über Linie, SHORT darunter."""
    if len(candles) < n+1: return None
    closes = [c["c"] for c in candles]
    atr = calc_atr(candles, n)
    if not atr: return None
    hl2 = (candles[-1]["h"] + candles[-1]["l"]) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    cur = closes[-1]
    prev = closes[-2] if len(closes) > 1 else cur
    # Preis über lower band = LONG, unter upper band = SHORT
    if cur > lower and prev <= lower: return 1   # LONG Signal
    if cur > lower: return 1                      # LONG
    return -1                                     # SHORT


def calc_adx(candles, n=14):
    """ADX — Trendstärke. >25 = starker Trend."""
    if len(candles) < n*2: return None
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(candles)):
        h=candles[i]["h"]; l=candles[i]["l"]
        ph=candles[i-1]["h"]; pl=candles[i-1]["l"]; pc=candles[i-1]["c"]
        up=h-ph; down=pl-l
        plus_dm.append(up if up>down and up>0 else 0)
        minus_dm.append(down if down>up and down>0 else 0)
        tr_list.append(max(h-l, abs(h-pc), abs(l-pc)))
    def smooth(lst):
        s = sum(lst[:n])
        result = [s]
        for v in lst[n:]: s = s - s/n + v; result.append(s)
        return result
    atr_s = smooth(tr_list)
    pdm_s = smooth(plus_dm)
    mdm_s = smooth(minus_dm)
    if not atr_s or atr_s[-1] == 0: return None
    pdi = 100 * pdm_s[-1] / atr_s[-1]
    mdi = 100 * mdm_s[-1] / atr_s[-1]
    dx  = 100 * abs(pdi-mdi) / (pdi+mdi) if (pdi+mdi) > 0 else 0
    # Gibt Richtung zurück: positiv=LONG (pdi>mdi), negativ=SHORT
    return pdi - mdi


def calc_parabolic_sar(candles, step=0.02, max_af=0.2):
    """Parabolic SAR — Punkte unter Preis = LONG, über Preis = SHORT."""
    if len(candles) < 5: return None
    # Einfache Implementierung
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    bull = closes[1] > closes[0]
    af = step
    sar = lows[0] if bull else highs[0]
    ep  = highs[0] if bull else lows[0]
    for i in range(1, len(candles)):
        if bull:
            if lows[i] < sar:
                bull = False; sar = ep; ep = lows[i]; af = step
            else:
                if highs[i] > ep: ep = highs[i]; af = min(af+step, max_af)
                sar = sar + af*(ep-sar)
                sar = min(sar, lows[i-1], lows[max(0,i-2)])
        else:
            if highs[i] > sar:
                bull = True; sar = ep; ep = highs[i]; af = step
            else:
                if lows[i] < ep: ep = lows[i]; af = min(af+step, max_af)
                sar = sar + af*(ep-sar)
                sar = max(sar, highs[i-1], highs[max(0,i-2)])
    return 1 if bull else -1  # 1=LONG, -1=SHORT


def calc_obv(candles):
    """OBV — Volumen Bestätigung."""
    if len(candles) < 2: return None
    obv = obv_p = 0.0
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i-1]["c"]: obv += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]: obv -= candles[i]["v"]
    for i in range(1, len(candles)-1):
        if candles[i]["c"] > candles[i-1]["c"]: obv_p += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]: obv_p -= candles[i]["v"]
    return obv - obv_p


def calc_vwap(candles):
    """VWAP — fairer Preis."""
    if not candles: return None
    tvp = sum(c["v"]*(c["h"]+c["l"]+c["c"])/3 for c in candles)
    tv  = sum(c["v"] for c in candles)
    if tv == 0: return None
    return candles[-1]["c"] - tvp/tv


def get_signal(candles):
    """7 Indikatoren — gibt long_count, short_count zurück."""
    if not candles or len(candles) < 30: return 0, 0, {}
    closes = [c["c"] for c in candles]

    ema_diff   = (calc_ema(closes, 9) or 0) - (calc_ema(closes, 21) or 0)
    atr        = calc_atr(candles)
    obv        = calc_obv(candles)
    vwap       = calc_vwap(candles)
    supertrend = calc_supertrend(candles)
    adx        = calc_adx(candles)
    psar       = calc_parabolic_sar(candles)

    if any(x is None for x in [atr, obv, vwap, supertrend, adx, psar]):
        return 0, 0, {}

    # ATR Richtung: Preis über EMA50 = LONG
    ema50 = calc_ema(closes, 50)
    atr_dir = 1 if (ema50 and closes[-1] > ema50) else -1

    longs = [
        ema_diff > 0,      # EMA 9/21
        atr_dir > 0,       # ATR + EMA50
        obv > 0,           # OBV
        vwap > 0,          # VWAP
        supertrend == 1,   # Supertrend
        adx > 0,           # ADX Richtung
        psar == 1,         # Parabolic SAR
    ]
    long_c  = sum(1 for v in longs if v)
    short_c = 7 - long_c
    return long_c, short_c, {"ST": supertrend, "ADX": round(adx, 1) if adx else 0}


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x",""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def place_order(is_buy, price, size, sl_order=False):
    global last_order_t
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
        last_order_t = time.time()
        return True
    try:
        from eth_account import Account
        slip = 0.005 if sl_order else 0.002
        px   = round(price * (1+slip if is_buy else 1-slip)) * int(1e18)
        amt  = int(size*1e18) if is_buy else -int(size*1e18)
        exp  = int(time.time()) + 60
        nonce = ((int(time.time()*1000)+5000) << 20) + random.randint(0, 99999)
        sndr = sender_hex()
        dom  = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,
                "verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ  = {"Order":[
            {"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
            {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
            {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg  = {"sender":sndr,"priceX18":px,"amount":amt,
                "expiration":exp,"nonce":nonce,"appendix":1}
        acc  = Account.from_key(SIGNER_KEY)
        sig  = acc.sign_typed_data(domain_data=dom,message_types=typ,message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x"+sig
        pld  = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":"1"
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G); last_order_t = time.time(); return True
        code = d.get("error_code", 0)
        if code == 2006:
            log("⚠️ Kein Kapital (2006) — Level überspringen", Y); return "NO_MARGIN"
        log(f"❌ {d.get('error','')} (Code:{code})", R); return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False


# ─── GRID ─────────────────────────────────────────────────

def build_grid(preis, modus):
    global grid, grid_mode, trail_sl, trail_best, entry_preis
    grid_mode = modus; grid = []
    trail_sl = None; trail_best = None; entry_preis = None

    for i in range(1, GRID_LEVELS+1):
        if modus == "LONG":
            ep = round(preis * (1 - i*GRID_STEP/100))
            xp = round(ep * (1 + GRID_PROFIT/100))
        else:
            ep = round(preis * (1 + i*GRID_STEP/100))
            xp = round(ep * (1 - GRID_PROFIT/100))
        grid.append({"entry_price":ep, "exit_price":xp, "filled":False, "open_time":0.0})

    lvls = " | ".join(fmt(lv["entry_price"]) for lv in grid)
    log(f"{G if modus=='LONG' else R}{modus} Grid @ {fmt(preis)} | {lvls}{X}", C)

    # Soforteinstieg
    is_buy = (modus == "LONG")
    log(f"{'🟢 LONG' if is_buy else '🔴 SHORT'} Soforteinstieg @ {fmt(preis)}", G if is_buy else R)
    ok = place_order(is_buy, preis, ORDER_SIZE)
    if ok is True:
        xp = round(preis*(1+GRID_PROFIT/100)) if modus=="LONG" else round(preis*(1-GRID_PROFIT/100))
        grid[0] = {"entry_price":round(preis),"exit_price":xp,"filled":True,"open_time":time.time()}
        entry_preis = preis
        # Trailing SL sofort setzen
        if modus == "LONG":
            trail_sl   = preis * (1 - TRAIL_PCT/100)
            trail_best = preis
        else:
            trail_sl   = preis * (1 + TRAIL_PCT/100)
            trail_best = preis
        log(f"Trailing SL @ {fmt(trail_sl)}", Y)


def close_all(preis, reason=""):
    global grid, grid_mode, trail_sl, trail_best, entry_preis
    n = real_filled_count()
    if n == 0: grid=[]; grid_mode=None; trail_sl=None; trail_best=None; entry_preis=None; return
    size = round(n * ORDER_SIZE, 4)
    log(f"⛔ {reason} — Schließe {n} Levels ({size} BTC)", R)
    is_buy = (grid_mode == "SHORT")
    ok = place_order(is_buy, preis, size, sl_order=True)
    if ok is True or DRY_RUN:
        grid=[]; grid_mode=None; trail_sl=None; trail_best=None; entry_preis=None
        log("✅ Alle Positionen geschlossen.", Y); return
    # Level für Level Fallback
    log("Schließe Level für Level...", Y)
    for lv in grid:
        if lv["filled"] and lv["open_time"] > 0:
            place_order(is_buy, preis, ORDER_SIZE, sl_order=True)
            lv["filled"]=False; lv["open_time"]=0.0
            time.sleep(2)
    grid=[]; grid_mode=None; trail_sl=None; trail_best=None; entry_preis=None
    log("✅ Alle Positionen geschlossen.", Y)


def update_trailing_sl(preis):
    """Trailing SL aktualisieren wenn Preis sich verbessert."""
    global trail_sl, trail_best
    if trail_sl is None or trail_best is None: return
    if grid_mode == "LONG":
        if preis > trail_best:
            trail_best = preis
            trail_sl   = preis * (1 - TRAIL_PCT/100)
    elif grid_mode == "SHORT":
        if preis < trail_best:
            trail_best = preis
            trail_sl   = preis * (1 + TRAIL_PCT/100)


def sync_nado():
    if (time.time() - last_order_t) < SYNC_WAIT: return
    nado = get_nado_position()
    if nado is None: return
    if grid_mode == "LONG":
        ns = max(0.0, nado); bs = total_long_size()
        if abs(ns-bs) > 0.0001:
            log(f"Sync LONG: Bot={bs:.4f} | Nado={ns:.4f}", Y)
            if ns == 0:
                for lv in grid: lv["filled"]=False; lv["open_time"]=0.0
    elif grid_mode == "SHORT":
        ns = max(0.0, -nado); bs = total_short_size()
        if abs(ns-bs) > 0.0001:
            log(f"Sync SHORT: Bot={bs:.4f} | Nado={ns:.4f}", Y)
            if ns == 0:
                for lv in grid: lv["filled"]=False; lv["open_time"]=0.0


# ─── LOOP ─────────────────────────────────────────────────

def loop():
    global prev_preis, just_acted, wins, total_pnl, grid_mode, grid, trail_sl, trail_best, entry_preis

    tick = 0
    log(f"Bot | LONG+SHORT Grid | 7 Indikatoren | {'DRY' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1; just_acted = False
            preis = get_preis()
            if not preis: log("Kein Preis...", Y); time.sleep(INTERVAL); continue

            candles = get_kerzen(100)
            if not candles: log("Keine Kerzen...", Y); time.sleep(INTERVAL); continue

            long_c, short_c, det = get_signal(candles)

            if tick % 4 == 0 and grid_mode: sync_nado()

            # Trailing SL aktualisieren
            if grid_mode and real_filled_count() > 0:
                update_trailing_sl(preis)

            # ── KEIN GRID ─────────────────────────────────
            if grid_mode is None:
                if long_c >= MIN_SIGNAL and long_c > short_c:
                    log(f"🎯 {long_c}/7 LONG — Grid starten", G)
                    build_grid(preis, "LONG")
                elif short_c >= MIN_SIGNAL and short_c > long_c:
                    log(f"🎯 {short_c}/7 SHORT — Grid starten", R)
                    build_grid(preis, "SHORT")
                else:
                    if tick % 2 == 0:
                        log(f"BTC {fmt(preis)} | L:{long_c}/7 S:{short_c}/7 | Warte 5/7...", Y)
                time.sleep(INTERVAL); prev_preis=preis; continue

            # ── TRAILING SL PRÜFEN ────────────────────────
            if trail_sl and real_filled_count() > 0:
                if grid_mode=="LONG" and preis <= trail_sl:
                    log(f"🔴 Trailing SL getroffen @ {fmt(preis)} (SL:{fmt(trail_sl)})", R)
                    close_all(preis, "TRAILING SL")
                    time.sleep(INTERVAL); prev_preis=preis; continue
                elif grid_mode=="SHORT" and preis >= trail_sl:
                    log(f"🟢 Trailing SL getroffen @ {fmt(preis)} (SL:{fmt(trail_sl)})", G)
                    close_all(preis, "TRAILING SL")
                    time.sleep(INTERVAL); prev_preis=preis; continue

            # ── SL gegen Einstieg ─────────────────────────
            if entry_preis and real_filled_count() > 0:
                if grid_mode=="LONG":
                    sl_p = entry_preis * (1 - SL_PCT/100)
                    if preis <= sl_p:
                        log(f"⛔ SL @ {fmt(sl_p)} (0.3% gegen Einstieg)", R)
                        close_all(preis, "STOP LOSS")
                        time.sleep(INTERVAL); prev_preis=preis; continue
                else:
                    sl_p = entry_preis * (1 + SL_PCT/100)
                    if preis >= sl_p:
                        log(f"⛔ SL @ {fmt(sl_p)} (0.3% gegen Einstieg)", R)
                        close_all(preis, "STOP LOSS")
                        time.sleep(INTERVAL); prev_preis=preis; continue

            # ── 7/7 GEGENSIGNAL + VERLUST → schließen ─────
            if real_filled_count() > 0 and entry_preis:
                if grid_mode=="LONG" and short_c==7 and preis < entry_preis:
                    log("🔄 7/7 SHORT + Verlust → schließen", M)
                    close_all(preis, "7/7 GEGENSIGNAL")
                    time.sleep(INTERVAL); prev_preis=preis; continue
                elif grid_mode=="SHORT" and long_c==7 and preis > entry_preis:
                    log("🔄 7/7 LONG + Verlust → schließen", M)
                    close_all(preis, "7/7 GEGENSIGNAL")
                    time.sleep(INTERVAL); prev_preis=preis; continue

            # ── RICHTUNGSWECHSEL (keine offenen Pos.) ─────
            if real_filled_count() == 0:
                if grid_mode=="LONG" and short_c==7:
                    log("🔄 7/7 SHORT → SHORT Grid", M)
                    grid=[]; grid_mode=None
                    build_grid(preis, "SHORT")
                    time.sleep(INTERVAL); prev_preis=preis; continue
                elif grid_mode=="SHORT" and long_c==7:
                    log("🔄 7/7 LONG → LONG Grid", M)
                    grid=[]; grid_mode=None
                    build_grid(preis, "LONG")
                    time.sleep(INTERVAL); prev_preis=preis; continue

            # ── GRID NEU wenn Preis weg ───────────────────
            if grid and real_filled_count()==0:
                if grid_mode=="LONG":
                    highest = max(lv["entry_price"] for lv in grid)
                    if preis > highest*1.001 and long_c >= MIN_SIGNAL:
                        log(f"Grid neu @ {fmt(preis)}", Y)
                        build_grid(preis,"LONG")
                        time.sleep(INTERVAL); prev_preis=preis; continue
                elif grid_mode=="SHORT":
                    lowest = min(lv["entry_price"] for lv in grid)
                    if preis < lowest*0.999 and short_c >= MIN_SIGNAL:
                        log(f"Grid neu @ {fmt(preis)}", Y)
                        build_grid(preis,"SHORT")
                        time.sleep(INTERVAL); prev_preis=preis; continue

            rising  = prev_preis is not None and preis > prev_preis
            falling = prev_preis is not None and preis < prev_preis

            # ── LONG GRID ─────────────────────────────────
            if grid_mode == "LONG":
                if falling and long_c >= 5:
                    for lv in grid:
                        if not lv["filled"] and preis <= lv["entry_price"]*1.001:
                            log(f"🟢 BUY @ {fmt(lv['entry_price'])} TP:{fmt(lv['exit_price'])}", G)
                            ok = place_order(True, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]=True; lv["open_time"]=time.time(); just_acted=True
                                if not entry_preis: entry_preis=preis
                            elif ok=="NO_MARGIN":
                                lv["filled"]=True; lv["open_time"]=-1
                            break
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"] or lv["open_time"]<=0: continue
                        if (time.time()-lv["open_time"])<60: continue
                        if preis >= lv["exit_price"]:
                            log(f"🔴 SELL @ {fmt(lv['exit_price'])} Einstieg:{fmt(lv['entry_price'])}", R)
                            ok = place_order(False, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]=False; lv["open_time"]=0.0
                                total_pnl+=GRID_PROFIT; wins+=1; just_acted=True
                                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                            break

            # ── SHORT GRID ────────────────────────────────
            elif grid_mode == "SHORT":
                if rising and short_c >= 5:
                    for lv in grid:
                        if not lv["filled"] and preis >= lv["entry_price"]*0.999:
                            log(f"🔴 SHORT @ {fmt(lv['entry_price'])} TP:{fmt(lv['exit_price'])}", R)
                            ok = place_order(False, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]=True; lv["open_time"]=time.time(); just_acted=True
                                if not entry_preis: entry_preis=preis
                            elif ok=="NO_MARGIN":
                                lv["filled"]=True; lv["open_time"]=-1
                            break
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"] or lv["open_time"]<=0: continue
                        if (time.time()-lv["open_time"])<60: continue
                        if preis <= lv["exit_price"]:
                            log(f"🟢 CLOSE @ {fmt(lv['exit_price'])} Einstieg:{fmt(lv['entry_price'])}", G)
                            ok = place_order(True, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]=False; lv["open_time"]=0.0
                                total_pnl+=GRID_PROFIT; wins+=1; just_acted=True
                                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                            break

            prev_preis = preis
            if tick % 2 == 0:
                n = real_filled_count()
                mt = f"{G}LONG{X}" if grid_mode=="LONG" else f"{R}SHORT{X}" if grid_mode=="SHORT" else "KEIN"
                tsl = f"TSL:{fmt(trail_sl)}" if trail_sl else ""
                log(f"BTC {fmt(preis)} | {mt} | Offen:{n}/{GRID_LEVELS} | L:{long_c}/7 S:{short_c}/7 | {wins}W P&L:{total_pnl:+.2f}% {tsl}")
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if grid_mode and real_filled_count()>0:
                log(f"⚠️ {real_filled_count()} offene {grid_mode} Pos. — manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   Nado.xyz — Long + Short Grid Bot       ║")
    print(f"  ║   7 Indikatoren | Trailing SL            ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:    {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:      {GRID_STEP}% | Levels: {GRID_LEVELS} | Profit: +{GRID_PROFIT}%")
    print(f"  Start:     {MIN_SIGNAL}/7 Indikatoren")
    print(f"  SL:        {SL_PCT}% gegen Einstieg")
    print(f"  Trailing:  {TRAIL_PCT}% hinter Preis")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:     {modus}\n")
    loop()

if __name__ == "__main__":
    main()
