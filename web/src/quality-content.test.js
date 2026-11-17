import test from 'node:test';
import assert from 'node:assert/strict';
import {artworkDocument} from './quality-content.js';

test('model markup is behind a restrictive policy before any generated content',()=>{
  const result=artworkDocument('```html\n<svg><script>fetch("https://example.invalid")</script></svg>\n```');
  assert.ok(result.indexOf('Content-Security-Policy')<result.indexOf('<svg>'));
  assert.ok(result.includes("default-src 'none'"));
  assert.ok(result.includes("form-action 'none'"));
  assert.ok(!result.includes('```'));
  assert.ok(!result.includes("script-src 'unsafe-inline'"));
});
