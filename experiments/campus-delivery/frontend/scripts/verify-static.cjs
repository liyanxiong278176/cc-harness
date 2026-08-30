// 前端静态一致性校验(离线可运行)
// 1) 校验 App.jsx 中 lazy(() => import(...)) 引用的模块文件真实存在
// 2) 校验 src 下所有 import 相对路径可解析到真实文件
// 3) 输出文件统计
const fs = require('fs');
const path = require('path');

const SRC = path.resolve(__dirname, '..', 'src');
let failed = 0;

function collect(dir, out = []) {
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    const st = fs.statSync(full);
    if (st.isDirectory()) collect(full, out);
    else if (/\.(js|jsx)$/.test(name)) out.push(full);
  }
  return out;
}

const files = collect(SRC);

function resolveRelative(fromFile, spec) {
  const dir = path.dirname(fromFile);
  let p = path.resolve(dir, spec);
  if (!fs.existsSync(p)) {
    // 尝试扩展名
    for (const ext of ['.js', '.jsx', '/index.js', '/index.jsx']) {
      if (fs.existsSync(p + ext)) return true;
    }
    return false;
  }
  return true;
}

for (const f of files) {
  const code = fs.readFileSync(f, 'utf8');
  const re = /(?:import\s+[^'"]+|import\s*\(|from\s+)['"]([^'"]+)['"]/g;
  let m;
  while ((m = re.exec(code)) !== null) {
    const spec = m[1];
    if (!spec.startsWith('.')) continue; // 外部包 / 绝对路径忽略
    if (!resolveRelative(f, spec)) {
      failed += 1;
      console.error(`MISSING ${path.relative(SRC, f)} -> '${spec}'`);
    }
  }
}

console.log(`\n[verify-static] ${files.length} files scanned`);
if (failed === 0) console.log('[verify-static] all relative imports resolve');
else console.error(`[verify-static] ${failed} broken imports`);
process.exit(failed > 0 ? 1 : 0);
