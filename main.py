"""
Nado.xyz Grid Trading Bot
===========================
LONG: kaufe wenn fällt, verkaufe wenn steigt
SHORT: shorte wenn steigt, schließe wenn fällt

Start:   4/7 Indikatoren → Grid öffnen
Wechsel: 7/7 andere Richtung + keine offenen Positionen → Grid wechseln
SL:      Preis 0.5% gegen Einstieg des ersten Levels → SOFORT alles schließen
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

ORDER_SIZE  = 0.001   # BTC pro Level
GRID_LEVELS = 5       # Anzahl Levels
GRID_STEP   = 0.2     # % Abstand zwischen Levels
GRID_PROFIT = 0.2     # % Gewinn pro Level
SL_PCT      = 0.5     # % gegen erstes Level → SL auslösen (früh genug)
MIN_SIGNAL  = 4       # Min Indikatoren für Start
INTERVAL    = 30      # Sek pro Tick
DRY_RUN     = False
# ═══════════════════════════════════════════════════════════

grid_mode    = None   # "LONG" oder "SHORT"
grid         = []     # [{entry_price, exit_price, filled, open_time}]
wins         = 0
total_pnl    = 0.0
prev_preis   = None
just_acted   = False
last_order_t = 0.0


def ts():    return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"

def real_filled():
    """Nur echte Levels — keine NO_MARGIN Levels."""
    return sum(1 for lv in grid if lv["filled"] and lv["open_time"] > 0)

def total_size():
    return round(real_filled() * ORDER_SIZE, 4)


# ─── API ──────────────────────────────────────────────────

def get_preis():
    try:
        r = requests.get(f"{GATEWAY}/query?type=all_products",
                         headers={"Accept-Encoding":"gzip"}, timeout=15, verify=False)
        for p in r.json().get("data", r.json()).get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e: log(f"Preis Fehler: {e}", Y)
    return None


def get_kerzen(limit=100):
    try:
        r = requests.post(ARCHIVE,
            json={"candlesticks": {"product_id": PRODUCT_ID, "granularity": 300, "limit": limit}},
            headers=HEADERS, timeout=15, verify=False)
        cs = r.json().get("candlesticks", [])
        if not cs: return None
        candles = [{"o": float(c.get("open_x18",0))/1e18, "h": float(c.get("high_x18",0))/1e18,
                    "l": float(c.get("low_x18",0))/1e18,  "c": float(c.get("close_x18",0))/1e18,
                    "v": float(c.get("volume",0))/1e18} for c in cs]
        return list(reversed(candles))
    except Exception as e: log(f"Kerzen Fehler: {e}", Y)
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
    k = 2 / (n + 1); e = sum(closes[:n]) / n
    for x in closes[n:]: e = x * k + e * (1 - k)
    return e

def calc_rsi(closes, n=14):
    if len(closes) < n + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n; al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag*(n-1)+gains[i])/n; al = (al*(n-1)+losses[i])/n
    return 100 if al == 0 else 100 - (100/(1+ag/al))

def calc_macd(closes):
    if len(closes) < 26: return None
    vals = []
    for i in range(26, len(closes)+1):
        e12 = calc_ema(closes[:i], 12); e26 = calc_ema(closes[:i], 26)
        if e12 and e26: vals.append(e12 - e26)
    if len(vals) < 9: return None
    sig = calc_ema(vals, 9)
    return vals[-1] - sig if sig else None

def calc_bb(closes, n=20):
    if len(closes) < n: return None
    sma = sum(closes[-n:]) / n
    return closes[-1] - sma

def calc_vwap(candles):
    if not candles: return None
    tvp = sum(c["v"]*(c["h"]+c["l"]+c["c"])/3 for c in candles)
    tv  = sum(c["v"] for c in candles)
    return candles[-1]["c"] - tvp/tv if tv > 0 else None

def calc_stoch_rsi(closes, n=14):
    if len(closes) < n*2: return None
    rsi_vals = [r for r in [calc_rsi(closes[:i], n) for i in range(n, len(closes)+1)] if r]
    if len(rsi_vals) < n: return None
    recent = rsi_vals[-n:]; lo, hi = min(recent), max(recent)
    if hi == lo: return 0
    return (rsi_vals[-1] - lo) / (hi - lo) - 0.5

def calc_obv(candles):
    if len(candles) < 2: return None
    obv = obv_p = 0.0
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i-1]["c"]: obv += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]: obv -= candles[i]["v"]
    for i in range(1, len(candles)-1):
        if candles[i]["c"] > candles[i-1]["c"]: obv_p += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]: obv_p -= candles[i]["v"]
    return obv - obv_p

def get_signal(candles):
    if not candles or len(candles) < 30: return 0, 0, {}
    closes = [c["c"] for c in candles]
    rsi   = calc_rsi(closes)
    macd  = calc_macd(closes)
    ema   = (calc_ema(closes,9) or 0) - (calc_ema(closes,21) or 0)
    bb    = calc_bb(closes)
    vwap  = calc_vwap(candles)
    stoch = calc_stoch_rsi(closes)
    obv   = calc_obv(candles)
    if any(v is None for v in [rsi, macd, bb, vwap, stoch, obv]): return 0, 0, {}
    lc = sum(1 for v in [rsi>50, macd>0, ema>0, bb>0, vwap>0, stoch>0, obv>0] if v)
    sc = 7 - lc
    return lc, sc, {"RSI": round(rsi, 1)}


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x", ""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def place_order(is_buy, price, size, sl_order=False):
    """
    sl_order=True → höhere Slippage damit SL immer gefüllt wird
    is_buy=True  → LONG öffnen oder SHORT schließen
    is_buy=False → SHORT öffnen oder LONG schließen
    """
    global last_order_t
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
        last_order_t = time.time()
        return True
    try:
        from eth_account import Account
        # SL Order: 0.5% Slippage damit sicher gefüllt
        # Normal: 0.1% Slippage nahe am Markt
        slip = 0.005 if sl_order else 0.001
        px   = round(price * (1 + slip if is_buy else 1 - slip)) * int(1e18)
        amt  = int(size * 1e18) if is_buy else -int(size * 1e18)
        exp  = int(time.time()) + 60
        nonce = ((int(time.time()*1000) + 5000) << 20) + random.randint(0, 99999)
        apx  = 1
        sndr = sender_hex()
        dom  = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,
                "verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ  = {"Order":[
            {"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
            {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
            {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg  = {"sender":sndr,"priceX18":px,"amount":amt,
                "expiration":exp,"nonce":nonce,"appendix":apx}
        acc  = Account.from_key(SIGNER_KEY)
        sig  = acc.sign_typed_data(domain_data=dom,message_types=typ,message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x" + sig
        pld  = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":str(apx)
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G)
            last_order_t = time.time()
            return True
        code = d.get("error_code", 0)
        if code == 2006:
            log("⚠️ Kein Kapital (2006) — Level überspringen", Y)
            return "NO_MARGIN"
        log(f"❌ {d.get('error','')} (Code:{code})", R)
        return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False


# ─── GRID ─────────────────────────────────────────────────

def build_grid(preis, modus):
    global grid, grid_mode
    grid_mode = modus
    grid = []
    for i in range(1, GRID_LEVELS + 1):
        if modus == "LONG":
            ep = round(preis * (1 - i * GRID_STEP / 100))
            xp = round(ep * (1 + GRID_PROFIT / 100))
        else:
            ep = round(preis * (1 + i * GRID_STEP / 100))
            xp = round(ep * (1 - GRID_PROFIT / 100))
        grid.append({"entry_price":ep, "exit_price":xp, "filled":False, "open_time":0.0})
    lvls = " | ".join(fmt(lv["entry_price"]) for lv in grid)
    log(f"{G if modus=='LONG' else R}{modus} Grid @ {fmt(preis)} | Levels: {lvls}{X}", C)
    sl_p = grid[0]["entry_price"] * (1 - SL_PCT/100) if modus=="LONG" else grid[0]["entry_price"] * (1 + SL_PCT/100)
    log(f"SL @ {fmt(sl_p)} ({SL_PCT}% gegen Level 1)", Y)
    # Soforteinstieg
    is_buy = (modus == "LONG")
    ok = place_order(is_buy, preis, ORDER_SIZE)
    if ok is True:
        xp = round(preis * (1+GRID_PROFIT/100)) if modus=="LONG" else round(preis * (1-GRID_PROFIT/100))
        grid.insert(0, {"entry_price":round(preis), "exit_price":xp,
                        "filled":True, "open_time":time.time()})
        log(f"{'🟢' if modus=='LONG' else '🔴'} Soforteinstieg @ {fmt(preis)}", G if modus=="LONG" else R)


def close_all(preis, reason=""):
    """SL: schließt alle Positionen. Versucht Bulk dann Level für Level."""
    global grid, grid_mode
    n = real_filled()
    if n == 0:
        grid = []; grid_mode = None; return
    size = round(n * ORDER_SIZE, 4)
    log(f"⛔ {reason} — Schließe {n} Levels ({size} BTC @ {fmt(preis)})", R)
    is_buy = (grid_mode == "SHORT")
    # Versuch 1: Alle auf einmal mit hoher Slippage
    ok = place_order(is_buy, preis, size, sl_order=True)
    if ok is True or DRY_RUN:
        grid = []; grid_mode = None
        log("✅ Alle Positionen geschlossen", G)
        return
    # Versuch 2: Level für Level
    log("Schließe Level für Level...", Y)
    for lv in grid:
        if lv["filled"] and lv["open_time"] > 0:
            ok2 = place_order(is_buy, preis, ORDER_SIZE, sl_order=True)
            if ok2 is True:
                lv["filled"] = False; lv["open_time"] = 0.0
            time.sleep(2)
    if real_filled() == 0:
        grid = []; grid_mode = None
        log("✅ Alle Positionen geschlossen", G)
    else:
        # Sync mit Nado
        log("Sync nach SL...", Y)
        nado = get_nado_position()
        if nado is not None and abs(nado) < 0.0001:
            grid = []; grid_mode = None
            log("Nado bestätigt: alles geschlossen", G)


def sync_nado():
    """Sync Bot-State mit Nado. Nur wenn 45 Sek nach letzter Order."""
    if (time.time() - last_order_t) < 45: return
    nado = get_nado_position()
    if nado is None: return
    if grid_mode == "LONG":
        nado_size = max(0.0, nado)
        bot_size  = total_size()
        if abs(nado_size - bot_size) > 0.0001:
            log(f"Sync LONG: Bot={bot_size:.4f} | Nado={nado_size:.4f}", Y)
            if nado_size == 0:
                for lv in grid: lv["filled"]=False; lv["open_time"]=0.0
    elif grid_mode == "SHORT":
        nado_size = max(0.0, -nado)
        bot_size  = total_size()
        if abs(nado_size - bot_size) > 0.0001:
            log(f"Sync SHORT: Bot={bot_size:.4f} | Nado={nado_size:.4f}", Y)
            if nado_size == 0:
                for lv in grid: lv["filled"]=False; lv["open_time"]=0.0


# ─── LOOP ─────────────────────────────────────────────────

def loop():
    global prev_preis, just_acted, wins, total_pnl, grid_mode, grid
    tick = 0
    log(f"Bot | LONG+SHORT Grid | 7 Indikatoren | {'DRY' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1; just_acted = False
            preis = get_preis()
            if not preis:
                log("Kein Preis...", Y); time.sleep(INTERVAL); continue
            candles = get_kerzen(100)
            if not candles:
                log("Keine Kerzen...", Y); time.sleep(INTERVAL); continue
            lc, sc, det = get_signal(candles)
            if tick % 4 == 0 and grid_mode: sync_nado()

            # ── KEIN AKTIVES GRID ─────────────────────────
            if grid_mode is None:
                if lc >= MIN_SIGNAL and lc > sc:
                    log(f"🎯 {lc}/7 LONG — Grid starten", G)
                    build_grid(preis, "LONG")
                elif sc >= MIN_SIGNAL and sc > lc:
                    log(f"🎯 {sc}/7 SHORT — Grid starten", R)
                    build_grid(preis, "SHORT")
                else:
                    if tick % 2 == 0:
                        log(f"BTC {fmt(preis)} | L:{lc}/7 S:{sc}/7 RSI:{det.get('RSI','?')} | Warte...", Y)
                time.sleep(INTERVAL); prev_preis = preis; continue

            # ── RICHTUNGSWECHSEL ──────────────────────────
            if real_filled() == 0:
                if grid_mode == "LONG" and sc == 7:
                    log("🔄 7/7 SHORT — wechsle zu SHORT Grid", M)
                    grid = []; grid_mode = None
                    build_grid(preis, "SHORT")
                    time.sleep(INTERVAL); prev_preis = preis; continue
                elif grid_mode == "SHORT" and lc == 7:
                    log("🔄 7/7 LONG — wechsle zu LONG Grid", M)
                    grid = []; grid_mode = None
                    build_grid(preis, "LONG")
                    time.sleep(INTERVAL); prev_preis = preis; continue

            # ── STOP LOSS ─────────────────────────────────
            # SL basiert auf erstem Level (nicht letztem) — greift früh genug
            if real_filled() > 0 and grid:
                # Finde erstes gefülltes Level
                erste = next((lv for lv in grid if lv["filled"] and lv["open_time"] > 0), None)
                if erste:
                    if grid_mode == "LONG":
                        sl_p = erste["entry_price"] * (1 - SL_PCT / 100)
                        if preis <= sl_p:
                            close_all(preis, "STOP LOSS")
                            time.sleep(INTERVAL); prev_preis = preis; continue
                    else:
                        sl_p = erste["entry_price"] * (1 + SL_PCT / 100)
                        if preis >= sl_p:
                            close_all(preis, "STOP LOSS")
                            time.sleep(INTERVAL); prev_preis = preis; continue

            # ── GRID NEU wenn Preis außerhalb ─────────────
            if real_filled() == 0 and grid:
                if grid_mode == "LONG":
                    highest = max(lv["entry_price"] for lv in grid)
                    if preis > highest * 1.002 and lc >= MIN_SIGNAL:
                        log(f"Preis über Grid — neu @ {fmt(preis)}", Y)
                        grid = []; build_grid(preis, "LONG")
                        time.sleep(INTERVAL); prev_preis = preis; continue
                elif grid_mode == "SHORT":
                    lowest = min(lv["entry_price"] for lv in grid)
                    if preis < lowest * 0.998 and sc >= MIN_SIGNAL:
                        log(f"Preis unter Grid — neu @ {fmt(preis)}", Y)
                        grid = []; build_grid(preis, "SHORT")
                        time.sleep(INTERVAL); prev_preis = preis; continue

            rising  = prev_preis is not None and preis > prev_preis
            falling = prev_preis is not None and preis < prev_preis

            # ── LONG GRID ─────────────────────────────────
            if grid_mode == "LONG":
                if falling:
                    for lv in grid:
                        if not lv["filled"] and preis <= lv["entry_price"] * 1.001:
                            log(f"🟢 BUY @ {fmt(lv['entry_price'])} TP:{fmt(lv['exit_price'])}", G)
                            ok = place_order(True, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]=True; lv["open_time"]=time.time(); just_acted=True
                            elif ok == "NO_MARGIN":
                                lv["filled"]=True; lv["open_time"]=-1
                            break
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"] or lv["open_time"] <= 0: continue
                        if (time.time()-lv["open_time"]) < 60: continue
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
                if rising:
                    for lv in grid:
                        if not lv["filled"] and preis >= lv["entry_price"] * 0.999:
                            log(f"🔴 SHORT @ {fmt(lv['entry_price'])} TP:{fmt(lv['exit_price'])}", R)
                            ok = place_order(False, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"]=True; lv["open_time"]=time.time(); just_acted=True
                            elif ok == "NO_MARGIN":
                                lv["filled"]=True; lv["open_time"]=-1
                            break
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"] or lv["open_time"] <= 0: continue
                        if (time.time()-lv["open_time"]) < 60: continue
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
                n = real_filled()
                mt = f"{G}LONG{X}" if grid_mode=="LONG" else f"{R}SHORT{X}" if grid_mode=="SHORT" else "KEIN"
                log(f"BTC {fmt(preis)} | {mt} | Offen:{n}/{GRID_LEVELS} | L:{lc}/7 S:{sc}/7 | {wins}W P&L:{total_pnl:+.2f}%")
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if grid_mode and real_filled() > 0:
                log(f"⚠️ {real_filled()} offene {grid_mode} Positionen — manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   Nado.xyz — Long + Short Grid Bot       ║")
    print(f"  ║   7 Indikatoren | SL | Auto-Richtung     ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:  {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:    {GRID_STEP}% | Levels: {GRID_LEVELS} | Profit: +{GRID_PROFIT}%")
    print(f"  SL:      {SL_PCT}% gegen erstes Level (früh, sicher)")
    print(f"  Start:   {MIN_SIGNAL}/7 Indikatoren")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:   {modus}\n")
    loop()

if __name__ == "__main__":
    main()
