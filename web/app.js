/* ==========================================================================
   app.js —— SPA 外壳：全局状态 + hash 路由 + Element Plus 中文初始化
   --------------------------------------------------------------------------
   全局状态（window.STATE，各页面通过 app.js 提供的全局 getter 读取）：
     backendOk  后端是否可用（决定「演示数据模式」标签）
     jobId      当前展示的比赛 job_id
     game       game.json          ← GET /api/games/{id}
     players    players.json       ← GET /api/games/{id}/players
     shotchart  shotchart.json     ← GET /api/games/{id}/shotchart
     report     {markdown, json}   ← GET /api/games/{id}/report
     highlights {clips, index_url} ← GET /api/games/{id}/highlights
   降级：探测不到后端就整体切到 ./demo/ 下的静态产物（见 api.js loadDemo）。
   ========================================================================== */
(function () {
  'use strict';

  // 全局数据状态：各页面组件直接读写这个对象（Vue 3 对普通对象做只读读取即可，
  // 需要响应式的字段都通过 root 的 reactive 版本暴露，见下方 state）
  var STATE = {
    backendOk: false,
    demoMode: true,
    jobId: '',
    game: null,
    players: [],
    shotchart: null,
    report: null,
    highlights: [],
    // 战术层（懒加载 —— 只有打开"战术分析"页才去拉，
    // 否则每次进总览页都要多下几百 KB 的逐帧数据）
    tactics: null,
    tacticsFrames: null,
    tacticsLoaded: false,
    tacticsError: '',
    charts: {},          // 页面里创建过的 ECharts 实例（切换页面时统一 resize）
    loading: false,
    notice: ''
  };
  window.STATE = STATE;

  // Vue 响应式代理：各页面在 computed 里读 window.STORE.xxx 即可自动刷新
  // （app.js 与 api.js 内部仍直接写 STATE，两者共享同一份底层数据）
  var STORE = Vue.reactive(STATE);
  window.STORE = STORE;

  // 菜单即路由表：group 决定在左侧栏归到哪一段（首页 / 分析流程 / 结果与产出）。
  // 用户反馈（2026-09-27）：顶栏原来又平铺了一遍同样的菜单，跟左侧栏重复 ——
  // 现在顶栏只留品牌与状态，导航统一走左侧栏，菜单只在这一处定义。
  var MENUS = [
    { key: 'home', icon: '🏠', title: '首页', group: 'home' },
    { key: 'upload', icon: '⬆', title: '上传与分析', group: 'flow' },
    { key: 'overview', icon: '📊', title: '比赛总览', group: 'flow' },
    { key: 'review', icon: '✅', title: '人工复核', group: 'flow' },
    { key: 'stats', icon: '👥', title: '球员/球队统计', group: 'output' },
    { key: 'shotchart', icon: '🎯', title: '投篮热区', group: 'output' },
    { key: 'tactics', icon: '🧭', title: '战术分析', group: 'output' },
    { key: 'highlights', icon: '🎬', title: '高光集锦', group: 'output' },
    { key: 'report', icon: '📄', title: '导出/报告', group: 'output' },
    { key: 'train', icon: '🏷', title: '训练标注', group: 'output' }
  ];
  // 首页要用这份表渲染模块入口卡片，所以显式挂到 window（首页脚本在 app.js 之前加载，
  // 因此必须在 computed 里**惰性**读取，不能在模块顶层读）
  window.MENUS = MENUS;

  // ------------------------------------------------------------------
  // 数据装载
  // ------------------------------------------------------------------
  /**
   * 装载某个 job 的全部产物。
   * 接口：GET /api/games/{id}、/players、/shotchart、/report、/highlights
   */
  function loadFromBackend(id) {
    var A = window.API;
    // players / shotchart / report / highlights 允许缺失（后端早期版本或 video 源可能没有），
    // 缺失时由 applyGame 用 game.timeline 现算补齐，保证页面仍然可用。
    return Promise.all([
      A.game(id),
      A.players(id).catch(function () { return []; }),
      A.shotchart(id).catch(function () { return null; }),
      A.report(id).catch(function () { return null; }),
      A.highlights(id).catch(function () { return null; })
    ]).then(function (r) {
      applyGame(r[0], r[1], r[2], r[3], r[4]);
      STATE.demoMode = false;
      STATE.jobId = id;
      STATE.notice = '';
    });
  }

  /**
   * 静态产物兜底：读 web/demo/{game,players,shotchart,report}.json + report.md。
   * （仓库默认不带这些产物；跑一次 `aihoop.cli demo` + `scripts\sync_demo.ps1`
   *   或 `scripts\run_demo.ps1` 就有了。）
   */
  function loadFromDemo() {
    var A = window.API;
    return A.loadDemo().then(function (d) {
      if (!d.game) {
        STATE.notice = '未找到静态产物（web/demo/game.json）。先在根目录跑一次：' +
          "python -m aihoop.cli demo --seed 7 --duration 600 --out out/demo " +
          '（或直接启动后端 127.0.0.1:8000，前端会自动连）。缺失：' +
          (d.missing || []).join(', ');
        return;
      }
      applyGame(d.game, d.players || [], d.shotchart, {
        markdown: d.reportMd || '',
        json: d.report_json || null
      }, d.highlights);
      STATE.demoMode = true;
      STATE.jobId = (d.game.meta && d.game.meta.job_id) || 'demo';
      STATE.notice = '';
    });
  }

  /** 规范化并落到全局状态 */
  function applyGame(game, players, shotchart, report, highlights) {
    var D = window.D;
    STATE.game = D.normalizeGame(game || {});

    // 出手点：优先 shotchart.json，其次 players.shots，最后 game.timeline
    var pts = (shotchart && shotchart.all && shotchart.all.points) || [];
    if (!pts.length) pts = D.shotsFromPlayers(players);
    if (!pts.length) pts = D.shotsFromTimeline(STATE.game);
    // 补上 team 字段（shotchart 默认不含 team，用 player_id 反查）
    var teamOf = {};
    (players || []).forEach(function (p) { teamOf[p.player_id] = p.team; });
    (STATE.game.timeline || []).forEach(function (e) { if (e.player_id) teamOf[e.player_id] = e.team; });
    pts.forEach(function (p) {
      if (!p.team) p.team = teamOf[p.player_id] || '';
      if (!p.zone) p.zone = D.zoneOf(p.x, p.y);
      if (p.distance === undefined) p.distance = D.distanceToHoop(p.x, p.y);
    });

    STATE.players = D.normalizePlayers(players || []);
    STATE.shotchart = D.normalizeShotchart(shotchart || {});
    STATE.shotchart.all.points = pts;                 // 保证点集统一
    STATE.shotchart.all.zones = STATE.shotchart.all.zones.length
      ? STATE.shotchart.all.zones : D.aggregateZones(pts);
    if (!STATE.shotchart.all.grid.length) STATE.shotchart.all.grid = D.buildGrid(pts, 1);
    // 分球队分区（后端 by_team 可能缺失）
    ['home', 'away'].forEach(function (s) {
      if (!STATE.shotchart.zonesByTeam[s].length) {
        STATE.shotchart.zonesByTeam[s] = D.aggregateZones(
          pts.filter(function (p) { return p.team === s; }));
      }
    });

    STATE.report = report || null;

    var clips = (highlights && highlights.clips) || STATE.game.highlights || [];
    STATE.highlights = clips;
    STATE.highlightsMeta = highlights || null;
  }
  STATE.applyGame = applyGame;

  /** 统一入口：优先后端，失败自动降级到 demo */
  function bootstrap() {
    STATE.loading = true;
    return window.API.probe().then(function (ok) {
      STATE.backendOk = ok;
      if (ok) {
        // 后端可用：取最近任务里最新的 done 任务作为默认展示对象
        return window.API.listJobs().then(function (jobs) {
          var list = Array.isArray(jobs) ? jobs : (jobs && jobs.jobs) || [];
          var done = list.filter(function (j) { return j.status === 'done'; });
          var pick = done[0] || list[0];
          if (pick && pick.job_id) return loadFromBackend(pick.job_id);
          return loadFromDemo().then(function () {
            STATE.demoMode = true;   // 后端在但没有任务，仍展示演示数据
          });
        }).catch(function () { return loadFromDemo(); });
      }
      return loadFromDemo();
    }).catch(function (e) {
      STATE.backendOk = false;
      return loadFromDemo();
    }).then(function () {
      STATE.loading = false;
    });
  }

  /**
   * 新建任务完成后切到该 job 的数据。
   * 上传页调用：POST /api/jobs -> watchJob -> 成功后 app.loadJob(id)
   */
  function loadJob(id) {
    STATE.tacticsLoaded = false;      // 换比赛了，战术数据要重新拉
    STATE.tactics = null;
    STATE.tacticsFrames = null;
    return loadFromBackend(id);
  }

  /**
   * 懒加载战术层数据。
   *
   * 为什么不跟其它产物一起在 bootstrap 里拉：tactics_frames.jsonl 是逐帧数据，
   * 一场 12 分钟的比赛就有 ~900 帧 × 10 名球员，比 game.json 大一个数量级。
   * 把它绑在总览页上会让"打开就慢"，而战术页并不是每次答辩都会点。
   *
   * 降级：后端没有战术产物（老任务/关闭了战术层）时，演示模式读
   * web/demo/tactics.json；真实模式下直接把 available=false 交给页面去提示。
   */
  function loadTactics(force) {
    if (STATE.tacticsLoaded && !force) return Promise.resolve(STATE.tactics);
    var A = window.API;
    function fromDemo() {
      return Promise.all([A.demoTactics(), A.demoTacticsFrames()])
        .then(function (r) {
          STATE.tactics = r[0] || {
            available: false,
            reason: '演示数据里没有 tactics.json —— 请先跑 scripts/sync_demo.ps1 同步一次'
          };
          STATE.tacticsFrames = r[1] || { available: false, frames: [] };
        });
    }
    var p;
    if (STATE.backendOk && STATE.jobId) {
      p = Promise.all([
        A.tactics(STATE.jobId).catch(function () { return null; }),
        A.tacticsFrames(STATE.jobId).catch(function () { return null; })
      ]).then(function (r) {
        STATE.tactics = r[0] || { available: false, reason: '后端没有返回战术数据' };
        STATE.tacticsFrames = r[1] || { available: false, frames: [] };
      });
    } else {
      p = fromDemo();
    }
    return p.then(function () {
      // 这里**故意不做**"后端没有战术产物就退回演示数据"的兜底：
      // 那样会把另一场比赛的战术图当成当前这场显示出来，属于"看起来像真的"的
      // 假数据 —— 比空状态危险得多。没有就明确说没有，并告诉用户怎么拿到。
      STATE.tacticsLoaded = true;
    }).catch(function (e) {
      STATE.tacticsError = String((e && e.message) || e);
      STATE.tacticsLoaded = true;
    });
  }

  // ------------------------------------------------------------------
  // Vue 根组件
  // ------------------------------------------------------------------
  var app = Vue.createApp({
    data: function () {
      return {
        state: STATE,
        menus: MENUS,
        // 打开网页先落在「首页」：以前默认落在比赛总览，没有任务时是一大片空白，
        // 用户不知道该点哪里（反馈 2026-09-27）
        route: 'home',
        api: window.API,
        isFileProtocol: window.API.isFileProtocol,
        currentPage: null,
        job: window.makeJob(),
        ticking: null,
        reconnectTimer: null
      };
    },
    computed: {
      teamName: function () {
        var self = this;
        return function (side) { return window.D.teamName(self.state.game, side); };
      }
    },
    methods: {
      /** 从 location.hash 解析当前页（#/shotchart -> window.PAGES['shotchart']） */
      syncRoute: function () {
        var key = (location.hash || '').replace(/^#\/?/, '').split('?')[0] || 'home';
        if (!window.PAGES[key]) key = 'home';
        this.route = key;
        this.currentPage = window.PAGES[key];
      },
      probe: function () {
        var self = this;
        window.API.probe().then(function (ok) {
          self.state.backendOk = ok;
          if (ok) {
            self.stopReconnect();
            self.$message.success('后端已连接：' + window.API.base);
            bootstrap();
          } else {
            self.$message.warning('仍未探测到后端，继续使用演示数据模式');
          }
        });
      },
      /** 顶栏任务快照：有活动任务时 1.5s 轮询一次（WS 由上传页负责） */
      trackJob: function () {
        var self = this;
        if (self.ticking) return;
        self.ticking = setInterval(function () {
          if (!self.job.job_id) return;
          window.API.getJob(self.job.job_id).then(function (j) {
            self.job = j;
            if (j.status === 'done' || j.status === 'error') {
              clearInterval(self.ticking); self.ticking = null;
            }
          }).catch(function () {});
        }, 3000);
      },
      /**
       * 后端起来之前打开的页面**自动重连** —— 不用再让用户自己点「重新探测」。
       *
       * 为什么必须有：`backendOk` 只在页面加载时探测一次（见 bootstrap），之后不会重试。
       * 于是"先开页面、后起后端"（或者后端正在自动重启的那两三秒）就会一直停在
       * "未检测到后端"：上传、标篮筐、标球场按钮全是灰的，用户以为界面坏了 ——
       * 实测反复踩到。这里每 5 秒探一次，连上就自动切回真实数据。
       */
      autoReconnect: function () {
        var self = this;
        if (self.reconnectTimer) return;
        var tries = 0;
        self.reconnectTimer = setInterval(function () {
          if (self.state.backendOk) { self.stopReconnect(); return; }
          if (++tries > 120) {                 // 约 10 分钟还没等到 → 停手，按钮仍在
            self.stopReconnect();
            self.$message.warning('仍未探测到后端：启动后端后点右上角「重新探测」');
            return;
          }
          window.API.probe().then(function (ok) {
            if (!ok) return;
            self.stopReconnect();
            self.state.backendOk = true;
            self.$message.success('后端已连接：' + window.API.base);
            bootstrap();
          }).catch(function () { /* 后端还没起来，下一轮继续 */ });
        }, 5000);
      },
      stopReconnect: function () {
        if (this.reconnectTimer) {
          clearInterval(this.reconnectTimer);
          this.reconnectTimer = null;
        }
      },
      /** 供上传页调用：登记正在跑的任务 */
      registerJob: function (job) { this.job = job; this.trackJob(); }
    },
    mounted: function () {
      var self = this;
      window.addEventListener('hashchange', function () { self.syncRoute(); });
      this.syncRoute();
      bootstrap();
      this.autoReconnect();      // 后端晚起来也能自动连上（见该方法注释）
      // 暴露给页面组件用（避免每个页面重复挂 window）
      window.APP = this;
    }
  });

  app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
  app.component('echarts-box', window.COMPONENTS['echarts-box']);
  app.component('court-view', window.COMPONENTS['court-view']);
  app.component('stat-compare', window.COMPONENTS['stat-compare']);
  app.component('big-scoreboard', window.COMPONENTS['big-scoreboard']);
  app.mount('#app');

  window.APP_BOOTSTRAP = bootstrap;
  window.APP_LOAD_JOB = loadJob;
  window.APP_TACTICS = loadTactics;
})();
