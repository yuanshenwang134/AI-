// 标定页「地标范围」的选择：全场 17 个命名地标（A端/B端），可临时筛一个半场。
//
// 为什么单独钉这一条（用户实测反馈）：
//   1) 界面原来一次列出后端那 17 个特征点，名字是"另一侧的底线角""另一端的
//      篮筐中心"——这台机位只拍得到一侧半场，那些点画面里根本不存在，
//      用户点了只能是猜（实测把"底线右角"点成了罚球区右角，偏 9.17m）。
//   2) 用户明确说「不需要说这个点靠近篮筐了」：既然已经分了半区，就不该再让他
//      分辨"篮筐那一头 / 中圈那一头"。
//   3) ⚠️ 最关键的一条：哪一套点对应哪一侧，必须和 court.py 的坐标约定一致
//      （左半场 = y<0 = _near 那一套）。写反**不会报错**，只会把球场整体平移 28m，
//      两分变三分、热区整片错位——所以这里把它钉死。
//   4) 【2026-10-06 更新】主流程改成**全场**：默认 `courtSide='full'`，
//      列 17 个地标、用 **A端/B端** 命名（"A端就是你选定的那个篮筐"）——
//      这样"两个镜头各拍半场"时，同一个物理地标在两个镜头里都叫同一个名字，
//      各自解算完再合并。left/right 两个 7 点列表保留作临时筛选。
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
// 干净的显示名（半场列表）：不带"（篮筐那一头）/（中圈那一头）"
const CLEAN = ['篮筐中心', '底线左角', '底线右角', '罚球区左角',
               '罚球区右角', '罚球线中点', '三分弧顶'];

// ---- 默认是全场 ----
assert.equal(p.data().courtSide, 'full', '默认应当显示全场地标（主流程）');
assert.ok(Array.isArray(p.data().courtSideNames.full),
  '必须有 full 那一套（全场 17 个命名地标）');
assert.ok(p.data().courtSideNames.full.length >= 15,
  '全场应当有 15 个以上地标（两端 + 中线），实际 ' +
  p.data().courtSideNames.full.length);

// ---- 全场用 A端/B端 命名：同一个物理地标在两台机位里同名 ----
{
  const full = p.data().courtSideNames.full;
  const labs = labelsOf(full);
  assert.ok(labs.some(l => l.includes('A端')), '全场命名要用 A端：' + labs.join('、'));
  assert.ok(labs.some(l => l.includes('B端')), '全场命名要用 B端：' + labs.join('、'));
  // 每个地标都必须带球场坐标（dst），否则解不出单应矩阵
  const noDst = full.filter(i => !i.dst);
  assert.equal(noDst.length, 0, '全场地标必须都带 dst 球场坐标：' +
    namesOf(noDst).join('、'));
  // 坐标要与 court.py 的约定一致：A端（y>0）与 B端（y<0）对称
  const aHoop = full.find(i => i.name === 'hoop_far');
  const bHoop = full.find(i => i.name === 'hoop_near');
  assert.ok(aHoop && bHoop, '两端篮筐中心都要在列表里');
  assert.ok(Math.abs(aHoop.dst[1] - 12.425) < 0.01,
    'A端篮筐中心应当是 y=+12.425（罚球线距中 8.2 + 4.225），实际 ' + aHoop.dst[1]);
  assert.ok(Math.abs(bHoop.dst[1] + 12.425) < 0.01,
    'B端篮筐中心应当是 y=-12.425，实际 ' + bHoop.dst[1]);
  // 罚球线距中线 8.2（= 14 - 5.8）。写成 5.8 是常见错误，会把球场缩错。
  const ftA = full.find(i => i.name === 'ft_far');
  assert.ok(ftA && Math.abs(ftA.dst[1] - 8.2) < 0.01,
    '罚球线距中线必须 8.2m（14-5.8），实际 ' + (ftA && ftA.dst[1]));
}

// ---- 左侧 = 工具里 y<0 的那半场 = _near 那一套（临时筛选仍然要正确）----
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

// ---- 切换地标范围必须清空已标点 ----
vm.mfPts = [{ t: 1, name: 'hoop_far', x: 0.2, y: 0.3 }];
vm.mfTarget = 'hoop_far';
vm.mfResult = { ok: true, H: [[1, 0, 0], [0, 1, 0], [0, 0, 1]] };
vm.setCourtSide('left');
assert.equal(vm.mfPts.length, 0, '切换范围后已标点必须清空（否则解出差 28m 的坏标定）');
assert.equal(vm.mfTarget, '', '切换后应清掉当前选中的点名');
assert.equal(vm.mfResult, null, '切换后应清掉上一次的解算结果');

// ---- 非法值兜底到 full（默认那套），不能把 courtItems 弄空 ----
vm.setCourtSide('bogus');
assert.equal(vm.courtSide, 'full', '非法输入应兜底到 full（默认那套）');
assert.ok(vm.courtItems.length >= 15, '非法输入也必须有一张有效的点名表');
// 没有已标点时切换不该弹提示（不要打扰用户）
assert.equal(vm.mfPts.length, 0);

console.log('标定页地标范围：全场 A端/B端 17 点 + 半场 7 点筛选 + 坐标与 court.py 一致（checks passed）');
