"""Diagnóstico descriptivo de la muestra manual; no garantía ni calibración externa."""
import math
from datetime import datetime

VERSION = 'context-v2'

def assess(prediction, records):
    rows = []
    previous_end = None
    # Separar mercado, horizonte, modelo y metodología. Evitar duplicados y ventanas solapadas.
    for row in sorted(records, key=lambda r:r.get('created_at','')):
        if not row.get('resolved') or row.get('evaluation_method') != 'last_trade_at_or_before_deadline_v1':
            continue
        if any(row.get(k) != prediction.get(k) for k in ('market_id','horizon','model','context_version')):
            continue
        if row.get('context_version') != VERSION:
            continue
        try:
            start, end = datetime.fromisoformat(row['created_at']), datetime.fromisoformat(row['resolves_at'])
            if datetime.fromisoformat(row['saved_at']) >= end:
                continue
            if previous_end is not None and start < previous_end:
                continue
            p = float(row['probability_up'])
            if not math.isfinite(p) or not 0 <= p <= 1 or row['actual_direction'] not in ('up','down','flat'):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        rows.append(row)
        previous_end = end
    n = len(rows)
    hits = sum((r['probability_up'] >= .5) == (r['actual_direction']=='up') for r in rows)
    rate = hits/n if n else None
    interval = None
    if n:
        z=1.96
        center=(rate+z*z/(2*n))/(1+z*z/n)
        half=z*math.sqrt(rate*(1-rate)/n+z*z/(4*n*n))/(1+z*z/n)
        interval=[max(0,center-half),min(1,center+half)]
    bins=[]
    for low,high in [(0,.2),(.2,.4),(.4,.6),(.6,.8),(.8,1.01)]:
        group=[r for r in rows if low <= r['probability_up'] < high]
        if group:
            bins.append({'from':low,'to':min(1,high),'count':len(group),
                'mean_probability_up':sum(r['probability_up'] for r in group)/len(group),
                'observed_up':sum(r['actual_direction']=='up' for r in group)/len(group)})
    reasons=[]
    context=prediction.get('market_context',{})
    if not context or context.get('price_variation_1h') is None or context.get('price_variation_4h') is None:
        reasons.append('Contexto incompleto')
    sample=context.get('recent_trade_sample',{})
    if sample.get('status') != 'sample_only' or sample.get('last_trade_age_seconds',float('inf'))>900:
        reasons.append('Muestra de transacciones ausente o antigua')
    p=prediction.get('probability_up',.5)
    if .4 <= p <= .6: reasons.append('Probabilidad sin dirección clara (40–60 %)')
    magnitude=prediction.get('magnitude_label','')
    if (p >= .5 and magnitude.startswith('Baja')) or (p < .5 and magnitude.startswith('Sube')):
        reasons.append('Dirección y magnitud contradictorias')
    if n < 30: reasons.append(f'Muestra insuficiente: {n}/30 casos comparables no solapados')
    if not reasons: reasons.append('Muestra seleccionada manualmente: fiabilidad general no validada')
    baseline=[r for r in rows if r.get('baseline_up') is not None]
    return {'status':'insufficient_evidence' if n<30 else 'experimental', 'reasons':reasons,
        'count':n,'accuracy':rate,'wilson_95':interval,
        'brier':sum((r['probability_up']-int(r['actual_direction']=='up'))**2 for r in rows)/n if n else None,
        'baseline_count':len(baseline),
        'baseline_accuracy':sum(r['baseline_up']==(r['actual_direction']=='up') for r in baseline)/len(baseline) if baseline else None,
        'calibration':bins,'selection_bias':True,
        'interpretation':'Estadística descriptiva del seguimiento manual; no validación de la próxima predicción.'}
