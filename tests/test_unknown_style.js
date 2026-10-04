// 「结果未知」的渲染口径：不能显示成"未中"。
//
// 背景：后端对 `result=unknown` 显式输出 `made=null`（不再用 false 占位）。
// 前端有四个地方要判断"这一球是不是结果未知"：时间轴徽章配色、徽章文案、
// 时间轴筛选、投篮图兜底。它们必须走**同一个判据**（`D.isUnknown`），
// 否则会出现"徽章是橙色待确认、文案却写未中""待确认筛选里找不到它"这类不一致。
// 这里用一个输入矩阵把四处钉在一起。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
global.window = { STORE: {} };
require(path.join(root, 'web', 'data.js'));
require(path.join(root, 'web', 'pages', 'overview.js'));

const D = window.D;
const page = window.PAGES.overview;

// 输入矩阵：命中 / 未中 / 未知（带 result）/ 未知（缺 result，made 为空）/ 未知（缺 made）/ 客队命中
const ROWS = [
  { t: 1, team: 'home', result: 'made', made: true, value: 2, points: 2 },
  { t: 2, team: 'home', result: 'missed', made: false, value: 2, points: 0 },
  { t: 3, team: 'home', result: 'unknown', made: null, value: 2, points: 0 },
  { t: 4, team: 'home', made: null, value: 2, points: 0 },          // 边界：缺 result
  { t: 5, team: 'home', value: 2, points: 0 },                       // 边界：连 made 都没有
  { t: 6, team: 'away', result: 'made', made: true, value: 3, points: 3 }
];
assert.deepEqual(ROWS.map(D.isUnknown), [false, false, true, true, true, false],
  'D.isUnknown 是四处共用的唯一判据');

// ---- ① 徽章配色 -------------------------------------------------------------
const vm = Object.assign({}, page.data ? page.data() : {});
Object.keys(page.methods).forEach(function (k) { vm[k] = page.methods[k].bind(vm); });
const badges = ROWS.map(function (e) { return vm.resultClass(e); });
assert.deepEqual(badges, ['made', 'miss', 'needs', 'needs', 'needs', 'made'], '徽章三态');
assert.equal(vm.resultClass({ result: 'made', made: true, counts_for_score: false }), 'made');
assert.equal(vm.resultClass(null), 'miss', '空行不该抛异常');
assert.equal(vm.resultClass({ made: null }), 'needs');
assert.equal(vm.resultClass({}), 'needs');

// ---- ② 徽章文案（从模板里取真实表达式求值，而不是抄一份） --------------------
const src = fs.readFileSync(path.join(root, 'web', 'pages', 'overview.js'), 'utf8');
const badgeLine = page.template.split('\n').find(function (l) {
  return l.indexOf('class="tl-badge" :class="resultClass(e)"') >= 0;
});
assert.ok(badgeLine, '时间轴徽章必须走 resultClass(e)');
const expr = badgeLine.match(/\{\{\s*(.*?)\s*\}\}/)[1].replace(/\\'/g, "'");
const textOf = new Function('e', 'resultClass', 'return (' + expr + ');');
const texts = ROWS.map(function (e) {
  return textOf(e, function (row) { return vm.resultClass(row); });
});
assert.deepEqual(texts, ['命中', '未中', '未知（待确认）', '未知（待确认）', '未知（待确认）', '命中'], '徽章文案');
ROWS.forEach(function (e, i) {
  assert.equal(texts[i] === '未知（待确认）', badges[i] === 'needs',
    '第 ' + (i + 1) + ' 行：文案与徽章对"待确认"的判断必须一致');
});
assert.ok(!/:class="e\.made\?/.test(src), '不该再出现直接按 e.made 配色的写法');
assert.ok(page.template.includes("{{ resultClass(e) === 'needs' ? '未知（待确认）'"),
  '文字与颜色必须使用同一未知结果判据');

// ---- ③ 时间轴筛选：三种口径必须与徽章一一对应 -------------------------------
Object.keys(page.computed).forEach(function (k) {
  const fn = typeof page.computed[k] === 'function' ? page.computed[k] : page.computed[k].get;
  if (!fn) return;
  Object.defineProperty(vm, k, { configurable: true, get: function () { return fn.call(vm); } });
});
Object.defineProperty(vm, 'game', { value: { timeline: ROWS }, configurable: true, writable: true });
Object.defineProperty(vm, 'filterTeam', { value: 'all', configurable: true, writable: true });
function visible(filter) {
  Object.defineProperty(vm, 'filterMade', { value: filter, configurable: true, writable: true });
  return vm.timeline.map(function (e) { return e.t; });
}
assert.deepEqual(visible('all'), [1, 2, 3, 4, 5, 6], '不过滤时全在');
assert.deepEqual(visible('made'), [1, 6], '命中筛选只留 made=true');
assert.deepEqual(visible('miss'), [2], '"未中"筛选不能把待确认算进来');
assert.deepEqual(visible('unknown'), [3, 4, 5], '待确认筛选必须能找到缺 result 的行');

// 总数独立于筛选；所有未知都保留，不拿已判定数冒充总出手数。
assert.deepEqual(vm.shotCounts, {total:6, made:2, miss:1, unknown:3});
assert.ok(page.template.indexOf('<video') < page.template.indexOf('投篮记录'));
assert.ok(page.template.indexOf('投篮记录') < page.template.indexOf('<big-scoreboard'));
assert.ok(!page.template.includes('showVideo'), '原视频默认可见');
let plays = 0;
vm.$refs = {video: {currentTime:0, duration:30, play:function(){plays++;}}};
Object.defineProperty(vm, 'videoUrl', {value:'/video', configurable:true});
vm.gotoEvent(ROWS[4], 0);
assert.equal(vm.activeRow, 4, '筛选后高亮按原始记录定位');
assert.equal(vm.$refs.video.currentTime, 0, '未加载时不能丢掉待跳转时刻');
vm.onVideoMeta();
assert.equal(vm.$refs.video.currentTime, 4.4);
assert.equal(plays, 1);
vm.gotoEvent({t:19.36, crossing_t:21.141}, 0, true);
assert.equal(vm.$refs.video.currentTime, 20.141, '篮下入口看穿筐时刻，而不是出手时刻');
vm.gotoEvent({t:100}, 0);
assert.equal(vm.$refs.video.currentTime, 29.95, '过期时间不超过视频末尾');
page.watch.videoUrl.call(vm);
assert.equal(vm.pendingSeek, null);
assert.equal(vm.videoReady, false);
assert.equal(vm.activeRow, -1);

// ---- ④ 投篮图兜底：未知一律不进图（与后端 rules.shot_chart 口径一致） --------
assert.deepEqual(D.shotsFromTimeline({ timeline: ROWS }).map(function (p) { return p.t; }),
  [1, 2, 6], '投篮图兜底也不画未知');
const UNKNOWN_ROWS = ROWS.filter(D.isUnknown);
assert.equal(UNKNOWN_ROWS.length, 3, '矩阵里应当正好 3 条未知');
assert.equal(D.shotsFromPlayers([{ player_id: 'H1', team: 'home', shots: UNKNOWN_ROWS }]).length,
  0, 'players.json 兜底同样排除未知');
assert.deepEqual(
  D.shotsFromPlayers([{ player_id: 'H1', team: 'home', shots: ROWS }]).map(function (p) { return p.t; }),
  [1, 2, 6], 'players.json 兜底只留命中/未中');

// ---- ⑤ tooltip 文案与样式 ---------------------------------------------------
assert.equal(D.shotLabel(2, true), '命中两分');
assert.equal(D.shotLabel(2, false), '未中两分');
assert.equal(D.shotLabel(3, null), '待确认三分');
assert.equal(D.shotLabel(1, undefined), '待确认罚球');
const css = fs.readFileSync(path.join(root, 'web', 'styles.css'), 'utf8');
assert.match(css, /\.tl-badge\.needs\s*\{/, '待确认用的 .needs 样式必须存在');

// ---- ⑥ 待确认徽章的可读性：从 styles.css 里取色现算，防止以后改回"看不清的橙" ----
// （11.5px 加粗属于小字号，WCAG AA 门槛 4.5:1）
// 说明：对比度是标准可判定量；下面那条"与客队底色的 RGB 距离"只是**防退化代理指标** ——
// 它能挡住"又变回和客队一样的实心橙"，但**不能替代人眼对区分度的判断**，
// 观感仍以人工目视为准（见 docs/复检_方案B落地_2026-09-26.md）。
function hex6(h) {
  h = h.replace('#', '');
  if (h.length === 3) h = h.split('').map(function (c) { return c + c; }).join('');
  return h.toLowerCase();
}
function lin(c) { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
function lum(h) {
  const n = parseInt(hex6(h), 16);
  return 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
}
function contrast(fg, bg) {
  const a = lum(fg), b = lum(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
function rgbDist(a, b) {
  const na = parseInt(hex6(a), 16), nb = parseInt(hex6(b), 16);
  const d = [16, 8, 0].map(function (s) { return ((na >> s) & 255) - ((nb >> s) & 255); });
  return Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
}
const needsBlock = css.match(/\.tl-badge\.needs\s*\{([^}]*)\}/)[1];
const needsBg = (needsBlock.match(/background:\s*(#[0-9a-fA-F]{3,6})/) || [])[1];
const needsFg = (needsBlock.match(/color:\s*(#[0-9a-fA-F]{3,6})/) || [])[1];
assert.ok(needsBg && needsFg, '.needs 必须显式声明 background 与 color（不能靠继承白字）');
const needsContrast = contrast(needsFg, needsBg);
assert.ok(needsContrast >= 4.5,
  '待确认徽章对比度只有 ' + needsContrast.toFixed(2) + ':1，达不到 AA 的 4.5:1');
const away = (css.match(/--away:\s*(#[0-9a-fA-F]{3,6})/) || [])[1];
assert.ok(away, '应能从 styles.css 取到 --away');
assert.ok(rgbDist(needsBg, away) >= 60,
  '待确认底色与客队主题色太近（RGB 距离 ' + rgbDist(needsBg, away).toFixed(0) +
  '），同一行相邻会混');
console.log('  待确认徽章：文字 ' + needsFg + ' / 底色 ' + needsBg +
  ' → 对比度 ' + needsContrast.toFixed(2) + ':1；与客队底色 RGB 距离 ' +
  rgbDist(needsBg, away).toFixed(0));

console.log('Unknown-result rendering checks passed');

// All existing small timeline badges must retain readable text.
for (const name of ['made','miss','a','h']) {
  const block = css.match(new RegExp('\\.tl-badge\\.'+name+'\\s*\\{([^}]*)\\}'))[1];
  let bg = block.match(/background:\s*([^;]+)/)[1].trim();
  if (bg.startsWith('var(')) {
    const key=bg.slice(4,-1);
    bg=css.match(new RegExp(key+':\\s*(#[0-9a-fA-F]{3,6})'))[1];
  }
  const fg=(block.match(/color:\s*(#[0-9a-fA-F]{3,6})/)||[])[1] || '#ffffff';
  const ratio=contrast(fg,bg);
  assert.ok(ratio>=4.5, name+' text contrast '+ratio);
  console.log(name+': '+ratio.toFixed(2)+':1');
}

// 旧引擎遮挡无 crossing_t 时仍可定位判定收尾；无位置的点不能进任何兜底图。
const hiddenShot={t:1,decision_t:4,crossing_t:null,made:true,result:'made',tags:['location_unknown']};
const normalShot={t:2,made:true,result:'made',x:1,y:4};
assert.equal(D.shotsFromTimeline({timeline:[hiddenShot,normalShot]}).length,1);
assert.equal(D.shotsFromPlayers([{shots:[hiddenShot,normalShot]}]).length,1);
const seekVm={game:{timeline:[hiddenShot]},videoUrl:'video',pendingSeek:null,$refs:{},seekVideo(){}};
page.methods.gotoEvent.call(seekVm,hiddenShot,0,true);
assert.equal(seekVm.pendingSeek,3,'判定收尾前一秒');
assert.ok(page.template.includes('v-else-if="e.decision_t != null"'));
assert.ok(page.template.includes('看判定收尾'));
assert.ok(page.template.includes('时间来源：'));
assert.ok(page.template.includes('分值尚未确认'));
console.log('Legacy review navigation and location checks passed');

// 跟踪摘要来自真实 game.meta，不把未记录的旧任务显示成“已锁定”。
assert.equal(page.computed.rimTracking.call({game:{meta:{}}}),null);
const rimSummary={lock_enabled:false,transitions:[{t:12,reason:'reacquired_far'}]};
assert.equal(page.computed.rimTracking.call({game:{meta:{shot_engine_details:{rim_tracking:rimSummary}}}}),rimSummary);
assert.equal(page.methods.rimTransitionLabel('reacquired_far'),'失效后在远处重新确认');
const rimSeek={$refs:{},videoUrl:'video',pendingSeek:null,seekVideo(){}};
page.methods.gotoRimTransition.call(rimSeek,{t:12});
assert.equal(rimSeek.pendingSeek,11);
assert.ok(page.template.includes('篮筐跟踪与标注反馈'));
assert.ok(page.template.includes('rimTracking.first_confirmed.hint_relation'));
assert.ok(page.template.includes('rimTracking.rejected_candidates'));
assert.ok(page.template.includes('@click="gotoRimTransition(r)"'));
console.log('Rim tracking visibility and seek checks passed');

assert.ok(page.template.includes('框外不代表标注错误'));
assert.ok(!page.template.includes('在首次确认框外，请核对标注'));

const suggestedSeek={game:{timeline:[]},videoUrl:'video',pendingSeek:null,$refs:{},seekVideo(){}};
page.methods.gotoEvent.call(suggestedSeek,{t:15.22,review_t:16.483,decision_t:16.75},0,true);
assert.equal(suggestedSeek.pendingSeek,15.483,'核对时刻前一秒');
page.methods.gotoEvent.call(suggestedSeek,{t:4,review_t:0},0,true);
assert.equal(suggestedSeek.pendingSeek,0);
page.methods.gotoEvent.call(suggestedSeek,{t:4,review_t:NaN,crossing_t:6},0,true);
assert.equal(suggestedSeek.pendingSeek,5);
assert.ok(page.template.includes('看待确认位置'));
assert.equal(page.computed.rimAvailability.call({rimTracking:null}),null);
assert.equal(page.computed.rimAvailability.call({rimTracking:{}}),null);
assert.ok(page.template.includes('没有记录不代表没有出手'));
assert.ok(page.template.includes('不是识别准确率'));
console.log('Review seek priority and availability warning passed');
