// 标定页「哪一侧半场」的选择：只列出这一侧的 7 个点，且名字干净。
//
// 为什么单独钉这一条（用户实测反馈）：
//   1) 界面原来一次列出后端那 17 个特征点，其中一半是"另一侧的底线角"、
//      "另一端的篮筐中心"——这台机位只拍得到一侧半场，那些点画面里根本不存在，
//      用户点了只能是猜（实测把"底线右角"点成了罚球区右角，偏 9.17m）。
//   2) 用户明确说「不需要说这个点靠近篮筐了」：既然已经分了半区，就不该再让他
//      分辨"篮筐那一头 / 中圈那一头"。
//   3) ⚠️ 最关键的一条：哪一套点对应哪一侧，必须和 court.py 的坐标约定一致
//      （左半场 = y<0 = _near 那一套）。写反**不会报错**，只会把球场整体平移 28m，
//      两分变三分、热区整片错位——所以这里把它钉死。
const assert = require('node:assert/strict');
global.window = { STORE: { backendOk: true } };
require(require('node:path').resolve(__dirname, '../web/pages/upload.js'));
const p = window.PAGES.upload;

function makeVm() {
  const v = { ...p.data(), $message: { warning() {}, info() {}, error() {}, success() {} } };
  for (const [k, fn] of Object.entries(p.methods)) v[k] = fn.bind(v);
  for (const [k, fn] of Object.entries(p.computed || {})) {
    Object.defineProperty(v, k, { get: fn.bind(v), configurable: true });
  }
  return v;
}

const vm = makeVm();
const labelsOf = list => list.map(i => i.label);
const namesOf = list => list.map(i => i.name);
// 干净的显示名：不带"（篮筐那一头）/（中圈那一头）/另一端的…"
const CLEAN = ['篮筐中心', '底线左角', '底线右角', '罚球区左角',
               '罚球区右角', '罚球线中点', '三分弧顶'];

// ---- 左侧 = 工具里 y<0 的那半场 = _near 那一套 ----
vm.setCourtSide('left');
assert.equal(vm.courtItems.length, 7, '左侧应当是 7 个点');
assert.ok(namesOf(vm.courtItems).every(n => /_near/.test(n)),
  '左侧必须用 _near 那一套（写反会把球场平移 28m）：' + namesOf(vm.courtItems));
assert.deepEqual(labelsOf(vm.courtItems), CLEAN,
  '左侧显示名必须是干净名字：' + labelsOf(vm.courtItems));

// ---- 右侧 = y>0 = _far 那一套 ----
vm.setCourtSide('right');
assert.equal(vm.courtItems.length, 7, '右侧应当是 7 个点');
assert.ok(namesOf(vm.courtItems).every(n => /_far/.test(n)),
  '右侧必须用 _far 那一套：' + namesOf(vm.courtItems));
assert.deepEqual(labelsOf(vm.courtItems), CLEAN,
  '右侧显示名也要干净（两边同一套名字，用户不必学两套）：' + labelsOf(vm.courtItems));

// ---- 切换半场必须清空已标点 ----
vm.mfPts = [{ t: 1, name: 'hoop_far', x: 0.2, y: 0.3 }];
vm.mfTarget = 'hoop_far';
vm.mfResult = { ok: true, H: [[1, 0, 0], [0, 1, 0], [0, 0, 1]] };
vm.setCourtSide('left');
assert.equal(vm.mfPts.length, 0, '切换半场后已标点必须清空（否则解出差 28m 的坏标定）');
assert.equal(vm.mfTarget, '', '切换后应清掉当前选中的点名');
assert.equal(vm.mfResult, null, '切换后应清掉上一次的解算结果');

// ---- 非法值要兜底到 left，不能把 courtItems 弄空 ----
vm.setCourtSide('bogus');
assert.equal(vm.courtSide, 'left');
assert.equal(vm.courtItems.length, 7, '非法输入也必须有一张有效的点名表');

console.log('标定页半场选择：7 个点 / 名字干净 / 与 court.py 坐标约定一致（checks passed）');
