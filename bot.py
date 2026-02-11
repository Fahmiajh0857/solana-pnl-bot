import os
import sqlite3
import logging
import requests
from datetime import datetime, time
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from dotenv import load_dotenv
load_dotenv()


# ================= CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DB_FILE = "pnl_data.db"
RESET_TIME = time(3, 0)

PRICE_CACHE = {"value": None, "timestamp": None}
BALANCE_CACHE = {"sol": None, "usdc": None, "timestamp": None}

logging.basicConfig(level=logging.INFO)

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY,
        base_sol_day REAL,
        base_usdc_day REAL,
        base_value_day REAL,
        base_sol_week REAL,
        base_usdc_week REAL,
        base_value_week REAL,
        base_sol_month REAL,
        base_usdc_month REAL,
        base_value_month REAL,
        last_daily_reset TEXT,
        last_weekly_reset TEXT,
        last_monthly_reset TEXT
    )
    """)

    cursor.execute("SELECT COUNT(*) FROM state")
    if cursor.fetchone()[0] == 0:
        sol, usdc = get_balances(force=True)
        price = get_sol_price()
        value = sol * price + usdc
        now = datetime.now().isoformat()

        cursor.execute("""
        INSERT INTO state VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sol, usdc, value,
            sol, usdc, value,
            sol, usdc, value,
            now, now, now
        ))

    conn.commit()
    conn.close()

def load_state():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM state WHERE id=1")
    row = cursor.fetchone()
    conn.close()
    return row

def save_state(data):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE state SET
    base_sol_day=?, base_usdc_day=?, base_value_day=?,
    base_sol_week=?, base_usdc_week=?, base_value_week=?,
    base_sol_month=?, base_usdc_month=?, base_value_month=?,
    last_daily_reset=?, last_weekly_reset=?, last_monthly_reset=?
    WHERE id=1
    """, data)
    conn.commit()
    conn.close()

# ================= CACHE =================
def get_sol_price():
    now = datetime.now()
    if PRICE_CACHE["timestamp"] and (now - PRICE_CACHE["timestamp"]).seconds < 60:
        return PRICE_CACHE["value"]

    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
    )
    price = r.json()["solana"]["usd"]
    PRICE_CACHE["value"] = price
    PRICE_CACHE["timestamp"] = now
    return price

def get_balances(force=False):
    now = datetime.now()
    if not force and BALANCE_CACHE["timestamp"] and \
       (now - BALANCE_CACHE["timestamp"]).seconds < 30:
        return BALANCE_CACHE["sol"], BALANCE_CACHE["usdc"]

    client = Client(SOLANA_RPC_URL)
    pubkey = Pubkey.from_string(WALLET_ADDRESS)
    sol = client.get_balance(pubkey).value / 1_000_000_000

    payload = {
        "jsonrpc":"2.0","id":1,
        "method":"getTokenAccountsByOwner",
        "params":[WALLET_ADDRESS,{"mint":USDC_MINT},{"encoding":"jsonParsed"}]
    }
    r = requests.post(SOLANA_RPC_URL,json=payload)
    data = r.json()
    usdc = 0.0
    if data["result"]["value"]:
        usdc = float(data["result"]["value"][0]
            ["account"]["data"]["parsed"]
            ["info"]["tokenAmount"]["uiAmount"])

    BALANCE_CACHE.update({
        "sol": sol,
        "usdc": usdc,
        "timestamp": now
    })
    return sol, usdc

# ================= RESET =================
def check_resets(sol, usdc):
    state = load_state()

    (_, dsol,dusdc,dval,
     wsol,wusdc,wval,
     msol,musdc,mval,
     ldaily,lweekly,lmonthly) = state

    now = datetime.now()
    price = get_sol_price()
    current_value = sol*price + usdc

    last_daily = datetime.fromisoformat(ldaily)
    last_weekly = datetime.fromisoformat(lweekly)
    last_monthly = datetime.fromisoformat(lmonthly)

    if now.time() >= RESET_TIME and last_daily.date() < now.date():
        dsol, dusdc = sol, usdc
        dval = current_value
        last_daily = now

    if (now - last_weekly).days >= 7 and now.time() >= RESET_TIME:
        wsol, wusdc = sol, usdc
        wval = current_value
        last_weekly = now

    if now.day == 1 and now.time() >= RESET_TIME and last_monthly.month != now.month:
        msol, musdc = sol, usdc
        mval = current_value
        last_monthly = now

    save_state((
        dsol,dusdc,dval,
        wsol,wusdc,wval,
        msol,musdc,mval,
        last_daily.isoformat(),
        last_weekly.isoformat(),
        last_monthly.isoformat()
    ))

    return dsol,dusdc,dval,wsol,wusdc,wval,msol,musdc,mval

# ================= CALC =================
def calc_pnl(sol, usdc, base_sol, base_usdc):
    price = get_sol_price()
    return (sol-base_sol)*price + (usdc-base_usdc)

def calc_percent(pnl, base_value):
    if base_value == 0:
        return 0
    return (pnl/base_value)*100

# ================= TELEGRAM =================
async def cek(update: Update, context: ContextTypes.DEFAULT_TYPE):

    sol, usdc = get_balances()
    price = get_sol_price()

    # update & ambil base data
    dsol,dusdc,dval,wsol,wusdc,wval,msol,musdc,mval = check_resets(sol, usdc)

    # current value
    sol_value = sol * price
    total_value = sol_value + usdc

    # DAILY
    pnl_d = calc_pnl(sol, usdc, dsol, dusdc)
    percent_d = calc_percent(pnl_d, dval)

    # WEEKLY
    pnl_w = calc_pnl(sol, usdc, wsol, wusdc)
    percent_w = calc_percent(pnl_w, wval)

    # MONTHLY
    pnl_m = calc_pnl(sol, usdc, msol, musdc)
    percent_m = calc_percent(pnl_m, mval)

    # icon function
    def pnl_icon(x):
        if x > 0:
            return "🟢"
        elif x < 0:
            return "🔴"
        return "⚪"

    # status
    if total_value > dval:
        status = "📈 Capital Growing"
    elif total_value < dval:
        status = "📉 Capital Drawdown"
    else:
        status = "⚡ Capital Stable"

    message = f"""
<b>DASHBOARD</b>
━━━━━━━━━━━━
($SOL): <code>${price:.2f}</code>

💎 Portfolio
SOL:  {sol:.4f} ≈ (<code>${sol_value:.2f}</code>)
USDC:  <code>${usdc:.2f}</code>

Total:  <code>${total_value:.2f}</code>

📊 PNL
Daily   {pnl_icon(pnl_d)} <code>${pnl_d:.2f}</code> ({percent_d:.2f}%)
7D       {pnl_icon(pnl_w)} <code>${pnl_w:.2f}</code> ({percent_w:.2f}%)
30D     {pnl_icon(pnl_m)} <code>${pnl_m:.2f}</code> ({percent_m:.2f}%)
"""
    await update.message.reply_text(
    message,
    parse_mode="HTML"
)


async def cek7(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sol, usdc = get_balances()
    _,_,_,wsol,wusdc,wval,_,_,_ = check_resets(sol, usdc)

    price = get_sol_price()

    pnl = calc_pnl(sol, usdc, wsol, wusdc)
    percent = calc_percent(pnl, wval)

    sign = "+" if pnl >= 0 else ""

    await update.message.reply_text(
        f"""
📅 WEEKLY PERFORMANCE
━━━━━━━━━━━━━━━━━━
Base: ${wval:.2f}
PnL: {sign}${pnl:.2f}
Return: {sign}{percent:.2f}%
"""
    )

async def cek30(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sol, usdc = get_balances()
    _,_,_,_,_,_,msol,musdc,mval = check_resets(sol, usdc)

    price = get_sol_price()

    pnl = calc_pnl(sol, usdc, msol, musdc)
    percent = calc_percent(pnl, mval)

    sign = "+" if pnl >= 0 else ""

    await update.message.reply_text(
        f"""
📆 MONTHLY PERFORMANCE
━━━━━━━━━━━━━━━━━━
Base: ${mval:.2f}
PnL: {sign}${pnl:.2f}
Return: {sign}{percent:.2f}%
"""
    )


# ================= MAIN =================
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("cek", cek))
    app.add_handler(CommandHandler("cek7", cek7))
    app.add_handler(CommandHandler("cek30", cek30))

    app.run_polling()

if __name__ == "__main__":
    main()
