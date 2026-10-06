// 「在画面上拖框选中比分牌」的坐标换算。
//
// 为什么单独立一条（用户实测："按计分板判断进球的功能没了"）：
//   比分牌自动定位在校园/村 BA 那种长横条台标上会失败，后端返回
//   "自动定位的区域没有稳定读到双方比分，请手动框选比分牌" ——
//   而当时唯一的入口是一个**要用户自己算归一化坐标的文本框**
//   （placeholder 写着"例如 0.21,0.10,0.79,0.15"）。让人手算坐标等于没有功能。
//   现在改成在画面上拖一个框。拖框的坐标换算很容易写错（反向拖、越界、
//   误触），所以这里把它钉死。
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');

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

const W = 1000, H = 600;
function ev(x, y) {
  return {
    clientX: x, clientY: y,
    preventDefault() {},
    target: { tagName: 'IMG',
              getBoundingClientRect: () => ({ left: 0, top: 0, width: W, height: H }) },
    currentTarget: { tagName: 'IMG',
                     getBoundingClientRect: () => ({ left: 0, top: 0, width: W, height: H }) },
  };
}

// ① 正向拖：左上 → 右下
{
  const vm = makeVm();
  vm.sbBoxDown(ev(200, 60));
  vm.sbBoxMove(ev(800, 150));
  vm.sbBoxUp();
  assert.equal(vm.sbBox, '0.2000,0.1000,0.8000,0.2500',
    '正向拖框的归一化坐标不对：' + vm.sbBox);
  console.log('① 正向拖框  ✅');
}

// ② 反向拖（右下 → 左上）必须被规范化，不能产生 x0>x1 的框
{
  const vm = makeVm();
  vm.sbBoxDown(ev(800, 150));
  vm.sbBoxMove(ev(200, 60));
  vm.sbBoxUp();
  assert.equal(vm.sbBox, '0.2000,0.1000,0.8000,0.2500',
    '反向拖框必须规范化成 x0<x1、y0<y1：' + vm.sbBox);
  console.log('② 反向拖框自动规范化  ✅');
}

// ③ 越界要把坐标夹回 [0,1]
{
  const vm = makeVm();
  vm.sbBoxDown(ev(-500, -300));
  vm.sbBoxMove(ev(2000, 900));
  vm.sbBoxUp();
  assert.equal(vm.sbBox, '0.0000,0.0000,1.0000,1.0000', '越界未夹紧：' + vm.sbBox);
  console.log('③ 越界坐标夹紧  ✅');
}

// ④ 点一下就松手（误触）不能写入框
{
  const vm = makeVm();
  vm.sbBoxDown(ev(300, 100));
  vm.sbBoxUp();
  assert.equal(vm.sbBox, '', '误触不该写入框（否则会拿一个零面积框去 OCR）');
  console.log('④ 误触不写入  ✅');
}

// ⑤ 拖动过程中要有实时预览矩形，松手后清掉拖动状态
{
  const vm = makeVm();
  vm.sbBoxDown(ev(100, 50));
  vm.sbBoxMove(ev(400, 200));
  assert.ok(vm.sbDrag, '拖动中应当有 sbDrag 供实时预览');
  assert.ok(vm.sbRect && Math.abs(vm.sbRect.x1 - 0.4) < 1e-9,
    '拖动中 sbRect 要跟着鼠标：' + JSON.stringify(vm.sbRect));
  vm.sbBoxUp();
  assert.equal(vm.sbDrag, null, '松手后要清掉拖动状态');
  assert.ok(vm.sbRect, '松手后 sbRect 应当回落到已保存的 sbBox');
  console.log('⑤ 实时预览 + 松手回落  ✅');
}

// ⑥ 没有 sbBox 时 sbRect 返回 null（模板里 v-if 才不会画出空矩形）
{
  const vm = makeVm();
  assert.equal(vm.sbRect, null, '没框选时 sbRect 必须是 null');
  vm.sbBox = '坏的,数据,xx,yy';
  assert.equal(vm.sbRect, null, '解析不了的框选要当没有，不能画出乱七八糟的矩形');
  console.log('⑥ 无效框选返回 null  ✅');
}

// ⑦ 清空框选后回到自动定位
{
  const vm = makeVm();
  vm.sbBox = '0.1,0.2,0.3,0.4';
  vm.sbClearBox();
  assert.equal(vm.sbBox, '', '清空后应当留空 = 自动定位');
  assert.equal(vm.sbRect, null);
  console.log('⑦ 清空后回到自动定位  ✅');
}

// ⑧ 界面（模板）里不能再出现"让用户手算坐标"的提示
{
  const fs = require('node:fs');
  const raw = fs.readFileSync(path.resolve(__dirname, '../web/pages/upload.js'), 'utf8');
  // ⚠️ 只查**模板**，不查注释 —— 注释里会引用那句旧文案来说明问题
  //    （第一版查了整份源码，于是被自己的注释绊倒，假失败）。
  const i = raw.indexOf('\n  template:');
  const tplOnly = i > 0 ? raw.slice(i) : raw;
  assert.ok(!tplOnly.includes('例如 0.21,0.10,0.79,0.15'),
    '模板里不能再让用户手算归一化坐标（给示例数字就是一种劝退）');
  assert.ok(tplOnly.includes('sbBoxDown') && tplOnly.includes('sbBoxUp'),
    '模板必须绑上拖框的鼠标事件');
  assert.ok(tplOnly.includes('框选比分牌'), '要有"框选比分牌"的入口按钮');
  assert.ok(tplOnly.includes('@mousedown') && tplOnly.includes('@mouseup'),
    '拖框需要 mousedown/mouseup 事件');
  console.log('⑧ 界面不再要求手算坐标  ✅');
}

console.log('\n比分牌拖框选择：坐标换算正确、误触与越界都处理了（checks passed）');
