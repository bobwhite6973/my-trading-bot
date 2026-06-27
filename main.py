#!/usr/bin/env python3
# My Trading Bot | Ethereum | Standard
# Customer: Customer
import time
from config_loader import load_config
from modules.stop_loss import StopLossModule
from modules.take_profit import TakeProfitModule
from modules.limit_orders import LimitOrdersModule
from modules.risk_manager import RiskManagerModule
from modules.telegram_alerts import TelegramAlertsModule
from modules.copy_trading import CopyTradingModule
from modules.wallet_tracker import WalletTrackerModule
from modules.scanner import ScannerModule

def main():
    cfg = load_config('config.json')
    print('My Trading Bot starting...')
    stop_loss = StopLossModule(cfg)
    take_profit = TakeProfitModule(cfg)
    limit_orders = LimitOrdersModule(cfg)
    risk_manager = RiskManagerModule(cfg)
    telegram_alerts = TelegramAlertsModule(cfg)
    copy_trading = CopyTradingModule(cfg)
    wallet_tracker = WalletTrackerModule(cfg)
    scanner = ScannerModule(cfg)
    wallet_tracker.on('activity',copy_trading.on_activity)
    copy_trading.set_risk(risk_manager)
    stop_loss.start()
    take_profit.start()
    limit_orders.start()
    risk_manager.start()
    telegram_alerts.start()
    copy_trading.start()
    wallet_tracker.start()
    scanner.start()
    print('Running - Ctrl+C to stop')
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: print('Stopped.')

if __name__ == '__main__': main()