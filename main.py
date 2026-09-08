import os, json, math
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
app=FastAPI(title='Solana Smart-Money Scanner V2')

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
'''CREATE TABLE IF NOT EXISTS wallets(address TEXT PRIMARY KEY,label TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,win_rate_30d REAL,win_rate_90d REAL,realized_pnl_30d REAL,realized_pnl_90d REAL,trades_30d INTEGER,trades_90d INTEGER,wallet_score INTEGER,stats_status TEXT,stats_updated_at TIMESTAMP,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS signals(id {ID},token_mint TEXT NOT NULL,wallet_address TEXT,price_usd REAL,liquidity_usd REAL,wallet_score INTEGER,confirmation_count INTEGER,score INTEGER NOT NULL,level TEXT NOT NULL,reason TEXT NOT NULL,signature TEXT,source TEXT DEFAULT 'REAL',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS paper_positions(id {ID},token_mint TEXT NOT NULL,signal_id INTEGER,usd_amount REAL NOT NULL,entry_price REAL NOT NULL,current_price REAL NOT NULL,quantity REAL NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',exit_reason TEXT,opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,closed_at TIMESTAMP,exit_price REAL,realized_pnl REAL)'''
]

@app.on_event('startup')
def startup():
    ident='INTEGER PRIMARY KEY AUTOINCREMENT' if sqlite() else 'INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY'
    with engine.begin() as c:
        for s in SCHEMA: c.execute(text(s.format(ID=ident)))

def bheaders():
    if not BIRDEYE_API_KEY: raise RuntimeError('BIRDEYE_API_KEY is not configured')
    return {'X-API-KEY':BIRDEYE_API_KEY,'x-chain':'solana'}

def wallet_pnl(addr,duration):
    r=requests.get('https://public-api.birdeye.so/wallet/v2/pnl/summary',params={'wallet':addr,'duration':duration,'position_scope':'duration_only','pnl_method':'net_cash'},headers=bheaders(),timeout=12)
    r.raise_for_status(); d=(r.json() or {}).get('data') or {}
    counts=d.get('counts') or {}; pnl=d.get('pnl') or {}
    return {'win_rate':float(counts.get('win_rate') or 0),'trades':int(counts.get('total_trade') or 0),'realized':float(pnl.get('realized_profit_usd') or 0)}

def market(mint):
    r=requests.get('https://public-api.birdeye.so/defi/price',params={'address':mint,'include_liquidity':'true'},headers=bheaders(),timeout=10)
    r.raise_for_status(); d=(r.json() or {}).get('data') or {}; liq=d.get('liquidity')
    if isinstance(liq,dict): liq=liq.get('usd') or liq.get('value')
    return {'price':float(d['value']) if d.get('value') is not None else None,'liquidity':float(liq) if liq is not None else None}

def calc_wallet_score(a,b):
    score=24*max(0,min(1,a['win_rate']))+18*max(0,min(1,b['win_rate']))
    if b['realized']>0: score+=min(10,2.5*math.log10(1+b['realized']))
    score+=min(8,max(a['trades'],b['trades'])/10)
    return int(max(0,min(60,round(score))))

def refresh_wallet(addr):
    a=wallet_pnl(addr,'30d'); b=wallet_pnl(addr,'90d'); s=calc_wallet_score(a,b)
    run('''UPDATE wallets SET win_rate_30d=:a,win_rate_90d=:b,realized_pnl_30d=:c,realized_pnl_90d=:d,trades_30d=:e,trades_90d=:f,wallet_score=:s,stats_status='OK',stats_updated_at=CURRENT_TIMESTAMP WHERE address=:w''',{'a':a['win_rate'],'b':b['win_rate'],'c':a['realized'],'d':b['realized'],'e':a['trades'],'f':b['trades'],'s':s,'w':addr})
    return {'30d':a,'90d':b,'wallet_score':s}

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
def health(): return {'ok':True,'version':'2.0','database':'sqlite' if sqlite() else 'postgres'}

@app.get('/api/wallets')
def wallets(): return q('SELECT * FROM wallets ORDER BY created_at DESC')

@app.post('/api/wallets')
def add_wallet(x:WalletIn):
    a=x.address.strip(); l=x.label.strip()
    if sqlite(): run("INSERT OR REPLACE INTO wallets(address,label,active,stats_status) VALUES(:a,:l,1,'CHECKING')",{'a':a,'l':l})
    else: run("INSERT INTO wallets(address,label,active,stats_status) VALUES(:a,:l,1,'CHECKING') ON CONFLICT(address) DO UPDATE SET label=EXCLUDED.label,active=1,stats_status='CHECKING'",{'a':a,'l':l})
    try:return {'ok':True,'stats':refresh_wallet(a)}
    except Exception as e:
        run('UPDATE wallets SET stats_status=:s WHERE address=:a',{'s':'ERROR: '+str(e)[:100],'a':a}); raise HTTPException(400,str(e))

@app.post('/api/wallets/{address}/refresh')
def rw(address:str): return {'ok':True,'stats':refresh_wallet(address)}
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
:root{color-scheme:dark}body{margin:0;background:#0b0d10;color:#f4f4f5;font-family:system-ui}.w{max-width:1050px;margin:auto;padding:18px}.muted{color:#9ca3af}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}.card{background:#14171c;border:1px solid #292e36;border-radius:14px;padding:14px}.m{font-size:28px;font-weight:800}.tabs{display:flex;gap:7px;margin:15px 0}button{border:0;border-radius:9px;padding:9px 11px;font-weight:700}.panel{display:none}.panel.on{display:block}input{width:100%;box-sizing:border-box;padding:11px;margin:5px 0;border-radius:9px;border:1px solid #343a45;background:#0d1014;color:white}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:9px 6px;border-bottom:1px solid #292e36}.pill{padding:3px 7px;border-radius:999px;font-weight:800;font-size:11px}.HIGH{background:#123a25;color:#8ef0b1}.MEDIUM{background:#3c3214;color:#f5d977}.LOW{background:#3b1b1b;color:#ffabab}.ok{color:#8ef0b1}.warn{color:#f5d977}.bad{color:#ffabab}@media(max-width:720px){.grid{grid-template-columns:repeat(2,1fr)}.hide{display:none}}
</style></head><body><div class="w"><h1>Solana Smart-Money Scanner V2</h1><div class="muted">Real wallet scoring + Helius feed + $500 paper account. No live trading.</div><div id="db" style="margin-top:8px"></div><div class="grid"><div class="card">Wallets<div class="m" id="wc">—</div></div><div class="card">Signals<div class="m" id="sc">—</div></div><div class="card">Closed trades<div class="m" id="cc">—</div></div><div class="card">Paper equity<div class="m" id="eq">—</div></div></div><div class="tabs"><button onclick="tab('wa')">Wallets</button><button onclick="tab('si')">Signals</button><button onclick="tab('pa')">Paper Trades</button></div>
<div id="wa" class="panel on"><div class="card"><h2>Add real wallet</h2><input id="addr" placeholder="Public Solana wallet address"><input id="label" placeholder="Label (optional)"><button onclick="add()">Analyze + Add</button><div id="status"></div></div><h2>Watchlist</h2><div class="card" style="overflow:auto"><table><thead><tr><th>Wallet</th><th>Score</th><th>30d</th><th>90d</th><th class="hide">90d P&L</th><th></th></tr></thead><tbody id="wr"></tbody></table></div></div>
<div id="si" class="panel"><h2>Signals</h2><div class="card" style="overflow:auto"><table><thead><tr><th>Level</th><th>Score</th><th>Token</th><th>Why</th></tr></thead><tbody id="sr"></tbody></table></div></div>
<div id="pa" class="panel"><h2>Paper Trades</h2><div class="muted">$25 max · +20% take profit · -10% stop · max 5 open</div><button onclick="refreshP()" style="margin:10px 0">Refresh prices</button><div class="card" style="overflow:auto"><table><thead><tr><th>Status</th><th>Token</th><th>Entry</th><th>Current/Exit</th><th>P&L</th></tr></thead><tbody id="pr"></tbody></table></div></div></div><script>
function tab(x){document.querySelectorAll('.panel').forEach(e=>e.classList.remove('on'));document.getElementById(x).classList.add('on')}function sh(s){return !s?'—':s.length>14?s.slice(0,6)+'…'+s.slice(-5):s}function money(v,d=2){return v==null?'—':'$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d})}function pct(v){return v==null?'—':(100*Number(v)).toFixed(0)+'%'}
async function add(){status.textContent='Analyzing…';let r=await fetch('/api/wallets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address:addr.value.trim(),label:label.value.trim()||'Watched wallet'})});let d=await r.json();status.innerHTML=r.ok?'<span class="ok">Added ✓</span>':'<span class="bad">'+(d.detail||'Error')+'</span>';if(r.ok){addr.value='';label.value='';load()}}
async function del(a){await fetch('/api/wallets/'+encodeURIComponent(a),{method:'DELETE'});load()}async function refreshP(){await fetch('/api/paper/refresh',{method:'POST'});load()}
async function load(){let [d,p,w]=await Promise.all([fetch('/api/dashboard').then(r=>r.json()),fetch('/api/paper').then(r=>r.json()),fetch('/api/wallets').then(r=>r.json())]);wc.textContent=d.wallets;sc.textContent=d.signals;cc.textContent=p.closed;eq.textContent=money(p.equity);db.innerHTML=d.database.includes('temporary')?'<span class="warn">⚠ Temporary database — connect Postgres before real test.</span>':'<span class="ok">● Persistent database connected</span>';wr.innerHTML='';w.forEach(x=>{wr.innerHTML+=`<tr><td><b>${x.label}</b><div class="muted">${sh(x.address)}</div></td><td>${x.wallet_score==null?'—':x.wallet_score+'/60'}</td><td>${pct(x.win_rate_30d)}</td><td>${pct(x.win_rate_90d)}</td><td class="hide">${money(x.realized_pnl_90d)}</td><td><button onclick="del('${x.address}')">Remove</button></td></tr>`});sr.innerHTML='';d.latest.forEach(x=>{sr.innerHTML+=`<tr><td><span class="pill ${x.level}">${x.level}</span></td><td>${x.score}</td><td>${sh(x.token_mint)}</td><td>${x.reason}</td></tr>`});pr.innerHTML='';p.positions.forEach(x=>{let pnl=x.status==='CLOSED'?Number(x.realized_pnl||0):Number(x.quantity)*Number(x.current_price)-Number(x.usd_amount);pr.innerHTML+=`<tr><td>${x.status}</td><td>${sh(x.token_mint)}</td><td>${money(x.entry_price,6)}</td><td>${money(x.status==='CLOSED'?x.exit_price:x.current_price,6)}</td><td class="${pnl>=0?'ok':'bad'}">${money(pnl)}</td></tr>`})}load();setInterval(load,15000)
</script></body></html>'''
@app.get('/',response_class=HTMLResponse)
def home(): return HTML
