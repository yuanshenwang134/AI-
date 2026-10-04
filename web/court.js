/* ==========================================================================
   court.js —— 标准半场绘制工具（自研，无图形依赖）
   --------------------------------------------------------------------------
   坐标口径（与后端 model.py / rules.py 严格一致）：
     * 球场真实坐标，单位米，原点在球场中心
     * x ∈ [-7.5, 7.5]（FIBA 宽 15m），y ∈ [-14, 14]（长 28m）
     * 篮筐在 (±1.575, 0)
     * 三分弧半径 6.75m；底角三分直线距边线 0.90m（即横向 |x| >= 6.60）
     * 罚球区宽 4.90m、罚球线距底线 5.80m；罚球圈半径 1.80m；合理冲撞区半径 1.25m
     * 出手可能落在左半场（x<0）或右半场（x>0）
   --------------------------------------------------------------------------
   两种视图：
     half 半场视图 —— 把所有出手「镜像」到同一个半场（右侧篮筐），
                      这是热区分析的标准做法，左右底角三分重合显示。
                      归一化坐标：u = |x| ∈ [0,15]（0 是边线，篮筐在 1.575）
                                  v = |y| ∈ [0,14]（0 是底线，14 是中圈）
     full 全场视图 —— u = x + 7.5 ∈ [0,15]，v = y + 14 ∈ [0,28]，
                      左右半场各一个篮筐，用于展示原始出手分布。
   --------------------------------------------------------------------------
   这一层被 4 个页面复用：投篮热区（主）、球员统计（点击展开散点）、
   人工复核（位置缩略图）、比赛总览（可选）。
   ========================================================================== */
(function () {
  'use strict';

  // ---- FIBA 2014+ 几何常量（米），与后端 model.py 一一对应 ----
  //
  // 两套坐标要分清，别混：
  //   ① 数据坐标（mapXY 的输入）：x 横向 ±7.5，y 纵向 ±14，原点在中圈。
  //      篮筐在 (0, ±1.575)。
  //   ② 画布坐标（SVG 的 u,v，单位米）：半场视图 u ∈ [0,15]、v ∈ [0,14]，
  //      v=0 是底线、v=14 是中场线；全场视图 v ∈ [0,28]。
  //      所以篮筐画在 (7.5, 1.575)，而不是 (1.575, 0)。
  var C = {
    COURT_W: 15.0,        // 宽（横向）
    COURT_L: 28.0,        // 长（纵向）
    HALF_L: 14.0,         // 半场长
    HOOP_X: 7.5,          // 画布 u：篮筐在中线上（= 球场宽 / 2）
    HOOP_Y: 1.575,        // 画布 v：篮筐圆心距底线
    THREE_R: 6.75,        // 三分弧半径
    CORNER_X: 6.60,       // 底角三分交界：|数据x| >= 6.60（= 7.5 - 0.90）
    PAINT_W: 4.90,        // 罚球区宽
    PAINT_L: 5.80,        // 罚球线距底线
    FT_R: 1.80,           // 罚球圈半径
    RA_R: 1.25,           // 合理冲撞区半径
    BOARD_Y: 1.20,        // 篮板距底线
    BOARD_HALF: 0.90      // 篮板半长
  };
  // 底角三分直线所在的画布 u（两侧各一条）
  C.CORNER_U_LO = (C.COURT_W - C.CORNER_X * 2) / 2;   // 0.9
  C.CORNER_U_HI = C.COURT_W - C.CORNER_U_LO;          // 14.1

  // 视图参数
  function viewOf(view) {
    return view === 'full'
      ? {
        view: 'full', W: C.COURT_W, H: C.COURT_L, pad: 1.5,
        label: '全场 15m × 28m · 左右半场各一篮筐'
      }
      : {
        view: 'half', W: C.COURT_W, H: C.HALF_L, pad: 1.3,
        label: '半场视图 15m × 14m · 左右半场出手已镜像到同一侧'
      };
  }

  /** 球场真实坐标 -> 画布坐标（米）。半场视图把两侧叠到一起。 */
  function mapXY(view, x, y) {
    if (view === 'full') return { u: Number(x) + C.COURT_W / 2, v: Number(y) + C.COURT_L / 2 };
    // 半场：|x| 镜像到「篮筐所在的半场」（u 从边线量起），|y| 把两侧底线都叠到底部。
    // 说明：严格的手性镜像应该是 (x,y) -> (-x,-y)，这里用了 (|x|,|y|)。
    // 对热区图没有影响，因为分区判据（底角看 |x|、距离看勾股）与镜像都对称，
    // 点的「到篮筐距离」和「分区」完全一致，只是在半场里的朝向略有差别。
    return { u: Math.abs(Number(x)), v: Math.abs(Number(y)) };
  }

  // ---------------------------------------------------------------- 工具
  function n(v) { return Math.round(v * 1000) / 1000; }
  function f(v) { return Math.round(v * 100) / 100; }

  // ---------------------------------------------------------------- 几何路径
  /**
   * 分区路径（画布坐标下的 SVG path 字符串）。
   *
   * 这里**不手写几何**，而是把半场切成小格、用与后端 rules.zone_of 完全相同的
   * 判据逐格分类，再把同分区的相邻格合并成矩形条。
   *
   * 为什么这么做：手写 6 个分区的边界弧（4.0m 弧 / 5.8m 弧 / 6.75m 三分弧 /
   * 底角直线 / 罚球区矩形，还要左右对称）非常容易写错，而且一旦后端改了
   * 分区口径，手写路径就会和散点分层矛盾 —— 热区图上会出现「点落在错误分区里」。
   * 用同一份规则生成网格，口径天然一致。
   */
  var _zoneCache = {};
  function zonePaths(view) {
    if (view === 'full') return {};
    if (_zoneCache.grid) return _zoneCache.grid;

    var HU = C.HOOP_X, HV = C.HOOP_Y;            // 画布坐标下的篮筐
    var R3 = C.THREE_R, CX = C.CORNER_X;
    var FT_Y = C.PAINT_L;

    // 与 data.js D.zoneOf 同口径：输入是**数据坐标**（x 横向、y 纵向），
    // 这里把画布坐标换算成数据坐标后复用同一判据，保证两边永不漂移。
    function zoneAtCanvas(u, v) {
      var x = u - C.COURT_W / 2;      // 画布 u -> 数据 x（横向）
      var y = v;                      // 画布 v -> 数据 y（纵向，半场 v>=0）
      var d = Math.min(Math.hypot(x, y + 1.575), Math.hypot(x, y - 1.575));
      if (d >= R3 || Math.abs(x) >= CX) {
        if (Math.abs(x) >= CX) return '底角三分';
        if (Math.abs(x) >= 4.0) return '45°三分';
        return '弧顶三分';
      }
      if (d < 4.0) return '禁区';
      if (d < 5.8) return '近距离中投';
      return '长两分';
    }

    var BIN = 0.15;                       // 分类网格粒度（米）
    var NX = Math.round(C.COURT_W / BIN);
    var NY = Math.round(C.HALF_L / BIN);
    var byZone = {};

    for (var iy = 0; iy < NY; iy++) {
      for (var ix = 0; ix < NX; ix++) {
        var u = (ix + 0.5) * BIN, v = (iy + 0.5) * BIN;
        var z = zoneAtCanvas(u, v);
        (byZone[z] || (byZone[z] = {}))[iy] = (byZone[z][iy] || 0) | 0;
        byZone[z][iy + ':' + ix] = 1;
      }
    }

    // 同分区、同行、连续的小格合并成一条矩形（path），减少 DOM 节点
    var MAX_SPAN = 2.0;                   // 单条最多跨 2m，避免边界过于粗糙
    var out = {};
    Object.keys(byZone).forEach(function (zname) {
      var cells = byZone[zname];
      var d = [];
      Object.keys(cells).forEach(function (key) {
        if (key.indexOf(':') < 0) return;  // 跳过行标记
        var parts = key.split(':');
        var iy = +parts[0], ix = +parts[1];
        if (!cells[key]) return;           // 已被前一条吃掉
        var u0 = ix * BIN;
        var n = 1;
        while (cells[iy + ':' + (ix + n)] && n * BIN < MAX_SPAN) n++;
        for (var k = 1; k < n; k++) cells[iy + ':' + (ix + k)] = 0;
        var w = n * BIN;
        var v0 = iy * BIN;
        d.push('M ' + f(u0) + ' ' + f(v0) + ' h ' + f(w) + ' v ' + f(BIN) +
          ' h ' + f(-w) + ' Z');
      });
      out[zname] = d.join(' ');
    });
    _zoneCache.grid = out;
    return out;
  }

  /**
   * 绘制球场线（SVG 字符串）。
   * opts: { view, showLabels, highlight }
   */
  function courtSVG(opts) {
    opts = opts || {};
    var v = viewOf(opts.view);
    var WA = v.W + v.pad * 2, HA = v.H + v.pad * 2;
    var HU = C.HOOP_X, HV = C.HOOP_Y;      // 篮筐画布坐标 (7.5, 1.575)
    var R3 = C.THREE_R;

    var s = [];

    s.push('<svg class="court-svg" viewBox="0 0 ' + n(WA) + ' ' + n(HA) + '" ' +
      'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">');
    // 地板
    s.push('<rect x="' + v.pad + '" y="' + v.pad + '" width="' + v.W + '" height="' + v.H +
      '" fill="#fbf8f2" stroke="#c9b48f" stroke-width="0.06"/>');

    // 篮筐：中场线的中点是球场中心（画布 u = 7.5）。
    // 半场视图只画靠近底线的那一个；全场视图两端各一个。
    var hoops = v.view === 'full'
      ? [{ hv: HV, dir: 1 }, { hv: C.COURT_L - HV, dir: -1 }]
      : [{ hv: HV, dir: 1 }];

    hoops.forEach(function (hp) {
      var hv = hp.hv, dir = hp.dir;        // dir=1 表示罚球区朝 v 增大方向（中场）

      function p(u, w) { return f(u + v.pad) + ' ' + f(w + v.pad); }

      // 罚球区（KEY）：宽 4.90m、长 5.80m，以篮筐横向中心为中轴
      s.push('<path d="M ' + p(HU - C.PAINT_W / 2, hv) +
        ' L ' + p(HU + C.PAINT_W / 2, hv) +
        ' L ' + p(HU + C.PAINT_W / 2, hv + dir * C.PAINT_L) +
        ' L ' + p(HU - C.PAINT_W / 2, hv + dir * C.PAINT_L) + ' Z"' +
        ' fill="#f6efe1" stroke="#c9b48f" stroke-width="0.05"/>');

      // 罚球圈（半径 1.80m，圆心在罚球线中点）
      s.push('<circle cx="' + f(HU + v.pad) + '" cy="' + f(hv + dir * C.PAINT_L + v.pad) +
        '" r="' + C.FT_R + '" fill="none" stroke="#c9b48f" stroke-width="0.05"/>');

      // 合理冲撞区（半圆，半径 1.25m，朝中场一侧开口）
      var sweep = dir > 0 ? 0 : 1;
      s.push('<path d="M ' + p(HU - C.RA_R, hv) + ' A ' + C.RA_R + ' ' + C.RA_R +
        ' 0 0 ' + sweep + ' ' + p(HU + C.RA_R, hv) + '"' +
        ' fill="none" stroke="#c9b48f" stroke-width="0.05"/>');

      // 三分线：两条底角直线 + 一段大圆弧。
      // 弧与底角直线的交点：|Δu| = 6.60 处的 v 偏移
      var dv = Math.sqrt(R3 * R3 - C.CORNER_X * C.CORNER_X);  // ≈1.41
      var vJoin = hv + dir * dv;
      var muchFar = dir > 0;                 // 弧是往中场方向鼓出来的
      s.push('<path d="M ' + p(C.CORNER_U_LO, hv + dir * 0) +
        ' L ' + p(C.CORNER_U_LO, vJoin) +
        '" stroke="#c9b48f" stroke-width="0.05" fill="none"/>');
      s.push('<path d="M ' + p(C.CORNER_U_HI, hv) +
        ' L ' + p(C.CORNER_U_HI, vJoin) +
        '" stroke="#c9b48f" stroke-width="0.05" fill="none"/>');
      // 从左上交点沿弧绕到右上交点，经过弧顶（v = hv + dir*6.75）
      s.push('<path d="M ' + p(C.CORNER_U_LO, vJoin) +
        ' A ' + R3 + ' ' + R3 + ' 0 0 ' + (muchFar ? 1 : 0) + ' ' +
        p(C.CORNER_U_HI, vJoin) + '" fill="none" stroke="#c9b48f" stroke-width="0.05"/>');

      // 篮板 + 篮筐
      s.push('<path d="M ' + p(HU - C.BOARD_HALF, hv - dir * (HV - C.BOARD_Y)) +
        ' L ' + p(HU + C.BOARD_HALF, hv - dir * (HV - C.BOARD_Y)) +
        '" stroke="#8f7a55" stroke-width="0.09"/>');
      s.push('<circle cx="' + f(HU + v.pad) + '" cy="' + f(hv + v.pad) + '" r="0.225" ' +
        'fill="none" stroke="#e2823c" stroke-width="0.08"/>');
    });

    // 全场：中线 + 中圈
    if (v.view === 'full') {
      s.push('<line x1="' + f(v.pad) + '" y1="' + f(C.HALF_L + v.pad) + '" x2="' +
        f(v.pad + C.COURT_W) + '" y2="' + f(C.HALF_L + v.pad) +
        '" stroke="#c9b48f" stroke-width="0.05"/>');
      s.push('<circle cx="' + f(C.COURT_W / 2 + v.pad) + '" cy="' + f(C.HALF_L + v.pad) +
        '" r="1.8" fill="none" stroke="#c9b48f" stroke-width="0.05"/>');
    }

    // 镜像提示 + 方位标注（半场视图）
    if (v.view !== 'full') {
      s.push('<text x="' + f(v.pad + 7.5) + '" y="' + f(v.pad + 13.6) + '" text-anchor="middle" ' +
        'font-size="0.62" fill="#a89a80">中圈方向 · 半场 14m</text>');
      s.push('<text x="' + f(v.pad + 0.15) + '" y="' + f(v.pad + 0.75) + '" font-size="0.55" ' +
        'fill="#b3a68d">底线</text>');
      s.push('<text x="' + f(v.pad + 13.4) + '" y="' + f(v.pad + 7.2) + '" font-size="0.55" ' +
        'fill="#b3a68d">边线</text>');
      s.push('<text x="' + f(v.pad + 8.3) + '" y="' + f(v.pad + 8.4) + '" font-size="0.55" ' +
        'fill="#b3a68d">6.75m</text>');
      s.push('<text x="' + f(v.pad + 2.5) + '" y="' + f(v.pad + 7.9) + '" font-size="0.55" ' +
        'fill="#b3a68d">底角三分线</text>');
    }
    s.push('</svg>');
    return s.join('');
  }

  // ---------------------------------------------------------------- 叠加图层
  /**
   * 出手散点层（SVG 字符串）。
   * 命中 = 实心绿圆，未中 = 空心红圈；三分球加一圈描边以示区别。
   * points: shotchart.json 的 all.points（或 players.shots 归集出来的点，字段一致）
   */
  function shotLayer(points, o) {
    o = o || {};
    var v = viewOf(o.view);
    var r = o.dotR || 0.20;
    var max = o.max || 900;                 // 性能保护
    var list = points || [];
    var step = list.length > max ? Math.ceil(list.length / max) : 1;
    var s = ['<svg class="court-svg" viewBox="0 0 ' + n(v.W + v.pad * 2) + ' ' + n(v.H + v.pad * 2) +
      '" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'];

    if (o.useHeat) {
      // 热力模式：按局部密度着色（每个点一个半透明圆，叠加成密度云）
      for (var i = 0; i < list.length; i += step) {
        var p = list[i];
        var q = mapXY(v.view, p.x, p.y);
        s.push('<circle cx="' + f(q.u + v.pad) + '" cy="' + f(q.v + v.pad) + '" r="0.75" ' +
          'fill="' + (p.made ? 'rgba(34,160,107,0.20)' : 'rgba(229,72,77,0.14)') + '"/>');
      }
    }

    for (var j = 0; j < list.length; j += step) {
      var pt = list[j];
      var q2 = mapXY(v.view, pt.x, pt.y);
      var cx = f(q2.u + v.pad), cy = f(q2.v + v.pad);
      var tip = '第' + (pt.period ? pt.period + '节 ' : '') + D.mmss(pt.t) + ' · ' +
        (pt.player_id || '未知球员') + ' · ' + D.shotLabel(pt.value, pt.made) +
        ' · ' + (pt.zone || D.zoneOf(pt.x, pt.y)) +
        (pt.distance !== undefined ? ' · 距篮 ' + pt.distance + 'm' : '');
      var fill = pt.made ? (o.madeColor || '#22a06b') : 'rgba(255,255,255,0.0)';
      var stroke = pt.made ? 'rgba(255,255,255,0.85)' : (o.missColor || '#e5484d');
      s.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + fill +
        '" stroke="' + stroke + '" stroke-width="' + (pt.value === 3 ? 0.075 : 0.055) + '">' +
        '<title>' + tip + '</title></circle>');
      if (pt.value === 3) {
        s.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + (r + 0.10) + '" fill="none" ' +
          'stroke="' + (pt.made ? 'rgba(34,160,107,0.55)' : 'rgba(229,72,77,0.45)') +
          '" stroke-width="0.035"/>');
      }
    }
    s.push('</svg>');
    return s.join('');
  }

  /**
   * 网格热力层（SVG 字符串）。
   * grid 口径与后端 rules.shot_chart 的 grid 完全一致：
   *   grid[iy][ix]，iy = floor(y / bin)（0 = 底线，13 = 中场线侧）—— 纵向
   *                   ix = floor((x + 7.5) / bin)（0 = 左边线，篮筐在中线 7.5）—— 横向
   * 画布坐标与网格下标一一对应：屏幕 x = ix*bin，屏幕 y = iy*bin。
   * metric: 'pct' 命中率 | 'att' 出手量
   */
  function gridLayer(grid, o) {
    o = o || {};
    var v = viewOf(o.view === 'full' ? 'half' : (o.view || 'half'));
    if (v.view === 'full') return '';
    var bin = o.bin || 1.0;
    var metric = o.metric || 'pct';
    var rows = grid || [];
    var s = ['<svg class="court-svg" viewBox="0 0 ' + n(v.W + v.pad * 2) + ' ' + n(v.H + v.pad * 2) +
      '" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'];

    var maxAtt = 0;
    rows.forEach(function (r) { r.forEach(function (c) { maxAtt = Math.max(maxAtt, c.att || 0); }); });

    rows.forEach(function (row, iy) {
      row.forEach(function (cell, ix) {
        if (!cell.att) return;
        var pct = cell.att ? cell.made / cell.att : 0;
        var color;
        if (metric === 'att') {
          var a = maxAtt ? cell.att / maxAtt : 0;
          color = 'rgba(47,111,237,' + (0.12 + 0.62 * a).toFixed(3) + ')';
        } else {
          color = D.zoneColor(pct, cell.att);   // 冷蓝 -> 热红
        }
        var x = ix * bin, y = iy * bin;
        s.push('<rect x="' + f(x + v.pad) + '" y="' + f(y + v.pad) + '" width="' + bin +
          '" height="' + bin + '" fill="' + color + '">' +
          '<title>' + (metric === 'att'
            ? '出手 ' + cell.att + ' 次'
            : '出手 ' + cell.att + ' 次 / 命中 ' + cell.made + ' 次 / 命中率 ' + D.pct(pct)) +
          '</title></rect>');
      });
    });
    s.push('</svg>');
    return s.join('');
  }

  /**
   * 分区热力层（SVG 字符串）：按分区命中率着色，标注「出手数 + 命中率 + 每回合得分」。
   * zones: shotchart.json 的 all.zones 或 by_team[side]（数组）
   */
  function zoneLayer(zones, o) {
    o = o || {};
    var v = viewOf('half');
    var paths = zonePaths('half');
    var map = {};
    (zones || []).forEach(function (z) { map[z.zone] = z; });
    var s = ['<svg class="court-svg" viewBox="0 0 ' + n(v.W + v.pad * 2) + ' ' + n(v.H + v.pad * 2) +
      '" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'];

    Object.keys(paths).forEach(function (name) {
      var z = map[name];
      var att = z ? z.att : 0;
      var pct = z && z.att ? (z.pct > 1 ? z.pct / 100 : z.pct) : 0;
      var fill = att ? D.zoneColor(pct, att) : 'rgba(238,241,246,0.35)';
      s.push('<path d="' + paths[name] + '" fill="' + fill + '" stroke="#ffffff" ' +
        'stroke-width="0.035"><title>' + name +
        (att ? '：出手 ' + att + ' 次 / 命中 ' + z.made + ' 次 / 命中率 ' + D.pct(pct) +
          ' / 每次出手 ' + z.pps + ' 分' : '：本场无出手') + '</title></path>');
    });
    // 分区标签位置（画布坐标 u,v，单位米）—— 放在各分区的视觉重心上
    var labels = {
      '禁区': [7.5, 4.4],
      '近距离中投': [7.5, 6.9],
      '长两分': [7.5, 9.6],
      '底角三分': [2.2, 4.6],
      '45°三分': [2.9, 8.6],
      '弧顶三分': [7.5, 11.2]
    };
    Object.keys(labels).forEach(function (name) {
      var z = map[name];
      if (!z || !z.att) return;
      var pct = z.pct > 1 ? z.pct / 100 : z.pct;
      var p = labels[name];
      s.push('<text x="' + f(p[0] + v.pad) + '" y="' + f(p[1] + v.pad) + '" text-anchor="middle" ' +
        'font-size="0.42" font-weight="700" fill="#22303f" style="paint-order:stroke;stroke:#ffffff;stroke-width:0.14">' +
        name + '</text>');
      s.push('<text x="' + f(p[0] + v.pad) + '" y="' + f(p[1] + 0.64 + v.pad) + '" text-anchor="middle" ' +
        'font-size="0.4" fill="#33414f" style="paint-order:stroke;stroke:#ffffff;stroke-width:0.14">' +
        z.made + '/' + z.att + ' · ' + D.pct(pct) + '</text>');
    });
    s.push('</svg>');
    return s.join('');
  }

  /** 位置缩略图（复核页用）：把一次出手放在标准半场上，带位置标记 */
  function shotThumb(shot, o) {
    o = o || {};
    return shotLayer([{
      x: shot.x, y: shot.y, made: !!shot.made, value: shot.value || 2,
      t: shot.t, player_id: shot.player_id, zone: shot.zone || D.zoneOf(shot.x, shot.y),
      distance: shot.distance === undefined ? D.distanceToHoop(shot.x, shot.y) : shot.distance
    }], { view: 'half', dotR: 0.34 });
  }

  /**
   * 组合视图：球场 + 一个叠加层。
   * layer: '' | 'shots' | 'zones' | 'grid'
   */
  function compose(cfg) {
    cfg = cfg || {};
    var court = courtSVG({ view: cfg.view || 'half' });
    var layer = '';
    if (cfg.layer === 'zones') layer = zoneLayer(cfg.zones, cfg);
    else if (cfg.layer === 'grid') layer = gridLayer(cfg.grid, cfg);
    else if (cfg.layer === 'shots') layer = shotLayer(cfg.points, cfg);
    return { court: court, layer: layer, view: viewOf(cfg.view || 'half') };
  }

  // ================================================================
  // 俯视战术图（bird's-eye）相关图层
  // ----------------------------------------------------------------
  // 数据口径（与后端 tactics.py 严格一致）：
  //   x 横向 ±7.5（x=0 是中轴）；y 的符号表示哪半场，|y| 是离本方底线的距离。
  //   「局部进攻坐标」= (x, d)，d = |y| ∈ [0,14]，被进攻的篮筐永远在 (0, 1.575)。
  // 所以战术图只需要画**一张半场**：横轴放 x、纵轴放 d，
  // 左右两侧的进攻会自动叠在一起（这正是战术层"折半"的意义）。
  //
  // 画布坐标：u = x + 7.5 + pad、v = d + pad —— 与 courtSVG({view:'half'})
  // 完全对齐，因此「球场层」和「球员层」两张 SVG 只要 viewBox 相同就能严丝合缝。
  // ================================================================
  var TEAM_COLOR = { home: '#2f6fed', away: '#f2762e' };

  /** 数据坐标 -> 半场画布坐标（含 pad） */
  function mapTactics(x, y) {
    var v = viewOf('half');
    return { u: Number(x) + C.COURT_W / 2 + v.pad,
             v: Math.abs(Number(y)) + v.pad };
  }

  /** 半场画布坐标 -> 数据坐标（点击战术图反查用） */
  function unmapTactics(u, v) {
    var vw = viewOf('half');
    return { x: Number(u) - vw.pad - C.COURT_W / 2,
             y: Number(v) - vw.pad };
  }

  function tacticsViewBox() {
    var v = viewOf('half');
    return '0 0 ' + n(v.W + v.pad * 2) + ' ' + n(v.H + v.pad * 2);
  }

  /** 半场画布尺寸（给 ECharts 之类的像素空间用） */
  function tacticsSize(px) {
    var v = viewOf('half');
    var w = v.W + v.pad * 2, h = v.H + v.pad * 2;
    var k = (px || 560) / w;
    return { w: Math.round(w * k), h: Math.round(h * k), k: k };
  }

  function _name(p) { return (p && (p.name || p.id)) || '?'; }

  /**
   * 传球网络图层：画在半场上（节点按球员平均站位摆，边宽 ∝ 传球次数）。
   * net: { nodes:[{id,name,x,d,passes,received}], edges:[{source,target,value}] }
   * opts: { team, showLabels, highlight }
   */
  function passNetLayer(net, o) {
    o = o || {};
    var vb = tacticsViewBox();
    var color = TEAM_COLOR[o.team] || '#2f6fed';
    var s = ['<svg class="court-svg" viewBox="' + vb + '" preserveAspectRatio="xMidYMid meet" ' +
      'xmlns="http://www.w3.org/2000/svg">'];

    var nodes = (net && net.nodes) || [];
    var edges = (net && net.edges) || [];
    var byId = {};
    nodes.forEach(function (p) { byId[p.id] = p; });
    var maxV = 1;
    edges.forEach(function (e) { maxV = Math.max(maxV, e.value || 1); });

    // ---- 边 ----
    edges.forEach(function (e) {
      var a = byId[e.source], b = byId[e.target];
      if (!a || !b) return;
      var qa = mapTactics(a.x, a.d), qb = mapTactics(b.x, b.d);
      // 双向传球时两条线会完全重合，按 id 顺序做一点点偏移，避免互相盖住
      var off = (String(e.source) < String(e.target)) ? 0.12 : -0.12;
      var w = 0.035 + 0.16 * ((e.value || 1) / maxV);
      s.push('<line x1="' + f(qa.u) + '" y1="' + f(qa.v) + '" x2="' + f(qb.u) +
        '" y2="' + f(qb.v) + '" stroke="' + color + '" stroke-opacity="0.55" ' +
        'stroke-width="' + f(w) + '" stroke-linecap="round" data-off="' + off + '">' +
        '<title>' + _name(a) + ' → ' + _name(b) + '：' + (e.value || 0) + ' 次</title></line>');
    });

    // ---- 节点 ----
    var maxP = 1;
    nodes.forEach(function (p) { maxP = Math.max(maxP, (p.passes || 0) + (p.received || 0)); });
    nodes.forEach(function (p) {
      var q = mapTactics(p.x, p.d);
      var tot = (p.passes || 0) + (p.received || 0);
      var r = 0.30 + 0.24 * (tot / maxP);
      s.push('<circle cx="' + f(q.u) + '" cy="' + f(q.v) + '" r="' + f(r) + '" ' +
        'fill="#ffffff" stroke="' + color + '" stroke-width="0.09">' +
        '<title>' + _name(p) + '：传球 ' + (p.passes || 0) + ' / 接球 ' +
        (p.received || 0) + '</title></circle>');
      s.push('<text x="' + f(q.u) + '" y="' + f(q.v + 0.16) + '" text-anchor="middle" ' +
        'font-size="' + f(r * 0.9) + '" font-weight="700" fill="' + color + '">' +
        tot + '</text>');
      if (o.showLabels !== false) {
        s.push('<text x="' + f(q.u) + '" y="' + f(q.v - r - 0.14) + '" text-anchor="middle" ' +
          'font-size="0.46" fill="#33414f" style="paint-order:stroke;stroke:#ffffff;stroke-width:0.16">' +
          _name(p) + '</text>');
      }
    });
    s.push('</svg>');
    return s.join('');
  }

  /**
   * 俯视战术图的「球员 + 球 + 轨迹」图层。
   * frame: { t, side, ball:[x,y]|null, players:[[id,team,x,y], ...] }
   * trails: { player_id: [[x,y], ...] }  最近若干帧的位置（画淡色尾迹）
   * opts: { showTrails, highlight:[player_id...] }
   */
  function tacticsPlayersLayer(frame, trails, o) {
    o = o || {};
    var vb = tacticsViewBox();
    var s = ['<svg class="court-svg" viewBox="' + vb + '" preserveAspectRatio="xMidYMid meet" ' +
      'xmlns="http://www.w3.org/2000/svg">'];

    // ---- 轨迹尾迹 ----
    if (o.showTrails !== false && trails) {
      Object.keys(trails).forEach(function (pid) {
        var seq = trails[pid] || [];
        if (seq.length < 2) return;
        var team = (frame.teamOf && frame.teamOf[pid]) || o.teamOf && o.teamOf[pid] || 'home';
        var pts = seq.map(function (p) {
          var q = mapTactics(p[0], p[1]);
          return f(q.u) + ',' + f(q.v);
        }).join(' ');
        s.push('<polyline points="' + pts + '" fill="none" stroke="' +
          (TEAM_COLOR[team] || '#94a3b8') + '" stroke-opacity="0.30" ' +
          'stroke-width="0.075" stroke-linecap="round" stroke-linejoin="round"/>');
      });
    }

    // ---- 球员 ----
    (frame.players || []).forEach(function (p) {
      var pid = p[0], team = p[1], x = p[2], y = p[3];
      var q = mapTactics(x, y);
      var col = TEAM_COLOR[team] || '#64748b';
      var hi = (o.highlight || []).indexOf(pid) >= 0;
      s.push('<circle cx="' + f(q.u) + '" cy="' + f(q.v) + '" r="' + (hi ? 0.42 : 0.34) + '" ' +
        'fill="' + col + '" stroke="#ffffff" stroke-width="0.09" opacity="0.95">' +
        '<title>' + pid + ' · ' + (team === 'home' ? '主队' : '客队') +
        ' · x=' + f(x) + ' y=' + f(y) + '</title></circle>');
      s.push('<text x="' + f(q.u) + '" y="' + f(q.v + 0.13) + '" text-anchor="middle" ' +
        'font-size="0.40" font-weight="700" fill="#ffffff">' + String(pid).slice(-2) + '</text>');
    });

    // ---- 球 ----
    if (frame.ball) {
      var qb = mapTactics(frame.ball[0], frame.ball[1]);
      s.push('<circle cx="' + f(qb.u) + '" cy="' + f(qb.v) + '" r="0.24" fill="#e2823c" ' +
        'stroke="#ffffff" stroke-width="0.07"><title>球</title></circle>');
    }
    s.push('</svg>');
    return s.join('');
  }

  /**
   * 阵型时间轴图层（纯 SVG 横条）：每段一个色块，宽度 ∝ 时长。
   * segments: tactics.formation.offense[team] / defense[team]
   * opts: { total, colorOf(label), ganttW }
   */
  function formationBar(segments, o) {
    o = o || {};
    var total = o.total || 1;
    var W = 100, H = 2.2;                       // 用百分比宽度，viewBox 只做高度
    var s = ['<svg class="formation-bar" viewBox="0 0 100 ' + H + '" preserveAspectRatio="none" ' +
      'xmlns="http://www.w3.org/2000/svg">'];
    (segments || []).forEach(function (g) {
      var x0 = (g.t0 / total) * W, w = Math.max(0.15, ((g.t1 - g.t0) / total) * W);
      var col = (o.colorOf || function () { return '#94a3b8'; })(g);
      s.push('<rect x="' + f(x0) + '" y="0" width="' + f(w) + '" height="' + H +
        '" fill="' + col + '" fill-opacity="0.85" stroke="#ffffff" stroke-width="0.05">' +
        '<title>' + (g.shape || g.scheme) + ' · ' + D.mmss(g.t0) + '–' + D.mmss(g.t1) +
        '</title></rect>');
    });
    s.push('</svg>');
    return s.join('');
  }

  window.Court = {
    C: C,
    viewOf: viewOf,
    mapXY: mapXY,
    courtSVG: courtSVG,
    zonePaths: zonePaths,
    shotLayer: shotLayer,
    gridLayer: gridLayer,
    zoneLayer: zoneLayer,
    shotThumb: shotThumb,
    mapTactics: mapTactics,
    unmapTactics: unmapTactics,
    tacticsViewBox: tacticsViewBox,
    tacticsSize: tacticsSize,
    passNetLayer: passNetLayer,
    tacticsPlayersLayer: tacticsPlayersLayer,
    formationBar: formationBar,
    TEAM_COLOR: TEAM_COLOR,
    compose: compose
  };
})();
