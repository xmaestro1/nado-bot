"""
Nado.xyz Grid Trading Bot — Perps Edition
==========================================
Korrekte Grid Logik für Perpetuals:
- Eine Position, mehrere Levels
- Kaufe günstig → verkaufe teurer
- Positionsgröße wächst beim Rückgang
- Positionsgröße schrumpft beim Anstieg

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
WALLET_ADDR  = "0xc15263578ce7fd6290f56Ab78a23D3b6C653B28C"
SIGNER_KEY   = "0x8097b0ec439aa91bd4f3c3ea79735be6688ce00589bbcd0e3dea2ab596580a4d"

PRODUCT_ID   = 2
CHAIN_ID     = 57073
GATEWAY      = "https://gateway.prod.nado.xyz/v1"
ARCHIVE      = "https://archive.prod.nado.xyz/v1"
HEADERS      = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0015   # BTC pro Grid Level
GRID_LEVELS  = 5        # Anzahl Buy Levels
GRID_STEP    = 0.4      # % Abstand zwischen Levels
GRID_PROFIT  = 0.4      # % Gewinn pro Level
INTERVAL     = 30
DRY_RUN      = False
STATE_FILE   = "grid_state.json"
# ═══════════════════════════════════════════════════════════

# Grid State
buy_levels   = {}   # {preis: {"size": 0.0015, "sell_at": preis*1.004, "filled": False}}
total_size   = 0.0  # Gesamte offene Position in BTC
wins         = 0
total_pnl    = 0.0
grid_center  = 0.0


def ts():    return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"


# ─── STATE ────────────────────────────────────────────────

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"buy_levels": buy_levels, "total_size": total_size,
                      "wins": wins, "total_pnl": total_pnl, "grid_center": grid_center}, f)
    except: pass

def load_state():
    global buy_levels, total_size, wins, total_pnl, grid_center
    try:
        if os.path.exists(STATE_FILE):
            d = json.load(open(STATE_FILE))
            buy_levels  = d.get("buy_levels", {})
            total_size  = d.get("total_size", 0.0)
            wins        = d.get("wins", 0)
            total_pnl   = d.get("total_pnl", 0.0)
            grid_center = d.get("grid_center", 0.0)
            if buy_levels:
                filled = sum(1 for v in buy_levels.values() if v["filled"])
                log(f"State: {filled} offene Positionen | Größe:{total_size:.4f} BTC | {wins}W P&L:{total_pnl:+.2f}%", Y)
    except Exception as e:
        log(f"State Fehler: {e}", Y)


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


# ─── NADO SYNC ───────────────────────────────────────────

SUBACCOUNT = '0xc15263578ce7fd6290f56ab78a23d3b6c653b28c64656661756c740000000000'

def get_nado_position():
    """Holt echte Position von Nado API."""
    try:
        url = f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}"
        r = requests.get(url, headers={"Accept-Encoding": "gzip"}, timeout=15, verify=False)
        if r.status_code != 200: return None
        data = r.json().get("data", {})
        for pb in data.get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                amt = float(pb["balance"]["amount"]) / 1e18
                return amt  # positiv = LONG, negativ = SHORT, 0 = kein Trade
    except Exception as e:
        log(f"Sync Fehler: {e}", Y)
    return None

def sync_mit_nado():
    """Synchronisiert Bot-State mit echter Nado Position."""
    global total_size, buy_levels
    nado_pos = get_nado_position()
    if nado_pos is None:
        return
    nado_size = abs(nado_pos)
    if abs(nado_size - total_size) > 0.0001:
        log(f"⚠️ Sync: Bot={total_size:.4f} BTC, Nado={nado_size:.4f} BTC — korrigiere!", Y)
        total_size = nado_size
        if nado_size == 0:
            # Alle Positionen auf Nado geschlossen — Bot State zurücksetzen
            for key in buy_levels:
                buy_levels[key]["filled"] = False
            log("State zurückgesetzt — kein offener Trade auf Nado", Y)
        save_state()

# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x",""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()

def place_order(is_buy, price, size=ORDER_SIZE, reduce_only=False):
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
        return True
    try:
        from eth_account import Account
        px    = round(price * (1.001 if is_buy else 0.999)) * int(1e18)
        amt   = int(size*1e18) if is_buy else -int(size*1e18)
        exp   = int(time.time()) + 60
        nonce = ((int(time.time()*1000)+5000) << 20) + random.randint(0,999)
        apx   = 1 | (1<<11 if reduce_only else 0)
        sndr  = sender_hex()
        dom = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,"verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ = {"Order":[{"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
                        {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
                        {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg = {"sender":sndr,"priceX18":px,"amount":amt,"expiration":exp,"nonce":nonce,"appendix":apx}
        acc = Account.from_key(SIGNER_KEY)
        sig = acc.sign_typed_data(domain_data=dom, message_types=typ, message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x"+sig
        pld = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":str(apx)
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log(f"✅ OK! Digest:{d.get('data',{}).get('digest','')[:16]}", G)
            return True
        err  = d.get("error", "")
        code = d.get("error_code", 0)
        log(f"❌ {err} (Code:{code})", R)
        return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False


# ─── GRID ─────────────────────────────────────────────────

def setup_grid(preis):
    """Erstellt Grid Levels — Level 0 = aktueller Preis, Rest darunter."""
    global buy_levels, grid_center, total_size
    buy_levels  = {}
    total_size  = 0.0
    grid_center = round(preis)

    # Level 0: aktueller Preis (sofort kaufen)
    sell_price_0 = round(preis * (1 + GRID_PROFIT / 100))
    buy_levels[str(round(preis))] = {
        "buy_price":  round(preis),
        "sell_price": sell_price_0,
        "size":       ORDER_SIZE,
        "filled":     False
    }

    # Level 1-4: darunter
    for i in range(1, GRID_LEVELS):
        buy_price  = round(preis * (1 - i * GRID_STEP / 100))
        sell_price = round(buy_price * (1 + GRID_PROFIT / 100))
        buy_levels[str(buy_price)] = {
            "buy_price":  buy_price,
            "sell_price": sell_price,
            "size":       ORDER_SIZE,
            "filled":     False
        }

    levels_str = " | ".join([fmt(float(k)) for k in sorted(buy_levels.keys(), key=float, reverse=True)])
    log(f"Grid @ {fmt(preis)} | Levels: {levels_str}", C)
    save_state()

def check_grid(preis):
    """Prüft Grid Levels und führt Orders aus."""
    global total_size, wins, total_pnl

    # BUY: Preis fällt auf ein Level
    for key, level in buy_levels.items():
        buy_price = level["buy_price"]

        # Noch nicht gekauft und Preis ist am Level
        if not level["filled"] and abs(preis - buy_price) / buy_price <= 0.002:
            log(f"🟢 BUY @ {fmt(buy_price)} | Verkaufe bei {fmt(level['sell_price'])}", G)
            ok = place_order(True, buy_price, size=ORDER_SIZE)
            if ok:
                level["filled"] = True
                total_size += ORDER_SIZE
                save_state()
                time.sleep(2)  # Kurz warten zwischen Orders
            break  # Nur ein Buy pro Tick

    # SELL: Preis steigt auf einen Verkaufslevel
    for key, level in list(buy_levels.items()):
        if level["filled"] and preis >= level["sell_price"] * 0.999:
            log(f"🔴 SELL @ {fmt(level['sell_price'])} | Gekauft @ {fmt(level['buy_price'])}", R)
            ok = place_order(False, level["sell_price"], size=ORDER_SIZE, reduce_only=True)
            if ok:
                pnl = GRID_PROFIT
                total_pnl += pnl
                wins += 1
                total_size -= ORDER_SIZE
                if total_size < 0: total_size = 0
                level["filled"] = False  # Level wieder verfügbar für nächsten Kauf
                log(f"✅ +{pnl}% | Total: {total_pnl:+.2f}% | {wins} Wins | Pos: {total_size:.4f} BTC", G)
                save_state()
                time.sleep(2)
            break  # Nur ein Sell pro Tick


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global buy_levels, total_size
    tick = 0
    log(f"Grid Bot | BTC | Step:{GRID_STEP}% | Profit:{GRID_PROFIT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1
            preis = get_preis()

            if not preis:
                log("Kein Preis", Y)
                time.sleep(INTERVAL); continue

            # Grid neu aufbauen wenn:
            # 1. Noch kein Grid vorhanden
            # 2. Preis zu weit nach oben (alle verkauft, neu starten)
            if not buy_levels:
                log(f"Grid wird aufgebaut @ {fmt(preis)}", Y)
                setup_grid(preis)

            # Nur neu aufbauen wenn Preis ÜBER allen Buy Levels ist
            # Das bedeutet alle Positionen wurden profitabel geschlossen
            if buy_levels and wins > 0:
                highest_buy = max(float(k) for k in buy_levels.keys())
                if preis > highest_buy * 1.005 and total_size == 0:
                    log(f"Alle Levels profitabel geschlossen! Neu aufbauen @ {fmt(preis)}", Y)
                    setup_grid(preis)

            # Sync mit Nado alle 5 Ticks
            if tick % 5 == 0:
                sync_mit_nado()

            # Grid prüfen
            check_grid(preis)

            # Status
            if tick % 3 == 0:
                filled = sum(1 for v in buy_levels.values() if v["filled"])
                log(f"BTC {fmt(preis)} | Offen:{filled}/{GRID_LEVELS} | Pos:{total_size:.4f} BTC | Wins:{wins} | P&L:{total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if total_size > 0:
                log(f"⚠️ Offene Position: {total_size:.4f} BTC — manuell auf app.nado.xyz schließen!", R)
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
    print(f"  Grid Step:   {GRID_STEP}% zwischen Levels")
    print(f"  Grid Levels: {GRID_LEVELS}")
    print(f"  Profit:      +{GRID_PROFIT}% pro Level")
    print(f"  Order Size:  {ORDER_SIZE} BTC")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:       {modus}\n")
    load_state()
    log("Synchronisiere mit Nado...", C)
    sync_mit_nado()
    loop()


if __name__ == "__main__":
    main()
