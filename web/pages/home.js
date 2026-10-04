/* ==========================================================================
   pages/home.js —— 首页（主界面）
   --------------------------------------------------------------------------
   为什么要有这一页（用户反馈 2026-09-27）：
     "应该搞一个主界面，不然每次一打开网页就是空的一大片，从这个主界面跳转到其他模块"
   以前默认落在「比赛总览」：没有任务、也没有离线样例时，打开就是一大片空白，
   新用户不知道该点哪里。现在打开先到首页，一眼能看到三件事：
     ① 这个软件是干什么的、从哪开始（一个主按钮：上传与分析）
     ② 现在处于什么状态（后端连没连上 / 当前比赛是谁、比分多少）
     ③ 有哪些模块、分别能看什么（卡片直接跳转）

   数据来源：
     window.STORE（全站共享状态：game / backendOk / demoMode / jobId）
     window.MENUS（菜单即路由表，见 app.js；顶部卡片就是按它渲染的）
     GET /api/jobs（最近任务；后端不在时静默失败，不影响页面）
   ========================================================================== */
window.PAGES = window.PAGES || {};

/** 每个模块一句"能看什么"，用来在首页卡片上解释（菜单表里只有标题） */
var HOME_BLURBS = {
  upload: '选一段比赛视频 → 自动认出出手与进球，产出比分、命中率、热区与战报',
  overview: '比分、节次得分、命中率、关键事件时间轴',
  review: '把结果未知或系统建议的球逐条确认，改判会写回比分与统计',
  stats: '每个球员的出手/命中、命中率、得分与区域分布',
  shotchart: '投篮点分布与热区（命中的球才有位置，未知的不进图）',
  tactics: '阵型与回合级别的战术看图（需要标定球场）',
  highlights: '自动切片生成的高光片段（逐段可播放/下载）',
  report: '导出 CSV / Markdown 战报 / 高光清单，一键交给别人复查',
  train: '逐球标注"进没进"，攒人工样本用来改进判据'
};

window.PAGES['home'] = {
  name: 'page-home',
  data: function () {
    return {
      S: window.STORE,
      api: window.API,
      recent: [],          // 最近任务（GET /api/jobs）
      recentErr: ''
    };
  },
  computed: {
    backendOk: function () { return !!(this.S && this.S.backendOk); },
    /** 当前装载的比赛（各页面共享同一个 STORE.game） */
    game: function () { return (this.S && this.S.game) || null; },
    hasGame: function () { return !!(this.game && (this.game.timeline || []).length); },
    scoreLine: function () {
      var g = this.game;
      if (!this.hasGame || !g.score) return '';
      var h = window.D.teamName(g, 'home');
      var a = window.D.teamName(g, 'away');
      return h + ' ' + (g.score.home || 0) + ' : ' + (g.score.away || 0) + ' ' + a;
    },
    shotCount: function () {
      return this.hasGame ? (this.game.timeline || []).length : 0;
    },
    reviewCount: function () {
      return this.hasGame ? (this.game.needs_review || []).length : 0;
    },
    /** 首页模块卡片：直接由菜单表渲染，避免两处各写一份 */
    cards: function () {
      var menus = window.MENUS || [];
      return menus.filter(function (m) { return m.key !== 'home'; })
        .map(function (m) {
          return { key: m.key, icon: m.icon, title: m.title,
                   desc: HOME_BLURBS[m.key] || '', href: '#/' + m.key };
        });
    }
  },
  methods: {
    go: function (key) { location.hash = '#/' + key; },
    /** 最近任务：点「查看」直接装载产物并跳结果页 */
    loadRecent: function () {
      var self = this;
      if (!this.backendOk || !window.API || !window.API.listJobs) return;
      window.API.listJobs().then(function (d) {
        var list = Array.isArray(d) ? d : (d && d.jobs) || [];
        self.recent = list.slice(0, 5);
        self.recentErr = '';
      }).catch(function () { self.recentErr = '读不到任务列表'; });
    },
    openJob: function (row) {
      var self = this;
      if (!row || row.status !== 'done') return;
      window.APP_LOAD_JOB(row.job_id).then(function () {
        self.S.demoMode = false;
        location.hash = '#/overview';
      }).catch(function () { /* 首页不打断用户，失败就留在原地 */ });
    },
    mmss: function (t) { return window.D.mmss(t); }
  },
  created: function () { this.loadRecent(); },
  template: [
    '<div class="page-home">',

    // ---- ① hero：这是什么 + 从哪开始 ----
    '  <div class="card">',
    '    <h3 class="card-title">把比赛视频变成可复查的数据',
    '      <span class="sub">上传一段比赛/训练视频，自动认出出手与进球，产出比分、命中率、热区、高光与文字战报</span>',
    '    </h3>',
    '    <div class="hint" style="margin:6px 0 14px">',
    '      第一步只需要一段视频；球场标定是<strong>可选</strong>的 —— 不标也能出比分、命中率、球员统计与战报，',
    '      只有热图、战术图和 2 分/3 分区分需要标定。',
    '    </div>',
    '    <div class="row" style="gap:10px;flex-wrap:wrap">',
    '      <el-button type="primary" @click="go(\'upload\')">开始分析一段视频</el-button>',
    '      <el-button v-if="hasGame" plain @click="go(\'overview\')">看当前比赛的比分</el-button>',
    '      <el-button v-if="hasGame && reviewCount" plain type="warning" @click="go(\'review\')">',
    '        去复核待确认的 {{ reviewCount }} 球</el-button>',
    '      <el-tag v-if="!backendOk" type="warning" effect="plain">后端未连接：先双击「启动后端.bat」，再点右上角「重新探测」</el-tag>',
    '    </div>',
    '  </div>',

    // ---- ② 当前状态：比赛快照 / 来源 ----
    '  <div class="card">',
    '    <h3 class="card-title">当前比赛',
    '      <span class="sub">{{ backendOk ? \'数据来自后端\' : \'演示数据模式（未连后端）\' }}</span>',
    '    </h3>',
    '    <div v-if="hasGame" class="grid grid-4" style="margin-top:6px">',
    '      <div class="kpi"><div class="k">比分</div><div class="v">{{ scoreLine }}</div></div>',
    '      <div class="kpi"><div class="k">出手</div><div class="v">{{ shotCount }}</div></div>',
    '      <div class="kpi"><div class="k">待复核</div><div class="v">{{ reviewCount }}</div></div>',
    '      <div class="kpi"><div class="k">任务</div><div class="v mono">{{ (S.jobId || \'—\').slice(0, 8) }}</div></div>',
    '    </div>',
    '    <div v-else class="hint">还没有分析结果。点上面「开始分析一段视频」新建一个任务；',
    '      跑完后这里会显示比分与待复核球数。</div>',
    '  </div>',

    // ---- ③ 模块入口卡片 ----
    '  <div class="card">',
    '    <h3 class="card-title">各模块 <span class="sub">点卡片直接进入</span></h3>',
    '    <div class="grid grid-3" style="margin-top:6px">',
    '      <a v-for="c in cards" :key="c.key" :href="c.href" class="home-card">',
    '        <div class="home-card-head"><span class="home-card-ico">{{ c.icon }}</span>',
    '          <b>{{ c.title }}</b></div>',
    '        <div class="home-card-desc">{{ c.desc }}</div>',
    '      </a>',
    '    </div>',
    '  </div>',

    // ---- ④ 最近任务 ----
    '  <div class="card" v-if="backendOk">',
    '    <h3 class="card-title">最近任务 <span class="sub">点「查看」直接回看产物</span></h3>',
    '    <el-table :data="recent" size="small" border empty-text="暂无任务">',
    '      <el-table-column prop="job_id" label="任务 ID" min-width="200" show-overflow-tooltip />',
    '      <el-table-column prop="status" label="状态" width="100" />',
    '      <el-table-column label="操作" width="90">',
    '        <template #default="s">',
    '          <el-button size="small" text :disabled="s.row.status !== \'done\'" @click="openJob(s.row)">查看</el-button>',
    '        </template>',
    '      </el-table-column>',
    '    </el-table>',
    '    <div class="hint" v-if="recentErr">{{ recentErr }}（不影响其它功能）</div>',
    '  </div>',

    '</div>'
  ].join('\n')
};
