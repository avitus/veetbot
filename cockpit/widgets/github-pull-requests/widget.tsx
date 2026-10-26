import React from 'react';
import {Tile, List, Row, StatusPill, formatInterval} from '@veetbot/cockpit-kit';
type Pull = {number:number; title:string; author:string; draft:boolean|null; review:string; checks:string; merge:string};
type Repo = {name:string; status:string; message:string; totalOpen:number|null; pullRequests:Pull[]};
type Snapshot = {observedAt:string; expectedIntervalSec:number; repositories:Repo[]};
const reviews:Record<string,string>={approved:'Approved',changes_requested:'Changes requested',review_required:'Review required',pending:'Awaiting review',none:'No review',unknown:'Review unknown'};
const checks:Record<string,string>={passing:'CI passing',failing:'CI failing',pending:'CI running',none:'No checks',unknown:'CI unknown'};
function blocker(p:Pull){return p.checks==='failing'||p.review==='changes_requested'||p.merge==='conflicting';}
export default function Widget({feeds,size}:any){
 const d=feeds['agent.github-pull-requests']?.data as Snapshot|undefined;
 const rs=d?.repositories||[];
 const unavailable=rs.filter(r=>r.status!=='ok').length;
 const count=rs.reduce((n,r)=>n+(r.status==='ok'?(r.totalOpen||0):0),0);
 const omitted=rs.reduce((n,r)=>n+(r.status==='ok'?Math.max(0,(r.totalOpen||0)-r.pullRequests.length):0),0);
 const fresh=d?{observedAt:Date.parse(d.observedAt),expectedIntervalSec:d.expectedIntervalSec}:undefined;
 const footer=d?'GitHub · Refresh '+formatInterval(d.expectedIntervalSec)+' · '+d.observedAt.replace('T',' ').slice(0,16)+' UTC':'Awaiting first GitHub snapshot';
 return <Tile title="Pull requests" size={size} freshness={fresh} attention={unavailable?`${unavailable} repository unavailable`:null} footer={footer}>
 <List ariaLabel="Open pull requests" more={omitted}>
 {d&&<Row title={unavailable?`${count} known open`:`${count} open`} detail={unavailable?'Incomplete coverage — unavailable repositories excluded':`${rs.length} repositories checked`} value={<StatusPill tone={unavailable?'attention':'neutral'}>{unavailable?'Partial coverage':'All checked'}</StatusPill>}/>}
 {rs.flatMap(r=>{
  const header=<Row key={r.name} title={r.name} detail={r.status==='ok'?(r.totalOpen===0?'No open pull requests':`${r.totalOpen} open pull requests`):r.message} value={<StatusPill tone={r.status==='ok'?'neutral':'attention'}>{r.status==='ok'?String(r.totalOpen):'Unavailable'}</StatusPill>} marker={r.status==='ok'?'none':'attention'}/>;
  if(r.status!=='ok')return [header];
  return [header,...[...r.pullRequests].sort((a,b)=>Number(blocker(b))-Number(blocker(a))).map(p=>{
   const state=p.draft===true?'Draft':p.draft===false?'Open':'Open · draft unknown';
   const bits=[p.author?`@${p.author}`:'Author unknown',reviews[p.review]||'Review unknown',checks[p.checks]||'CI unknown'];
   bits.push(p.merge==='conflicting'?'Merge conflicts':p.merge==='mergeable'?'No merge conflicts':'Mergeability unknown');
   return <Row key={`${r.name}-${p.number}`} title={`#${p.number} ${p.title}`} detail={bits.join(' · ')} value={<StatusPill tone={blocker(p)?'attention':'neutral'}>{state}</StatusPill>} marker={p.checks==='failing'?'error':blocker(p)?'attention':'none'}/>;
  })];
 })}
 </List>
 </Tile>;
}