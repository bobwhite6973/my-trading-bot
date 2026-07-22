# LeverBot — Automated Grid Trading Bot for Solana DEXs

Run automated spot grid trading on Solana DEXs (Raydium, Jupiter). Multi-pair, smart trailing buys/sells, partial profit taking, auto-compounding.

## One-Click Deploy to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/bobwhite6973/my-trading-bot&branch=release)

1. Click the button above
2. Set your environment variables (LICENSE_KEY, SOLANA_PRIVATE_KEY, etc.)
3. Render spins up your bot in ~2 minutes
4. Open your Render URL to access the dashboard

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LICENSE_KEY` | Yes | Your LeverBot license key (LB-XXXX-XXXX-XXXX) |
| `SOLANA_PRIVATE_KEY` | Yes (live) | Solana wallet private key |
| `PAPER_TRADING` | No | Set to `true` for demo mode (default: true) |
| `API_SECRET` | No | Dashboard API authentication |
| `TG_BOT_TOKEN` | No | Telegram bot token for alerts |
| `TG_CHAT_ID` | No | Telegram chat ID for alerts |
| `PORT` | No | Server port (default: 10000) |

Full config in `.env.example`.

## License

LeverBot requires a valid license key. Get yours at [aitrader.ctonew.app](https://aitrader.ctonew.app).

- Trial: 7 days, full functionality
- Full: One-time purchase, no recurring fees

## Manual Deploy

```bash
pip install -r requirements.txt
export LICENSE_KEY="LB-YOUR-KEY-HERE"
export PAPER_TRADING="true"
python main.py
```

## Support

- Website: https://aitrader.ctonew.app
- Repository: https://github.com/bobwhite6973/my-trading-bot
