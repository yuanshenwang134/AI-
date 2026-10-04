"""单帧检测 → 旧版选球/筐与状态机。无视频 I/O，可离线重放。"""
from __future__ import annotations

import copy
import math
from .detector import Det, filter_by_size, pick_best, pick_rim
from .scorer import ScoreEngine

# 旧版 config.yaml 的有效默认值；不搬旧版的机器路径、UI 和估算三分。
DEFAULT_CONFIG = {
    'detect': {'imgsz': 640, 'ball_conf': .25, 'rim_min_conf': .35,
               'ball_min_w_ratio': .012, 'ball_max_w_ratio': .24,
               'ball_jump_px_per_frame': 90., 'rim_min_aspect': 1.2, 'center_lock_widths': 0.,
               # 候选出手层（只提示、不计分）的阈值；见 shot_candidates()
               'cand_window_s': .6, 'cand_rise_px': 25., 'cand_fall_px': 12.,
               'cand_near_rim_widths': 3., 'cand_dedupe_s': 1.2,
               'cand_close_start_widths': 3.5, 'cand_match_tol_s': 1.5, 'cand_max': 40},
    'score': {'enable_three_point': False, 'make_radius_ratio': .65,
              'make_exit_radius_ratio': 1., 'in_rim_below_ratio': .6,
              'make_window_s': .9, 'make_allow_occluded_entry': False,
              'make_window_from_crossing': False, 'approach_radius_ratio': 5.,
              'miss_timeout_s': 3.5, 'attempt_min_gap_s': .35,
              'leave_radius_ratio': 3., 'ball_recent_s': .5, 'suspected_make_max_gap_s': .7,
              'rim_max_jump_px': 0, 'rim_readopt_far_frames': 6,
              'rim_stale_s': 1.5, 'rim_need_confirm': 2,
              'ball_max_gap_frames': 5, 'release_lookback_s': 2.,
              'release_max_person_dist_ratio': .9}}

def rim_availability(frames, fps):
    """Observed tracker availability, not detector accuracy or shot recall.

    Old traces without the tracked_rim contract must not acquire invented
    coverage numbers. Intervals describe runs of sampled unavailable frames.
    """
    if not frames or any('tracked_rim' not in r for r in frames):
        return None
    usable = sum(bool((r.get('tracked_rim') or {}).get('fresh')) for r in frames)
    gaps = []
    start = None
    for row in frames:
        fresh = bool((row.get('tracked_rim') or {}).get('fresh'))
        if not fresh and start is None:
            start = float(row['t'])
        if fresh and start is not None:
            end = float(row['t'])
            if end - start >= 2.0:
                gaps.append(dict(start=round(start, 3), end=round(end, 3)))
            start = None
    if start is not None:
        end = float(frames[-1]['t']) + 1 / max(float(fps), 1)
        if end - start >= 2.0:
            gaps.append(dict(start=round(start, 3), end=round(end, 3)))
    return dict(sampled_frames=len(frames), usable_frames=usable,
                usable_fraction=usable / len(frames), unavailable_ranges=gaps,
                min_gap_s=2.0)

def ball_visibility(frames, fps):
    """球可见性摘要：哪几段**缺少球检测证据**（与筐可用性是两件事）。

    只统计"真实观测"（predicted=False）。用途是让用户知道"这段没看到球"，
    而不是把"没有记录"当成"没有投篮"。旧 trace 没有 ball 字段时不伪造数字。
    """
    if not frames or any('ball' not in r for r in frames):
        return None
    def observed(row):
        ball = row.get('ball') or {}
        return bool(ball.get('xyxy')) and not ball.get('predicted')
    seen = sum(1 for r in frames if observed(r))
    gaps, start, longest = [], None, 0.0
    for row in frames:
        t = float(row['t'])
        if not observed(row):
            if start is None:
                start = t
            longest = max(longest, t - start)
        elif start is not None:
            if t - start >= 2.0:
                gaps.append(dict(start=round(start, 3), end=round(t, 3)))
            longest = max(longest, t - start)
            start = None
    if start is not None:
        end = float(frames[-1]['t']) + 1 / max(float(fps), 1)
        if end - start >= 2.0:
            gaps.append(dict(start=round(start, 3), end=round(end, 3)))
        longest = max(longest, end - start)
    return dict(sampled_frames=len(frames), observed_frames=seen,
                observed_fraction=seen / len(frames), blind_ranges=gaps,
                longest_blind_s=round(longest, 3), min_gap_s=2.0,
                note='按真实球观测统计；predicted 帧不计，缺证据不等于没有投篮。')

def shot_candidates(frames, config, events=None):
    """**待核对的轨迹候选**（独立候选层）：只提示与回看，不参与出手次数与命中统计。

    为什么单独一层：现在的出手依赖"球接近一个可用篮筐"，球看不清、筐暂时失效、
    或篮下连续补篮时整次出手会被漏掉；而判定层（进/未中/未知）不该为了补召回而放宽。
    所以这里只做"发现"，判定仍由 ScoreEngine 负责。

    规则（用真实球观测，全部相对当时的筐归一）：
      1. **先按切镜与跟踪段断开**（cut 帧、evidence_segment / tracked_rim.segment 变化），
         禁止跨段拼接（否则切镜前后的坐标跳变会被误当成一条弧线）；
      2. 在 ±window 秒的滚动窗口里找局部顶点（y 最小 = 最高点）；
      3. 顶点前上升 ≥ rise_px、顶点后下落 ≥ fall_px；
      4. 该段轨迹曾接近篮筐（到筐心最小距离 ≤ near_rim_widths × 筐宽）；
      5. dedupe 秒内只留上升最高的一个；
      6. 弧线起点离筐 < close_start_widths × 筐宽 的记为 near 类（补篮/二次进攻/打铁反弹），
         其余记为 far 类 —— 两类都只是提示，不能自动算投篮；
      7. 每条候选与**已有事件**做关联（出手时刻最近、容差 match_tol_s），
         标出 matched / matched_event_t：已识别为出手的弧线不算"漏检"。
    另补一类 `vanish`：上升后球消失（对应飞出画面/被遮挡）。
    """
    if not frames or any('ball' not in r for r in frames):
        return None
    # 兜底用模块默认值（旧 trace 的 config 里可能没有新增的键，两处默认值不能分叉）
    d = {**DEFAULT_CONFIG['detect'], **((config or {}).get('detect') or {})}
    win = float(d.get('cand_window_s', .6))
    rise_min = float(d.get('cand_rise_px', 25.))
    fall_min = float(d.get('cand_fall_px', 12.))
    near_w = float(d.get('cand_near_rim_widths', 3.))
    dedupe = float(d.get('cand_dedupe_s', 1.2))
    close_w = float(d.get('cand_close_start_widths', 3.5))
    match_tol = float(d.get('cand_match_tol_s', 1.5))
    cap = int(d.get('cand_max', 40))

    obs = []
    block = 0
    prev_seg = None
    for r in frames:
        seg = ((r.get('evidence_segment') if 'evidence_segment' in r else None),
               ((r.get('tracked_rim') or {}).get('segment')))
        cut = bool(r.get('cut'))
        # 切镜标记来自"当前帧 vs 上一帧"的直方图/帧差突变（sources.py），
        # 也就是 **cut 标的是新镜头的首帧**：这一帧的检测属于新镜头，
        # 因此它必须开新段（不能划给上一段，否则仍会跨镜头拼接）。
        # 跟踪段变化（无 cut 标记时）同样在处理前开新段。
        if cut or (prev_seg is not None and seg != prev_seg):
            block += 1
        prev_seg = seg
        ball = r.get('ball') or {}
        box = ball.get('xyxy')
        if box and not ball.get('predicted'):
            tr = r.get('tracked_rim') or {}
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            persons = [p.get('xyxy') for p in (r.get('persons') or []) if p.get('xyxy')]
            # 球心是否落在某个人体框里：用来区分"球在飞行"与"球还被人抱着/贴着人"
            # （用户目视实测：场边人员抱球走动会产生候选）。没有人框时为 None（未知，不是"否"）。
            in_person = (any(pb[0] <= cx <= pb[2] and pb[1] <= cy <= pb[3] for pb in persons)
                         if persons else None)
            obs.append(dict(t=float(r['t']), x=cx, y=cy, block=block,
                            w=float(box[2] - box[0]), h=float(box[3] - box[1]),
                            conf=float(ball.get('conf') or 0.0), in_person=in_person,
                            rim=(tr if tr.get('fresh') else None),
                            rcx=tr.get('cx'), rcy=tr.get('cy'), rw=tr.get('w')))
    # 每一段里"球最后被真实观测到"的时刻：判断切镜是不是把观测截断了要用它，
    # **不是**用候选窗口的边界（窗口边界是算法截的，跟球有没有继续被看到无关）。
    block_last_ball = {}
    for p in obs:
        block_last_ball[p['block']] = max(block_last_ball.get(p['block'], -1e9), p['t'])
    # 该片段"正常球框"的尺度基准：全片真实球检出的宽度中位数。
    # 夜间的球框误检会给出 2~3 倍于此的大框（实测 night 20.621 窗口里是 97~158px 的竖长框，
    # 而该片段中位宽只有 56px），所以用比值而不是绝对值来标记。
    widths = sorted(p['w'] for p in obs)
    clip_ball_w = widths[len(widths) // 2] if widths else None
    # 切镜时刻（cut=True 的帧就是新镜头首帧）：用来判断"这条候选的证据是不是被切镜截断的"。
    # 实测 day 的 4.004s / 18.819s 就是观测刚结束画面就切走（差 0.33s / 0.13s），
    # 这种"看不到结果"与"球被遮挡/飞出画面"是两回事，必须分开说 —— 用户明确要求过。
    cut_times = [float(r['t']) for r in frames if r.get('cut')]

    def _evidence(seg_pts, apex):
        """候选窗口内的"球框身份"描述量：只描述证据，不参与任何判定。

        cut_truncated 用"顶点所在那一段里球最后被观测到的时刻"，而不是窗口边界：
        窗口边界是算法截出来的，跟"球还有没有被看到"无关（实测按窗口边界算会把
        day 26.86 这种完整弧线也误标成截断）。
        """
        n = len(seg_pts)
        if not n:
            return {}
        ws = sorted(p['w'] for p in seg_pts)
        w_med = ws[len(ws) // 2]
        tall = sum(1 for p in seg_pts if p['h'] > 1.3 * p['w']) / n
        known = [p for p in seg_pts if p['in_person'] is not None]
        in_person_frac = (sum(1 for p in known if p['in_person']) / len(known)) if known else None
        # 最长连续"球在人框外"的帧数：真实出手应当在离手后连续若干帧离开人体框；
        # 抱球走动时几乎为 0（实测 night 27.995 = 0 帧、24.458 = 2 帧）。
        run = best = 0
        for p in sorted(known, key=lambda q: q['t']):
            run = run + 1 if not p['in_person'] else 0
            best = max(best, run)
        t_last = max(p['t'] for p in seg_pts)
        # 只有"这条候选自己的证据**就是**该段里最后的球观测、且切镜紧接其后"才算被截断。
        # 两个条件缺一不可：
        #   - 只看窗口边界会误报（窗口是算法截的，球可能还在被观测）→ day 26.86 那种完整弧线被误标；
        #   - 只看"该段最后的球观测离切镜近"会泛滥（同一段里有多次出手）→ day 一次标出 8 条。
        cut_truncated = (t_last >= block_last_ball.get(apex['block'], t_last) - 1e-6
                         and any(-0.05 <= c - t_last <= 0.5 for c in cut_times))
        flags = []
        if tall >= 0.3:
            flags.append('ball_box_shape')          # 竖长框：多半把人也当成球了
        if clip_ball_w and w_med >= 1.5 * clip_ball_w:
            flags.append('ball_box_size')           # 比本片段正常球框大太多
        if in_person_frac is not None and in_person_frac >= 0.8:
            flags.append('ball_with_person')        # 球全程贴着人：像持球/走动
        if n < 5:
            flags.append('few_ball_observations')   # 球观测太少，轨迹本就不稳
        if cut_truncated:
            flags.append('cut_truncated')           # 证据被切镜截断：结果要去下一个镜头里找
        return dict(ball_obs=n, ball_box_w_med=round(w_med, 1),
                    ball_box_w_ratio=(round(w_med / clip_ball_w, 2) if clip_ball_w else None),
                    tall_box_frac=round(tall, 2),
                    in_person_frac=(round(in_person_frac, 2) if in_person_frac is not None else None),
                    outside_person_run=(best if known else None),
                    cut_truncated=cut_truncated,
                    evidence_flags=flags)
    def _scan(respect_block):
        """在 ±win 秒窗口里找局部顶点。respect_block=True 只用同一段内的点（主列表口径，
        避免把切镜前后两个镜头的球点拼成一条弧线）；False 时不受段边界限制。"""
        raw = []
        for o in obs:
            back = [p for p in obs if o['t'] - win <= p['t'] <= o['t']
                    and (not respect_block or p['block'] == o['block'])]
            fwd = [p for p in obs if o['t'] <= p['t'] <= o['t'] + win
                   and (not respect_block or p['block'] == o['block'])]
            if not back:
                continue
            seg_pts = back + fwd
            apex_is_min = o['y'] == min(p['y'] for p in seg_pts)
            near = None
            if any(p['rim'] for p in seg_pts):
                near = min(math.hypot(p['x'] - p['rcx'], p['y'] - p['rcy']) / max(p['rw'], 1e-6)
                           for p in seg_pts if p['rim']) <= near_w
            rise = max(p['y'] for p in back) - o['y']
            if len(fwd) >= 2:
                if not apex_is_min or rise < rise_min:
                    continue
                if near is False:
                    continue
                fall = max(p['y'] for p in fwd) - o['y']
                kind = 'arc' if fall >= fall_min else 'rise_only'
            else:
                # 顶点贴到尾：上升后球就没了（飞出画面/被遮挡）
                if rise < rise_min or near is False:
                    continue
                fall, kind = 0.0, 'vanish'
            start = min(back, key=lambda p: p['t'])
            sdist = None
            if start['rim']:
                sdist = math.hypot(start['x'] - start['rcx'], start['y'] - start['rcy']) / max(start['rw'], 1e-6)
            raw.append(dict(release_t=round(start['t'], 3), apex_t=round(o['t'], 3), kind=kind,
                            rise_px=round(rise), fall_px=round(fall), obs_frames=len(seg_pts),
                            # 窗口内的段数：主列表口径下恒为 1；不受段边界限制时反映真实跨度
                            blocks_in_window=len({p['block'] for p in seg_pts}),
                            start_dist_rims=round(sdist, 2) if sdist is not None else None,
                            klass=('near' if (sdist is not None and sdist < close_w)
                                   else 'far' if sdist is not None else 'unknown'),
                            **_evidence(seg_pts, o)))
        return raw

    def _dedupe(raw):
        raw.sort(key=lambda c: c['release_t'])
        out = []
        for c in raw:
            if out and c['release_t'] - out[-1]['release_t'] < dedupe:
                if c['rise_px'] > out[-1]['rise_px']:
                    out[-1] = c
                continue
            out.append(c)
        return out

    # 与已有事件关联：已识别为出手的弧线不是"漏检"（用户实测反馈：fixedcam 9 条里有 7 条
    # 就是已识别的出手，文案不能说"判定层没有建立出手"）。
    # 参照用**离手时刻** release_t（判定收尾时刻比离手晚 1~2 秒，直接比 t 会系统偏移）。
    events = [e for e in (events or []) if isinstance(e, dict)]

    def _associate(cands):
        for c in cands:
            best = None
            for e in events:
                ref = e.get('release_t')
                ref = float(ref if ref is not None else e.get('t'))
                dist = abs(c['release_t'] - ref)
                if best is None or dist < best[1]:
                    best = (e, dist, ref)
            if best is not None and best[1] <= match_tol:
                e, dist, ref = best
                c['matched'] = True
                c['matched_event_t'] = round(float(e['t']), 3)
                c['matched_event_release_t'] = round(ref, 3)
                c['matched_event_type'] = e.get('type')
                c['matched_delta_s'] = round(dist, 2)
            else:
                c['matched'] = False
                c['matched_event_t'] = None
                c['matched_event_release_t'] = None
                c['matched_event_type'] = None
                c['matched_delta_s'] = None
        return cands

    out = _associate(_dedupe(_scan(respect_block=True)))
    unmatched = [c for c in out if not c['matched']]

    # 证据被段边界从中间截断的候选：主列表为了保证"不跨镜头拼接"会把这些丢掉，但丢掉不等于
    # 不存在（用户要求：不能把看不清/没看到证据的时段当成检测完整）。所以按不受段限制的口径
    # 再算一遍，只取主列表里没有的顶点，**单独一份、单独计数**地列出来。
    # 注意段边界的两个来源：镜头切换（cut）与跟踪段变化（evidence_segment / 篮筐跟踪段），
    # 后者在没切镜的片段里也会出现（实测 game01 就是这种），所以叫"跨段"而不是"跨切镜"。
    primary_apex = {c['apex_t'] for c in out}
    loose_only = [c for c in _dedupe(_scan(respect_block=False)) if c['apex_t'] not in primary_apex]
    _associate(loose_only)
    for c in loose_only:
        c['cross_segment'] = c['blocks_in_window'] >= 2
    cross_seg = [c for c in loose_only if c['cross_segment']]
    truncated = len(out) > cap
    flag_counts = {}
    for c in out:
        for f in c.get('evidence_flags') or []:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    return dict(count=len(out), unmatched_count=len(unmatched), truncated=truncated,
                candidates=out[:cap], review_offset_s=1.0, match_tol_s=match_tol,
                cross_segment_count=len(cross_seg), cross_segment=cross_seg[:cap],
                cross_segment_truncated=len(cross_seg) > cap,
                # 不受段限制时多出来、但窗口并没有跨段的候选（=与主列表某条在时间上重叠、
                # 被去重合并掉的），这里只报个数、不重复列出。
                loose_only_same_segment=len(loose_only) - len(cross_seg),
                evidence_flag_counts=flag_counts,
                note=('待核对的轨迹候选：轨迹像投篮。matched=true 表示该候选的**离手时刻与某条已有'
                      '事件接近**（只说明时间上靠近，**不证明是同一次出手**，需人工确认）；'
                      'matched=false 的是**离手时刻附近没有任何事件**、更需要回看的候选。'
                      '两类都不计入出手次数与命中统计。cross_segment 是**另一份、单独计数**的列表：'
                      '这些轨迹的证据**跨越了镜头切换或跟踪段边界**（上升段在一个镜头里、顶点在'
                      '另一个镜头里），主列表为不跨段拼接而排除了它们；这不代表它们不真实，'
                      '必须靠目视核对，同样不计入出手次数与命中统计，也不计入 count/unmatched_count。'
                      'evidence_flags 是**证据描述**（球框形态/尺寸是否异常、球是否全程贴着人、'
                      '球观测是否过少、证据是否被切镜截断），用来判断"这条候选值不值得看、'
                      '看的时候要注意什么"，**不能**当成投篮与否的判定（实测真实投篮的窗口里'
                      '也可能有竖长框）。cut_truncated=true 表示**观测刚结束画面就切走**：'
                      '这一条的"进没进"要到下一个镜头里去找，不能因为本段看不到结果就说没进。'))


def unpack(det):
    return None if det is None else Det(det['cls'], det['conf'], tuple(det['xyxy']),
                                       det.get('track_id'), det.get('predicted',False))

def pack(det):
    # 不四舍五入：回放应使用同一次运行、同精度的检测。
    return None if det is None else dict(cls=det.cls_name, conf=det.conf,
        xyxy=list(det.xyxy), track_id=det.track_id, predicted=det.predicted)

class LegacyShotStream:
    def __init__(self, fps, width, config=None, manual_hoop=None, hint=None):
        self.config=copy.deepcopy(DEFAULT_CONFIG)
        for section, values in (config or {}).items():
            if section in self.config:self.config[section].update(values)
        self.fps=fps; self.width=width; self.engine=ScoreEngine(self.config,fps)
        self.manual_hoop=manual_hoop; self.hint=hint
        self.center_lock_width=None; self.center_rejected=0
        self.last_ball=None; self.last_frame=-999; self.vx=self.vy=0.
        self.events=[]; self.frames=[]; self.rim_seen=0; self.real_seen=0
        self.track_segment=0; self.transitions=[]; self.first_confirmed=None
        self.center_rejected_frames=0

    def tracked_state(self, t):
        r=self.engine.rim
        if not r.initialized:return None
        return dict(cx=r.cx,cy=r.cy,w=r.w,h=r.h,segment=self.track_segment,
                    fresh=r.fresh(t,self.engine.rim_stale_s),last_seen_t=r.last_seen_t)

    def hint_relation(self, state):
        if self.hint is None or state is None:return None
        dx=state['cx']-self.hint[0];dy=state['cy']-self.hint[1]
        return dict(distance_px=math.hypot(dx,dy),
                    inside_box=abs(dx)<=state['w']/2 and abs(dy)<=state['h']/2)


    def reset(self, frame, t):
        # 切镜结束已有候选并清空身份，禁止跨镜头拼接穿筐。
        previous=self.tracked_state(t)
        closed=self.engine.finalize(frame,t)
        self.events.extend(closed)
        self.transitions.append(dict(frame=frame,t=t,reason='cut',previous=previous,
                                     current=None,closed_attempts=len(closed)))
        self.engine=ScoreEngine(self.config,self.fps)
        self.last_ball=None; self.last_frame=-999; self.vx=self.vy=0.

    def update(self, frame, t, detections, persons=(), cut=False):
        if cut:self.reset(frame,t)
        d=self.config['detect'];s=self.config['score']
        gap=s['ball_max_gap_frames']
        if self.last_ball and frame-self.last_frame>max(gap,int(self.fps*s['ball_recent_s'])):
            self.last_ball=None; self.vx=self.vy=0.
        balls=filter_by_size([b for b in detections if ('ball' in b.cls_name.lower() or
            'basket' in b.cls_name.lower()) and b.conf>=d['ball_conf']],self.width,
            d['ball_min_w_ratio'],d['ball_max_w_ratio'])
        ball=pick_best(balls,(self.last_ball.cx,self.last_ball.cy) if self.last_ball else None)
        if ball and self.last_ball:
            delta=max(1,frame-self.last_frame)
            # Prediction is bounded by the existing extrapolation horizon.
            # Missing frames increase uncertainty, not the allowable speed linearly.
            horizon=min(delta,max(1,gap))
            px=self.last_ball.cx+self.vx*horizon;py=self.last_ball.cy+self.vy*horizon
            bound=d['ball_jump_px_per_frame']*math.sqrt(delta)
            if math.hypot(ball.cx-px,ball.cy-py)>bound:
                valid=[b for b in balls if math.hypot(b.cx-px,b.cy-py)<=bound]
                ball=max(valid,key=lambda b:b.conf) if valid else None
        if ball:
            if self.last_ball and 0<frame-self.last_frame<=10:
                delta=frame-self.last_frame
                self.vx=(ball.cx-self.last_ball.cx)/delta;self.vy=(ball.cy-self.last_ball.cy)/delta
            self.last_ball=ball;self.last_frame=frame;self.real_seen+=1
        elif self.last_ball and frame-self.last_frame<=gap:
            delta=frame-self.last_frame;b=self.last_ball
            ball=Det('basketball',0.,tuple(v+(self.vx if i%2==0 else self.vy)*delta
                        for i,v in enumerate(b.xyxy)),predicted=True)
        rims=[r for r in detections if any(k in r.cls_name.lower() for k in ('rim','hoop'))]
        # 完整指送到此处的 NMS 后候选，不包含检测器阈值以下的框。
        candidates=[dict(index=i,**pack(r),selection_reason='not_selected') for i,r in enumerate(rims)]
        candidate_by_id={id(r):c for r,c in zip(rims,candidates)}
        rejected_before=self.center_rejected
        for r in rims:
            if r.conf<d['rim_min_conf']:candidate_by_id[id(r)]['selection_reason']='low_confidence'
            elif (r.h>0 and r.w/r.h<d['rim_min_aspect']) or (self.hint and not self.engine.rim.initialized and r.h<=0):candidate_by_id[id(r)]['selection_reason']='invalid_shape'

        # 中心提示仅用于初次选筐，不把无尺寸的点变成静态篮筐。
        if self.hint and not self.engine.rim.initialized:
            valid=[r for r in rims if r.conf>=d['rim_min_conf'] and r.h>0 and r.w/r.h>=d['rim_min_aspect']]
            rims=sorted(valid,key=lambda r:math.hypot(r.cx-self.hint[0],r.cy-self.hint[1]))[:1]
            for r in valid:
                if not rims or r is not rims[0]:candidate_by_id[id(r)]['selection_reason']='not_nearest_hint'
        if self.hint and d['center_lock_widths'] > 0:
            valid=[]
            for r in rims:
                radius=d['center_lock_widths'] * (self.center_lock_width or r.w)
                if math.hypot(r.cx-self.hint[0],r.cy-self.hint[1]) <= radius:
                    valid.append(r)
                else:
                    self.center_rejected+=1
                    candidate_by_id[id(r)]["selection_reason"]="outside_center_lock"
            rims=valid
        rim,_=pick_rim(rims,min_conf=d['rim_min_conf'],min_aspect=d['rim_min_aspect'])
        if rim is not None:candidate_by_id[id(rim)]['selection_reason']='selected'
        selected_source='detector' if rim is not None else 'none'
        if self.manual_hoop is not None and self.manual_hoop.rx>0 and self.manual_hoop.ry>0:
            for c in candidates:
                if c['selection_reason']=='selected':c['selection_reason']='manual_override'
            selected_source='manual'
            h=self.manual_hoop
            rim=Det('rim',1.,(h.cx-h.rx,h.cy-h.ry,h.cx+h.rx,h.cy+h.ry))
        self.rim_seen+=int(rim is not None)
        previous=self.tracked_state(t)
        row=dict(frame=frame,t=t,ball=pack(ball),rim=pack(rim),persons=[pack(p) for p in persons],cut=cut,
                 rim_candidates=candidates,selected_rim_source=selected_source,
                 ball_candidates=[pack(b) for b in balls])
        new_events=self.engine.update(frame,t,ball,rim,list(persons))
        reason=self.engine.rim.last_update_reason
        if reason in ('initialized','reacquired_near','reacquired_far'):
            self.track_segment+=1
            state=self.tracked_state(t)
            self.transitions.append(dict(frame=frame,t=t,reason=reason,previous=previous,
                current=state,closed_attempts=len(new_events) if previous is not None else 0))
            if self.first_confirmed is None:
                self.first_confirmed=dict(t=t,tracked_rim=state,hint_relation=self.hint_relation(state))
        state=self.tracked_state(t)
        row.update(tracked_rim=state,tracker_reason=reason,hint_relation=self.hint_relation(state),
                   center_rejected_candidates=self.center_rejected-rejected_before,
                   evidence_segment=(previous['segment'] if previous and new_events and reason.startswith('reacquired') else self.track_segment))
        for c in candidates:
            c['tracker_reason']=reason if c['selection_reason']=='selected' else 'not_submitted'
        self.center_rejected_frames+=int(self.center_rejected>rejected_before)
        self.frames.append(row)
        self.events.extend(new_events)
        if self.center_lock_width is None and self.engine.rim.initialized:
            self.center_lock_width=self.engine.rim.w
        return ball

    def finish(self, frame, t):
        self.events.extend(self.engine.finalize(frame,t))
        return self.events

    def to_dict(self):
        return dict(version='hoopai_legacy_v1',trace_schema=2,fps=self.fps,config=self.config,
                    rim_tracking=self.tracking_summary(),
                    shot_candidates=shot_candidates(self.frames,self.config,self.events),
                    frames=self.frames,events=self.events,real_ball_frames=self.real_seen,
                    rim_frames=self.rim_seen, center_rejected=self.center_rejected,
                    center_lock_width=self.center_lock_width, center_hint=self.hint)

    def tracking_summary(self):
        last=self.frames[-1] if self.frames else {}
        return dict(schema=2,center_hint=self.hint,
            availability=rim_availability(self.frames,self.fps),
            ball_visibility=ball_visibility(self.frames,self.fps),
            lock_enabled=bool(self.hint is not None and self.config['detect']['center_lock_widths']>0),
            lock_requested=self.config['detect']['center_lock_widths']>0,
            lock_widths=self.config['detect']['center_lock_widths'],lock_width=self.center_lock_width,
            rejected_candidates=self.center_rejected,rejected_frames=self.center_rejected_frames,
            frames=len(self.frames),first_confirmed=self.first_confirmed,
            last_tracked=last.get('tracked_rim'),transitions=self.transitions,
            note='跟踪段变化不等于物理换筐；候选为检测器阈值和 NMS 后输出。')

def replay_trace(trace):
    """同一运行已选中的真实/预测框回放，不重跑检测器，不混用别的筐位。"""
    engine=ScoreEngine(trace['config'],trace['fps']);events=[]
    for row in trace['frames']:
        if row.get('cut'):
            events.extend(engine.finalize(row['frame'],row['t']))
            engine=ScoreEngine(trace['config'],trace['fps'])
        events.extend(engine.update(row['frame'],row['t'],unpack(row['ball']),unpack(row['rim']),
                                    [unpack(p) for p in row['persons']]))
    if trace['frames']:
        row=trace['frames'][-1]
        events.extend(engine.finalize(row['frame']+1,row['t']+1/trace['fps']))
    return events
