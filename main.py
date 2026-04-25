"""
Nado.xyz Grid Trading Bot
==========================
Kaufe günstig, verkaufe teurer.
Einrichten: SIGNER_KEY = 1-Click Trading Key (app.nado.xyz → Settings)
"""

import time, random, requests, sys, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN
    X=Style.RESET_ALL; B=Style.BRIGHT
except:
    G=R=Y=C=X=B=""

# ═══════════════════════════════════════════════════════════
WALLET_ADDR = "0x14A26C3F3fF2C7A5bC4a1E5E5B15972628288ab7"
SIGNER_KEY  = "0xf7bbe14bbca148872730e063ca969d37f2d7006b22e4486877adaef9206192f9"
SUBACCOUNT  = "0x14a26c3f3ff2c7a5bc4a1e5e5b15972628288ab764656661756c740000000000"

PRODUCT_ID  = 2
CHAIN_ID    = 57073
GATEWAY     = "https://gateway.prod.nado.xyz/v1"
HEADERS     = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE  = 0.0035  # BTC pro Level
GRID_LEVELS = 5       # Anzahl Levels
GRID_STEP   = 0.4     # % Abstand zwischen Levels
GRID_PROFIT = 0.4     # % Gewinn pro Level
BUY_TOL     = 0.004   # 0.4% Toleranz für Buy Level
SELL_TOL    = 0.002   # 0.2% Toleranz für Sell Level
INTERVAL    = 30      # Sekunden
DRY_RUN     = True
# ═══════════════════════════════════════════════════════════

# State
levels        = {}    # {key: {"buy_price": int, "sell_price": int, "filled": bool, "buy_time": float}}
total_size    = 0.0
wins          = 0
total_pnl     = 0.0
prev_preis    = None


def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(m, c=""):
    print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}")
    sys.stdout.flush()

def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"


# ─── PREIS ────────────────────────────────────────────────

def get_preis():
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=all_products",
            headers={"Accept-Encoding": "gzip"},
            timeout=15, verify=False
        )
        if r.status_code != 200: return None
        body = r.json()
        data = body.get("data", body)
        for p in data.get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e:
        log(f"Preis Fehler: {e}", Y)
    return None


# ─── NADO POSITION ────────────────────────────────────────

def get_nado_size():
    """Holt echte BTC Position von Nado API."""
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}",
            headers={"Accept-Encoding": "gzip"},
            timeout=15, verify=False
        )
        if r.status_code != 200: return None
        data = r.json().get("data", {})
        for pb in data.get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                amt = float(pb["balance"]["amount"]) / 1e18
                return max(0.0, amt)  # nur positive (LONG)
    except Exception as e:
        log(f"Nado API Fehler: {e}", Y)
    return None


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x", ""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()

def place_order(is_buy, price):
    """
    Platziert eine Order.
    is_buy=True  → LONG öffnen (positive Menge)
    is_buy=False → LONG schließen (negative Menge)
    Immer ohne reduce_only — Nado erkennt Richtung durch Menge.
    Preis auf $1 gerundet mit 0.2% Slippage für schnelle Füllung.
    """
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {ORDER_SIZE} BTC @ {fmt(price)}", Y)
        return True
    try:
        from eth_account import Account

        # Preis auf $1 runden + Slippage
        slippage = 1.002 if is_buy else 0.998
        px       = round(price * slippage) * int(1e18)

        # Menge: positiv = LONG öffnen, negativ = LONG schließen
        amt = int(ORDER_SIZE * 1e18) if is_buy else -int(ORDER_SIZE * 1e18)

        exp   = int(time.time()) + 60
        nonce = ((int(time.time() * 1000) + 5000) << 20) + random.randint(0, 99999)
        apx   = 1  # Version=1, kein reduce_only, kein IOC
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
        signed = acc.sign_typed_data(
            domain_data=dom, message_types=typ, message_data=msg
        )
        sig = signed.signature.hex()
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

        code = d.get("error_code", 0)
        err  = d.get("error", "")
        log(f"❌ {err} (Code:{code})", R)
        return False

    except Exception as e:
        log(f"Order Exception: {e}", R)
        return False


# ─── GRID ─────────────────────────────────────────────────

def setup_grid(preis):
    """Baut Grid mit GRID_LEVELS unter aktuellem Preis."""
    global levels, total_size
    levels     = {}
    total_size = 0.0

    for i in range(1, GRID_LEVELS + 1):
        buy_price  = round(preis * (1 - i * GRID_STEP / 100))
        sell_price = round(buy_price * (1 + GRID_PROFIT / 100))
        key = str(buy_price)
        levels[key] = {
            "buy_price":  buy_price,
            "sell_price": sell_price,
            "filled":     False,
            "buy_time":   0.0
        }

    lvl = " | ".join([
        fmt(v["buy_price"])
        for v in sorted(levels.values(), key=lambda x: x["buy_price"], reverse=True)
    ])
    log(f"Grid @ {fmt(preis)} | Levels: {lvl}", C)


def sync_nado(preis):
    """
    Vergleicht Bot-State mit echter Nado Position.
    Wird nur aufgerufen wenn mindestens 3 Min seit letztem Kauf vergangen.
    """
    global total_size, levels

    # Prüfe ob letzter Kauf zu kürzlich war
    now = time.time()
    for lv in levels.values():
        if lv["filled"] and (now - lv["buy_time"]) < 180:
            log("Sync übersprungen — Kauf zu kürzlich", Y)
            return

    nado_size = get_nado_size()
    if nado_size is None: return

    if abs(nado_size - total_size) < 0.0001: return

    log(f"Sync: Bot={total_size:.4f} BTC | Nado={nado_size:.4f} BTC", Y)

    if nado_size == 0 and total_size > 0:
        # Nado hat keine Position — alle Levels zurücksetzen
        log("Nado: keine Position — Levels zurücksetzen", Y)
        for key in levels:
            levels[key]["filled"]   = False
            levels[key]["buy_time"] = 0.0
        total_size = 0.0

    elif nado_size < total_size:
        # Weniger als erwartet — anpassen
        total_size = nado_size


def check_grid(preis):
    """Prüft ob Preis ein Buy oder Sell Level erreicht hat."""
    global total_size, wins, total_pnl

    falling = prev_preis is not None and preis < prev_preis

    # ── BUY: Preis fällt auf Level ────────────────────────
    if falling:
        for key, lv in sorted(
            levels.items(),
            key=lambda x: x[1]["buy_price"],
            reverse=True
        ):
            buy_p = lv["buy_price"]
            if not lv["filled"] and abs(preis - buy_p) / buy_p <= BUY_TOL:
                log(f"🟢 BUY @ {fmt(buy_p)} (Preis:{fmt(preis)}) | TP:{fmt(lv['sell_price'])}", G)
                ok = place_order(True, preis)
                if ok:
                    lv["filled"]   = True
                    lv["buy_time"] = time.time()
                    total_size     = round(total_size + ORDER_SIZE, 4)
                    time.sleep(3)
                break  # nur 1 Buy pro Tick

    # ── SELL: Preis steigt auf Sell Level ─────────────────
    for key, lv in sorted(
        levels.items(),
        key=lambda x: x[1]["sell_price"]
    ):
        if lv["filled"] and preis >= lv["sell_price"] * (1 - SELL_TOL):
            log(f"🔴 SELL @ {fmt(lv['sell_price'])} (Preis:{fmt(preis)}) | Gekauft:{fmt(lv['buy_price'])}", R)
            ok = place_order(False, preis)
            if ok:
                total_pnl       += GRID_PROFIT
                wins            += 1
                total_size       = max(0.0, round(total_size - ORDER_SIZE, 4))
                lv["filled"]     = False
                lv["buy_time"]   = 0.0
                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W | Pos:{total_size:.4f} BTC", G)
                time.sleep(3)
            break  # nur 1 Sell pro Tick


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global prev_preis, total_size, levels
    tick = 0

    log(f"Bot gestartet | Step:{GRID_STEP}% | Profit:{GRID_PROFIT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    # Beim Start: Nado Position holen
    preis_start = get_preis()
    if preis_start:
        setup_grid(preis_start)
        nado = get_nado_size()
        if nado and nado > 0:
            log(f"Bestehende Position: {nado:.4f} BTC — Verkauf bei {fmt(round(preis_start * (1 + GRID_PROFIT/100)))}", Y)
            sell_p = round(preis_start * (1 + GRID_PROFIT / 100))
            key = f"existing_{int(time.time())}"
            levels[key] = {
                "buy_price":  round(preis_start),
                "sell_price": sell_p,
                "filled":     True,
                "buy_time":   0.0  # 0 = sofort sync erlaubt
            }
            total_size = nado

    while True:
        try:
            tick += 1
            preis = get_preis()

            if not preis:
                log("Kein Preis — warte...", Y)
                time.sleep(INTERVAL)
                continue

            # Sync alle 4 Ticks (~2 Min)
            if tick % 4 == 0:
                sync_nado(preis)

            # Grid neu aufbauen NUR wenn keine Levels vorhanden
            if not levels:
                log(f"Grid neu aufbauen @ {fmt(preis)}", Y)
                setup_grid(preis)

            # Grid prüfen
            check_grid(preis)

            # Richtung merken
            prev_preis = preis

            # Status anzeigen
            if tick % 2 == 0:
                filled = sum(1 for v in levels.values() if v["filled"])
                log(f"BTC {fmt(preis)} | Offen:{filled}/{GRID_LEVELS} | Pos:{total_size:.4f} BTC | {wins}W | P&L:{total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if total_size > 0:
                log(f"⚠️ {total_size:.4f} BTC offen — bitte manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R)
            time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║      Nado.xyz — Grid Trading Bot         ║")
    print(f"  ║   Kaufe günstig, verkaufe teurer         ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:  {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:    {GRID_STEP}% | Levels: {GRID_LEVELS} | Profit: +{GRID_PROFIT}%")
    print(f"  Size:    {ORDER_SIZE} BTC pro Level")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:   {modus}\n")
    loop()


if __name__ == "__main__":
    main()
