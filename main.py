"""
Nado.xyz Grid Trading Bot — Komplett neu
==========================================
Alle Fehler behoben:
1. Buy nur beim Preisfall (nicht beim Anstieg)
2. Alte Positionen bekommen Verkaufsziel
3. Code 2064 wird korrekt behandelt
4. Grid Neuaufbau funktioniert immer
5. Kein sofortiger Kauf beim Start
6. DRY_RUN = False (Live)
7. place_order size korrekt
8. Toleranz auf 0.3% erhöht
9. Sell Break nur bei Erfolg
10. State via Nado API (kein File-Problem)
11. Sync jede Minute
12. grid_center entfernt
13. Vorheriger Preis gespeichert für Richtungsprüfung

Einrichten:
    SIGNER_KEY = 1-Click Trading Key (app.nado.xyz → Settings)
"""

import time, random, requests, sys, json, os, urllib3
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
#  EINSTELLUNGEN
# ═══════════════════════════════════════════════════════════
WALLET_ADDR  = "0x14A26C3F3fF2C7A5bC4a1E5E5B15972628288ab7"
SIGNER_KEY   = "0xf7bbe14bbca148872730e063ca969d37f2d7006b22e4486877adaef9206192f9"
SUBACCOUNT   = "0x14a26c3f3ff2c7a5bc4a1e5e5b15972628288ab764656661756c740000000000"

PRODUCT_ID   = 2
CHAIN_ID     = 57073
GATEWAY      = "https://gateway.prod.nado.xyz/v1"
HEADERS      = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0032   # BTC pro Grid Level
GRID_LEVELS  = 5        # Anzahl Buy Levels
GRID_STEP    = 0.4      # % Abstand zwischen Levels
GRID_PROFIT  = 0.4      # % Gewinn pro Level
BUY_TOL      = 0.003    # 0.3% Toleranz für Buy Level (Fix 8)
SELL_TOL     = 0.001    # 0.1% Toleranz für Sell Level
SYNC_EVERY   = 2        # Sync alle 2 Ticks = ~60 Sek (Fix 13)
INTERVAL     = 30
DRY_RUN      = False    # Fix 6
# ═══════════════════════════════════════════════════════════

# State — nur im RAM (Fix 11: kein File das bei Neustart gelöscht wird)
levels       = {}   # {str(buy_price): {"buy": int, "sell": int, "filled": bool}}
total_size   = 0.0
wins         = 0
total_pnl    = 0.0
prev_preis   = None  # Für Richtungsprüfung (Fix 1)


def ts():    return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"


# ─── API ──────────────────────────────────────────────────

def get_preis():
    try:
        r = requests.get(f"{GATEWAY}/query?type=all_products",
                        headers={"Accept-Encoding": "gzip"}, timeout=15, verify=False)
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

def get_nado_position():
    """Holt echte BTC Position von Nado."""
    try:
        url = f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}"
        r = requests.get(url, headers={"Accept-Encoding": "gzip"}, timeout=15, verify=False)
        if r.status_code != 200: return None
        data = r.json().get("data", {})
        for pb in data.get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                return float(pb["balance"]["amount"]) / 1e18
    except Exception as e:
        log(f"Nado API Fehler: {e}", Y)
    return None


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x",""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()

def place_order(is_buy, price, reduce_only=False):
    """Platziert eine Limit Order mit 0.1% Slippage."""
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {ORDER_SIZE} BTC @ {fmt(price)}", Y)
        return True
    try:
        from eth_account import Account
        px    = round(price * (1.001 if is_buy else 0.999)) * int(1e18)
        amt   = int(ORDER_SIZE * 1e18) if is_buy else -int(ORDER_SIZE * 1e18)
        exp   = int(time.time()) + 60
        nonce = ((int(time.time()*1000) + 5000) << 20) + random.randint(0, 999)
        apx   = 1 | (1 << 11 if reduce_only else 0)
        sndr  = sender_hex()
        dom   = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,"verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ   = {"Order":[
            {"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
            {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
            {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}
        ]}
        msg = {"sender":sndr,"priceX18":px,"amount":amt,"expiration":exp,"nonce":nonce,"appendix":apx}
        acc = Account.from_key(SIGNER_KEY)
        sig = acc.sign_typed_data(domain_data=dom, message_types=typ, message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x" + sig
        pld = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":str(apx)
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log(f"✅ OK!", G); return True
        code = d.get("error_code", 0)
        err  = d.get("error", "")
        if code == 2064:
            log(f"⚠️ Position nicht auf Nado (2064) — Level zurücksetzen", Y)
            return "RESET"  # Fix 3: spezieller Rückgabewert
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

    # Alle Levels UNTER aktuellem Preis (Fix 5: kein sofortiger Kauf)
    for i in range(1, GRID_LEVELS + 1):
        buy_price  = round(preis * (1 - i * GRID_STEP / 100))
        sell_price = round(buy_price * (1 + GRID_PROFIT / 100))
        levels[str(buy_price)] = {
            "buy":    buy_price,
            "sell":   sell_price,
            "filled": False
        }

    lvl_str = " | ".join([fmt(v["buy"]) for v in sorted(levels.values(), key=lambda x: x["buy"], reverse=True)])
    log(f"Grid @ {fmt(preis)} | Buy Levels: {lvl_str}", C)
    log(f"Warte bis BTC fällt auf {fmt(max(v['buy'] for v in levels.values()))}...", Y)


def sync_nado(preis):
    """
    Synchronisiert mit echter Nado Position.
    Fix 2: Erstellt Verkaufsziel für unbekannte Positionen.
    Fix 11: State kommt von Nado API, nicht von File.
    """
    global total_size, levels

    nado_amt = get_nado_position()
    if nado_amt is None: return

    nado_size = max(0.0, nado_amt)  # nur LONG

    if abs(nado_size - total_size) < 0.0001:
        return  # Alles synchron

    log(f"Sync: Bot={total_size:.4f} BTC | Nado={nado_size:.4f} BTC", Y)

    if nado_size == 0 and total_size > 0:
        # Nado hat keine Position mehr → alle Levels zurücksetzen
        log("Nado: keine Position — alle Levels zurücksetzen", Y)
        for key in levels:
            levels[key]["filled"] = False
        total_size = 0.0

    elif nado_size > total_size:
        # Nado hat mehr als Bot weiß → unbekannte Position
        diff = round(nado_size - total_size, 4)
        log(f"Unbekannte Position: {diff:.4f} BTC — erstelle Verkaufsziel @ {fmt(preis * 1.004)}", Y)
        # Fix 2: Verkaufsziel für unbekannte Position erstellen
        sell_price = round(preis * (1 + GRID_PROFIT / 100))
        key = f"unknown_{int(time.time())}"
        levels[key] = {
            "buy":    round(preis),
            "sell":   sell_price,
            "filled": True   # bereits gefüllt
        }
        total_size = nado_size

    elif nado_size < total_size:
        # Nado hat weniger → Position wurde extern geschlossen
        total_size = nado_size
        # Filled Levels anpassen
        filled_count = round(total_size / ORDER_SIZE)
        count = 0
        for key in sorted(levels.keys()):
            if levels[key]["filled"]:
                if count >= filled_count:
                    levels[key]["filled"] = False
                count += 1


def check_grid(preis):
    """
    Prüft Grid Levels und führt Orders aus.
    Fix 1: Buy nur wenn Preis fällt.
    Fix 9: Break nur bei Erfolg.
    """
    global total_size, wins, total_pnl, prev_preis

    falling = prev_preis is not None and preis < prev_preis  # Fix 1

    # BUY: Preis fällt auf ein Level
    if falling:
        for key, lv in sorted(levels.items(), key=lambda x: x[1]["buy"], reverse=True):
            if not lv["filled"] and abs(preis - lv["buy"]) / lv["buy"] <= BUY_TOL:
                log(f"🟢 BUY @ {fmt(lv['buy'])} | TP: {fmt(lv['sell'])} | Preis fällt ↓", G)
                ok = place_order(True, lv["buy"])
                if ok is True:
                    lv["filled"] = True
                    total_size   = round(total_size + ORDER_SIZE, 4)
                    time.sleep(2)
                    break  # Fix 9: Break nur bei Erfolg
                elif ok is False:
                    break  # Fehler — warte nächsten Tick

    # SELL: Preis steigt auf Verkaufslevel
    for key, lv in sorted(levels.items(), key=lambda x: x[1]["sell"]):
        if lv["filled"] and preis >= lv["sell"] * (1 - SELL_TOL):
            log(f"🔴 SELL @ {fmt(lv['sell'])} | Gekauft @ {fmt(lv['buy'])}", R)
            ok = place_order(False, lv["sell"], reduce_only=True)
            if ok is True:
                pnl        = GRID_PROFIT
                total_pnl += pnl
                wins      += 1
                total_size = max(0.0, round(total_size - ORDER_SIZE, 4))
                lv["filled"] = False
                log(f"✅ +{pnl}% | Total: {total_pnl:+.2f}% | {wins} Wins | Pos: {total_size:.4f} BTC", G)
                time.sleep(2)
                break  # Fix 9: Break bei Erfolg
            elif ok == "RESET":
                # Fix 3: Code 2064 — Position nicht auf Nado
                lv["filled"] = False
                total_size   = max(0.0, round(total_size - ORDER_SIZE, 4))
                break
            else:
                break  # Anderer Fehler — warte nächsten Tick


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global prev_preis, total_size, levels
    tick = 0
    log(f"Grid Bot | Step:{GRID_STEP}% | Profit:{GRID_PROFIT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    # Beim Start: echte Nado Position holen
    log("Prüfe Nado Position...", C)
    preis_start = get_preis()
    if preis_start:
        setup_grid(preis_start)
        sync_nado(preis_start)

    while True:
        try:
            tick += 1
            preis = get_preis()

            if not preis:
                log("Kein Preis", Y)
                time.sleep(INTERVAL); continue

            # Sync alle 2 Ticks (Fix 13: ~60 Sek)
            if tick % SYNC_EVERY == 0:
                sync_nado(preis)

            # Grid neu aufbauen wenn:
            # A) Kein Grid vorhanden
            # B) Preis zu weit über allen Levels UND keine offene Position (Fix 4)
            if not levels:
                setup_grid(preis)
            else:
                highest_buy = max(v["buy"] for v in levels.values())
                no_filled   = not any(v["filled"] for v in levels.values())
                if preis > highest_buy * 1.005 and no_filled:
                    log(f"Alle Levels geschlossen — Grid neu @ {fmt(preis)}", Y)
                    setup_grid(preis)

            # Grid prüfen
            check_grid(preis)
            prev_preis = preis  # Fix 1: Richtung merken

            # Status anzeigen
            if tick % 2 == 0:
                filled = sum(1 for v in levels.values() if v["filled"])
                log(f"BTC {fmt(preis)} | Offen:{filled} | Pos:{total_size:.4f} BTC | Wins:{wins} | P&L:{total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if total_size > 0:
                log(f"⚠️ {total_size:.4f} BTC offen — manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R)
            time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║      Nado.xyz — Grid Trading Bot         ║")
    print(f"  ║   Kaufe günstig, verkaufe teurer         ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:      {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Grid Step:   {GRID_STEP}% | Levels: {GRID_LEVELS}")
    print(f"  Profit:      +{GRID_PROFIT}% pro Level")
    print(f"  Order Size:  {ORDER_SIZE} BTC")
    print(f"  Buy Tol:     {BUY_TOL*100}% | Sell Tol: {SELL_TOL*100}%")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:       {modus}\n")
    loop()


if __name__ == "__main__":
    main()
