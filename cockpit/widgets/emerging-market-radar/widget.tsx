import React from 'react';
import {Tile,List,Row,StatusPill,Metric,Between,PendingState} from '@veetbot/cockpit-kit';
const t=(v:any,n=500)=>typeof v==='string'?v.slice(0,n):'';
export default function Widget({feeds,size}:any){
 const f=feeds['agent.emerging-market-radar'],d=f?.data;
 const recent=Array.isArray(d?.recent)?d.recent:[],items=Array.isArray(d?.items)?d.items:[],markets=Array.isArray(d?.markets)?d.markets:[];
 const time=Date.parse(d?.observedAt||'');const freshness={observedAt:Number.isFinite(time)?time:null,expectedIntervalSec:86400,error:f?.error?'Refresh failed':d?.message||null};
 const rows=(a:any[])=>a.map((i:any,k:number)=><Row key={k} title={`${t(i.company,100)} · ${t(i.amount,80)}`} detail={`${t(i.date,10)} · ${t(i.source,80)} | ${t(i.market,90)} — ${t(i.description,180)} Fit: ${t(i.why,180)} (${t(i.productStatus,40)})`}/>);
 return <Tile title="Emerging-market radar" size={size} freshness={freshness} attention={d?.status==='partial'?'Limited coverage':null} footer={d?`${t(d.coverage)} Daily refresh · Clusters are analysis.`:'Daily refresh'}>
 {!d?<PendingState/>:<>
 <Between><Metric value={markets.length} label="markets · 30-day sample"/><StatusPill tone="neutral">{recent.length} rounds / 7 days</StatusPill></Between>
 <List ariaLabel="Recent sourced rounds" more={Math.max(0,recent.length-3)} empty={!recent.length?{title:'No date-supported rounds in the past 7 days',detail:'Limited-source scan; not evidence that no rounds occurred.'}:null}>{rows(recent.slice(0,3))}</List>
 <details><summary>Clusters, all rounds & sources</summary><List ariaLabel="Market clusters">{markets.map((m:any,k:number)=><Row key={k} title={`${t(m.name,90)} · ${Number(m.count)||0} rounds`} detail={Array.isArray(m.companies)?m.companies.map((x:any)=>t(x,100)).join(', '):''}/>)}</List><List ariaLabel="All rounds">{rows(items)}</List><List ariaLabel="Source URLs">{items.map((i:any,k:number)=><Row key={k} title={`${t(i.company,100)} · ${t(i.date,10)}`} detail={`${t(i.url,1000)} | ${t(i.evidence)}`}/>)}</List></details>
 </>}
 </Tile>;
}
