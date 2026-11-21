import React,{useState} from 'react';
import {Tile,List,Row,StatusPill,Metric,Between,PendingState,useNow} from '@veetbot/cockpit-kit';
const text=(v:any,n=220)=>typeof v==='string'?v.slice(0,n):'';
const object=(v:any)=>v&&typeof v==='object'&&!Array.isArray(v)?v:null;
const rows=(v:any,n=60)=>Array.isArray(v)?v.slice(0,n).filter(object):[];
const time=(v:any)=>{const n=Date.parse(text(v,40));return Number.isFinite(n)?n:null;};
export default function Widget({feeds,size}:any){
 const now=useNow(),[view,setView]=useState('rounds');
 const f=feeds?.['agent.emerging-market-radar'],latest=object(f?.data);
 const failed=['error','failed'].includes(text(latest?.status));
 const noFunding=failed||rows(latest?.sources,8).some(s=>s.role==='funding'&&s.status==='unavailable');
 const previous=rows(f?.history,10).map(h=>object(h.data)).filter(d=>d&&!['error','failed'].includes(text(d.status))&&rows(d.items).length).sort((a,b)=>(time(b.observedAt)||0)-(time(a.observedAt)||0))[0];
 const retained=!!(noFunding&&previous),d=retained?previous:latest,stamp=time(d?.observedAt);
 const stale=stamp!==null&&now-stamp>86400000;
 const items=rows(d?.items).filter(i=>{const at=time(i.date);return at!==null&&at<=now&&at>=now-30*86400000;});
 const sources=rows(latest?.sources,8),news=rows(failed?d?.news:latest?.news,20),markets=rows(d?.markets,25);
 const warning=failed||stale||retained||!!text(latest?.message)||latest?.status==='partial';
 const cached=(i:any)=>retained||i.verificationStatus==='retained'||!time(i.lastVerifiedAt)||now-(time(i.lastVerifiedAt)||0)>86400000;
 const selected=view==='rounds'?items:view==='news'?news:view==='markets'?markets:sources;
 const rendered=selected.slice(0,20).map((i:any,k:number)=>{
  if(view==='rounds')return <Row key={k} title={`${cached(i)?'Cached · ':''}${text(i.company,100)} · ${text(i.market,100)}`} marker={cached(i)?'stale':'none'} detail={`${i.dateBasis==='deal'?'Deal date':i.dateBasis==='announcement'?'Announcement':'Reported date'} ${text(i.date,10)} · ${text(i.source,80)}. ${text(i.description,220)} ${text(i.productStatus,90)}. ${text(i.url,180)}`} meta={`Checked ${text(i.lastVerifiedAt,10)||'unknown'} · ${text(i.amount,80)||'Amount not verified'}`}/>;
  if(view==='news')return <Row key={k} title={`${failed?'Cached · ':''}${text(i.title,180)}`} detail={`${text(i.publishedAt,10)} · ${text(i.source,60)} · ${text(i.summary,220)} ${text(i.url,220)}`}/>;
  if(view==='markets')return <Row key={k} title={`${retained||stale?'Cached · ':''}${text(i.name,100)}`} detail={`${text(i.basis,100)||'Analytical grouping'} · ${Array.isArray(i.companies)?i.companies.slice(0,20).map((c:any)=>text(c,70)).filter(Boolean).join(', '):''}`}/>;
  return <Row key={k} title={`${text(i.name,80)} · ${text(i.status,30)}`} marker={i.status==='ok'?'none':'attention'} detail={text(i.detail,400)}/>;
 });
 return <Tile title="Emerging-market radar" size={size} freshness={{observedAt:stamp,expectedIntervalSec:86400,error:failed?(text(latest?.message)||'Sources unavailable'):null}} footer={text(latest?.coverage,260)||'Daily bounded scan · Deal dates are not announcement dates.'}>
 {!latest?<PendingState/>:<>
 <Between><Metric value={items.length} label="funding signals · 30 days"/><StatusPill tone={warning?'attention':'neutral'}>{failed?'Sources failed':retained?'Cached rounds':stale?'Out of date':warning?'Limited coverage':`${markets.length} markets`}</StatusPill></Between>
 {text(latest.message)?<Row title="Source status" detail={`${text(latest.message,200)}${retained?' Cached funding records retain original check dates.':''}`} marker="attention"/>:null}
 <label>Show <select aria-label="Radar view" value={view} onChange={e=>setView(e.target.value)}><option value="rounds">Rounds</option><option value="markets">Markets</option><option value="news">News</option><option value="sources">Source health</option></select></label>
 <List ariaLabel={`Radar ${view}`} more={Math.max(0,selected.length-20)} empty={rendered.length?null:{title:warning?'No current signals available in this view':'No matching signals in this scan',detail:warning?'Check source health; this does not establish that no rounds occurred.':'Bounded coverage, not exhaustive.'}}>{rendered}</List>
 </>}
 </Tile>;
}
