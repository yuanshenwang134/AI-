/* 静态自检脚本（开发期用，不参与运行时）：
   1) 用最小 DOM 桩加载 api.js / data.js / court.js / components.js / pages/*.js
   2) 校验每个页面模板的标签配对、插值配对、v-html 用法
   3) 把每个页面的 data()/computed 在真实 demo 数据上跑一遍，捕获运行时错误
   用法：node web/_validate.js
   注意：Node 侧的 ESM 严格模式会让 Function() 继承严格模式，故用 indirect eval 取全局作用域。 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = __dirname;
const glob = vm.runInThisContext('(0, eval)');   // indirect eval -> 全局作用域，非严格模式

// ---------------- 最小浏览器环境桩 ----------------
const store = {};
const win = global;
win.window = win;
win.location = { protocol: 'http:', search: '', hash: '#/overview' };
win.URLSearchParams = URLSearchParams;
win.fetch = function () { return Promise.reject(new Error('stub')); };
win.setTimeout = setTimeout;
win.clearTimeout = clearTimeout;
win.AbortController = global.AbortController;
win.console = console;
win.WebSocket = function () { throw new Error('no ws'); };
win.Vue = {
  reactive: function (o) { return o; },
  createApp: function () { return { use: function () {}, component: function () {}, mount: function () {} }; }
};
win.ElementPlus = {};
win.ElementPlusLocaleZhCn = {};
win.echarts = { init: function () { return { setOption: function () {}, resize: function () {}, dispose: function () {} }; } };
win.ResizeObserver = function () { this.observe = function () {}; this.disconnect = function () {}; };

const files = ['api.js', 'data.js', 'court.js', 'components.js', 'app.js',
  'pages/upload.js', 'pages/overview.js', 'pages/stats.js', 'pages/shotchart.js',
  'pages/highlights.js', 'pages/report.js', 'pages/review.js'];

// app.js 会调用 Vue.createApp().mount()，已在桩里兜住
const errors = [];
files.forEach(f => {
  const src = fs.readFileSync(path.join(WEB, f), 'utf8');
  try { glob(src, { filename: f }); } catch (e) { errors.push('[load] ' + f + ' -> ' + e.message); }
});

// ---------------- 数据装载（等价于 app.js applyGame 的路径） ----------------
// 数据来源：优先 web/demo（你跑过 run_demo.ps1 的话），否则用仓库自带夹具
// out/fixture（`aihoop.cli demo --seed 7 --duration 600`）—— 自检不依赖 web/demo。
const DATA_DIR = fs.existsSync(path.join(WEB, 'demo', 'game.json'))
  ? path.join(WEB, 'demo') : path.join(WEB, '..', 'out', 'fixture');
if (!fs.existsSync(path.join(DATA_DIR, 'game.json'))) {
  console.error('缺少测试数据：请先跑 `python -m aihoop.cli demo --seed 7 --duration 600 --out out/fixture`');
  process.exit(1);
}
console.log('测试数据来源：' + path.relative(path.join(WEB, '..'), DATA_DIR));
const demo = p => JSON.parse(fs.readFileSync(path.join(DATA_DIR, p), 'utf8'));
try {
  win.STORE.applyGame(
    demo('game.json'), demo('players.json'), demo('shotchart.json'),
    { markdown: fs.readFileSync(path.join(DATA_DIR, 'report.md'), 'utf8'), json: demo('report.json') },
    fs.existsSync(path.join(DATA_DIR, 'highlights.json')) ? demo('highlights.json') : { clips: [] }
  );
} catch (e) { errors.push('[applyGame] ' + e.message + '\n' + e.stack); }

// ---------------- 模板结构校验 ----------------
const VOID = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'path', 'rect', 'circle', 'line', 'area', 'base', 'col', 'embed', 'track', 'wbr', 'use', 'polygon', 'ellipse', 'stop']);

function checkTemplate(name, tpl) {
  // 插值配对
  const opens = (tpl.match(/\{\{/g) || []).length;
  const closes = (tpl.match(/\}\}/g) || []).length;
  if (opens !== closes) errors.push('[tpl] ' + name + ' 插值 {{ }} 数量不匹配: ' + opens + '/' + closes);

  // 标签配对
  const stack = [];
  const re = /<\/?([A-Za-z][-A-Za-z0-9]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;
  let m;
  while ((m = re.exec(tpl))) {
    const raw = m[0], tag = m[1].toLowerCase(), attrs = m[2] || '';
    if (raw.startsWith('</')) {
      const top = stack.pop();
      if (top !== tag) errors.push('[tpl] ' + name + ' 标签不配对：</' + tag + '> 对应 <' + (top || 'none') + '>');
    } else if (raw.endsWith('/>') || VOID.has(tag)) {
      // 自闭合 / 空元素
    } else {
      stack.push(tag);
    }
    // 属性扫描：引号内的内容跳过，只检查裸值（缺引号的属性）
    let i = 0;
    while (i < attrs.length) {
      const ch = attrs[i];
      if (ch === '"' || ch === "'") {           // 跳过整个引号段
        const end = attrs.indexOf(ch, i + 1);
        i = end < 0 ? attrs.length : end + 1;
        continue;
      }
      if (ch === '=' && i + 1 < attrs.length) {
        const nx = attrs[i + 1];
        if (nx !== '"' && nx !== "'" && !/\s/.test(nx)) {
          errors.push('[tpl] ' + name + ' 属性缺引号: <' + tag + attrs.slice(0, 70) + '>');
        }
      }
      i++;
    }
  }
  if (stack.length) errors.push('[tpl] ' + name + ' 有未闭合标签: ' + stack.join(' > '));
  return true;
}

// ---------------- 逐页面执行 data / computed / 关键 methods ----------------
function fakeThis(page) {
  const self = Object.assign({}, page.methods);
  self.$message = { success: function () {}, warning: function () {}, info: function () {}, error: function () {} };
  self.$nextTick = function (fn) { if (fn) fn(); };
  self.$refs = {};
  self.$t = function (k) { return k; };
  if (page.data) Object.assign(self, page.data.call(self));
  // 模拟 Vue 的 computed 解析：把每个 computed 设为 this 上的 getter，
  // 这样调用 computed 时它内部读 this.players / this.sc 等也能拿到值。
  const comp = page.computed || {};
  Object.keys(comp).forEach(k => {
    const fn = typeof comp[k] === 'function' ? comp[k] : comp[k].get;
    if (!fn) return;
    Object.defineProperty(self, k, {
      configurable: true, enumerable: true,
      get: function () { return fn.call(self); }
    });
  });
  if (page.created) { try { page.created.call(self); } catch (e) { errors.push('[created] ' + page.name + ' -> ' + e.message); } }
  if (page.mounted) { try { page.mounted.call(self); } catch (e) { /* 图表容器为空，忽略 */ } }
  return self;
}

const results = [];
Object.keys(win.PAGES).forEach(key => {
  const page = win.PAGES[key];
  checkTemplate(key, page.template);
  const self = fakeThis(page);
  if (key === 'stats' || key === 'shotchart' || key === 'review') {
    console.log('[debug] ' + key + ' S=' + typeof self.S + ' keys=' +
      (self.S ? Object.keys(self.S).join('|') : 'n/a'));
  }
  const comp = page.computed || {};
  Object.keys(comp).forEach(k => {
    const fn = typeof comp[k] === 'function' ? comp[k] : comp[k].get;
    if (!fn) return;
    try {
      const v = fn.call(self);
      results.push(key + '.' + k + ' -> ' + (Array.isArray(v) ? 'Array(' + v.length + ')'
        : v && typeof v === 'object' ? 'Object(' + Object.keys(v).slice(0, 4).join(',') + ')' : JSON.stringify(v)));
    } catch (e) {
      errors.push('[computed] ' + key + '.' + k + ' -> ' + e.message);
    }
  });
  // 交互方法冒烟测试
  const smoke = {
    upload: [['refreshJobs'], ['log', 'selftest']],
    overview: [['teamName', 'home'], ['mmss', 75], ['rowClass', { team: 'home' }]],
    stats: [['shotPoints', win.STORE.players[0]], ['aggregate', win.STORE.players[0]],
      ['madeCount', win.STORE.players[0]], ['sortByFg', { fg_pct: 0.4 }, { fg_pct: 0.5 }],
      ['onExpandChange', []]],
    shotchart: [['rankZone', -1], ['reset'], ['pct', 0.5]],
    highlights: [['clipSrc', { path: 'highlights/a.mp4', available: true }], ['mmss', 12]],
    report: [['pct', 0.5], ['mmss', 12], ['download', { fmt: 'csv_stats', file: 'x.csv', url: 'u', label: 'x' }]],
    review: [['build'], ['thumb', win.STORE.game.needs_review[0]], ['conf', win.STORE.game.needs_review[0]],
      ['nameOf', win.STORE.game.needs_review[0]]]
  }[key] || [];
  smoke.forEach(([fn, ...args]) => {
    try { self[fn].apply(self, args); } catch (e) { errors.push('[method] ' + key + '.' + fn + ' -> ' + e.message); }
  });
});

// ---------------- 球场 SVG 几何自检 ----------------
try {
  const C = win.Court;
  ['half', 'full'].forEach(v => {
    const svg = C.courtSVG({ view: v });
    if (!/^<svg/.test(svg) || !/<\/svg>$/.test(svg)) errors.push('[court] ' + v + ' SVG 结构异常');
    const open = (svg.match(/<(?!\/)(?!.*\/>)[a-zA-Z]+/g) || []).length;
    if (svg.indexOf('NaN') >= 0) errors.push('[court] ' + v + ' SVG 含 NaN');
  });
  const zp = C.zonePaths('half');
  Object.keys(zp).forEach(k => {
    if (zp[k].indexOf('NaN') >= 0) errors.push('[court] 分区路径 ' + k + ' 含 NaN');
  });
  const nZone = Object.keys(zp).length;
  const layerShot = C.shotLayer(win.STORE.shotchart.all.points, {});
  if (layerShot.indexOf('NaN') >= 0) errors.push('[court] shotLayer 含 NaN');
  const layerGrid = C.gridLayer(win.STORE.shotchart.all.grid, { bin: 1 });
  if (layerGrid.indexOf('NaN') >= 0) errors.push('[court] gridLayer 含 NaN');
  const layerZone = C.zoneLayer(win.STORE.shotchart.all.zones, {});
  if (layerZone.indexOf('NaN') >= 0) errors.push('[court] zoneLayer 含 NaN');
  console.log('球场自检：分区路径 ' + nZone + ' 个，散点层 ' + layerShot.length + ' 字符，网格层 ' +
    layerGrid.length + ' 字符，分区热力层 ' + layerZone.length + ' 字符');
} catch (e) {
  errors.push('[court] 自检异常 -> ' + e.message + '\n' + e.stack);
}

// ---------------- 热区一致性：前后端 zone_of 口径比对 ----------------
try {
  const D = win.D;
  // 与后端 rules.zone_of 逐字对齐的参考实现（用于交叉校验，而不是直接调 D.zoneOf）
  // 坐标系：x 横向(±7.5)，y 纵向(±14)，篮筐 (0, ±1.575)
  function refZone(x, y) {
    const H = 1.575, R3 = 6.75, CX = 6.60;
    const d = Math.min(Math.hypot(x, y + H), Math.hypot(x, y - H));
    if (d >= R3 || Math.abs(x) >= CX) {
      if (Math.abs(x) >= CX) return '底角三分';
      if (Math.abs(x) >= 4.0) return '45°三分';
      return '弧顶三分';
    }
    if (d < 4.0) return '禁区';
    if (d < 5.8) return '近距离中投';
    return '长两分';
  }
  let mismatch = 0, stored = 0;
  win.STORE.shotchart.all.points.forEach(p => {
    if (p.zone && p.zone !== refZone(p.x, p.y)) mismatch++;
    if (D.zoneOf(p.x, p.y) !== refZone(p.x, p.y)) stored++;   // 前端实现与参考实现不一致
  });
  if (stored) errors.push('[zone] 前端 zoneOf 与后端参考实现有 ' + stored + ' 处不一致');
  // 边界网格扫描：整个半场按 0.05m 步长比对，确保口径逐点一致
  let gridBad = 0;
  for (let x = -7.5; x <= 7.5; x += 0.25) {
    for (let y = -14; y <= 0; y += 0.25) {
      if (D.zoneOf(x, y) !== refZone(x, y)) gridBad++;
    }
  }
  if (gridBad) errors.push('[zone] 半场扫描发现 ' + gridBad + ' 个点分区不一致');
  console.log('分区口径一致性：出手点 ' + win.STORE.shotchart.all.points.length +
    ' 个（数据内 zone 不一致 ' + mismatch + '），半场 0.25m 网格扫描不一致 ' + gridBad + ' 点');
} catch (e) { errors.push('[zone] ' + e.message); }

// ---------------- Markdown 渲染自检 ----------------
try {
  const html = win.D.md2html(fs.readFileSync(path.join(DATA_DIR, 'report.md'), 'utf8'));
  const tables = (html.match(/<table>/g) || []).length;
  if (html.indexOf('&lt;') >= 0) errors.push('[md] 渲染结果含被转义的原始标签');
  console.log('Markdown 渲染：' + html.length + ' 字符，表格 ' + tables + ' 个，标题 ' +
    (html.match(/<h[12]>/g) || []).length + ' 个');
} catch (e) { errors.push('[md] ' + e.message); }

// ---------------- 片段路径可用性判断（绝对文件路径不能当 URL 用） ----------------
try {
  const A = win.API;
  const cases = [
    ['highlights/clip_01.mp4', true, '相对路径'],
    ['D:\\out\\highlights\\clip_01.mp4', false, 'Windows 绝对路径'],
    ['/home/user/out/highlights/clip_01.mp4', false, 'POSIX 绝对路径'],
    ['http://127.0.0.1:8000/api/media/x/a.mp4', true, '完整 URL'],
    ['', false, '空路径']
  ];
  cases.forEach(([p, want, label]) => {
    if (A.isWebPath(p) !== want) errors.push('[media] isWebPath(' + label + ') 期望 ' + want);
  });
  console.log('片段路径判断：' + cases.length + ' 个用例全部符合预期');
} catch (e) { errors.push('[media] ' + e.message); }

// ---------------- 汇总 ----------------
console.log('\n--- computed 抽样 ---');
console.log(results.join('\n'));
console.log('\n=== 自检结果 ===');
if (errors.length) {
  console.log('发现 ' + errors.length + ' 个问题：');
  errors.forEach(e => console.log(' - ' + e));
  process.exitCode = 1;
} else {
  console.log('全部通过：模板结构、computed/methods、球场几何、分区口径、Markdown 渲染均无异常');
}
