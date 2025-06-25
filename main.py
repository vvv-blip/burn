import os
import asyncio
import json
import base64
import logging
import sys
from typing import Optional
from urllib.parse import urljoin

from solders.pubkey import Pubkey
from solana.rpc.api import Client

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, CallbackContext, Dispatcher

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from firebase_admin import credentials, firestore, initialize_app
from flask import Flask, request
from waitress import serve

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TOKEN_MINT_ADDRESS_STR = os.getenv("TOKEN_MINT_ADDRESS")
FIREBASE_SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://solana-burn-monitor.onrender.com")
WEBHOOK_PATH = "/webhook"
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", "9"))
BURN_ADDRESS = "11111111111111111111111111111111"

# Blockchain supply config
INITIAL_SUPPLY = 15800000  # <-- Your token's initial supply (change if needed)

db = None
bot = None
dispatcher = None

# Scheduler setup
scheduler = AsyncIOScheduler()
scheduler.start()
scheduled_job = None  # handle for the current scheduled job

app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    logger.info("Health check endpoint accessed")
    return "Bot is running and healthy!", 200

@app_flask.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher.process_update(update)
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return "Error", 500

# --- Blockchain-aware total burn API endpoint ---
@app_flask.route('/api/totalburn_blockchain', methods=['GET'])
def api_totalburn_blockchain():
    client = Client(SOLANA_RPC_URL)
    resp = client.get_token_supply(Pubkey.from_string(TOKEN_MINT_ADDRESS_STR))
    current_supply = int(resp.value.amount) / (10 ** resp.value.decimals)
    burned = INITIAL_SUPPLY - current_supply
    return {"total_burned_blockchain": burned}, 200

def initialize_firebase():
    global db
    logger.info("Attempting to initialize Firebase")
    if not FIREBASE_SERVICE_ACCOUNT_JSON_BASE64:
        logger.warning("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64 not set.")
        return False
    try:
        service_account_info = json.loads(base64.b64decode(FIREBASE_SERVICE_ACCOUNT_JSON_BASE64).decode('utf-8'))
        cred = credentials.Certificate(service_account_info)
        initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Error initializing Firebase: {e}")
        return False

async def send_telegram_message(message: str):
    global bot
    logger.info(f"Sending Telegram message: {message}")
    if not bot or not TELEGRAM_CHAT_ID:
        logger.error("Telegram Bot or Chat ID not initialized.")
        return
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')
        logger.info(f"Telegram message sent to chat {TELEGRAM_CHAT_ID}: '{message}'")
    except TelegramError as e:
        logger.error(f"Error sending Telegram message: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending Telegram message: {e}")

def start_command(update: Update, context: CallbackContext):
    try:
        # Send the GIF first
        context.bot.send_animation(
            chat_id=update.effective_chat.id,
            animation="https://media0.giphy.com/media/v1.Y2lkPTc5MGI3NjExeHRxNGFjd2VpZ2psbDkydWM5NXY5YmRsMnF3dnd6YnJzM3dmZG5peiZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/OMYKYMa53NVbKBWvJY/giphy.gif"
        )
        update.message.reply_text(
            "🔥 *Welcome to GOY ARMY $JEWS burner program*  🔥\n\n"
            "I monitor $JEWS burns on Solana..\n\n"
            "Commands:\n"
            "• `/totalburn`: View total $JEWS burned\n"
            "• `/setschedule HH:MM [daily|hourly]`: Schedule automatic total burn messages (admin only)\n"
            "• `/cancelschedule`: Cancel scheduled burn messages (admin only)\n\n"
            "*NIGGA HEIL HITLER - Kanye West (YE)* ",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error handling /start command: {e}")

def help_command(update: Update, context: CallbackContext):
    try:
        update.message.reply_text(
            "🔥 Solana Burn Monitor Bot Commands:\n\n"
            "• `/start`: Welcome message.\n"
            "• `/totalburn`: Blockchain-based total $JEWS burned.\n"
            "• `/setschedule HH:MM [daily|hourly]`: Schedule automatic total burn messages (admin only)\n"
            "• `/cancelschedule`: Cancel scheduled burn messages (admin only)\n"
            "• `/whomadethebot`: Bot creator.\n",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error handling /help command: {e}")

def whomadethebot_command(update: Update, context: CallbackContext):
    try:
        update.message.reply_text("@nakatroll")
    except Exception as e:
        logger.error(f"Error handling /whomadethebot command: {e}")

def total_burn_command(update: Update, context: CallbackContext):
    try:
        client = Client(SOLANA_RPC_URL)
        resp = client.get_token_supply(Pubkey.from_string(TOKEN_MINT_ADDRESS_STR))
        current_supply = int(resp.value.amount) / (10 ** resp.value.decimals)
        burned = INITIAL_SUPPLY - current_supply
        token_symbol = "JEWS"
        message = (
            f"🔥 *Total $JEWS Burned* 🔥\n\n"
            f"🔥 ${token_symbol}: *{burned:,.2f}* 🔥\n"
            f"(since inception, on-chain)"
        )
        update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error handling /totalburn command: {e}")
        update.message.reply_text("Error fetching blockchain burn data.")

def is_admin(update: Update, context: CallbackContext):
    try:
        chat_member = context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return chat_member.status in ["administrator", "creator"]
    except Exception as e:
        logger.error(f"Failed to check admin status: {e}")
        return False

async def send_scheduled_totalburn():
    try:
        client = Client(SOLANA_RPC_URL)
        resp = client.get_token_supply(Pubkey.from_string(TOKEN_MINT_ADDRESS_STR))
        current_supply = int(resp.value.amount) / (10 ** resp.value.decimals)
        burned = INITIAL_SUPPLY - current_supply
        token_symbol = "JEWS"
        message = (
            f"🔥 *Total $JEWS Burned* 🔥\n\n"
            f"🔥 ${token_symbol}: *{burned:,.2f}* 🔥\n"
            f"(since inception, on-chain)"
        )
        await send_telegram_message(message)
    except Exception as e:
        logger.error(f"Error sending scheduled totalburn: {e}")

def setschedule_command(update: Update, context: CallbackContext):
    global scheduled_job
    if not is_admin(update, context):
        update.message.reply_text("Only admins can set the schedule!")
        return

    if len(context.args) < 1:
        update.message.reply_text("Usage: /setschedule HH:MM [daily|hourly]")
        return

    time_part = context.args[0]
    interval = context.args[1] if len(context.args) > 1 else "daily"

    try:
        if interval == "hourly":
            minute = int(time_part)
            hour = None
        else:
            hour, minute = map(int, time_part.split(":"))
    except Exception:
        update.message.reply_text("Invalid time format. Use HH:MM (e.g. 14:00) or [minute] for hourly.")
        return

    # Remove old job
    if scheduled_job:
        scheduled_job.remove()
    
    if interval == "hourly":
        trigger = CronTrigger(minute=minute)
        interval_text = f"every hour at minute {minute:02d}"
    else:
        trigger = CronTrigger(hour=hour, minute=minute)
        interval_text = f"every day at {hour:02d}:{minute:02d}"

    scheduled_job = scheduler.add_job(
        lambda: asyncio.create_task(send_scheduled_totalburn()),
        trigger=trigger,
        name="Scheduled Total Burn"
    )
    update.message.reply_text(f"Scheduled total burn report {interval_text}.")

def cancelschedule_command(update: Update, context: CallbackContext):
    global scheduled_job
    if not is_admin(update, context):
        update.message.reply_text("Only admins can cancel the schedule!")
        return
    if scheduled_job:
        scheduled_job.remove()
        scheduled_job = None
        update.message.reply_text("Scheduled total burn report cancelled.")
    else:
        update.message.reply_text("No scheduled report to cancel.")

def setup_dispatcher(bot_instance):
    disp = Dispatcher(bot_instance, None, workers=0, use_context=True)
    disp.add_handler(CommandHandler("start", start_command))
    disp.add_handler(CommandHandler("help", help_command))
    disp.add_handler(CommandHandler("whomadethebot", whomadethebot_command))
    disp.add_handler(CommandHandler("totalburn", total_burn_command))
    disp.add_handler(CommandHandler("setschedule", setschedule_command, pass_args=True))
    disp.add_handler(CommandHandler("cancelschedule", cancelschedule_command))
    return disp

# The following functions are retained for monitoring and storage,
# but /totalburn now always uses the blockchain, not historical tracked value.
def get_total_burned_amount_local():
    return 0.0  # Legacy, not used in /totalburn

def set_total_burned_amount_local(amount):
    pass  # Legacy, not used in /totalburn

async def monitor_burns():
    logger.info("monitor_burns task started")
    client = Client(SOLANA_RPC_URL)
    mint_pubkey = Pubkey.from_string(TOKEN_MINT_ADDRESS_STR)
    last_signature = None
    burned_total = get_total_burned_amount_local()
    logger.info(f"Monitoring burns for: {TOKEN_MINT_ADDRESS_STR}")

    while True:
        try:
            txs = client.get_signatures_for_address(mint_pubkey, limit=20)
            txs_list = txs.value

            new_burns = []
            for tx in txs_list:
                sig = tx.signature
                if sig == last_signature:
                    break
                tx_data = client.get_transaction(
                    sig,
                    encoding="jsonParsed",
                    max_supported_transaction_version=0
                )
                if tx_data.value is None or not isinstance(tx_data.value, dict):
                    continue
                # ---- BEGIN: Improved BurnChecked Detection ----
                try:
                    instructions = tx_data.value['transaction']['message']['instructions']
                    for instr in instructions:
                        if 'program' in instr and 'parsed' in instr:
                            if (
                                instr['program'] == 'spl-token'
                                and instr['parsed']['type'] == 'burnChecked'
                                and instr['parsed']['info']['mint'] == TOKEN_MINT_ADDRESS_STR
                            ):
                                burned = int(instr['parsed']['info']['amount']) / (10 ** TOKEN_DECIMALS)
                                new_burns.append((sig, burned))
                                burned_total += burned
                except Exception as e:
                    logger.error(f"Error parsing burnChecked instructions in tx {sig}: {e}")
                # ---- END: Improved BurnChecked Detection ----
            if txs_list:
                last_signature = txs_list[0].signature
            for sig, burned in reversed(new_burns):
                link = f"https://solscan.io/tx/{sig}"
                await send_telegram_message(
                    f"🔥 *$JEWS Burned!* 🔥\n\n"
                    f"Amount: *{burned:,.{TOKEN_DECIMALS}f}* $JEWS\n"
                    f"[View Transaction]({link})"
                )
                logger.info(f"Burn event: {burned} at {sig}")
            if new_burns:
                set_total_burned_amount_local(burned_total)
            await asyncio.sleep(45)
        except Exception as e:
            logger.error(f"Error in monitor_burns: {e}", exc_info=True)
            await asyncio.sleep(60)

async def init_bot_components():
    global bot, dispatcher
    logger.info("Starting bot component initialization")
    env_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "TOKEN_MINT_ADDRESS": TOKEN_MINT_ADDRESS_STR,
        "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64": FIREBASE_SERVICE_ACCOUNT_JSON_BASE64,
        "SOLANA_RPC_URL": SOLANA_RPC_URL,
        "RENDER_EXTERNAL_URL": RENDER_EXTERNAL_URL
    }
    for key, value in env_vars.items():
        logger.info(f"Environment variable {key}: {'set' if value else 'not set'}")
    missing_vars = [key for key, value in env_vars.items() if not value]
    if missing_vars:
        logger.critical(f"Missing environment variables: {missing_vars}. Bot cannot start.")
        raise SystemExit(f"Missing environment variables: {missing_vars}")

    logger.info("Initializing Telegram bot")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    bot_info = bot.get_me()
    logger.info(f"Telegram bot initialized as @{bot_info.username}")

    logger.info("Setting up Telegram webhook")
    webhook_url = urljoin(RENDER_EXTERNAL_URL, WEBHOOK_PATH)
    bot.delete_webhook()
    result = bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}: {result}")

    initialize_firebase()

    logger.info("Initializing Telegram Dispatcher")
    global dispatcher
    dispatcher = setup_dispatcher(bot)

    logger.info("Starting Solana burn monitoring")
    asyncio.create_task(monitor_burns())

if __name__ == "__main__":
    logger.info("Starting main application")
    try:
        asyncio.run(init_bot_components())
        port = int(os.getenv("PORT", 10000))
        logger.info(f"Starting Flask web service on port {port}")
        serve(app_flask, host="0.0.0.0", port=port)
    except Exception as e:
        logger.error(f"Error in main block: {e}", exc_info=True)
        raise
