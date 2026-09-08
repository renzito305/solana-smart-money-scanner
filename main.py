import os, json, math, time, threading
from typing import Any
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

BIRDEYE_API_KEY=os.getenv('BIRDEYE_API_KEY','')
HELIUS_WEBHOOK_SECRET=os.getenv('HELIUS_WEBHOOK_SECRET','')
DATABASE_URL=os.getenv('DATABASE_URL','sqlite:///smart_money.db')
PAPER_STARTING_CASH=float(os.getenv('PAPER_STARTING_CASH','500'))
MIN_LIQUIDITY_USD=float(os.getenv('MIN_LIQUIDITY_USD','100000'))
SIGNAL_SCORE_THRESHOLD=int(os.getenv('SIGNAL_SCORE_THRESHOLD','75'))
PAPER_POSITION_USD=float(os.getenv('PAPER_POSITION_USD','25'))
PAPER_MAX_OPEN=int(os.getenv('PAPER_MAX_OPEN','5'))
PAPER_TAKE_PROFIT_PCT=float(os.getenv('PAPER_TAKE_PROFIT_PCT','20'))
PAPER_STOP_LOSS_PCT=float(os.getenv('PAPER_STOP_LOSS_PCT','10'))

if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL='postgresql+psycopg://'+DATABASE_URL[len('postgres://'):]
elif DATABASE_URL.startswith('postgresql://') and '+psycopg' not in DATABASE_URL:
    DATABASE_URL='postgresql+psycopg://'+DATABASE_URL[len('postgresql://'):]

engine=create_engine(DATABASE_URL,pool_pre_ping=True,future=True)
app=FastAPI(title='Solana Smart-Money Scanner V2.4')

def sqlite(): return DATABASE_URL.startswith('sqlite')

def q(sql,p=None):
    with engine.begin() as c:
        r=c.execute(text(sql),p or {})
        return [dict(x._mapping) for x in r]

def one(sql,p=None):
    x=q(sql,p); return x[0] if x else None

def run(sql,p=None):
    with engine.begin() as c: c.execute(text(sql),p or {})

SCHEMA=[
'''CREATE TABLE IF NOT EXISTS wallets(address TEXT PRIMARY KEY,label TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,win_rate_30d REAL,win_rate_90d REAL,realized_pnl_30d REAL,realized_pnl_90d REAL,trades_30d INTEGER,trades_90d INTEGER,avg_profit_30d REAL,avg_profit_90d REAL,realized_pnl_30d_wac REAL,realized_pnl_90d_wac REAL,avg_profit_30d_wac REAL,avg_profit_90d_wac REAL,wallet_score INTEGER,stats_status TEXT,stats_updated_at TIMESTAMP,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS signals(id {ID},token_mint TEXT NOT NULL,wallet_address TEXT,price_usd REAL,liquidity_usd REAL,wallet_score INTEGER,confirmation_count INTEGER,score INTEGER NOT NULL,level TEXT NOT NULL,reason TEXT NOT NULL,signature TEXT,source TEXT DEFAULT 'REAL',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS paper_positions(id {ID},token_mint TEXT NOT NULL,signal_id INTEGER,usd_amount REAL NOT NULL,entry_price REAL NOT NULL,current_price REAL NOT NULL,quantity REAL NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',exit_reason TEXT,opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,closed_at TIMESTAMP,exit_price REAL,realized_pnl REAL)'''
]

@app.on_event('startup')
def startup():
    ident='INTEGER PRIMARY KEY AUTOINCREMENT' if sqlite() else 'INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY'
    with engine.begin() as c:
        for s in SCHEMA:
            c.execute(text(s.format(ID=ident)))

    # V2.4 migration for databases created by earlier versions.
    for col in [
        'avg_profit_30d REAL',
        'avg_profit_90d REAL',
        'realized_pnl_30d_wac REAL',
        'realized_pnl_90d_wac REAL',
        'avg_profit_30d_wac REAL',
        'avg_profit_90d_wac REAL',
    ]:
        try:
            with engine.begin() as c:
                c.execute(text('ALTER TABLE wallets ADD COLUMN '+col))
        except Exception:
            pass

# Birdeye Standard/free tier is rate-limited. Serialize all Birdeye calls
# and keep them a little over one second apart so normal wallet analysis
# does not trigger HTTP 429.
_bird_lock=threading.Lock()
_bird_last_request=0.0
BIRDEYE_MIN_INTERVAL=float(os.getenv('BIRDEYE_MIN_INTERVAL','1.15'))

def bheaders():
    if not BIRDEYE_API_KEY: raise RuntimeError('BIRDEYE_API_KEY is not configured')
    return {'X-API-KEY':BIRDEYE_API_KEY,'x-chain':'solana','accept':'application/json'}

def bird_get(url,params,timeout=10):
    global _bird_last_request
    last_response=None

    # Retry 429s automatically. This lets the free 1-rps tier work
    # without requiring the user to manually wait between requests.
    for attempt in range(4):
        with _bird_lock:
            elapsed=time.monotonic()-_bird_last_request
            wait=max(0.0,BIRDEYE_MIN_INTERVAL-elapsed)
            if wait:
                time.sleep(wait)

            try:
                r=requests.get(url,params=params,headers=bheaders(),timeout=timeout)
            except requests.Timeout:
                raise RuntimeError('Birdeye timed out. Please try again.')
            except requests.RequestException as e:
                raise RuntimeError(f'Could not reach Birdeye: {e}')
            finally:
                _bird_last_request=time.monotonic()

        last_response=r
        if r.status_code != 429:
            break

        # Respect Retry-After when Birdeye supplies it; otherwise back off.
        try:
            retry_after=float(r.headers.get('Retry-After') or 0)
        except Exception:
            retry_after=0
        time.sleep(max(retry_after,1.5*(attempt+1)))

    r=last_response
    if r is None:
        raise RuntimeError('Birdeye request failed before receiving a response.')

    if not r.ok:
        try:
            body=r.json()
            detail=body.get('message') or body.get('error') or str(body)
        except Exception:
            detail=r.text
        detail=str(detail).replace('\n',' ')[:220]
        if r.status_code == 429:
            detail='Rate limit reached after automatic retries. Wait about a minute and try again.'
        raise RuntimeError(f'Birdeye HTTP {r.status_code}: {detail}')

    try:
        payload=r.json() or {}
    except Exception:
        raise RuntimeError('Birdeye returned an unreadable response.')

    if payload.get('success') is False:
        raise RuntimeError('Birdeye: '+str(payload.get('message') or payload.get('error') or 'request failed'))
    return payload

def _rate(v):
    x=float(v or 0)
    # Be defensive if a provider returns a percentage instead of a 0-1 ratio.
    if 1 < x <= 100: x/=100
    return max(0,min(1,x))

def wallet_pnl(addr,duration,method='net_cash'):
    payload=bird_get(
        'https://public-api.birdeye.so/wallet/v2/pnl/summary',
        {'wallet':addr,'duration':duration,'position_scope':'duration_only','pnl_method':method},
        timeout=12
    )
    d=payload.get('data') or {}
    if isinstance(d.get('summary'),dict):
        d=d['summary']
    counts=d.get('counts') or {}
    pnl=d.get('pnl') or {}
    if not counts and not pnl:
        raise RuntimeError(f'Birdeye returned no {duration} PnL summary for this wallet.')

    trades=int(float(counts.get('total_trade') or counts.get('totalTrade') or 0))
    realized=float(
        pnl.get('realized_profit_usd')
        or pnl.get('realizedProfitUsd')
        or pnl.get('realized_usd')
        or 0
    )
    avg=pnl.get('avg_profit_per_trade_usd')
    if avg is None:
        avg=pnl.get('avgProfitPerTradeUsd')
    if avg is None:
        avg=realized/trades if trades else 0

    return {
        'win_rate':_rate(counts.get('win_rate') or counts.get('winRate')),
        'trades':trades,
        'realized':realized,
        'avg_profit':float(avg or 0),
        'method':method,
    }

def market(mint):
    payload=bird_get('https://public-api.birdeye.so/defi/price',{'address':mint,'include_liquidity':'true'},timeout=8)
    d=payload.get('data') or {}; liq=d.get('liquidity')
    if isinstance(liq,dict): liq=liq.get('usd') or liq.get('value')
    return {'price':float(d['value']) if d.get('value') is not None else None,'liquidity':float(liq) if liq is not None else None}

def calc_wallet_score(a,b):
    # V2.4: less emphasis on raw win rate; more on realized profitability and expectancy.
    wr30=max(0,min(1,float(a.get('win_rate') or 0)))
    wr90=max(0,min(1,float(b.get('win_rate') or 0)))
    p30=float(a.get('realized') or 0)
    p90=float(b.get('realized') or 0)
    av30=float(a.get('avg_profit') or 0)
    av90=float(b.get('avg_profit') or 0)
    t30=int(a.get('trades') or 0)
    t90=int(b.get('trades') or 0)

    score=10*wr30 + 8*wr90

    if p30>0: score+=min(9,2.25*math.log10(1+p30))
    if p90>0: score+=min(9,2.0*math.log10(1+p90))
    if av30>0: score+=min(8,2.5*math.log10(1+av30))
    if av90>0: score+=min(6,2.0*math.log10(1+av90))

    score+=min(3,t30/20)
    score+=min(3,t90/40)

    if p30>0 and p90>0 and av30>0 and av90>0:
        score+=4
    elif p30<0 and p90<0:
        score-=4

    return int(max(0,min(60,round(score))))

def analyze_wallet_stats(addr):
    n30=wallet_pnl(addr,'30d','net_cash')
    n90=wallet_pnl(addr,'90d','net_cash')
    w30=wallet_pnl(addr,'30d','wac')
    w90=wallet_pnl(addr,'90d','wac')
    return {
        '30d':n30,'90d':n90,
        '30d_wac':w30,'90d_wac':w90,
        'wallet_score':calc_wallet_score(n30,n90)
    }

def refresh_wallet(addr):
    z=analyze_wallet_stats(addr)
    a=z['30d']; b=z['90d']; wa=z['30d_wac']; wb=z['90d_wac']; s=z['wallet_score']
    run("""UPDATE wallets SET
        win_rate_30d=:a,win_rate_90d=:b,
        realized_pnl_30d=:c,realized_pnl_90d=:d,
        trades_30d=:e,trades_90d=:f,
        avg_profit_30d=:g,avg_profit_90d=:h,
        realized_pnl_30d_wac=:i,realized_pnl_90d_wac=:j,
        avg_profit_30d_wac=:k,avg_profit_90d_wac=:l,
        wallet_score=:s,stats_status='OK',stats_updated_at=CURRENT_TIMESTAMP
        WHERE address=:w""",
        {'a':a['win_rate'],'b':b['win_rate'],'c':a['realized'],'d':b['realized'],
         'e':a['trades'],'f':b['trades'],'g':a['avg_profit'],'h':b['avg_profit'],
         'i':wa['realized'],'j':wb['realized'],'k':wa['avg_profit'],'l':wb['avg_profit'],
         's':s,'w':addr})
    return z

STABLE={'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v','Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB','So11111111111111111111111111111111111111112'}
def mentions(x,w):
    if isinstance(x,dict): return any(mentions(v,w) for v in x.values())
    if isinstance(x,list): return any(mentions(v,w) for v in x)
    return x==w

def bought_mint(ev,w):
    sw=((ev.get('events') or {}).get('swap') or {})
    outs=sw.get('tokenOutputs') or []
    for o in outs:
        m=o.get('mint'); u=o.get('userAccount') or o.get('toUserAccount')
        if m and m not in STABLE and u==w: return m
    if str(ev.get('type') or '').upper()=='SWAP':
        for o in outs:
            m=o.get('mint')
            if m and m not in STABLE: return m
    for t in ev.get('tokenTransfers') or []:
        m=t.get('mint') or t.get('tokenMint'); u=t.get('toUserAccount') or t.get('toUser')
        if m and m not in STABLE and u==w: return m
    return None

def signal_score(ws,liq,conf):
    s=float(ws or 0); liq=float(liq or 0)
    s+=25 if liq>=1e6 else 22 if liq>=5e5 else 18 if liq>=2.5e5 else 13 if liq>=MIN_LIQUIDITY_USD else 3 if liq>0 else 0
    s+=min(15,max(0,conf-1)*5)
    if 0<liq<MIN_LIQUIDITY_USD: s-=10
    s=int(max(0,min(100,round(s))))
    return s,'HIGH' if s>=80 else 'MEDIUM' if s>=65 else 'LOW'

def open_paper(signal_id,mint,price,score):
    if score<SIGNAL_SCORE_THRESHOLD or not price: return
    if one("SELECT COUNT(*) n FROM paper_positions WHERE status='OPEN'")['n']>=PAPER_MAX_OPEN: return
    if one("SELECT COUNT(*) n FROM paper_positions WHERE status='OPEN' AND token_mint=:m",{'m':mint})['n']: return
    realized=float(one("SELECT COALESCE(SUM(realized_pnl),0) x FROM paper_positions WHERE status='CLOSED'")['x'] or 0)
    deployed=float(one("SELECT COALESCE(SUM(usd_amount),0) x FROM paper_positions WHERE status='OPEN'")['x'] or 0)
    amount=min(PAPER_POSITION_USD,max(0,PAPER_STARTING_CASH+realized-deployed))
    if amount<1:return
    run('''INSERT INTO paper_positions(token_mint,signal_id,usd_amount,entry_price,current_price,quantity,status) VALUES(:m,:sid,:u,:p,:p,:q,'OPEN')''',{'m':mint,'sid':signal_id,'u':amount,'p':price,'q':amount/price})

def refresh_paper():
    n=closed=0
    for p in q("SELECT * FROM paper_positions WHERE status='OPEN'"):
        try:
            price=market(p['token_mint']).get('price')
            if not price: continue
            pct=(price/float(p['entry_price'])-1)*100; reason=None
            if pct>=PAPER_TAKE_PROFIT_PCT: reason='TAKE_PROFIT'
            elif pct<=-PAPER_STOP_LOSS_PCT: reason='STOP_LOSS'
            if reason:
                pnl=float(p['quantity'])*price-float(p['usd_amount'])
                run("UPDATE paper_positions SET current_price=:p,status='CLOSED',exit_price=:p,realized_pnl=:x,exit_reason=:r,closed_at=CURRENT_TIMESTAMP WHERE id=:id",{'p':price,'x':pnl,'r':reason,'id':p['id']}); closed+=1
            else: run('UPDATE paper_positions SET current_price=:p WHERE id=:id',{'p':price,'id':p['id']})
            n+=1
        except Exception: pass
    return {'updated':n,'closed':closed}

def process_events(events):
    try:
        wallets=q("SELECT * FROM wallets WHERE active=1 AND stats_status='OK'")
        for ev in events:
            sig=ev.get('signature')
            for w in wallets:
                if not mentions(ev,w['address']): continue
                mint=bought_mint(ev,w['address'])
                if not mint: continue
                if one('SELECT COUNT(*) n FROM signals WHERE signature=:s AND wallet_address=:w AND token_mint=:m',{'s':sig,'w':w['address'],'m':mint})['n']: continue
                try: md=market(mint)
                except Exception: md={'price':None,'liquidity':None}
                if sqlite(): recent=one("SELECT COUNT(DISTINCT wallet_address) n FROM signals WHERE token_mint=:m AND created_at>=datetime('now','-60 minutes')",{'m':mint})['n']
                else: recent=one("SELECT COUNT(DISTINCT wallet_address) n FROM signals WHERE token_mint=:m AND created_at>=CURRENT_TIMESTAMP-INTERVAL '60 minutes'",{'m':mint})['n']
                conf=max(1,int(recent or 0)+1); score,level=signal_score(w['wallet_score'],md.get('liquidity'),conf)
                reason=f"wallet {w['wallet_score']}/60; 30d win {float(w['win_rate_30d'] or 0):.0%}; 90d win {float(w['win_rate_90d'] or 0):.0%}; liquidity ${float(md.get('liquidity') or 0):,.0f}; confirmation {conf}"
                run('''INSERT INTO signals(token_mint,wallet_address,price_usd,liquidity_usd,wallet_score,confirmation_count,score,level,reason,signature,source) VALUES(:m,:w,:p,:l,:ws,:c,:s,:lv,:r,:sig,'REAL')''',{'m':mint,'w':w['address'],'p':md.get('price'),'l':md.get('liquidity'),'ws':w['wallet_score'],'c':conf,'s':score,'lv':level,'r':reason,'sig':sig})
                sid=one('SELECT id FROM signals WHERE wallet_address=:w AND token_mint=:m ORDER BY id DESC LIMIT 1',{'w':w['address'],'m':mint})['id']
                open_paper(sid,mint,md.get('price'),score)
        refresh_paper()
    except Exception as e: print('webhook worker error',repr(e))

class WalletIn(BaseModel):
    address:str=Field(min_length=32,max_length=60)
    label:str=Field(default='Watched wallet',max_length=80)

@app.get('/health')
def health(): return {'ok':True,'version':'2.3','database':'sqlite' if sqlite() else 'postgres'}

@app.get('/api/wallets')
def wallets(): return q('SELECT * FROM wallets WHERE active=1 ORDER BY created_at DESC')

@app.post('/api/wallets')
def add_wallet(x:WalletIn):
    a=x.address.strip(); l=x.label.strip() or 'Watched wallet'
    try:
        z=analyze_wallet_stats(a)
    except Exception as e:
        raise HTTPException(400,str(e))

    p30=z['30d']; p90=z['90d']; w30=z['30d_wac']; w90=z['90d_wac']; score=z['wallet_score']
    params={
        'a':a,'l':l,'wr30':p30['win_rate'],'wr90':p90['win_rate'],
        'p30':p30['realized'],'p90':p90['realized'],
        't30':p30['trades'],'t90':p90['trades'],
        'a30':p30['avg_profit'],'a90':p90['avg_profit'],
        'wp30':w30['realized'],'wp90':w90['realized'],
        'wa30':w30['avg_profit'],'wa90':w90['avg_profit'],
        'score':score
    }

    if sqlite():
        run("""INSERT OR REPLACE INTO wallets(
            address,label,active,win_rate_30d,win_rate_90d,
            realized_pnl_30d,realized_pnl_90d,trades_30d,trades_90d,
            avg_profit_30d,avg_profit_90d,
            realized_pnl_30d_wac,realized_pnl_90d_wac,
            avg_profit_30d_wac,avg_profit_90d_wac,
            wallet_score,stats_status,stats_updated_at
        ) VALUES(
            :a,:l,1,:wr30,:wr90,:p30,:p90,:t30,:t90,
            :a30,:a90,:wp30,:wp90,:wa30,:wa90,:score,'OK',CURRENT_TIMESTAMP
        )""",params)
    else:
        run("""INSERT INTO wallets(
            address,label,active,win_rate_30d,win_rate_90d,
            realized_pnl_30d,realized_pnl_90d,trades_30d,trades_90d,
            avg_profit_30d,avg_profit_90d,
            realized_pnl_30d_wac,realized_pnl_90d_wac,
            avg_profit_30d_wac,avg_profit_90d_wac,
            wallet_score,stats_status,stats_updated_at
        ) VALUES(
            :a,:l,1,:wr30,:wr90,:p30,:p90,:t30,:t90,
            :a30,:a90,:wp30,:wp90,:wa30,:wa90,:score,'OK',CURRENT_TIMESTAMP
        )
        ON CONFLICT(address) DO UPDATE SET
            label=EXCLUDED.label,active=1,
            win_rate_30d=EXCLUDED.win_rate_30d,win_rate_90d=EXCLUDED.win_rate_90d,
            realized_pnl_30d=EXCLUDED.realized_pnl_30d,realized_pnl_90d=EXCLUDED.realized_pnl_90d,
            trades_30d=EXCLUDED.trades_30d,trades_90d=EXCLUDED.trades_90d,
            avg_profit_30d=EXCLUDED.avg_profit_30d,avg_profit_90d=EXCLUDED.avg_profit_90d,
            realized_pnl_30d_wac=EXCLUDED.realized_pnl_30d_wac,
            realized_pnl_90d_wac=EXCLUDED.realized_pnl_90d_wac,
            avg_profit_30d_wac=EXCLUDED.avg_profit_30d_wac,
            avg_profit_90d_wac=EXCLUDED.avg_profit_90d_wac,
            wallet_score=EXCLUDED.wallet_score,stats_status='OK',
            stats_updated_at=CURRENT_TIMESTAMP
        """,params)

    return {'ok':True,**z}

@app.post('/api/wallets/{address}/refresh')
def rw(address:str):
    try:
        stats=refresh_wallet(address)
        return {'ok':True,'stats':stats}
    except Exception as e:
        run('UPDATE wallets SET stats_status=:s WHERE address=:a',{'s':'ERROR: '+str(e)[:160],'a':address})
        raise HTTPException(400,str(e))

@app.get('/api/birdeye/test')
def birdeye_test():
    try:
        m=market('So11111111111111111111111111111111111111112')
        return {'ok':True,'message':'Birdeye API connected','sol_price':m.get('price')}
    except Exception as e:
        raise HTTPException(400,str(e))
@app.delete('/api/wallets/{address}')
def dw(address:str): run('UPDATE wallets SET active=0 WHERE address=:a',{'a':address}); return {'ok':True}

@app.post('/webhooks/helius')
async def helius(req:Request,bg:BackgroundTasks):
    if HELIUS_WEBHOOK_SECRET and req.headers.get('authorization','') != HELIUS_WEBHOOK_SECRET:
        raise HTTPException(401,'Invalid webhook authorization')
    p=await req.json(); events=p if isinstance(p,list) else [p]
    bg.add_task(process_events,events)
    return {'ok':True,'received':len(events)}

@app.post('/api/paper/refresh')
def rp(): return {'ok':True,**refresh_paper()}

@app.get('/api/paper')
def psum():
    p=q('SELECT * FROM paper_positions ORDER BY id DESC'); closed=[x for x in p if x['status']=='CLOSED']
    realized=sum(float(x.get('realized_pnl') or 0) for x in closed); unreal=sum(float(x['quantity'])*float(x['current_price'])-float(x['usd_amount']) for x in p if x['status']=='OPEN')
    wins=sum(1 for x in closed if float(x.get('realized_pnl') or 0)>0)
    return {'equity':round(PAPER_STARTING_CASH+realized+unreal,2),'closed':len(closed),'win_rate':wins/len(closed) if closed else None,'positions':p}

@app.get('/api/dashboard')
def dash():
    return {'wallets':one('SELECT COUNT(*) n FROM wallets WHERE active=1')['n'],'signals':one("SELECT COUNT(*) n FROM signals WHERE source='REAL'")['n'],'latest':q('SELECT * FROM signals ORDER BY id DESC LIMIT 30'),'database':'SQLite (temporary)' if sqlite() else 'Postgres (persistent)'}

HTML='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Scanner V2</title><style>
:root{color-scheme:dark}body{margin:0;background:#0b0d10;color:#f4f4f5;font-family:system-ui}.w{max-width:1050px;margin:auto;padding:18px}.muted{color:#9ca3af}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}.card{background:#14171c;border:1px solid #292e36;border-radius:14px;padding:14px}.m{font-size:28px;font-weight:800}.tabs{display:flex;gap:7px;margin:15px 0}button{border:0;border-radius:9px;padding:9px 11px;font-weight:700}button:disabled{opacity:.55}.status{min-height:24px;margin-top:10px;font-size:13px}.smallbtn{padding:6px 8px;font-size:11px;margin:2px}.panel{display:none}.panel.on{display:block}input{width:100%;box-sizing:border-box;padding:11px;margin:5px 0;border-radius:9px;border:1px solid #343a45;background:#0d1014;color:white}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:9px 6px;border-bottom:1px solid #292e36}.pill{padding:3px 7px;border-radius:999px;font-weight:800;font-size:11px}.HIGH{background:#123a25;color:#8ef0b1}.MEDIUM{background:#3c3214;color:#f5d977}.LOW{background:#3b1b1b;color:#ffabab}.ok{color:#8ef0b1}.warn{color:#f5d977}.bad{color:#ffabab}@media(max-width:720px){.grid{grid-template-columns:repeat(2,1fr)}.hide{display:none}}
</style></head><body><div class="w"><h1>Solana Smart-Money Scanner V2.4</h1><div class="muted">Real wallet scoring + Helius feed + $500 paper account. No live trading.</div><div id="db" style="margin-top:8px"></div><div class="grid"><div class="card">Wallets<div class="m" id="wc">—</div></div><div class="card">Signals<div class="m" id="sc">—</div></div><div class="card">Closed trades<div class="m" id="cc">—</div></div><div class="card">Paper equity<div class="m" id="eq">—</div></div></div><div class="tabs"><button onclick="tab('wa')">Wallets</button><button onclick="tab('si')">Signals</button><button onclick="tab('pa')">Paper Trades</button></div>
<div id="wa" class="panel on"><div class="card"><h2>Add real wallet</h2><div class="muted" style="font-size:13px;margin-bottom:8px">V2.4 checks profitability + expectancy, not just win rate. Analysis may take ~5–10 seconds.</div><input id="addr" placeholder="Public Solana wallet address"><input id="label" placeholder="Label (optional)"><button id="addBtn" onclick="add()">Analyze + Add</button> <button id="birdBtn" onclick="testBird()">Test Birdeye</button><div id="feedback" class="status"></div></div><h2>Watchlist</h2><div class="card" style="overflow:auto"><table><thead><tr><th>Wallet</th><th>Score</th><th>30d</th><th>90d</th><th class="hide">30d P&L</th><th class="hide">90d P&L</th><th class="hide">Avg/trade</th><th class="hide">WAC 90d</th><th></th></tr></thead><tbody id="wr"></tbody></table></div></div>
<div id="si" class="panel"><h2>Signals</h2><div class="card" style="overflow:auto"><table><thead><tr><th>Level</th><th>Score</th><th>Token</th><th>Why</th></tr></thead><tbody id="sr"></tbody></table></div></div>
<div id="pa" class="panel"><h2>Paper Trades</h2><div class="muted">$25 max · +20% take profit · -10% stop · max 5 open</div><button onclick="refreshP()" style="margin:10px 0">Refresh prices</button><div class="card" style="overflow:auto"><table><thead><tr><th>Status</th><th>Token</th><th>Entry</th><th>Current/Exit</th><th>P&L</th></tr></thead><tbody id="pr"></tbody></table></div></div></div><script>
function tab(x){document.querySelectorAll('.panel').forEach(e=>e.classList.remove('on'));document.getElementById(x).classList.add('on')}function sh(s){return !s?'—':s.length>14?s.slice(0,6)+'…'+s.slice(-5):s}function money(v,d=2){return v==null?'—':'$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d})}function pct(v){return v==null?'—':(100*Number(v)).toFixed(0)+'%'}
const feedbackEl=document.getElementById('feedback');
function msg(html){feedbackEl.innerHTML=html}
async function timedFetch(url,options={},ms=15000){
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),ms);
 try{return await fetch(url,{...options,signal:controller.signal})}
 catch(e){if(e.name==='AbortError')throw new Error('Request timed out after '+Math.round(ms/1000)+' seconds.');throw e}
 finally{clearTimeout(timer)}
}
async function add(){
 const a=document.getElementById('addr').value.trim();
 const lbl=document.getElementById('label').value.trim()||'Watched wallet';
 const btn=document.getElementById('addBtn');
 if(!a){msg('<span class="bad">Paste a Solana wallet address first.</span>');return}
 btn.disabled=true;btn.textContent='Analyzing…';
 msg('<span class="warn">Checking Birdeye 30d + 90d stats…</span>');
 try{
  let r=await timedFetch('/api/wallets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address:a,label:lbl})},30000);
  let d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Could not analyze wallet');
  msg('<span class="ok">Analyzed + added ✓ Score '+d.wallet_score+'/60</span>');
  document.getElementById('addr').value='';
  document.getElementById('label').value='';
  await load();
 }catch(e){msg('<span class="bad">'+e.message+'</span>')}
 finally{btn.disabled=false;btn.textContent='Analyze + Add'}
}
async function testBird(){
 const btn=document.getElementById('birdBtn');
 btn.disabled=true;btn.textContent='Testing…';
 msg('<span class="warn">Testing Birdeye connection…</span>');
 try{
  let r=await timedFetch('/api/birdeye/test',{},15000);
  let d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Birdeye test failed');
  msg('<span class="ok">Birdeye connected ✓'+(d.sol_price?' SOL ≈ $'+Number(d.sol_price).toFixed(2):'')+'</span>');
 }catch(e){msg('<span class="bad">'+e.message+'</span>')}
 finally{btn.disabled=false;btn.textContent='Test Birdeye'}
}
async function del(a,b){b.disabled=true;b.textContent='Removing…';try{await fetch('/api/wallets/'+encodeURIComponent(a),{method:'DELETE'});await load()}finally{b.disabled=false;b.textContent='Remove'}}
async function analyzeExisting(a,b){b.disabled=true;b.textContent='Analyzing…';msg('<span class="warn">Re-analyzing '+sh(a)+'…</span>');try{let r=await timedFetch('/api/wallets/'+encodeURIComponent(a)+'/refresh',{method:'POST'},30000);let d=await r.json();if(!r.ok)throw new Error(d.detail||'Analysis failed');msg('<span class="ok">Wallet stats updated ✓</span>');await load()}catch(e){msg('<span class="bad">'+e.message+'</span>');await load()}finally{b.disabled=false;b.textContent='Analyze'}}
async function refreshP(){await fetch('/api/paper/refresh',{method:'POST'});load()}
async function load(){let [d,p,w]=await Promise.all([fetch('/api/dashboard').then(r=>r.json()),fetch('/api/paper').then(r=>r.json()),fetch('/api/wallets').then(r=>r.json())]);wc.textContent=d.wallets;sc.textContent=d.signals;cc.textContent=p.closed;eq.textContent=money(p.equity);db.innerHTML=d.database.includes('temporary')?'<span class="warn">⚠ Temporary database — connect Postgres before real test.</span>':'<span class="ok">● Persistent database connected</span>';wr.innerHTML='';w.forEach(x=>{let st=x.stats_status||'NOT SCORED';let cls=st==='OK'?'ok':st.startsWith('ERROR')?'bad':'warn';wr.innerHTML+=`<tr><td><b>${x.label}</b><div class="muted">${sh(x.address)}</div><div class="${cls}" style="font-size:11px;margin-top:3px">${st}</div></td><td>${x.wallet_score==null?'—':x.wallet_score+'/60'}</td><td>${pct(x.win_rate_30d)}</td><td>${pct(x.win_rate_90d)}</td><td class="hide">${money(x.realized_pnl_30d)}</td><td class="hide">${money(x.realized_pnl_90d)}</td><td class="hide">${money(x.avg_profit_90d)}</td><td class="hide">${money(x.realized_pnl_90d_wac)}</td><td><button class="smallbtn" onclick="analyzeExisting('${x.address}',this)">Analyze</button><button class="smallbtn" onclick="del('${x.address}',this)">Remove</button></td></tr>`});sr.innerHTML='';d.latest.forEach(x=>{sr.innerHTML+=`<tr><td><span class="pill ${x.level}">${x.level}</span></td><td>${x.score}</td><td>${sh(x.token_mint)}</td><td>${x.reason}</td></tr>`});pr.innerHTML='';p.positions.forEach(x=>{let pnl=x.status==='CLOSED'?Number(x.realized_pnl||0):Number(x.quantity)*Number(x.current_price)-Number(x.usd_amount);pr.innerHTML+=`<tr><td>${x.status}</td><td>${sh(x.token_mint)}</td><td>${money(x.entry_price,6)}</td><td>${money(x.status==='CLOSED'?x.exit_price:x.current_price,6)}</td><td class="${pnl>=0?'ok':'bad'}">${money(pnl)}</td></tr>`})}load();setInterval(load,15000)
</script></body></html>'''
@app.get('/',response_class=HTMLResponse)
def home(): return HTML
