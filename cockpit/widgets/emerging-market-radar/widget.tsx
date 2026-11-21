import React,{useState,useRef,useLayoutEffect} from 'react';
import {Tile,Row,StatusPill,Between,PendingState,useNow} from '@veetbot/cockpit-kit';
const text=(v:any,n=180)=>typeof v==='string'?v.slice(0,n):'';
const object=(v:any)=>v&&typeof v==='object'&&!Array.isArray(v)?v:null;
const rows=(v:any,n=60)=>Array.isArray(v)?v.slice(0,n).filter(object):[];
const time=(v:any)=>{const n=Date.parse(text(v,40));return Number.isFinite(n)?n:null;};
const date=(v:any)=>{const n=time(v);return n===null?'Unknown':new Date(n).toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'});};
const button:React.CSSProperties={font:'inherit',color:'inherit',background:'transparent',border:0,cursor:'pointer',padding:'4px 0',textAlign:'left'};
const clip:React.CSSProperties={display:'block',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'};
const ROW=54;
export default function Widget({feeds,size}:any){
 const now=useNow(),[tab,setTab]=useState('rounds'),[start,setStart]=useState(0),[focus,setFocus]=useState<any>(null),[capacity,setCapacity]=useState(1),[compact,setCompact]=useState(size==='wide');
 const body=useRef<HTMLDivElement>(null),area=useRef<HTMLDivElement>(null);
 const f=feeds?.['agent.emerging-market-radar'],latest=object(f?.data),sources=rows(latest?.sources,10),failed=['error','failed'].includes(text(latest?.status));
 const down=failed||sources.some(s=>s.role==='funding'&&s.status==='unavailable');
 const prior=rows(f?.history,10).map(h=>object(h.data)).filter(d=>d&&!['error','failed'].includes(text(d.status))&&rows(d.items).length).sort((a,b)=>(time(b.observedAt)||0)-(time(a.observedAt)||0))[0];
 const retained=!!(down&&prior),data=retained?prior:latest,stamp=time(data?.observedAt),stale=stamp!==null&&now-stamp>86400000;
 const inWindow=(v:any)=>{const n=time(v);return n!==null&&n<=now&&n>=now-30*86400000;};
 const items=rows(data?.items).filter(i=>inWindow(i.date)).sort((a,b)=>(time(b.date)||0)-(time(a.date)||0)||text(a.company).localeCompare(text(b.company)));
 const groups:any[]=[],map=new Map<string,any>();
 items.forEach(i=>{const key=text(i.market,120)||'Unclassified';if(!map.has(key)){const g={name:key,items:[],date:i.date};map.set(key,g);groups.push(g);}map.get(key).items.push(i);});
 groups.sort((a,b)=>b.items.length-a.items.length||(time(b.date)||0)-(time(a.date)||0)||a.name.localeCompare(b.name));
 const news=rows(failed?data?.news:latest?.news,20).filter(n=>inWindow(n.publishedAt));
 const cached=(i:any)=>retained||i.verificationStatus==='retained'||!time(i.lastVerifiedAt)||now-(time(i.lastVerifiedAt)||0)>86400000;
 const warning=failed||retained||stale||latest?.status==='partial';
 const chosen=tab==='rounds'?items:tab==='markets'?groups:tab==='news'?news:sources;
 const offset=Math.min(start,Math.max(0,chosen.length-1)),end=Math.min(offset+capacity,chosen.length),visible=chosen.slice(offset,end);
 useLayoutEffect(()=>{const b=body.current,a=area.current;if(!b)return;const measure=()=>{setCompact(b.clientHeight<180);if(a)setCapacity(Math.max(1,Math.min(60,Math.floor(a.clientHeight/ROW))));};measure();const ro=new ResizeObserver(measure);ro.observe(b);if(a)ro.observe(a);return()=>ro.disconnect();},[!!latest,!!focus]);
 const change=(id:string)=>{setTab(id);setStart(0);setFocus(null);};
 const title=(name:string,kind:string,item:any)=><button type="button" style={{...button,fontWeight:600,padding:0,maxWidth:'100%'}} onClick={()=>setFocus({kind,item})}><span style={clip}>{name}</span></button>;
 const listing=visible.map((i:any,k:number)=>{let content;
  if(tab==='rounds')content=<Row title={title(text(i.company,100),'round',i)} detail={<span style={clip}>{text(i.market,120)}</span>} value={date(i.date)} marker={cached(i)?'stale':'none'}/>;
  else if(tab==='markets')content=<Row title={title(i.name,'market',i)} detail={<span style={clip}>{i.items.map((x:any)=>text(x.company,80)).join(' · ')}</span>} value={`${i.items.length} ${i.items.length===1?'round':'rounds'}`}/>;
  else if(tab==='news')content=<Row title={title(text(i.title,150),'news',i)} detail={<span style={clip}>{text(i.source,80)}</span>} value={date(i.publishedAt)}/>;
  else content=<Row title={title(text(i.name,80),'source',i)} detail={<span style={clip}>{i.role==='analysis'?'Database categories · no model dependency':i.status==='ok'?'Available at last refresh':'Tap for details'}</span>} value={<StatusPill tone={i.status==='ok'?'neutral':'attention'}>{i.status==='ok'?'Available':'Limited'}</StatusPill>}/>;
  return <div role="listitem" key={`${tab}-${offset+k}`} style={{height:ROW,boxSizing:'border-box',display:'flex',flexDirection:'column',justifyContent:'center',overflow:'hidden'}}>{content}</div>;
 });
 const detail=()=>{const i=focus.item;
  if(focus.kind==='round')return <><Row title={text(i.company,100)} detail={text(i.market,160)}/><Row title={`${i.dateBasis==='announcement'?'Announcement':i.dateBasis==='deal'?'Deal date':'Reported date'} · ${date(i.date)}`} detail={`${text(i.source,100)} · checked ${date(i.lastVerifiedAt)}${cached(i)?' · cached':''}`}/><Row title="Funding & product" detail={`${text(i.amount,150)||'Amount unverified'}. ${text(i.productStatus,160)||'Product status unverified'}.`}/><Row title="Company website" detail={<span style={{overflowWrap:'anywhere',userSelect:'text'}}>{text(i.url,1000)||'Not available'}</span>}/></>;
  if(focus.kind==='market')return <><Row title={text(i.name,120)} detail="Database category · not an independently researched market"/>{i.items.map((c:any,k:number)=><Row key={k} title={title(text(c.company,100),'round',c)} value={date(c.date)}/>)}</>;
  if(focus.kind==='news')return <><Row title={text(i.title,240)} detail={`${text(i.source,100)} · ${date(i.publishedAt)}`}/><Row title="Reported development" detail={text(i.summary,450)}/><Row title="Source URL" detail={<span style={{overflowWrap:'anywhere',userSelect:'text'}}>{text(i.url,1200)}</span>}/></>;
  return <><Row title={text(i.name,120)} detail={text(i.detail,550)}/><Row title="Scan limits" detail={text(latest?.coverage,700)}/></>;
 };
 const pager=<Between><span style={{fontSize:11}}>{chosen.length?`${offset+1}–${end} of ${chosen.length}`:'0 results'}</span><div style={{display:'flex',gap:16}}><button type="button" aria-label="Previous page" disabled={offset===0} style={{...button,fontSize:12,opacity:offset===0?.3:1}} onClick={()=>setStart(Math.max(0,offset-capacity))}>← Previous</button><button type="button" aria-label="Next page" disabled={end>=chosen.length} style={{...button,fontSize:12,opacity:end>=chosen.length?.3:1}} onClick={()=>setStart(end)}>Next →</button></div></Between>;
 return <Tile title="Emerging-market radar" size={size} hideNotice freshness={{observedAt:stamp,expectedIntervalSec:86400,error:failed?'Discovery sources unavailable':null}} footer={latest&&!focus?pager:'Seed discovery · Deal dates ≠ announcements'}>
 {!latest?<PendingState/>:<div ref={body} style={{display:'flex',flexDirection:'column',gap:compact?4:8,flex:'1 1 0%',minHeight:0,height:'100%',overflow:'hidden'}}>
 {focus?<><div style={{flexShrink:0}}><button type="button" style={button} onClick={()=>setFocus(null)}>← Back to {tab==='markets'?'categories':tab}</button></div><div style={{overflowY:'auto',flex:'1 1 0%',minHeight:0,display:'flex',flexDirection:'column',gap:10}}>{detail()}</div></>:<>
 {!compact?<div style={{fontSize:12,flexShrink:0,opacity:.8}}>{items.length} seed signals · {groups.length} categories · {news.length} news · 30 days</div>:null}
 <nav aria-label="Radar views" style={{display:'flex',flexWrap:'wrap',columnGap:12,rowGap:0,flexShrink:0,alignItems:'center'}}>{[['rounds','Rounds'],['markets','Categories'],['news','News'],['sources','Info']].map(([id,label])=><button key={id} type="button" aria-pressed={tab===id} style={{...button,fontSize:12,fontWeight:tab===id?650:400,opacity:tab===id?1:.6,borderBottom:tab===id?'2px solid currentColor':'2px solid transparent'}} onClick={()=>change(id)}>{label}</button>)}{warning?<StatusPill tone="attention">{failed?'Unavailable':retained?'Cached':stale?'Stale':'Partial'}</StatusPill>:null}</nav>
 <div ref={area} style={{flex:'1 1 0%',minHeight:0,overflowY:'auto'}}><div role="list" aria-label={`Radar ${tab}`}>{listing.length?listing:<Row title={warning?'No current data in this view':'No signals in this scan'} detail="Bounded coverage, not an exhaustive market survey."/>}</div></div>
 </>}
 </div>}
 </Tile>;
}
