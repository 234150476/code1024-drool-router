import React,{useEffect,useState} from 'react';
import {request} from './console-client.js';
import {artworkDocument} from './quality-content.js';

const names={pass:'通过',fail:'答错',error:'请求故障',quota:'限额跳过',unknown:'答案无法判定',reference:'仅参考',cancelled:'已取消'};
const when=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'—';
const source=s=>s==='user'?'用户 Key 自测':'管理员账号巡检';
export const isolatedDefaults={enabled:false,failure_rounds:2,cooldown_minutes:60,min_available:1};

export function Artwork({text}){
  const document=artworkDocument(text);
  return <iframe title="检测作品预览" sandbox="" referrerPolicy="no-referrer" srcDoc={document}/>;
}

export function GroupHistory({groupId,stamp}){
  const [runs,setRuns]=useState([]),[model,setModel]=useState(''),[origin,setOrigin]=useState(''),[selected,setSelected]=useState(''),[detail,setDetail]=useState(null),[error,setError]=useState(''),[code,setCode]=useState(false),[copied,setCopied]=useState(false);
  useEffect(()=>{let alive=true;setError('');request('/api/public/groups/'+groupId+'/history','GET',undefined,false).then(v=>{if(alive){setRuns(v.runs);setSelected(old=>v.runs.some(r=>r.id===old)?old:v.runs[0]?.id||'');}}).catch(e=>{if(alive)setError(e.message);});return()=>{alive=false;};},[groupId,stamp]);
  const filtered=runs.filter(r=>(!model||r.model===model)&&(!origin||r.source===origin));
  const current=filtered.find(r=>r.id===selected)||filtered[0];
  useEffect(()=>{let alive=true;setDetail(null);setCopied(false);if(current)request('/api/public/records/'+current.id,'GET',undefined,false).then(v=>{if(alive)setDetail(v);}).catch(e=>{if(alive)setError(e.message);});return()=>{alive=false;};},[current?.id,stamp]);
  const models=[...new Set(runs.map(r=>r.model))];
  const response=detail?.rounds?.map(r=>r.response||'').join('\n\n')||'';
  const drawing=current?.question_type==='drawing'||current?.kind==='drawing';
  return <div className="qv-history"><div className="qc-toolbar"><label className="qc-field"><span>模型</span><select value={model} onChange={e=>setModel(e.target.value)}><option value="">全部模型</option>{models.map(m=><option key={m}>{m}</option>)}</select></label><label className="qc-field"><span>检测来源</span><select value={origin} onChange={e=>setOrigin(e.target.value)}><option value="">全部来源</option><option value="admin">管理员账号巡检</option><option value="user">用户 Key 自测</option></select></label></div>
    {error&&<p role="alert">{error}</p>}
    {!runs.length?<div className="qc-empty">还没有公开的检测记录。</div>:<div className="qv-reader"><aside className="qv-timeline" aria-label="检测时间轴">{filtered.map(r=><button key={r.id} className={'qv-record '+(current?.id===r.id?'active':'')} onClick={()=>setSelected(r.id)}><small>{when(r.slot)}</small><strong>{r.title||r.kind}</strong><span>{r.model} · {source(r.source)}</span><span className={'qc-status '+r.status}>{names[r.status]||r.status}</span></button>)}</aside><article className="qv-result">{current?<><div className="qc-section-head"><div><h3>{current.title||current.kind}</h3><p>{current.model} · {current.effort} · {source(current.source)}</p></div><span className={'qc-status '+current.status}>{names[current.status]||current.status}</span></div><p>检测时间 {when(current.slot)} · 耗时 {current.duration??'—'} 秒 · 题目版本 {current.bank_version||'旧版'}</p>{drawing&&<p className="qc-note">画面供人工比较；“通过”仅表示返回了完整 SVG，不表示绘图能力已通过人工评估。</p>}{current.source==='user'&&<p className="qc-note">通过用户 Key 进入分组正常调度，不能据此判断具体上游账号。</p>}
      {current.content_available?<><div className="qc-inline qv-tools">{drawing&&<button onClick={()=>setCode(!code)}>{code?'预览作品':'查看源码'}</button>}<button disabled={!response} onClick={async()=>{try{await navigator.clipboard.writeText(response);setCopied(true);}catch{setError('复制失败，请在源码区域手动复制。');}}}>{copied?'已复制':'复制回答 / 源码'}</button></div>{!detail?<p role="status">正在加载作品与回答…</p>:response?drawing&&!code?<Artwork text={response}/>:<pre tabIndex="0">{response}</pre>:<div className="qc-empty">本次请求没有可展示的回答。请求故障不计为答错。</div>}</>:<div className="qc-empty">本条记录仅公开检测摘要，正文与作品未公开。</div>}</>:<div className="qc-empty">当前筛选没有记录。</div>}</article></div>}
  </div>;
}

export function QuestionEditor({bootstrap,onSaved,perform}){
  const [draft,setDraft]=useState(()=>structuredClone(bootstrap.questions.value)),[selected,setSelected]=useState(bootstrap.questions.value[0]?.id),[expected,setExpected]=useState('');
  useEffect(()=>setDraft(structuredClone(bootstrap.questions.value)),[bootstrap.questions.revision]);
  const q=draft.find(q=>q.id===selected)||draft[0];
  useEffect(()=>{if(q)setExpected(typeof q.expected==='object'?JSON.stringify(q.expected,null,2):String(q.expected));},[q?.id,q?.kind,bootstrap.questions.revision]);
  const change=(k,v)=>setDraft(rows=>rows.map(r=>r.id===q.id?{...r,[k]:v}:r));
  if(!q)return null;
  async function save(){await perform(async()=>{const value=draft.map(row=>{let parsed=row.id===q.id?expected:row.expected;if(row.kind==='json'&&typeof parsed==='string'){try{parsed=JSON.parse(parsed);}catch{throw Error(row.title+'：期望答案必须为有效 JSON 对象。');}}return {...row,expected:parsed};});await request('/api/admin/questions','PUT',{revision:bootstrap.questions.revision,value});await onSaved();},'统一题库已保存，新任务使用新版本，历史记录保留原题目快照。');}
  return <section className="qc-panel"><h2>统一题库</h2><p className="qc-note">管理员账号巡检与用户 Key 自测使用同一题库。作品和指纹只作参考，自动隔离仅采纳有标准答案的题。</p><div className="qc-toolbar"><label className="qc-field"><span>选择题目</span><select value={q.id} onChange={e=>{const val=expected;setDraft(rows=>rows.map(r=>r.id===q.id&&r.kind!=='json'?{...r,expected:val}:r));setSelected(e.target.value);}}>{draft.map(r=><option key={r.id} value={r.id}>{r.title}{r.enabled?'':'（停用）'}</option>)}</select></label><button onClick={()=>{const id='question-'+crypto.randomUUID().slice(0,8);setDraft([...draft,{id,title:'新题目',description:'',prompt:'请填写检测题目。',instructions:'严格遵循题目要求。',kind:'number',expected:'0',choices:[],unit:'',rounds:1,enabled:true}]);setSelected(id);}}>新增题目</button></div><div className="qc-grid"><label className="qc-field"><span>题目名称</span><input value={q.title} onChange={e=>change('title',e.target.value)}/></label><label className="qc-field"><span>判题类型</span><select value={q.kind} onChange={e=>{change('kind',e.target.value);}}>{[['number','数值答案'],['choice','选择题'],['json','JSON 对象'],['drawing','SVG / HTML 作品'],['reference','指纹 / 参考观察']].map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label></div><label className="qc-check"><input type="checkbox" checked={q.enabled} onChange={e=>change('enabled',e.target.checked)}/>启用此题</label><label className="qc-field"><span>题目说明</span><input value={q.description} onChange={e=>change('description',e.target.value)}/></label><label className="qc-field"><span>提示词</span><textarea rows="7" value={q.prompt} onChange={e=>change('prompt',e.target.value)}/></label><label className="qc-field"><span>系统指令</span><textarea rows="3" value={q.instructions} onChange={e=>change('instructions',e.target.value)}/></label>{['number','choice','json'].includes(q.kind)&&<label className="qc-field"><span>期望答案{q.kind==='json'?'（JSON 对象）':''}</span><textarea rows="3" value={expected} onChange={e=>{setExpected(e.target.value);change('expected',e.target.value);}}/></label>}{q.kind==='choice'&&<label className="qc-field"><span>单字符选项，以英文逗号分隔</span><input value={q.choices.join(',')} onChange={e=>change('choices',e.target.value.split(',').map(s=>s.trim()))}/></label>}<div className="qc-grid"><label className="qc-field"><span>独立回答次数（1—3）</span><input type="number" min="1" max="3" value={q.rounds} onChange={e=>change('rounds',Number(e.target.value))}/></label>{q.kind==='number'&&<label className="qc-field"><span>答案单位</span><input value={q.unit} onChange={e=>change('unit',e.target.value)}/></label>}</div><button className="primary" onClick={save}>保存统一题库</button></section>;
}

export function IsolationFields({value,onChange,ready}){
  const p=value||isolatedDefaults;
  return <><label className="qc-check"><input type="checkbox" checked={p.enabled} disabled={!ready} onChange={e=>onChange({...p,enabled:e.target.checked})}/>答题连续失败后自动设为不调度，冷却后复测</label><div className="qc-grid">{[['failure_rounds','连续失败轮数',1,10],['cooldown_minutes','冷却时间（分钟）',5,10080],['min_available','每组最低可用账号',1,100]].map(([k,t,min,max])=><label className="qc-field" key={k}><span>{t}</span><input type="number" min={min} max={max} value={p[k]} onChange={e=>onChange({...p,[k]:Number(e.target.value)})}/></label>)}</div><p className="qc-note">冷却期间保持不调度，复测标准答案题全部通过才恢复。模型不支持、网络故障、限额和指纹不触发质量隔离；外部修改或回执不明确时转管理员核对。</p></>;
}

export function AutomationPanel({bootstrap,onSaved,perform}){
  const [key,setKey]=useState('');
  const status={cooling:'冷却等待复测',pending:'写入待确认',review:'需要管理员核对',restored:'复测通过已恢复',released:'已由管理员接管'};
  return <section className="qc-panel"><h2>调度与自动化</h2><p className="qc-note">机器凭据供定时任务和重启后的冷却复测使用，保存在服务私有目录，界面不会回显。账号凭据导出仍遵循网关二次验证；若导出受保护，请在部署时配置受保护的本地账号凭据文件。</p><p>机器凭据：{bootstrap.capabilities.machine_credential_ready?'已配置':'未配置，定时与自动隔离不可启用'}</p><label className="qc-field"><span>网关管理员 API Key</span><input type="password" autoComplete="new-password" value={key} onChange={e=>setKey(e.target.value)} placeholder="填写后验证并保存"/></label><button disabled={!key} onClick={()=>perform(async()=>{await request('/api/admin/automation','PUT',{key});setKey('');await onSaved();},'机器凭据验证并保存成功。')}>验证并保存凭据</button><h3>账号隔离与恢复记录</h3>{bootstrap.isolations?.length?<div className="qc-table-wrap"><table><thead><tr><th>账号</th><th>状态</th><th>下次复测</th><th>提示</th><th>操作</th></tr></thead><tbody>{bootstrap.isolations.map(r=><tr key={r.account_id}><td>#{r.account_id}</td><td>{status[r.state]||r.state}</td><td>{when(r.due_at)}</td><td>{r.message||'—'}</td><td>{['cooling','review','pending'].includes(r.state)&&<button onClick={()=>perform(async()=>{await request('/api/admin/isolations/'+r.account_id+'/release','POST',{});await onSaved();},'已停止自动恢复。请在 code1024 管理页核对并手动处理账号。')}>接管此账号</button>}</td></tr>)}</tbody></table></div>:<p className="qc-note">暂无隔离记录。启用策略不会立即更改账号，需后续检测产生足够证据。</p>}</section>;
}
