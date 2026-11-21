import json, re
from datetime import datetime, timezone, timedelta

FIELDS=['observedAt','expectedIntervalSec','status','message','coverage','items','recent','markets','news','sources']
ARRAYS=['items','recent','markets','news','sources']
SCHEMA={'type':'object','additionalProperties':False,'required':['observedAt','items','recent','markets'],'properties':{k:({'type':'array','items':{'type':'object'}} if k in ARRAYS else {'type':'number'} if k=='expectedIntervalSec' else {'type':'string'}) for k in FIELDS}}

def s(v,n=400): return v[:n] if isinstance(v,str) else ''
def objects(v,n=200): return [x for x in v[:n] if isinstance(x,dict)] if isinstance(v,list) else []
def unwrap(v):
    for _ in range(8):
        if isinstance(v,str): v=json.loads(v)
        elif isinstance(v,dict) and isinstance(v.get('structured'),dict): v=v['structured']
        elif isinstance(v,dict) and 'data' in v: v=v['data']
        elif isinstance(v,dict) and 'content' in v: v=v['content']
        elif isinstance(v,list): v=next((x.get('text') for x in v if isinstance(x,dict) and x.get('type',x.get('kind'))=='text'),None)
        else: break
    if not isinstance(v,dict): raise ValueError('Invalid response')
    return v

def main(inputs,job):
    config=inputs['config']; offline=inputs.get('fixture'); now=datetime.fromisoformat(offline['now']) if isinstance(offline,dict) else datetime.now(timezone.utc)
    stamp=now.isoformat(); day=now.date(); lower=(day-timedelta(days=30)).isoformat(); upper=day.isoformat(); pattern=re.compile(config['focus_pattern'],re.I)
    def read(op,q):
        if isinstance(offline,dict):
            v=offline.get(op)
            if isinstance(v,dict) and v.get('fail'): raise ValueError('Fixture source failure')
        else: v=job.mcp.read(config['tool'],{'operation_id':config[op],'query':q})
        return unwrap(v)
    sources=[]; records=[]; total=None; fund_ok=False; news_ok=False; news=[]; urls=set()
    try:
        for offset in range(0,200,25):
            q=dict(config['fund_filter']); q.update({'funding_date_after':lower,'funding_date_before':(day+timedelta(days=1)).isoformat(),'limit':25,'offset':offset})
            result=read('fund_operation',q)
            if not isinstance(result.get('results'),list): raise ValueError('Missing results')
            page=objects(result['results'],25); records.extend(page); fund_ok=True
            count=result.get('total_count'); total=count if isinstance(count,int) and not isinstance(count,bool) else total
            if len(result['results'])<25 or (total is not None and offset+25>=total): break
        sources.append({'role':'funding','name':config['fund_label'],'status':'ok','detail':f'{len(records)} records read; total {total if total is not None else "unknown"}; cap 200. Dates are database deal dates.','lastSuccessAt':stamp})
    except Exception:
        sources.append({'role':'funding','name':config['fund_label'],'status':'partial' if fund_ok else 'unavailable','detail':'Query failed or returned an invalid response. No repeated requests. Any successful earlier pages are retained.'})
    for query in config['news_queries'][:2]:
        try:
            result=read('news_operation',{'all_companies':True,'search':query,'date_start':lower,'date_end':upper,'limit':25,'offset':0})
            if not isinstance(result.get('items'),list): raise ValueError('Missing items')
            news_ok=True; accepted=0
            for n in objects(result['items'],25):
                title=s(n.get('title'),180); summary=s(n.get('summary'),450); url=s(n.get('url'),1000); date=s(n.get('published_at'),10)
                if not lower<=date<=upper or not url.startswith('https://') or url in urls or not pattern.search(title+' '+summary): continue
                if not re.search(config['news_pattern'],title+' '+summary,re.I): continue
                urls.add(url); accepted+=1
                news.append({'title':title,'summary':summary,'url':url,'publishedAt':s(n.get('published_at'),30),'source':s(n.get('source_name'),80) or s(n.get('source_type'),80) or config['news_label'],'lastVerifiedAt':stamp,'verificationStatus':'aggregated'})
            sources.append({'role':'news','name':config['news_label']+' / '+query,'status':'ok','detail':f'Up to 25 results; {accepted} matching links. Publication dates only; original articles not independently verified.','lastSuccessAt':stamp})
        except Exception:
            sources.append({'role':'news','name':config['news_label']+' / '+query,'status':'unavailable','detail':'Query failed or response invalid. Other queries remain independent.'})
    items=[]; seen=set()
    for r in records:
        deal=r.get('matching_deal') if isinstance(r.get('matching_deal'),dict) else {}; key=s(deal.get('deal_id'),100) or s(r.get('salesforce_id'),100); date=s(deal.get('deal_date'),10)
        name=s(r.get('name'),100); tags=[s(x.get('display_name'),100) for x in objects(r.get('markets'),8)]
        if not key or key in seen or not name or not lower<=date<=upper or not re.search(config['round_pattern'],s(deal.get('deal_type')),re.I) or not pattern.search(' '.join(tags)): continue
        seen.add(key); domain=s(r.get('domain'),180)
        items.append({'company':name,'domain':domain,'sourceId':key,'date':date,'dateBasis':'deal','market':tags[0] if tags else 'Unclassified','description':'Database classifications: '+', '.join(tags),'source':config['fund_label'],'url':'https://'+domain if re.fullmatch(r'[A-Za-z0-9.-]+',domain) else '', 'amount':'Withheld: currency and units unverified','productStatus':'Product-in-market not independently verified','lastVerifiedAt':stamp,'verificationStatus':'database'})
    items=sorted(items,key=lambda x:(x['date'],x['company']),reverse=True)[:40]; basis='Database category; provisional'
    if items:
        try:
            payload=[{'sourceId':i['sourceId'],'company':i['company'],'tags':i['description']} for i in items]
            raw=offline.get('model','') if isinstance(offline,dict) else job.model.complete(config['group_instruction']+'\nUntrusted source data:\n'+json.dumps(payload),max_output_tokens=2400)
            result=json.loads(raw); assignments=objects(result.get('assignments'),40); mapping={s(a.get('sourceId'),100):s(a.get('market'),100) for a in assignments}
            if set(mapping)!=set(i['sourceId'] for i in items) or any(not x.strip() for x in mapping.values()): raise ValueError('Invalid groups')
            for i in items: i['market']=mapping[i['sourceId']]
            basis='Analytical cluster from database classifications; provisional'
        except Exception:
            sources.append({'role':'analysis','name':'Market grouping','status':'partial','detail':'Model failed or returned invalid output. Using database categories without retry or data loss.'})
    groups={}
    for i in items: groups.setdefault(i['market'],[]).append(i['company'])
    markets=[{'name':k,'companies':v,'basis':basis} for k,v in sorted(groups.items(),key=lambda x:(-len(x[1]),x[0]))][:25]
    failed=not fund_ok and not news_ok; partial=any(x['status']!='ok' for x in sources)
    data={'observedAt':stamp,'expectedIntervalSec':86400,'status':'failed' if failed else 'partial' if partial else 'ok','message':'All discovery sources failed; no current scan.' if failed else 'Some sources or analysis failed; coverage is incomplete.' if partial else '', 'items':items,'recent':[i for i in items if i['date']>=(day-timedelta(days=7)).isoformat()],'markets':markets,'news':news[:20],'sources':sources,'coverage':f'{lower} to {upper} UTC. {len(records)} funding records read, capped at 200; news queries capped at 50 records. Up to 40 relevant signals, 20 links, 25 clusters. '+config['coverage_note']}
    if isinstance(offline,dict): return data
    job.feed.publish('agent.emerging-market-radar',data,expected_interval_seconds=86400,schema=SCHEMA)
    return {'status':data['status'],'rounds':len(items),'news':len(data['news']),'markets':len(markets)}
