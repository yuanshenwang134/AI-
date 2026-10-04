/* ==========================================================================
   pages/stats.js —— 页面 3：球员 / 球队统计
   --------------------------------------------------------------------------
   数据来源接口：
     GET /api/games/{job_id}/players   players.json（含每人 shots 出手明细）
     GET /api/games/{job_id}           game.json（球队汇总 stats）
   展示内容：
     * 得分榜（ECharts 横向柱状，蓝=主队 橙=客队）
     * Element Plus 可排序统计表（得分/篮板/助攻/命中率/三分/eFG/TS…）
     * 点击球员行 → 展开该球员出手散点（court.js 画标准半场，左右半场自动镜像）
     * 两名球员雷达图对比（得分/篮板/助攻/命中率/三分命中率/真实命中率）
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['stats'] = {
  name: 'page-stats',
  data: function () {
    return {
      S: window.STORE,
      teamFilter: 'all',
      compare: [],
      expandKeys: []
    };
  },
  computed: {
    players: function () { return this.S.players || []; },
    game: function () { return this.S.game; },

    /** 表格数据：按球队过滤并补一个中文球队名列 */
    rows: function () {
      var self = this;
      return this.players.filter(function (p) {
        return self.teamFilter === 'all' || p.team === self.teamFilter;
      }).map(function (p) {
        return Object.assign({}, p, { teamName: window.D.teamName(self.game, p.team) });
      });
    },

    /** 球队数据对比条（stat-compare 组件的数据源） */
    teamMetrics: function () {
      var g = this.game;
      if (!g) return [];
      var h = g.teams.home.stats, a = g.teams.away.stats;
      return [
        { label: '总得分', home: h.points, away: a.points },
        { label: '运动战命中率', home: h.fg_pct, away: a.fg_pct, isPct: true },
        { label: '三分命中率', home: h.tp_pct, away: a.tp_pct, isPct: true },
        { label: '三分命中数', home: h.tpm, away: a.tpm },
        { label: '罚球命中数', home: h.ftm, away: a.ftm },
        { label: '总出手数', home: h.fga, away: a.fga }
      ];
    },

    /** 得分榜 option：取前 12 名 */
    scoreOption: function () {
      var top = this.players.slice()
        .sort(function (a, b) { return a.points - b.points; })
        .slice(-12);
      return {
        tooltip: {
          trigger: 'axis', axisPointer: { type: 'shadow' },
          formatter: function (ps) {
            var row = top[ps[0].dataIndex];
            return row.name + '<br/>得分 ' + row.points +
              '<br/>投篮 ' + row.fgm + '/' + row.fga + '（' + window.D.pctSmart(row.fg_pct) + '）' +
              '<br/>三分 ' + row.tpm + '/' + row.tpa + '（' + window.D.pctSmart(row.tp_pct) + '）' +
              '<br/>罚球 ' + row.ftm + '/' + row.fta;
          }
        },
        grid: { left: 96, right: 48, top: 12, bottom: 24, containLabel: true },
        xAxis: { type: 'value', name: '得分' },
        yAxis: { type: 'category', data: top.map(function (p) { return p.name; }), axisLabel: { fontSize: 11.5 } },
        series: [{
          type: 'bar', barMaxWidth: 18,
          label: { show: true, position: 'right', formatter: '{c} 分' },
          itemStyle: {
            borderRadius: [0, 4, 4, 0],
            color: function (p) { return top[p.dataIndex].team === 'home' ? '#2f6fed' : '#f2762e'; }
          },
          data: top.map(function (p) { return p.points; })
        }]
      };
    },

    compareOptions: function () {
      var self = this;
      return this.players.map(function (p) {
        return { value: p.player_id, label: p.name + '（' + window.D.teamName(self.game, p.team) + '）' };
      });
    },

    /** 雷达图：得分/篮板/助攻按全体球员最大值归一，命中率类取百分比 */
    radarOption: function () {
      var D = window.D;
      var list = this.players;
      var sel = [];
      var self = this;
      this.compare.forEach(function (id) {
        for (var i = 0; i < list.length; i++) {
          if (list[i].player_id === id) { sel.push(list[i]); break; }
        }
      });
      if (!sel.length) {
        return {
          title: {
            text: '请选择球员进行对比', left: 'center', top: 'middle',
            textStyle: { color: '#8b95a3', fontSize: 13, fontWeight: 400 }
          }
        };
      }
      function maxOf(key) {
        var m = 1;
        list.forEach(function (p) { m = Math.max(m, Number(p[key]) || 0); });
        return m;
      }
      var mp = maxOf('points'), mr = maxOf('reb'), ma = maxOf('ast');
      var colors = ['#2f6fed', '#f2762e', '#22a06b', '#8b5cf6'];
      return {
        tooltip: {},
        legend: { bottom: 0 },
        radar: {
          indicator: [
            { name: '得分', max: 100 }, { name: '篮板', max: 100 }, { name: '助攻', max: 100 },
            { name: '命中率', max: 100 }, { name: '三分命中率', max: 100 }, { name: '真实命中率', max: 100 }
          ],
          radius: '62%', splitNumber: 4,
          axisName: { fontSize: 11.5, color: '#4b5563' },
          splitArea: { areaStyle: { color: ['#fbfcfe', '#f4f7fc'] } }
        },
        series: [{
          type: 'radar',
          areaStyle: { opacity: 0.16 },
          symbolSize: 4,
          data: sel.map(function (p, i) {
            return {
              name: p.name,
              value: [
                Math.round(p.points / mp * 100),
                Math.round(p.reb / mr * 100),
                Math.round(p.ast / ma * 100),
                Math.round(D.asRate(p.fg_pct) * 100),
                Math.round(D.asRate(p.tp_pct) * 100),
                Math.round(D.asRate(p.ts) * 100)
              ],
              lineStyle: { color: colors[i % 4], width: 2 },
              itemStyle: { color: colors[i % 4] }
            };
          })
        }]
      };
    }
  },
  methods: {
    teamName: function (s) { return window.D.teamName(this.game, s); },
    pct: function (v) { return window.D.pctSmart(v); },
    num: function (v, d) { return window.D.num(v, d); },
    pctVal: function (v) { return window.D.asRate(v); },

    /** 某球员的出手点（players[].shots；缺 zone/value 时前端按同一口径补齐） */
    shotPoints: function (p) {
      var D = window.D;
      return (p.shots || []).map(function (s) {
        return {
          x: s.x, y: s.y, made: !!s.made, value: Number(s.value || 2), t: s.t,
          player_id: p.player_id, team: p.team,
          zone: s.zone || D.zoneOf(s.x, s.y),
          distance: s.distance === undefined ? D.distanceToHoop(s.x, s.y) : s.distance,
          period: s.period
        };
      });
    },

    /** 该球员的分区分布（复用 data.js 的聚合口径，与后端 zone_of 一致） */
    aggregate: function (row) { return window.D.aggregateZones(this.shotPoints(row)); },
    madeCount: function (row) {
      return this.shotPoints(row).filter(function (p) { return p.made; }).length;
    },
    missCount: function (row) {
      return this.shotPoints(row).filter(function (p) { return !p.made; }).length;
    },
    threeCount: function (row) {
      return this.shotPoints(row).filter(function (p) { return p.value === 3; }).length;
    },

    /** 展开行变化时维护 expandKeys（用于「全部折叠」等扩展操作） */
    onExpandChange: function (rows) {
      this.expandKeys = rows.map(function (r) { return r.player_id; });
    },

    // ---- 表格排序：百分比字段需要先转成数值再比（el-table 的 sort-method）----
    sortByFg: function (a, b) { return this.pctVal(a.fg_pct) - this.pctVal(b.fg_pct); },
    sortByTp: function (a, b) { return this.pctVal(a.tp_pct) - this.pctVal(b.tp_pct); },
    sortByEfg: function (a, b) { return this.pctVal(a.efg) - this.pctVal(b.efg); },
    sortByTs: function (a, b) { return this.pctVal(a.ts) - this.pctVal(b.ts); },

    collapseAll: function () {
      this.expandKeys = [];
      var t = this.$refs.table;
      if (t && t.clearSelection) t.clearSelection();
    }
  },
  created: function () {
    // 默认选中两队得分王，打开页面就有一张可讲的对比图
    var list = (this.S.players || []).slice().sort(function (a, b) { return b.points - a.points; });
    var picked = [];
    ['home', 'away'].forEach(function (t) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].team === t) { picked.push(list[i].player_id); break; }
      }
    });
    this.compare = picked;
  },
  template: [
    '<div>',
    '  <div class="card" v-if="!players.length">',
    '    <el-empty description="暂无球员数据，请先在上传页创建分析任务或载入演示数据" />',
    '  </div>',

    '  <template v-else>',
    '    <div class="grid grid-court">',
    '      <div class="card">',
    '        <h3 class="card-title">得分榜 <span class="sub">数据来源：GET /api/games/{{ S.jobId }}/players（players.json）</span></h3>',
    '        <echarts-box :option="scoreOption" height="380px" />',
    '      </div>',
    '      <div class="card">',
    '        <h3 class="card-title">球队数据对比 <span class="sub">game.teams[side].stats</span></h3>',
    '        <stat-compare v-for="(m,i) in teamMetrics" :key="i" :label="m.label"',
    '                      :home="m.home" :away="m.away" :is-pct="m.isPct"',
    '                      :home-name="teamName(\'home\')" :away-name="teamName(\'away\')" />',
    '      </div>',
    '    </div>',

    '    <div class="card">',
    '      <h3 class="card-title">球员统计表 <span class="sub">表头可排序；点击行首箭头展开该球员出手散点（左右半场已镜像到同一侧半场）</span>',
    '        <span class="grow"></span>',
    '        <el-radio-group v-model="teamFilter" size="small">',
    '          <el-radio-button label="all">全部</el-radio-button>',
    '          <el-radio-button label="home">{{ teamName(\'home\') }}</el-radio-button>',
    '          <el-radio-button label="away">{{ teamName(\'away\') }}</el-radio-button>',
    '        </el-radio-group>',
    '      </h3>',
    '      <el-table ref="table" :data="rows" size="small" border row-key="player_id"',
    '                @expand-change="onExpandChange" empty-text="暂无数据">',
    '        <el-table-column type="expand">',
    '          <template #default="s">',
    '            <div class="grid grid-2" style="padding:6px 4px">',
    '              <court-view layer="shots" :points="shotPoints(s.row)" :dot-r="0.19" />',
    '              <div>',
    '                <div class="hint" style="margin-bottom:8px">',
    '                  <b>{{ s.row.name }}</b> 共 {{ (s.row.shots||[]).length }} 次出手，',
    '                  运动战 {{ s.row.fgm }}/{{ s.row.fga }}（{{ pct(s.row.fg_pct) }}），',
    '                  三分 {{ s.row.tpm }}/{{ s.row.tpa }}，罚球 {{ s.row.ftm }}/{{ s.row.fta }}。',
    '                </div>',
    '                <div class="row" style="margin-bottom:10px">',
    '                  <el-tag size="small" type="success">命中 {{ madeCount(s.row) }}</el-tag>',
    '                  <el-tag size="small" type="danger" effect="plain">未中 {{ missCount(s.row) }}</el-tag>',
    '                  <el-tag size="small" type="warning" effect="plain">三分出手 {{ threeCount(s.row) }}</el-tag>',
    '                </div>',
    '                <div class="legend"><span><i class="dot"></i>命中（实心绿）</span><span><i class="ring"></i>未中（空心红）</span><span>外圈=三分出手</span></div>',
    '                <div class="hint" style="margin-top:10px">分区分布：',
    '                  <span v-for="(z,i) in aggregate(s.row)" :key="i">{{ z.zone }} {{ z.made }}/{{ z.att }}　</span>',
    '                </div>',
    '              </div>',
    '            </div>',
    '          </template>',
    '        </el-table-column>',
    '        <el-table-column prop="name" label="球员" min-width="110" fixed />',
    '        <el-table-column prop="teamName" label="球队" width="100" />',
    '        <el-table-column prop="jersey" label="号码" width="70" align="center" />',
    '        <el-table-column prop="points" label="得分" width="86" sortable align="center" />',
    '        <el-table-column label="投篮" width="96" align="center" sortable :sort-by="\'fga\'">',
    '          <template #default="s">{{ s.row.fgm }}/{{ s.row.fga }}</template>',
    '        </el-table-column>',
    '        <el-table-column label="命中率" width="98" align="center" sortable :sort-method="sortByFg">',
    '          <template #default="s">{{ pct(s.row.fg_pct) }}</template>',
    '        </el-table-column>',
    '        <el-table-column label="三分" width="96" align="center">',
    '          <template #default="s">{{ s.row.tpm }}/{{ s.row.tpa }}</template>',
    '        </el-table-column>',
    '        <el-table-column label="三分率" width="98" align="center" sortable :sort-method="sortByTp">',
    '          <template #default="s">{{ pct(s.row.tp_pct) }}</template>',
    '        </el-table-column>',
    '        <el-table-column label="罚球" width="92" align="center">',
    '          <template #default="s">{{ s.row.ftm }}/{{ s.row.fta }}</template>',
    '        </el-table-column>',
    '        <el-table-column label="eFG%" width="92" align="center" sortable :sort-method="sortByEfg">',
    '          <template #default="s">{{ pct(s.row.efg) }}</template>',
    '        </el-table-column>',
    '        <el-table-column label="TS%" width="92" align="center" sortable :sort-method="sortByTs">',
    '          <template #default="s">{{ pct(s.row.ts) }}</template>',
    '        </el-table-column>',
    '        <el-table-column prop="reb" label="篮板" width="80" sortable align="center" />',
    '        <el-table-column prop="ast" label="助攻" width="80" sortable align="center" />',
    '        <el-table-column prop="stl" label="抢断" width="80" sortable align="center" />',
    '        <el-table-column prop="blk" label="盖帽" width="80" sortable align="center" />',
    '        <el-table-column prop="tov" label="失误" width="80" sortable align="center" />',
    '        <el-table-column prop="pf" label="犯规" width="80" sortable align="center" />',
    '      </el-table>',
    '    </div>',

    '    <div class="card">',
    '      <h3 class="card-title">球员能力对比（雷达图） <span class="sub">得分/篮板/助攻按全体最大值归一，命中率类直接取百分比</span></h3>',
    '      <el-select v-model="compare" multiple filterable collapse-tags placeholder="选择 1~4 名球员进行对比"',
    '                 style="width:100%;max-width:640px">',
    '        <el-option v-for="o in compareOptions" :key="o.value" :label="o.label" :value="o.value" />',
    '      </el-select>',
    '      <echarts-box :option="radarOption" height="390px" style="margin-top:10px" />',
    '    </div>',
    '  </template>',
    '</div>'
  ].join('\n')
};
