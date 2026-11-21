import React from 'react';
import {Tile,List,Row,Delta,PendingState} from '@veetbot/cockpit-kit';
const finite=(v:any):v is number=>typeof v==='number'&&Number.isFinite(v);
function money(value:number,currency:string){try{return new Intl.NumberFormat('en-US',{style:'currency',currency,minimumFractionDigits:2,maximumFractionDigits:2}).format(value);}catch{return value.toFixed(2)+' '+currency;}}
function stamp(value:any){if(!finite(value))return 'Quote time unavailable';return new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZone:'America/New_York',timeZoneName:'short'}).format(new Date(value));}
export default function Widget({feeds,size}:any){
 const data=feeds['agent.stock-ticker']?.data;
 const quotes=Array.isArray(data?.quotes)?data.quotes.filter((q:any)=>q&&typeof q==='object').slice(0,20):[];
 const freshness={observedAt:finite(data?.observedAt)?data.observedAt:null,expectedIntervalSec:600,error:typeof data?.error==='string'?data.error:null};
 return <Tile title="Stocks" size={size} freshness={freshness} footer="Yahoo Finance · 10-min refresh · May be delayed · Regular session">
 {!data?<PendingState/>:<List empty={quotes.length?null:{title:'No stocks configured'}}>{quotes.map((q:any,i:number)=>{
 const ok=q.status==='ok'&&finite(q.price)&&finite(q.quoteAt);
 const change=finite(q.change)?`${q.change>0?'+':q.change<0?'−':''}${money(Math.abs(q.change),q.currency||'USD')}`:'Change unavailable';
 return <Row key={String(q.symbol)+i} title={String(q.symbol||'Unknown')} detail={ok?`${String(q.name||'')} · ${stamp(q.quoteAt)}`:`${String(q.name||'')} · ${String(q.message||'Quote unavailable')}`} value={ok?money(q.price,q.currency||'USD'):'Unavailable'} meta={ok?<>{change} · <Delta value={finite(q.changeFraction)?q.changeFraction*100:null} format="percent" period="vs prior close"/></>:undefined} marker={ok?'none':'attention'}/>;
 })}</List>}
 </Tile>;
}
