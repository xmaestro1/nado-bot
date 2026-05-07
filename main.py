"""
Nado.xyz Grid Trading Bot — Long + Short
==========================================
LONG Grid: preis fällt → kaufen (5 levels), preis steigt → verkaufen (5 levels)
SHORT Grid: preis steigt → shorten (5 levels), preis fällt → zurückkaufen (5 levels)

Start:     4/7 Indikatoren gleiche Richtung → Grid starten
Wechsel:   Alle 7 Indikatoren andere Richtung + keine offenen Positionen → neue Richtung
SL:        Alle echten Levels voll + 1% weiter → alles schließen

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
GRID_LEVELS  = 3       # Anzahl Levels (3 = sicher mit $100)
GRID_STEP    = 0.2     # % Abstand zwischen Levels
GRID_PROFIT  = 0.2     # % Gewinn pro Level
SL_PCT       = 1.0     # % außerhalb letztem Level → SL
MIN_SIGNAL   = 4       # Min Indikatoren für ersten Start
SYNC_WAIT    = 180     # Sek nach Order kein Sync
INTERVAL     = 30      # Sek pro Tick
DRY_RUN      = True
# ═══════════════════════════════════════════════════════════

# State
grid_mode     = None
grid          = []
wins          = 0
total_pnl     = 0.0
prev_preis    = None
last_order_t  = 0.0
just_acted    = False


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

def real_filled_count():
    """Nur echte Positionen — keine NO_MARGIN Level"""
    return sum(1 for lv in grid if lv["filled"] and lv["open_time"] > 0)

def total_long_size():
    if grid_mode == "LONG":
        return round(real_filled_count() * ORDER_SIZE, 4)
    return 0.0

def total_short_size():
    if grid_mode == "SHORT":
        return round(real_filled_count() * ORDER_SIZE, 4)
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
        return list(reversed(candles))
    except Exception as e:
        log(f"Kerzen Fehler: {e}", Y)
    return None


def get_nado_position():
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
    if len(closes) < 26: return None
    e12 = calc_ema(closes, 12)
    e26 = calc_ema(closes, 26)
    if e12 is None or e26 is None: return None
    macd_line = e12 - e26
    macd_vals = []
    for i in range(26, len(closes) + 1):
        e12i = calc_ema(closes[:i], 12)
        e26i = calc_ema(closes[:i], 26)
        if e12i and e26i: macd_vals.append(e12i - e26i)
    if len(macd_vals) < 9: return None
    signal = calc_ema(macd_vals, 9)
    if signal is None: return None
    return macd_line - signal


def calc_bb(closes, n=20):
    if len(closes) < n: return None
    sma = sum(closes[-n:]) / n
    mid = sma
    return closes[-1] - mid


def calc_vwap(candles):
    if not candles: return None
    tvp = sum(c["v"] * (c["h"] + c["l"] + c["c"]) / 3 for c in candles)
    tv  = sum(c["v"] for c in candles)
    if tv == 0: return None
    vwap = tvp / tv
    return candles[-1]["c"] - vwap


def calc_stoch_rsi(closes, n=14):
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
    return stoch - 0.5


def calc_obv(candles):
    if len(candles) < 2: return None
    obv = 0.0
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i-1]["c"]:
            obv += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]:
            obv -= candles[i]["v"]
    obv_prev = 0.0
    for i in range(1, len(candles) - 1):
        if candles[i]["c"] > candles[i-1]["c"]:
            obv_prev += candles[i]["v"]
        elif candles[i]["c"] < candles[i-1]["c"]:
            obv_prev -= candles[i]["v"]
    return obv - obv_prev


def get_signal(candles):
    if not candles or len(candles) < 30:
        return 0, 0, {}
    closes = [c["c"] for c in candles]
    rsi      = calc_rsi(closes)
    macd     = calc_macd(closes)
    ema_diff = (calc_ema(closes, 9) or 0) - (calc_ema(closes, 21) or 0)
    bb       = calc_bb(closes)
    vwap     = calc_vwap(candles)
    stoch    = calc_stoch_rsi(closes)
    obv      = calc_obv(candles)
    if any(x is None for x in [rsi, macd, bb, vwap, stoch, obv]):
        return 0, 0, {}
    long_count  = sum(1 for v in [rsi>50, macd>0, ema_diff>0, bb>0, vwap>0, stoch>0, obv>0] if v)
    short_count = 7 - long_count
    return long_count, short_count, {"RSI": round(rsi, 1)}


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x", ""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def place_order(is_buy, price, size, sl_order=False):
    global last_order_t
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
        last_order_t = time.time()
        return True
    try:
        from eth_account import Account
        # SL: 0.5% Slippage für sichere Füllung / Normal: 0.2%
        slip = 0.005 if sl_order else 0.002
        px   = round(price * (1 + slip if is_buy else 1 - slip)) * int(1e18)
        amt  = int(size * 1e18) if is_buy else -int(size * 1e18)
        exp  = int(time.time()) + 60
        nonce = ((int(time.time() * 1000) + 5000) << 20) + random.randint(0, 99999)
        apx  = 1
        sndr = sender_hex()
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
            return True
        code = d.get("error_code", 0)
        if code == 2006:
            log("⚠️ Kein Kapital (2006) — Level überspringen", Y)
            return "NO_MARGIN"
        log(f"❌ {d.get('error','')} (Code:{code})", R)
        return False
    except Exception as e:
        log(f"Order Exception: {e}", R)
        return False


# ─── GRID AUFBAUEN ────────────────────────────────────────

def build_grid(preis, modus):
    global grid, grid_mode
    grid_mode = modus
    grid = []
    for i in range(1, GRID_LEVELS + 1):
        if modus == "LONG":
            entry_p = round(preis * (1 - i * GRID_STEP / 100))
            exit_p  = round(entry_p * (1 + GRID_PROFIT / 100))
        else:
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
    # Soforteinstieg
    is_buy = (modus == "LONG")
    log(f"{'🟢 LONG' if modus == 'LONG' else '🔴 SHORT'} Soforteinstieg @ {fmt(preis)}", G if modus == "LONG" else R)
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
    """Schließt alle echten Positionen. Erst bulk, dann Level für Level."""
    global grid, grid_mode
    n = real_filled_count()
    if n == 0:
        grid = []
        grid_mode = None
        return
    size = round(n * ORDER_SIZE, 4)
    log(f"⛔ {reason} — Schließe {n} Levels ({size} BTC)", R)
    is_buy = (grid_mode == "SHORT")
    # Versuch 1: Alle auf einmal
    ok = place_order(is_buy, preis, size, sl_order=True)
    if ok is True or DRY_RUN:
        grid = []
        grid_mode = None
        log("✅ Alle Positionen geschlossen.", Y)
        return
    # Versuch 2: Level für Level
    log("Schließe Level für Level...", Y)
    for lv in grid:
        if lv["filled"] and lv["open_time"] > 0:
            place_order(is_buy, preis, ORDER_SIZE, sl_order=True)
            lv["filled"] = False
            lv["open_time"] = 0.0
            time.sleep(2)
    grid = []
    grid_mode = None
    log("✅ Alle Positionen geschlossen.", Y)


# ─── SYNC ─────────────────────────────────────────────────

def sync_nado(preis):
    if (time.time() - last_order_t) < SYNC_WAIT:
        return
    nado = get_nado_position()
    if nado is None: return

    if grid_mode == "LONG":
        nado_size = max(0.0, nado)
        bot_size  = total_long_size()
        if abs(nado_size - bot_size) > 0.0001:
            log(f"Sync LONG: Bot={bot_size:.4f} | Nado={nado_size:.4f}", Y)
            if nado_size == 0:
                for lv in grid: lv["filled"] = False; lv["open_time"] = 0.0
            elif nado_size < bot_size:
                diff = max(1, round((bot_size - nado_size) / ORDER_SIZE))
                count = 0
                for lv in reversed(grid):
                    if lv["filled"] and lv["open_time"] > 0 and count < diff:
                        lv["filled"] = False; lv["open_time"] = 0.0; count += 1

    elif grid_mode == "SHORT":
        nado_size = max(0.0, -nado)
        bot_size  = total_short_size()
        if abs(nado_size - bot_size) > 0.0001:
            log(f"Sync SHORT: Bot={bot_size:.4f} | Nado={nado_size:.4f}", Y)
            if nado_size == 0:
                for lv in grid: lv["filled"] = False; lv["open_time"] = 0.0
            elif nado_size < bot_size:
                diff = max(1, round((bot_size - nado_size) / ORDER_SIZE))
                count = 0
                for lv in reversed(grid):
                    if lv["filled"] and lv["open_time"] > 0 and count < diff:
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

            candles = get_kerzen(100)
            if not candles:
                log("Keine Kerzen — warte...", Y)
                time.sleep(INTERVAL)
                continue

            long_c, short_c, details = get_signal(candles)

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
                        log(f"BTC {fmt(preis)} | L:{long_c}/7 S:{short_c}/7 RSI:{details.get('RSI','?')} | Warte...", Y)
                time.sleep(INTERVAL)
                prev_preis = preis
                continue

            # ── RICHTUNGSWECHSEL (nur wenn nichts offen) ──
            if real_filled_count() == 0:
                if grid_mode == "LONG" and short_c == 7:
                    log("🔄 7/7 SHORT + keine offenen Positionen → SHORT Grid", M)
                    grid = []; grid_mode = None
                    build_grid(preis, "SHORT")
                    time.sleep(INTERVAL); prev_preis = preis; continue
                elif grid_mode == "SHORT" and long_c == 7:
                    log("🔄 7/7 LONG + keine offenen Positionen → LONG Grid", M)
                    grid = []; grid_mode = None
                    build_grid(preis, "LONG")
                    time.sleep(INTERVAL); prev_preis = preis; continue

            # ── STOP LOSS (nur echte Positionen) ──────────
            if real_filled_count() >= GRID_LEVELS:
                if grid_mode == "LONG":
                    sl_p = grid[-1]["entry_price"] * (1 - SL_PCT / 100)
                    if preis <= sl_p:
                        close_all(preis, "STOP LOSS")
                        time.sleep(INTERVAL); prev_preis = preis; continue
                else:
                    sl_p = grid[-1]["entry_price"] * (1 + SL_PCT / 100)
                    if preis >= sl_p:
                        close_all(preis, "STOP LOSS")
                        time.sleep(INTERVAL); prev_preis = preis; continue

            # ── GRID NEU aufbauen ─────────────────────────
            if grid and real_filled_count() == 0:
                if grid_mode == "LONG":
                    highest = max(lv["entry_price"] for lv in grid)
                    if preis > highest * 1.002 and long_c >= MIN_SIGNAL:
                        log(f"Preis über Grid — LONG neu @ {fmt(preis)} ({long_c}/7)", Y)
                        build_grid(preis, "LONG")
                        time.sleep(INTERVAL); prev_preis = preis; continue
                elif grid_mode == "SHORT":
                    lowest = min(lv["entry_price"] for lv in grid)
                    if preis < lowest * 0.998 and short_c >= MIN_SIGNAL:
                        log(f"Preis unter Grid — SHORT neu @ {fmt(preis)} ({short_c}/7)", Y)
                        build_grid(preis, "SHORT")
                        time.sleep(INTERVAL); prev_preis = preis; continue

            rising  = prev_preis is not None and preis > prev_preis
            falling = prev_preis is not None and preis < prev_preis

            # ── LONG GRID ─────────────────────────────────
            if grid_mode == "LONG":
                if falling:
                    for lv in grid:
                        if not lv["filled"] and preis <= lv["entry_price"] * 1.001:
                            log(f"🟢 LONG BUY @ {fmt(lv['entry_price'])} | TP: {fmt(lv['exit_price'])}", G)
                            ok = place_order(True, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"] = True; lv["open_time"] = time.time(); just_acted = True
                            elif ok == "NO_MARGIN":
                                lv["filled"] = True; lv["open_time"] = -1
                            break
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"] or lv["open_time"] <= 0: continue
                        if (time.time() - lv["open_time"]) < 60: continue
                        if preis >= lv["exit_price"]:
                            log(f"🔴 LONG SELL @ {fmt(lv['exit_price'])} | Einstieg: {fmt(lv['entry_price'])}", R)
                            ok = place_order(False, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"] = False; lv["open_time"] = 0.0
                                total_pnl += GRID_PROFIT; wins += 1; just_acted = True
                                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                            break

            # ── SHORT GRID ────────────────────────────────
            elif grid_mode == "SHORT":
                if rising:
                    for lv in grid:
                        if not lv["filled"] and preis >= lv["entry_price"] * 0.999:
                            log(f"🔴 SHORT @ {fmt(lv['entry_price'])} | TP: {fmt(lv['exit_price'])}", R)
                            ok = place_order(False, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"] = True; lv["open_time"] = time.time(); just_acted = True
                            elif ok == "NO_MARGIN":
                                lv["filled"] = True; lv["open_time"] = -1
                            break
                if not just_acted:
                    for lv in grid:
                        if not lv["filled"] or lv["open_time"] <= 0: continue
                        if (time.time() - lv["open_time"]) < 60: continue
                        if preis <= lv["exit_price"]:
                            log(f"🟢 SHORT CLOSE @ {fmt(lv['exit_price'])} | Einstieg: {fmt(lv['entry_price'])}", G)
                            ok = place_order(True, preis, ORDER_SIZE)
                            if ok is True:
                                lv["filled"] = False; lv["open_time"] = 0.0
                                total_pnl += GRID_PROFIT; wins += 1; just_acted = True
                                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                            break

            prev_preis = preis

            if tick % 2 == 0:
                n = real_filled_count()
                modus_txt = f"{G}LONG{X}" if grid_mode == "LONG" else f"{R}SHORT{X}" if grid_mode == "SHORT" else "KEIN"
                log(f"BTC {fmt(preis)} | {modus_txt} | Offen:{n}/{GRID_LEVELS} | L:{long_c}/7 S:{short_c}/7 | {wins}W P&L:{total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if grid_mode and real_filled_count() > 0:
                log(f"⚠️ {real_filled_count()} offene {grid_mode} Positionen — manuell auf app.nado.xyz schließen!", R)
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
