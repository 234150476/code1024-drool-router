import test from 'node:test';
import assert from 'node:assert/strict';
import {batches,selectedRecord,verdict} from './inspection-view.js';

test('a round groups samples and models without using account identities',()=>{
  const rows=[{id:'a',batch_id:'round1',slot:10,model:'sol',kind:'drawing',source:'admin'},{id:'b',batch_id:'round1',slot:10,model:'astra',kind:'drawing',source:'admin'},{id:'c',batch_id:'round2',slot:20,model:'sol',kind:'candy',source:'user'}];
  assert.deepEqual(batches(rows).map(v=>v.id),['round2','round1']);
  assert.equal(batches(rows,'drawing','admin')[0].rows.length,2);
  assert.equal(selectedRecord(batches(rows)[1],'astra','drawing','a').id,'b');
  assert.equal(selectedRecord(batches(rows)[1],'missing','','a'),null);
});
test('artwork generation never claims a quality pass and request errors are distinct',()=>{
  assert.equal(verdict({question_type:'drawing',status:'pass'}),'生成完成');
  assert.equal(verdict({question_type:'number',status:'pass'}),'通过');
  assert.equal(verdict({question_type:'drawing',status:'error'}),'请求失败');
});
