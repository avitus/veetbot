import React from 'react';
import {Tile,List,Row,StatusPill,Metric,Between,PendingState} from '@veetbot/cockpit-kit';
const t=(v:any,n=500)=>typeof v==='string'?v.slice(0,n):'';
const arr=(v:any)=>Array.isArray(v)?v:[];
export default function Widget({feeds,size}:any){
 const f=feeds['agent.emerging-market-radar'],d=f?.data;
 const items=arr(d?.items),markets=arr(d?.markets),news=arr(d?.news);
 const time=Date.parse(d?.observedAt||'');
 const freshness={observedAt:Number.isFinite(time)?time:null,expectedIntervalSec:86400,error:f?.error?'Refresh failed':d?.message||null};
 const basis=(i:any)=>i.dateBasis==='deal'?'Deal date':i.dateBasis==='announcement'?'Announcement':'Reported date';
 const row=(i:any,k:number,full=false)=><Row key={k} title={`${t(i.company,100)} · ${t(i.market,90)}`} detail={`${basis(i)} ${t(i.date,10)} · ${t(i.source,90)}. ${t(i.description,full?350:180)}${full?` Fit (analysis): ${t(i.why,250)}. Product: ${t(i.productStatus,80)}. Amount: ${t(i.amount,90)}. ${i.dateBasis==='deal'?'Not a verified announcement date.':''}`:''}`}/>;
 return <Tile title="Emerging-market radar" size={size} freshness={freshness} footer="Daily refresh · Categories are analysis; deal dates ≠ announcement dates.">
 {!d?<PendingState/>:<>
 <Between><Metric value={items.length} label="selected rounds · 30 days"/><StatusPill tone="neutral">{markets.length} markets</StatusPill></Between>
 <List ariaLabel="Selected funding signals" empty={!items.length?{title:d.message?'Refresh incomplete':'No qualifying rounds in this scan',detail:t(d.message||d.coverage,250)}:null}>{items.slice(0,3).map((i:any,k:number)=>row(i,k))}</List>
 <details><summary>All rounds, news & coverage</summary>
 <List ariaLabel="All rounds">{items.map((i:any,k:number)=>row(i,k,true))}</List>
 <List ariaLabel="Market clusters">{markets.map((m:any,k:number)=><Row key={k} title={t(m.name,100)} detail={arr(m.companies).map((x:any)=>t(x,100)).join(', ')}/>)}</List>
 <List ariaLabel="Related news" empty={!news.length?{title:'No additional verified news selected'}:null}>{news.map((n:any,k:number)=><Row key={k} title={t(n.title,220)} detail={`${t(n.publishedAt,10)} · ${t(n.source,90)} · ${t(n.summary,350)} ${t(n.url,1000)}`}/>)}</List>
 <List ariaLabel="Provenance">{items.map((i:any,k:number)=><Row key={k} title={t(i.company,100)} detail={`${t(i.source,120)} ${t(i.sourceId,100)} | ${t(i.url,1000)} | ${t(i.evidence,500)}`}/>)}</List>
 <Row title="Coverage" detail={t(d.coverage,1200)}/>
 <Row title="Source checks" detail={arr(d.sources).map((s:any)=>`${t(s.name,100)}: ${t(s.status,40)} — ${t(s.detail,250)}`).join(' · ')}/>
 </details>
 </>}
 </Tile>;
}
