import React,{useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';
import {ArrowLeft,History,X} from 'lucide-react';
import {request} from './console-client.js';
import ResultViewer,{ArtworkFrame,ResultBody} from './result-viewer.jsx';
import {MIN_CELLS,cellTone,groupBoard,latestArtwork,overview,percent} from './board-view.js';
import {clock,verdict} from './inspection-view.js';
import './board.css';

const base=admin=>'/api/'+(admin?'admin':'public');
const LEGEND=[['pass','通过'],['fail','答错'],['error','请求失败 / 不可用'],['empty','暂无数据']];

function useRecord(record,admin){
  const [detail,setDetail]=useState(null),[error,setError]=useState('');
  useEffect(()=>{
    if(!record)return;
    let alive=true;
    const load=()=>request(`${base(admin)}/records/${record.id}`).then(v=>{if(alive){setDetail(v);setError('');}}).catch(e=>{if(alive)setError(e.message);});
    setDetail(null);load();
    const timer=setInterval(load,300000); // render links expire after ten minutes
    return()=>{alive=false;clearInterval(timer);};
  },[record?.id,admin]);
  return [detail,error];
}

function RecordDialog({open,admin,onClose}){
  const dialog=useRef(null);
  const [detail,error]=useRecord(open?.record,admin);
  useLayoutEffect(()=>{if(open)dialog.current?.showModal();else dialog.current?.close();},[open]);
  const record=open?.record;
  return <dialog className="ib-dialog" ref={dialog} onClose={onClose} aria-label="检测详情">
    {record&&<>
      <header><div><strong>{open.name}</strong><span className="ib-mono">{record.model}</span><span>· {record.title}</span>
        <p>检测于 {clock(record.slot,true)} · 耗时 {record.duration??'—'} s · <span className={'ib-tone-text '+cellTone(record.status)}>{verdict(record)}</span></p></div>
        <button aria-label="关闭" onClick={()=>dialog.current.close()}><X size={18}/></button></header>
      {error&&<p className="ir-error" role="alert">{error}</p>}
      {admin&&record.account_id&&<p className="ir-note">检测账号 #{record.account_id} {record.account_name}{record.hidden_from_results?' · 本轮答错，结果页已对用户隐藏':''}</p>}
      <ResultBody record={record} detail={detail} loading={!detail&&!error} renderUrls={detail?.render_urls||[]} compact/>
    </>}
  </dialog>;
}

function ArtworkCard({entry,admin,onOpen}){
  const [detail]=useRecord(entry.art,admin);
  const url=detail?.render_urls?.[0];
  return <button className="ib-art" onClick={()=>onOpen({name:entry.group.name,record:entry.art})} aria-label={'查看 '+entry.group.name+' 的作品'}>
    <div className="ib-art-frame">{url?<ArtworkFrame url={url}/>:<span>正在加载作品…</span>}</div>
    <div className="ib-art-meta"><strong>{entry.group.name}</strong><span className="ib-mono">{entry.art.duration??'—'} s</span>
      <small className="ib-mono">{entry.art.model} · {clock(entry.art.slot)}</small></div>
  </button>;
}

function Strip({strip,name,onOpen}){
  const pad=Math.max(0,MIN_CELLS-strip.cells.length),last=strip.cells.length-1;
  return <div className="ib-strip">
    <div className="ib-strip-label"><span>{strip.title}</span>
      <strong className={'ib-mono ib-tone-text '+(strip.rate===null?'empty':strip.rate>=0.9?'pass':strip.rate>=0.7?'error':'fail')}>{percent(strip.rate)}</strong>
      <small className="ib-mono">{strip.passed}/{strip.samples} {strip.drawing?'画出':'答对'}</small></div>
    <div className="ib-cells" style={{gridTemplateColumns:`repeat(${pad+strip.cells.length},minmax(0,1fr))`}}>
      {Array.from({length:pad},(_,i)=><span key={'pad'+i} className="ib-cell empty"/>)}
      {strip.cells.map((cell,i)=>{
        const label=clock(cell.slot)+' · '+(cell.record?verdict(cell.record):'账号不可用');
        return cell.record?<button key={cell.id} className={'ib-cell '+cellTone(cell.status)+(i===last?' latest':'')} title={label} aria-label={label} onClick={()=>onOpen({name,record:cell.record})}/>
          :<span key={cell.id} className={'ib-cell '+cellTone(cell.status)+(i===last?' latest':'')} title={label}/>;
      })}
    </div>
  </div>;
}

function GroupCard({entry,onOpen,onHistory}){
  const {group,board}=entry;
  return <section className="ib-card" aria-label={group.name}>
    <header><h3>{group.name}</h3>{board.model&&<span className="ib-mono">{board.model}</span>}{board.effort&&<span className="ib-chip">推理 {board.effort}</span>}
      <span className={'ib-health '+board.health.tone}>{board.health.label}</span>
      <button className="ib-link" onClick={()=>onHistory(group)}><History size={14}/>全部记录</button></header>
    {board.strips.length?<>{board.strips.map(strip=><Strip key={strip.kind} strip={strip} name={group.name} onOpen={onOpen}/>)}
      <div className="ib-axis ib-mono"><span>{clock(board.firstSlot)}</span><span>最新</span></div></>
      :<p className="ib-none">这个时间范围内还没有巡检结果。</p>}
  </section>;
}

export default function InspectionBoard({groups,stamp,admin=false,historyHours=24}){
  const [histories,setHistories]=useState({}),[hours,setHours]=useState(24),[error,setError]=useState(''),[open,setOpen]=useState(null),[focus,setFocus]=useState(null);
  useEffect(()=>{
    let alive=true;
    Promise.allSettled(groups.map(g=>request(`${base(admin)}/groups/${g.id}/history`).then(v=>[g.id,v.runs]))).then(results=>{
      if(!alive)return;
      const ok=results.filter(r=>r.status==='fulfilled').map(r=>r.value);
      setHistories(Object.fromEntries(ok));
      const failed=results.find(r=>r.status==='rejected');
      setError(failed?failed.reason.message:'');
    });
    return()=>{alive=false;};
  },[stamp,admin,groups.map(g=>g.id).join()]);
  const entries=useMemo(()=>groups.map(group=>{
    const rows=histories[group.id]||[];
    return {group,rows,loaded:group.id in histories,board:groupBoard(rows,{hours}),art:latestArtwork(rows)};
  }),[groups,histories,hours]);
  if(focus)return <ResultViewer key={focus.id} group={focus} stamp={stamp} admin={admin} historyHours={historyHours}
    groupControls={<button onClick={()=>setFocus(null)}><ArrowLeft size={16}/>返回看板 · {focus.name}</button>}/>;
  const total=overview(entries.filter(e=>e.loaded).map(e=>e.board)),gallery=entries.filter(e=>e.art);
  const windows=[[24,'24h'],...(historyHours>24?[[Math.min(72,historyHours),Math.min(72,historyHours)>=72?'3d':historyHours+'h']]:[])];
  return <div className="ib-board">
    <div className="ib-toolbar"><div className="ib-range" role="group" aria-label="时间范围">{windows.map(([value,label])=><button key={value} aria-pressed={hours===value} className={hours===value?'active':''} onClick={()=>setHours(value)}>{label}</button>)}</div></div>
    {error&&<p className="ir-error" role="alert">{error}</p>}
    <div className="ib-stats">
      <div><span>标准答案题通过率 · 最近 {hours} 小时</span><strong className="ib-mono">{percent(total.rate)}</strong><small className="ib-mono">{total.passed}/{total.samples}</small></div>
      <div><span>监测分组</span><strong className="ib-mono">{total.groups}</strong><small><i className="ib-dot pass"/>{total.healthy} 个正常</small></div>
      <div><span>最近一轮</span><strong className="ib-mono">{total.last?clock(total.last).slice(-5):'—'}</strong><small className="ib-mono">{total.last?clock(total.last).slice(0,5):'暂无'}</small></div>
    </div>
    {gallery.length>0&&<section className="ib-gallery-wrap"><h2>最新作品</h2><div className="ib-gallery">{gallery.map(e=><ArtworkCard key={e.group.id} entry={e} admin={admin} onOpen={setOpen}/>)}</div></section>}
    <div className="ib-legend">{LEGEND.map(([tone,label])=><span key={tone}><i className={'ib-dot '+tone}/>{label}</span>)}</div>
    {entries.map(e=>e.loaded?<GroupCard key={e.group.id} entry={e} onOpen={setOpen} onHistory={setFocus}/>:<section key={e.group.id} className="ib-card"><p className="ib-none">正在加载 {e.group.name}…</p></section>)}
    <p className="ib-foot">一格代表一轮巡检，点击查看那一轮的回答或作品。状态按最新一轮判断；请求失败不算答错。</p>
    <RecordDialog open={open} admin={admin} onClose={()=>setOpen(null)}/>
  </div>;
}
