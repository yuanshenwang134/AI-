/* ==========================================================================
   pages/shotchart.js —— 页面 4：投篮热区（本方案可视化重点）
   --------------------------------------------------------------------------
   数据来源接口：
     GET /api/games/{job_id}/shotchart   shotchart.json
         all        : { points[], zones[], grid[][], bin_size }
         by_team    : { home: zone[], away: zone[] }
     其中：
       points  每个出手点 {x,y,made,value,t,player_id,zone,distance}
       zones   分区聚合   {zone,att,made,pct,points,pps}（中文分区名来自后端 zone_of）
       grid    网格热力   grid[iy][ix]，iy=floor((y+14)/bin)，ix=floor(|x|/bin)
     GET /api/games/{job_id}             game.json（兜底点集 + 球队名）
   绘制方式：court.js 手工绘制标准 FIBA 半场（底线 / 6.75m 三分弧 / 底角三分直线 /
             4.9m×5.8m 罚球区 / 1.8m 罚球圈 / 1.25m 合理冲撞区 / 篮筐），
             再叠加「出手散点」「分区热力」「网格热力」三种图层。
   关键点：左右半场（x 正负）的出手全部镜像到同一个半场显示 —— 这是热区分析的标准做法。
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['shotchart'] = {
  name: 'page-shotchart',
  data: function () {
    return {
      S: window.STORE,
      team: 'all',            // all | home | away
      layer: 'shots',         // shots | zones | grid
      metric: 'pct',          // pct 命中率 | att 出手量（网格热力用）
      useHeat: false,         // 散点模式下是否叠加密度云
      view: 'half',           // half 半场（镜像） | full 全场（原始分布）
      playerFilter: '',
      minAtt: 3
    };
  },
  computed: {
    game: function () { return this.S.game; },
    sc: function () { return this.S.shotchart || { all: { points: [], zones: [], grid: [] } }; },
    /**
     * 球场标定是否适用于这段视频。
     * 不适用时后端**不再产出**热区（`all.unavailable` + `reason`）——
     * 因为球场坐标整片错位，画出来像真的但全是垃圾（用户实测反馈"热点完全不对"）。
     * 这里要把原因明说，而不是展示一张空图让人以为"没出手"。
     */
    unavailable: function () {
      var a = (this.sc && this.sc.all) || {};
      return !!a.unavailable;
    },
    unavailableReason: function () {
      var a = (this.sc && this.sc.all) || {};
      return a.reason || (this.game && this.game.meta &&
        this.game.meta.court_outputs_reason) ||
        '这份球场标定不适用于这段视频（机位/分辨率不匹配），无法把出手点投到球场上。';
    },
    /** 「标定可用但 0 次出手」时，说清楚为什么 —— 不能只给一张空球场。
     *  实测用户看到空热区会以为功能坏了；真正的原因通常是：镜头在动 →
     *  篮筐判据放弃；球太小 → 追不到；也没有记分牌事件 → 出手数就是 0。 */
    emptyWhy: function () {
      var m = (this.game && this.game.meta) || {};
      var lines = [];
      lines.push('出手次数：0（这次分析没有产生任何出手记录）');
      if (m.court_outputs_unverified) {
        lines.push('另外提醒：热区用的是**你手动标的**球场标定，'
          + '自动校验没通过 —— 位置可能有偏差。');
      }
      var ve = m.visual_error || (m.hoopsight && m.hoopsight.reason);
      if (ve) { lines.push('为什么没出手：' + String(ve).slice(0, 160)); }
      var rej = m.calibration_rejected;
      if (rej) { lines.push('标定校验：' + String(rej).slice(0, 160)); }
      lines.push('');
      lines.push('想让热区有内容，三条路：');
      lines.push('  ① 用记分牌路径：视频里有可读记分牌时，'
        + '得分事件会直接变成出手记录（这段视频底部就有记分牌）；');
      lines.push('  ② 换更清楚的素材：球在画面里 ≥30 像素时，'
        + '出手检测才能工作（判断标准见 README「素材体检」）；');
      lines.push('  ③ 人工复核：在「人工复核」里逐条确认/补录出手。');
      return lines.join('\n');
    },

    /** 当前球队的出手点：优先用 shotchart 点集的 team 字段过滤 */
    points: function () {
      var self = this;
      var all = (this.sc.all && this.sc.all.points) || [];
      var list = this.team === 'all' ? all : all.filter(function (p) { return p.team === self.team; });
      if (this.playerFilter) {
        list = list.filter(function (p) { return p.player_id === self.playerFilter; });
      }
      return list;
    },

    /** 当前球队的分区聚合（后端 by_team 优先，缺失则前端按同一口径现算） */
    zones: function () {
      var D = window.D;
      if (this.playerFilter) {
        // 按球员筛选时后端没有对应聚合，前端用与 zone_of 相同的口径现算
        return D.aggregateZones(this.points);
      }
      if (this.team === 'all') {
        return (this.sc.all && this.sc.all.zones && this.sc.all.zones.length)
          ? this.sc.all.zones : D.aggregateZones(this.points);
      }
      var bt = this.sc.zonesByTeam || {};
      var arr = bt[this.team] || [];
      return arr.length ? arr : D.aggregateZones(this.points);
    },

    /** 网格热力：后端 grid 只覆盖镜像后的单侧半场；按球员筛选时前端现算 */
    grid: function () {
      var bin = this.sc.all.bin_size || 1.0;
      if (this.playerFilter || !this.sc.all.grid || !this.sc.all.grid.length) {
        return window.D.buildGrid(this.points, bin);
      }
      return this.sc.all.grid;
    },
    binSize: function () { return this.sc.all.bin_size || 1.0; },

    /** 分区表：按出手数排序，供右侧表格展示（含「每次出手得分 PPS」效率口径） */
    zoneRows: function () {
      return (this.zones || []).slice().map(function (z) {
        var pct = z.pct > 1 ? z.pct / 100 : z.pct;
        return {
          zone: z.zone, att: z.att, made: z.made, pct: pct,
          points: z.points === undefined ? z.made * 2 : z.points,
          pps: z.pps === undefined ? (z.att ? (z.points || z.made * 2) / z.att : 0) : z.pps
        };
      }).sort(function (a, b) { return b.att - a.att; });
    },

    /** 高效区 / 低效区：样本量达标（默认 ≥3 次）后按 PPS 排序，答辩时的「结论」 */
    bestZone: function () { return this.rankZone(0); },
    worstZone: function () { return this.rankZone(-1); },

    /** 出手构成：罚球 / 两分 / 三分 */
    valueBreak: function () {
      var v = { 1: { att: 0, made: 0 }, 2: { att: 0, made: 0 }, 3: { att: 0, made: 0 } };
      this.points.forEach(function (p) {
        var k = Number(p.value || 2);
        if (!v[k]) v[k] = { att: 0, made: 0 };
        v[k].att++;
        if (p.made) v[k].made++;
      });
      return [
        { label: '罚球', att: v[1].att, made: v[1].made, color: '#8b5cf6' },
        { label: '两分球', att: v[2].att, made: v[2].made, color: '#2f6fed' },
        { label: '三分球', att: v[3].att, made: v[3].made, color: '#f2762e' }
      ];
    },

    /** 距离带分布（0-1m 到 7m+）：命中率随距离衰减，最能讲故事的一张图 */
    distOption: function () {
      var bands = [
        { label: '0-1m', lo: 0, hi: 1 }, { label: '1-2m', lo: 1, hi: 2 },
        { label: '2-3m', lo: 2, hi: 3 }, { label: '3-4m', lo: 3, hi: 4 },
        { label: '4-5.5m', lo: 4, hi: 5.5 }, { label: '5.5-6.75m', lo: 5.5, hi: 6.75 },
        { label: '6.75m+', lo: 6.75, hi: 99 }
      ];
      var D = window.D;
      bands.forEach(function (b) { b.att = 0; b.made = 0; });
      this.points.forEach(function (p) {
        var d = p.distance === undefined ? D.distanceToHoop(p.x, p.y) : p.distance;
        for (var i = 0; i < bands.length; i++) {
          if (d >= bands[i].lo && d < bands[i].hi) { bands[i].att++; if (p.made) bands[i].made++; break; }
        }
      });
      return {
        tooltip: {
          trigger: 'axis',
          formatter: function (ps) {
            var i = ps[0].dataIndex, b = bands[i];
            return b.label + '<br/>出手 ' + b.att + ' 次 / 命中 ' + b.made + ' 次<br/>命中率 ' +
              (b.att ? D.pct(b.made / b.att) : '—');
          }
        },
        grid: { left: 44, right: 20, top: 30, bottom: 28, containLabel: true },
        xAxis: { type: 'category', data: bands.map(function (b) { return b.label; }) },
        yAxis: [
          { type: 'value', name: '出手数' },
          { type: 'value', name: '命中率', max: 1, axisLabel: { formatter: function (v) { return Math.round(v * 100) + '%'; } } }
        ],
        series: [
          { name: '出手数', type: 'bar', barMaxWidth: 26, itemStyle: { color: '#cfd8e6', borderRadius: [3, 3, 0, 0] }, data: bands.map(function (b) { return b.att; }) },
          { name: '命中数', type: 'bar', barMaxWidth: 26, itemStyle: { color: '#22a06b', borderRadius: [3, 3, 0, 0] }, data: bands.map(function (b) { return b.made; }) },
          { name: '命中率', type: 'line', yAxisIndex: 1, smooth: true, symbolSize: 6, lineStyle: { color: '#f2762e', width: 2.4 }, itemStyle: { color: '#f2762e' }, data: bands.map(function (b) { return b.att ? +(b.made / b.att).toFixed(3) : 0; }) }
        ]
      };
    },

    playerOptions: function () {
      var self = this;
      var opts = [{ value: '', label: '全部球员' }];
      (this.S.players || []).forEach(function (p) {
        if (self.team !== 'all' && p.team !== self.team) return;
        opts.push({ value: p.player_id, label: p.name });
      });
      return opts;
    },

    /** 场均/合计摘要文字 */
    summary: function () {
      var att = this.points.length;
      var made = this.points.filter(function (p) { return p.made; }).length;
      var three = this.points.filter(function (p) { return Number(p.value) === 3; });
      var threeMade = three.filter(function (p) { return p.made; }).length;
      var pts = this.points.reduce(function (a, p) { return a + (p.made ? Number(p.value || 2) : 0); }, 0);
      return {
        att: att, made: made, pct: att ? made / att : 0,
        tpa: three.length, tpm: threeMade, tpPct: three.length ? threeMade / three.length : 0,
        points: pts, pps: att ? Math.round(pts / att * 100) / 100 : 0
      };
    },
    teamLabel: function () {
      if (this.team === 'all') return '双方合计';
      return window.D.teamName(this.game, this.team);
    }
  },
  methods: {
    teamName: function (s) { return window.D.teamName(this.game, s); },
    pct: function (v) { return window.D.pct(v); },
    rankZone: function (dir) {
      var min = this.minAtt || 3;
      var mins = this.zoneRows.filter(function (z) { return z.att >= min; });
      if (!mins.length) return null;
      mins.sort(function (a, b) { return dir < 0 ? a.pps - b.pps : b.pps - a.pps; });
      return mins[0];
    },
    reset: function () {
      this.team = 'all'; this.layer = 'shots'; this.metric = 'pct';
      this.useHeat = false; this.view = 'half'; this.playerFilter = '';
    }
  },
  watch: {
    // 切换球队时清掉球员筛选（否则会筛出空集）
    team: function () { this.playerFilter = ''; },
    // 分区热力只定义在半场（分区口径基于到篮筐距离），选分区时自动回到半场视图
    layer: function (v) { if (v === 'zones') this.view = 'half'; }
  },
  template: [
    '<div>',
    // 标定不适用 → 后端不出热区。这里必须明说原因，不能只给空图。
    '  <div class="card" v-if="unavailable">',
    '    <h3 class="card-title">投篮热区 <span class="sub">不适用</span></h3>',
    '    <el-alert type="warning" :closable="false" show-icon',
    '      title="这段视频没有可用的球场标定，无法生成投篮热区"',
    '      :description="unavailableReason + \'\\n\\n\' +',
    '        \'（宁可不画，也不画错的：球场坐标整片错位时，热区图看起来正常但全是垃圾。\' +',
    '        \'要出热区请先用 calibrate 对这段视频做球场标定。）\'" />',
    '  </div>',
    '  <div class="card" v-else-if="!points.length">',
    '    <h3 class="card-title">投篮热区 <span class="sub">标定可用，但本片段没有出手</span></h3>',
    '    <el-alert type="info" :closable="false" show-icon',
    '      title="球场标定是好的，但这次分析**一次出手都没检测到**，所以热区是空的"',
    '      :description="emptyWhy" />',
    '  </div>',
    '  <div class="card" v-else-if="!points.length && !(S.game)">',
    '    <el-empty description="暂无投篮数据" />',
    '  </div>',

    '  <template v-else>',
    '    <!-- 工具栏 -->',
    '    <div class="card">',
    '      <h3 class="card-title">投篮热区 <span class="sub">数据来源：GET /api/games/{{ S.jobId }}/shotchart（shotchart.json）· 坐标口径与后端 zone_of / shot_chart 完全一致</span>',
    '        <span class="grow"></span>',
    '        <el-tag size="small" type="info" effect="plain">{{ teamLabel }}</el-tag>',
    '        <el-tag size="small" effect="plain">出手 {{ summary.att }} · 命中 {{ summary.made }} · {{ pct(summary.pct) }}</el-tag>',
    '      </h3>',
    '      <div class="court-toolbar">',
    '        <el-radio-group v-model="team" size="small">',
    '          <el-radio-button label="all">双方</el-radio-button>',
    '          <el-radio-button label="home">{{ teamName(\'home\') }}</el-radio-button>',
    '          <el-radio-button label="away">{{ teamName(\'away\') }}</el-radio-button>',
    '        </el-radio-group>',
    '        <el-select v-model="playerFilter" size="small" style="width:150px" placeholder="全部球员">',
    '          <el-option v-for="o in playerOptions" :key="o.value" :label="o.label" :value="o.value" />',
    '        </el-select>',
    '        <el-divider direction="vertical" />',
    '        <el-radio-group v-model="layer" size="small">',
    '          <el-radio-button label="shots">出手散点</el-radio-button>',
    '          <el-radio-button label="zones">分区热力</el-radio-button>',
    '          <el-radio-button label="grid">网格热力</el-radio-button>',
    '        </el-radio-group>',
    '        <el-radio-group v-model="view" size="small">',
    '          <el-radio-button label="half">半场（镜像）</el-radio-button>',
    '          <el-radio-button label="full" :disabled="layer===\'zones\'">全场（原始）</el-radio-button>',
    '        </el-radio-group>',
    '        <template v-if="layer===\'grid\'">',
    '          <el-select v-model="metric" size="small" style="width:120px">',
    '            <el-option label="按命中率" value="pct" />',
    '            <el-option label="按出手量" value="att" />',
    '          </el-select>',
    '        </template>',
    '        <el-checkbox v-if="layer===\'shots\'" v-model="useHeat">叠加密度云</el-checkbox>',
    '        <el-button size="small" text @click="reset">重置</el-button>',
    '      </div>',

    '      <div class="grid grid-court">',
    '        <div>',
    '          <!-- 球场 + 图层（court.js 手工绘制标准 FIBA 半场） -->',
    '          <court-view :view="view" :layer="layer" :points="points" :zones="zones"',
    '                      :grid="grid" :bin="binSize" :metric="metric" :use-heat="useHeat" />',
    '          <div class="legend">',
    '            <span><i class="dot"></i>命中（实心绿）</span>',
    '            <span><i class="ring"></i>未中（空心红）</span>',
    '            <span>外圈描边 = 三分出手</span>',
    '            <span v-if="layer===\'zones\'">分区色 = 该区命中率（冷蓝→热红）</span>',
    '            <span v-if="layer===\'grid\' && metric===\'pct\'">网格色 = 该格命中率</span>',
    '            <span v-if="layer===\'grid\' && metric===\'att\'">网格色 = 该格出手量</span>',
    '          </div>',
    '          <div class="hint" style="margin-top:8px">',
    '            球场几何（FIBA 2014+）：三分弧半径 <b>6.75m</b>、底角三分直线距边线 <b>0.90m</b>（横向 |x| ≥ 6.60）、',
    '            罚球区 <b>4.90m × 5.80m</b>、罚球圈半径 <b>1.80m</b>、合理冲撞区半径 <b>1.25m</b>、篮筐距底线 <b>1.575m</b>。',
    '          </div>',
    '        </div>',

    '        <div>',
    '          <!-- 结论卡片：高效区 / 低效区 -->',
    '          <div class="grid grid-2" style="gap:10px">',
    '            <div class="kpi" v-if="bestZone">',
    '              <div class="k">最高效区域（每次出手得分）</div>',
    '              <div class="v" style="color:#22a06b">{{ bestZone.pps }}</div>',
    '              <div class="d">{{ bestZone.zone }} · {{ bestZone.made }}/{{ bestZone.att }} · {{ pct(bestZone.pct) }}</div>',
    '            </div>',
    '            <div class="kpi" v-if="worstZone">',
    '              <div class="k">最低效区域</div>',
    '              <div class="v" style="color:#e5484d">{{ worstZone.pps }}</div>',
    '              <div class="d">{{ worstZone.zone }} · {{ worstZone.made }}/{{ worstZone.att }} · {{ pct(worstZone.pct) }}</div>',
    '            </div>',
    '          </div>',
    '          <div class="hint" style="margin:10px 0">仅统计出手 ≥ {{ minAtt }} 次的区域；',
    '            <b>每次出手得分（PPS）</b>比命中率更能反映效率 —— 这是热区分析的核心口径。</div>',

    '          <el-table :data="zoneRows" size="small" border max-height="300" style="margin-top:6px">',
    '            <el-table-column prop="zone" label="分区" min-width="104" />',
    '            <el-table-column prop="att" label="出手" width="70" align="center" sortable />',
    '            <el-table-column prop="made" label="命中" width="70" align="center" sortable />',
    '            <el-table-column label="命中率" width="92" align="center" sortable prop="pct">',
    '              <template #default="s">{{ pct(s.row.pct) }}</template>',
    '            </el-table-column>',
    '            <el-table-column prop="pps" label="每次出手得分" width="120" align="center" sortable />',
    '          </el-table>',

    '          <el-divider />',
    '          <!-- 出手构成 -->',
    '          <div class="row" style="gap:16px">',
    '            <div v-for="b in valueBreak" :key="b.label" class="kpi" style="flex:1">',
    '              <div class="k">{{ b.label }}</div>',
    '              <div class="v" :style="{color:b.color}">{{ b.made }}/{{ b.att }}</div>',
    '              <div class="d">命中率 {{ b.att ? pct(b.made/b.att) : \'—\' }}</div>',
    '            </div>',
    '          </div>',
    '          <div class="hint" style="margin-top:10px">',
    '            合计得分 <b>{{ summary.points }}</b> 分，每次出手 <b>{{ summary.pps }}</b> 分；',
    '            三分 {{ summary.tpm }}/{{ summary.tpa }}（{{ pct(summary.tpPct) }}）。',
    '          </div>',
    '        </div>',
    '      </div>',
    '    </div>',

    '    <!-- 距离带分布 -->',
    '    <div class="card">',
    '      <h3 class="card-title">出手距离分布与命中率衰减 <span class="sub">按「到最近篮筐的距离」分桶（与后端 Shot.distance 口径一致）</span></h3>',
    '      <echarts-box :option="distOption" height="300px" />',
    '    </div>',

    '    <!-- 分区命中率条形对比 -->',
    '    <div class="card">',
    '      <h3 class="card-title">分区效率对比 <span class="sub">命中率（柱） + 每次出手得分（折线），按后端 zone_of 的 6 个中文分区</span></h3>',
    '      <echarts-box :option="zoneOption" height="300px" />',
    '    </div>',
    '  </template>',
    '</div>'
  ].join('\n')
};

/** 分区效率对比图：命中率柱 + PPS 折线（放在 computed 上，避免模板里写复杂表达式） */
Object.defineProperty(window.PAGES['shotchart'].computed, 'zoneOption', {
  get: function () {
    var order = window.D.ZONES;
    var rows = this.zoneRows.slice().sort(function (a, b) {
      return order.indexOf(a.zone) - order.indexOf(b.zone);
    });
    return {
      tooltip: { trigger: 'axis' },
      legend: { top: 0 },
      grid: { left: 48, right: 46, top: 34, bottom: 26, containLabel: true },
      xAxis: { type: 'category', data: rows.map(function (r) { return r.zone; }) },
      yAxis: [
        { type: 'value', name: '命中率', max: 1, axisLabel: { formatter: function (v) { return Math.round(v * 100) + '%'; } } },
        { type: 'value', name: 'PPS', max: 3 }
      ],
      series: [
        {
          name: '命中率', type: 'bar', barMaxWidth: 34,
          itemStyle: { borderRadius: [4, 4, 0, 0], color: '#2f6fed' },
          label: { show: true, position: 'top', formatter: function (p) { return Math.round(p.value * 100) + '%'; }, fontSize: 11 },
          data: rows.map(function (r) { return +(r.pct || 0).toFixed(3); })
        },
        {
          name: '每次出手得分', type: 'line', yAxisIndex: 1, smooth: true, symbolSize: 7,
          lineStyle: { color: '#f2762e', width: 2.4 }, itemStyle: { color: '#f2762e' },
          data: rows.map(function (r) { return r.pps; })
        }
      ]
    };
  }
});
