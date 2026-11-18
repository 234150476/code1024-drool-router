import React,{useEffect,useState} from 'react';
import {request} from './console-client.js';
import {clock} from './inspection-view.js';
import {gradableQuestion,managedState,quarantineState,quarantineError} from './quarantine-policy.js';

export function QuarantinePolicy({value,questions,onChange,keyReady}){
  const supported=questions.some(gradableQuestion);
  return <div className="ir-own-result"><h3>明确答错自动剔除</h3>
    <label className="ir-check"><input type="checkbox" checked={value.enabled} disabled={!supported||!keyReady} onChange={e=>onChange({...value,enabled:e.target.checked})}/>启用此分组的自动剔除（默认关闭）</label>
    {!supported&&<p className="ir-note">所选题目均需要人工观察，不能自动剔除。仅数值、选择和 JSON 标准答案题可以启用。</p>}
    {!keyReady&&<p className="ir-note">请先在下方配置专用调度 Key，再启用此开关。</p>}
    <label className="ir-field"><span>失败后冷却（分钟）</span><input type="number" min="5" max="10080" disabled={!value.enabled} value={value.cooldown_minutes} onChange={e=>onChange({...value,cooldown_minutes:Number(e.target.value)})}/></label>
    <p className="ir-note">所选标准答案题中任意一题明确答错即可剔除，不要求其他题目全部完成或失败；请求错误、超时、限额及用户 Key 检测均不剔除。剔除会暂停该账号在所有所属分组的调度。冷却后直接复测，原标准答案题全部通过才恢复；失败继续冷却。复测沿用原题目、模型和思考强度，并遵守暂停时段。</p>
    <label className="ir-check"><input type="checkbox" checked={value.hide_failed_results} onChange={e=>onChange({...value,hide_failed_results:e.target.checked})}/>隐藏明确答错账号本轮的全部结果（含作品）</label>
    <p className="ir-note">隐藏对开启自动剔除的巡检生效，公开页的历史、统计、回答和作品同时隐藏；管理员仍可查看。此展示开关修改后即时生效。</p>
    <p className="ir-note">关闭开关或停用分组巡检会停止自动处理，已剔除账号保持原状态；重新启用后继续复测，也可停止托管并到网关人工处理。</p>
  </div>;
}

export default function QuarantineAdmin({onCredentialChange}){
  const [data,setData]=useState(null),[key,setKey]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  async function load(){setData(await request('/api/admin/quarantines'));}
  useEffect(()=>{load().catch(e=>setError(e.message));const timer=setInterval(()=>load().catch(e=>setError(e.message)),15000);return()=>clearInterval(timer);},[]);
  async function act(fn,success){setBusy(true);setError('');setMessage('');try{await fn();await load();setMessage(success);}catch(e){setError(e.message);}finally{setBusy(false);}}
  return <section className="ir-panel"><h2>自动剔除管理</h2><p>仅管理员可见。每个分组独立启用；没有启用的分组只检测、展示结果。</p>
    {error&&<p className="ir-error" role="alert">{error}</p>}{message&&<p className="ir-message" role="status">{message}</p>}
    <details className="ir-credentials"><summary>专用调度凭据 · {data?.key_ready?'已配置':'待配置'}</summary>
      <p>使用单独的受限管理员 Key，至少需要 accounts.read、accounts.status；范围仅授权需要检测的分组和账号。原定时巡检 Key 可继续保持只读。</p>
      <p className="ir-note">网关将调度开关归于 accounts.status；accounts.schedule 用于并发、优先级与有效期，不能替代该权限。服务始终只修改 schedulable。</p>
      <label className="ir-field"><span>专用调度 API Key</span><input type="password" autoComplete="new-password" value={key} onChange={e=>setKey(e.target.value)} placeholder="保存后不回显"/></label>
      <p className="ir-note">保存时仅验证读取权限，不修改真实账号；调度写权限在实际执行时确认，权限不足会显示错误并停止该账号的自动处理。</p>
      <button disabled={busy||!key} onClick={()=>act(async()=>{await request('/api/admin/quarantine-key','PUT',{key});setKey('');await onCredentialChange();},'专用 Key 已保存，尚未修改任何账号。')}>验证读取并保存</button>
    </details>
    {!data?<p>正在加载剔除记录…</p>:!data.entries.length?<p className="ir-note">暂无自动剔除记录。</p>:data.entries.map(row=><div className="ir-own-result" key={row.account_id}><h3>{row.account?.name||'账号'} · #{row.account_id}</h3>
      <p>{quarantineState(row.state)}{managedState(row.state)&&!row.policy_enabled?' · 策略已暂停，保持当前调度状态':''}{row.due_at&&managedState(row.state)?' · 下次检查 '+clock(row.due_at):''}</p>
      {row.error_code&&<p className="ir-error">{quarantineError(row.error_code)}{quarantineError(row.error_code)!==row.error_code?'（'+row.error_code+'）':''}</p>}
      {row.state==='blocked'&&<button disabled={busy||!row.policy_enabled} onClick={()=>act(()=>request(`/api/admin/quarantines/${row.account_id}/retry`,'POST',{}),'已重新核验并尝试处理，请查看账号状态；未重新请求模型。')}>修复后核验并重试</button>}
      {managedState(row.state)&&<button disabled={busy} onClick={()=>act(()=>request(`/api/admin/quarantines/${row.account_id}/handover`,'POST',{}),'已停止自动托管，账号当前调度状态保持不变；请到网关人工处理。')}>停止托管（保持当前调度状态）</button>}
    </div>)}
    {data?.events.length>0&&<details><summary>最近处理记录</summary>{data.events.map((event,index)=><p key={index}>{clock(event.created_at)} · {event.action} · {event.object_id}</p>)}</details>}
  </section>;
}
