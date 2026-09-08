import os, json, sqlite3
from contextlib import contextmanager
from typing import Any
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DATABASE_PATH", "smart_money.db")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
PAPER_STARTING_CASH = float(os.getenv("PAPER_STARTING_CASH", "100"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "100000"))

app = FastAPI(title="Solana Smart-Money Scanner", version="0.2-phone")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
  address TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  historical_win_rate REAL NOT NULL DEFAULT .50,
  early_entry_rate REAL NOT NULL DEFAULT .50,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  symbol TEXT,
  wallet_address TEXT,
  price_usd REAL,
  liquidity_usd REAL,
  score INTEGER NOT NULL,
  label TEXT NOT NULL,
  reason TEXT NOT NULL,
  signature TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  symbol TEXT,
  usd_amount REAL NOT NULL,
  entry_price REAL NOT NULL,
  current_price REAL NOT NULL,
  quantity REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',
  opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at TEXT,
  exit_price REAL,
  realized_pnl REAL
);
"""

@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init_db():
    with conn() as c:
        c.executescript(SCHEMA)

@app.on_event("startup")
def startup():
    init_db()

def market_price(token_mint: str):
    if not BIRDEYE_API_KEY:
        return {"price": None, "liquidity": None}
    r = requests.get(
        "https://public-api.birdeye.so/defi/price",
        params={"address": token_mint, "include_liquidity": "true"},
        headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"},
        timeout=10,
    )
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    liq = data.get("liquidity")
    if isinstance(liq, dict):
        liq = liq.get("usd") or liq.get("value")
    return {
        "price": float(data["value"]) if data.get("value") is not None else None,
        "liquidity": float(liq) if liq is not None else None,
    }

def score_signal(win, early, liquidity, confirmations=1):
    win = max(0, min(1, float(win)))
    early = max(0, min(1, float(early)))
    liq = float(liquidity or 0)

    score = 40*win + 25*early
    if liq >= 1_000_000: score += 20
    elif liq >= 500_000: score += 17
    elif liq >= 250_000: score += 14
    elif liq >= MIN_LIQUIDITY_USD: score += 10
    elif liq > 0: score += 2

    score += min(15, max(0, confirmations-1)*5)
    if 0 < liq < MIN_LIQUIDITY_USD:
        score -= 15

    score = int(max(0, min(100, round(score))))
    label = "HIGH" if score >= 80 else "MEDIUM" if score >= 65 else "LOW"
    reason = (
        f"wallet win rate {win:.0%}; early-entry rate {early:.0%}; "
        f"liquidity ${liq:,.0f}; watched-wallet confirmation {confirmations}"
    )
    return score, label, reason

def extract_candidate(event: dict[str, Any]):
    sig = event.get("signature")
    event_type = str(event.get("type") or event.get("transactionType") or "UNKNOWN")
    transfers = event.get("tokenTransfers") or []
    for t in transfers:
        mint = t.get("mint") or t.get("tokenMint")
        if mint:
            return {
                "signature": sig,
                "event_type": event_type,
                "token_mint": mint,
                "to_user": t.get("toUserAccount") or t.get("toUser"),
                "from_user": t.get("fromUserAccount") or t.get("fromUser"),
            }
    swap = ((event.get("events") or {}).get("swap") or {})
    outs = swap.get("tokenOutputs") or []
    if outs:
        mint = outs[0].get("mint") or outs[0].get("tokenMint")
        if mint:
            return {
                "signature": sig, "event_type": event_type, "token_mint": mint,
                "to_user": None, "from_user": None
            }
    return None

class WalletIn(BaseModel):
    address: str = Field(min_length=20)
    label: str = "Watched wallet"
    historical_win_rate: float = Field(default=.50, ge=0, le=1)
    early_entry_rate: float = Field(default=.50, ge=0, le=1)

class PaperIn(BaseModel):
    token_mint: str
    symbol: str | None = None
    usd_amount: float = Field(gt=0)

HTML = r"""
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Money Scanner</title>
<style>
:root{color-scheme:dark}body{margin:0;font-family:system-ui,sans-serif;background:#0b0d10;color:#f3f4f6}
.wrap{max-width:1050px;margin:auto;padding:22px}.muted{color:#9ca3af}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}
.card{background:#14171c;border:1px solid #242830;border-radius:14px;padding:15px}.metric{font-size:29px;font-weight:750;margin-top:7px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid #252a32;vertical-align:top}
.pill{display:inline-block;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:800}.HIGH{background:#123a25;color:#8ef0b1}.MEDIUM{background:#3c3214;color:#f5d977}.LOW{background:#3b1b1b;color:#ffabab}
button{background:#f3f4f6;color:#111827;border:0;padding:9px 12px;border-radius:10px;font-weight:750}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.mobilehide{display:none}}
</style></head><body><div class="wrap">
<h1>Solana Smart-Money Scanner</h1>
<div class="muted">Paper trading / research only — no wallet keys and no live execution.</div>
<div class="grid">
<div class="card"><div class="muted">Watched wallets</div><div class="metric" id="wallets">—</div></div>
<div class="card"><div class="muted">Signals</div><div class="metric" id="signals">—</div></div>
<div class="card"><div class="muted">High signals</div><div class="metric" id="high">—</div></div>
<div class="card"><div class="muted">Paper equity</div><div class="metric" id="equity">—</div></div>
</div>
<div style="display:flex;justify-content:space-between;align-items:center"><h2>Latest signals</h2><button onclick="load()">Refresh</button></div>
<div class="card" style="overflow-x:auto"><table><thead><tr>
<th>Level</th><th>Score</th><th>Token</th><th class="mobilehide">Liquidity</th><th class="mobilehide">Wallet</th><th>Why</th>
</tr></thead><tbody id="rows"></tbody></table></div>
<p class="muted">Tip: open <b>/demo</b> once to add sample data.</p>
</div>
<script>
function money(v){return v==null?"—":"$"+Number(v).toLocaleString(undefined,{maximumFractionDigits:2})}
function sh(s){if(!s)return"—";return s.length>13?s.slice(0,6)+"…"+s.slice(-5):s}
async function load(){
 const d=await fetch("/api/dashboard").then(r=>r.json());
 const p=await fetch("/api/paper").then(r=>r.json());
 wallets.textContent=d.wallets;signals.textContent=d.signals;high.textContent=d.high_signals;equity.textContent=money(p.equity_estimate);
 rows.innerHTML="";
 d.latest.forEach(s=>{let tr=document.createElement("tr");tr.innerHTML=`<td><span class="pill ${s.label}">${s.label}</span></td><td><b>${s.score}</b></td><td>${sh(s.symbol||s.token_mint)}</td><td class="mobilehide">${money(s.liquidity_usd)}</td><td class="mobilehide">${sh(s.wallet_address)}</td><td>${s.reason}</td>`;rows.appendChild(tr)})
}
load();setInterval(load,10000)
</script></body></html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/demo")
def demo():
    wallets = [
        ("DemoWallet111111111111111111111111111111111","Wallet A",.74,.68),
        ("DemoWallet222222222222222222222222222222222","Wallet B",.66,.81),
        ("DemoWallet333333333333333333333333333333333","Wallet C",.58,.60),
    ]
    demos = [
        ("DemoTokenAAA111111111111111111111111111","ALPHA",wallets[0][0],.0042,640000,2),
        ("DemoTokenBBB222222222222222222222222222","BETA",wallets[1][0],.018,230000,1),
        ("DemoTokenCCC333333333333333333333333333","GAMMA",wallets[2][0],.0011,68000,1),
    ]
    with conn() as c:
        for w in wallets:
            c.execute("""INSERT OR REPLACE INTO wallets(address,label,historical_win_rate,early_entry_rate,active)
                         VALUES(?,?,?,?,1)""", w)
        for mint,symbol,wallet,price,liq,conf in demos:
            w=next(x for x in wallets if x[0]==wallet)
            score,label,reason=score_signal(w[2],w[3],liq,conf)
            c.execute("""INSERT INTO signals(token_mint,symbol,wallet_address,price_usd,liquidity_usd,score,label,reason)
                         VALUES(?,?,?,?,?,?,?,?)""",(mint,symbol,wallet,price,liq,score,label,reason))
    return {"ok": True, "message": "Demo data added. Go back to /"}

@app.get("/api/dashboard")
def dashboard():
    with conn() as c:
        wallets=c.execute("SELECT COUNT(*) n FROM wallets WHERE active=1").fetchone()["n"]
        signals=c.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"]
        high=c.execute("SELECT COUNT(*) n FROM signals WHERE label='HIGH'").fetchone()["n"]
        latest=[dict(r) for r in c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 20").fetchall()]
    return {"wallets":wallets,"signals":signals,"high_signals":high,"latest":latest}

@app.get("/api/wallets")
def wallets():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM wallets ORDER BY created_at DESC").fetchall()]

@app.post("/api/wallets")
def add_wallet(item: WalletIn):
    with conn() as c:
        c.execute("""INSERT INTO wallets(address,label,historical_win_rate,early_entry_rate)
                     VALUES(?,?,?,?) ON CONFLICT(address) DO UPDATE SET
                     label=excluded.label,historical_win_rate=excluded.historical_win_rate,
                     early_entry_rate=excluded.early_entry_rate,active=1""",
                  (item.address,item.label,item.historical_win_rate,item.early_entry_rate))
    return {"ok": True}

@app.post("/webhooks/helius")
async def helius(request: Request):
    payload=await request.json()
    events=payload if isinstance(payload,list) else [payload]
    created=[]
    with conn() as c:
        watched={r["address"]:dict(r) for r in c.execute("SELECT * FROM wallets WHERE active=1").fetchall()}
        for event in events:
            cand=extract_candidate(event)
            if not cand: continue
            w=None
            for addr in (cand.get("to_user"),cand.get("from_user")):
                if addr in watched: w=watched[addr]; break
            if w is None and len(watched)==1:
                w=next(iter(watched.values()))
            if w is None: continue
            try: market=market_price(cand["token_mint"])
            except Exception: market={"price":None,"liquidity":None}
            recent=c.execute("""SELECT COUNT(DISTINCT wallet_address) n FROM signals
                                WHERE token_mint=? AND created_at>=datetime('now','-60 minutes')""",
                             (cand["token_mint"],)).fetchone()["n"]
            score,label,reason=score_signal(w["historical_win_rate"],w["early_entry_rate"],market.get("liquidity"),max(1,int(recent)+1))
            cur=c.execute("""INSERT INTO signals(token_mint,wallet_address,price_usd,liquidity_usd,score,label,reason,signature)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (cand["token_mint"],w["address"],market.get("price"),market.get("liquidity"),score,label,reason,cand.get("signature")))
            created.append(cur.lastrowid)
    return {"ok": True, "signals_created": created}

@app.get("/api/paper")
def paper():
    with conn() as c:
        pos=[dict(r) for r in c.execute("SELECT * FROM paper_positions ORDER BY id DESC").fetchall()]
    realized=sum((p["realized_pnl"] or 0) for p in pos if p["status"]=="CLOSED")
    unrealized=sum(p["quantity"]*p["current_price"]-p["usd_amount"] for p in pos if p["status"]=="OPEN")
    deployed=sum(p["usd_amount"] for p in pos if p["status"]=="OPEN")
    return {"starting_cash":PAPER_STARTING_CASH,"deployed":round(deployed,2),"realized_pnl":round(realized,2),
            "unrealized_pnl":round(unrealized,2),"equity_estimate":round(PAPER_STARTING_CASH+realized+unrealized,2),"positions":pos}

@app.post("/api/paper/open")
def paper_open(item: PaperIn):
    m=market_price(item.token_mint); price=m.get("price")
    if not price or price<=0: raise HTTPException(400,"No valid market price available.")
    qty=item.usd_amount/price
    with conn() as c:
        cur=c.execute("""INSERT INTO paper_positions(token_mint,symbol,usd_amount,entry_price,current_price,quantity)
                         VALUES(?,?,?,?,?,?)""",(item.token_mint,item.symbol,item.usd_amount,price,price,qty))
    return {"ok":True,"position_id":cur.lastrowid,"entry_price":price,"quantity":qty}

@app.post("/api/paper/refresh")
def paper_refresh():
    with conn() as c:
        opens=c.execute("SELECT * FROM paper_positions WHERE status='OPEN'").fetchall()
        n=0
        for p in opens:
            try:
                price=market_price(p["token_mint"]).get("price")
                if price and price>0:
                    c.execute("UPDATE paper_positions SET current_price=? WHERE id=?",(price,p["id"])); n+=1
            except Exception: pass
    return {"ok":True,"updated":n}

@app.post("/api/paper/close/{position_id}")
def paper_close(position_id:int):
    with conn() as c:
        p=c.execute("SELECT * FROM paper_positions WHERE id=? AND status='OPEN'",(position_id,)).fetchone()
        if not p: raise HTTPException(404,"Open position not found.")
        try: exit_price=market_price(p["token_mint"]).get("price") or p["current_price"]
        except Exception: exit_price=p["current_price"]
        pnl=p["quantity"]*exit_price-p["usd_amount"]
        c.execute("""UPDATE paper_positions SET status='CLOSED',current_price=?,exit_price=?,realized_pnl=?,closed_at=CURRENT_TIMESTAMP WHERE id=?""",
                  (exit_price,exit_price,pnl,position_id))
    return {"ok":True,"realized_pnl":round(pnl,4)}
