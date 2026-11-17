import React,{useEffect,useState} from 'react';
import {request} from './console-client.js';

export default function ResultAccess(){
  const [document,setDocument]=useState(null),[anonymous,setAnonymous]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  useEffect(()=>{let alive=true;request('/api/admin/access').then(value=>{if(alive){setDocument(value);setAnonymous(value.value.allow_anonymous_results);}}).catch(e=>{if(alive)setError(e.message);});return()=>{alive=false;};},[]);
  async function save(){
    setBusy(true);setError('');setMessage('');
    try{
      const value=await request('/api/admin/access','PUT',{value:{allow_anonymous_results:anonymous},revision:document.revision});
      setDocument(value);setMessage(value.value.allow_anonymous_results?'已允许匿名查看分组结果和公开作品。':'已关闭匿名访问，查看结果和作品都需要 code1024 登录。');
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  return <section className="ir-panel"><h2>结果访问权限</h2><p>此开关对所有分组生效。默认关闭，已登录的 code1024 嵌入页面可正常查看。</p>{error&&<p className="ir-error" role="alert">{error}</p>}{message&&<p className="ir-message" role="status">{message}</p>}{document?<fieldset disabled={busy}><label className="ir-check"><input type="checkbox" checked={anonymous} onChange={e=>{setAnonymous(e.target.checked);setMessage('');}}/>允许匿名查看结果</label><p className="ir-note">开启后，未登录访客可以查看分组结果和已公开的回答、作品。巡检设置仍仅管理员可用，本人 Key 检测和私有记录始终需要登录。普通用户和访客都看不到巡检账号。</p><button className="ir-primary" disabled={anonymous===document.value.allow_anonymous_results} onClick={save}>{busy?'正在保存…':'保存访问权限'}</button></fieldset>:!error&&<div className="ir-empty">正在加载访问权限…</div>}</section>;
}
