#!/usr/bin/env python3
"""
Trading Bot — Web Dashboard + Live Trading
Serves a control panel UI and runs selected strategy against LBank
"""
import os, json, time, hmac, hashlib, threading, requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlparse

# ── Config ────────────────────────────────────────────────────────────────────
cfg = {
    "api_key":    os.environ.get("API_KEY", ""),
    "api_secret": os.environ.get("API_SECRET", ""),
    "pair":       os.environ.get("TRADING_PAIR", "BTC/USDT"),
    "risk_pct":   float(os.environ.get("RISK_PCT", "2")),
    "stop_loss":  float(os.environ.get("STOP_LOSS_PCT", "5")),
    "take_profit":float(os.environ.get("TAKE_PROFIT_PCT", "15")),
    "max_pos":    float(os.environ.get("MAX_POSITION_USD", "500")),
    "max_loss":   float(os.environ.get("MAX_DAILY_LOSS_USD", "200")),
}

# ── Bot State ─────────────────────────────────────────────────────────────────
state = {
    "running":    False,
    "strategy":   None,
    "pair":       cfg["pair"],
    "price":      0.0,
    "balance":    0.0,
    "positions":  [],
    "trades":     [],
    "pnl":        0.0,
    "daily_loss": 0.0,
    "log":        [],
    "error":      None,
}

def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    entry = "["+ts+"] ["+level+"] "+msg
    print(entry)
    state["log"].insert(0, entry)
    if len(state["log"]) > 100:
        state["log"] = state["log"][:100]

# ── LBank API ─────────────────────────────────────────────────────────────────
LBANK_BASE = "https://api.lbank.info/v2"

def lbank_sign(params):
    params["api_key"] = cfg["api_key"]
    params["timestamp"] = str(int(time.time() * 1000))
    sorted_params = sorted(params.items())
    query = urlencode(sorted_params)
    sign = hmac.new(cfg["api_secret"].encode(), query.encode(), hashlib.md5).hexdigest().upper()
    params["sign"] = sign
    return params

def get_price(symbol):
    try:
        sym = symbol.replace("/","_").lower()
        r = requests.get(LBANK_BASE+"/ticker/price.do", params={"symbol": sym}, timeout=5)
        data = r.json()
        if data.get("result") == "true":
            return float(data["data"][0]["price"])
    except Exception as ex:
        log("Price fetch error: "+str(ex), "ERROR")
    return 0.0

def get_balance():
    try:
        params = lbank_sign({"echostr": "test"})
        r = requests.post(LBANK_BASE+"/supplement/user_info.do", data=params, timeout=5)
        data = r.json()
        if data.get("result") == "true":
            info = data.get("data", {}).get("info", {}).get("free", {})
            usdt = float(info.get("usdt", 0))
            state["balance"] = usdt
            return usdt
    except Exception as ex:
        log("Balance fetch error: "+str(ex), "ERROR")
    return 0.0

def place_order(symbol, side, amount):
    try:
        sym = symbol.replace("/","_").lower()
        params = lbank_sign({
            "symbol": sym,
            "type": side,
            "price": "-1",
            "amount": str(amount),
        })
        r = requests.post(LBANK_BASE+"/supplement/create_order.do", data=params, timeout=10)
        data = r.json()
        if data.get("result") == "true":
            return data.get("data", {}).get("order_id")
        else:
            log("Order error: "+str(data.get("error_code")), "ERROR")
    except Exception as ex:
        log("Order exception: "+str(ex), "ERROR")
    return None

# ── Strategies ────────────────────────────────────────────────────────────────

def run_dca():
    log("DCA strategy started on "+state["pair"])
    buy_prices = []
    interval = 60

    while state["running"] and state["strategy"] == "dca":
        price = get_price(state["pair"])
        if price <= 0:
            time.sleep(interval); continue
        state["price"] = price
        bal = get_balance()

        if not buy_prices:
            trade_size = min(bal * cfg["risk_pct"]/100, cfg["max_pos"])
            if trade_size > 1:
                amount = round(trade_size / price, 6)
                oid = place_order(state["pair"], "buy_market", amount)
                if oid:
                    buy_prices.append(price)
                    state["positions"].append({"price": price, "amount": amount, "side": "buy", "strategy": "DCA"})
                    state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "BUY", "price": price, "amount": amount})
                    log("DCA BUY "+str(amount)+" @ "+str(price))
        else:
            avg = sum(buy_prices) / len(buy_prices)
            gain = (price - avg) / avg * 100
            loss = (avg - price) / avg * 100

            if gain >= cfg["take_profit"]:
                total = sum(p["amount"] for p in state["positions"])
                oid = place_order(state["pair"], "sell_market", total)
                if oid:
                    pnl = (price - avg) * total
                    state["pnl"] += pnl
                    state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "SELL", "price": price, "amount": total, "pnl": round(pnl,2)})
                    log("DCA SELL all @ "+str(price)+" | PnL: $"+str(round(pnl,2)))
                    buy_prices.clear()
                    state["positions"].clear()
            elif loss >= cfg["stop_loss"]:
                total = sum(p["amount"] for p in state["positions"])
                oid = place_order(state["pair"], "sell_market", total)
                if oid:
                    pnl = (price - avg) * total
                    state["pnl"] += pnl
                    state["daily_loss"] += abs(pnl)
                    state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "STOP", "price": price, "amount": total, "pnl": round(pnl,2)})
                    log("STOP LOSS triggered @ "+str(price)+" | Loss: $"+str(round(abs(pnl),2)), "WARN")
                    buy_prices.clear()
                    state["positions"].clear()
            elif loss >= 2 and state["daily_loss"] < cfg["max_loss"]:
                trade_size = min(bal * cfg["risk_pct"]/100, cfg["max_pos"])
                if trade_size > 1:
                    amount = round(trade_size / price, 6)
                    oid = place_order(state["pair"], "buy_market", amount)
                    if oid:
                        buy_prices.append(price)
                        state["positions"].append({"price": price, "amount": amount, "side": "buy", "strategy": "DCA"})
                        state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "DCA-BUY", "price": price, "amount": amount})
                        log("DCA averaging down @ "+str(price))

        if state["daily_loss"] >= cfg["max_loss"]:
            log("Daily loss limit hit — pausing trading", "WARN")
            time.sleep(3600)
        time.sleep(interval)

def run_grid():
    log("Grid strategy started on "+state["pair"])
    price = get_price(state["pair"])
    if price <= 0:
        log("Cannot get price — aborting grid", "ERROR"); return

    grid_range = 0.05
    levels = 5
    low  = price * (1 - grid_range)
    high = price * (1 + grid_range)
    step = (high - low) / levels
    grids = [round(low + i * step, 4) for i in range(levels+1)]
    filled = {}
    log("Grid levels: "+str(grids))

    while state["running"] and state["strategy"] == "grid":
        price = get_price(state["pair"])
        if price <= 0:
            time.sleep(30); continue
        state["price"] = price
        bal = get_balance()
        trade_size = min(bal * cfg["risk_pct"]/100, cfg["max_pos"]) / levels

        for i, g in enumerate(grids[:-1]):
            next_g = grids[i+1]
            if g <= price < next_g:
                if i not in filled:
                    amount = round(trade_size / price, 6)
                    oid = place_order(state["pair"], "buy_market", amount)
                    if oid:
                        filled[i] = {"price": price, "amount": amount}
                        state["positions"].append({"price": price, "amount": amount, "side": "buy", "grid": i, "strategy": "Grid"})
                        state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "GRID-BUY", "price": price, "amount": amount})
                        log("Grid BUY level "+str(i)+" @ "+str(price))
                elif price >= filled[i]["price"] * (1 + cfg["take_profit"]/100):
                    amt = filled[i]["amount"]
                    oid = place_order(state["pair"], "sell_market", amt)
                    if oid:
                        pnl = (price - filled[i]["price"]) * amt
                        state["pnl"] += pnl
                        state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "GRID-SELL", "price": price, "amount": amt, "pnl": round(pnl,2)})
                        log("Grid SELL level "+str(i)+" @ "+str(price)+" | PnL: $"+str(round(pnl,2)))
                        del filled[i]
                        state["positions"] = [p for p in state["positions"] if p.get("grid") != i]
        time.sleep(30)

def run_scalp():
    log("Scalping strategy started on "+state["pair"])
    prices = []
    position = None

    while state["running"] and state["strategy"] == "scalp":
        price = get_price(state["pair"])
        if price <= 0:
            time.sleep(10); continue
        state["price"] = price
        prices.append(price)
        if len(prices) > 20: prices.pop(0)

        if len(prices) < 10:
            time.sleep(10); continue

        sma = sum(prices) / len(prices)
        bal = get_balance()
        trade_size = min(bal * cfg["risk_pct"]/100, cfg["max_pos"])

        if position is None and price < sma * 0.999 and trade_size > 1:
            amount = round(trade_size / price, 6)
            oid = place_order(state["pair"], "buy_market", amount)
            if oid:
                position = {"price": price, "amount": amount}
                state["positions"] = [{"price": price, "amount": amount, "side": "buy", "strategy": "Scalp"}]
                state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "SCALP-BUY", "price": price, "amount": amount})
                log("Scalp BUY @ "+str(price))
        elif position:
            gain = (price - position["price"]) / position["price"] * 100
            loss = (position["price"] - price) / position["price"] * 100
            if gain >= cfg["take_profit"] / 3 or loss >= cfg["stop_loss"] / 2:
                oid = place_order(state["pair"], "sell_market", position["amount"])
                if oid:
                    pnl = (price - position["price"]) * position["amount"]
                    state["pnl"] += pnl
                    if pnl < 0: state["daily_loss"] += abs(pnl)
                    state["trades"].append({"time": time.strftime("%H:%M:%S"), "side": "SCALP-SELL", "price": price, "amount": position["amount"], "pnl": round(pnl,2)})
                    log("Scalp SELL @ "+str(price)+" | PnL: $"+str(round(pnl,2)))
                    position = None
                    state["positions"] = []
        time.sleep(10)

def run_copy():
    source = os.environ.get("SOURCE_WALLET","")
    log("Copy trading — watching: "+source)
    seen = set()
    while state["running"] and state["strategy"] == "copy":
        # Placeholder: in production, poll on-chain API for source wallet txs
        log("Copy trading active — monitoring "+source)
        time.sleep(60)

STRATEGIES = {"dca": run_dca, "grid": run_grid, "scalp": run_scalp, "copy": run_copy}

def start_strategy(name, pair):
    if state["running"]:
        log("Already running — stop first", "WARN"); return
    state["strategy"] = name
    state["pair"] = pair
    state["running"] = True
    state["error"] = None
    t = threading.Thread(target=STRATEGIES[name], daemon=True)
    t.start()
    log("Started "+name.upper()+" on "+pair)

def stop_bot():
    state["running"] = False
    state["strategy"] = None
    log("Bot stopped")

# ── Dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Trading Bot Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#080808;color:#eee;padding:20px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:22px;font-weight:900;color:#fff;margin-bottom:4px}
.sub{font-size:13px;color:#444;margin-bottom:28px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px;margin-bottom:20px}
.stat{background:#111;border:1px solid #1a1a1a;border-radius:10px;padding:16px}
.stat-label{font-size:10px;font-weight:700;letter-spacing:2px;color:#555;text-transform:uppercase;margin-bottom:6px}
.stat-value{font-size:24px;font-weight:900;color:#fff}
.stat-value.green{color:#00ff9d}
.stat-value.red{color:#ff6b6b}
.stat-value.yellow{color:#ffd43b}
.card{background:#111;border:1px solid #1a1a1a;border-radius:10px;padding:20px;margin-bottom:16px}
.card-title{font-size:11px;font-weight:700;letter-spacing:2px;color:#00ff9d;text-transform:uppercase;margin-bottom:16px}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.btn{padding:10px 20px;border:none;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;transition:all .15s}
.btn-strategy{background:#1a1a1a;color:#888;border:1.5px solid #222}
.btn-strategy.active{background:#00ff9d18;color:#00ff9d;border-color:#00ff9d}
.btn-pair{background:#1a1a1a;color:#888;border:1.5px solid #222}
.btn-pair.active{background:#4dabf718;color:#4dabf7;border-color:#4dabf7}
.btn-start{background:#00ff9d;color:#000;padding:12px 32px;font-size:14px}
.btn-stop{background:#ff6b6b22;color:#ff6b6b;border:1.5px solid #ff6b6b44;padding:12px 32px;font-size:14px}
.btn-start:disabled{background:#1a1a1a;color:#333;cursor:not-allowed}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#444;font-weight:700;text-align:left;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:10px;letter-spacing:1px;text-transform:uppercase}
td{padding:8px 0;border-bottom:1px solid #111;color:#aaa}
td.buy{color:#00ff9d;font-weight:700}
td.sell{color:#ff6b6b;font-weight:700}
td.stop{color:#ffd43b;font-weight:700}
.log-box{background:#0a0a0a;border:1px solid #1a1a1a;border-radius:8px;padding:14px;height:200px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.8}
.log-box .info{color:#555}
.log-box .warn{color:#ffd43b}
.log-box .error{color:#ff6b6b}
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
.status-dot.on{background:#00ff9d;box-shadow:0 0 8px #00ff9d}
.status-dot.off{background:#333}
@media(max-width:600px){.grid{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Trading Bot</h1>
  <div class="sub" id="status-line"><span class="status-dot off" id="dot"></span>Stopped</div>

  <div class="grid">
    <div class="stat"><div class="stat-label">Price</div><div class="stat-value" id="price">—</div></div>
    <div class="stat"><div class="stat-label">Balance (USDT)</div><div class="stat-value" id="balance">—</div></div>
    <div class="stat"><div class="stat-label">Total P&amp;L</div><div class="stat-value" id="pnl">$0.00</div></div>
    <div class="stat"><div class="stat-label">Open Positions</div><div class="stat-value" id="positions">0</div></div>
  </div>

  <div class="card">
    <div class="card-title">Strategy</div>
    <div class="btn-row">
      <button class="btn btn-strategy" id="s-dca" onclick="selectStrategy('dca')">DCA</button>
      <button class="btn btn-strategy" id="s-grid" onclick="selectStrategy('grid')">Grid</button>
      <button class="btn btn-strategy" id="s-scalp" onclick="selectStrategy('scalp')">Scalping</button>
      <button class="btn btn-strategy" id="s-copy" onclick="selectStrategy('copy')">Copy Trading</button>
    </div>
    <div class="card-title">Trading Pair</div>
    <div class="btn-row">
      <button class="btn btn-pair" id="p-BTC/USDT" onclick="selectPair('BTC/USDT')">BTC/USDT</button>
      <button class="btn btn-pair" id="p-ETH/USDT" onclick="selectPair('ETH/USDT')">ETH/USDT</button>
      <button class="btn btn-pair" id="p-BNB/USDT" onclick="selectPair('BNB/USDT')">BNB/USDT</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-start" id="start-btn" onclick="startBot()" disabled>Select Strategy & Pair</button>
      <button class="btn btn-stop" onclick="stopBot()">Stop Bot</button>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Recent Trades</div>
    <table>
      <thead><tr><th>Time</th><th>Action</th><th>Price</th><th>Amount</th><th>P&amp;L</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="5" style="color:#333;text-align:center;padding:20px">No trades yet</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-title">Live Log</div>
    <div class="log-box" id="log-box"></div>
  </div>
</div>

<script>
var selectedStrategy = null;
var selectedPair = null;

function selectStrategy(s) {
  selectedStrategy = s;
  document.querySelectorAll('.btn-strategy').forEach(b=>b.classList.remove('active'));
  document.getElementById('s-'+s).classList.add('active');
  updateStartBtn();
}

function selectPair(p) {
  selectedPair = p;
  document.querySelectorAll('.btn-pair').forEach(b=>b.classList.remove('active'));
  document.getElementById('p-'+p).classList.add('active');
  updateStartBtn();
}

function updateStartBtn() {
  var btn = document.getElementById('start-btn');
  if(selectedStrategy && selectedPair) {
    btn.disabled = false;
    btn.textContent = 'Start ' + selectedStrategy.toUpperCase() + ' on ' + selectedPair;
  } else {
    btn.disabled = true;
    btn.textContent = 'Select Strategy & Pair';
  }
}

function startBot() {
  if(!selectedStrategy||!selectedPair) return;
  fetch('/start?strategy='+selectedStrategy+'&pair='+encodeURIComponent(selectedPair))
    .then(r=>r.json()).then(d=>console.log(d));
}

function stopBot() {
  fetch('/stop').then(r=>r.json()).then(d=>console.log(d));
}

function formatPnl(v) {
  if(v==null) return '—';
  var s = (v>=0?'+':'')+'$'+Math.abs(v).toFixed(2);
  return '<span style="color:'+(v>=0?'#00ff9d':'#ff6b6b')+'">'+s+'</span>';
}

function refresh() {
  fetch('/state').then(r=>r.json()).then(d=>{
    var running = d.running;
    var dot = document.getElementById('dot');
    dot.className = 'status-dot '+(running?'on':'off');
    document.getElementById('status-line').innerHTML = '<span class="status-dot '+(running?'on':'off')+'" id="dot"></span>'+(running?'Running — '+(d.strategy||'').toUpperCase()+' on '+d.pair:'Stopped');
    document.getElementById('price').textContent = d.price>0?'$'+d.price.toFixed(4):'—';
    document.getElementById('balance').textContent = d.balance>0?'$'+d.balance.toFixed(2):'—';
    var pnlEl = document.getElementById('pnl');
    pnlEl.textContent = (d.pnl>=0?'+':'')+'$'+Math.abs(d.pnl).toFixed(2);
    pnlEl.className = 'stat-value '+(d.pnl>0?'green':d.pnl<0?'red':'');
    document.getElementById('positions').textContent = (d.positions||[]).length;

    var tbody = document.getElementById('trades-body');
    var trades = (d.trades||[]).slice().reverse().slice(0,20);
    if(trades.length===0) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:#333;text-align:center;padding:20px">No trades yet</td></tr>';
    } else {
      tbody.innerHTML = trades.map(t=>
        '<tr>'+
        '<td>'+t.time+'</td>'+
        '<td class="'+(t.side.includes('BUY')?'buy':t.side.includes('STOP')?'stop':'sell')+'">'+t.side+'</td>'+
        '<td>$'+parseFloat(t.price).toFixed(4)+'</td>'+
        '<td>'+parseFloat(t.amount).toFixed(6)+'</td>'+
        '<td>'+formatPnl(t.pnl)+'</td>'+
        '</tr>'
      ).join('');
    }

    var logBox = document.getElementById('log-box');
    logBox.innerHTML = (d.log||[]).map(l=>{
      var cls = l.includes('[WARN]')?'warn':l.includes('[ERROR]')?'error':'info';
      return '<div class="'+cls+'">'+l+'</div>';
    }).join('');

    if(running && d.strategy) {
      document.getElementById('s-'+d.strategy) && document.getElementById('s-'+d.strategy).classList.add('active');
    }
  }).catch(console.error);
}

setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>'''

# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self.respond(200, "text/html", DASHBOARD.encode())
        elif path == "/state":
            self.respond(200, "application/json", json.dumps(state).encode())
        elif path == "/start":
            strat = params.get("strategy",["dca"])[0]
            pair  = params.get("pair",[cfg["pair"]])[0]
            start_strategy(strat, pair)
            self.respond(200, "application/json", b'{"ok":true}')
        elif path == "/stop":
            stop_bot()
            self.respond(200, "application/json", b'{"ok":true}')
        else:
            self.respond(404, "text/plain", b"Not found")

    def respond(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args): pass

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    log("Starting dashboard on port "+str(port))
    server = HTTPServer(("0.0.0.0", port), Handler)
    log("Dashboard ready — open your Render URL to control the bot")
    server.serve_forever()
