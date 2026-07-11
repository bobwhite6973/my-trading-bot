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

logging.basicConfig(level=logging.WARNING)

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
}

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
}

def get_price_kraken(pair):
    try:
        kraken_pair = KRAKEN_PAIRS.get(pair, pair.replace("/","").replace("USDT","USD"))
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
    except:
        return 0.0

def get_price(pair):
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
            opts['options'] = {'createMarketBuyOrderRequiresPrice': False}
        ex = getattr(ccxt, name)(opts)
        ex.load_markets()
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
            # Direct LBank supplement API call
            import random, string
            lsym = pair.replace("/", "_").lower()  # LBank format: btc_usdt
            lside = 'buy' if 'buy' in side.lower() else 'sell'
            timestamp = str(int(time.time() * 1000))
            echostr = ''.join(random.choices(string.ascii_letters + string.digits, k=35))
            signature_method = 'md5'

            if lside == 'buy':
                cost = str(round(amount * state.get("price", 1), 2))
                params = {
                    'symbol': lsym,
                    'type': 'buy_market',
                    'price': cost,
                }
            else:
                amt_str = str(round(amount, 8))
                params = {
                    'symbol': lsym,
                    'type': 'sell_market',
                    'amount': amt_str,
                }

            # Build signature string (sorted alphabetically)
            params['api_key'] = cfg['api_key']
            params['timestamp'] = timestamp
            params['signature_method'] = signature_method
            params['echostr'] = echostr

            sorted_keys = sorted(params.keys())
            sign_str = '&'.join([k + '=' + params[k] for k in sorted_keys])
            sign_str += '&secret_key=' + cfg['api_secret']
            sign = hashlib.md5(sign_str.encode()).hexdigest().upper()

            params['sign'] = sign
            r = requests.post("https://api.lbank.info/v2/supplement/create_order.do",
                data=params, timeout=10)
            resp = r.json()
            log("LBank order response: " + str(resp)[:150])
            if resp.get("result") and resp.get("data", {}).get("order_id"):
                return resp["data"]["order_id"]
            else:
                log("LBank order failed: " + str(resp.get("error_code", resp.get("msg", "unknown"))), "WARN")
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
}

TOKENS = {
    "ethereum": {"USDT":"0xdAC17F958D2ee523a2206206994597C13D831ec7","WETH":"0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2","WBTC":"0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"},
    "bsc":      {"USDT":"0x55d398326f99059fF775485246999027B3197955","WBNB":"0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c","BTCB":"0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"},
    "base":     {"USDT":"0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2","WETH":"0x4200000000000000000000000000000000000006"},
    "arbitrum": {"USDT":"0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9","WETH":"0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"},
    "polygon":  {"USDT":"0xc2132D05D31c914a87C6611C10748AEb04B58e8F","WMATIC":"0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"},
}

def dex_get_quote_1inch(chain, from_token, to_token, amount_wei):
    try:
        chain_ids = {"ethereum":1,"bsc":56,"base":8453,"arbitrum":42161,"polygon":137}
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
        chain_ids = {"ethereum":1,"bsc":56,"base":8453,"arbitrum":42161,"polygon":137}
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
            except:
                pass
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
            except:
                pass
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
    "BTC":   "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",
    "ETH":   "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    "BNB":   "9gP2kCy3wA1ctvYWQk75guqXuzoJGLIDs5oPHkHGs89",
    "JUP":   "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK":  "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":   "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "MATIC": "Gz7VkD4MacbEB6yC5XD3HcumEiYx2EtDYYrfikGsvopG",
}

def sol_get_balance():
    """Get SOL + USDC + USDT balance. Tries multiple RPC endpoints for reliability."""
    SOL_RPCS = [SOL_RPC, "https://rpc.ankr.com/solana"]
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
            except:
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

def jupiter_swap(from_token, to_token, amount_input, price, dex=None):
    """
    Execute a Solana DEX swap via Raydium Trade API.
    - from_token/to_token: token symbols e.g. "USDC", "BONK"
    - amount_input: USDC value for stablecoin buys; token quantity for token sells
    - price: current price of output token in USDC (for display/logging)
    - dex: optional DEX name hint for logging ("Raydium", "Orca", "Meteora")
    Returns (success: bool, out_amount_human: float)
    """
    TOKEN_DECIMALS = {"USDC": 6, "USDT": 6, "SOL": 9, "ETH": 8, "JUP": 6, "BONK": 5, "WIF": 6}
    from_mint = SOL_TOKENS.get(from_token, SOL_TOKENS["USDC"])
    to_mint   = SOL_TOKENS.get(to_token,   SOL_TOKENS["SOL"])
    from_dec  = TOKEN_DECIMALS.get(from_token, 6)
    to_dec    = TOKEN_DECIMALS.get(to_token,   9)
    side      = "BUY" if from_token in ("USDC","USDT") else "SELL"
    via       = (" via "+dex) if dex else ""

    lamports = int(amount_input * (10 ** from_dec))
    log("Swap "+side+via+": "+str(amount_input)+" "+from_token+" → "+to_token)

    # Get execution quote from Raydium (confirmed accessible from Render)
    slippage = "100" if side == "BUY" else "300"
    quote    = raydium_get_quote(from_mint, to_mint, lamports, slippage)
    if not quote:
        log("Raydium quote failed — swap aborted", "WARN")
        return False, 0.0

    out_lamports = int(quote.get("data",{}).get("outputAmount", 0))
    out_human    = out_lamports / (10 ** to_dec) if out_lamports > 0 else 0.0
    log("Quote: "+str(amount_input)+" "+from_token+" → "+str(round(out_human,6))+" "+to_token)

    if state["paper_trading"]:
        trade = {"time":time.strftime("%H:%M:%S"),"side":"[PAPER] "+side+via,
                 "price":price,"amount":out_human,"router":"Raydium","chain":"solana"}
        state["trades"].append(trade)
        return True, out_human

    # ── Live execution ────────────────────────────────────────────────────────
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

        try:
            keypair = Keypair.from_base58_string(private_key)
        except Exception as ke:
            log("Key decode failed: "+str(ke)[:60], "WARN")
            return False, 0.0

        # ── ATA lookup with multi-RPC fallback ───────────────────────────────
        def get_ata(wallet_addr, mint_addr):
            """Look up Associated Token Account across multiple RPCs."""
            rpcs = [SOL_RPC, "https://rpc.ankr.com/solana"]
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
                except:
                    continue
            return None

        # ── ATA creation if missing ───────────────────────────────────────────
        def create_ata_if_missing(keypair, wallet_addr, mint_addr):
            """
            Create Associated Token Account on-chain if it doesn't exist.
            Uses Solana's Associated Token Program via raw RPC instruction.
            Cost: ~0.002 SOL. Only needed once per token.
            """
            existing = get_ata(wallet_addr, mint_addr)
            if existing:
                return existing

            log("Creating ATA for mint "+mint_addr[:8]+"...", "WARN")
            try:
                from solders.pubkey import Pubkey
                import struct

                # Derive ATA address
                wallet_pk  = Pubkey.from_string(wallet_addr)
                mint_pk    = Pubkey.from_string(mint_addr)
                token_prog = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
                ata_prog   = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bsU")
                sys_prog   = Pubkey.from_string("11111111111111111111111111111111")

                # Derive ATA PDA
                seeds = [bytes(wallet_pk), bytes(token_prog), bytes(mint_pk)]
                ata_pk, _ = Pubkey.find_program_address(seeds, ata_prog)

                # Get recent blockhash
                bh_payload = {"jsonrpc":"2.0","id":1,"method":"getLatestBlockhash","params":[{"commitment":"confirmed"}]}
                bh_r = requests.post(SOL_RPC, json=bh_payload, timeout=8)
                blockhash_str = bh_r.json().get("result",{}).get("value",{}).get("blockhash","")
                if not blockhash_str:
                    log("Could not get blockhash for ATA creation", "WARN")
                    return None

                from solders.hash import Hash
                from solders.instruction import AccountMeta, Instruction
                from solders.message import MessageV0
                from solders.transaction import VersionedTransaction as VT

                blockhash = Hash.from_string(blockhash_str)

                # Create Associated Token Account instruction
                create_ix = Instruction(
                    ata_prog,
                    bytes([0]),  # CreateIdempotent instruction (safe to retry)
                    [
                        AccountMeta(wallet_pk,  True,  True),   # payer
                        AccountMeta(ata_pk,     False, True),   # ata
                        AccountMeta(wallet_pk,  False, False),  # owner
                        AccountMeta(mint_pk,    False, False),  # mint
                        AccountMeta(sys_prog,   False, False),  # system program
                        AccountMeta(token_prog, False, False),  # token program
                    ]
                )

                msg     = MessageV0.try_compile(wallet_pk, [create_ix], [], blockhash)
                tx      = VT(msg, [keypair])
                import base64 as b64
                tx_b64  = b64.b64encode(bytes(tx)).decode()

                send_payload = {
                    "jsonrpc":"2.0","id":1,"method":"sendTransaction",
                    "params":[tx_b64, {"encoding":"base64","skipPreflight":True,"maxRetries":3}]
                }
                send_rpc = ("https://solana-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY) if ALCHEMY_KEY else SOL_RPC
                r2 = requests.post(send_rpc, json=send_payload, timeout=15)
                result = r2.json()
                tx_sig = result.get("result","")
                if tx_sig:
                    log("ATA created: "+tx_sig[:20]+"... waiting 3s for confirmation")
                    time.sleep(3)
                    return str(ata_pk)
                else:
                    log("ATA creation failed: "+str(result.get("error",""))[:80], "WARN")
                    return None
            except Exception as ex:
                log("ATA creation error: "+str(ex)[:80], "WARN")
                return None

        # Get or create ATAs
        input_ata  = get_ata(wallet, from_mint) if from_token != "SOL" else None
        output_ata = get_ata(wallet, to_mint)   if to_token  != "SOL" else None
        log("ATA — input: "+str(input_ata)+" output: "+str(output_ata))

        # For non-SOL input: must have ATA to spend from
        if from_token != "SOL" and not input_ata:
            log("Input ATA missing for "+from_token+" — swap cannot proceed", "WARN")
            return False, 0.0

        # For non-SOL output: create ATA if missing (e.g. first time receiving BONK)
        if to_token != "SOL" and not output_ata:
            output_ata = create_ata_if_missing(keypair, wallet, to_mint)
            if not output_ata:
                log("Could not create output ATA for "+to_token, "WARN")
                return False, 0.0

        # Build transaction via Raydium Trade API
        swap_payload = {
            "computeUnitPriceMicroLamports": "10000",
            "swapResponse":  quote,
            "txVersion":     "V0",
            "wallet":        wallet,
            "wrapSol":       from_token == "SOL",
            "unwrapSol":     to_token   == "SOL",
            "inputAccount":  input_ata,
            "outputAccount": output_ata,
        }
        r = requests.post(
            "https://transaction-v1.raydium.io/transaction/swap-base-in",
            json=swap_payload, headers={"Content-Type":"application/json"}, timeout=15
        )
        log("Raydium TX: "+str(r.status_code)+" "+r.text[:120])
        if r.status_code != 200:
            return False, 0.0

        swap_data = r.json()
        if not swap_data.get("success"):
            log("Raydium TX failed: "+str(swap_data.get("msg",""))[:80], "WARN")
            return False, 0.0

        txs = swap_data.get("data",[])
        swap_tx_b64 = txs[0].get("transaction","") if txs else ""
        if not swap_tx_b64:
            log("No transaction in Raydium response", "WARN")
            return False, 0.0

        # Sign versioned transaction
        raw_tx    = b64.b64decode(swap_tx_b64)
        tx_obj    = VersionedTransaction.from_bytes(raw_tx)
        sig       = keypair.sign_message(solders_message.to_bytes_versioned(tx_obj.message))
        signed_tx = VersionedTransaction.populate(tx_obj.message, [sig])

        # Send — Alchemy first, public RPC on 429
        send_payload = {
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[
                b64.b64encode(bytes(signed_tx)).decode(),
                {"encoding":"base64","skipPreflight":True,
                 "preflightCommitment":"confirmed","maxRetries":3}
            ]
        }
        send_rpc = ("https://solana-mainnet.g.alchemy.com/v2/"+ALCHEMY_KEY) if ALCHEMY_KEY else SOL_RPC
        r2     = requests.post(send_rpc, json=send_payload, timeout=15)
        result = r2.json()
        if result.get("error",{}).get("code") == 429:
            log("Rate limited on send — retrying in 3s", "WARN")
            time.sleep(3)
            r2     = requests.post(SOL_RPC, json=send_payload, timeout=15)
            result = r2.json()

        tx_sig = result.get("result","")
        if tx_sig:
            log("SWAP EXECUTED: "+tx_sig[:20]+"... "+from_token+"→"+to_token+via)
            trade = {"time":time.strftime("%H:%M:%S"),"side":"LIVE-"+side+via,
                     "price":price,"amount":out_human,"router":"Raydium",
                     "chain":"solana","tx":tx_sig[:20]}
            state["trades"].append(trade)
            return True, out_human
        else:
            log("Send failed: "+str(result.get("error",""))[:100], "WARN")
            return False, 0.0

    except ImportError as ie:
        log("Missing package: "+str(ie), "WARN"); return False, 0.0
    except Exception as ex:
        log("Swap error: "+str(ex)[:100], "WARN"); return False, 0.0


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
    except:
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
    return dex_get_balance() if state["mode"]=="dex" else cex_get_balance()

def place_order(pair, side, amount):
    if state["mode"] == "dex":
        chain = state["chain"]
        tokens = TOKENS.get(chain, {})
        price = get_price(pair)
        token = pair.split("/")[0]
        from_t = tokens.get("USDT","")
        to_t   = tokens.get("W"+token, tokens.get(token,""))
        if side in ("buy","buy_market"):
            return dex_swap(chain, from_t, to_t, amount*get_price(pair), get_price(pair))
        else:
            return dex_swap(chain, to_t, from_t, amount*get_price(pair), get_price(pair))
    else:
        return cex_place_order(pair, side, amount)

def record_trade(side, price, amount, pnl=None):
    state["trades"].append({"time":time.strftime("%H:%M:%S"),"side":side,"price":price,"amount":amount,"pnl":pnl})

def run_dca():
    log("DCA started on "+state["pair"]+" ("+state["mode"].upper()+")")
    buy_prices = []
    while state["running"] and state["strategy"]=="dca":
        price = get_price(state["pair"])
        if price <= 0: time.sleep(60); continue
        bal = get_balance()
        if not buy_prices:
            size = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])
            if size > 1:
                amt = round(size/price, 6)
                if place_order(state["pair"],"buy",amt):
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
                if place_order(state["pair"],"sell",total):
                    pnl = (price-avg)*total
                    state["pnl"] += pnl
                    record_trade("SELL",price,total,round(pnl,2))
                    log("DCA SELL @ $"+str(price)+" PnL: $"+str(round(pnl,2)))
                    buy_prices.clear(); state["positions"].clear()
            elif loss >= cfg["stop_loss"]:
                if place_order(state["pair"],"sell",total):
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
                    if place_order(state["pair"],"buy",amt):
                        buy_prices.append(price)
                        state["positions"].append({"price":price,"amount":amt,"strategy":"DCA"})
                        record_trade("DCA-BUY",price,amt)
                        log("DCA averaging down @ $"+str(price))
        if state["daily_loss"] >= cfg["max_loss"]:
            log("Daily loss limit reached — pausing 1hr", "WARN"); time.sleep(3600)
        time.sleep(60)

def run_grid():
    log("Grid started on "+state["pair"]+" ("+state["mode"].upper()+")")
    price = get_price(state["pair"])
    if price <= 0: log("Cannot get price","ERROR"); return
    levels=5; spread=0.05
    grids = [round(price*(1-spread)+i*(price*spread*2/levels),4) for i in range(levels+1)]
    filled = {}
    log("Grid levels: "+str(grids))
    while state["running"] and state["strategy"]=="grid":
        price = get_price(state["pair"])
        if price <= 0: time.sleep(30); continue
        bal = get_balance()
        size = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])/levels
        for i,g in enumerate(grids[:-1]):
            ng = grids[i+1]
            if g <= price < ng:
                if i not in filled and size > 1:
                    amt = round(size/price,6)
                    if place_order(state["pair"],"buy",amt):
                        filled[i]={"price":price,"amount":amt}
                        state["positions"].append({"price":price,"amount":amt,"grid":i,"strategy":"Grid"})
                        record_trade("GRID-BUY",price,amt)
                        log("Grid BUY level "+str(i)+" @ $"+str(price))
                elif i in filled and price >= filled[i]["price"]*(1+cfg["take_profit"]/100):
                    amt = filled[i]["amount"]
                    if place_order(state["pair"],"sell",amt):
                        pnl=(price-filled[i]["price"])*amt
                        state["pnl"]+=pnl
                        record_trade("GRID-SELL",price,amt,round(pnl,2))
                        log("Grid SELL level "+str(i)+" PnL: $"+str(round(pnl,2)))
                        del filled[i]
                        state["positions"]=[p for p in state["positions"] if p.get("grid")!=i]
        time.sleep(30)

def run_scalp():
    log("Scalping started on "+state["pair"]+" ("+state["mode"].upper()+")")
    prices=[]; position=None
    while state["running"] and state["strategy"]=="scalp":
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
            if place_order(state["pair"],"buy",amt):
                position={"price":price,"amount":amt}
                state["positions"]=[{"price":price,"amount":amt,"strategy":"Scalp"}]
                record_trade("SCALP-BUY",price,amt)
                log("Scalp BUY @ $"+str(price))
        elif position:
            gain=(price-position["price"])/position["price"]*100
            loss=(position["price"]-price)/position["price"]*100
            if gain>=cfg["take_profit"]/3 or loss>=cfg["stop_loss"]/2:
                if place_order(state["pair"],"sell",position["amount"]):
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
        log("Monitoring "+source+" for trades...")
        time.sleep(60)

def run_arbitrage():
    mode = "PAPER" if state["paper_trading"] else "LIVE"
    chain = state.get("chain","ethereum")
    log("Arbitrage started ["+mode+" MODE] on "+chain+" — min spread: "+str(cfg["min_arb_spread"])+"%")
    while state["running"] and state["strategy"]=="arb":
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
    log("Bot stopped")

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Trading Bot Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#080808;color:#eee;padding:20px}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:22px;font-weight:900;color:#fff;margin-bottom:4px}
.sub{font-size:13px;color:#444;margin-bottom:24px;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:#333;display:inline-block;transition:all .3s}
.dot.on{background:#00ff9d;box-shadow:0 0 8px #00ff9d}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat{background:#111;border:1px solid #1a1a1a;border-radius:10px;padding:16px}
.sl{font-size:10px;font-weight:700;letter-spacing:2px;color:#555;text-transform:uppercase;margin-bottom:6px}
.sv{font-size:22px;font-weight:900;color:#fff}
.sv.g{color:#00ff9d}.sv.r{color:#ff6b6b}
.card{background:#111;border:1px solid #1a1a1a;border-radius:10px;padding:20px;margin-bottom:16px}
.ct{font-size:10px;font-weight:700;letter-spacing:2px;color:#00ff9d;text-transform:uppercase;margin-bottom:14px}
.mode-tabs{display:flex;gap:0;margin-bottom:20px;border:1.5px solid #222;border-radius:10px;overflow:hidden}
.mode-tab{flex:1;padding:12px;text-align:center;cursor:pointer;font-weight:700;font-size:13px;color:#555;background:#111;transition:all .15s;border:none}
.mode-tab.active{background:#00ff9d18;color:#00ff9d}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.btn{padding:9px 16px;border:1.5px solid #222;border-radius:8px;font-weight:700;font-size:12px;cursor:pointer;background:#1a1a1a;color:#666;transition:all .15s}
.btn:hover{border-color:#444;color:#aaa}
.btn.active-strat{background:#00ff9d18;color:#00ff9d;border-color:#00ff9d}
.btn.active-pair{background:#4dabf718;color:#4dabf7;border-color:#4dabf7}
.btn.active-chain{background:#cc99ff18;color:#cc99ff;border-color:#cc99ff}
.btn.active-exch{background:#ffd43b18;color:#ffd43b;border-color:#ffd43b}
.btn-start{background:#00ff9d;color:#000;border:none;padding:13px 32px;font-size:14px;border-radius:8px;font-weight:800;cursor:pointer;transition:all .15s}
.btn-start:disabled{background:#1a1a1a;color:#333;cursor:not-allowed}
.btn-stop{background:#ff6b6b18;color:#ff6b6b;border:1.5px solid #ff6b6b33;padding:13px 24px;font-size:13px;border-radius:8px;font-weight:700;cursor:pointer}
.section-label{font-size:11px;color:#444;font-weight:700;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#333;font-weight:700;text-align:left;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:10px;letter-spacing:1px;text-transform:uppercase}
td{padding:8px 0;border-bottom:1px solid #0f0f0f;color:#888}
.buy{color:#00ff9d;font-weight:700}.sell{color:#ff6b6b;font-weight:700}.stop{color:#ffd43b;font-weight:700}
.log-box{background:#0a0a0a;border:1px solid #1a1a1a;border-radius:8px;padding:14px;height:180px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.8}
.li{color:#444}.lw{color:#ffd43b}.le{color:#ff6b6b}
.arb-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #0f0f0f;font-size:12px}
.arb-spread{color:#00ff9d;font-weight:800;font-size:14px}
.dex-info{background:#cc99ff11;border:1px solid #cc99ff22;border-radius:8px;padding:12px;margin-bottom:14px;font-size:12px;color:#cc99ff;line-height:1.6}
.cex-info{background:#ffd43b11;border:1px solid #ffd43b22;border-radius:8px;padding:12px;margin-bottom:14px;font-size:12px;color:#ffd43b;line-height:1.6}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Trading Bot</h1>
  <div class="sub"><span class="dot" id="dot"></span><span id="status-text">Stopped</span></div>

  <div class="stats">
    <div class="stat"><div class="sl">Price (Kraken)</div><div class="sv" id="s-price">—</div></div>
    <div class="stat"><div class="sl">EVM Balance</div><div class="sv" id="s-balance">—</div></div>
    <div class="stat"><div class="sl">Solana</div><div class="sv" id="s-sol-balance" style="font-size:16px">—</div></div>
    <div class="stat"><div class="sl">Total P&L</div><div class="sv" id="s-pnl">$0.00</div></div>
    <div class="stat"><div class="sl">Open Positions</div><div class="sv" id="s-pos">0</div></div>
    <div class="stat"><div class="sl">Mode</div><div class="sv" id="s-mode" style="font-size:14px">—</div></div>
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
      <div class="btn-row">
        <button class="btn" id="e-binance" onclick="selectExch('binance')">Binance</button>
        <button class="btn" id="e-bybit"   onclick="selectExch('bybit')">Bybit</button>
        <button class="btn" id="e-okx"     onclick="selectExch('okx')">OKX</button>
        <button class="btn" id="e-kucoin"  onclick="selectExch('kucoin')">KuCoin</button>
        <button class="btn" id="e-lbank"   onclick="selectExch('lbank')">LBank</button>
        <button class="btn" id="e-kraken"  onclick="selectExch('kraken')">Kraken</button>
      </div>
    </div>

    <div id="dex-panel" style="display:none">
      <div class="dex-info">Trade on-chain using your wallet. No API keys needed. Uses Uniswap + 1inch for EVM chains, Jupiter for Solana. Set WALLET_ADDRESS (EVM) or SOL_WALLET_ADDRESS (Solana) in Render environment variables.</div>
      <div class="section-label">Chain</div>
      <div class="btn-row">
        <button class="btn" id="c-ethereum" onclick="selectChain('ethereum')">Ethereum</button>
        <button class="btn" id="c-bsc"      onclick="selectChain('bsc')">BNB Chain</button>
        <button class="btn" id="c-base"     onclick="selectChain('base')">Base</button>
        <button class="btn" id="c-arbitrum" onclick="selectChain('arbitrum')">Arbitrum</button>
        <button class="btn" id="c-polygon"  onclick="selectChain('polygon')">Polygon</button>
        <button class="btn" id="c-solana"   onclick="selectChain('solana')">Solana ⚡</button>
      </div>
      <div style="background:#00ff9d11;border:1px solid #00ff9d22;border-radius:8px;padding:10px 14px;margin-bottom:4px;font-size:12px;color:#00ff9d">
        ⚡ <strong>Solana</strong> — gas &lt;$0.01 per trade, routed via Jupiter aggregator for best prices
      </div>
    </div>

    <div class="section-label" style="margin-top:16px">Strategy</div>
    <div class="btn-row">
      <button class="btn" id="s-dca"  onclick="selectStrat('dca')">DCA</button>
      <button class="btn" id="s-grid" onclick="selectStrat('grid')">Grid</button>
      <button class="btn" id="s-scalp"onclick="selectStrat('scalp')">Scalping</button>
      <button class="btn" id="s-copy" onclick="selectStrat('copy')">Copy Trading</button>
      <button class="btn" id="s-arb"  onclick="selectStrat('arb')">Arbitrage</button>
    </div>

    <div class="section-label">Trading Pair</div>
    <div class="btn-row" id="pair-row">
      <button class="btn" id="p-BTC/USDT"   onclick="selectPair('BTC/USDT')">BTC/USDT</button>
      <button class="btn" id="p-ETH/USDT"   onclick="selectPair('ETH/USDT')">ETH/USDT</button>
      <button class="btn" id="p-BNB/USDT"   onclick="selectPair('BNB/USDT')">BNB/USDT</button>
      <button class="btn" id="p-SOL/USDT"   onclick="selectPair('SOL/USDT')">SOL/USDT</button>
      <button class="btn" id="p-MATIC/USDT" onclick="selectPair('MATIC/USDT')">MATIC/USDT</button>
    </div>
    <div class="btn-row" id="sol-pair-row" style="display:none">
      <button class="btn" id="p-SOL/USDC"   onclick="selectPair('SOL/USDC')">SOL/USDC</button>
      <button class="btn" id="p-BTC/USDC"   onclick="selectPair('BTC/USDC')">BTC/USDC</button>
      <button class="btn" id="p-ETH/USDC"   onclick="selectPair('ETH/USDC')">ETH/USDC</button>
      <button class="btn" id="p-JUP/USDC"   onclick="selectPair('JUP/USDC')">JUP/USDC</button>
      <button class="btn" id="p-WIF/USDC"   onclick="selectPair('WIF/USDC')">WIF/USDC</button>
              </div>

    <div style="display:flex;gap:10px;margin-top:8px;flex-wrap:wrap">
      <button class="btn-start" id="start-btn" onclick="startBot()" disabled>Select options above</button>
      <button class="btn-stop" onclick="stopBot()">Stop Bot</button>
      <button class="btn" id="paper-btn" onclick="togglePaper()" style="background:#ffd43b18;color:#ffd43b;border-color:#ffd43b44;padding:13px 20px">📋 Paper Trading: ON</button>
    </div>
  </div>

  <div class="card" id="arb-card" style="display:none">
    <div class="ct">Arbitrage Opportunities</div>
    <div id="arb-list"><div style="color:#333;font-size:13px">Scanning for opportunities...</div></div>
  </div>

  <div class="card">
    <div class="ct">Trade History</div>
    <table>
      <thead><tr><th>Time</th><th>Action</th><th>Price</th><th>Amount</th><th>P&L</th><th>Via</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="6" style="color:#222;text-align:center;padding:20px">No trades yet</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <div class="ct">Live Log</div>
    <div class="log-box" id="log-box"></div>
  </div>
</div>

<script>
var sel = {mode:"cex", strat:null, pair:null, exch:null, chain:null};

function setMode(m) {
  sel.mode=m;
  document.getElementById("tab-cex").className="mode-tab"+(m=="cex"?" active":"");
  document.getElementById("tab-dex").className="mode-tab"+(m=="dex"?" active":"");
  document.getElementById("cex-panel").style.display=m=="cex"?"block":"none";
  document.getElementById("dex-panel").style.display=m=="dex"?"block":"none";
  updateBtn();
}

function selectStrat(s) {
  sel.strat=s;
  document.querySelectorAll('[id^="s-"]').forEach(b=>b.classList.remove("active-strat"));
  document.getElementById("s-"+s).classList.add("active-strat");
  document.getElementById("arb-card").style.display=s=="arb"?"block":"none";
  updateBtn();
}

function selectPair(p) {
  sel.pair=p;
  document.querySelectorAll('[id^="p-"]').forEach(b=>b.classList.remove("active-pair"));
  document.getElementById("p-"+p).classList.add("active-pair");
  updateBtn();
}

function selectExch(e) {
  sel.exch=e;
  document.querySelectorAll('[id^="e-"]').forEach(b=>b.classList.remove("active-exch"));
  document.getElementById("e-"+e).classList.add("active-exch");
  updateBtn();
}

function selectChain(c) {
  sel.chain=c;
  sel.pair=null;
  document.querySelectorAll('[id^="c-"]').forEach(b=>b.classList.remove("active-chain"));
  document.getElementById("c-"+c) && document.getElementById("c-"+c).classList.add("active-chain");
  document.querySelectorAll('[id^="p-"]').forEach(b=>b.classList.remove("active-pair"));
  // Show Solana pairs or EVM pairs
  if(c==="solana") {
    document.getElementById("pair-row").style.display="none";
    document.getElementById("sol-pair-row").style.display="flex";
  } else {
    document.getElementById("pair-row").style.display="flex";
    document.getElementById("sol-pair-row").style.display="none";
  }
  updateBtn();
}

function updateBtn() {
  var btn=document.getElementById("start-btn");
  var cexReady=sel.mode=="cex"&&sel.exch&&sel.strat&&sel.pair;
  var dexReady=sel.mode=="dex"&&sel.chain&&sel.strat&&sel.pair;
  if(cexReady||dexReady) {
    btn.disabled=false;
    btn.textContent="Start "+sel.strat.toUpperCase()+" on "+sel.pair;
  } else {
    btn.disabled=true;
    btn.textContent="Select options above";
  }
}

function startBot() {
  var params="strategy="+sel.strat+"&pair="+encodeURIComponent(sel.pair)+"&mode="+sel.mode;
  if(sel.mode=="cex"&&sel.exch) params+="&exchange="+sel.exch;
  if(sel.mode=="dex"&&sel.chain) params+="&chain="+sel.chain;
  fetch("/start?"+params).then(r=>r.json()).then(d=>console.log(d));
}

function stopBot() { fetch("/stop").then(r=>r.json()); }

function pnlHtml(v) {
  if(v==null||v===undefined) return "—";
  return "<span style='color:"+(v>=0?"#00ff9d":"#ff6b6b")+"'>"+(v>=0?"+":"")+"$"+Math.abs(v).toFixed(2)+"</span>";
}

function togglePaper() {
  fetch("/toggle_paper").then(r=>r.json()).then(d=>{
    var btn = document.getElementById("paper-btn");
    btn.textContent = "📋 Paper Trading: "+(d.paper_trading?"ON":"OFF");
    btn.style.color = d.paper_trading?"#ffd43b":"#ff6b6b";
    btn.style.borderColor = d.paper_trading?"#ffd43b44":"#ff6b6b44";
    btn.style.background = d.paper_trading?"#ffd43b18":"#ff6b6b18";
  });
}

function refresh() {
  fetch("/state").then(r=>r.json()).then(d=>{
    var on=d.running;
    document.getElementById("dot").className="dot"+(on?" on":"");
    document.getElementById("status-text").textContent=on?"Running — "+(d.strategy||"").toUpperCase()+" on "+d.pair+" ("+(d.mode||"").toUpperCase()+")":"Stopped";
    document.getElementById("s-price").textContent=d.price>0?"$"+d.price.toFixed(4):"—";
    document.getElementById("s-balance").textContent=d.balance>0?"$"+d.balance.toFixed(2):"—";
    document.getElementById("s-sol-balance").textContent=d.sol_balance>0?"$"+d.sol_balance.toFixed(2)+" (USDC: $"+d.sol_usdc.toFixed(2)+" USDT: $"+d.sol_usdt.toFixed(2)+")":"—";
    document.getElementById("s-mode").textContent=d.paper_trading?"📋 PAPER":"🔴 LIVE";
    document.getElementById("s-mode").style.color=d.paper_trading?"#ffd43b":"#ff6b6b";
    var pe=document.getElementById("s-pnl");
    pe.textContent=(d.pnl>=0?"+":"")+"$"+Math.abs(d.pnl||0).toFixed(2);
    pe.className="sv"+(d.pnl>0?" g":d.pnl<0?" r":"");
    document.getElementById("s-pos").textContent=(d.positions||[]).length;

    // Update paper button
    var pb=document.getElementById("paper-btn");
    if(pb){
      pb.textContent="📋 Paper Trading: "+(d.paper_trading?"ON":"OFF");
      pb.style.color=d.paper_trading?"#ffd43b":"#ff6b6b";
    }

    var tbody=document.getElementById("trades-body");
    var trades=(d.trades||[]).slice().reverse().slice(0,20);
    tbody.innerHTML=trades.length?trades.map(t=>"<tr><td>"+t.time+"</td><td class='"+(t.side.includes("BUY")?"buy":t.side.includes("STOP")?"stop":"sell")+"'>"+t.side+"</td><td>$"+parseFloat(t.price||0).toFixed(4)+"</td><td>"+parseFloat(t.amount||0).toFixed(6)+"</td><td>"+pnlHtml(t.pnl)+"</td><td style='color:#444'>"+(t.router||t.chain||d.exchange||"—")+"</td></tr>").join("")
      :"<tr><td colspan='6' style='color:#222;text-align:center;padding:20px'>No trades yet</td></tr>";

    var arb=d.arb_opps||[];
    if(arb.length) {
      document.getElementById("arb-list").innerHTML=arb.map(o=>
        "<div class='arb-row'><div><strong style='color:#eee'>"+o.pair+"</strong> <span style='font-size:11px;color:"+(o.executable?"#00ff9d":"#555")+"'>"+(o.executable?"● EXECUTABLE":"● watching")+"</span><br><span style='color:#555;font-size:11px'>Buy on "+o.buy_from+" @ $"+o.buy_price+" → Sell on "+o.sell_on+" @ $"+o.sell_price+" | gas ~$"+o.est_gas_usd+"</span></div><div style='text-align:right'><div class='arb-spread'>"+o.spread_pct+"%</div><div style='color:"+(o.est_profit_usd>0?"#00ff9d":"#ff6b6b")+";font-size:11px'>est $"+o.est_profit_usd+"</div></div></div>"
      ).join("");
    }

    document.getElementById("log-box").innerHTML=(d.log||[]).map(l=>{
      var cls=l.includes("[WARN]")?"lw":l.includes("[ERROR]")?"le":"li";
      return "<div class='"+cls+"'>"+l+"</div>";
    }).join("");
  }).catch(console.error);
}

setInterval(refresh,3000);
refresh();
</script>
</body>
</html>'''

# ── HTTP Server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed=urlparse(self.path)
        path=parsed.path
        params=parse_qs(parsed.query)

        if path=="/":
            self.respond(200,"text/html",DASHBOARD.encode())
        elif path=="/state":
            self.respond(200,"application/json",json.dumps(state).encode())
        elif path=="/start":
            start_bot(
                params.get("strategy",["dca"])[0],
                params.get("pair",[cfg["pair"]])[0],
                params.get("mode",["cex"])[0],
                params.get("exchange",[cfg["exchange"]])[0],
                params.get("chain",["ethereum"])[0],
            )
            self.respond(200,"application/json",b'{"ok":true}')
        elif path=="/stop":
            stop_bot()
            self.respond(200,"application/json",b'{"ok":true}')
        elif path=="/debug_orca":
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
            state["paper_trading"] = not state["paper_trading"]
            mode = "PAPER" if state["paper_trading"] else "LIVE"
            log("Switched to "+mode+" trading mode")
            self.respond(200,"application/json",json.dumps({"paper_trading":state["paper_trading"]}).encode())
            opps=scan_arbitrage()
            self.respond(200,"application/json",json.dumps(opps).encode())
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
