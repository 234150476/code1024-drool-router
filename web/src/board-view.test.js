import test from 'node:test';
import assert from 'node:assert/strict';
import {cellTone,groupBoard,latestArtwork,overview,percent} from './board-view.js';

const now=100000;
const row=(id,batch,slot,kind,status,extra={})=>({id,batch_id:batch,slot,kind,status,title:kind,model:'m',effort:'low',source:'admin',question_type:kind==='drawing'?'drawing':'number',...extra});

test('one cell per round and question, oldest first, user checks excluded',()=>{
  const rows=[row('a','r1',now-7200,'numeric','pass'),row('b','r1',now-7200,'drawing','pass'),
    row('c','r2',now-3600,'numeric','fail'),row('d','r2',now-3600,'drawing','pass'),
    row('u','u1',now-60,'numeric','fail',{source:'user'})];
  const board=groupBoard(rows,{now});
  const numeric=board.strips.find(s=>s.kind==='numeric');
  assert.deepEqual(numeric.cells.map(c=>c.status),['pass','fail']);
  assert.equal(numeric.rate,0.5);
  assert.equal(board.health.label,'疑似降智');
  assert.equal(board.strips.find(s=>s.kind==='drawing').drawing,true);
});

test('a retried round counts once and a confirmed wrong answer wins',()=>{
  const rows=[row('a','r1',now-60,'numeric','quota'),row('b','r1',now-60,'numeric','pass')];
  assert.deepEqual(groupBoard(rows,{now}).strips[0].cells.map(c=>c.status),['pass']);
  rows.push(row('c','r1',now-60,'numeric','fail'));
  assert.deepEqual(groupBoard(rows,{now}).strips[0].cells.map(c=>c.status),['fail']);
});

test('request errors are never reported as lower intelligence',()=>{
  const board=groupBoard([row('a','r1',now-60,'numeric','error')],{now});
  assert.equal(board.health.label,'请求异常');
  assert.equal(board.strips[0].samples,0);
  assert.equal(cellTone('unavailable'),'error');
  assert.equal(groupBoard([],{now}).health.label,'暂无数据');
});

test('an unavailable round marks every question strip of that round',()=>{
  const rows=[row('a','r1',now-7200,'numeric','pass'),row('e','r2',now-60,'inspection','unavailable')];
  const board=groupBoard(rows,{now});
  assert.equal(board.strips.length,1);
  assert.deepEqual(board.strips[0].cells.map(c=>c.status),['pass','unavailable']);
  assert.equal(board.strips[0].cells[1].record,null);
});

test('time window, overview and gallery choices',()=>{
  const rows=[row('old','r0',now-30*3600,'numeric','fail'),row('a','r1',now-60,'numeric','pass'),
    row('d','r1',now-60,'drawing','pass',{content_available:true})];
  const day=groupBoard(rows,{now,hours:24}),three=groupBoard(rows,{now,hours:72});
  assert.equal(day.strips[0].cells.length,1);assert.equal(three.strips[0].cells.length,2);
  const total=overview([day,three]);
  assert.deepEqual([total.passed,total.samples,total.healthy,total.groups],[2,3,2,2]);
  assert.equal(latestArtwork(rows).id,'d');
  assert.equal(percent(2/3),'66.7%');assert.equal(percent(null),'—');
});
