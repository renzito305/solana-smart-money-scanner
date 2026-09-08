# Solana Smart-Money Scanner — Phone Deploy Edition

This is the simplified phone-friendly version:
- `main.py`
- `requirements.txt`
- `render.yaml`
- `README.md`

It is designed to be easy to upload to GitHub from an Android phone and deploy to Render.

## Important
This version uses a local SQLite file for the first deployment test. On Render Free, local filesystem data can disappear when the service spins down, restarts, or redeploys. That is okay for the first "get it online" step. The next upgrade is persistent Postgres.

Never upload API keys, wallet private keys, or seed phrases to GitHub.

## Quick test after Render deploys
Open your Render URL.

Then add `/demo` to the end once, for example:
`https://your-app.onrender.com/demo`

Return to the main URL and you should see three demo wallets/signals.


## Paper account
This build starts with a **$500 simulated bankroll** so the paper test matches the amount you would realistically consider risking later.
