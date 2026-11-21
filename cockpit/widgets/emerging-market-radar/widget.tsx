import React,{useState} from 'react';
import {Tile,List,Row,Metric,StatusPill,Between,PendingState,useNow} from '@veetbot/cockpit-kit';
const t=(v:any,n=180)=>typeof v==='string'?v.slice(0,n):'';
const obj=(v:any)=>v&&typeof v==='object'&&!Array.isArray(v)?v:null;
const rows=(v:any,n=60)=>Array.isArray(v)?v.slice(0,n).filter(obj):[];
const at=(v:any)=>{const n=Date.parse(t(v,40));return Number.isFinite(n)?n:null;};
const date=(v:any)=>{const n=at(v);return n===null?'Unknown':new Date(n).toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'});};
const btn:React.CSSProperties={font:'inherit',color:'inherit',background:'transparent',border:0,cursor:'pointer',padding:'6px 0',textAlign:'left',borderRadius:0};
const clip:React.CSSProperties={display:'block',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'};
export default function Widget({feeds,size}:any){
 const now=useNow(),[tab,setTab]=useState('rounds'),[page,setPage]=useState(0),[focus,setFocus]=useState<any>(null);
 const f=feeds?.['agent.emerging-market-radar'],latest=obj(f?.data),sources=rows(latest?.sources,10),failed=['error','failed'].includes(t(latest?.status));
 const down=failed||sources.some(s=>s.role==='funding'&&s.status==='unavailable');
 const prior=rows(f?.history,10).map(h=>obj(h.data)).filter(d=>d&&!['error','failed'].includes(t(d.status))&&rows(d.items).length).sort((a,b)=>(at(b.observedAt)||0)-(at(a.observedAt)||0))[0];
 const retained=!!(down&&prior),d=retained?prior:latest,stamp=at(d?.observedAt),stale=stamp!==null&&now-stamp>86400000;
 const inWindow=(v:any)=>{const n=at(v);return n!==null&&n<=now&&n>=now-30*86400000;};
 const items=rows(d?.items).filter(i=>inWindow(i.date)).sort((a,b)=>(at(b.date)||0)-(at(a.date)||0)||t(a.company).localeCompare(t(b.company)));
 const groups:any[]=[],map=new Map<string,any>();
 items.forEach(i=>{const key=t(i.market,120)||'Unclassified';if(!map.has(key)){const g={name:key,items:[],date:i.date};map.set(key,g);groups.push(g);}map.get(key).items.push(i);});
 groups.sort((a,b)=>b.items.length-a.items.length||(at(b.date)||0)-(at(a.date)||0)||a.name.localeCompare(b.name));
 const news=rows(failed?d?.news:latest?.news,20).filter(n=>inWindow(n.publishedAt));
 const cached=(i:any)=>retained||i.verificationStatus==='retained'||!at(i.lastVerifiedAt)||now-(at(i.lastVerifiedAt)||0)>86400000;
 const warning=failed||retained||stale||latest?.status==='partial';
 const count=size==='wide'?2:size==='tall'?3:4,chosen=tab==='rounds'?items:tab==='markets'?groups:tab==='news'?news:sources;
 const pages=Math.max(1,Math.ceil(chosen.length/count)),current=Math.min(page,pages-1),visible=chosen.slice(current*count,(current+1)*count);
 const change=(id:string)=>{setTab(id);setPage(0);setFocus(null);};
 const open=(kind:string,item:any)=>setFocus({kind,item});
 const title=(name:string,click:()=>void)=><button type="button" style={{...btn,fontWeight:600,padding:0,maxWidth:'100%'}} onClick={click}><span style={clip}>{name}</span></button>;
 const listing=visible.map((i:any,k:number)=>{
  if(tab==='rounds')return <Row key={k} title={title(t(i.company,100),()=>open('round',i))} detail={<span style={clip}>{t(i.market,120)}</span>} value={date(i.date)} marker={cached(i)?'stale':'none'}/>;
  if(tab==='markets')return <Row key={k} title={title(i.name,()=>open('market',i))} detail={<span style={clip}>{i.items.map((x:any)=>t(x.company,80)).join(' · ')}</span>} value={`${i.items.length} ${i.items.length===1?'round':'rounds'}`}/>;
  if(tab==='news')return <Row key={k} title={title(t(i.title,150),()=>open('news',i))} detail={<span style={clip}>{t(i.source,80)}</span>} value={date(i.publishedAt)}/>;
  return <Row key={k} title={title(t(i.name,80),()=>open('source',i))} detail={i.role==='analysis'?'Database categories · no model dependency':i.status==='ok'?'Available at last refresh':'Tap for details'} value={<StatusPill tone={i.status==='ok'?'neutral':'attention'}>{i.status==='ok'?'Available':'Limited'}</StatusPill>}/>;
 });
 const detail=()=>{const i=focus.item;
  if(focus.kind==='round')return <><Row title={t(i.company,100)} detail={t(i.market,160)}/><Row title={`${i.dateBasis==='announcement'?'Announcement':i.dateBasis==='deal'?'Deal date':'Reported date'} · ${date(i.date)}`} detail={`${t(i.source,100)} · checked ${date(i.lastVerifiedAt)}${cached(i)?' · cached':''}`}/><Row title="Funding & product" detail={`${t(i.amount,150)||'Amount unverified'}. ${t(i.productStatus,160)||'Product status unverified'}.`}/><Row title="Company website" detail={<span style={{overflowWrap:'anywhere',userSelect:'text'}}>{t(i.url,1000)||'Not available'}</span>}/></>;
  if(focus.kind==='market')return <><Row title={t(i.name,120)} detail="Database category · not an independently researched market"/>{i.items.slice(0,25).map((c:any,k:number)=><Row key={k} title={title(t(c.company,100),()=>open('round',c))} value={date(c.date)}/>)}</>;
  if(focus.kind==='news')return <><Row title={t(i.title,240)} detail={`${t(i.source,100)} · ${date(i.publishedAt)}`}/><Row title="Reported development" detail={t(i.summary,450)}/><Row title="Source URL" detail={<span style={{overflowWrap:'anywhere',userSelect:'text'}}>{t(i.url,1200)}</span>}/></>;
  return <><Row title={t(i.name,120)} detail={t(i.detail,550)}/><Row title="Scan limits" detail={t(latest?.coverage,700)}/></>;
 };
 return <Tile title="Emerging-market radar" size={size} freshness={{observedAt:stamp,expectedIntervalSec:86400,error:failed?'Discovery sources unavailable':null}} footer={<span style={{fontSize:11,opacity:.7}}>Seed discovery · Deal dates ≠ announcements</span>}>
 {!latest?<PendingState/>:<div style={{display:'flex',flexDirection:'column',gap:10,minHeight:0,height:'100%'}}>
 {focus?<><Between><button type="button" style={btn} onClick={()=>setFocus(null)}>← Back to {tab==='markets'?'categories':tab}</button>{focus.kind==='round'&&cached(focus.item)?<StatusPill tone="stale">Cached</StatusPill>:null}</Between><div style={{overflowY:'auto',minHeight:0,display:'flex',flexDirection:'column',gap:10}}>{detail()}</div></>:<>
 <div style={{display:'grid',gridTemplateColumns:'repeat(3,minmax(0,1fr))',gap:16}}><Metric value={items.length} size="m" label="seed signals"/><Metric value={groups.length} size="m" label="categories"/><Metric value={news.length} size="m" label="news links"/></div>
 {warning?<Between><StatusPill tone="attention">{failed?'Sources unavailable':retained?'Cached rounds':stale?'Data is stale':'Partial scan'}</StatusPill><button type="button" style={{...btn,fontSize:12}} onClick={()=>change('sources')}>Source health →</button></Between>:null}
 <nav aria-label="Radar views" style={{display:'flex',gap:18,flexShrink:0}}>{[['rounds','Rounds'],['markets','Categories'],['news','News'],['sources','Info']].map(([id,label])=><button key={id} type="button" aria-pressed={tab===id} style={{...btn,fontSize:12,fontWeight:tab===id?650:400,opacity:tab===id?1:.6,borderBottom:tab===id?'2px solid currentColor':'2px solid transparent'}} onClick={()=>change(id)}>{label}</button>)}</nav>
 <Between><span style={{fontSize:10,letterSpacing:'.08em',textTransform:'uppercase',opacity:.65}}>{tab==='rounds'?'Latest deal dates':tab==='markets'?'Database categories':tab==='news'?'Reported developments':'Sources & methodology'}</span><span style={{fontSize:11,opacity:.6}}>30 days</span></Between>
 <div style={{flex:'1 1 auto',minHeight:0}}><List ariaLabel={`Radar ${tab}`} empty={listing.length?null:{title:warning?'No current data in this view':'No signals in this scan',detail:'Bounded coverage, not an exhaustive market survey.'}}>{listing}</List></div>
 {pages>1?<Between><span style={{fontSize:11,opacity:.65}}>{current*count+1}–{Math.min((current+1)*count,chosen.length)} of {chosen.length}</span><div style={{display:'flex',gap:16}}><button type="button" aria-label="Previous page" disabled={current===0} style={{...btn,fontSize:12,opacity:current===0?.3:1}} onClick={()=>setPage(current-1)}>← Previous</button><button type="button" aria-label="Next page" disabled={current===pages-1} style={{...btn,fontSize:12,opacity:current===pages-1?.3:1}} onClick={()=>setPage(current+1)}>Next →</button></div></Between>:null}
 </>}
 </div>}
 </Tile>;
}
