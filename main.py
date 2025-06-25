import os
import asyncio
import json
import base64
import logging
import threading
import itertools
from typing import Optional

from solana.rpc.api import Client
import websockets
from solders.pubkey import Pubkey
from solders.rpc.config import RpcTransactionConfig

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from firebase_admin import credentials, firestore, initialize_app
from flask import Flask
from waitress import serve

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=<YOUR_HELIUS_API_KEY>")
SOLANA_WS_URL = os.getenv("SOLANA_WS_URL", "wss://mainnet.helius-rpc.com/?api-key=<YOUR_HELIUS_API_KEY>")
TOKEN_MINT_ADDRESS_STR = os.getenv("TOKEN_MINT_ADDRESS")
FIREBASE_SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64")

# Constants
SPL_TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

# Global Variables
db = None  # Firestore client
bot = None  # Telegram bot instance
application = None  # Telegram Application instance
TOKEN_MINT_ADDRESS = None  # Parsed Pubkey of the token mint
TOKEN_DECIMALS = None  # Number of decimals for the token
TOTAL_BURNED_AMOUNT_KEY = "default_total_burned_token"

total_burned_lock = threading.Lock()
_request_id_counter = itertools.count(1)

# Flask app instance
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    """Health check endpoint for Render."""
    return "Bot is running and healthy!", 200

# Firebase Functions
def initialize_firebase():
    """Initializes Firebase Admin SDK for Firestore."""
    global db
    if not FIREBASE_SERVICE_ACCOUNT_JSON_BASE64:
        logger.warning("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64 not set. Firebase will not be initialized.")
        return False
    try:
        service_account_info = json.loads(base64.b64decode(FIREBASE_SERVICE_ACCOUNT_JSON_BASE64).decode('utf-8'))
        cred = credentials.Certificate(service_account_info)
        initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase initialized successfully.")
        return True
    except Exception as e:
        logger.error(f"Error initializing Firebase: {e}")
        return False

async def get_total_burned_amount() -> float:
    """Retrieves total burned amount from Firestore or returns 0.0 if unavailable."""
    if not db:
        logger.warning("Firestore not initialized. Returning 0.0 for total burned amount.")
        return 0.0
    try:
        doc_ref = db.collection("token_burn_stats").document(TOTAL_BURNED_AMOUNT_KEY)
        doc = await asyncio.to_thread(doc_ref.get)
        if doc.exists:
            amount = doc.to_dict().get("total_burned_amount", 0.0)
            if isinstance(amount, (int, float)):
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
    """Updates total burned amount in Firestore."""
    if not db:
        logger.warning("Firestore not initialized. Skipping update of total burned amount.")
        return
    try:
        doc_ref = db.collection("token_burn_stats").document(TOTAL_BURNED_AMOUNT_KEY)
        await asyncio.to_thread(doc_ref.set, {"total_burned_amount": amount})
        logger.info(f"Total burned amount updated to {amount}")
    except Exception as e:
        logger.error(f"Error updating total burned amount: {e}")

# Solana Functions
async def get_token_decimals(solana_client: Client, mint_address: Pubkey) -> Optional[int]:
    """Fetches token decimals for a mint address."""
    try:
        response = await asyncio.to_thread(solana_client.get_account_info, mint_address, encoding="jsonParsed")
        if response and response.value:
            mint_data = response.value.data.parsed["info"]
            if mint_data["type"] == "mint":
                decimals = mint_data["decimals"]
                logger.info(f"Token decimals for {mint_address}: {decimals}")
                return decimals
        logger.warning(f"Could not find decimals for mint address: {mint_address}")
        return None
    except Exception as e:
        logger.error(f"Error fetching token decimals for {mint_address}: {e}")
        return None

async def process_solana_transaction(solana_client: Client, signature: str):
    """Processes a Solana transaction to detect burn events."""
    global TOKEN_DECIMALS, TOKEN_MINT_ADDRESS_STR
    if TOKEN_DECIMALS is None or TOKEN_MINT_ADDRESS_STR is None:
        logger.warning("Token decimals or mint address not initialized. Skipping transaction.")
        return
    try:
        config = RpcTransactionConfig(encoding="jsonParsed", max_supported_transaction_version=0, commitment="confirmed")
        transaction_response = await asyncio.to_thread(solana_client.get_transaction, signature, config)
        if not transaction_response or not transaction_response.value:
            logger.warning(f"Could not fetch transaction details for signature: {signature}")
            return
        tx = transaction_response.value
        burned_amount_raw = 0
        is_burn_event = False
        for instruction in tx.transaction.message.instructions:
            if hasattr(instruction, 'parsed') and instruction.program_id == SPL_TOKEN_PROGRAM_ID:
                parsed_info = instruction.parsed.get('info', {})
                if instruction.parsed.get('type') in ['burn', 'burnChecked'] and parsed_info.get('mint') == TOKEN_MINT_ADDRESS_STR:
                    burned_amount_raw = int(parsed_info.get('amount', 0))
                    is_burn_event = True
                    break
        if not is_burn_event and tx.meta and tx.meta.inner_instructions:
            for inner_instruction_list in tx.meta.inner_instructions:
                for instruction in inner_instruction_list.instructions:
                    if hasattr(instruction, 'parsed') and instruction.program_id == SPL_TOKEN_PROGRAM_ID:
                        parsed_info = instruction.parsed.get('info', {})
                        if instruction.parsed.get('type') in ['burn', 'burnChecked'] and parsed_info.get('mint') == TOKEN_MINT_ADDRESS_STR:
                            burned_amount_raw = int(parsed_info.get('amount', 0))
                            is_burn_event = True
                            break
                if is_burn_event:
                    break
        if is_burn_event and burned_amount_raw > 0:
            burned_amount_readable = burned_amount_raw / (10**TOKEN_DECIMALS)
            logger.info(f"Detected burn of {burned_amount_readable} tokens (raw: {burned_amount_raw}). Signature: {signature}")
            with total_burned_lock:
                current_total_burned = await get_total_burned_amount()
                new_total_burned = current_total_burned + burned_amount_readable
                await update_total_burned_amount(new_total_burned)
            token_symbol = "JEWS"
            message = (
                f"🔥 Someone burned {burned_amount_readable:,.{TOKEN_DECIMALS}f} ${token_symbol}!\n"
                f"📊 Total amount of burned {token_symbol} is now {new_total_burned:,.{TOKEN_DECIMALS}f}"
            )
            await send_telegram_message(message)
        else:
            logger.debug(f"Transaction {signature} does not contain a relevant burn for {TOKEN_MINT_ADDRESS_STR}.")
    except Exception as e:
        logger.error(f"Error processing transaction {signature}: {e}")

async def monitor_burns():
    """Monitors Solana blockchain for burn events via WebSocket."""
    global TOKEN_DECIMALS, TOKEN_MINT_ADDRESS, TOTAL_BURNED_AMOUNT_KEY
    if TOKEN_MINT_ADDRESS_STR:
        try:
            TOKEN_MINT_ADDRESS = Pubkey.from_string(TOKEN_MINT_ADDRESS_STR)
            TOTAL_BURNED_AMOUNT_KEY = f"total_burned_{TOKEN_MINT_ADDRESS_STR.lower()}"
        except Exception as e:
            logger.error(f"Invalid TOKEN_MINT_ADDRESS: {TOKEN_MINT_ADDRESS_STR}. Error: {e}")
            return
    else:
        logger.error("TOKEN_MINT_ADDRESS not set. Exiting monitoring.")
        return
    solana_client = Client(SOLANA_RPC_URL)
    TOKEN_DECIMALS = await get_token_decimals(solana_client, TOKEN_MINT_ADDRESS)
    if TOKEN_DECIMALS is None:
        logger.error(f"Failed to retrieve decimals for token mint {TOKEN_MINT_ADDRESS_STR}. Exiting.")
        return
    logger.info(f"Monitoring burn events for token: {TOKEN_MINT_ADDRESS_STR} (Decimals: {TOKEN_DECIMALS})")
    while True:
        try:
            async with websockets.connect(SOLANA_WS_URL, ping_interval=20, ping_timeout=20, max_size=2**24) as ws:
                subscribe_request = {
                    "jsonrpc": "2.0",
                    "id": next(_request_id_counter),
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [str(SPL_TOKEN_PROGRAM_ID)]},
                        {"commitment": "confirmed"}
                    ]
                }
                await ws.send(json.dumps(subscribe_request))
                logger.info("Sent logsSubscribe request to WebSocket.")
                subscribe_response_raw = await ws.recv()
                subscribe_response = json.loads(subscribe_response_raw)
                if 'result' in subscribe_response and subscribe_response['result'] is not None:
                    logger.info(f"Logs subscription confirmed: {subscribe_response['result']}")
                else:
                    logger.error(f"Failed to subscribe to logs: {subscribe_response.get('error', 'Unknown error')}")
                    continue
                async for msg_raw in ws:
                    try:
                        msg_data = json.loads(msg_raw)
                        if 'params' in msg_data and 'result' in msg_data['params'] and 'value' in msg_data['params']['result']:
                            log_info = msg_data['params']['result']['value']
                            signature = log_info['signature']
                            logs = log_info['logs']
                            err = log_info['err']
                            if err:
                                logger.warning(f"Transaction {signature} failed: {err}. Skipping.")
                                continue
                            is_burn_log = any("Program log: Instruction: Burn" in log or "Program log: Instruction: BurnChecked" in log for log in logs)
                            if is_burn_log:
                                logger.info(f"Detected potential burn in transaction {signature}. Fetching details.")
                                await process_solana_transaction(solana_client, signature)
                            else:
                                logger.debug(f"Skipping non-burn logs for signature: {signature}")
                    except json.JSONDecodeError as jde:
                        logger.error(f"Failed to decode WebSocket message: {jde}. Message: {msg_raw}")
                    except Exception as e:
                        logger.error(f"Error processing WebSocket message: {e}")
        except websockets.exceptions.ConnectionClosed as cc:
            logger.warning(f"WebSocket closed: {cc}. Reconnecting in 10 seconds...")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in 10 seconds...")
            await asyncio.sleep(10)

# Telegram Functions
async def send_telegram_message(message: str):
    """Sends a message to the Telegram chat isotherms    global bot
    if not bot or not TELEGRAM_CHAT_ID:
        logger.error("Telegram Bot or Chat ID not initialized. Cannot send message.")
        return
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Telegram message sent: '{message}'")
    except TelegramError as e:
        logger.error(f"Error sending Telegram message: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending Telegram message: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command."""
    await update.message.reply_text(
        "🔥 Welcome to the Solana Burn Monitor Bot! 🔥\n\n"
        "I monitor token burns for $JEWS on Solana and notify this group.\n\n"
        "Commands:\n"
        "• `/totalburn`: View total $JEWS burned.\n"
        "• `/help`: List all commands.\n"
        "• `/whomadethebot`: Bot creator info.\n\n"
        "Let the flames begin! 🚀",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /help command."""
    await update.message.reply_text(
        "🔥 Solana Burn Monitor Bot Commands:\n\n"
        "• `/start`: Welcome message.\n"
        "• `/totalburn`: Total $JEWS burned.\n"
        "• `/whomadethebot`: Bot creator.\n",
        parse_mode='Markdown'
    )

async def whomadethebot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /whomadethebot command."""
    await update.message.reply_text("@nakatroll")

async def total_burn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /totalburn command."""
    total_burned = await get_total_burned_amount()
    token_symbol = "JEWS"
    message = (
        f"🔥 *Burn Status!* 🔥\n\n"
        f"Total burned ${token_symbol}: *{total_burned:,.{TOKEN_DECIMALS if TOKEN_DECIMALS is not None else 0}f}* 🔥\n"
        f"Keep the flames roaring! 😼"
    )
    await update.message.reply_text(message, parse_mode='Markdown')

async def init_bot_components():
    """Initializes Telegram bot and starts async tasks."""
    global bot, application
    # Enhanced environment variable validation
    env_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "TOKEN_MINT_ADDRESS": TOKEN_MINT_ADDRESS_STR,
        "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64": FIREBASE_SERVICE_ACCOUNT_JSON_BASE64,
        "SOLANA_RPC_URL": SOLANA_RPC_URL,
        "SOLANA_WS_URL": SOLANA_WS_URL
    }
    missing_vars = [key for key, value in env_vars.items() if not value]
    if missing_vars:
        logger.critical(f"Missing environment variables: {missing_vars}. Bot cannot start.")
        raise SystemExit(f"Missing environment variables: {missing_vars}")
    for key, value in env_vars.items():
        logger.info(f"Environment variable {key}: {'set' if value else 'not set'}")
    
    # Initialize Firebase (optional)
    initialize_firebase()
    
    # Initialize Telegram bot
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        bot_info = await bot.get_me()
        logger.info(f"Telegram bot initialized as @{bot_info.username}")
    except TelegramError as e:
        logger.error(f"Failed to initialize Telegram bot: {e}")
        raise SystemExit("Telegram bot initialization failed")
    
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("whomadethebot", whomadethebot_command))
    application.add_handler(CommandHandler("totalburn", total_burn_command))
    
    asyncio.create_task(application.run_polling())
    logger.info("Telegram bot polling started.")
    asyncio.create_task(monitor_burns())
    logger.info("Solana burn monitoring started.")

if __name__ == "__main__":
    logger.info("Starting Flask web service and bot tasks.")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.create_task(init_bot_components())
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Flask web service starting on port {port}")
    serve(app_flask, host="0.0.0.0", port=port)
