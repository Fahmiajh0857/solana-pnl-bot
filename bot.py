import os
import sqlite3
from datetime import datetime, time, timedelta, timezone
import asyncio
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
WIB = timezone(timedelta(hours=7))

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
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_history (
        date TEXT PRIMARY KEY,
        pnl REAL
    )
    """)


    cursor.execute("SELECT COUNT(*) FROM state")
    if cursor.fetchone()[0] == 0:
        sol, usdc = get_balances(force=True)
        price = get_sol_price()
        value = sol * price + usdc
        now = datetime.now(WIB).isoformat()

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

def get_last_n_days(n):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT pnl FROM daily_history
    ORDER BY date DESC
    LIMIT ?
    """, (n,))

    rows = cursor.fetchall()
    conn.close()

    return sum(r[0] for r in rows)


# ================= CACHE =================
def get_sol_price():
    now = datetime.now(WIB)
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
    now = datetime.now(WIB)
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
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT base_sol_day, base_usdc_day, base_value_day, last_daily_reset
    FROM state WHERE id=1
    """)

    dsol, dusdc, dval, ldaily = cursor.fetchone()

    now = datetime.now(WIB)
    price = get_sol_price()
    current_value = sol * price + usdc

    last_daily = datetime.fromisoformat(ldaily)

    if now.time() >= RESET_TIME and last_daily.date() < now.date():

        # hitung closing pnl
        daily_pnl = current_value - dval
        today_str = now.date().isoformat()

        cursor.execute("""
        INSERT OR REPLACE INTO daily_history (date, pnl)
        VALUES (?, ?)
        """, (today_str, daily_pnl))

        # update base baru
        cursor.execute("""
        UPDATE state
        SET base_sol_day=?,
            base_usdc_day=?,
            base_value_day=?,
            last_daily_reset=?
        WHERE id=1
        """, (sol, usdc, current_value, now.isoformat()))

        conn.commit()

        dsol = sol
        dusdc = usdc
        dval = current_value

    conn.close()

    return dsol, dusdc, dval


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

    dsol, dusdc, dval = check_resets(sol, usdc)

    sol_value = sol * price
    total_value = sol_value + usdc

    # DAILY realtime
    pnl_d = total_value - dval
    percent_d = calc_percent(pnl_d, dval)

    # 7D rolling
    pnl_w = get_last_n_days(7)
    percent_w = calc_percent(pnl_w, dval)

    # 30D rolling
    pnl_m = get_last_n_days(30)
    percent_m = calc_percent(pnl_m, dval)

    def pnl_icon(x):
        if x > 0:
            return "🟢"
        elif x < 0:
            return "🔴"
        return "⚪"

    message = f"""
<b>DASHBOARD</b>
━━━━━━━━━━━━
($SOL): <code>${price:.2f}</code>
Daily TVL: <code>${dval:.2f}</code>

💎 Portfolio
SOL:  {sol:.4f} ≈ (<code>${sol_value:.2f}</code>)
USDC:  <code>${usdc:.2f}</code>

Total:  <code>${total_value:.2f}</code>

📊 PNL
Daily   {pnl_icon(pnl_d)} <code>${pnl_d:.2f}</code> ({percent_d:.2f}%)
7D       {pnl_icon(pnl_w)} <code>${pnl_w:.2f}</code> ({percent_w:.2f}%)
30D     {pnl_icon(pnl_m)} <code>${pnl_m:.2f}</code> ({percent_m:.2f}%)
"""

    await update.message.reply_text(message, parse_mode="HTML")


async def cek7(update: Update, context: ContextTypes.DEFAULT_TYPE):

    pnl = get_last_n_days(7)
    sign = "+" if pnl >= 0 else ""

    await update.message.reply_text(
        f"""
📅 LAST 7 DAYS
━━━━━━━━━━━━━━
PnL: {sign}${pnl:.2f}
"""
    )


async def cek30(update: Update, context: ContextTypes.DEFAULT_TYPE):

    pnl = get_last_n_days(30)
    sign = "+" if pnl >= 0 else ""

    await update.message.reply_text(
        f"""
📆 LAST 30 DAYS
━━━━━━━━━━━━━━
PnL: {sign}${pnl:.2f}
"""
    )
async def auto_reset(context: ContextTypes.DEFAULT_TYPE):
    sol, usdc = get_balances()
    check_resets(sol, usdc)
    


# ================= MAIN =================
def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("cek", cek))
    app.add_handler(CommandHandler("cek7", cek7))
    app.add_handler(CommandHandler("cek30", cek30))

    # auto check reset tiap 60 detik
    app.job_queue.run_repeating(auto_reset, interval=60, first=5)

    app.run_polling()

if __name__ == "__main__":
    main()
