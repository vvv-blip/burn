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

db = None
bot = None
dispatcher = None

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
        update.message.reply_text(
            "🔥 Welcome to the Solana Burn Monitor Bot! 🔥\n\n"
            "I monitor token burns for $JEWS on Solana and notify the group.\n\n"
            "Commands:\n"
            "• `/totalburn`: View total $JEWS burned.\n"
            "• `/help`: List all commands.\n"
            "• `/whomadethebot`: Bot creator info.\n\n"
            "Let the flames begin! 🚀",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error handling /start command: {e}")

def help_command(update: Update, context: CallbackContext):
    try:
        update.message.reply_text(
            "🔥 Solana Burn Monitor Bot Commands:\n\n"
            "• `/start`: Welcome message.\n"
            "• `/totalburn`: Total $JEWS burned.\n"
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
        total_burned = get_total_burned_amount_local()
        token_symbol = "JEWS"
        message = (
            f"🔥 *Burn Status!* 🔥\n\n"
            f"Total burned ${token_symbol}: *{total_burned:,.{TOKEN_DECIMALS}f}* 🔥\n"
            f"Keep the flames roaring! 😼"
        )
        update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error handling /totalburn command: {e}")

def setup_dispatcher(bot_instance):
    disp = Dispatcher(bot_instance, None, workers=0, use_context=True)
    disp.add_handler(CommandHandler("start", start_command))
    disp.add_handler(CommandHandler("help", help_command))
    disp.add_handler(CommandHandler("whomadethebot", whomadethebot_command))
    disp.add_handler(CommandHandler("totalburn", total_burn_command))
    return disp

def get_total_burned_amount_local():
    try:
        if db:
            doc_ref = db.collection("token_burn_stats").document("default_total_burned_token")
            doc = doc_ref.get()
            if doc.exists:
                amount = doc.to_dict().get("total_burned_amount", 0.0)
                return float(amount)
        if os.path.exists("burned_amount.txt"):
            with open("burned_amount.txt") as f:
                return float(f.read().strip())
    except Exception as e:
        logger.error(f"Error getting total burned amount: {e}")
    return 0.0

def set_total_burned_amount_local(amount):
    try:
        if db:
            doc_ref = db.collection("token_burn_stats").document("default_total_burned_token")
            doc_ref.set({"total_burned_amount": amount})
        with open("burned_amount.txt", "w") as f:
            f.write(str(amount))
    except Exception as e:
        logger.error(f"Error saving total burned amount: {e}")

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
                tx_data = client.get_transaction(sig, encoding="jsonParsed")
                if tx_data.value is None:
                    continue
                meta = tx_data.value.get("meta")
                if meta is None or not meta.get("postTokenBalances"):
                    continue
                for i, balance in enumerate(meta["postTokenBalances"]):
                    if balance["mint"] == TOKEN_MINT_ADDRESS_STR:
                        owner = balance.get("owner")
                        pre_amount = int(meta["preTokenBalances"][i]["uiTokenAmount"]["amount"])
                        post_amount = int(balance["uiTokenAmount"]["amount"])
                        if owner == BURN_ADDRESS and post_amount < pre_amount:
                            burned = (pre_amount - post_amount) / 10**TOKEN_DECIMALS
                            new_burns.append((sig, burned))
                            burned_total += burned
            if txs_list:
                last_signature = txs_list[0].signature
            for sig, burned in reversed(new_burns):
                link = f"https://solscan.io/tx/{sig}"
                await send_telegram_message(
                    f"🔥 *Token Burned!* 🔥\n\n"
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
