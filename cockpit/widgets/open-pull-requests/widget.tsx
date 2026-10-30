import React from 'react';
import { Tile, List, Row, StatusPill, Metric, Between, PendingState } from '@veetbot/cockpit-kit';
type Pull = {number:number; title:string; author:string; draft:boolean|null; review:string; checks:string; merge:string};
type Repo = {name:string; status:string; message:string; totalOpen:number|null; pullRequests:Pull[]};
type Snapshot = {observedAt:string; expectedIntervalSec:number; repositories:Repo[]};
const reviewLabels:Record<string,string> = {approved:'Approved review',changes_requested:'Changes requested',review_required:'Review required',pending:'Review requested',none:'Not reviewed',unknown:'Review unknown'};
const checkLabels:Record<string,string> = {passing:'CI passing',failing:'CI failed',pending:'CI pending',none:'No checks',unknown:'CI unknown'};
const mergeLabels:Record<string,string> = {mergeable:'No conflicts',conflicting:'Merge conflicts',unknown:'Conflicts unknown'};
const needsAttention = (p:Pull) => p.checks === 'failing' || p.review === 'changes_requested' || p.merge === 'conflicting';
export default function Widget({feeds,size}:any) {
 const data = feeds['agent.github-pull-requests']?.data as Snapshot | null | undefined;
 const repos = data?.repositories || [];
 const unavailable = repos.filter(r => r.status !== 'ok');
 const total = repos.filter(r => r.status === 'ok').reduce((sum,r) => sum + (r.totalOpen || 0),0);
 const flagged = repos.reduce((sum,r) => sum + r.pullRequests.filter(needsAttention).length,0);
 const interval = data?.expectedIntervalSec || 3600;
 const freshness = {observedAt:data ? Date.parse(data.observedAt) : null, expectedIntervalSec:interval, error:unavailable.length ? 'Repository data unavailable; open PR count is incomplete.' : null};
 const rows:React.ReactNode[] = [];
 repos.forEach(r => {
  rows.push(<Row key={r.name} title={r.name} value={<StatusPill tone={r.status === 'ok' ? 'neutral' : 'attention'}>{r.status === 'ok' ? `${r.totalOpen} open` : 'Unavailable'}</StatusPill>} detail={r.status !== 'ok' ? r.message : r.totalOpen === 0 ? 'No open pull requests' : r.message || undefined} />);
  if (r.status === 'ok') [...r.pullRequests].sort((a,b) => Number(needsAttention(b))-Number(needsAttention(a)) || b.number-a.number).forEach(p => {
   rows.push(<Row key={`${r.name}-${p.number}`} title={`#${p.number} ${p.title}`} detail={`${reviewLabels[p.review] || 'Review unknown'} · ${checkLabels[p.checks] || 'CI unknown'} · ${mergeLabels[p.merge] || 'Conflicts unknown'}`} meta={p.author ? `@${p.author}` : 'Author unknown'} value={<StatusPill tone={needsAttention(p) ? 'attention' : 'neutral'}>{p.draft === true ? 'Draft' : p.draft === false ? 'Open' : 'Draft unknown'}</StatusPill>} marker={p.checks === 'failing' ? 'error' : needsAttention(p) ? 'attention' : 'none'} />);
  });
 });
 return <Tile title="Open pull requests" size={size} freshness={freshness} footer={`Refreshes every ${Math.round(interval/60)} min · Reviews are not merge authorization`}>
  {!data ? <PendingState /> : <>
   <Between><Metric value={unavailable.length ? `${total}+` : total} label={unavailable.length ? 'known open · incomplete' : 'open across repositories'} /><StatusPill tone={flagged ? 'attention' : 'neutral'}>{flagged ? `${flagged} with issues` : 'PR status'}</StatusPill></Between>
   <List ariaLabel="Repository pull requests" empty={repos.length === 0 ? {title:'No repositories configured'} : null}>{rows}</List>
  </>}
 </Tile>;
}
