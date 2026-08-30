// 前端 JSX/JS 语法校验(离线可运行)
// 使用系统自带的 @babel/core + preset-react(位于 /usr/share/nodejs)
// 无需 npm install:直接以绝对路径 require,校验 frontend/src 下全部 .js/.jsx
const fs = require('fs');
const path = require('path');

const babel = require('/usr/share/nodejs/@babel/core');
const presetReact = require('/usr/share/nodejs/@babel/preset-react');

const ROOT = path.resolve(__dirname, '..', 'src');

function collect(dir, out = []) {
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    const st = fs.statSync(full);
    if (st.isDirectory()) {
      collect(full, out);
    } else if (/\.(js|jsx)$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

const files = collect(ROOT);
let failed = 0;

for (const f of files) {
  const code = fs.readFileSync(f, 'utf8');
  try {
    babel.transformSync(code, {
      filename: f,
      presets: [[presetReact, { runtime: 'automatic' }]],
      configFile: false,
      babelrc: false,
    });
    console.log(`OK   ${path.relative(ROOT, f)}`);
  } catch (e) {
    failed += 1;
    console.error(`FAIL ${path.relative(ROOT, f)}: ${e.message.split('\n')[0]}`);
  }
}

console.log(`\n[verify-syntax] ${files.length - failed}/${files.length} files parsed OK`);
process.exit(failed > 0 ? 1 : 0);
