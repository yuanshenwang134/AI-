/* 「只点 4 个点」模式（courtMini）的回归测试。
 *
 * 为什么单独钉这一条：这是标定页现在的**默认**流程（courtMini: true）。
 * 它和"逐个点名"的差别是根本性的 ——
 *   * 不给点命名，只记录**点选顺序**（顺序本身就是数据）；
 *   * 载荷走 mini_frames（有序数组），不走 landmarks（按名字的字典）；
 *   * 因此"发出去的顺序必须和点选顺序一致"，否则后端会把点配到错误的地物上。
 */
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

global.window = { STORE: { backendOk: true } };
require(path.resolve(__dirname, '../web/pages/upload.js'));
const p = window.PAGES.upload;

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
    target: { tagName: 'IMG', getBoundingClientRect: () => ({ left: 0, top: 0, width: 854, height: 480 }) },
    currentTarget: {
      tagName: 'DIV',
      getAttribute: (k) => (k === 'data-side' ? String(side) : null),
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 854, height: 480 }),
    },
  });
}

function readyVm() {
  const vm = makeVm();
  vm.markKind = 'court';
  vm.form.video_path = 'x.mp4';
  vm.dualView = true;
  vm.mfFrames = [{ t: 10.0, w: 854, h: 480, image: 'x' }];
  vm.mfIdx = 0;
  vm.mfIdx2 = 0;
  vm.mfActive = 0;
  vm.courtMini = true;
  return vm;
}

const FOUR = [[0.20, 0.40], [0.60, 0.40], [0.65, 0.70], [0.25, 0.70]];

// ① 默认就是 4 点模式
{
  const vm = makeVm();
  assert.equal(vm.courtMini, true, '4 点模式应当默认开启（它是推荐流程）');
  console.log('① 默认开启 4 点模式  ✅');
}

// ② 4 点模式不需要选名字，按顺序记下来
{
  const vm = readyVm();
  vm.mfTarget = '';                       // 故意不选名字
  FOUR.forEach(([x, y]) => click(vm, 0, x, y));
  assert.equal(vm.mfMiniPts.length, 4, '4 点模式不该依赖 mfTarget');
  assert.deepEqual(vm.mfMiniPts.map((q) => q.seq), [1, 2, 3, 4],
    '序号必须等于点选顺序');
  console.log('② 不选名字、按顺序记 4 点  ✅');
}

// ③ 载荷：走 mini_frames，且顺序与点选一致
{
  const vm = readyVm();
  FOUR.forEach(([x, y]) => click(vm, 0, x, y));
  const frames = vm.miniPayload();
  assert.equal(frames.length, 1, '同一幅画面应当只出一组');
  assert.equal(frames[0].t, 10.0);
  assert.equal(frames[0].points.length, 4);
  frames[0].points.forEach((pt, i) => {
    assert.ok(Math.abs(pt[0] - FOUR[i][0]) < 1e-6 &&
              Math.abs(pt[1] - FOUR[i][1]) < 1e-6,
      '第 %d 个点发出去的位置应当等于第 %d 次点击：%s vs %s',
      i + 1, i + 1, JSON.stringify(pt), JSON.stringify(FOUR[i]));
  });
  console.log('③ 载荷顺序 == 点选顺序  ✅');
}

// ④ 只有 3 个点时，不产生可用载荷（后端要正好 4 个）
{
  const vm = readyVm();
  FOUR.slice(0, 3).forEach(([x, y]) => click(vm, 0, x, y));
  assert.equal(vm.miniPayload().length, 0, '不满 4 个点不该产生载荷');
  console.log('④ 不足 4 点不产生载荷  ✅');
}

// ⑤ 同一幅画面点超过 4 个会被挡住（第 5 个不记）
{
  const vm = readyVm();
  FOUR.forEach(([x, y]) => click(vm, 0, x, y));
  click(vm, 0, 0.9, 0.9);
  assert.equal(vm.mfMiniPts.length, 4, '一幅画面最多 4 个点');
  console.log('⑤ 一幅画面最多 4 点  ✅');
}

// ⑥ 切换模式要清干净（两套点集不通用）
{
  const vm = readyVm();
  FOUR.forEach(([x, y]) => click(vm, 0, x, y));
  vm.setCourtMini(false);
  assert.equal(vm.mfMiniPts.length, 0, '切到点名模式要清掉 4 点');
  assert.equal(vm.courtMini, false);
  vm.setCourtMini(true);
  assert.equal(vm.mfPts.length, 0, '切回 4 点模式要清掉点名');
  console.log('⑥ 切换模式清空两边点集  ✅');
}

// ⑦ 渲染层：两个画面都要画 mini 点（否则用户看不见自己点的）
{
  const src = fs.readFileSync(path.resolve(__dirname, '../web/pages/upload.js'), 'utf8');
  assert.ok(src.includes('in miniPtsHere'), '左画面要渲染 miniPtsHere');
  assert.ok(src.includes('in miniPtsRight'), '右画面要渲染 miniPtsRight');
  assert.ok(src.includes('p.seq'), '要显示点选序号');
  console.log('⑦ 两个画面都渲染 4 点与序号  ✅');
}

// ⑧ **miniPtsHere 必须是 computed，不能是 method**
//    这是我犯过的错：放 methods 里时，模板拿到的是**函数对象**，于是
//      * `miniPtsHere.length` 读的是形参个数 = 0 → 槽位永远显示「未点」；
//      * `v-for="p in miniPtsHere"` → 0 次迭代 → 画面上不画任何圈；
//      * 点其实都记下了（所以点满 4 个后守卫会拦住下一次点击）。
//    用户看到的"点完 4 个啥也点不了了、而且没有圈"就是这一个根因。
{
  const vm = readyVm();
  vm.mfFrames = [{ t: 10.0, w: 854, h: 480, image: 'x' },
                 { t: 20.0, w: 854, h: 480, image: 'x' }];
  vm.mfIdx = 0; vm.mfIdx2 = 1; vm.mfActive = 1;
  FOUR.forEach(([x, y]) => click(vm, 1, x, y));   // 点在**右**画面
  assert.ok(Array.isArray(vm.miniPtsHere),
    'miniPtsHere 必须是数组（computed），不是函数');
  assert.ok(Array.isArray(vm.miniPtsRight), 'miniPtsRight 必须是数组');
  assert.ok(Array.isArray(vm.miniPtsActive), 'miniPtsActive 必须是数组');
  assert.equal(vm.miniPtsRight.length, 4, '右画面应有 4 个点');
  assert.equal(vm.miniPtsActive.length, 4,
    'miniPtsActive 要跟着"正在点的那一面"（右）—— 否则槽位会错显示为未点');
  console.log('⑧ miniPts* 是数组、且跟随当前画面  ✅');
}

console.log('\n4 点模式（courtMini）：载荷有序、切换清空、渲染齐全（checks passed）');
