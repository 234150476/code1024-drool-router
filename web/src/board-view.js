// Turns per-group history rows into the dashboard model: one strip per
// question, one cell per inspection round. Pure functions, no network.
import {isDrawing} from './inspection-view.js';

export const MIN_CELLS=24;
const SEVERITY={fail:4,pass:3,error:2,quota:2,unavailable:2,unknown:1,reference:1,cancelled:0};
export const cellTone=status=>status==='pass'?'pass':status==='fail'?'fail':
  ['error','quota','unavailable'].includes(status)?'error':status?'muted':'empty';

// Several rows of one round (retried accounts, repeated samples) collapse
// into the most decisive outcome: a confirmed wrong answer always wins.
function merge(rows){
  return rows.reduce((best,row)=>(SEVERITY[row.status]??0)>(SEVERITY[best.status]??0)?row:best);
}

export function groupBoard(rows,{hours=24,now=Date.now()/1000}={}){
  const since=now-hours*3600;
  // Admins also receive rows hidden by quarantine (e.g. rechecks of a paused
  // account); the board shows what users see, 全部记录 keeps the full view.
  const own=rows.filter(row=>row.source!=='user'&&!row.hidden_from_results&&row.slot>=since);
  const rounds=new Map();
  for(const row of own){
    const key=row.batch_id||row.id;
    if(!rounds.has(key))rounds.set(key,{id:key,slot:row.slot,rows:[]});
    rounds.get(key).rows.push(row);
  }
  const ordered=[...rounds.values()].sort((a,b)=>a.slot-b.slot);
  const questions=new Map();
  for(const row of own){
    if(row.kind==='inspection'||questions.has(row.kind))continue;
    questions.set(row.kind,{kind:row.kind,title:row.title||row.kind,drawing:isDrawing(row)});
  }
  const strips=[...questions.values()].sort((a,b)=>a.drawing-b.drawing).map(question=>{
    const cells=ordered.map(round=>{
      const mine=round.rows.filter(row=>row.kind===question.kind);
      const empty=round.rows.find(row=>row.kind==='inspection');
      const row=mine.length?merge(mine):empty;
      return row?{id:row.id,status:row.status,slot:round.slot,record:row.kind==='inspection'?null:row}:null;
    }).filter(Boolean);
    const passed=cells.filter(c=>c.status==='pass').length;
    const samples=cells.filter(c=>c.status==='pass'||c.status==='fail').length;
    return {...question,cells,passed,samples,rate:samples?passed/samples:null};
  });
  const latest=[...own].sort((a,b)=>b.slot-a.slot).find(row=>row.model);
  return {strips,model:latest?.model||'',effort:latest?.effort||'',lastSlot:ordered.at(-1)?.slot??null,
    firstSlot:ordered[0]?.slot??null,health:health(strips)};
}

// Health follows the newest round only: one old miss should not keep a
// group red for a whole day, and request failures are never "less smart".
export function health(strips){
  const latest=strips.map(s=>s.cells.at(-1)).filter(Boolean);
  const graded=strips.filter(s=>!s.drawing).map(s=>s.cells.at(-1)).filter(Boolean);
  if(graded.some(c=>c.status==='fail'))return {tone:'fail',label:'疑似降智'};
  if(graded.some(c=>c.status==='pass'))return {tone:'pass',label:'智力正常'};
  if(latest.some(c=>c.status==='pass'))return {tone:'pass',label:'运行正常'};
  if(latest.length)return {tone:'error',label:'请求异常'};
  return {tone:'empty',label:'暂无数据'};
}

export function overview(boards){
  let passed=0,samples=0,last=null;
  for(const board of boards){
    for(const strip of board.strips.filter(s=>!s.drawing)){passed+=strip.passed;samples+=strip.samples;}
    if(board.lastSlot&&(!last||board.lastSlot>last))last=board.lastSlot;
  }
  return {passed,samples,rate:samples?passed/samples:null,last,
    healthy:boards.filter(b=>b.health.tone==='pass').length,groups:boards.length};
}

// Newest generated artwork of a group, for the gallery on top.
export function latestArtwork(rows){
  return rows.filter(row=>row.source!=='user'&&isDrawing(row)&&row.status==='pass'&&row.content_available!==false)
    .sort((a,b)=>b.slot-a.slot)[0]||null;
}

export const percent=value=>value===null?'—':(Math.round(value*1000)/10)+'%';
