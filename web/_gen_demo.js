/* 临时脚本：生成 web/demo/ 下的合成演示数据（与后端 pipeline.py 产物结构一致）。
   仅用于「没有后端也能演示」的降级路径，不参与任何运行时逻辑。
   生成完毕可删除本文件：node web/_gen_demo.js */
'use strict';
const fs = require('fs');
const path = require('path');

const OUT = path.join(__dirname, 'demo');
fs.mkdirSync(OUT, { recursive: true });

// ---- 可复现随机数（mulberry32） ----
let _s = 20240607;
function rnd() {
  _s |= 0; _s = _s + 0x6D2B79F5 | 0;
  let t = Math.imul(_s ^ _s >>> 15, 1 | _s);
  t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
  return ((t ^ t >>> 14) >>> 0) / 4294967296;
}
function pick(a) { return a[Math.floor(rnd() * a.length)]; }
/** 与主随机流独立的哈希随机量：保证「位置」与「命中」统计独立 */
function hash01(i) {
  const s = Math.sin(i * 12.9898 + 78.233) * 43758.5453;
  return s - Math.floor(s);
}

// ---- 球场几何（与后端 model.py / rules.py 严格一致）----
// 坐标系：x = 横向（宽度 ±7.5），y = 纵向（长度 ±14），篮筐在 (0, ±1.575)。
// 千万不要写成 (±1.575, 0) —— 那等于把两根轴对调（历史 bug，已修）。
const HOOP_Y = 1.575, THREE_R = 6.75, CORNER_X = 6.60;
const PAINT_L = 5.80;          // 罚球线距底线
function distToHoop(x, y) {
  return Math.min(Math.hypot(x, y + HOOP_Y), Math.hypot(x, y - HOOP_Y));
}
function isThree(x, y) {
  return distToHoop(x, y) >= THREE_R || Math.abs(x) >= CORNER_X;
}
// 与后端 rules.zone_of 完全一致：先判三分（弧或横向底角直线），再细分；否则按距离分档
function zoneOf(x, y) {
  const d = distToHoop(x, y);
  if (isThree(x, y)) {
    if (Math.abs(x) >= CORNER_X) return '底角三分';
    if (Math.abs(x) >= 4.0) return '45°三分';
    return '弧顶三分';
  }
  if (d < 4.0) return '禁区';
  if (d < PAINT_L) return '近距离中投';
  return '长两分';
}

// accept/reject 采样：先随机生成，再用上面的规范判据过滤。
// 不能「按矩形范围采样再事后判分区」—— 三分线是弧线，矩形范围会采出
// 大量弧内两分球，导致三分占比严重偏低（这是实际踩过的坑）。
let _guard = 0;
function sampleTwo(rnd) {
  for (let i = 0; i < 800; i++) {
    const r = rnd();
    let d, lat;
    if (r < 0.45) { d = 0.3 + rnd() * 2.9; lat = (rnd() - 0.5) * 4.6; }        // 禁区
    else if (r < 0.80) { d = 3.5 + rnd() * 2.3; lat = (rnd() - 0.5) * 9.2; }   // 近距离中投
    else { d = 5.8 + rnd() * 0.9; lat = (rnd() - 0.5) * 12.8; }                // 长两分
    if (Math.abs(lat) <= Math.abs(d)) {
      const dy = Math.sqrt(d * d - lat * lat);
      for (const sgn of (rnd() < 0.5 ? [1, -1] : [-1, 1])) {
        const y = sgn * (HOOP_Y + dy), x = sgn * lat;
        if (Math.abs(x) <= 7.5 && Math.abs(y) <= 14 && !isThree(x, y)) {
          return { x, y, isThree: false, isFt: false };
        }
      }
    }
  }
  _guard++;
  return { x: (rnd() - 0.5) * 3, y: HOOP_Y + 1.2, isThree: false, isFt: false };
}
function sampleThree(rnd) {
  for (let i = 0; i < 800; i++) {
    if (rnd() < 0.38) {                       // 底角：横向幅度窄，直接按 |x| 采
      const x = (rnd() < 0.5 ? 1 : -1) * (6.70 + rnd() * 0.6);
      const d = 6.9 + rnd() * 1.5, dy2 = d * d - x * x;
      if (dy2 <= 0) continue;
      const y = HOOP_Y + Math.sqrt(dy2);
      if (Math.abs(x) <= 7.4 && y <= 14 && isThree(x, y)) {
        return { x, y, isThree: true, isFt: false };
      }
      continue;
    }
    // 45° 与弧顶：以「角度 + 距离」采样（角度以纵向为 0，向边线方向增大）
    const r = rnd();
    const a0 = r < 0.45 ? 0.56 : (r < 0.90 ? 0.10 : -0.52);   // 32° / 弧顶 / 左 45°
    const a1 = r < 0.45 ? 1.08 : (r < 0.90 ? 0.52 : -0.10);
    const ang = a0 + rnd() * (a1 - a0);
    const d = 6.9 + rnd() * 1.5;
    for (const sgn of (rnd() < 0.5 ? [1, -1] : [-1, 1])) {
      const x = sgn * d * Math.sin(ang);
      const y = sgn * (HOOP_Y + d * Math.cos(ang));
      if (Math.abs(x) <= 7.4 && Math.abs(y) <= 14 && isThree(x, y)) {
        return { x, y, isThree: true, isFt: false };
      }
    }
  }
  _guard++;
  return { x: 0, y: HOOP_Y + 7.2, isThree: true, isFt: false };
}

// ---- 一次出手采样：位置与结果用独立随机流，避免"强队"与"位置"相关 ----
function samplePos() {
  const r = rnd();
  if (r < 0.10) {                          // 罚球（约 10% 的出手，业余比赛常见量级）
    const sgn = rnd() < 0.5 ? 1 : -1;
    return { x: (rnd() - 0.5) * 0.6, y: sgn * (HOOP_Y + 4.6), isThree: false, isFt: true };
  }
  if (r < 0.78) return sampleTwo(rnd);     // 约 68% 两分
  return sampleThree(rnd);                 // 约 32% 三分
}

// ---- 球员 ----
const ROSTER = {
  home: [['H1', '红队 4号', 4], ['H2', '红队 7号', 7], ['H3', '红队 11号', 11],
    ['H4', '红队 15号', 15], ['H5', '红队 23号', 23], ['H6', '红队 33号', 33]],
  away: [['A1', '蓝队 3号', 3], ['A2', '蓝队 5号', 5], ['A3', '蓝队 9号', 9],
    ['A4', '蓝队 12号', 12], ['A5', '蓝队 21号', 21], ['A6', '蓝队 30号', 30]]
};
const players = [];
Object.keys(ROSTER).forEach(team => {
  ROSTER[team].forEach(([id, name, jersey]) => {
    players.push({
      player_id: id, name, jersey, team,
      points: 0, fgm: 0, fga: 0, fg_pct: 0, tpm: 0, tpa: 0, tp_pct: 0,
      ftm: 0, fta: 0, ft_pct: 0, efg: 0, ts: 0,
      reb: 0, ast: 0, stl: 0, blk: 0, tov: 0, pf: 0, shots: []
    });
  });
});
const byId = {};
players.forEach(p => byId[p.player_id] = p);

// ---- 生成 4 节比赛 ----
const duration = 4 * 12 * 60;   // 48 分钟（按业余比赛 4×12 计）
const timeline = [];
const highlightsCand = [];
const periods = 4;
for (let period = 1; period <= periods; period++) {
  const n = 40 + Math.floor(rnd() * 5);   // 每节 40~44 次出手
  // 两队出手次数均衡（各一半后打散）
  const teams = [];
  for (let i = 0; i < n; i++) teams.push(i % 2 ? 'home' : 'away');
  for (let i = teams.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    const tmp = teams[i]; teams[i] = teams[j]; teams[j] = tmp;
  }
  // 分两趟：第一趟只采位置（不让"位置随机流"与"命中随机流"互相干扰），
  // 第二趟用独立的随机流判命中，避免出现假性的球队强弱差。
  const raw = [];
  for (let i = 0; i < n; i++) {
    const team = teams[i];
    const roster = ROSTER[team];
    const pl = roster[Math.floor(rnd() * roster.length)];
    const pos = samplePos();
    const t = +((period - 1) * duration / periods + (i + rnd()) * (duration / periods) / n).toFixed(2);
    raw.push({ team, pl, pos, t });
  }
  // 每节给双方一点"手感"偏移（±0.04），让分节比分有起伏但不制造假性强弱差
  const hot = { home: (rnd() - 0.5) * 0.08, away: (rnd() - 0.5) * 0.08 };
  let k = 0;
  raw.forEach(item => {
    const pos = item.pos;
    const d = distToHoop(pos.x, pos.y);
    let p = pos.isFt ? 0.72 : pos.isThree ? 0.33 : d < 2 ? 0.60 : d < 4 ? 0.45 : 0.38;
    p = Math.max(0.12, Math.min(0.85, p + hot[item.team]));
    // 用与主随机流无关的哈希随机量判命中：保证「位置采样」与「是否命中」统计独立
    const made = hash01(period * 7919 + k++ * 104729 + (item.team === 'home' ? 13 : 29)) < p;
    const value = pos.isFt ? 1 : (pos.isThree || isThree(pos.x, pos.y) ? 3 : 2);
    const x = +pos.x.toFixed(3), y = +pos.y.toFixed(3);
    const e = {
      t: item.t, team: item.team, player_id: item.pl[0], player: item.pl[1],
      value, made, points: made ? value : 0, x, y, zone: zoneOf(x, y),
      confidence: +(0.55 + rnd() * 0.45).toFixed(3),
      source: pick(['ball_through_rim', 'scoreboard_ocr', 'net_motion', 'synthetic']),
      period
    };
    if (rnd() < 0.09) e.confidence = +(0.28 + rnd() * 0.28).toFixed(3);  // 制造低置信度样本
    timeline.push(e);
    if (made) highlightsCand.push(e);
  });
}
timeline.sort((a, b) => a.t - b.t);

// ---- 统计 ----
const teamStats = {
  home: { points: 0, fgm: 0, fga: 0, tpm: 0, tpa: 0, ftm: 0, fta: 0 },
  away: { points: 0, fgm: 0, fga: 0, tpm: 0, tpa: 0, ftm: 0, fta: 0 }
};
const quarter = [];
for (let p = 1; p <= periods; p++) quarter.push({ period: p, home: 0, away: 0 });
timeline.forEach(e => {
  const st = teamStats[e.team];
  st.points += e.points;
  quarter[e.period - 1][e.team] += e.points;
  if (e.value === 1) { st.fta++; if (e.made) st.ftm++; }
  else {
    st.fga++; if (e.made) st.fgm++;
    if (e.value === 3) { st.tpa++; if (e.made) st.tpm++; }
  }
  const p = byId[e.player_id];
  p.points += e.points;
  if (e.value === 1) { p.fta++; if (e.made) p.ftm++; }
  else {
    p.fga++; if (e.made) p.fgm++;
    if (e.value === 3) { p.tpa++; if (e.made) p.tpm++; }
  }
  p.shots.push({ t: e.t, x: e.x, y: e.y, made: e.made, value: e.value });
});
Object.keys(teamStats).forEach(k => {
  const st = teamStats[k];
  st.fg_pct = st.fga ? +(st.fgm / st.fga).toFixed(3) : 0;
  st.tp_pct = st.tpa ? +(st.tpm / st.tpa).toFixed(3) : 0;
});
players.forEach(p => {
  p.fg_pct = p.fga ? +(p.fgm / p.fga).toFixed(3) : 0;
  p.tp_pct = p.tpa ? +(p.tpm / p.tpa).toFixed(3) : 0;
  p.ft_pct = p.fta ? +(p.ftm / p.fta).toFixed(3) : 0;
  p.efg = p.fga ? +((p.fgm + 0.5 * p.tpm) / p.fga).toFixed(3) : 0;
  p.ts = (p.fga + 0.44 * p.fta) ? +(p.points / (2 * (p.fga + 0.44 * p.fta))).toFixed(3) : 0;
  p.reb = Math.floor(rnd() * 11);
  p.ast = Math.floor(rnd() * 8);
  p.stl = Math.floor(rnd() * 4);
  p.blk = Math.floor(rnd() * 3);
  p.tov = Math.floor(rnd() * 5);
  p.pf = Math.floor(rnd() * 5);
});
players.sort((a, b) => b.points - a.points);

// ---- 走势 ----
let h = 0, a = 0;
const progression = [{ t: 0, home: 0, away: 0 }];
timeline.forEach(e => {
  if (e.made) { if (e.team === 'home') h += e.points; else a += e.points; }
  progression.push({ t: e.t, home: h, away: a, scorer: e.player_id, value: e.value, made: e.made });
});

// ---- 需要复核（低置信度） ----
const needs_review = timeline
  .map((e, i) => Object.assign({ index: i }, e))
  .filter(e => e.confidence < 0.6)
  .map(e => ({
    t: e.t, team: e.team, player_id: e.player_id, x: e.x, y: e.y,
    value: e.value, made: e.made, result: e.made ? 'made' : 'missed',
    period: e.period, clock: 0, contest: 0,
    outcome_source: e.source, confidence: e.confidence,
    release_frame: 0, rim_frame: null, clip_start: Math.max(0, e.t - 2),
    clip_end: e.t + 1.5, tags: ['needs_review'],
    points: e.points, distance: +distToHoop(e.x, e.y).toFixed(3)
  }));

// ---- 热区 ----
function chart(points) {
  const zoneMap = {};
  points.forEach(p => {
    const z = zoneOf(p.x, p.y);
    const d = zoneMap[z] || (zoneMap[z] = { zone: z, att: 0, made: 0, points: 0 });
    d.att++; if (p.made) { d.made++; d.points += p.value; }
  });
  const zones = Object.keys(zoneMap).map(k => {
    const d = zoneMap[k];
    d.pct = d.att ? +(d.made / d.att).toFixed(3) : 0;
    d.pps = d.att ? +(d.points / d.att).toFixed(2) : 0;
    return d;
  }).sort((x, y) => y.att - x.att);

  const bin = 1.0, nx = 15, ny = 14;
  const grid = [];
  for (let iy = 0; iy < ny; iy++) {
    const row = [];
    for (let ix = 0; ix < nx; ix++) row.push({ att: 0, made: 0 });
    grid.push(row);
  }
  points.forEach(p => {
    const x = Math.abs(p.x), iy = Math.floor((p.y + 14) / bin), ix = Math.floor(x / bin);
    if (iy < 0 || iy >= ny || ix < 0 || ix >= nx) return;
    grid[iy][ix].att++; if (p.made) grid[iy][ix].made++;
  });
  return { points, zones, grid, bin_size: bin };
}

const allPoints = timeline.map(e => ({
  x: e.x, y: e.y, made: e.made, value: e.value, t: e.t,
  player_id: e.player_id, zone: e.zone, distance: +distToHoop(e.x, e.y).toFixed(2),
  team: e.team, period: e.period
}));
const shotchart = {
  all: chart(allPoints),
  by_team: {
    home: chart(allPoints.filter(p => p.team === 'home')).zones,
    away: chart(allPoints.filter(p => p.team === 'away')).zones
  }
};

// ---- 高光（前 12 个得分球，按精彩度：三分优先 + 末节加权） ----
const cand = timeline.filter(e => e.made).slice().sort((x, y) => {
  const sx = (x.value === 3 ? 2 : 1) + x.period * 0.3;
  const sy = (y.value === 3 ? 2 : 1) + y.period * 0.3;
  return sy - sx;
}).slice(0, 12).sort((x, y) => x.t - y.t);

const highlights = cand.map((e, i) => ({
  index: i + 1, t: e.t, start: Math.max(0, +(e.t - 2).toFixed(2)), end: +(e.t + 1.5).toFixed(2),
  path: 'highlights/clip_' + String(i + 1).padStart(2, '0') + '.mp4',
  made: e.made, value: e.value, team: e.team, player_id: e.player_id,
  label: e.player + ' ' + (e.value === 3 ? '三分' : e.value === 1 ? '罚球' : '两分') + '命中（' + e.zone + '）',
  available: false   /* 演示数据不含视频文件 */
}));

const game = {
  score: { home: teamStats.home.points, away: teamStats.away.points },
  teams: {
    home: { name: '红队', stats: teamStats.home },
    away: { name: '蓝队', stats: teamStats.away }
  },
  quarter_scores: quarter,
  progression,
  timeline: timeline.map(e => ({
    t: e.t, team: e.team, player_id: e.player_id, player: e.player,
    value: e.value, made: e.made, points: e.points, x: e.x, y: e.y,
    zone: e.zone, confidence: e.confidence, source: e.source, period: e.period
  })),
  periods, duration, fps: 25,
  needs_review,
  possessions: Math.round(timeline.length * 0.62),
  meta: { source: 'demo_fixture', fps: 25, duration, job_id: 'demo', note: '前端自带的合成演示数据；后端 demo 产出会覆盖此文件' },
  highlights
};

fs.writeFileSync(path.join(OUT, 'game.json'), JSON.stringify(game, null, 2), 'utf8');
fs.writeFileSync(path.join(OUT, 'players.json'), JSON.stringify(players, null, 2), 'utf8');
fs.writeFileSync(path.join(OUT, 'shotchart.json'), JSON.stringify(shotchart, null, 2), 'utf8');
fs.writeFileSync(path.join(OUT, 'highlights.json'), JSON.stringify({ clips: highlights, index_url: '/api/media/demo/highlights/index.json' }, null, 2), 'utf8');

// ---- report.json / report.md（与后端 report.build_report_* 同结构） ----
const hs = teamStats.home, as_ = teamStats.away;
const hotZones = shotchart.all.zones.filter(z => z.att >= 3).slice().sort((x, y) => y.pps - x.pps).slice(0, 4);
const keyShots = timeline.filter(e => e.made && (e.period >= 4 || e.value === 3))
  .sort((x, y) => (y.period - x.period) || (y.value - x.value) || (x.t - y.t))
  .slice(0, 5)
  .map(e => ({
    t: +e.t.toFixed(1), period: e.period, player_id: e.player_id, team: e.team,
    value: e.value, zone: e.zone, distance: +distToHoop(e.x, e.y).toFixed(2)
  }));

function top(list, key, n) {
  return list.filter(p => p[key] > 0).slice().sort((x, y) => y[key] - x[key]).slice(0, n || 3)
    .map(p => ({ player: p.name, value: p[key] }));
}
const reportJson = {
  title: '红队 vs 蓝队 比赛分析报告',
  final_score: { home: hs.points, away: as_.points },
  winner: hs.points > as_.points ? '红队' : (as_.points > hs.points ? '蓝队' : '平局'),
  margin: Math.abs(hs.points - as_.points),
  quarter_scores: quarter,
  team_stats: { home: hs, away: as_ },
  leaders: {
    home: { scoring: top(players.filter(p => p.team === 'home'), 'points'), rebounds: top(players.filter(p => p.team === 'home'), 'reb'), assists: top(players.filter(p => p.team === 'home'), 'ast') },
    away: { scoring: top(players.filter(p => p.team === 'away'), 'points'), rebounds: top(players.filter(p => p.team === 'away'), 'reb'), assists: top(players.filter(p => p.team === 'away'), 'ast') }
  },
  shooting: {
    home: { fgm: hs.fgm, fga: hs.fga, fg_pct: hs.fg_pct, tpm: hs.tpm, tpa: hs.tpa, tp_pct: hs.tp_pct, ftm: hs.ftm, fta: hs.fta, points: hs.points },
    away: { fgm: as_.fgm, fga: as_.fga, fg_pct: as_.fg_pct, tpm: as_.tpm, tpa: as_.tpa, tp_pct: as_.tp_pct, ftm: as_.ftm, fta: as_.fta, points: as_.points }
  },
  hot_zones: hotZones,
  key_shots: keyShots,
  confidence: {
    total: timeline.length,
    needs_review: needs_review.length,
    rate: +(1 - needs_review.length / Math.max(1, timeline.length)).toFixed(3)
  }
};
fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(reportJson, null, 2), 'utf8');

const mmss = t => String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(Math.floor(t % 60)).padStart(2, '0');
const L = [];
L.push('# ' + reportJson.title);
L.push('');
L.push('**最终比分：红队 ' + hs.points + ' : ' + as_.points + ' 蓝队**  ');
L.push('胜者：**' + reportJson.winner + '**，分差 ' + reportJson.margin + ' 分。');
L.push('');
L.push('> 本文件是前端自带的合成演示战报（结构等同后端 report.build_report_md 的产出）。');
L.push('> 后端跑通 demo 后会覆盖 web/demo/ 下的同名文件。');
L.push('');
L.push('## 一、分节比分');
L.push('');
L.push('| 节次 | 红队 | 蓝队 |');
L.push('|---|---|---|');
quarter.forEach(q => L.push('| 第' + q.period + '节 | ' + q.home + ' | ' + q.away + ' |'));
L.push('| **总计** | **' + hs.points + '** | **' + as_.points + '** |');
L.push('');
L.push('## 二、球队投篮数据');
L.push('');
L.push('| 指标 | 红队 | 蓝队 |');
L.push('|---|---|---|');
L.push('| 总得分 | ' + hs.points + ' | ' + as_.points + ' |');
L.push('| 投篮 | ' + hs.fgm + '/' + hs.fga + ' (' + (hs.fg_pct * 100).toFixed(1) + '%) | ' + as_.fgm + '/' + as_.fga + ' (' + (as_.fg_pct * 100).toFixed(1) + '%) |');
L.push('| 三分 | ' + hs.tpm + '/' + hs.tpa + ' (' + (hs.tp_pct * 100).toFixed(1) + '%) | ' + as_.tpm + '/' + as_.tpa + ' (' + (as_.tp_pct * 100).toFixed(1) + '%) |');
L.push('| 罚球 | ' + hs.ftm + '/' + hs.fta + ' | ' + as_.ftm + '/' + as_.fta + ' |');
L.push('');
const dFg = (hs.fg_pct - as_.fg_pct) * 100;
const dTp = hs.tpm - as_.tpm;
const parts = [];
if (Math.abs(dFg) >= 3) parts.push((dFg > 0 ? '红队' : '蓝队') + '的整体命中率高出 ' + Math.abs(dFg).toFixed(1) + ' 个百分点');
if (Math.abs(dTp) >= 2) parts.push((dTp > 0 ? '红队' : '蓝队') + '多命中 ' + Math.abs(dTp) + ' 记三分球');
const lead = hs.points > as_.points
  ? ('红队最终以 ' + (hs.points - as_.points) + ' 分优势取胜')
  : (as_.points > hs.points ? ('蓝队最终以 ' + (as_.points - hs.points) + ' 分优势取胜') : '双方战平');
L.push('**技术分析：**' + lead + '。' + (parts.length ? parts.join('；') + '。' : '双方在投篮效率上较为接近，胜负更多取决于失误与篮板球。'));
L.push('');
L.push('## 三、得分榜');
L.push('');
L.push('| 球员 | 球队 | 得分 | 投篮 | 三分 | 罚球 | 篮板 | 助攻 |');
L.push('|---|---|---|---|---|---|---|---|');
players.slice(0, 12).forEach(p => {
  L.push('| ' + p.name + ' | ' + p.team + ' | ' + p.points + ' | ' + p.fgm + '/' + p.fga + ' | ' +
    p.tpm + '/' + p.tpa + ' | ' + p.ftm + '/' + p.fta + ' | ' + p.reb + ' | ' + p.ast + ' |');
});
L.push('');
L.push('## 四、高效出手区域');
L.push('');
L.push('| 区域 | 出手 | 命中 | 命中率 | 每次出手得分 |');
L.push('|---|---|---|---|---|');
hotZones.forEach(z => L.push('| ' + z.zone + ' | ' + z.att + ' | ' + z.made + ' | ' + (z.pct * 100).toFixed(1) + '% | ' + z.pps + ' |'));
L.push('');
L.push('## 五、关键球');
L.push('');
keyShots.forEach(k => {
  L.push('- 第' + k.period + '节 ' + mmss(k.t) + ' — 球员 ' + k.player_id + ' 在' + k.zone + '（距篮 ' + k.distance + 'm）命中 ' + k.value + ' 分球');
});
L.push('');
L.push('## 六、数据可信度');
L.push('');
L.push('- 自动识别出手共 **' + timeline.length + '** 次，其中 **' + needs_review.length +
  '** 次置信度偏低已进入人工复核，自动判定准确率参考值 **' + (reportJson.confidence.rate * 100).toFixed(1) + '%**。');
L.push('- 低置信度事件在「复核页」一键修正，修正结果会写回统计口径。');
L.push('');
L.push('---');
L.push('');
L.push('*本报告由 AI 篮球分析软件自动生成：计分规则引擎负责 1/2/3 分判定与命中融合，统计口径与热区划分均为自研实现。*');
fs.writeFileSync(path.join(OUT, 'report.md'), L.join('\n'), 'utf8');

console.log('demo 数据生成完成 ->', OUT);
console.log('出手', timeline.length, '次；低置信度', needs_review.length, '次；比分',
  game.score.home, ':', game.score.away);
