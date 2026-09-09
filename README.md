# Solana Smart-Money Scanner V2.7

Paper trading only. This update fixes refresh diagnostics and schedules price/exit checks. It does not promise profits or change the strategy.

## Install over your current version

1. Extract the ZIP. It contains main.py, requirements.txt, render.yaml, and this README.md at the top level.
2. In your existing GitHub repository, choose Add file → Upload files. Upload these four extracted files into the same directory as the existing main.py, then commit. Do not upload the ZIP itself or a containing folder.
3. Let your existing Render service deploy the commit (or use Manual Deploy → Deploy latest commit if automatic deployment is off).
4. Keep your existing DATABASE_URL, BIRDEYE_API_KEY and HELIUS_WEBHOOK_SECRET in Render. No keys belong in these files. Keep the existing database connection to preserve records.
5. Confirm V2.7 appears at the top of the app. Open Paper Trades and tap Refresh prices. Expect immediate “Refreshing…” feedback, then a count of updates, closes and failures, or an explanation of the failure. Send the exact message if all quotes still fail.

The database migration only adds columns. Existing wallets, signals and trades are retained; it does not reset the $500 account. Old price timestamps were not recorded, so old positions initially show unverified until a successful check. Old signal descriptions remain historical; only new signals distinguish unavailable liquidity from $0.

## What was wrong

- refresh_paper caught every exception and discarded it; old prices remained with no warning.
- The refresh button ignored the response and had no busy, success, or failure state.
- Market-request failures were converted to missing values, but rendered as liquidity $0.
- Exit checks ran after a webhook batch or manual click; there was no independent schedule.

These code defects are confirmed. The specific cause of the production market-data failure cannot be determined without a real provider response. This version reveals it rather than claiming to fix an unknown account/API problem.

## What changed

- Manual refresh queues a background check and returns promptly; the page polls status every five seconds.
- A single process timer runs a price/exit cycle at startup and waits 60 seconds after each cycle before the next. It works without a browser or new signals while the server is awake.
- Concurrent refresh cycles are prevented; single-process position allocation is serialized.
- Per-position successful-check timestamps, provider quote timestamps when supplied, and latest errors are saved. Failed checks retain last known prices, flag them, and never execute an exit with a failed quote.
- Prices must be positive and finite. Quotes with a supplied timestamp older than 180 seconds are rejected for both entry and exit. When the provider omits its timestamp, the UI says so: a successful fetch is not independent proof of freshness.
- Unknown liquidity stays unknown and blocks new entries; price-only responses can still update existing positions.
- The equity figure has an asterisk when any open position has a stale/unverified price. Read the warning below the refresh button.
- Webhook batches no longer trigger a five-position refresh for every batch, reducing redundant requests. Existing request spacing and limited 429 retries remain.
- Exact take-profit and stop-price comparisons avoid rounding around the exit thresholds.

## Your strategy defaults

$500 starting paper cash; up to $25 per position; score 65+; minimum $100,000 liquidity; maximum five open positions; +20% take profit and -10% stop. Existing Render environment overrides still apply. The interface's strategy summary remains the default-settings description.

Exit fills use the next accepted observed quote, which can be beyond the threshold. These are simulated exits, not resting exchange orders. A quote gap cannot be reconstructed. Fees, slippage, taxes, and realistic execution latency are not modeled yet; results remain gross paper estimates.

## Hosting and API limits

Use one Uvicorn worker and one service instance: the refresh and allocation locks are process-local. render.yaml explicitly uses --workers 1; if you have a custom Render Start Command, use:

    uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1

The supplied deployment remains on Render's free plan; no paid-plan change is included. Free services can spin down after 15 minutes without inbound traffic. A Python timer cannot run while the host sleeps. Continuous monitoring requires an always-running deployment; do not assume this update alone provides 24/7 operation.

Official hosting details: https://render.com/docs/free

Five positions at one cycle per minute can request roughly 7,200 quotes/day before signal lookups and retries. Actual frequency is lower when cycles take time. Check your Birdeye allowance. PAPER_REFRESH_SECONDS defaults to 60 (minimum 30); PRICE_STALE_SECONDS defaults to 180 (minimum 60). Longer cycles reduce requests but slow exits. Changing render.yaml in an existing non-Blueprint deployment may not apply environment variables; the same defaults are built into main.py.

- HTTP 401/403: inspect the API key and endpoint permissions in Birdeye.
- HTTP 429: inspect rate limits and remaining quota; a code update cannot grant additional allowance.
- Timeout/network error: check provider/service health and retry.
- Missing/stale token quote: the app will keep that position flagged until usable data arrives.

Secrets are redacted from newly recorded price errors. This is still the original public-dashboard design; authenticated admin controls, durable webhook queuing, multi-instance synchronization, and a full strategy audit are outside this refresh fix. Webhooks still use the existing secret header when configured. In-flight webhook background work can be lost on a restart; this update does not backfill historical missed signals.

## Validation performed

11 offline backend regression tests passed: additive/idempotent old-schema migration; failure visibility and price retention; partial success and recovery; take-profit and stop-loss including exact boundaries; invalid/stale quotes; unavailable liquidity and sequential duplicate delivery; entry constraints and concurrent position cap; refresh endpoint/busy state; timer-driven exits without events; secret redaction.

JavaScript syntax and simulated-DOM checks passed for the button's busy state, partial-failure text, stale-equity marker, and network-failure recovery. These are not a visual browser test. Live Birdeye access, your production PostgreSQL migration, and Render deployment have not been exercised here. No production data or deployment was changed.
