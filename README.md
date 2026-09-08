# Solana Smart-Money Scanner V2.4

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


## V2.4 fixes
- Removed wallets now disappear from the watchlist immediately (they remain inactive in the database for history).
- Failed Birdeye analysis no longer leaves a new ghost wallet with blank stats.
- Analyze/Add shows an obvious busy state and success/error feedback.
- Existing unscored wallets have an Analyze button so they can be retried without removing them.
- Added a Test Birdeye button that checks the configured API key/connection.
- Birdeye HTTP errors are surfaced on screen instead of silently appearing as blank stats.


## V2.4
- Fixes feedback messages on mobile Chrome by avoiding the browser's built-in `window.status`.
- Birdeye Test now shows a visible result.
- Adds a 15-second browser timeout for the API test.
- Wallet analysis has a 30-second browser timeout.


## V2.4
- Adds a server-side Birdeye request queue for the free/Standard rate limit.
- Keeps Birdeye requests at least 1.15 seconds apart.
- Automatically retries HTTP 429 responses with backoff.
- No paid Birdeye upgrade is required for this fix.


## V2.4
- Wallet score now weighs realized P&L, average profit per trade, trade sample size, and consistency.
- Raw win rate is intentionally a smaller part of the score.
- Pulls both Net Cash and WAC wallet P&L for comparison.
- Adds 30d/90d P&L, average profit/trade, and WAC 90d P&L to the watchlist table.
- Existing Neon database is migrated automatically.
