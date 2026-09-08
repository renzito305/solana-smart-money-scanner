# Solana Smart-Money Scanner V2

V2 is still paper-trading only.

Upgrades:
- Add/remove real Solana wallets from the phone dashboard.
- Birdeye automatically measures 30-day and 90-day wallet PnL/win rate.
- Wallet scoring uses measured history instead of demo numbers.
- Helius webhook returns HTTP 200 immediately and processes events afterward.
- Detects likely token buys and scores them using wallet quality, token liquidity and multi-wallet confirmation.
- Qualified signals can open simulated $25 positions automatically.
- Default paper exits: +20% take profit / -10% stop loss, maximum 5 simultaneous positions.
- Optional persistent PostgreSQL using the `DATABASE_URL` environment variable.

Required private Render variables:
- `BIRDEYE_API_KEY`
- `HELIUS_WEBHOOK_SECRET` (we'll generate a random value and enter the same value in the Helius webhook Authorization field).

Do not start the real paper test while the dashboard says the database is temporary. Render Free loses local SQLite data when the service restarts or spins down. Add a persistent Postgres `DATABASE_URL` first.

When ready, Helius Enhanced Mainnet webhook endpoint:
`https://solana-smart-money-scanner.onrender.com/webhooks/helius`

Use SWAP first and monitor only the vetted wallet addresses.
