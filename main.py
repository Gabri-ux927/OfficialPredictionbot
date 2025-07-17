import os
import json
import asyncio
import datetime
import random
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- Config ---
AUTH_FILE = "auth.json"
PASSWORD_FILE = "password.txt"
ADMIN_TELEGRAM_ID = 2008190133
BOT_TOKEN = "7923809131:AAHZPX19cZYJNfUrR2SbFrHJ5CsLS6BWzDg"

# --- Globals ---
auth_data = {}
current_password = ""
user_tasks = {}  # user_id (str) -> asyncio.Task
user_predictions = {}  # user_id (str) -> (prediction, issueNumber)
user_stats = {}  # user_id (str) -> {"wins": int, "losses": int}

# --- Helper functions ---
def load_auth():
    if not os.path.exists(AUTH_FILE):
        return {}
    with open(AUTH_FILE, "r") as f:
        return json.load(f)

def save_auth(data):
    with open(AUTH_FILE, "w") as f:
        json.dump(data, f)

def load_password():
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, "r") as f:
            return f.read().strip()
    return "admin123"

def save_password(new_password):
    with open(PASSWORD_FILE, "w") as f:
        f.write(new_password)

def is_authorized(user_id):
    return str(user_id) in auth_data

def interval_to_seconds(interval_str):
    if interval_str.endswith("s"):
        return int(interval_str[:-1])
    if interval_str.endswith("m"):
        return int(interval_str[:-1]) * 60
    return 60

def generate_all_predictions():
    bigsmall = random.choice(["Big", "Small"])
    redgreen = random.choice(["Red", "Green"])
    numbers = random.sample(range(0, 10), 3)
    return bigsmall, redgreen, numbers

def interval_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏰ 30 seconds", callback_data="interval_30s"),
            InlineKeyboardButton("⏰ 1 minute", callback_data="interval_1m"),
        ],
        [
            InlineKeyboardButton("⏰ 3 minutes", callback_data="interval_3m"),
            InlineKeyboardButton("⏰ 5 minutes", callback_data="interval_5m"),
        ],
    ])

async def fetch_latest_issue():
    async with aiohttp.ClientSession() as session:
        async with session.get("https://draw.ar-lottery01.com/WinGo/WinGo_1M.json") as resp:
            data = await resp.json(content_type=None)
            issue_number = data["current"]["issueNumber"]
            end_time_val = data["current"]["endTime"]

            if isinstance(end_time_val, int) or isinstance(end_time_val, float):
                # It's a timestamp in milliseconds
                end_time_ts = end_time_val / 1000
            else:
                # Assume string datetime
                end_time_dt = datetime.datetime.strptime(end_time_val, "%Y-%m-%d %H:%M:%S")
                end_time_ts = end_time_dt.replace(tzinfo=datetime.timezone.utc).timestamp()

            return issue_number, int(end_time_ts * 1000)



async def fetch_real_result(issue_number, max_retries=5, delay=2):
    for _ in range(max_retries):
        async with aiohttp.ClientSession() as session:
            ts = int(datetime.datetime.utcnow().timestamp() * 1000)
            url = f"https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json?ts={ts}"
            async with session.get(url) as resp:
                try:
                    data = await resp.json(content_type=None)
                    for item in data["data"]["list"]:
                        if item["issueNumber"] == issue_number:
                            return item
                except Exception as e:
                    print(f"Error decoding result JSON: {e}")
        await asyncio.sleep(delay)
    print(f"❌ Result not found for issue {issue_number} after {max_retries} retries.")
    return None


async def send_prediction_realtime(app, chat_id: int, interval_seconds: int):
    while True:
        try:
            # Fetch latest issue and end time (in ms)
            issue_number, end_time_ms = await fetch_latest_issue()
            end_time_utc = datetime.datetime.utcfromtimestamp(end_time_ms / 1000).replace(tzinfo=datetime.timezone.utc)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            wait_seconds = (end_time_utc - now_utc).total_seconds()

            print(f"[PREDICTION] New issue: {issue_number}, ends at: {end_time_utc} UTC")
            print(f"[TIME] Current UTC time: {now_utc} (timestamp: {now_utc.timestamp()})")
            print(f"[WAIT] Sleeping for {wait_seconds:.2f} seconds until issue ends.")

            # Generate prediction
            bigsmall, redgreen, numbers = generate_all_predictions()
            numbers_str = " ".join(str(n) for n in numbers)
            user_predictions[str(chat_id)] = ((bigsmall, redgreen, numbers), issue_number)

            prediction_text = (
                f"🎯 *Prediction for Issue:* `{issue_number}`\n"
                f"🔵 *Big/Small:* {bigsmall}\n"
                f"🟥 *Red/Green:* {redgreen}\n"
                f"🔢 *Numbers:* {numbers_str}"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊 Stats", callback_data="show_stats"),
                    InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"),
                ]
            ])

            # Send prediction message
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=prediction_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard
                )
                print(f"[INFO] Sent prediction to user {chat_id}")
            except Exception as e:
                print(f"[ERROR] Failed to send prediction to user {chat_id}: {e}")

            # Wait until issue ends plus a small buffer
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds + 2)

            # Try to fetch the real result with retries
            result_data = None
            for attempt in range(10):
                result_data = await fetch_real_result(issue_number)
                if result_data:
                    break
                await asyncio.sleep(2)

            if not result_data:
                await app.bot.send_message(chat_id=chat_id, text="⚠️ Result not found. Skipping this round.")
                continue

            # Extract actual results
            number = int(result_data["number"])
            color = result_data["color"].split(",")[0].capitalize()
            real_bigsmall = "Big" if number >= 5 else "Small"
            real_redgreen = color

            # Get user prediction
            prediction, _ = user_predictions.get(str(chat_id), (("", "", []), ""))
            pred_bigsmall, pred_redgreen, pred_numbers = prediction

            bs_win = "✅" if real_bigsmall == pred_bigsmall else "❌"
            rg_win = "✅" if real_redgreen == pred_redgreen else "❌"
            num_win = "✅" if number in pred_numbers else "❌"

            win_count = sum([bs_win == "✅", rg_win == "✅", num_win == "✅"])
            is_win = win_count >= 2

            if str(chat_id) not in user_stats:
                user_stats[str(chat_id)] = {
        "bigsmall_win": 0, "bigsmall_lose": 0,
        "redgreen_win": 0, "redgreen_lose": 0,
        "number_win": 0, "number_lose": 0
    }

            stats = user_stats[str(chat_id)]
            stats["bigsmall_win" if bs_win == "✅" else "bigsmall_lose"] += 1
            stats["redgreen_win" if rg_win == "✅" else "redgreen_lose"] += 1
            stats["number_win" if num_win == "✅" else "number_lose"] += 1

            result_header = "✅ *Result Recorded!*"

            stats = user_stats[str(chat_id)]
            result_text = (
                f"{result_header}\n\n"
                f"📢 *Result for Issue:* `{issue_number}`\n"
                f"🔵 *Big/Small:* {real_bigsmall} {bs_win}\n"
                f"🟥 *Red/Green:* {real_redgreen} {rg_win}\n"
                f"🔢 *Winning Number:* {number} {num_win}\n\n"
                f"📊 *Your Stats:*\n"
                f"🔵 Big/Small: ✅ {stats.get('bigsmall_win', 0)} \n"
                f"🟥 Red/Green: ✅ {stats.get('redgreen_win', 0)} \n"
                f"🔢 Number: ✅ {stats.get('number_win', 0)} "
            )


            # Send result message
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=result_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard
                )
                print(f"[INFO] Sent result to user {chat_id}")
            except Exception as e:
                print(f"[ERROR] Failed to send result to user {chat_id}: {e}")

            # Short delay before next prediction loop
            await asyncio.sleep(2)

        except asyncio.CancelledError:
            print(f"Prediction task cancelled for user {chat_id}")
            break
        except Exception as e:
            print(f"[ERROR] Prediction loop failed for {chat_id}: {e}")
            await asyncio.sleep(5)


def get_user_stats_text(user_id_str):
    stats = user_stats.get(user_id_str)
    if not stats:
        return "📊 No stats available yet."

    return (
        f"📊 *Your Prediction Accuracy:*\n\n"
        f"🔵 Big/Small: ✅ {stats.get('bigsmall_win', 0)} | ❌ {stats.get('bigsmall_lose', 0)}\n"
        f"🟥 Red/Green: ✅ {stats.get('redgreen_win', 0)} | ❌ {stats.get('redgreen_lose', 0)}\n"
        f"🔢 Number: ✅ {stats.get('number_win', 0)} | ❌ {stats.get('number_lose', 0)}"
    )


def generate_random_password(length=8):
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(chars) for _ in range(length))

# --- Bot handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_authorized(user_id):
        await show_interval_menu(update)
    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📞 Contact Admin", url="https://t.me/Teacheravrilbot"),
            ],
            [
                InlineKeyboardButton("❓ How to Get Password", callback_data="how_to_get_password"),
            ],
        ])
        await update.message.reply_text(
            "🔒 You need a password to use this bot.\n\n"
            "Please deposit minimum *500rs* and send your deposit receipt to @Teacheravrilbot.\n\n"
            "Once you have the password, just send it here to access.",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_password, auth_data
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()
    if not is_authorized(user_id):
        if text == current_password:
            auth_data[user_id] = True
            save_auth(auth_data)
            await update.message.reply_text("✅ Access granted!")
            await show_interval_menu(update)
        else:
            await update.message.reply_text("❌ Wrong password. Try again:")

async def show_interval_menu(update: Update):
    await update.message.reply_text(
        "⏰ Please select the time interval to get all predictions:",
        reply_markup=interval_menu_keyboard(),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id_str = str(query.from_user.id)
    await query.answer()

    if not is_authorized(user_id_str):
        if query.data == "how_to_get_password":
            await query.edit_message_text(
                "❓ *How to Get Password*\n\n"
                "1️⃣ Deposit minimum *500rs*\n"
                "2️⃣ Send your deposit receipt to @Teacheravrilbot\n"
                "3️⃣ Receive password from admin\n"
                "4️⃣ Send password here to access the bot",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📞 Contact Admin", url="https://t.me/Teacheravrilbot")],
                ])
            )
        else:
            await query.edit_message_text("🔒 Please enter the access password first using /start")
        return

    data = query.data
    if data == "main_menu":
        # Cancel any existing periodic task for user
        if user_id_str in user_tasks:
            user_tasks[user_id_str].cancel()
            user_tasks.pop(user_id_str, None)

        await query.edit_message_text(
            "⏰ Please select the time interval to get all predictions:",
            reply_markup=interval_menu_keyboard(),
        )
        return

    if data.startswith("interval_"):
        interval = data.replace("interval_", "")
        seconds = interval_to_seconds(interval)

        # Cancel existing task if any
        if user_id_str in user_tasks:
            user_tasks[user_id_str].cancel()

        # Start new periodic sending task
        task = asyncio.create_task(send_prediction_realtime(context.application, int(user_id_str), seconds))
        user_tasks[user_id_str] = task

        await query.edit_message_text(
            f"🎯 Started sending predictions every *{interval}*.\n\nYou'll get new predictions here automatically.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data == "show_stats":
        stats_text = get_user_stats_text(user_id_str)
        await query.edit_message_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await query.edit_message_text("❌ Unknown option selected.")

async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_authorized(user_id):
        await update.message.reply_text("🔒 Please enter the access password first.")
    else:
        await update.message.reply_text("❓ Please use the buttons to select the time interval.")

async def setpassword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_password, auth_data, user_tasks

    user_id = update.effective_user.id
    if user_id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ You are not authorized to change the password.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /setpassword <new_password>")
        return

    new_password = args[0].strip()
    if len(new_password) < 4:
        await update.message.reply_text("Password too short! Use at least 4 characters.")
        return

    current_password = new_password
    save_password(new_password)

    # Kick all users
    auth_data.clear()
    save_auth(auth_data)

    # Cancel all running user tasks
    for t in user_tasks.values():
        t.cancel()
    user_tasks.clear()

    await update.message.reply_text(
        f"✅ Password changed successfully to: `{new_password}`\n\n"
        "⚠️ All members have been logged out and need to re-enter the new password.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def daily_password_reset_task(app):
    global current_password, auth_data, user_tasks

    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    while True:
        now_ist = datetime.datetime.now(IST)

        target_time = now_ist.replace(hour=11, minute=30, second=0, microsecond=0)
        if now_ist > target_time:
            # Already past 11:30 today, schedule for next day
            target_time += datetime.timedelta(days=1)

        wait_seconds = (target_time - now_ist).total_seconds()
        print(f"[PASSWORD RESET] Sleeping {wait_seconds} seconds until next reset at 11:30 AM IST")
        await asyncio.sleep(wait_seconds)

        # Generate new password and reset users as you already do
        new_password = generate_random_password(8)
        current_password = new_password
        save_password(new_password)

        auth_data.clear()
        save_auth(auth_data)

        for task in user_tasks.values():
            task.cancel()
        user_tasks.clear()

        # Notify admin about new password
        try:
            await app.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=f"🔔 Daily password has been reset automatically.\n\nNew password: `{new_password}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            print(f"Failed to notify admin: {e}")

# --- Main entry point ---
if __name__ == "__main__":
    import nest_asyncio

    nest_asyncio.apply()  # To allow nested event loops if running interactively

    auth_data = load_auth()
    current_password = load_password()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setpassword", setpassword))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    print("Bot started...")

    async def main():
        # Start the background daily reset task
        asyncio.create_task(daily_password_reset_task(app))

        # Start polling
        await app.run_polling()

    nest_asyncio.apply()
asyncio.get_event_loop().run_until_complete(main())
