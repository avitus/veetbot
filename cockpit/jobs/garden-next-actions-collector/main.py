from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

LAWN='https://ipm.ucanr.edu/TOOLS/TURF/RENOVATE/oversdfeir.html'
HYDRANGEA='https://ccmg.ucanr.edu/files/103022.pdf'

def main(inputs,job):
    fixture=inputs.get('fixture')
    now=datetime.fromisoformat(fixture['now'].replace('Z','+00:00')) if fixture else datetime.now(timezone.utc)
    today=now.astimezone(ZoneInfo(inputs['timezone'])).date()
    weather=None; errors=[]
    try:
        if fixture:
            weather=fixture.get('weather')
        else:
            url='https://api.open-meteo.com/v1/forecast?'+urlencode({'latitude':inputs['latitude'],'longitude':inputs['longitude'],'daily':'temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max','temperature_unit':'fahrenheit','precipitation_unit':'inch','timezone':inputs['timezone'],'forecast_days':7})
            r=job.http.request('GET',url)
            if r.get('status')!=200: raise ValueError()
            weather=r.get('json')
        d=weather['daily']; units=weather['daily_units']
        if units.get('temperature_2m_min')!='°F' or units.get('precipitation_sum')!='inch': raise ValueError()
        if d['time'][0]!=today.isoformat() or len(d['time'])<3: raise ValueError()
        fields=['temperature_2m_max','temperature_2m_min','precipitation_sum','precipitation_probability_max']
        for field in fields:
            if len(d[field])!=len(d['time']) or any(not isinstance(v,(int,float)) for v in d[field]): raise ValueError()
    except Exception:
        weather=None; errors.append('Forecast unavailable; inspect soil and check local weather before acting.')
    alerts=[]; rain=False; heavy=False; forecast='Forecast unavailable'
    if weather:
        d=weather['daily']; rain=sum(d['precipitation_sum'][:3])>=0.1 or max(d['precipitation_probability_max'][:3])>=60
        heavy=max(d['precipitation_sum'][:3])>=0.5
        low=min(d['temperature_2m_min'][:3]); high=max(d['temperature_2m_max'][:3]); total=sum(d['precipitation_sum'][:3])
        forecast='Next 3 days: '+str(round(low))+'–'+str(round(high))+' °F · '+format(total,'.2f')+' in rain'
        if low<=36: alerts.append('Frost risk: protect tender growth; hillside microclimates may be colder.')
        if high>=90: alerts.append('Heat: check soil early; avoid feeding heat-stressed plants.')
        if rain: alerts.append('Rain possible: adjust watering only after checking actual soil moisture.')
        if heavy: alerts.append('Heavy rain forecast: postpone fertilizer to reduce runoff risk.')
        if not alerts: alerts.append('No rain, heat or frost flags in the 3-day forecast. Still inspect soil.')
    else: alerts=errors[:]
    tasks=[]
    def task(title,detail,window,status,url):
        tasks.append({'title':title,'detail':detail,'window':window,'status':status,'url':url,'source':'UC ANR guidance + conditional rule'})
    days=None
    if inputs.get('overseeded'):
        try: days=(today-date.fromisoformat(inputs['overseeded'])).days
        except Exception: errors.append('Overseeding date invalid; establishment stage unknown.')
    establishing=days is not None and 0<=days<=42
    if establishing:
        detail='Day '+str(days)+' since reported overseeding. Keep the seedbed moist, not soaked; taper only as seedlings establish. Seed product and growth stage are unconfirmed.'
        if rain: detail+=' Check moisture before watering after rain.'
        task('Check lawn seedbed moisture',detail,'Today · inspect first','Check',LAWN)
        detail='Review seed/fertilizer labels and soil needs before selecting any dose. Do not automatically double up or apply a catch-up dose.'
        if inputs.get('fertilizer_at_sowing') is False: detail='No fertilizer was reported at sowing. '+detail
        if heavy: detail+=' Hold application during forecast heavy rain.'
        task('Review lawn feeding plan',detail,'This week · product-dependent','Confirm first',LAWN)
    else:
        task('Check lawn moisture and growth','Use actual soil moisture and grass growth, not a fixed watering schedule. Current grass type and recent care are not confirmed.','This week','Check',LAWN)
    month=today.month
    plant=inputs['hydrangeas']
    if month>=9 or month<=2:
        task('Hydrangeas: hold feeding and hard pruning',plant+': avoid late-season fertilizer and preserve old-wood flower buds. Reassess feeding in spring; remove only clearly dead or damaged material.','Now → spring review','Wait',HYDRANGEA)
    elif month<=5:
        task('Hydrangeas: review spring feeding',plant+': inspect growth and soil first. Compost or balanced fertilizer only if needed and per label; no automatic dose. Preserve old-wood flower buds.','Spring · conditional','Check',HYDRANGEA)
    elif month<=7:
        task('Hydrangeas: check post-bloom care',plant+': prune only after the plant has finished blooming; unknown reblooming varieties need caution. Mid-summer feeding only if needed; avoid heat-stressed plants.','After flowering · confirm stage','Confirm first',HYDRANGEA)
    else:
        task('Hydrangeas: protect next season’s buds',plant+': avoid hard pruning as next year’s buds form. Finish any justified feeding before autumn; do not force new growth.','Late summer','Wait',HYDRANGEA)
    if not establishing:
        task('Review lawn fertilization needs','Identify current grass type, fertilizer label and last application before setting a rate or date. Do not feed automatically; avoid saturated soil or heavy rain.','Seasonal review · no fixed dose','Confirm first',LAWN)
    task('Inspect plant root-zone moisture','Keep hydrangea soil evenly moist, not waterlogged. Check beneath mulch; forecast rainfall is not measured soil moisture.','This week','Check',HYDRANGEA)
    data={'observedAt':now.isoformat(),'expectedIntervalSec':21600,'location':inputs['location'],'status':'partial' if errors else 'ok','message':' | '.join(errors),'forecast':forecast,'alerts':alerts,'tasks':tasks,'weatherUrl':'https://open-meteo.com/','guidance':'Suggested windows, not recorded completions. UC ANR guidance; Open-Meteo forecast. Other plants not yet inventoried.','references':inputs['references']}
    if fixture: return data
    schema={'type':'object','properties':{'observedAt':{'type':'string'},'expectedIntervalSec':{'type':'number'},'location':{'type':'string'},'status':{'type':'string'},'message':{'type':'string'},'forecast':{'type':'string'},'alerts':{'type':'array','items':{'type':'string'}},'tasks':{'type':'array','items':{'type':'object'}},'weatherUrl':{'type':'string'},'guidance':{'type':'string'},'references':{'type':'array','items':{'type':'string'}}},'required':['observedAt','tasks','forecast','alerts'],'additionalProperties':False}
    job.feed.publish('agent.garden-next-actions',data,expected_interval_seconds=21600,schema=schema)
    return {'tasks':len(tasks),'forecastAvailable':weather is not None}
