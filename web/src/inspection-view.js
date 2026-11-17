export const verdicts={pass:'通过',fail:'未通过',error:'请求失败',quota:'限额跳过',cancelled:'已取消',unknown:'无法判定',reference:'仅供参考',queued:'排队中',running:'检测中',completed:'已完成',completed_with_errors:'请求失败',failed:'执行失败',interrupted:'已中断'};
export const isDrawing=row=>row?.question_type==='drawing'||row?.kind==='drawing';
export const verdict=row=>isDrawing(row)&&row.status==='pass'?'生成完成':verdicts[row?.status]||row?.status||'等待检测';
export const clock=(value,full=false)=>value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,...(full?{}:{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}):'—';
export function batches(rows,question='',source='') {
  const groups=new Map();
  for(const row of rows) {
    if((question&&row.kind!==question)||(source&&row.source!==source))continue;
    const key=row.batch_id||row.id;
    if(!groups.has(key))groups.set(key,{id:key,slot:row.slot,source:row.source,rows:[]});
    groups.get(key).rows.push(row);
  }
  return [...groups.values()].sort((a,b)=>b.slot-a.slot);
}
export function selectedRecord(batch,model,kind,id) {
  if(!batch)return null;
  const rows=batch.rows.filter(row=>(!model||row.model===model)&&(!kind||row.kind===kind));
  return rows.find(row=>row.id===id)||rows[0]||null;
}
export const sourceName=value=>value==='user'?'用户 Key 检测':'定时巡检';
