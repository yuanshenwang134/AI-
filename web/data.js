/* ==========================================================================
   data.js —— 数据口径工具层（纯函数，无副作用）
   --------------------------------------------------------------------------
   职责：
     1. 数值/时间/百分比格式化（中文篮球行话：命中率、三分率、真实命中率…）
     2. 数据规范化：把 game.json / players.json / shotchart.json 补齐成
        前端各页面都能安心读取的形状（缺字段不算崩，演示数据也能撑住）
     3. 关键指标计算：分节比分补全、热区聚合、球员出手归集
     4. 导出兜底：后端 /export 不可用时，用本地数据现场生成 CSV
     5. 极简 Markdown 渲染（标题/表格/列表/粗体/链接），用于战报页
   ========================================================================== */
(function () {
  'use strict';

  var D = {};

  // ---------------------------------------------------------------- 格式化
  D.pct = function (v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (Number(v) * 100).toFixed(digits === undefined ? 1 : digits) + '%';
  };
  // 后端 pct 字段可能已是 0~100 的数；统一成「按 0~1 存储」
  D.asRate = function (v) {
    if (v === null || v === undefined || isNaN(v)) return 0;
    v = Number(v);
    return v > 1.001 ? v / 100 : v;
  };
  D.pctSmart = function (v, digits) { return D.pct(D.asRate(v), digits); };

  D.num = function (v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Number(v).toFixed(digits === undefined ? 0 : digits);
  };
  // 秒 -> mm:ss
  D.mmss = function (t) {
    t = Math.max(0, Number(t) || 0);
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  };
  // 「结果未知」的唯一判据：后端对未知结果显式输出 made=null，同时 result='unknown'。
  // 两者都要认 —— 只看 result 会把"缺 result 但结果为空"的行当成"未中"（实测这处边界
  // 在徽章/文案/筛选之间不一致过）。徽章配色、tooltip 文案、时间轴筛选、投篮图兜底
  // 全部走这一个判据，避免各写一套。
  D.isUnknown = function (e) {
    return !!e && (e.result === 'unknown' || e.made === null || e.made === undefined);
  };

  // 出手结果标签（made 为 null 表示"结果未知"，不能说成"未中"）
  D.shotLabel = function (value, made) {
    var head = (made === null || made === undefined) ? '待确认'
      : (made ? '命中' : '未中');
    return head + ({ 1: '罚球', 2: '两分', 3: '三分' }[value] || value + '分');
  };
  D.teamName = function (game, side) {
    var t = game && game.teams && game.teams[side];
    return (t && t.name) || (side === 'home' ? '主队' : '客队');
  };
  D.zoneColor = function (pct, att) {
    // 分区热力配色：0% 冷蓝 -> 100% 热红；样本量小则更透明
    if (!att) return 'rgba(240,243,248,0.55)';
    var p = Math.max(0, Math.min(1, pct));
    var r = Math.round(60 + 195 * p), g = Math.round(150 - 40 * p), b = Math.round(220 - 170 * p);
    var a = att >= 6 ? 0.85 : att >= 3 ? 0.68 : 0.48;
    return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
  };

  // ---------------------------------------------------------------- 规范化
  /**
   * 规范化 game.json。
   * 数据来源接口：GET /api/games/{job_id}（演示模式：web/demo/game.json）
   */
  D.normalizeGame = function (g) {
    g = g || {};
    g.score = g.score || { home: 0, away: 0 };
    g.teams = g.teams || {};
    ['home', 'away'].forEach(function (s) {
      g.teams[s] = g.teams[s] || {};
      g.teams[s].name = g.teams[s].name || (s === 'home' ? '主队' : '客队');
      var st = g.teams[s].stats || {};
      ['points', 'fgm', 'fga', 'tpm', 'tpa', 'ftm', 'fta'].forEach(function (k) {
        st[k] = Number(st[k] || 0);
      });
      st.fg_pct = st.fg_pct === undefined || st.fg_pct === null
        ? (st.fga ? st.fgm / st.fga : 0) : st.fg_pct;
      st.tp_pct = st.tp_pct === undefined || st.tp_pct === null
        ? (st.tpa ? st.tpm / st.tpa : 0) : st.tp_pct;
      g.teams[s].stats = st;
    });
    g.timeline = Array.isArray(g.timeline) ? g.timeline : [];
    g.progression = Array.isArray(g.progression) ? g.progression : [];
    g.needs_review = Array.isArray(g.needs_review) ? g.needs_review : [];
    g.highlights = Array.isArray(g.highlights) ? g.highlights : [];
    g.periods = g.periods || 4;
    g.duration = Number(g.duration || (g.meta && g.meta.duration) || 0);
    g.fps = Number(g.fps || (g.meta && g.meta.fps) || 0);

    // 分节比分缺失时，用 timeline 现算（保证总览页永远有分节表）
    if (!Array.isArray(g.quarter_scores) || !g.quarter_scores.length) {
      g.quarter_scores = D.quarterScoresFromTimeline(g.timeline, g.periods);
    }
    // 走势缺失时，用 timeline 现算
    if (!g.progression.length && g.timeline.length) {
      g.progression = D.progressionFromTimeline(g.timeline);
    }
    // 给了 progression 但第一帧不是 0:0 时补一个起点，折线图更好看
    if (g.progression.length && !(g.progression[0].t === 0 && !g.progression[0].home && !g.progression[0].away)) {
      g.progression = [{ t: 0, home: 0, away: 0, made: false }].concat(g.progression);
    }
    return g;
  };

  D.quarterScoresFromTimeline = function (timeline, periods) {
    periods = periods || 4;
    var out = [];
    for (var p = 1; p <= periods; p++) out.push({ period: p, home: 0, away: 0 });
    (timeline || []).forEach(function (e) {
      var p = Number(e.period || 1);
      if (!out[p - 1]) return;
      if (e.made && (e.points === undefined ? true : e.points)) {
        var pts = e.points === undefined ? (e.made ? e.value : 0) : e.points;
        if (e.team === 'home') out[p - 1].home += Number(pts || 0);
        else if (e.team === 'away') out[p - 1].away += Number(pts || 0);
      }
    });
    return out;
  };

  D.progressionFromTimeline = function (timeline) {
    var h = 0, a = 0, out = [{ t: 0, home: 0, away: 0 }];
    (timeline || []).slice().sort(function (x, y) { return x.t - y.t; }).forEach(function (e) {
      if (e.made) {
        var pts = e.points === undefined ? e.value : e.points;
        if (e.team === 'home') h += Number(pts || 0);
        else if (e.team === 'away') a += Number(pts || 0);
      }
      out.push({ t: e.t, home: h, away: a, scorer: e.player, value: e.value, made: e.made });
    });
    return out;
  };

  /**
   * 规范化 players.json（数组）。
   * 数据来源接口：GET /api/games/{job_id}/players
   */
  D.normalizePlayers = function (list) {
    list = Array.isArray(list) ? list : [];
    var keys = ['points', 'fgm', 'fga', 'tpm', 'tpa', 'ftm', 'fta',
      'reb', 'ast', 'stl', 'blk', 'tov', 'pf'];
    list.forEach(function (p) {
      keys.forEach(function (k) { p[k] = Number(p[k] || 0); });
      p.name = p.name || p.player_id;
      p.team = p.team || 'home';
      p.fg_pct = p.fg_pct === undefined || p.fg_pct === null ? (p.fga ? p.fgm / p.fga : 0) : p.fg_pct;
      p.tp_pct = p.tp_pct === undefined || p.tp_pct === null ? (p.tpa ? p.tpm / p.tpa : 0) : p.tp_pct;
      p.ft_pct = p.ft_pct === undefined || p.ft_pct === null ? (p.fta ? p.ftm / p.fta : 0) : p.ft_pct;
      p.efg = p.efg === undefined || p.efg === null
        ? (p.fga ? (p.fgm + 0.5 * p.tpm) / p.fga : 0) : p.efg;
      p.ts = p.ts === undefined || p.ts === null
        ? ((p.fga + 0.44 * p.fta) ? p.points / (2 * (p.fga + 0.44 * p.fta)) : 0) : p.ts;
      p.shots = Array.isArray(p.shots) ? p.shots : [];
    });
    return list;
  };

  /**
   * 规范化 shotchart.json -> { all:{points,zones,grid,bin_size}, by_team:{home,away} }
   * 数据来源接口：GET /api/games/{job_id}/shotchart
   * 说明：by_team 在后端是「zone 数组」，前端统一成同一套结构，便于过滤。
   */
  D.normalizeShotchart = function (sc) {
    sc = sc || {};
    if (!sc.all) {
      sc.all = { points: [], zones: [], grid: [], bin_size: 1.0 };
    }
    sc.all.points = Array.isArray(sc.all.points) ? sc.all.points : [];
    sc.all.zones = Array.isArray(sc.all.zones) ? sc.all.zones : [];
    sc.all.grid = Array.isArray(sc.all.grid) ? sc.all.grid : [];
    sc.all.bin_size = Number(sc.all.bin_size || 1.0);

    var bt = sc.by_team || {};
    if (Array.isArray(bt)) bt = { home: bt };      // 容错：后端直接给数组时按主队处理
    // 后端 by_team.home/away 直接是 zone 数组；某些实现也可能给完整 chart 对象，两种都兼容
    sc.zonesByTeam = {
      home: Array.isArray(bt.home) ? bt.home : (bt.home && bt.home.zones) || [],
      away: Array.isArray(bt.away) ? bt.away : (bt.away && bt.away.zones) || []
    };
    sc.pointsByTeam = {
      home: (bt.home && bt.home.points) || [],
      away: (bt.away && bt.away.points) || []
    };
    return sc;
  };

  /**
   * 从 players.json / game.timeline 兜底重建投篮点集。
   * 后端没给 shotchart.json（或演示数据只有 game+players）时使用。
   * 未知结果一律不进图（与后端 `rules.shot_chart` 的口径一致），
   * 判据统一用 `D.isUnknown`，避免"缺 result 但结果为空"漏成红点。
   */
  D.shotsFromPlayers = function (players) {
    var pts = [];
    (players || []).forEach(function (p) {
      (p.shots || []).forEach(function (s) {
        if (D.isUnknown(s) || s.location_unknown || (s.tags || []).indexOf("location_unknown") >= 0) return;
        pts.push({
          x: Number(s.x), y: Number(s.y), made: !!s.made, value: Number(s.value || 2),
          t: Number(s.t || 0), player_id: p.player_id, team: p.team,
          zone: D.zoneOf(s.x, s.y), distance: D.distanceToHoop(s.x, s.y)
        });
      });
    });
    return pts;
  };

  D.shotsFromTimeline = function (game) {
    return (game && game.timeline || []).filter(function(e){return !D.isUnknown(e) && !e.location_unknown && (e.tags || []).indexOf("location_unknown") < 0;}).map(function (e) {
      return {
        x: Number(e.x), y: Number(e.y), made: !!e.made, value: Number(e.value || 2),
        t: Number(e.t || 0), player_id: e.player_id, team: e.team,
        zone: e.zone || D.zoneOf(e.x, e.y), distance: D.distanceToHoop(e.x, e.y)
      };
    });
  };

  /** 拿到某支球队的全部出手点（优先 shotchart，再退到 timeline） */
  D.pointsForTeam = function (ctx, team) {
    if (team === 'all') return ctx.points;
    var mine = ctx.points.filter(function (p) { return p.team === team; });
    if (mine.length) return mine;
    return [];
  };

  // ------------------------------------------------- 球场几何（与 rules.py 对齐）
  // 坐标系：x 横向（宽度 ±7.5），y 纵向（长度 ±14），篮筐在 (0, ±1.575)。
  // 千万不要把篮筐写成 (±1.575, 0) —— 那等于把两根轴对调，
  // 会让计分引擎与热区网格互相矛盾（历史 bug，已修）。
  D.HOOP_Y = 1.575;         // 篮筐纵向偏移（距底线 1.575m）
  D.THREE_R = 6.75;         // 三分弧半径
  D.CORNER_X = 6.60;        // 底角三分直线：|x| >= 6.60（横向！）

  D.distanceToHoop = function (x, y) {
    var dl = Math.hypot(Number(x), Number(y) + D.HOOP_Y);
    var dr = Math.hypot(Number(x), Number(y) - D.HOOP_Y);
    return Math.round(Math.min(dl, dr) * 100) / 100;
  };

  /**
   * 与后端 rules.zone_of 完全一致的中文分区口径。
   * 后端实现：
   *   先判三分：距最近篮筐 >= 6.75 或 |x| >= 6.60 -> 三分
   *     三分再细分：|x| >= 6.60 底角三分；>= 4.0 45°三分；否则 弧顶三分
   *   否则按距篮距离切档：< 4.0 禁区；< 5.8 近距离中投；否则 长两分
   * 注意底角判据用的是横向 x，不是 y；距离档的边界是 4.0 / 5.8。
   */
  D.ZONES = ['禁区', '近距离中投', '长两分', '底角三分', '45°三分', '弧顶三分'];
  D.zoneOf = function (x, y) {
    x = Number(x); y = Number(y);
    var dist = Math.min(Math.hypot(x, y + D.HOOP_Y), Math.hypot(x, y - D.HOOP_Y));
    var isThree = dist >= D.THREE_R || Math.abs(x) >= D.CORNER_X;
    if (isThree) {
      if (Math.abs(x) >= D.CORNER_X) return '底角三分';
      if (Math.abs(x) >= 4.0) return '45°三分';
      return '弧顶三分';
    }
    if (dist < 4.0) return '禁区';
    if (dist < 5.8) return '近距离中投';
    return '长两分';
  };

  /** 前端自算分区聚合（后端 zones 缺失或需要按球员过滤时用） */
  D.aggregateZones = function (points) {
    var map = {};
    (points || []).forEach(function (p) {
      var z = p.zone || D.zoneOf(p.x, p.y);
      var d = map[z] || (map[z] = { zone: z, att: 0, made: 0, points: 0 });
      d.att++;
      if (p.made) { d.made++; d.points += Number(p.value || 2); }
    });
    var out = Object.keys(map).map(function (k) {
      var d = map[k];
      d.pct = d.att ? d.made / d.att : 0;
      d.pps = d.att ? Math.round((d.points / d.att) * 100) / 100 : 0;
      return d;
    });
    out.sort(function (a, b) {
      return D.ZONES.indexOf(a.zone) - D.ZONES.indexOf(b.zone);
    });
    return out;
  };

  /** 前端自算网格热力（1m 网格，镜像到同一半场），与后端 grid 口径一致 */
  D.buildGrid = function (points, bin) {
    bin = bin || 1.0;
    var nx = Math.round(15 / bin), ny = Math.round(14 / bin);
    var grid = [];
    for (var iy = 0; iy < ny; iy++) {
      var row = [];
      for (var ix = 0; ix < nx; ix++) row.push({ att: 0, made: 0 });
      grid.push(row);
    }
    (points || []).forEach(function (p) {
      var x = Math.abs(Number(p.x));                        // 镜像到右半场
      var iy = Math.floor((Number(p.y) + 14) / bin);        // y: -14..0 -> 0..14
      var ix = Math.floor(x / bin);
      if (iy < 0 || iy >= ny) return;
      if (ix < 0 || ix >= nx) return;
      grid[iy][ix].att++;
      if (p.made) grid[iy][ix].made++;
    });
    return grid;
  };

  // ---------------------------------------------------------------- 导出
  function csvEscape(v) {
    var s = (v === null || v === undefined) ? '' : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  D.toCSV = function (header, rows) {
    var lines = [header.map(csvEscape).join(',')];
    (rows || []).forEach(function (r) { lines.push(r.map(csvEscape).join(',')); });
    return lines.join('\r\n');
  };

  /** 球员统计 CSV（字段与后端 export.write_stats_csv 对齐） */
  D.playersCSV = function (players, game) {
    var cols = ['player_id', 'name', 'jersey', 'team', 'points', 'fgm', 'fga', 'fg_pct',
      'tpm', 'tpa', 'tp_pct', 'ftm', 'fta', 'ft_pct', 'efg', 'ts',
      'reb', 'ast', 'stl', 'blk', 'tov', 'pf'];
    var rows = (players || []).map(function (p) {
      return cols.map(function (c) {
        var v = p[c];
        if (typeof v === 'number' && /pct|efg|ts/.test(c)) v = v.toFixed(3);
        return v;
      });
    });
    if (game && game.teams) {
      rows.push([]);
      rows.push(['球队汇总']);
      rows.push(['team', 'points', 'fgm', 'fga', 'fg_pct', 'tpm', 'tpa', 'tp_pct', 'ftm', 'fta']);
      ['home', 'away'].forEach(function (s) {
        var st = game.teams[s].stats;
        rows.push([s, st.points, st.fgm, st.fga, Number(st.fg_pct).toFixed(3),
          st.tpm, st.tpa, Number(st.tp_pct).toFixed(3), st.ftm, st.fta]);
      });
    }
    return D.toCSV(cols, rows);
  };

  /** 出手明细 CSV（字段与后端 export.write_shots_csv 对齐） */
  // ------------------------------------------------------------------
  // 战术层导出（与后端 export.write_passes_csv / write_spacing_csv 同结构）
  // ------------------------------------------------------------------
  /** 传球 / 失误明细 + 传球网络汇总 + 球员传球统计 */
  D.tacticsPassesCSV = function (tac) {
    if (!tac || !tac.available) return '（本场没有战术数据）';
    var P = tac.passes || {};
    var blocks = [];
    blocks.push('传球 / 失误事件');
    blocks.push(D.toCSV(['t', 'kind', 'team', 'from', 'to', 'dist_m', 'battle_s', 'x', 'depth'],
      (P.events || []).map(function (e) {
        return [e.t, e.kind || 'pass', e.team, e.from, e.to, e.dist,
          e.duration === undefined ? '' : e.duration, e.x, e.d];
      })));
    var netRows = [];
    Object.keys(tac.pass_network || {}).sort().forEach(function (team) {
      ((tac.pass_network[team] || {}).edges || []).forEach(function (e) {
        netRows.push([team, e.source, e.target, e.value]);
      });
    });
    blocks.push('');
    blocks.push('传球网络（有向：从 -> 到）');
    blocks.push(D.toCSV(['team', 'from', 'to', 'count'], netRows));
    blocks.push('');
    blocks.push('球员传球统计');
    blocks.push(D.toCSV(['player_id', 'name', 'team', 'passes', 'received'],
      (P.leaderboard || []).map(function (r) {
        return [r.player_id, r.name, r.team, r.passes, r.received];
      })));
    return blocks.join('\r\n');
  };

  /** 球队空间指标时间序列（前端"间距曲线"的原始数据） */
  D.tacticsSpacingCSV = function (tac) {
    if (!tac || !tac.available) return '（本场没有战术数据）';
    var rows = ((tac.spacing || {}).timeline || []).map(function (r) {
      return [r.t, r.team, r.n, r.area, r.mean_dist, r.width, r.depth,
        r.radius, r.cx, r.cd];
    });
    return D.toCSV(['t', 'team', 'n', 'area', 'mean_dist', 'width', 'depth',
      'radius', 'cx', 'cd'], rows);
  };

  D.shotsCSV = function (points) {
    var cols = ['t', 'period', 'team', 'player_id', 'value', 'made', 'points',
      'x', 'y', 'distance', 'zone'];
    var rows = (points || []).slice().sort(function (a, b) { return a.t - b.t; }).map(function (p) {
      return [Number(p.t).toFixed(2), p.period || '', p.team || '', p.player_id || '',
        p.value, p.made ? 1 : 0, p.made ? p.value : 0,
        Number(p.x).toFixed(3), Number(p.y).toFixed(3),
        p.distance === undefined ? D.distanceToHoop(p.x, p.y) : p.distance,
        p.zone || D.zoneOf(p.x, p.y)];
    });
    return D.toCSV(cols, rows);
  };

  /** 触发浏览器下载（无后端时的导出兜底） */
  D.download = function (filename, content, mime) {
    var blob = new Blob([content], { type: (mime || 'text/plain') + ';charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(function () { document.body.removeChild(a); URL.revokeObjectURL(url); }, 400);
  };

  // ---------------------------------------------------------------- Markdown
  function inline(s) {
    return s
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  /**
   * 极简 markdown -> HTML：标题 / 表格 / 列表 / 粗体 / 斜体 / 行内代码 / 分隔线。
   * report.md 就是这套语法（见后端 report.build_report_md），足够 1:1 还原。
   */
  D.md2html = function (md) {
    if (!md) return '<p class="muted">（暂无战报文本）</p>';
    var lines = String(md).replace(/\r\n/g, '\n').split('\n');
    var out = [], i = 0;

    function isTableRow(s) { return /^\s*\|.*\|\s*$/.test(s); }
    function cells(s) {
      return s.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); });
    }

    while (i < lines.length) {
      var line = lines[i];

      // 表格
      if (isTableRow(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        var head = cells(line);
        i += 2;
        var body = [];
        while (i < lines.length && isTableRow(lines[i])) { body.push(cells(lines[i])); i++; }
        out.push('<table><thead><tr>' + head.map(function (h) { return '<th>' + inline(h) + '</th>'; }).join('') +
          '</tr></thead><tbody>' +
          body.map(function (r) {
            return '<tr>' + r.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table>');
        continue;
      }

      // 标题
      var h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) { out.push('<h' + h[1].length + '>' + inline(h[2]) + '</h' + h[1].length + '>'); i++; continue; }

      // 分隔线
      if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { out.push('<hr />'); i++; continue; }

      // 引用块（> 开头，报告中用来放提示）
      if (/^\s*>\s?/.test(line)) {
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          q.push(lines[i].replace(/^\s*>\s?/, '')); i++;
        }
        out.push('<blockquote>' + inline(q.join(' ')) + '</blockquote>');
        continue;
      }

      // 列表
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        var ordered = /^\s*\d+\./.test(line);
        var items = [];
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
          i++;
        }
        out.push('<' + (ordered ? 'ol' : 'ul') + '>' +
          items.map(function (t) { return '<li>' + inline(t) + '</li>'; }).join('') +
          '</' + (ordered ? 'ol' : 'ul') + '>');
        continue;
      }

      // 空行
      if (!line.trim()) { i++; continue; }

      // 段落（合并连续行；行尾两个空格 = Markdown 硬换行 <br/>）
      var buf = [line];
      i++;
      while (i < lines.length && lines[i].trim() &&
        !/^(#{1,6})\s/.test(lines[i]) && !isTableRow(lines[i]) &&
        !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) && !/^\s*(---|\*\*\*|___)\s*$/.test(lines[i])) {
        buf.push(lines[i]); i++;
      }
      out.push('<p>' + buf.map(function (t) {
        var hard = /\s{2,}$/.test(t);              // 行尾两个空格 = Markdown 硬换行
        return inline(t.replace(/\s+$/, '')) + (hard ? '<br/>' : '');
      }).join(' ') + '</p>');
    }
    return out.join('\n');
  };

  window.D = D;
})();
