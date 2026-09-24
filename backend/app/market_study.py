"""Resumen acotado de hasta 100 trades; no representa una ventana fija ni velas."""
import math
import time
from . import buda

def summarize(entries):
    if len(entries)<2:
        return {'status':'insufficient_data','count':len(entries)}
    rows=sorted([(int(e[0]),buda.number(e[1],nonnegative=True),buda.number(e[2],nonnegative=True)) for e in entries], key=lambda e:e[0])
    if any(r[2]<=0 for r in rows):raise ValueError('Precio inválido')
    total=sum(r[1] for r in rows)
    prices=[r[2] for r in rows]
    returns=[math.log(b/a) for a,b in zip(prices,prices[1:])]
    mean=sum(returns)/len(returns)
    return {'status':'sample_only','count':len(rows),'start_timestamp_ms':rows[0][0],
        'end_timestamp_ms':rows[-1][0],'span_seconds':(rows[-1][0]-rows[0][0])/1000,
        'last_trade_age_seconds':max(0,time.time()-rows[-1][0]/1000),
        'sample_return_pct':(prices[-1]/prices[0]-1)*100,
        'sample_vwap':sum(r[1]*r[2] for r in rows)/total if total else None,
        'sample_log_return_std':math.sqrt(sum((v-mean)**2 for v in returns)/len(returns)),
        'note':'Muestra de hasta 100 trades. Intervalo variable; no volatilidad horaria ni pronóstico validado.'}

async def fetch(market_id):
    try:
        async with buda.get_http_client() as client:
            response=await client.get(f'/markets/{market_id}/trades',params={'limit':100})
            response.raise_for_status()
            return summarize(response.json()['trades']['entries'])
    except Exception:
        return {'status':'unavailable','note':'No se pudo obtener la muestra; no inferir actividad ni tendencia.'}
