/* ==========================================================================
   pages/review.js —— 页面 7：人工复核（自研加分点）
   --------------------------------------------------------------------------
   数据来源接口：
     GET  /api/games/{job_id}                          game.json → needs_review 数组
     POST /api/games/{job_id}/shots/{index}/correct    body {made, value} → {ok:true}
         index 是「出手在整场事件流中的序号」（后端按 shots 顺序编号）
   业务口径（答辩讲解要点）：
     * 后端计分规则引擎用「加权投票 + 置信度」融合球穿筐 / 记分牌 OCR / 网动多路证据，
       置信度低于阈值（RulesConfig.review_threshold = 0.6）的出手进入复核队列，
       而不是硬判 —— 这是「可解释、可干预」的产品差异点。
     * 改判会写回统计口径：该次出手的分值（1/2/3）与命中结果重新参与
       比分、球队统计、球员统计、热区聚合，报告与导出随之更新。
     * 复核页自身不重算规则，只提交人工结论（后端 outcome_source 会标记为 manual）。
   无后端时的降级：只做本地改判（前端重算统计口径），并在页面顶部明确标注。
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['review'] = {
  name: 'page-review',
  data: function () {
    return {
      S: window.STORE,
      rows: [],                 // 本地可改判队列（复制自 game.needs_review）
<<<<<<< HEAD
      onlyInferred: false,
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
      showAll: false,           // 是否把高置信度出手也列出来
      busyIdx: -1,
      localLog: [],             // 改判记录（用于答辩演示时说明写回口径）
      manual: { t: 0, team: 'home', value: 2 },  // 手动补录进球
      adding: false
    };
  },
  computed: {
    game: function () { return this.S.game; },
    /** 全部出手（timeline 口径），用于「显示全部」模式 */
    allShots: function () {
<<<<<<< HEAD
      return ((this.game && this.game.timeline) || []).map(function (s, i) {
        return Object.assign({}, s, { _index: i });
      });
    },
    list: function () {
      var list = this.showAll ? this.allShots : this.rows;
      return this.onlyInferred ? list.filter(this.hasSuggestion) : list;
=======
      return (this.game && this.game.timeline) || [];
    },
    list: function () {
      if (this.showAll) return this.allShots;
      return this.rows;
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    },
    stats: function () {
      var total = this.allShots.length;
      var need = this.rows.length;
<<<<<<< HEAD
      var done = this.localLog.length;
      return {
        total: total, need: need, done: done, left: need,
=======
      var done = this.rows.filter(function (r) { return r._corrected; }).length;
      return {
        total: total, need: need, done: done, left: need - done,
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        rate: total ? (1 - need / Math.max(1, total)) : 1
      };
    },
    /** 是否可提交到后端（演示/无后端时只做本地改判） */
    canSubmit: function () { return this.S.backendOk && !this.S.demoMode && this.S.jobId && this.S.jobId !== 'demo'; }
  },
  watch: {
    // game 变化（新建任务/切换任务）时重建复核队列
    'S.game': {
      deep: false,
      handler: function () { this.build(); }
    }
  },
  created: function () { this.build(); },
  methods: {
    /** 手动补录一次进球：填时间/球队/分值，后端按 manual 出手整条重算。 */
    addManual: function () {
      var self = this;
      if (!this.canSubmit) {
        this.$message.warning('无后端/演示模式下不能补录');
        return;
      }
      var t = Number(this.manual.t);
      if (!(t >= 0)) {
        this.$message.warning('请输入进球时间（秒）');
        return;
      }
      this.adding = true;
      window.API.addManualShot(this.S.jobId, {
        t: t, team: this.manual.team, value: Number(this.manual.value)
      }).then(function (r) {
        self.adding = false;
        self.$message.success('已补录：' + (r.summary || ''));
        return window.APP_LOAD_JOB(self.S.jobId);
      }).then(function () {
        self.build();
      }).catch(function (e) {
        self.adding = false;
        self.$message.error('补录失败：' + (e && e.message ? e.message : e));
      });
    },

    /** 用 game.needs_review 初始化复核队列，并记录它在全场出手里的序号 index */
    build: function () {
      var g = this.game;
      var timeline = (g && g.timeline) || [];
      var need = (g && g.needs_review) || [];
      this.rows = need.map(function (s) {
        // index：优先用后端给的 _index/shot_index，否则按 t 匹配 timeline 序号
<<<<<<< HEAD
        var explicit = s.index !== undefined ? s.index : s.shot_index;
        var idx = Number.isInteger(explicit) && explicit >= 0 && explicit < timeline.length
          ? explicit : findIndex(timeline, s);
        return Object.assign({}, s, { _index: idx, _corrected: false, _origin: { made: s.made, value: s.value, result: s.result } });
      }).sort(function (a, b) { return a._index - b._index; });

      function findIndex(list, s) {
        // Legacy timelines round seconds to two decimals. Never choose an ambiguous match.
        var candidates = [];
        list.forEach(function (e, i) {
          if (Math.abs(Number(e.t) - Number(s.t)) <= 0.005001 && e.player_id === s.player_id)
            candidates.push(i);
        });
        var exact = candidates.filter(function (i) { return Number(list[i].value) === Number(s.value); });
        if (exact.length === 1) return exact[0];
        return candidates.length === 1 ? candidates[0] : -1;
      }
    },
    isUnknown: function (row) { return row.result === 'unknown' || row.made == null; },
    suggestionReviewTime: function (row) {
      var t = row.review_t != null ? row.review_t : row.crossing_t;
      return typeof t === 'number' && Number.isFinite(t) && t >= 0 ? t : null;
    },
    unknownReason: function (row) {
      if (!this.isUnknown(row)) return '';
      var codes = [row.evidence].concat(row.tags || []);
      if (codes.includes('cross_hoop_uncertain')) return '这一刻篮筐位置不够稳定，无法可靠判定。';
      if (codes.includes('cross_rim_contact')) return '球的轨迹靠近筐沿，可能擦筐；是否进球需要人工确认。';
      return '';
    },
    hasSuggestion: function (row) {
      return this.isUnknown(row) && !this.unknownReason(row) && typeof row.suggested_made === 'boolean';
    },
    resultLabel: function (row) { return this.isUnknown(row) ? '结果未知' : row.made ? '命中' : '未中'; },
    suggestionReason: function (row) {
      if (row.evidence === 'legacy_occlusion_descent') return '筐口短暂遮挡后，连续观测到球在筐下下降；尚未确认穿筐，请回看核实';
      return row.evidence === 'cross_extrapolated' ? '筐口轨迹缺失，依据趋势拟合推算' : '筐口轨迹缺失，依据前后位置插值推算';
    },
    teamName: function (s) { return window.D.teamName(this.game, s); },
    mmss: function (t) { return window.D.mmss(t); },
    /** 位置缩略图：court.js 的标准半场 + 该次出手标记 */
    thumb: function (row) { return this.isUnknown(row) ? '' : window.Court.shotThumb(row, {}); },
=======
        var idx = (s.index !== undefined) ? s.index
          : (s.shot_index !== undefined) ? s.shot_index : findIndex(timeline, s);
        return Object.assign({}, s, { _index: idx, _corrected: false, _origin: { made: s.made, value: s.value } });
      }).sort(function (a, b) { return a._index - b._index; });

      function findIndex(list, s) {
        for (var i = 0; i < list.length; i++) {
          var e = list[i];
          if (Math.abs(Number(e.t) - Number(s.t)) < 0.001 &&
            e.player_id === s.player_id && Number(e.value) === Number(s.value)) return i;
        }
        return -1;
      }
    },
    teamName: function (s) { return window.D.teamName(this.game, s); },
    mmss: function (t) { return window.D.mmss(t); },
    /** 位置缩略图：court.js 的标准半场 + 该次出手标记 */
    thumb: function (row) { return window.Court.shotThumb(row, {}); },
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    conf: function (row) { return Math.round((Number(row.confidence) || 0) * 100); },
    confType: function (row) {
      var c = Number(row.confidence) || 0;
      return c < 0.4 ? 'danger' : c < 0.6 ? 'warning' : 'info';
    },
    nameOf: function (row) {
      var id = row.player_id;
      for (var i = 0; i < (this.S.players || []).length; i++) {
        if (this.S.players[i].player_id === id) return this.S.players[i].name;
      }
      return id;
    },

    /** 改判入口：value 传 1|2|3，made 传 true/false */
    async correct(row, made, value) {
      var self = this;
<<<<<<< HEAD
      if (this.busyIdx !== -1) return;
      if (!Number.isInteger(row._index) || row._index < 0) {
        this.$message.error('这条记录无法定位到比赛事件，请刷新后重试');
=======
      if (row._index === undefined || row._index < 0) {
        this.$message.error('该出手在事件流中定位失败，无法提交改判');
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        return;
      }
      this.busyIdx = row._index;

      // 1) 有后端：POST /api/games/{id}/shots/{index}/correct
      //    后端会累积 corrections.json 并整条管线重算，返回 {ok, summary, score, corrections, needs_review_left}
      var persisted = false;
      var remote = null;
      if (this.canSubmit) {
        try {
          remote = await window.API.correctShot(this.S.jobId, row._index, { made: !!made, value: Number(value) });
          persisted = true;
          this.$message.success('已提交后端改判，统计口径已写回' +
            (remote && remote.score ? ('（后端比分 ' + remote.score.home + ':' + remote.score.away + '）') : ''));
        } catch (e) {
<<<<<<< HEAD
          this.$message.error('改判未保存：' + e.message + '。请重试。');
          this.busyIdx = -1;
          return;
=======
          this.$message.error('改判提交失败：' + e.message + '（已改为本地改判）');
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        }
      } else {
        this.$message.info('演示/离线模式：本次改判只在本地重算统计口径，不会写回后端');
      }

      // 2) 本地写回：更新 needs_review 与 timeline，并重算比分/球队统计/热区
<<<<<<< HEAD
      if (persisted) {
        try { await window.APP_LOAD_JOB(this.S.jobId); this.build(); }
        catch (e) { this.$message.warning('改判已保存，但刷新失败，请重新载入比赛'); }
      } else {
        this.applyLocal(row, made, value);
      }
=======
      this.applyLocal(row, made, value);
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
      row._corrected = true;
      row.made = !!made;
      row.value = Number(value);
      this.localLog.unshift({
        t: this.mmss(row.t), player: this.nameOf(row), made: made, value: value,
        before: row._origin, confidence: row.confidence, persisted: persisted,
        summary: remote && remote.summary ? remote.summary : ''
      });
      this.busyIdx = -1;
    },

    /** 本地重算：把改判结果回写到 timeline / needs_review，再交给全局的规范化+聚合 */
    applyLocal: function (row, made, value) {
      var g = this.S.game;
      if (!g) return;
      var tl = g.timeline || [];
      [g.timeline, g.needs_review].forEach(function (list) {
        (list || []).forEach(function (e) {
          if (e === row || (e.player_id === row.player_id &&
            Math.abs(Number(e.t) - Number(row.t)) < 0.001)) {
            e.made = !!made;
<<<<<<< HEAD
            e.result = made ? 'made' : 'missed';
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
            e.value = Number(value);
            e.points = made ? Number(value) : 0;
            e.source = 'manual';       // 与后端 OutcomeSource.MANUAL 对应
            e.confidence = 1;
          }
        });
      });
      // 重算比分与分节比分（保持与后端 pipeline 自检一致：分节之和 = 总分）
      var h = 0, a = 0;
      var qs = [];
      for (var p = 1; p <= (g.periods || 4); p++) qs.push({ period: p, home: 0, away: 0 });
      tl.forEach(function (e) {
<<<<<<< HEAD
        var pts = e.result !== 'unknown' && e.counts_for_score !== false && e.made ? Number(e.points === undefined ? e.value : e.points) : 0;
=======
        var pts = e.made ? Number(e.points === undefined ? e.value : e.points) : 0;
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        if (e.team === 'home') { h += pts; if (qs[Number(e.period || 1) - 1]) qs[Number(e.period || 1) - 1].home += pts; }
        else if (e.team === 'away') { a += pts; if (qs[Number(e.period || 1) - 1]) qs[Number(e.period || 1) - 1].away += pts; }
      });
      g.score = { home: h, away: a };
      g.quarter_scores = qs;

      // 球员统计重算（口径与后端 rules.compute_player_stats 一致）
      var byId = {};
      (this.S.players || []).forEach(function (p) { byId[p.player_id] = p; });
      function blank(p, team) {
        return {
          player_id: p, name: (byId[p] && byId[p].name) || p, team: team || (byId[p] && byId[p].team) || 'home',
          jersey: byId[p] ? byId[p].jersey : null,
          points: 0, fgm: 0, fga: 0, tpm: 0, tpa: 0, ftm: 0, fta: 0,
          reb: (byId[p] && byId[p].reb) || 0, ast: (byId[p] && byId[p].ast) || 0,
          stl: (byId[p] && byId[p].stl) || 0, blk: (byId[p] && byId[p].blk) || 0,
          tov: (byId[p] && byId[p].tov) || 0, pf: (byId[p] && byId[p].pf) || 0,
          shots: []
        };
      }
      var agg = {};
      tl.forEach(function (e) {
        var r = agg[e.player_id] || (agg[e.player_id] = blank(e.player_id, e.team));
        var v = Number(e.value || 2);
<<<<<<< HEAD
        if (e.result === 'unknown') return;
        r.points += e.counts_for_score !== false && e.made ? v : 0;
=======
        r.points += e.made ? v : 0;
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        if (v === 1) { r.fta++; if (e.made) r.ftm++; }
        else { r.fga++; if (e.made) r.fgm++; if (v === 3) { r.tpa++; if (e.made) r.tpm++; } }
        r.shots.push({ t: e.t, x: e.x, y: e.y, made: !!e.made, value: v });
      });
      var D = window.D;
      var rows = Object.keys(agg).map(function (k) {
        var r = agg[k];
        r.fg_pct = r.fga ? r.fgm / r.fga : 0;
        r.tp_pct = r.tpa ? r.tpm / r.tpa : 0;
        r.ft_pct = r.fta ? r.ftm / r.fta : 0;
        r.efg = r.fga ? (r.fgm + 0.5 * r.tpm) / r.fga : 0;
        r.ts = (r.fga + 0.44 * r.fta) ? r.points / (2 * (r.fga + 0.44 * r.fta)) : 0;
        return r;
      }).sort(function (x, y) { return y.points - x.points; });

      // 球队汇总 + 热区重算，最后统一走全局规范化
      ['home', 'away'].forEach(function (side) {
        var st = { points: 0, fgm: 0, fga: 0, tpm: 0, tpa: 0, ftm: 0, fta: 0 };
        tl.forEach(function (e) {
          if (e.team !== side) return;
          var v = Number(e.value || 2);
<<<<<<< HEAD
          if (e.result === 'unknown') return;
          st.points += e.counts_for_score !== false && e.made ? v : 0;
=======
          st.points += e.made ? v : 0;
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
          if (e.made) { if (v === 1) st.ftm++; else { st.fgm++; if (v === 3) st.tpm++; } }
          if (v === 1) st.fta++; else { st.fga++; if (v === 3) st.tpa++; }
        });
        st.fg_pct = st.fga ? st.fgm / st.fga : 0;
        st.tp_pct = st.tpa ? st.tpm / st.tpa : 0;
        if (g.teams && g.teams[side]) g.teams[side].stats = st;
      });
      g.needs_review = (g.needs_review || []).filter(function (e) { return Number(e.confidence) < 0.6; });

      // 重新走一遍全局规范化：出手点集 / 分区聚合 / 网格热力全部刷新
      var pts = D.shotsFromTimeline(g);
      var sc = { all: { points: pts, zones: [], grid: [], bin_size: 1 }, by_team: {} };
      this.S.applyGame(g, rows, sc, this.S.report, this.S.highlights);
      this.build();          // 复核队列跟着更新（已改判的不再是低置信度）
    },

    /** 一键批量：把所有剩余低置信度出手按「保持原判但标记为已确认」处理（演示用） */
<<<<<<< HEAD
    confirmAll: async function () {
      var self = this;
      var left = this.rows.filter(function (r) { return !r._corrected && !self.isUnknown(r); });
      if (!left.length) { this.$message.info('没有待处理的出手'); return; }
      for (var r of left) await self.correct(r, r.made, r.value);
=======
    confirmAll: function () {
      var self = this;
      var left = this.rows.filter(function (r) { return !r._corrected; });
      if (!left.length) { this.$message.info('没有待处理的出手'); return; }
      left.forEach(function (r, i) {
        setTimeout(function () { self.correct(r, r.made, r.value); }, i * 220);
      });
      this.$message.success('已按「维持原判」确认 ' + left.length + ' 次出手（置信度置为 100%）');
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    }
  },
  template: [
    '<div>',
    '  <div class="card" v-if="!game">',
    '    <el-empty description="暂无比赛数据" />',
    '  </div>',

    '  <template v-else>',
    '    <!-- ① 说明与统计 -->',
    '    <div class="card">',
<<<<<<< HEAD
    '      <h3 class="card-title">人工复核',
=======
    '      <h3 class="card-title">人工复核 <span class="sub">数据来源：GET /api/games/{{ S.jobId }}（game.needs_review）· 改判提交 POST /api/games/{{ S.jobId }}/shots/{index}/correct</span>',
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    '        <span class="grow"></span>',
    '        <el-tag :type="canSubmit ? \'success\' : \'warning\'" size="small" effect="dark">',
    '          {{ canSubmit ? \'改判将写回后端\' : \'演示模式：仅本地改判\' }}',
    '        </el-tag>',
    '      </h3>',
    '      <div class="grid grid-4">',
    '        <div class="kpi"><div class="k">自动识别出手</div><div class="v">{{ stats.total }}</div><div class="d">整场事件流</div></div>',
<<<<<<< HEAD
    '        <div class="kpi"><div class="k">待复核出手</div><div class="v" style="color:#f5a623">{{ stats.need }}</div><div class="d">含结果未知与系统建议</div></div>',
    '        <div class="kpi"><div class="k">已人工确认</div><div class="v" style="color:#22a06b">{{ stats.done }}</div><div class="d">本次会话内</div></div>',
    '        <div class="kpi"><div class="k">无需复核占比</div><div class="v">{{ (stats.rate*100).toFixed(1) }}%</div><div class="d">= 1 - 待复核 / 总出手</div></div>',
    '      </div>',
    '      <div class="hint" style="margin-top:12px">',
    '        系统建议尚未计入命中统计。请核对视频后逐球确认；批量维持原判会跳过结果未知的出手。',
    '        确认后更新比赛统计。球队或分值不确定时，还需核对归属与分值。',
    '      </div>',
    '      <div class="row" style="margin-top:10px;flex-wrap:wrap">',
    '        <el-switch v-model="showAll" active-text="显示全部出手（含高置信度）" />',
    '        <el-checkbox v-model="onlyInferred">只看待确认建议</el-checkbox>',
=======
    '        <div class="kpi"><div class="k">低置信度待复核</div><div class="v" style="color:#f5a623">{{ stats.need }}</div><div class="d">置信度 &lt; 0.6</div></div>',
    '        <div class="kpi"><div class="k">已人工确认</div><div class="v" style="color:#22a06b">{{ stats.done }}</div><div class="d">本次会话内</div></div>',
    '        <div class="kpi"><div class="k">自动判定准确率参考值</div><div class="v">{{ (stats.rate*100).toFixed(1) }}%</div><div class="d">= 1 - 待复核 / 总出手</div></div>',
    '      </div>',
    '      <div class="hint" style="margin-top:12px">',
    '        后端计分规则引擎用<b>加权投票 + 置信度</b>融合「球穿筐 / 记分牌 OCR / 网动」多路证据，',
    '        置信度低于阈值的出手进入本队列而不是硬判 —— 这是「可解释、可干预」的产品差异点。',
    '        改判后<b>会写回统计口径</b>：该次出手的分值与命中重新参与比分、球队/球员统计与热区聚合，',
    '        导出与战报随之更新（后端会把 outcome_source 标记为 <code>manual</code>）。',
    '      </div>',
    '      <div class="row" style="margin-top:10px">',
    '        <el-switch v-model="showAll" active-text="显示全部出手（含高置信度）" />',
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    '        <el-button size="small" @click="build">重新载入待复核队列</el-button>',
    '        <el-button size="small" type="primary" plain :disabled="!stats.left" @click="confirmAll">批量维持原判</el-button>',
    '      </div>',
    '    </div>',
    '    <!-- ①.5 手动补录 -->',
    '    <div class="card">',
    '      <h3 class="card-title">手动补录进球 <span class="sub">自动检测漏掉时使用：填时间/球队/分值，后端按 manual 出手重算</span></h3>',
    '      <div class="row" style="gap:12px;flex-wrap:wrap">',
    '        <el-input-number v-model="manual.t" :min="0" :max="36000" :step="0.1" controls-position="right" style="width:160px" />',
    '        <el-select v-model="manual.team" style="width:120px">',
    '          <el-option label="主队" value="home" /><el-option label="客队" value="away" />',
    '        </el-select>',
    '        <el-select v-model="manual.value" style="width:120px">',
    '          <el-option label="1 分" :value="1" /><el-option label="2 分" :value="2" /><el-option label="3 分" :value="3" />',
    '        </el-select>',
    '        <el-button type="primary" :loading="adding" :disabled="!canSubmit" @click="addManual">补录进球并重算</el-button>',
    '      </div>',
    '      <div class="hint">时间单位：秒；例如视频 3.8 秒处的进球填 3.8。</div>',
    '    </div>',

    '    <!-- ② 复核列表 -->',
    '    <div class="card">',
<<<<<<< HEAD
    '      <h3 class="card-title">出手列表 <span class="sub">当前筛选 {{ list.length }} 条</span></h3>',
=======
    '      <h3 class="card-title">待复核出手 <span class="sub">{{ showAll ? \'全部出手（\' + list.length + \' 条）\' : \'低置信度出手（\' + list.length + \' 条）\' }}</span></h3>',
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    '      <div class="rev-row rev-head">',
    '        <div>时刻 / 节次</div><div>球员与位置</div><div>位置缩略图</div><div>当前判定</div><div>改判操作</div>',
    '      </div>',
    '      <div v-for="(row,i) in list" :key="i" class="rev-row">',
    '        <div>',
    '          <div class="mono">{{ mmss(row.t) }}</div>',
    '          <div class="muted" style="font-size:12px">第{{ row.period }}节 · 序号 {{ row._index===undefined ? \'—\' : row._index }}</div>',
    '        </div>',
    '        <div>',
    '          <div><span class="tl-badge" :class="row.team===\'home\'?\'h\':\'a\'">{{ row.team===\'home\' ? teamName(\'home\') : teamName(\'away\') }}</span>',
    '            <b style="margin-left:6px">{{ nameOf(row) }}</b></div>',
    '          <div class="muted" style="font-size:12px;margin-top:4px">',
    '            {{ row.zone || \'—\' }} · 距篮 {{ row.distance !== undefined ? row.distance : \'—\' }}m · 坐标 ({{ Number(row.x).toFixed(2) }}, {{ Number(row.y).toFixed(2) }})',
    '          </div>',
    '          <div class="muted" style="font-size:12px">证据来源：{{ row.outcome_source || row.source || \'—\' }}</div>',
    '        </div>',
    '        <div v-html="thumb(row)"></div>',
    '        <div>',
<<<<<<< HEAD
    '          <el-tag size="small" :type="isUnknown(row) ? \'info\' : row.made ? \'success\' : \'danger\'" effect="dark">',
    '            {{ resultLabel(row) }} {{ row.value }} 分',
    '          </el-tag>',
    '          <div v-if="unknownReason(row)" style="margin-top:8px">',
    '            <div><strong>判定依据：</strong>{{ unknownReason(row) }}</div>',
    '            <div v-if="row.crossing_t != null">建议核对 {{ mmss(row.crossing_t) }} 附近画面</div>',
    '          </div>',
    '          <div v-if="hasSuggestion(row)" style="margin-top:8px">',
    '            <strong>系统建议：{{ row.suggested_made ? \'进球\' : \'未中\' }}</strong>',
    '            <div>{{ suggestionReason(row) }}。尚未计入命中统计。</div>',
    '            <div v-if="suggestionReviewTime(row) != null">建议核对 {{ mmss(suggestionReviewTime(row)) }} 附近画面</div>',
    '          </div>',
    '          <div v-else class="muted" style="font-size:12px;margin-top:6px">置信度 {{ conf(row) }}%</div>',
    '          <div v-if="!hasSuggestion(row)" class="conf-bar"><i :style="{width: conf(row) + \'%\', background: conf(row)<40 ? \'#e5484d\' : \'#f5a623\'}"></i></div>',
    '          <el-tag v-if="row._corrected" size="small" type="success" effect="plain" style="margin-top:6px">已改判</el-tag>',
    '        </div>',
    '        <div>',
    '          <el-button v-if="hasSuggestion(row)" size="small" :disabled="busyIdx !== -1" @click="correct(row, row.suggested_made, row.value)">确认此建议</el-button>',
    '          <div class="row" style="gap:6px;flex-wrap:wrap;margin-top:6px">',
=======
    '          <el-tag size="small" :type="row.made ? \'success\' : \'danger\'" effect="dark">',
    '            {{ row.made ? \'命中\' : \'未中\' }} {{ row.value }} 分',
    '          </el-tag>',
    '          <div class="muted" style="font-size:12px;margin-top:6px">置信度 {{ conf(row) }}%</div>',
    '          <div class="conf-bar"><i :style="{width: conf(row) + \'%\', background: conf(row)<40 ? \'#e5484d\' : \'#f5a623\'}"></i></div>',
    '          <el-tag v-if="row._corrected" size="small" type="success" effect="plain" style="margin-top:6px">已改判</el-tag>',
    '        </div>',
    '        <div>',
    '          <div class="row" style="gap:6px">',
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    '            <el-button size="small" type="success" :loading="busyIdx===row._index" @click="correct(row, true, row.value)">判为命中</el-button>',
    '            <el-button size="small" type="danger" plain :loading="busyIdx===row._index" @click="correct(row, false, row.value)">判为未中</el-button>',
    '          </div>',
    '          <div class="row" style="gap:6px;margin-top:6px">',
    '            <span class="muted" style="font-size:12px">分值改判：</span>',
    '            <el-button size="small" text @click="correct(row, true, 1)">1分</el-button>',
    '            <el-button size="small" text @click="correct(row, true, 2)">2分</el-button>',
    '            <el-button size="small" text @click="correct(row, true, 3)">3分</el-button>',
    '          </div>',
    '        </div>',
    '      </div>',
<<<<<<< HEAD
    '      <el-empty v-if="!list.length" :image-size="70" description="当前筛选下没有待复核出手" />',
=======
    '      <el-empty v-if="!list.length" :image-size="70" description="没有需要复核的出手：所有出手置信度都不低于阈值，或已全部处理" />',
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    '    </div>',

    '    <!-- ③ 改判记录：答辩时用来说明「写回口径」 -->',
    '    <div class="card" v-if="localLog.length">',
    '      <h3 class="card-title">本次会话改判记录 <span class="sub">展示改判前后取值与是否写回后端</span></h3>',
    '      <el-table :data="localLog" size="small" border>',
    '        <el-table-column prop="t" label="时刻" width="90" />',
    '        <el-table-column prop="player" label="球员" width="120" />',
    '        <el-table-column label="改判前" min-width="140">',
<<<<<<< HEAD
    '          <template #default="s">{{ resultLabel(s.row.before) }} {{ s.row.before.value }} 分（置信度 {{ (s.row.confidence*100).toFixed(0) }}%）</template>',
=======
    '          <template #default="s">{{ s.row.before.made ? \'命中\' : \'未中\' }} {{ s.row.before.value }} 分（置信度 {{ (s.row.confidence*100).toFixed(0) }}%）</template>',
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    '        </el-table-column>',
    '        <el-table-column label="改判后" min-width="120">',
    '          <template #default="s">{{ s.row.made ? \'命中\' : \'未中\' }} {{ s.row.value }} 分</template>',
    '        </el-table-column>',
    '        <el-table-column label="是否写回后端" width="130" align="center">',
    '          <template #default="s"><el-tag size="small" :type="s.row.persisted ? \'success\' : \'info\'">{{ s.row.persisted ? \'已写回\' : \'仅本地\' }}</el-tag></template>',
    '        </el-table-column>',
    '        <el-table-column label="后端返回摘要" min-width="220" show-overflow-tooltip>',
    '          <template #default="s"><span class="muted">{{ s.row.summary || \'（本地重算）\' }}</span></template>',
    '        </el-table-column>',
    '      </el-table>',
    '    </div>',
    '  </template>',
    '</div>'
  ].join('\n')
};
