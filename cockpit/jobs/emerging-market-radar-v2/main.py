import json,re,time
from datetime import datetime,timezone,timedelta
FEED='agent.emerging-market-radar'
ARRAYS=['items','recent','markets','news','sources']
FIELDS=['observedAt','expectedIntervalSec','status','message','coverage']+ARRAYS
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
    c=inputs['config']; fixture=inputs.get('fixture'); offline=isinstance(fixture,dict)
    now=datetime.fromisoformat(fixture['now']) if offline else datetime.now(timezone.utc)
    stamp=now.isoformat(); day=now.date(); lower=(day-timedelta(days=30)).isoformat(); upper=day.isoformat()
    started=time.monotonic(); simulated=0; reads=0; publishes=0
    budget=max(10,min(75,float(c.get('scan_budget_seconds',25))))
    focus=re.compile(c['focus_pattern'],re.I); rounds=re.compile(c['round_pattern'],re.I)
    news_pattern=re.compile(c['news_pattern'],re.I); records=[]; news=[]; urls=set(); total=None
    funding={'role':'funding','name':c['fund_label'],'status':'pending','detail':'Funding scan in progress.'}
    queries=c['news_queries'][:2]
    news_sources=[{'role':'news','name':c['news_label']+' / '+q,'status':'pending','detail':'Not checked in this scan yet.'} for q in queries]
    snapshots=[]
    def elapsed(): return simulated if offline else time.monotonic()-started
    def read(op,q):
        nonlocal reads,simulated
        reads+=1
        if offline:
            sequence=fixture.get(op,[]); index=q.get('offset',0)//10 if op=='fund_operation' else queries.index(q['search'])
            response=sequence[index] if index<len(sequence) else {'fail':True}
            simulated+=response.get('seconds',0)
            if response.get('fail'): raise ValueError('Synthetic source failure')
            value=response['value']
        else: value=job.mcp.read(c['tool'],{'operation_id':c[op],'query':q})
        return unwrap(value)
    def publish(phase):
        nonlocal publishes
        items=[]; seen=set()
        for r in records:
            deal=r.get('matching_deal') if isinstance(r.get('matching_deal'),dict) else {}
            key=s(deal.get('deal_id'),100) or s(r.get('salesforce_id'),100); date=s(deal.get('deal_date'),10)
            name=s(r.get('name'),100); tags=[s(x.get('display_name'),100) for x in objects(r.get('markets'),8)]
            if not key or key in seen or not name or not lower<=date<=upper or not rounds.search(s(deal.get('deal_type'))) or not focus.search(' '.join(tags)): continue
            seen.add(key); domain=s(r.get('domain'),180)
            items.append({'company':name,'domain':domain,'sourceId':key,'date':date,'dateBasis':'deal','market':tags[0] if tags else 'Unclassified','description':('Database classifications: '+', '.join(tags))[:220],'source':c['fund_label'],'url':'https://'+domain if re.fullmatch(r'[A-Za-z0-9.-]+',domain) else '', 'amount':'Withheld: currency and units unverified','productStatus':'Product-in-market not independently verified','lastVerifiedAt':stamp,'verificationStatus':'database'})
        items=sorted(items,key=lambda x:(x['date'],x['company']),reverse=True)[:25]
        groups={}
        for i in items: groups.setdefault(i['market'],[]).append(i['company'])
        markets=[{'name':k,'companies':v,'basis':'Database category, not a new analytical cluster'} for k,v in sorted(groups.items(),key=lambda x:(-len(x[1]),x[0]))][:25]
        sources=[dict(funding)]+[dict(x) for x in news_sources]+[{'role':'analysis','name':'Market grouping','status':'ok','detail':'Deterministic database categories; no model dependency.'}]
        usable=funding['status'] in ['ok','partial'] or any(x['status']=='ok' for x in news_sources)
        partial=any(x['status']!='ok' for x in sources)
        status='failed' if not usable else 'partial' if partial else 'ok'
        message='All discovery sources unavailable; previous snapshots remain cached.' if not usable else 'Scan in progress; funding checkpoint saved.' if phase!='final' else 'Some sources were deferred or failed. See source health for exact coverage.' if partial else ''
        data={'observedAt':stamp,'expectedIntervalSec':86400,'status':status,'message':message,'items':items,'recent':[{'sourceId':i['sourceId'],'company':i['company'],'date':i['date']} for i in items if i['date']>=(day-timedelta(days=7)).isoformat()],'markets':markets,'news':news[:10],'sources':sources,'coverage':f'{lower} to {upper} UTC. {len(records)} funding records read (maximum 100; database total {total if total is not None else "unknown"}); {reads} source requests. Up to 25 relevant signals and 10 news results examined. Phase: {phase}. '+c['coverage_note']}
        publishes+=1
        if offline: snapshots.append(data)
        else: job.feed.publish(FEED,data,expected_interval_seconds=86400,schema=SCHEMA)
        return data
    for offset in range(0,100,10):
        if elapsed()>=budget:
            funding.update(status='partial' if records else 'unavailable',detail=f'Time budget reached after {len(records)} records; remaining pages deferred.')
            break
        try:
            q=dict(c['fund_filter']);q.update(funding_date_after=lower,funding_date_before=(day+timedelta(days=1)).isoformat(),limit=10,offset=offset)
            result=read('fund_operation',q)
            if not isinstance(result.get('results'),list): raise ValueError('Missing results')
            page=objects(result['results'],10);records.extend(page)
            n=result.get('total_count');total=n if isinstance(n,int) and not isinstance(n,bool) else total
            funding.update(status='partial',detail=f'{len(records)} records read; further pages pending.',lastSuccessAt=stamp)
        except Exception as e:
            funding.update(status='partial' if records else 'unavailable',detail=f'Funding request failed ({type(e).__name__}); {len(records)} records retained. No retry.')
            break
        if offset==0: publish('first-page')
        if len(result['results'])<10 or (total is not None and offset+10>=total) or offset==90:
            funding.update(status='ok',detail=f'{len(records)} records read; database total {total if total is not None else "unknown"}; cap 100. Database deal dates, not announcements.')
            break
    publish('funding-saved')
    for index,query in enumerate(queries):
        source=news_sources[index]
        if elapsed()>=budget:
            source.update(status='deferred',detail='Optional news query deferred to keep the refresh within its time budget. Funding already saved.')
            continue
        try:
            result=read('news_operation',{'all_companies':True,'search':query,'date_start':lower,'date_end':upper,'limit':5,'offset':0})
            if not isinstance(result.get('items'),list): raise ValueError('Missing items')
            accepted=0
            for n in objects(result['items'],5):
                title=s(n.get('title'),180);summary=s(n.get('summary'),220);url=s(n.get('url'),1000);date=s(n.get('published_at'),10)
                if not lower<=date<=upper or not url.startswith('https://') or url in urls or not focus.search(title+' '+summary) or not news_pattern.search(title+' '+summary):continue
                urls.add(url);accepted+=1
                news.append({'title':title,'summary':summary,'url':url,'publishedAt':s(n.get('published_at'),30),'source':s(n.get('source_name'),80) or s(n.get('source_type'),80) or c['news_label'],'lastVerifiedAt':stamp,'verificationStatus':'aggregated'})
            source.update(status='ok',detail=f'Up to 5 results examined; {accepted} matching links. Publication dates only; originals not independently verified.',lastSuccessAt=stamp)
        except Exception as e:
            source.update(status='unavailable',detail=f'News query failed ({type(e).__name__}); funding remains saved. No retry.')
    result=publish('final')
    summary={'status':result['status'],'rounds':len(result['items']),'news':len(result['news']),'markets':len(result['markets']),'records':len(records),'requests':reads,'checkpoints':publishes,'elapsed_seconds':round(elapsed(),1)}
    return {'summary':summary,'snapshots':snapshots} if offline else summary
