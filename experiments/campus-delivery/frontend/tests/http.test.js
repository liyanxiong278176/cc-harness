// http.js 请求封装单测(离线 node:test,注入 fake fetch)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildUrl, parseResult, request, BizError } from '../src/api/http.js';

function okFetch(payload, status = 200) {
  return async () => new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

test('buildUrl: 拼接 query 并忽略空值', () => {
  assert.equal(buildUrl('/api', '/merchants', { page: 1, size: 10 }), '/api/merchants?page=1&size=10');
  assert.equal(buildUrl('/api', '/orders', { status: undefined, page: 2 }), '/api/orders?page=2');
  assert.equal(buildUrl('/api', '/orders', { page: 2, q: '' }), '/api/orders?page=2');
});

test('parseResult: code===0 返回 data', () => {
  assert.deepEqual(parseResult({ code: 0, message: 'ok', data: { a: 1 } }), { a: 1 });
});

test('parseResult: 非 0 抛 BizError(code/message)', () => {
  assert.throws(() => parseResult({ code: 5001, message: '库存不足' }), (e) => {
    assert.ok(e instanceof BizError);
    assert.equal(e.code, 5001);
    assert.equal(e.message, '库存不足');
    return true;
  });
});

test('parseResult: 异常载荷抛 BizError', () => {
  assert.throws(() => parseResult(null));
  assert.throws(() => parseResult({}));
});

test('request: 成功路径返回 data,自动携带 Bearer', async () => {
  const calls = [];
  const r = await request('/api/auth/me', {
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ code: 0, message: 'ok', data: { id: 1 } }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });
  assert.deepEqual(r, { id: 1 });
  assert.equal(calls[0].url, '/api/auth/me');
});

test('request: 401 抛 BizError 并清 token', async () => {
  await assert.rejects(
    () => request('/api/x', {
      fetchImpl: async () => new Response('{}', { status: 401 }),
    }),
    (e) => e instanceof BizError && e.code === 40101,
  );
});
