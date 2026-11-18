import React,{useEffect,useRef,useState} from 'react';
import {Code2,Copy,Pause,Play} from 'lucide-react';
import {request} from './console-client.js';
import {endpoint} from './endpoints.js';
import {batches,clock,isDrawing,selectedRecord,sourceName,verdict} from './inspection-view.js';

export function ResultBody({record,detail,loading=false,renderUrls=null}) {
  const [preview,setPreview]=useState(true),[code,setCode]=useState(false),[copied,setCopied]=useState(false),[copyError,setCopyError]=useState(''),[round,setRound]=useState(0);
  const textRef=useRef(null);
  useEffect(()=>{setPreview(true);setCode(false);setCopied(false);setCopyError('');setRound(0);},[record?.id]);
  const rounds=detail?.rounds||[],selected=rounds[round]||rounds[0],text=selected?.response||'',drawing=isDrawing(record);
  const bytes=new TextEncoder().encode(text).length;
  const expected=detail?.expected;
  async function copy(){try{await navigator.clipboard.writeText(text);setCopied(true);setCopyError('');}catch{setCode(true);setCopyError('浏览器未允许复制，请在源码区域选中后复制。');textRef.current?.focus();}}
  return <>
    <div className="ir-meta"><span>耗时 <b>{record.duration??'—'} s</b></span>{drawing&&text&&<span>源码 <b>{(bytes/1024).toFixed(1)} KB</b></span>}<span>思考强度 <b>{record.effort||'—'}</b></span><span className={'ir-status '+record.status}>{verdict(record)}</span></div>
    {detail?.prompt&&<p className="ir-prompt">题目：{detail.prompt}</p>}
    {rounds.length>1&&<div className="ir-models" aria-label="独立回答次数">{rounds.map((item,i)=><button key={i} aria-pressed={round===i} onClick={()=>{setRound(i);setCopied(false);}} className={round===i?'active':''}>第 {i+1} 次 · {verdict({...record,status:item.status})}</button>)}</div>}
    {loading?<div className="ir-empty" role="status">正在加载检测结果…</div>:record.status==='unavailable'?<div className="ir-empty">本轮可尝试的账号均不可用，未能完成检测。</div>:record.content_available===false?<div className="ir-empty">这条历史记录只公开了摘要。</div>:!text?<div className="ir-empty">{verdict(record)}，本次没有可展示的回答。请求失败不作能力判定。</div>:<>
      <div className="ir-tools">{drawing&&<button onClick={()=>setPreview(value=>!value)}>{preview?<Pause size={16}/>:<Play size={16}/ >}{preview?'停止预览':'播放预览'}</button>}<button onClick={()=>setCode(value=>!value)}><Code2 size={16}/>{code?'收起源码':drawing?'查看源码':'查看原始回答'}</button><button onClick={copy}><Copy size={16}/>{copied?'已复制':drawing?'复制源码':'复制回答'}</button></div>
      {copyError&&<p role="status">{copyError}</p>}
      {drawing?<>{preview?(!renderUrls?.[round]?<p role="status">正在准备作品预览…</p>:<iframe key={record.id+':'+round} className="ir-artwork" title="鹈鹕骑车检测作品" sandbox="allow-scripts" referrerPolicy="no-referrer" src={endpoint(renderUrls[round])}/>):<div className="ir-preview-stopped">预览已停止，播放时会从头开始。</div>}{code&&<pre ref={textRef} tabIndex="0" className="ir-code">{text}</pre>}<p className="ir-footnote">画面由本轮模型生成，供人工比较。“生成完成”只说明返回了完整 SVG，画面质量请以实际作品判断。</p></>:<><div className={'ir-verdict '+record.status}><strong>{verdict(record)}</strong>{expected!==null&&expected!==undefined&&<span>标准答案：{typeof expected==='object'?JSON.stringify(expected):String(expected)}</span>}{selected?.answer!==null&&selected?.answer!==undefined&&<span>识别答案：{String(selected.answer)}</span>}</div><pre tabIndex="0" className="ir-answer">{text}</pre>{code&&<pre ref={textRef} tabIndex="0" className="ir-code">{text}</pre>}</>}
    </>}
  </>;
}

export default function ResultViewer({group,stamp,admin=false}) {
  const [rows,setRows]=useState([]),[batchId,setBatchId]=useState(''),[model,setModel]=useState(''),[topic,setTopic]=useState(''),[source,setSource]=useState(''),[recordId,setRecordId]=useState(''),[detail,setDetail]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(true);
  useEffect(()=>{setRows([]);setBatchId('');setDetail(null);setError('');setLoading(true);},[group.id,admin]);
  useEffect(()=>{let alive=true;request(`/api/${admin?'admin':'public'}/groups/${group.id}/history`,'GET',undefined,admin).then(value=>{if(alive){setRows(value.runs);setError('');setLoading(false);}}).catch(e=>{if(alive){setError(e.message);setLoading(false);}});return()=>{alive=false;};},[group.id,stamp,admin]);
  const rounds=batches(rows,topic,source),batch=rounds.find(row=>row.id===batchId)||rounds[0];
  const models=[...new Set(batch?.rows.map(row=>row.model)||[])],activeModel=models.includes(model)?model:models[0];
  const current=selectedRecord(batch,activeModel,'',recordId),samples=batch?.rows.filter(row=>row.model===activeModel)||[];
  useEffect(()=>{let alive=true;async function load(){if(!current)return;try{const value=await request(`/api/${admin?'admin':'public'}/records/${current.id}`);if(alive){setDetail(value);setError('');}}catch(e){if(alive){setDetail(null);setError(e.message);}}}setDetail(null);load();const timer=setInterval(load,300000);return()=>{alive=false;clearInterval(timer);};},[current?.id,admin]);
  const topics=[...new Map(rows.map(row=>[row.kind,row.title||row.kind])).entries()];
  return <section className="ir-viewer"><header className="ir-viewer-head"><div><h2>降智检测 · {group.name}</h2><p>每轮独立会话 · 作品供人工判断 · 账号只对管理员展示</p></div><div className="ir-filters"><select aria-label="题目筛选" value={topic} onChange={e=>setTopic(e.target.value)}><option value="">全部题目</option>{topics.map(([id,title])=><option key={id} value={id}>{title}</option>)}</select><select aria-label="来源筛选" value={source} onChange={e=>setSource(e.target.value)}><option value="">全部来源</option><option value="admin">定时巡检</option><option value="user">用户 Key 检测</option></select></div></header>
    {error&&<p className="ir-error" role="alert">{error}</p>}
    {loading?<div className="ir-empty">正在加载检测记录…</div>:!rounds.length?<div className="ir-empty">{rows.length?'此筛选下没有记录。':'还没有检测结果，完成巡检后会在这里展示。'}</div>:<div className="ir-reader"><aside className="ir-timeline" aria-label="检测记录"><div className="ir-timeline-head"><strong>检测记录</strong><span>北京时间</span></div>{rounds.map((item,index)=><button key={item.id} aria-pressed={batch.id===item.id} className={'ir-round '+(batch.id===item.id?'active':'')} onClick={()=>{setBatchId(item.id);setRecordId('');}}><div><time>{clock(item.slot)}</time><small>{index===0?'最新一轮':sourceName(item.source)}</small></div>{item.rows.map(row=><div className="ir-round-line" key={row.id}><span className={'ir-dot '+row.status}/><span>{row.model}</span><b>{verdict(row)}</b><small>{row.duration??'—'}s</small></div>)}</button>)}</aside><article className="ir-result"><div className="ir-result-head"><div className="ir-models" aria-label="检测模型">{models.map(value=><button key={value} aria-pressed={activeModel===value} className={activeModel===value?'active':''} onClick={()=>{setModel(value);setRecordId('');}}>{value}</button>)}</div><span>开始 {clock(current?.slot,true)}{current?.finished_at&&' · 结束 '+clock(current.finished_at,true)}</span></div>{samples.length>1&&<select aria-label="选择检测结果" value={current?.id||''} onChange={e=>setRecordId(e.target.value)}>{samples.map((row,index)=><option key={row.id} value={row.id}>{row.title} · 样本 {index+1}</option>)}</select>}{current&&<><div className="ir-origin"><span>{sourceName(current.source)} · {current.title}</span>{admin&&current.account_id&&<strong>检测账号 #{current.account_id} {current.account_name}</strong>}</div>{current.hidden_from_results&&<p className="ir-note">该账号本轮有明确答错，仅管理员可见，结果页已隐藏。</p>}<ResultBody record={current} detail={detail} loading={!detail&&!error} renderUrls={detail?.render_urls||[]}/></>}</article></div>}
  </section>;
}
