import React from 'react';
import { Tile, List, Row, StatusPill, Metric, Between, PendingState } from '@veetbot/cockpit-kit';
type Pull = {number:number; title:string; author:string; draft:boolean|null; review:string; checks:string; merge:string; automatedReview?:string; automatedReviewNames?:string; security?:string; mergeState?:string; reviewOnOlderCommit?:boolean; classificationVersion?:number};
type Repo = {name:string; status:string; message:string; totalOpen:number|null; pullRequests:Pull[]};
type Snapshot = {observedAt:string; expectedIntervalSec:number; repositories:Repo[]};
const formalLabels:Record<string,string> = {approved:'Approval submitted',changes_requested:'Changes requested',review_required:'Review required',pending:'Review requested',commented:'Comments submitted; no approval',none:'No submitted review',unknown:'Submitted reviews unknown'};
const autoLabels:Record<string,string> = {passing:'completed (not approval)',failing:'check failed',pending:'reviewing',skipped:'skipped / neutral',none:'No automated review reported',unknown:'Automated review unknown'};
const ciLabels:Record<string,string> = {passing:'Passing',failing:'Failed',pending:'Pending',skipped:'Skipped / neutral',none:'No CI checks reported',unknown:'Unknown'};
const securityLabels:Record<string,string> = {passing:'Passed',failing:'Failed',pending:'Pending',skipped:'Skipped / neutral',none:'No security checks reported',unknown:'Unknown'};
const conflictLabels:Record<string,string> = {mergeable:'No conflicts',conflicting:'Merge conflicts',unknown:'Conflicts unknown'};
const eligibilityLabels:Record<string,string> = {blocked:'Merging blocked',behind:'Branch behind base',dirty:'GitHub reports conflicts',draft:'Draft',unstable:'GitHub reports unstable checks',clean:'GitHub reports clean',has_hooks:'GitHub reports merge hooks',unknown:'Merge eligibility unknown'};
const needsAttention = (p:Pull) => p.checks === 'failing' || p.security === 'failing' || p.automatedReview === 'failing' || p.review === 'changes_requested' || p.merge === 'conflicting' || p.mergeState === 'blocked';
const indent:React.CSSProperties = {paddingInlineStart:'1em'};
function Details({p}:{p:Pull}) {
 const classified = p.classificationVersion === 2;
 const formal = (formalLabels[p.review] || formalLabels.unknown) + (p.reviewOnOlderCommit ? ' (includes older commit)' : '');
 const automated = !classified ? 'Automated review not separated in this snapshot' : p.automatedReview === 'none' ? autoLabels.none : p.automatedReview === 'unknown' ? autoLabels.unknown : `${p.automatedReviewNames || 'Automated review'}: ${autoLabels[p.automatedReview || 'unknown'] || autoLabels.unknown}`;
 return <>
  <div><strong>Code review:</strong> {automated} · {formal}</div>
  <div><strong>CI:</strong> {classified ? (ciLabels[p.checks] || ciLabels.unknown) : 'Awaiting classified refresh'}</div>
  {classified && p.security !== 'none' && <div><strong>Security:</strong> {securityLabels[p.security || 'unknown'] || securityLabels.unknown}</div>}
  <div><strong>Merge:</strong> {conflictLabels[p.merge] || conflictLabels.unknown} · {eligibilityLabels[p.mergeState || 'unknown'] || eligibilityLabels.unknown}</div>
 </>;
}
export default function Widget({feeds,size}:any) {
 const data = feeds['agent.github-pull-requests']?.data as Snapshot | null | undefined;
 const repos = data?.repositories || [];
 const unavailable = repos.filter(r => r.status !== 'ok');
 const total = repos.filter(r => r.status === 'ok').reduce((sum,r) => sum + (r.totalOpen || 0),0);
 const flagged = repos.reduce((sum,r) => sum + r.pullRequests.filter(needsAttention).length,0);
 const interval = data?.expectedIntervalSec || 180;
 const freshness = {observedAt:data ? Date.parse(data.observedAt) : null, expectedIntervalSec:interval, error:unavailable.length ? 'Repository data unavailable; open PR count is incomplete.' : null};
 const rows:React.ReactNode[] = [];
 repos.forEach(r => {
  rows.push(<Row key={r.name} title={<strong>{r.name}</strong>} value={<StatusPill tone={r.status === 'ok' ? 'neutral' : 'attention'}>{r.status === 'ok' ? `${r.totalOpen} open` : 'Unavailable'}</StatusPill>} />);
  const note = r.status !== 'ok' ? r.message || 'Repository data unavailable' : r.totalOpen === 0 ? 'No open pull requests' : r.message;
  if (note) rows.push(<div key={`${r.name}-note`} style={indent}><Row title={note} /></div>);
  if (r.status === 'ok') [...r.pullRequests].sort((a,b) => Number(needsAttention(b))-Number(needsAttention(a)) || b.number-a.number).forEach(p => {
   rows.push(<div key={`${r.name}-${p.number}`} style={indent}><Row title={`#${p.number} ${p.title}`} detail={<Details p={p} />} meta={p.author ? `@${p.author}` : 'Author unknown'} value={<StatusPill tone={needsAttention(p) ? 'attention' : 'neutral'}>{p.draft === true ? 'Draft' : p.draft === false ? 'Open' : 'Draft unknown'}</StatusPill>} marker={p.checks === 'failing' || p.security === 'failing' || p.automatedReview === 'failing' ? 'error' : needsAttention(p) ? 'attention' : 'none'} /></div>);
  });
 });
 return <Tile title="Open pull requests" size={size} freshness={freshness} footer={`Refreshes every ${Math.round(interval/60)} min · No conflicts does not mean ready to merge`}>
  {!data ? <PendingState /> : <>
   <Between><Metric value={unavailable.length ? `${total}+` : total} label={unavailable.length ? 'known open · incomplete' : 'open across repositories'} /><StatusPill tone={flagged ? 'attention' : 'neutral'}>{flagged ? `${flagged} with issues` : 'PR status'}</StatusPill></Between>
   <List ariaLabel="Repository pull requests" empty={repos.length === 0 ? {title:'No repositories configured'} : null}>{rows}</List>
  </>}
 </Tile>;
}
