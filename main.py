import os
import asyncio
import json
import base64
import logging
import threading
import itertools # Used for unique request IDs
from solana.rpc.api import Client
import websockets # Import websockets directly
# Removed: from solana.rpc.websocket_api import logs_subscribe
from solders.pubkey import Pubkey
from solders.rpc.config import RpcTransactionConfig, RpcTransactionLogsConfig

# Changed: Only import what's directly needed for telegram.ext
from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from firebase_admin import credentials, firestore, initialize_app

# --- Set up logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Define a logger instance

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_WS_URL = os.getenv("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com")
TOKEN_MINT_ADDRESS_STR = os.getenv("TOKEN_MINT_ADDRESS")
FIREBASE_SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64")

# --- Constants ---
SPL_TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

# --- Global Variables ---
db = None  # Firestore client
bot = None  # Telegram bot instance
application = None # Telegram Application instance for command handlers
TOKEN_MINT_ADDRESS = None  # Parsed Pubkey of the token mint
TOKEN_DECIMALS = None  # Number of decimals for the token
TOTAL_BURNED_AMOUNT_KEY = "default_total_burned_token"

total_burned_lock = threading.Lock()
# Counter for WebSocket JSON-RPC request IDs
_request_id_counter = itertools.count(1)

# --- Firebase Functions ---
def initialize_firebase():
    """Initializes the Firebase Admin SDK for Firestore access."""
    global db
    if not FIREBASE_SERVICE_ACCOUNT_JSON_BASE64:
        logger.error("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64 environment variable not set. Firebase cannot be initialized.")
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
    """
    Retrieves the total burned amount from Firestore.
    Returns 0.0 if not found or on error.
    """
    if not db:
        logger.error("Firestore not initialized. Cannot retrieve total burned amount.")
        return 0.0
    try:
        doc_ref = db.collection("token_burn_stats").document(TOTAL_BURNED_AMOUNT_KEY)
        doc = await asyncio.to_thread(doc_ref.get)
        if doc.exists:
            amount = doc.to_dict().get("total_burned_amount", 0.0)
            if isinstance(amount, (int, float)):
                return float(amount)
            else:
                try:
                    return float(str(amount))
                except ValueError:
                    logger.warning(f"Total burned amount in Firestore is not a valid number: {amount}. Returning 0.0.")
                    return 0.0
        logger.info(f"Total burned amount document '{TOTAL_BURNED_AMOUNT_KEY}' not found in Firestore. Initializing to 0.0.")
        return 0.0
    except Exception as e:
        logger.error(f"Error getting total burned amount from Firestore: {e}")
        return 0.0

async def update_total_burned_amount(amount: float):
    """
    Updates the total burned amount in Firestore.
    Stores the amount as a float.
    """
    if not db:
        logger.error("Firestore not initialized. Cannot update total burned amount.")
        return
    try:
        doc_ref = db.collection("token_burn_stats").document(TOTAL_BURNED_AMOUNT_KEY)
        await asyncio.to_thread(doc_ref.set, {"total_burned_amount": amount})
        logger.info(f"Total burned amount in Firestore updated to {amount}")
    except Exception as e:
        logger.error(f"Error updating total burned amount in Firestore: {e}")

# --- Solana Functions ---
async def get_token_decimals(solana_client: Client, mint_address: Pubkey) -> int | None:
    """
    Fetches the number of decimals for a given token mint address.
    """
    try:
        response = await asyncio.to_thread(solana_client.get_account_info, mint_address, encoding="jsonParsed")
        if response and response.value:
            mint_data = response.value.data.parsed["info"]
            if mint_data["type"] == "mint":
                decimals = mint_data["decimals"]
                logger.info(f"Token decimals for {mint_address} found: {decimals}")
                return decimals
        logger.warning(f"Could not find decimals for mint address: {mint_address}. Account info response: {response}")
        return None
    except Exception as e:
        logger.error(f"Error fetching token decimals for {mint_address}: {e}")
        return None

async def process_solana_transaction(solana_client: Client, signature: str):
    """
    Fetches a Solana transaction by signature, parses it, and processes any detected burn events.
    """
    global TOKEN_DECIMALS, TOKEN_MINT_ADDRESS_STR

    if TOKEN_DECIMALS is None or TOKEN_MINT_ADDRESS_STR is None:
        logger.warning("Token decimals or mint address not initialized. Skipping transaction processing.")
        return

    try:
        config = RpcTransactionConfig(
            encoding="jsonParsed",
            max_supported_transaction_version=0,
            rewards=False,
            commitment="confirmed"
        )
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
                if instruction.parsed.get('type') in ['burn', 'burnChecked'] and \
                   parsed_info.get('mint') == TOKEN_MINT_ADDRESS_STR:
                    burned_amount_raw = int(parsed_info.get('amount', 0))
                    is_burn_event = True
                    break

        if not is_burn_event and tx.meta and tx.meta.inner_instructions:
            for inner_instruction_list in tx.meta.inner_instructions:
                for instruction in inner_instruction_list.instructions:
                    if hasattr(instruction, 'parsed') and instruction.program_id == SPL_TOKEN_PROGRAM_ID:
                        parsed_info = instruction.parsed.get('info', {})
                        if instruction.parsed.get('type') in ['burn', 'burnChecked'] and \
                           parsed_info.get('mint') == TOKEN_MINT_ADDRESS_STR:
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
    """
    Monitors the Solana blockchain for burn events using WebSocket subscription to logs.
    """
    global TOKEN_DECIMALS, TOKEN_MINT_ADDRESS, TOTAL_BURNED_AMOUNT_KEY

    if TOKEN_MINT_ADDRESS_STR:
        try:
            TOKEN_MINT_ADDRESS = Pubkey.from_string(TOKEN_MINT_ADDRESS_STR)
            TOTAL_BURNED_AMOUNT_KEY = f"total_burned_{TOKEN_MINT_ADDRESS_STR.lower()}"
        except Exception as e:
            logger.error(f"Invalid TOKEN_MINT_ADDRESS: {TOKEN_MINT_ADDRESS_STR}. Error: {e}")
            return
    else:
        logger.error("TOKEN_MINT_ADDRESS environment variable not set. Exiting monitoring.")
        return

    solana_client = Client(SOLANA_RPC_URL)

    TOKEN_DECIMALS = await get_token_decimals(solana_client, TOKEN_MINT_ADDRESS)
    if TOKEN_DECIMALS is None:
        logger.error(f"Failed to retrieve decimals for token mint {TOKEN_MINT_ADDRESS_STR}. Cannot monitor burns without decimals. Exiting.")
        return

    logger.info(f"Starting to monitor burn events for token: {TOKEN_MINT_ADDRESS_STR} (Decimals: {TOKEN_DECIMALS})")
    logger.info(f"Using Solana RPC: {SOLANA_RPC_URL} and WebSocket: {SOLANA_WS_URL}")

    while True:
        try:
            async with websockets.connect(SOLANA_WS_URL) as ws:
                # Manually construct the logsSubscribe JSON-RPC request
                subscribe_request = {
                    "jsonrpc": "2.0",
                    "id": next(_request_id_counter), # Unique ID for the request
                    "method": "logsSubscribe",
                    "params": [
                        {
                            "mentions": [str(SPL_TOKEN_PROGRAM_ID)] # Pubkey needs to be a string
                        },
                        {
                            "commitment": "confirmed"
                        }
                    ]
                }
                await ws.send(json.dumps(subscribe_request))
                logger.info("Sent logsSubscribe request to WebSocket.")

                # Wait for the subscription confirmation response
                subscribe_response_raw = await ws.recv()
                subscribe_response = json.loads(subscribe_response_raw)
                if 'result' in subscribe_response and subscribe_response['result'] is not None:
                    logger.info(f"Received logsSubscribe confirmation: {subscribe_response['result']}")
                else:
                    logger.error(f"Failed to subscribe to logs: {subscribe_response.get('error', 'Unknown error')}")
                    # If subscription fails, close and retry connection
                    continue

                # Process incoming WebSocket messages (logs)
                async for msg_raw in ws:
                    try:
                        msg_data = json.loads(msg_raw)
                        if msg_data and 'params' in msg_data and 'result' in msg_data['params'] and 'value' in msg_data['params']['result']:
                            log_info = msg_data['params']['result']['value']
                            signature = log_info['signature']
                            logs = log_info['logs']
                            err = log_info['err']

                            if err:
                                logger.warning(f"Transaction {signature} failed with error: {err}. Skipping.")
                                continue

                            is_burn_log = False
                            for log in logs:
                                if "Program log: Instruction: Burn" in log or "Program log: Instruction: BurnChecked" in log:
                                    is_burn_log = True
                                    break
                            
                            if is_burn_log:
                                logger.info(f"Detected potential burn log in transaction {signature}. Fetching full transaction details.")
                                await process_solana_transaction(solana_client, signature)
                            else:
                                logger.debug(f"Skipping non-burn related logs for signature: {signature}")

                    except json.JSONDecodeError as jde:
                        logger.error(f"Failed to decode WebSocket message JSON: {jde}. Message: {msg_raw}")
                    except Exception as e:
                        logger.error(f"Error processing individual WebSocket message: {e}")
        except websockets.exceptions.ConnectionClosed as cc:
            logger.warning(f"WebSocket connection closed unexpectedly: {cc}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

# --- Telegram Functions ---
async def send_telegram_message(message: str):
    """
    Sends a message to the configured Telegram chat.
    """
    global bot
    if not bot:
        logger.error("Telegram Bot not initialized. Cannot send message.")
        return
    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID environment variable not set. Cannot send message.")
        return

    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Telegram message sent: '{message}'")
    except TelegramError as e:
        logger.error(f"Error sending Telegram message: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while sending Telegram message: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    await update.message.reply_text(
        "🔥 Welcome to the Solana Burn Monitor Bot! 🔥\n\n"
        "I will continuously monitor for token burns from the specified Solana contract address and notify this group.\n\n"
        "Here are some commands:\n"
        "• `/totalburn`: See the current total amount of $JEWS burned.\n"
        "• `/help`: Show a list of all commands.\n"
        "• `/whomadethebot`: Find out who crafted this bot.\n\n"
        "Let the flames begin! 🚀",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with the list of commands."""
    help_text = (
        "🔥 Here are the commands you can use with Solana Burn Monitor Bot:\n\n"
        "• `/start`: See the welcome message.\n"
        "• `/totalburn`: See the current total amount of $JEWS burned.\n"
        "• `/whomadethebot`: Find out who crafted this bot.\n"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def whomadethebot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /whomadethebot command."""
    await update.message.reply_text("@nakatroll")

async def total_burn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /totalburn command, showing the total amount of tokens burned."""
    total_burned = await get_total_burned_amount()
    
    token_symbol = "JEWS" 

    message = (
        f"🔥 *Burn Status!* 🔥\n\n"
        f"The total amount of burned ${token_symbol} is now: *{total_burned:,.{TOKEN_DECIMALS if TOKEN_DECIMALS is not None else 0}f}* 🔥\n"
        f"Keep those flames roaring! 😼"
    )
    await update.message.reply_text(message, parse_mode='Markdown')


async def run_bot():
    """
    Initializes and runs the Telegram bot.
    """
    global bot, application

    # Check for essential environment variables
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TOKEN_MINT_ADDRESS_STR, FIREBASE_SERVICE_ACCOUNT_JSON_BASE64]):
        logger.error("Missing one or more required environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TOKEN_MINT_ADDRESS, GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64). Please check your .env file or Render environment settings. Exiting.")
        return

    if not initialize_firebase():
        logger.error("Firebase initialization failed. Cannot proceed. Exiting.")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        bot_info = await bot.get_me()
        logger.info(f"Telegram bot initialized successfully as @{bot_info.username}")
    except TelegramError as e:
        logger.error(f"Failed to initialize Telegram bot. Check TELEGRAM_BOT_TOKEN validity: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error during Telegram bot initialization: {e}")
        return

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("whomadethebot", whomadethebot_command))
    application.add_handler(CommandHandler("totalburn", total_burn_command))

    asyncio.create_task(application.run_polling())
    logger.info("Telegram bot polling started.")

    await monitor_burns()


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually by KeyboardInterrupt.")
    except Exception as e:
        logger.critical(f"An unhandled critical error occurred, bot is stopping: {e}", exc_info=True)

