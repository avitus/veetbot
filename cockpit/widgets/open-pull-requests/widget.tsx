import React, {useMemo} from 'react';
import {Tile,List,Row,StatusPill,Metric,Between,PendingState} from '@veetbot/cockpit-kit';
const formal:Record<string,string>={approved:'Approval submitted',changes_requested:'Changes requested',review_required:'Review required',pending:'Review requested',commented:'Comments submitted; no approval',none:'No submitted review',unknown:'Submitted reviews unknown'};
const automated:Record<string,string>={passing:'completed (not approval)',failing:'check failed',pending:'reviewing',skipped:'skipped / neutral',none:'No automated review reported',unknown:'Automated review unknown'};
const ci:Record<string,string>={passing:'Passing',failing:'Failed',pending:'Pending',skipped:'Skipped / neutral',none:'No CI checks reported',unknown:'Unknown'};
const security:Record<string,string>={passing:'Passed',failing:'Failed',pending:'Pending',skipped:'Skipped / neutral',none:'No security checks reported',unknown:'Unknown'};
const conflicts:Record<string,string>={mergeable:'No conflicts',conflicting:'Merge conflicts',unknown:'Conflicts unknown'};
const eligibility:Record<string,string>={blocked:'Merging blocked',behind:'Branch behind base',dirty:'GitHub reports conflicts',draft:'Draft',unstable:'GitHub reports unstable checks',clean:'GitHub reports clean',has_hooks:'GitHub reports merge hooks',unknown:'Merge eligibility unknown'};
const attention=(p:any)=>p.checks==='failing'||p.security==='failing'||p.automatedReview==='failing'||p.review==='changes_requested'||p.merge==='conflicting'||p.mergeState==='blocked';
const text=(v:any,n=500)=>typeof v==='string'?v.slice(0,n):'';
function detail(p:any){
 const classified=p.classificationVersion===2;
 const a=!classified?'Automated review not separated in this snapshot':p.automatedReview==='none'?automated.none:p.automatedReview==='unknown'||!p.automatedReview?automated.unknown:`${text(p.automatedReviewNames,200)||'Automated review'}: ${automated[p.automatedReview]||automated.unknown}`;
 return `Code review: ${a} · ${formal[p.review]||formal.unknown}${p.reviewOnOlderCommit?' (includes older commit)':''} | CI: ${classified?(ci[p.checks]||ci.unknown):'Awaiting classified refresh'}${classified&&p.security!=='none'?` | Security: ${security[p.security]||security.unknown}`:''} | Merge: ${conflicts[p.merge]||conflicts.unknown} · ${eligibility[p.mergeState]||eligibility.unknown}`;
}
export default function Widget({feeds,size}:any){
 const feed=feeds['agent.github-pull-requests'];
 const data=feed?.data;
 const view=useMemo(()=>{
  const repos=Array.isArray(data?.repositories)?data.repositories.slice(0,10):[];
  let total=0,flagged=0,shown=0,hidden=0,incomplete=false;
  const rows:React.ReactNode[]=[];
  repos.forEach((r:any,ri:number)=>{
   if(!r||typeof r!=='object'){incomplete=true;return;}
   const ok=r.status==='ok'&&Number.isInteger(r.totalOpen)&&r.totalOpen>=0;
   const pulls=Array.isArray(r.pullRequests)?r.pullRequests.slice(0,300).filter((p:any)=>p&&typeof p==='object'):[];
   incomplete=incomplete||!ok||!Array.isArray(r.pullRequests);
   if(ok)total+=r.totalOpen;
   flagged+=pulls.filter(attention).length;
   const note=!ok?text(r.message,300)||'Repository data unavailable':r.totalOpen===0?'No open pull requests':text(r.message,300);
   rows.push(<Row key={`repo-${ri}`} title={<strong>{text(r.name,200)||'Repository'}</strong>} detail={note||undefined} value={<StatusPill tone={ok?'neutral':'attention'}>{ok?`${r.totalOpen} open`:'Unavailable'}</StatusPill>}/>);
   if(ok){
    pulls.sort((a:any,b:any)=>Number(attention(b))-Number(attention(a))||(Number(b.number)||0)-(Number(a.number)||0)).forEach((p:any,pi:number)=>{
     if(shown>=24){hidden++;return;}
     shown++;
     const failed=p.checks==='failing'||p.security==='failing'||p.automatedReview==='failing';
     rows.push(<Row key={`pr-${ri}-${pi}`} title={`#${Number.isInteger(p.number)?p.number:'?'} ${text(p.title)}`} detail={detail(p)} meta={text(p.author,100)?`@${text(p.author,100)}`:'Author unknown'} value={<StatusPill tone={attention(p)?'attention':'neutral'}>{p.draft===true?'Draft':p.draft===false?'Open':'Draft unknown'}</StatusPill>} marker={failed?'error':attention(p)?'attention':'none'}/>);
    });
   }
  });
  return {rows,total,flagged,hidden,incomplete,empty:repos.length===0};
 },[data]);
 const interval=Number.isFinite(data?.expectedIntervalSec)&&data.expectedIntervalSec>0?data.expectedIntervalSec:180;
 const timestamp=Date.parse(data?.observedAt||'');
 const freshness={observedAt:Number.isFinite(timestamp)?timestamp:null,expectedIntervalSec:interval,error:feed?.error?'Feed refresh error':view.incomplete?'Repository data unavailable; open PR count is incomplete.':null};
 return <Tile title="Open pull requests" size={size} freshness={freshness} footer={`Refreshes every ${Math.round(interval/60)} min · No conflicts does not mean ready to merge`}>
  {!data?<PendingState/>:<>
   <Between><Metric value={view.incomplete?`${view.total}+`:view.total} label={view.incomplete?'known open · incomplete':'open across repositories'}/><StatusPill tone={view.flagged?'attention':'neutral'}>{view.flagged?`${view.flagged} with issues`:'PR status'}</StatusPill></Between>
   <List ariaLabel="Repository pull requests" more={view.hidden} empty={view.empty?{title:'No repositories configured'}:null}>{view.rows}</List>
  </>}
 </Tile>;
}
