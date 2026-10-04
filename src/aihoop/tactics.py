"""战术层 —— 套餐 B 的自研核心。

套餐 A 回答的是「发生了什么」（比分 / 命中率 / 热区）；
这一层回答的是「怎么打的」：

  1. 控球归属    球轨迹 + 球员轨迹 -> 每一段球权属于谁（possession）
  2. 传球网络    球权在队友之间转移 -> 传球事件 -> 有向传球网络（pass network）
  3. 阵型识别    进攻落位形状（五外 / 四外一内 / 三外两内 / 双塔 …）
                 防守阵型（人盯人 / 2-3 / 3-2 / 1-3-1 / 前场施压）
  4. 空间指标    凸包面积 / 平均间距 / 阵型宽度与纵深 / 重心到篮筐距离（spacing）
  5. 俯视战术图  逐帧的球员 + 球球场坐标（供前端播放器做动画回放）

--------------------------------------------------------------------------
坐标系（与 rules.py / court.py / 前端 court.js 严格一致：折半分析坐标）
--------------------------------------------------------------------------
  x 横向 ±7.5（x = 0 是球场中轴）
  y 纵向：**符号表示在哪半场，|y| 是离"本方底线"的距离**
         |y| = 0 是底线、|y| = 14 是中圈；篮筐在 (0, ±1.575)
  于是只要取 d = |y| 当纵轴，「被进攻的篮筐」在局部坐标里永远是 (0, 1.575)：
  进攻方向被统一掉了，左右两侧的进攻可以直接叠在一起比较。
  这个局部坐标就是下面的 attack_frame()，是本模块所有几何量的基础。

--------------------------------------------------------------------------
设计原则（答辩会问）
--------------------------------------------------------------------------
  * **零第三方依赖**：凸包、线性插值、分段都是纯 Python 实现，
    和 court.py / rules.py 一样，最小依赖环境下也能跑完整条自检。
  * **不做黑盒**：阵型识别是**可解释的几何规则**，不是训练出来的分类器。
    每个结论都带着它依据的原始量（外线/内线人数、离篮距离、最近盯人距离…），
    所以答辩时能逐条讲清楚「为什么判成 2-3 联防」，测试也能逐条断言。
  * **判不了就明说**：球员轨迹缺失、球轨迹覆盖率太低时返回 available=False
    + 具体原因，而不是硬编一张好看的战术图 —— 假数据比没数据更糟。
"""
from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .model import COURT_LENGTH, COURT_WIDTH, HOOP_LEFT, HOOP_RIGHT, Player

# --------------------------------------------------------------------------
# 常量与阈值
# --------------------------------------------------------------------------
#
# 这些阈值都是"能讲出道理"的经验值，不是调参调出来的玄学：
#   POSSESS_RADIUS  2.5m —— 一名持球球员到球的距离；再远就是"球在空中/没人控球"
#   MIN_POSSESSION  1.0s —— 短于这个时间的球权是检测抖动，不算一次球权
#   PASS_MIN_GAP    0.45s—— 两次传球之间至少要这么久（排除一次传球的重复计数）
#   ZONE_TOP_DEPTH  5.5m —— 联防"上线"与"下线"的纵深分界（罚球线附近）
#   ZONE_MID_DEPTH  3.0m —— "下线（收缩在篮下）"的纵深分界
#   MAN_DIST        1.6m —— 平均最近盯人距离小于它，认为是在人盯人
#   PRESS_DEPTH     11.0m—— 防守人站到离本方底线 11m 以外（接近中圈）算施压
POSSESS_RADIUS = 2.5
MIN_POSSESSION = 1.0
PASS_MIN_GAP = 0.45
# 联防的"上线 / 中间 / 下线"用**离本方底线的纵深 d** 划分，而不是离篮筐的
# 距离 —— 底角三分点离篮筐也有 6.7m，用离篮距离会把"站底角的防守人"
# 误判成"上线"。纵深才真正对应教练说的"收缩在里面 / 提到罚球线以上"。
ZONE_TOP_DEPTH = 5.5
ZONE_MID_DEPTH = 3.0
MAN_DIST = 1.6
PRESS_DEPTH = 11.0
THREE_R = 6.75
CORNER_X = 6.60
RIM_D = 1.575


@dataclass
class TacticsConfig:
    """战术层算法的全部可调参数（答辩时方便现场改）。"""
    frame_step: float = 1.0        # 战术帧采样间隔（秒）——阵型片段以秒计，1Hz 足够
    # 空间指标时间序列最多存这么多点（超出按等间隔抽稀）。
    # 30 分钟的比赛按 1Hz 采样有 1800 条 × 两队，全存进 tactics.json 会让产物
    # 变成好几 MB —— 而前端画一条曲线用 1200 个点已经足够平滑。
    max_spacing_points: int = 1200
    anim_step: float = 0.5         # 俯视战术图逐帧输出的间隔（秒）
    max_gap: float = 3.0           # 球员位置插值的最大时间空隙（秒），超了就认为该球员不在场
    # 「保持首尾采样点」的窗口，**必须远小于 max_gap**。
    # 这个参数踩过一个严重的坑：一开始复用了 max_gap(3s)，结果任何
    # "3 秒后才开始"或"3 秒前就结束"的轨迹在 t 时刻也被算成在场 ——
    # 一条 3 分钟视频裂出 466 条碎片轨迹，一帧就画出 30 个点
    # （真实只有 10 人），同一个球员被重复画好几次。
    hold_gap: float = 0.5
    possess_radius: float = POSSESS_RADIUS
    min_possession: float = MIN_POSSESSION
    # 飞行段反推传球的参数
    flight_max_gap: float = 0.7      # 同一段飞行内允许的最大时间断档(s)
    flight_max_step_m: float = 5.0   # 相邻采样点允许的最大位移(m)
    flight_radius: float = 6.0       # 起点/终点多远内算"这个人出的球"
    # ---- 传球的统一质量门槛（两条判据都过这一关）----
    pass_min_dist: float = 2.0       # 球位移 < 2m 的"传球"多半是同一次传球的抖动
    pass_dedupe_window: float = 2.5  # A->B 后 2.5s 内又 B->A，判为同一次被算了两次
    pass_min_gap: float = PASS_MIN_GAP
    man_dist: float = MAN_DIST
    press_depth: float = PRESS_DEPTH
    segment_min: float = 3.0       # 阵型片段的最短时长（秒），比这短的不单独成段
    max_frames: int = 4000         # 战术帧上限（超长视频抽稀，防止产物爆炸）
    min_players: int = 4           # 每队至少要有这么多人，阵型识别才有意义


@dataclass
class PlayerSample:
    """一名球员在某一时刻的球场坐标（分析坐标，米）。

    真视频路径由 VideoSource 用单应矩阵把球员框底部中点投影到地面得到；
    合成数据源直接构造。存进 raw_track.json 的 player_track 字段。
    """
    t: float
    player_id: str
    team: str
    x: float
    y: float
    conf: float = 1.0

    def to_dict(self) -> dict:
        return {"t": round(self.t, 3), "player_id": self.player_id,
                "team": self.team, "x": round(self.x, 3),
                "y": round(self.y, 3), "conf": round(self.conf, 3)}


# --------------------------------------------------------------------------
# 基础几何（纯 Python，无 numpy）
# --------------------------------------------------------------------------
def convex_hull_area(points: Sequence[Sequence[float]]) -> float:
    """点集的凸包面积（m²）—— 空间指标里最直观的一个。

    Andrew monotone chain，O(n log n)。少于 3 个不重合点时面积为 0。
    """
    pts = sorted({(round(float(p[0]), 5), round(float(p[1]), 5)) for p in points})
    if len(pts) < 3:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return 0.0
    area = 0.0
    for i, (x1, y1) in enumerate(hull):
        x2, y2 = hull[(i + 1) % len(hull)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def mean_pairwise_distance(points: Sequence[Sequence[float]]) -> float:
    """两两平均距离（m）。小于 2 个点时返回 0。"""
    n = len(points)
    if n < 2:
        return 0.0
    total = 0.0
    cnt = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += math.hypot(points[i][0] - points[j][0],
                                points[i][1] - points[j][1])
            cnt += 1
    return total / cnt if cnt else 0.0


def attack_frame(x: float, y: float) -> tuple[float, float]:
    """分析坐标 -> 局部进攻坐标 (x, d)。

    d = |y| 是"离本方底线的距离"（0 = 底线，14 = 中圈）。
    在这个局部坐标系里，被进攻的篮筐永远是 (0, 1.575)，
    所以左右两侧的进攻形状可以直接叠在一起比较 —— 这是阵型识别的前提。
    """
    return float(x), abs(float(y))


def dist_to_rim(x: float, y: float) -> float:
    """到**被进攻篮筐**的距离（米）。入参是分析坐标。"""
    _, d = attack_frame(x, y)
    return math.hypot(x, d - RIM_D)


def attack_side_of(y: float) -> int:
    """由纵坐标的符号判断在攻哪一侧：+1 = 右半场（攻右篮筐），-1 = 左半场。"""
    return 1 if y >= 0 else -1


def _median(vals: Sequence[float]) -> float:
    v = sorted(vals)
    n = len(v)
    if not n:
        return 0.0
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def _mean(vals: Sequence[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


# --------------------------------------------------------------------------
# 球员位置索引：任意时刻取"谁在哪"（线性插值）
# --------------------------------------------------------------------------
class PlayerIndex:
    """把稀疏的 PlayerSample 变成"任意时刻的位置查询"。

    真视频里球员检测是抽帧的（player_stride），所以必须插值；
    两个采样点间隔超过 cfg.max_gap 就认为该球员这段时间不在画面里
    （下场 / 被遮挡），**不插值** —— 否则会凭空造出一个球员。
    """

    def __init__(self, samples: Sequence[PlayerSample], cfg: TacticsConfig):
        self.cfg = cfg
        self.times: list[float] = []
        self.by_pid: dict[str, list[PlayerSample]] = {}
        for s in sorted(samples, key=lambda s: s.t):
            self.by_pid.setdefault(s.player_id, []).append(s)
        self.teams: dict[str, str] = {}
        for pid, lst in self.by_pid.items():
            self.teams[pid] = lst[0].team
        self._times: dict[str, list[float]] = {
            pid: [s.t for s in lst] for pid, lst in self.by_pid.items()}

    def at(self, t: float) -> dict[str, tuple[float, float]]:
        """返回 {player_id: (x, y)}；不在场/超出插值窗的球员不出现。"""
        out: dict[str, tuple[float, float]] = {}
        for pid, lst in self.by_pid.items():
            ts = self._times[pid]
            i = bisect.bisect_left(ts, t)
            if i < len(ts) and abs(ts[i] - t) <= 1e-9:
                s = lst[i]
                out[pid] = (s.x, s.y)
                continue
            if i == 0:
                # 比第一个采样点还早：在 hold_gap 内才"保持"第一个采样点。
                # 这一步是为了让"传球发生在两名球员采样点之间的缝隙里"这种情况
                # 仍然能取到传球人；但窗口必须小（0.5s），否则就变成
                # "所有历史轨迹都还在场上"，战术图会被重复点淹没。
                if ts[0] - t <= self.cfg.hold_gap:
                    out[pid] = (lst[0].x, lst[0].y)
                continue
            if i >= len(ts):
                if t - ts[-1] <= self.cfg.hold_gap:
                    out[pid] = (lst[-1].x, lst[-1].y)
                continue
            t0, t1 = ts[i - 1], ts[i]
            if t1 - t0 > self.cfg.max_gap:
                continue
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            a, b = lst[i - 1], lst[i]
            out[pid] = (a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f)
        return out


# --------------------------------------------------------------------------
# 1) 控球归属
# --------------------------------------------------------------------------
def detect_possessions(ball_track, idx: PlayerIndex, cfg: TacticsConfig
                       ) -> list[dict]:
    """球轨迹 + 球员位置 -> 球权片段（possession spells）。

    做法（可解释、无训练）：
      1. 每个球的采样点找**最近的球员**，距离在 possess_radius 内就认为他在控球；
      2. 对"控球人"序列做滑窗多数投票，压掉单帧抖动（球从手边飞过会被误判）；
      3. 连续相同的控球人合并成一段；短于 min_possession 的丢掉。

    这就是"最简单的控球判定"，但它对答辩很友好：每一步都能复现、能可视化。
    """
    samples = sorted(ball_track, key=lambda b: b.t)
    if not samples:
        return []
    raw: list[tuple[float, Optional[str], Optional[str]]] = []   # (t, pid, team)
    for b in samples:
        pos = idx.at(b.t)
        best_pid, best_d = None, 1e9
        for pid, (px, py) in pos.items():
            d = math.hypot(px - b.x, py - b.y)
            if d < best_d:
                best_pid, best_d = pid, d
        if best_pid is not None and best_d <= cfg.possess_radius:
            raw.append((b.t, best_pid, idx.teams.get(best_pid, "")))
        else:
            raw.append((b.t, None, None))

    # 滑窗多数投票（窗口 = 0.6s 或 5 个采样点，取大者）
    if len(raw) >= 3:
        # 平均采样间隔 -> 0.6s 内有多少个采样点，夹到 [5, 9]（奇数窗口）
        avg_dt = (raw[-1][0] - raw[0][0]) / max(1, len(raw) - 1)
        span = max(5, min(9, int(round(0.6 / avg_dt)) if avg_dt > 1e-6 else 5))
        half = span // 2
        smoothed = list(raw)
        for i in range(len(raw)):
            window = [r[1] for r in raw[max(0, i - half):i + half + 1]]
            window = [w for w in window if w]
            if not window:
                continue
            counts: dict[str, int] = {}
            for w in window:
                counts[w] = counts.get(w, 0) + 1
            win = max(counts, key=lambda k: counts[k])
            if win != raw[i][1] and counts[win] >= 2:
                smoothed[i] = (raw[i][0], win, idx.teams.get(win, ""))
        raw = smoothed

    # 合并成片段
    spells: list[dict] = []
    for (t, pid, team) in raw:
        if pid is None:
            continue
        if spells and spells[-1]["player_id"] == pid:
            spells[-1]["t1"] = t
            spells[-1]["n"] += 1
            continue
        spells.append({"t0": t, "t1": t, "player_id": pid, "team": team or "",
                       "n": 1})
    out = [s for s in spells if s["t1"] - s["t0"] >= cfg.min_possession
           or s["n"] >= 3]
    return out


# --------------------------------------------------------------------------
# 2) 传球事件与传球网络
# --------------------------------------------------------------------------
def detect_passes(spells: Sequence[dict], idx: PlayerIndex,
                  cfg: TacticsConfig,
                  attempts: Optional[Sequence] = None) -> list[dict]:
    """球权转移 -> 传球事件。

    同队交接 = **传球**（pass）；跨队交接分两种：

      * 附近（前后 3s 内）有一次出手 -> **投篮后的球权转换**
        （投进后对方发底线球 / 打铁后的防守篮板），这是比赛流程，**不是失误**；
      * 没有任何出手 -> **失误 / 抢断**（turnover）。

    为什么必须区分：合成数据里每次进攻都以一次出手结束，下一回合必然是对方控球。
    如果把这些"投篮之后必然发生的换手"都算成失误，一场比赛会凭空多出几十次
    失误（实测 157 回合里被误算成 93 次"球权转换"），答辩时一问就露馅。

    过滤两类噪声：
      * 时间上贴得极近的相邻传球（同一次传球被拆成两段）
      * 传球距离 > 25m（明显是轨迹串了）
    """
    shots_t = sorted(float(getattr(a, "t", 0)) for a in (attempts or []))
    raw: list[dict] = []
    for a, b in zip(spells, spells[1:]):
        # 同一个人"断了又捡回来"不是传球（抢到自己的篮板 / 检测抖动）
        if a["player_id"] == b["player_id"]:
            continue
        pa = idx.at((a["t1"] + b["t0"]) / 2.0)
        if a["player_id"] not in pa or b["player_id"] not in pa:
            continue
        ax, ay = pa[a["player_id"]]
        bx, by = pa[b["player_id"]]
        d = math.hypot(bx - ax, by - ay)
        if d > 25.0:
            continue
        ev = {"t": round((a["t1"] + b["t0"]) / 2.0, 2),
              "team": a["team"],
              "from": a["player_id"], "to": b["player_id"],
              "dist": round(d, 2),
              "duration": round(max(0.0, b["t0"] - a["t1"]), 2),
              "x": round(bx, 2), "d": round(abs(by), 2),
              "same_team": bool(a["team"] == b["team"])}
        if ev["same_team"]:
            ev["kind"] = "pass"
        else:
            # 出手前 3s ~ 出手后 1s 内的换手 = 投篮造成的球权转换，不算失误
            near_shot = any(-1.0 <= ev["t"] - st <= 3.0 for st in shots_t)
            ev["kind"] = "possession_change" if near_shot else "turnover"
        raw.append(ev)

    # 去抖：时间上贴得极近的相邻传球只保留第一条。
    # 注意**不能**用"两段球权之间的空隙"来过滤 —— 那个空隙是球员检测的
    # 采样间隔造成的（A 最后一个采样点 -> B 第一个采样点），
    # 拿它跟 pass_min_gap 比会把所有传球都误杀（抽帧越稀杀得越干净）。
    out: list[dict] = []
    for ev in raw:
        if out and ev["t"] - out[-1]["t"] < cfg.pass_min_gap:
            continue
        out.append(ev)
    return out


def dedupe_positions(pos: dict, min_dist: float = 0.8,
                     prefer: Optional[set] = None) -> dict:
    """同一帧里**近到不可能属于两个人**的点只保留一个。

    为什么需要：ByteTrack/BoT-SORT 在球员被遮挡换 ID 时，会出现"同一个人
    有两段轨迹同时存在"（旧轨迹在 track_buffer 里还没死、新轨迹已经开了）。
    表现就是战术图上同一个球员画了两个点 —— 实测一帧里相距 <1m 的点对多达 110 对。

    保留策略：优先保留 `prefer`（上一帧也在场的 id），这样点不会来回跳；
    其余按 id 顺序，谁先来留谁。阈值 0.8m 是"两人不可能站得比这更近"的经验值。
    """
    prefer = prefer or set()
    items = sorted(pos.items(), key=lambda kv: 0 if kv[0] in prefer else 1)
    out: dict = {}
    for pid, (x, y) in items:
        if any(math.hypot(x - ox, y - oy) < min_dist for ox, oy in out.values()):
            continue
        out[pid] = (x, y)
    return out


def detect_flights(ball_track, max_gap: float = 0.7,
                   max_step_m: float = 5.0) -> list[list]:
    """把球检测点切成「飞行段」：时间相邻 + 位置连续。

    为什么需要它：真视频里球被球员拿住时**被身体挡住、检测不到**，
    只有飞在空中才看得见（实测一段 15 秒广播素材：450 帧里只有 117 帧
    检测到球，且集中在两段连续飞行里）。所以"球离谁最近 = 谁控球"这个
    判据天生失效 —— 可见的时刻恰恰是球**不在**人身边的时刻。
    """
    samples = sorted(ball_track, key=lambda b: b.t)
    out: list[list] = []
    cur: list = []
    for b in samples:
        if cur and (b.t - cur[-1].t > max_gap
                    or math.hypot(b.x - cur[-1].x, b.y - cur[-1].y) > max_step_m):
            if len(cur) >= 2:
                out.append(cur)
            cur = []
        cur.append(b)
    if len(cur) >= 2:
        out.append(cur)
    return out


def _nearest_player_at(pos: dict, b, radius: float):
    """在时刻 pos（{pid: (x,y)}）里找离球 b 最近、且距离在 radius 内的球员。"""
    best, bd = None, 1e9
    for pid, (px, py) in pos.items():
        d = math.hypot(px - b.x, py - b.y)
        if d < bd:
            best, bd = pid, d
    return (best, bd) if (best is not None and bd <= radius) else (None, bd)


def detect_passes_from_flights(flights: Sequence[Sequence],
                               idx: PlayerIndex, cfg: TacticsConfig,
                               radius: float = 6.0,
                               min_duration: float = 0.2) -> list[dict]:
    """**从飞行段反推传球** —— 真视频里唯一可行的传球判据。

    球一旦飞出去，唯一贴近球员的两个瞬间就是**起点（出手/传球）**和
    **终点（接球/落地）**：
        起点最近的球员 = 出球人；终点最近的球员 = 接球人。
    同队 -> 传球；跨队 -> 抢断/失误；只认出一端 -> 标 loose（球在空中/出界）。
    """
    out: list[dict] = []
    for fl in flights:
        dur = fl[-1].t - fl[0].t
        if dur < min_duration:
            continue
        pos0, pos1 = idx.at(fl[0].t), idx.at(fl[-1].t)
        a, da = _nearest_player_at(pos0, fl[0], radius)
        b, db = _nearest_player_at(pos1, fl[-1], radius)
        ta = idx.teams.get(a, "") if a else ""
        tb = idx.teams.get(b, "") if b else ""
        dist = math.hypot(fl[-1].x - fl[0].x, fl[-1].y - fl[0].y)
        path = sum(math.hypot(fl[i].x - fl[i - 1].x, fl[i].y - fl[i - 1].y)
                   for i in range(1, len(fl)))
        if a and b and a != b:
            kind = "pass" if ta == tb else "turnover"
        elif a or b:
            kind = "loose"
        else:
            kind = "unattributed"
        out.append({"t": round(fl[0].t, 2), "team": ta or tb or "",
                    "from": a or "", "to": b or "",
                    "dist": round(dist, 2), "path": round(path, 2),
                    "duration": round(dur, 2),
                    "x": round(fl[0].x, 2), "d": round(abs(fl[0].y), 2),
                    "same_team": bool(ta and tb and ta == tb),
                    "kind": kind, "points": len(fl),
                    "source": "flight"})
    # 去抖：时间上贴得极近的相邻事件只留第一条
    out.sort(key=lambda e: e["t"])
    dedup: list[dict] = []
    for e in out:
        if dedup and e["t"] - dedup[-1]["t"] < cfg.pass_min_gap:
            continue
        dedup.append(e)
    return dedup


def clean_passes(events: Sequence[dict], cfg: TacticsConfig) -> list[dict]:
    """传球事件的统一清洗：质量门槛 + 方向抖动去重。

    真视频上实测到的两类噪声：
      1. **位移过小**的假传球（球在球员身边抖一下就被判成换手）；
      2. **同一次传球被算成两次** —— A->B 之后紧接着 B->A（球检测在两人
         之间跳一下，控球归属就来回切）。
    宁可少而准：答辩时一次错传球被追问，比少两次传球难看得多。
    """
    out: list[dict] = []
    for e in sorted(events, key=lambda x: float(x.get("t", 0.0))):
        if float(e.get("dist", 0.0) or 0.0) < cfg.pass_min_dist:
            continue
        if out:
            p = out[-1]
            dt = float(e.get("t", 0)) - float(p.get("t", 0))
            if dt <= cfg.pass_dedupe_window:
                same_pair = (p.get("from") == e.get("from")
                             and p.get("to") == e.get("to"))
                reversed_pair = (p.get("from") == e.get("to")
                                 and p.get("to") == e.get("from"))
                if same_pair or reversed_pair:
                    continue
        out.append(e)
    return out


def pass_network(passes: Sequence[dict], players: dict[str, Player],
                 positions: dict[str, tuple[float, float]]) -> dict:
    """传球网络：节点=球员（含平均站位），边=有向传球次数。

    node.x / node.d 是**局部进攻坐标**（d = |y| 离底线距离），
    前端直接把它画在半场上（u = x + 7.5, v = d）。
    """
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str], int] = {}
    for p in passes:
        if p.get("kind") != "pass":
            continue
        for pid in (p["from"], p["to"]):
            if pid not in nodes:
                pl = players.get(pid)
                px, pd = positions.get(pid, (0.0, 0.0))
                nodes[pid] = {"id": pid,
                              "name": pl.name if pl else pid,
                              "team": pl.team if pl else p["team"],
                              "x": round(px, 2), "d": round(pd, 2),
                              "passes": 0, "received": 0}
        nodes[p["from"]]["passes"] += 1
        nodes[p["to"]]["received"] += 1
        key = (p["from"], p["to"])
        edges[key] = edges.get(key, 0) + 1
    return {
        "nodes": sorted(nodes.values(), key=lambda n: -(n["passes"] + n["received"])),
        "edges": [{"source": a, "target": b, "value": v}
                  for (a, b), v in sorted(edges.items(), key=lambda kv: -kv[1])],
    }


def group_possessions(spells: Sequence[dict], passes: Sequence[dict],
                      attempts: Sequence, gap: float = 5.0) -> list[dict]:
    """把「控球片段」合并成篮球语义上的「回合」（possession）。

    为什么还要这一步：传球会让球权在队友之间换手，**每一次换手都会切出一个
    新的控球片段** —— 一段阵地进攻往往有 3~4 个片段。而篮球统计里的
    「一个回合」指的是"同一支球队连续控球直到出手 / 失误 / 球权转换"，
    所以要把同队、间隔很小的片段合并起来。否则"回合数"会被传球次数放大
    好几倍（一次进攻被算成 4 个回合），答辩时一问就露馅。

    gap 取 5s：比这更久的空档说明中间发生了球权转换或死球。
    """
    out: list[dict] = []
    for sp in spells:
        if (out and out[-1]["team"] == sp["team"]
                and sp["t0"] - out[-1]["t1"] <= gap):
            out[-1]["t1"] = max(out[-1]["t1"], sp["t1"])
            out[-1]["touches"] += 1
        else:
            out.append({"t0": sp["t0"], "t1": sp["t1"], "team": sp["team"],
                        "touches": 1})
    for po in out:
        po["passes"] = sum(1 for p in passes
                           if po["t0"] <= p["t"] <= po["t1"]
                           and p["team"] == po["team"]
                           and p["kind"] == "pass")
        po["turnovers"] = sum(1 for p in passes
                              if po["t0"] <= p["t"] <= po["t1"]
                              and p["team"] == po["team"]
                              and p.get("kind") != "pass")
        sh = [a for a in attempts
              if po["t0"] <= float(getattr(a, "t", 0)) <= po["t1"]]
        po["shots"] = len(sh)
        po["points"] = sum(int(getattr(a, "score_points", 0) or 0) for a in sh)
    return out


def pass_leaderboard(passes: Sequence[dict], players: dict[str, Player]
                     ) -> list[dict]:
    """每名球员的传球 / 接球数（传球网络右侧的排行榜）。"""
    rows: dict[str, dict] = {}
    for p in passes:
        if p.get("kind") != "pass":
            continue
        for pid, key in ((p["from"], "passes"), (p["to"], "received")):
            if pid not in rows:
                pl = players.get(pid)
                rows[pid] = {"player_id": pid,
                             "name": pl.name if pl else pid,
                             "team": pl.team if pl else p["team"],
                             "passes": 0, "received": 0}
            rows[pid][key] += 1
    return sorted(rows.values(), key=lambda r: -(r["passes"] + r["received"]))


# --------------------------------------------------------------------------
# 3) 阵型识别（可解释几何规则）
# --------------------------------------------------------------------------
def classify_offense(pts: Sequence[Sequence[float]]) -> dict:
    """进攻落位形状。入参是**局部进攻坐标** [(x, d), ...]。

    判据（全部可解释）：
      * 外线人数：三分线外（距篮 >= 6.75m 或横向 |x| >= 6.60m）
      * 内线人数：距篮 <= 4.0m
      * 形状命名沿用教练习惯：五外拉开 / 四外一内 / 三外两内 / 双塔 / 均衡落位
      * 只要有人退到 d >= 13（接近中圈），认为还在推进 —— 归为"转换进攻"
    """
    if len(pts) < 3:
        return {"shape": "人数不足", "outside": 0, "inside": 0, "confidence": 0.0}
    outside = inside = 0
    max_d = 0.0
    for x, d in pts:
        r = math.hypot(x, d - RIM_D)
        max_d = max(max_d, d)
        if r >= THREE_R or abs(x) >= CORNER_X:
            outside += 1
        if r <= 4.0:
            inside += 1
    if max_d >= 13.0:
        shape = "转换进攻"
    elif outside >= 5:
        shape = "五外拉开"
    elif outside == 4:
        shape = "四外一内"
    elif outside == 3:
        shape = "三外两内"
    elif outside <= 2 and inside >= 2:
        shape = "双塔（内线为主）"
    else:
        shape = "均衡落位"
    # 置信度 = 形状判据的"干净程度"：外线人数离分界越远越有把握
    edge = min(abs(outside - 3), abs(outside - 4), abs(outside - 5))
    conf = 0.55 + 0.10 * min(edge, 3)
    if shape == "转换进攻":
        conf = 0.7
    return {"shape": shape, "outside": outside, "inside": inside,
            "max_depth": round(max_d, 2), "confidence": round(min(conf, 0.95), 2)}


def classify_defense(def_pts: Sequence[Sequence[float]],
                     atk_pts: Sequence[Sequence[float]],
                     cfg: Optional[TacticsConfig] = None) -> dict:
    """防守阵型。入参都是**局部进攻坐标**（被进攻篮筐在 (0, 1.575)）。

    两个正交的判据都保留在返回值里，方便答辩时逐条核对：

      * 盯人紧密度 ``man_gap``：每名**进攻**球员到"最近的防守人"的距离均值。
        人盯人时它很小（防守人贴着人）；联防时它偏大（守的是区域）。
      * 纵深分布 ``top/mid/low``：按"离本方底线的纵深 d"把防守人分三档
        （>=5.5 上线 / 3.0~5.5 中间 / <3.0 收缩在篮下）。
        联防的经典站位在纵深分布上是**特定形状**：
            2-3   -> 上线 2、篮下 3         3-2   -> 上线 3、篮下 2
            1-3-1 -> 上线 1、中间 3、篮下 1  1-2-2 -> 上线 1、中间 2、篮下 2

    判定优先级（可解释规则，不是训练出来的分类器）：
      1. 有 >=2 人站在离本方底线 >= 11m（接近中圈）  -> 前场施压（紧逼）
      2. 纵深分布命中某个经典联防形状，且不是"贴脸盯人" -> 对应联防
      3. man_gap <= man_dist                        -> 人盯人
      4. 只命中形状                                 -> 对应联防
      5. 都不像                                      -> 混合防守

    为什么把"形状"排在"盯人"前面：联防站位本身也会让 man_gap 偶然变小
    （进攻方恰好站在防守人旁边），但**纵深分布的形状**在整段进攻里是稳定的；
    反过来，真正的人盯人很难凑出 2-3 / 3-2 / 1-3-1 的纵深形状。两者置信度
    都随"干净程度"变化，前端不把它们当绝对值用。
    """
    cfg = cfg or TacticsConfig()
    if len(def_pts) < 3:
        return {"scheme": "人数不足", "confidence": 0.0}
    n = len(def_pts)
    top = mid = low = 0
    depths = []
    rim_d = []
    for x, d in def_pts:
        rim_d.append(math.hypot(x, d - RIM_D))
        depths.append(d)
        if d >= ZONE_TOP_DEPTH:
            top += 1
        elif d >= ZONE_MID_DEPTH:
            mid += 1
        else:
            low += 1

    press = sum(1 for d in depths if d >= cfg.press_depth)
    if atk_pts:
        nearest = [min(math.hypot(ax - dx, ad - dd) for dx, dd in def_pts)
                   for ax, ad in atk_pts]
        man_gap = _mean(nearest)
    else:
        man_gap = 99.0

    extra = {"top": top, "mid": mid, "low": low, "n": n,
             "man_gap": round(man_gap, 2), "press": press,
             "mean_rim_dist": round(_mean(rim_d), 2)}

    zone = None
    if n >= 5:
        if top >= 2 and low >= 3:
            zone = "2-3 联防"
        elif top >= 3 and low >= 2:
            zone = "3-2 联防"
        elif top <= 1 and mid >= 3 and low >= 1:
            zone = "1-3-1 联防"
        elif top <= 1 and mid >= 2 and low >= 2:
            zone = "1-2-2 联防"
    if zone is None and low >= max(3, n - 1):
        zone = "收缩联防"
    extra["zone_shape"] = zone or ""

    if press >= 2:
        return {"scheme": "前场施压（紧逼）", "confidence": 0.75, **extra}
    if zone and man_gap > cfg.man_dist * 0.85:
        return {"scheme": zone, "confidence": 0.72, **extra}
    if man_gap <= cfg.man_dist and len(atk_pts) >= 3:
        conf = 0.6 + 0.3 * max(0.0, (cfg.man_dist - man_gap) / cfg.man_dist)
        return {"scheme": "人盯人", "confidence": round(min(conf, 0.95), 2),
                **extra}
    if zone:
        return {"scheme": zone, "confidence": 0.6, **extra}
    return {"scheme": "混合防守", "confidence": 0.4, **extra}


# --------------------------------------------------------------------------
# 4) 空间指标
# --------------------------------------------------------------------------
def spacing_metrics(pts: Sequence[Sequence[float]]) -> dict:
    """一队球员的空间指标。入参是**分析坐标** [(x, y), ...]。

    area      凸包面积（m²）—— 最直观的"拉开了没有"
    mean_dist 两两平均距离（m）
    width     横向宽度（m）—— 球场横向的展开度
    depth     纵向纵深（m，按离底线距离算）
    radius    重心到被进攻篮筐的距离（m）—— 整体压得多深
    """
    if len(pts) < 2:
        return {"area": 0.0, "mean_dist": 0.0, "width": 0.0,
                "depth": 0.0, "radius": 0.0, "cx": 0.0, "cd": 0.0}
    local = [attack_frame(x, y) for x, y in pts]
    xs = [p[0] for p in local]
    ds = [p[1] for p in local]
    cx, cd = _mean(xs), _mean(ds)
    return {"area": round(convex_hull_area(local), 2),
            "mean_dist": round(mean_pairwise_distance(local), 2),
            "width": round(max(xs) - min(xs), 2),
            "depth": round(max(ds) - min(ds), 2),
            "radius": round(math.hypot(cx, cd - RIM_D), 2),
            "cx": round(cx, 2), "cd": round(cd, 2)}


# --------------------------------------------------------------------------
# 5) 主入口：把上面这些拼成 tactics.json
# --------------------------------------------------------------------------
def _segments(timeline: Sequence[dict], key_from: str, key_to: str,
              label_key: str, min_len: float) -> list[dict]:
    """把逐帧标签合并成时间段（连续同标签合并，太短的丢掉）。

    这是"阵型片段"的产出方式：前端拿到的是 [{t0,t1,label}, ...]，
    而不是每秒一条记录 —— 战术结论本来就是"一段时间里一直这么打"。
    """
    segs: list[dict] = []
    for row in timeline:
        lab = row[label_key]
        if segs and segs[-1][label_key] == lab:
            segs[-1][key_to] = row["t"]
            segs[-1]["n"] += 1
            continue
        segs.append({"t0": row["t"], "t1": row["t"], label_key: lab,
                     "n": 1, "confidence": row.get("confidence", 0.5)})
    out = []
    for s in segs:
        if s["t1"] - s["t0"] >= min_len or s["n"] >= 3:
            out.append(s)
    return out


def _summarize_top(labels: Sequence[str], k: int = 5) -> list[dict]:
    counts: dict[str, int] = {}
    for l in labels:
        counts[l] = counts.get(l, 0) + 1
    return [{"label": l, "n": c} for l, c in
            sorted(counts.items(), key=lambda kv: -kv[1])[:k]]


def build_tactics(player_track: Sequence[PlayerSample], ball_track,
                  players: Optional[dict[str, Player]] = None,
                  attempts: Optional[Sequence] = None,
                  duration: float = 0.0,
                  cfg: Optional[TacticsConfig] = None,
                  progress: Optional[Callable[[float, str], None]] = None
                  ) -> dict:
    """算出整套战术结论（tactics.json 的内容）。"""
    cfg = cfg or TacticsConfig()
    players = players or {}
    attempts = list(attempts or [])
    notes: list[str] = []

    def step(p, m):
        if progress:
            progress(p, m)

    samples = [s for s in player_track if isinstance(s, PlayerSample)]
    if len(samples) < 20:
        return {"available": False, "version": "1.0",
                "reason": "没有球员轨迹（player_track 为空）。"
                          "真视频路径需要球员检测 + 球场标定；"
                          "用 synthetic 数据源可以立刻看到完整效果。",
                "notes": notes}

    idx = PlayerIndex(samples, cfg)
    teams = sorted({s.team for s in samples if s.team}) or ["home", "away"]
    if len(teams) < 2:
        notes.append("只检测到一支球队的轨迹，阵型对比仅对单侧有效。")

    # ---- 采样时间轴 ----
    t_end = duration or max(s.t for s in samples)
    times: list[float] = []
    t = 0.0
    step_t = max(0.2, cfg.frame_step)
    while t <= t_end:
        times.append(round(t, 3))
        t += step_t
    if len(times) > cfg.max_frames:
        stride = max(1, len(times) // cfg.max_frames)
        notes.append(f"采样点过多，按 1/{stride} 抽稀到 {cfg.max_frames} 帧。")
        times = times[::stride]

    step(0.15, "识别控球归属与传球")
    spells = detect_possessions(ball_track, idx, cfg)
    passes = detect_passes(spells, idx, cfg, attempts)
    # 细粒度的"控球片段" -> 篮球语义上的"回合"
    poss = group_possessions(spells, passes, attempts)

    # ---- 逐帧：阵型 + 空间 ----
    step(0.45, "逐帧识别阵型与空间指标")
    spacing_timeline: list[dict] = []
    off_tl: dict[str, list] = {t: [] for t in teams}
    def_tl: dict[str, list] = {t: [] for t in teams}
    side_votes: dict[int, int] = {}
    ball_by_t: dict[float, tuple[float, float]] = {}
    unlabeled = 0          # 因为没有球权而"只判落位、不判攻防"的帧数

    bt = sorted(ball_track, key=lambda b: b.t)
    bt_times = [b.t for b in bt]

    prev_ids: set = set()
    for ti, tt in enumerate(times):
        # 近邻去重：否则"同一个人的两段碎片轨迹"会让阵型/间距被重复点带偏
        pos = dedupe_positions(idx.at(tt), prefer=prev_ids)
        if pos:
            prev_ids = set(pos)
        if len(pos) < 2:
            continue
        # 球的位置（就近取样，只用于定进攻方向）
        ball_xy = None
        if bt_times:
            k = bisect.bisect_left(bt_times, tt)
            cand = [j for j in (k - 1, k, k + 1) if 0 <= j < len(bt)]
            if cand:
                j = min(cand, key=lambda j: abs(bt_times[j] - tt))
                if abs(bt_times[j] - tt) <= max(2.0, cfg.max_gap):
                    ball_xy = (bt[j].x, bt[j].y)

        by_team: dict[str, list] = {}
        for pid, (px, py) in pos.items():
            by_team.setdefault(idx.teams.get(pid, "?"), []).append((px, py))

        # 进攻方向：优先用球的位置，球没有就用两队整体位置中较深的一侧
        if ball_xy is not None:
            side = attack_side_of(ball_xy[1])
        else:
            side = 1
        side_votes[side] = side_votes.get(side, 0) + 1
        ball_by_t[tt] = ball_xy or (0.0, 0.0)

        # 控球方（该时刻最近的球权片段）
        holder = None
        for s in spells:
            if s["t0"] <= tt <= s["t1"]:
                holder = s
                break
        off_team = holder["team"] if holder else None

        for team in teams:
            pts = by_team.get(team, [])
            if len(pts) < 2:
                continue
            m = spacing_metrics(pts)
            m.update({"t": tt, "team": team, "n": len(pts)})
            spacing_timeline.append(m)

            local = [attack_frame(x, y) for x, y in pts]
            if len(pts) >= cfg.min_players:
                if off_team == team:
                    r = classify_offense(local)
                    r.update({"t": tt, "team": team, "role": "offense"})
                    off_tl[team].append(r)
                elif off_team and off_team != team:
                    off_local = [attack_frame(x, y) for x, y in
                                 by_team.get(off_team, [])]
                    r = classify_defense(local, off_local, cfg)
                    r.update({"t": tt, "team": team, "role": "defense"})
                    def_tl[team].append(r)
                else:
                    # 判不出球权时的退路：**仍然给出落位形状**，只是不带攻防语义。
                    # 真视频里球轨迹经常是空的（球太小/被遮挡），如果这里直接
                    # 跳过，整页阵型会全空 —— 而球员位置明明是好的，等于白白
                    # 丢掉一半有用信息。所以退化成"站位形状"，role 标成 unlabeled，
                    # 前端会明确提示"本场未判定攻防"。
                    r = classify_offense(local)
                    r.update({"t": tt, "team": team, "role": "unlabeled"})
                    off_tl[team].append(r)
                    unlabeled += 1

    # 空间曲线的存储抽稀（统计量已经全部算完，这里只影响"存多少"）
    #
    # ⚠️ 必须**按球队分别抽稀**：spacing_timeline 是 home/away 交替排列的，
    # 直接整体 [::2] 会恰好只留下同一支球队（采样 2Hz、两队交替时步长 2 就是
    # 完全混叠）——曲线会变成"只有客队"，而且不报任何错。
    spacing_stored = spacing_timeline
    if len(spacing_timeline) > cfg.max_spacing_points:
        per_team = max(1, cfg.max_spacing_points // max(1, len(teams)))
        buckets: dict[str, list] = {}
        for r in spacing_timeline:
            buckets.setdefault(r["team"], []).append(r)
        spacing_stored = []
        for t in teams:
            rows = buckets.get(t, [])
            # 用 ceil 而不是 floor：floor 会留下比预算多的点（900//150=6 只删到
            # 150 个，但 901//150=6 会留下 151 个；用 ceil 才严格不超预算）
            stride = max(1, math.ceil(len(rows) / per_team))
            spacing_stored.extend(rows[::stride])
        spacing_stored.sort(key=lambda r: (r["t"], r["team"]))
        notes.append(f"空间曲线由 {len(spacing_timeline)} 点抽稀到 "
                     f"{len(spacing_stored)} 点存储（统计量仍按全量计算）。")

    # ---- 传球判据二选一 ----
    # 球轨迹"连续可见"（合成数据、固定机位近景）-> 用控球切换判传球；
    # 球轨迹"只在空中可见"（真视频常态）-> 用飞行段反推传球。
    # 后者是这次真视频检验里唯一判出传球的路子（见 detect_passes_from_flights）。
    flights = detect_flights(ball_track, max_gap=cfg.flight_max_gap,
                             max_step_m=cfg.flight_max_step_m)
    passes_flight = detect_passes_from_flights(
        flights, idx, cfg, radius=cfg.flight_radius)
    # 两条判据**先过同一套质量门槛与去抖**，再比较 —— 否则拿"带噪声的
    # 控球路径"跟"干净的飞行段路径"比数量，永远是噪声多的那条赢。
    passes_poss_raw, passes_flight_raw = passes, passes_flight
    passes_poss = clean_passes(passes_poss_raw, cfg)
    passes_flight = clean_passes(passes_flight_raw, cfg)
    n_poss_pass = sum(1 for pp in passes_poss if pp.get("kind") in ("pass", "turnover"))
    n_flight_pass = sum(1 for pp in passes_flight
                        if pp.get("kind") in ("pass", "turnover"))
    # 平手时优先飞行段：它的证据链更直接（球真的从 A 飞到了 B），
    # 而控球路径依赖"球在谁手里"这个在真视频上更脆弱的推断。
    if n_flight_pass >= n_poss_pass and n_flight_pass > 0:
        passes = passes_flight
        pass_method = "flight"
        notes.append(
            f"传球由「飞行段反推」得到（{n_flight_pass} 次，"
            f"共 {len(flights)} 段飞行）；已过滤位移 <{cfg.pass_min_dist}m "
            "与 A→B 后又 B→A 的重复计数。")
    else:
        passes = passes_poss
        pass_method = "possession"
        # 注意别在文本里写裸的 `<`/`>`：战报是 Markdown，渲染时会走 HTML 转义，
        # 出现「位移 <0.5m」这种写法会被当成半个标签、在自检里报成"被转义的原始标签"。
        notes.append(f"传球由「控球人切换」得到（清洗后 {n_poss_pass} 次）；"
                     f"已过滤位移不足 {cfg.pass_min_dist}m 与方向抖动重复。")

    step(0.75, "汇总传球网络与阵型片段")
    # ---- 球员平均站位（局部进攻坐标）用于画传球网络 ----
    positions: dict[str, tuple[float, float]] = {}
    acc: dict[str, list] = {}
    for s in samples:
        acc.setdefault(s.player_id, []).append(s)
    for pid, lst in acc.items():
        positions[pid] = (_mean([s.x for s in lst]),
                          _mean([abs(s.y) for s in lst]))

    net = {}
    for team in teams:
        net[team] = pass_network([p for p in passes if p["team"] == team],
                                 players, positions)

    formation = {
        # 攻防是否真的判出来了（球轨迹可用时才有攻防语义）
        "roles_labeled": unlabeled == 0 and bool(spells),
        "unlabeled_frames": unlabeled,
        "offense": {t: _segments(off_tl[t], "t0", "t1", "shape",
                                 cfg.segment_min) for t in teams},
        "defense": {t: _segments(def_tl[t], "t0", "t1", "scheme",
                                 cfg.segment_min) for t in teams},
        "summary": {t: {
            "offense": _summarize_top([r["shape"] for r in off_tl[t]]),
            "defense": _summarize_top([r["scheme"] for r in def_tl[t]]),
        } for t in teams},
    }

    # ---- 空间指标汇总 + 曲线（前端画"间距曲线"直接用 spacing.timeline） ----
    space_summary = {}
    for team in teams:
        rows = [r for r in spacing_timeline if r["team"] == team]
        space_summary[team] = {
            k: round(_mean([r[k] for r in rows]), 2)
            for k in ("area", "mean_dist", "width", "depth", "radius")
        } if rows else {}

    poss_by_team = {t: sum(1 for s in poss if s["team"] == t) for t in teams}
    pass_by_team = {t: sum(1 for p in passes
                           if p["team"] == t and p["kind"] == "pass")
                    for t in teams}
    dur_by_team = {}
    for t in teams:
        d = sum(s["t1"] - s["t0"] for s in poss if s["team"] == t)
        dur_by_team[t] = round(d, 2)

    # ---- 战术亮点（可直接进战报 / 前端高亮条）----
    insights: list[dict] = []
    for team in teams:
        rows = [r for r in spacing_timeline if r["team"] == team]
        if rows:
            best = max(rows, key=lambda r: r["area"])
            insights.append({
                "type": "space", "team": team, "t": best["t"],
                "text": (f"{_team_label(players, team)} 在 {_mmss(best['t'])} "
                         f"打出最大空间：凸包面积 {best['area']} m²")})
        tl = formation["offense"].get(team) or []
        if tl:
            longest = max(tl, key=lambda s: s["t1"] - s["t0"])
            insights.append({
                "type": "formation", "team": team, "t": longest["t0"],
                "text": (f"{_team_label(players, team)} 有 "
                         f"{longest['t1'] - longest['t0']:.0f}s 一直在打"
                         f"「{longest['shape']}」")})
    longest_pass = None
    for p in passes:
        if p.get("kind") != "pass":
            continue
        if longest_pass is None or p["dist"] > longest_pass["dist"]:
            longest_pass = p
    if longest_pass:
        insights.append({
            "type": "pass", "team": longest_pass["team"], "t": longest_pass["t"],
            "text": (f"最远一次传球 {longest_pass['dist']}m："
                     f"{_pname(players, longest_pass['from'])} → "
                     f"{_pname(players, longest_pass['to'])}")})

    # 进攻方向：有球的一侧占绝对多数就报 left/right，两边都有不少进攻则报 mixed
    attack_side = "right"
    if side_votes:
        total_votes = sum(side_votes.values())
        side_major = max(side_votes, key=lambda k: side_votes[k])
        minority = min(side_votes.get(1, 0), side_votes.get(-1, 0))
        if total_votes and minority / total_votes > 0.15:
            attack_side = "mixed"
        else:
            attack_side = {1: "right", -1: "left"}.get(side_major, "right")
    available = bool(poss) or bool(spacing_timeline)
    reason = ""
    if not poss:
        reason = ("球轨迹覆盖不足，没能判出球权（传球网络为空）；"
                  "阵型与空间指标仍然有效。"
                  "真视频里球在空中只有 1 秒左右，球追踪必须逐帧。")
        notes.append(reason)
    if not ball_track:
        notes.append("没有球轨迹：无法给出传球网络与控球统计，"
                     "也无法区分进攻/防守（阵型退化为「落位形状」）。")
    elif unlabeled:
        notes.append(f"球轨迹覆盖不足，{unlabeled} 帧判不出球权："
                     "阵型部分只给出「落位形状」，未区分进攻/防守。")

    return {
        "available": available,
        "version": "1.0",
        "reason": reason,
        "config": {"frame_step": cfg.frame_step, "anim_step": cfg.anim_step,
                   "possess_radius": cfg.possess_radius,
                   "min_possession": cfg.min_possession,
                   "man_dist": cfg.man_dist},
        "coverage": {
            "player_samples": len(samples),
            "players": len({s.player_id for s in samples}),
            "ball_samples": len(list(ball_track)),
            "frames": len({r["t"] for r in spacing_timeline}),
            "duration": round(t_end, 2),
        },
        "attack_side": attack_side,
        "possession": {
            # total = 回合数（同队多次传导合并后）；
            # spells = 细粒度控球片段（每换一次手就是一段，传球网络靠它算）
            "total": len(poss),
            "spells_total": len(spells),
            "by_team": poss_by_team,
            "duration_by_team": dur_by_team,
            "avg_duration": round(_mean([s["t1"] - s["t0"] for s in poss]), 2),
            "avg_passes": round(_mean([s["passes"] for s in poss]), 2),
            "list": [{"t0": round(s["t0"], 2), "t1": round(s["t1"], 2),
                      "team": s["team"], "touches": s["touches"],
                      "passes": s["passes"], "turnovers": s["turnovers"],
                      "shots": s["shots"], "points": s["points"]}
                     for s in poss],
            "spells": [{"t0": round(s["t0"], 2), "t1": round(s["t1"], 2),
                        "team": s["team"], "player_id": s["player_id"]}
                       for s in spells],
        },
        "passes": {
            "method": pass_method,
            "flights": len(flights),
            "raw_possession": sum(1 for pp in passes_poss_raw
                                  if pp.get("kind") in ("pass", "turnover")),
            "raw_flight": sum(1 for pp in passes_flight_raw
                              if pp.get("kind") in ("pass", "turnover")),
            "cleaned_possession": n_poss_pass,
            "cleaned_flight": n_flight_pass,
            "total": sum(1 for p in passes if p.get("kind") == "pass"),
            # 失误/抢断：**不含**"投篮之后的球权转换"（那是比赛流程，不是丢球）
            "turnovers": sum(1 for p in passes if p.get("kind") == "turnover"),
            "possession_changes": sum(1 for p in passes
                                      if p.get("kind") == "possession_change"),
            "by_team": pass_by_team,
            "events": passes,
            "leaderboard": pass_leaderboard(passes, players),
        },
        "pass_network": net,
        "formation": formation,
        "spacing": {
            "teams": space_summary,
            "timeline": spacing_stored,
            "timeline_points": len(spacing_timeline),
        },
        "insights": insights,
        "notes": notes,
    }


def _team_label(players: dict, team: str) -> str:
    for p in (players or {}).values():
        if getattr(p, "team", "") == team:
            n = str(getattr(p, "name", "") or "")
            n = n.replace("号", "").rstrip("0123456789")
            if n and not n.startswith(("T", "t")):
                return n
    return "主队" if team == "home" else ("客队" if team == "away" else team)


def _pname(players: dict, pid: str) -> str:
    p = (players or {}).get(pid)
    return getattr(p, "name", None) or pid


def _mmss(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 60:02d}:{t % 60:02d}"


# --------------------------------------------------------------------------
# 6) 俯视战术图的逐帧数据（单独一个 jsonl，避免 tactics.json 过大）
# --------------------------------------------------------------------------
def build_tactics_frames(player_track: Sequence[PlayerSample],
                         ball_track,
                         duration: float = 0.0,
                         cfg: Optional[TacticsConfig] = None) -> list[dict]:
    """逐帧输出 {t, side, ball, players}，供前端做俯视战术图动画。

    坐标系仍是分析坐标：前端画半场时用 u = x + 7.5、v = |y|，
    这样左右半场自动叠到同一张半场上（进攻方向已被"折半"统一）。
    """
    cfg = cfg or TacticsConfig()
    samples = [s for s in player_track if isinstance(s, PlayerSample)]
    if not samples:
        return []
    idx = PlayerIndex(samples, cfg)
    bt = sorted(ball_track, key=lambda b: b.t)
    bt_times = [b.t for b in bt]
    t_end = duration or max(s.t for s in samples)
    out: list[dict] = []
    t = 0.0
    step_t = max(0.2, cfg.anim_step)
    prev_ids: set = set()
    while t <= t_end:
        pos = dedupe_positions(idx.at(t), prefer=prev_ids)
        if pos:
            prev_ids = set(pos)
            ball = None
            if bt_times:
                k = bisect.bisect_left(bt_times, t)
                cand = [j for j in (k - 1, k, k + 1) if 0 <= j < len(bt)]
                if cand:
                    j = min(cand, key=lambda j: abs(bt_times[j] - t))
                    if abs(bt_times[j] - t) <= max(2.0, cfg.max_gap):
                        ball = [round(bt[j].x, 3), round(bt[j].y, 3)]
            players = [[pid, idx.teams.get(pid, ""), round(px, 3), round(py, 3)]
                       for pid, (px, py) in sorted(pos.items())]
            side = 1
            if ball:
                side = attack_side_of(ball[1])
            out.append({"t": round(t, 2), "side": side, "ball": ball,
                        "players": players})
            if len(out) >= cfg.max_frames:
                break
        t += step_t
    return out


def write_tactics_frames(path: str, frames: Sequence[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for fr in frames:
            f.write(json.dumps(fr, ensure_ascii=False) + "\n")