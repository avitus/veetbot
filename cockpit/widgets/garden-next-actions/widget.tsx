import React from 'react';
import {Tile,List,Row,PendingState} from '@veetbot/cockpit-kit';
const t=(v:any,n=500)=>typeof v==='string'?v.slice(0,n):'';
export default function Widget({feeds,size}:any){
 const f=feeds['agent.garden-next-actions'],d=f?.data,tasks=Array.isArray(d?.tasks)?d.tasks:[],alerts=Array.isArray(d?.alerts)?d.alerts:[];
 const time=Date.parse(d?.observedAt||'');const freshness={observedAt:Number.isFinite(time)?time:null,expectedIntervalSec:21600,error:f?.error?'Refresh failed':d?.message||null};
 const rows=(a:any[],full=false)=>a.map((x:any,k:number)=><Row key={k} title={`${t(x.title,120)} · ${t(x.status,40)}`} detail={`${t(x.window,100)}. ${full?t(x.detail):t(x.detail,140)+(t(x.detail).length>140?'…':'')}`} marker={x.status==='Confirm first'?'attention':'none'}/>);
 return <Tile title="Garden: next actions" size={size} freshness={freshness} attention={d?.status==='partial'?'Forecast or input incomplete':null} footer={d?`${t(d.guidance,400)} Updates every 6 hours.`:'Updates every 6 hours'}>
 {!d?<PendingState/>:<>
 <Row title={t(d.location,100)} detail={`${t(d.forecast,180)} · Open-Meteo`}/>
 <Row title="Weather check" detail={alerts.map((a:any)=>t(a,250)).join(' ')}/>
 <List ariaLabel="Next three garden actions">{rows(tasks.slice(0,3))}</List>
 <details><summary>Full guidance, sources & context</summary><List ariaLabel="All garden actions">{rows(tasks,true)}</List><Row title="Basis" detail={Array.isArray(d.references)?d.references.map((x:any)=>t(x,300)).join(' · '):'Owner notes and UC ANR guidance'}/><List ariaLabel="Sources">{tasks.slice(0,3).map((x:any,k:number)=><Row key={k} title={t(x.title,120)} detail={t(x.url,1000)}/>)}</List></details>
 </>}
 </Tile>;
}
