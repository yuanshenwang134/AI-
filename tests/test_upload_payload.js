// 上传页「开始分析」的请求体契约（不用浏览器、不连后端）。
//
// 为什么单独测这一条：后端 `JobCreate.allow_no_calibration` 默认是 False，
// 没标定球场就会把任务直接拒掉（抛「尚未标定球场…」）；而上传页第 2 步的引导写着
// "不标也能分析"。两边口径必须一致 —— 曾经不一致，用户实测反馈
// 「不标定球场他就是不给分析」。这里把它钉住。
const assert = require('node:assert/strict');
global.window = { STORE: { backendOk: true } };
require(require('node:path').resolve(__dirname, '../web/pages/upload.js'));
const p = window.PAGES.upload;

function makeVm() {
  const v = { ...p.data(), $message: { warning() {}, info() {}, error() {}, success() {} } };
  for (const [k, fn] of Object.entries(p.methods)) v[k] = fn.bind(v);
  // computed 也要挂上：start() 会读 this.canSubmit（原本由 Vue 计算）
  for (const [k, fn] of Object.entries(p.computed || {})) {
    Object.defineProperty(v, k, { get: fn.bind(v), configurable: true });
  }
  v.logs = [];
  v.log = function (m) { this.logs.push(m); };
  v.subscribe = function () {};
  v.refreshJobs = function () {};
  v.S.backendOk = true;
  return v;
}

const sent = [];
global.window.API = {
  createJob(payload) { sent.push(payload); return Promise.resolve({ job_id: 'stub' }); }
};
global.window.APP = { registerJob() {} };

function startWith(vm, videoPath, calExists) {
  sent.length = 0;
  vm.form.source = 'video';
  vm.form.video_path = videoPath;
  vm.calInfo = { exists: !!calExists };
  vm.start();
  assert.equal(sent.length, 1, '应当提交一个任务');
  return sent[0];
}

// 1) 没标定 → 必须显式声明 allow_no_calibration = true（否则后端直接拒）
const noCal = startWith(makeVm(), 'D:\\videos\\a.mp4', false);
assert.equal(noCal.allow_no_calibration, true,
  '没标定时必须带 allow_no_calibration=true，否则点「开始分析」必被后端拒绝');
assert.equal(noCal.video_path, 'D:\\videos\\a.mp4');
assert.equal(noCal.source, 'video');

// 2) 已标定 → 不带（或显式 false），走正常的球场坐标口径
const withCal = startWith(makeVm(), 'D:\\videos\\a.mp4', true);
assert.ok(withCal.allow_no_calibration === false || withCal.allow_no_calibration === undefined,
  '已标定时不该声明"不要球场坐标"');

// 3) 没标定时要给用户一句看得懂的日志（说明这次少了什么）
const vm3 = makeVm();
startWith(vm3, 'D:\\videos\\b.mp4', false);
assert.ok(vm3.logs.some(l => l.includes('未标定球场') && l.includes('只判进球')),
  '未标定时应在运行日志里说明本次按「只判进球」跑');

// 4) jsonl 源不涉及标定/球场坐标，不该带这个字段影响后端判定
const vm4 = makeVm();
vm4.form.source = 'jsonl';
vm4.form.video_path = 'D:\\out\\x\\raw_track.json';
sent.length = 0;
vm4.start();
assert.equal(sent.length, 1);
assert.equal(sent[0].raw_path, 'D:\\out\\x\\raw_track.json');

// 5) 引导与文案必须跟真实行为一致（口径写错比没写更糟）
const vm5 = makeVm();
vm5.calInfo = { exists: false };
const step2 = vm5.guideSteps[1];
assert.ok(step2.title.includes('标定球场'), '引导第 2 步应是「标定球场」');
assert.ok(step2.desc.includes('不标也能分析'),
  '未标定时引导必须明说"不标也能分析"（否则用户以为卡住了）');
assert.ok(step2.desc.includes('热图') && step2.desc.includes('战术图'),
  '未标定的代价要写清楚：没有热图/战术图');
assert.ok(!p.template.includes('随机种子') && sent[0].seed === undefined,
  '上传页不该再有随机种子输入框，请求体也不该再发 seed（它只对合成比赛生效）');
assert.ok(p.template.includes('在画面上标篮筐') && p.template.includes('标球场（点场地特征点）'),
  '标篮筐 / 标球场两个入口都应在模板里');

console.log('Upload payload: allow_no_calibration contract passed (5 checks)');
