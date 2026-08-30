// health.js 单测:验证健康检查 API 路径与响应解析(离线 node:test,注入 fake fetch)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { healthApi } from '../src/api/health.js';

test('healthApi.get: 请求 GET /api/health 并返回 data', async () => {
  const calls = [];
  const payload = {
    code: 0,
    message: 'success',
    data: {
      status: 'UP',
      components: { db: 'UP', redis: 'UP', rabbit: 'UP' },
      version: '1.0.0',
      time: '2026-08-29T00:00:00',
    },
  };
  const res = await healthApi.get({
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/health');
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(res.status, 'UP');
  assert.equal(res.components.db, 'UP');
});

test('healthApi.get: 组件 DOWN 仍返回 data(探针不抛业务错)', async () => {
  const res = await healthApi.get({
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          code: 0,
          message: 'success',
          data: { status: 'DOWN', components: { db: 'UP', redis: 'DOWN', rabbit: 'DOWN' } },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
  });
  assert.equal(res.status, 'DOWN');
  assert.equal(res.components.redis, 'DOWN');
});
