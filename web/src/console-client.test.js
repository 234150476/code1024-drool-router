import test from 'node:test';
import assert from 'node:assert/strict';
import {request, loadSelfData} from './console-client.js';

test('self-service uses host login even when legacy dashboard password exists',async()=>{
  const savedFetch=globalThis.fetch,savedStorage=globalThis.sessionStorage;
  globalThis.sessionStorage={getItem:key=>key==='drool-host-session'?'fixture.host.session':'legacy-password'};
  globalThis.fetch=async(url,options)=>{
    assert.equal(url,'/api/user/keys');
    assert.equal(options.headers.get('Authorization'),'Bearer fixture.host.session');
    assert.equal(options.headers.get('Accept'),'application/json');
    return {ok:true,status:200,json:async()=>({keys:[{id:7}]})};
  };
  try{assert.deepEqual(await request('/api/user/keys','GET',undefined,'user'),{keys:[{id:7}]});}
  finally{globalThis.fetch=savedFetch;globalThis.sessionStorage=savedStorage;}
});

test('HTML and error responses show actionable errors without exposing response bodies',async()=>{
  const savedFetch=globalThis.fetch;
  try{
    globalThis.fetch=async()=>({ok:false,status:502,json:async()=>{throw SyntaxError('private HTML');}});
    await assert.rejects(request('/api/user/keys','GET',undefined,false),e=>e.status===502&&e.message.includes('未返回有效 JSON')&&!e.message.includes('private'));
    globalThis.fetch=async()=>({ok:false,status:401,json:async()=>({error:'需要重新登录'})});
    await assert.rejects(request('/api/user/keys','GET',undefined,false),e=>e.status===401&&e.message==='需要重新登录');
  }finally{globalThis.fetch=savedFetch;}
});

test('a broken key endpoint does not discard available questions or history',async()=>{
  const savedFetch=globalThis.fetch;
  globalThis.fetch=async url=>({ok:!url.endsWith('/keys'),status:url.endsWith('/keys')?502:200,
    json:async()=>url.endsWith('/keys')?{error:'Key 清单加载失败'}:url.endsWith('/questions')?{questions:[{id:'candy'}]}:{jobs:[]}});
  try{
    const data=await loadSelfData();
    assert.equal(data.keys.status,'rejected');
    assert.deepEqual(data.questions.value,{questions:[{id:'candy'}]});
    assert.deepEqual(data.jobs.value,{jobs:[]});
  }finally{globalThis.fetch=savedFetch;}
});

test('result requests keep the host login even through the old public audience flag',async()=>{
  const savedFetch=globalThis.fetch,savedStorage=globalThis.sessionStorage;
  globalThis.sessionStorage={getItem:key=>key==='drool-host-session'?'fixture.user.session':'legacy-password'};
  globalThis.fetch=async(url,options)=>{
    assert.equal(options.headers.get('Authorization'),'Bearer fixture.user.session');
    assert.equal(options.credentials,'omit');
    assert.equal(options.cache,'no-store');
    return {ok:true,status:200,json:async()=>({groups:[]})};
  };
  try{await request('/api/public/overview','GET',undefined,false);}
  finally{globalThis.fetch=savedFetch;globalThis.sessionStorage=savedStorage;}
});

test('anonymous result requests never use the legacy dashboard credential',async()=>{
  const savedFetch=globalThis.fetch,savedStorage=globalThis.sessionStorage;
  globalThis.sessionStorage={getItem:key=>key==='drool-dashboard-access'?'legacy-password':null};
  globalThis.fetch=async(url,options)=>{
    assert.equal(options.headers.has('Authorization'),false);
    return {ok:true,status:200,json:async()=>({groups:[]})};
  };
  try{await request('/api/public/overview');}
  finally{globalThis.fetch=savedFetch;globalThis.sessionStorage=savedStorage;}
});
