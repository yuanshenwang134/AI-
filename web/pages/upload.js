/* ==========================================================================
   pages/upload.js —— 页面 1：上传与分析配置
   --------------------------------------------------------------------------
   数据来源接口：
     POST /api/jobs            建任务，body {video_path, source, seed, make_highlights}
     WS   /api/jobs/{id}/ws    每帧推送任务 JSON（status/progress/message）
     GET  /api/jobs/{id}       进度轮询兜底
     GET  /api/jobs            最近 20 条任务（可直接回看历史产物）
     GET  /api/games/{id} ...  任务完成后自动装载全部产物
   无后端时的降级：给出明确提示，并允许直接「载入演示数据」。
   ========================================================================== */
window.PAGES = window.PAGES || {};

window.PAGES['upload'] = {
  name: 'page-upload',
  data: function () {
    return {
      S: window.STORE,
      api: window.API,
      form: {
        source: 'video',            // video | jsonl（合成演示入口已移除）
        video_path: '',
        // 不再有 seed 输入框：它只对合成比赛生效，真实视频不用（见 start() 里的注释）
        make_highlights: true,
        // 真视频快速模式：跳过逐帧 YOLO，只走比分牌 + 颜色追球
        fast: false,
        // 进球标注文件（scripts/label_baskets.py 产出）—— 开发/评测用的基准输入，
        // 2026-09-27 起**不再暴露在界面上**（用户反馈：产品不该先要一份标注）。
        // 字段与后端参数保留，脚本调用仍可用；人工纠错走「人工复核」页。
        labels_path: '',
        // 可选：比分牌得分事件文件（「读比分牌」按钮的产出）。给了它，
        // 非标准台标也能走比分牌路径；留空则由后端自动定位。
        scoreboard_events: ''
      },
      busy: false,
      job: { job_id: '', status: '', progress: 0, message: '', error: null, out_dir: '', summary: '' },
      logs: [],
      jobs: [],
      watcher: null,
      uploadPct: -1,          // -1 = 未上传；0~1 = 上传中
      uploadedName: '',
      health: null,
      fileName: '',           // 本地文件名（仅用于界面显示；上传后用后端返回的路径）
      // ---- 「在画面上标篮筐」弹窗 ----
      markOpen: false,
      markAt: 1.0,            // 用第几秒的帧来标
      markImg: '',            // data:image/jpeg;base64,...
      markSize: { w: 0, h: 0 },
      markPts: [],            // [{name, label, x, y}]（归一化 0~1）
      markIdx: 0,
      // 可选步骤：选了"量筐宽"里的某一项后，下一次点画面落的就是它
      // （不选它就是默认路径：只点中心，半径交给检测器量）
      hoopOptTarget: '',
      markSaving: false,
      savedMarks: null,       // 该视频已保存的标点
      // ---- 标球场：点场地特征点解标定（用户建议：不一定要标篮筐）----
      markKind: 'hoop',       // hoop | court
      courtItems: [],         // [{name,label,hint}] 当前这一侧半场可见的特征点
      // 「这一侧半场」由用户选一次（左半场 / 右半场）。为什么不让工具自己认：
      // 试过用自动标定 + 罚球区可见面积投票，投影会退化、面积算成 0，不可靠；
      // 而选一次只影响**点名表显示哪 7 个点**，代价极小、结果确定。
      // 用户实测反馈：「不需要说这个点靠近篮筐了」—— 因为这台机位只拍得到
      // 篮筐这一侧，列"另一侧/中圈那一头"的点只会让人点画面里不存在的东西。
      courtSide: 'left',      // left = 摄像机拍的是左边那半场 | right
      // ⚠️ 哪一套点对应哪一侧，必须按**工具内部的坐标约定**来，不能凭直觉：
      //   court.py 里左半场 = y ∈ [-14, 0]（底线在 y=-14），也就是 **_near 那一套**；
      //   右半场 = y ∈ [0, 14]（底线在 y=+14），即 **_far 那一套**。
      //   写反了不会报错，只会把球场整体平移 28m（两分变三分、热区整片错位）。
      courtSideNames: {
        left: [
          { name: 'hoop_near', label: '篮筐中心' },
          { name: 'corner_near_left', label: '底线左角' },
          { name: 'corner_near_right', label: '底线右角' },
          { name: 'lane_near_left', label: '罚球区左角' },
          { name: 'lane_near_right', label: '罚球区右角' },
          { name: 'ft_near', label: '罚球线中点' },
          { name: 'arc_near', label: '三分弧顶' }
        ],
        right: [
          { name: 'hoop_far', label: '篮筐中心' },
          { name: 'corner_far_left', label: '底线左角' },
          { name: 'corner_far_right', label: '底线右角' },
          { name: 'lane_far_left', label: '罚球区左角' },
          { name: 'lane_far_right', label: '罚球区右角' },
          { name: 'ft_far', label: '罚球线中点' },
          { name: 'arc_far', label: '三分弧顶' }
        ]
      },
      mfFrames: [],           // 抽出来的画面 [{t,image,w,h}]
      mfIdx: 0,               // 当前是第几张
      mfPts: [],              // 已标点 [{t,name,label,x,y}]
      mfTarget: '',           // 当前选中要标的点名
      mfResult: null,         // 解算结果
      mfPreviewed: false,     // 预览过且可用（界面上用来区分"预览"与"已保存"）
      hoverPt: null,          // 鼠标在画面上的位置（画准星用）
      mfCenter: 60,           // 在哪个时刻附近抽帧（同一镜头内）
      mfSpan: 3,              // 抽帧的时间跨度（秒）
      // 用户实测反馈：「只能在一个画面里点，想点完所有点根本不显示」——
      // 一个镜头里只看得见半场，剩下的半场在这个画面里压根不存在，
      // 于是怎么点都点不全。做法改成**左右两个画面各自点各自的点**：
      // 左画面标一侧半场、右画面标另一侧半场，两边的点一起送去解算。
      // 单应矩阵的硬要求没变：**每个画面自己要有 4 个以上、铺得开的点**
      // （backend /api/calibrate_multi 的 per_frame_fit 就是按这个判的）。
      dualView: true,         // true = 左右两个画面；false = 老的「一张一张切」
      mfActive: 0,            // 正在点哪个画面：0=左（上面那张）1=右
      mfIdx2: 0,              // 右边画面对应的帧下标
      courtPts: [],
      courtIdx: 0,
      courtResult: null,
      // 标定自检叠加层：把"这份 H 算出来的球场线"画回画面上，
      // 对不对得上一眼就能看见 —— 而不是只给一个"误差 4.2m"的数字。
      // 为什么必须加：5 个点里**任取 4 个都能精确解出 H（误差恒为 0）**，
      // 所以单看误差数字，用户根本无法判断自己点得对不对（实测争议点）。
      showCourtOverlay: true,
      overlayW: 0,
      overlayH: 0,
      overlayLines: [],        // [[[x,y],...], ...] 画面像素坐标
      snapPts: [],             // 「点吸附精修」被采纳时的点（画成空心圈，供对照）
      // ---- 新手上手（onboard）----
      // 目标：让第一次拿到软件的人在 5 分钟内看到一份结果，而不是对着表单发愣。
      // 关闭状态记在 localStorage：看过就不再烦他，但可以随时重新打开。
      guideOpen: true,
      samples: [],             // 机器上现成的视频（示例 + 已上传）
      samplePick: '',
      videoUrl: '',            // 原片播放地址（标定页拖动定位用）
      courtPreview: null,      // 「预览」的结果（不落盘）：每点像素误差 + 合规性
      courtPreviewed: false,   // 预览过且可用 → 才允许点「保存」
      calInfo: null,
      // ---- 「读比分牌」（自动定位 / 手动框选 + OCR）----
      sbBusy: false,
      sbResult: null,          // {ok, method, box, ocr_hit, n_crops, events, final, path, note}
      sbStartHome: null,       // 起始比分（可选；视频从半场中间开始录时填上更稳）
      sbStartAway: null,
      sbBox: ''                // 手动框选用：归一化 "x0,y0,x1,y1"；留空=自动定位
    };
  },
  computed: {
    canSubmit: function () {
      // 只有 video / jsonl 两种源（合成演示入口已移除）
      return !!String(this.form.video_path || '').trim();
    },
    /** 标点顺序：**只要求篮筐中心**；左右缘/上下沿是可选的"量筐宽"步骤。
     *
     * 用户反馈（2026-09-27）："标篮筐中心就挺准的吧，可以不需要再标上下左右四个点了"。对 ——
     * 一开始把 5 个点排成必须依次点的流程，会让人以为不点完就没法分析。
     * 现在中心是唯一必点项；四边收进「可选：量一下筐宽」里，不点也能保存、也能分析
     * （半径由后端检测器量，见 sources._visual_attempts 里的 manual_center+detector_radius）。 */
    markItems: function () {
      return [
        { name: 'rim_center', label: '篮筐中心', hint: '点篮圈正中心（只有这一项是必点的）' }
      ];
    },
    /** 可选的四边：量出半径能让"判进球的横向尺度"更准 */
    markItemsOptional: function () {
      return [
        { name: 'rim_left', label: '篮圈左缘', hint: '点篮圈最左边' },
        { name: 'rim_right', label: '篮圈右缘', hint: '点篮圈最右边' },
        { name: 'rim_top', label: '篮圈上沿', hint: '点篮圈最上边' },
        { name: 'rim_bottom', label: '篮圈下沿', hint: '点篮圈最下边' }
      ];
    },
    /** 当前模式要点的项（篮筐 5 项，只有"中心"必填 / 球场是点名清单，点够 4 个即可） */
    activeItems: function () {
      return this.markKind === 'court' ? this.courtItems : this.markItems;
    },
    /** 当前模式已点的点（**只用于篮筐模式**；球场模式的左右画面各自渲染
     *  mfPtsHere / mfPtsRight —— 用 activePts 会让另一边"看起来消失"） */
    activePts: function () {
      return this.markKind === 'court' ? this.mfPtsHere : this.markPts;
    },
    /** 当前模式的下标 */
    activeIdx: function () {
      return this.markKind === 'court' ? this.courtIdx : this.markIdx;
    },
    /** 当前画面 */
    mfCurrent: function () { return this.mfFrames[this.mfIdx] || null; },
    /** 右画面的帧（两个画面模式下才有意义；没有第二帧就退回跟左画面同一张） */
    mfCurrentRight: function () {
      if (!this.dualView) return null;
      return this.mfFrames[this.mfIdx2] || this.mfFrames[this.mfIdx] || null;
    },
    /** 两个画面模式：左右各自一张，t 相同就只有一张（还没加第二帧） */
    mfDualSplit: function () {
      return !!(this.dualView && this.mfCurrentRight &&
                this.mfCurrent && this.mfCurrentRight.t !== this.mfCurrent.t);
    },
    /** 当前画面的原始像素尺寸。
     *  画点用 SVG 的 viewBox 对齐到图片原始尺寸 —— 之前用百分比定位，
     *  依赖"容器宽度==图片宽度"这个不成立的前提，点会整体偏移。
     *  （另：这两个 computed 曾经漏定义，导致 SVG 永不渲染、点完全看不见。） */
    frameW: function () {
      if (this.markKind === 'court') return this.mfCurrent ? this.mfCurrent.w : 0;
      return this.markSize.w || 0;
    },
    frameH: function () {
      if (this.markKind === 'court') return this.mfCurrent ? this.mfCurrent.h : 0;
      return this.markSize.h || 0;
    },
    /** 本帧已标的点（只画这些 —— 别的帧的点坐标不通用） */
    mfPtsHere: function () {
      var t = this.mfCurrent ? this.mfCurrent.t : null;
      return this.mfPts.filter(function (p) { return p.t === t; });
    },
    /** **右画面**那一帧上的点。
     *
     *  为什么必须单独有一个 computed（实测 bug）：右画面以前渲染的是
     *  `activePts`，而 `activePts === mfPtsHere` 只认 `mfCurrent`（左帧）。
     *  于是"在左画面标完、切到右画面标"的时候，左画面会把右帧的点排除掉，
     *  用户看到的现象就是"我标完左画面再标右画面，左画面标的点没了"
     *  （数据其实都在，是渲染时用错了时刻）。
     */
    mfPtsRight: function () {
      var t = this.mfCurrentRight ? this.mfCurrentRight.t : null;
      return this.mfPts.filter(function (p) { return p.t === t; });
    },
    /** **当前正在点的那一面**上有几个点（撤销/清空按钮的禁用状态要用它）。
     *  以前用 mfPtsHere（只认左帧），在右画面标完点后按钮还是灰的（实测 bug）。 */
    mfActivePtsCount: function () {
      if (!this.mfDualSplit) return this.mfPts.length;
      return (this.mfActive === 1 ? this.mfPtsRight : this.mfPtsHere).length;
    },
    /** 另一个画面上的点（已不再用于渲染：右画面现在画 mfPtsRight。
     *  保留这段注释是为了记住踩过的坑：曾经右画面画的是"除左帧以外的所有点"，
     *  理由是"另一帧的坐标不通用、只当参照"，但那会让右画面的点出现在左画面上
     *  或反之，看起来就像"点丢了"。现在两边各画各的那一帧，界限清楚。） */
    /** 每个画面各自有几个点、够不够 4 个、点铺得开不开。
     *
     *  为什么必须单独显示：一个画面里只看得见半场，另一个半场的点要么在画面外、
     *  要么挤在边角上（点接近共线时单应矩阵的误差照样是 0.00m，但整份标定是错的）。
     *  所以"这个画面够不够"要当场告诉用户，而不是等解算失败才说一句"误差偏大"。 */
    mfFrameCov: function () {
      var F = this.mfFrames || [];
      return F.map(function (f) {
        var pts = this.mfPts.filter(function (p) { return p.t === f.t; });
        var tri = 0;
        for (var i = 0; i < pts.length; i++) {
          for (var j = i + 1; j < pts.length; j++) {
            for (var k = j + 1; k < pts.length; k++) {
              var a = Math.abs((pts[j].x - pts[i].x) * (pts[k].y - pts[i].y) -
                               (pts[k].x - pts[i].x) * (pts[j].y - pts[i].y)) / 2;
              if (a > tri) tri = a;
            }
          }
        }
        return { idx: F.indexOf(f), t: f.t, n: pts.length, tri: tri,
                 enough: pts.length >= 4, spread: tri >= 0.02 };
      }, this);
    },
    /** 两个画面模式下，左右画面各自标了几个点（界面顶部那两行读数用的） */
    mfLeftCov: function () {
      var t = this.mfCurrent ? this.mfCurrent.t : null;
      var c = null;
      this.mfFrameCov.forEach(function (x) { if (x.t === t) c = x; });
      return c || { t: t, n: 0, tri: 0, enough: false, spread: false };
    },
    mfRightCov: function () {
      var t = this.mfCurrentRight ? this.mfCurrentRight.t : null;
      var c = null;
      this.mfFrameCov.forEach(function (x) { if (x.t === t) c = x; });
      return c || { t: t, n: 0, tri: 0, enough: false, spread: false };
    },
    /** 哪些画面自己就够 4 个点（后端 per_frame_fit 也是按这个判的） */
    solvableFrames: function () {
      return this.mfFrameCov.filter(function (f) { return f.enough; });
    },
    /** 某个点名是否已标过（按钮变绿） */
    mfDone: function () {
      var m = {};
      this.mfPts.forEach(function (p) { m[p.name] = 1; });
      return m;
    },
    mfEnough: function () { return this.mfPts.length >= 4; },
    courtCurrent: function () { return this.courtItems[this.courtIdx] || null; },
    courtDone: function () { return this.courtIdx >= this.courtItems.length; },
    courtEnough: function () { return this.courtPts.length >= 4; },
    markCurrent: function () {
      if (this.markKind === 'court') return this.courtCurrent;
      return this.markItems[this.markIdx] || null;
    },
    markDone: function () {
      if (this.markKind === 'court') return this.courtDone;
      return this.markIdx >= this.markItems.length;
    },
    /** 当前已点几个（按模式取：多帧看 mfPts、单帧看 courtPts、篮筐看 markPts） */
    guideCount: function () {
      if (this.markKind !== 'court') return this.markPts.length;
      return this.mfFrames.length ? this.mfPts.length : this.courtPts.length;
    },
    /** 新手三步的当前状态：做到哪一步，引导就跟到哪一步（不看教程也能自己走） */
    guideSteps: function () {
      var hasVideo = !!String(this.form.video_path || '').trim();
      var hasCal = !!(this.calInfo && this.calInfo.exists);
      var job = (this.S && this.S.job) || {};
      var done = job.status === 'done';
      var rmse = (this.calInfo && this.calInfo.reproj_error_m != null)
        ? this.calInfo.reproj_error_m : null;
      return [
        { key: 'video', n: 1, title: '选一段视频', done: hasVideo,
          desc: hasVideo ? ('已选：' + this.videoBaseName)
                         : '点右按钮用机器上现成的示例素材，或选你本地的文件上传',
          cta: hasVideo ? '换一个视频' : (this.samples.length ? '用示例视频' : '选择本地文件') },
        { key: 'calib', n: 2, title: '标定球场（可选，推荐做）', done: hasCal,
          desc: hasCal
            ? ('已标定' + (rmse != null ? ('：重投影误差 ' + rmse + ' m') : ''))
            : '告诉软件"球场在画面的哪个位置"：在同一个画面上点 4~6 个地面点即可。'
              + '不标也能分析（会按「只判进球」跑）：出手 / 进 / 不中 / 未知、比分'
              + '（分值按图像估计并标注为估计值）、命中率、球员统计、文字战报都能出；'
              + '只有热图、战术图和 2 分/3 分区分需要标定。',
          cta: hasCal ? '重新标定' : '去标定' },
        { key: 'run', n: 3, title: '开始分析，然后看结果', done: done,
          desc: done
            ? '分析完成 —— 去【结果总览】看比分、命中率、投篮热图、战术与文字战报'
            : '点「开始分析」；4 分钟的视频大约要 3 分钟，跑完这里会变成 ✓',
          cta: done ? '去看结果' : '开始分析' }
      ];
    },
    guideDoneCount: function () {
      var n = 0, self = this;
      this.guideSteps.forEach(function (s) { if (s.done) n += 1; });
      return n;
    },
    /** 只取文件名，路径太长会把引导卡撑爆 */
    videoBaseName: function () {
      var p = String(this.form.video_path || '');
      return p.split(/[\\/]/).filter(Boolean).pop() || '';
    },
    /** 界面该引导用户点的**下一个点**。
     *
     * 为什么不能直接用 `courtCurrent`：多帧模式下 `courtIdx` 永远停在 0
     * （只有单帧的 onCourtClick 会自增），而点名表第一项就是"篮筐中心" ——
     * 于是无论用户刚点完哪个点，提示都一路写着"篮筐中心"（用户实测反馈：
     * "他让我每一个都去标篮筐中心"）。多帧模式必须看用户自己选的那个点名
     * （`mfTarget`），没选就返回 null，由界面提示"先去上面选一个"。
     */
    guideNext: function () {
      var i;
      if (this.markKind !== 'court') {
        var it = this.markItems[this.markIdx] || null;
        return it ? { label: it.label, hint: it.hint, idx: this.markIdx + 1 } : null;
      }
      if (this.mfFrames.length) {
        for (i = 0; i < this.courtItems.length; i++) {
          if (this.courtItems[i].name === this.mfTarget) {
            return { label: this.courtItems[i].label,
                     hint: this.courtItems[i].hint,
                     idx: this.mfPts.length + 1 };
          }
        }
        return null;                       // 还没选点名 → 走"先去上面选一个"那条提示
      }
      var c = this.courtCurrent;
      return c ? { label: c.label, hint: c.hint, idx: this.courtPts.length + 1 } : null;
    },
    /** 可选四边里哪些已经点过（按钮变绿，与球场模式的 mfDone 同一套做法） */
    hoopOptDone: function () {
      var m = {};
      this.markPts.forEach(function (p) { m[p.name] = 1; });
      return m;
    },
    /** 与 guideNext 配套的一句话：已点几个、离能保存还差几个 */
    guideDesc: function () {
      if (this.markKind !== 'court') {
        return '已点 ' + this.markPts.length + ' 个；' +
          (this.markHoop ? '篮筐中心已标好，现在就能保存（其余 4 项可选，用来量筐宽/高度）'
                         : '请先点「篮筐中心」');
      }
      var n = this.guideCount;
      var enough = this.mfFrames.length ? this.mfEnough : this.courtEnough;
      var tail = this.mfFrames.length
        ? (this.mfDualSplit
            ? '。左画面 ' + this.mfLeftCov.n + ' 个、右画面 ' + this.mfRightCov.n +
              ' 个 —— 每边各自够 4 个才算解得出（两边的点不能互相补）'
            : '。画面里看不到的点按「跳过这一项」跳过它。')
        : '';
      return '已点 ' + n + ' 个（两边合计）' +
        (enough ? '——已经够了，可以直接按「保存并解算标定」；想更准就继续点'
                : '——每边至少 4 个、建议 5~6 个') + tail;
    },
    /** 已点出的点里，篮筐中心 + 左右缘 → 算出归一化 rx；中心 + 上下沿 → ry */
    markHoop: function () {
      var self = this;
      function g(n) {
        var p = self.markPts.filter(function (q) { return q.name === n; })[0];
        return p ? { x: p.x, y: p.y } : null;
      }
      var c = g('rim_center');
      if (!c) return null;
      var l = g('rim_left'), r = g('rim_right'), t = g('rim_top'), b = g('rim_bottom');
      var rx = 0, ry = 0;
      if (l && r) rx = Math.abs(r.x - l.x) / 2;
      else if (l) rx = Math.abs(c.x - l.x);
      else if (r) rx = Math.abs(r.x - c.x);
      if (t && b) ry = Math.abs(b.y - t.y) / 2;
      else if (t) ry = Math.abs(c.y - t.y);
      else if (b) ry = Math.abs(c.y - b.y);
      // 没量到宽度就**交 0 出去**（= "不知道"），由后端用检测器量。
      // 这里以前会给一个"画面宽 2%"的猜测值 —— 那是假精度：实测 852×480 的素材真实
      // rx≈29px、猜测只有 17px，偏小 40%，会让真进球被当成"贴筐掠过"，而且不报错。
      if (!(rx > 0)) { rx = 0; ry = 0; }
      else if (!(ry > 0)) ry = rx * 0.42;   // 上下沿没标时按篮圈扁率估算（只影响薄筐的下落下限）
      return { cx: c.x, cy: c.y, rx: rx, ry: ry, measured: rx > 0 };
    },
    /** 表单里有没有可用视频（取帧、标点、开工都必须先有它） */
    hasVideoPath: function () {
      return !!String((this.form && this.form.video_path) || '').trim();
    },
    statusTag: function () {
      return { queued: 'info', running: 'warning', done: 'success', error: 'danger' }[this.job.status] || 'info';
    },
    statusText: function () {
      return { queued: '排队中', running: '分析中', done: '已完成', error: '失败' }[this.job.status] || '未开始';
    },
    percent: function () { return Math.round((Number(this.job.progress) || 0) * 100); },
    /** 标定"吻合度"读数：分析端收不收这份标定的硬门槛是 ≥1.25。
     *  放在 computed 里是因为模板要拿它判颜色/文案。 */
    fitRatio: function () {
      var r = this.mfResult || this.courtResult;
      var f = r && r.calibration_fit;
      var v = f && f.ratio;
      return (typeof v === 'number') ? v : 0;
    },
    /** 哪些点名在某一幅画面里根本看不见（用解出来的 H 反投影判断）。
     *
     *  为什么要这个：这台机位只拍得到半场时，「另一侧的底线角」在画面里
     *  压根不存在，用户硬点只能靠猜 —— 误差就是这么来的（实测：用户把
     *  "底线右角"点成了罚球区右角，偏 9m）。解算后用同一份 H 就能算出
     *  每个特征点该落在画面的哪个位置，落在画面外的就是"看不见"。
     *
     *  ⚠️ 必须放在 computed：模板里当值用（offFrameNames.length）。
     */
    offFrameNames: function () {
      var r = this.mfResult || this.courtResult;
      var H = r && r.H;
      if (!H || this.markKind !== 'court') return [];
      var items = this.courtItems || [];
      var frames = (this.mfFrames || []).slice();
      if (!frames.length && this.frameW) {
        frames = [{ t: null, w: this.frameW, h: this.frameH }];
      }
      if (!frames.length) return [];
      var out = [];
      items.forEach(function (it) {
        var m = it.dst;                 // 后端 /api/court_landmarks 给的球场坐标（米）
        if (!m || m.length < 2) return;
        var w = H[2][0] * m[0] + H[2][1] * m[1] + H[2][2];
        if (Math.abs(w) < 1e-9) { out.push(it.label); return; }
        var u = (H[0][0] * m[0] + H[0][1] * m[1] + H[0][2]) / w;
        var v = (H[1][0] * m[0] + H[1][1] * m[1] + H[1][2]) / w;
        if (!isFinite(u) || !isFinite(v)) { out.push(it.label); return; }
        // 至少得在**一幅**画面里能看见，否则这个点名就是"点了也白点"。
        // 用 6% 的宽容边框：标定本身有误差，紧贴边缘的点不算"看不见"。
        var seen = frames.some(function (f) {
          var pad = 0.06;
          return u > -pad * f.w && u < f.w * (1 + pad) &&
                 v > -pad * f.h && v < f.h * (1 + pad);
        });
        if (!seen) out.push(it.label);
      });
      return out;
    }
  },
  mounted: function () {
    this.refreshJobs();
    this.fetchHealth();
    this.loadSamples();          // 上手第一步要给得出"能直接用的视频"，不让新用户自己找路径
    try {
      this.guideOpen = localStorage.getItem('aihoop-guide-dismissed') !== '1';
    } catch (e) { this.guideOpen = true; }
  },
  beforeUnmount: function () {
    if (this.watcher) this.watcher.close();
  },
  methods: {
    /* ------------------------------------------------------------------
       在画面上标篮筐（不用敲命令行）
       ------------------------------------------------------------------ */
    openMark: function () {
      var self = this;
      this.markKind = 'hoop';
      var v = String(this.form.video_path || '').trim();
      if (!v) { this.$message.warning('先选一个视频（上传或填路径）'); return; }
      this.markOpen = true;
      this.markIdx = 0;
      this.markPts = [];
      this.loadPlayer();                 // 挂上原片，用户可以直接拖着定位
      this.loadMarkFrame();
      window.API.getMarks(v).then(function (r) {
        self.savedMarks = (r && r.exists) ? r.marks : null;
      }).catch(function () { self.savedMarks = null; });
    },
    /** 打开「标球场」：抽 N 个画面，多帧累加标点 */
    openCourt: function () {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      if (!v) { this.$message.warning('先选一个视频'); return; }
      this.markKind = 'court';
      this.markOpen = true;
      this.mfFrames = [];
      this.mfIdx = 0;
      this.mfIdx2 = 0;
      this.mfActive = 0;
      this.mfPts = [];
      this.mfTarget = '';
      this.mfResult = null;
      this.markImg = '';
      this.loadPlayer();                 // 挂上原片：可以拖着找到"场地看得最全"的那一帧再加进来
      // 点名表**只用本地这张表**（按"摄像机拍的是哪一侧"取 7 个），不再拉后端那 17 个。
      // 后端那 17 个里有一半是"另一侧/中圈那一头"的点，在这种机位下画面里根本不存在，
      // 列出来只会让用户去点不存在的东西（实测：把"底线右角"点成了罚球区右角，偏 9.17m）。
      this.setCourtSide(this.courtSide);
      // 默认直接走"自动挑同一镜头的两帧"：用户不用自己拖，也就不会拖到两个镜头里。
      // （原来的"在 mfCenter 附近抽 6 帧"已删掉：那批帧是**同一秒附近**的，
      //   对"两侧半场各标一遍"没意义，还会和自动挑的两帧抢 mfFrames。）
      this.$message.info('正在判断镜头有没有切…（约 3 秒）');
      this.pickStableFrames();
      window.API.getCalibration(v).then(function (r) {
        self.calInfo = (r && r.exists) ? r : null;
      }).catch(function () { self.calInfo = null; });
    },
    /** 读回"这段视频有没有标定"（保存/撤销之后要刷新状态与 revision） */
    loadCalInfo: function () {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      if (!v) { this.calInfo = null; return; }
      window.API.getCalibration(v).then(function (r) {
        self.calInfo = (r && r.exists) ? r : null;
      }).catch(function () { self.calInfo = null; });
    },
    /** 把原片挂到弹窗里的播放器上（拖动定位用；后端 /api/video 支持 Range） */
    loadPlayer: function () {
      var v = String(this.form.video_path || '').trim();
      this.videoUrl = v ? window.API.videoUrl(v) : '';
    },
    /** 数字框改了秒数 → 播放器跟着跳过去（两边保持同步） */
    seekVideo: function (t) {
      var el = this.$refs.markVideo;
      if (!el) { return; }
      try { el.currentTime = Math.max(0, Number(t) || 0); } catch (e) { /* 元数据还没到就忽略 */ }
    },
    /** 把某一时刻的画面**加进帧列表**（球场模式用：想标哪一帧就加哪一帧）     *
     *  `onReady(t)` 可选：加完帧后回调，两个画面模式用它把新帧直接放到对应半边
     *  （不然用户"加了一帧却不知道它在哪个画面里"）。 */
    addFrameAtTime: function (t, onReady) {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      var tt = Number(Number(t).toFixed(2));
      window.API.getFrame(v, tt).then(function (r) {
        self.mfFrames.push({ t: tt, w: r.w, h: r.h, image: r.image });
        self.mfIdx = self.mfFrames.length - 1;
        self.mfTarget = '';
        self.mfResult = null;
        if (typeof onReady === 'function') {
          onReady(tt);
          return;                     // 由回调给出更贴切的提示
        }
        self.$message.success('已加入 t=' + tt + 's 这一帧（共 ' + self.mfFrames.length +
          ' 帧）—— 现在可以在它上面标点');
      }).catch(function (e) {
        self.$message.error('取帧失败：' + (e && e.message ? e.message : e));
      });
    },
    /** 「用当前画面取帧」：读播放器当前位置 → 篮筐模式换取帧图、球场模式把这一帧加进列表 */
    useVideoMoment: function () {
      var el = this.$refs.markVideo;
      var t = (el && el.currentTime) ? el.currentTime : this.markAt;
      if (this.markKind === 'court') {
        var self = this;
        var side = this.mfDualSplit ? this.mfActive : 0;
        this.addFrameAtTime(t, function (tt) {
          var i = self.mfFrames.length - 1;
          if (self.dualView) {          // 两个画面模式：放到当前那一半，并切过去
            if (side === 1) self.mfIdx2 = i; else self.mfIdx = i;
            self.mfActive = side;
          }
          self.$message.success('已把 t=' + tt + 's 的这幅画面放到' +
            (self.dualView ? (side === 0 ? '左' : '右') : '') +
            '画面 —— 现在可以在它上面点这个半场的点');
        });
        return;
      }
      this.markAt = Number(Number(t).toFixed(2));
      this.loadMarkFrame(false);
      this.$message.success('已取 t=' + this.markAt + 's 的画面，可以在它上面标点了');
    },
    /** 在指定时刻附近重抽（这几帧属于同一镜头，点才能叠加） */
    mfResample: function () {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      if (!v) return;
      window.API.getFrames(v, 6, this.mfCenter, this.mfSpan).then(function (d) {
        // 换批画面：只保留"新画面里还有同一时刻"的点。
        // 不能一律清空 —— 用户可能已经辛辛苦苦标了一个半场，重抽一次就全没了；
        // 也不能一律留着 —— 画面换了以后那些点会飘到别的地方去（实测反馈的"点跟着漂"）。
        var frames = (d && d.frames) || [];
        var kept = 0, lost = 0;
        if (self.mfPts.length) {
          var ts = {};
          frames.forEach(function (f) { ts[f.t] = 1; });
          var before = self.mfPts.length;
          self.mfPts = self.mfPts.filter(function (p) { return !!ts[p.t]; });
          kept = self.mfPts.length;
          lost = before - kept;
        }
        self.mfFrames = frames;
        self.mfIdx = 0;
        self.mfIdx2 = Math.min(1, Math.max(0, self.mfFrames.length - 1));
        self.mfActive = 0;
        self.$message.success('抽到 ' + self.mfFrames.length + ' 帧（t=' +
          (self.mfFrames.length ? self.mfFrames[0].t + '~' +
           self.mfFrames[self.mfFrames.length - 1].t : '') + 's）' +
          (lost ? ('；' + lost + ' 个点所在的画面已经不在这批里，已丢弃（保留 ' +
                   kept + ' 个）') : ''));
      }).catch(function (e) {
        self.$message.error('抽帧失败：' + (e && e.message ? e.message : e));
      });
    },
    mfPrev: function () { if (this.mfIdx > 0) this.mfIdx -= 1; },
    mfNext: function () {
      if (this.mfIdx < this.mfFrames.length - 1) this.mfIdx += 1;
      else this.$message.info('已经是最后一个画面了');
    },
    mfPick: function (item) { this.mfTarget = item.name; },
    /** 当前要往哪个画面的帧上落点（左右各自一张画面时以点击的那张为准） */
    mfClickT: function (ev) {
      var el = ev && ev.currentTarget;
      var side = (el && el.getAttribute) ? el.getAttribute('data-side') : null;
      if (side === '1' && this.mfCurrentRight) { this.mfActive = 1; return this.mfCurrentRight.t; }
      if (side === '0' && this.mfCurrent) { this.mfActive = 0; return this.mfCurrent.t; }
      return this.mfCurrent ? this.mfCurrent.t : null;
    },
    /** 在画面上点一个点（必须是先选中了某个特征点）
     *
     *  左右两个画面各有各的坐标系：点落在**哪一张画面**上，就记那一张的 t。
     *  以前这里只认 mfCurrent，于是"在右边画面点的点"会被记到左边画面上，
     *  坐标整体跑到另一边去。 */
    mfClick: function (ev) {
      if (!this.mfCurrent) return;
      var t = this.mfClickT(ev);
      if (t === null) return;
      var el = (ev.target && ev.target.tagName === 'IMG') ? ev.target
                                                          : ev.currentTarget;
      var box = el.getBoundingClientRect();
      var x = Math.max(0, Math.min(1, (ev.clientX - box.left) / box.width));
      var y = Math.max(0, Math.min(1, (ev.clientY - box.top) / box.height));
      if (!this.mfTarget) {
        this.$message.warning('先在下面选一个特征点（比如「篮筐中心」），再在画面里点它位置'
          + (this.mfDualSplit ? '—— 选一次就能在两边各点一下' : ''));
        return;
      }
      var item = null;
      for (var i = 0; i < this.courtItems.length; i++) {
        if (this.courtItems[i].name === this.mfTarget) { item = this.courtItems[i]; }
      }
      // 同名点只保留最后一次，但**只在同一个画面里替换**：
      // 「底线左角」在左半场和右半场各出现一次，那是两个不同的点；
      // 以前按名字全局去重，点第二半场时会把第一半场的点删掉
      // （用户看到的现象就是"点完所有点一个都不显示"）。
      var self = this;
      var sameT = this.mfPts.filter(function (p) {
        return p.t === t && p.name === self.mfTarget; });
      var extra = sameT.length ? '（改到了新位置）' : '（这也是一幅画面里的第 ' +
        (this.mfPtsHere.length + 1) + ' 个点）';
      this.mfPts = this.mfPts.filter(function (p) {
        return !(p.t === t && p.name === self.mfTarget); });
      this.mfPts.push({ t: t, name: this.mfTarget,
                        label: item ? item.label : this.mfTarget, x: x, y: y });
      this.mfTarget = '';
      // ★ 加了新点就清掉上一次的解算结果 —— 否则界面一直显示"旧报错"，
      // 用户会以为补点之后还是不行（实测踩到：先点 4 个点报错，再补到 6 个，
      // 红框仍写着"只点了 4 个点"，用户就卡在这里了）
      this.mfResult = null;
      // 立刻回显坐标：用户一眼就能看出记的是不是鼠标点的地方
      this.$message.success('已记录 ' + (item ? item.label : '') + extra +
        ' → (' + x.toFixed(3) + ', ' + y.toFixed(3) + ')' +
        (this.mfDualSplit ? ('（' + (this.mfActive === 1 ? '右' : '左') + '画面）') : ''));
    },
    mfUndo: function () {
      // 两个画面分开撤销：撤销要退的是**当前这个画面**上刚点的那一个
      if (this.mfDualSplit) {
        var t = this.mfActive === 1
          ? (this.mfCurrentRight ? this.mfCurrentRight.t : null)
          : (this.mfCurrent ? this.mfCurrent.t : null);
        var found = false;
        for (var i = this.mfPts.length - 1; i >= 0; i--) {
          if (this.mfPts[i].t === t) { this.mfPts.splice(i, 1); found = true; break; }
        }
        if (!found) this.$message.info(this.mfActive === 1 ? '右画面还没有点' : '左画面还没有点');
      } else if (this.mfPts.length) {
        this.mfPts.pop();
      }
      this.mfResult = null;      // 撤销后旧结果失效
    },
    /** 清空 = 清掉**当前画面**上的点（想全清再点右边那个「全部清空」） */
    mfClear: function () {
      var self = this;
      if (this.mfDualSplit) {
        var t = this.mfActive === 1
          ? (this.mfCurrentRight ? this.mfCurrentRight.t : null)
          : (this.mfCurrent ? this.mfCurrent.t : null);
        var n = this.mfPts.length;
        this.mfPts = this.mfPts.filter(function (p) { return p.t !== t; });
        this.mfTarget = '';
        this.mfResult = null;
        this.$message.success('已清空' + (this.mfActive === 1 ? '右' : '左') + '画面上的 ' +
          (n - this.mfPts.length) + ' 个点（另一边的点还在）');
        return;
      }
      this.mfPts = []; this.mfTarget = ''; this.mfResult = null;
    },
    /** 全部清空（左右两边一起清） */
    mfClearAll: function () {
      this.mfPts = []; this.mfTarget = ''; this.mfResult = null;
      this.$message.success('已清空所有画面上的点');
    },
    /* ---------------- 左右两个画面（各标一个半场） ---------------- */
    /** 某个画面（0=左 1=右）上已标的点（下面那份文字清单用） */
    mfFramePts: function (panel) {
      var f = panel === 1 ? this.mfCurrentRight : this.mfCurrent;
      var t = f ? f.t : null;
      return this.mfPts.filter(function (p) { return p.t === t; });
    },
    /** 切换"正在点哪个画面" —— 也可以直接在画面里点一下自动切 */
    setMfActive: function (i) {
      if (i === 1 && !this.mfCurrentRight) return;
      this.mfActive = i;
    },
    /** 切换「左右两个画面 / 一张一张切」 */
    setDualView: function (v) {
      this.dualView = !!v;
      if (this.dualView) {
        this.mfActive = 0;
        if (this.mfFrames.length > 1 && this.mfIdx2 === this.mfIdx) {
          this.mfIdx2 = (this.mfIdx + 1) % this.mfFrames.length;
        }
        if (this.mfFrames.length < 2) {
          this.$message.info('现在左右两边是同一幅画面：拖动下面的原片到「看另一侧半场」' +
            '的那一刻，再按「把这帧放到右画面」，左右就各自一张了');
        }
      }
    },
    /** 把某一幅已抽好的画面放到左/右画面（用户不用记"第几帧"） */
    assignToPanel: function (panel, idx) {
      idx = Number(idx);
      if (panel === 1) { this.mfIdx2 = idx; this.mfActive = 1; }
      else { this.mfIdx = idx; this.mfActive = 0; }
      this.mfResult = null;
    },
    /** 下拉框直接用的两个入口（面板号写死，模板里不必写内联函数） */
    setLeftFrame: function (idx) { this.assignToPanel(0, idx); },
    setRightFrame: function (idx) { this.assignToPanel(1, idx); },
    /** 右画面直接对着原片取一帧（"我看的是另一侧半场"那一刻） */
    addRightFrame: function () {
      var self = this;
      var el = this.$refs.markVideo;
      var t = (el && el.currentTime) ? el.currentTime : this.mfCenter;
      this.addFrameAtTime(t, function (tt) {
        self.mfIdx2 = self.mfFrames.length - 1;
        self.mfActive = 1;
        self.$message.success('右画面已换成 t=' + tt + 's 这一帧 —— 现在在它上面点另一侧半场的点');
      });
    },
    /** 把已标的点整理成 /api/calibrate_multi 的入参：每幅画面各自一组。
     *
     *  左右两个画面的坐标各归各的，**不能混在一个 frames 项里** ——
     *  后端就是按 t 分组、逐帧拟合来判断"这幅画面自己够不够 4 个点"的。
     *  返回 null 表示拦下了（并已经把原因说给用户了），调用方直接 return。 */
    courtMultiPayload: function () {
      var byT = {};
      this.mfPts.forEach(function (p) {
        byT[p.t] = byT[p.t] || { t: p.t, landmarks: {} };
        // 用 {value,label} 形式带上**界面上显示的名字**：后端点名诊断会原样回传，
        // 这样它说"最可疑的是「篮筐中心」"时，你能对上自己刚才按的那个按钮
        // （后端自己的措辞可能不一样，比如把这一侧的筐叫"另一端的篮筐中心"）。
        byT[p.t].landmarks[p.name] = { value: [p.x, p.y], label: p.label };
      });
      var frames = Object.keys(byT).map(function (k) { return byT[k]; });
      var solvable = 0, best = 0, bestT = null, okT = [];
      frames.forEach(function (f) {
        var n = Object.keys(f.landmarks || {}).length;
        if (n > best) { best = n; bestT = f.t; }
        if (n >= 4) { solvable += 1; okT.push(f.t); }
      });
      if (solvable === 0) {
        // 这是最常见的翻车点：点全撒在几幅画面上，**没有一幅凑够 4 个**。
        // 直接把话说透，别让用户对着"误差偏大"猜。
        var detail = frames.map(function (f) {
          return 't=' + f.t + 's 有 ' + Object.keys(f.landmarks || {}).length + ' 个';
        }).join('、');
        this.$message.warning('每一幅画面都不够 4 个点（' + (detail || '还没有点') +
          '）。单应矩阵要求**同一幅画面里至少 4 个点**：左边画面点够 4 个、右边画面再点够 4 个。');
        return null;
      }
      var note = solvable > 1
        ? ('有 ' + solvable + ' 幅画面各自都够 4 个点，用其中点最多的那一幅（t=' +
           bestT + 's，' + best + ' 个点）解算')
        : ('只有 t=' + bestT + 's 那一幅画面够 4 个点，用它解算');
      return { frames: frames, note: note, solvable: solvable, best: best };
    },
    /** 保存并解算（多帧累加）
     *
     * two-step：`confirm:false` 先预览（不落盘），`confirm:true` 才保存并带 revision
     * 做乐观锁（移植自旧版标定页）。宽容规则在服务端：只拒绝了"数学上无意义"的
     * 退化点位和"篮筐投偏 >6m"；只点 4 个点、有点矛盾、篮筐偏 3~6m 都**允许保存**，
     * 但会标 position_unverified，位置结论（热区/战术图）由分析端关掉。 */
    previewCourtMulti: function () {
      var self = this;
      if (!this.mfEnough) {
        this.$message.warning('至少要点 4 个特征点（现在 ' + this.mfPts.length +
          ' 个）；左右两个画面**各自**都要有 4 个以上才是稳的');
        return;
      }
      var got = this.courtMultiPayload();
      if (!got) return;
      this.markSaving = true;
      window.API.calibrateMulti({
        video_path: String(this.form.video_path).trim(), frames: got.frames,
        confirm: false
      }).then(function (r) {
        self.markSaving = false;
        self.mfResult = r;
        self.mfPreviewed = !!(r && r.ok);
        self.refreshOverlay();
        if (r.ok) {
          self.$message.success('预览通过：平均误差 ' + r.rmse_m + ' m' +
            (r.hoop_error_m != null ? ('，篮筐投影偏 ' + r.hoop_error_m + ' m') : '') +
            '（' + got.note + '）—— 确认没问题再点保存');
        } else {
          self.$message.warning('这份点法还不能用：' + String(r.note || '').slice(0, 60));
        }
      }).catch(function (e) {
        self.markSaving = false;
        self.$message.error('解算失败：' + (e && e.message ? e.message : e));
      });
    },
    saveCourtMulti: function () {
      var self = this;
      if (!this.mfEnough) {
        this.$message.warning('至少要点 4 个特征点（现在 ' + this.mfPts.length +
          ' 个）；左右两个画面**各自**都要有 4 个以上才是稳的');
        return;
      }
      var got = this.courtMultiPayload();
      if (!got) return;
      this.markSaving = true;
      window.API.calibrateMulti({
        video_path: String(this.form.video_path).trim(), frames: got.frames,
        confirm: true,
        revision: (this.calInfo && this.calInfo.revision != null)
          ? this.calInfo.revision : null
      }).then(function (r) {
        self.markSaving = false;
        self.mfResult = r;
        self.refreshOverlay();
        if (r.saved) {
          if (r.position_unverified) {
            self.$message.warning('标定已保存，但位置结论会被判为**未校验**：' +
              String(r.note || '').slice(0, 80));
          } else {
            self.$message.success('标定成功：' + r.n_points + ' 个点 / ' +
              r.n_frames + ' 个画面，平均误差 ' + r.rmse_m + ' m（' + got.note +
              '）—— 分析时会自动使用');
          }
          self.loadCalInfo();
          self.markOpen = false;
        } else {
          self.$message.error('未保存：' + String(r.note || '点位不可用').slice(0, 90));
        }
      }).catch(function (e) {
        self.markSaving = false;
        self.$message.error('解算失败：' + (e && e.message ? e.message : e));
      });
    },
    /* ---------------- 标定自检：把算出来的球场线画回画面 ----------------
     *
     * 背景（实测争议）：用户标了 5 个点，界面说"误差 4.2m、有点互相矛盾"，
     * 用户觉得"我标得明显是对的"。而数学上 **5 个点里任取 4 个都能精确解出 H**
     * （误差恒为 0），所以那个数字本身不能证明谁对谁错。
     * 唯一能被肉眼验证的办法：把这份 H 算出来的球场线**投影回画面** ——
     * 对得上就是对，对不上错在哪一条线上一眼就看到。
     */
    /** 球场线在"分析坐标"下的折线（米）：用于投影自检 */
    _courtPolylines: function () {
      var lines = [];
      var i, x, y, pts, arc;
      // 边线 x=±7.5
      for (i = 0; i < 2; i++) {
        x = i ? 7.5 : -7.5;
        pts = [];
        for (y = -14; y <= 14.01; y += 0.5) pts.push([x, y]);
        lines.push(pts);
      }
      // 底线 y=±14、中线 y=0、罚球线（距底线 5.8）
      [-14, 14, 0, -8.2, 8.2].forEach(function (yy) {
        var p = [];
        for (var xx = -7.5; xx <= 7.51; xx += 0.25) p.push([xx, yy]);
        lines.push(p);
      });
      // 罚球区两侧竖线（宽 4.9）
      [-2.45, 2.45].forEach(function (xx) {
        lines.push([[xx, -14], [xx, -8.2]]);
        lines.push([[xx, 8.2], [xx, 14]]);
      });
      // 三分弧（两侧，半径 6.75，圆心 = 篮筐 (0, ±12.425)）
      [-1, 1].forEach(function (sgn) {
        arc = [];
        for (i = 0; i <= 48; i++) {
          var a = -Math.PI / 2 + i * (Math.PI / 48);
          arc.push([6.75 * Math.cos(a), sgn * 12.425 + sgn * 6.75 * Math.sin(a)]);
        }
        lines.push(arc);
      });
      return lines;
    },
    /** 用一份 H 把球场线投影到画面像素坐标 */
    _buildOverlay: function (H, W, Hh) {
      if (!H || !W || !Hh) { this.overlayLines = []; return; }
      var out = [];
      this._courtPolylines().forEach(function (line) {
        var pix = [];
        line.forEach(function (p) {
          var x = p[0], y = p[1];
          var w = H[2][0] * x + H[2][1] * y + H[2][2];
          if (Math.abs(w) < 1e-9) return;
          var u = (H[0][0] * x + H[0][1] * y + H[0][2]) / w;
          var v = (H[1][0] * x + H[1][1] * y + H[1][2]) / w;
          // 只留画面附近的点，避免远处投影飞出去把 SVG 撑爆
          if (u > -W || u < 2 * W) pix.push([u, v]);
        });
        if (pix.length > 1) out.push(pix);
      });
      this.overlayW = W;
      this.overlayH = Hh;
      this.overlayLines = out;
    },
    /** 预览/解算回来之后刷新自检叠加层（用的是后端返回的那份 H） */
    refreshOverlay: function () {
      var r = this.mfResult || this.courtResult;
      var H = r && r.H;
      // 「点吸附精修」被采纳时，存下来的标定用的是**吸附后的点**。
      // 所以画面上必须也把吸附后的位置画出来（画成空心圈），
      // 否则用户看到的点和他实际存下来的标定不一致 —— 会以为工具偷偷改了位置。
      var snapPts = null;
      var sn = r && r.snap;
      if (sn && sn.accepted && sn.landmarks) {
        var byT = {};
        (this.mfPts || []).forEach(function (p) { byT[p.t] = byT[p.t] || {}; byT[p.t][p.name] = p; });
        snapPts = [];
        Object.keys(byT).forEach(function (tt) {
          Object.keys(byT[tt]).forEach(function (nm) {
            var v = sn.landmarks[nm];
            if (v) { snapPts.push({ t: Number(tt), name: nm, x: v[0], y: v[1] }); }
          });
        });
      }
      this.snapPts = snapPts || [];
      if (!this.showCourtOverlay || !H) { this.overlayLines = []; return; }
      var t = null, frame = null, i;
      if (this.mfFrames && this.mfFrames.length) {
        t = this.mfResult && this.mfResult.best_frame_t;
        frame = this.mfCurrent;
        if (t != null) {
          for (i = 0; i < this.mfFrames.length; i++) {
            if (Math.abs(this.mfFrames[i].t - t) < 1e-6) { frame = this.mfFrames[i]; }
          }
        }
      }
      var W = frame ? frame.w : this.markSize.w;
      var Hh = frame ? frame.h : this.markSize.h;
      this._buildOverlay(H, W, Hh);
    },
    /** 本帧上"吸附后"的点（精修被采纳时才非空） */
    snapPtsHere: function () {
      var t = this.mfCurrent ? this.mfCurrent.t : null;
      return (this.snapPts || []).filter(function (p) { return p.t === t; });
    },
    snapPtsRight: function () {
      var t = this.mfCurrentRight ? this.mfCurrentRight.t : null;
      return (this.snapPts || []).filter(function (p) { return p.t === t; });
    },
    toggleCourtOverlay: function () {
      this.showCourtOverlay = !this.showCourtOverlay;
      this.refreshOverlay();
    },
    /** 切换"摄像机拍的是哪一侧半场" —— 只影响点名表显示哪 7 个点 */
    setCourtSide: function (side) {
      this.courtSide = (side === 'right') ? 'right' : 'left';
      this.courtItems = (this.courtSideNames[this.courtSide] || []).slice();
      // 换了这一侧，已标的点对应的物理位置就全变了 —— 必须清掉，别让用户
      // 拿着上一侧的点去解算（那会得到一份"看着成功、其实差 14m"的标定）。
      if (this.mfPts && this.mfPts.length) {
        this.mfPts = [];
        this.mfTarget = '';
        this.mfResult = null;
        this.$message.info('已切换到另一侧半场，原标点已清空（两半场的点是不同物理位置）');
      }
    },
    /** 用**指定时刻**的两帧装进左右画面（同一镜头才行）。
     *
     *  为什么要有这一条：标定页原来是"用户拖播放器 → 每边各加一帧"，
     *  很容易把两帧取在**不同镜头**上（实测 t=4.75s / t=58.5s，中间镜头切过），
     *  两帧不同机位 -> 合并解必然互相矛盾 -> 界面只会报"不在同一镜头/点互相矛盾"，
     *  用户根本不知道问题出在抽帧。现在由后端自动挑同一段镜头的首尾两帧。 */
    loadFramesAt: function (t1, t2) {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      if (!v) { return; }
      this.markSaving = false;
      this.mfFrames = [];
      this.mfIdx = 0;
      this.mfIdx2 = 0;
      this.mfActive = 0;
      this.mfPts = [];
      this.mfTarget = '';
      this.mfResult = null;
      this.overlayLines = [];
      var jobs = [[t1, 0], [t2, 1]];
      jobs.forEach(function (it) {
        var t = it[0], slot = it[1];
        window.API.getFrame(v, t).then(function (r) {
          var fr = { t: Number(Number(t).toFixed(2)), w: r.w, h: r.h,
                     image: r.image, slot: slot };
          self.mfFrames.push(fr);
          if (self.mfFrames.length === 1) { self.mfIdx = 0; }
          if (self.mfFrames.length === 2) {
            // 按 slot 摆好：左画面 = 第一帧、右画面 = 第二帧
            self.mfFrames.sort(function (a, b) { return a.slot - b.slot; });
            self.mfIdx = 0;
            self.mfIdx2 = 1;
            self.$message.success('已装好两帧（t=' + self.mfFrames[0].t +
              's / t=' + self.mfFrames[1].t + 's，同一镜头）：' +
              '左画面标一侧半场、右画面标另一侧，每边点够 4~6 个点');
          }
        }).catch(function (e) {
          self.$message.error('取帧失败：' + (e && e.message ? e.message : e));
        });
      });
    },
    /** 自动挑"同一段镜头"的两帧（后端用 ORB 内点率判镜头有没有切） */
    pickStableFrames: function () {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      if (!v) { return; }
      this.$message.info('正在判断镜头有没有切…');
      window.API.framesStable(v, 8).then(function (r) {
        if (!r || !r.ok) {
          self.$message.warning((r && r.note) || '没能自动认出连续镜头');
          return;
        }
        self.loadFramesAt(r.t1, r.t2);
        self.$message.success(r.note);
      }).catch(function (e) {
        self.$message.error('自动挑帧失败：' + (e && e.message ? e.message : e));
      });
    },
    /** 球场模式下点一个点 */
    onCourtClick: function (ev) {
      if (this.mfFrames.length) { this.mfClick(ev); return; }
      if (this.courtDone || !this.courtCurrent) return;
      // 用**图片元素**的框做基准（不是容器）：容器是 inline-block + max-width，
      // 在弹窗里它的宽度可能比图片显示宽度大，用容器归一化会导致绿圈整体偏移。
      var el = (ev.target && ev.target.tagName === 'IMG') ? ev.target
                                                          : ev.currentTarget;
      var box = el.getBoundingClientRect();
      var x = Math.max(0, Math.min(1, (ev.clientX - box.left) / box.width));
      var y = Math.max(0, Math.min(1, (ev.clientY - box.top) / box.height));
      this.courtPts.push({ name: this.courtCurrent.name,
                           label: this.courtCurrent.label, x: x, y: y });
      this.courtIdx += 1;
    },
    /** 读比分牌：自动定位（或手动框选）+ 放大 OCR → 得分事件文件 */
    readScoreboard: function () {
      var self = this;
      var v = String(this.videoPath || this.form.video_path || '').trim();
      if (!v) { this.$message.warning('先填视频路径（或上传视频）'); return; }
      var payload = { video_path: v, step: 0.5, zoom: 4.0 };
      var box = [];
      String(this.sbBox || '').split(',').forEach(function (x) {
        var n = parseFloat(x);
        if (!isNaN(n)) box.push(n);
      });
      if (box.length === 4) payload.box = box;
      var sh = parseInt(this.sbStartHome, 10);
      var sa = parseInt(this.sbStartAway, 10);
      if (!isNaN(sh) && !isNaN(sa)) payload.start = { home: sh, away: sa };
      this.sbBusy = true;
      this.sbResult = { loading: true };
      this.log('POST /api/scoreboard/ocr ' + JSON.stringify(payload));
      window.API.scoreboardOcr(payload).then(function (r) {
        self.sbBusy = false;
        self.sbResult = r;
        self.form.scoreboard_events = r.path || '';
        self.$message.success('比分牌读出 ' + r.n_events + ' 个得分事件，最终 ' +
          r.final.home + ' : ' + r.final.away);
        self.log('比分牌：' + r.note);
      }).catch(function (e) {
        self.sbBusy = false;
        self.sbResult = { ok: false, note: (e && e.message) || String(e) };
        self.$message.error('读比分牌失败：' + ((e && e.message) || e));
      });
    },
    /** 切视频后，看这段视频是否已经读过比分牌 */
    loadScoreboardEvents: function () {
      var self = this;
      var v = String(this.videoPath || this.form.video_path || '').trim();
      if (!v) return;
      window.API.scoreboardEvents(v).then(function (r) {
        if (r && r.exists) {
          self.form.scoreboard_events = r.path;
          self.sbResult = { ok: true, saved: true, path: r.path, final: r.final,
                            n_events: r.n_events, method: r.method,
                            note: '这段视频已经读过比分牌（' + r.n_events +
                                  ' 个得分事件，最终 ' + (r.final || {}).home +
                                  ' : ' + (r.final || {}).away + '），分析时会用它' };
        }
      }).catch(function () { /* 没读过就算了，不打扰用户 */ });
    },
    /** 提交球场标定
     *
     * 两段式（移植自旧版标定页）：先"预览"（confirm:false，不落盘），
     * 让用户看清每个点差多少像素、篮筐投影偏多少米，再决定要不要存。
     * 以前只有"保存"一个按钮，点错了只能反复覆盖，用户既不知道错在哪、
     * 也不知道能不能重来。 */
    previewCourt: function () {
      var self = this;
      if (!this.courtEnough) {
        this.$message.warning('至少要点 4 个场地特征点（现在 ' +
          this.courtPts.length + ' 个）');
        return;
      }
      var lm = {};
      this.courtPts.forEach(function (p) { lm[p.name] = [p.x, p.y]; });
      this.markSaving = true;
      window.API.saveCourtMarks({
        video_path: String(this.form.video_path).trim(),
        at: this.markAt, landmarks: lm, confirm: false
      }).then(function (r) {
        self.markSaving = false;
        self.courtResult = r;
        self.courtPreview = r;
        self.courtPreviewed = !!(r && r.ok);
      }).catch(function (e) {
        self.markSaving = false;
        self.$message.error('解算失败：' + (e && e.message ? e.message : e));
      });
    },
    saveCourt: function () {
      if (this.mfFrames.length) { this.saveCourtMulti(); return; }
      var self = this;
      if (!this.courtEnough) {
        this.$message.warning('至少要点 4 个场地特征点（现在 ' +
          this.courtPts.length + ' 个）');
        return;
      }
      var lm = {};
      this.courtPts.forEach(function (p) { lm[p.name] = [p.x, p.y]; });
      this.markSaving = true;
      window.API.saveCourtMarks({
        video_path: String(this.form.video_path).trim(),
        at: this.markAt, landmarks: lm,
        confirm: true,
        revision: (this.calInfo && this.calInfo.revision != null)
          ? this.calInfo.revision : null
      }).then(function (r) {
        self.markSaving = false;
        self.courtResult = r;
        self.courtPreview = r;
        if (r.saved) {
          if (r.position_unverified) {
            self.$message.warning('标定已保存，但篮筐投影偏 ' + r.hoop_error_m +
              ' m —— 位置类结论（热区/战术图）会被判为未校验；' +
              '想用位置结论请重新标一次');
          } else {
            self.$message.success('标定已保存：' + r.n_points + ' 个点，重投影误差 ' +
              r.rmse_m + ' m（最大单点 ' + r.reproj_err_px + ' px）');
          }
          self.markOpen = false;
          self.loadCalInfo();
        } else {
          self.$message.error(r.note || '标定未通过校验，未保存');
        }
      }).catch(function (e) {
        self.markSaving = false;
        self.$message.error('标定失败：' + (e && e.message ? e.message : e));
      });
    },
    /** 撤销这份标定（标错了要能重来，而不是只能覆盖） */
    clearCourtCalib: function () {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      if (!v) { return; }
      window.API.deleteCalibration(v, (this.calInfo && this.calInfo.revision) || 0)
        .then(function (r) {
          self.$message.success(r && r.removed ? '已撤销这份标定' : '本来就没有标定');
          self.courtResult = null;
          self.courtPreview = null;
          self.courtPreviewed = false;
          self.loadCalInfo();
        }).catch(function (e) {
          self.$message.error('撤销失败：' + (e && e.message ? e.message : e));
        });
    },
    loadMarkFrame: function (keepPoints) {
      var self = this;
      var v = String(this.form.video_path || '').trim();
      // 换帧就清空标点：标点存的是**像素位置**，换了帧那些位置就指向别的东西了
      // （实测用户看到的现象是"点跟着画面漂移"，其实是我们没清）。
      if (!keepPoints && (this.courtPts.length || this.markPts.length)) {
        this.courtPts = [];
        this.courtIdx = 0;
        this.markPts = [];
        this.markIdx = 0;
        this.$message.info('已换帧，标点已清空，请按提示重新点');
      }
      this.markImg = '';
      this.log('GET /api/frame?at=' + this.markAt);
      window.API.getFrame(v, this.markAt).then(function (r) {
        self.markImg = r.image;
        self.markSize = { w: r.w, h: r.h };
      }).catch(function (e) {
        self.$message.error('取帧失败：' + (e && e.message ? e.message : e));
      });
    },
    /** 鼠标在画面上移动 → 记下位置，用来画准星（点之前就知道会落在哪） */
    onMarkMove: function (ev) {
      var el = (ev.target && ev.target.tagName === 'IMG') ? ev.target
                                                          : ev.currentTarget;
      var box = el.getBoundingClientRect();
      if (!box.width || !box.height) return;
      var x = (ev.clientX - box.left) / box.width;
      var y = (ev.clientY - box.top) / box.height;
      if (x < 0 || x > 1 || y < 0 || y > 1) { this.hoverPt = null; return; }
      this.hoverPt = { x: x, y: y };
    },
    onMarkLeave: function () { this.hoverPt = null; },
    onMarkClick: function (ev) {
      if (this.markKind === 'court') { this.onCourtClick(ev); return; }
      // 必点项（篮筐中心）已点完时，如果用户选了"可选：量筐宽"的某一项，就落那一项。
      // 这样"只点中心"是默认路径，想量精确半径的人也不用被困在 5 步流程里。
      var target = (this.markDone && this.hoopOptTarget) ? this.hoopOptTarget : null;
      if (!target && (this.markDone || !this.markCurrent)) return;
      var el = (ev.target && ev.target.tagName === 'IMG') ? ev.target
                                                          : ev.currentTarget;
      var box = el.getBoundingClientRect();
      var x = (ev.clientX - box.left) / box.width;
      var y = (ev.clientY - box.top) / box.height;
      x = Math.max(0, Math.min(1, x));
      y = Math.max(0, Math.min(1, y));
      if (target) {
        var item = this.markItemsOptional.filter(function (o) {
          return o.name === target; })[0] || { name: target, label: target };
        // 同名点只能有一个：再点一次就是覆盖（避免越点越多）
        this.markPts = this.markPts.filter(function (p) { return p.name !== target; });
        this.markPts.push({ name: item.name, label: item.label, x: x, y: y });
        this.hoopOptTarget = '';
        return;
      }
      this.markPts.push({ name: this.markCurrent.name,
                          label: this.markCurrent.label, x: x, y: y });
      this.markIdx += 1;
    },
    markUndo: function () {
      if (this.markKind === 'court') { this.courtUndo(); return; }
      if (this.markIdx > 0) {
        this.markIdx -= 1;
        this.markPts.pop();
      }
    },
    markSkip: function () {
      if (this.markKind === 'court') { this.courtSkip(); return; }
      if (!this.markDone) this.markIdx += 1;
    },
    courtUndo: function () {
      if (this.courtIdx > 0) { this.courtIdx -= 1; this.courtPts.pop(); }
    },
    courtSkip: function () { if (!this.courtDone) this.courtIdx += 1; },
    markPct: function (p) { return { left: (p.x * 100) + '%', top: (p.y * 100) + '%' }; },
    /** 保存标点：POST /api/marks（归一化坐标，后端换回像素） */
    saveMarks: function () {
      var self = this;
      if (this.markKind === 'court') { this.saveCourt(); return; }
      var h = this.markHoop;
      if (!h) { this.$message.warning('至少要点出「篮筐中心」'); return; }
      var lm = {};
      this.markPts.forEach(function (p) { lm[p.name] = [p.x, p.y]; });
      this.markSaving = true;
      window.API.saveMarks({
        video_path: String(this.form.video_path).trim(),
        at: this.markAt,
        landmarks: lm,
        hoop: [h.cx, h.cy, h.rx, h.ry]
      }).then(function (r) {
        self.markSaving = false;
        self.markOpen = false;
        self.savedMarks = r.marks;
        self.$message.success('标点已保存，分析时会直接用它（不再跑篮筐检测器）');
      }).catch(function (e) {
        self.markSaving = false;
        self.$message.error('保存失败：' + (e && e.message ? e.message : e));
      });
    },
    clearMarks: function () {
      if (this.markKind === 'court') { this.courtPts = []; this.courtIdx = 0; return; }
      this.markPts = []; this.markIdx = 0;
    },
    /** 把已保存的标点画回去（只读展示） */
    savedMarkPct: function (p) { return { left: (p[0] * 100) + '%', top: (p[1] * 100) + '%' }; },

    /** 后端能力探测：GET /api/health（ffmpeg / 检测模型 / OpenCV 是否就绪） */
    fetchHealth: function () {
      var self = this;
      if (!this.S.backendOk) return;
      window.API.health().then(function (h) { self.health = h; })
        .catch(function () { self.health = null; });
    },
    /** 浏览器上传视频：POST /api/upload（带 x-filename 头）→ 得到后端可见的路径 */
    /* ------------------------------------------------------------------
       新手上手引导（onboard）：让第一次拿到软件的人 5 分钟内看到一份结果
       ------------------------------------------------------------------ */
    /** 拉取"机器上现成的视频"清单（示例素材 + 已上传），给引导里的下拉用 */
    loadSamples: function () {
      var self = this;
      window.API.samples().then(function (r) {
        self.samples = (r && r.samples) || [];
        if (!self.samplePick && self.samples.length) {
          self.samplePick = self.samples[0].path;
        }
      }).catch(function () { self.samples = []; });
    },
    /** 一键用现成视频（新手上手最省事的一步） */
    useSample: function () {
      var p = this.samplePick || (this.samples[0] && this.samples[0].path);
      if (!p) {
        this.$message.warning('这台机器上没找到现成素材，请选一个本地文件上传');
        this.pickFile();
        return;
      }
      // 一定要填**绝对路径**：/api/samples 返回的就是绝对路径；
      // 手抄成相对路径（data\uploads\xxx.mp4）在浏览器/资源管理器里会解析到别的地方，
      // 表现是"路径不存在"（用户实测踩过）。
      this.form.video_path = p;
      this.loadCalInfo();
      if (this.loadScoreboardEvents) { this.loadScoreboardEvents(); }
      this.$message.success('已选好视频（绝对路径已填进「文件路径」）：' + this.videoBaseName);
    },
    /** 把当前视频的绝对路径复制到剪贴板（给"想用别的播放器打开"的人用） */
    copyVideoPath: function () {
      var p = String(this.form.video_path || '');
      if (!p) { return; }
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(p);
          this.$message.success('已复制绝对路径：' + p);
          return;
        }
      } catch (e) { /* 下面兜底 */ }
      this.$message.info('绝对路径：' + p);
    },
    /** 引导里的按钮：按步骤分发（1 选视频 / 2 标定 / 3 分析或看结果） */
    guideAct: function (st) {
      if (!st) { return; }
      if (st.key === 'video') {
        if (this.samples.length) { this.useSample(); } else { this.pickFile(); }
        return;
      }
      if (st.key === 'calib') { this.openCourt(); return; }
      if (st.key === 'run') {
        var job = (this.S && this.S.job) || {};
        if (job.status === 'done') { location.hash = '#/overview'; return; }
        this.start();
      }
    },
    /** 收起引导：记在 localStorage，别每次都来烦人（但随时可以重新打开） */
    dismissGuide: function () {
      this.guideOpen = false;
      try { localStorage.setItem('aihoop-guide-dismissed', '1'); } catch (e) { /* 无痕模式 */ }
      this.$message.info('已收起上手引导；需要时在同一位置点「重新打开上手引导」');
    },
    openGuide: function () {
      this.guideOpen = true;
      try { localStorage.removeItem('aihoop-guide-dismissed'); } catch (e) { /* 忽略 */ }
    },
    /** 选本地文件上传（引导第 1 步的兜底路径） */
    pickFile: function () {
      var self = this;
      if (!this.S.backendOk) {
        this.$message.warning('未连接后端，无法上传文件；可先填写后端机器上的绝对路径');
        return;
      }
      if (this.health && this.health.stale) {
        this.$message.warning('后端正在自动重启或仍载入旧代码，请等几秒后点「刷新」再上传');
        return;
      }
      var input = document.createElement('input');
      input.type = 'file';
      input.accept = 'video/*';
      input.onchange = function () {
        var f = input.files && input.files[0];
        if (!f) return;
        self.fileName = f.name;
        self.uploadPct = 0;
        self.log('POST /api/upload（x-filename=' + f.name + '，' + (f.size / 1048576).toFixed(1) + ' MB）');
        window.API.uploadVideo(f, function (p) { self.uploadPct = p; }).then(function (r) {
          self.uploadPct = -1;
          self.form.source = 'video';
          self.form.video_path = r.path;
          self.log('上传完成：' + r.path + '（' + r.bytes + ' 字节）');
          self.$message.success('视频已上传到后端：' + r.path);
        }).catch(function (e) {
          self.uploadPct = -1;
          self.log('上传失败：' + e.message);
          // 错误消息本身已经写清楚了原因（后端没响应 / 被中断 / HTTP 码），
          // 这里不再套一层「上传失败：」，免得出现「上传失败：上传失败：…」
          self.$message.error(e.message);
        });
      };
      input.click();
    },

    /** 静态产物模式：载入 web/demo/*.json（不需要后端进程） */
    loadDemo: function () {
      var self = this;
      window.API.loadDemo().then(function (d) {
        if (!d.game) {
          self.$message.error('未找到 web/demo/game.json：先跑 '
            + 'python -m aihoop.cli demo --seed 7 --duration 600 --out out/demo');
          return;
        }
        self.S.applyGame(d.game, d.players || [], d.shotchart, {
          markdown: d.reportMd || '', json: d.report_json || null
        }, d.highlights);
        self.S.demoMode = true;
        self.S.jobId = 'demo';
        self.$message.success('已载入演示数据');
        location.hash = '#/overview';
      });
    },

    /** 拉取历史任务列表：GET /api/jobs */
    refreshJobs: function () {
      var self = this;
      if (!this.S.backendOk) return;
      window.API.listJobs().then(function (d) {
        self.jobs = Array.isArray(d) ? d : (d && d.jobs) || [];
      }).catch(function () { /* 静默：无后端时不打扰用户 */ });
    },

    /** 回看某个历史任务的全部产物：GET /api/games/{id} 等 */
    openJob: function (row) {
      var self = this;
      if (row.status !== 'done') { this.$message.info('该任务还未完成'); return; }
      window.APP_LOAD_JOB(row.job_id).then(function () {
        self.S.demoMode = false;
        self.$message.success('已载入任务 ' + String(row.job_id).slice(0, 8) + ' 的产物');
        location.hash = '#/overview';
      }).catch(function (e) {
        self.$message.error('载入失败：' + e.message);
      });
    },

    /** 开始分析：POST /api/jobs → WS/轮询跟踪进度 → 完成后装载产物 */
    start: function () {
      var self = this;
      if (!this.S.backendOk) {
        this.$message.warning('未连接后端，无法新建任务；可先点「载入演示数据」走完整演示流程');
        return;
      }
      if (!this.canSubmit) { this.$message.warning('请填写视频路径或 JSONL 文件路径'); return; }
      this.busy = true;
      this.logs = [];
      this.job = { job_id: '', status: 'queued', progress: 0, message: '正在提交任务…', error: null, out_dir: '', summary: '' };

      var payload = {
        // 后端 JobCreate：video 源用 video_path；jsonl 源用 raw_path（两个都带上，互不冲突）
        video_path: String(this.form.video_path).trim(),
        source: this.form.source,
        // 不再发 seed：它只对后端「合成比赛」生效（JobCreate.seed → synthetic_game），
        // 真实视频分析用不到（管线里没有随机数）。合成入口已从界面移除，
        // 需要合成数据时用命令行 `python -m aihoop.cli demo --seed 7`。
        make_highlights: !!this.form.make_highlights
      };
      if (this.form.source === 'video') {
        // 快速模式：跳过逐帧 YOLO，只走「比分牌 + 颜色追球」
        payload.detect_players = !this.form.fast;
        payload.stride = this.form.fast ? 1 : 2;
        // 如果标过篮筐，把标点文件带上：后端直接用你的坐标，跳过检测器
        if (this.savedMarks && this.savedMarks.path) {
          payload.marks = this.savedMarks.path;
          this.log('带上人工标点：' + this.savedMarks.path);
        }
        // 如果自己也标过进球（label_baskets 的产出），带上当准绳
        if (this.form.labels_path) payload.basket_labels = String(this.form.labels_path).trim();
        // 比分牌得分事件（「读比分牌」的产出）：带上它，非标准台标也能走比分牌路径
        if (this.form.scoreboard_events) {
          payload.scoreboard_events = String(this.form.scoreboard_events).trim();
          this.log('带上比分牌得分事件：' + payload.scoreboard_events);
        }
      }
      if (this.form.source === 'jsonl') payload.raw_path = payload.video_path;
      // **没标定球场时必须显式声明"不要球场坐标"**：后端 JobCreate.allow_no_calibration
      // 默认是 False，没标定就会直接抛「尚未标定球场…」把任务拒掉；而本页第 2 步的引导
      // 明明写着"不标也能出比分、命中率、球员统计和战报"。两边口径必须一致，
      // 否则用户照着引导走、点「开始分析」必被拒（用户实测反馈：不标定就是不给分析）。
      if (this.form.source === 'video') {
        var hasCal = !!(this.calInfo && this.calInfo.exists);
        payload.allow_no_calibration = !hasCal;
        if (!hasCal) {
          this.log('未标定球场：本次按「只判进球」跑（没有热图/战术图/2-3 分区分）'
            + '；标定后这些位置类结论才会打开。');
        }
      }
      this.log('POST /api/jobs ' + JSON.stringify(payload));

      window.API.createJob(payload).then(function (r) {
        var id = r.job_id || r.id;
        self.job.job_id = id;
        self.log('任务已创建 job_id=' + id + '，订阅 WS /api/jobs/' + id + '/ws');
        window.APP.registerJob(self.job);
        self.subscribe(id);
      }).catch(function (e) {
        self.busy = false;
        self.job.status = 'error';
        self.job.error = e.message;
        self.log('创建任务失败：' + e.message);
        self.$message.error('创建任务失败：' + e.message);
      });
    },

    /** 订阅进度：WS 优先，失败自动切轮询（api.js 内部实现） */
    subscribe: function (id) {
      var self = this;
      if (this.watcher) this.watcher.close();
      this.watcher = window.API.watchJob(id, function (j) {
        var prev = self.job.message;
        self.job = j;
        if (j.message && j.message !== prev) self.log('阶段：' + j.message + '（' + Math.round((j.progress || 0) * 100) + '%）');
        if (j.status === 'done') {
          self.busy = false;
          self.log('分析完成：' + (j.summary || ''));
          self.$message.success('分析完成，正在载入产物…');
          window.APP_LOAD_JOB(id).then(function () {
            self.S.demoMode = false;
            self.refreshJobs();
            location.hash = '#/overview';
          }).catch(function (e) { self.$message.error('产物载入失败：' + e.message); });
        } else if (j.status === 'error') {
          self.busy = false;
          self.log('任务失败：' + (j.error || '未知错误'));
          self.$message.error('分析失败：' + (j.error || '未知错误'));
        }
      }, function (err) {
        self.log('⚠ ' + err.message);
      });
    },

    log: function (msg) {
      var t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      this.logs.push('[' + t + '] ' + msg);
      if (this.logs.length > 200) this.logs.shift();
      var self = this;
      this.$nextTick(function () {
        var el = self.$refs.logbox;
        if (el) el.scrollTop = el.scrollHeight;
      });
    }
  },
  template: [
    '<div>',
    // ---- 新手上手引导：3 步走到"看到一份结果" ----
    // 为什么放最上面：第一次拿到软件的人面对 ①②③ 三张卡会一头雾水 ——
    // 不知道该先干什么、标定是不是必须、结果在哪看。这里把最短路径摊开，
    // 并**跟着用户的实际进度**打勾（状态驱动，不是一段死教程）。
    '  <div class="card" v-if="guideOpen">',
    '    <div class="row" style="align-items:center;justify-content:space-between">',
    // 注意：**不要**给卡片加左侧彩色竖条（border-left:3px 是 AI 生成界面的典型痕迹，
    // 检测器会直接判为 slop）。这里改用一个小标签来区分"这是引导，不是普通表单"。
    '      <h3 class="card-title" style="margin:0"><el-tag size="small" type="primary" effect="plain">上手引导</el-tag>',
    '        3 步跑出第一份分析',
    '        <span class="sub">已完成 {{ guideDoneCount }}/3 · 全程约 5 分钟</span></h3>',
    '      <el-button size="small" text @click="dismissGuide">收起（不再自动显示）</el-button>',
    '    </div>',
    '    <div class="hint" style="margin:6px 0 10px">做完这 3 步你能看到：比分与命中率、球员统计、投篮热图、战术分析，以及一份文字战报。不用先读文档，也不用先配环境。</div>',
    '    <div class="grid grid-3" style="gap:10px">',
    '      <div v-for="st in guideSteps" :key="st.key" class="src-card" :class="{active: !st.done}">',
    '        <div class="t">{{ st.done ? \'✓\' : st.n }}. {{ st.title }}</div>',
    '        <div class="d" style="margin-bottom:8px">{{ st.desc }}</div>',
    '        <el-button size="small" :type="st.done ? \'default\' : \'primary\'" @click="guideAct(st)">{{ st.cta }}</el-button>',
    '      </div>',
    '    </div>',
    '    <div class="row" style="margin-top:10px;align-items:center;flex-wrap:wrap;gap:6px">',
    '      <span class="hint">手头没有素材？直接选一段机器上现成的：</span>',
    '      <el-select v-model="samplePick" size="small" style="width:340px" placeholder="选择现成的视频" filterable>',
    '        <el-option v-for="s in samples" :key="s.path"',
    '          :label="s.group + \' · \' + s.name + \'（\' + s.size_mb + \' MB）\'" :value="s.path" />',
    '      </el-select>',
    '      <el-button size="small" @click="useSample" :disabled="!samples.length">用它</el-button>',
    '      <span class="hint" v-if="!samples.length">（没找到现成素材：点第 1 步的「选择本地文件」上传你自己的视频）</span>',
    '      <span class="grow"></span>',
    '      <span class="hint" v-if="!S.backendOk" style="color:#c45656">后端没连上：先双击项目根目录的「启动后端.bat」，并确认那个窗口一直开着（里面有「接口文档 / 健康检查 / 自动重启」三行才算起来了）。起来后本页会自动重连（每 5 秒试一次），也可以点右上角「重新探测」或按 F5。</span>',
    '    </div>',
    '  </div>',
    '  <div class="card" v-else style="padding:10px 14px">',
    '    <div class="row" style="align-items:center;gap:8px">',
    '      <span class="hint">上手引导已收起。</span>',
    '      <el-button size="small" text type="primary" @click="openGuide">重新打开上手引导</el-button>',
    '      <span class="grow"></span>',
    '      <span class="hint">没跳过的话，按下面 ① ② ③ 顺序走也一样。</span>',
    '    </div>',
    '  </div>',
    '  <div class="card">',
    '    <h3 class="card-title">① 选择数据源</h3>',
    '    <div class="grid grid-2">',
    '      <div class="src-card" :class="{active: form.source===\'video\'}" @click="form.source=\'video\'">',
    '        <div class="t">🎥 上传视频（video）</div>',
    '        <div class="d">两种方式：① 直接<b>选择本地文件</b>上传（POST /api/upload，后端复制到 data/uploads）；',
    '          ② 填写后端机器上的<b>绝对路径</b>（同机演示最省事）。</div>',
    '      </div>',
    '      <div class="src-card" :class="{active: form.source===\'jsonl\'}" @click="form.source=\'jsonl\'">',
    '        <div class="t">📄 已有 JSONL（jsonl）</div>',
    '        <div class="d">直接用既有的检测结果重跑统计与热区，跳过目标检测环节；',
    '          后端取 <code>raw_path</code> 指向的 raw_track.json。</div>',
    '      </div>',
    '    </div>',
    '    <div class="hint">「合成演示」入口已移除：它生成的是<b>模拟比赛</b>（比分/出手/回合全是造出来的），',
    '      混在历史任务里会被误认成真实分析结果。回归测试请用命令行：',
    '      <code>python -m aihoop.cli demo --seed 7 --duration 600 --out out/fixture</code></div>',
    '  </div>',

    '  <div class="grid grid-2">',
    '    <div class="card">',
    '      <h3 class="card-title">② 分析参数</h3>',
    '      <el-form label-width="112px" label-position="left">',
    '        <el-form-item label="文件路径">',
    '          <el-input v-model="form.video_path" @change="loadScoreboardEvents" placeholder="例如 D:\\\\videos\\\\game1.mp4 或 out\\\\xxx\\\\raw_track.json" clearable />',
    '          <div class="row" style="margin-top:8px">',
    '            <el-button size="small" :disabled="!S.backendOk || !!(health && health.stale)" @click="pickFile">选择本地视频上传</el-button>',
    '            <el-button size="small" text type="primary" :disabled="!hasVideoPath" @click="copyVideoPath">复制绝对路径</el-button>',
    '            <span class="hint" v-if="fileName">已选：{{ fileName }}</span>',
    '            <span class="hint" v-if="uploadPct>=0">上传中 {{ Math.round(uploadPct*100) }}%</span>',
    '          </div>',
    // 为什么必须显式说清楚：这个按钮在「后端进程比源码旧」时会被禁用（避免新代码没生效时上传），
    // 但以前**只是灰着**、一个字都不说 —— 用户遇到的就是"我没法上传视频"，
    // 而且他改过的源码正是让他自己上传不了的原因，根本猜不到（实测反馈）。
    '          <el-alert v-if="health && health.stale" type="warning" :closable="false" show-icon style="margin-top:8px"',
    '            title="上传按钮暂时禁用：后端进程比源码旧（新代码还没生效）"',
    '            description="改过 .py 源码后，正在跑的后端进程里还是老模块，所以这里刻意不让你上传，免得用旧代码分析。重启后端即可（关掉后端窗口 → 重新双击「启动后端.bat」；那个脚本平时会在约 2 秒内自动重启，这次没自动重启）。点右边的「重新探测」也可以再查一次状态。" />',
    '          <el-progress v-if="uploadPct>=0" :percentage="Math.round(uploadPct*100)" :stroke-width="8" style="margin-top:8px;max-width:420px" />',
    '          <div class="hint">路径由后端进程解析；也可以点上面的按钮把视频上传到后端（返回可直接使用的路径）。</div>',
    '        </el-form-item>',
    // ---- 在画面上标篮筐：这是「分析前你先告诉我篮筐在哪」的入口 ----
    '        <el-form-item v-if="form.source===\'video\'" label="篮筐标点">',
    '          <div class="row">',
    '            <el-button size="small" type="primary" plain :disabled="!S.backendOk || !hasVideoPath" @click="openMark">在画面上标篮筐</el-button>',
    // 这里原来还有一个「标球场」按钮，跟下面「球场标定」那一行完全重复 ——
    // 用户反馈（2026-09-27）："篮筐标点的地方就已经有标球场的选项了，后面又有一个球场标注就重复了"。
    // 现在只有「篮筐标点」管篮筐，「球场标定」管球场，各一处。
    '            <el-tag v-if="savedMarks && savedMarks.hoop" size="small" type="success" effect="plain">',
    '              已标：中心 {{ Math.round(savedMarks.hoop[0]) }},{{ Math.round(savedMarks.hoop[1]) }}',
    '              <template v-if="savedMarks.hoop[2] > 0">，半径 {{ Math.round(savedMarks.hoop[2]) }}×{{ Math.round(savedMarks.hoop[3]) }}</template>',
    '              <template v-else>（半径由检测器量）</template>',
    '            </el-tag>',
    // 这里原来还有一个「未标点（走自动检测）」标签 —— 用户反馈（2026-09-27）"把未标点删了"。
    // 没标点时不显示任何标签：没有徽章就是"还没标"，不需要多一个灰标签占地方。
    '          </div>',
    // 用户反馈（2026-09-27）："标定球筐和球场不行，没法取帧" —— 实际原因是**表单里没有视频路径**
    // （没上传/没填/没选示例），此时按钮以前只是弹一个一闪而过的提示就 return 了，
    // 连取帧请求都没发出去，看起来像功能坏了。现在按钮直接灰掉并常驻说明原因。
    '          <el-alert v-if="!hasVideoPath" type="warning" :closable="false" show-icon',
    '            style="margin-top:6px" title="先选一段视频，按钮才会亮"',
    '            description="取帧、标篮筐、标球场都需要先有视频：用上面的「示例视频 → 用它」、或「选择本地视频上传」、或直接在「文件路径」里填一段本机视频的绝对路径。" />',
    '          <div class="hint"><b>只点一下篮筐中心就够了</b>：框心当起点，筐位仍逐帧跟随镜头，',
    '            判进球的尺度由检测器量（实测 480p 手持素材 ~30px）。想让尺度完全由你定，',
    '            再点一下圈的左缘或右缘即可（那时就以你的为准，不再跑检测器）。</div>',
    '        </el-form-item>',
    // ---- 标球场：用户建议「不一定要标篮筐，别的有特色的点也可以」----
    '        <el-form-item v-if="form.source===\'video\'" label="球场标定">',
    '          <div class="row">',
    '            <el-button size="small" plain :disabled="!S.backendOk || !hasVideoPath" @click="openCourt">标球场（点场地特征点）</el-button>',
    '            <el-tag v-if="calInfo" size="small" type="success" effect="plain">',
    '              已标定：重投影误差 {{ calInfo.reproj_error_m }} m',
    '            </el-tag>',
    '            <el-tag v-else size="small" type="warning" effect="plain">未标定（也能分析：热区不出、战术图退化成「自动逐帧标定」、2/3 分只能估）</el-tag>',
    '          </div>',
    '          <el-alert v-if="!hasVideoPath" type="info" :closable="false" show-icon style="margin-top:6px"',
    '            title="先选一段视频才能抽帧标定" description="同上：没有视频路径就无法取帧。" />',
    '          <div class="hint">点 4 个以上<b>场地特征点</b>（四角 / 中线两端 / 中圈中心 / 罚球线中点 / 篮筐），',
    '            自动解出这段视频的球场标定。<b>热区、战术图、球场坐标全靠它</b> —— 用别的视频的标定会整片错位。',
    '            没有标定时：战术图仍会用<b>自动逐帧标定</b>算球员坐标（结果上会明确标注「位置未校验」），但热区不出。</div>',
    '        </el-form-item>',
    // 这里原来是「进球标注(可选)」输入框（要用户先准备一份 label_baskets.py 的产出当准绳）。
    // 用户反馈（2026-09-27）："本来就是一个投篮判定软件，却有一个进球标注的选项，这合适吗，
    // 你还不如在视频分析完以后加一个报错的选项" —— 那本质是开发/评测用的基准输入，
    // 不该出现在产品主流程里。人工纠错走「人工复核」页（判为命中/判为未中即时写回），
    // 攒训练样本走「训练标注」页。后端 `basket_labels` 参数保留，供脚本/命令行使用。
    // 随机种子输入框也一并去掉了：它只对「合成演示比赛」生效，真实视频分析根本不用它
    // （视频管线里没有任何随机数），摆在主流程里只会让人以为"结果是编出来的"。
    // 合成入口已从界面移除，需要合成数据时用命令行：
    //   python -m aihoop.cli demo --seed 7 --duration 600 --out out/demo
    '        <el-form-item label="生成高光片段">',
    '          <el-switch v-model="form.make_highlights" />',
    '          <span class="hint" style="margin-left:10px">需要本机安装 ffmpeg；关闭则只出统计数据，速度更快</span>',
    '        </el-form-item>',
    '        <el-form-item v-if="form.source===\'video\'" label="快速模式">',
    '          <el-switch v-model="form.fast" />',
    '          <span class="hint" style="margin-left:10px">跳过逐帧 YOLO（球员检测）：只读比分牌 + 颜色线索追球。CPU 上快十几倍；代价是没有球员个体归属，1v1/野球场视频会把进球都算在一边</span>',
    '        </el-form-item>',
    '        <el-form-item v-if="form.source===\'video\'" label="比分牌（可选）">',
    '          <div class="row" style="flex-wrap:wrap;align-items:center;gap:8px">',
    '            <el-button size="small" type="primary" :loading="sbBusy" @click="readScoreboard">',
    '              读比分牌（自动定位 + OCR）</el-button>',
    '            <span class="hint">起始比分（可选）：</span>',
    '            <el-input-number v-model="sbStartHome" :min="0" :max="199" size="small" style="width:100px" placeholder="主队" />',
    '            <el-input-number v-model="sbStartAway" :min="0" :max="199" size="small" style="width:100px" placeholder="客队" />',
    '          </div>',
    '          <div class="hint" style="margin-top:6px">',
    '            转播台标（含校园/村 BA 那种长横条）自动定位读不出时，用它兜底：',
    '            自动定位比分区域 → 放大 4 倍 → Windows OCR。填了起始比分更稳',
    '            （视频从半场中间开始录时用它当基线）。</div>',
    '          <el-input v-model="form.scoreboard_events" size="small" clearable style="margin-top:6px"',
    '            placeholder="得分事件文件路径（点上面的按钮自动填；也可手填 out\\\\sb_events.json）" />',
    '          <el-input v-model="sbBox" size="small" clearable style="margin-top:6px"',
    '            placeholder="手动框选（可选）：归一化 x0,y0,x1,y1，例如 0.21,0.10,0.79,0.15；留空=自动定位" />',
    '          <el-alert v-if="sbResult" style="margin-top:8px" :closable="false" show-icon',
    "            :type=\"sbResult.loading ? 'info' : (sbResult.ok ? 'success' : 'error')\"",
    "            :title=\"sbResult.loading ? '正在定位并 OCR…（十几秒到一分钟）' : (sbResult.ok ? ('读出 ' + (sbResult.n_events||0) + ' 个得分事件，最终 ' + ((sbResult.final||{}).home) + ' : ' + ((sbResult.final||{}).away)) : '没读出比分')\"",
    "            :description=\"sbResult.loading ? '' : (sbResult.note || '')\" />",
    '        </el-form-item>',
    '        <el-form-item label="后端状态">',
    '          <el-tag :type="S.backendOk ? \'success\' : \'warning\'" effect="plain">{{ api.base }}</el-tag>',
    '          <span class="hint" style="margin-left:10px">{{ S.backendOk ? \'已连接\' : \'未连接（演示数据模式）\' }}</span>',
    '          <div class="row" style="margin-top:8px" v-if="health">',
    '            <el-tag size="small" :type="health.ffmpeg ? \'success\' : \'info\'" effect="plain">ffmpeg {{ health.ffmpeg ? \'就绪\' : \'缺失\' }}</el-tag>',
    '            <el-tag size="small" :type="health.opencv ? \'success\' : \'info\'" effect="plain">OpenCV {{ health.opencv ? \'就绪\' : \'缺失\' }}</el-tag>',
    '            <el-tag size="small" :type="health.ultralytics ? \'success\' : \'info\'" effect="plain">检测模型 {{ health.ultralytics ? \'就绪\' : \'未装\' }}</el-tag>',
    '            <el-tag size="small" effect="plain" v-if="health.code_rev">代码 {{ health.code_rev }}</el-tag>',
    '            <el-tag size="small" effect="plain" type="info" v-if="health.code_loaded_at">进程载入 {{ health.code_loaded_at }}</el-tag>',
    '            <el-button size="small" text @click="fetchHealth">刷新</el-button>',
    '          </div>',
    '          <el-alert v-if="health && health.stale" type="warning" :closable="false" show-icon style="margin-top:8px"',
    '            title="后端正在自动重启以加载新代码"',
    '            description="源码（代码时间）比进程载入时间新，说明后端还没切到最新代码。用「启动后端.bat」启动的话会在 2 秒内自动重启；稍等片刻点「刷新」即可。若一直不变，请关掉后端窗口重新双击「启动后端.bat」。" />',
    '          <el-alert v-else-if="health && health.has_ball_rim_path === false" type="error" :closable="false" show-icon style="margin-top:8px"',
    '            title="后端进程跑的是旧代码，新功能不会生效"',
    '            description="uvicorn 只在启动时加载一次源码：改了 .py 但没重启，进程里还是老模块，而且不报错（表现就是「改了跟没改一样」）。请关掉后端窗口后重新双击项目根目录的「启动后端.bat」（它有自动重启，之后就不用管了）。" />',
    '          <div class="hint" v-if="health">ffmpeg 决定能否生成高光片段；检测模型与 OpenCV 只在 video 源需要。</div>',
    '        </el-form-item>',
    '      </el-form>',
    '      <div class="row">',
    '        <el-button type="primary" :loading="busy" @click="start">开始分析</el-button>',
    '        <el-button @click="refreshJobs">刷新任务列表</el-button>',
    // 说明清楚它是什么：这是**界面演示用的离线样例**（仓库里的合成比赛产物），
    // 不是从视频分析出来的结果。不写清楚会被当成"假数据"（用户反馈 2026-09-27）。
    '        <el-button plain type="warning" @click="loadDemo">载入离线样例（合成比赛，仅供界面演示）</el-button>',
    '      </div>',
    // 用户反馈（2026-09-27）："你还不如在视频分析完以后加一个报错的选项" ——
    // 纠错入口本来就有（人工复核页逐球改判、立刻写回比分/统计），但在这里没有指路，
    // 用户自然找不到。这里补一句去处说明，去掉的是"分析前先交一份标注"那种前置负担。
    '      <div class="hint" style="margin-top:8px">分析跑完后：判错的球到',
    '        <b>人工复核</b> 页逐球改判（立刻写回比分与统计）；要把误判留作样本去 <b>训练标注</b> 页。</div>',
    '    </div>',

    '    <div class="card">',
    '      <h3 class="card-title">③ 分析进度 <span class="sub">WebSocket 实时推送 /api/jobs/{id}/ws</span></h3>',
    '      <div class="progress-box">',
    '        <div class="row" style="justify-content:space-between;margin-bottom:8px">',
    '          <span><el-tag :type="statusTag" effect="dark" size="small">{{ statusText }}</el-tag>',
    '            <span class="mono muted" style="margin-left:8px">{{ job.job_id ? job.job_id.slice(0,12) : \'—\' }}</span></span>',
    '          <b class="mono">{{ percent }}%</b>',
    '        </div>',
    '        <el-progress :percentage="percent" :stroke-width="12" :status="job.status===\'error\' ? \'exception\' : (job.status===\'done\' ? \'success\' : \'\')" />',
    '        <div class="hint" style="margin-top:8px">当前阶段：{{ job.message || \'等待开始\' }}</div>',
    '        <div class="hint" v-if="job.summary">结果摘要：{{ job.summary }}</div>',
    '        <div class="hint" v-if="job.out_dir">产物目录：<code>{{ job.out_dir }}</code></div>',
    '        <el-alert v-if="job.error" type="error" :closable="false" :title="job.error" style="margin-top:10px" />',
    '      </div>',
    '      <div class="stage-log" ref="logbox" style="margin-top:12px">',
    '        <div v-for="(l,i) in logs" :key="i">{{ l }}</div>',
    '        <div v-if="!logs.length" class="muted">等待任务开始…（阶段：载入检测结果 → 计分规则引擎 → 统计与热区 → 战报 → 导出 → 高光）</div>',
    '      </div>',
    '    </div>',
    '  </div>',

    '  <div class="card" v-if="S.backendOk">',
    '    <h3 class="card-title">历史任务 <span class="sub">GET /api/jobs（最近 20 条）· 点「查看」可直接回看产物</span></h3>',
    '    <el-table :data="jobs" size="small" border empty-text="暂无任务">',
    '      <el-table-column prop="job_id" label="任务 ID" min-width="200" show-overflow-tooltip />',
    '      <el-table-column prop="status" label="状态" width="100">',
    '        <template #default="s"><el-tag size="small" :type="{done:\'success\',running:\'warning\',error:\'danger\',queued:\'info\'}[s.row.status]">{{ s.row.status }}</el-tag></template>',
    '      </el-table-column>',
    '      <el-table-column label="进度" width="150">',
    '        <template #default="s"><el-progress :percentage="Math.round((s.row.progress||0)*100)" :stroke-width="8" /></template>',
    '      </el-table-column>',
    '      <el-table-column prop="message" label="阶段" min-width="180" show-overflow-tooltip />',
    '      <el-table-column prop="summary" label="摘要" min-width="240" show-overflow-tooltip />',
    '      <el-table-column label="操作" width="110" align="center">',
    '        <template #default="s"><el-button size="small" text type="primary" @click="openJob(s.row)">查看</el-button></template>',
    '      </el-table-column>',
    '    </el-table>',
    '  </div>',

    // ---- 标注弹窗（篮筐 / 球场 两种模式共用）----
    '  <el-dialog v-model="markOpen" :title="markKind===\'court\' ? \'标球场：点场地特征点（4 个以上）\' : \'在画面上标篮筐\'" width="82%" top="4vh" :close-on-click-modal="false">',
    '    <div class="row" style="margin-bottom:8px;align-items:center">',
    '      <span class="hint">取帧时刻</span>',
    '      <el-input-number v-model="markAt" :min="0" :step="0.5" :precision="1" size="small" style="width:120px" @change="seekVideo(markAt)" />',
    '      <el-button size="small" @click="loadMarkFrame(false)">重新取帧（会清空已点的点）</el-button>',
    '      <span class="hint">（也可以在下面拖着原片找到最清楚的那一刻）</span>',
    '    </div>',
    // 原片播放器：拖动进度条定位 —— 光靠手输秒数等于碰运气（用户实测反馈：
    // "只调时间秒数不好整，一点也不方便"）。旧版标定页就是内嵌播放器 + 现场截帧。
    '    <div v-if="videoUrl" style="margin-bottom:8px">',
    '      <div class="row" style="align-items:center;margin-bottom:6px">',
    '        <b>原片</b>',
    '        <span class="hint">拖进度条：先找到「一侧半场看得清」的那一刻 → 点右边按钮放到左画面；再拖到「另一侧半场」→ 放到右画面</span>',
    '        <span class="grow"></span>',
    '        <el-button size="small" type="primary" @click="useVideoMoment()">',
    '          {{ markKind===\'court\' ? (dualView ? (mfActive===1 ? \'把这一帧放到右画面\' : \'把这一帧放到左画面\') : \'把这帧加进来标点\') : \'用当前画面取帧\' }}</el-button>',
    '      </div>',
    '      <video ref="markVideo" :src="videoUrl" controls preload="metadata"',
    '             @loadedmetadata="seekVideo(markAt)"',
    '             style="width:100%;max-height:38vh;background:#000;border-radius:6px" />',
    '    </div>',
    // 第一步：先说清「这件事要干到什么程度」——用户实测反馈"标完篮筐中心就不知道怎么办了"
    '    <el-alert type="info" :closable="false" show-icon style="margin-bottom:8px"',
    '      :title="markKind===\'court\' ? \'怎么标：左右两个画面，一个标一侧半场，每边点够 4~6 个点\' : \'怎么标：只标「篮筐中心」就能保存，另外 4 项是可选的\'"',
    '      :description="markKind===\'court\' ? (\'每侧半场推荐点这 5~6 个：底线左角、底线右角、罚球区左角、罚球区右角、罚球线中点、篮筐中心。\' + (mfFrames.length ? \'流程：把「看近端半场（篮筐那侧）」的那一帧放到左画面、另一侧半场的放到右画面；先点下面的点名 → 再到画面里点它；一个名字可以在左右两边各点一次。\' : \'画面还在抽，抽完就会出现「左右两个画面」的选择器。\') + \' ★每幅画面自己要有 4 个以上、铺得开的点（单应矩阵只认同一幅画面里的点）；画面里看不到的点按「跳过这一项」。\') : \'另外 4 项（篮圈左/右缘、上/下沿）只用来量筐宽和篮筐高度，跳过也行；标了中心就能保存。\'" />',
    // 当前目标 + 实时反馈（点了几个、离"能保存"还差几个）
    // 条件必须用 guideNext（它是模式感知的）；用 markCurrent 的话，多帧模式下
    // courtCurrent 恒为真而 guideNext 为 null，模板会取 null.idx 直接报错。
    '    <el-alert v-if="guideNext" type="info" :closable="false" show-icon style="margin-bottom:8px"',
    '      :title="\'第 \' + guideNext.idx + \' 个点：\' + guideNext.label + \'（\' + guideNext.hint + \'）\'"',
    '      :description="guideDesc" />',
    // 多帧模式还没选点名：明确说"先去下面的点名列表里选一个"（不能沿用 courtIdx，那永远停在第一个点名）。
    // 方位词必须对：这条 alert 在**点名按钮上方**，按钮在它下面。
    '    <el-alert v-else-if="markKind===\'court\'" type="info" :closable="false" show-icon style="margin-bottom:8px"',
    '      title="下一步：在下面的点名里点一个（推荐先点底线两角、罚球区两角、篮筐中心），再到画面里点它；同一个名字可以在左右两个画面各点一次"',
    '      :description="guideDesc" />',
    '    <el-alert v-else type="success" :closable="false" show-icon style="margin-bottom:8px"',
    '      title="篮筐中心已标好 —— 可以直接保存（点多一下就是一步）"',
    '      :description="markHoop && markHoop.measured',
    '        ? (\'中心 \' + Math.round(markHoop.cx*markSize.w) + \',\' + Math.round(markHoop.cy*markSize.h)',
    '           + \'，半径 \' + Math.round(markHoop.rx*markSize.w) + \'×\' + Math.round(markHoop.ry*markSize.h) + \' 像素（你量的）\')',
    '        : (\'中心 \' + Math.round((markHoop?markHoop.cx:0)*markSize.w) + \',\' + Math.round((markHoop?markHoop.cy:0)*markSize.h)',
    '           + \'。没量筐宽也没关系：半径会由检测器量；想让判进球的尺度由你定，就再点一下「篮圈左缘」或「右缘」（可选）\')" />',
    // 球场模式下点不够 4 个的提示
    '    <el-alert v-if="markKind===\'court\' && guideCount < 4" type="warning" :closable="false" show-icon style="margin-bottom:8px"',
    '      title="至少要 4 个点才能解出标定"',
    '      :description="\'现在两边合计 \' + guideCount + \' 个。\' + (mfDualSplit ? (\'左画面 \' + mfLeftCov.n + \' 个、右画面 \' + mfRightCov.n + \' 个 —— 每边各自要有 4 个以上。\') : \'\') + \'画面里看不清的点按「跳过这一项」。\'" />',
    // 标定失败时的原因（重投影误差偏大 / 点位退化 / 解释不了画面里的真篮筐）
    '    <el-alert v-if="courtResult && !courtResult.usable" type="error" :closable="false" show-icon style="margin-bottom:8px"',
    "      :title=\"'标定结果不可用' + (courtResult.rmse_m != null ? ('（重投影误差 ' + courtResult.rmse_m + ' m）') : '')\"",
    '      :description="courtResult.note || \'点位可能有误：检查是否点在了正确的角/线上，或换一张更清楚的帧重新标。\'" />',
    // 多帧标定的结果与"未校验"提示（宽容规则：能存但位置结论会关掉）
    '    <el-alert v-if="markKind===\'court\' && mfResult" :type="mfResult.ok ? (mfResult.position_unverified ? \'warning\' : \'success\') : \'error\'"',
    '      :closable="false" show-icon style="margin-bottom:8px"',
    '      :title="(mfResult.ok ? (mfResult.position_unverified ? \'标定已保存，但位置结论未校验\' : \'标定可用\') : \'这份点法还不能用\') + (mfResult.rmse_m != null ? (\'（平均误差 \' + mfResult.rmse_m + \' m\' + (mfResult.hoop_error_m != null ? (\'；篮筐投影偏 \' + mfResult.hoop_error_m + \' m\') : \'\') + \'）\') : \'\')"',
    '      :description="String(mfResult.note || \'\').slice(0, 200)" />',
    // 逐点责任诊断：直接点名"哪个点跟其余对不上"。
    // 为什么必须有：以前只说"有 N 个点互相矛盾"，不说是哪个 —— 用户只能反复重标，
    // 最后怀疑软件本身（实测原话："我标点就说表的不对，你这个标定的代码真的准确吗"）。
    '    <el-alert v-if="markKind===\'court\' && mfResult && mfResult.point_diagnosis" type="warning" :closable="false" show-icon style="margin-bottom:6px"',
    '      title="逐点体检：这些点里哪个跟其余对不上"',
    '      :description="mfResult.point_diagnosis.note" />',
    '    <div v-if="markKind===\'court\' && mfResult && mfResult.point_diagnosis" class="row" style="flex-wrap:wrap;margin-bottom:6px">',
    '      <el-tag v-for="(r,i) in mfResult.point_diagnosis.rows" :key="\'pd\'+i" size="small" effect="plain" style="margin:2px"',
    '        :type="r.excused_err_m != null && r.excused_err_m > 3 ? \'danger\' : \'info\'">',
    '        {{ r.label }}：排除它后偏 {{ r.excused_err_m != null ? r.excused_err_m : \'?\' }} m（其余点平均 {{ r.others_mean_err_m != null ? r.others_mean_err_m : \'?\' }} m）</el-tag>',
    '    </div>',
    '    <div v-if="markKind===\'court\' && mfResult && (mfResult.worst || []).length" style="margin-bottom:8px">',
    '      <span class="hint">误差最大的几个点：</span>',
    '      <el-tag v-for="(w,i) in mfResult.worst" :key="\'w\'+i" size="small" type="warning" effect="plain" style="margin:2px">',
    '        {{ w.label }} @{{ w.t }}s → {{ w.err_m }} m（像素 {{ w.px[0] }},{{ w.px[1] }}）</el-tag>',
    '    </div>',
    // 标定自检叠加层：把这份 H 算出来的球场线投影回画面。
    // 这是唯一能让人**肉眼**判断标定对错的证据 —— 因为 5 个点里任取 4 个都能
    // 精确解出 H（误差恒为 0），单看那个数字谁也说不清（实测争议点）。
    '    <div v-if="markKind===\'court\' && mfResult && mfResult.H" class="row" style="align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">',
    '      <el-switch :model-value="showCourtOverlay" active-text="叠加球场线自检" @change="toggleCourtOverlay" />',
    '      <span class="hint">红色虚线 = 用你标的点算出来的球场线（边线/底线/中线/罚球区/三分弧）。',
    '        <b>压在画面里真实的白线/绿区上 = 标定是对的</b>；整体偏移或歪斜 = 有点名对不上位置。</span>',
    '    </div>',
    // 把"通过校验"的客观读数摆出来。为什么必须显式给数字：
    // 分析端有一道硬门槛（投影线与画面白线的吻合度 ratio ≥1.25），
    // 达不到就**不生成战术图/热图**。以前界面只说"标定可用"，用户存下去才发现
    // 战术图是空的，完全对不上账（实测踩到：AI 自己标的点 ratio 只有 0.32）。
    '    <el-alert v-if="markKind===\'court\' && mfResult && mfResult.calibration_fit" :closable="false" show-icon style="margin-bottom:6px"',
    '      :type="fitRatio >= 1.25 ? \'success\' : \'warning\'"',
    '      :title="\'吻合度读数：\' + (mfResult.calibration_fit.ratio != null ? mfResult.calibration_fit.ratio : \'—\') + \'（门槛 1.25）\' + (fitRatio >= 1.25 ? \' —— 这份标定会被分析端接受，战术图/热图能出\' : \' —— 太低，分析端会拒收：战术图、热图、2/3 分区分都不会生成（只判进球）\')"',
    '      description="这个数字是「把球场线投回画面、量它压在白线上的比例」。低于 1.25 时请开上面的叠加层，看看红线整体偏到哪去了，重新点几个更准的点（优先四角）。" />',
    // 「点吸附精修」的结果：采纳了没有、吻合度前后对比。
    // 为什么必须显式区分"采纳/没采纳"：护栏拒绝时点集**根本没动**，
    // 若只显示一个"已精修"的绿条，用户会以为点被改过（实测这机位就会被拒）。
    '    <el-alert v-if="markKind===\'court\' && mfResult && mfResult.snap && mfResult.snap.note" :closable="false" show-icon style="margin-bottom:6px"',
    '      :type="mfResult.snap.accepted ? \'success\' : \'info\'"',
    '      :title="(mfResult.snap.accepted ? \'点吸附精修：已采纳 —— \' : \'点吸附精修：没有采纳（保留了你自己标的点）—— \') + mfResult.snap.note"',
    '      description="工具会试着把你标的点吸附到画面里的球场线上（位移上限 12 像素），但只有在结果**几何上仍像球场**时才采纳。被拒不是出错：说明这个机位太正对球场，单应矩阵本身就病态 —— 点挪几个像素就能让吻合度虚高到几十，那种解一律不用。" />',
    // 外层盒子宽度 = min(100%, 画面宽)；图片 width:100% 撑满它。
    // 这样"图片显示尺寸"和"定位容器尺寸"严格相等，绿圈才会落在鼠标点上。
    // ---- 球场模式：画面切换 + 特征点选择（多画面累加）----
    '    <div v-if="markKind===\'court\' && mfFrames.length" style="margin-bottom:8px">',
    // 一句总纲：这件事现在是「左右两个画面各自标一个半场」
    '      <div class="hint" style="margin-bottom:6px">',
    '        ① 先把「看这一侧半场」的两个时刻分别放到<b>左画面 / 右画面</b>（各点一下「用左边画面提取 / 用右边画面提取」的那张图即选中它）；',
    '        ② 再从下面的点名里选一个特征点 → 到<b>那个画面</b>里点它的位置。',
    '        ★ 硬要求：<b>每一幅画面自己要有 4 个以上、铺得开的点</b> —— 单应矩阵只认同一幅画面里的点；',
    '        一边只有两三个点时，两边加起来是解不出来的。画面里看不到的点按「跳过这一项」。</div>',
    '      <div class="row" style="align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap">',
    '        <el-switch :model-value="dualView" active-text="左右两个画面" inactive-text="一张一张切" @change="setDualView" />',
    '        <span class="hint">左 = 一侧半场的画面、右 = 另一侧半场的画面；两边各自标各自的点</span>',
    '      </div>',
    // ---- 单画面：老的「一张一张切」----
    '      <template v-if="!dualView">',
    '        <div class="row" style="align-items:center;margin-bottom:6px">',
    '          <b>画面 {{ mfIdx+1 }}/{{ mfFrames.length }}</b>',
    '          <span class="hint">t={{ mfCurrent ? mfCurrent.t : 0 }}s</span>',
    '          <el-button size="small" @click="mfPrev" :disabled="mfIdx<=0">上一张画面</el-button>',
    '          <el-button size="small" @click="mfNext" :disabled="mfIdx>=mfFrames.length-1">下一张画面</el-button>',
    '          <span class="grow"></span>',
    '          <el-tag size="small" :type="mfEnough?\'success\':\'info\'" effect="plain">已标 {{ mfPts.length }} 个点（本画面 {{ mfPtsHere.length }} 个）</el-tag>',
    '        </div>',
    '      </template>',
    // ---- 左右两个画面：各选一幅、各自点各自的点 ----
    '      <template v-else>',
    '        <div class="row" style="align-items:stretch;gap:8px;margin-bottom:6px;flex-wrap:wrap">',
    '          <div class="mf-panel-head" :class="{on: mfActive===0}" @click="setMfActive(0)">',
    '            <div class="row" style="align-items:center;gap:6px">',
    '              <b>左画面</b>',
    '              <el-tag size="small" :type="mfLeftCov.enough ? \'success\' : \'warning\'" effect="plain">',
    '                {{ mfLeftCov.n }} 个点{{ mfLeftCov.enough ? \'\' : \'（不够 4 个）\' }}</el-tag>',
    '            </div>',
    '            <div class="hint" style="margin-top:4px">这一幅是左半场{{ mfDualSplit ? \'\' : \'（目前跟右画面是同一幅）\' }}</div>',
    '            <div class="row" style="align-items:center;gap:4px;margin-top:4px">',
    '              <span class="hint">换这一幅：</span>',
    '              <el-select :model-value="mfIdx" size="small" style="width:150px" @change="setLeftFrame">',
    '                <el-option v-for="(f,i) in mfFrames" :key="\'L\'+i" :value="i"',
    '                  :label="\'第\' + (i+1) + \'幅 t=\' + f.t + \'s（\' + (mfFrameCov[i] ? mfFrameCov[i].n : 0) + \'点）\'" />',
    '              </el-select>',
    '            </div>',
    '          </div>',
    '          <div class="mf-panel-head" :class="{on: mfActive===1}" @click="setMfActive(1)">',
    '            <div class="row" style="align-items:center;gap:6px">',
    '              <b>右画面</b>',
    '              <el-tag size="small" :type="mfRightCov.enough ? \'success\' : \'warning\'" effect="plain">',
    '                {{ mfRightCov.n }} 个点{{ mfRightCov.enough ? \'\' : \'（不够 4 个）\' }}</el-tag>',
    '            </div>',
    '            <div class="hint" style="margin-top:4px">这一幅是右半场（看不到另一侧是正常的）</div>',
    '            <div class="row" style="align-items:center;gap:4px;margin-top:4px">',
    '              <span class="hint">换这一幅：</span>',
    '              <el-select :model-value="mfIdx2" size="small" style="width:150px" @change="setRightFrame">',
    '                <el-option v-for="(f,i) in mfFrames" :key="\'R\'+i" :value="i"',
    '                  :label="\'第\' + (i+1) + \'幅 t=\' + f.t + \'s（\' + (mfFrameCov[i] ? mfFrameCov[i].n : 0) + \'点）\'" />',
    '              </el-select>',
    '              <el-button size="small" @click="addRightFrame">把原片当前这帧放到右画面</el-button>',
    '            </div>',
    '          </div>',
    '        </div>',
    '        <div class="hint" style="margin-bottom:6px">',
    '          <span :style="{color: mfActive===0 ? \'#409eff\' : \'#909399\'}">现在点在<b>左画面</b>上</span>',
    '          /',
    '          <span :style="{color: mfActive===1 ? \'#409eff\' : \'#909399\'}">点在<b>右画面</b>上</span>',
    '          —— 直接在画面里点一下就会切过去；两边合计已标 {{ mfPts.length }} 个点',
    '          <span v-if="!mfDualSplit">｜ 现在两边还是同一幅画面：把右侧原片拖到「看另一侧半场」的那一刻，再按「把原片当前这帧放到右画面」</span>',
    '        </div>',
    '      </template>',
    '      <div class="row" style="align-items:center;margin-bottom:6px;flex-wrap:wrap">',
    '        <span class="hint">在</span>',
    '        <el-input-number v-model="mfCenter" :min="0" :step="1" :precision="1" size="small" style="width:110px" />',
    '        <span class="hint">秒附近抽</span>',
    '        <el-input-number v-model="mfSpan" :min="0.5" :max="30" :step="0.5" size="small" style="width:100px" />',
    '        <span class="hint">秒内的帧</span>',
    '        <el-button size="small" @click="mfResample">按这个时刻重抽</el-button>',
    '        <span class="hint">（覆盖上面左右两个画面的候选）</span>',
    '      </div>',
    // 「这一侧半场」选一次：只影响点名表显示哪 7 个点。
    // 这台机位只拍得到篮筐这一侧，所以只需要这一侧的 7 个特征点；
    // 选它 = 告诉工具"那 7 个名字对应的是哪一端的物理位置"。
    '      <div class="row" style="align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">',
    '        <span class="hint">摄像机拍的是哪一侧篮筐：</span>',
    '        <el-radio-group :model-value="courtSide" size="small" @change="setCourtSide">',
    '          <el-radio-button label="left">左侧（篮筐在左边那条底线）</el-radio-button>',
    '          <el-radio-button label="right">右侧</el-radio-button>',
    '        </el-radio-group>',
    '        <el-button size="small" @click="pickStableFrames">自动挑同一镜头的两帧</el-button>',
    '        <span class="hint">只影响下面显示哪 7 个点；两幅画面必须是**同一镜头**，否则合并解必然矛盾。</span>',
    '      </div>',
    // 球场的点名列表（篮筐模式没有这一排：中心是唯一必点项）
    '      <div class="row" style="flex-wrap:wrap">',
    '        <el-button v-for="it in courtItems" :key="it.name" size="small"',
    '          :type="mfTarget===it.name ? \'primary\' : (mfDone[it.name] ? \'success\' : \'default\')"',
    '          :plain="mfTarget!==it.name" @click="mfPick(it)">',
    '          {{ mfDone[it.name] ? \'✓ \' : \'\' }}{{ it.label }}</el-button>',
    '      </div>',
    '      <div class="hint" style="margin-top:2px">',
    '        选一个名字 → 在左画面点一下 → 再在右画面点一下（同一个名字可以两边各标一次，各记各的）；',
    '        点名上打勾表示<b>至少有一幅画面</b>标过它。</div>',
    // 篮筐模式的可选步骤：想自己量半径的人点这里，不点就交给检测器（默认路径）
    '      <div v-if="markKind===\'hoop\'" class="row" style="flex-wrap:wrap;margin-top:2px">',
    '        <span class="hint">可选：想让判进球的横向尺度由你定，就再点一下圈的一侧边缘',
    '          （只点中心也完全可以）</span>',
    '        <el-button v-for="it in markItemsOptional" :key="it.name" size="small"',
    '          :type="hoopOptTarget===it.name ? \'primary\' : (hoopOptDone[it.name] ? \'success\' : \'default\')"',
    '          :plain="hoopOptTarget!==it.name"',
    '          @click="hoopOptTarget = (hoopOptTarget===it.name ? \'\' : it.name)">',
    '          {{ hoopOptDone[it.name] ? \'✓ \' : \'\' }}{{ it.label }}</el-button>',
    '        <span class="hint" v-if="hoopOptTarget">现在去画面里点：{{ hoopOptTarget }}</span>',
    '      </div>',
    '      <div class="hint" v-if="mfTarget" style="margin-top:6px;color:#409eff">',
    '        现在去画面里点：<b>{{ mfTarget }}</b> —— 顺序随便，选中的那一个高亮；',
    '        两个半场都可以用同一个名字各点一次（各自独立记录）</div>',
      // 点完之后**必须留下"下一步"**：原来 mfTarget 一被清空，上一句就消失了，
      // 屏幕上只剩一排点名按钮 —— 用户就卡在"标完篮筐中心不知道怎么办"（实测反馈）。
      '      <div class="hint" v-else style="margin-top:6px;color:#409eff">',
      '        下一步：选一个点名 → 到画面里点它。现在：',
      '        左画面 {{ mfLeftCov.n }} 个{{ mfLeftCov.enough ? \'（够了）\' : \'（还差 \' + (4 - mfLeftCov.n) + \' 个）\' }}、',
      '        右画面 {{ mfRightCov.n }} 个{{ mfRightCov.enough ? \'（够了）\' : \'（还差 \' + (4 - mfRightCov.n) + \' 个）\' }}。',
      '        两边都够了再按「保存并解算标定」——每边 4 个以上是能解出来的底线，建议每边 5~6 个。</div>',
    // 每幅画面各自的覆盖度：哪一边还差、点是不是挤在一条线上，一眼看到
    '      <div class="row" style="flex-wrap:wrap;align-items:center;margin-top:6px">',
    '        <span class="hint">每幅画面的覆盖度：</span>',
    '        <el-tag v-for="(f,i) in mfFrameCov" :key="\'cov\'+i" size="small" style="margin:2px"',
    '          :type="f.enough ? (f.spread ? \'success\' : \'warning\') : \'info\'" effect="plain"',
    '          :title="f.enough ? (f.spread ? \'这幅画面自己就能解算\' : \'点数够了，但点挤在一条线上 —— 再往别处点一下\') : \'这幅画面还差 \' + (4 - f.n) + \' 个点（别的画面的点补不上）\'">',
    '          第{{ f.idx+1 }}幅 t={{ f.t }}s · {{ f.n }}点{{ f.enough ? (f.spread ? \' ✓可解\' : \' ⚠太集中\') : \' ✗不够\' }}</el-tag>',
    '        <span class="hint">（「可解」= 这幅画面自己够 4 个点且铺得开；挤在一条线上的点解出来的标定是错的）</span>',
    '      </div>',
    // 画面外点名提示：解出一份 H 之后就能算出"每个特征点会落在画面哪里"。
    // 有些点名（比如另一侧的底线角）在这台机位的画面里**根本看不见**，
    // 用户硬点也只能是猜 —— 实测踩过（用户把"底线右角"点成了罚球区右角）。
    '      <el-alert v-if="offFrameNames.length" type="warning" :closable="false" show-icon style="margin-top:6px"',
    '        :title="\'有 \' + offFrameNames.length + \' 个点，在你这两幅画面里**根本看不见**：\' + offFrameNames.join(\'、\')"',
    '        description="这份标定是拿它们猜出来的，所以误差偏大。做法：重标时跳过这些点（按「跳过这一项」），改点画面里能看见的（篮筐中心、罚球区两角、三分弧顶、中圈中心）。也可以打开上面的「叠加球场线自检」看红色虚线对不对得上画面里的白线。" />',
    '      <el-alert v-if="mfResult && !mfResult.ok" type="error" :closable="false" show-icon style="margin-top:8px"',
    '        :title="mfResult.note ? String(mfResult.note).slice(0, 120) : (\'标定误差偏大（平均 \' + mfResult.rmse_m + \' m）\')"',
    '        :description="\'最离群的几个点：\' + (mfResult.worst || []).map(function(d){return (d.label || d.name || \'?\') + \'(t=\' + d.t + \'s, \' + d.err_m + \'m)\'}).join(\'，\')" />',
    // 客观体检读数：点位退化 / 与画面里的真篮筐对不上。
    // 单看"重投影误差"是不够的 —— 点位几乎共线时误差必然是 0.00m 左右。
    '      <div v-if="mfResult && mfResult.degeneracy" class="hint" style="margin-top:6px">',
    '        点位展开度：画面侧最大三角形 {{ mfResult.degeneracy.src_tri_px2 }}px²（占画面 {{ (mfResult.degeneracy.src_frac*100).toFixed(2) }}%）、',
    '        球场侧 {{ mfResult.degeneracy.dst_tri_m2 }}m²',
    '        <span v-if="mfResult.degeneracy.degenerate" style="color:#f56c6c"> —— 判定为退化（近共线/重合）</span>',
    '        <span v-if="mfResult.hoop_check && mfResult.hoop_check.checked"',
    '              :style="{color: mfResult.hoop_check.ok ? \'#67c23a\' : \'#f56c6c\'}">',
    '          ｜ 独立校验（真篮筐）：{{ mfResult.hoop_check.reason }}</span>',
    '      </div>',
    '      <div v-if="mfResult && mfResult.per_frame_fit" style="margin-top:8px">',
    '        <div class="hint">各画面单独拟合的结果（能标定的画面会标绿）：</div>',
    '        <div class="row" style="flex-wrap:wrap;margin-top:4px">',
    '          <el-tag v-for="fr in mfResult.per_frame_fit" :key="fr.t" size="small"',
    '            :type="fr.ok ? \'success\' : (fr.n >= 4 ? \'warning\' : \'info\')" effect="plain" style="margin:2px">',
    '            t={{ fr.t }}s · {{ fr.n }}点 · {{ fr.rmse_m === null ? \'—\' : fr.rmse_m + \'m\' }}</el-tag>',
    '        </div>',
    '      </div>',
    '      <el-alert v-else-if="mfResult && mfResult.ok" type="success" :closable="false" show-icon style="margin-top:8px"',
    '        :title="\'标定成功：平均误差 \' + mfResult.rmse_m + \' m\'"',
    '        description="已保存为这段视频的标定，分析时会自动使用。" />',
    '    </div>',
    // 画面（球场模式用抽帧图，篮筐模式用取帧图）
    // 两个画面模式下：左画面 + 右画面并排。**每张图各自记自己那一半场地的点** ——
    // 这就是用户要的"分两个画面点，一个画面点左半场、一个画面点右半场"。
    '    <div :class="markKind===\'court\' && mfDualSplit ? \'mf-panels\' : \'\'">',
    '      <div v-if="markKind===\'court\'" class="mf-panel-wrap"',
    '           :class="{ on: !mfDualSplit || mfActive===0 }" data-side="0"',
    '           style="position:relative;width:100%;max-width:1100px;cursor:crosshair"',
    '           @click="onMarkClick" @mousemove="onMarkMove" @mouseleave="onMarkLeave">',
    '        <img v-if="mfCurrent" :src="mfCurrent.image" style="width:100%;height:auto;display:block;border-radius:6px" />',
    '        <div v-else class="muted" style="padding:40px">正在抽画面…</div>',
    '        <div v-if="mfDualSplit" class="mf-badge">左画面{{ mfActive===0 ? \'（正在点这里）\' : \'\' }}</div>',
    // 已点的标记（两种模式都画：篮筐模式画 markPts/activePts；
    // 球场模式**左画面画 mfPtsHere、右画面画 mfPtsRight** ——
    // 以前两边都用 activePts（=只认左帧），于是右画面的点会让左画面"看起来消失"）
    '        <svg v-if="frameW && frameH" :viewBox="\'0 0 \' + frameW + \' \' + frameH"',
    '             preserveAspectRatio="none"',
    '             style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none">',
    '          <g v-if="hoverPt && (markKind!==\'court\' || !mfDualSplit || mfActive===0)">',
    '            <line :x1="hoverPt.x*frameW - 22" :y1="hoverPt.y*frameH" :x2="hoverPt.x*frameW + 22" :y2="hoverPt.y*frameH" stroke="#f59e0b" :stroke-width="Math.max(1.5, frameW*0.0022)" />',
    '            <line :x1="hoverPt.x*frameW" :y1="hoverPt.y*frameH - 22" :x2="hoverPt.x*frameW" :y2="hoverPt.y*frameH + 22" stroke="#f59e0b" :stroke-width="Math.max(1.5, frameW*0.0022)" />',
    '          </g>',
    '          <template v-for="(p,i) in mfPtsHere" :key="i">',
    '            <circle :cx="p.x * frameW" :cy="p.y * frameH" :r="Math.max(10, frameW*0.012)"',
    '                    fill="rgba(34,197,94,.35)" stroke="#22c55e" :stroke-width="Math.max(2, frameW*0.003)" />',
    '            <text :x="p.x * frameW + Math.max(12, frameW*0.014)" :y="p.y * frameH"',
    '                  fill="#22c55e" :font-size="Math.max(16, frameW*0.022)"',
    '                  style="paint-order:stroke;stroke:#000;stroke-width:3px">{{ i + 1 }}. {{ p.label }}</text>',
    '          </template>',
    // 精修被采纳时，把"吸附后"的点也画出来（空心黄圈）——存下来的标定用的是它。
    '          <template v-for="(p,i) in snapPtsHere" :key="\'S\'+i">',
    '            <circle :cx="p.x * frameW" :cy="p.y * frameH" :r="Math.max(11, frameW*0.013)"',
    '                    fill="none" stroke="#f59e0b" :stroke-width="Math.max(2, frameW*0.0035)"',
    '                    stroke-dasharray="4 3" />',
    '          </template>',
    // 标定自检：把这份 H 算出来的球场线画回画面（对不上就是对不上，一眼可见）
    '          <template v-if="showCourtOverlay">',
    '            <polyline v-for="(ln,li) in overlayLines" :key="\'ov\'+li"',
    '              :points="ln.map(function(q){return q[0].toFixed(0)+\',\'+q[1].toFixed(0)}).join(\' \')"',
    '              fill="none" stroke="#f5222d" :stroke-width="Math.max(2, frameW*0.0035)"',
    '              stroke-dasharray="10 6" opacity="0.95" />',
    '          </template>',
    '        </svg>',
    // 篮圈示意（只在篮筐模式）
    '        <div v-if="markKind===\'hoop\' && markHoop" style="position:absolute;pointer-events:none;border:2px dashed #ef4444;border-radius:50%"',
    '             :style="{left:((markHoop.cx-markHoop.rx)*100)+\'%\',top:((markHoop.cy-markHoop.ry)*100)+\'%\',width:(markHoop.rx*200)+\'%\',height:(markHoop.ry*200)+\'%\'}"></div>',
    '      </div>',
    // 右画面：自己的坐标系、自己的点（另一侧半场）
    '      <div v-if="markKind===\'court\' && mfDualSplit" class="mf-panel-wrap"',
    '           :class="{ on: mfActive===1 }" data-side="1"',
    '           style="position:relative;width:100%;max-width:1100px;cursor:crosshair"',
    '           @click="onMarkClick" @mousemove="onMarkMove" @mouseleave="onMarkLeave">',
    '        <img v-if="mfCurrentRight" :src="mfCurrentRight.image" style="width:100%;height:auto;display:block;border-radius:6px" />',
    '        <div v-if="mfDualSplit" class="mf-badge">右画面{{ mfActive===1 ? \'（正在点这里）\' : \'\' }}</div>',
    '        <svg v-if="frameW && frameH" :viewBox="\'0 0 \' + frameW + \' \' + frameH"',
    '             preserveAspectRatio="none"',
    '             style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none">',
    '          <template v-for="(p,i) in mfPtsRight" :key="\'R\'+i">',
    '            <circle :cx="p.x * frameW" :cy="p.y * frameH" :r="Math.max(10, frameW*0.012)"',
    '                    fill="rgba(34,197,94,.35)" stroke="#22c55e" :stroke-width="Math.max(2, frameW*0.003)" />',
    '            <text :x="p.x * frameW + Math.max(12, frameW*0.014)" :y="p.y * frameH"',
    '                  fill="#22c55e" :font-size="Math.max(16, frameW*0.022)"',
    '                  style="paint-order:stroke;stroke:#000;stroke-width:3px">{{ i + 1 }}. {{ p.label }}</text>',
    '          </template>',
    '          <template v-for="(p,i) in snapPtsRight" :key="\'SR\'+i">',
    '            <circle :cx="p.x * frameW" :cy="p.y * frameH" :r="Math.max(11, frameW*0.013)"',
    '                    fill="none" stroke="#f59e0b" :stroke-width="Math.max(2, frameW*0.0035)"',
    '                    stroke-dasharray="4 3" />',
    '          </template>',
    '          <template v-if="showCourtOverlay">',
    '            <polyline v-for="(ln,li) in overlayLines" :key="\'ovr\'+li"',
    '              :points="ln.map(function(q){return q[0].toFixed(0)+\',\'+q[1].toFixed(0)}).join(\' \')"',
    '              fill="none" stroke="#f5222d" :stroke-width="Math.max(2, frameW*0.0035)"',
    '              stroke-dasharray="10 6" opacity="0.95" />',
    '          </template>',
    '        </svg>',
    '      </div>',
    '    </div>',
    // 已点清单（文字版，双重反馈）：球场模式按"哪一幅画面"分开列 ——
    // 左右两个画面的坐标不通用，混成一列会让人以为它们在同一套坐标里。
    '    <div v-if="markKind===\'court\' && mfPts.length" style="margin-top:8px">',
    '      <div class="hint">左画面（t={{ mfLeftCov.t }}s）：</div>',
    '      <div class="row" style="flex-wrap:wrap">',
    '        <el-tag v-for="(p,i) in mfFramePts(0)" :key="\'lp\'+i" size="small" type="success" effect="plain" style="margin:2px">',
    '          {{ i + 1 }}. {{ p.label }}</el-tag>',
    '        <span v-if="!mfFramePts(0).length" class="hint">（还没点）</span>',
    '      </div>',
    '      <template v-if="mfDualSplit">',
    '        <div class="hint" style="margin-top:6px">右画面（t={{ mfRightCov.t }}s）：</div>',
    '        <div class="row" style="flex-wrap:wrap">',
    '          <el-tag v-for="(p,i) in mfFramePts(1)" :key="\'rp\'+i" size="small" type="success" effect="plain" style="margin:2px">',
    '            {{ i + 1 }}. {{ p.label }}</el-tag>',
    '          <span v-if="!mfFramePts(1).length" class="hint">（还没点）</span>',
    '        </div>',
    '      </template>',
    '    </div>',
    '    <div v-else-if="markKind!==\'court\' && activePts.length" class="row" style="margin-top:8px;flex-wrap:wrap">',
    '      <el-tag v-for="(p,i) in activePts" :key="\'t\'+i" size="small" type="success" effect="plain" style="margin:2px">',
    '        {{ i + 1 }}. {{ p.label }} ({{ Math.round(p.x*frameW) }},{{ Math.round(p.y*frameH) }})</el-tag>',
    '    </div>',
    '    <template #footer>',
    '      <el-button v-if="markKind===\'court\'" size="small" @click="mfUndo" :disabled="!mfPts.length">撤销上一个点</el-button>',
    '      <el-button v-else size="small" @click="markUndo" :disabled="!activePts.length">撤销上一个</el-button>',
    // 跳过：**两种模式都要有**。以前球场的"跳过这一项"被 v-if 排除了，
    // 而球场点名里有"另一端的篮筐中心（若只有一个篮筐可见就别选它）"这类
    // 画面里根本不存在的点 —— 用户于是卡住：不点前进不了、点也点不出来。
    '      <el-button size="small" @click="markSkip" :disabled="markDone">跳过这一项</el-button>',
    // 清空：两个画面模式下「清空」只清当前这一个画面（另一边辛苦标的点不该被连坐），
    // 要全清就用后面那个「全部清空」。
    '      <el-button size="small" @click="markKind===\'court\' ? mfClear() : clearMarks()" :disabled="markKind===\'court\' ? !mfActivePtsCount : !activePts.length">',
    '        {{ markKind===\'court\' && mfDualSplit ? \'清空这个画面\' : \'清空\' }}</el-button>',
    '      <el-button v-if="markKind===\'court\' && mfDualSplit" size="small" @click="mfClearAll" :disabled="!mfPts.length">全部清空</el-button>',
    '      <el-button size="small" @click="markOpen=false">取消</el-button>',
    // 预览（不落盘）：先看清逐点误差与合规性，再决定要不要保存（移植自旧版标定页）
    '      <el-button v-if="markKind===\'court\'" size="small" :loading="markSaving"',
    '        :disabled="!(mfFrames.length ? (mfEnough && solvableFrames.length) : courtEnough)" @click="previewCourtMulti()">先预览（不保存）</el-button>',
    // 保存：篮筐模式标了中心就能存；球场模式要求**至少有一幅画面自己够 4 个点**
    // （以前只看总点数，于是"6 个点撒在 3 幅画面上"这种解不出来的点法也能点保存，
    //  点下去只会拿到一句"误差偏大"，用户根本不知道该改什么）
    '      <el-button size="small" type="primary" :loading="markSaving"',
    '        :disabled="markKind===\'court\' ? !(mfFrames.length ? (mfEnough && solvableFrames.length) : courtEnough) : !markHoop"',
    '        @click="markKind===\'court\' ? (mfFrames.length ? saveCourtMulti() : saveCourt()) : saveMarks()">',
    '        {{ markKind===\'court\' ? \'保存并解算标定\' : \'保存标点\' }}</el-button>',
    '    </template>',
    '  </el-dialog>',
    '</div>'
  ].join('\n')
};
