import React from 'react';
import {Tile,List,Row,StatusPill,FreshnessIndicator} from '@veetbot/cockpit-kit';
type Pull={number:number;title:string;author:string;draft:boolean|null;review:string;checks:string;merge:string};
type Repo={name:string;status:string;message:string;totalOpen:number|null;pullRequests:Pull[]};
type Branch={name:string;ahead:number;behind:number};
type BranchRepo={name:string;status:string;message:string;branches:Branch[];checked:number;totalBranches:number|null};
type Snapshot<T>={observedAt:string;expectedIntervalSec:number;repositories:T[]};
const reviews:Record<string,string>={approved:'Approved',changes_requested:'Changes requested',review_required:'Review required',pending:'Awaiting review',none:'No review',unknown:'Review unknown'};
const checks:Record<string,string>={passing:'CI passing',failing:'CI failing',pending:'CI running',none:'No checks',unknown:'CI unknown'};
const blocked=(p:Pull)=>p.checks==='failing'||p.review==='changes_requested'||p.merge==='conflicting';
export default function Widget({feeds,size}:any){
 const pr=feeds['agent.github-pull-requests']?.data as Snapshot<Repo>|undefined;
 const br=feeds['agent.github-branches']?.data as Snapshot<BranchRepo>|undefined;
 const names=Array.from(new Set([...(pr?.repositories||[]).map(r=>r.name),...(br?.repositories||[]).map(r=>r.name)]));
 const freshness=(d:any)=>({observedAt:d?Date.parse(d.observedAt):null,expectedIntervalSec:d?.expectedIntervalSec||300});
 const rows:any[]=[];
 names.forEach(name=>{
  const p=pr?.repositories.find(r=>r.name===name);
  const b=br?.repositories.find(r=>r.name===name);
  const bs=(b?.branches||[]).filter(x=>x.ahead>0).sort((a,c)=>c.ahead-a.ahead||a.name.localeCompare(c.name));
  rows.push(<Row key={name} title={name} detail={`${p?.status==='ok'?p.totalOpen+' open PRs':'PRs unavailable'} · ${b?bs.length+(b.status==='ok'?'':' known')+' branches ahead':'Branches pending'}`}/>);
  rows.push(<Row key={name+'-pr'} title="Open pull requests" detail={p?.status==='ok'?(p.totalOpen===0?'No open pull requests':undefined):(p?.message||'Awaiting PR data')} value={<FreshnessIndicator freshness={freshness(pr)}/>}/>);
  if(p?.status==='ok') [...p.pullRequests].sort((a,c)=>Number(blocked(c))-Number(blocked(a))).forEach(x=>rows.push(<Row key={name+'-pr-'+x.number} title={`#${x.number} ${x.title}`} detail={[x.author?'@'+x.author:'Author unknown',reviews[x.review]||'Review unknown',checks[x.checks]||'CI unknown',x.merge==='conflicting'?'Merge conflicts':x.merge==='mergeable'?'No merge conflicts':'Mergeability unknown'].join(' · ')} value={<StatusPill tone={blocked(x)?'attention':'neutral'}>{x.draft===true?'Draft':x.draft===false?'Open':'Draft unknown'}</StatusPill>} marker={x.checks==='failing'?'error':blocked(x)?'attention':'none'}/>));
  rows.push(<Row key={name+'-branches'} title="Branches ahead of main" detail={b?(b.status==='ok'?(bs.length?`${b.checked} non-main branches checked`:'No branches ahead of main'):(b.message||'Branch scan incomplete')):'Awaiting branch scan'} value={<FreshnessIndicator freshness={freshness(br)}/>} marker={b&&b.status!=='ok'?'attention':'none'}/>);
  bs.forEach(x=>rows.push(<Row key={name+'-branch-'+x.name} title={x.name} detail={`${x.behind} commit${x.behind===1?'':'s'} behind main${x.behind?' · Diverged':''}`} value={<StatusPill tone="neutral">{x.ahead} ahead</StatusPill>}/>));
  if(b?.status==='ok'&&b.message) rows.push(<Row key={name+'-note'} title="Branch scan note" detail={b.message}/>);
 });
 const omitted=(pr?.repositories||[]).reduce((n,r)=>n+(r.status==='ok'?Math.max(0,(r.totalOpen||0)-r.pullRequests.length):0),0);
 return <Tile title="PRs & branches" size={size} footer="GitHub · Branch counts are commits relative to main">
 <List ariaLabel="Pull requests and branches ahead of main" more={omitted} empty={{title:'Awaiting GitHub data',detail:'PR and branch snapshots load independently.'}}>{rows}</List>
 </Tile>;
}