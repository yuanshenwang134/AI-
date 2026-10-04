"""计分规则引擎（自研核心）。

设计要点（对应源码审查报告指出的 P1 问题）：
1. **穿筐证据链**：命中必须由"真实观测"构成——先真实观测到球进入筐口，随后真实观测到
   球仍在筐口横向范围内、正在下落、并落到筐底以下，且相邻观测的时间/位移都在合理区间。
   单帧跳变、球横向飞走都不会被计为命中。
2. **预测球不参与确认**：`Det.predicted=True` 的外推球只用于保持轨迹连续性，
   既不能置 `entered`，也不能确认命中。
3. **篮筐确认与过期**：筐需要连续两次位置一致的检测才被采纳；长时间未再观测则失效（不再就绪）；
   位置跳变超过上限的候选被剔除并计数（raw/accepted/rejected/stale 分开统计）。
4. **出手人提前锁定**：用最近若干秒的"球—人"历史回溯离手时刻，锁定出手人与脚下位置；
   没有证据就标"未知"（binding=none），而不是强行绑定篮筐附近的最近球员。
5. **去重靠"离开筐区"**：一次判罚结束后，必须重新观测到球离开筐周范围，才允许建立新的出手，
   从而支持补篮这类短间隔二次出手，而不是用全局冷却把后续出手全部屏蔽。
6. **丢球与片尾收尾**：球丢失不再让已有出手"悬空"；超时按证据充分程度判 `miss` 或 `uncertain`，
   视频结束时由 `finalize()` 统一收尾。
7. **距离是估算**：用"筐宽像素 ≈ 真实筐宽"做单目尺度换算，事件里明确标 `calibrated=false`，
   三分标记为待标定确认。

事件类型：`make`（命中）/ `miss`（未中）/ `uncertain`（证据不足，结果未知）。
这个模块产出的 event 列表是统计、热区、高光、AI 解说的统一数据源。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .detector import Det

EVENT_MAKE = "make"
EVENT_MISS = "miss"
EVENT_UNCERTAIN = "uncertain"


def _dist(x1: float, y1: float, x2: float, y2: float) -> float:
    return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5


@dataclass
class RimTracker:
    """篮筐跟踪：确认 → EMA 平滑 → 跳变剔除 → 超时失效。"""

    cx: float = 0.0
    cy: float = 0.0
    w: float = 0.0
    h: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    seen: int = 0
    rejected: int = 0
    raw: int = 0
    alpha: float = 0.35
    pending: tuple[float, float, float, float] | None = None
    pending_count: int = 0
    pending_frame: int = -1
    last_seen_t: float = -1e9
    last_seen_frame: int = -1e9
    # 筐"身份"稳定性（2026-09-17）：一场比赛画面里有两个篮筐，转播机位又会平移，
    # 实测筐会在两个篮筐之间来回跳（game_01 跨度 543px、game_06 跨度 1048px），
    # 于是"球接近当前筐"的判定横跨半个球场，传球也被记成出手。
    switch_count: int = 0                 # 换过几次"这是哪个筐"
    switch_times: list[float] = field(default_factory=list)
    identity_rejected: int = 0            # 因身份锁定被拒绝的候选数
    generation: int = 0  # 跟踪证据段编号，不等同于物理篮筐身份
    last_update_reason: str = "uninitialized"
    scale_rejected: int = 0               # 因尺度不相容被拒绝的候选数

    @property
    def initialized(self) -> bool:
        return self.seen > 0

    def update(self, rim: Det | None, t: float, frame_idx: int,
               jump_limit: float, need_confirm: int = 2, stale_s: float = 1.5,
               readopt_far: int | None = None, identity_lock: bool = False,
               switch_absent_s: float = 2.5, switch_confirm: int = 10,
               scale_tol: float = 0.0) -> None:
        self.last_update_reason = "no_candidate"
        if rim is None:
            self.pending = None
            self.pending_count = 0
            return
        self.raw += 1
        cand = (rim.cx, rim.cy, rim.w, rim.h)

        if not self.initialized or t - self.last_seen_t > stale_s:
            # 已经锁定过一个筐（identity_lock）时，远处的候选不再能随便顶替它：
            # 只有"旧筐连续 switch_absent_s 秒没被观测到"且"新候选连续 switch_confirm 帧一致"
            # 才允许换身份。这样两个篮筐会被当成**先后两段**（各算一段时间），而不是来回跳。
            # 拒绝期间 old 筐保持"不新鲜"，计分引擎就不会据此建立新出手（宁缺毋滥）。
            if identity_lock and self.initialized:
                if t - self.last_seen_t < switch_absent_s:
                    self.last_update_reason = "identity_wait"
                    self.identity_rejected += 1
                    return
                if scale_tol > 0 and self.w > 4:
                    ratio = cand[2] / self.w
                    if ratio < 1.0 / (1.0 + scale_tol) or ratio > 1.0 + scale_tol:
                        self.last_update_reason = "scale_rejected"
                        self.scale_rejected += 1
                        return
            # 需要连续 N 次位置一致的检测才采纳，避免单个误检"锁死"篮筐位置。
            # 注意：这里的"一致"只比较**相邻两帧的候选**，与已跟踪的位置无关——所以过期之后
            # 一个远处的候选同样能被采纳（实测有 165px / 735px / 905px 的瞬移）。
            # readopt_far 就是为这件事准备的：远距离重捕要求更多连续帧（默认等于 need_confirm，
            # 即保持原有行为）。见 eval/rim_tracking_audit.md 第三节 (a)。
            need = max(1, need_confirm)
            if identity_lock and self.initialized:
                need = max(need, int(switch_confirm))
            if (readopt_far and self.initialized
                    and _dist(self.cx, self.cy, cand[0], cand[1]) > jump_limit):
                need = max(need, int(readopt_far))
            consistent = (self.pending is not None and self.pending_frame == frame_idx - 1
                          and _dist(self.pending[0], self.pending[1], cand[0], cand[1]) <= jump_limit)
            self.pending_count = self.pending_count + 1 if consistent else 1
            self.pending = cand
            self.pending_frame = frame_idx
            self.last_update_reason = "pending_confirmation"
            if self.pending_count >= need:
                self.last_update_reason = ("initialized" if not self.initialized else
                    "reacquired_far" if _dist(self.cx,self.cy,cand[0],cand[1]) > jump_limit else "reacquired_near")
                self.generation += 1
                if identity_lock and self.initialized:
                    self.switch_count += 1
                    self.switch_times.append(round(float(t), 2))
                self._adopt(cand)
                self.seen += 1
                self.pending = None
                self.pending_count = 0
                self.last_seen_t = t
                self.last_seen_frame = frame_idx
            return

        if _dist(self.cx, self.cy, cand[0], cand[1]) > jump_limit:
            self.last_update_reason = "jump_rejected"
            self.rejected += 1
            return
        self.last_update_reason = "tracked"
        a = self.alpha
        self.cx = (1 - a) * self.cx + a * cand[0]
        self.cy = (1 - a) * self.cy + a * cand[1]
        self.w = (1 - a) * self.w + a * cand[2]
        self.h = (1 - a) * self.h + a * cand[3]
        self.seen += 1
        self.last_seen_t = t
        self.last_seen_frame = frame_idx
        self.top = self.cy - self.h / 2.0
        self.bottom = self.cy + self.h / 2.0

    def _adopt(self, cand: tuple[float, float, float, float]) -> None:
        self.cx, self.cy, self.w, self.h = cand
        self.top = self.cy - self.h / 2.0
        self.bottom = self.cy + self.h / 2.0

    def fresh(self, t: float, stale_s: float) -> bool:
        """筐是否"当前有效"：已确认、宽度合理、且最近仍在被观测。"""
        return self.initialized and self.w > 4 and (t - self.last_seen_t) <= stale_s


@dataclass
class Attempt:
    start_frame: int
    start_t: float
    release_xy: tuple[float, float]
    dist_est_m: float
    player_id: int | None
    binding: str = "none"                       # history / rim_nearest / none
    ball_release_xy: tuple[float, float] | None = None
    entered: bool = False                       # 仅由真实观测置位
    entered_frame: int | None = None
    entered_t: float | None = None
    entered_cy: float | None = None             # 置位 entered 那一帧的球心 y（备用证据用）
    real_obs: int = 0
    last_real_t: float = -1e9
    last_real_cy: float | None = None
    last_real_xy: tuple[float, float] | None = None
    crossing_t: float | None = None
    near_rim_descent_y: float | None = None
    rim_rebound_seen: bool = False
    suggestion_observation: tuple | None = None  # t, raw relative x/y, width, height
    suggestion_start_t: float | None = None
    suggestion_last_y: float | None = None
    suggestion_count: int = 0
    review_t: float | None = None
    left_streak: int = 0          # "离开筐区/掉到筐下"连续成立的真实观测数（防单帧异常）


class ScoreEngine:
    def __init__(self, cfg: dict[str, Any], fps: float) -> None:
        s = cfg.get("score", {}) or {}
        self.fps = fps or 30.0
        self.rim_real_width_m = float(s.get("rim_real_width_m", 0.45))
        self.three_point_m = float(s.get("three_point_m", 6.30))
        self.enable_three_point = bool(s.get("enable_three_point", True))
        self.make_radius_ratio = float(s.get("make_radius_ratio", 0.65))
        self.entry_radius_ratio = float(s.get("entry_radius_ratio", 1.0))
        self.entry_band_ratio = float(s.get("entry_band_ratio", 1.0))
        self.make_exit_radius_ratio = float(s.get("make_exit_radius_ratio", 1.0))
        self.in_rim_below_ratio = float(s.get("in_rim_below_ratio", 0.60))
        self.approach_radius_ratio = float(s.get("approach_radius_ratio", 5.0))
        self.make_window_s = float(s.get("make_window_s", 0.9))
        # Experimental: improves dunks but failed a free-throw regression; opt-in only.
        self.make_window_from_crossing = bool(s.get("make_window_from_crossing", False))
        # Experimental (default off): accept "observed inside the rim, then observed below it"
        # as make evidence when no downward crossing of the rim centre was ever observed,
        # which is what happens when the ball is occluded for a few frames at the hoop.
        # See eval/diagnose_unseen_make.md; must pass a full regression before becoming default.
        self.make_allow_occluded_entry = bool(s.get("make_allow_occluded_entry", False))
        # Experimental (default off): tighten the two thresholds that caused user-reported
        # false positives on real footage (a pass near the hoop counted as a shot; a ball
        # crossing in front of the rim counted as a make). See eval/strict_shot_gate.md.
        #   a) an attempt now requires the ball to be observed ABOVE the rim centre
        #      (was: above the rim bottom edge), which cannot lose a confirmable make
        #      because the make rule already needs an observed downward crossing of rim.cy;
        #   b) a make now requires the ball centre within make_radius_ratio x rim width
        #      (was: a full rim width, i.e. the ball could be half a rim off-centre).
        self.strict_shot_gate = bool(s.get("strict_shot_gate", False))
        # 筐身份锁（2026-09-17，默认关闭，需回归通过后再开）：比赛画面里有两个篮筐，
        # 转播平移时实测筐会在两者之间来回跳，导致"球接近筐"的判定横跨半个球场。
        # 打开后：旧筐连续 rim_switch_absent_s 秒没被观测到、且新候选连续 rim_switch_confirm 帧
        # 一致，才允许换身份；拒绝期间旧筐保持"不新鲜"，计分引擎不会据此建立新出手。
        # rim_switch_scale_tol>0 时还要求新候选的筐宽与旧筐相差不超过该比例。
        self.rim_identity_lock = bool(s.get("rim_identity_lock", False))
        self.rim_switch_absent_s = float(s.get("rim_switch_absent_s", 2.5))
        self.rim_switch_confirm = int(s.get("rim_switch_confirm", 10))
        self.rim_switch_scale_tol = float(s.get("rim_switch_scale_tol", 0.0))
        self.miss_timeout_s = float(s.get("miss_timeout_s", 3.5))
        self.attempt_min_gap_s = float(s.get("attempt_min_gap_s", 0.35))
        self.leave_radius_ratio = float(s.get("leave_radius_ratio", 3.0))
        self.rim_stale_s = float(s.get("rim_stale_s", 1.5))
        self.rim_need_confirm = int(s.get("rim_need_confirm", 2))
        self.rim_max_jump_px = float(s.get("rim_max_jump_px") or 0)
        self.ball_recent_s = float(s.get("ball_recent_s", 0.5))
        self.suspected_make_max_gap_s = float(s.get("suspected_make_max_gap_s", 0.7))
        self.release_lookback_s = float(s.get("release_lookback_s", 2.0))
        self.release_max_person_dist_ratio = float(s.get("release_max_person_dist_ratio", 0.9))

        self.rim = RimTracker()
        # 远距离重捕需要的连续一致帧数（默认 2 = 原有行为）。见 eval/rim_tracking_audit.md 第三节 (a)。
        self.rim_readopt_far_frames = int(s.get("rim_readopt_far_frames", 6) or 0)
        self.attempt: Attempt | None = None
        self.last_close_t = -1e9
        self.last_close_type = ""
        self.needs_leave = False
        self.leave_streak = 0
        self.last_observed_t = -1e9
        self.events: list[dict[str, Any]] = []
        self.ball_hist: deque[tuple[float, float, float, float]] = deque(maxlen=24)
        self.recent_real: deque[tuple[float, float]] = deque(maxlen=5)
        # 目标历史：用于回溯"离手时刻"（球—人最接近的时刻）
        self.history: deque[dict[str, Any]] = deque(maxlen=int(max(30, self.fps * 3)))

    # ------------------------------------------------------------------ 工具
    def _dist_est_m(self, xy: tuple[float, float]) -> float:
        if self.rim.w <= 1:
            return 0.0
        return round(_dist(xy[0], xy[1], self.rim.cx, self.rim.cy) / self.rim.w * self.rim_real_width_m, 2)

    def _points(self, dist_est_m: float) -> tuple[int, bool]:
        """返回 (得分, 是否已标定确认)。三分目前是单目估算 → 未确认。"""
        if self.enable_three_point and dist_est_m >= self.three_point_m:
            return 3, False
        return 2, True

    def _approach_r(self) -> float:
        return max(self.rim.w * self.approach_radius_ratio, 30.0)

    def _emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)

    def _bind_shooter(self, t: float) -> tuple[tuple[float, float], int | None, str, float]:
        """回溯历史，锁定出手人与离手时刻。

        规则：在回溯窗口内统计"每个球员与球的接触帧数"，**持球时间最长的那个球员**判为出手人，
        其**最后一次接触时刻**作为离手点。这样既不会被"球飞行途中路过别人"误导，
        也不会把运球阶段的起点当成离手时刻。
        返回 (脚下位置, 球员ID, 绑定来源, 离手时刻)。
        """
        snaps = [s for s in self.history if t - s["t"] <= self.release_lookback_s]
        if not snaps:
            return (0.0, 0.0), None, "none", t
        contact_count: dict[int, int] = {}
        last_contact: dict[int, tuple[float, float, float]] = {}   # pid -> (t, cx, feet_y)
        for s in snaps:
            bx, by = s["ball_xy"]
            for pid, pcx, feet_y, ph in s["persons"]:
                if pid is None:
                    continue
                d = _dist(bx, by, pcx, feet_y)
                if ph > 0 and d > ph * self.release_max_person_dist_ratio:
                    continue
                contact_count[pid] = contact_count.get(pid, 0) + 1
                last_contact[pid] = (s["t"], pcx, feet_y)
        if not contact_count:
            return (0.0, 0.0), None, "none", t
        pid = max(contact_count, key=lambda k: (contact_count[k], last_contact[k][0]))
        rel_t, pcx, feet_y = last_contact[pid]
        return (pcx, feet_y), pid, "history", rel_t

    def _smooth_xy(self, ball: Det) -> tuple[float, float]:
        """用最近 3 次真实观测的中位数做几何判定，抑制单帧误检造成的跳变。"""
        pts = list(self.recent_real)[-3:]
        if len(pts) >= 3:
            xs = sorted(p[0] for p in pts)
            ys = sorted(p[1] for p in pts)
            return xs[1], ys[1]
        return ball.cx, ball.cy

    def _observe_suspected_make(self, at: Attempt, t: float, ball: Det) -> None:
        """Raw measured samples only; never fabricate a measured crossing.

        Use each sample's contemporaneous hoop coordinates. Missing or predicted
        frames cannot count toward the two measured descending observations.
        The result only annotates an eventual unknown event; it never closes an
        attempt early or changes confirmed outcomes.
        """
        x, y = ball.cx - self.rim.cx, ball.cy - self.rim.cy
        below = abs(x) <= self.rim.w * .5 and y > self.rim.h * .5
        previous = at.suggestion_observation
        if previous is not None:
            pt, px, py, pw, ph = previous
            dt = t - pt
            if (at.suggestion_start_t is not None and 0 < dt <= self.ball_recent_s
                    and below and y > at.suggestion_last_y):
                at.suggestion_count += 1
                if at.suggestion_count >= 2:
                    at.review_t = at.suggestion_start_t
            else:
                # A rise, lateral departure or another gap breaks this shape.
                at.suggestion_start_t = None
                at.suggestion_count = 0
                at.review_t = None
                if (self.ball_recent_s < dt <= self.suspected_make_max_gap_s
                        and abs(px) <= pw * .5 and -1.5 * ph <= py <= 0
                        and below):
                    at.suggestion_start_t = t
                    at.suggestion_count = 1
            at.suggestion_last_y = y
        at.suggestion_observation = (t, x, y, self.rim.w, self.rim.h)

    # ------------------------------------------------------------------- 主循环
    def update(self, frame_idx: int, t: float, ball: Det | None, rim: Det | None,
               persons: list[Det]) -> list[dict[str, Any]]:
        new_events: list[dict[str, Any]] = []
        previous_generation = self.rim.generation
        jump_limit = self.rim_max_jump_px if self.rim_max_jump_px > 0 else max(60.0, 3.0 * max(self.rim.w, 1.0))
        self.rim.update(rim, t, frame_idx, jump_limit, self.rim_need_confirm, self.rim_stale_s,
                        readopt_far=self.rim_readopt_far_frames,
                        identity_lock=self.rim_identity_lock,
                        switch_absent_s=self.rim_switch_absent_s,
                        switch_confirm=self.rim_switch_confirm,
                        scale_tol=self.rim_switch_scale_tol)
        if (previous_generation > 0 and self.rim.generation != previous_generation
                and self.rim.fresh(t, self.rim_stale_s)):
            # A reacquired hoop starts a new evidence chain, never a cross-scene make.
            if self.attempt is not None:
                new_events.append(self._close(frame_idx, t, EVENT_UNCERTAIN, None))
            self.recent_real.clear()
            self.history.clear()
            self.ball_hist.clear()
            self.needs_leave = False
            if new_events:
                return new_events

        real_ball = ball is not None and not ball.predicted

        # 记录历史（仅真实观测，供回溯出手人使用）
        if real_ball:
            # A median must not mix old observations with a newly acquired ball.
            if t - self.last_observed_t > self.ball_recent_s:
                self.recent_real.clear()
            self.last_observed_t = t
            self.history.append({
                "frame": frame_idx, "t": t, "ball_xy": (ball.cx, ball.cy),
                "persons": [(p.track_id, p.cx, p.xyxy[3], p.h) for p in persons],
            })
            self.recent_real.append((ball.cx, ball.cy))

        if not self.rim.fresh(t, self.rim_stale_s):
            before = len(self.events)
            self._advance_without_rim(t, ball, frame_idx)
            return new_events + self.events[before:]

        # After an observed return below the hoop, a prolonged detection gap
        # ends this evidence chain. Do not bridge a rebound possession into
        # the next attempt, and do not invent a miss from missing observations.
        at = self.attempt
        if (at is not None and at.entered and at.last_real_xy is not None
                and at.last_real_xy[1] > self.rim.bottom
                and t - at.last_real_t > self.ball_recent_s):
            new_events.append(self._close(frame_idx, t, EVENT_UNCERTAIN, None,
                                          reason="below_rim_tracking_gap"))
            self.needs_leave = False
            self.recent_real.clear()
            self.history.clear()
            self.ball_hist.clear()

        # 允许新出手的前提：上一次判罚后，球必须重新离开筐周（支持补篮，屏蔽重复计数）。
        # 阈值用独立的 leave_radius_ratio，避免比"出手判定半径"还大而导致后续出手永远建立不了。
        leave_r = max(self.rim.w * self.leave_radius_ratio, 40.0)
        if self.needs_leave:
            # Two observations, including a return below the basket, allow a new
            # possession. One distant detection must not unlock and start a shot.
            away = real_ball and (abs(ball.cx - self.rim.cx) > leave_r
                    or ball.cy > self.rim.bottom + self.rim.h * 3.0)
            self.leave_streak = self.leave_streak + 1 if away else 0
            if self.leave_streak >= 2:
                self.needs_leave = False
                self.recent_real.clear()
                self.recent_real.append((ball.cx, ball.cy))
                return new_events

        if ball is None:
            before = len(self.events)
            self._advance_without_ball(t, frame_idx)
            return new_events + self.events[before:]

        self.ball_hist.append((t, ball.cx, ball.cy, ball.w))
        bx, by = self._smooth_xy(ball) if real_ball else (ball.cx, ball.cy)

        hd = abs(bx - self.rim.cx)
        above = by < self.rim.cy
        approach_r = self._approach_r()
        # "入筐证据"判定放宽：只要观测到球进入筐口一带即可（球速快时只有 1~2 帧落在窄带里）
        inside_rim = (
            hd < self.rim.w * self.entry_radius_ratio
            and self.rim.top - self.rim.h * self.entry_band_ratio
            <= by <= self.rim.bottom + self.rim.h * self.entry_band_ratio
        )
        below_rim = by > self.rim.bottom + self.rim.h * self.in_rim_below_ratio
        far_below = by > self.rim.bottom + self.rim.h * 3.0
        near_rim_at_close = hd <= self.rim.w * 1.5 and abs(by - self.rim.cy) <= self.rim.h * 2.0

        # 1) 建立一次出手（真实观测且球未掉到筐下）
        #    严格模式：球必须被观测到**高于筐心**才算一次出手。传球（尤其是胸前的横传）
        #    通常不会升到筐心以上，因此不再被记成"出手→未中"（用户实测反馈：队友传球被判成投篮）。
        #    这不会漏掉任何本来能确认的命中——命中本来就必须有一对真实观测跨过筐心高度。
        approach_top = self.rim.cy if self.strict_shot_gate else self.rim.bottom
        if (real_ball and self.attempt is None and not self.needs_leave
                and (t - self.last_close_t) >= self.attempt_min_gap_s
                and hd < approach_r and by < approach_top):
            if real_ball:
                origin, pid, binding, release_t = self._bind_shooter(t)
                if binding != "history":
                    origin, pid, release_t = (bx, by), None, t
            else:
                origin, pid, binding, release_t = (bx, by), None, "none", t
            self.attempt = Attempt(
                start_frame=frame_idx, start_t=release_t, release_xy=origin,
                dist_est_m=self._dist_est_m(origin), player_id=pid, binding=binding,
                ball_release_xy=(bx, by),
            )

        if self.attempt is None:
            return new_events

        at = self.attempt
        if real_ball:
            self._observe_suspected_make(at, t, ball)
            at.real_obs += 1
            previous_real_t = at.last_real_t
            at.last_real_t = t
            # The rim width is a diameter, not a radius. A previous approach is
            # insufficient: a recent observed downward segment must cross the aperture.
            previous_xy = at.last_real_xy
            continuous = 0 < t - previous_real_t <= self.ball_recent_s
            if previous_xy is not None and continuous:
                px, py = previous_xy
                # Only a downward approach followed by a substantial rise is a
                # rebound. Upward dunk preparation alone must not trigger it.
                relative_y = by - self.rim.cy
                if (by > py and hd <= self.rim.w / 2
                        and -ball.h <= relative_y <= self.rim.h):
                    at.near_rim_descent_y = (relative_y if at.near_rim_descent_y is None
                                             else max(at.near_rim_descent_y, relative_y))
                if (at.near_rim_descent_y is not None and by < py
                        and at.near_rim_descent_y - relative_y > max(ball.h * .5, 3.0)):
                    at.rim_rebound_seen = True
                if by < py and by < self.rim.cy:
                    at.crossing_t = None
                if py <= self.rim.cy < by and t - previous_real_t <= self.ball_recent_s:
                    fraction = (self.rim.cy - py) / (by - py)
                    crossing_x = px + fraction * (bx - px)
                    if abs(crossing_x - self.rim.cx) <= self.rim.w / 2:
                        at.crossing_t = t
            at.last_real_xy = (bx, by)
            # 2) 进入筐口（只有真实观测能置位）
            if inside_rim:
                at.entered = True
                if at.entered_frame is None:
                    at.entered_frame = frame_idx
                    at.entered_t = t
                    at.entered_cy = by

            prev_cy = at.last_real_cy
            # 3) 出筐时窗从真实向下穿越筐口开始，而不是从较宽的接近区域开始。
            # 慢速接近或扣篮准备阶段不应提前耗尽穿筐后的确认时间。
            crossing_ok = (at.crossing_t is not None and (t - at.crossing_t) <= self.make_window_s)
            # 备用证据（实验开关，默认关闭）：球在筐口内被真实观测到之后，在时窗内又被真实观测到
            # 落到筐底以下、横向仍在筐口内、且比"进入筐口那一帧"更低。用于球进筐瞬间被筐沿/球网/
            # 球员遮挡几帧、永远拿不到"向下穿越筐心"观测的情形（见 eval/diagnose_unseen_make.md）。
            # 必须由完整回归证明不会把"其实没进"的球判成命中，才允许考虑设为默认。
            occluded_ok = (
                self.make_allow_occluded_entry and not crossing_ok
                and at.entered_cy is not None and by > at.entered_cy
                and at.entered_t is not None and (t - at.entered_t) <= self.make_window_s
            )
            # 严格模式：命中要求球心在筐心附近（筐宽 × make_radius_ratio，默认 0.65），
            # 而不是原来的"偏出整整一个筐宽也算"。用户实测反馈"篮球划框而过却被判进球"，
            # 单目视角下球从筐前/筐后掠过时，二维轨迹会和"进筐"长得一样，
            # 收紧横向容差是最直接的一道防线（见 eval/strict_shot_gate.md）。
            exit_ratio = (self.make_radius_ratio if self.strict_shot_gate
                          else self.make_exit_radius_ratio)
            if (at.entered and below_rim and hd <= self.rim.w * exit_ratio
                    and (crossing_ok or occluded_ok)
                    and continuous and prev_cy is not None and by > prev_cy
                    and ((self.make_window_from_crossing and not at.rim_rebound_seen)
                         or (at.entered_t is not None and (t - at.entered_t) <= self.make_window_s))
                    and at.real_obs >= 2):
                # 遮挡兜底只提供建议；缺少实测向下穿越，不自动计分。
                inferred = occluded_ok and not crossing_ok
                new_events.append(self._close(
                    frame_idx, t, EVENT_UNCERTAIN if inferred else EVENT_MAKE, ball,
                    reason="occluded_entry_suggestion" if inferred else None,
                    suggested_made=True if inferred else None))
                return new_events

            # 4) 未中：需要"连续两帧"都成立，避免单帧误检把球判成飞出筐区
            left_region = hd > approach_r * 1.6 and not above
            if left_region or far_below:
                at.left_streak += 1
            else:
                at.left_streak = 0
            timed_out = (t - at.start_t) > self.miss_timeout_s
            if (at.left_streak >= 2 or timed_out) and (t - at.start_t) >= 0.35:
                # 球已到筐口一带但没拿到"穿筐"证据 → 证据不足，记未知而不是未中
                if ((at.entered and at.crossing_t is None)
                        or ((not at.entered) and (near_rim_at_close or (t - at.last_real_t) > self.ball_recent_s))):
                    kind = EVENT_UNCERTAIN
                else:
                    kind = EVENT_MISS
                new_events.append(self._close(frame_idx, t, kind, ball))
                return new_events
            at.last_real_cy = by
        else:
            # 预测球：只推进时间，绝不确认结果
            if (t - at.start_t) > self.miss_timeout_s:
                kind = EVENT_UNCERTAIN if (at.entered and at.crossing_t is None) or (t - at.last_real_t) > self.ball_recent_s else EVENT_MISS
                new_events.append(self._close(frame_idx, t, kind, ball))
        return new_events

    # --------------------------------------------------------- 无球 / 无筐推进
    def _advance_without_ball(self, t: float, frame_idx: int) -> None:
        at = self.attempt
        if at is None:
            return
        if (t - at.start_t) > self.miss_timeout_s:
            kind = EVENT_UNCERTAIN if (at.entered and at.crossing_t is None) or (t - at.last_real_t) > self.ball_recent_s else EVENT_MISS
            self._close(frame_idx, t, kind, None)

    def _advance_without_rim(self, t: float, ball: Det | None, frame_idx: int) -> None:
        """篮筐失效期间不产生新判罚，但已有出手要按时间收尾，避免悬空。"""
        at = self.attempt
        if at is None:
            return
        if (t - at.start_t) > self.miss_timeout_s:
            self._close(frame_idx, t, EVENT_UNCERTAIN, ball)

    def finalize(self, frame_idx: int, t: float) -> list[dict[str, Any]]:
        """视频结束收尾：未决出手记为 uncertain，而不是静默丢弃。"""
        if self.attempt is None:
            return []
        return [self._close(frame_idx, t, EVENT_UNCERTAIN, None)]

    # ------------------------------------------------------------------- 收尾
    def _close(self, frame_idx: int, t: float, kind: str, ball: Det | None,
               reason: str | None = None, suggested_made: bool | None = None) -> dict[str, Any]:
        assert self.attempt is not None
        at = self.attempt
        if kind == EVENT_UNCERTAIN and at.review_t is not None:
            suggested_made = True
            reason = "occlusion_descent_suggestion"
        points, verified = (self._points(at.dist_est_m) if kind == EVENT_MAKE else (0, False))
        ev = {
            "type": kind,
            # 保留旧 type 契约，同时对接新版复核的三态字段。
            "result": {EVENT_MAKE: "made", EVENT_MISS: "missed", EVENT_UNCERTAIN: "unknown"}[kind],
            "made": None if kind == EVENT_UNCERTAIN else kind == EVENT_MAKE,
            "needs_review": kind == EVENT_UNCERTAIN,
            "suggested_made": suggested_made,
            "review_t": round(at.review_t, 3) if reason == "occlusion_descent_suggestion" else None,
            "review_reason": reason or ("insufficient_evidence" if kind == EVENT_UNCERTAIN else None),
            "evidence_grade": "inferred" if suggested_made is not None else
                              ("unknown" if kind == EVENT_UNCERTAIN else
                               "measured_crossing" if kind == EVENT_MAKE else "rule_based"),
            "crossing_t": round(at.crossing_t, 3) if at.crossing_t is not None else None,
            "frame": int(frame_idx),
            "t": round(float(t), 2),
            "points": int(points),
            "points_verified": bool(verified) if kind == EVENT_MAKE else False,
            "player_id": at.player_id,
            "player_binding": at.binding,
            "dist_m": at.dist_est_m,
            "dist_est_m": at.dist_est_m,
            "dist_basis": "rim_width_px_estimate",
            "calibrated": False,
            "release_t": round(float(at.start_t), 2),
            "release_xy": [round(float(at.release_xy[0]), 1), round(float(at.release_xy[1]), 1)],
            "release_source": ("player_feet" if at.binding == "history" else "ball"),
            "ball_release_xy": [round(float(at.ball_release_xy[0]), 1), round(float(at.ball_release_xy[1]), 1)]
            if at.ball_release_xy else None,
            "ball_xy": [round(float(ball.cx), 1), round(float(ball.cy), 1)] if ball else None,
            "rim_xy": [round(float(self.rim.cx), 1), round(float(self.rim.cy), 1)],
            "rim_w": round(float(self.rim.w), 1),
            "flight_s": round(float(t - at.start_t), 2),
            "rim_crossing_t": round(at.crossing_t, 3) if at.crossing_t is not None else None,
            "real_obs": at.real_obs,
            "entered": bool(at.entered),
            "evidence": (EVENT_MAKE == kind and "entered+descend+exit") or
                        (EVENT_MISS == kind and "left_region_or_timeout") or "insufficient_evidence",
        }
        self.attempt = None
        self.last_close_t = t
        self.last_close_type = kind
        self.needs_leave = True
        self.leave_streak = 0
        self._emit(ev)
        return ev

    # ------------------------------------------------------------------ 统计
    @property
    def score(self) -> int:
        return sum(e["points"] for e in self.events if e["type"] == EVENT_MAKE)

    def summary(self) -> dict[str, Any]:
        makes = [e for e in self.events if e["type"] == EVENT_MAKE]
        misses = [e for e in self.events if e["type"] == EVENT_MISS]
        uncertain = [e for e in self.events if e["type"] == EVENT_UNCERTAIN]
        attempts = len(makes) + len(misses)
        return {
            "score": self.score,
            "makes": len(makes),
            "misses": len(misses),
            "uncertain": len(uncertain),
            "attempts": attempts,
            "attempts_incl_uncertain": attempts + len(uncertain),
            "fg_pct": round(100.0 * len(makes) / attempts, 1) if attempts else 0.0,
            "three_made": len([e for e in makes if e["points"] == 3]),
            "three_point_verified": False,
            "distance_calibrated": False,
            "rim_frames_seen": self.rim.seen,
            "rim_frames_raw": self.rim.raw,
            "rim_frames_rejected": self.rim.rejected,
        }
