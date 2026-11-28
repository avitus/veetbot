import React from 'react';
import {Tile,List,Row,Metric,StatusPill,PendingState} from '@veetbot/cockpit-kit';
type Tone='ok'|'error'|'attention'|'neutral';
type Signal=[string,Tone];
const s=(s:string,t:Tone='neutral'):Signal=>[s,t];
const reviews:Record<string,Signal>={approved:s('approved','ok'),changes_requested:s('changes requested','error'),review_required:s('required','attention'),pending:s('requested','attention'),commented:s('commented, not approved'),none:s('none submitted')};
const checks:Record<string,Signal>={passing:s('passed','ok'),failing:s('failed','error'),pending:s('pending','attention'),none:s('none reported'),skipped:s('skipped / neutral')};
const merges:Record<string,Signal>={clean:s('clean','ok'),blocked:s('blocked','error'),dirty:s('conflicts','error'),behind:s('behind base','attention'),unstable:s('unstable','attention'),draft:s('draft'),has_hooks:s('hooks apply')};
const labels=['needs action','waiting','ready','draft','unconfirmed'];
const tones:Tone[]=['error','attention','ok','neutral','neutral'];
const txt=(v:any,n=200)=>typeof v==='string'?v.slice(0,n):'';
const known=(v:string)=>['passing','none','skipped'].includes(v);
function classify(p:any){
 const v=p.classificationVersion===2, rev=reviews[p.review]||s('unknown');
 const old=p.reviewOnOlderCommit===true, draft=p.draft===true||p.mergeState==='draft';
 const ck=(k:string)=>v?(checks[p[k]]||s('unknown')):s('unclassified');
 const ci=ck('checks'),auto=ck('automatedReview'),sec=ck('security');
 const merge=p.merge==='conflicting'?s('conflicts','error'):merges[p.mergeState]||s('unknown');
 const bad=p.merge==='conflicting'||p.mergeState==='dirty'||rev[1]==='error'||[ci,auto,sec].some(a=>a[1]==='error')||p.mergeState==='blocked'&&!draft;
 const wait=rev[1]==='attention'||[ci,auto,sec].some(a=>a[1]==='attention')||['unstable','behind'].includes(p.mergeState)||old;
 const ready=!draft&&p.draft===false&&p.merge==='mergeable'&&p.mergeState==='clean'&&v&&p.review==='approved'&&!old&&p.checks==='passing'&&known(p.security)&&known(p.automatedReview);
 const rank=bad?0:draft?3:wait?1:ready?2:4;
 const pairs:[string,Signal][]=[['Review',old?s(rev[0]+' · older commit',rev[1]==='error'?'error':'attention'):rev],['CI',ci],['Merge',merge]];
 if(p.merge!=='conflicting'&&p.mergeState!=='dirty'&&p.mergeState!=='clean'&&p.merge==='mergeable')pairs.push(['Conflicts',s('none')]);
 if(p.merge!=='conflicting'&&p.merge!=='mergeable'&&p.mergeState!=='dirty')pairs.push(['Conflicts',s('unknown')]);
 if(!v||p.automatedReview!=='none')pairs.push([txt(p.automatedReviewNames,50)||'Auto-review',auto]);
 if(!v||p.security!=='none')pairs.push(['Security',sec]);
 return {rank,pairs,draft};
}
const group={display:'flex',flexWrap:'wrap' as const,alignItems:'center',gap:4};
export default function Widget({feeds,size}:any){
 const f=feeds['agent.github-pull-requests'],d=f?.data,arr=Array.isArray(d?.repositories),repos=arr?d.repositories.slice(0,10):[];
 const prs:any[]=[],gaps:string[]=[],counts=[0,0,0,0,0];let total=0,partial=!arr, missing=0;
 for(const r of repos){
  const repo=txt(r?.name)||'Repository';if(r?.status!=='ok'||!Number.isInteger(r.totalOpen)||r.totalOpen<0){partial=true;gaps.push(repo+' unavailable');continue;}
  total+=r.totalOpen;const pp=Array.isArray(r.pullRequests)?r.pullRequests.slice(0,300).filter((p:any)=>p&&typeof p==='object'):[];
  missing+=Math.max(0,r.totalOpen-pp.length);
  pp.forEach((p:any)=>{const a=classify(p);counts[a.rank]++;prs.push({...a,p,repo});});
 }
 prs.sort((a,b)=>a.rank-b.rank||a.repo.localeCompare(b.repo)||(Number(b.p.number)||0)-(Number(a.p.number)||0));
 const sec=Number.isFinite(d?.expectedIntervalSec)&&d.expectedIntervalSec>0?d.expectedIntervalSec:180;
 const dt=Date.parse(d?.observedAt||'');const freshness={observedAt:Number.isFinite(dt)?dt:null,expectedIntervalSec:sec,error:f?.error?'Refresh failed':d&&partial?(gaps.join(' · ')||'Repository data unavailable'):null};
 const empty=!arr||partial?'No PR details available':!repos.length?'No repositories configured':total===0?'No open pull requests':'PR details unavailable';
 return <Tile title="Open pull requests" size={size} freshness={freshness} footer={missing?`${missing} open without status details`:`Refreshes every ${Math.round(sec/60)} min`}>
 {!d?<PendingState/>:<><div style={{...group,justifyContent:'space-between'}}><Metric value={partial?`${total}+`:total} label={`open · ${repos.length} ${repos.length===1?'repository':'repositories'}${partial?' · partial':''}`}/><div style={group}>{counts.map((n,i)=>n?<StatusPill key={i} tone={tones[i]}>{n} {labels[i]}</StatusPill>:null)}</div></div>
 <List ariaLabel="Pull requests by priority" more={Math.max(0,prs.length-24)} empty={!prs.length?{title:empty}:null}>{prs.slice(0,24).map(({p,repo,rank,pairs,draft},i)=><Row key={repo+':'+p.number+':'+i} marker={tones[rank]} title={txt(p.title,500)||'Untitled pull request'} meta={`${repo} #${Number.isInteger(p.number)?p.number:'?'}${txt(p.author)?' · @'+txt(p.author,100):''}${draft?' · Draft':''}`} detail={<span style={group}>{pairs.map(([name,state]:[string,Signal],j:number)=><StatusPill key={j} tone={state[1]}>{name}: {state[0]}</StatusPill>)}</span>}/>)}</List></>}
 </Tile>;
}