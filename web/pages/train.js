/* ==========================================================================
   pages/train.js —— 训练标注页：逐球判「进没进」，攒训练数据
   --------------------------------------------------------------------------
   为什么单独一页：
     自动判据在真实素材上误报严重（实测 nybo 3 个判定全错），而模型要能用
     就必须有**人工标注的进球样本**。这页就是攒样本的地方：
       * 左边列表是候选时刻（按"像进球"程度排序）；
       * 每一条能看**证据图**（篮筐放大 + 逐帧 + 球块圆圈）——不靠数字猜；
       * 点「进了 / 没进」立即写入后端（POST /api/label/labeled）,
         同时用于过滤误报、并作为训练样本。
   数据来源：
     GET  /api/label/candidates?path=...   候选列表
     GET  /api/label/sheet?path=..&idx=..  证据图
     POST /api/label/labeled              记录一条标注
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['train'] = {
  name: 'page-train',
  data: function () {
    return {
      S: window.STORE,
      candPath: 'out/label_bili/candidates.json',
      info: null,
      list: [],
      cur: 0,
      clipUrl: '',
      sheetUrl: '',
      zoom: 4,
      slow: 3,
      useSheet: false,
      loading: false,
      video: '',
      onlyLikely: false,
      onlyUnlabeled: true,
      showDone: true
    };
  },
  computed: {
    shown: function () {
      var self = this;
      return this.list.filter(function (c) {
        // 「只看未标注」默认开：标过的就不该再占屏幕，剩下的才是要干的活
        if (self.onlyUnlabeled && c.feedback) return false;
        if (self.onlyLikely && !c.likely) return false;
        if (!self.showDone && c.feedback) return false;
        return true;
      });
    },
    curCand: function () {
      return this.shown[this.cur] || null;
    },
    progress: function () {
      if (!this.list.length) return 0;
      var n = this.list.filter(function (c) { return !!c.feedback; }).length;
      return Math.round(n * 100 / this.list.length);
    },
    madeN: function () {
      return this.list.filter(function (c) { return c.feedback === 'made'; }).length;
    }
  },
  methods: {
    load: function () {
      var self = this;
      if (!this.S.backendOk) { this.$message.warning('需要后端在线'); return; }
      this.loading = true;
      window.API.labelCandidates(this.candPath).then(function (d) {
        self.info = d;
        self.list = d.candidates || [];
        self.video = d.video || '';
        self.cur = 0;
        self.loadSheet();
        self.loading = false;
        self.$message.success('载入 ' + d.count + ' 个候选（像进球 ' + d.likely +
          ' 个，已标注 ' + d.labeled + '）');
      }).catch(function (e) {
        self.loading = false;
        self.$message.error('载入失败：' + (e && e.message ? e.message : e));
      });
    },
    loadSheet: function () {
      if (!this.curCand) { this.clipUrl = ''; this.sheetUrl = ''; return; }
      // 默认给「放大慢放视频」：逐帧拼图用户看不懂
      this.clipUrl = window.API.labelClipUrl(this.candPath, this.curCand.idx,
                                             this.zoom, this.slow);
      this.sheetUrl = window.API.labelSheetUrl(this.candPath, this.curCand.idx);
    },
    reloadClip: function () {
      if (this.curCand) {
        this.clipUrl = window.API.labelClipUrl(this.candPath, this.curCand.idx,
                                               this.zoom, this.slow);
      }
    },
    pick: function (i) { this.cur = i; this.loadSheet(); },
    prev: function () { if (this.cur > 0) { this.cur--; this.loadSheet(); } },
    next: function () { if (this.cur < this.shown.length - 1) { this.cur++; this.loadSheet(); } },
    mark: function (label) {
      var self = this;
      var c = this.curCand;
      if (!c) return;
      window.API.labelOne({
        path: this.candPath, idx: c.idx, t: c.t0, label: label,
        video: this.video, note: '训练标注页'
      }).then(function (r) {
        c.feedback = label;
        self.$message.success((label === 'made' ? '记为进球' : '记为没进') +
          '（累计进球 ' + r.make + ' / 共 ' + r.count + ' 条）');
        self.next();
      }).catch(function (e) {
        self.$message.error('记录失败：' + (e && e.message ? e.message : e));
      });
    },
    mmss: function (t) {
      var s = Math.floor(t % 60), m = Math.floor(t / 60);
      return m + ':' + (s < 10 ? '0' : '') + s;
    }
  },
  mounted: function () { this.load(); },
  template: [
    '<div>',
    '  <div class="card">',
    '    <h3 class="card-title">训练标注 <span class="sub">逐球判「进没进」——每条判断都用于过滤误报，并累积成训练样本</span></h3>',
    '    <div class="row" style="align-items:center">',
    '      <span class="hint">候选文件</span>',
    '      <el-input v-model="candPath" size="small" style="max-width:420px" />',
    '      <el-button size="small" type="primary" :loading="loading" @click="load">载入</el-button>',
    '      <el-checkbox v-model="onlyUnlabeled" size="small">只看未标注</el-checkbox>',
    '      <el-checkbox v-model="onlyLikely" size="small">只看「像进球」</el-checkbox>',
    '      <el-checkbox v-model="showDone" size="small">显示已标注</el-checkbox>',
    '    </div>',
    '    <el-progress :percentage="progress" :stroke-width="10" style="margin-top:10px;max-width:520px" />',
    '    <div class="hint">已标注 {{ progress }}%（进球 {{ madeN }} 个）· 当前列表 {{ shown.length }} 条</div>',
    '  </div>',

    '  <div class="grid grid-2">',
    '    <div class="card">',
    '      <h3 class="card-title">候选列表 <span class="sub">按「像进球」程度排序</span></h3>',
    '      <div class="clip-list" style="max-height:560px;overflow:auto">',
    '        <div v-for="(c,i) in shown" :key="c.idx" @click="pick(i)"',
    '             :style="{padding:\'6px 8px\',cursor:\'pointer\',borderRadius:\'6px\',background:(i===cur?\'#eef2ff\':\'transparent\')}">',
    '          <span :style="{color:c.likely?\'#16a34a\':\'#94a3b8\'}">{{ c.likely ? \'★\' : \'·\' }}</span>',
    '          <b style="margin-left:6px">{{ mmss(c.t0) }}</b>',
    '          <span class="hint" style="margin-left:8px">下落 {{ Math.round(c.drop_px||0) }}px · 居圈心 {{ (c.rel_x_at_rim||0).toFixed(2) }}rx</span>',
    '          <el-tag v-if="c.feedback" size="small" :type="c.feedback===\'made\'?\'success\':\'info\'"',
    '                  effect="plain" style="margin-left:8px">{{ c.feedback===\'made\'?\'进了\':\'没进\' }}</el-tag>',
    '        </div>',
    '        <div v-if="!shown.length" class="muted" style="padding:20px">没有可标注的候选（先点「载入」）</div>',
    '      </div>',
    '    </div>',

    '    <div class="card">',
    '      <h3 class="card-title">放大慢放视频 <span class="sub">红圈=篮圈，黄圈=球；球有没有落进网，看视频一眼就知道</span></h3>',
    '      <div v-if="curCand">',
    '        <div class="row" style="align-items:center;margin-bottom:8px">',
    '          <b>{{ mmss(curCand.t0) }}</b>',
    '          <span class="hint">（{{ curCand.t0.toFixed(2) }}s）</span>',
    '          <span class="grow"></span>',
    '          <el-button size="small" @click="prev">上一个</el-button>',
    '          <el-button size="small" @click="next">下一个</el-button>',
    '        </div>',
    '        <div class="row" style="align-items:center;margin-bottom:6px">',
    '          <span class="hint">放大</span>',
    '          <el-select v-model="zoom" size="small" style="width:90px" @change="reloadClip">',
    '            <el-option :value="3" label="3x" /><el-option :value="4" label="4x" /><el-option :value="6" label="6x" />',
    '          </el-select>',
    '          <span class="hint">慢放</span>',
    '          <el-select v-model="slow" size="small" style="width:100px" @change="reloadClip">',
    '            <el-option :value="1" label="原速" /><el-option :value="2" label="2x慢" />',
    '            <el-option :value="3" label="3x慢" /><el-option :value="5" label="5x慢" />',
    '          </el-select>',
    '          <el-checkbox v-model="useSheet" size="small">改用逐帧拼图</el-checkbox>',
    '        </div>',
    '        <div v-if="!useSheet" style="border:1px solid #e5e7eb;border-radius:6px;background:#000">',
    '          <video v-if="clipUrl" :src="clipUrl" controls autoplay loop muted playsinline',
    '                 style="width:100%;display:block;border-radius:6px" />',
    '          <div v-else class="muted" style="padding:20px">正在切片…（首次约 1~3 秒，之后秒开）</div>',
    '        </div>',
    '        <div v-else style="max-height:420px;overflow:auto;border:1px solid #e5e7eb;border-radius:6px">',
    '          <img v-if="sheetUrl" :src="sheetUrl" style="width:100%;display:block" />',
    '        </div>',
    '        <div class="row" style="margin-top:12px">',
    '          <el-button type="success" size="large" @click="mark(\'made\')">✓ 进了</el-button>',
    '          <el-button type="info" size="large" @click="mark(\'miss\')">✗ 没进</el-button>',
    '          <span class="hint" style="margin-left:10px">判完自动跳到下一个</span>',
    '        </div>',
    '      </div>',
    '      <el-empty v-else description="左边选一个候选" />',
    '    </div>',
    '  </div>',
    '</div>'
  ].join('\n')
};
