import test from 'node:test';
import assert from 'node:assert/strict';
import {defaultQuarantine,gradableQuestion,policyForQuestions,managedState,quarantineState} from './quarantine-policy.js';
test('automatic quarantine defaults off and only accepts enabled closed-answer questions',()=>{
  assert.equal(defaultQuarantine.enabled,false);
  for(const kind of ['number','choice','json'])assert.equal(gradableQuestion({kind,enabled:true}),true);
  for(const kind of ['drawing','reference',undefined])assert.equal(gradableQuestion({kind,enabled:true}),false);
  assert.equal(gradableQuestion({kind:'number',enabled:false}),false);
});
test('changing to a visual question disables quarantine and preserves the cooldown',()=>{
  const policy={enabled:true,cooldown_minutes:120};
  assert.deepEqual(policyForQuestions(policy,[{kind:'drawing',enabled:true}]),{enabled:false,cooldown_minutes:120,hide_failed_results:true});
  assert.equal(policyForQuestions(policy,[{kind:'number',enabled:true}]).enabled,true);
  assert.equal(policy.enabled,true);
});
test('closed entries cannot offer automatic control and blocked entries remain visible',()=>{
  assert.equal(managedState('manual'),false);assert.equal(managedState('recovered'),false);
  assert.equal(managedState('blocked'),true);assert.equal(quarantineState('blocked'),'需要管理员处理');
});

test('mixed questions keep quarantine available when at least one can be graded',()=>{assert.equal(policyForQuestions({enabled:true},[{kind:'drawing',enabled:true},{kind:'number',enabled:true}]).enabled,true);});
