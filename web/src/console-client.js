import {adminFetch} from './access.js';
import {endpoint} from './endpoints.js';

// Result, administrator and self-service requests all carry the host session.
// The legacy dashboard password is never a code1024 user credential.
export async function request(path, method='GET', body) {
  const options={method,headers:{Accept:'application/json'},cache:'no-store'};
  if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await adminFetch(endpoint(path),options);
  let value;
  try { value=await response.json(); }
  catch {
    const error=Error(`检测接口未返回有效 JSON（HTTP ${response.status}），请刷新页面；若仍失败，请联系管理员检查服务转发。`);
    error.status=response.status;
    throw error;
  }
  if(!value || typeof value!=='object' || Array.isArray(value)) throw Error('检测接口返回的数据格式无效');
  if(!response.ok){const error=Error(value.error||`检测请求失败（HTTP ${response.status}）`);error.status=response.status;throw error;}
  return value;
}

export async function loadSelfData() {
  const names=['keys','questions','jobs'];
  const values=await Promise.allSettled(names.map(name=>request('/api/user/'+name,'GET',undefined,'user')));
  return Object.fromEntries(values.map((result,index)=>[names[index],result]));
}
