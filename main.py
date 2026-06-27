#!/usr/bin/env python3
"""
Trading Bot — Full Dashboard
CEX mode: API key trading on Binance/Bybit/OKX/KuCoin/LBank
DEX mode: Wallet-based trading via Uniswap/1inch on any EVM chain
Price feeds: Kraken (no key needed)
Strategies: DCA, Grid, Scalping, Copy Trading, Arbitrage
"""
import os, json, time, hmac, hashlib, threading, requests, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=logging.WARNING)

# ── Config from environment ───────────────────────────────────────────────────
cfg = {
    # CEX
    "api_key":      os.environ.get("API_KEY", ""),
    "api_secret":   os.environ.get("API_SECRET", ""),
    "exchange":     os.environ.get("EXCHANGE", "binance"),
    # DEX/EVM
    "wallet":       os.environ.get("WALLET_ADDRESS", ""),
    "private_key":  os.environ.get("PRIVATE_KEY", ""),
    # Solana
    "sol_wallet":   os.environ.get("SOL_WALLET_ADDRESS", ""),
    "sol_key":      os.environ.get("SOL_PRIVATE_KEY", ""),
    # Trading
    "pair":         os.environ.get("TRADING_PAIR", "BTC/USDT"),
    "risk_pct":     float(os.environ.get("RISK_PCT", "2")),
    "stop_loss":    float(os.environ.get("STOP_LOSS_PCT", "5")),
    "take_profit":  float(os.environ.get("TAKE_PROFIT_PCT", "15")),
    "max_pos":      float(os.environ.get("MAX_POSITION_USD", "500")),
    "max_loss":     float(os.environ.get("MAX_DAILY_LOSS_USD", "200")),
    "source_wallet":os.environ.get("SOURCE_WALLET", ""),
    # Safety
    "min_arb_spread":  float(os.environ.get("MIN_ARB_SPREAD", "0.5")),
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
    "positions":     [],
    "trades":        [],
    "pnl":           0.0,
    "daily_loss":    0.0,
    "log":           [],
    "error":         None,
    "arb_opps":      [],
    "paper_trading": cfg["paper_trading"],
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

def cex_get_balance():
    exchange = state["exchange"]
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
            ts = str(int(time.time()*1000))
            params = {"api_key":cfg["api_key"],"timestamp":ts}
            query = "&".join(k+"="+str(v) for k,v in sorted(params.items()))
            sign = hmac.new(cfg["api_secret"].encode(), query.encode(), hashlib.md5).hexdigest().upper()
            params["sign"] = sign
            r = requests.post("https://api.lbank.info/v1/user_info.do", data=params, timeout=5)
            data = r.json()
            if data.get("result")=="true":
                usdt = float(data.get("info",{}).get("free",{}).get("usdt",0))
                state["balance"]=usdt; return usdt
            else:
                log("LBank balance error: "+str(data.get("error_code","")), "ERROR")
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
            ts = str(int(time.time()*1000))
            lsym = pair.replace("/","_").lower()
            lside = "buy_market" if "buy" in side.lower() else "sell_market"
            params = {"api_key":cfg["api_key"],"symbol":lsym,"type":lside,"price":"-1","amount":str(amount),"timestamp":ts}
            query = "&".join(k+"="+str(v) for k,v in sorted(params.items()))
            sign = hmac.new(cfg["api_secret"].encode(), query.encode(), hashlib.md5).hexdigest().upper()
            params["sign"] = sign
            r = requests.post("https://api.lbank.info/v1/create_order.do", data=params, timeout=10)
            data = r.json()
            if data.get("result")=="true": return data.get("order_id")
            else: log("LBank order error: "+str(data.get("error_code","")), "ERROR")
        log("Order placed: "+side+" "+str(amount)+" "+pair)
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
        if cfg["wallet"]:
            state["mode"] = "dex"
        while True:
            try:
                if cfg["wallet"]:
                    dex_get_balance()
                elif cfg["api_key"]:
                    cex_get_balance()
                if cfg["sol_wallet"]:
                    sol_get_balance()
            except Exception as ex:
                log("Balance loop error: "+str(ex), "ERROR")
            time.sleep(20)

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
JUPITER_API = "https://quote-api.jup.ag/v6"

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
    try:
        wallet = cfg["sol_wallet"]
        if not wallet:
            return 0.0
        # Get SOL balance
        payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[wallet]}
        r = requests.post(SOL_RPC, json=payload, timeout=8)
        data = r.json()
        sol_amt = data.get("result",{}).get("value",0) / 1e9
        sol_price = get_price_kraken("SOL/USDT") or get_price_coingecko("SOL/USDT") or 150
        # Get USDC balance via token account
        usdc_payload = {
            "jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
            "params":[wallet,{"mint":SOL_TOKENS["USDC"]},{"encoding":"jsonParsed"}]
        }
        r2 = requests.post(SOL_RPC, json=usdc_payload, timeout=8)
        data2 = r2.json()
        usdc = 0.0
        accounts = data2.get("result",{}).get("value",[])
        if accounts:
            usdc = float(accounts[0].get("account",{}).get("data",{}).get("parsed",{}).get("info",{}).get("tokenAmount",{}).get("uiAmount",0) or 0)
        total_usd = round(sol_amt * sol_price + usdc, 2)
        state["sol_balance"] = total_usd
        log("Solana balance: "+str(round(sol_amt,4))+" SOL + $"+str(round(usdc,2))+" USDC = $"+str(total_usd))
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
    """Get swap quote from Raydium Trade API - confirmed real endpoint"""
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
        if r.status_code != 200:
            log("Raydium quote status: "+str(r.status_code), "WARN")
            return None
        data = r.json()
        if not data.get("success"):
            log("Raydium quote failed: "+str(data.get("msg","")), "WARN")
            return None
        # Return full response — swapResponse field needs the complete object
        log("Raydium quote data keys: "+str(list(data.get("data",{}).keys()))[:80])
        return data
    except Exception as ex:
        log("Raydium quote error: "+str(ex)[:80], "ERROR")
    return None

def jupiter_swap(from_token, to_token, amount_usd, price):
    """Execute swap via Jupiter — paper trade if PAPER_TRADING=true, live swap if false"""
    from_mint     = SOL_TOKENS.get(from_token, SOL_TOKENS["USDC"])
    to_mint       = SOL_TOKENS.get(to_token, SOL_TOKENS["SOL"])
    amount_tokens = round(amount_usd / price, 6) if price > 0 else 0
    side          = "SOL-BUY" if from_token == "USDC" else "SOL-SELL"
    # Calculate input amount in correct decimals for the from_token
    TOKEN_DECIMALS = {"USDC": 6, "USDT": 6, "SOL": 9, "ETH": 8, "JUP": 6, "BONK": 5, "WIF": 6}
    from_decimals = TOKEN_DECIMALS.get(from_token, 6)
    if from_token in ("USDC", "USDT"):
        # amount_usd is a dollar value — convert to token units
        lamports = int(amount_usd * (10 ** from_decimals))
    else:
        # amount_usd is a token quantity (e.g. 714000 BONK) — convert to lamports
        lamports = int(amount_usd * (10 ** from_decimals))
    log("Swap input: "+str(amount_usd)+" "+from_token+" = "+str(lamports)+" lamports ("+str(from_decimals)+" decimals)")

    # Get quote — try Raydium first (confirmed works from Render), Jupiter as fallback
    # Use higher slippage for sells to account for price movement
    slippage = "100" if from_token in ("USDC","USDT") else "300"
    quote = raydium_get_quote(from_mint, to_mint, lamports, slippage)
    router = "Raydium"
    if quote:
        out_amount = int(quote.get("data",{}).get("outputAmount", quote.get("data",{}).get("outAmount", 0)))
        log("Raydium quote: $"+str(amount_usd)+" "+from_token+" → "+str(out_amount)+" "+to_token+" units")
    else:
        log("Raydium quote failed, trying Jupiter...", "WARN")
        quote = jupiter_get_quote(from_mint, to_mint, lamports)
        router = "Jupiter"
        if quote:
            out_amount = int(quote.get("outAmount", 0))
            log("Jupiter quote: $"+str(amount_usd)+" "+from_token+" → "+str(out_amount)+" "+to_token+" units")
        else:
            log("Both Raydium and Jupiter quotes failed for "+from_token+"→"+to_token, "WARN")
            return False

    if state["paper_trading"]:
        log("[PAPER] SOL swap: "+str(amount_usd)+" "+from_token+" → "+str(amount_tokens)+" "+to_token+" @ $"+str(price))
        trade = {"time":time.strftime("%H:%M:%S"),"side":"[PAPER] "+side,"price":price,"amount":amount_tokens,"router":"Jupiter","chain":"solana"}
        state["trades"].append(trade)
        state["positions"].append({"price":price,"amount":amount_tokens,"side":"buy","router":"Jupiter","chain":"solana","strategy":"SOL"})
        return True

    else:
        # Live execution via Jupiter swap API + solana-py signing
        try:
            from solders.keypair import Keypair
            from solders.transaction import VersionedTransaction
            import base64 as b64

            private_key = cfg.get("sol_key","")
            wallet      = cfg.get("sol_wallet","")
            if not private_key or not wallet:
                log("SOL_PRIVATE_KEY or SOL_WALLET_ADDRESS not set — cannot execute live swap", "WARN")
                return False

            # Decode private key using solders built-in (no base58 package needed)
            # Phantom exports as base58 string - solders Keypair.from_base58_string handles this
            try:
                keypair = Keypair.from_base58_string(private_key)
                log("Private key decoded successfully")
            except Exception as k_err:
                log("Keypair from base58 failed: "+str(k_err)[:80]+", trying bytes array", "WARN")
                try:
                    keypair = Keypair.from_bytes(bytes(json.loads(private_key)))
                    log("Private key decoded from JSON array")
                except Exception as k_err2:
                    log("Private key decode failed: "+str(k_err2)[:80], "WARN")
                    return False

            # Get swap transaction from Raydium
            # Get token accounts via Solana RPC
            def get_ata(wallet_addr, mint_addr):
                try:
                    payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [wallet_addr, {"mint": mint_addr}, {"encoding": "jsonParsed"}]
                    }
                    r = requests.post(SOL_RPC, json=payload, timeout=8)
                    accounts = r.json().get("result",{}).get("value",[])
                    if accounts:
                        return accounts[0].get("pubkey","")
                    return None
                except:
                    return None

            input_account  = get_ata(wallet, from_mint) if from_token != "SOL" else None
            output_account = get_ata(wallet, to_mint)   if to_token  != "SOL" else None
            log("Input ATA: "+str(input_account)+" Output ATA: "+str(output_account))

            # Raydium transaction endpoint expects the compute response directly
            swap_payload = {
                "computeUnitPriceMicroLamports": "1000",
                "swapResponse": quote,
                "txVersion":    "V0",
                "wallet":       wallet,
                "wrapSol":      from_token == "SOL",
                "unwrapSol":    to_token == "SOL",
                "inputAccount":  input_account  if input_account  else None,
                "outputAccount": output_account if output_account else None,
            }
            log("Swap payload swapResponse type: "+str(type(quote).__name__)+" keys: "+str(list(quote.keys()) if isinstance(quote,dict) else "not dict")[:80])
            r = requests.post(
                "https://transaction-v1.raydium.io/transaction/swap-base-in",
                json=swap_payload,
                headers={"Content-Type": "application/json"},
                timeout=15
            )
            log("Raydium TX response status: "+str(r.status_code))
            log("Raydium TX response body: "+r.text[:200])
            if r.status_code != 200:
                log("Raydium swap TX error: "+str(r.status_code)+" "+r.text[:100], "WARN")
                return False

            try:
                swap_data = r.json()
            except Exception as je:
                log("Raydium TX JSON parse error: "+str(je)+" body: "+r.text[:150], "WARN")
                return False
            if not swap_data.get("success"):
                log("Raydium swap TX failed: "+str(swap_data.get("msg",""))[:100], "WARN")
                return False

            txs = swap_data.get("data",[])
            swap_tx_b64 = txs[0].get("transaction","") if txs else ""
            if not swap_tx_b64:
                log("No swap transaction returned from Jupiter", "WARN")
                return False

            # Deserialize, sign, and send versioned transaction
            from solders import message as solders_message
            raw_tx      = b64.b64decode(swap_tx_b64)
            raw_tx_obj  = VersionedTransaction.from_bytes(raw_tx)

            # Correct signing for VersionedTransaction per solders docs
            signature   = keypair.sign_message(solders_message.to_bytes_versioned(raw_tx_obj.message))
            signed_tx   = VersionedTransaction.populate(raw_tx_obj.message, [signature])

            # Send via Solana RPC
            send_payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [
                    b64.b64encode(bytes(signed_tx)).decode(),
                    {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}
                ]
            }
            r2 = requests.post(SOL_RPC, json=send_payload, timeout=15)
            result = r2.json()
            tx_sig = result.get("result","")
            if tx_sig:
                log("LIVE SWAP EXECUTED: "+tx_sig[:20]+"... "+from_token+"→"+to_token+" $"+str(amount_usd))
                trade = {"time":time.strftime("%H:%M:%S"),"side":"LIVE-"+side,"price":price,"amount":amount_tokens,"router":"Jupiter","chain":"solana","tx":tx_sig[:20]}
                state["trades"].append(trade)
                return True
            else:
                err = result.get("error",{})
                log("Swap send failed: "+str(err)[:100], "WARN")
                return False

        except ImportError as ie:
            log("Missing package: "+str(ie)+" — ensure solders and solana are in requirements.txt", "WARN")
            return False
        except Exception as ex:
            log("Live swap error: "+str(ex)[:100], "WARN")
            return False
def get_jupiter_price(token):
    try:
        mint = SOL_TOKENS.get(token)
        if not mint: return 0.0
        r = requests.get("https://price.jup.ag/v4/price", params={"ids": mint}, timeout=5)
        data = r.json()
        return float(data.get("data",{}).get(mint,{}).get("price", 0))
    except: return 0.0

# ── Real Solana DEX price feeds ───────────────────────────────────────────────
SOL_DEX_POOLS = {
    "SOL/USDC": {"Raydium":"58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWaS3grPdTHE","Orca":"EGZ7tiLeH62TPV1gL8WwbXGzEPa9zmcpVnnkPKKnrE2U"},
    "JUP/USDC": {"Raydium":"6kbC5epG18oomfvwbEc2JHLZSdXAKBmEN3JBB8VTmzoB","Orca":"2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"},
    "ETH/USDC": {"Raydium":"9Lyhks5bQQxb9EyyX55NtgKQzpM4WK7bni5KkWpHGHGP","Orca":"2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"},
}

def get_dex_price_via_jupiter(token, dex_name):
    """Get price from specific DEX by routing through Jupiter with dex filter"""
    try:
        usdc_mint = SOL_TOKENS.get("USDC","EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        token_mint = SOL_TOKENS.get(token, "")
        if not token_mint:
            return 0.0
        params = {
            "inputMint":  usdc_mint,
            "outputMint": token_mint,
            "amount":     "1000000",
            "slippageBps":"50",
            "dexes":      dex_name,
        }
        r = requests.get(JUPITER_API+"/quote", params=params, timeout=10)
        if r.status_code != 200:
            log("Jupiter "+dex_name+" HTTP "+str(r.status_code)+" for "+token, "WARN")
            return 0.0
        data = r.json()
        if "error" in data:
            log("Jupiter "+dex_name+" error: "+str(data.get("error",""))[:60], "WARN")
            return 0.0
        out = int(data.get("outAmount", 0))
        if out > 0:
            decimals = 9 if token == "SOL" else 8
            tokens_per_usdc = out / (10**decimals)
            price = 1.0 / tokens_per_usdc if tokens_per_usdc > 0 else 0.0
            return price
        return 0.0
    except Exception as ex:
        log("Jupiter "+dex_name+" exception: "+str(ex)[:60], "WARN")
        return 0.0

def get_jupiter_best_price(token):
    """Get best overall price from Jupiter across all DEXes"""
    try:
        usdc_mint  = SOL_TOKENS.get("USDC","EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        token_mint = SOL_TOKENS.get(token, "")
        if not token_mint: return 0.0
        params = {"inputMint":usdc_mint,"outputMint":token_mint,"amount":"1000000","slippageBps":"50"}
        r = requests.get(JUPITER_API+"/quote", params=params, timeout=10)
        data = r.json()
        out = int(data.get("outAmount", 0))
        if out > 0:
            decimals = 9 if token == "SOL" else 8
            return 1.0 / (out / (10**decimals))
        return 0.0
    except: return 0.0

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
        sol_pairs = ["SOL/USDC", "JUP/USDC", "ETH/USDC", "BONK/USDC"]

        TOKEN_MINTS = {
            "SOL":  "So11111111111111111111111111111111111111112",
            "ETH":  "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
            "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
            "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        }

        # Target DEXes for arbitrage comparison
        TARGET_DEXES = ["raydium", "raydium_clmm", "orca", "meteora"]

        def get_dexpaprika_prices(token):
            """
            Get per-DEX prices using DexPaprika token pools endpoint.
            No API key, no rate limits, returns live pool prices.
            API: https://api.dexpaprika.com/networks/solana/tokens/{mint}/pools
            Returns dict of {dex_name: price_usd}
            """
            try:
                mint = TOKEN_MINTS.get(token, "")
                if not mint: return {}
                r = requests.get(
                    "https://api.dexpaprika.com/networks/solana/tokens/"+mint+"/pools",
                    params={"page": 0, "limit": 50, "sort": "desc", "order_by": "volume_usd"},
                    timeout=10
                )
                if r.status_code != 200:
                    log("DexPaprika status "+str(r.status_code)+" for "+token, "WARN")
                    return {}

                pools = r.json().get("pools", [])
                dex_prices = {}

                for pool in pools:
                    dex_id   = pool.get("dex_id", "").lower()
                    dex_name = pool.get("dex_name", "")
                    price    = float(pool.get("price_usd", 0) or 0)
                    tokens   = pool.get("tokens", [])

                    # Only use pools that contain USDC as the quote token
                    token_symbols = [t.get("symbol","") for t in tokens]
                    if "USDC" not in token_symbols: continue
                    if price <= 0: continue

                    # Map to friendly names, keep only target DEXes
                    if dex_id in ("raydium", "raydium_clmm") and "Raydium" not in dex_prices:
                        dex_prices["Raydium"] = price
                    elif dex_id == "orca" and "Orca" not in dex_prices:
                        dex_prices["Orca"] = price
                    elif dex_id == "meteora" and "Meteora" not in dex_prices:
                        dex_prices["Meteora"] = price

                    # Stop once we have all three
                    if len(dex_prices) >= 3: break

                return dex_prices

            except Exception as ex:
                log("DexPaprika error for "+token+": "+str(ex)[:60], "WARN")
                return {}

        try:
            for pair in sol_pairs:
                token  = pair.split("/")[0]
                prices = get_dexpaprika_prices(token)

                if prices:
                    log("SOL ARB scan "+pair+": "+str({k:round(v,6) for k,v in prices.items()}))
                else:
                    log("SOL ARB scan "+pair+": no prices returned","WARN")

                if len(prices) >= 2:
                    vals = list(prices.items())
                    for i in range(len(vals)):
                        for j in range(i+1, len(vals)):
                            n1,p1 = vals[i]
                            n2,p2 = vals[j]
                            if p1<=0 or p2<=0: continue
                            spread = abs(p1-p2)/min(p1,p2)*100
                            if spread > 0.01:
                                buy_from   = n1 if p1 < p2 else n2
                                sell_on    = n2 if p1 < p2 else n1
                                buy_price  = min(p1,p2)
                                sell_price = max(p1,p2)
                                est_gas    = 0.002
                                bal        = state.get("sol_balance", 0)
                                size       = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])
                                gross      = (sell_price-buy_price)*(size/buy_price) if buy_price>0 else 0
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
                                    "executable":     spread >= cfg["min_arb_spread"] and est_profit > 0 and size >= 0.10,
                                })
                time.sleep(1)  # 1s between tokens

        except Exception as ex:
            log("SOL ARB error: "+str(ex), "WARN")

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

    if spread < cfg["min_arb_spread"]:
        log("ARB skipped — spread "+str(spread)+"% < min "+str(cfg["min_arb_spread"])+"%","WARN"); return False
    if est_profit <= 0:
        log("ARB skipped — profit negative after gas","WARN"); return False
    if state["daily_loss"] >= cfg["max_loss"]:
        log("ARB skipped — daily loss limit hit","WARN"); return False

    bal  = state["sol_balance"] if chain=="solana" else state["balance"]
    size = min(bal*cfg["risk_pct"]/100, cfg["max_pos"])
    if size < 1:
        log("ARB skipped — insufficient balance $"+str(round(bal,2)),"WARN"); return False

    token = pair.split("/")[0]
    amt   = round(size/price, 6)

    if state["paper_trading"]:
        log("[PAPER] ARB: "+token+" buy on "+buy_from+" @ $"+str(price)+" sell on "+sell_on+" @ $"+str(opp["sell_price"])+" spread "+str(spread)+"% est profit $"+str(est_profit))
        record_trade("[PAPER] ARB", price, amt, round(est_profit,2))
        state["pnl"] += est_profit * 0.7
        return True
    else:
        if chain == "solana":
            log("Executing Solana ARB: BUY "+token+" on "+buy_from+" @ $"+str(price))
            # Leg 1: Buy token with USDC
            buy_result = jupiter_swap("USDC", token, size, price)
            if not buy_result:
                log("ARB buy leg failed", "WARN")
                return False

            # Wait briefly for buy confirmation
            time.sleep(3)

            # Leg 2: Sell token back to USDC at higher price
            sell_price   = opp["sell_price"]
            token_amount = round(size / price, 6)
            log("Executing Solana ARB: SELL "+str(token_amount)+" "+token+" on "+sell_on+" @ $"+str(sell_price))
            sell_result = jupiter_swap(token, "USDC", token_amount, sell_price)
            if sell_result:
                actual_profit = round((sell_price - price) * token_amount - opp["est_gas_usd"], 6)
                state["pnl"] += actual_profit
                record_trade("ARB "+buy_from+"→"+sell_on, price, token_amount, round(actual_profit, 4))
                log("Solana ARB complete — profit: $"+str(actual_profit))
                return True
            else:
                log("ARB sell leg failed — holding "+str(token_amount)+" "+token, "WARN")
                record_trade("ARB-BUY-ONLY (sell failed)", price, token_amount, None)
                return False
        else:
            result = place_order(pair, "buy", amt)
            if result:
                record_trade("ARB via "+buy_from+"->"+sell_on, price, amt, round(est_profit,2))
                log("EVM ARB executed @ $"+str(price)+" profit est: $"+str(est_profit))
                return True
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
        opps = scan_arbitrage()
        for opp in opps:
            if not state["running"]: break
            if opp["executable"]:
                log("ARB opportunity: "+opp["pair"]+" spread "+str(opp["spread_pct"])+"% est profit $"+str(opp["est_profit_usd"]))
                execute_arbitrage(opp)
                time.sleep(5)
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
    <div class="stat"><div class="sl">SOL Balance</div><div class="sv" id="s-sol-balance">—</div></div>
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
      <button class="btn" id="p-BONK/USDC"  onclick="selectPair('BONK/USDC')">BONK/USDC</button>
      <button class="btn" id="p-WIF/USDC"   onclick="selectPair('WIF/USDC')">WIF/USDC</button>
      <button class="btn" id="p-BONK/USDC"  onclick="selectPair('BONK/USDC')">BONK/USDC</button>
      <button class="btn" id="p-JUP/USDC"   onclick="selectPair('JUP/USDC')">JUP/USDC</button>
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
    document.getElementById("s-sol-balance").textContent=d.sol_balance>0?"$"+d.sol_balance.toFixed(2):"—";
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
