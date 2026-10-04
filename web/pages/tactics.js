/* ==========================================================================
   pages/tactics.js —— 页面 5：战术分析（核心展示页）
   --------------------------------------------------------------------------
   数据来源（懒加载：只有真的打开这一页才去拉，不拖慢总览/统计页）：
     GET /api/games/{id}/tactics          tactics.json
        possession  回合统计（total / by_team / avg_passes / list / spells）
        passes      传球事件 + 排行榜（events / leaderboard）
        pass_network{home,away}  有向传球网络（nodes 带平均站位 x/d、edges 带次数）
        formation   {offense:{team:[段]}, defense:{team:[段]}, summary:{team:{...}}}
        spacing     {teams:{team:{area,...}}, timeline:[{t,team,area,...}]}
        insights    可直接展示的战术结论
     GET /api/games/{id}/tactics/frames   俯视战术图逐帧数据
        {available, count, frames:[{t, side, ball:[x,y], players:[[id,team,x,y],...]}]}

   四块内容（对应方案里的"战术分析页"清单）：
     ① 俯视战术图播放器（带球员轨迹尾迹、球、可拖进度条）
     ② 传球网络图（画在半场上，节点=球员平均站位、边宽=传球次数）
     ③ 阵型识别结果（进攻落位 / 防守阵型的时间轴 + 占比）
     ④ 间距曲线（凸包面积 / 平均间距 / 宽度 / 纵深，两队对比）

   为什么战术图只画"一张半场"：后端 tactics.py 把两侧进攻折叠成
   「局部进攻坐标」(x, d=|y|)，被进攻的篮筐永远在 (0,1.575)。
   所以一张半场就能同时表达左右两侧的进攻，且两队可以直接叠着比。
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['tactics'] = {
  name: 'page-tactics',
  data: function () {
    return {
      S: window.STORE,
      // 球场底图与叠加层用同一个 viewBox，保证像素级对齐
      courtSVG: window.Court.courtSVG({ view: 'half' }),
      loading: false,
      err: '',
      // 播放器
      altJobs: [],          // 其它「有战术数据」的任务（空状态下一键切换）
      playing: false,
      playT: 0,
      speed: 1,
      showTrails: true,
      raf: null,
      _last: 0,
      // 传球网络
      netTeam: 'home',
      // 间距曲线
      metric: 'area',
      METRICS: [
        { key: 'area', label: '凸包面积', unit: 'm²' },
        { key: 'mean_dist', label: '平均间距', unit: 'm' },
        { key: 'width', label: '阵型宽度', unit: 'm' },
        { key: 'depth', label: '阵型纵深', unit: 'm' },
        { key: 'radius', label: '重心到篮筐', unit: 'm' }
      ]
    };
  },
  computed: {
    game: function () { return this.S.game; },
    tac: function () { return this.S.tactics; },
    summary: function () { return (this.game && this.game.tactics) || {}; },
    frames: function () {
      var d = this.S.tacticsFrames;
      if (!d) return [];
      return Array.isArray(d) ? d : (d.frames || []);
    },
    duration: function () {
      return (this.game && this.game.duration) || (this.frames.length ? this.frames[this.frames.length - 1].t : 0);
    },
    hasTactics: function () {
      return !!(this.tac && this.tac.available);
    },
    reason: function () {
      if (this.tac && !this.tac.available) return this.tac.reason || '';
      return this.summary.reason || '';
    },

    // ---------------- 播放器 ----------------
    frameIdx: function () {
      var f = this.frames;
      if (!f.length) return 0;
      var t = this.playT, lo = 0, hi = f.length - 1;
      if (t <= f[0].t) return 0;
      if (t >= f[hi].t) return hi;
      while (lo < hi) {
        var mid = (lo + hi + 1) >> 1;
        if (f[mid].t <= t) lo = mid; else hi = mid - 1;
      }
      return lo;
    },
    frame: function () { return this.frames[this.frameIdx] || null; },
    teamOf: function () {
      var m = {};
      (this.frame ? this.frame.players : []).forEach(function (p) { m[p[0]] = p[1]; });
      return m;
    },
    /** 最近 10 帧的轨迹尾迹（按球员 id 聚合） */
    trails: function () {
      if (!this.showTrails || !this.frames.length) return {};
      var out = {}, i0 = Math.max(0, this.frameIdx - 10);
      for (var i = i0; i <= this.frameIdx; i++) {
        (this.frames[i].players || []).forEach(function (p) {
          (out[p[0]] = out[p[0]] || []).push([p[2], p[3]]);
        });
      }
      return out;
    },
    overlayHTML: function () {
      if (!this.frame) return '';
      var f = this.frame;
      var teams = this.teamOf;
      var trails = {};
      Object.keys(this.trails).forEach(function (k) { trails[k] = this.trails[k]; }, this);
      return window.Court.tacticsPlayersLayer(f, trails, {
        showTrails: this.showTrails, teamOf: teams
      });
    },
    timeLabel: function () { return window.D.mmss(this.playT); },

    // ---------------- 传球网络 ----------------
    net: function () {
      var t = this.tac;
      if (!t || !t.pass_network) return { nodes: [], edges: [] };
      return t.pass_network[this.netTeam] || { nodes: [], edges: [] };
    },
    netHTML: function () {
      if (!this.hasTactics) return '';
      return window.Court.passNetLayer(this.net, { team: this.netTeam });
    },
    netEdges: function () {
      var self = this;
      var names = {};
      (this.net.nodes || []).forEach(function (n) { names[n.id] = n.name || n.id; });
      return (this.net.edges || []).slice(0, 8).map(function (e) {
        return {
          from: names[e.source] || e.source, to: names[e.target] || e.target,
          value: e.value
        };
      });
    },

    // ---------------- 阵型 ----------------
    teams: function () { return ['home', 'away']; },
    formationRows: function () {
      var t = this.tac;
      if (!t) return [];
      var self = this;
      var out = [];
      this.teams.forEach(function (side) {
        var off = ((t.formation || {}).summary || {})[side] || {};
        out.push({
          side: side,
          name: window.D.teamName(self.game, side),
          offense: (off.offense || [])[0] ? off.offense[0].label : '-',
          offenseTop: (off.offense || []).slice(0, 3),
          defense: (off.defense || [])[0] ? off.defense[0].label : '-',
          defenseTop: (off.defense || []).slice(0, 3)
        });
      });
      return out;
    },
    offBars: function () { return this._bars('offense'); },
    defBars: function () { return this._bars('defense'); },

    // ---------------- 间距曲线 ----------------
    spacingOption: function () {
      var t = this.tac;
      var tl = (t && t.spacing && t.spacing.timeline) || [];
      var m = this.metric;
      var self = this;
      // ⚠️ 必须先按球队过滤再抽稀：timeline 是"同一时刻主客各一条"交替排列的，
      // 直接整体按 i+=step 抽稀会让步长与球队交替同频，结果只剩一支球队
      // （后端也踩过同一个坑，见 tactics.py 里按球队分别抽稀的注释）。
      function pick(side) {
        var rows = tl.filter(function (r) { return r.team === side; });
        var st = rows.length > 600 ? Math.ceil(rows.length / 600) : 1;
        var out = [];
        for (var i = 0; i < rows.length; i += st) {
          out.push([+rows[i].t.toFixed(1), rows[i][m]]);
        }
        return out;
      }
      return {
        tooltip: { trigger: 'axis' },
        legend: { top: 0, data: [window.D.teamName(this.game, 'home'), window.D.teamName(this.game, 'away')] },
        grid: { left: 52, right: 20, top: 34, bottom: 40, containLabel: true },
        xAxis: { type: 'value', name: '时间(秒)', min: 0, max: Math.ceil(this.duration) },
        yAxis: { type: 'value', name: this.METRICS.filter(function (x) { return x.key === m; })[0].label },
        series: [
          { name: window.D.teamName(this.game, 'home'), type: 'line', showSymbol: false,
            smooth: true, lineStyle: { width: 2, color: '#2f6fed' }, itemStyle: { color: '#2f6fed' },
            data: pick('home') },
          { name: window.D.teamName(this.game, 'away'), type: 'line', showSymbol: false,
            smooth: true, lineStyle: { width: 2, color: '#f2762e' }, itemStyle: { color: '#f2762e' },
            data: pick('away') }
        ]
      };
    },
    attackSideText: function () {
      var v = (this.tac && this.tac.attack_side) || '';
      return ({ left: '左半场', right: '右半场', mixed: '左右两侧' })[v] || '-';
    },
    spacingTeams: function () {
      var sp = (this.tac && this.tac.spacing && this.tac.spacing.teams) || this.summary.spacing || {};
      var self = this;
      return this.teams.map(function (s) {
        return { side: s, name: window.D.teamName(self.game, s), d: sp[s] || {} };
      });
    }
  },
  methods: {
    teamName: function (side) { return window.D.teamName(this.game, side); },
    mmss: function (t) { return window.D.mmss(t); },

    // ---- 数据加载 ----
    load: function () {
      var self = this;
      if (self.S.tacticsLoaded) return Promise.resolve();
      self.loading = true;
      return window.APP_TACTICS().then(function () {
        self.loading = false;
        if (!self.hasTactics && self.S.backendOk) self.loadAltJobs();
      }).catch(function (e) {
        self.loading = false;
        self.err = String((e && e.message) || e);
      });
    },
    /** 后端连着但这一场没有战术数据时，列出"有战术数据"的任务供一键切换。 */
    loadAltJobs: function () {
      var self = this;
      return window.API.listJobs().then(function (list) {
        var arr = Array.isArray(list) ? list : (list && list.jobs) || [];
        self.altJobs = arr.filter(function (j) {
          return j.status === 'done' && j.has_tactics && j.job_id !== self.S.jobId;
        }).slice(0, 5);
      }).catch(function () { self.altJobs = []; });
    },
    switchTo: function (id) {
      var self = this;
      self.loading = true;
      window.APP_LOAD_JOB(id).then(function () {
        return self.load();
      }).then(function () {
        self.loading = false;
        self.$message.success('已切换到任务 ' + String(id).slice(0, 8));
      }).catch(function (e) {
        self.loading = false;
        self.$message.error('切换失败：' + ((e && e.message) || e));
      });
    },

    _bars: function (kind) {
      var t = this.tac;
      if (!t) return [];
      var self = this;
      return this.teams.map(function (side) {
        var segs = ((t.formation || {})[kind] || {})[side] || [];
        return {
          side: side,
          name: window.D.teamName(self.game, side),
          html: window.Court.formationBar(segs, {
            total: self.duration || 1,
            colorOf: function (g) { return self.colorOf(g.shape || g.scheme); }
          }),
          count: segs.length
        };
      });
    },
    colorOf: function (label) {
      var map = {
        '五外拉开': '#2f6fed', '四外一内': '#0ea5e9', '三外两内': '#22a06b',
        '双塔（内线为主）': '#f2762e', '均衡落位': '#8b5cf6', '转换进攻': '#e5484d',
        '人盯人': '#2f6fed', '2-3 联防': '#22a06b', '3-2 联防': '#f5a623',
        '1-3-1 联防': '#8b5cf6', '1-2-2 联防': '#0ea5e9', '收缩联防': '#64748b',
        '前场施压（紧逼）': '#e5484d', '混合防守': '#94a3b8'
      };
      return map[label] || '#94a3b8';
    },

    // ---- 播放控制 ----
    toggle: function () {
      if (!this.frames.length) return;
      this.playing = !this.playing;
      if (this.playing) {
        if (this.playT >= this.duration) this.playT = 0;
        this._last = 0;
        this.raf = requestAnimationFrame(this.tick);
      } else if (this.raf) {
        cancelAnimationFrame(this.raf); this.raf = null;
      }
    },
    tick: function (ts) {
      if (!this.playing) return;
      if (!this._last) this._last = ts;
      var dt = Math.min(0.5, (ts - this._last) / 1000);
      this._last = ts;
      this.playT = Math.min(this.duration, this.playT + dt * this.speed);
      if (this.playT >= this.duration) {
        this.playing = false; this.raf = null; return;
      }
      var self = this;
      this.raf = requestAnimationFrame(function (t) { self.tick(t); });
    },
    seek: function (v) { this.playT = Number(v) || 0; },
    jumpTo: function (t) {
      this.playT = Number(t) || 0;
      if (!this.playing) this.playing = true;
      this._last = 0;
      if (this.raf) cancelAnimationFrame(this.raf);
      var self = this;
      this.raf = requestAnimationFrame(function (x) { self.tick(x); });
    },
    step: function (d) { this.seek(Math.max(0, Math.min(this.duration, this.playT + d))); }
  },
  mounted: function () {
    this.load();
  },
  beforeUnmount: function () {
    this.playing = false;
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = null; }
  },
  template: [
    '<div class="page">',
    '  <div class="page-head">',
    '    <h2>战术分析</h2>',
    '    <div class="page-sub">控球归属 → 传球网络 → 阵型识别 → 空间指标；俯视战术图可逐帧回放。</div>',
    '  </div>',

    '  <el-skeleton v-if="loading" :rows="6" animated />',

    '  <el-alert v-else-if="!hasTactics" type="warning" :closable="false" show-icon',
    '            title="本场没有战术数据">',
    '    <div style="line-height:1.7">',
    '      {{ reason || "战术层需要球员逐帧球场坐标：真视频要么有一份通过校验的球场标定，要么自动逐帧标定达标（工具会自动尝试）；合成数据源默认自带。" }}',
    '      <div class="muted" style="margin-top:6px">',
    '        怎么拿到战术数据：① 用「上传与分析」建一个 <b>synthetic</b> 任务（最快，零依赖）；',
    '        ② 真视频任务：保持球员检测开启，并做一次球场标定（同一帧里点 6 个以上、不在同一条线上的特征点）；',
    '        没标定时工具会尝试<b>自动逐帧标定</b>，不达标时上面的原因里会给出读数；',
    '        ③ 无后端时把 <code>web/demo/tactics.json</code> 准备好（跑一次 sync_demo 脚本）。',
    '      </div>',
    '      <div v-if="altJobs.length" style="margin-top:10px">',
    '        <div class="muted">下面这些任务<b>已经有战术数据</b>，点一下直接切过去：</div>',
    '        <el-button v-for="j in altJobs" :key="j.job_id" size="small" type="primary"',
    '                   plain style="margin:6px 6px 0 0" @click="switchTo(j.job_id)">',
    '          {{ String(j.job_id).slice(0,8) }} · {{ j.source }} · {{ j.summary }}',
    '        </el-button>',
    '      </div>',
    '    </div>',
    '  </el-alert>',

    '  <template v-else>',
    // 位置来源提示：静态标定没通过校验、改用**自动逐帧标定**算球员坐标时，
    // 结论照给，但必须说清"这些坐标没有经过独立校验"——不能让它冒充已校验。
    '    <el-alert v-if="tac.position_unverified" type="warning" :closable="false" show-icon style="margin:6px 0 12px">',
    '      <template #title>球员位置来自<b>自动逐帧标定</b>（未独立校验）</template>',
    '      <div style="line-height:1.7">',
    '        {{ tac.position_note || "这份坐标是按球场线自动拟合出来的，没有用画面里的真值点核对过：回合/传球/阵型/间距等相对结论可参考，具体坐标可能有系统偏差。" }}',
    '        <div class="muted" style="margin-top:6px">',
    '          想要更可信的位置：回到「上传与分析」→ 标球场，在<b>同一帧里点 6 个以上不在同一条线上</b>的特征点（底线两角 + 罚球区两角 + 中圈）。',
    '        </div>',
    '      </div>',
    '    </el-alert>',
    '    <!-- ① KPI -->',
    '    <div class="kpi-row">',
    '      <div class="kpi"><div class="v">{{ tac.possession.total }}</div><div class="k">回合数</div></div>',
    '      <div class="kpi"><div class="v">{{ tac.passes.total }}</div><div class="k">成功传球</div></div>',
    '      <div class="kpi"><div class="v">{{ tac.passes.turnovers }}</div><div class="k">球权转换</div></div>',
    '      <div class="kpi"><div class="v">{{ tac.possession.avg_passes }}</div><div class="k">每回合传球</div></div>',
    '      <div class="kpi"><div class="v">{{ tac.possession.avg_duration }}s</div><div class="k">平均回合时长</div></div>',
    '      <div class="kpi"><div class="v">{{ attackSideText }}</div><div class="k">进攻方向</div></div>',
    '    </div>',

    '    <!-- ② 俯视战术图 + 战术亮点 -->',
    '    <el-card shadow="never" class="blk">',
    '      <template #header><b>① 俯视战术图</b>',
    '        <span class="muted">（半场俯视 · 左右两侧进攻已折叠到同一套坐标 · 点轨迹为最近 5 秒）</span>',
    '      </template>',
    '      <div class="tac-grid">',
    '        <div>',
    '          <div class="court-wrap">',
    '            <div v-html="courtSVG"></div>',
    '            <div class="tac-overlay" v-html="overlayHTML"></div>',
    '          </div>',
    '          <div class="playbar">',
    '            <el-button size="small" :icon="playing ? \'VideoPause\' : \'VideoPlay\'" @click="toggle">',
    '              {{ playing ? "暂停" : "播放" }}</el-button>',
    '            <el-button size="small" @click="step(-5)">-5s</el-button>',
    '            <el-button size="small" @click="step(5)">+5s</el-button>',
    '            <el-slider class="playbar-slider" :model-value="playT" :min="0" :max="duration"',
    '                       :step="0.1" :show-tooltip="false" @input="seek" />',
    '            <span class="mono">{{ timeLabel }} / {{ mmss(duration) }}</span>',
    '            <el-select v-model="speed" size="small" style="width:88px">',
    '              <el-option :value="0.5" label="0.5×" /><el-option :value="1" label="1×" />',
    '              <el-option :value="2" label="2×" /><el-option :value="4" label="4×" />',
    '            </el-select>',
    '            <el-checkbox v-model="showTrails" size="small">轨迹</el-checkbox>',
    '          </div>',
    '        </div>',
    '        <div>',
    '          <div class="blk-title">战术亮点</div>',
    '          <ul class="insight-list">',
    '            <li v-for="(i,k) in tac.insights" :key="k">',
    '              <a href="javascript:void(0)" @click="jumpTo(i.t)">▶ {{ mmss(i.t) }}</a>',
    '              <span>{{ i.text }}</span>',
    '            </li>',
    '            <li v-if="!tac.insights.length" class="muted">本场没有提取到战术亮点。</li>',
    '          </ul>',
    '          <div class="blk-title" style="margin-top:14px">口径说明</div>',
    '          <div class="muted small">',
    '            控球 = 球位置上「最近的球员」在 {{ (tac.config && tac.config.possess_radius) || 2.5 }}m 内；',
    '            传球 = 控球人在队友之间切换；阵型 = 可解释的几何规则（不是训练模型）。',
    '          </div>',
    '        </div>',
    '      </div>',
    '    </el-card>',

    '    <!-- ③ 传球网络 -->',
    '    <el-card shadow="never" class="blk">',
    '      <template #header><b>② 传球网络</b>',
    '        <span class="muted">（节点=球员平均站位，圆内数字=传球+接球次数，连线粗细=二人之间传球次数）</span>',
    '      </template>',
    '      <el-radio-group v-model="netTeam" size="small" style="margin-bottom:10px">',
    '        <el-radio-button label="home">{{ teamName("home") }}</el-radio-button>',
    '        <el-radio-button label="away">{{ teamName("away") }}</el-radio-button>',
    '      </el-radio-group>',
    '      <div class="tac-net">',
    '        <div class="court-wrap" v-html="netHTML"></div>',
    '        <el-table :data="netEdges" size="small" style="width:340px">',
    '          <el-table-column label="传球连线" >',
    '            <template #default="s">{{ s.row.from }} → {{ s.row.to }}</template>',
    '          </el-table-column>',
    '          <el-table-column prop="value" label="次数" width="70" align="center" />',
    '        </el-table>',
    '      </div>',
    '    </el-card>',

    '    <!-- ④ 阵型识别 -->',
    '    <el-card shadow="never" class="blk">',
    '      <template #header><b>③ 阵型识别</b>',
    '        <span class="muted">（进攻落位与防守阵型的时间轴；点击时间轴可跳到该时刻）</span>',
    '      </template>',
    '      <el-table :data="formationRows" size="small">',
    '        <el-table-column label="球队" width="110">',
    '          <template #default="s"><b>{{ s.row.name }}</b></template>',
    '        </el-table-column>',
    '        <el-table-column label="主要进攻落位">',
    '          <template #default="s">',
    '            <el-tag v-for="x in s.row.offenseTop" :key="x.label" size="small"',
    '                    :color="colorOf(x.label)" effect="dark" class="tag-gap">',
    '              {{ x.label }} · {{ x.n }}</el-tag>',
    '          </template>',
    '        </el-table-column>',
    '        <el-table-column label="主要防守阵型">',
    '          <template #default="s">',
    '            <el-tag v-for="x in s.row.defenseTop" :key="x.label" size="small"',
    '                    :color="colorOf(x.label)" effect="dark" class="tag-gap">',
    '              {{ x.label }} · {{ x.n }}</el-tag>',
    '          </template>',
    '        </el-table-column>',
    '      </el-table>',
    '      <div v-for="b in offBars" :key="\'o\'+b.side" class="bar-row">',
    '        <span class="bar-label">{{ b.name }} · 进攻</span>',
    '        <span class="bar-track" v-html="b.html"></span>',
    '      </div>',
    '      <div v-for="b in defBars" :key="\'d\'+b.side" class="bar-row">',
    '        <span class="bar-label">{{ b.name }} · 防守</span>',
    '        <span class="bar-track" v-html="b.html"></span>',
    '      </div>',
    '    </el-card>',

    '    <!-- ⑤ 间距曲线 -->',
    '    <el-card shadow="never" class="blk">',
    '      <template #header><b>④ 空间/间距曲线</b>',
    '        <span class="muted">（球队整体的空间拉开程度随时间变化；两队对比）</span>',
    '      </template>',
    '      <el-radio-group v-model="metric" size="small" style="margin-bottom:8px">',
    '        <el-radio-button v-for="m in METRICS" :key="m.key" :label="m.key">',
    '          {{ m.label }}</el-radio-button>',
    '      </el-radio-group>',
    '      <echarts-box :option="spacingOption" height="300px" />',
    '      <el-table :data="spacingTeams" size="small" style="margin-top:10px">',
    '        <el-table-column label="球队" width="110">',
    '          <template #default="s"><b>{{ s.row.name }}</b></template>',
    '        </el-table-column>',
    '        <el-table-column label="凸包面积(m²)" align="center">',
    '          <template #default="s">{{ s.row.d.area }}</template></el-table-column>',
    '        <el-table-column label="平均间距(m)" align="center">',
    '          <template #default="s">{{ s.row.d.mean_dist }}</template></el-table-column>',
    '        <el-table-column label="宽度(m)" align="center">',
    '          <template #default="s">{{ s.row.d.width }}</template></el-table-column>',
    '        <el-table-column label="纵深(m)" align="center">',
    '          <template #default="s">{{ s.row.d.depth }}</template></el-table-column>',
    '        <el-table-column label="重心到篮筐(m)" align="center">',
    '          <template #default="s">{{ s.row.d.radius }}</template></el-table-column>',
    '      </el-table>',
    '    </el-card>',
    '  </template>',
    '  <el-alert v-if="err" type="error" :closable="false" :title="err" />',
    '</div>'
  ].join('\n'),
  // 球场层直接用 court.js（与叠加层同一 viewBox，保证像素级对齐）
};
