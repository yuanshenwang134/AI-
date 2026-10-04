/* 快速功能测试（开发期用）：
   1) 复核页改判后，比分/球员统计/热区是否正确重算（前端写回口径）
   2) 夹具数据的关键不变量：分节之和 = 总分、区域口径一致
   用法：node web/_test.js

   数据来源（按顺序找第一个存在的）：
     * `web/demo/`      —— 如果你跑过 `scripts/run_demo.ps1`，用你自己的产物
     * `out/fixture/`   —— 仓库自带的小夹具（`aihoop.cli demo --seed 7 --duration 600`）
   这样自检**不依赖** web/demo 是否被清理过。 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ev = vm.runInThisContext('(0, eval)');

const DEMO_DIR = path.join(__dirname, 'demo');
const FIXTURE_DIR = path.join(__dirname, '..', 'out', 'fixture');
const DATA_DIR = fs.existsSync(path.join(DEMO_DIR, 'game.json')) ? DEMO_DIR : FIXTURE_DIR;
if (!fs.existsSync(path.join(DATA_DIR, 'game.json'))) {
  console.error('缺少测试数据：请先跑 `python -m aihoop.cli demo --seed 7 --duration 600 --out out/fixture`');
  process.exit(1);
}
console.log('测试数据来源：' + path.relative(path.join(__dirname, '..'), DATA_DIR));

const win = global;
win.window = win;
win.location = { protocol: 'http:', search: '', hash: '#/overview' };
win.URLSearchParams = URLSearchParams;
win.fetch = () => Promise.reject(new Error('stub'));
win.ElementPlus = {}; win.ElementPlusLocaleZhCn = {};
win.Vue = { reactive: o => o, createApp: () => ({ use() {}, component() {}, mount() {} }) };
win.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {} }) };
['api.js', 'data.js', 'court.js', 'components.js', 'app.js', 'pages/review.js'].forEach(f =>
  ev(fs.readFileSync(path.join(__dirname, f), 'utf8'), { filename: f }));

const demo = p => JSON.parse(fs.readFileSync(path.join(DATA_DIR, p), 'utf8'));
// 战术逐帧：仓库夹具里是 jsonl，web/demo 里是 json —— 两种都认
function loadFrames() {
  const j = path.join(DATA_DIR, 'tactics_frames.json');
  if (fs.existsSync(j)) return JSON.parse(fs.readFileSync(j, 'utf8'));
  const jl = path.join(DATA_DIR, 'tactics_frames.jsonl');
  const rows = fs.readFileSync(jl, 'utf8').split('\n').filter(Boolean).map(s => JSON.parse(s));
  return { frames: rows };
}
win.STORE.applyGame(demo('game.json'), demo('players.json'), demo('shotchart.json'),
  { markdown: fs.readFileSync(path.join(DATA_DIR, 'report.md'), 'utf8'), json: demo('report.json') },
  fs.existsSync(path.join(DATA_DIR, 'highlights.json')) ? demo('highlights.json') : { clips: [] });

const D = win.D;
let failures = 0;
function ok(name, cond, extra) {
  console.log((cond ? '  ✓ ' : '  ✗ ') + name + (extra ? '  ' + extra : ''));
  if (!cond) failures++;
}

console.log('\n[1] game.json 关键不变量');
const g = win.STORE.game;
const qs = g.quarter_scores;
ok('分节比分之和 = 总分(home)', qs.reduce((a, q) => a + q.home, 0) === g.score.home,
  qs.map(q => q.home).join('+') + ' = ' + g.score.home);
ok('分节比分之和 = 总分(away)', qs.reduce((a, q) => a + q.away, 0) === g.score.away,
  qs.map(q => q.away).join('+') + ' = ' + g.score.away);
ok('timeline 长度与 shotchart 点数一致',
  g.timeline.length === win.STORE.shotchart.all.points.length,
  g.timeline.length + ' / ' + win.STORE.shotchart.all.points.length);
ok('needs_review 全部低于阈值 0.6', g.needs_review.every(s => s.confidence < 0.6),
  'n=' + g.needs_review.length);
// 与后端 rules.zone_of 逐字对齐的参考实现（交叉校验前端 D.zoneOf）
// 坐标系：x 横向(±7.5)，y 纵向(±14)，篮筐 (0, ±1.575) —— 别写成 (±1.575, 0)
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
let bad = 0;
win.STORE.shotchart.all.points.forEach(p => { if (D.zoneOf(p.x, p.y) !== refZone(p.x, p.y)) bad++; });
ok('前端 zoneOf 与后端 rules.zone_of 逐点一致', bad === 0, 'bad=' + bad);
// 数据里自带的 zone 字段若与规范口径不符，只作提示（后端产物可能是早期版本生成的）
let stale = 0;
win.STORE.shotchart.all.points.forEach(p => { if (p.zone && p.zone !== refZone(p.x, p.y)) stale++; });
console.log('  · 数据自带 zone 字段与规范口径不一致 ' + stale + ' 处（前端以自算为准，仅提示）');
ok('三分球命中率数据存在', g.teams.home.stats.tpa > 0 && g.teams.away.stats.tpa > 0);

console.log('\n[2] 复核页改判 -> 统计口径写回');
const page = win.PAGES['review'];
const self = Object.assign({}, page.methods);
self.$message = { success() {}, warning() {}, info() {}, error() {} };
self.$nextTick = fn => fn && fn();
Object.assign(self, page.data());
Object.keys(page.computed).forEach(k => {
  const fn = typeof page.computed[k] === 'function' ? page.computed[k] : page.computed[k].get;
  if (!fn) return;
  Object.defineProperty(self, k, { configurable: true, get: () => fn.call(self) });
});
page.created.call(self);

console.log('  待复核出手 ' + self.rows.length + ' 条；总出手 ' + self.stats.total + ' 次');
const target = self.rows[0];
const beforeScore = JSON.parse(JSON.stringify(g.score));
const beforeMakes = g.timeline.filter(e => e.made).length;
const beforePid = target.player_id;
const beforeValue = Number(target.value);
const beforeMade = !!target.made;

<<<<<<< HEAD
// 本文件是**离线单测**：没有后端可写回。复核页的改判从 2026-09-25 起是 async：
//   * 有后端（canSubmit=true）时会 await 提交 → 再 APP_LOAD_JOB 重新拉产物，
//     本地状态靠"重新加载"更新 —— node 单测里没有后端，这条路根本走不通；
//   * 离线模式（canSubmit=false）走 applyLocal 本地重算，而且**整段同步执行**
//     （第一个 await 之前就做完了），所以下面可以直接断言。
// 注意不要用 await 来"修"这里的时序：await 会把断言推进微任务，而这个文件末尾
// 是同步算 process.exitCode 的，反而会把失败吞掉。
win.STORE.backendOk = false;
win.STORE.demoMode = true;

=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
// 场景 A：把「未中」改成「命中」（或反向），分值保持不变
self.correct(target, !beforeMade, beforeValue);

ok('timeline 中该次出手结果已被改写',
  g.timeline.some(e => e.player_id === beforePid && Math.abs(e.t - target.t) < 0.001 &&
    e.made === !beforeMade && e.source === 'manual'));
const afterMakes = g.timeline.filter(e => e.made).length;
ok('命中数按改判方向变化 ±1', Math.abs(afterMakes - beforeMakes) === 1,
  beforeMakes + ' -> ' + afterMakes);
const delta = (beforeMade ? -1 : 1) * beforeValue;
ok('比分随改判同步更新', g.score.home + g.score.away === beforeScore.home + beforeScore.away + delta,
  beforeScore.home + ':' + beforeScore.away + ' -> ' + g.score.home + ':' + g.score.away);
ok('分节之和仍等于总分',
  g.quarter_scores.reduce((a, q) => a + q.home, 0) === g.score.home &&
  g.quarter_scores.reduce((a, q) => a + q.away, 0) === g.score.away);
const pl = win.STORE.players.filter(p => p.player_id === beforePid)[0];
ok('球员统计已重算（存在该球员行）', !!pl, pl ? (pl.name + ' 得分 ' + pl.points) : '');
ok('复核队列已剔除该次出手', !self.rows.some(r => Math.abs(r.t - target.t) < 0.001 && r.player_id === beforePid));

// 场景 B：分值改判（2 分 -> 3 分，保持命中）
const t2 = g.timeline.filter(e => e.made && Number(e.value) === 2 && e.team === 'home')[0];
if (t2) {
<<<<<<< HEAD
  // 按 (t, player_id) 定位可改判的行。**优先从 allShots 取**：它每条都带有效的
  // _index；而复核队列里的行在"后端没给 index、又按 (t, player_id, value) 匹配不上
  // timeline"时会是 _index=-1，改判会被 review.js 的守卫直接挡掉（静默 return）。
  const row2 = self.allShots.concat(self.rows).filter(
    r => r.player_id === t2.player_id && Math.abs(r.t - t2.t) < 0.001)[0];
=======
  // 按 (t, player_id) 定位可改判的行（correct() 需要 row._index 才提交）
  const row2 = self.rows.concat(self.allShots.map(e => Object.assign({}, e, {
    _index: self.allShots.indexOf(e), _corrected: false
  }))).filter(r => r.player_id === t2.player_id && Math.abs(r.t - t2.t) < 0.001)[0];
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
  const s0 = g.score.home;
  self.correct(row2, true, 3);
  ok('分值改判 2->3 后主队得分 +1', g.score.home === s0 + 1, s0 + ' -> ' + g.score.home);
  const p2 = win.STORE.players.filter(p => p.player_id === t2.player_id)[0];
  ok('该球员三分命中数已 +1', p2 && p2.tpm >= 1, p2 ? ('tpm=' + p2.tpm + ' tpa=' + p2.tpa) : '');
} else {
  ok('存在可测试的 2 分命中球', false);
}

console.log('\n[3] 热区图层几何');
const C = win.Court;
const half = C.courtSVG({ view: 'half' });
const full = C.courtSVG({ view: 'full' });
ok('半场 SVG 无 NaN 且有底线/三分弧', half.indexOf('NaN') < 0 && half.indexOf('<path') > 0);
ok('全场 SVG 无 NaN', full.indexOf('NaN') < 0);
const layers = ['shots', 'zones', 'grid'];
layers.forEach(l => {
  const html = l === 'zones' ? C.zoneLayer(win.STORE.shotchart.all.zones, {})
    : l === 'grid' ? C.gridLayer(win.STORE.shotchart.all.grid, { bin: 1, metric: 'pct' })
      : C.shotLayer(win.STORE.shotchart.all.points, {});
  ok('图层 ' + l + ' 正常生成', html.length > 200 && html.indexOf('NaN') < 0, html.length + ' 字符');
});
// 镜像：x 与 -x 的出手必须落在同一个 u
const mirror = C.mapXY('half', -5.2, -9.3), orig = C.mapXY('half', 5.2, -9.3);
ok('左右半场出手镜像到同一半场', mirror.u === orig.u && mirror.v === orig.v,
  '(-5.2,-9.3)->u' + mirror.u + ' (5.2,-9.3)->u' + orig.u);

console.log('\n[4] 导出兜底（无后端时的本地 CSV/JSON）');
const csvStats = D.playersCSV(win.STORE.players, g);
const csvShots = D.shotsCSV(win.STORE.shotchart.all.points);
ok('球员统计 CSV 行数正确', csvStats.split('\r\n').length > win.STORE.players.length,
  csvStats.split('\r\n').length + ' 行');
ok('出手明细 CSV 行数 = 出手数 + 表头', csvShots.split('\r\n').length === g.timeline.length + 1,
  csvShots.split('\r\n').length + ' 行');
ok('CSV 含中文表头（UTF-8 正常）', csvStats.indexOf('球队汇总') > 0);

// ---------------------------------------------------------------------------
// [5] 战术分析页
// ---------------------------------------------------------------------------
console.log('\n[5] 战术分析页');
ev(fs.readFileSync(path.join(__dirname, 'pages/tactics.js'), 'utf8'),
  { filename: 'pages/tactics.js' });
const tactics = demo('tactics.json');
const frames = loadFrames();

ok('战术产物可用', tactics.available === true,
  '回合 ' + (tactics.possession && tactics.possession.total) +
  ' / 传球 ' + (tactics.passes && tactics.passes.total));
ok('俯视战术图逐帧数据非空', frames.frames && frames.frames.length > 0,
  frames.frames ? frames.frames.length + ' 帧' : '');
ok('逐帧数据里每帧都有球员坐标',
  frames.frames.slice(0, 50).every(f => f.players && f.players.length >= 4));

// 传球网络 / 球员图层 / 阵型时间轴：不能出现 NaN，且要有实际图元
const net = (tactics.pass_network || {}).home || { nodes: [], edges: [] };
const netSvg = C.passNetLayer(net, { team: 'home' });
ok('传球网络图层正常生成', netSvg.indexOf('<circle') > 0 && netSvg.indexOf('NaN') < 0,
  netSvg.length + ' 字符 / 节点 ' + net.nodes.length + ' 边 ' + net.edges.length);
const f0 = frames.frames[Math.floor(frames.frames.length / 2)];
const plSvg = C.tacticsPlayersLayer(f0, { H1: [[0, -2], [1, -3]] }, { showTrails: true, teamOf: {} });
ok('俯视战术图球员图层正常生成',
  plSvg.indexOf('<circle') > 0 && plSvg.indexOf('NaN') < 0, plSvg.length + ' 字符');
const offSegs = ((tactics.formation || {}).offense || {}).home || [];
const barSvg = C.formationBar(offSegs, { total: g.duration, colorOf: () => '#2f6fed' });
ok('阵型时间轴正常生成', barSvg.indexOf('<rect') > 0 && barSvg.indexOf('NaN') < 0,
  offSegs.length + ' 段');

// 坐标系必须与球场底图对齐：篮筐 (0,±1.575) 要落在半场画布中线上
const hoopPt = C.mapTactics(0, 1.575);
ok('战术坐标与半场底图对齐（篮筐在中线）',
  Math.abs(hoopPt.u - (C.C.COURT_W / 2 + 1.3)) < 1e-6 && Math.abs(hoopPt.v - (1.575 + 1.3)) < 1e-6,
  'u=' + hoopPt.u + ' v=' + hoopPt.v);
ok('战术图 v 轴 = 离底线距离（|y|）',
  C.mapTactics(0, -8).v === C.mapTactics(0, 8).v && C.mapTactics(0, -8).v === 9.3);

// 页面 computed 冒烟测试：直接绑定到假 this 上跑一遍
const tacPage = win.PAGES.tactics;
const ctx = Object.assign(tacPage.data(), { S: win.STORE });
ctx.S.tactics = tactics;
ctx.S.tacticsFrames = frames;
Object.keys(tacPage.computed).forEach(k =>
  Object.defineProperty(ctx, k, { get: tacPage.computed[k].bind(ctx), configurable: true }));
Object.keys(tacPage.methods).forEach(k => { ctx[k] = tacPage.methods[k].bind(ctx); });
ok('页面识别到战术数据', ctx.hasTactics === true);
ok('播放器能定位到当前帧', ctx.frame && ctx.frame.players.length >= 4,
  'playT=' + ctx.playT + ' -> t=' + (ctx.frame && ctx.frame.t));
ok('球员轨迹尾迹已聚合', Object.keys(ctx.trails).length >= 4,
  Object.keys(ctx.trails).length + ' 名球员');
ok('叠加层 SVG 正常', String(ctx.overlayHTML).indexOf('<circle') > 0);
ok('间距曲线选项正常', (ctx.spacingOption.series || []).length === 2 &&
  ctx.spacingOption.series[0].data.length > 0);
ok('阵型表格有两队', ctx.formationRows.length === 2);
ok('阵型时间轴有两队 × 攻防', ctx.offBars.length === 2 && ctx.defBars.length === 2);

// 战术导出（演示/无后端时的本地降级路径，与后端 passes.csv / spacing.csv 同结构）
const passCsv = D.tacticsPassesCSV(tactics);
const spaceCsv = D.tacticsSpacingCSV(tactics);
ok('传球 CSV 生成正常',
  passCsv.indexOf('传球网络') > 0 && passCsv.split('\r\n').length > tactics.passes.total / 2,
  passCsv.split('\r\n').length + ' 行');
ok('空间 CSV 生成正常',
  spaceCsv.indexOf('mean_dist') > 0 && spaceCsv.indexOf('area') > 0 &&
  spaceCsv.split('\r\n').length > 100,
  spaceCsv.split('\r\n').length + ' 行');
ok('没有战术数据时不崩、给出可读提示',
  D.tacticsPassesCSV(null) === '（本场没有战术数据）');

console.log('\n' + (failures ? '✗ 有 ' + failures + ' 项未通过' : '✓ 全部功能测试通过'));
process.exitCode = failures ? 1 : 0;
