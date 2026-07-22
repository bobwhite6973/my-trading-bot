#!/usr/bin/env python3
"""
Trading Bot — Full Dashboard
CEX mode: API key trading on Binance/Bybit/OKX/KuCoin/LBank
DEX mode: Wallet-based trading via Uniswap/1inch on any EVM chain
Price feeds: Kraken (no key needed)
Strategies: DCA, Grid, Scalping, Copy Trading, Arbitrage
"""
import os, json, time, hmac, hashlib, threading, requests, logging, base64, random, string
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
os.environ["TZ"] = "US/Eastern"
time.tzset()

logging.basicConfig(level=logging.WARNING)
TOKEN_DECIMALS = {"USDC": 6, "USDT": 6, "SOL": 9, "BTC": 8, "ETH": 8, "JUP": 6, "BONK": 5, "WIF": 6, "SPCX": 6}

# ── Config from environment ───────────────────────────────────────────────────
cfg = {
    # CEX
    "api_key":      os.environ.get("API_KEY", ""),
    "api_secret":   os.environ.get("API_SECRET", ""),
    "exchange":     os.environ.get("EXCHANGE", "bybit"),
    # DEX/EVM
    "wallet":       os.environ.get("WALLET_ADDRESS", ""),
    "private_key":  os.environ.get("PRIVATE_KEY", ""),
    # Solana
    "sol_wallet":   os.environ.get("SOL_WALLET_ADDRESS", ""),
    "sol_key":      os.environ.get("SOL_PRIVATE_KEY", ""),
    # Trading
    "pair":         os.environ.get("TRADING_PAIR", "SOL/USDC"),
    "risk_pct":     float(os.environ.get("RISK_PCT", "2")),
    "stop_loss":    float(os.environ.get("STOP_LOSS_PCT", "5")),
    "take_profit":  float(os.environ.get("TAKE_PROFIT_PCT", "15")),
    "max_pos":      float(os.environ.get("MAX_POSITION_USD", "500")),
    "max_loss":     float(os.environ.get("MAX_DAILY_LOSS_USD", "200")),
    "source_wallet":os.environ.get("SOURCE_WALLET", ""),
    # Safety — default to paper trading to avoid accidental live trades
    "min_arb_spread":  float(os.environ.get("MIN_ARB_SPREAD", "1.5")),
    "paper_trading":   os.environ.get("PAPER_TRADING", "true").lower() != "false",
    "auto_compound":   os.environ.get("AUTO_COMPOUND", "true").lower() != "false",
    "partial_sell_pct":  max(1, min(99, float(os.environ.get("PARTIAL_SELL_PCT", "50")))),
    "tg_bot_token":    os.environ.get("TG_BOT_TOKEN", ""),
    "tg_chat_id":      os.environ.get("TG_CHAT_ID", ""),
}

# ── Bot State ─────────────────────────────────────────────────────────────────
state = {
    "running":       False,
    "strategy":      None,
    "mode":          None,
    "exchange":      cfg["exchange"],
    "chain":         "ethereum",
    "pair":          cfg["pair"],
    "price":         0.0,
    "balance":       0.0,
    "sol_balance":   0.0,
    "sol_usdc":      0.0,
    "sol_usdt":      0.0,
    "sol_native":    0.0,
    "positions":     [],
    "trades":        [],
    "pnl":           0.0,
    "daily_loss":    0.0,
    "log":           [],
    "error":         None,
    "arb_opps":      [],
    "paper_trading": cfg["paper_trading"],
    "trading_lock":  False,   # Prevent simultaneous trades
    "last_trade_time": 0,     # Cooldown between trades
    # Dashboard UI fields
    "paused":        False,
    "win_rate":      0,
    "avg_profit":    0.0,
    "trades_count":  0,
    
    "best_trade":    None,
    "trades_list":   [],
    "positions_list": [],
    "config":        {"max_leverage": 3, "max_position": 1000, "cooldown": 30, "slippage": 0.5},
    "last_trade":    None,
    "price_history": [],
    "grid_levels":   [],
    "grid_buy_zone": 0.0,
    "grid_filled":   {},
    "grid_trailing_active": False,
    "grid_trailing_high": 0.0,
    "grid_mid_idx": 0,
    "positions_count": 0,
    "compound_profit":  0.0,
    "partial_positions": {},
    "active_pairs":   [],
    "grid_pairs":     {},
    "daily_pnl":      0.0,
    "peak_balance":   0.0,
    "dip_active":     False,
    "dip_24h_high":   0.0,
    "last_midnight":  0,
    "emergency_stop":  False,
}

def send_telegram(msg):
    token = cfg.get("tg_bot_token", "")
    chat_id = cfg.get("tg_chat_id", "")
    if not token or not chat_id:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5)
        if r.status_code != 200:
            log("Telegram error "+str(r.status_code)+": "+r.text[:200], "WARN")
    except Exception as e:
        log("Telegram send failed: "+str(e), "WARN")

def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    entry = "["+ts+"] ["+level+"] "+msg
    print(entry)
    state["log"].insert(0, entry)
    if len(state["log"]) > 150:
        state["log"] = state["log"][:150]

# ── Price Feeds (Kraken — no API key needed) ──────────────────────────────────
KRAKEN_PAIRS = {
    "BTC/USDT": "XBTUSD", "ETH/USDT": "ETHUSD", "BNB/USDT": "BNBUSD",
    "SOL/USDT": "SOLUSD", "ARB/USDT": "ARBUSD", "MATIC/USDT": "MATICUSD",
    "AVAX/USDT": "AVAXUSD", "LINK/USDT": "LINKUSD", "UNI/USDT": "UNIUSD",
    "BTC/USDC": "XBTUSD", "ETH/USDC": "ETHUSD", "SOL/USDC": "SOLUSD",
    "BNB/USDC": "BNBUSD", "MATIC/USDC": "MATICUSD",
}

def get_price_kraken(pair):
    try:
        kraken_pair = KRAKEN_PAIRS.get(pair, pair.replace("/","").replace("USDT","USD").replace("USDC","USD"))
        r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": kraken_pair}, timeout=5)
        data = r.json()
        if not data.get("error"):
            result = data.get("result", {})
            key = list(result.keys())[0] if result else None
            if key:
                return float(result[key]["c"][0])
    except Exception as ex:
        log("Kraken price error: "+str(ex), "ERROR")
    return 0.0

def get_price_coingecko(token):
    try:
        ids = {"BTC":"bitcoin","ETH":"ethereum","BNB":"binancecoin","SOL":"solana","MATIC":"matic-network","ARB":"arbitrum"}
        cid = ids.get(token.split("/")[0], token.split("/")[0].lower())
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids":cid,"vs_currencies":"usd"}, timeout=5)
        data = r.json()
        return float(data.get(cid,{}).get("usd",0))
    except Exception as e:
        log("CoinGecko error: "+str(e), "WARN")
        return 0.0

def get_price_raydium(pair):
    """Get price from Raydium pool (matches execution price)."""
    try:
        token = pair.split("/")[0]
        token_upper = token.upper()
        usdc_mint = SOL_TOKENS.get("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        token_mint = SOL_TOKENS.get(token_upper)
        if not token_mint:
            return 0.0
        # Quote a small amount (0.01 token) to avoid liquidity issues, then scale
        decimals = TOKEN_DECIMALS.get(token_upper, 6)
        small_amount = 10 ** (decimals - 2)  # 0.01 of the token
        if small_amount < 1000:
            small_amount = 10 ** decimals  # fallback to 1 full unit for low-dec tokens
        quote = raydium_get_quote(token_mint, usdc_mint, small_amount, "100")
        if quote and quote.get("data") and quote["data"].get("outputAmount"):
            out = int(quote["data"]["outputAmount"])  # USDC units (6 decimals)
            # Scale up: output for 0.01 token * 100 = price for 1 token
            scale = (10 ** decimals) / small_amount
            price = (out / 10**6) * scale
            if price > 0:
                return price
    except Exception as ex:
        log("Raydium price error: "+str(ex), "WARN")
    return 0.0

def get_price(pair):
    price = get_price_raydium(pair)
    if price > 0:
        state["price"] = price
        return price
    price = get_price_kraken(pair)
    if price <= 0:
        price = get_price_coingecko(pair)
    state["price"] = price
    return price

# ── CEX Trading ───────────────────────────────────────────────────────────────
CEX_CONFIGS = {
    "binance": {"base":"https://api.binance.com","sign":"sha256"},
    "bybit":   {"base":"https://api.bybit.com","sign":"sha256"},
    "okx":     {"base":"https://www.okx.com","sign":"sha256"},
    "kraken":  {"base":"https://api.kraken.com","sign":"sha512"},
    "kucoin":  {"base":"https://api.kucoin.com","sign":"sha256"},
    "lbank":   {"base":"https://api.lbank.info","sign":"md5"},
}

# Reusable ccxt exchange instances to avoid reloading markets on every call
_cex_exchanges = {}

def _get_cex_exchange(name):
    """Get or create a cached ccxt exchange instance."""
    global _cex_exchanges
    if name not in _cex_exchanges:
        import ccxt
        opts = {'apiKey': cfg['api_key'], 'secret': cfg['api_secret']}
        if name == 'lbank':
            opts['options'] = {
                'createMarketBuyOrderRequiresPrice': False,
            }
        ex = getattr(ccxt, name)(opts)
        ex.load_markets()
        # Force LBank to use HmacSHA256 regardless of secret length
        if name == 'lbank':
            ex.options['createOrder'] = ex.options.get('createOrder', {})
            ex.options['createOrder']['method'] = 'spotPrivatePostSupplementCreateOrder'
        _cex_exchanges[name] = ex
    return _cex_exchanges[name]

def cex_get_balance():
    exchange = state["exchange"]
    # Rate-limit self-protection: don't check more than once per 60s per exchange
    now = time.time()
    last_check = state.get("_last_balance_check", {})
    last_time = last_check.get(exchange, 0)
    if now - last_time < 60:
        return state.get("balance", 0.0)
    last_check[exchange] = now
    state["_last_balance_check"] = last_check

    try:
        if exchange == "binance":
            ts = str(int(time.time()*1000))
            params = "timestamp="+ts
            sig = hmac.new(cfg["api_secret"].encode(), params.encode(), hashlib.sha256).hexdigest()
            r = requests.get("https://api.binance.com/api/v3/account",
                headers={"X-MBX-APIKEY": cfg["api_key"]},
                params={"timestamp":ts,"signature":sig}, timeout=5)
            data = r.json()
            for b in data.get("balances",[]):
                if b["asset"] == "USDT":
                    state["balance"] = float(b["free"])
                    return float(b["free"])
        elif exchange == "bybit":
            ts = str(int(time.time()*1000))
            params = "timestamp="+ts+"&api_key="+cfg["api_key"]
            sig = hmac.new(cfg["api_secret"].encode(), params.encode(), hashlib.sha256).hexdigest()
            r = requests.get("https://api.bybit.com/v2/private/wallet/balance",
                params={"timestamp":ts,"api_key":cfg["api_key"],"sign":sig,"coin":"USDT"}, timeout=5)
            data = r.json()
            usdt = data.get("result",{}).get("USDT",{}).get("available_balance",0)
            state["balance"] = float(usdt)
            return float(usdt)
        elif exchange == "okx":
            import base64, datetime
            ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
            path = "/api/v5/account/balance"
            sign_str = ts+"GET"+path+""
            sig = base64.b64encode(hmac.new(cfg["api_secret"].encode(),sign_str.encode(),hashlib.sha256).digest()).decode()
            r = requests.get("https://www.okx.com"+path,
                headers={"OK-ACCESS-KEY":cfg["api_key"],"OK-ACCESS-SIGN":sig,"OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":os.environ.get("OKX_PASSPHRASE","")}, timeout=5)
            data = r.json()
            for d in data.get("data",[{}])[0].get("details",[]):
                if d.get("ccy")=="USDT":
                    state["balance"]=float(d.get("availBal",0)); return state["balance"]
        elif exchange == "lbank":
            ex = _get_cex_exchange('lbank')
            bal = ex.fetch_balance()
            usdt = bal.get('USDT', {}).get('free', 0)
            state['balance'] = usdt
            return usdt
        elif exchange == "kucoin":
            ts = str(int(time.time()*1000))
            path = "/api/v1/accounts"
            sign_str = ts+"GET"+path
            sig = hmac.new(cfg["api_secret"].encode(), sign_str.encode(), hashlib.sha256).hexdigest()
            r = requests.get("https://api.kucoin.com"+path,
                headers={"KC-API-KEY":cfg["api_key"],"KC-API-SIGN":sig,"KC-API-TIMESTAMP":ts,"KC-API-PASSPHRASE":os.environ.get("KUCOIN_PASSPHRASE","")}, timeout=5)
            data = r.json()
            for a in data.get("data",[]):
                if a.get("currency")=="USDT" and a.get("type")=="trade":
                    state["balance"]=float(a.get("available",0)); return state["balance"]
        elif exchange == "kraken":
            ts = str(int(time.time()))
            path = "/0/private/Balance"
            sig_str = "/0/private/Balance"+hashlib.sha256((str(ts)+"nonce="+ts).encode()).hexdigest()
            sig = base64.b64encode(hmac.new(cfg["api_secret"].encode(), sig_str.encode(), hashlib.sha512).digest()).decode()
            r = requests.post("https://api.kraken.com"+path,
                headers={"API-Key":cfg["api_key"],"API-Sign":sig},
                data={"nonce": ts}, timeout=5)
            data = r.json()
            if not data.get("error"):
                for asset, bal in data.get("result",{}).items():
                    if asset in ("USDT", "ZUSD"):
                        state["balance"] = float(bal)
                        return float(bal)
    except Exception as ex:
        log("Balance error ("+exchange+"): "+str(ex), "ERROR")
    return 0.0

def cex_place_order(pair, side, amount):
    exchange = state["exchange"]
    try:
        sym = pair.replace("/","")
        if exchange == "binance":
            ts = str(int(time.time()*1000))
            params = "symbol="+sym+"&side="+side.upper()+"&type=MARKET&quantity="+str(amount)+"&timestamp="+ts
            sig = hmac.new(cfg["api_secret"].encode(), params.encode(), hashlib.sha256).hexdigest()
            r = requests.post("https://api.binance.com/api/v3/order",
                headers={"X-MBX-APIKEY":cfg["api_key"]},
                params={"symbol":sym,"side":side.upper(),"type":"MARKET","quantity":amount,"timestamp":ts,"signature":sig}, timeout=10)
            data = r.json()
            return data.get("orderId")
        elif exchange == "bybit":
            ts = str(int(time.time()*1000))
            body = json.dumps({"symbol":sym,"side":side.capitalize(),"orderType":"Market","qty":str(amount),"timeInForce":"GoodTillCancel"})
            sig = hmac.new(cfg["api_secret"].encode(),(ts+cfg["api_key"]+"5000"+body).encode(),hashlib.sha256).hexdigest()
            r = requests.post("https://api.bybit.com/v5/order/create",
                headers={"X-BAPI-API-KEY":cfg["api_key"],"X-BAPI-SIGN":sig,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":"5000","Content-Type":"application/json"},
                data=body, timeout=10)
            data = r.json()
            return data.get("result",{}).get("orderId")
        elif exchange == "lbank":
            lside = 'buy' if 'buy' in side.lower() else 'sell'
            try:
                ex = _get_cex_exchange('lbank')
                if lside == 'buy':
                    cost = amount * state.get("price", 1)
                    order = ex.create_order(pair, 'market', 'buy', cost, None, {
                        'createMarketBuyOrderRequiresPrice': False,
                    })
                else:
                    order = ex.create_order(pair, 'market', 'sell', amount, None)
                oid = order.get('id')
                if oid:
                    return oid
                info = order.get('info', {})
                log("LBank: " + str(info)[:200], "WARN")
            except Exception as e:
                log("LBank: " + str(e)[:200], "WARN")
        elif exchange == "okx":
            import base64, datetime
            ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
            path = "/api/v5/trade/order"
            body = json.dumps({
                "instId": sym + "-USDT" if not sym.endswith("USDT") else sym,
                "tdMode": "cash",
                "side": side.lower(),
                "ordType": "market",
                "sz": str(amount),
            })
            sign_str = ts+"POST"+path+body
            sig = base64.b64encode(hmac.new(cfg["api_secret"].encode(),sign_str.encode(),hashlib.sha256).digest()).decode()
            r = requests.post("https://www.okx.com"+path,
                headers={"OK-ACCESS-KEY":cfg["api_key"],"OK-ACCESS-SIGN":sig,"OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":os.environ.get("OKX_PASSPHRASE",""),"Content-Type":"application/json"},
                data=body, timeout=10)
            data = r.json()
            return data.get("data",[{}])[0].get("ordId")
        elif exchange == "kucoin":
            ts = str(int(time.time()*1000))
            path = "/api/v1/orders"
            body = json.dumps({
                "clientOid": ts,
                "side": side.lower(),
                "symbol": sym + "-USDT" if not sym.endswith("USDT") else sym,
                "type": "market",
                "size": str(amount),
            })
            sign_str = ts+"POST"+path+body
            sig = hmac.new(cfg["api_secret"].encode(), sign_str.encode(), hashlib.sha256).hexdigest()
            r = requests.post("https://api.kucoin.com"+path,
                headers={"KC-API-KEY":cfg["api_key"],"KC-API-SIGN":sig,"KC-API-TIMESTAMP":ts,"KC-API-PASSPHRASE":os.environ.get("KUCOIN_PASSPHRASE",""),"Content-Type":"application/json"},
                data=body, timeout=10)
            data = r.json()
            return data.get("data",{}).get("orderId")
        elif exchange == "kraken":
            ts = str(int(time.time()))
            path = "/0/private/AddOrder"
            post_data = "pair="+sym+"&type="+("buy" if "buy" in side.lower() else "sell")+"&ordertype=market&volume="+str(amount)
            sig_str = "/0/private/AddOrder"+hashlib.sha256((str(ts)+post_data).encode()).hexdigest()
            sig = base64.b64encode(hmac.new(cfg["api_secret"].encode(), sig_str.encode(), hashlib.sha512).digest()).decode()
            r = requests.post("https://api.kraken.com"+path,
                headers={"API-Key":cfg["api_key"],"API-Sign":sig},
                data=post_data, timeout=10)
            data = r.json()
            if not data.get("error"):
                return data.get("result",{}).get("txid",[None])[0]
    except Exception as ex:
        log("Order error ("+exchange+"): "+str(ex), "ERROR")
        return None

# ── DEX Trading ───────────────────────────────────────────────────────────────
ALCHEMY_KEY = os.environ.get("ALCHEMY_KEY", "")

def get_rpc(chain):
    alchemy_rpcs = {
        "ethereum": "https://eth-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY,
        "bsc":      "https://bnb-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY,
        "base":     "https://base-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY,
        "arbitrum": "https://arb-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY,
        "polygon":  "https://polygon-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY,
    }
    public_rpcs = {
        "ethereum": ["https://cloudflare-eth.com","https://rpc.ankr.com/eth"],
        "bsc":      ["https://bsc-dataseed1.binance.org","https://rpc.ankr.com/bsc"],
        "base":     ["https://mainnet.base.org","https://rpc.ankr.com/base"],
        "arbitrum": ["https://arb1.llamarpc.com","https://rpc.ankr.com/arbitrum"],
        "polygon":  ["https://polygon-rpc.com","https://rpc.ankr.com/polygon"],
    }
    if ALCHEMY_KEY:
        return [alchemy_rpcs.get(chain, alchemy_rpcs["ethereum"])]
    return public_rpcs.get(chain, public_rpcs["ethereum"])

CHAIN_CONFIG = {
    "ethereum": {"chain_id":1,    "name":"Ethereum"},
    "bsc":      {"chain_id":56,   "name":"BNB Chain"},
    "base":     {"chain_id":8453, "name":"Base"},
    "arbitrum": {"chain_id":42161,"name":"Arbitrum"},
    "polygon":  {"chain_id":137,  "name":"Polygon"},
    "monad":    {"chain_id":10143,"name":"Monad", "rpc":"https://rpc.monad.xyz", "native":"MON"},
}

TOKENS = {
    "ethereum": {"USDT":"0xdAC17F958D2ee523a2206206994597C13D831ec7","WETH":"0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2","WBTC":"0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"},
    "bsc":      {"USDT":"0x55d398326f99059fF775485246999027B3197955","WBNB":"0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c","BTCB":"0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"},
    "base":     {"USDT":"0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2","WETH":"0x4200000000000000000000000000000000000006"},
    "arbitrum": {"USDT":"0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9","WETH":"0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"},
    "polygon":  {"USDT":"0xc2132D05D31c914a87C6611C10748AEb04B58e8F","WMATIC":"0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"},
    "monad":    {},  # TODO: add Monad token addresses when available
}

def dex_get_quote_1inch(chain, from_token, to_token, amount_wei):
    try:
        chain_ids = {"ethereum":1,"bsc":56,"base":8453,"arbitrum":42161,"polygon":137,"monad":10143}
        cid = chain_ids.get(chain, 1)
        r = requests.get(
            "https://api.1inch.dev/swap/v6.0/"+str(cid)+"/quote",
            headers={"Authorization":"Bearer "+os.environ.get("ONEINCH_API_KEY","")},
            params={"src":from_token,"dst":to_token,"amount":str(amount_wei)}, timeout=5)
        data = r.json()
        return int(data.get("dstAmount", 0))
    except Exception as ex:
        log("1inch quote error: "+str(ex), "ERROR")
    return 0

def dex_get_quote_uniswap(chain, from_token, to_token, amount_wei):
    try:
        chain_ids = {"ethereum":1,"bsc":56,"base":8453,"arbitrum":42161,"polygon":137,"monad":10143}
        cid = chain_ids.get(chain, 1)
        r = requests.get(
            "https://api.uniswap.org/v1/quote",
            params={"protocols":"v2,v3","tokenInAddress":from_token,"tokenInChainId":cid,
                    "tokenOutAddress":to_token,"tokenOutChainId":cid,"amount":str(amount_wei),"type":"exactIn"}, timeout=5)
        data = r.json()
        return int(float(data.get("quote","0")) * 1e6)
    except Exception as ex:
        log("Uniswap quote error: "+str(ex), "ERROR")
    return 0

def dex_best_quote(chain, from_token, to_token, amount_wei):
    q1 = dex_get_quote_1inch(chain, from_token, to_token, amount_wei)
    q2 = dex_get_quote_uniswap(chain, from_token, to_token, amount_wei)
    if q1 >= q2:
        return q1, "1inch"
    return q2, "Uniswap"

def dex_swap(chain, from_token, to_token, amount_usd, price):
    try:
        amount_wei = int(amount_usd * 1e6)
        best_amount, router = dex_best_quote(chain, from_token, to_token, amount_wei)
        log("DEX swap via "+router+": $"+str(amount_usd)+" on "+CHAIN_CONFIG[chain]["name"])
        token_amount = amount_usd / price
        trade = {"time":time.strftime("%H:%M:%S"),"side":"DEX-BUY","price":price,"amount":round(token_amount,6),"router":router,"chain":chain}
        state["trades"].append(trade)
        state["positions"].append({"price":price,"amount":round(token_amount,6),"side":"buy","router":router,"chain":chain})
        log("Swap executed via "+router+" on "+CHAIN_CONFIG[chain]["name"])
        return True
    except Exception as ex:
        log("DEX swap error: "+str(ex), "ERROR")
    return False

def dex_get_balance():
    try:
        chain = state["chain"]
        wallet = cfg["wallet"]
        if not wallet:
            log("No wallet address — add WALLET_ADDRESS to Render environment", "WARN")
            return 0.0

        # Try Alchemy Token API first (most reliable)
        if ALCHEMY_KEY:
            try:
                chain_map = {
                    "ethereum":"eth-mainnet","bsc":"bnb-mainnet",
                    "base":"base-mainnet","arbitrum":"arb-mainnet","polygon":"polygon-mainnet"
                }
                network = chain_map.get(chain, "eth-mainnet")
                url = "https://"+network+".g.alchemy.com/v2/"+ALCHEMY_KEY
                payload = {
                    "jsonrpc":"2.0","method":"alchemy_getTokenBalances",
                    "params":[wallet,["0xdAC17F958D2ee523a2206206994597C13D831ec7"]],
                    "id":1
                }
                r = requests.post(url, json=payload, timeout=8)
                data = r.json()
                log("Alchemy token response: "+str(data)[:80])
                balances = data.get("result",{}).get("tokenBalances",[])
                if balances:
                    hex_val = balances[0].get("tokenBalance","0x0")
                    if hex_val and hex_val != "0x0" and hex_val != "0x":
                        balance = int(hex_val, 16) / 1e6
                        state["balance"] = balance
                        log("USDT Balance: $"+str(round(balance,2)))
                        return balance
            except Exception as ex:
                log("Alchemy token API error: "+str(ex), "WARN")

        # Fallback: native ETH balance
        rpcs = get_rpc(chain)
        for rpc in rpcs:
            try:
                payload = {"jsonrpc":"2.0","method":"eth_getBalance","params":[wallet,"latest"],"id":1}
                r = requests.post(rpc, json=payload, timeout=8)
                result = r.json().get("result","0x0")
                if result and result != "0x" and result != "0x0":
                    native = int(result, 16) / 1e18
                    price = get_price_kraken("ETH/USDT") or 3000
                    usd_val = round(native * price, 2)
                    state["balance"] = usd_val
                    log("ETH Balance: "+str(round(native,6))+" = $"+str(usd_val))
                    return usd_val
            except Exception as ex:
                log("Native balance failed: "+str(ex), "WARN")
                continue

        log("All balance checks failed", "WARN")
    except Exception as ex:
        log("DEX balance error: "+str(ex), "ERROR")
    return 0.0

def start_background_loops():
    """Start continuous price + balance + arb scanning regardless of strategy"""
    def price_loop():
        while True:
            try:
                pair = state.get("pair","ETH/USDT")
                p = get_price(pair)
                if p > 0:
                    state["price"] = p
                    state["price_history"].append({"time": int(time.time()), "value": p})
                    if len(state["price_history"]) > 1440:
                        state["price_history"] = state["price_history"][-1440:]
            except Exception as e:
                log("price loop error: "+str(e), "WARN")
            time.sleep(5)

    def balance_loop():
        time.sleep(3)
        # Set initial mode hint from config, but dashboard selection overrides
        if cfg["wallet"] and state["mode"] is None:
            state["mode"] = "dex"
        while True:
            try:
                m = state.get("mode", "cex")
                if m == "cex" and cfg["api_key"]:
                    cex_get_balance()
                elif cfg["wallet"]:
                    dex_get_balance()
                elif cfg["sol_wallet"]:
                    sol_get_balance()
                # Always show Solana balance if wallet is set, regardless of mode
                if cfg["sol_wallet"]:
                    sol_get_balance()
            except Exception as ex:
                log("Balance loop error: "+str(ex), "ERROR")
            time.sleep(120)

    def arb_loop():
        while True:
            try:
                scan_arbitrage()
            except Exception as e:
                log("arb loop error: "+str(e), "WARN")
            time.sleep(120)

    threading.Thread(target=price_loop, daemon=True).start()
    threading.Thread(target=balance_loop, daemon=True).start()
    threading.Thread(target=arb_loop, daemon=True).start()
    log("Background price feed, balance and arb scanner started")

# ── Solana ────────────────────────────────────────────────────────────────────
SOL_RPC = "https://api.mainnet-beta.solana.com"
# Jupiter API — use lite-api.jup.ag (new endpoint, requires API key after June 2026)
# Set JUPITER_API_KEY in Render env vars from dev.jup.ag
JUPITER_API     = "https://quote-api.jup.ag/v6"
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "")

# Solana token mints
SOL_TOKENS = {
    "USDC":  "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT":  "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "SOL":   "So11111111111111111111111111111111111111112",
    "BTC":   "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
    "ETH":   "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    "BNB":   "9gP2kCy3wA1ctvYWQk75guqXuzoJGLIDs5oPHkHGs89",
    "JUP":   "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK":  "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":   "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "MATIC": "Gz7VkD4MacbEB6yC5XD3HcumEiYx2EtDYYrfikGsvopG",
    "SPCX":  "SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb",
}

def sol_get_balance():
    """Get SOL + USDC + USDT balance. Tries multiple RPC endpoints for reliability."""
    SOL_RPCS = [
        "https://api.mainnet-beta.solana.com",
        "https://rpc.ankr.com/solana",
        "https://solana-rpc.publicnode.com",
    ]
    if ALCHEMY_KEY:
        SOL_RPCS = ["https://solana-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY] + SOL_RPCS

    wallet = cfg["sol_wallet"]
    if not wallet:
        return 0.0

    def rpc_call(method, params):
        payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
        for rpc in SOL_RPCS:
            try:
                r = requests.post(rpc, json=payload, timeout=8)
                result = r.json()
                if "result" in result:
                    return result["result"]
            except Exception as e:
                log("RPC error: "+str(e), "WARN")
                continue
        return None

    def get_token_balance(mint):
        """Helper to get balance of any SPL token by mint address."""
        raw = rpc_call("getTokenAccountsByOwner",
            [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
        if raw and raw.get("value"):
            return float(
                raw["value"][0]
                .get("account",{}).get("data",{}).get("parsed",{})
                .get("info",{}).get("tokenAmount",{}).get("uiAmount", 0) or 0
            )
        return 0.0

    try:
        # Get SOL native balance
        sol_raw = rpc_call("getBalance", [wallet])
        sol_amt = (sol_raw.get("value", 0) / 1e9) if isinstance(sol_raw, dict) else 0.0
        sol_price = get_price_kraken("SOL/USDT") or get_price_coingecko("SOL/USDT") or 150

        # Get USDC and USDT balances
        usdc = get_token_balance(SOL_TOKENS["USDC"])
        usdt = get_token_balance(SOL_TOKENS["USDT"])

        stable_total = round(usdc + usdt, 2)
        total_usd = round(sol_amt * sol_price + stable_total, 2)
        state["sol_balance"] = total_usd
        state["sol_usdc"]    = usdc
        state["sol_usdt"]    = usdt
        state["sol_native"]  = round(sol_amt * sol_price, 2)
        log("Solana balance: "+str(round(sol_amt,4))+" SOL + $"+str(usdc)+" USDC + $"+str(usdt)+" USDT = $"+str(total_usd))
        return total_usd
    except Exception as ex:
        log("Solana balance error: "+str(ex), "ERROR")
    return 0.0

def jupiter_get_quote(from_mint, to_mint, amount_lamports):
    """Get best swap quote from Jupiter aggregator"""
    try:
        r = requests.get(JUPITER_API+"/quote", params={
            "inputMint": from_mint,
            "outputMint": to_mint,
            "amount": str(amount_lamports),
            "slippageBps": "50",
        }, timeout=8)
        data = r.json()
        return data
    except Exception as ex:
        log("Jupiter quote error: "+str(ex), "ERROR")
    return None

def raydium_get_quote(from_mint, to_mint, amount, slippage_bps="200"):
    """
    Get swap quote from Raydium Trade API.
    Confirmed endpoint: transaction-v1.raydium.io/compute/swap-base-in
    Returns full response object — swapResponse in the TX payload needs the complete object.
    Handles 429 with Retry-After backoff.
    """
    for attempt in range(3):
        try:
            r = requests.get(
                "https://transaction-v1.raydium.io/compute/swap-base-in",
                params={
                    "inputMint":   from_mint,
                    "outputMint":  to_mint,
                    "amount":      str(amount),
                    "slippageBps": slippage_bps,
                    "txVersion":   "V0",
                },
                timeout=10
            )
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 5))
                log("Raydium quote 429 — waiting "+str(wait)+"s", "WARN")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                log("Raydium quote status: "+str(r.status_code), "WARN")
                return None
            data = r.json()
            if not data.get("success"):
                log("Raydium quote failed: "+str(data.get("msg","")), "WARN")
                return None
            return data  # Full response needed by transaction endpoint
        except Exception as ex:
            log("Raydium quote error (attempt "+str(attempt+1)+"): "+str(ex)[:80], "WARN")
            time.sleep(2)
    return None

def _raydium_execute_swap(from_token, to_token, from_mint, to_mint,
                          amount_input, out_human, price, side, via,
                          lamports, raydium_quote, to_dec):
    """Execute a Raydium swap using the quote from raydium_get_quote."""
    if state["paper_trading"]:
        trade = {"time":time.strftime("%H:%M:%S"),"side":"[PAPER] "+side+via,
                 "price":price,"amount":out_human,"router":"Raydium","chain":"solana"}
        state["trades"].append(trade)
        return True, out_human

    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        from solders import message as solders_message
        import base64 as b64

        private_key = cfg.get("sol_key","")
        wallet      = cfg.get("sol_wallet","")
        if not private_key or not wallet:
            log("SOL_PRIVATE_KEY or SOL_WALLET_ADDRESS not set", "WARN")
            return False, 0.0
        # Check USDC balance before attempting swap
        usdc_bal = state.get("sol_usdc", 0)
        if from_token in ("USDC","USDT") and usdc_bal > 0 and amount_input > usdc_bal:
            log(f"Insufficient USDC: need ${amount_input:.2f}, have ${usdc_bal:.2f}", "WARN")
            return False, 0.0

        keypair = Keypair.from_base58_string(private_key)

        # ── ATA helpers ────────────────────────────────────────────────────
        def get_ata(wallet_addr, mint_addr):
            """Find existing Associated Token Account for a wallet+mint."""
            rpcs = list(SOL_RPCS)
            if ALCHEMY_KEY:
                rpcs = ["https://solana-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY] + rpcs
            payload = {
                "jsonrpc":"2.0","id":1,
                "method":"getTokenAccountsByOwner",
                "params":[wallet_addr, {"mint":mint_addr}, {"encoding":"jsonParsed"}]
            }
            for rpc in rpcs:
                try:
                    r = requests.post(rpc, json=payload, timeout=8)
                    accs = r.json().get("result",{}).get("value",[])
                    if accs:
                        return accs[0].get("pubkey")
                except Exception as e:
                    log("ATA error: "+str(e), "WARN")
                    continue
            return None

        def create_ata_if_missing(wallet_addr, mint_addr):
            """Create ATA on-chain if missing, return its address."""
            existing = get_ata(wallet_addr, mint_addr)
            if existing:
                return existing
            log("Creating ATA for mint "+mint_addr[:8]+"...", "WARN")
            try:
                from solders.pubkey import Pubkey
                from solders.hash import Hash
                from solders.instruction import AccountMeta, Instruction
                from solders.message import MessageV0

                wallet_pk  = Pubkey.from_string(wallet_addr)
                mint_pk    = Pubkey.from_string(mint_addr)
                token_prog = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
                ata_prog   = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bsU")
                sys_prog   = Pubkey.from_string("11111111111111111111111111111111")

                seeds = [bytes(wallet_pk), bytes(token_prog), bytes(mint_pk)]
                ata_pk, _ = Pubkey.find_program_address(seeds, ata_prog)

                # Get blockhash
                bh_r = requests.post("https://api.mainnet-beta.solana.com", json={
                    "jsonrpc":"2.0","id":1,"method":"getLatestBlockhash",
                    "params":[{"commitment":"confirmed"}]
                }, timeout=8)
                blockhash_str = bh_r.json().get("result",{}).get("value",{}).get("blockhash","")
                if not blockhash_str:
                    log("Could not get blockhash", "WARN")
                    return None

                blockhash = Hash.from_string(blockhash_str)

                create_ix = Instruction(
                    ata_prog,
                    bytes([0]),
                    [
                        AccountMeta(wallet_pk, True, True),    # payer
                        AccountMeta(ata_pk,    False, True),   # ata
                        AccountMeta(wallet_pk, False, False),  # owner
                        AccountMeta(mint_pk,   False, False),  # mint
                        AccountMeta(sys_prog,  False, False),  # system program
                        AccountMeta(token_prog,False, False),  # token program
                    ]
                )
                msg = MessageV0.try_compile(wallet_pk, [create_ix], [], blockhash)
                tx = VersionedTransaction(msg, [keypair])

                send_payload = {
                    "jsonrpc":"2.0","id":1,"method":"sendTransaction",
                    "params":[b64.b64encode(bytes(tx)).decode(),
                              {"encoding":"base64","skipPreflight":False,
                               "preflightCommitment":"confirmed"}]
                }
                rpc = "https://api.mainnet-beta.solana.com"
                if ALCHEMY_KEY:
                    rpc = "https://solana-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY
                rr = requests.post(rpc, json=send_payload, timeout=15)
                rr_result = rr.json().get("result","")
                log("ATA creation tx: "+str(rr_result)[:40], "INFO")
                if rr_result:
                    # Wait for confirmation and verify
                    time.sleep(3)
                    for _ in range(5):
                        existing = get_ata(wallet_addr, mint_addr)
                        if existing:
                            return existing
                        time.sleep(2)
                log("ATA not confirmed after creation, trying again...", "WARN")
                return get_ata(wallet_addr, mint_addr)
            except Exception as ate:
                log("ATA creation error: "+str(ate)[:80], "WARN")
                return get_ata(wallet_addr, mint_addr)  # might exist now

        input_ata  = get_ata(wallet, from_mint)
        if not input_ata:
            log("No ATA for input token "+from_token+", trying to create...", "WARN")
            input_ata = create_ata_if_missing(wallet, from_mint)
        if not input_ata:
            log("Cannot swap — no input ATA for "+from_token, "WARN")
            return False, 0.0

        output_ata = get_ata(wallet, to_mint)
        if not output_ata:
            output_ata = create_ata_if_missing(wallet, to_mint)
        if not output_ata:
            log("Cannot swap — no output ATA for "+to_token, "WARN")
            return False, 0.0

        # Build Raydium swap transaction payload
        swap_payload = {
            "computeUnitPriceMicroLamports": "10000",
            "swapResponse":  raydium_quote,
            "txVersion":     "V0",
            "wallet":        wallet,
            "wrapSol":       from_token == "SOL",
            "unwrapSol":     to_token   == "SOL",
            "inputAccount":  input_ata,
            "outputAccount": output_ata,
        }
        log("Raydium swap payload keys: "+str(list(swap_payload.keys())), "DEBUG")
        r = requests.post("https://transaction-v1.raydium.io/transaction/swap-base-in",
            json=swap_payload, timeout=15)
        log("Raydium TX status: "+str(r.status_code)+" body: "+r.text[:200])
        tx_data = r.json()
        if not tx_data.get("success") or not tx_data.get("data"):
            log("Raydium tx build failed: "+str(tx_data.get("msg",""))[:100], "WARN")
            return False, 0.0

        txs = tx_data.get("data", [])
        if isinstance(txs, list) and len(txs) > 0:
            tx_b64 = txs[0].get("transaction", "")
        elif isinstance(txs, dict):
            tx_b64 = txs.get("transaction", "")
        else:
            tx_b64 = ""
        if not tx_b64:
            log("No transaction in Raydium response", "WARN")
            return False, 0.0
        raw_tx = b64.b64decode(tx_b64)
        tx_obj = VersionedTransaction.from_bytes(raw_tx)
        sig = keypair.sign_message(solders_message.to_bytes_versioned(tx_obj.message))
        signed_tx = VersionedTransaction.populate(tx_obj.message, [sig])

        # Submit
        rpc = ("https://solana-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY) if ALCHEMY_KEY else "https://api.mainnet-beta.solana.com"
        r2 = requests.post(rpc, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[
                b64.b64encode(bytes(signed_tx)).decode(),
                {"encoding":"base64","skipPreflight":True,
                 "preflightCommitment":"confirmed","maxRetries":5}
            ]
        }, timeout=20)
        result = r2.json()

        tx_sig = result.get("result","")
        if tx_sig:
            time.sleep(2)
            verify_payload = {"jsonrpc":"2.0","id":1,"method":"getSignatureStatuses","params":[[tx_sig]]}
            vr = requests.post(rpc, json=verify_payload, timeout=8)
            vdata = vr.json()
            vresult = vdata.get("result",{}).get("value",[{}])[0]
            tx_ok = vresult and vresult.get("confirmationStatus") in ("confirmed","finalized") and vresult.get("err") is None
            if tx_ok:
                log("RAYDIUM SWAP CONFIRMED: "+tx_sig[:20]+"... "+from_token+"→"+to_token+via)
                trade = {"time":time.strftime("%H:%M:%S"),"side":"LIVE-"+side+via,
                         "price":price,"amount":out_human,"router":"Raydium",
                         "chain":"solana","tx":tx_sig[:20]}
                state["trades"].append(trade)
                return True, out_human
            else:
                err_msg = str(vresult.get("err","")) if vresult else "no status"
                log("Swap TX failed: "+tx_sig[:20]+" err="+err_msg, "WARN")
                return False, 0.0
        else:
            log("Raydium send failed: "+str(result.get("error",""))[:100], "WARN")
            return False, 0.0
    except ImportError as ie:
        log("Missing package: "+str(ie), "WARN"); return False, 0.0
    except Exception as ex:
        log("Raydium swap error: "+str(ex)[:100], "WARN"); return False, 0.0

def jupiter_swap(from_token, to_token, amount_input, price, dex=None):
    """
    Execute a Solana DEX swap via Jupiter aggregator (v6 API).
    Jupiter routes through all DEXes (Raydium, Orca, Meteora, etc.) for best price.
    """
    from_mint = SOL_TOKENS.get(from_token, SOL_TOKENS["USDC"])
    to_mint   = SOL_TOKENS.get(to_token,   SOL_TOKENS["SOL"])
    from_dec  = TOKEN_DECIMALS.get(from_token, 6)
    to_dec    = TOKEN_DECIMALS.get(to_token,   9)
    side      = "BUY" if from_token in ("USDC","USDT") else "SELL"
    via       = (" via "+dex) if dex else ""

    lamports = int(amount_input * (10 ** from_dec))
    log("Swap "+side+via+": "+str(amount_input)+" "+from_token+" → "+to_token)

    # Try Raydium first (primary), Jupiter as fallback
    slippage_bps = "300"
    rq = raydium_get_quote(from_mint, to_mint, lamports, slippage_bps)
    if rq:
        out_lamports = int(rq.get("data",{}).get("outputAmount", 0))
        out_human = out_lamports / (10 ** to_dec) if out_lamports > 0 else 0.0
        if out_human > 0:
            log("Raydium quote: "+str(amount_input)+" "+from_token+" → "+str(round(out_human,6))+" "+to_token)
            return _raydium_execute_swap(from_token, to_token, from_mint, to_mint,
                amount_input, out_human, price, side, via, lamports, rq, to_dec)

    # Fallback: Jupiter
    log("Raydium unavailable, trying Jupiter...", "WARN")
    try:
        r = requests.get("https://quote-api.jup.ag/v6/quote", params={
            "inputMint": from_mint,
            "outputMint": to_mint,
            "amount": str(lamports),
            "slippageBps": "100",
        }, timeout=10)
        qdata = r.json()
        if qdata and not qdata.get("error"):
            out_amount = int(qdata.get("outAmount", 0))
            out_human = out_amount / (10 ** to_dec) if out_amount > 0 else 0.0
            if out_human > 0:
                log("Jupiter quote: "+str(amount_input)+" "+from_token+" → "+str(round(out_human,6))+" "+to_token)
                quote = qdata
            else:
                log("Jupiter quote failed", "WARN")
                return False, 0.0
        else:
            log("Jupiter quote failed: "+str(qdata.get("error","no data")), "WARN")
            return False, 0.0
    except Exception as e:
        log("Jupiter unavailable: "+str(e)[:80], "WARN")
        return False, 0.0

    if state["paper_trading"]:
        trade = {"time":time.strftime("%H:%M:%S"),"side":"[PAPER] "+side+via,
                 "price":price,"amount":out_human,"router":"Jupiter","chain":"solana"}
        state["trades"].append(trade)
        return True, out_human

    # ── Live execution via Jupiter ────────────────────────────────────────────
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        from solders import message as solders_message
        import base64 as b64

        private_key = cfg.get("sol_key","")
        wallet      = cfg.get("sol_wallet","")
        if not private_key or not wallet:
            log("SOL_PRIVATE_KEY or SOL_WALLET_ADDRESS not set", "WARN")
            return False, 0.0
        # Check USDC balance before attempting swap
        usdc_bal = state.get("sol_usdc", 0)
        if from_token in ("USDC","USDT") and usdc_bal > 0 and amount_input > usdc_bal:
            log(f"Insufficient USDC: need ${amount_input:.2f}, have ${usdc_bal:.2f}", "WARN")
            return False, 0.0

        try:
            keypair = Keypair.from_base58_string(private_key)
        except Exception as ke:
            log("Key decode failed: "+str(ke)[:60], "WARN")
            return False, 0.0

        # Get swap transaction from Jupiter (handles ATA creation automatically)
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": wallet,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 10000,
        }
        r = requests.post("https://quote-api.jup.ag/v6/swap",
            json=swap_payload, timeout=15)
        swap_data = r.json()
        if not swap_data.get("swapTransaction"):
            log("Jupiter swap tx failed: "+str(swap_data.get("error",""))[:100], "WARN")
            return False, 0.0

        swap_tx_b64 = swap_data["swapTransaction"]
        raw_tx = b64.b64decode(swap_tx_b64)
        tx_obj = VersionedTransaction.from_bytes(raw_tx)
        sig = keypair.sign_message(solders_message.to_bytes_versioned(tx_obj.message))
        signed_tx = VersionedTransaction.populate(tx_obj.message, [sig])

        # Send transaction
        send_payload = {
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[
                b64.b64encode(bytes(signed_tx)).decode(),
                {"encoding":"base64","skipPreflight":False,
                 "preflightCommitment":"confirmed","maxRetries":3}
            ]
        }
        send_rpc = ("https://solana-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY) if ALCHEMY_KEY else "https://api.mainnet-beta.solana.com"
        r2 = requests.post(send_rpc, json=send_payload, timeout=15)
        result = r2.json()
        if result.get("error",{}).get("code") == 429:
            log("Rate limited — retrying", "WARN")
            time.sleep(3)
            r2 = requests.post("https://api.mainnet-beta.solana.com", json=send_payload, timeout=15)
            result = r2.json()

        tx_sig = result.get("result","")
        if tx_sig:
            # Verify confirmation
            time.sleep(2)
            verify_payload = {"jsonrpc":"2.0","id":1,"method":"getSignatureStatuses","params":[[tx_sig]]}
            vr = requests.post(send_rpc, json=verify_payload, timeout=8)
            vdata = vr.json()
            status = vdata.get("result",{}).get("value",[None])[0]
            if status and status.get("confirmationStatus") in ("confirmed","finalized"):
                log("SWAP CONFIRMED: "+tx_sig[:20]+"... "+from_token+"→"+to_token+via)
                trade = {"time":time.strftime("%H:%M:%S"),"side":"LIVE-"+side+via,
                         "price":price,"amount":out_human,"router":"Jupiter",
                         "chain":"solana","tx":tx_sig[:20]}
                state["trades"].append(trade)
                return True, out_human
            else:
                log("Swap submitted but not confirmed: "+tx_sig[:20], "WARN")
                return False, 0.0
        else:
            log("Send failed: "+str(result.get("error",""))[:100], "WARN")
            return False, 0.0

    except ImportError as ie:
        log("Missing package: "+str(ie), "WARN"); return False, 0.0
    except Exception as ex:
        log("Jupiter swap error: "+str(ex)[:100], "WARN"); return False, 0.0


def get_evm_dex_price(chain, pair):
    """Get on-chain DEX price via 0x API for EVM chains"""
    try:
        tokens_map = TOKENS.get(chain, {})
        token = pair.split("/")[0]
        sell_token = tokens_map.get("USDT","")
        buy_token  = tokens_map.get("W"+token, tokens_map.get(token,""))
        if not sell_token or not buy_token: return 0.0
        r = requests.get("https://api.0x.org/swap/v1/price", params={
            "sellToken":  sell_token,
            "buyToken":   buy_token,
            "sellAmount": "1000000",
        }, headers={"0x-api-key": os.environ.get("ZEROX_API_KEY","")}, timeout=8)
        data = r.json()
        price = float(data.get("price", 0))
        return 1.0/price if price > 0 else 0.0
    except Exception as e:
        log("EVM DEX price error: "+str(e), "WARN")
        return 0.0

def scan_arbitrage():
    opps = []
    chain = state.get("chain", "ethereum")

    if chain == "solana":
        sol_pairs = ["SOL/USDC", "JUP/USDC", "ETH/USDC"]

        TOKEN_MINTS = {
            "SOL":  "So11111111111111111111111111111111111111112",
            "ETH":  "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
            "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        }

        def get_dexpaprika_prices(token):
            """
            Get per-DEX prices via DexPaprika — confirmed accessible from Render.
            Returns {dex_name: price_usd} for Raydium, Orca, Meteora.
            """
            try:
                mint = TOKEN_MINTS.get(token, "")
                if not mint: return {}
                r = requests.get(
                    "https://api.dexpaprika.com/networks/solana/tokens/"+mint+"/pools",
                    params={"page": 0, "limit": 50, "sort": "desc", "order_by": "volume_usd"},
                    timeout=10
                )
                if r.status_code == 429:
                    log("DexPaprika 429 for "+token+" — skipping", "WARN")
                    return {}
                if r.status_code != 200:
                    log("DexPaprika status "+str(r.status_code)+" for "+token, "WARN")
                    return {}
                pools = r.json().get("pools", [])
                dex_prices = {}
                for pool in pools:
                    dex_id = pool.get("dex_id", "").lower()
                    price  = float(pool.get("price_usd", 0) or 0)
                    tokens_in_pool = [t.get("symbol","") for t in pool.get("tokens",[])]
                    if "USDC" not in tokens_in_pool or price <= 0:
                        continue
                    if dex_id in ("raydium","raydium_clmm") and "Raydium" not in dex_prices:
                        dex_prices["Raydium"] = price
                    elif dex_id == "orca" and "Orca" not in dex_prices:
                        dex_prices["Orca"] = price
                    elif dex_id == "meteora" and "Meteora" not in dex_prices:
                        dex_prices["Meteora"] = price
                    if len(dex_prices) >= 3:
                        break
                return dex_prices
            except Exception as ex:
                log("DexPaprika error for "+token+": "+str(ex)[:60], "WARN")
                return {}

        try:
            usdc_bal = state.get("sol_usdc", 0)
            size     = min(usdc_bal * cfg["risk_pct"] / 100, cfg["max_pos"])

            for pair in sol_pairs:
                token  = pair.split("/")[0]
                prices = get_dexpaprika_prices(token)

                if prices:
                    log("SOL ARB scan "+pair+": "+str({k:round(v,6) for k,v in prices.items()}))
                else:
                    log("SOL ARB scan "+pair+": no prices","WARN")

                if len(prices) >= 2:
                    est_gas = 0.004  # two Solana transactions
                    vals = list(prices.items())
                    for i in range(len(vals)):
                        for j in range(i+1, len(vals)):
                            n1,p1 = vals[i]
                            n2,p2 = vals[j]
                            if p1<=0 or p2<=0: continue
                            spread = abs(p1-p2)/min(p1,p2)*100
                            if spread < 1.5: continue
                            buy_from   = n1 if p1 < p2 else n2
                            sell_on    = n2 if p1 < p2 else n1
                            buy_price  = min(p1,p2)
                            sell_price = max(p1,p2)
                            # Deduct 0.75% per leg for DEX fees + slippage
                            net_spread = spread - 1.5
                            gross      = (net_spread/100) * size if size > 0 else 0
                            est_profit = round(gross - est_gas, 6)
                            opps.append({
                                "pair":           pair,
                                "buy_from":       buy_from,
                                "sell_on":        sell_on,
                                "buy_price":      round(buy_price,6),
                                "sell_price":     round(sell_price,6),
                                "spread_pct":     round(spread,4),
                                "est_gas_usd":    est_gas,
                                "est_profit_usd": est_profit,
                                "chain":          "solana",
                                "executable":     (
                                    spread >= cfg["min_arb_spread"]
                                    and est_profit > 0
                                    and size >= 0.10
                                    and usdc_bal >= 0.10
                                ),
                            })

                time.sleep(3)  # 3s between tokens — DexPaprika allows 60 req/min

        except Exception as ex:
            log("SOL ARB scan error: "+str(ex), "WARN")

    else:
        evm_pairs = ["BTC/USDT","ETH/USDT","BNB/USDT","SOL/USDT"]
        for pair in evm_pairs:
            prices = {}
            p_kraken = get_price_kraken(pair)
            p_cg     = get_price_coingecko(pair)
            p_dex    = get_evm_dex_price(chain, pair)
            if p_kraken > 0: prices["Kraken"]  = p_kraken
            if p_cg     > 0: prices["CoinGecko"] = p_cg
            if p_dex    > 0: prices["DEX"]      = p_dex
            if len(prices) >= 2:
                vals = list(prices.items())
                for i in range(len(vals)):
                    for j in range(i+1, len(vals)):
                        n1,p1 = vals[i]
                        n2,p2 = vals[j]
                        if p1<=0 or p2<=0: continue
                        spread = abs(p1-p2)/min(p1,p2)*100
                        if spread > 0.1:
                            buy_from   = n1 if p1 < p2 else n2
                            sell_on    = n2 if p1 < p2 else n1
                            buy_price  = min(p1,p2)
                            sell_price = max(p1,p2)
                            est_gas    = 0.50 if chain in("base","arbitrum","polygon") else 5.0
                            bal        = state.get("balance",0)
                            size       = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])
                            est_profit = round((sell_price-buy_price)*(size/buy_price if buy_price>0 else 0)-est_gas, 2)
                            opps.append({
                                "pair":           pair,
                                "buy_from":       buy_from,
                                "sell_on":        sell_on,
                                "buy_price":      round(buy_price,4),
                                "sell_price":     round(sell_price,4),
                                "spread_pct":     round(spread,3),
                                "est_gas_usd":    est_gas,
                                "est_profit_usd": est_profit,
                                "chain":          chain,
                                "executable":     spread >= cfg["min_arb_spread"] and est_profit > 0,
                            })

    state["arb_opps"] = sorted(opps, key=lambda x: x["spread_pct"], reverse=True)[:10]
    return state["arb_opps"]

def execute_arbitrage(opp):
    spread     = opp["spread_pct"]
    est_profit = opp["est_profit_usd"]
    chain      = opp.get("chain", state.get("chain","ethereum"))
    pair       = opp["pair"]
    price      = opp["buy_price"]
    buy_from   = opp["buy_from"]
    sell_on    = opp["sell_on"]

    # Pre-flight safety checks
    if spread < cfg["min_arb_spread"]:
        log("ARB skipped — spread "+str(spread)+"% < min "+str(cfg["min_arb_spread"])+"%","WARN")
        return False
    if est_profit <= 0:
        log("ARB skipped — estimated profit negative after fees","WARN")
        return False
    if state["daily_loss"] >= cfg["max_loss"]:
        log("ARB skipped — daily loss limit hit","WARN")
        return False

    usdc_bal = state.get("sol_usdc", 0) if chain == "solana" else state["balance"]
    size     = min(usdc_bal * cfg["risk_pct"] / 100, cfg["max_pos"])
    if size < 0.10:
        log("ARB skipped — USDC balance $"+str(round(usdc_bal,2))+" too low","WARN")
        return False

    token = pair.split("/")[0]
    amt   = round(size / price, 6) if price > 0 else 0

    if state["paper_trading"]:
        log("[PAPER] ARB: "+token+" buy on "+buy_from+" @ $"+str(price)+
            " → sell on "+sell_on+" @ $"+str(opp["sell_price"])+
            " spread "+str(spread)+"% est $"+str(est_profit))
        record_trade("[PAPER] ARB", price, amt, round(est_profit,2))
        state["pnl"] += est_profit * 0.7
        return True

    if chain != "solana":
        # EVM arb — basic implementation
        result = place_order(pair, "buy", amt)
        if result:
            record_trade("ARB "+buy_from+"→"+sell_on, price, amt, round(est_profit,2))
        return bool(result)

    # ── Solana live two-leg arbitrage ─────────────────────────────────────────
    state["trading_lock"]   = True
    state["last_trade_time"] = time.time()

    try:
        log("ARB LEG 1: BUY "+token+" with $"+str(round(size,4))+" USDC on "+buy_from)
        buy_ok, token_received = jupiter_swap("USDC", token, size, price, dex=buy_from)
        if not buy_ok or token_received <= 0:
            log("ARB buy leg failed — aborting", "WARN")
            state["trading_lock"] = False
            return False

        log("ARB LEG 1 complete: received "+str(round(token_received,6))+" "+token)

        # Wait for on-chain confirmation before selling
        time.sleep(5)

        # Sell EXACTLY what we received from the buy quote
        sell_price = opp["sell_price"]
        log("ARB LEG 2: SELL "+str(round(token_received,6))+" "+token+" on "+sell_on)
        sell_ok, usdc_received = jupiter_swap(token, "USDC", token_received, sell_price, dex=sell_on)

        if sell_ok and usdc_received > 0:
            # Actual profit = USDC returned minus USDC spent minus gas
            actual_profit = round(usdc_received - size - opp["est_gas_usd"], 6)
            state["pnl"] += actual_profit
            if actual_profit < 0:
                state["daily_loss"] += abs(actual_profit)
            record_trade(
                "ARB "+buy_from+"→"+sell_on,
                price, token_received,
                round(actual_profit, 4)
            )
            log("ARB complete — spent $"+str(round(size,4))+
                " received $"+str(round(usdc_received,4))+
                " profit $"+str(actual_profit))
            state["trading_lock"] = False
            return True
        else:
            log("ARB sell leg failed — holding "+str(round(token_received,6))+" "+token, "WARN")
            record_trade("ARB-BUY-ONLY (sell failed)", price, token_received, None)
            state["trading_lock"] = False
            return False

    except Exception as ex:
        log("execute_arbitrage error: "+str(ex)[:80], "WARN")
        state["trading_lock"] = False
        return False

ARB_PAIRS = ["BTC/USDT","ETH/USDT","BNB/USDT","SOL/USDT"]

# ── Strategies ────────────────────────────────────────────────────────────────
def get_balance():
    if state["mode"] == "dex":
        chain = state.get("chain", "ethereum")
        if chain == "solana" and cfg["sol_wallet"]:
            sol_get_balance()
            return state.get("sol_balance", 0.0)
        else:
            dex_get_balance()
            return state.get("balance", 0.0)
    else:
        return cex_get_balance()

def place_order(pair, side, amount):
    if state["mode"] == "dex":
        chain = state["chain"]
        price = get_price(pair)
        token = pair.split("/")[0]
        stablecoin = pair.split("/")[1]

        if chain == "solana":
            # Use Jupiter/Raydium for Solana trades
            if side in ("buy","buy_market"):
                # amount is token quantity, jupiter_swap needs USDC cost
                cost = amount * price
                log(f"place_order BUY: amt={amount} price={price} cost={cost} pair={pair}", "DEBUG")
                result = jupiter_swap(stablecoin, token, cost, price, dex="Raydium")
            else:
                log(f"place_order SELL: amt={amount} price={price} pair={pair}", "DEBUG")
                result = jupiter_swap(token, stablecoin, amount, price, dex="Raydium")
            # jupiter_swap returns (success_bool, amount) tuple — unpack it
            if isinstance(result, tuple):
                return result[0]
            return bool(result)
        else:
            # EVM chains: use 1inch/Uniswap
            tokens = TOKENS.get(chain, {})
            from_t = tokens.get("USDT","")
            to_t   = tokens.get("W"+token, tokens.get(token,""))
            if side in ("buy","buy_market"):
                return dex_swap(chain, from_t, to_t, amount * price, price)
            else:
                return dex_swap(chain, to_t, from_t, amount * price, price)
    else:
        return cex_place_order(pair, side, amount)

def record_trade(side, price, amount, pnl=None):
    trade = {"time":time.strftime("%H:%M:%S"),"side":side,"price":price,"amount":amount,"pnl":pnl}
    state["trades"].append(trade)
    if len(state["trades"]) > 500:
        state["trades"] = state["trades"][-500:]
    state["last_trade"] = {"action": side, "pair": state["pair"], "price": price, "time": time.time()}
    state["trades_list"] = [{"time":t["time"],"action":t["side"],"price":t["price"],"amount":t["amount"],"pnl":t.get("pnl"),"via":t.get("router","")} for t in state["trades"][-50:]]

def run_dca():
    log("DCA started on "+state["pair"]+" ("+state["mode"].upper()+")")
    buy_prices = []
    while state["running"] and state["strategy"]=="dca":
        while state["paused"]: time.sleep(1)
        price = get_price(state["pair"])
        if price <= 0: time.sleep(60); continue
        bal = get_balance()
        if not buy_prices:
            size = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])
            if size > 1:
                amt = round(size/price, 6)
                if place_order(pair,"buy",amt):
                    buy_prices.append(price)
                    state["positions"].append({"price":price,"amount":amt,"strategy":"DCA"})
                    record_trade("DCA-BUY",price,amt)
                    log("DCA BUY "+str(amt)+" @ $"+str(price))
        else:
            avg = sum(buy_prices)/len(buy_prices)
            gain = (price-avg)/avg*100
            loss = (avg-price)/avg*100
            total = sum(p["amount"] for p in state["positions"])
            if gain >= cfg["take_profit"]:
                if place_order(pair,"sell",total):
                    pnl = (price-avg)*total
                    state["pnl"] += pnl
                    record_trade("SELL",price,total,round(pnl,2))
                    log("DCA SELL @ $"+str(price)+" PnL: $"+str(round(pnl,2)))
                    buy_prices.clear(); state["positions"].clear()
            elif loss >= cfg["stop_loss"]:
                if place_order(pair,"sell",total):
                    pnl = (price-avg)*total
                    state["pnl"] += pnl
                    state["daily_loss"] += abs(pnl)
                    record_trade("STOP",price,total,round(pnl,2))
                    log("STOP LOSS @ $"+str(price), "WARN")
                    buy_prices.clear(); state["positions"].clear()
            elif loss >= 2 and state["daily_loss"] < cfg["max_loss"]:
                size = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])
                if size > 1:
                    amt = round(size/price,6)
                    if place_order(pair,"buy",amt):
                        buy_prices.append(price)
                        state["positions"].append({"price":price,"amount":amt,"strategy":"DCA"})
                        record_trade("DCA-BUY",price,amt)
                        log("DCA averaging down @ $"+str(price))
        if state["daily_loss"] >= cfg["max_loss"]:
            log("Daily loss limit reached — pausing 1hr", "WARN"); time.sleep(3600)
        time.sleep(60)

def _init_grid_pair(pair):
    """Initialize grid state for a pair, return dict with all local vars."""
    price = get_price(pair)
    if price <= 0: return None
    levels=5; spread=cfg.get("base_spread", 0.05)
    # Dynamic spread: widen in volatile markets
    if cfg.get("dynamic_spread", True):
        try:
            ph = state.get("price_history", [])
            if len(ph) >= 20:
                prices = [p["value"] for p in ph[-20:] if p.get("value")]
                if prices:
                    avg = sum(prices)/len(prices)
                    var = sum((p-avg)**2 for p in prices)/(len(prices)-1 or 1)
                    vol = (var**0.5)/avg if avg>0 else 0
                    spread = min(spread * (1 + vol * 10), spread * 3)  # max 3x
        except: pass
    grids = [round(price*(1-spread)+i*(price*spread*2/levels),4) for i in range(levels+1)]
    mid_idx = len(grids) // 2
    return {
        "grids": grids, "mid_idx": mid_idx, "filled": {},
        "trailing_pct": 0.5, "trailing_high": 0.0, "trailing_sell_active": False,
        "trailing_low": 0.0, "trailing_buy_active": False, "dip_occurred": False,
        "price": price, "levels": levels, "spread": spread,
    }

def _grid_sync_state(pair, gs, grids, mid_idx, filled, trailing_sell_active, trailing_high):
    """Sync per-pair grid state to state dict for dashboard display."""
    gp = state["grid_pairs"].get(pair, {})
    gp.update({
        "grid_levels": grids[:], "grid_buy_zone": grids[mid_idx], "grid_mid_idx": mid_idx,
        "grid_filled": {k: v for k, v in filled.items()},
        "grid_trailing_active": trailing_sell_active, "grid_trailing_high": trailing_high,
        "grids": grids[:], "filled": filled, "mid_idx": mid_idx,
        "trailing_sell_active": trailing_sell_active, "trailing_high": trailing_high,
    })
    state["grid_pairs"][pair] = gp
    # Also set top-level state for backward compat (shows active pair's data)
    if state.get("pair") == pair:
        state["grid_levels"] = grids[:]
        state["grid_buy_zone"] = grids[mid_idx]
        state["grid_mid_idx"] = mid_idx
        state["grid_filled"] = filled
        state["grid_trailing_active"] = trailing_sell_active
        state["grid_trailing_high"] = trailing_high

def run_grid():
    pair = state.get("pair","SOL/USDC")
    if pair not in state["active_pairs"]:
        state["active_pairs"].append(pair)
    # Initialize per-pair state for any new pair
    for p in state["active_pairs"]:
        if p not in state["grid_pairs"]:
            gs = _init_grid_pair(p)
            if gs:
                state["grid_pairs"][p] = gs
                log("Grid initialized for "+p+": "+str(gs["grids"]), "INFO")
    if not state["active_pairs"]:
        log("No active pairs to grid", "WARN"); return
    log("Grid started on "+str(state["active_pairs"])+" ("+state["mode"].upper()+")")

    while state["running"] and state["strategy"]=="grid":
        # Check for new pairs added mid-run
        for p in list(state["active_pairs"]):
            if p not in state["grid_pairs"]:
                gs = _init_grid_pair(p)
                if gs:
                    state["grid_pairs"][p] = gs
                    log("Grid initialized for "+p+": "+str(gs["grids"]), "INFO")
        for pair in list(state["active_pairs"]):
            gs = state["grid_pairs"].get(pair)
            if not gs: continue
            grids = gs["grids"]; mid_idx = gs["mid_idx"]; filled = gs["filled"]
            trailing_pct = gs["trailing_pct"]; trailing_high = gs["trailing_high"]
            trailing_sell_active = gs["trailing_sell_active"]
            trailing_low = gs["trailing_low"]; trailing_buy_active = gs["trailing_buy_active"]
            dip_occurred = gs["dip_occurred"]; levels = gs["levels"]; spread = gs["spread"]

            price = get_price(pair)
            if price <= 0:
                _grid_sync_state(pair, gs, grids, mid_idx, filled, trailing_sell_active, trailing_high)
                time.sleep(5); continue

            # ── Pause check: wait while paused ──
            while state["paused"]:
                time.sleep(1)
                price = get_price(pair)
                if price <= 0: break

            # ── Grid re-centering ──
            if (price < grids[0] * 0.98 or price > grids[-1] * 1.02) or (not filled and price >= grids[mid_idx]):
                if not filled and price > grids[mid_idx]:
                    log("["+pair+"] Grid re-centering: no positions at $"+str(price))
                else:
                    log("["+pair+"] Grid re-centering: price $"+str(price)+" outside ["+str(round(grids[0],2))+","+str(round(grids[-1],2))+"]")
                grids = [round(price*(1-spread)+i*(price*spread*2/levels),4) for i in range(levels+1)]
                mid_idx = len(grids) // 2
                trailing_sell_active = False; trailing_high = 0.0
                trailing_buy_active = False; trailing_low = 0.0; dip_occurred = False
                state["partial_positions"] = {}
                log("["+pair+"] Grid re-centered: "+str(grids)+" buy_zone=<="+str(grids[mid_idx]))

            bal = get_balance()
            effective_bal = bal + (state.get("compound_profit", 0) if cfg.get("auto_compound", True) else 0)
            size = min(effective_bal*cfg["risk_pct"]/100, cfg["max_pos"])/levels
            for i,g in enumerate(grids[:-1]):
                ng = grids[i+1]
                if g <= price < ng:
                    is_buy_zone = i < mid_idx
                    # ── BUY ZONE: trailing buy (buy on bounce) ──
                    if is_buy_zone:
                        # Track the low
                        if not trailing_buy_active and i not in filled:
                            trailing_buy_active = True
                            trailing_low = price
                            dip_occurred = False
                        elif trailing_buy_active:
                            if price < trailing_low:
                                trailing_low = price
                                dip_occurred = True
                        dip_mult = 1.5 if state.get("dip_active") else 1.0
                        # Buy: immediately if no dip, or on 0.5% bounce if dipped
                        if trailing_buy_active and i not in filled and size > 1:
                            should_buy = (not dip_occurred) or (price >= trailing_low * (1 + trailing_pct / 100))
                            if should_buy:
                                amt = round(size*dip_mult/price,6)
                                if place_order(pair,"buy",amt):
                                    filled[i]={"price":price,"amount":amt}
                                    state["positions"].append({"price":price,"amount":amt,"grid":i,"strategy":"Grid"})
                                    record_trade("GRID-BUY",price,amt)
                                    log("["+pair+"] BUY level "+str(i)+" @ $"+str(round(price,2))+(" (low $"+str(round(trailing_low,2))+" +"+str(trailing_pct)+"% bounce)" if dip_occurred else " (no dip)"))
                                    send_telegram("🟢 <b>BUY</b> "+state["pair"]+"\nLevel: "+str(i)+"\nPrice: $"+str(round(price,2))+"\nAmount: "+str(round(amt,6))+"\nMode: "+("LIVE" if not state["paper_trading"] else "PAPER"))
                                    trailing_buy_active = False
                                    trailing_low = 0.0
                                    # Reset sell trailing too, new position opened
                                    trailing_sell_active = False
                                    trailing_high = 0.0
                                    state["grid_trailing_active"] = trailing_sell_active
                                    state["grid_trailing_high"] = trailing_high
                    else:
                        # Reset buy trailing when leaving buy zone
                        if trailing_buy_active:
                            trailing_buy_active = False
                            trailing_low = 0.0
                            dip_occurred = False
                            state["grid_trailing_active"] = trailing_sell_active
                            state["grid_trailing_high"] = trailing_high

                    # ── Stop-loss check: immediate sell if position drops too far ──
                    stop_pct = cfg.get("grid_stop_loss_pct", 8)
                    for sl_buy_idx in sorted(list(filled.keys())):
                        sl_bp = filled[sl_buy_idx]["price"]
                        sl_loss = (price - sl_bp) / sl_bp * 100
                        if sl_loss < -stop_pct:
                            sl_amt = filled[sl_buy_idx]["amount"]
                            if place_order(pair,"sell",sl_amt):
                                sl_pnl = (price - sl_bp) * sl_amt
                                state["pnl"] += sl_pnl
                                record_trade("STOP-LOSS",price,sl_amt,round(sl_pnl,2))
                                log("["+pair+"] STOP-LOSS @ $"+str(round(price,2))+" (bought $"+str(round(sl_bp,2))+" loss "+str(round(abs(sl_loss),1))+"%)")
                                del filled[sl_buy_idx]
                                state["positions"]=[p for p in state["positions"] if p.get("grid")!=sl_buy_idx]
                    # ── SELL ZONE: trailing take profit ──
                    if not is_buy_zone:
                        if not trailing_sell_active and filled:
                            trailing_sell_active = True
                            trailing_high = price
                            state["grid_trailing_active"] = trailing_sell_active
                            state["grid_trailing_high"] = trailing_high
                            log("["+pair+"] Trailing sell active at $"+str(price))
                        elif trailing_sell_active:
                            if price > trailing_high:
                                trailing_high = price
                                state["grid_trailing_high"] = trailing_high
                                log("["+pair+"] Trailing high updated to $"+str(price))
                        # Sell when price drops trailing_pct% below peak
                        if trailing_sell_active and price <= trailing_high * (1 - trailing_pct / 100):
                            for buy_idx in sorted(filled.keys()):
                                if buy_idx < i:
                                    amt = filled[buy_idx]["amount"]
                                    buy_price = filled[buy_idx]["price"]
                                    partial_pct = cfg.get("partial_sell_pct", 50)
                                    # Check if this position still has a partial remainder
                                    partial_key = str(buy_idx)
                                    is_partial_sell = cfg.get("partial_sell_pct", 50) < 100
                                    sell_amt = amt
                                    # ── Partial sell logic ──
                                    if is_partial_sell and partial_key not in state.get("partial_positions", {}):
                                        # First sell: only sell partial_pct%
                                        sell_amt = amt * partial_pct / 100
                                        keep_amt = amt - sell_amt
                                        state["partial_positions"][partial_key] = {
                                            "amount": keep_amt, "buy_price": buy_price,
                                            "orig_amount": amt, "price": price
                                        }
                                        # Update filled entry to reflect kept amount
                                        filled[buy_idx]["amount"] = keep_amt
                                        log("PARTIAL SELL: sold "+str(round(sell_amt,6))+" ("
                                            +str(int(partial_pct))+"%) @ $"+str(round(price,2))
                                            +", keeping "+str(round(keep_amt,6))+" for wider trailing")
                                    elif partial_key in state.get("partial_positions", {}):
                                        # Second sell: sell the remainder
                                        sell_amt = amt  # sell everything left
                                        if partial_key in state["partial_positions"]:
                                            del state["partial_positions"][partial_key]
                                    if place_order(pair,"sell",sell_amt):
                                        pnl=(price-buy_price)*sell_amt
                                        state["pnl"]+=pnl
                                        state["daily_pnl"] = state.get("daily_pnl",0)+pnl
                                        if cfg.get("auto_compound", True) and pnl > 0:
                                            state["compound_profit"] += pnl
                                        tag = "GRID-PARTIAL" if is_partial_sell else "GRID-SELL"
                                        record_trade(tag,price,sell_amt,round(pnl,2))
                                        log("SELL level "+str(buy_idx)+" @ $"+str(round(price,2))+" (peak $"+str(round(trailing_high,2))+" missed $"+str(round(trailing_high-price,2))+" PnL $"+str(round(pnl,2))+")")
                                        log("TRADE SUMMARY: bought $"+str(round(buy_price,2))+" sold $"+str(round(price,2))+" peak $"+str(round(trailing_high,2))+" missed $"+str(round(trailing_high-price,2))+" PnL $"+str(round(pnl,2)))
                                        send_telegram("🔴 <b>SELL</b> "+state["pair"]+"\nBought: $"+str(round(buy_price,2))+"\nSold: $"+str(round(price,2))+"\nPnL: $"+str(round(pnl,2))+"\nTag: "+tag+"\nMode: "+("LIVE" if not state["paper_trading"] else "PAPER"))
                                        if is_partial_sell and partial_key in state.get("partial_positions",{}):
                                            # Don't delete the position yet — still holding remainder
                                            trailing_sell_active = False
                                            trailing_high = 0.0
                                            state["grid_trailing_active"] = False
                                            state["grid_trailing_high"] = 0.0
                                            state["grid_filled"] = {k: v for k, v in filled.items()}
                                            break
                                        else:
                                            del filled[buy_idx]
                                            state["positions"]=[p for p in state["positions"] if p.get("grid")!=buy_idx]
                                            trailing_sell_active = False
                                            trailing_high = 0.0
                                            state["grid_trailing_active"] = False
                                            state["grid_trailing_high"] = 0.0
                                            state["grid_filled"] = {k: v for k, v in filled.items()}
                                            break
                    else:
                        # Price back in buy zone — reset sell trailing
                        if trailing_sell_active:
                            log("["+pair+"] Trailing sell reset — price back in buy zone")
                            trailing_sell_active = False
                            trailing_high = 0.0
            # ── Daily loss limit check ──
            now = int(time.time())
            today_midnight = now - (now % 86400)
            if state.get("last_midnight",0) < today_midnight:
                state["daily_pnl"] = 0.0
                state["last_midnight"] = today_midnight
            # Track peak balance
            b = get_balance()
            if b > state.get("peak_balance", 0):
                state["peak_balance"] = b
            # Drawdown check
            dd_pct = cfg.get("max_drawdown_pct", 20)
            pk = state.get("peak_balance", 0)
            if pk > 0 and b < pk * (1 - dd_pct/100):
                log("DRAWDOWN STOP: balance $"+str(round(b,2))+" < "+str(round(pk*(1-dd_pct/100),2))+" ("+str(int(dd_pct))+"% drawdown)", "WARN")
                state["running"] = False
                state["strategy"] = None
                state["emergency_stop"] = True
                return
            dl = cfg.get("daily_loss_limit", 200)
            if state["daily_pnl"] < -dl:
                log("DAILY LOSS LIMIT: $"+"{:.2f}".format(-state["daily_pnl"])+" exceeds $"+str(dl), "WARN")
                state["running"] = False
                state["strategy"] = None
                state["emergency_stop"] = True
                return
            # Save per-pair state back
            gs.update({
                "grids": grids, "mid_idx": mid_idx, "filled": filled,
                "trailing_high": trailing_high, "trailing_sell_active": trailing_sell_active,
                "trailing_low": trailing_low, "trailing_buy_active": trailing_buy_active,
                "dip_occurred": dip_occurred,
            })
            _grid_sync_state(pair, gs, grids, mid_idx, filled, trailing_sell_active, trailing_high)
        time.sleep(30)

def run_scalp():
    log("Scalping started on "+state["pair"]+" ("+state["mode"].upper()+")")
    prices=[]; position=None
    while state["running"] and state["strategy"]=="scalp":
        while state["paused"]: time.sleep(1)
        price=get_price(state["pair"])
        if price<=0: time.sleep(10); continue
        prices.append(price)
        if len(prices)>20: prices.pop(0)
        if len(prices)<10: time.sleep(10); continue
        sma=sum(prices)/len(prices)
        bal=get_balance()
        size=min(bal*cfg["risk_pct"]/100,cfg["max_pos"])
        if position is None and price<sma*0.999 and size>1:
            amt=round(size/price,6)
            if place_order(pair,"buy",amt):
                position={"price":price,"amount":amt}
                state["positions"]=[{"price":price,"amount":amt,"strategy":"Scalp"}]
                record_trade("SCALP-BUY",price,amt)
                log("Scalp BUY @ $"+str(price))
        elif position:
            gain=(price-position["price"])/position["price"]*100
            loss=(position["price"]-price)/position["price"]*100
            if gain>=cfg["take_profit"]/3 or loss>=cfg["stop_loss"]/2:
                if place_order(pair,"sell",position["amount"]):
                    pnl=(price-position["price"])*position["amount"]
                    state["pnl"]+=pnl
                    if pnl<0: state["daily_loss"]+=abs(pnl)
                    record_trade("SCALP-SELL",price,position["amount"],round(pnl,2))
                    log("Scalp SELL @ $"+str(price)+" PnL: $"+str(round(pnl,2)))
                    position=None; state["positions"]=[]
        time.sleep(10)

def run_copy():
    source=cfg["source_wallet"]
    log("Copy Trading watching: "+source)
    while state["running"] and state["strategy"]=="copy":
        while state["paused"]: time.sleep(1)
        log("Monitoring "+source+" for trades...")
        time.sleep(60)

def run_arbitrage():
    mode = "PAPER" if state["paper_trading"] else "LIVE"
    chain = state.get("chain","ethereum")
    log("Arbitrage started ["+mode+" MODE] on "+chain+" — min spread: "+str(cfg["min_arb_spread"])+"%")
    while state["running"] and state["strategy"]=="arb":
        while state["paused"]: time.sleep(1)
        # Don't scan if a trade is in progress
        if state["trading_lock"]:
            time.sleep(5)
            continue

        # Cooldown between trades — wait 15s after last trade
        time_since_last = time.time() - state["last_trade_time"]
        if time_since_last < 15:
            time.sleep(15 - time_since_last)
            continue

        opps = scan_arbitrage()
        # Only execute the BEST opportunity per cycle, not all of them
        for opp in opps:
            if not state["running"]: break
            if opp["executable"]:
                log("ARB opportunity: "+opp["pair"]+" spread "+str(opp["spread_pct"])+"% est profit $"+str(opp["est_profit_usd"]))
                execute_arbitrage(opp)
                break  # Stop after first executable — wait for next scan cycle
        time.sleep(30)

STRATEGIES = {"dca":run_dca,"grid":run_grid,"scalp":run_scalp,"copy":run_copy,"arb":run_arbitrage}

def start_bot(strategy, pair, mode, exchange=None, chain=None):
    if state["running"]:
        # Multi-pair: if grid is already running, add new pair without restarting
        if strategy == "grid" and pair not in state.get("active_pairs", []):
            state["active_pairs"].append(pair)
            log("Added "+pair+" to active grids ("+str(len(state["active_pairs"]))+" total)")
            return
        log("Already running — stop first","WARN"); return
    state["strategy"]=strategy
    state["pair"]=pair
    state["mode"]=mode
    if exchange: state["exchange"]=exchange
    if chain: state["chain"]=chain
    state["running"]=True
    state["error"]=None
    t=threading.Thread(target=STRATEGIES.get(strategy,run_dca),daemon=True)
    t.start()
    log("Started "+strategy.upper()+" on "+pair+" via "+mode.upper()+((" / "+chain) if mode=="dex" else ""))

def stop_bot():
    state["running"]=False
    state["strategy"]=None
    state["active_pairs"]=[]
    log("Bot stopped")

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Trading Bot Dashboard</title>
<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#080808;--card:#111;--border:#1a1a1a;--text:#eee;--text2:#888;--dim:#444;--accent:#00ff9d;--red:#ff6b6b;--blue:#4dabf7;--purple:#cc99ff;--yellow:#ffd43b}
.light{--bg:#f0f2f5;--card:#fff;--border:#d0d5dd;--text:#1a1a1a;--text2:#555;--dim:#999;--accent:#00b875;--red:#e03131;--blue:#1971c2;--purple:#7c3aed;--yellow:#e67700}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);padding:20px;transition:background .3s,color .3s}
.wrap{max-width:960px;margin:0 auto}
.head-row{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:4px}
h1{font-size:22px;font-weight:900;color:var(--text)}
.sub{font-size:13px;color:var(--dim);margin-bottom:24px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dot{width:8px;height:8px;border-radius:50%;background:#333;display:inline-block;transition:all .3s}
.dot.on{background:var(--accent);box-shadow:0 0 8px var(--accent)}
.theme-btn{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px 12px;cursor:pointer;font-size:13px;color:var(--text);transition:all .15s}
.theme-btn:hover{border-color:var(--accent)}
#chart-container{height:350px;flex:1;min-width:0;border-radius:10px;background:var(--card);border:1px solid var(--border);overflow:hidden;position:relative}
#chart-container iframe{border-radius:10px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.sl{font-size:10px;font-weight:700;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin-bottom:6px}
.sv{font-size:22px;font-weight:900;color:var(--text)}
.sv.g{color:var(--accent)}.sv.r{color:var(--red)}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px}
.ct{font-size:10px;font-weight:700;letter-spacing:2px;color:var(--accent);text-transform:uppercase;margin-bottom:14px}
.mode-tabs{display:flex;gap:0;margin-bottom:20px;border:1.5px solid var(--border);border-radius:10px;overflow:hidden}
.mode-tab{flex:1;padding:12px;text-align:center;cursor:pointer;font-weight:700;font-size:13px;color:var(--dim);background:var(--card);transition:all .15s;border:none}
.mode-tab.active{background:var(--accent)18;color:var(--accent)}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.btn{padding:9px 16px;border:1.5px solid var(--border);border-radius:8px;font-weight:700;font-size:12px;cursor:pointer;background:var(--card);color:var(--text2);transition:all .15s}
.btn:hover{border-color:var(--accent);color:var(--text)}
.btn.active-strat{background:var(--accent)18;color:var(--accent);border-color:var(--accent)}
.btn.active-pair{background:var(--blue)18;color:var(--blue);border-color:var(--blue)}
.btn.active-chain{background:var(--purple)18;color:var(--purple);border-color:var(--purple)}
.btn.active-exch{background:var(--yellow)18;color:var(--yellow);border-color:var(--yellow)}
.btn-start{background:var(--accent);color:var(--bg);border:none;padding:13px 32px;font-size:14px;border-radius:8px;font-weight:800;cursor:pointer;transition:all .15s}
.btn-start:disabled{background:var(--card);color:var(--dim);cursor:not-allowed}
.btn-stop{background:var(--red)18;color:var(--red);border:1.5px solid var(--red)33;padding:13px 24px;font-size:13px;border-radius:8px;font-weight:700;cursor:pointer}
.btn-pause{background:var(--yellow)18;color:var(--yellow);border:1.5px solid var(--yellow)33;padding:13px 24px;font-size:13px;border-radius:8px;font-weight:700;cursor:pointer}
.section-label{font-size:11px;color:var(--dim);font-weight:700;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
select.dd{width:100%;padding:10px 12px;border:1.5px solid var(--border);border-radius:8px;font-size:13px;font-weight:600;background:var(--card);color:var(--text);cursor:pointer;margin-bottom:12px;transition:all .15s;appearance:auto}
select.dd:focus{outline:none;border-color:var(--accent)}
.config-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.config-field{display:flex;flex-direction:column;gap:4px}
.config-field label{font-size:10px;color:var(--dim);font-weight:700;text-transform:uppercase;letter-spacing:1px}
.config-field input,.config-field select{padding:8px 10px;border:1.5px solid var(--border);border-radius:6px;font-size:12px;background:var(--card);color:var(--text);transition:all .15s}
.config-field input:focus,.config-field select:focus{outline:none;border-color:var(--accent)}
.preset-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.preset-btn{padding:6px 14px;border:1.5px solid var(--border);border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;background:var(--card);color:var(--text2);transition:all .15s}
.preset-btn:hover{border-color:var(--accent);color:var(--accent)}
.preset-btn.active{background:var(--accent)18;color:var(--accent);border-color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--dim);font-weight:700;text-align:left;padding:8px 0;border-bottom:1px solid var(--border);font-size:10px;letter-spacing:1px;text-transform:uppercase}
td{padding:8px 0;border-bottom:1px solid var(--border);color:var(--text2)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.badge-p{background:var(--accent)18;color:var(--accent)}
.badge-l{background:var(--red)18;color:var(--red)}
.badge-s{background:var(--yellow)18;color:var(--yellow)}
.buy{color:var(--accent);font-weight:700}.sell{color:var(--red);font-weight:700}.stop{color:var(--yellow);font-weight:700}
.log-box{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px;height:180px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.8}
.li{color:var(--text2)}.lw{color:var(--yellow)}.le{color:var(--red)}
.arb-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);font-size:12px}
.arb-spread{color:var(--accent);font-weight:800;font-size:14px}
.dex-info{background:var(--purple)11;border:1px solid var(--purple)22;border-radius:8px;padding:12px;margin-bottom:14px;font-size:12px;color:var(--purple);line-height:1.6}
.cex-info{background:var(--yellow)11;border:1px solid var(--yellow)22;border-radius:8px;padding:12px;margin-bottom:14px;font-size:12px;color:var(--yellow);line-height:1.6}
.summary-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
.summary-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;text-align:center}
.summary-card .label{font-size:9px;color:var(--dim);font-weight:700;text-transform:uppercase;letter-spacing:1px}
.summary-card .value{font-size:16px;font-weight:900;margin-top:4px;color:var(--text)}
.summary-card .value.g{color:var(--accent)}.summary-card .value.r{color:var(--red)}
.toast-container{position:fixed;top:12px;right:12px;z-index:9999;display:flex;flex-direction:column;gap:6px;pointer-events:none}
.toast{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 16px;font-size:12px;color:var(--text);box-shadow:0 4px 20px rgba(0,0,0,.4);animation:slideIn .3s ease-out;max-width:320px;pointer-events:auto}
.toast.trade{border-left:3px solid var(--accent)}.toast.error{border-left:3px solid var(--red)}.toast.info{border-left:3px solid var(--blue)}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
@keyframes fadeOut{from{opacity:1}to{opacity:0}}
.action-bar{display:flex;gap:10px;margin-top:8px;flex-wrap:wrap;align-items:center}
.twocol{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.stats{grid-template-columns:1fr 1fr}.summary-cards{grid-template-columns:1fr 1fr}.twocol{grid-template-columns:1fr}.config-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div id="toast-container" class="toast-container"></div>
<div class="wrap">
  <div class="head-row">
    <div><h1>Trading Bot</h1><div class="sub"><span class="dot" id="dot"></span><span id="status-text">Stopped</span></div></div>
    <div style="display:flex;gap:6px">
      <button class="theme-btn" id="theme-btn" onclick="toggleTheme()">🌙 Dark</button>
      <button class="btn" onclick="exportCSV()" style="font-size:11px">&#11015; CSV</button>
      <button class="btn" onclick="killSwitch()" title="Emergency close all positions" style="font-size:11px;color:var(--red);border-color:var(--red)44">&#128721; Kill</button>
      <button class="btn" onclick="runBacktest()" style="font-size:11px">📊 Backtest</button>
    </div>
  </div>

  <div class="stats">
    <div class="stat"><div class="sl">Price (Raydium)</div><div class="sv" id="s-price">—</div></div>
    <div class="stat"><div class="sl">EVM Balance</div><div class="sv" id="s-balance">—</div></div>
    <div class="stat"><div class="sl">Solana</div><div class="sv" id="s-sol-balance" style="font-size:16px">—</div></div>
    <div class="stat"><div class="sl">Total P&amp;L</div><div class="sv" id="s-pnl">$0.00</div></div>
    <div class="stat"><div class="sl">Open Positions</div><div class="sv" id="s-pos">0</div></div>
    <div class="stat"><div class="sl">Mode</div><div class="sv" id="s-mode" style="font-size:14px">—</div></div>
  </div>

  <div style="display:flex;gap:16px;align-items:stretch">
    <div id="chart-container" style="flex:1;min-width:0"></div>
    <div class="card" id="grid-details-card" style="width:420px;flex-shrink:0;height:400px;overflow-y:auto">
      <div class="ct" style="display:flex;align-items:center;gap:8px">
        Grid Details
        <span id="gdt-status" style="font-size:11px;font-weight:400;color:var(--dim)"></span>
      </div>
      <div id="grid-details-body"></div>
    </div>
  </div>

  <div class="summary-cards" id="summary-cards">
    <div class="summary-card"><div class="label">Win Rate</div><div class="value" id="sm-winrate">0%</div></div>
    <div class="summary-card"><div class="label">Avg Profit</div><div class="value g" id="sm-avgprofit">$0.00</div></div>
    <div class="summary-card"><div class="label">Total Trades</div><div class="value" id="sm-trades">0</div></div>
    <div class="summary-card"><div class="label">Best Trade</div><div class="value g" id="sm-best">—</div></div>
  </div>

  <div class="card">
    <div class="ct">Trading Mode</div>
    <div class="mode-tabs">
      <button class="mode-tab active" id="tab-cex" onclick="setMode('cex')">CEX — Exchange Trading</button>
      <button class="mode-tab" id="tab-dex" onclick="setMode('dex')">DEX — Wallet Trading</button>
    </div>

    <div id="cex-panel">
      <div class="cex-info">Trade on centralized exchanges using your API key and secret. Set these in Render environment variables.</div>
      <div class="section-label">Exchange</div>
      <select class="dd" id="exch-select" onchange="selectExch(this.value)">
        <option value="">— Select Exchange —</option>
        <option value="binance">Binance</option>
        <option value="bybit">Bybit</option>
        <option value="okx">OKX</option>
        <option value="kucoin">KuCoin</option>
        <option value="lbank">LBank</option>
        <option value="kraken">Kraken</option>
      </select>
    </div>

    <div id="dex-panel" style="display:none">
      <div class="dex-info">Trade on-chain using your wallet. No API keys needed. Uses Uniswap + 1inch for EVM chains, Jupiter for Solana.</div>
      <div class="section-label">Chain</div>
      <select class="dd" id="chain-select" onchange="selectChain(this.value)">
        <option value="">— Select Chain —</option>
        <option value="ethereum">Ethereum</option>
        <option value="bsc">BNB Chain</option>
        <option value="base">Base</option>
        <option value="arbitrum">Arbitrum</option>
        <option value="polygon">Polygon</option>
        <option value="solana">Solana &#9889;</option>
      </select>
      <div style="background:var(--accent)11;border:1px solid var(--accent)22;border-radius:8px;padding:10px 14px;margin-bottom:4px;font-size:12px;color:var(--accent)">
        &#9889; <strong>Solana</strong> — gas &lt;$0.01 per trade, routed via Jupiter aggregator for best prices
      </div>
    </div>

    <div class="section-label" style="margin-top:16px">Strategy</div>
    <select class="dd" id="strat-select" onchange="selectStrat(this.value)">
      <option value="">— Select Strategy —</option>
      <option value="dca">DCA — Dollar Cost Average</option>
      <option value="grid">Grid Trading</option>
      <option value="scalp">Scalping</option>
      <option value="copy">Copy Trading</option>
      <option value="arb">Arbitrage</option>
    </select>

    <div class="section-label">Trading Pair</div>
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <select class="dd" id="pair-select" onchange="selectPair(this.value)" style="flex:1;margin-bottom:0">
        <option value="">— Select Pair —</option>
        <optgroup label="USDT Pairs" id="usdt-optgroup">
          <option value="BTC/USDT">BTC/USDT</option>
          <option value="ETH/USDT">ETH/USDT</option>
          <option value="BNB/USDT">BNB/USDT</option>
          <option value="SOL/USDT">SOL/USDT</option>
          <option value="MATIC/USDT">MATIC/USDT</option>
        </optgroup>
        <optgroup label="USDC Pairs" id="usdc-optgroup" style="display:none">
          <option value="SOL/USDC">SOL/USDC</option>
          <option value="BTC/USDC">BTC/USDC</option>
          <option value="ETH/USDC">ETH/USDC</option>
          <option value="JUP/USDC">JUP/USDC</option>
          <option value="WIF/USDC">WIF/USDC</option>
        </optgroup>
      </select>
      <button class="btn" onclick="switchPair()" title="One-click pair switch" style="padding:9px 12px">&#128260;</button>
    </div>

    <div class="action-bar">
      <button class="btn-start" id="start-btn" onclick="startBot()" disabled>Select options above</button>
      <button class="btn-stop" onclick="stopBot()">&#9209; Stop</button>
      <button class="btn-pause" id="pause-btn" onclick="pauseBot()" style="display:none">⏸ Pause</button>
      <button class="btn" id="paper-btn" onclick="togglePaper()" style="background:var(--yellow)18;color:var(--yellow);border-color:var(--yellow)44;padding:13px 20px">📋 Paper: ON</button>
    </div>
  </div>

  <div class="card" id="config-card" style="display:none">
    <div class="ct">Configuration</div>
    <div class="config-grid">
      <div class="config-field"><label>Max Leverage</label><input type="number" id="cfg-leverage" value="3" min="1" max="10"/></div>
      <div class="config-field"><label>Max Position ($)</label><input type="number" id="cfg-maxpos" value="1000" min="0"/></div>
      <div class="config-field"><label>Cooldown (sec)</label><input type="number" id="cfg-cooldown" value="30" min="5"/></div>
      <div class="config-field"><label>Slippage %</label><input type="number" id="cfg-slippage" value="0.5" min="0.1" step="0.1"/></div>
    </div>
    <div class="section-label">Quick Presets</div>
    <div class="preset-row">
      <button class="preset-btn" onclick="applyPreset('conservative')">&#128737; Conservative</button>
      <button class="preset-btn" onclick="applyPreset('moderate')">&#9878; Moderate</button>
      <button class="preset-btn" onclick="applyPreset('aggressive')">&#128640; Aggressive</button>
    </div>
    <button class="btn" onclick="saveConfig()" style="margin-top:8px;background:var(--accent)18;color:var(--accent);border-color:var(--accent)">&#128190; Save Config</button>
  </div>

  <div class="card" id="arb-card" style="display:none">
    <div class="ct">Arbitrage Opportunities</div>
    <div id="arb-list"><div style="color:var(--dim);font-size:13px">Scanning for opportunities...</div></div>
  </div>

  <div class="card">
    <div class="ct" style="display:flex;justify-content:space-between;align-items:center">
      <span>Trade History</span>
      <button class="btn" onclick="exportCSV()" style="font-size:10px;padding:4px 10px">&#11015; CSV</button>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Time</th><th>Action</th><th>Price</th><th>Amount</th><th>P&amp;L</th><th>Via</th></tr></thead>
        <tbody id="trades-body"><tr><td colspan="6" style="color:var(--dim);text-align:center;padding:20px">No trades yet</td></tr></tbody>
      </table>
    </div>
  </div>



  <div class="card">
    <div class="ct">Live Log</div>
    <div class="log-box" id="log-box"></div>
  </div>
</div>

<script>
var sel = {mode:"cex", strat:null, pair:null, exch:null, chain:null};
var isDark = true;
var tradeLog = [];
var _lastTradeTime = null;
var toastId = 0;
var notifRequested = false;

function toggleTheme() {
  isDark = !isDark;
  document.body.classList.toggle("light", !isDark);
  document.getElementById("theme-btn").textContent = isDark ? "🌙 Dark" : "☀ Light";
  if (chart) setTimeout(function() { updateChartTheme(isDark); }, 100);
}


var chart = null;
var candleSeries = null;
var gridLines = [];

function aggregateCandles(data, intervalSec) {
  var candles = [], current = null;
  data.forEach(function(d) {
    var bucket = Math.floor(d.time / intervalSec) * intervalSec;
    if (!current || current.time !== bucket) {
      if (current) candles.push(current);
      current = {time: bucket, open: d.value, high: d.value, low: d.value, close: d.value};
    } else {
      current.high = Math.max(current.high, d.value);
      current.low = Math.min(current.low, d.value);
      current.close = d.value;
    }
  });
  if (current) candles.push(current);
  return candles;
}
function initChart() {
  try {
    chart = LightweightCharts.createChart(document.getElementById("chart-container"), {
      width: document.getElementById("chart-container").clientWidth || 600,
      height: 350,
      layout: {
        background: {type: "solid", color: "transparent"},
        textColor: "#888",
      },
      grid: {
        vertLines: {color: "#1a1a1a"},
        horzLines: {color: "#1a1a1a"},
      },
      crosshair: {
        vertLine: {color: "#444", labelBackgroundColor: "#111"},
        horzLine: {color: "#444", labelBackgroundColor: "#111"},
      },
      timeScale: {
        borderColor: "#1a1a1a",
        timeVisible: true,
        secondsVisible: false,
        barSpacing: 3,
      },
      rightPriceScale: {
        borderColor: "#1a1a1a",
      },
    });
    candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: "#00ff9d",
      downColor: "#ff6b6b",
      borderUpColor: "#00ff9d",
      borderDownColor: "#ff6b6b",
      wickUpColor: "#00ff9d",
      wickDownColor: "#ff6b6b",
      priceFormat: {type: "price", precision: 4, minMove: 0.0001},
    });
  } catch(e) { console.log("Chart init error:", e); }
}

function updateChartTheme(isDarkMode) {
  if (!chart) return;
  chart.applyOptions({
    layout: {
      textColor: isDarkMode ? "#888" : "#666",
    },
    grid: {
      vertLines: {color: isDarkMode ? "#1a1a1a" : "#e0e0e0"},
      horzLines: {color: isDarkMode ? "#1a1a1a" : "#e0e0e0"},
    },
    timeScale: {
      borderColor: isDarkMode ? "#1a1a1a" : "#d0d5dd",
    },
    rightPriceScale: {
      borderColor: isDarkMode ? "#1a1a1a" : "#d0d5dd",
    },
  });
}

function updateChart(data, gridLevels, gridBuyZone, pair) {
  if (!chart || !candleSeries) return;
  // Remove old grid lines (do this first, regardless of data)
  try {
    gridLines.forEach(function(l) { chart.removeSeries(l); });
  } catch(e) { console.log("Grid remove error:", e); }
  gridLines = [];
  if (!data || data.length < 2) return;

  // Update candles
  var candles = aggregateCandles(data, 60);
  candleSeries.setData(candles);
  var dataStart = candles[0].time;
  var dataEnd = candles[candles.length - 1].time;

  // Fixed 3px candles — always 3px, scroll to latest
  chart.applyOptions({ timeScale: { barSpacing: 3 } });
  chart.timeScale().scrollToPosition(candles.length, false);

  // Grid overlay
  if (!gridLevels || gridLevels.length < 2) return;

  var midPrice = gridBuyZone;
  var buyZone = gridLevels.filter(function(g) { return g <= midPrice; });
  var sellZone = gridLevels.filter(function(g) { return g > midPrice; });

  try {
    // Buy zone lines (green)
    buyZone.forEach(function(g) {
      var s = chart.addSeries(LightweightCharts.LineSeries, {
        color: "#00ff9d44",
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      s.setData([{time: dataStart, value: g}, {time: dataEnd, value: g}]);
      gridLines.push(s);
    });

    // Sell zone lines (red)
    sellZone.forEach(function(g) {
      var s = chart.addSeries(LightweightCharts.LineSeries, {
        color: "#ff6b6b44",
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      s.setData([{time: dataStart, value: g}, {time: dataEnd, value: g}]);
      gridLines.push(s);
    });

    // Midpoint line (yellow, thicker)
    var midLine = chart.addSeries(LightweightCharts.LineSeries, {
      color: "#ffd43b88",
      lineWidth: 2,
      lineStyle: 2,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    midLine.setData([{time: dataStart, value: midPrice}, {time: dataEnd, value: midPrice}]);
    gridLines.push(midLine);
  } catch(e) { console.log("Grid overlay error:", e); }
}


function showToast(msg, type) {
  var c = document.getElementById("toast-container");
  var t = document.createElement("div");
  t.className = "toast " + (type || "info");
  t.textContent = msg;
  t.id = "t" + (++toastId);
  c.appendChild(t);
  setTimeout(function(){ var el = document.getElementById(t.id); if(el) el.style.animation = "fadeOut .3s ease-out"; setTimeout(function(){ if(el) el.remove(); }, 300); }, 4000);
}

function playBeep() {
  try {
    var ctx = new (window.AudioContext || window.webkitAudioContext)();
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = 880;
    gain.gain.value = 0.08;
    osc.start(); gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.12);
    osc.stop(ctx.currentTime + 0.12);
  } catch(e) {}
}

function sendNotif(title, body) {
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(title, {body: body});
  }
}

function requestNotif() {
  if (!notifRequested && "Notification" in window) {
    Notification.requestPermission();
    notifRequested = true;
  }
}

function exportCSV() {
  if (tradeLog.length === 0) { showToast("No trades to export", "error"); return; }
  var headers = "Time,Action,Price,Amount,P&L,Via";
  var rows = tradeLog.map(function(t) {
    return [t.time, t.action, t.price, t.amount, t.pnl, t.via].join(",");
  });
  var csv = [headers].concat(rows).join("\\n");
  var blob = new Blob([csv], {type: "text/csv"});
  var url = URL.createObjectURL(blob);
  var a = document.createElement("a");
  a.href = url; a.download = "trades_" + new Date().toISOString().slice(0,10) + ".csv";
  a.click(); URL.revokeObjectURL(url);
  showToast("Exported " + tradeLog.length + " trades", "info");
}

function switchPair() {
  var common = ["SOL/USDC","BTC/USDC","ETH/USDC","SOL/USDT","BTC/USDT","ETH/USDT"];
  var current = sel.pair;
  if (!current) { showToast("Select a pair first", "error"); return; }
  var idx = common.indexOf(current);
  var next = common[(idx + 1) % common.length];
  var ps = document.getElementById("pair-select");
  ps.value = next;
  selectPair(next);
  showToast("Switched to " + next, "info");
}

function applyPreset(name) {
  var presets = {
    conservative: {leverage: 2, maxpos: 500, cooldown: 60, slippage: 0.3},
    moderate: {leverage: 3, maxpos: 1000, cooldown: 30, slippage: 0.5},
    aggressive: {leverage: 5, maxpos: 2000, cooldown: 15, slippage: 1.0}
  };
  var p = presets[name];
  document.getElementById("cfg-leverage").value = p.leverage;
  document.getElementById("cfg-maxpos").value = p.maxpos;
  document.getElementById("cfg-cooldown").value = p.cooldown;
  document.getElementById("cfg-slippage").value = p.slippage;
  document.querySelectorAll(".preset-btn").forEach(function(b) { b.classList.remove("active"); });
  event.target.classList.add("active");
  showToast("Preset '" + name + "' applied", "info");
}

function saveConfig() {
  var cfg = {
    max_leverage: parseInt(document.getElementById("cfg-leverage").value) || 3,
    max_position: parseInt(document.getElementById("cfg-maxpos").value) || 1000,
    cooldown: parseInt(document.getElementById("cfg-cooldown").value) || 30,
    slippage: parseFloat(document.getElementById("cfg-slippage").value) || 0.5
  };
  apiFetch("/config", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(cfg)
  }).then(function(r) { return r.json(); }).then(function(d) {
    showToast("Config saved", "info");
  }).catch(function() { showToast("Failed to save config", "error"); });
}

function setMode(m) {
  sel.mode = m;
  document.getElementById("tab-cex").className = "mode-tab" + (m=="cex"?" active":"");
  document.getElementById("tab-dex").className = "mode-tab" + (m=="dex"?" active":"");
  document.getElementById("cex-panel").style.display = m=="cex"?"block":"none";
  document.getElementById("dex-panel").style.display = m=="dex"?"block":"none";
  updateBtn();
}

function selectStrat(s) {
  if (!s) return;
  sel.strat = s;
  document.getElementById("arb-card").style.display = s=="arb"?"block":"none";
  document.getElementById("config-card").style.display = "block";
  updateBtn();
}

function selectPair(p) {
  if (!p) return;
  sel.pair = p;
  updateBtn();
  // Immediately refresh grid details for selected pair
  refresh();
}

function selectExch(e) {
  if (!e) return;
  sel.exch = e;
  updateBtn();
}

function selectChain(c) {
  if (!c) return;
  sel.chain = c;
  sel.pair = null;
  document.getElementById("pair-select").value = "";
  document.getElementById("usdt-optgroup").style.display = (c === "solana") ? "none" : "";
  document.getElementById("usdc-optgroup").style.display = (c === "solana") ? "" : "none";
  updateBtn();
}

function updateBtn() {
  var btn = document.getElementById("start-btn");
  var cexReady = sel.mode == "cex" && sel.exch && sel.strat && sel.pair;
  var dexReady = sel.mode == "dex" && sel.chain && sel.strat && sel.pair;
  if (cexReady || dexReady) {
    btn.disabled = false;
    btn.textContent = "Start " + sel.strat.toUpperCase() + " on " + sel.pair;
  } else {
    btn.disabled = true;
    btn.textContent = "Select options above";
  }
}

function startBot() {
  var params = "strategy=" + sel.strat + "&pair=" + encodeURIComponent(sel.pair) + "&mode=" + sel.mode;
  if (sel.mode == "cex" && sel.exch) params += "&exchange=" + sel.exch;
  if (sel.mode == "dex" && sel.chain) params += "&chain=" + sel.chain;
  apiFetch("/start?" + params).then(function(r) { return r.json(); }).then(function(d) {
    showToast("Bot started: " + sel.strat.toUpperCase(), "info");
    document.getElementById("pause-btn").style.display = "inline-block";
    document.getElementById("pause-btn").textContent = "⏸ Pause";
  });
}

function stopBot() {
  apiFetch("/stop").then(function(r) { return r.json(); }).then(function(d) {
    showToast("Bot stopped", "info");
    document.getElementById("pause-btn").style.display = "none";
  });
}

function pauseBot() {
  apiFetch("/pause").then(function(r) { return r.json(); }).then(function(d) {
    var paused = d.paused || d.status === "paused";
    document.getElementById("pause-btn").textContent = paused ? "▶ Resume" : "⏸ Pause";
    showToast(paused ? "Bot paused" : "Bot resumed", "info");
  }).catch(function() {
    var btn = document.getElementById("pause-btn");
    var paused = btn.textContent.indexOf("Pause") !== -1;
    btn.textContent = paused ? "▶ Resume" : "⏸ Pause";
    showToast(paused ? "Bot paused" : "Bot resumed", "info");
  });
}

function pnlHtml(v) {
  if (v == null || v === undefined) return "—";
  var cls = v >= 0 ? "badge badge-p" : "badge badge-l";
  return "<span class='" + cls + "'>" + (v >= 0 ? "+" : "") + "$" + Math.abs(v).toFixed(2) + "</span>";
}

function killSwitch() {
  if (!confirm("🛑 KILL SWITCH: Close ALL positions on ALL pairs? This cannot be undone.")) return;
  fetch("/kill",{method:"POST"}).then(function(r){return r.json()}).then(function(d){
    showToast("KILL: "+d.closed+" positions closed, $"+d.total_value.toFixed(2),"error");
  }).catch(function(){showToast("Kill failed","error")});
}

function runBacktest() {
  var pair = document.getElementById("pair-select").value;
  var strategy = document.getElementById("strat-select").value;
  showToast("Running backtest on " + pair + "...", "info");
  apiFetch("/backtest", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pair: pair, strategy: strategy})
  })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.error) { showToast("Backtest error: " + d.error, "error"); return; }
      var msg = "Backtest: " + d.total_trades + " trades | Win: " + d.win_rate + "% | PnL: $" + (d.total_pnl||0).toFixed(2) + " | Drawdown: " + (d.max_drawdown||0).toFixed(1) + "%";
      showToast(msg, "info");
      if (d.trades && d.trades.length) {
        var lines = d.trades.slice(0, 5).map(function(t) { return t.action + " @ $" + t.price.toFixed(2) + " PnL: $" + (t.pnl||0).toFixed(2); });
        addLog("Backtest: " + lines.join(" | "));
      } else {
        showToast("Backtest done: 0 simulated trades in range", "info");
      }
    })
    .catch(function(e) { showToast("Backtest failed: " + e, "error"); });
}

function togglePaper() {
  apiFetch("/toggle_paper").then(function(r) { return r.json(); }).then(function(d) {
    var btn = document.getElementById("paper-btn");
    var on = d.paper_trading;
    btn.textContent = "📋 Paper: " + (on ? "ON" : "OFF");
    btn.style.color = on ? "var(--yellow)" : "var(--red)";
    btn.style.borderColor = on ? "var(--yellow)44" : "var(--red)44";
    btn.style.background = on ? "var(--yellow)18" : "var(--red)18";
    showToast("Paper trading: " + (on ? "ON" : "OFF"), "info");
  });
}

function refresh() {
  apiFetch("/state").then(function(r) { return r.json(); }).then(function(d) {
    var on = d.running;
    document.getElementById("dot").className = "dot" + (on ? " on" : "");
    document.getElementById("status-text").textContent = on ? "Running — " + (d.strategy || "").toUpperCase() + " on " + (d.active_pairs ? d.active_pairs.join(", ") : d.pair) + " (" + (d.mode || "").toUpperCase() + ")" : "Stopped";
    document.getElementById("s-price").textContent = d.price > 0 ? "$" + d.price.toFixed(4) : "—";
    if (d.price_history && d.price_history.length > 1) {
      // Show grid for currently selected pair
      var viewPair = sel.pair || d.pair || "SOL/USDC";
      var gp = d.grid_pairs && d.grid_pairs[viewPair];
      var levels = gp ? gp.grids : d.grid_levels;
      var buyZone = gp ? gp.grids[gp.mid_idx] : d.grid_buy_zone;
      updateChart(d.price_history, levels, buyZone, viewPair);
      // Override grid details for selected pair
      if (gp) {
        d.grid_levels = gp.grids;
        d.grid_buy_zone = gp.grids[gp.mid_idx];
        d.grid_filled = gp.filled;
        d.grid_mid_idx = gp.mid_idx;
        d.grid_trailing_active = gp.trailing_sell_active;
        d.grid_trailing_high = gp.trailing_high;
      }
    }
    document.getElementById("s-balance").textContent = d.balance > 0 ? "$" + d.balance.toFixed(2) : "—";
    document.getElementById("s-sol-balance").textContent = d.sol_balance > 0 ? "$" + d.sol_balance.toFixed(2) + " (USDC: $" + d.sol_usdc.toFixed(2) + " USDT: $" + d.sol_usdt.toFixed(2) + ")" : "—";
    document.getElementById("s-mode").textContent = d.paper_trading ? "📋 PAPER" : "🔴 LIVE";
    document.getElementById("s-mode").style.color = d.paper_trading ? "var(--yellow)" : "var(--red)";
    document.getElementById("s-pnl").innerHTML = d.pnl != null ? pnlHtml(d.pnl) : "$0.00";
    document.getElementById("s-pos").textContent = d.positions != null && d.positions.length != null ? d.positions.length : 0;

    // Update pause button state
    if (on) {
      document.getElementById("pause-btn").style.display = "inline-block";
      document.getElementById("pause-btn").textContent = d.paused ? "▶ Resume" : "⏸ Pause";
    } else {
      document.getElementById("pause-btn").style.display = "none";
    }

    // Update summary cards
    document.getElementById("sm-winrate").textContent = d.win_rate != null ? d.win_rate + "%" : "0%";
    document.getElementById("sm-avgprofit").textContent = d.avg_profit != null ? "$" + d.avg_profit.toFixed(2) : "$0.00";
    document.getElementById("sm-trades").textContent = d.trades_count != null ? d.trades_count : 0;
    document.getElementById("sm-best").textContent = d.best_trade != null ? "$" + d.best_trade.toFixed(2) : "—";

    // Update trade table
    if (d.trades_list && d.trades_list.length) {
      var html = "";
      tradeLog = [];
      d.trades_list.forEach(function(t) {
        var actionClass = t.action === "buy" ? "buy" : t.action === "sell" ? "sell" : "stop";
        var pnlBadge = t.pnl != null ? pnlHtml(t.pnl) : "—";
        html += "<tr><td>" + t.time + "</td><td class='" + actionClass + "'>" + t.action.toUpperCase() + "</td><td>$" + t.price + "</td><td>$" + t.amount + "</td><td>" + pnlBadge + "</td><td>" + (t.via || "—") + "</td></tr>";
        // Log to trade log for CSV export
        tradeLog.push({time: t.time, action: t.action, price: t.price, amount: t.amount, pnl: t.pnl, via: t.via || "", strategy: d.strategy, pair: d.pair});
      });
      document.getElementById("trades-body").innerHTML = html;
    }

    // Update positions with badges
    if (d.positions_list && d.positions_list.length) {
      var pHtml = "";
      d.positions_list.forEach(function(p) {
        var badge = p.pnl != null ? pnlHtml(p.pnl) : "—";
        pHtml += "<div class='arb-row'><span>" + p.token + " @ $" + p.entry + "</span><span>" + badge + "</span></div>";
      });
      // Could add a positions card here
    }
    // ── Grid Details ──
    var gdCard = document.getElementById("grid-details-card");
    if (d.strategy === "grid" && d.grid_levels && d.grid_levels.length >= 2) {
      gdCard.style.display = "block";
      var gl = d.grid_levels;
      var midIdx = d.grid_mid_idx != null ? d.grid_mid_idx : Math.floor(gl.length / 2);
      var midPrice = gl[midIdx];
      var curPrice = d.price || 0;
      var filled = d.grid_filled || {};
      var trailActive = d.grid_trailing_active || false;
      var trailHigh = d.grid_trailing_high || 0;
      var trailingPct = 0.5;
      document.getElementById("gdt-status").textContent = trailActive ? "🔴 TRAILING SELL ACTIVE" : "\u23F8 Waiting for sell zone";
      var html = '<div style="margin-top:12px">';
      var minP = gl[0], maxP = gl[gl.length-1], range = maxP - minP;
      var curPct = range > 0 ? ((curPrice - minP) / range * 100) : 50;
      html += '<div style="position:relative;height:6px;background:linear-gradient(90deg,#00ff9d44,#ffd43b44,#ff6b6b44);border-radius:3px;margin-bottom:16px">';
      html += '<div style="position:absolute;left:' + curPct.toFixed(0) + '%;top:-4px;width:3px;height:14px;background:#3399ff;border-radius:1px"></div>';
      html += '<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-top:8px">';
      html += '<span style="color:#00ff9d">$' + minP.toFixed(0) + '</span>';
      html += '<span style="color:#ffd43b">Mid $' + midPrice.toFixed(0) + '</span>';
      html += '<span style="color:#ff6b6b">$' + maxP.toFixed(0) + '</span></div></div>';
      html += '<div style="display:grid;grid-template-columns:50px 90px 1fr 80px;gap:4px;font-size:11px;color:var(--dim);padding:4px 8px;text-transform:uppercase;letter-spacing:0.5px">';
      html += '<span>Zone</span><span>Price</span><span>Status</span><span style="text-align:right">Dist</span></div>';
      for (var i = gl.length - 1; i >= 0; i--) {
        var isMid = i === midIdx;
        var isBuy = i < midIdx;
        var isFilled = filled[i] != null;
        var isCur = (i < gl.length - 1 && curPrice >= gl[i] && curPrice < gl[i+1]) || (i === gl.length - 1 && curPrice >= gl[i]);
        var zone = isMid ? "MID" : isBuy ? "BUY" : "SELL";
        var zoneColor = isMid ? "#ffd43b" : isBuy ? "#00ff9d" : "#ff6b6b";
        var bgColor = isMid ? "#ffd43b08" : isBuy ? "#00ff9d08" : "#ff6b6b08";
        var borderColor = isMid ? "#ffd43b44" : isBuy ? "#00ff9d44" : "#ff6b6b44";
        if (isCur) { zone = "\u25CF"; zoneColor = "#3399ff"; bgColor = "#3399ff10"; borderColor = "#3399ff"; }
        if (isFilled) { zoneColor = "#00ff9d"; bgColor = "#00ff9d15"; borderColor = "#00ff9d"; }
        var dist = curPrice > 0 ? (gl[i] - curPrice) : 0;
        var distStr = dist > 0 ? "+$" + dist.toFixed(0) : dist < 0 ? "-$" + Math.abs(dist).toFixed(0) : "\u2014";
        var status = isFilled ? "\u2705 $" + filled[i].price.toFixed(0) : isCur ? "\u2190 Current" : isMid ? "Buy Zone \u2191" : "Waiting";
        if (isFilled && trailActive && i < midIdx) status = "\u2705 Trailing...";
        if (isFilled && !trailActive && i < midIdx) status = "\u2705 Filled";
        html += '<div style="display:grid;grid-template-columns:50px 90px 1fr 80px;gap:4px;align-items:center;padding:5px 8px;border-radius:4px;margin-bottom:2px;font-size:12px;background:' + bgColor + ';border-left:2px solid ' + borderColor + '">';
        html += '<span style="font-weight:600;color:' + zoneColor + ';font-size:10px;text-transform:uppercase">' + zone + '</span>';
        html += '<span style="font-family:monospace;font-weight:600">$' + gl[i].toFixed(2) + '</span>';
        html += '<span style="color:' + (isFilled ? "#00ff9d" : isCur ? "#3399ff" : "var(--text)") + '">' + status + '</span>';
        html += '<span style="text-align:right;font-family:monospace;font-size:11px;color:' + (dist > 0 ? "#ff6b6b" : dist < 0 ? "#00ff9d" : "var(--dim)") + '">' + distStr + '</span></div>';
      }
      html += '</div>';
      if (trailActive && trailHigh > 0) {
        var sellTrigger = trailHigh * (1 - trailingPct / 100);
        var distToSell = curPrice - sellTrigger;
        html += '<div style="margin-top:12px;padding:10px 12px;background:#ffd43b08;border:1px solid #ffd43b33;border-radius:6px">';
        html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">';
        html += '<span>🎯</span><span style="font-weight:600;color:#ffd43b">Take-profit triggered \u2014 waiting for pullback</span></div>';
        html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:12px">';
        html += '<div><span style="color:var(--dim);font-size:10px">Peak</span><div style="font-weight:600;font-family:monospace;color:#ffd43b">$' + trailHigh.toFixed(2) + '</div></div>';
        html += '<div><span style="color:var(--dim);font-size:10px">Sell Trigger</span><div style="font-weight:600;font-family:monospace;color:#ff6b6b">$' + sellTrigger.toFixed(2) + '</div></div>';
        html += '<div><span style="color:var(--dim);font-size:10px">Distance</span><div style="font-weight:600;font-family:monospace;color:' + (distToSell > 0 ? "#ffd43b" : "#00ff9d") + '">$' + Math.abs(distToSell).toFixed(2) + '</div></div></div></div>';
      }
      document.getElementById("grid-details-body").innerHTML = html;
    } else {
      gdCard.style.display = "block";
      document.getElementById("gdt-status").textContent = "\u23F8 Idle \u2014 start a Grid strategy to see levels";
      document.getElementById("grid-details-body").innerHTML = '<div style="padding:20px;text-align:center;color:var(--dim);font-size:13px;margin-top:40px">Start a Grid strategy to see buy/sell levels, filled positions, and trailing sell status here.</div>';
    }
    // Update log
    if (d.log && d.log.length) {
      var logHtml = d.log.slice(0, 30).map(function(l) {
        var cls = "li";
        if (l.includes("BUY") || l.includes("buy")) cls = "lw";
        if (l.includes("SELL") || l.includes("sell")) cls = "buy";
        if (l.includes("ERROR") || l.includes("error")) cls = "le";
        return "<div class='" + cls + "'>" + l + "</div>";
      }).join("");
      document.getElementById("log-box").innerHTML = logHtml;
    }

    // Toast on new trade (only once per trade)
    if (d.last_trade && d.last_trade.action && d.last_trade.time != _lastTradeTime) {
      _lastTradeTime = d.last_trade.time;
      showToast(d.last_trade.action.toUpperCase() + " " + d.last_trade.pair + " @ $" + d.last_trade.price, "trade");
      playBeep();
      requestNotif();
      sendNotif("LeverBot", d.last_trade.action.toUpperCase() + " " + d.last_trade.pair + " @ $" + d.last_trade.price);
    }

    // Update config display
    if (d.config) {
      document.getElementById("cfg-leverage").value = d.config.max_leverage || 3;
      document.getElementById("cfg-maxpos").value = d.config.max_position || 1000;
      document.getElementById("cfg-cooldown").value = d.config.cooldown || 30;
      document.getElementById("cfg-slippage").value = d.config.slippage || 0.5;
    }
  }).catch(console.error);
}

window.addEventListener("resize", function() {
  if (chart) {
    var w = document.getElementById("chart-container").clientWidth || 600;
    chart.applyOptions({width: w});
  }
});

setInterval(refresh, 3000);
refresh();
initChart();
  var API_SECRET = "{API_SECRET}";
  function apiFetch(url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (API_SECRET) opts.headers["X-API-Secret"] = API_SECRET;
    return fetch(url, opts);
  }
</script>
</body>
</html>'''

# ── HTTP Server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    API_SECRET = os.environ.get("API_SECRET", "")

    def _check_auth(self):
        sent = self.headers.get("X-API-Secret", "")
        if self.API_SECRET and sent != self.API_SECRET:
            self.respond(401, "text/plain", b"Unauthorized")
            return False
        return True

    def _auth_or_401(self):
        if not self._check_auth():
            return False
        return True

    def do_GET(self):
        parsed=urlparse(self.path)
        path=parsed.path
        params=parse_qs(parsed.query)

        if path=="/":
            self.respond(200,"text/html",DASHBOARD.replace("{API_SECRET}", os.environ.get("API_SECRET","")).encode())
        elif path=="/state":
            state["trades_list"] = [{"time":t["time"],"action":t["side"],"price":t["price"],"amount":t["amount"],"pnl":t.get("pnl"),"via":t.get("router","")} for t in state["trades"][-50:]]
            state["positions_count"] = len(state.get("positions", []))
            if not self._check_auth():
                self.respond(200,"application/json",json.dumps({"price":state.get("price",0),"running":state.get("running",False),"strategy":state.get("strategy",""),"pair":state.get("pair",""),"mode":state.get("mode",""),"paper_trading":state.get("paper_trading",True)}).encode())
                return
            self.respond(200,"application/json",json.dumps(state).encode())
        elif path=="/start":
            if not self._auth_or_401(): return
            start_bot(
                params.get("strategy",["dca"])[0],
                params.get("pair",[cfg["pair"]])[0],
                params.get("mode",["cex"])[0],
                params.get("exchange",[cfg["exchange"]])[0],
                params.get("chain",["ethereum"])[0],
            )
            self.respond(200,"application/json",b'{"ok":true}')
        elif path=="/stop":
            if not self._auth_or_401(): return
            stop_bot()
            self.respond(200,"application/json",b'{"ok":true}')
        elif path=="/backtest":
            if not self._auth_or_401(): return
            try: data = json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
            except: data = {}
            pair = data.get("pair", state.get("pair", "SOL/USDC"))
            strategy = data.get("strategy", "grid")
            # Load price data
            prices = []
            if state.get("price_history") and len(state["price_history"]) > 5:
                prices = state["price_history"]
            else:
                # Fallback: fetch from Kraken
                try:
                    r = requests.get("https://api.kraken.com/0/public/OHLC", params={
                        "pair": pair.replace("/",""), "interval": 5
                    }, timeout=10)
                    ohlc = r.json().get("result", {})
                    for k in ohlc:
                        if k != "last":
                            prices = [{"time": int(p[0]), "value": float(p[4])} for p in ohlc[k][-200:]]
                except: pass
            if not prices or len(prices) < 5:
                self.respond(200,"application/json",json.dumps({"error":"Not enough price data"}).encode()); return
            # Simple grid backtest
            trades = []; pnl_total = 0; wins = 0; peak_equity = 0; max_dd = 0; equity = 100
            levels=5; spread_val=cfg.get("base_spread",0.05)
            base_price = prices[0]["value"]
            grids = [round(base_price*(1-spread_val)+i*(base_price*spread_val*2/levels),4) for i in range(levels+1)]
            mid_idx = len(grids)//2; filled = {}
            for pt in prices[1:]:
                pr = pt["value"]
                if pr <= 0: continue
                # Re-center check
                if pr < grids[0]*0.98 or pr > grids[-1]*1.02:
                    base_price = pr
                    grids = [round(pr*(1-spread_val)+i*(pr*spread_val*2/levels),4) for i in range(levels+1)]
                    mid_idx = len(grids)//2
                for i,g in enumerate(grids[:-1]):
                    ng = grids[i+1]
                    if g <= pr < ng:
                        is_buy = i < mid_idx
                        if is_buy and i not in filled:
                            filled[i] = {"price":pr,"amount":1}
                        elif not is_buy:
                            for bi in sorted(filled.keys()):
                                if bi < i:
                                    bp = filled[bi]["price"]
                                    pnl = pr - bp
                                    pnl_total += pnl; equity += pnl
                                    if equity > peak_equity: peak_equity = equity
                                    dd = peak_equity - equity
                                    if dd > max_dd: max_dd = dd
                                    if pnl > 0: wins += 1
                                    trades.append({"action":"sell","price":pr,"buy_price":bp,"pnl":round(pnl,2),"time":pt["time"]})
                                    del filled[bi]; break
            result = {
                "total_trades": len(trades),
                "win_rate": round(wins/max(len(trades),1)*100,1),
                "total_pnl": round(pnl_total,2),
                "max_drawdown": round(max_dd,2),
                "trades": trades[-20:]
            }
            self.respond(200,"application/json",json.dumps(result).encode())
        elif path=="/webhook":
            if not self._auth_or_401(): return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
            except:
                self.respond(400,"application/json",json.dumps({"error":"Invalid JSON"}).encode()); return
            signal = data.get("signal","")
            wpair = data.get("pair",state.get("pair","SOL/USDC"))
            wprice = data.get("price", 0.0)
            if signal == "buy" and wprice > 0:
                # Force buy at signaled level
                gs = state["grid_pairs"].get(wpair, {})
                grids = gs.get("grids", [])
                filled = gs.get("filled", {})
                mid_idx = gs.get("mid_idx", len(grids)//2) if grids else 2
                if not grids:
                    levels=5; spread_val=cfg.get("base_spread",0.05)
                    grids = [round(wprice*(1-spread_val)+i*(wprice*spread_val*2/levels),4) for i in range(levels+1)]
                    mid_idx = len(grids)//2
                    state["grid_pairs"][wpair] = {"grids":grids,"mid_idx":mid_idx,"filled":{}}
                    if wpair not in state.get("active_pairs",[]): state["active_pairs"].append(wpair)
                bal = get_balance()
                sz = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])/5
                amt = round(sz/wprice,6)
                if place_order(wpair,"buy",amt):
                    for i,g in enumerate(grids[:-1]):
                        if g <= wprice < grids[i+1] and i < mid_idx and i not in filled:
                            filled[i] = {"price":wprice,"amount":amt}
                            state["grid_pairs"][wpair]["filled"] = filled
                            record_trade("WEBHOOK-BUY",wprice,amt)
                            log("[WEBHOOK] Forced buy "+wpair+" @ $"+str(round(wprice,2)))
                            break
                self.respond(200,"application/json",json.dumps({"ok":True,"pair":wpair}).encode())
            elif signal == "sell":
                gs = state["grid_pairs"].get(wpair, {})
                filled = gs.get("filled", {})
                sold = 0
                for bi in sorted(filled.keys()):
                    amt = filled[bi]["amount"]
                    bp = filled[bi]["price"]
                    sp = wprice if wprice > 0 else get_price(wpair)
                    if place_order(wpair,"sell",amt):
                        pnl = (sp - bp) * amt
                        state["pnl"] += pnl
                        record_trade("WEBHOOK-SELL",sp,amt,round(pnl,2))
                        log("[WEBHOOK] Forced sell "+wpair+" @ $"+str(round(sp,2)))
                        sold += 1
                state["grid_pairs"][wpair]["filled"] = {}
                self.respond(200,"application/json",json.dumps({"ok":True,"pair":wpair,"closed":sold}).encode())
            else:
                self.respond(400,"application/json",json.dumps({"error":"signal must be buy or sell"}).encode())
        elif path=="/debug_orca":
            if not self._auth_or_401(): return
            try:
                import base64 as b64
                pool = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"
                payload = {"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":[pool,{"encoding":"base64"}]}
                r = requests.post(SOL_RPC, json=payload, timeout=10)
                raw_b64 = r.json().get("result",{}).get("value",{}).get("data",[None])[0]
                if raw_b64:
                    raw = b64.b64decode(raw_b64)
                    sqrt_price = int.from_bytes(raw[65:81], "little")
                    price = (sqrt_price / (2**64))**2 * (10**(6-9))
                    result = {"length":len(raw),"sqrt_price":sqrt_price,"price":round(price,4),"offset_65_hex":raw[65:81].hex()}
                else:
                    result = {"error":"no data"}
                self.respond(200,"application/json",json.dumps(result).encode())
            except Exception as ex:
                self.respond(200,"application/json",json.dumps({"error":str(ex)}).encode())
        elif path=="/toggle_paper":
            if not self._auth_or_401(): return
            state["paper_trading"] = not state["paper_trading"]
            mode = "PAPER" if state["paper_trading"] else "LIVE"
            log("Switched to "+mode+" trading mode")
            self.respond(200,"application/json",json.dumps({"paper_trading":state["paper_trading"]}).encode())
            return
        elif path=="/pause":
            if not self._auth_or_401(): return
            state["paused"] = not state["paused"]
            log("Bot "+("paused" if state["paused"] else "resumed"))
            self.respond(200,"application/json",json.dumps({"paused":state["paused"]}).encode())
            return
        else:
            self.respond(404,"text/plain",b"Not found")

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len > 0:
            body = self.rfile.read(content_len)
            try: data = json.loads(body)
            except Exception as e:
                log("JSON parse error: "+str(e), "WARN")
                data = {}
        else: data = {}
        path = urlparse(self.path).path
        if path == "/config":
            if not self._auth_or_401(): return
            for key in ["max_leverage", "max_position", "cooldown", "slippage", "auto_compound", "partial_sell_pct"]:
                if key in data:
                    if key in ("auto_compound",):
                        cfg[key] = str(data[key]).lower() in ("true","1","yes")
                    elif key in ("partial_sell_pct",):
                        try:
                            cfg[key] = float(data[key])
                        except (ValueError, TypeError) as e:
                            log("Config "+key+" parse error: "+str(e), "WARN")
                    else:
                        cfg[key] = data[key]
            state["config"] = {k: cfg.get(k) for k in ["max_leverage", "max_position", "cooldown", "slippage", "auto_compound", "partial_sell_pct", "dynamic_spread", "base_spread"] if cfg.get(k) is not None}
            log("Config updated: "+json.dumps(data))
            self.respond(200,"application/json",json.dumps({"status":"ok","config":state["config"]}).encode())
        else:
            self.respond(404,"text/plain",b"Not found")
    
    def respond(self,code,ctype,body):
        self.send_response(code)
        self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self,format,*args): pass

if __name__=="__main__":
    port=int(os.environ.get("PORT",10000))
    log("Bot dashboard starting on port "+str(port))
    start_background_loops()
    server=HTTPServer(("0.0.0.0",port),Handler)
    log("Ready — open your URL to control the bot")
    server.serve_forever()
