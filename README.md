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

## Push to your private GitHub repo

1. On GitHub: **New repository** → name: `most-profitable-polymarket-strategy` → **Private** → Create (do not add README or .gitignore).
2. From this folder:
   ```bash
   git push -u origin main
   ```
   Remote is already set to `git@github.com:dekacypher/most-profitable-polymarket-strategy.git`. If you use a different GitHub user/org, change it:
   ```bash
   git remote set-url origin git@github.com:YOUR_USER/most-profitable-polymarket-strategy.git
   git push -u origin main
   ```
