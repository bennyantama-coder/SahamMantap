from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime, date, timedelta
import sqlite3, json, csv, io, math, os
from apscheduler.schedulers.background import BackgroundScheduler

BASE = Path(__file__).resolve().parent.parent
DB = BASE / 'data' / 'stockintel.db'
IMPORT_DIR = Path(os.getenv('IDX_IMPORT_DIR', BASE / 'data' / 'imports'))
IMPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title='IDX Stock Intelligence', version='0.1.0')
app.mount('/static', StaticFiles(directory=BASE/'app'/'static'), name='static')


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = conn(); cur = c.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS prices(
      symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL,
      volume REAL, value REAL, frequency REAL, PRIMARY KEY(symbol,d));
    CREATE TABLE IF NOT EXISTS foreign_flow(
      symbol TEXT, d TEXT, foreign_buy REAL, foreign_sell REAL,
      domestic_buy REAL DEFAULT 0, domestic_sell REAL DEFAULT 0,
      PRIMARY KEY(symbol,d));
    CREATE TABLE IF NOT EXISTS broker_flow(
      symbol TEXT, d TEXT, broker TEXT, buy_value REAL, sell_value REAL,
      PRIMARY KEY(symbol,d,broker));
    CREATE TABLE IF NOT EXISTS fundamentals(
      symbol TEXT, period TEXT, revenue REAL, net_profit REAL, equity REAL,
      debt REAL, ocf REAL, eps REAL, shares REAL, source TEXT,
      PRIMARY KEY(symbol,period));
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, event_time TEXT,
      category TEXT, title TEXT, source TEXT, url TEXT, impact_hint TEXT,
      UNIQUE(symbol,event_time,title));
    CREATE TABLE IF NOT EXISTS predictions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, created_at TEXT,
      model_version TEXT, horizon_days INTEGER, bias TEXT, confidence REAL,
      entry_low REAL, entry_high REAL, target1 REAL, target2 REAL, invalidation REAL,
      technical REAL, bandarmology REAL, foreign_score REAL, fundamental REAL,
      event_score REAL, market_context REAL, rationale TEXT, locked INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS evaluations(
      prediction_id INTEGER PRIMARY KEY, evaluated_at TEXT, actual_close REAL,
      max_high REAL, min_low REAL, target1_hit INTEGER, target2_hit INTEGER,
      invalidation_hit INTEGER, direction_correct INTEGER, return_pct REAL,
      FOREIGN KEY(prediction_id) REFERENCES predictions(id));
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    ''')
    c.commit(); c.close()


def clamp(x,a=0,b=100): return max(a,min(b,x))

def sma(vals,n):
    return sum(vals[-n:])/n if len(vals)>=n else None

def rsi(vals,n=14):
    if len(vals)<n+1: return None
    ch=[vals[i]-vals[i-1] for i in range(len(vals)-n,len(vals))]
    gains=sum(max(x,0) for x in ch)/n; losses=sum(max(-x,0) for x in ch)/n
    if losses==0: return 100.0
    rs=gains/losses
    return 100-(100/(1+rs))

def latest_rows(table, symbol, days=120):
    c=conn(); rows=c.execute(f"SELECT * FROM {table} WHERE symbol=? ORDER BY d ASC",(symbol,)).fetchall(); c.close()
    return [dict(r) for r in rows[-days:]]

def technical_score(symbol):
    rows=latest_rows('prices',symbol,120)
    if len(rows)<20: return 50, {'note':'data harga belum cukup'}
    closes=[r['close'] for r in rows]; vols=[r['volume'] or 0 for r in rows]
    px=closes[-1]; s20=sma(closes,20); s50=sma(closes,50) or s20; rr=rsi(closes) or 50
    v20=sma(vols,20) or max(vols[-1],1); vr=(vols[-1]/v20) if v20 else 1
    score=50
    score += 12 if px>s20 else -12
    score += 10 if px>s50 else -10
    score += 8 if 52<=rr<=70 else (-7 if rr>78 else 3 if rr<35 else 0)
    score += min(10,max(-10,(vr-1)*10))
    return round(clamp(score),1), {'close':px,'sma20':s20,'sma50':s50,'rsi14':rr,'volume_ratio20':vr}

def foreign_score(symbol):
    rows=latest_rows('foreign_flow',symbol,60)
    if not rows: return 50, {'net_1d':0,'net_5d':0,'net_20d':0,'net_60d':0}
    net=[(r['foreign_buy'] or 0)-(r['foreign_sell'] or 0) for r in rows]
    sums={n:sum(net[-n:]) for n in [1,5,20,60]}
    base=sum(abs(x) for x in net[-20:])/max(1,min(20,len(net)))
    z=(sums[5]/(base*5)) if base else 0
    score=clamp(50+30*max(-1,min(1,z)) + (10 if sums[20]>0 else -10))
    return round(score,1), {f'net_{n}d':sums[n] for n in [1,5,20,60]}

def broker_score(symbol):
    c=conn(); rows=c.execute("SELECT broker,SUM(buy_value-sell_value) net FROM broker_flow WHERE symbol=? AND d >= date('now','-20 day') GROUP BY broker ORDER BY ABS(net) DESC LIMIT 20",(symbol,)).fetchall(); c.close()
    if not rows: return 50, {'top':[]}
    vals=[r['net'] or 0 for r in rows]; pos=sum(v for v in vals if v>0); neg=-sum(v for v in vals if v<0)
    score=50 + 35*((pos-neg)/(pos+neg)) if pos+neg else 50
    return round(clamp(score),1), {'top':[dict(r) for r in rows[:8]],'net_top20':pos-neg}

def fundamental_score(symbol):
    c=conn(); rows=c.execute("SELECT * FROM fundamentals WHERE symbol=? ORDER BY period DESC LIMIT 2",(symbol,)).fetchall(); c.close()
    if not rows: return 50, {'note':'fundamental belum diimpor'}
    r=dict(rows[0]); prev=dict(rows[1]) if len(rows)>1 else None
    roe=(r['net_profit']/r['equity']*100) if r['equity'] else None
    der=(r['debt']/r['equity']) if r['equity'] else None
    growth=((r['net_profit']/prev['net_profit']-1)*100) if prev and prev['net_profit'] else None
    score=50
    if roe is not None: score += 15 if roe>=15 else (7 if roe>=8 else -5)
    if der is not None: score += 10 if der<1 else (-8 if der>2 else 0)
    if growth is not None: score += 15 if growth>15 else (7 if growth>0 else -12)
    if (r['ocf'] or 0)>0: score+=10
    return round(clamp(score),1), {'roe':roe,'der':der,'net_profit_growth':growth,'latest':r}

def event_score(symbol):
    c=conn(); rows=c.execute("SELECT * FROM events WHERE symbol=? AND event_time >= datetime('now','-183 day') ORDER BY event_time DESC LIMIT 50",(symbol,)).fetchall(); c.close()
    if not rows: return 50, {'events':[]}
    mapv={'positive':8,'negative':-8,'neutral':0,'unknown':0}
    weighted=0
    for i,r in enumerate(rows[:12]): weighted += mapv.get((r['impact_hint'] or 'unknown').lower(),0)*(0.9**i)
    return round(clamp(50+weighted),1), {'events':[dict(r) for r in rows[:12]]}

def score_symbol(symbol):
    ts,td=technical_score(symbol); bs,bd=broker_score(symbol); fs,fd=foreign_score(symbol); funs,fund=fundamental_score(symbol); es,ed=event_score(symbol)
    market=50
    composite=.28*ts+.22*bs+.18*fs+.15*funs+.12*es+.05*market
    bias='BULLISH' if composite>=58 else ('BEARISH' if composite<=42 else 'NEUTRAL')
    return {'symbol':symbol,'score':round(composite,1),'bias':bias,'technical':ts,'bandarmology':bs,'foreign':fs,'fundamental':funs,'event':es,'market':market,'details':{'technical':td,'broker':bd,'foreign':fd,'fundamental':fund,'event':ed}}

def symbols():
    c=conn(); rows=c.execute("SELECT DISTINCT symbol FROM prices ORDER BY symbol").fetchall(); c.close(); return [r['symbol'] for r in rows]

def make_prediction(symbol):
    s=score_symbol(symbol)
    c=conn(); p=c.execute("SELECT close FROM prices WHERE symbol=? ORDER BY d DESC LIMIT 1",(symbol,)).fetchone(); c.close()
    if not p: raise ValueError('Belum ada harga')
    px=p['close']; confidence=min(92,max(51,50+abs(s['score']-50)*1.4))
    if s['bias']=='BULLISH':
        entry=(px*.985,px*1.005); t1=px*1.035; t2=px*1.07; inv=px*.965
    elif s['bias']=='BEARISH':
        entry=(px*.995,px*1.015); t1=px*.965; t2=px*.93; inv=px*1.035
    else:
        entry=(px*.985,px*1.015); t1=px*1.02; t2=px*1.04; inv=px*.96
    rationale=json.dumps(s['details'],ensure_ascii=False)
    c=conn(); cur=c.cursor(); cur.execute('''INSERT INTO predictions(symbol,created_at,model_version,horizon_days,bias,confidence,entry_low,entry_high,target1,target2,invalidation,technical,bandarmology,foreign_score,fundamental,event_score,market_context,rationale,locked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)''',
      (symbol,datetime.now().isoformat(timespec='seconds'),'v0.1-rulebased',5,s['bias'],confidence,*entry,t1,t2,inv,s['technical'],s['bandarmology'],s['foreign'],s['fundamental'],s['event'],s['market'],rationale))
    pid=cur.lastrowid; c.commit(); c.close(); return pid

def evaluate_predictions():
    c=conn(); preds=c.execute("SELECT * FROM predictions WHERE id NOT IN (SELECT prediction_id FROM evaluations)").fetchall()
    for p in preds:
        created=date.fromisoformat(p['created_at'][:10]); end=created+timedelta(days=max(7,p['horizon_days']+4))
        rows=c.execute("SELECT * FROM prices WHERE symbol=? AND d>? AND d<=? ORDER BY d",(p['symbol'],created.isoformat(),end.isoformat())).fetchall()
        if len(rows)<p['horizon_days']: continue
        rows=rows[:p['horizon_days']]; actual=rows[-1]['close']; maxh=max(r['high'] for r in rows); minl=min(r['low'] for r in rows)
        if p['bias']=='BULLISH':
            t1=maxh>=p['target1']; t2=maxh>=p['target2']; inv=minl<=p['invalidation']; correct=actual>p['entry_high']; ret=(actual/p['entry_high']-1)*100
        elif p['bias']=='BEARISH':
            t1=minl<=p['target1']; t2=minl<=p['target2']; inv=maxh>=p['invalidation']; correct=actual<p['entry_low']; ret=(p['entry_low']/actual-1)*100
        else:
            t1=False;t2=False;inv=False;correct=abs(actual/p['entry_low']-1)<.03;ret=0
        c.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?,?,?)",(p['id'],datetime.now().isoformat(timespec='seconds'),actual,maxh,minl,int(t1),int(t2),int(inv),int(correct),ret))
    c.commit(); c.close()


def import_csv_text(kind, text):
    reader=csv.DictReader(io.StringIO(text)); n=0; c=conn()
    for r in reader:
        try:
            if kind=='prices':
                c.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?,?)",(r['symbol'].upper(),r['date'],float(r['open']),float(r['high']),float(r['low']),float(r['close']),float(r.get('volume',0)),float(r.get('value',0)),float(r.get('frequency',0))))
            elif kind=='foreign':
                c.execute("INSERT OR REPLACE INTO foreign_flow VALUES(?,?,?,?,?,?)",(r['symbol'].upper(),r['date'],float(r.get('foreign_buy',0)),float(r.get('foreign_sell',0)),float(r.get('domestic_buy',0)),float(r.get('domestic_sell',0))))
            elif kind=='brokers':
                c.execute("INSERT OR REPLACE INTO broker_flow VALUES(?,?,?,?,?)",(r['symbol'].upper(),r['date'],r['broker'].upper(),float(r.get('buy_value',0)),float(r.get('sell_value',0))))
            elif kind=='fundamentals':
                c.execute("INSERT OR REPLACE INTO fundamentals VALUES(?,?,?,?,?,?,?,?,?,?)",(r['symbol'].upper(),r['period'],float(r.get('revenue',0)),float(r.get('net_profit',0)),float(r.get('equity',0)),float(r.get('debt',0)),float(r.get('ocf',0)),float(r.get('eps',0)),float(r.get('shares',0)),r.get('source','import')))
            elif kind=='events':
                c.execute("INSERT OR IGNORE INTO events(symbol,event_time,category,title,source,url,impact_hint) VALUES(?,?,?,?,?,?,?)",(r['symbol'].upper(),r['event_time'],r.get('category','other'),r['title'],r.get('source','import'),r.get('url',''),r.get('impact_hint','unknown')))
            n+=1
        except Exception:
            continue
    c.commit(); c.close(); return n

def ingest_drop_folder():
    mapping={'prices.csv':'prices','foreign.csv':'foreign','brokers.csv':'brokers','fundamentals.csv':'fundamentals','events.csv':'events'}
    for fn,kind in mapping.items():
        p=IMPORT_DIR/fn
        if p.exists():
            import_csv_text(kind,p.read_text(encoding='utf-8-sig'))
    evaluate_predictions()

@app.on_event('startup')
def startup():
    init_db(); ingest_drop_folder()
    sch=BackgroundScheduler(timezone='Asia/Jakarta')
    sch.add_job(ingest_drop_folder,'cron',day_of_week='mon-fri',hour=16,minute=20,id='eod_ingest',replace_existing=True)
    sch.start(); app.state.scheduler=sch

@app.get('/')
def root(): return FileResponse(BASE/'app'/'static'/'index.html')

@app.get('/api/status')
def status():
    c=conn(); latest=c.execute("SELECT MAX(d) d FROM prices").fetchone()['d']; pc=c.execute("SELECT COUNT(*) n FROM predictions").fetchone()['n']; ev=c.execute("SELECT COUNT(*) n FROM evaluations").fetchone()['n']; c.close()
    return {'latest_market_date':latest,'symbols':len(symbols()),'predictions':pc,'evaluated':ev,'mode':'LIVE-IMPORT READY','import_dir':str(IMPORT_DIR)}

@app.get('/api/scanner')
def scanner(limit:int=30):
    out=[score_symbol(s) for s in symbols()]
    out.sort(key=lambda x:x['score'],reverse=True); return out[:limit]

@app.get('/api/stock/{symbol}')
def stock(symbol:str):
    symbol=symbol.upper(); s=score_symbol(symbol); p=latest_rows('prices',symbol,190); f=latest_rows('foreign_flow',symbol,190)
    return {'score':s,'prices':p,'foreign':f}

@app.get('/api/predictions')
def predictions():
    c=conn(); rows=c.execute('''SELECT p.*,e.direction_correct,e.return_pct,e.target1_hit,e.target2_hit,e.invalidation_hit FROM predictions p LEFT JOIN evaluations e ON p.id=e.prediction_id ORDER BY p.created_at DESC LIMIT 300''').fetchall(); c.close(); return [dict(r) for r in rows]

@app.post('/api/predict/{symbol}')
def predict(symbol:str):
    try: pid=make_prediction(symbol.upper())
    except ValueError as e: raise HTTPException(400,str(e))
    return {'prediction_id':pid,'locked':True}

@app.post('/api/run-eod')
def run_eod():
    ingest_drop_folder()
    made=[]
    for s in symbols():
        sc=score_symbol(s)
        if sc['score']>=62 or sc['score']<=38:
            c=conn(); exists=c.execute("SELECT 1 FROM predictions WHERE symbol=? AND date(created_at)=date('now')",(s,)).fetchone(); c.close()
            if not exists:
                made.append(make_prediction(s))
    evaluate_predictions(); return {'predictions_created':len(made)}

@app.post('/api/import/{kind}')
async def import_file(kind:str, file:UploadFile=File(...)):
    if kind not in {'prices','foreign','brokers','fundamentals','events'}: raise HTTPException(400,'kind tidak dikenal')
    text=(await file.read()).decode('utf-8-sig'); n=import_csv_text(kind,text); return {'imported':n}

@app.get('/api/performance')
def performance():
    c=conn(); rows=c.execute("SELECT * FROM evaluations").fetchall(); c.close()
    if not rows: return {'n':0,'accuracy':None,'avg_return':None,'profit_factor':None}
    rets=[r['return_pct'] or 0 for r in rows]; wins=[x for x in rets if x>0]; losses=[x for x in rets if x<0]
    return {'n':len(rows),'accuracy':round(100*sum(r['direction_correct'] for r in rows)/len(rows),1),'avg_return':round(sum(rets)/len(rets),2),'profit_factor':round(sum(wins)/abs(sum(losses)),2) if losses else None}
