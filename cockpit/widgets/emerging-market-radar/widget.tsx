import React from 'react';
import {Tile,List,Row,StatusPill,Metric,Between,PendingState,useNow} from '@veetbot/cockpit-kit';
const t=(v:any,n=500)=>typeof v==='string'?v.slice(0,n):'';
const arr=(v:any)=>Array.isArray(v)?v:[];
export default function Widget({feeds,size}:any){
 const now=useNow(),f=feeds['agent.emerging-market-radar'],d=f?.data;
 const items=arr(d?.items),markets=arr(d?.markets),news=arr(d?.news),sources=arr(d?.sources);
 const failed=!!f?.error||['error','failed'].includes(d?.status);
 const warning=!!d?.message||sources.some((s:any)=>['error','unavailable'].includes(s.status)||s.automationStatus==='unavailable');
 const stamp=Date.parse(d?.observedAt||'');
 const freshness={observedAt:Number.isFinite(stamp)?stamp:null,expectedIntervalSec:86400,error:failed?t(d?.message||'Refresh failed; showing last known data',200):null};
 const cached=(i:any)=>i.verificationStatus==='retained'||t(i.evidence).startsWith('STALE:')||(i.lastVerifiedAt&&now-Date.parse(i.lastVerifiedAt)>86400000);
 const date=(i:any)=>`${i.dateBasis==='deal'?'Deal date':i.dateBasis==='announcement'?'Announcement':'Reported date'} ${t(i.date,10)}`;
 const row=(i:any,k:number,full=false)=><Row key={k} title={`${cached(i)?'Cached · ':''}${t(i.company,100)} · ${t(i.market,90)}`} marker={cached(i)?'stale':'none'} detail={`${date(i)} · ${t(i.source,80)}. ${t(i.description,full?350:140)}${full?` Fit (analysis): ${t(i.why,250)}. Product: ${t(i.productStatus,80)}. Amount: ${t(i.amount,100)}. Funding checked: ${t(i.lastVerifiedAt,25)||'See provenance'}.`:''}`}/>;
 return <Tile title="Emerging-market radar" size={size} freshness={freshness} footer="Daily news scan · Source health below · Deal dates ≠ announcements.">
 {!d?<PendingState/>:<>
 <Between><Metric value={items.length} label="selected rounds · 30 days"/><StatusPill tone={failed?'error':warning?'attention':'neutral'}>{failed?'Refresh failed':warning?'Limited sources':`${markets.length} markets`}</StatusPill></Between>
 {warning&&!failed?<Row title="Source warning" detail={t(d.message||'Some sources are unavailable; see source checks.',190)} marker="attention"/>:null}
 <List ariaLabel="Selected funding signals" empty={!items.length?{title:failed||warning?'No verified rounds available in this scan':'No qualifying rounds in this scan',detail:t(d.coverage,220)}:null}>{items.slice(0,3).map((i:any,k:number)=>row(i,k))}</List>
 <details><summary>All rounds, news & source health</summary>
 <List ariaLabel="Source checks">{sources.map((s:any,k:number)=><Row key={k} title={`${t(s.name,100)} · ${t(s.status,40)}`} detail={`${t(s.detail,450)}${s.automationStatus==='unavailable'?' Automated access unavailable.':''} ${s.lastSuccessAt?`Last successful check: ${t(s.lastSuccessAt,25)}`:''}`}/>)}</List>
 {d.message?<Row title="Refresh detail" detail={t(d.message,1600)}/>:null}
 <List ariaLabel="All rounds">{items.map((i:any,k:number)=>row(i,k,true))}</List>
 <List ariaLabel="Market clusters">{markets.map((m:any,k:number)=><Row key={k} title={t(m.name,100)} detail={arr(m.companies).map((x:any)=>t(x,100)).join(', ')}/>)}</List>
 <List ariaLabel="Related news">{news.map((n:any,k:number)=><Row key={k} title={`${n.verificationStatus==='retained'?'Cached · ':''}${t(n.title,180)}`} detail={`${t(n.publishedAt,10)} · ${t(n.source,100)} · ${t(n.summary,350)} ${t(n.url,1000)}`}/>)}</List>
 <List ariaLabel="Provenance">{items.map((i:any,k:number)=><Row key={k} title={t(i.company,100)} detail={`${t(i.sourceId,200)} | ${t(i.url,1000)} | ${t(i.evidence,700)}`}/>)}</List>
 <Row title="Coverage" detail={t(d.coverage,1800)}/>
 </details></>}
 </Tile>;
}
