/* ==========================================================================
   components.js —— 跨页面复用的 Vue 组件
   --------------------------------------------------------------------------
   1) echarts-box   ECharts 统一封装（自适应 + option 热更新 + 统一浅色主题）
   2) court-view    标准半场视图容器（court.js 生成 SVG + 叠加层，半场/全场切换）
   3) stat-compare  双方指标对比条（命中率 / 三分率 / 罚球…）
   4) big-scoreboard 大比分牌 + 分节比分表
   ========================================================================== */
(function () {
  'use strict';

  // ECharts 统一主题：与 styles.css 的深顶栏 / 浅内容区一致
  var PALETTE = ['#2f6fed', '#f2762e', '#22a06b', '#8b5cf6', '#f5a623', '#0ea5e9'];

  window.COMPONENTS = {};

  /** ---------------- ECharts 容器 ---------------- */
  window.COMPONENTS['echarts-box'] = {
    name: 'echarts-box',
    props: {
      option: { type: Object, required: true },
      height: { type: String, default: '320px' }
    },
    template: '<div ref="el" :style="{width:\'100%\',height:height}"></div>',
    data: function () { return { chart: null, ro: null }; },
    mounted: function () {
      if (!window.echarts) {
        console.warn('[echarts-box] ECharts 未加载，请检查 CDN');
        return;
      }
      this.chart = window.echarts.init(this.$refs.el, null, { renderer: 'canvas' });
      this.chart.setOption(this.buildOption());
      var self = this;
      if (window.ResizeObserver) {
        this.ro = new ResizeObserver(function () { if (self.chart) self.chart.resize(); });
        this.ro.observe(this.$refs.el);
      }
    },
    watch: {
      option: {
        deep: true,
        handler: function () {
          if (!this.chart) return;
          // 用 notMerge=true 避免切换球队/指标时残留上一次的 series
          this.chart.setOption(this.buildOption(), true);
        }
      }
    },
    beforeUnmount: function () {
      if (this.ro) this.ro.disconnect();
      if (this.chart) { this.chart.dispose(); this.chart = null; }
    },
    methods: {
      buildOption: function () {
        var o = JSON.parse(JSON.stringify(this.option || {}));
        o.color = o.color || PALETTE;
        o.textStyle = Object.assign({ fontFamily: 'PingFang SC, Microsoft YaHei, sans-serif', fontSize: 12 }, o.textStyle || {});
        if (!o.grid && o.series) {
          o.grid = { left: 46, right: 20, top: 34, bottom: 34, containLabel: true };
        }
        if (o.tooltip === undefined && o.series) {
          o.tooltip = { trigger: 'axis', axisPointer: { type: 'cross', label: { backgroundColor: '#1f2d3d' } } };
        }
        if (!o.legend && (o.series || []).length > 1) {
          o.legend = { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10 };
        }
        return o;
      },
      resize: function () { if (this.chart) this.chart.resize(); }
    }
  };

  /** ---------------- 标准半场容器 ----------------
   * props.points  —— 出手点数组（shotchart.all.points 的口径）
   * props.zones   —— 分区聚合数组
   * props.grid    —— 网格热力 grid[iy][ix]
   * props.layer   —— 'shots' | 'zones' | 'grid' | 'none'
   */
  window.COMPONENTS['court-view'] = {
    name: 'court-view',
    props: {
      view: { type: String, default: 'half' },
      layer: { type: String, default: 'shots' },
      points: { type: Array, default: function () { return []; } },
      zones: { type: Array, default: function () { return []; } },
      grid: { type: Array, default: function () { return []; } },
      bin: { type: Number, default: 1 },
      metric: { type: String, default: 'pct' },
      dotR: { type: Number, default: 0.2 },
      useHeat: { type: Boolean, default: false }
    },
    template:
      '<div class="court-wrap">' +
      '  <div v-html="courtHTML"></div>' +
      '  <div v-if="layerHTML" v-html="layerHTML" style="position:absolute;inset:0"></div>' +
      '</div>',
    computed: {
      courtHTML: function () { return window.Court.courtSVG({ view: this.view }); },
      layerHTML: function () {
        var C = window.Court;
        if (this.layer === 'zones') return C.zoneLayer(this.zones, {});
        if (this.layer === 'grid') return C.gridLayer(this.grid, { bin: this.bin, metric: this.metric });
        if (this.layer === 'shots') {
          return C.shotLayer(this.points, { view: this.view, dotR: this.dotR, useHeat: this.useHeat });
        }
        return '';
      }
    }
  };

  /** ---------------- 双方指标对比 ---------------- */
  window.COMPONENTS['stat-compare'] = {
    name: 'stat-compare',
    props: {
      label: String,
      home: { type: Number, default: 0 },
      away: { type: Number, default: 0 },
      isPct: { type: Boolean, default: false },
      homeName: String,
      awayName: String
    },
    template:
      '<div style="margin-bottom:14px">' +
      '  <div class="row" style="justify-content:space-between;font-size:12.5px">' +
      '    <span><b style="color:var(--home)">{{ fmt(home) }}</b> <span class="muted">{{ homeName }}</span></span>' +
      '    <span class="muted">{{ label }}</span>' +
      '    <span><span class="muted">{{ awayName }}</span> <b style="color:var(--away)">{{ fmt(away) }}</b></span>' +
      '  </div>' +
      '  <div class="pctbar" style="margin-top:6px">' +
      '    <i class="h" :style="{width: hw + \'%\'}"></i>' +
      '    <i class="a" :style="{width: aw + \'%\'}"></i>' +
      '  </div>' +
      '</div>',
    computed: {
      total: function () { return Math.max(1e-6, this.home + this.away); },
      hw: function () { return (this.home / this.total * 100).toFixed(1); },
      aw: function () { return (this.away / this.total * 100).toFixed(1); }
    },
    methods: {
      fmt: function (v) { return this.isPct ? window.D.pctSmart(v) : window.D.num(v); }
    }
  };

  /** ---------------- 大比分牌 + 分节比分表 ---------------- */
  window.COMPONENTS['big-scoreboard'] = {
    name: 'big-scoreboard',
    props: { game: { type: Object, required: true } },
    template:
      '<div>' +
      '  <div class="scoreboard">' +
      '    <div class="team home">' +
      '      <div class="nm">{{ name("home") }}</div>' +
      '      <div class="pts">{{ displayScore.home }}</div>' +
<<<<<<< HEAD
      '      <div class="meta">已判定投篮 {{ st("home").fgm }}/{{ st("home").fga }} · 待确认 {{ st("home").unknown || 0 }}<br>已判定记录命中率 {{ pct(st("home").fg_pct) }}（非全部出手）<br>' +
=======
      '      <div class="meta">投篮 {{ st("home").fgm }}/{{ st("home").fga }} · 命中率 {{ pct(st("home").fg_pct) }}<br>' +
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
      '        三分 {{ st("home").tpm }}/{{ st("home").tpa }} · 罚球 {{ st("home").ftm }}/{{ st("home").fta }}</div>' +
      '    </div>' +
      '    <div class="mid">' +
      '      <div class="vs">{{ scoreLabel }}</div>' +
      '      <div class="win">{{ winnerText }}</div>' +
      '      <div class="vs" style="letter-spacing:0">{{ periods }} 节 · {{ mmss(game.duration) }}</div>' +
      '      <div v-if="courtMode && hasCarry" class="vs" style="letter-spacing:0;margin-top:8px;font-size:12px;color:#8a93a4">' +
      '        比分牌读数 {{ game.carry_in.home }} : {{ game.carry_in.away }}（仅供参考，不计入本片段得分）' +
      '      </div>' +
      '      <div v-if="!courtMode && hasCarry" class="vs" style="letter-spacing:0;margin-top:8px;font-size:12px;color:#8a93a4">' +
      '        比分牌带入 {{ game.carry_in.home }} : {{ game.carry_in.away }}<br>' +
      '        当前大比分 {{ totalScore.home }} : {{ totalScore.away }}<br>' +
      '        {{ currentLeadText }}' +
      '      </div>' +
      '    </div>' +
      '    <div class="team away">' +
      '      <div class="nm">{{ name("away") }}</div>' +
      '      <div class="pts">{{ displayScore.away }}</div>' +
<<<<<<< HEAD
      '      <div class="meta">已判定投篮 {{ st("away").fgm }}/{{ st("away").fga }} · 待确认 {{ st("away").unknown || 0 }}<br>已判定记录命中率 {{ pct(st("away").fg_pct) }}（非全部出手）<br>' +
=======
      '      <div class="meta">投篮 {{ st("away").fgm }}/{{ st("away").fga }} · 命中率 {{ pct(st("away").fg_pct) }}<br>' +
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
      '        三分 {{ st("away").tpm }}/{{ st("away").tpa }} · 罚球 {{ st("away").ftm }}/{{ st("away").fta }}</div>' +
      '    </div>' +
      '  </div>' +
      '  <el-alert v-if="hasCarry" type="info" :closable="false" show-icon style="margin-top:10px"' +
      '    :title="carryExplain" />' +
      '  <el-table :data="quarterRows" size="small" style="margin-top:12px" border>' +
      '    <el-table-column prop="label" label="节次" width="110" />' +
      '    <el-table-column :label="name(\'home\')" align="center">' +
      '      <template #default="s"><span :style="{fontWeight: s.row.home>=s.row.away?700:400, color:\'var(--home)\'}">{{ s.row.home }}</span></template>' +
      '    </el-table-column>' +
      '    <el-table-column :label="name(\'away\')" align="center">' +
      '      <template #default="s"><span :style="{fontWeight: s.row.away>=s.row.home?700:400, color:\'var(--away)\'}">{{ s.row.away }}</span></template>' +
      '    </el-table-column>' +
      '    <el-table-column label="本节分差" align="center" width="110">' +
      '      <template #default="s">{{ s.row.home - s.row.away > 0 ? "+" : "" }}{{ s.row.home - s.row.away }}</template>' +
      '    </el-table-column>' +
      '  </el-table>' +
      '</div>',
    computed: {
      scorePolicy: function () {
        return String(this.game.score_policy || 'scoreboard').toLowerCase();
      },
      courtMode: function () {
        return ['court', 'visual', 'auto'].indexOf(this.scorePolicy) >= 0;
      },
      hasCarry: function () {
        var ci = this.game.carry_in || {};
        return !!(ci.home || ci.away);
      },
      totalScore: function () {
        return this.game.score || { home: 0, away: 0 };
      },
      clipScore: function () {
        if (this.game.clip_score) return this.game.clip_score;
        var ci = this.game.carry_in || { home: 0, away: 0 };
        return {
          home: (this.game.score.home || 0) - (ci.home || 0),
          away: (this.game.score.away || 0) - (ci.away || 0)
        };
      },
      displayScore: function () {
        return (this.courtMode || this.hasCarry) ? this.clipScore : this.totalScore;
      },
      scoreLabel: function () {
        return (this.courtMode || this.hasCarry) ? '本片段得分' : 'FINAL';
      },
      periods: function () { return this.game.periods || 4; },
      winnerText: function () {
        var h = this.displayScore.home, a = this.displayScore.away;
        if (h === a) return this.hasCarry ? '本片段暂无得分' : '战平';
        var leader = h > a ? this.name('home') : this.name('away');
        return leader + (this.hasCarry ? ' 本片段领先 ' : ' 胜 ') + Math.abs(h - a) + ' 分';
      },
      currentLeadText: function () {
        var h = this.totalScore.home, a = this.totalScore.away;
        if (h === a) return '比赛当前战平';
        var leader = h > a ? this.name('home') : this.name('away');
        return '比赛当前：' + leader + ' 领先 ' + Math.abs(h - a) + ' 分';
      },
      carryExplain: function () {
        var ci = this.game.carry_in || { home: 0, away: 0 };
        if (this.courtMode) {
          return '上方大字是「本片段得分」，按场上检测到的进球计分；比分牌读数 '
            + ci.home + ' : ' + ci.away + ' 仅供参考，不计入本片段得分。';
        }
        return '上方大字是「本片段得分」；' + ci.home + ' : ' + ci.away +
          ' 是比分牌带入的比赛当前大比分，不是这 ' + this.mmss(this.game.duration) +
          ' 得的。下方表格里「本片段」行才是这段视频真正产生的得分。';
      },
      quarterRows: function () {
        var rows = (this.game.quarter_scores || []).map(function (q) {
          return { label: '第' + q.period + '节', home: q.home, away: q.away };
        });
        if (this.hasCarry && !this.courtMode) {
          rows.push({ label: '本片段', home: this.clipScore.home, away: this.clipScore.away });
        }
        rows.push({
          label: this.courtMode ? '本片段总计' : (this.hasCarry ? '当前大比分' : '总计'),
          home: this.totalScore.home, away: this.totalScore.away
        });
        return rows;
      }
    },
    methods: {
      name: function (s) { return window.D.teamName(this.game, s); },
      st: function (s) { return (this.game.teams[s] || {}).stats || {}; },
      pct: function (v) { return window.D.pctSmart(v); },
      mmss: function (t) { return window.D.mmss(t); }
    }
  };

  /** ---------------- 小工具：把状态轮询/WS 逻辑封装起来 ---------------- */
  window.makeJob = function () {
    return {
      job_id: '', status: '', progress: 0, message: '', error: null,
      out_dir: '', summary: ''
    };
  };
})();
