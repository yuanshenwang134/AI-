/* 专项校验：模板里引用的名字，**不能只在 methods 里**（除非带了括号调用）。
 *
 * 为什么单独立这一条（真实事故，一个根因造成三个症状）：
 *   我把「4 点模式」的 miniPtsHere / miniPtsRight 写进了 `methods` 而不是 `computed`。
 *   模板里写的是 `miniPtsHere`（不带括号），Vue 解析到的是**函数对象本身**，于是：
 *     * `miniPtsHere.length` 读的是"函数的形参个数" = 0 → 槽位永远显示「未点」；
 *     * `v-for="p in miniPtsHere"` 遍历一个函数 → 0 次迭代 → **画面上不画任何圈**；
 *     * 点其实全都记进了数据（所以点满之后守卫会拦住下一次点击）。
 *   用户看到的是"点完 4 个啥也点不了了，而且没有圈"，完全对不上账。
 *   而 web/_validate.js 当时是**全绿**的 —— 这类错误它抓不到。
 *
 * 实现说明：**纯源码解析**，不 require 页面文件（那需要 window.Court / echarts 等
 * 一大套运行时；实测 tactics.js 的 data() 就会抛）。只按缩进抽 data/computed/methods
 * 三块的顶层键名，够用且不会因为环境缺失而误报。
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const FILES = ['web/pages/upload.js', 'web/pages/tactics.js', 'web/pages/overview.js',
               'web/pages/stats.js', 'web/pages/shotchart.js', 'web/pages/highlights.js',
               'web/pages/report.js', 'web/pages/review.js', 'web/pages/home.js',
               'web/pages/train.js'];

/** 抽出某个块（data/computed/methods）里的顶层键。
 *  data 的键在 6 空格缩进，computed/methods 在 4 空格。 */
function blockKeys(src, block, indent) {
  const start = src.indexOf('\n  ' + block + ':');
  if (start < 0) return new Set();
  const tail = src.slice(start + 1);
  // 截到下一个顶层块（两个空格缩进的 key:）
  const end = tail.search(/\n {2}[A-Za-z_$][\w$]*\s*[:(]/);
  const body = end > 0 ? tail.slice(0, end) : tail;
  const re = new RegExp('^ {' + indent + '}([A-Za-z_$][\\w$]*)\\s*[:(]', 'gm');
  const out = new Set();
  for (const m of body.matchAll(re)) out.add(m[1]);
  return out;
}

/** 把模板数组（'...' 拼接）还原成文本 */
function templateText(src) {
  const start = src.indexOf('\n  template:');
  if (start < 0) return '';
  const tail = src.slice(start);
  const end = tail.search(/\n {2}[A-Za-z_$][\w$]*\s*[:(]/);
  const body = end > 0 ? tail.slice(0, end) : tail;
  // 把每个 '...' 行里的内容抽出来
  const parts = [];
  for (const m of body.matchAll(/'((?:[^'\\]|\\.)*)'/g)) {
    parts.push(m[1].replace(/\\'/g, "'").replace(/\\\\/g, '\\'));
  }
  return parts.join('');
}

const problems = [];

for (const rel of FILES) {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) continue;
  const src = fs.readFileSync(abs, 'utf8');
  const data = blockKeys(src, 'data', 6);
  const computed = blockKeys(src, 'computed', 4);
  const methods = blockKeys(src, 'methods', 4);
  const tpl = templateText(src);
  if (!tpl) continue;

  // 只检查**当值用**的位置。两类要区分开：
  //   * `:sort-method="sortByFg"` 这类是**传函数引用**，方法本来就是对的
  //     （Element Plus 的 sort-method 就要求传函数）→ **不能报**；
  //   * `v-for="p in X"`（X 要可迭代）、`{{ X }}`、`X.length`（X 要取值）
  //     → 方法在这里必然出错 → 必须报。
  // 第一版没做这个区分，把 stats.js 的 sort-method 误报成了错误。
  const names = new Set();
  // ① v-for 的迭代对象（后面不能跟括号，那是调用）
  for (const m of tpl.matchAll(/v-for="[^"]*?\bin\s+([A-Za-z_$][\w$]*)\s*(?![\w$(])/g)) names.add(m[1]);
  // ② **任何**属性访问 X.something —— 明确是取值。
  //    第一版只列了 .length/.filter 这类白名单，结果漏掉 `sbRect.x0`
  //    （我自己新写的代码正好踩中，选择框永远画不出来）→ 改成匹配任意属性名。
  for (const m of tpl.matchAll(/([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)/g)) {
    if (['Math', 'JSON', 'Object', 'String', 'Number', 'Array', 'Date',
         'Boolean', 'RegExp', 'Promise'].includes(m[1])) continue;
    names.add(m[1]);
  }
  // ③ 插值 {{ X }}（纯名字，不带括号）
  for (const m of tpl.matchAll(/\{\{\s*!?\s*([A-Za-z_$][\w$]*)\s*(?![\w$(])/g)) names.add(m[1]);

  const bad = [];
  for (const n of names) {
    if (['true', 'false', 'null', 'undefined', 'Math', 'String', 'Number'].includes(n)) continue;
    if (data.has(n) || computed.has(n)) continue;
    if (methods.has(n)) bad.push(n);      // 只在 methods 里 → 模板拿到函数对象
  }
  if (bad.length) {
    problems.push(`${rel}: 模板把 methods 里的名字当值用了 → ` + bad.join('、') +
      '（应放进 computed：否则 .length 是形参个数、v-for 迭代 0 次）');
  }
}

if (problems.length) {
  console.log('\n=== 模板引用检查：发现问题 ===');
  problems.forEach((p) => console.log('  ✗ ' + p));
  process.exit(1);
}
console.log('模板引用检查：通过（没有把 methods 当 computed 用）');
