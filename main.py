"""
Nado.xyz Grid Trading Bot — Final
===================================
Strategie:
  - Grid: kaufe günstig, verkaufe teurer
  - SL: alle Positionen schließen wenn Markt stark fällt
  - Wiedereinstieg: RSI > 40 UND EMA9 > EMA21 (5-Min)

Einrichten:
  SIGNER_KEY = 1-Click Trading Key (app.nado.xyz → Settings)
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

ORDER_SIZE  = 0.0015   # BTC pro Level
GRID_LEVELS = 5        # Anzahl Levels
GRID_STEP   = 0.2      # % Abstand zwischen Levels
GRID_PROFIT = 0.2      # % Gewinn pro Level
SL_PCT      = 1.0      # % unter letztem Level → SL auslösen
RSI_ENTRY   = 40       # RSI muss über diesem Wert sein für Wiedereinstieg
SYNC_WAIT   = 180      # Sekunden nach Order kein Sync
INTERVAL    = 30       # Sekunden pro Tick
DRY_RUN     = False
# ═══════════════════════════════════════════════════════════

# Zustände
ZUSTAND_GRID    = "GRID"    # Grid läuft normal
ZUSTAND_WARTEN  = "WARTEN"  # Nach SL, warte auf Signal

grid          = []     # Liste: {buy_price, sell_price, filled, bought_at}
zustand       = ZUSTAND_GRID
wins          = 0
total_pnl     = 0.0
prev_preis    = None
last_order_t  = 0.0    # Zeit der letzten Order (für Sync-Delay)
grid_built_at = 0.0    # Zeit des letzten Grid-Aufbaus
just_bought   = False  # Verhindert Sell im gleichen Tick wie Buy


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

def total_size():
    return round(filled_count() * ORDER_SIZE, 4)


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


def get_kerzen():
    """Holt 5-Min Kerzen vom Archive (älteste zuerst)."""
    try:
        r = requests.post(
            ARCHIVE,
            json={"candlesticks": {"product_id": PRODUCT_ID, "granularity": 300, "limit": 60}},
            headers=HEADERS, timeout=15, verify=False
        )
        cs = r.json().get("candlesticks", [])
        if not cs: return None
        candles = [{"c": float(c.get("close_x18", 0)) / 1e18} for c in cs]
        return list(reversed(candles))  # älteste zuerst
    except Exception as e:
        log(f"Kerzen Fehler: {e}", Y)
    return None


def get_nado_size():
    """Echte BTC Position von Nado."""
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}",
            headers={"Accept-Encoding": "gzip"},
            timeout=15, verify=False
        )
        data = r.json().get("data", {})
        for pb in data.get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                return max(0.0, float(pb["balance"]["amount"]) / 1e18)
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


def signal_wiedereinstieg():
    """
    Prüft ob Wiedereinstieg erlaubt ist.
    Bedingung: RSI > 40 UND EMA9 > EMA21 auf 5-Min Kerzen.
    """
    cs = get_kerzen()
    if not cs or len(cs) < 25: return False
    closes = [c["c"] for c in cs]
    rsi  = calc_rsi(closes)
    e9   = calc_ema(closes, 9)
    e21  = calc_ema(closes, 21)
    if rsi is None or e9 is None or e21 is None: return False
    ok = rsi > RSI_ENTRY and e9 > e21
    log(f"Signal Check: RSI={rsi:.1f} (>{RSI_ENTRY}?) EMA9={fmt(e9)} EMA21={fmt(e21)} ({'✅ JA' if ok else '❌ NEIN'})", M)
    return ok


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x", ""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def _send_order(is_buy, price, size):
    """
    Interne Order-Funktion.
    is_buy=True  → positive Menge → LONG öffnen
    is_buy=False → negative Menge → LONG schließen
    Kein reduce_only. Preis mit 0.2% Slippage.
    """
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
        return True
    try:
        from eth_account import Account
        px    = round(price * (1.002 if is_buy else 0.998)) * int(1e18)
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
            return True
        log(f"❌ {d.get('error','')} (Code:{d.get('error_code','')})", R)
        return False
    except Exception as e:
        log(f"Order Exception: {e}", R)
        return False


def buy(price):
    """Öffnet eine LONG Position (ORDER_SIZE BTC)."""
    global last_order_t
    ok = _send_order(True, price, ORDER_SIZE)
    if ok: last_order_t = time.time()
    return ok


def sell(price, size):
    """Schließt eine LONG Position (size BTC)."""
    global last_order_t
    ok = _send_order(False, price, size)
    if ok: last_order_t = time.time()
    return ok


# ─── GRID ─────────────────────────────────────────────────

def build_grid(preis):
    global grid, grid_built_at
    grid = []
    for i in range(1, GRID_LEVELS + 1):
        buy_p  = round(preis * (1 - i * GRID_STEP / 100))
        sell_p = round(buy_p  * (1 + GRID_PROFIT / 100))
        grid.append({
            "buy_price":  buy_p,
            "sell_price": sell_p,
            "filled":     False,
            "bought_at":  0.0,
        })
    lvls = " | ".join(fmt(lv["buy_price"]) for lv in grid)
    log(f"Grid @ {fmt(preis)} | Levels: {lvls}", C)
    grid_built_at = time.time()
    log(f"SL wenn BTC unter {fmt(grid[-1]['buy_price'] * (1 - SL_PCT/100))}", Y)
    # Sofort kaufen beim Grid-Aufbau
    log(f"🟢 Sofortkauf @ {fmt(preis)}", G)
    ok = buy(preis)
    if ok:
        sell_p = round(preis * (1 + GRID_PROFIT / 100))
        grid.insert(0, {"buy_price": round(preis), "sell_price": sell_p, "filled": True, "bought_at": time.time()})


def sl_auslösen(preis):
    """Schließt alle offenen Positionen mit einer einzigen Order."""
    global zustand, wins, total_pnl
    n = filled_count()
    if n == 0: return
    size = round(n * ORDER_SIZE, 4)
    verlust_pnl = sum(
        (preis - lv["buy_price"]) / lv["buy_price"] * 100
        for lv in grid if lv["filled"]
    ) / n
    log(f"⛔ STOP LOSS — {n} Levels ({size} BTC) | Verlust: {verlust_pnl:+.2f}%", R)
    ok = sell(preis, size)
    if ok or DRY_RUN:
        for lv in grid:
            lv["filled"]   = False
            lv["bought_at"] = 0.0
        zustand = ZUSTAND_WARTEN
        log("Zustand: WARTEN auf Wiedereinstieg-Signal...", Y)


def sync_nado(preis):
    """Sync mit Nado API. Nur wenn keine Order zu kürzlich."""
    if (time.time() - last_order_t) < SYNC_WAIT:
        return
    nado = get_nado_size()
    if nado is None: return
    bot  = total_size()
    if abs(nado - bot) < 0.0001: return
    log(f"Sync: Bot={bot:.4f} | Nado={nado:.4f}", Y)
    if nado == 0 and bot > 0:
        log("Nado: keine Position — Grid zurücksetzen", Y)
        for lv in grid:
            lv["filled"]   = False
            lv["bought_at"] = 0.0
    elif nado < bot:
        # Nado hat weniger — State anpassen
        diff = max(1, round((bot - nado) / ORDER_SIZE))
        count = 0
        for lv in reversed(grid):
            if lv["filled"] and count < diff:
                lv["filled"]   = False
                lv["bought_at"] = 0.0
                count += 1
    # Wenn Nado mehr hat als Bot — ignorieren


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global prev_preis, zustand, just_bought, wins, total_pnl, grid_built_at

    tick = 0
    log(f"Bot | Grid+SL+Signal | DRY={'JA' if DRY_RUN else 'NEIN'}", C)

    # Beim Start Grid aufbauen
    p = get_preis()
    if p:
        build_grid(p)
        nado = get_nado_size()
        if nado and nado > 0:
            log(f"Bestehende Position: {nado:.4f} BTC erkannt", Y)
            n = round(nado / ORDER_SIZE)
            for i, lv in enumerate(grid[:n]):
                lv["filled"]   = True
                lv["bought_at"] = 0.0

    while True:
        try:
            tick       += 1
            just_bought = False

            preis = get_preis()
            if not preis:
                log("Kein Preis — warte...", Y)
                time.sleep(INTERVAL)
                continue

            # Sync alle 4 Ticks
            if tick % 4 == 0:
                sync_nado(preis)

            # ── ZUSTAND: WARTEN ───────────────────────────
            if zustand == ZUSTAND_WARTEN:
                log(f"BTC {fmt(preis)} | Warte auf Signal (RSI>{RSI_ENTRY} + EMA9>EMA21)...", Y)
                if signal_wiedereinstieg():
                    log("🚀 Signal erkannt! Neues Grid wird aufgebaut.", G)
                    build_grid(preis)
                    zustand = ZUSTAND_GRID
                time.sleep(INTERVAL)
                prev_preis = preis
                continue

            # ── ZUSTAND: GRID ─────────────────────────────

            # Grid neu aufbauen nur wenn Preis ÜBER dem höchsten Level ist (BTC stieg über alle Levels)
            # Nur neu aufbauen wenn ALLE levels verkauft wurden (min 1 win) UND preis deutlich über grid
            filled_ever = any(lv["bought_at"] > 0 for lv in grid)
            if filled_count() == 0 and filled_ever and preis > grid[0]["buy_price"] * 1.002:
                log(f"Alle Levels verkauft — neues Grid @ {fmt(preis)}", C)
                build_grid(preis)

            # SL: alle Levels gefüllt UND Preis 1% unter letztem Level
            if filled_count() == GRID_LEVELS:
                sl_preis = grid[-1]["buy_price"] * (1 - SL_PCT / 100)
                if preis <= sl_preis:
                    sl_auslösen(preis)
                    time.sleep(INTERVAL)
                    prev_preis = preis
                    continue

            falling = prev_preis is not None and preis < prev_preis

            # BUY: Preis fällt auf Level
            if falling:
                for lv in grid:
                    if not lv["filled"] and preis <= lv["buy_price"] * 1.001:
                        log(f"🟢 BUY @ {fmt(lv['buy_price'])} | TP: {fmt(lv['sell_price'])}", G)
                        ok = buy(preis)
                        if ok:
                            lv["filled"]   = True
                            lv["bought_at"] = time.time()
                            just_bought    = True
                        break  # 1 Buy pro Tick

            # SELL: Preis steigt auf Sell Level (nicht im gleichen Tick wie Buy)
            if not just_bought:
                for lv in grid:
                    if not lv["filled"]: continue
                    if (time.time() - lv["bought_at"]) < 60: continue  # 60s warten
                    if preis >= lv["sell_price"]:
                        log(f"🔴 SELL @ {fmt(lv['sell_price'])} | Gekauft @ {fmt(lv['buy_price'])}", R)
                        ok = sell(preis, ORDER_SIZE)
                        if ok:
                            lv["filled"]   = False
                            lv["bought_at"] = 0.0
                            total_pnl     += GRID_PROFIT
                            wins          += 1
                            log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                        break  # 1 Sell pro Tick

            prev_preis = preis

            # Status anzeigen
            if tick % 2 == 0:
                n = filled_count()
                sl_info = ""
                if n == GRID_LEVELS:
                    sl_p = grid[-1]["buy_price"] * (1 - SL_PCT / 100)
                    sl_info = f" | SL @ {fmt(sl_p)}"
                log(f"BTC {fmt(preis)} | Offen:{n}/{GRID_LEVELS} | {wins}W | P&L:{total_pnl:+.2f}%{sl_info}")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if total_size() > 0:
                log(f"⚠️ {total_size():.4f} BTC offen — manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R)
            time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║      Nado.xyz — Grid + SL + Signal Bot   ║")
    print(f"  ║   Kaufe günstig, verkaufe teurer         ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:    {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:      {GRID_STEP}% | Levels: {GRID_LEVELS} | Profit: +{GRID_PROFIT}%")
    print(f"  SL:        {SL_PCT}% unter letztem Level")
    print(f"  Einstieg:  RSI > {RSI_ENTRY} + EMA9 > EMA21")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:     {modus}\n")
    loop()


if __name__ == "__main__":
    main()
