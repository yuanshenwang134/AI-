/* ==========================================================================
   pages/highlights.js —— 页面 5：高光集锦
   --------------------------------------------------------------------------
   数据来源接口：
     GET /api/games/{job_id}/highlights   { clips:[...], index_url }
     GET /api/media/{job_id}/{path}       片段文件（highlights/*.mp4）
     GET /api/games/{job_id}/video        原始视频（片段不可用时的兜底播放源）
   片段字段（后端 game.highlights / GameResult.clips）：
     {index,t,start,end,path,made,value,team,player_id,label,available}
   无片段文件时的降级：显示占位卡 + 一行 ffmpeg 命令，说明如何在本机生成片段；
   若本机有原始视频，也可直接调用 /api/games/{id}/video 从 start 秒播放。
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['highlights'] = {
  name: 'page-highlights',
  data: function () {
    return {
      S: window.STORE,
      sortBy: 'index',        // index 精彩度 | points 得分 | three 三分 | time 时间
      activeIdx: -1,
      playing: {},
      failed: {},
      // ---- 进球确认（训练数据）----
      cand: { list: [], shots: [], rejected: [], note: '' },
      verdicts: {},           // {时刻: 'made'|'miss'}，提交前先攒在这里
      confirmed: 0
    };
  },
  computed: {
    /** 「没有高光片段」时把真正的原因说清楚 —— 不能只丢一句"暂无高光片段"。
     *  实测用户看到空高光会以为功能坏了；真正原因通常是这次分析 0 进球：
     *  镜头在动 → 篮筐判据放弃；球太小 → 追不到；也没有记分牌事件。 */
    emptyWhy: function () {
      var g = this.game || {};
      var m = g.meta || {};
      var lines = ['进球/出手数：0（这次分析没有产生任何可剪的回合）'];
      var ve = m.visual_error || (m.hoopsight && m.hoopsight.reason);
      if (ve) { lines.push('为什么没有：' + String(ve).slice(0, 160)); }
      lines.push('');
      lines.push('想让高光有内容，三条路：');
      lines.push('  ① 自动候选 + 逐条确认：用上面的「进球确认」面板，'
        + '或「人工复核」页逐条判进/没进，判完后点「重建高光」；');
      lines.push('  ② 记分牌路径：视频里有可读记分牌时，'
        + '得分事件会直接变成进球记录（这段视频底部就有记分牌）；');
      lines.push('  ③ 换更清楚的素材：球在画面里 ≥30 像素时，'
        + '进球检测才能工作（README「素材体检」）。');
      return lines.join('\n');
    },
    /** 已选但还没提交的判断数量（按钮上显示「提交我的判断(N)」） */
    pending: function () {
      var n = 0;
      for (var k in this.verdicts) {
        if (this.verdicts[k] === 'made' || this.verdicts[k] === 'miss') n++;
      }
      return n;
    },
    game: function () { return this.S.game; },
    clips: function () {
      var list = (this.S.highlights || []).slice();
      var s = this.sortBy;
      list.sort(function (a, b) {
        if (s === 'points') return (b.made ? Number(b.value || 0) : 0) - (a.made ? Number(a.value || 0) : 0) || a.t - b.t;
        if (s === 'three') return (Number(b.value) === 3 ? 1 : 0) - (Number(a.value) === 3 ? 1 : 0) || a.t - b.t;
        if (s === 'time') return a.t - b.t;
        return (a.index === undefined ? 0 : a.index) - (b.index === undefined ? 0 : b.index);
      });
      return list;
    },
    availableCount: function () {
      return this.clips.filter(function (c) { return c.available && c.path; }).length;
    },
    /** 演示模式：片段路径按 web/demo 相对路径解析 */
    isDemo: function () { return this.S.demoMode || !this.S.jobId || this.S.jobId === 'demo'; },
    ffmpegHint: function () {
      var c = this.clips[0];
      var start = c ? (c.start !== undefined ? c.start : Math.max(0, c.t - 2)) : 0;
      var end = c ? (c.end !== undefined ? c.end : (c.t + 1.5)) : 3.5;
      var out = (c && c.path) ? c.path : 'highlights/clip_01.mp4';
      return 'ffmpeg -ss ' + Number(start).toFixed(1) + ' -to ' + Number(end).toFixed(1) +
        ' -i game.mp4 -c copy ' + out;
    }
  },
  methods: {
    teamName: function (s) { return window.D.teamName(this.game, s); },
    mmss: function (t) { return window.D.mmss(t); },
    /** 片段播放地址：优先 /api/media，兜底原始视频；绝对文件路径不可用时返回空串 */
    clipSrc: function (c) {
      if (!c || !c.path || !window.API.isWebPath(c.path)) return '';
      if (this.isDemo) return window.API.demoMediaUrl(c.path);
      return window.API.mediaUrl(this.S.jobId, c.path);
    },
    playable: function (c) {
      if (!c || !c.available || !c.path) return false;
      // 绝对文件系统路径（如 D:\out\highlights\x.mp4）浏览器拿不到，只能走原始视频跳转
      return !!this.clipSrc(c);
    },
    videoSrc: function () {
      if (this.isDemo || !this.S.jobId) return '';
      return window.API.jobVideoUrl(this.S.jobId);
    },
    /* ------------------------------------------------------------------
       进球确认（人工判断 → 过滤误报 + 攒训练数据）
       ------------------------------------------------------------------ */
    /** 读本次分析自动判成进球的时刻（GET /api/games/{id}/candidates） */
    loadCandidates: function () {
      var self = this;
      if (!this.S.jobId) { this.$message.warning('还没有任务'); return; }
      window.API.candidates(this.S.jobId).then(function (d) {
        self.cand = d || { shots: [], auto_shots: [], rejected: [], note: '' };
        // 界面上要判的是**自动判定**（过滤前），否则过滤完就看不到东西了
        self.cand.list = (d && d.auto_shots && d.auto_shots.length)
          ? d.auto_shots : ((d && d.shots) || []);
        self.verdicts = {};
        var n = 0;
        self.cand.list.forEach(function (s) {
          if (s.feedback) { self.verdicts[s.t] = s.feedback; n += 1; }
        });
        self.confirmed = n;
        if (!self.cand.list.length) {
          self.$message.info('这次分析没有判定出进球（或该任务没有 hoopsight 结果）');
        }
      }).catch(function (e) {
        self.$message.error('读取失败：' + (e && e.message ? e.message : e));
      });
    },
    /** 提交判断：POST /api/feedback（下次分析同一视频会自动用它过滤） */
    submitFeedback: function () {
      var self = this;
      var items = Object.keys(this.verdicts).map(function (t) {
        return { t: Number(t), label: self.verdicts[t] };
      }).filter(function (x) { return x.label === 'made' || x.label === 'miss'; });
      if (!items.length) { this.$message.warning('先给至少一个球选「进了/没进」'); return; }
      var vp = (this.S.game && this.S.game.meta && this.S.game.meta.video_path) ||
        this.cand.video_path || '';
      window.API.postFeedback(vp, items).then(function (r) {
        self.$message.success('已记录 ' + r.saved + ' 条判断（该视频累计 ' +
          r.total_for_video + ' 条，其中 ' + r.made + ' 个进球）—— ' +
          '重新分析这段视频时会自动按你的判断过滤');
        self.confirmed = r.total_for_video;
      }).catch(function (e) {
        self.$message.error('提交失败：' + (e && e.message ? e.message : e));
      });
    },
    play: function (c, i) {
      this.activeIdx = i;
      if (!c) return;
      // 没有可用片段文件时：若有原始视频，用 <video> 的 currentTime 跳到该回合
      if (!this.playable(c)) {
        var v = this.$refs.raw;
        if (v) {
          v.currentTime = Math.max(0, Number(c.start !== undefined ? c.start : c.t - 2));
          v.play();
        } else {
          this.$message.info('该片段没有视频文件，且当前没有可用的原始视频源；请看下方 ffmpeg 命令自行切片');
        }
        return;
      }
      this.playing[i] = true;
    },
    onErr: function (i) { this.failed[i] = true; this.playing[i] = false; this.$message.warning('该片段无法播放（文件缺失或编码不支持），已回退为占位卡'); },
    /** 逐个触发下载（浏览器不允许一次性打包下载多个文件） */
    downloadAll: function () {
      var self = this;
      var list = this.clips.filter(function (c) { return self.playable(c); });
      if (!list.length) { this.$message.warning('没有可下载的片段文件'); return; }
      list.forEach(function (c, i) {
        setTimeout(function () {
          var a = document.createElement('a');
          a.href = self.clipSrc(c);
          a.download = String(c.path).split('/').pop();
          a.target = '_blank';
          document.body.appendChild(a); a.click();
          setTimeout(function () { document.body.removeChild(a); }, 300);
        }, i * 350);
      });
      this.$message.success('已开始下载 ' + list.length + ' 个片段（浏览器逐个下载）');
    },
    downloadIndex: function () {
      var idx = this.S.highlightsMeta && this.S.highlightsMeta.index_url;
      var href;
      if (idx && !this.isDemo) {
        href = /^https?:/.test(idx) ? idx : window.API.base + idx;
      } else {
        href = window.API.demoMediaUrl('highlights.json');
      }
      var a = document.createElement('a');
      a.href = href; a.download = 'highlights.json'; a.target = '_blank';
      document.body.appendChild(a); a.click();
      setTimeout(function () { document.body.removeChild(a); }, 300);
    }
  },
  template: [
    '<div>',

    // ---- 进球确认：把「这一下到底进没进」的人工判断记下来 ----
    // 为什么放在高光页：这里本来就是"逐球回看"的地方。判过之后
    //   1) 后端下次分析同一段视频会自动用它过滤误报；
    //   2) 这些带标签的时刻会攒成训练/评估数据（GET/POST /api/feedback）。
    '  <div class="card" v-if="S.backendOk && S.jobId && S.jobId!==\'demo\'">',
    '    <h3 class="card-title">进球确认 <span class="sub">逐球回看并标记「进了 / 没进」——判过的会用于过滤误报，并攒成训练数据</span>',
    '      <span class="grow"></span>',
    '      <el-tag size="small" type="warning" effect="plain" v-if="cand.list && cand.list.length">自动判定 {{ cand.list.length }} 球 · 已确认 {{ confirmed }} 个</el-tag>',
    '    </h3>',
    '    <div class="row">',
    '      <el-button size="small" @click="loadCandidates">读取本次自动判定</el-button>',
    '      <el-button size="small" type="primary" :disabled="!pending.length" @click="submitFeedback">提交我的判断（{{ pending.length }}）</el-button>',
    '      <span class="hint" v-if="cand.note">{{ cand.note }}</span>',
    '      <span class="hint" v-if="cand.auto_dropped && cand.auto_dropped.length">· 已被既有判断否掉：{{ cand.auto_dropped.join(\', \') }}s</span>',
    '    </div>',
    '    <el-table v-if="cand.list && cand.list.length" :data="cand.list" size="small" border style="margin-top:10px">',
    '      <el-table-column prop="t" label="时刻(s)" width="90" />',
    '      <el-table-column label="自动判定" width="100">',
    '        <template #default="s"><el-tag size="small" type="danger">判为进球</el-tag></template>',
    '      </el-table-column>',
    '      <el-table-column label="置信度" width="90">',
    '        <template #default="s">{{ (s.row.confidence||0).toFixed(2) }}</template>',
    '      </el-table-column>',
    '      <el-table-column label="下落(px)" width="90">',
    '        <template #default="s">{{ Math.round(s.row.drop_px||0) }}</template>',
    '      </el-table-column>',
    '      <el-table-column label="我的判断" min-width="220">',
    '        <template #default="s">',
    '          <el-radio-group v-model="verdicts[s.row.t]" size="small">',
    '            <el-radio-button :label="\'made\'">进了</el-radio-button>',
    '            <el-radio-button :label="\'miss\'">没进</el-radio-button>',
    '          </el-radio-group>',
    '          <el-tag v-if="s.row.feedback" size="small" effect="plain" style="margin-left:8px">已判过：{{ s.row.feedback===\'made\'?\'进了\':\'没进\' }}</el-tag>',
    '        </template>',
    '      </el-table-column>',
    '    </el-table>',
    '    <div class="hint" v-else>点「读取本次自动判定」看这次分析把哪些时刻判成了进球；逐条标记后提交。</div>',
    '  </div>',

    '  <div class="card" v-if="!clips.length">',
    '    <h3 class="card-title">高光集锦 <span class="sub">本片段没有可剪的回合</span></h3>',
    '    <el-alert type="info" :closable="false" show-icon',
    '      title="没有高光片段 —— 因为这次分析没有判定出任何进球/出手"',
    '      :description="emptyWhy" />',
    '    <div class="hint" style="max-width:620px;text-align:left;margin-top:10px">',
    '      技术说明：高光由后端在任务里勾选「生成高光片段」后产出'
    + '（<code>make_highlights=true</code>，依赖 ffmpeg）；'
    + '但没有进球/出手时，它会正确地产出**空集**，而不是随便剪几段充数。',
    '    </div>',
    '  </div>',

    '  <template v-else>',
    '    <div class="card">',
    '      <h3 class="card-title">高光集锦 <span class="sub">数据来源：GET /api/games/{{ S.jobId }}/highlights · 片段文件经 /api/media/{{ S.jobId }}/… 访问</span>',
    '        <span class="grow"></span>',
    '        <el-tag size="small" type="info" effect="plain">共 {{ clips.length }} 个片段 · 可播 {{ availableCount }} 个</el-tag>',
    '      </h3>',
    '      <div class="row">',
    '        <el-radio-group v-model="sortBy" size="small">',
    '          <el-radio-button label="index">按精彩度</el-radio-button>',
    '          <el-radio-button label="points">按得分</el-radio-button>',
    '          <el-radio-button label="three">优先三分</el-radio-button>',
    '          <el-radio-button label="time">按时间</el-radio-button>',
    '        </el-radio-group>',
    '        <span class="spacer"></span>',
    '        <el-button size="small" type="primary" plain @click="downloadAll">一键下载全部片段</el-button>',
    '        <el-button size="small" @click="downloadIndex">下载索引 JSON</el-button>',
    '      </div>',
    '    </div>',

    '    <div class="card clip-grid">',
    '      <div class="clip" v-for="(c,i) in clips" :key="i">',
    '        <video v-if="playing[i] && playable(c) && !failed[i]" :src="clipSrc(c)" controls autoplay',
    '               @error="onErr(i)" style="width:100%"></video>',
    '        <div v-else class="thumb" @click="play(c,i)">',
    '          <span>▶</span>',
    '          <span class="lab">{{ c.result === \'unknown\' ? \'待确认\' : c.made ? \'+\' + c.value + \' 分\' : \'未中\' }}</span>',
    '        </div>',
    '        <div class="body">',
    '          <div class="ttl">',
    '            #{{ (c.index===undefined? i+1 : c.index) }} {{ c.label || (c.player_id + \' \' + (c.made? (\'命中\'+c.value+\'分球\') : \'出手未中\')) }}',
    '          </div>',
    '          <div class="mt">',
    '            时刻 {{ mmss(c.t) }} · 区间 {{ mmss(c.start) }} ~ {{ mmss(c.end) }}<br>',
    '            <span class="tl-badge" :class="c.team===\'home\'?\'h\':\'a\'">{{ c.team===\'home\' ? teamName(\'home\') : teamName(\'away\') }}</span>',
    '            <span style="margin-left:6px">{{ c.player_id }}</span> · {{ c.value }} 分球<br>',
    '            <span class="muted">文件：{{ c.path || \'（未生成）\' }}</span>',
    '          </div>',
    '          <div class="row" style="margin-top:8px">',
    '            <el-button size="small" text type="primary" @click="play(c,i)">播放</el-button>',
    '            <a v-if="playable(c)" :href="clipSrc(c)" :download="(c.path||\'\').split(\'/\').pop()" target="_blank">',
    '              <el-button size="small" text>下载</el-button>',
    '            </a>',
    '            <el-tag v-else size="small" type="warning" effect="plain">无可用片段文件</el-tag>',
    '          </div>',
    '        </div>',
    '      </div>',
    '    </div>',

    '    <div class="card">',
    '      <h3 class="card-title">没有片段文件怎么办？ <span class="sub">两条兜底路径，答辩现场不会卡住</span></h3>',
    '      <div class="hint">',
    '        <b>路径 1 · 用原始视频按时间点跳转：</b>后端 <code>GET /api/games/{id}/video</code> 支持 Range 请求，',
    '        下方的播放器点「跳到该回合」即可复现任意片段（片段的 start/end 已经在表里给出）。',
    '      </div>',
    '      <div class="row" style="margin:10px 0">',
    '        <el-select v-model="activeIdx" size="small" style="width:320px" placeholder="选择要跳转的片段">',
    '          <el-option v-for="(c,i) in clips" :key="i" :label="\'#\'+(c.index===undefined?i+1:c.index)+\' \'+mmss(c.start)+\' \'+(c.player_id||\'\')" :value="i" />',
    '        </el-select>',
    '        <el-button size="small" type="primary" :disabled="activeIdx<0 || isDemo"',
    '                   @click="activeIdx>=0 && play(clips[activeIdx], activeIdx)">跳到该回合（原始视频）</el-button>',
    '      </div>',
    '      <video v-if="videoSrc" ref="raw" :src="videoSrc" controls preload="metadata"',
    '             style="width:100%;max-height:360px;border-radius:10px;background:#000"></video>',
    '      <el-alert v-else type="info" :closable="false"',
    '                title="演示数据模式下没有原始视频；连接后端并新建 video 任务后，这里会显示原始视频播放器。" />',
    '      <el-divider />',
    '      <div class="hint">',
    '        <b>路径 2 · 本机用 ffmpeg 现场切片</b>（后端没装 ffmpeg 或只跑合成数据时）：',
    '        <div class="stage-log" style="margin-top:8px"><div>{{ ffmpegHint }}</div></div>',
    '        把 <code>-ss/-to</code> 换成上表任意片段的 <code>start/end</code> 即可；',
    '        <code>-c copy</code> 不重新编码，速度最快。',
    '      </div>',
    '    </div>',
    '  </template>',
    '</div>'
  ].join('\n')
};
