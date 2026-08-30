#!/usr/bin/env node
/**
 * verify-api-contract.cjs — 离线接口契约三方交叉核对(静态)
 * 对比对象:
 *   1) campus-web 控制器注解端点(后端事实)
 *   2) frontend/src/api/*.js 中的 http 调用(前端事实)
 *   3) docs/api.md 表格中记录的端点(文档事实)
 * 归一化: 去掉 /api 前缀、模板变量 ${x}/{x} -> {param}、合并重复斜杠。
 * 用法: node scripts/verify-api-contract.cjs   (在仓库根目录运行)
 * 退出码: 0=全部一致; 1=存在差异(输出明细)。
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
let errors = 0;

function walk(dir, ext) {
  if (!fs.existsSync(dir)) return [];
  const out = [];
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f);
    const st = fs.statSync(p);
    if (st.isDirectory()) out.push(...walk(p, ext));
    else if (f.endsWith(ext)) out.push(p);
  }
  return out;
}

function norm(p) {
  return p
    .replace(/^\//, '')
    .replace(/^api\//, '')
    .replace(/\$\{[^}]+\}/g, '{param}')
    .replace(/\{[^}]+\}/g, '{param}')
    .replace(/\/+/g, '/')
    .replace(/\/$/, '');
}

function toKey(method, p) {
  return method.toUpperCase() + ' /' + norm(p);
}

/* ---------- 1) backend: 控制器注解 ---------- */
const be = new Set();
for (const file of walk(path.join(ROOT, 'campus-web/src/main/java'), '.java')) {
  if (!file.includes('Controller')) continue;
  const s = fs.readFileSync(file, 'utf8');
  const cls = /@RequestMapping\(\s*(?:value\s*=\s*)?["']([^"']+)["']/.exec(s);
  const base = cls ? cls[1] : '';
  // 显式带路径的映射
  for (const m of s.matchAll(/@(Get|Post|Put|Delete)Mapping\(\s*(?:value\s*=\s*)?["']([^"']*)["']/g)) {
    be.add(toKey(m[1], base + m[2]));
  }
  // 裸映射(无 path 参数): @GetMapping / @PostMapping 等
  for (const m of s.matchAll(/@(Get|Post|Put|Delete)Mapping(?!\s*\()/g)) {
    be.add(toKey(m[1], base));
  }
}

/* ---------- 2) frontend: src/api/*.js ---------- */
const fe = new Set();
for (const file of walk(path.join(ROOT, 'frontend/src/api'), '.js')) {
  const s = fs.readFileSync(file, 'utf8');
  for (const m of s.matchAll(/http\.(get|post|put|delete)\(\s*[`'"]([^`'"]+)[`'"]/g)) {
    fe.add(toKey(m[1], m[2]));
  }
}

/* ---------- 3) docs: api.md 表格 ---------- */
const doc = new Set();
const apiMd = path.join(ROOT, 'docs/api.md');
if (fs.existsSync(apiMd)) {
  for (const line of fs.readFileSync(apiMd, 'utf8').split('\n')) {
    const m = /^\|\s*(GET|POST|PUT|DELETE)(?:\/(GET|POST|PUT|DELETE))?\s*\|\s*(\/[^|\s]+)/.exec(line);
    if (!m) continue;
    doc.add(toKey(m[1], m[3]));
    if (m[2]) doc.add(toKey(m[2], m[3]));
  }
}

function diff(nameA, a, nameB, b) {
  const onlyA = [...a].filter((x) => !b.has(x)).sort();
  const onlyB = [...b].filter((x) => !a.has(x)).sort();
  if (!onlyA.length && !onlyB.length) {
    console.log(`[ok] ${nameA} <-> ${nameB}: ${a.size} 端点完全一致`);
    return;
  }
  errors++;
  console.log(`[DIFF] ${nameA}(${a.size}) <-> ${nameB}(${b.size}):`);
  for (const x of onlyA) console.log(`  仅在 ${nameA}: ${x}`);
  for (const x of onlyB) console.log(`  仅在 ${nameB}: ${x}`);
}

console.log(`backend 控制器端点: ${be.size}`);
console.log(`frontend api 调用:  ${fe.size}`);
console.log(`docs/api.md 端点:   ${doc.size}`);
console.log('---');
diff('backend', be, 'frontend', fe);
diff('backend', be, 'docs', doc);
diff('frontend', fe, 'docs', doc);

if (errors) {
  console.error(`\n[verify-api-contract] FAIL: ${errors} 组差异`);
  process.exit(1);
}
console.log('\n[verify-api-contract] PASS: 三方接口契约一致');
