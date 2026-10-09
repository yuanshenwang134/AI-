/* ==========================================================================
   api.js —— 后端接口封装 + WebSocket 进度推送 + 演示数据降级
   --------------------------------------------------------------------------
   后端契约（Base: http://127.0.0.1:8000）：
     POST /api/jobs                             建任务  {job_id}
     GET  /api/jobs/{job_id}                    任务状态（轮询兜底）
     WS   /api/jobs/{job_id}/ws                 任务状态（每帧同一份 job JSON）
     GET  /api/jobs                             最近 20 条任务
     GET  /api/games/{job_id}                   game.json
     GET  /api/games/{job_id}/players           players.json
     GET  /api/games/{job_id}/shotchart         shotchart.json
     GET  /api/games/{job_id}/report            {markdown, json}
     GET  /api/games/{job_id}/highlights        {clips, index_url}
     GET  /api/games/{job_id}/export?fmt=...    csv_stats | csv_shots | json | report_md
     GET  /api/games/{job_id}/video             原始视频（支持 Range）
     GET  /api/media/{job_id}/{path}            高光片段等静态文件
     POST /api/games/{job_id}/shots/{i}/correct 人工复核改判 {made, value}
   --------------------------------------------------------------------------
   降级策略：探测不到后端（或跨域/断网）时，state.demoMode = true，
   所有读接口改读 web/demo/ 下的静态产物；写接口（导出/复核）改用本地
   生成 / 本地改判，保证无后端也能完整走完答辩演示流程。
   ========================================================================== */
(function () {
  'use strict';

  var qs = new URLSearchParams(location.search);
  var DEFAULT_BASE = 'http://127.0.0.1:8000';

  var API = {
    // 后端地址：?api=http://host:port 可覆盖（方便换机演示）
    base: (qs.get('api') || DEFAULT_BASE).replace(/\/+$/, ''),
    // 演示数据目录（相对本页，兼容 file:// 与任意静态托管路径）
    demoDir: './demo/',
    lastError: '',
    probeDetail: ''
  };

  API.isFileProtocol = location.protocol === 'file:';

  // ------------------------------------------------------------------
  // 底层 fetch
  // ------------------------------------------------------------------
  function withTimeout(ms) {
    // 老浏览器没有 AbortController 时退化为「不超时」
    if (typeof AbortController === 'undefined') return { signal: undefined, done: function () {} };
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, ms);
    return { signal: ctl.signal, done: function () { clearTimeout(timer); } };
  }

  API.getJSON = function (url, ms) {
    var t = withTimeout(ms || 20000);
    return fetch(url, { signal: t.signal, headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status + ' @ ' + url);
        return r.json();
      })
      .finally(t.done);
  };

  API.getText = function (url, ms) {
    var t = withTimeout(ms || 20000);
    return fetch(url, { signal: t.signal })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status + ' @ ' + url);
        return r.text();
      })
      .finally(t.done);
  };

  API.deleteJSON = function (url, ms) {
    var t = withTimeout(ms || 20000);
    return fetch(url, { method: 'DELETE', signal: t.signal })
      .then(function (r) {
        if (r.ok) return r.json();
        return r.json().catch(function () { return null; }).then(function (j) {
          var msg = (j && (j.detail || j.message)) || '';
          if (typeof msg !== 'string') { msg = JSON.stringify(msg); }
          throw new Error(msg || ('HTTP ' + r.status + ' @ ' + url));
        });
      })
      .finally(t.done);
  };

  API.postJSON = function (url, body, ms) {
    var t = withTimeout(ms || 30000);
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: t.signal
    }).then(function (r) {
      if (r.ok) return r.json();
      // 把服务端的 detail 带出来 —— 只显示 "HTTP 400" 等于没说，
      // 用户看到的是"保存不了"却不知道原因（实测踩过：
      // 后端明明回了"点共线或退化，请重新选点"，前端只显示 HTTP 400）。
      return r.json().catch(function () { return null; }).then(function (j) {
        var msg = (j && (j.detail || j.message)) || '';
        if (typeof msg !== 'string') { msg = JSON.stringify(msg); }
        throw new Error(msg || ('HTTP ' + r.status + ' @ ' + url));
      });
    }).finally(t.done);
  };

  // 演示数据里的静态文件（相对路径）
  function demo(path) { return API.demoDir + path; }

  // ------------------------------------------------------------------
  // 后端可用性探测：命中 /api/jobs（契约里的任务列表接口，最轻量）
  // ------------------------------------------------------------------
  API.probe = function () {
    return API.getJSON(API.base + '/api/jobs', 2500)
      .then(function (d) {
        API.lastError = '';
        API.probeDetail = 'GET /api/jobs 正常，返回 ' +
          (Array.isArray(d) ? d.length : Object.keys(d || {}).length) + ' 条任务';
        return true;
      })
      .catch(function (e) {
        API.lastError = (e && e.name === 'AbortError') ? '探测超时（后端没起来）' : String(e && e.message || e);
        API.probeDetail = API.isFileProtocol
          ? '当前为 file:// 打开，浏览器会拦截本地文件/接口读取'
          : API.lastError;
        return false;
      });
  };

  // ------------------------------------------------------------------
  // 任务：创建 / 查询 / 列表 / WS 订阅
  // ------------------------------------------------------------------
  API.createJob = function (payload) {
    return API.postJSON(API.base + '/api/jobs', payload);
  };

  /**
   * 直接用浏览器上传视频文件（可选路径）。
   * 后端契约：POST /api/upload，二进制 body，文件名放 x-filename 头，
   * 返回 {path, bytes, info} —— 拿到的 path 再填进 /api/jobs 的 video_path。
   * 用 XHR 而不是 fetch，是为了拿到上传进度（fetch 无法读上传进度）。
   *
   * **自动重试**：后端在自动重启（改完源码约 2 秒）或刚启动时会短暂不可用，
   * 那一刻的 XHR 只会抛一个含糊的「网络错误」。以前这里直接把它报成
   * 「网络或跨域被拦」，害人以为要改 CORS。现在失败会自动重试 3 次，
   * 仍然失败才报错，并且把"后端在不在"查清楚再给结论。
   */
  API.uploadVideo = function (file, onProgress) {
    function authHeaders() {
      return { 'x-filename': file.name, 'Content-Type': 'application/octet-stream' };
    }

    function onceXHR() {
      return new Promise(function (resolve, reject) {
        if (typeof XMLHttpRequest === 'undefined') {
          reject(new Error('当前环境不支持 XMLHttpRequest'));
          return;
        }
        var xhr = new XMLHttpRequest();
        xhr.open('POST', API.base + '/api/upload');
        xhr.setRequestHeader('x-filename', file.name);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.timeout = 120000;   // 1.2MB 视频正常 1 秒内传完；120s 是兜底
        if (xhr.upload && onProgress) {
          xhr.upload.onprogress = function (e) {
            if (e.lengthComputable) onProgress(e.loaded / e.total);
          };
        }
        xhr.onload = function () {
          if (xhr.status >= 200 && xhr.status < 300) {
            try { resolve(JSON.parse(xhr.responseText)); }
            catch (e) { reject(new Error('上传响应不是 JSON')); }
          } else {
            reject(new Error('HTTP ' + xhr.status + ' @ /api/upload'
              + (xhr.responseText ? '：' + String(xhr.responseText).slice(0, 200) : '')));
          }
        };
        // 网络层失败（后端没起 / 被代理或扩展拦 / 连接被重置）
        xhr.onerror = function () {
          var e = new Error('NETWORK');
          if (xhr.status) e.message += ' HTTP ' + xhr.status;
          e.retryable = true;
          reject(e);
        };
        xhr.ontimeout = function () {
          var e = new Error('上传超时（120 秒）');
          e.retryable = true;
          reject(e);
        };
        xhr.onabort = function () {
          var e = new Error('上传被浏览器取消');
          e.retryable = true;
          reject(e);
        };
        xhr.send(file);
      });
    }

    // XHR 失败时的兜底：fetch（有些浏览器扩展/代理只拦 XHR）。
    // fetch 读不了上传进度，所以只在最后失败时试一次。
    function onceFetch() {
      if (typeof fetch === 'undefined') {
        var e = new Error('当前环境不支持 fetch，无法回退上传');
        e.retryable = true;
        return Promise.reject(e);
      }
      if (onProgress) onProgress(0);
      return fetch(API.base + '/api/upload', {
        method: 'POST', headers: authHeaders(), body: file
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status + ' @ /api/upload');
        return r.json();
      }).catch(function (err) {
        err.retryable = true;
        throw err;
      });
    }

    var tries = 0;
    function attempt() {
      return onceXHR().catch(function (e) {
        if (!e.retryable || tries >= 2) throw e;
        tries += 1;
        if (onProgress) onProgress(0);
        return new Promise(function (r) { setTimeout(r, 700 * tries); })
          .then(attempt);
      });
    }

    return attempt().catch(function (e) {
      if (!e.retryable) throw e;
      return onceFetch().catch(function (e2) {
        // 后端在不在？在的话给「上传被中断」；不在的话给更明确的结论。
        return API.getJSON(API.base + '/api/health', 3000).then(function (h) {
          var msg = '上传被中断';
          if (h && h.stale) {
            msg += '：后端进程还在跑旧代码（code_loaded_at < code_rev），'
              + '请重新双击「启动后端.bat」，等自动重启完成后再试';
          } else {
            msg += '，请重试一次';
          }
          var detail = (e && e.message && e.message !== 'NETWORK')
            ? e.message : (e2 && e2.message) || '';
          if (detail) msg += '（' + detail + '）';
          throw new Error(msg);
        }, function () {
          throw new Error('后端没有响应（连接被拒）。'
            + '请确认后端的命令行窗口还开着；如果刚改过代码它可能在自动重启，'
            + '等几秒再试。注意这与「跨域」无关。');
        });
      });
    });
  };

  /** 后端能力探测：GET /api/health → {ok, ffmpeg, ultralytics, opencv, jobs} */
  API.health = function () {
    return API.getJSON(API.base + '/api/health', 3000);
  };

  /** 复核队列（后端可选接口）：GET /api/games/{id}/review */
  API.review = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId + '/review');
  };

  /** LLM 润色战报（可选）：POST /api/games/{id}/report/llm */
  API.reportLlm = function (jobId) {
    return API.postJSON(API.base + '/api/games/' + jobId + '/report/llm');
  };

  API.getJob = function (id) {
    return API.getJSON(API.base + '/api/jobs/' + encodeURIComponent(id));
  };
  API.listJobs = function (includeSynthetic) {
    return API.getJSON(API.base + '/api/jobs' +
      (includeSynthetic ? '?include_synthetic=true' : ''), 5000);
  };

  /**
   * 订阅任务进度：优先 WebSocket（每帧推送完整 job JSON）。
   * 返回 { close() }，后端不可用/WSS 连接失败时自动切到 1.2s 轮询。
   */
  API.watchJob = function (id, onFrame, onError) {
    var stopped = false, ws = null, timer = null;

    function poll() {
      if (stopped) return;
      API.getJob(id).then(function (j) {
        if (stopped) return;
        onFrame(j);
        if (j.status === 'done' || j.status === 'error') return;
        timer = setTimeout(poll, 1200);
      }).catch(function (e) {
        if (stopped) return;
        if (onError) onError(e);
        timer = setTimeout(poll, 2000);
      });
    }

    try {
      var wsUrl = API.base.replace(/^http/, 'ws') + '/api/jobs/' + encodeURIComponent(id) + '/ws';
      ws = new WebSocket(wsUrl);
      ws.onmessage = function (ev) {
        if (stopped) return;
        try { onFrame(JSON.parse(ev.data)); } catch (e) { /* 非 JSON 帧忽略 */ }
      };
      ws.onerror = function () {
        if (stopped) return;
        if (onError) onError(new Error('WebSocket 不可用，已切换轮询'));
        try { ws.close(); } catch (e) {}
        ws = null;
        poll();
      };
      ws.onclose = function () { /* 任务结束时后端会主动关闭，无需处理 */ };
    } catch (e) {
      poll();
    }

    return {
      close: function () {
        stopped = true;
        if (timer) clearTimeout(timer);
        if (ws) { try { ws.close(); } catch (e) {} }
      }
    };
  };

  // ------------------------------------------------------------------
  // 比赛产物：这些接口在演示模式下走 web/demo/*.json
  // ------------------------------------------------------------------
  /**
   * 判断后端给的片段路径能否直接作为浏览器 URL。
   * 后端返回的可能是绝对文件系统路径（Windows 的 D:\... 或 /home/...），
   * 这类路径浏览器无法访问，必须走 /api/media/{job_id}/{path} 或原始视频。
   */
  API.isWebPath = function (p) {
    p = String(p || '');
    if (!p) return false;
    if (/^[a-zA-Z]:[\\/]/.test(p)) return false;   // D:\out\highlights\a.mp4
    if (p.charAt(0) === '/') return false;         // /home/user/out/highlights/a.mp4
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(p)) return true;  // http(s)://
    return true;                                    // 相对路径
  };

  API.game = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId);
  };
  API.players = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId + '/players');
  };
  API.shotchart = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId + '/shotchart');
  };
  API.report = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId + '/report');
  };
  API.highlights = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId + '/highlights');
  };
  /** 战术层结论（控球/传球网络/阵型/空间） */
  API.tactics = function (jobId) {
    // 带时间戳：战术数据会被"重算战术层"原地覆盖，URL 不变的话浏览器会拿旧缓存
    return API.getJSON(API.base + '/api/games/' + jobId + '/tactics?_=' +
      Date.now(), 30000);
  };
  /** 俯视战术图逐帧数据（比较大，只在打开战术页时才拉） */
  API.tacticsFrames = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId +
      '/tactics/frames?_=' + Date.now(), 60000);
  };
  API.correctShot = function (jobId, index, payload) {
    return API.postJSON(API.base + '/api/games/' + jobId + '/shots/' + index + '/correct', payload);
  };
  API.addManualShot = function (jobId, payload) {
    return API.postJSON(API.base + '/api/games/' + jobId + '/shots/add', payload);
  };
  // 注意：这里原来叫 API.videoUrl(jobId)，而文件后面**又**定义了一个
  // API.videoUrl(videoPath)（标定页用），后一个把前一个覆盖掉 —— 于是总览页/高光页
  // 传 jobId 进去得到 `/api/video?video_path=<jobId>` 这种永远打不开的地址（实测发现）。
  // 现在拆成两个名字，各管一种入参：
  //   jobVideoUrl(jobId)  → /api/games/{jobId}/video  （按任务取原视频）
  //   videoUrl(videoPath) → /api/video?video_path=... （按文件路径取，标定页用）
  API.jobVideoUrl = function (jobId) { return API.base + '/api/games/' + jobId + '/video'; };
  API.mediaUrl = function (jobId, path) {
    return API.base + '/api/media/' + jobId + '/' + String(path).replace(/^\/+/, '');
  };
  API.exportUrl = function (jobId, fmt) {
    return API.base + '/api/games/' + jobId + '/export?fmt=' + fmt;
  };
  /** 重切高光片段（按已保存的时间码 + 源视频，不重新推理） */
  API.regenerateHighlights = function (jobId) {
    return API.postJSON(API.base + '/api/games/' + jobId + '/export/highlights', {});
  };
  /** 取视频某一秒的一帧（给「在画面上标点」用） */
  API.getFrame = function (videoPath, at) {
    return API.getJSON(API.base + '/api/frame?video_path=' +
      encodeURIComponent(videoPath) + '&at=' + encodeURIComponent(at || 1), 30000);
  };
  /** 保存画面标点（归一化坐标） */
  API.saveMarks = function (payload) {
    return API.postJSON(API.base + '/api/marks', payload);
  };
  /** 读回某视频已保存的标点 */
  API.getMarks = function (videoPath) {
    return API.getJSON(API.base + '/api/marks?video_path=' +
      encodeURIComponent(videoPath), 10000);
  };
  /** 某次分析里被判成进球的时刻（给「进球确认」用） */
  API.candidates = function (jobId) {
    return API.getJSON(API.base + '/api/games/' + jobId + '/candidates', 15000);
  };
  /** 提交「进没进」的人工判断（过滤误报 + 攒训练数据） */
  API.postFeedback = function (videoPath, items) {
    return API.postJSON(API.base + '/api/feedback',
      { video_path: videoPath, items: items });
  };
  /** 读回某视频的历史判断 */
  API.getFeedback = function (videoPath) {
    return API.getJSON(API.base + '/api/feedback' +
      (videoPath ? '?video_path=' + encodeURIComponent(videoPath) : ''), 10000);
  };
  /** 训练标注：读候选列表（candidates.json） */
  API.labelCandidates = function (path) {
    return API.getJSON(API.base + '/api/label/candidates?path=' +
      encodeURIComponent(path), 30000);
  };
  /** 训练标注：第 idx 个候选的证据图 URL（可直接给 <img src>） */
  API.labelSheetUrl = function (path, idx) {
    return API.base + '/api/label/sheet?path=' + encodeURIComponent(path) +
      '&idx=' + idx + '&_=' + Date.now();
  };
  /**
   * 训练标注：第 idx 个候选的**放大慢放视频** URL（给 <video src>）。
   * 用户明确说逐帧拼图看不懂，要视频 —— 所以默认走这个。
   */
  API.labelClipUrl = function (path, idx, zoom, slow) {
    return API.base + '/api/label/clip?path=' + encodeURIComponent(path) +
      '&idx=' + idx + '&zoom=' + (zoom || 4) + '&slow=' + (slow || 3) +
      '&_=' + Date.now();
  };
  /** 训练标注：记录一条「进没进」 */
  API.labelOne = function (payload) {
    return API.postJSON(API.base + '/api/label/labeled', payload);
  };
  /** 球场特征点清单（点几个就能标定这段视频的球场） */
  API.courtLandmarks = function () {
    return API.getJSON(API.base + '/api/court_landmarks', 10000);
  };
  /** 用球场特征点标定（>=4 个点） */
  API.saveCourtMarks = function (payload) {
    return API.postJSON(API.base + '/api/calibrate', payload);
  };
  /** 这段视频是否已有标定 */
  API.getCalibration = function (videoPath) {
    return API.getJSON(API.base + '/api/calibrate?video_path=' +
      encodeURIComponent(videoPath), 10000);
  };
  /** 撤销这段视频的标定（标错了要能重来；revision 做乐观锁） */
  API.deleteCalibration = function (videoPath, revision) {
    return API.deleteJSON(API.base + '/api/calibrate?video_path=' +
      encodeURIComponent(videoPath) + '&revision=' + (revision || 0));
  };
  /** 原片的播放地址（给标定页的 <video> 用；后端支持 Range 才能拖动定位） */
  API.videoUrl = function (videoPath) {
    return API.base + '/api/video?video_path=' + encodeURIComponent(videoPath);
  };
  /** 机器上现成的视频清单（示例素材 + 已上传）—— 新手上手第一步用 */
  API.samples = function () {
    return API.getJSON(API.base + '/api/samples', 20000);
  };
  /** 随机抽 N 个画面（多画面标定用）。给了 center 就抽那一时刻附近的帧
   *  —— 这几帧属于同一镜头，标点才能叠加到同一坐标系。 */
  API.getFrames = function (videoPath, n, center, span) {
    var u = API.base + '/api/frames?video_path=' +
      encodeURIComponent(videoPath) + '&n=' + (n || 6);
    if (center !== undefined && center !== null && center >= 0) {
      u += '&center=' + encodeURIComponent(center) +
           '&span=' + encodeURIComponent(span || 3);
    }
    return API.getJSON(u, 60000);
  };
  /** 自动挑两帧"同一镜头"的画面（标定页用）。
   *  为什么需要后端做：判定镜头有没有切要算 ORB 内点率，浏览器里做不了。
   *  两帧必须在同一镜头，合并解才成立（实测用户取到两个镜头 -> 必然矛盾）。 */
  API.framesStable = function (videoPath, n) {
    return API.getJSON(API.base + '/api/frames_stable?video_path=' +
      encodeURIComponent(videoPath) + '&n=' + (n || 8), 120000);
  };
  /** 多画面累加解标定（并返回逐个点误差） */
  API.calibrateAuto = function (payload) {
  return postJSON('/api/calibrate_auto', payload);
};
  /* 标定解算是**重操作**（要抓帧、跑投影线吻合度、跨镜头时还要定位切镜），
     30 秒的默认超时不够：后端还在算，浏览器先 abort，界面只会显示
     "signal is aborted without reason" —— 用户完全看不出是超时（实测踩到）。
     给足 180 秒；真出错时后端会自己返回错误，不需要靠超时来兜。 */
  API.calibrateMulti = function (payload) {
    return API.postJSON(API.base + '/api/calibrate_multi', payload, 180000);
  };

  /* ---- 比分牌：自动定位 / 手动框选 + OCR 读得分事件 ----
     为什么需要：模板匹配只认它标过的样式，非标准台标（校园/村 BA 的横条）
     读不出来；而"框出比分区域 + 放大 OCR"实测能稳定读出
     （这段素材读出 27:33 → 27:35 → 27:37 → 29:38，与画面逐帧一致）。
     返回里的 path 直接作为任务的 scoreboard_events 传给后端。 */
  API.scoreboardOcr = function (payload) {
    return API.postJSON(API.base + '/api/scoreboard/ocr', payload);
  };
  /** 这段视频是否已有 OCR 读出的得分事件（界面据此显示"已读比分牌"） */
  API.scoreboardEvents = function (videoPath) {
    return API.getJSON(API.base + '/api/scoreboard/events?video_path=' +
                       encodeURIComponent(videoPath), 8000);
  };

  // 演示模式下的高光片段：静态文件直接给链接
  API.demoMediaUrl = function (path) { return demo(String(path).replace(/^\/+/, '')); };

  // 演示模式下的战术产物（没有后端时战术页也能完整演示）
  API.demoTactics = function () {
    return API.getJSON(demo('tactics.json'), 15000)
      .catch(function () { return null; });
  };
  API.demoTacticsFrames = function () {
    return API.getJSON(demo('tactics_frames.json'), 30000)
      .catch(function () { return null; });
  };

  // ------------------------------------------------------------------
  // 演示数据加载（后端不可用时的完整降级路径）
  // ------------------------------------------------------------------
  API.loadDemo = function () {
    var missing = [];
    function soft(p) {
      return API.getJSON(demo(p), 6000).catch(function (e) { missing.push(p); API.lastError = String(e.message || e); return null; });
    }
    function softText(p) {
      return API.getText(demo(p), 6000).catch(function () { missing.push(p); return ''; });
    }
    return Promise.all([
      soft('game.json'),
      soft('players.json'),
      soft('shotchart.json'),
      soft('report.json'),
      softText('report.md'),
      soft('highlights.json')
    ]).then(function (r) {
      return {
        game: r[0],
        players: r[1],
        shotchart: r[2],
        report_json: r[3],
        reportMd: r[4],
        highlights: r[5],
        missing: missing
      };
    });
  };

  window.API = API;
})();
