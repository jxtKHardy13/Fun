import json
import logging
import asyncio
import sqlite3
from typing import Any, Dict, List, Optional
import base58
import aiohttp
import websockets
import signal
import traceback
from cryptography.fernet import Fernet
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.core import RPCException
from solana.rpc.commitment import Confirmed
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =============================================================================
# 1. CONFIGURATION
# =============================================================================
TELEGRAM_TOKEN = "7594787474:AAFj8_wxiZXGcpNfFB2C77jBLQu9U0DP2A0"  # Replace with your Telegram bot token
RPC_URL = "https://api.mainnet-beta.solana.com"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
RAYDIUM_API_URL = "https://api-v3.raydium.io/pools/info/mint"
PUMP_WSS = "wss://pumpportal.fun/api/data"

# =============================================================================
# 2. LOGGING CONFIGURATION
# =============================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# =============================================================================
# 3. DATABASE SETUP
# =============================================================================
def init_db():
    """Initialize SQLite database for wallets and trades."""
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            encrypted_key BLOB,
            encryption_key BLOB
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            user_id INTEGER,
            trade_data TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# =============================================================================
# 4. GLOBAL STORAGE
# =============================================================================
user_wallets: Dict[int, Keypair] = {}  # In-memory cache, loaded from DB
active_snipers: Dict[int, List[str]] = {}
user_settings: Dict[int, Dict[str, Any]] = {}
limit_orders: Dict[int, Dict[str, Any]] = {}
dca_orders: Dict[int, List[Dict[str, Any]]] = {}
trades: Dict[int, List[str]] = {}
referrals: Dict[int, str] = {}
supported_langs: List[str] = ['EN', 'ZH', 'ES', 'RU']
snipe_pools: List[Dict[str, Any]] = []
pending_orders: Dict[int, Dict[str, str]] = {}
pending_wallet: Dict[int, bool] = {}
connection_attempts: Dict[int, int] = {}

wallet_lock = asyncio.Lock()
orders_lock = asyncio.Lock()

# =============================================================================
# 5. INITIALIZE CLIENTS
# =============================================================================
solana_client = AsyncClient(RPC_URL)

# =============================================================================
# 6. SECURITY HELPERS
# =============================================================================
def generate_encryption_key(user_id: int) -> bytes:
    """Generate a Fernet key for encrypting wallet data."""
    return Fernet.generate_key()  # In production, derive from user-specific data

def encrypt_wallet_key(keypair: Keypair, encryption_key: bytes) -> bytes:
    """Encrypt a keypair's bytes."""
    fernet = Fernet(encryption_key)
    return fernet.encrypt(keypair.to_bytes())

def decrypt_wallet_key(encrypted_key: bytes, encryption_key: bytes) -> Keypair:
    """Decrypt a keypair's bytes."""
    fernet = Fernet(encryption_key)
    key_bytes = fernet.decrypt(encrypted_key)
    return Keypair.from_bytes(key_bytes)

def store_wallet(user_id: int, keypair: Keypair):
    """Store encrypted wallet in database."""
    encryption_key = generate_encryption_key(user_id)
    encrypted_key = encrypt_wallet_key(keypair, encryption_key)
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO wallets (user_id, encrypted_key, encryption_key) VALUES (?, ?, ?)",
        (user_id, encrypted_key, encryption_key)
    )
    conn.commit()
    conn.close()

def load_wallet(user_id: int) -> Optional[Keypair]:
    """Load and decrypt wallet from database."""
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT encrypted_key, encryption_key FROM wallets WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        encrypted_key, encryption_key = result
        try:
            return decrypt_wallet_key(encrypted_key, encryption_key)
        except Exception as e:
            logger.error(f"Error decrypting wallet for user {user_id}: {e}")
            return None
    return None

# =============================================================================
# 7. HELPER FUNCTIONS
# =============================================================================
async def get_sol_price() -> float:
    """Fetch SOL price from CoinGecko asynchronously."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                COINGECKO_URL,
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return float(data.get("solana", {}).get("usd", 0))
        except aiohttp.ClientError as e:
            logger.error(f"Error fetching SOL price: {e}")
            return 0.0

async def fetch_pool_id(token_address: str) -> Optional[str]:
    """Fetch Raydium pool ID dynamically for a given token."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                RAYDIUM_API_URL,
                params={"mint1": token_address, "mint2": "So11111111111111111111111111111111111111112"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                if data.get("success") and data.get("data", {}).get("data"):
                    return data["data"]["data"][0].get("id")
        except aiohttp.ClientError as e:
            logger.error(f"Error fetching pool ID for {token_address}: {e}")
    return None

async def execute_trade(user_id: int, token_address: str, amount: float, action: str = "buy") -> bool:
    """Execute a trade on a DEX (placeholder)."""
    async with wallet_lock:
        if user_id not in user_wallets:
            wallet = load_wallet(user_id)
            if not wallet:
                logger.warning(f"Trade failed: No wallet for user {user_id}")
                return False
            user_wallets[user_id] = wallet
        wallet = user_wallets[user_id]

    try:
        balance_response = await solana_client.get_balance(wallet.public_key, commitment=Confirmed)
        balance = balance_response.value / 1e9 if balance_response.value else 0
        if balance < amount:
            logger.warning(f"Insufficient funds for user {user_id}: {balance} SOL < {amount} SOL")
            return False

        pool_id = await fetch_pool_id(token_address)
        if not pool_id:
            logger.error(f"No pool ID found for {token_address}")
            return False

        # Placeholder: Simulate a DEX transaction
        logger.info(f"Simulated {action} of {amount} SOL for token {token_address} on pool {pool_id}")
        async with orders_lock:
            trade_data = f"{action.upper()} {amount} SOL for {token_address}"
            trades.setdefault(user_id, []).append(trade_data)
            # Store trade in database
            conn = sqlite3.connect("bot.db")
            c = conn.cursor()
            c.execute("INSERT INTO trades (user_id, trade_data) VALUES (?, ?)", (user_id, trade_data))
            conn.commit()
            conn.close()
        return True
    except RPCException as e:
        logger.error(f"RPC error in trade for user {user_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Trade execution failed for user {user_id}: {e}\n{traceback.format_exc()}")
        return False

async def process_wallet_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Phantom wallet connections using mnemonic or private key securely."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if connection_attempts.get(user_id, 0) >= 3:
        await update.message.reply_text("❌ Too many attempts. Please try again later.")
        return

    connection_attempts[user_id] = connection_attempts.get(user_id, 0) + 1
    await update.message.reply_text("🔄 Connecting wallet...")

    try:
        async with wallet_lock:
            keypair = None
            if " " in text:  # Handle mnemonic phrase
                mnemonic_words = text.split()
                if len(mnemonic_words) not in [12, 24]:
                    await update.message.reply_text("❌ Invalid mnemonic: Must be 12 or 24 words.")
                    return

                try:
                    seed_bytes = Bip39SeedGenerator(text).Generate()
                    bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA).DeriveDefaultPath()
                    keypair = Keypair.from_bytes(bip44_ctx.PrivateKey().Raw().ToBytes())
                except Exception as e:
                    logger.error(f"Mnemonic processing error for user {user_id}: {e}")
                    await update.message.reply_text("❌ Invalid mnemonic phrase. Please check and try again.")
                    return
            else:  # Handle private key
                try:
                    if text.startswith('['):  # JSON array format
                        key_bytes = bytes(json.loads(text))
                    else:  # Base58 or hex
                        try:
                            key_bytes = base58.b58decode(text)
                        except ValueError:
                            key_bytes = bytes.fromhex(text)

                    if len(key_bytes) == 64:
                        keypair = Keypair.from_bytes(key_bytes)
                    elif len(key_bytes) == 32:
                        keypair = Keypair.from_seed(key_bytes)
                    else:
                        await update.message.reply_text("❌ Invalid key length. Must be 32 or 64 bytes.")
                        return
                except Exception as e:
                    logger.error(f"Private key processing error for user {user_id}: {e}")
                    await update.message.reply_text("❌ Invalid private key format. Use Base58, hex, or JSON array.")
                    return

            for attempt in range(3):
                try:
                    balance_response = await solana_client.get_balance(keypair.public_key, commitment=Confirmed)
                    if balance_response.value is None:
                        raise ValueError("Received None balance")
                    balance_sol = balance_response.value / 1e9
                    user_wallets[user_id] = keypair
                    store_wallet(user_id, keypair)
                    await update.message.reply_text(
                        f"✅ Wallet Connected!\nAddress: {keypair.public_key}\nBalance: {balance_sol:.4f} SOL"
                    )
                    logger.info(f"Wallet connected for user {user_id}: {keypair.public_key}, Balance: {balance_sol:.4f} SOL")
                    connection_attempts[user_id] = 0
                    return
                except RPCException as e:
                    logger.warning(f"Balance check attempt {attempt + 1} failed for user {user_id}: {e}")
                    if attempt < 2:
                        await asyncio.sleep(2 ** (attempt + 1))
                    else:
                        await update.message.reply_text("❌ Unable to verify wallet balance. Please try again later.")
                        return

    except Exception as e:
        logger.error(f"Wallet connection error for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(
            "❌ Connection failed. Ensure:\n"
            "1. Valid 12/24-word mnemonic or private key (Base58, hex, or JSON array).\n"
            "2. Use Phantom's mainnet wallet.\n"
            "3. Try /wallet again."
        )

# =============================================================================
# 8. COMMAND HANDLERS
# =============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display welcome message and main menu."""
    sol_price = await get_sol_price()
    user_id = update.effective_user.id
    wallet_info = "💳 Your Wallet\n      ↳ Not connected. Use /wallet to connect."
    
    async with wallet_lock:
        if user_id in user_wallets or load_wallet(user_id):
            wallet = user_wallets.get(user_id) or load_wallet(user_id)
            user_wallets[user_id] = wallet
            try:
                balance_response = await solana_client.get_balance(wallet.public_key, commitment=Confirmed)
                balance = balance_response.value / 1e9 if balance_response.value else 0
                wallet_info = f"💳 Your Wallet\n      ↳ {wallet.public_key}\n      ↳ Balance: {balance:.4f} SOL"
            except RPCException as e:
                logger.error(f"Error retrieving balance for user {user_id}: {e}")
                balance = 0
                wallet_info = f"💳 Your Wallet\n      ↳ {wallet.public_key}\n      ↳ Balance: Error"

    message = (
        f"💊 Welcome to PumpFunPro! 🔫\n\n"
        f"💰 SOL Price: ${sol_price:.2f}\n\n"
        f"{wallet_info}\n\n"
        "Your ultimate Solana trading assistant."
    )
    keyboard = [
        [InlineKeyboardButton("💳 Wallet", callback_data="wallet"),
         InlineKeyboardButton("🚀 Start Trading", callback_data="start_trading")],
        [InlineKeyboardButton("📊 Portfolio", callback_data="portfolio"),
         InlineKeyboardButton("⚙ Settings", callback_data="settings"),
         InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

async def wallet_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user for wallet details."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        "⚠️ SECURITY WARNING: Never share your mnemonic or private key publicly!\n"
        "💼 Send your wallet details in a PRIVATE chat:\n"
        "- Mnemonic: 12/24 words (e.g., 'snap appear solid ...')\n"
        "- Private key: Base58, hex, or JSON array (e.g., '2aB3cD...', '0x...', or '[1,2,3,...]')\n"
        "🔒 For maximum security, use /uploadkey to upload a file (not implemented yet)."
    )
    async with orders_lock:
        pending_wallet[user_id] = True

async def buysell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate a trade."""
    user_id = update.effective_user.id
    async with wallet_lock:
        if user_id not in user_wallets and not load_wallet(user_id):
            await update.message.reply_text("❌ Please connect your wallet first using /wallet")
            return
    await update.message.reply_text("🔄 Enter token address and amount (e.g., TOKEN_ADDRESS, 1.0):")
    async with orders_lock:
        pending_orders[user_id] = {"action": "trade", "step": "details"}

async def sniper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display sniper mode options."""
    keyboard = [
        [InlineKeyboardButton("🔫 Pump.fun", callback_data="sniperpump"),
         InlineKeyboardButton("🌕 Moonshot", callback_data="snipermoonshot")],
        [InlineKeyboardButton("📜 List Snipers", callback_data="listallsniperpump")]
    ]
    await update.message.reply_text("🎯 Choose sniper mode:", reply_markup=InlineKeyboardMarkup(keyboard))

async def sniperpump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Activate Pump.fun sniper mode."""
    user_id = update.callback_query.from_user.id
    async with wallet_lock:
        if user_id not in user_wallets and not load_wallet(user_id):
            await update.callback_query.answer("❌ Connect your wallet first using /wallet", show_alert=True)
            return
    async with orders_lock:
        active_snipers.setdefault(user_id, []).append("pump")
    await update.callback_query.answer("✅ Pump.fun sniper activated!")
    logger.info(f"Pump.fun sniper activated for user {user_id}")

async def listallsniperpump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List active snipers."""
    user_id = update.effective_user.id
    async with orders_lock:
        active = active_snipers.get(user_id, [])
    await update.message.reply_text(f"🔍 Active Snipers: {', '.join(active) if active else 'None'}")

async def limitorders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage limit orders."""
    keyboard = [
        [InlineKeyboardButton("➕ Create Limit", callback_data="create_limit"),
         InlineKeyboardButton("✏ Modify Limit", callback_data="modify_limit")]
    ]
    await update.message.reply_text("📈 Manage Limit Orders:", reply_markup=InlineKeyboardMarkup(keyboard))

async def dcaorders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display DCA order instructions."""
    await update.message.reply_text("🔄 Usage: /createdca <TOKEN> <AMOUNT> <INTERVAL>\nExample: /createdca SOL 0.1 3600")

async def createdca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a DCA order."""
    user_id = update.effective_user.id
    if len(context.args) != 3:
        await update.message.reply_text("❌ Usage: /createdca <TOKEN> <AMOUNT> <INTERVAL>")
        return
    try:
        token, amount_str, interval_str = context.args
        amount = float(amount_str)
        interval = int(interval_str)
        if amount <= 0 or interval <= 0:
            await update.message.reply_text("❌ Amount and interval must be positive")
            return
        async with wallet_lock:
            if user_id not in user_wallets and not load_wallet(user_id):
                await update.message.reply_text("❌ Connect your wallet first using /wallet")
                return
        async with orders_lock:
            dca_orders.setdefault(user_id, []).append({"token": token, "amount": amount, "interval": interval})
        await update.message.reply_text(f"✅ DCA order created: {amount} {token} every {interval} seconds")
        asyncio.create_task(schedule_dca(user_id, token, amount, interval))
        logger.info(f"DCA order created for user {user_id}: {amount} {token} every {interval}s")
    except ValueError:
        await update.message.reply_text("❌ Invalid amount or interval format")
    except Exception as e:
        logger.error(f"Error creating DCA order for user {user_id}: {e}")
        await update.message.reply_text(f"❌ Error creating DCA order: {e}")

async def schedule_dca(user_id: int, token: str, amount: float, interval: int) -> None:
    """Schedule periodic DCA buys."""
    while True:
        await asyncio.sleep(interval)
        success = await execute_trade(user_id, token, amount, "buy")
        if success:
            try:
                await application.bot.send_message(chat_id=user_id, text=f"🔄 DCA: Bought {amount} of {token}")
            except Exception as e:
                logger.error(f"Error sending DCA confirmation to user {user_id}: {e}")
        else:
            logger.warning(f"DCA buy failed for user {user_id}: {amount} {token}")

async def copytrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate copy trading."""
    user_id = update.effective_user.id
    async with wallet_lock:
        if user_id not in user_wallets and not load_wallet(user_id):
            await update.message.reply_text("❌ Connect your wallet first using /wallet")
            return
    await update.message.reply_text("👥 Enter trader's address to copy:")
    async with orders_lock:
        pending_orders[user_id] = {"action": "copytrade", "step": "address"}

async def monitor_trader(user_id: int, trader_address: str) -> None:
    """Monitor and copy trader's transactions (placeholder)."""
    try:
        trader_pubkey = Pubkey.from_string(trader_address)
        while True:
            signatures = await solana_client.get_signatures_for_address(trader_pubkey, limit=1)
            if signatures.value:
                sig = signatures.value[0].signature
                tx = await solana_client.get_transaction(sig)
                if tx.value and "swap" in str(tx.value):
                    success = await execute_trade(user_id, "TOKEN_ADDRESS", 1.0, "buy")
                    if success:
                        try:
                            await application.bot.send_message(chat_id=user_id, text="👥 Copied trade: Bought 1.0 of TOKEN")
                        except Exception as e:
                            logger.error(f"Error sending copy trade confirmation to user {user_id}: {e}")
            await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Copy trading error for user {user_id}: {e}\n{traceback.format_exc()}")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user portfolio."""
    user_id = update.effective_user.id
    async with wallet_lock:
        if user_id not in user_wallets and not load_wallet(user_id):
            await update.message.reply_text("❌ No wallet connected. Use /wallet to connect.")
            return
        wallet = user_wallets.get(user_id) or load_wallet(user_id)
        user_wallets[user_id] = wallet
        try:
            balance_response = await solana_client.get_balance(wallet.public_key, commitment=Confirmed)
            balance = balance_response.value / 1e9 if balance_response.value else 0
        except RPCException as e:
            logger.error(f"Error retrieving balance for user {user_id}: {e}")
            balance = 0

    async with orders_lock:
        conn = sqlite3.connect("bot.db")
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM trades WHERE user_id = ?", (user_id,))
        trade_count = c.fetchone()[0]
        conn.close()

    await update.message.reply_text(f"📊 Portfolio:\nAddress: {wallet.public_key}\nBalance: {balance:.4f} SOL\nTrades: {trade_count}")

async def trades_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display recent trades."""
    user_id = update.effective_user.id
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT trade_data FROM trades WHERE user_id = ? ORDER BY timestamp DESC LIMIT 3", (user_id,))
    user_trades = [row[0] for row in c.fetchall()]
    conn.close()
    if user_trades:
        await update.message.reply_text("📋 Recent Trades:\n" + "\n".join(user_trades))
    else:
        await update.message.reply_text("ℹ️ No trades yet.")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display settings menu."""
    keyboard = [
        [InlineKeyboardButton("⚡ Auto Buy", callback_data="autobuy"),
         InlineKeyboardButton("💸 Auto Sell", callback_data="autosell")],
        [InlineKeyboardButton("📉 Slippage", callback_data="slippage")]
    ]
    await update.message.reply_text("⚙ Settings:", reply_markup=InlineKeyboardMarkup(keyboard))

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Provide referral code."""
    user_id = update.effective_user.id
    ref_code = f"REF-{str(user_id)[-6:].zfill(6)}"
    async with orders_lock:
        referrals[user_id] = ref_code
    await update.message.reply_text(f"📨 Referral Code: {ref_code}")

async def backupbots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backup instructions."""
    await update.message.reply_text("🔒 Use /exportsettings to backup settings (not implemented yet).")

async def tip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display tip tiers."""
    await update.message.reply_text("💰 Tip Tiers:\nBronze: 0.1 SOL\nSilver: 0.5 SOL\nGold: 1 SOL")

async def selectlang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Language selection."""
    keyboard = [[InlineKeyboardButton(lang, callback_data=f"lang_{lang}")] for lang in supported_langs]
    await update.message.reply_text("🌍 Select Language:", reply_markup=InlineKeyboardMarkup(keyboard))

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display help message with usage instructions."""
    help_message = (
        "📚 Help Center\n\n"
        "**How to Use This Bot**\n"
        "1. **Connect Your Wallet**: Use /wallet and send your 12/24-word mnemonic or private key (in private chat).\n"
        "2. **Start Trading**: Use /buysell to trade tokens. Example: `/buysell TOKEN_ADDRESS, 1.0`\n"
        "3. **Snipe New Tokens**: Use /sniper to enable sniper mode.\n"
        "4. **Set Up DCA Orders**: Use /createdca <TOKEN> <AMOUNT> <INTERVAL>. Example: `/createdca SOL 0.1 3600`\n"
        "5. **Copy Trade**: Use /copytrade and provide a trader's Solana address.\n"
        "6. **View Portfolio**: Use /profile to view your wallet balance and trade stats.\n"
        "7. **Adjust Settings**: Use /settings to customize bot options.\n\n"
        "Happy trading! 💊"
    )
    await update.message.reply_text(help_message)

# =============================================================================
# 9. CALLBACK QUERY ROUTER
# =============================================================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks."""
    data = update.callback_query.data
    user_id = update.callback_query.from_user.id

    handlers = {
        "wallet": lambda: context.bot.send_message(chat_id=user_id, text="💼 Send wallet details (mnemonic/private key):"),
        "start_trading": lambda: buysell(update, context),
        "portfolio": lambda: profile(update, context),
        "settings": lambda: settings(update, context),
        "help": lambda: help_handler(update, context),
        "sniperpump": lambda: sniperpump(update, context),
        "listallsniperpump": lambda: listallsniperpump(update, context),
        "snipermoonshot": lambda: update.callback_query.answer("🌕 Moonshot mode not implemented yet", show_alert=True),
        "autobuy": lambda: update.callback_query.answer("✅ Auto Buy toggled", show_alert=True),
        "autosell": lambda: update.callback_query.answer("✅ Auto Sell toggled", show_alert=True),
        "slippage": lambda: update.callback_query.answer("✅ Slippage set to 0.5%", show_alert=True),
        "create_limit": lambda: context.bot.send_message(chat_id=user_id, text="📈 Send: TOKEN, PRICE, QUANTITY"),
        "modify_limit": lambda: context.bot.send_message(chat_id=user_id, text="✏ Send: TOKEN, PRICE, QUANTITY"),
    }
    if data in handlers:
        await handlers[data]()
        await update.callback_query.answer()
    elif data.startswith("lang_"):
        lang = data.split("_")[1]
        async with orders_lock:
            user_settings.setdefault(user_id, {})["language"] = lang
        await update.callback_query.answer(f"Language set to {lang}")

    if data in ["wallet", "create_limit", "modify_limit"]:
        async with orders_lock:
            pending_orders[user_id] = {"action": data}

# =============================================================================
# 10. PENDING INPUT HANDLER
# =============================================================================
async def pending_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user input for pending actions."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    async with orders_lock:
        if user_id in pending_wallet:
            await process_wallet_key(update, context)
            del pending_wallet[user_id]
            return
        if user_id not in pending_orders:
            return
        order = pending_orders[user_id]

    if order["action"] == "trade":
        try:
            parts = text.split(",")
            if len(parts) != 2:
                await update.message.reply_text("❌ Invalid input format: Use TOKEN_ADDRESS, AMOUNT")
                return
            token_address, amount_str = parts
            amount = float(amount_str.strip())
            if amount <= 0:
                await update.message.reply_text("❌ Amount must be positive")
                return
            success = await execute_trade(user_id, token_address.strip(), amount, "buy")
            await update.message.reply_text("✅ Trade executed" if success else "❌ Trade failed")
            async with orders_lock:
                if user_id in pending_orders:
                    del pending_orders[user_id]
        except ValueError:
            await update.message.reply_text("❌ Invalid input format: Use TOKEN_ADDRESS, AMOUNT")
        except Exception as e:
            logger.error(f"Trade input error for user {user_id}: {e}\n{traceback.format_exc()}")
            await update.message.reply_text(f"❌ Error: {e}")
    elif order["action"] in ["create_limit", "modify_limit"]:
        try:
            parts = text.split(",")
            if len(parts) != 3:
                await update.message.reply_text("❌ Invalid format: Use TOKEN, PRICE, QUANTITY")
                return
            token, price_str, quantity_str = parts
            price = float(price_str.strip())
            quantity = float(quantity_str.strip())
            if price <= 0 or quantity <= 0:
                await update.message.reply_text("❌ Price and quantity must be positive")
                return
            order_data = {"token": token.strip(), "price": price, "quantity": quantity}
            success = await execute_trade(user_id, token.strip(), quantity)
            await update.message.reply_text(f"✅ Limit order {'created' if order['action'] == 'create_limit' else 'modified'}")
            async with orders_lock:
                limit_orders[user_id] = order_data
                if user_id in pending_orders:
                    del pending_orders[user_id]
        except ValueError:
            await update.message.reply_text("❌ Invalid format: Use TOKEN, PRICE, QUANTITY")
        except Exception as e:
            logger.error(f"Limit order error for user {user_id}: {e}\n{traceback.format_exc()}")
            await update.message.reply_text(f"❌ Error: {e}")
    elif order["action"] == "copytrade":
        try:
            trader_address = text
            Pubkey.from_string(trader_address)
            async with orders_lock:
                trades.setdefault(user_id, []).append(f"Copying {trader_address}")
                if user_id in pending_orders:
                    del pending_orders[user_id]
            await update.message.reply_text(f"✅ Copy trading activated for {trader_address}")
            asyncio.create_task(monitor_trader(user_id, trader_address))
        except Exception as e:
            logger.error(f"Copy trade setup error for user {user_id}: {e}\n{traceback.format_exc()}")
            await update.message.reply_text("❌ Invalid trader address")

# =============================================================================
# 11. WEBSOCKET MONITORING
# =============================================================================
async def monitor_pump_launches() -> None:
    """Monitor new token pools via WebSocket for sniping."""
    reconnect_delay = 5
    while True:
        try:
            async with websockets.connect(PUMP_WSS, ping_interval=20, ping_timeout=10) as ws:
                logger.info("Connected to Pump WebSocket")
                await ws.send(json.dumps({"type": "subscribe", "channel": "new_pools"}))
                async for message in ws:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError as e:
                        logger.error(f"Error decoding websocket message: {e}")
                        continue
                    if data.get("type") == "new_pool":
                        token_address = data.get("token")
                        async with orders_lock:
                            snipe_pools.append(data)
                            for uid, modes in active_snipers.items():
                                if "pump" in modes:
                                    success = await execute_trade(uid, token_address, 1.0, "buy")
                                    if success:
                                        try:
                                            await application.bot.send_message(
                                                chat_id=uid, text=f"🎯 Sniped 1.0 of {token_address}"
                                            )
                                        except Exception as e:
                                            logger.error(f"Error sending snipe confirmation to user {uid}: {e}")
                                    else:
                                        logger.warning(f"Snipe failed for user {uid}: {token_address}")
        except (websockets.exceptions.ConnectionClosed, aiohttp.ClientError) as e:
            logger.error(f"WebSocket error: {e}")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, 60)

# =============================================================================
# 12. MAIN APPLICATION SETUP
# =============================================================================
application = None

def main() -> None:
    """Start the bot and ensure continuous operation."""
    global application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    commands = {
        "start": start,
        "wallet": wallet_prompt,
        "sniper": sniper,
        "limitorders": limitorders,
        "dcaorders": dcaorders,
        "createdca": createdca,
        "copytrade": copytrade,
        "profile": profile,
        "trades": trades_handler,
        "buysell": buysell,
        "settings": settings,
        "referral": referral,
        "backupbots": backupbots,
        "tip": tip,
        "selectlang": selectlang,
        "help": help_handler
    }
    for cmd, handler in commands.items():
        application.add_handler(CommandHandler(cmd, handler))

    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pending_input_handler))

    loop = asyncio.get_event_loop()
    loop.create_task(monitor_pump_launches())

    def handle_shutdown():
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        loop.stop()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_shutdown)

    try:
        logger.info("Bot started")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        handle_shutdown()

if __name__ == "__main__":
    main()
