/* 回归测试：左右两个画面各标各的点 —— 谁都不许把对方的点弄丢/画丢。
 *
 * 用户报的 bug（原话）：
 *   "我标完左画面再标右画面那左画面标的点就没了？"
 *
 * 根因（已定位）：右画面以前渲染的是 activePts，而 activePts === mfPtsHere，
 * mfPtsHere 只认 mfCurrent（左帧）。于是切到右画面时，左画面拿"左帧的点"去比，
 * 右帧的点被排除；两处按钮禁用状态也用 mfPtsHere。表现出来就是"点没了"——
 * 数据其实一直在 mfPts 里，是渲染时用错了时刻。
 */
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');

global.window = { STORE: { backendOk: true } };
require(path.resolve(__dirname, '../web/pages/upload.js'));
const p = window.PAGES.upload;

const LEFT_T = 15.2;
const RIGHT_T = 16.7;

function makeVm() {
  const v = { ...p.data(), $message: { warning() {}, info() {}, error() {}, success() {} } };
  for (const [k, fn] of Object.entries(p.methods)) v[k] = fn.bind(v);
  for (const [k, fn] of Object.entries(p.computed || {})) {
    Object.defineProperty(v, k, { get: fn.bind(v), configurable: true });
  }
  return v;
}

function click(vm, side, nx, ny) {
  vm.onCourtClick({
    clientX: nx * 854, clientY: ny * 480,
    target: {
      tagName: 'IMG',
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 854, height: 480 }),
    },
    currentTarget: {
      tagName: 'DIV',
      getAttribute: (k) => (k === 'data-side' ? String(side) : null),
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 854, height: 480 }),
    },
  });
}

/** 两帧都装好、点名表就绪的标定页 */
function readyVm() {
  const vm = makeVm();
  vm.markKind = 'court';
  vm.form.video_path = 'x.mp4';
  vm.dualView = true;
  vm.setCourtSide('left');
  // 本文件测的是**逐个点名**的老流程；「只点 4 点」模式默认是开的
  // （courtMini: true），不关掉的话点击会走 mini 通道，这里就测不到命名流程。
  vm.courtMini = false;
  vm.mfFrames = [{ t: LEFT_T, w: 854, h: 480, image: 'x' },
                 { t: RIGHT_T, w: 854, h: 480, image: 'x' }];
  vm.mfIdx = 0;
  vm.mfIdx2 = 1;
  vm.mfActive = 0;
  return vm;
}

const namesOf = (v) => v.courtItems.slice(0, 5).map((i) => i.name);

// ---- ① 左右各标 5 个点后，两边都还在（这是用户报的那一条）----
{
  const vm = readyVm();
  const NS = namesOf(vm);
  assert.equal(NS.length, 5, '点名表应当至少有 5 个点可用');

  NS.forEach((nm, i) => {
    vm.mfActive = 0; vm.mfTarget = nm; click(vm, 0, 0.30 + i * 0.08, 0.40 + i * 0.05);
  });
  assert.equal(vm.mfPtsHere.length, 5, '左画面应当有 5 个点');
  assert.equal(vm.mfPtsRight.length, 0, '这时右画面不该有点');

  NS.forEach((nm, i) => {
    vm.mfActive = 1; vm.mfTarget = nm; click(vm, 1, 0.35 + i * 0.08, 0.45 + i * 0.05);
  });

  assert.equal(vm.mfPts.length, 10, '合计应当 10 个点');
  assert.equal(vm.mfPtsHere.length, 5, '左画面的 5 个点必须还在（用户报的 bug）');
  assert.equal(vm.mfPtsRight.length, 5, '右画面应当有 5 个点');
  console.log('① 左右各标 5 个：左 %d / 右 %d / 合计 %d  ✅',
    vm.mfPtsHere.length, vm.mfPtsRight.length, vm.mfPts.length);
}

// ---- ② 每个点都归到自己那一幅画面上，且两边像素坐标各自独立 ----
{
  const vm = readyVm();
  const NS = namesOf(vm);
  NS.forEach((nm, i) => { vm.mfActive = 0; vm.mfTarget = nm; click(vm, 0, 0.30 + i * 0.08, 0.40); });
  NS.forEach((nm, i) => { vm.mfActive = 1; vm.mfTarget = nm; click(vm, 1, 0.35 + i * 0.08, 0.60); });

  vm.mfPtsHere.forEach((q) => assert.equal(q.t, LEFT_T, '左画面的点应记在左帧'));
  vm.mfPtsRight.forEach((q) => assert.equal(q.t, RIGHT_T, '右画面的点应记在右帧'));

  const l = vm.mfPtsHere[0], r = vm.mfPtsRight[0];
  assert.equal(l.name, r.name, '两边第一个点应当同名（左右各标一次才有意义）');
  assert.notEqual(Math.round(l.x * 100), Math.round(r.x * 100),
    '两边的像素坐标应当各自保留，不能互相覆盖');
  console.log('② 归属正确：左帧 %s / 右帧 %s，同名点坐标各自独立  ✅', LEFT_T, RIGHT_T);
}

// ---- ③ 渲染用的列表：左看左帧、右看右帧（不能是同一个）----
{
  const vm = readyVm();
  const NS = namesOf(vm);
  NS.forEach((nm, i) => { vm.mfActive = 0; vm.mfTarget = nm; click(vm, 0, 0.30 + i * 0.08, 0.40); });
  NS.forEach((nm, i) => { vm.mfActive = 1; vm.mfTarget = nm; click(vm, 1, 0.35 + i * 0.08, 0.60); });

  assert.equal(vm.mfFramePts(0).length, 5, '左画面清单应有 5 个');
  assert.equal(vm.mfFramePts(1).length, 5, '右画面清单应有 5 个');
  assert.notEqual(vm.mfPtsHere, vm.mfPtsRight, '两侧不能共用同一个列表');
  console.log('③ 渲染列表：左画面 %d 个 / 右画面 %d 个，互不共用  ✅',
    vm.mfPtsHere.length, vm.mfPtsRight.length);
}

// ---- ④「清空这个画面」只清当前那一面 ----
{
  const vm = readyVm();
  const NS = namesOf(vm);
  NS.forEach((nm, i) => { vm.mfActive = 0; vm.mfTarget = nm; click(vm, 0, 0.30 + i * 0.08, 0.40); });
  NS.forEach((nm, i) => { vm.mfActive = 1; vm.mfTarget = nm; click(vm, 1, 0.35 + i * 0.08, 0.60); });

  vm.mfActive = 1;
  vm.mfClear();
  assert.equal(vm.mfPtsRight.length, 0, '右画面应被清空');
  assert.equal(vm.mfPtsHere.length, 5, '左画面的点必须留着');

  vm.mfActive = 0;
  vm.mfClear();
  assert.equal(vm.mfPts.length, 0, '再清左画面就全空了');
  console.log('④ 清空只影响当前画面：清右后左仍 %d 个  ✅', 5);
}

// ---- ⑤ 清空按钮的禁用状态跟着"正在点的这一面"走 ----
{
  const vm = readyVm();
  const NS = namesOf(vm);
  vm.mfActive = 1;
  vm.mfTarget = NS[0];
  click(vm, 1, 0.40, 0.50);

  vm.mfActive = 1;
  assert.ok(vm.mfActivePtsCount >= 1, '在右画面时应认为"有东西可清"');
  vm.mfActive = 0;
  assert.equal(vm.mfActivePtsCount, 0, '切到没点过的左画面时按钮该是灰的');
  console.log('⑤ 清空按钮禁用状态跟着当前画面走  ✅');
}

// ---- ⑥ 同名点在同一画面内只保留最后一次；不同画面各自独立 ----
{
  const vm = readyVm();
  const nm = namesOf(vm)[0];
  vm.mfActive = 0; vm.mfTarget = nm;
  click(vm, 0, 0.30, 0.40);
  click(vm, 0, 0.32, 0.42);              // 同一点名再点一次 -> 替换
  assert.equal(vm.mfPtsHere.length, 1, '同一画面内同名点应被替换，不该累加');

  vm.mfActive = 1; vm.mfTarget = nm;
  click(vm, 1, 0.36, 0.46);              // 另一画面上的同名点 -> 新增
  assert.equal(vm.mfPts.length, 2, '两幅画面上同名点各算一个');
  assert.equal(vm.mfPtsRight.length, 1);
  console.log('⑥ 同名点：同画面内替换、跨画面各算一个  ✅');
}

// ---- ⑦ 渲染层：两个画面的 svg 必须各绑各的 computed（这条才是真正抓住 bug 的）----
// 为什么要读源码断言：⑥ 之前的检查全在数据层，而用户看到的"点没了"是**渲染层**。
// 变异测试证明过：把模板改回旧写法（两边都画 mfPtsHere / activePts），
// 只查数据的断言照样全绿 —— 所以必须把模板绑定本身钉住。
{
  const fs = require('node:fs');
  const src = fs.readFileSync(path.resolve(__dirname, '../web/pages/upload.js'), 'utf8');
  const iLeft = src.indexOf('左画面');
  const iRight = src.indexOf('右画面：自己的坐标系');
  assert.ok(iLeft > 0 && iRight > iLeft, '应当能定位到左右两个画面的模板片段');

  const leftTpl = src.slice(iLeft, iRight);
  const rightTpl = src.slice(iRight, iRight + 3000);

  assert.ok(leftTpl.includes('in mfPtsHere'), '左画面必须渲染 mfPtsHere（它自己那一帧的点）');
  assert.ok(!leftTpl.includes('in activePts" :key="i"'),
    '左画面不能再渲染 activePts —— 它只认左帧，会让"另一边"看起来消失');
  assert.ok(rightTpl.includes('in mfPtsRight'),
    '右画面必须渲染 mfPtsRight（右帧的点）');
  assert.ok(!rightTpl.includes('in mfPtsHere'),
    '右画面不能渲染 mfPtsHere —— 那会画出左帧的点而不是它自己的（这就是旧 bug）');
  assert.ok(src.includes('!mfActivePtsCount'),
    '清空按钮的禁用状态必须跟着"正在点的那一面"，不能用 mfPtsHere');
  console.log('⑦ 渲染层绑定：左=mfPtsHere、右=mfPtsRight、按钮=mfActivePtsCount  ✅');
}

console.log('\n左右两个画面的点互不丢失（checks passed）');
