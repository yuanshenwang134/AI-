/* ==========================================================================
   pages/report.js —— 页面 6：导出 / 战报报告
   --------------------------------------------------------------------------
   数据来源接口：
     GET /api/games/{job_id}/report                 { markdown, json }
     GET /api/games/{job_id}/export?fmt=...         文件下载
         fmt = csv_stats（球员统计 CSV）
               csv_shots（出手明细 CSV）
               json      （结构化报告 report.json）
               report_md （Markdown 战报）
   展示内容：
     * 战报 Markdown 轻量渲染（标题/表格/列表/粗体，见 data.js md2html）
     * 关键指标：数据可信度、关键球、高效区域（来自 report.json）
     * 4 个下载按钮：有后端直接走 /export；无后端用本地数据现场生成（降级）
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['report'] = {
  name: 'page-report',
  data: function () {
    return {
      S: window.STORE,
      downloading: '',
      polishing: false,
      showRaw: false
    };
  },
  computed: {
    game: function () { return this.S.game; },
    report: function () { return this.S.report || {}; },
    /** 后端 report 接口返回 {markdown, json}；演示模式读 web/demo/report.md */
    markdown: function () {
      var r = this.report || {};
      if (r.markdown) return r.markdown;
      if (typeof r === 'string') return r;
      return '';
    },
    html: function () { return window.D.md2html(this.markdown); },
    reportJson: function () {
      var r = this.report || {};
      return r.json || (r.confidence ? r : null);
    },
    confidence: function () {
      var j = this.reportJson;
      if (j && j.confidence) return j.confidence;
      var total = (this.game && this.game.timeline || []).length;
      var need = (this.game && this.game.needs_review || []).length;
      return {
        total: total, needs_review: need,
        rate: total ? Math.round((1 - need / Math.max(1, total)) * 1000) / 1000 : 0
      };
    },
    keyShots: function () {
      var j = this.reportJson;
      return (j && j.key_shots) || [];
    },
    // 位置未知（占位坐标）的出手条数：热区/距离类结论必须排除它们，
    // 否则会把占位点聚成"全在禁区"的假热区（后端 export/report 已按同一口径挡掉）。
    noLocation: function () {
      var tl = (this.game && this.game.timeline) || [];
      return tl.filter(function (e) {
        return (e.tags || []).indexOf('location_unknown') >= 0;
      }).length;
    },
    hotZones: function () {
      var j = this.reportJson;
      if (j && j.hot_zones && j.hot_zones.length) return j.hot_zones;
      // 兜底：用前端分区聚合取前 4 个高效区
      var rows = window.D.aggregateZones((this.S.shotchart.all || {}).points || [])
        .filter(function (z) { return z.att >= 3; })
        .sort(function (a, b) { return b.pps - a.pps; });
      return rows.slice(0, 4);
    },
    leaders: function () {
      var j = this.reportJson;
      if (j && j.leaders) return j.leaders;
      var self = this;
      var out = { home: { scoring: [], rebounds: [], assists: [] }, away: { scoring: [], rebounds: [], assists: [] } };
      ['home', 'away'].forEach(function (t) {
        var list = (self.S.players || []).filter(function (p) { return p.team === t; });
        function top(k) {
          return list.slice().sort(function (a, b) { return b[k] - a[k]; }).slice(0, 3)
            .filter(function (p) { return p[k] > 0; })
            .map(function (p) { return { player: p.name, value: p[k] }; });
        }
        out[t] = { scoring: top('points'), rebounds: top('reb'), assists: top('ast') };
      });
      return out;
    },
    /** 是否走后端接口（演示数据模式下一律本地降级） */
    canUseBackend: function () {
      return !!(this.S.backendOk && !this.S.demoMode && this.S.jobId && this.S.jobId !== 'demo');
    },
    exports: function () {
      var id = this.S.jobId;
      function url(fmt) { return window.API.exportUrl(id, fmt); }
      return [
        { fmt: 'csv_stats', label: '球员统计 CSV', icon: '📋', mode: 'fetch', url: url('csv_stats'), file: 'stats_' + id + '.csv', desc: '每人得分/命中率/三分/罚球/篮板助攻，Excel 可直接打开（UTF-8 BOM）' },
        { fmt: 'csv_shots', label: '出手明细 CSV', icon: '🎯', mode: 'fetch', url: url('csv_shots'), file: 'shots_' + id + '.csv', desc: '每次出手的时刻/球员/坐标/分值/命中/分区，用于二次建模与复核' },
        { fmt: 'json', label: '结构化 JSON', icon: '🧾', mode: 'fetch', url: url('json'), file: 'game_' + id + '.json', desc: '后端 /export?fmt=json 返回的是 game.json（完整比赛对象）；无后端时导出 report.json 结构化战报' },
        { fmt: 'report_md', label: 'Markdown 战报', icon: '📄', mode: 'text', url: url('report_md'), file: 'report_' + id + '.md', desc: '可直接贴到公众号/答辩 PPT 的文字战报（模板生成，离线可复现）' },
        { fmt: 'events', label: '完整事件流 JSONL', icon: '🧵', mode: 'fetch', url: url('events'), file: 'events_' + id + '.jsonl', desc: '后端 /export?fmt=events：出手 + 派生事件的逐行 JSONL，便于二次分析' },
        // 下面两个是后端产物目录里的原始文件，演示模式下直接从 web/demo/ 取，方便离线核查
        // 战术层导出（后端 /export?fmt=csv_passes / csv_spacing / tactics）
        { fmt: 'csv_passes', label: '传球网络 CSV', icon: '🔗', mode: 'fetch', url: url('csv_passes'), file: 'passes_' + id + '.csv', desc: '逐次传球/失误明细 + 有向传球网络汇总 + 每人传球/接球数' },
        { fmt: 'csv_spacing', label: '空间指标 CSV', icon: '📐', mode: 'fetch', url: url('csv_spacing'), file: 'spacing_' + id + '.csv', desc: '每秒钟两队的位置指标：凸包面积 / 平均间距 / 宽度 / 纵深 / 重心到篮筐' },
        { fmt: 'tactics', label: '战术分析 JSON', icon: '🧭', mode: 'fetch', url: url('tactics'), file: 'tactics_' + id + '.json', desc: '完整战术结论：回合 / 传球网络 / 阵型片段 / 空间曲线 / 战术亮点' },
        // 高光片段是「产物文件」，容易被清理/换机器时丢掉；这里给一个按时间码重切的入口
        { fmt: 'highlights_regen', label: '重切高光片段', icon: '🎬', mode: 'post', file: '', desc: '用已保存的出手时刻 + 源视频重新切片（不重新推理）；片段文件丢失时点这个' },
    { fmt: 'raw_stats_csv', label: '产物原始 stats.csv', icon: '📎', mode: 'rawfile', file: 'stats.csv', desc: '后端产物目录里的球员统计表（含球队汇总块），演示模式读 web/demo/stats.csv' },
        { fmt: 'raw_shots_csv', label: '产物原始 shots.csv', icon: '📎', mode: 'rawfile', file: 'shots.csv', desc: '后端产物目录里的出手明细（含 outcome_source / confidence / tags 列）' }
      ];
    }
  },
  methods: {
    teamName: function (s) { return window.D.teamName(this.game, s); },
    pct: function (v) { return window.D.pctSmart(v); },
    mmss: function (t) { return window.D.mmss(t); },
    /**
     * 下载：后端可用时直接跳 /export（浏览器原生下载）；
     * 否则用本地数据现场生成 —— 这是「没有后端也能导出」的降级路径。
     */
    download: function (item) {
      var self = this;
      // 重切高光：不是下载，而是让后端按已保存的时间码重新切片
      if (item.fmt === 'highlights_regen') {
        if (!this.canUseBackend) {
          this.$message.warning('重切高光需要后端在线（离线模式只能导出时间码）');
          return;
        }
        this.downloading = item.fmt;
        window.API.regenerateHighlights(this.S.jobId).then(function (r) {
          self.downloading = '';
          self.$message.success(r && r.note ? r.note : '高光片段已重切');
          if (self.$root && self.$root.reloadGame) self.$root.reloadGame();
        }).catch(function (e) {
          self.downloading = '';
          self.$message.error('重切失败：' + (e && e.message ? e.message : e));
        });
        return;
      }
      if (this.canUseBackend && item.mode !== 'rawfile') {
        var a = document.createElement('a');
        a.href = item.url; a.download = item.file;
        document.body.appendChild(a); a.click();
        setTimeout(function () { document.body.removeChild(a); }, 300);
        this.$message.success('已开始下载（来自后端 /export）');
        return;
      }
      this.downloading = item.fmt;
      try {
        if (item.mode === 'rawfile') {
          // 直接取后端产物目录里的原始文件（演示模式读 web/demo/ 下的同名文件）
          this.downloadRaw(item);
          this.downloading = '';
          return;
        }
        if (item.fmt === 'csv_stats') {
          window.D.download(item.file, '\ufeff' + window.D.playersCSV(this.S.players, this.game), 'text/csv');
        } else if (item.fmt === 'csv_shots') {
          window.D.download(item.file, '\ufeff' + window.D.shotsCSV(this.S.shotchart.all.points), 'text/csv');
        } else if (item.fmt === 'csv_passes') {
          window.D.download(item.file, '\ufeff' + window.D.tacticsPassesCSV(this.S.tactics), 'text/csv');
        } else if (item.fmt === 'csv_spacing') {
          window.D.download(item.file, '\ufeff' + window.D.tacticsSpacingCSV(this.S.tactics), 'text/csv');
        } else if (item.fmt === 'tactics') {
          var tac = this.S.tactics;
          if (!tac) {
            this.$message.warning('战术数据还没加载：请先打开一次「战术分析」页');
            this.downloading = '';
            return;
          }
          window.D.download(item.file, JSON.stringify(tac, null, 2), 'application/json');
        } else if (item.fmt === 'json') {
          var payload = this.reportJson || {
            game: this.game, players: this.S.players, shotchart: {
              all: { zones: this.S.shotchart.all.zones, bin_size: this.S.shotchart.all.bin_size },
              by_team: this.S.shotchart.zonesByTeam
            }
          };
          window.D.download(item.file, JSON.stringify(payload, null, 2), 'application/json');
        } else if (item.fmt === 'events') {
          // 本地兜底：把 timeline 逐行写成 JSONL（字段与后端 events.jsonl 的 shot 记录对齐）
          var lines = (this.game.timeline || []).map(function (e) {
            return JSON.stringify({
              t: e.t, period: e.period, team: e.team, player_id: e.player_id,
              value: e.value, made: e.made, points: e.points,
              x: e.x, y: e.y, zone: e.zone,
              outcome_source: e.source, confidence: e.confidence
            });
          });
          window.D.download(item.file, lines.join('\n') + '\n', 'application/x-ndjson');
        } else {
          window.D.download(item.file, this.markdown || '（无战报文本）', 'text/markdown');
        }
        this.$message.success('已在本地生成并下载（演示数据模式降级导出）');
      } catch (e) {
        this.$message.error('导出失败：' + e.message);
      }
      this.downloading = '';
    },
    copyMd: function () {
      var self = this;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(this.markdown).then(function () {
          self.$message.success('战报 Markdown 已复制到剪贴板');
        }).catch(function () { self.$message.warning('复制失败，请手动选择文本'); });
      } else {
        this.$message.warning('当前浏览器不支持剪贴板接口');
      }
    },

    /**
     * LLM 润色战报（可选加分项）：POST /api/games/{id}/report/llm
     * 后端没有 DEEPSEEK_API_KEY 时会返回 400 并提示用模板战报，这里原样展示错误。
     */
    polish: function () {
      var self = this;
      if (!this.canUseBackend) { this.$message.warning('需要连接后端才能调用 LLM 润色'); return; }
      this.polishing = true;
      window.API.reportLlm(this.S.jobId).then(function (r) {
        self.polishing = false;
        if (r && r.markdown) {
          self.S.report = { markdown: r.markdown, json: (self.report || {}).json };
          self.$message.success('已用 LLM 生成润色版战报（覆盖显示，原模板战报仍在后端 report.md）');
        }
      }).catch(function (e) {
        self.polishing = false;
        self.$message.warning('LLM 润色不可用：' + e.message + '（模板战报已可直接使用）');
      });
    },

    /**
     * 下载后端产物目录里的原始文件（stats.csv / shots.csv）。
     * 后端可用时走 /api/media/{job_id}/{file}；演示模式读 web/demo/{file}。
     */
    downloadRaw: function (item) {
      var self = this;
      var url = (this.canUseBackend && this.S.jobId)
        ? window.API.mediaUrl(this.S.jobId, item.file)
        : window.API.demoMediaUrl(item.file);
      fetch(url).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      }).then(function (b) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(b);
        a.download = item.file;
        document.body.appendChild(a); a.click();
        setTimeout(function () { document.body.removeChild(a); URL.revokeObjectURL(a.href); }, 400);
        self.$message.success('已下载 ' + item.file);
      }).catch(function (e) {
        self.$message.warning('该文件不可用（' + e.message + '）：后端产物目录里可能没有 ' + item.file);
      });
    }
  },
  template: [
    '<div>',
    '  <div class="card" v-if="!markdown && !game">',
    '    <el-empty description="暂无战报，请先运行分析任务或载入演示数据" />',
    '  </div>',

    '  <template v-else>',
    '    <!-- ① 导出按钮区 -->',
    '    <div class="card">',
    '      <h3 class="card-title">导出与报告 <span class="sub">数据来源：GET /api/games/{{ S.jobId }}/report 与 /export?fmt=…</span>',
    '        <span class="grow"></span>',
    '        <el-tag :type="S.backendOk && !S.demoMode ? \'success\' : \'warning\'" size="small" effect="plain">',
    '          {{ S.backendOk && !S.demoMode ? \'后端导出（/export）\' : \'本地降级导出\' }}',
    '        </el-tag>',
    '      </h3>',
    '      <div class="grid grid-4">',
    '        <div class="src-card" v-for="e in exports" :key="e.fmt">',
    '          <div class="t">{{ e.icon }} {{ e.label }}</div>',
    '          <div class="d">{{ e.desc }}</div>',
    '          <el-button size="small" type="primary" plain style="margin-top:8px"',
    '                     :loading="downloading===e.fmt" @click="download(e)">下载</el-button>',
    '        </div>',
    '      </div>',
    '      <div class="hint" style="margin-top:10px">',
    '        后端可用时点击直接命中 <code>GET /api/games/{id}/export?fmt=csv_stats|csv_shots|json|report_md</code>；',
    '        演示数据模式下前端用同一套统计口径在浏览器里现场生成等价文件（CSV 带 UTF-8 BOM，Excel 双击不乱码）。',
    '        最后两个卡片对应后端产物目录里的原始文件（stats.csv / shots.csv），经 <code>/api/media/{id}/…</code> 取回。',
    '      </div>',
    '    </div>',

    '    <!-- ② 可信度 + 关键球 + 高效区 -->',
    '    <div class="grid grid-3">',
    '      <div class="card">',
    '        <h3 class="card-title">数据可信度 <span class="sub">report.json · confidence</span></h3>',
    '        <div class="row" style="gap:18px">',
    '          <el-statistic title="自动识别出手" :value="confidence.total || 0" />',
    '          <el-statistic title="低置信度待复核" :value="confidence.needs_review || 0" />',
    '        </div>',
    '        <div style="margin-top:12px">',
    '          <div class="hint">自动判定准确率参考值</div>',
    '          <el-progress :percentage="Math.round((confidence.rate||0)*100)" :stroke-width="12"',
    '                       :color="(confidence.rate||0) > 0.85 ? \'#22a06b\' : \'#f5a623\'" />',
    '        </div>',
    '        <div class="hint" style="margin-top:10px">',
    '          低置信度出手在「人工复核」页一键改判，修正结果会写回统计口径（分值/命中）。',
    '        </div>',
    '      </div>',

    '      <div class="card">',
    '        <h3 class="card-title">关键球 <span class="sub">report.json · key_shots（末节得分球与三分）</span></h3>',
    '        <el-table :data="keyShots" size="small" border empty-text="暂无关键球">',
    '          <el-table-column label="时刻" width="76"><template #default="s">{{ mmss(s.row.t) }}</template></el-table-column>',
    '          <el-table-column prop="period" label="节" width="52" align="center" />',
    '          <el-table-column label="球队" width="90"><template #default="s">{{ teamName(s.row.team) }}</template></el-table-column>',
    '          <el-table-column prop="player_id" label="球员" min-width="80" />',
    '          <el-table-column prop="zone" label="区域" min-width="96" />',
    '          <el-table-column prop="value" label="分值" width="62" align="center" />',
    '          <el-table-column prop="distance" label="距篮(m)" width="86" align="center" />',
    '        </el-table>',
    '      </div>',

    '      <div class="card">',
    '        <h3 class="card-title">高效出手区域 <span class="sub">report.json · hot_zones（按每次出手得分排序）</span></h3>',
    '        <p v-if="!hotZones.length && noLocation" class="hint">本片段有 <b>{{ noLocation }}</b> 次出手的位置是占位估计（缺少这个机位的球场标定），所以不给热区 —— 与其画一张全是「禁区」的假热区，不如空着。比分与命中判定不依赖坐标。</p>',
    '        <el-table :data="hotZones" size="small" border empty-text="暂无数据">',
    '          <el-table-column prop="zone" label="区域" min-width="104" />',
    '          <el-table-column prop="att" label="出手" width="66" align="center" />',
    '          <el-table-column prop="made" label="命中" width="66" align="center" />',
    '          <el-table-column label="命中率" width="86" align="center"><template #default="s">{{ pct(s.row.pct) }}</template></el-table-column>',
    '          <el-table-column prop="pps" label="PPS" width="70" align="center" />',
    '        </el-table>',
    '      </div>',
    '    </div>',

    '    <!-- ③ 得分榜（来自 report.json.leaders） -->',
    '    <div class="card">',
    '      <h3 class="card-title">两队领先球员 <span class="sub">report.json · leaders</span></h3>',
    '      <div class="grid grid-2">',
    '        <div v-for="side in [\'home\',\'away\']" :key="side">',
    '          <h4 style="margin:4px 0 8px">{{ teamName(side) }}</h4>',
    '          <el-descriptions :column="1" border size="small">',
    '            <el-descriptions-item label="得分">',
    '              <span v-for="(x,i) in leaders[side].scoring" :key="i">{{ x.player }} {{ x.value }}分　</span>',
    '            </el-descriptions-item>',
    '            <el-descriptions-item label="篮板">',
    '              <span v-for="(x,i) in leaders[side].rebounds" :key="i">{{ x.player }} {{ x.value }}　</span>',
    '            </el-descriptions-item>',
    '            <el-descriptions-item label="助攻">',
    '              <span v-for="(x,i) in leaders[side].assists" :key="i">{{ x.player }} {{ x.value }}　</span>',
    '            </el-descriptions-item>',
    '          </el-descriptions>',
    '        </div>',
    '      </div>',
    '    </div>',

    '    <!-- ④ 战报正文 -->',
    '    <div class="card">',
    '      <h3 class="card-title">比赛分析报告 <span class="sub">report.markdown · 轻量 Markdown 渲染（标题/表格/列表/粗体）</span>',
    '        <span class="grow"></span>',
    '        <el-button size="small" text @click="copyMd">复制 Markdown</el-button>',
    '        <el-button size="small" text :loading="polishing" @click="polish">LLM 润色（可选）</el-button>',
    '        <el-button size="small" text @click="showRaw=!showRaw">{{ showRaw ? \'看渲染效果\' : \'看原始文本\' }}</el-button>',
    '      </h3>',
    '      <pre v-if="showRaw" class="stage-log" style="max-height:none;white-space:pre-wrap">{{ markdown }}</pre>',
    '      <div v-else class="md" v-html="html"></div>',
    '    </div>',
    '  </template>',
    '</div>'
  ].join('\n')
};
