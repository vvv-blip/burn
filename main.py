import os
import asyncio
import json
import base64
import logging
import threading
import itertools
import sys
from typing import Optional
from urllib.parse import urljoin

from solana.rpc.api import Client
import websockets
from solders.pubkey import Pubkey
from solders.rpc.config import RpcTransactionConfig

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, CallbackContext, Dispatcher

from firebase_admin import credentials, firestore, initialize_app
from flask import Flask, request
from waitress import serve

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=<YOUR_HELIUS_API_KEY>")
SOLANA_WS_URL = os.getenv("SOLANA_WS_URL", "wss://mainnet.helius-rpc.com/?api-key=<YOUR_HELIUS_API_KEY>")
TOKEN_MINT_ADDRESS_STR = os.getenv("TOKEN_MINT_ADDRESS")
FIREBASE_SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://solana-burn-monitor.onrender.com")

# Constants
SPL_TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
WEBHOOK_PATH = "/webhook"

# Global Variables
db = None  # Firestore client
bot = None  # Telegram bot instance
dispatcher = None  # Telegram Dispatcher instance
TOKEN_MINT_ADDRESS = None  # Parsed Pubkey of the token mint
TOKEN_DECIMALS = None  # Number of decimals for the token
TOTAL_BURNED_AMOUNT_KEY = "default_total_burned_token"

total_burned_lock = threading.Lock()
_request_id_counter = itertools.count(1)

# Flask app instance
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    logger.info("Health check endpoint accessed")
    return "Bot is running and healthy!", 200

@app_flask.route('/setwebhook')
def set_webhook_manual():
    """Manual webhook set trigger for debugging."""
    global bot
    try:
        webhook_url = urljoin(RENDER_EXTERNAL_URL, WEBHOOK_PATH)
        result = asyncio.run(bot.set_webhook(url=webhook_url))
        logger.info(f"Manual webhook set to {webhook_url}: {result}")
        return f"Webhook set to {webhook_url}: {result}", 200
    except Exception as e:
        logger.error(f"Error setting webhook manually: {e}")
        return f"Failed: {e}", 500

@app_flask.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    """Handle Telegram webhook updates."""
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        logger.info(f"Received webhook update: {update}")
        dispatcher.process_update(update)
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return "Error", 500

# Firebase Functions
def initialize_firebase():
    global db
    logger.info("Attempting to initialize Firebase")
    if not FIREBASE_SERVICE_ACCOUNT_JSON_BASE64:
        logger.warning("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64 not set. Firebase will not be initialized.")
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

async def get_total_burned_amount() -> float:
    logger.info("Fetching total burned amount")
    if not db:
        logger.warning("Firestore not initialized. Returning 0.0 for total burned amount.")
        return 0.0
    try:
        doc_ref = db.collection("token_burn_stats").document(TOTAL_BURNED_AMOUNT_KEY)
        doc = await asyncio.to_thread(doc_ref.get)
        if doc.exists:
            amount = doc.to_dict().get("total_burned_amount", 0.0)
            if isinstance(amount, (int, float)):
                logger.info(f"Retrieved total burned amount: {amount}")
                return float(amount)
            else:
                logger.warning(f"Invalid total burned amount in Firestore: {amount}. Returning 0.0.")
                return 0.0
        logger.info(f"Total burned amount document '{TOTAL_BURNED_AMOUNT_KEY}' not found. Returning 0.0.")
        return 0.0
    except Exception as e:
        logger.error(f"Error getting total burned amount: {e}")
        return 0.0

async def update_total_burned_amount(amount: float):
    logger.info(f"Updating total burned amount to {amount}")
    if not db:
        logger.warning("Firestore not initialized. Skipping update of total burned amount.")
        return
    try:
        doc_ref = db.collection("token_burn_stats").document(TOTAL_BURNED_AMOUNT_KEY)
        await asyncio.to_thread(doc_ref.set, {"total_burned_amount": amount})
        logger.info(f"Total burned amount updated to {amount}")
    except Exception as e:
        logger.error(f"Error updating total burned amount: {e}")

# Solana Functions (unchanged, omitted for brevity, use your original ones...)

# Telegram Functions
async def send_telegram_message(message: str):
    global bot
    logger.info(f"Attempting to send Telegram message: {message}")
    if not bot or not TELEGRAM_CHAT_ID:
        logger.error("Telegram Bot or Chat ID not initialized. Cannot send message.")
        return
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Telegram message sent to chat {TELEGRAM_CHAT_ID}: '{message}'")
    except TelegramError as e:
        logger.error(f"Error sending Telegram message: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending Telegram message: {e}")

def start_command(update: Update, context: CallbackContext):
    try:
        chat_type = update.message.chat.type
        chat_id = update.message.chat_id
        user = update.message.from_user
        logger.info(f"Received /start command from user @{user.username} (ID: {user.id}) in {chat_type} chat (ID: {chat_id})")
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
        async def send_total_burn():
            total_burned = await get_total_burned_amount()
            token_symbol = "JEWS"
            message = (
                f"🔥 *Burn Status!* 🔥\n\n"
                f"Total burned ${token_symbol}: *{total_burned:,.{TOKEN_DECIMALS if TOKEN_DECIMALS is not None else 0}f}* 🔥\n"
                f"Keep the flames roaring! 😼"
            )
            update.message.reply_text(message, parse_mode='Markdown')
        asyncio.run(send_total_burn())
    except Exception as e:
        logger.error(f"Error handling /totalburn command: {e}")

def setup_dispatcher(bot_instance):
    """Initialize the dispatcher (used for webhook mode)."""
    disp = Dispatcher(bot_instance, None, workers=0, use_context=True)
    disp.add_handler(CommandHandler("start", start_command))
    disp.add_handler(CommandHandler("help", help_command))
    disp.add_handler(CommandHandler("whomadethebot", whomadethebot_command))
    disp.add_handler(CommandHandler("totalburn", total_burn_command))
    return disp

async def init_bot_components():
    global bot, dispatcher, TOKEN_MINT_ADDRESS, TOKEN_DECIMALS, TOTAL_BURNED_AMOUNT_KEY
    logger.info("Starting bot component initialization")
    # Environment variable validation
    env_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "TOKEN_MINT_ADDRESS": TOKEN_MINT_ADDRESS_STR,
        "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64": FIREBASE_SERVICE_ACCOUNT_JSON_BASE64,
        "SOLANA_RPC_URL": SOLANA_RPC_URL,
        "SOLANA_WS_URL": SOLANA_WS_URL,
        "RENDER_EXTERNAL_URL": RENDER_EXTERNAL_URL
    }
    for key, value in env_vars.items():
        logger.info(f"Environment variable {key}: {'set' if value else 'not set'}")
    missing_vars = [key for key, value in env_vars.items() if not value]
    if missing_vars:
        logger.critical(f"Missing environment variables: {missing_vars}. Bot cannot start.")
        raise SystemExit(f"Missing environment variables: {missing_vars}")

    # Initialize Telegram bot and dispatcher
    logger.info("Initializing Telegram bot")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    bot_info = bot.get_me()
    logger.info(f"Telegram bot initialized as @{bot_info.username}")

    # Set the webhook
    logger.info("Setting up Telegram webhook")
    webhook_url = urljoin(RENDER_EXTERNAL_URL, WEBHOOK_PATH)
    bot.delete_webhook()
    result = bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}: {result}")

    # Firebase
    initialize_firebase()

    # Telegram dispatcher (for webhook mode)
    logger.info("Initializing Telegram Dispatcher")
    global dispatcher
    dispatcher = setup_dispatcher(bot)

    # Start Solana burn monitor (run in background)
    logger.info("Starting Solana burn monitoring")
    asyncio.create_task(monitor_burns())  # Ensure you copy your original monitor_burns() here

if __name__ == "__main__":
    logger.info("Starting main application")
    try:
        # Run async bot init before starting Flask
        asyncio.run(init_bot_components())
        port = int(os.getenv("PORT", 10000))
        logger.info(f"Starting Flask web service on port {port}")
        serve(app_flask, host="0.0.0.0", port=port)
    except Exception as e:
        logger.error(f"Error in main block: {e}", exc_info=True)
        raise
