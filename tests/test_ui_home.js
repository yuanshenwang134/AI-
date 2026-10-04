/* 界面去重与主页回归（用户 2026-09-27 提的 5 条意见）。
   全部是"结构/文案"级断言，不需要浏览器、不需要后端：

     1) 上传页「篮筐标点」那一行原来还有一个「标球场」按钮，与下面「球场标定」重复 → 只能有一个
     2) 上传页不该再出现「进球标注(可选)」与「随机种子」——那是开发/评测输入，
        人工纠错走「人工复核」页、攒样本走「训练标注」页
     3) 顶栏不该再平铺一遍菜单（与左侧栏重复）→ index.html 里不能再有 topnav
     4) 打开网页先落「首页」：PAGES.home 必须存在，菜单第一项是 home
     5) 菜单每一项都要有对应页面（防止菜单指向不存在的页）
        + 左侧栏按 group 分组渲染（首页 / 分析流程 / 结果与产出）
*/
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

// ---- 上传页 ----
global.window = { STORE: { backendOk: true } };
require(path.join(ROOT, 'web/pages/upload.js'));
const upload = window.PAGES.upload;
const tpl = upload.template;

const countOf = (s, sub) => s.split(sub).length - 1;

// 只数**按钮**，不数弹窗标题（弹窗标题里也有「在画面上标篮筐」，那是正常的）
assert.equal(countOf(tpl, '>标球场（点场地特征点）</el-button>'), 1,
  '「标球场」按钮只能出现一次（原来篮筐标点行 + 球场标定行各一个）');
assert.equal(countOf(tpl, '>在画面上标篮筐</el-button>'), 1, '「在画面上标篮筐」按钮应恰好一个');
assert.ok(!tpl.includes('进球标注'), '上传页不该再有「进球标注」输入框');
assert.ok(!tpl.includes('随机种子'), '上传页不该再有「随机种子 seed」输入框');
assert.ok(!tpl.includes('form.labels_path'), 'labels_path 不该再绑到界面控件上');
assert.ok(tpl.includes('人工复核') || tpl.includes('训练标注'),
  '去掉标注入口后，应能在界面上找到人工纠错/标注的去处');
assert.ok(tpl.includes('离线样例'), '离线数据按钮要写清它是"合成样例、仅供界面演示"');
assert.ok(!tpl.includes('载入演示数据（离线可用）'), '旧的含糊说法不该再出现');

// ---- app.js：菜单表 + 默认路由 ----
const appSrc = read('web/app.js');
assert.ok(/route:\s*'home'/.test(appSrc), '默认路由必须是 home');
assert.ok(/key:\s*'home'/.test(appSrc), '菜单里必须有 home');
assert.ok(appSrc.includes('window.MENUS = MENUS'), '菜单表要挂到 window 供首页使用');
assert.ok(/group:\s*'home'/.test(appSrc) && /group:\s*'flow'/.test(appSrc)
  && /group:\s*'output'/.test(appSrc), '菜单项要带 group（首页/分析流程/结果与产出）');

// ---- index.html：顶栏不再重复菜单；侧栏按分组渲染 ----
const html = read('web/index.html');
assert.ok(!html.includes('class="topnav"') && !html.includes('topnav-item'),
  '顶栏的重复导航应已移除');
assert.ok(html.includes("x.group === 'flow'") && html.includes("x.group === 'output'"),
  '左侧栏应按 group 分组');
assert.ok(html.includes('pages/home.js'), '首页脚本必须被加载');
// 缓存击穿：**每个** script 都要带 ?v= 参数。以前这条断言写死成 '?v=34'，
// 一 +1 就得改测试（等于没在守纪律）；改成守"都带参数"这个真正的不变量。
const scriptTags = html.match(/<script src="[^"]+"><\/script>/g) || [];
assert.ok(scriptTags.length >= 10, '应当加载应用脚本');
const noVer = scriptTags.filter(t => !/\?v=[\w.\-]+/.test(t));
assert.equal(noVer.length, 0,
  '每个 script 都要带 ?v= 缓存击穿参数，缺的：' + JSON.stringify(noVer));

// ---- 后端自动重连（"先开页面、后起后端"不再需要手点「重新探测」）----
const appSrc2 = read('web/app.js');
assert.ok(/autoReconnect:\s*function/.test(appSrc2) && /stopReconnect:\s*function/.test(appSrc2),
  'app.js 要有 autoReconnect / stopReconnect');
assert.ok(/this\.autoReconnect\(\)/.test(appSrc2), 'mounted 里要启动自动重连');
assert.ok(/reconnectTimer:\s*null/.test(appSrc2), '要有一个定时器句柄可以停');
assert.ok(/stopReconnect\(\)/.test(appSrc2.split('probe: function')[1] || ''),
  '手动「重新探测」成功后要停掉自动重连，避免重复轮询');
assert.ok(read('web/pages/upload.js').includes('本页会自动重连'),
  '后端未连接时的提示要告诉用户会自动重连');

// ---- 首页 ----
require(path.join(ROOT, 'web/pages/home.js'));
const home = window.PAGES.home;
assert.ok(home, 'window.PAGES.home 必须存在');
// 菜单表在 app.js 里定义，这里手工给一份最小表来验证首页 computed
global.window.MENUS = [
  { key: 'home', icon: '🏠', title: '首页', group: 'home' },
  { key: 'upload', icon: '⬆', title: '上传与分析', group: 'flow' },
  { key: 'report', icon: '📄', title: '导出/报告', group: 'output' }
];
global.window.D = { teamName: () => '主队', mmss: (t) => String(t) };
const vm = { ...home.data(), $message: { error() {}, info() {}, warning() {}, success() {} } };
for (const [k, fn] of Object.entries(home.methods || {})) vm[k] = fn.bind(vm);
for (const [k, fn] of Object.entries(home.computed || {})) {
  Object.defineProperty(vm, k, { get: fn.bind(vm), configurable: true });
}
assert.equal(vm.cards.length, 2, '首页卡片要覆盖除 home 以外的所有菜单项');
assert.ok(vm.cards.every(c => c.desc && c.href === '#/' + c.key),
  '每张卡片都要有说明和跳转地址');
assert.equal(vm.hasGame, false);
assert.equal(vm.scoreLine, '', '没有比赛时不显示比分');

global.window.STORE = { backendOk: true, game: { score: { home: 78, away: 71 },
  timeline: [{}, {}], needs_review: [{}] }, jobId: 'abcdef123456' };
const vm2 = { ...home.data(), $message: { error() {}, info() {}, warning() {}, success() {} } };
for (const [k, fn] of Object.entries(home.methods || {})) vm2[k] = fn.bind(vm2);
for (const [k, fn] of Object.entries(home.computed || {})) {
  Object.defineProperty(vm2, k, { get: fn.bind(vm2), configurable: true });
}
assert.equal(vm2.hasGame, true);
assert.ok(vm2.scoreLine.includes('78') && vm2.scoreLine.includes('71'), '比分行要显示双方比分');
assert.equal(vm2.shotCount, 2);
assert.equal(vm2.reviewCount, 1);

// 首页模板里跳到「上传与分析」的主按钮必须在（这是首页唯一的主行动）
assert.ok(home.template.includes('开始分析一段视频'), '首页要有明确的主行动按钮');

// ---- 篮筐标点：只点中心即可（用户 2026-09-27）----
global.window.STORE = {};
require(path.join(ROOT, 'web/pages/upload.js'));
const up = window.PAGES.upload;
assert.equal(up.computed.markItems.call({}).length, 1,
  '篮筐标点只有「篮筐中心」一项是必点的');
assert.equal(up.computed.markItems.call({})[0].name, 'rim_center');
assert.equal(up.computed.markItemsOptional.call({}).length, 4,
  '左右缘/上下沿要保留为可选步骤（想自己量半径的人要用）');
assert.ok(!up.template.includes('未标点'), '「未标点（走自动检测）」标签应已删除');

const hoopOf = (pts) => up.computed.markHoop.call({ markPts: pts });
const onlyCenter = hoopOf([{ name: 'rim_center', x: 0.5, y: 0.3 }]);
assert.equal(onlyCenter.rx, 0, '只点中心时不该编一个半径出来（交 0 = 不知道，由后端检测器量）');
assert.equal(onlyCenter.ry, 0);
assert.equal(onlyCenter.measured, false);
const centerAndLeft = hoopOf([
  { name: 'rim_center', x: 0.5, y: 0.3 }, { name: 'rim_left', x: 0.47, y: 0.3 }]);
assert.ok(centerAndLeft.rx > 0 && centerAndLeft.measured === true,
  '点了中心 + 左缘 → 半径是量出来的');
assert.ok(Math.abs(centerAndLeft.rx - 0.03) < 1e-9, 'rx 应等于中心到左缘的距离');
assert.ok(!read('web/pages/upload.js').includes('rx = 0.02'),
  '不该再有"画面宽 2%"这种假精度默认值（真实 rx 可能是它的 2 倍，会让真进球被判成贴筐）');
assert.ok('hoopOptTarget' in up.data(), '可选四边要有一个选中目标（hoopOptTarget）');

console.log('UI dedup & home: 27 checks passed');
