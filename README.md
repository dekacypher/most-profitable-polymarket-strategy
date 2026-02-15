# Most Profitable Polymarket Strategy

**Complete-Set Maker Bot** — bid on both YES and NO, redeem at $1.00. Mathematical arbitrage, no market prediction.

- **Paper results**: +$46.88 (100% win rate, 239% ROI)
- **Strategy**: Limit orders at $0.02 per leg → redeem at resolution for $1.00
- **Redemption**: Configured to redeem as soon as the market ends (no delay)

Full documentation: [MOST_PROFITABLE_STRATEGY.md](MOST_PROFITABLE_STRATEGY.md)

## Quick start

```bash
# Install
pip install -r requirements.txt

# Copy env and add your Polymarket credentials
cp .env.example .env

# Paper trading
python -m bot.main --env .env

# Live trading
python -m bot.main --live --env .env

# 24h background
python -m bot.run_24h --live --env .env
```

## Repo

Standalone copy of the most profitable strategy from the reverse-engineer-polymarket project. Private.
