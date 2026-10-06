"""数据来源适配层 —— 让整条管线「模型可插拔」。

三个 Source，输出同一种中间结构 RawTrack：
    SyntheticSource  合成一场比赛（零依赖，CI/答辩兜底，随时可跑）
    JsonlSource      读已有的 events/track.jsonl（前端与算法解耦开发）
    VideoSource      YOLOv8 + ByteTrack 真推理（需要 GPU/CPU + ultralytics）

这样安排的好处（答辩可以讲的产品化思路）：
  只要 RawTrack 契约不变，换检测模型、换球场标定方案都不影响后端与前端。
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .court import Calibration, apply_homography, fold_to_analysis
from .model import (
    COURT_LENGTH, COURT_WIDTH, HOOP_LEFT, HOOP_RIGHT, Player,
)
from .rules import BallSample
from .tactics import PlayerSample


# --------------------------------------------------------------------------
# 中间结构
# --------------------------------------------------------------------------
@dataclass
class Attempt:
    """一次出手候选（出手瞬间的信息）。

    made / conf 是「可选的先验证据」：
      * 合成数据源会填上（用来验证整条链路与统计口径）
      * 人工复核页修正后也会填上
      * 纯自动推理时留 None，此时完全靠球轨迹 + 记分牌两路证据融合
    """
    t: float
    team: str
    player_id: str
    x: float
    y: float
    period: int = 1
    clock: float = 0.0
    is_free_throw: bool = False
    release_frame: int = 0
    contest: float = 0.0
    made: Optional[bool] = None
    conf: Optional[float] = None
    # 人工/规则强制的分值（2 或 3）—— 坐标不准时比分牌仍然能定出分值
    forced_value: Optional[int] = None
    # 这条出手是哪来的：scoreboard / ball_track / manual / synthetic
    source: str = ""
    # 出手位置是怎么来的：rim_placeholder / ball_track / manual
    location_source: str = ""
    # 位置是否为估计值（单目球轨迹估计出的一律是）
    location_estimated: bool = False
    # 分值是不是「假设的」（没有球场标定时无法区分 2 分和 3 分，只能按默认值计）
    value_assumed: bool = False
    # 这次命中是否计入总分。比分牌可读但本片段没有比分事件时，
    # 视觉路径的命中标为 False（可能是回放/误检），仍然会出现在高光和报告里。
    counts_for_score: bool = True
    # 分值来源：manual / scoreboard / visual_estimate / forced / default
    value_source: str = ""
    # 标记（如 low_confidence）：报告与复核页据此提示"需人工确认"
    tags: list = field(default_factory=list)
    evidence: str = ""
    crossing_t: Optional[float] = None
    review_t: Optional[float] = None  # suggested review instant; not a measured crossing
    suggested_made: Optional[bool] = None
    decision_t: Optional[float] = None
    release_source: str = ""
    clip_end: Optional[float] = None  # 复核片段须覆盖判定收尾，不能只截离手后 1.5s

    def attach_shot_evidence(self, shot) -> None:
        """Keep inferred outcomes reviewable without treating them as observations."""
        self.evidence = shot.evidence
        self.crossing_t = shot.crossing_t
        if self.evidence:
            self.tags.append(self.evidence)
        if self.evidence in {"cross_interpolated", "cross_extrapolated"}:
            self.suggested_made = shot.made
            self.made = None
            self.tags.extend(["inferred_outcome", "needs_review"])

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class _ManualScoreScan:
    """用外部（手动框选 + OCR）算出的得分事件，充当比分牌扫描结果。

    为什么需要：现有识别靠样式模板，非标准台标（村BA/校园转播的底部横条）
    直接读不出来。而"手动框选 + OCR"这条路已实测可用
    （scripts/read_marked_scoreboard.py 在 Attleboro 那段读出 3 条得分事件）。
    本类把它接进管线，让比分牌路径在非标准台标上也能工作。
    接口只需 events / to_dict / frames_hit / final，与 ScoreboardScan 兼容。
    """

    def __init__(self, events, start=None, source="manual-ocr"):
        self.events = events
        self.source = source
        self.frames_hit = 1
        self.frames_read = 1
        # 基线是怎么来的、在哪一刻建立的、有没有被推迟 —— 决定"事件表全不全"，
        # 必须一路带到报告里（见下面 to_dict 的注释）。
        self.baseline_from = ""
        self.baseline_at = {}
        self.baseline_note = ""
        st = start or {}
        self.final = [
            int(st.get("home", 0)) + sum(int(e["delta"]) for e in events
                                         if e.get("team") == "home"),
            int(st.get("away", 0)) + sum(int(e["delta"]) for e in events
                                         if e.get("team") == "away"),
        ]
        self.team_names = {}

    def to_dict(self):
        return {"source": self.source, "events": self.events,
                "frames_hit": self.frames_hit,
                # CLI 会读 frames_read 打印"读到 x/y 帧"——替身对象也要给，
                # 否则 KeyError（实测踩到：整条推理跑完却在打印统计时崩掉）。
                "frames_read": max(1, self.frames_read or self.frames_hit),
                "final": self.final,
                # 基线位移：某队比分在画面开头读不出来时，基线会推迟到"第一次可读"那一刻，
                # 那之前的得分不会进事件表。报告要**说出来**，不能让用户以为事件表是全的
                # （实测 game_04：主队 31→33 被吞，终场 38:38 看着完全正确）。
                "baseline_from": self.baseline_from,
                "baseline_at": dict(self.baseline_at or {}),
                "baseline_note": self.baseline_note,
                "note": "由「手动框选记分牌 + Windows OCR」得到的事件"
                        "（不是模板匹配，非标准台标也能用）"}


@dataclass
class RawTrack:
    """一整个视频/一场比赛的检测结果聚合。"""
    fps: float = 30.0
    duration: float = 0.0
    video_path: Optional[str] = None
    width: int = 1920
    height: int = 1080
    players: dict[str, Player] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    ball_track: list[BallSample] = field(default_factory=list)
    # 球员逐帧球场坐标（套餐 B 战术层的输入）。
    # 真视频路径由 VideoSource 用单应矩阵把球员框底部中点投影到地面得到；
    # 合成数据源直接构造。为空时战术层会明确返回 available=False。
    player_track: list[PlayerSample] = field(default_factory=list)
    # 球员在**画面像素坐标**下的观测（脚底点 (foot_x, foot_y) + 检测框）。
    # 为什么必须留着：不管标定准不准，像素位置永远是准的。
    # 判「这次进球是哪一队」时要用它取「穿筐瞬间离篮筐最近的球员」——
    # 用投到地面的球场坐标反而会被坏标定带偏（实测一份错标定把离筐 1m 的
    # 球员算到了 8m 外，进球被记到另一队头上）。
    player_obs: list[dict] = field(default_factory=list)
    scoreboard_events: list[dict] = field(default_factory=list)
    # 开局带入比分：视频从半场中间开始录时，比分牌上已经有分。
    # 这部分分数是**真的**（比分牌写着），但没有对应的出手事件，
    # 所以单独记一笔并计入总分，绝不伪造出手来凑数。
    base_score: dict = field(default_factory=dict)
    base_period: int = 1
    detections_meta: dict = field(default_factory=dict)

    def save(self, path: str) -> None:
        d = {
            "fps": self.fps, "duration": self.duration,
            "video_path": self.video_path, "width": self.width,
            "height": self.height,
            "players": {k: v.to_dict() for k, v in self.players.items()},
            "attempts": [a.to_dict() for a in self.attempts],
            "ball_track": [b.__dict__ for b in self.ball_track],
            "player_track": [p.to_dict() for p in self.player_track],
            "player_obs": self.player_obs,
            "scoreboard_events": self.scoreboard_events,
            "base_score": self.base_score,
            "base_period": self.base_period,
            "detections_meta": self.detections_meta,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: str) -> "RawTrack":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        rt = RawTrack(fps=d.get("fps", 30.0), duration=d.get("duration", 0.0),
                      video_path=d.get("video_path"),
                      width=d.get("width", 1920), height=d.get("height", 1080),
                      scoreboard_events=d.get("scoreboard_events", []),
                      base_score=d.get("base_score", {}) or {},
                      base_period=d.get("base_period", 1) or 1,
                      detections_meta=d.get("detections_meta", {}))
        for k, v in d.get("players", {}).items():
            rt.players[k] = Player(**v)
        rt.attempts = [Attempt(**a) for a in d.get("attempts", [])]
        rt.ball_track = [BallSample(**b) for b in d.get("ball_track", [])]
        rt.player_track = [PlayerSample(**p) for p in d.get("player_track", [])]
        rt.player_obs = list(d.get("player_obs", []) or [])
        return rt


# --------------------------------------------------------------------------
# 1) 合成数据源
# --------------------------------------------------------------------------
# 出手点采样：按真实比赛的出手分布加权。
#
# 关键教训：不能「按 x/y 范围采样再事后判分区」。三分线是弧线，
# 用矩形范围描述「底角三分」会采出大量弧内的两分球 —— 我第一版就踩了这个坑，
# 结果合成数据的三分占比只有 6%，热区图也不像真实比赛。
# 正确做法：**先随机采点，再用 rules 里的规范判据 accept/reject**，
# 这样分区分布严格由几何定义决定，不会和主逻辑走偏。
#
# 采样参数用「距篮距离 d + 横向偏移 x」表达（x 横向 ±7.5，y 纵向）：
#   d  = 出手点到该侧篮筐的距离（米）
#   xr = 横向偏移范围（米，x=0 是中线，|x| 越大越贴边线）
TWO_PT_ZONES = [
    # (权重, 距篮距离范围, 横向偏移范围, 期望分区)
    (40, (0.3, 3.2), (-2.3, 2.3), "禁区"),
    (17, (3.5, 5.8), (-4.6, 4.6), "近距离中投"),
    (11, (5.8, 6.70), (-6.4, 6.4), "长两分"),
]

# 三分区统一用「以被进攻篮筐为原点的极坐标」描述：
#   角度 a 以纵向（指向中场）为 0，向边线方向增大；
#   横向偏移 |x| = d·sin(a)，所以：
#     a < 30°  -> |x| < 3.8  -> 弧顶
#     30~60°   -> |x| 4.0~5.8 -> 45°
#     a > 65°  -> |x| > 6.6   -> 底角
THREE_ARC_SECTORS = [
    (10, 32.0, 62.0),     # 右侧 45°
    (10, -62.0, -32.0),   # 左侧 45°
    (18, -30.0, 30.0),    # 弧顶
]

# 三分线距离范围：弧顶到边线的合理三分射程
THREE_DIST_RANGE = (6.9, 8.4)
# 底角三分单独采样（横向幅度很窄，用极坐标很难采到，直接按 |x| 采）
CORNER_X_RANGE = (6.70, 7.30)


def _sample_two(rng: random.Random) -> tuple[float, float]:
    """在三分线内采一个点（保证判成两分且分区正确）。"""
    from .rules import is_three_pointer, zone_of

    total = sum(w for w, _, _, _ in TWO_PT_ZONES)
    for _ in range(600):
        r = rng.uniform(0, total)
        acc = 0.0
        pick = None
        for w, dr, xr, zname in TWO_PT_ZONES:
            acc += w
            if r <= acc:
                pick = (dr, xr, zname)
                break
        if pick is None:
            continue
        dr, xr, zname = pick
        x = rng.uniform(*xr)          # 横向
        d = rng.uniform(*dr)          # 距篮距离
        # 该侧篮筐在 (0, ±1.575)；把点放在右半场，稍后随机镜像
        hy = HOOP_RIGHT[1]
        y = hy + math.sqrt(max(0.0, d * d - x * x))
        if is_three_pointer(x, y):
            continue                   # 采到弧外了，重采
        if zone_of(x, y) != zname:
            continue
        return (x, y) if rng.random() < 0.5 else (-x, -y)
    return (rng.uniform(-1.5, 1.5), HOOP_RIGHT[1] + rng.uniform(0.6, 3.0))


def _sample_three(rng: random.Random) -> tuple[float, float]:
    """在三分线外采样：底角（按 |x| 直采）+ 两侧 45° + 弧顶（极坐标）。"""
    from .rules import is_three_pointer

    # 38% 给底角。底角横向幅度只有 0.6m 左右，用极坐标很难采到，
    # 所以直接采横向偏移 |x|，再反解出纵向位置。
    if rng.random() < 0.38:
        for _ in range(400):
            x = rng.choice((1, -1)) * rng.uniform(*CORNER_X_RANGE)
            d = rng.uniform(THREE_DIST_RANGE[0], THREE_DIST_RANGE[1])
            dy2 = d * d - x * x
            if dy2 <= 0:
                continue
            y = HOOP_RIGHT[1] + math.sqrt(dy2)
            if abs(x) > 7.4 or y > 14.0:
                continue
            if is_three_pointer(x, y):
                return (x, y)
        return (6.9, HOOP_RIGHT[1] + 2.2)

    total = sum(w for w, _, _ in THREE_ARC_SECTORS)
    for _ in range(600):
        r = rng.uniform(0, total)
        acc = 0.0
        sector = None
        for w, a0, a1 in THREE_ARC_SECTORS:
            acc += w
            if r <= acc:
                sector = (a0, a1)
                break
        if sector is None:
            continue
        ang = math.radians(rng.uniform(*sector))
        dist = rng.uniform(*THREE_DIST_RANGE)
        # 角度以纵向为 0：cos 给纵向分量，sin 给横向分量
        x = dist * math.sin(ang)
        y = HOOP_RIGHT[1] + dist * math.cos(ang)
        if abs(x) > 7.4 or y > 14.0:
            continue
        if is_three_pointer(x, y):
            # 底角已单独采样，这里避免重复采到底角带
            return (x, y) if rng.random() < 0.5 else (-x, -y)
    return (0.0, HOOP_RIGHT[1] + 7.2)


def _sample_shot_point(rng: random.Random) -> tuple[float, float]:
    """采样一个合法的出手点，保证分区归属正确。

    约 32% 三分、68% 两分；两分与三分都用 accept/reject 保证落在期望分区。
    """
    if rng.random() < 0.32:
        return _sample_three(rng)
    return _sample_two(rng)


def _zone_make_rate(x: float, y: float) -> float:
    """按分区给一个合理的命中率 —— 让合成数据看起来像真的比赛。

    距离与三分判定都复用 rules 里的规范实现，避免这里再抄一份几何逻辑
    （抄一份就一定会和主逻辑走偏，这是合成数据最容易出的错）。
    """
    from .rules import is_three_pointer, nearest_hoop

    hx, hy = nearest_hoop(x, y)
    d = math.hypot(x - hx, y - hy)
    if d < 2.0:
        return 0.62      # 篮下
    if d < 4.0:
        return 0.48      # 禁区
    if not is_three_pointer(x, y):
        if d < 5.8:
            return 0.40  # 近距离中投
        return 0.37      # 长两分
    # 三分：底角（横向贴边线）比弧顶略准
    corner = abs(x) >= 6.6
    return 0.42 if corner else 0.36


def _simulate_ball_flight(rng: random.Random, t0: float, x: float, y: float,
                          made: bool) -> list[BallSample]:
    """给一次出手生成球轨迹采样（用于验证 detect_rim_events 与展示）。

    轨迹分两段：
      * 飞行段（u < 0.80）：水平位置线性插值到「篮筐平面投影点」，
        再叠加一个中途张开、末段收拢的横向弧度弧（真实投篮的弧线感）；
        高度走一条抛物线到篮筐上方。
      * 终段（u >= 0.80）：模拟球与篮筐的交互。命中时球落到筐下；
        未中时球撞筐外侧（横向偏出），高度抬高 —— 依此区分穿筐与打铁。
    """
    # 攻哪一侧由纵向坐标 y 决定（x 是横向，不携带攻防方向信息）
    hoop = HOOP_RIGHT if y >= 0 else HOOP_LEFT
    out: list[BallSample] = []
    flight = rng.uniform(0.9, 1.4)
    peak = 3.05 + rng.uniform(0.6, 1.4)

    # 落点：命中时弹道收在篮筐圆心附近；未中时偏出 0.30~0.60m
    if made:
        end_x = hoop[0] + rng.uniform(-0.10, 0.10)
        end_y = hoop[1] + rng.uniform(-0.18, 0.18)
    else:
        off = rng.uniform(0.30, 0.60)
        ang = rng.uniform(0, 2 * math.pi)
        end_x = hoop[0] + off * math.cos(ang)
        end_y = hoop[1] + off * math.sin(ang)

    ARC_END = 0.80          # 飞行段结束、进入篮筐交互段
    arc_x = rng.uniform(-1.2, 1.2)      # 弧线中途的最大横向偏移
    arc_y = rng.uniform(-1.2, 1.2)

    n = max(12, int(flight * 30))
    for i in range(n + 1):
        u = i / n
        t = t0 + u * flight
        if u <= ARC_END:
            k = u / ARC_END
            bx = x + (end_x - x) * k + arc_x * math.sin(math.pi * k)
            by = y + (end_y - y) * k + arc_y * math.sin(math.pi * k)
            # 高度：0.9*u 处的抛物线达到峰值，u=ARC_END 时略高于筐
            bz = 2.1 + (peak - 2.1) * math.sin(math.pi * min(1.0, u * 0.95))
            # 保证到筐附近时高度接近筐上沿之上一点
            if k > 0.7:
                bz = max(bz, 3.05 + (1 - (k - 0.7) / 0.3) * 0.5)
        else:
            k = (u - ARC_END) / (1 - ARC_END)
            bx, by = end_x, end_y
            if made:
                # 从上往下穿过筐平面，随后继续下落
                bz = 3.25 - 0.95 * k
            else:
                # 打铁：球被弹开，位置横向偏移、高度回到筐上方
                bx = end_x + (end_x - hoop[0]) * k * 0.8
                by = end_y + (end_y - hoop[1]) * k * 0.8
                bz = 3.05 + 0.55 * k
        out.append(BallSample(t=round(t, 3), x=round(bx, 3),
                              y=round(by, 3), z=round(max(0.4, bz), 3),
                              conf=1.0))
    return out


def synthetic_game(seed: int = 7, duration: float = 720.0,
                   home_name: str = "主队", away_name: str = "客队",
                   players_per_team: int = 5,
                   fps: float = 30.0) -> RawTrack:
    """合成一场比赛（默认 12 分钟 = 单节或集锦用的时长）。

    产物刻意做得「像真的」：
      * 每队 5 名球员，有球衣号与合理的得分分布
      * 出手点服从真实空间分布，命中率按分区给
      * 有罚球、有篮板/助攻/抢断/盖帽事件
      * 球轨迹会真的穿过篮筐（供 detect_rim_events 验证）
    """
    rng = random.Random(seed)
    rt = RawTrack(fps=fps, duration=duration)

    players: list[Player] = []
    for team, tname in (("home", home_name), ("away", away_name)):
        for i in range(players_per_team):
            pid = f"{team[0].upper()}{i + 1}"
            rt.players[pid] = Player(pid, f"{tname}{i + 1}号", team,
                                     jersey=rng.randint(3, 55))
            players.append(rt.players[pid])

    t = 5.0
    period = 1
    period_len = duration / 4.0
    while t < duration - 8:
        if t > period * period_len:
            period = min(4, period + 1)
        team = "home" if rng.random() < 0.52 else "away"
        squad = [p for p in players if p.team == team]
        shooter = rng.choice(squad)

        is_ft = rng.random() < 0.10
        if is_ft:
            # 罚球点：篮筐正前方（纵向外移）4.6m，横向居中
            hoop = HOOP_RIGHT if rng.random() < 0.5 else HOOP_LEFT
            x = 0.0
            y = hoop[1] + (4.6 if hoop[1] > 0 else -4.6)
        else:
            x, y = _sample_shot_point(rng)

        p_make = 0.75 if is_ft else _zone_make_rate(x, y)
        n_ft = rng.choice([1, 2, 2, 3]) if is_ft else 1

        for k in range(n_ft):
            tt = round(t + k * 1.6, 2)
            made = rng.random() < p_make
            a = Attempt(t=tt, team=team, player_id=shooter.player_id,
                        x=x, y=y, period=period,
                        clock=round(max(0.0, period * period_len - tt), 1),
                        is_free_throw=is_ft,
                        release_frame=int(tt * fps),
                        contest=round(rng.uniform(0, 1), 2),
                        made=made, conf=round(rng.uniform(0.75, 1.0), 2))
            rt.attempts.append(a)
            rt.ball_track.extend(_simulate_ball_flight(rng, tt, x, y, made))
            if made:
                val = 1 if is_ft else (3 if _is_three(x, y) else 2)
                rt.scoreboard_events.append(
                    {"t": round(tt + rng.uniform(1.0, 2.5), 2),
                     "team": team, "delta": val, "source": "synthetic_ocr"})

        # 其他事件：篮板/助攻/抢断/盖帽/失误/犯规
        def maybe(etype: str, p: float, subject: Optional[Player] = None):
            if rng.random() < p:
                event_t = round(t + rng.uniform(0.5, 3.0), 2)
                actor = subject or rng.choice(squad)
                rt.detections_meta.setdefault("events", []).append(
                    {"t": event_t, "type": etype, "team": actor.team,
                     "player_id": actor.player_id, "period": period})

        mid = [p for p in players if p.team != team]
        maybe("rebound", 0.45, rng.choice(squad + mid))
        maybe("assist", 0.30)
        maybe("steal", 0.10, rng.choice(mid))
        maybe("block", 0.07, rng.choice(mid))
        maybe("turnover", 0.12)
        maybe("foul", 0.15)

        t += rng.uniform(9.0, 20.0)

    # 套餐 B：给每个出手补上"球是怎么传到他手里的"——
    # 球员轨迹（阵型/间距/俯视战术图）+ 持球段球轨迹（控球与传球网络）。
    positions, hold_balls = synthetic_player_track(rng, rt.players,
                                                   rt.attempts, duration)
    rt.player_track = positions
    rt.ball_track.extend(hold_balls)
    # 飞行段（source="flight"）与持球段（source="possession"）都留在
    # ball_track 里：前者给进球判定用，后者给战术层用，靠 source 区分。
    for b in rt.ball_track:
        # _simulate_ball_flight 造出来的采样点用的是 BallSample 的默认 source
        # （"track"）；合成数据里它们一律是"飞行段"，标清楚给 detect_rim_events 用。
        if getattr(b, "source", "track") in ("", "track"):
            b.source = "flight"
    rt.ball_track.sort(key=lambda b: b.t)
    rt.detections_meta["source"] = "synthetic"
    rt.detections_meta["seed"] = seed
    rt.detections_meta["tactics"] = {
        "player_samples": len(rt.player_track),
        "possession_balls": sum(1 for b in rt.ball_track
                                if getattr(b, "source", "") == "possession"),
        "flight_balls": sum(1 for b in rt.ball_track
                            if getattr(b, "source", "") == "flight"),
    }
    return rt


# --------------------------------------------------------------------------
# 1b) 合成球员轨迹（套餐 B 战术层的数据来源）
# --------------------------------------------------------------------------
# 进攻落位模板 —— **局部进攻坐标** (x, d)：x 横向 ±7.5，d = 离本方底线的纵深
# （0 = 底线，14 = 中圈）。被进攻的篮筐在局部坐标里永远是 (0, 1.575)。
#
# 这些数字不是随手写的：每一套都经过 rules.is_three_pointer 的口径验算，
# 保证 classify_offense 能把它们认成对应的形状（测试里有断言）。
SYNTH_OFFENSE_SETS = {
    "五外拉开": [(0.0, 8.4), (-5.6, 6.4), (5.6, 6.4), (-6.8, 1.9), (6.8, 1.9)],
    "四外一内": [(0.0, 8.4), (-5.6, 6.4), (5.6, 6.4), (-6.8, 1.9), (0.0, 2.7)],
    "三外两内": [(0.0, 8.4), (-5.6, 6.4), (5.6, 6.4), (-2.5, 2.5), (2.5, 2.5)],
    "双塔（内线为主）": [(0.0, 7.6), (-6.0, 5.2), (6.0, 5.2), (-1.7, 2.1), (1.7, 2.1)],
}

# 防守站位模板（局部进攻坐标）。"人盯人"没有固定模板，按进攻人逐一对位。
SYNTH_DEFENSE_SETS = {
    "2-3 联防": [(-2.6, 6.2), (2.6, 6.2), (-5.6, 1.9), (0.6, 1.7), (5.6, 1.9)],
    "3-2 联防": [(-4.2, 6.3), (0.0, 6.7), (4.2, 6.3), (-3.0, 2.0), (3.0, 2.0)],
    "1-3-1 联防": [(0.0, 7.7), (-4.4, 4.7), (0.0, 4.5), (4.4, 4.7), (0.0, 1.8)],
}
SYNTH_MAN = "人盯人"


def _unit_to_rim(x: float, d: float) -> tuple[float, float]:
    """从 (x, d) 指向被进攻篮筐 (0, 1.575) 的单位向量。"""
    vx, vd = 0.0 - x, 1.575 - d
    n = math.hypot(vx, vd)
    return (0.0, 0.0) if n < 1e-9 else (vx / n, vd / n)


def stitch_player_tracks(tracks: list, max_gap: float = 3.0,
                         max_dist: float = 2.5, max_iter: int = 8
                         ) -> tuple[list, dict]:
    """把断开的球员轨迹碎片接回同一个人（tracklet stitching）。

    为什么需要：ByteTrack 是**逐帧在线**关联的，球员一被遮挡/出画再回来，
    就会拿到一个新的 track ID。实测一段 30 秒的广播片段裂出了 **83 条** 轨迹
    （真实场上只有 10 个人）——传球网络会因此碎成一地，每个"人"只传一两次。

    判据（简单、可解释、不依赖任何模型）：
      两条轨迹**时间上不重叠**（A 结束后 B 才开始）、间隔 <= max_gap、
      且 A 的最后位置与 B 的起始位置距离 <= max_dist -> 认为是同一个人。

    只做"端点相接"，所以不会把同时在场的两个人错误合并
    （时间不重叠是硬约束，这是它安全的原因）。
    """
    by_id: dict[str, list] = {}
    for s in sorted(tracks, key=lambda x: x.t):
        by_id.setdefault(s.player_id, []).append(s)
    changed = True
    it = 0
    while changed and it < max_iter:
        changed = False
        it += 1
        ids = sorted(by_id, key=lambda k: by_id[k][0].t)
        for i, a in enumerate(ids):
            if a not in by_id:
                continue
            for b in ids[i + 1:]:
                if b not in by_id:
                    continue
                ta, tb = by_id[a], by_id[b]
                if tb[0].t < ta[-1].t:          # 时间重叠 -> 同时在场，绝不合并
                    continue
                gap = tb[0].t - ta[-1].t
                if gap > max_gap:
                    continue
                d = math.hypot(tb[0].x - ta[-1].x, tb[0].y - ta[-1].y)
                if d > max_dist:
                    continue
                # 把 b 接到 a 后面
                for s in tb:
                    s.player_id = ta[0].player_id
                    s.team = ta[0].team
                by_id[a] = ta + tb
                del by_id[b]
                changed = True
                break
            if changed:
                break
    out: list = []
    for lst in by_id.values():
        out.extend(lst)
    out.sort(key=lambda x: x.t)
    meta = {"raw_tracks": len({s.player_id for s in tracks}),
            "merged_tracks": len(by_id),
            "iterations": it}
    return out, meta


def _synth_possession(rng: random.Random, t0: float, t1: float,
                      team: str, side: int, mates: list[str],
                      shot_xy: tuple[float, float], shooter: str,
                      opp_ids: list[str]
                      ) -> tuple[list[PlayerSample], list[BallSample]]:
    """生成**一次球权**的球员轨迹与持球段球轨迹。

    side: -1 = 攻左半场（y<0），+1 = 攻右半场（y>0）。
          局部坐标 (x, d) 换算回分析坐标就是 (x, side * d)。
    """
    samples: list[PlayerSample] = []
    balls: list[BallSample] = []

    def to_court(x: float, d: float) -> tuple[float, float]:
        return x, side * d

    # ---- 进攻站位 ----
    set_name = rng.choice(list(SYNTH_OFFENSE_SETS))
    spots = [list(s) for s in SYNTH_OFFENSE_SETS[set_name]]
    order = list(mates)
    rng.shuffle(order)
    off_pos: dict[str, tuple[float, float]] = {}
    shot_local = (float(shot_xy[0]), abs(float(shot_xy[1])))
    for i, pid in enumerate(order):
        off_pos[pid] = tuple(spots[i % len(spots)])
    if shooter in off_pos:
        # 出手点以真实出手记录为准 —— 战术图和热区图因此天然对得上
        off_pos[shooter] = shot_local

    # ---- 防守站位 ----
    def_name = rng.choice([SYNTH_MAN] + list(SYNTH_DEFENSE_SETS))
    opp = "away" if team == "home" else "home"
    # 防守人必须挂在**对方真实球员 ID** 上：否则传球网络 / 球员列表里会出现
    # 一堆幽灵球员（曾经造出过 "Hd0" 这种 ID，把"10 名球员"变成 20 名）。
    defenders = list(opp_ids) or [f"{opp[0].upper()}{i + 1}" for i in range(5)]
    def_pos: dict[str, tuple[float, float]] = {}
    if def_name == SYNTH_MAN:
        for i, op in enumerate([off_pos[p] for p in sorted(off_pos)]):
            ux, ud = _unit_to_rim(*op)
            def_pos[defenders[i % len(defenders)]] = (op[0] + ux * 1.15,
                                                      op[1] + ud * 1.15)
    else:
        for i, sp in enumerate(SYNTH_DEFENSE_SETS[def_name]):
            def_pos[defenders[i % len(defenders)]] = tuple(sp)

    # ---- 传球序列：2~4 次传导，最后到出手人 ----
    others = [m for m in mates if m != shooter]
    rng.shuffle(others)
    chain = others[:rng.randint(1, 2)] + [shooter]
    if len(chain) < 2:
        chain = [shooter]

    hold_start, hold_end = t0 + 0.7, t1 - 0.25
    n_seg = max(1, len(chain))
    span = max(0.0, hold_end - hold_start)
    step = span / n_seg if n_seg else span
    # 每一段: (开始时刻, 谁是持球人)
    segs = []
    for i, pid in enumerate(chain):
        segs.append((hold_start + i * step, pid))

    def handler_at(t: float):
        """返回 (持球人, 是否在传球窗口, 传球起点, 传球终点, 插值系数)。

        每一段的**开头 0.55s** 是传球窗口：球正从上一个人飞过来，
        这段时间里球不再贴着任何一个人 —— 控球判定因此会切出一段
        "无人控球"，传球事件就是靠这个缺口被识别出来的。
        """
        cur_i = 0
        for i, (ts, _pid) in enumerate(segs):
            if t >= ts:
                cur_i = i
            else:
                break
        ts, pid = segs[cur_i]
        if cur_i > 0 and t - ts < 0.55:
            return (pid, True, segs[cur_i - 1][1], pid, (t - ts) / 0.55)
        return (pid, False, None, None, 0.0)

    # ---- 采样 ----
    t = t0
    dt = 0.5
    while t <= t1 + 1e-9:
        jitter = (rng.uniform(-0.20, 0.20), rng.uniform(-0.20, 0.20))
        for pid, (px, pd) in off_pos.items():
            x, y = to_court(px + jitter[0], pd + jitter[1])
            samples.append(PlayerSample(t=round(t, 3), player_id=pid,
                                        team=team, x=round(x, 3),
                                        y=round(y, 3), conf=0.95))
        for pid, (px, pd) in def_pos.items():
            x, y = to_court(px + jitter[0] * 0.6, pd + jitter[1] * 0.6)
            samples.append(PlayerSample(t=round(t, 3), player_id=pid,
                                        team=opp, x=round(x, 3),
                                        y=round(y, 3), conf=0.95))
        # 球：持球人脚下，传球窗口内做插值
        holder, passing, a_pid, b_pid, alpha = handler_at(t)
        if passing and a_pid in off_pos and b_pid in off_pos:
            ax, ad = off_pos[a_pid]
            bx, bd = off_pos[b_pid]
            bx = ax + (bx - ax) * alpha
            bd = ad + (bd - ad) * alpha
        else:
            bx, bd = off_pos.get(holder, (0.0, 5.0))
        cx, cy = to_court(bx, bd)
        balls.append(BallSample(t=round(t, 3), x=round(cx, 3),
                                y=round(cy, 3), z=1.2,
                                conf=0.9, source="possession"))
        # 传球中加密采样，让"球离开 A、到 B 手上"这件事能被控球判定切开
        if passing:
            for k in range(1, 5):
                tt = t + 0.55 * k / 5.0
                if tt > t1:
                    break
                holder2, passing2, a2, b2, al2 = handler_at(tt)
                if passing2 and a2 in off_pos and b2 in off_pos:
                    ax, ad = off_pos[a2]
                    bxx, bdd = off_pos[b2]
                    mx = ax + (bxx - ax) * al2
                    md = ad + (bdd - ad) * al2
                    ccx, ccy = to_court(mx, md)
                    balls.append(BallSample(t=round(tt, 3), x=round(ccx, 3),
                                            y=round(ccy, 3), z=1.2,
                                            conf=0.9, source="possession"))
        t += dt
    return samples, balls


def synthetic_player_track(rng: random.Random, players: dict,
                           attempts: list, duration: float
                           ) -> tuple[list[PlayerSample], list[BallSample]]:
    """为合成比赛生成球员轨迹 + 持球段球轨迹。

    为什么合成数据也要做这个：套餐 B 的战术层（传球网络 / 阵型 / 间距 /
    俯视战术图）全部依赖**球员轨迹**。如果只在真视频路径上有轨迹，
    那么在没有显卡、没有素材的答辩机上就完全看不到 B 的效果 ——
    这正是"演示必须能离线复现"这条原则要避免的。
    """
    squad: dict[str, list[str]] = {}
    for pid, p in players.items():
        squad.setdefault(p.team, []).append(pid)
    for v in squad.values():
        v.sort()
    # 主队攻左半场（y<0）、客队攻右半场（y>0）—— 与
    # VideoSource.home_hoop="left" 的默认约定一致。
    side_of = {"home": -1, "away": 1}

    atk = sorted(attempts, key=lambda a: a.t)
    if not atk:
        return [], []

    # 把出手按球队/时间连成"球权"（连续罚球算同一次球权）
    poss: list[dict] = []
    for a in atk:
        if poss and a.team == poss[-1]["team"] and a.t - poss[-1]["t1"] <= 8.0:
            poss[-1]["shots"].append(a)
            poss[-1]["t1"] = a.t
        else:
            poss.append({"team": a.team, "t1": a.t, "shots": [a]})

    samples: list[PlayerSample] = []
    balls: list[BallSample] = []
    prev_end = -1.5
    for seg in poss:
        t1 = seg["t1"]
        t0 = max(0.0, t1 - rng.uniform(7.0, 15.0), prev_end + 1.5)
        if t1 - t0 < 1.5:
            t0 = max(0.0, t1 - 1.5)
        prev_end = t1
        team = seg["team"]
        mates = squad.get(team, [])
        if len(mates) < 3:
            continue
        last = seg["shots"][-1]
        if last.player_id in mates:
            shooter = last.player_id
        else:
            shooter = mates[0]
        opp = "away" if team == "home" else "home"
        s, b = _synth_possession(rng, t0, t1, team, side_of.get(team, -1),
                                 mates, (last.x, last.y), shooter,
                                 squad.get(opp, []))
        samples.extend(s)
        balls.extend(b)

    samples.sort(key=lambda s: s.t)
    balls.sort(key=lambda x: x.t)
    return samples, balls


def _is_three(x: float, y: float) -> bool:
    from .rules import is_three_pointer
    return is_three_pointer(x, y)


# --------------------------------------------------------------------------
# 2) JSONL / JSON 回放源
# --------------------------------------------------------------------------
def jsonl_source(path: str) -> RawTrack:
    """从之前跑过、已经存好的 raw_track.json 载入 —— 跳过推理，
    让前后端在无 GPU 的机器上也能开发调试。"""
    return RawTrack.load(path)


# --------------------------------------------------------------------------
# 3) 真视频源：YOLOv8 检测 + ByteTrack 跟踪 + 单应变换
# --------------------------------------------------------------------------
class VideoSource:
    """真推理路径。

    依赖（可选安装）：ultralytics, opencv-python

    **设计要点（这是「3 分钟视频跑出 0:0」的修复）：**

      原版的 run() 只收集了球员框和球的位置，`extract_attempts()` 是个 TODO，
      于是 attempts 恒为空、比分恒为 0:0。而「靠 YOLO 检测篮球」这条路在
      720p 远景画面里根本走不通（COCO 的 sports ball 几乎不触发）。

      现在改成**两路证据分工**：

        比分牌扫描（scoreboard.py）—— 负责**计分与命中判定**
            广播台标由导播系统直接渲染，数字零噪声。它给出「谁、在什么时刻、
            得了 1/2/3 分」，这是规则级确定的事实，不需要认识球也不需要认识人。

        球轨迹（ball.py）—— 负责**出手位置与出手时刻的细化**
            球能追到就用球的位置当出手点；追不到就退回「被进攻篮筐附近」的
            占位坐标，并明确标记 location_estimated，绝不假装是测量值。

      球员身份则用球衣颜色聚类分成主/客两队，不再是「一律 home」。
    """

    def __init__(self, video_path: str, calibration: Calibration,
                 weights: str = "yolov8n.pt", conf: float = 0.25,
                 imgsz: int = 960, device: Optional[str] = None,
                 stride: int = 1,
                 player_stride: int = 2,
                 scoreboard: bool = True,
                 detect_players: bool = True,
                 ball_cfg: Optional["BallConfig"] = None,
                 sb_cfg=None,
                 hoop_cfg=None,
                 home_hoop: str = "left",
                 visual_shot_value: int = 2,
                 jersey_teams: Optional[dict] = None,
                 score_policy: str = "court",
                 hoop_hint: Optional[tuple] = None,
                 sliding: Optional[dict] = None,
                 auto_sliding: bool = True,
                 sliding_stride: int = 5,
                 sliding_max_seconds: float = 0.0,
                 half_court: bool = True,
                 player_conf: float = 0.40,
                 max_players: int = 12,
                 tracker: str = "botsort.yaml",
                 ball_weights: str = "",
                 hoop_weights: str = "",
                 hoop_sight_cfg=None,
                 basket_teams: Optional[dict] = None,
                 basket_lookback_s: float = 1.5,
                 manual_hoop=None,
        scoreboard_events=None,
                 basket_labels=None,
                 basket_model=None,
                 multi_cal=None,
                 shot_engine: str = "legacy", legacy_center_lock: bool = False):
        if shot_engine not in ("legacy", "geometry"):
            raise ValueError("shot_engine 必须为 legacy 或 geometry")
        self.shot_engine = shot_engine
        self.legacy_center_lock = legacy_center_lock
        self.video_path = video_path
        self.cal = calibration
        self.weights = weights
        self.conf = conf
        self.imgsz = imgsz
        self.device = device
        self.stride = stride
        # 逐帧读取/追球，但球员检测抽帧 —— 两者对时间分辨率的要求完全不同
        self.player_stride = max(1, int(player_stride))
        self.scoreboard = scoreboard
        # False = 跳过逐帧 YOLO（CPU 上这是唯一的性能瓶颈）。
        # 比分牌那条路根本不需要 YOLO，所以「只要比分/得分事件」时把它关掉，
        # 3 分钟视频从几十分钟缩到一两分钟。
        self.detect_players = detect_players
        self.ball_cfg = ball_cfg
        self.sb_cfg = sb_cfg
        # 「球 + 篮筐」视觉路径的阈值（比分牌不可用时启用）
        self.hoop_cfg = hoop_cfg
        self.home_hoop = home_hoop
        # 没有球场标定时，视觉路径无法分辨 2 分和 3 分，统一按这个分值计
        self.visual_shot_value = int(visual_shot_value)
        # 球衣颜色 -> 队伍。默认 None 表示用「色相聚类 + 绿色算主队」的启发式。
        self.jersey_teams = jersey_teams
        # 计分口径：
        #   court（默认）—— 按场上检测到的进球计分；比分牌读数只作参考，不计入；
        #   visual        —— 同 court，视觉命中直接计入；
        #   scoreboard    —— 以广播比分牌带入分/得分事件为准；
        #   auto          —— 有比分牌得分事件时用比分牌事件，否则按场上进球计分。
        self.score_policy = (score_policy if score_policy in
                             ("court", "visual", "scoreboard", "auto")
                             else "court")
        # 手动篮筐提示 (cx, cy, r?)；移动机位/复杂场景下用它锁定篮筐。
        self.hoop_hint = hoop_hint
        # 多机位标定（multical.MultiCal）：**每个镜头一份 H**。
        # 为什么需要：一台机位拍不到全场，实际素材常是"两个机位各拍半场"。
        # 两个机位没有共同坐标系，用其中一份 H 去投另一个镜头的画面，
        # 球员会被投到球场外（实测）。所以按帧所属镜头取标定：`_cal_at(t)`。
        self.multi_cal = multi_cal
        # 逐帧滑动标定（calibcheck.sliding_calibration 的结果）。
        # 给了它就用**每一帧各自的 H** 投球员，而不是一份静态 H ——
        # 实测真实素材（含所谓固定机位）几十秒就会漂到边线跑掉，
        # 只有逐帧跟住才能得到正确的俯视战术图。
        self.sliding = sliding
        # 静态标定用不了（没标定 / 对不上机位 / 校验不达标）时，
        # **自动**改用逐帧滑动标定 —— 这是真视频能出战术图的兜底路径。
        # 以前没有这一步：上传的视频一旦没有匹配的标定就直接判死，
        # 界面上永远只显示「本场没有战术数据」。
        self.auto_sliding = auto_sliding
        self.sliding_stride = max(1, int(sliding_stride))
        self.sliding_max_seconds = float(sliding_max_seconds)
        self.half_court = bool(half_court)
        # 球员检测的**独立**置信度门槛（球用 self.conf，两者要求不同）：
        # 0.25 是给球用的；人用 0.25 会捞出一堆场边观众。
        self.player_conf = float(player_conf)
        # 半场里同时最多可能有几个人（两队 10 人 + 裁判）。超出的按置信度丢，
        # 这是压制"一帧 19 个点"最直接的一刀。
        self.max_players = max(2, int(max_players))
        self.tracker = tracker
        # 专用球检测权重（例如 ShotTracker 的 best.pt：类别 = basketball / rim）。
        # 为什么必须有它：COCO 的 sports ball 在 720p 远景里几乎不触发，
        # 而"橙色色块"这条线索在广播画面上每帧能产出 ~70 个候选
        # （球衣/木地板/皮肤全是橙色）—— 实测 30 秒 6.2 万个候选、3614 条假轨迹。
        # 换成在篮球素材上训过的检测器后降到 ~0.9 个/帧，量级终于对了。
        self.ball_weights = ball_weights or ""
        # 训练好的篮筐检测权重（scripts/label_rim.py 标注 + 训练得到）。
        # 不给就走手工颜色+形状启发式 —— 那条路在真实素材上只有 1~3%
        # 帧覆盖率，还会把黄色球衣当篮筐。
        self.hoop_weights = hoop_weights or ""
        # 「篮下判进球」（hoopsight）的阈值；None 用默认。
        # 这条路不依赖球检测器，是**没有比分牌时自动计分的主证据**。
        self.hoop_sight_cfg = hoop_sight_cfg
        # 人工指定「第 N 个进球是哪一队」：{序号(1 起): "home"/"away"}。
        # 只在自动分队判不出时用（小球员个头小 + 筐下遮挡时，检测器可能
        # 整场都认不出筐下的人），绝不自动编一个队别。
        self.basket_teams = dict(basket_teams or {})
        # 「向前找」秒数：判某球是哪一队时，看进球前这么久谁离被进攻篮筐最近。
        # 实测（basketball_match.mp4 + 比分牌真值）1.5s 最准 6/8，而看进球
        # 瞬间只有 4/8 —— 因为得分那一刻筐下多是防守/抢板的人。
        self.basket_lookback_s = float(basket_lookback_s or 0.0)
        # 人工标定的篮筐（scripts/mark_landmarks.py 产出）。
        # 给了它就不再跑篮筐检测器：判据里的 rx/ry 有了真实尺度，
        # 也不会因为检测器在这个机位上不灵而整条路径失效。
        self.manual_hoop = manual_hoop
        # 外部记分牌事件（scripts/read_marked_scoreboard.py 的产出）：
        # 给非标准台标用 —— 模板匹配读不出来时，靠它走比分牌路径
        self.scoreboard_events_path = scoreboard_events
        # 人工标注的进球白名单（来自 scripts/label_baskets.py 的标注文件）。
        # 给了它就**只报告命中白名单的进球** —— 因为实测有些机位上自动判据
        # precision = 0%（nybo_3min 三次判进球全错），这时候唯一诚实的做法是
        # 用人工标注当准绳，而不是继续输出错的比分。
        self.basket_labels = basket_labels
        # 学习式判据（scripts/train_basket_model.py 的产出目录 → BasketScorer）
        self.basket_model = basket_model

    def _require(self):
        try:
            import cv2  # noqa
        except ImportError as e:
            raise RuntimeError(
                "缺少 opencv-python。安装：pip install opencv-python") from e
        if not self.detect_players:
            return
        try:
            import ultralytics  # noqa
        except ImportError as e:
            raise RuntimeError(
                "缺少 ultralytics。安装：pip install ultralytics "
                "（无 GPU 也能跑，只是慢；只要比分的话用 --fast 跳过 YOLO）") from e

    def _scoreboard_only(self, fps, total, width, height, progress=None):
        """Score-only tasks need neither player inference nor court coordinates."""
        if progress: progress(.1, "读取比分牌事件")
        if self.scoreboard_events_path:
            data = json.loads(Path(self.scoreboard_events_path).read_text(encoding="utf-8"))
        else:
            import importlib.util
            script = Path(__file__).resolve().parents[2] / "scripts" / "read_marked_scoreboard.py"
            spec = importlib.util.spec_from_file_location("score_only_ocr", script)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            data = mod.read_scoreboard(self.video_path)
        base = data.get("start") or {}
        if any(base.get(team) is None for team in ("home", "away")):
            raise ValueError("比分牌事件缺少有效起始比分，请重新读取或提供 start")
        events = data.get("events", [])
        if any(e.get("team") not in ("home", "away") or e.get("delta") not in (1,2,3) for e in events):
            raise ValueError("比分牌事件包含非法队别或增量")
        scan_events = [{"kind":"baseline", "t":0., "team":"", "delta":0,
                        "home":int(base["home"]), "away":int(base["away"]), "period":1}]
        scan_events += [dict(e,kind="score") for e in events]
        scan = _ManualScoreScan(scan_events, base)
        # 覆盖率与基线位移要从这份事件文件里带过来：以前这里用替身对象的默认值
        # （frames_hit=1），报告便写成"读数覆盖 1/1"，而实际是 379/480 —— 数字
        # 看着不可信（实测踩到）。基线位移同理：某队开头读不出来时基线会被推迟，
        # 那之前的得分不在事件表里，必须让用户看见。
        scan.frames_hit = int(data.get("ocr_hit") or scan.frames_hit)
        scan.frames_read = int(data.get("n_crops") or data.get("ocr_hit") or scan.frames_hit)
        scan.baseline_from = data.get("baseline_from", "")
        scan.baseline_at = data.get("baseline_at") or {}
        scan.baseline_note = data.get("baseline_note", "")
        rt = RawTrack(fps=fps,duration=total/fps,video_path=self.video_path,width=width,height=height)
        rt.base_score = dict(base)
        rt.scoreboard_events = events
        from .scoreboard import score_points_to_attempts
        rt.attempts = [Attempt(**a) for a in score_points_to_attempts(scan, fps=fps)]
        rt.detections_meta.update(source="video",score_policy="scoreboard",
            calibration_valid=False,calibration_method="unavailable",calibration_rmse_m=None,
            calibration_degeneracy={"degenerate":True,"reason":"仅比分分析，无球场坐标"},
            scoreboard=scan.to_dict(), final_score=dict(zip(("home","away"),scan.final)))
        if progress: progress(1., "比分事件读取完成")
        return rt

    def _resolve_device(self):
        """把 `self.device` 校验并**落实**成这台机器上真能用的设备，返回说明。

        为什么必须有这一层：任务默认 `device="0"`（第一块 GPU），而**没装 CUDA 版
        torch 的机器**上 ultralytics 会直接抛
        `ValueError: Invalid CUDA 'device=0' requested ... torch.cuda.device_count(): 0`
        —— 任务 0 秒就失败，用户只看到一大段 CUDA 报错，根本不知道"这台机器没有 GPU"
        （实测踩到：本机装的是 torch 2.14.1+cpu）。

        规则：留空 = 自动（有 CUDA 用 GPU，否则 CPU）；显式要 GPU 但不可用 = 退回 CPU
        并给出原因（不静默改变行为）。返回值是给 meta 说明用的 note。
        """
        want = str(self.device or "").strip()

        def _cuda_n():
            try:
                import torch
                return int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
            except Exception:  # noqa: BLE001  torch 没装/导入失败
                return 0

        if not want or want.lower() == "cpu":
            self.device = "0" if _cuda_n() > 0 else "cpu"
            return ""
        low = want.lower()
        wants_gpu = low.startswith("cuda") or all(
            p.strip().isdigit() for p in want.split(",") if p.strip())
        if not wants_gpu:
            return ""                   # 其它设备串（如 mps）交给 ultralytics 自己解释
        n = _cuda_n()
        if n > 0:
            return ""
        self.device = "cpu"
        return ("请求的设备是 %r（GPU），但这台机器上 torch 看不到 CUDA"
                "（torch.cuda.device_count()=0，装的应该是 CPU 版 torch）——"
                "已自动改用 CPU 跑。想用 GPU：按 https://pytorch.org 的指引装"
                "对应 CUDA 版本的 torch。" % want)

    def run(self, progress=None) -> RawTrack:
        self._require()
        import cv2
        from .ball import (BallConfig, _link_tracks, _color_candidates,
                           BallCandidate, build_background, rank_ball_tracks)

        cfg_ball = self.ball_cfg or BallConfig()
        cfg_ball.weights = self.weights
        cfg_ball.imgsz = self.imgsz
        cfg_ball.device = self.device
        cfg_ball.stride = 1
        self._ball_cfg = cfg_ball

        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            raise RuntimeError(f"打不开视频：{self.video_path}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not self.detect_players and self.score_policy == "scoreboard":
            cap.release()
            return self._scoreboard_only(fps, total, W, H, progress)
        model = None
        # 设备兜底：默认的 "0"（第一块 GPU）在**没装 CUDA 版 torch 的机器上**会让
        # ultralytics 直接抛 `ValueError: Invalid CUDA 'device=0' requested`
        # —— 任务 0 秒就失败（实测踩到：本机装的是 torch 2.14.1+cpu）。
        # 必须在**加载模型之前**把 self.device 落实成可用值。
        device_note = self._resolve_device()
        if device_note:
            print("[设备] " + device_note, flush=True)
        if self.detect_players:
            from ultralytics import YOLO
            model = YOLO(self.weights)
        ball_model = None
        if self.ball_weights:
            from ultralytics import YOLO
            ball_model = YOLO(self.ball_weights)

        rt = RawTrack(fps=fps, duration=total / fps if fps else 0.0,
                      video_path=self.video_path, width=W, height=H)
        rt.detections_meta.update({"source": "video", "weights": self.weights,
                                   "reproj_error_m": self.cal.reproj_error_m,
                                   "score_policy": self.score_policy,
                                   "device": self.device or "cpu"})
        if device_note:
            rt.detections_meta["device_note"] = device_note
        # 标定是否适用于这段视频 —— 标定文件是按机位存的，分辨率对不上就不能用
        rt.detections_meta["calibration_valid"] = self._calibration_matches(W, H)
        # 记下来源与校验状态：手工点位只有在未被标记为 position_unverified 时
        # 才能用于热区/战术图的宽松展示路径。
        if self.cal is not None:
            rt.detections_meta["calibration_method"] = getattr(
                self.cal, "method", "") or ""
            # 柔性吻合度门槛失败时仍可在 UI 中预览人工标定；点数不足或独立校验
            # 未通过的标定由 position_unverified 单独阻止位置类结论。
            _m = str(rt.detections_meta["calibration_method"]).lower()
            rt.detections_meta["calibration_is_manual"] = (
                _m.startswith(("web-keypoints", "web_keypoints", "manual",
                               "auto-identify"))
                or "keypoints" in _m or "manual" in _m)
            unverified = bool(getattr(self.cal, "position_unverified", False))
            rt.detections_meta["calibration_position_unverified"] = unverified
            if unverified:
                why = ("这份球场标定点数不足或未通过独立校验；请补标 6 个以上的命名地标，"
                       "再核对投影球场线")
                rt.detections_meta["calibration_valid"] = False
                rt.detections_meta["calibration_rejected"] = why
            # 注意：**不能用 `or 99.0`** —— 标定误差正好是 0.0 时会被当成假值，
            # 于是"0 米误差的完美标定"被记成 99 米，热区/战术图又被关掉
            # （实测踩到：标定 0.00m、门禁却显示 calibration_rmse_m=99.0）。
            _rmse = getattr(self.cal, "reproj_error_m", None)
            rt.detections_meta["calibration_rmse_m"] = (
                float(_rmse) if _rmse is not None else 99.0)
            # 退化（近共线/重合）标定必须在这里就挡掉：它的**重投影误差照样是
            # 0.00m**，只靠误差看不出问题（实测：4 个点几乎共线 → 篮筐被投到
            # 33m 外，界面却显示"标定可用 1.45m"）。判据与读数写进 meta，
            # 界面/报告据此说清"为什么不能用"。
            try:
                from .court import calibration_degeneracy
                deg = calibration_degeneracy(self.cal)
                rt.detections_meta["calibration_degeneracy"] = deg
                if deg.get("degenerate"):
                    rt.detections_meta["calibration_valid"] = False
                    rt.detections_meta["calibration_rejected"] = deg.get("reason", "")
            except Exception as e:  # noqa: BLE001  校验失败不该拖垮主流程
                rt.detections_meta["calibration_degeneracy_error"] = (
                    f"{type(e).__name__}: {e}")

        # ---- 独立校验：这份标定解释得了画面里那个（手标的）真篮筐吗？----
        # 为什么放在这里而不是只在"篮下判进球"那条路径里：有比分牌的转播走的是
        # 比分牌路径、根本不跑 hoopsight，于是这道校验被整条绕过 ——
        # 实测 basketball_match 那份标定把画面里的篮筐投到了 87m 外，
        # 却照样能出热区/战术图。用户手标过篮筐时，这是**与特征点无关的真值**，
        # 一票否决。
        try:
            mh = getattr(self, "manual_hoop", None)
            if mh is not None and self.cal is not None and getattr(self.cal, "H", None):
                from .baskets import calibration_sane_for_scoring
                ok_h, why_h = calibration_sane_for_scoring(
                    self.cal, (float(mh.cx), float(mh.cy), float(mh.rx)))
                rt.detections_meta["calibration_hoop_check"] = {
                    "ok": bool(ok_h), "reason": why_h,
                    "hoop_px": [float(mh.cx), float(mh.cy)],
                    "source": "manual_marks"}
                if not ok_h:
                    rt.detections_meta["calibration_for_value"] = {
                        "ok": False, "reason": why_h}
                    rt.detections_meta["calibration_valid"] = False
                    rt.detections_meta["calibration_rejected"] = why_h
        except Exception as e:  # noqa: BLE001
            rt.detections_meta["calibration_hoop_check_error"] = (
                f"{type(e).__name__}: {e}")
        # ---- 套餐 B 前置关卡：机位稳不稳 + 标定准不准 ----
        # 单应标定只对固定机位成立，而且标错了**不会报错**（画出来依然像战术图）。
        # 所以在花几十分钟跑推理之前先把这两件事量出来。
        try:
            from .calibcheck import camera_motion, court_fit_score, FIT_RATIO_BAD
            m = camera_motion(self.video_path)
            rt.detections_meta["camera_motion"] = m
            if m.get("verdict") == "moving":
                rt.detections_meta["camera_warning"] = m.get("note")
            if rt.detections_meta["calibration_valid"]:
                fit = court_fit_score(self.video_path, self.cal)
                rt.detections_meta["calibration_fit"] = fit
                if fit.get("ratio", 0) < FIT_RATIO_BAD:
                    # 标定与画面基本对不上 -> 宁可不出坐标，也不出错的坐标
                    rt.detections_meta["calibration_valid"] = False
                    rt.detections_meta["calibration_rejected"] = (
                        f"标定与画面的吻合度过低（ratio={fit.get('ratio')}）："
                        + str(fit.get("note", "")))
        except Exception as e:  # noqa: BLE001  校验失败不该拖垮主流程
            rt.detections_meta["calcheck_error"] = f"{type(e).__name__}: {e}"
        # 中值背景：固定机位下用来压掉静止橙色物体；移动镜头下当作额外线索。
        bg = None
        if getattr(cfg_ball, "use_bg", True) and total > 3:
            try:
                bg = build_background(self.video_path,
                                      n=getattr(cfg_ball, "bg_samples", 40))
            except Exception:
                bg = None
        bg_gray = None
        if bg is not None:
            try:
                if bg.shape[:2] == (H, W):
                    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
            except Exception:
                bg_gray = None

        def step(p: float, msg: str):
            if progress:
                progress(p, msg)

        legacy = None
        legacy_persons = []
        if self.shot_engine == "legacy":
            from .legacy_shots.stream import LegacyShotStream
            from .legacy_shots.detector import Det as LegacyDet
            hint = self.hoop_hint
            if hint is None and self.manual_hoop is not None:
                hint = (self.manual_hoop.cx, self.manual_hoop.cy)
            legacy = LegacyShotStream(fps, W, manual_hoop=self.manual_hoop, hint=hint,
                config={"detect": {"center_lock_widths": 3. if self.legacy_center_lock else 0.}})
            rt.detections_meta["shot_engine"] = "legacy"
            if ball_model is None:
                rt.detections_meta["visual_error"] = "旧版投篮引擎需要球/筐联合检测权重，当前没有可用权重"

        # ---- 1) 逐帧检测：球员框（含球衣颜色）+ 球候选 ----
        step(0.02, "检测球员与篮球")
        # track_id -> {"n": 帧数, "bins": {粗bin: 次数}}（众数色，抗混色）
        jersey_sum: dict[str, dict] = {}
        player_pos: dict[str, list] = {}          # track_id -> [(t, cx, cy)]
        player_boxes: dict[str, list] = {}        # track_id -> [(t, x1, y1, x2, y2)]
        ball_cands: list = []
        prev_gray = None
        idx = 0
        # 场景切镜检测：帧间差异突然变大且直方图相关性骤降，认为发生切镜。
        # 切镜点会用于篮筐/球轨迹的分段，避免把两个镜头里的东西连成一条轨迹。
        cuts: list[float] = []
        prev_hist = None
        while True:
            ok = cap.grab()
            if not ok:
                break
            # 球追踪必须逐帧：球在空中只有 1 秒左右，抽帧会漏掉整条投篮弧线。
            # 球员检测的抽帧由 player_stride 控制，不作用在球上。
            ok, frame = cap.retrieve()
            if not ok:
                break
            t = idx / fps
            legacy_cut = False
            legacy_detections = []
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            motion = cv2.absdiff(gray, prev_gray) \
                if prev_gray is not None and prev_gray.shape == gray.shape else None
            prev_gray = gray
            bg_motion = None
            if bg_gray is not None and bg_gray.shape == gray.shape:
                bg_motion = cv2.absdiff(gray, bg_gray)
            motion_for_candidates = motion
            if motion_for_candidates is None:
                motion_for_candidates = bg_motion
            elif bg_motion is not None:
                try:
                    motion_for_candidates = cv2.max(motion, bg_motion)
                except Exception:
                    motion_for_candidates = motion

            # 场景切镜：帧间差 + HSV 直方图相关性。
            try:
                hist = cv2.calcHist([hsv], [0], None, [180], [0, 180])
                cv2.normalize(hist, hist)
            except Exception:
                hist = None
            if prev_hist is not None and hist is not None and motion is not None:
                corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
                mad = float(motion.mean())
                if corr < 0.55 and mad > 25.0:
                    cuts.append(round(t, 3))
                    legacy_cut = True
            prev_hist = hist

            res = None
            # 球员检测可以抽帧（YOLO 是 CPU 上唯一的瓶颈），但球追踪不能抽帧。
            run_players = model is not None and (
                self.player_stride <= 1 or idx % self.player_stride == 0)
            if run_players:
                legacy_persons = []
                # BoT-SORT（带 ReID）比默认的 ByteTrack 抗遮挡得多 ——
                # 广播/手持画面里球员互相遮挡是常态，ByteTrack 一遮就换 ID，
                # 实测 15 秒裂出 27 条轨迹。跟踪不稳，后面阵型/传球全乱。
                res = model.track(frame, persist=True, conf=self.conf,
                                  imgsz=self.imgsz, device=self.device,
                                  tracker=self.tracker, verbose=False)[0]
            # ---- 球：优先用专用球检测器 ----
            if ball_model is not None:
                predict_options = dict(conf=self.conf, imgsz=self.imgsz,
                                       device=self.device, verbose=False)
                if legacy is not None:
                    predict_options.update(conf=.25, imgsz=640, iou=.5)
                rb = ball_model.predict(frame, **predict_options)[0]
                if rb.boxes is not None:
                    for b in rb.boxes:
                        nm = str(rb.names.get(int(b.cls[0]), "")).lower()
                        if legacy is not None:
                            legacy_detections.append(LegacyDet(nm, float(b.conf[0]),
                                tuple(float(v) for v in b.xyxy[0])))
                        if "ball" in nm:
                            bx = [float(v) for v in b.xyxy[0]]
                            bw, bh = bx[2] - bx[0], bx[3] - bx[1]
                            # 球框应该近似方形、且尺寸在合理范围（远景 8~60px）。
                            # 太扁/太大的多半是误检（球衣、灯箱、观众席）。
                            if bw < 4 or bh < 4 or bw > 120 or bh > 120:
                                continue
                            if max(bw, bh) > min(bw, bh) * 2.2:
                                continue
                            ball_cands.append(BallCandidate(
                                t=t, x=(bx[0] + bx[2]) / 2,
                                y=(bx[1] + bx[3]) / 2,
                                score=min(1.0, float(b.conf[0]) + 0.5),
                                source="ball_model"))
            if res is not None and res.boxes is not None and res.boxes.id is not None:
                for b in res.boxes:
                    name = res.names.get(int(b.cls[0]), "")
                    xyxy = [float(v) for v in b.xyxy[0]]
                    cx = (xyxy[0] + xyxy[2]) / 2
                    tid = int(b.id[0]) if b.id is not None else -1
                    if name in ("sports ball", "ball"):
                        ball_cands.append(BallCandidate(
                            t=t, x=cx, y=(xyxy[1] + xyxy[3]) / 2,
                            score=min(1.0, float(b.conf[0]) + 0.3), source="yolo"))
                    elif name == "person":
                        # ---- 球员框过滤：广播/手持画面里"人"远多于球员 ----
                        # 实测一帧能检出 23~26 个 person，其中一大半是
                        # 场边观众、工作人员、只露半个身子的路人。
                        bw, bh = xyxy[2] - xyxy[0], xyxy[3] - xyxy[1]
                        if bh < 28 or (bw > 0 and bh < bw * 0.9):
                            continue          # 太小 / 太扁 —— 不是站着的人
                        if xyxy[3] >= H - 2:
                            continue          # 贴着画面底边 —— 镜头前的观众
                        if float(b.conf[0]) < self.player_conf:
                            continue          # 人用更高的置信度门槛
                        pid = f"T{tid}"
                        if legacy is not None:
                            legacy_persons.append(LegacyDet("person",float(b.conf[0]),tuple(xyxy),pid))
                        if pid not in rt.players:
                            rt.players[pid] = Player(pid, pid, "home")
                        player_pos.setdefault(pid, []).append(
                            (t, cx, (xyxy[1] + xyxy[3]) / 2))
                        player_boxes.setdefault(pid, []).append(
                            (t, xyxy[0], xyxy[1], xyxy[2], xyxy[3],
                             float(b.conf[0])))
                        # 球衣颜色：取框的上半身中间那块
                        y0 = int(max(0, xyxy[1] + (xyxy[3] - xyxy[1]) * 0.18))
                        y1 = int(min(H, xyxy[1] + (xyxy[3] - xyxy[1]) * 0.55))
                        x0 = int(max(0, xyxy[0] + (xyxy[2] - xyxy[0]) * 0.28))
                        x1 = int(min(W, xyxy[2] - (xyxy[2] - xyxy[0]) * 0.28))
                        if y1 > y0 + 2 and x1 > x0 + 2:
                            import numpy as _np
                            patch = hsv[y0:y1, x0:x1].reshape(-1, 3).astype(_np.int32)
                            Hh, Ss, Vv = patch[:, 0], patch[:, 1], patch[:, 2]
                            # 剔除肤色：OpenCV 色相里皮肤落在 H≈0~25（红黄之间）
                            # 且饱和度中等、明度不低。不剔的话，取样窗口里的
                            # 头和手臂会把球衣色"染"成肤色 —— 实测两队色相
                            # 全散在 15~93，根本聚不出两簇。
                            # 肤色判定要**收窄**：红色球衣的色相也在 H≈0~10，
                            # 用 H<=25 & S 40~180 会把整队红球衣当皮肤剔掉
                            # （实测把红衣整队判成"没有颜色"）。肤色的饱和度
                            # 通常低于纯色球衣，所以卡在 S<=110 且 V>=70。
                            skin = (Hh <= 18) & (Ss >= 25) & (Ss <= 110) & (Vv >= 70)
                            # 剔除过暗/过曝（阴影、地板反光）
                            keep = (~skin) & (Vv >= 25) & (Vv <= 245)
                            if keep.sum() >= 6:
                                sel = patch[keep]
                                # 众数色：粗分箱（色相 8 度一格、饱和度 32 一格）
                                # 取出现最多的那个 bin —— 比均值稳得多，
                                # 因为窗口边角总混进背景，均值会被拖走。
                                bins = {}
                                for h, ss, vv in sel[::max(1, len(sel) // 400)]:
                                    key = (int(h) // 8, int(ss) // 32, int(vv) // 64)
                                    bins[key] = bins.get(key, 0) + 1
                                for b, c in bins.items():
                                    acc = jersey_sum.setdefault(pid, {"n": 0, "bins": {}})
                                    acc["bins"][b] = acc["bins"].get(b, 0) + c
                                jersey_sum.setdefault(pid, {"n": 0, "bins": {}})["n"] += 1

            if legacy is not None:
                legacy.update(idx, t, legacy_detections, legacy_persons, cut=legacy_cut)

            # 颜色线索（YOLO 漏掉的球靠它兜底）；背景运动量已经在上面的 bg_motion 里融合。
            # 但**有专用球检测器时直接跳过** —— 广播画面下这条线索是纯噪声
            # （实测每帧 ~70 个橙色候选，球衣/地板/皮肤全中）。
            color_cands = []
            if ball_model is None:
                color_cands = _color_candidates(frame, hsv, cfg_ball,
                                                motion_for_candidates, bg_motion)
            for (bx, by, r) in color_cands:
                sc = 0.35
                if r <= 12:
                    sc += 0.15
                if motion_for_candidates is not None:
                    yy = int(max(0, min(H - 1, by)))
                    xx = int(max(0, min(W - 1, bx)))
                    m = float(motion_for_candidates[max(0, yy - 3):yy + 4,
                                                    max(0, xx - 3):xx + 4].mean())
                    sc += 0.35 if m >= cfg_ball.motion_min else -0.15
                if sc > 0.2:
                    ball_cands.append(BallCandidate(t=t, x=bx, y=by, score=sc,
                                                    source="color"))
            if total and idx % 30 == 0:
                step(0.02 + 0.58 * idx / total, "检测球员与篮球")
            idx += 1
        cap.release()
        self._cuts = cuts
        self._legacy_events = legacy.finish(idx, idx / fps) if legacy is not None else []
        if legacy is not None:
            import hashlib
            meta = legacy.to_dict()
            meta["model_path"] = self.ball_weights
            if self.ball_weights and Path(self.ball_weights).is_file():
                with open(self.ball_weights, "rb") as model_file:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: model_file.read(1024*1024), b""):
                        digest.update(chunk)
                meta["model_sha256"] = digest.hexdigest()
            rt.detections_meta["legacy_shots"] = meta
            if not legacy.rim_seen:
                rt.detections_meta["visual_error"] = "没有检测到可用篮筐，无法确认投篮结果"
            elif not legacy.real_seen:
                rt.detections_meta["visual_error"] = "没有有效的真实篮球观测，无法确认投篮结果"

        # ---- 2) 球轨迹连接 ----
        step(0.56, "连接球轨迹")
        from .ball import stitch_tracks
        tracks = _link_tracks(ball_cands, cfg_ball, cuts=self._cuts)
        # 先按运动连续性把断开的轨迹接起来 —— 进球判据要求同一条轨迹里既有
        # 上升段又有穿越篮筐平面的下降段，断成两截就永远判不出进球。
        tracks = stitch_tracks(tracks, cfg_ball, cuts=self._cuts)
        tracks.sort(key=lambda tr: -len(tr))
        # 像素空间的轨迹先**全部**留给视觉路径；等篮筐识别出来以后，
        # 再按「是否靠近篮筐 / 是否有穿筐趋势」重新排序取 top-K。
        # 球的投篮弧线往往不是最长的那条，不能在这一步用长度一刀切掉。
        self._tracks_px = tracks
        self._player_pos = player_pos
        self._player_boxes = player_boxes
        if self.ball_weights:
            # 【专用球检测器路径：**旁路轨迹连接**】
            # 轨迹连接是为「颜色线索时代」设计的 —— 那时每帧几十个橙色噪声，
            # 只能靠"运动连续性"把球从噪声里筛出来。现在检测器已经可靠
            # （~1 个/帧），再做连接就是在**制造错误**：实测 120 个干净检测点
            # 被连成 2 条横跨整段的长轨迹，控球判定因此恒为 0（传球网络全空）。
            #
            # 关键认识：detect_possessions 只需要回答"每个时刻球在哪、离谁最近"，
            # 它**根本不需要轨迹**。所以这里检测点直接进球轨迹；
            # 轨迹连接仍然保留给上面那条「进球判定」的视觉路径（那里要完整弧线）。
            kept = sorted(ball_cands, key=lambda c: c.t)
        else:
            loc_tracks = rank_ball_tracks(tracks, cfg_ball)[:3]
            kept = sorted([c for tr in loc_tracks for c in tr], key=lambda c: c.t)
        # ⚠️ 球必须和球员用**同一套坐标**。球员走的是逐帧滑动标定，
        # 如果球还用静态标定，两者会随镜头移动逐渐错开 ——
        # 表现就是"控球片段恒为 0、传球网络永远为空"，而且不报任何错。
        # （实测：15 秒片段里球员用滑动 H、球用静态 H，球有 110 个点却一个球权都判不出来。）
        for c in kept:
            if self.sliding and self.sliding.get("anchors"):
                from .calibcheck import homography_at
                # ⚠️ 变量名**必须**区别于 H（画面高度）。这里以前写成 `H = 单应矩阵`，
                # 把 run() 里的画面高度覆盖掉了 —— 于是后面 `_load_bug(..., W, H, ...)`
                # 传进去的是一个 3x3 矩阵，缓存指纹算不出来：
                # `TypeError: int() argument must be ... not 'list'`，
                # 表现为"比分牌模板匹配整条路静默失效"。
                Hf = homography_at(self.sliding, int(round(c.t * fps)),
                                   bool(self.sliding.get("half_court", True)))
                if Hf is None:
                    continue
                xm, ym = apply_homography(Hf, c.x, c.y)
                xm, ym = fold_to_analysis(xm, ym, "half")
            elif rt.detections_meta.get("calibration_valid"):
                # 多机位时按**这一帧所属镜头**取标定（单机位就是 self.cal）
                _c = self._cal_at(c.t)
                if _c is None or not _c.H:
                    continue
                xm, ym = _c.to_court(c.x, c.y)
            else:
                continue
            rt.ball_track.append(BallSample(t=round(c.t, 3), x=round(xm, 3),
                                            y=round(ym, 3), z=1.2, conf=c.score))
        rt.detections_meta["ball"] = {
            "candidates": len(ball_cands), "tracks": len(tracks),
            "tracks_ranked": min(len(tracks), cfg_ball.keep_top_tracks),
            "kept_points": len(rt.ball_track),
            "coverage": round(len(rt.ball_track) / max(1, total), 4)}

        # ---- 3) 球员分队 ----
        step(0.60, "按球衣颜色分主客队")
        self._assign_teams(rt, jersey_sum)

        # ---- 3a) 静态标定不可用 -> 自动逐帧滑动标定 ----
        if (self.sliding is None and self.auto_sliding
                and not rt.detections_meta.get("calibration_valid")
                and self._player_boxes):
            step(0.63, "球场未标定：启用逐帧滑动标定（自动）")
            try:
                from .calibcheck import sliding_calibration
                self.sliding = sliding_calibration(
                    self.video_path, self.cal, half_court=self.half_court,
                    stride=self.sliding_stride,
                    max_seconds=self.sliding_max_seconds,
                    progress=lambda p, m="": step(0.63 + 0.06 * p, m))
                # ⚠️ 这里刻意**不存全部 anchors**（上千个，塞进 detections_meta 太肥），
                # 但必须留**几个样本**：否则事后就没法验证"这套 H 投出来合不合理"——
                # 实测就是因为锚点被剔了，pipeline 里那道合理性检查拿不到 H，
                # 只能干看着坏标定往下走（球员坐标全被压到边线上）。
                _anch = self.sliding.get("anchors") or []
                _sample = ([_anch[0]] if _anch else []) + \
                          ([_anch[len(_anch) // 2]] if len(_anch) > 2 else []) + \
                          ([_anch[-1]] if len(_anch) > 1 else [])
                rt.detections_meta["sliding_calibration"] = {
                    k: v for k, v in self.sliding.items() if k != "anchors"}
                # 样本只保留判合理性需要的字段（帧号 + 四角 + 分数）
                rt.detections_meta["sliding_calibration"]["anchors"] = [
                    {kk: a.get(kk) for kk in ("frame", "corners", "ratio", "kind")
                     if kk in a} for a in _sample]
                rt.detections_meta["sliding_calibration"]["anchors_truncated"] = (
                    len(_anch) > len(_sample))
                rt.detections_meta["sliding_anchors"] = len(_anch)
            except Exception as e:  # noqa: BLE001
                rt.detections_meta["sliding_error"] = f"{type(e).__name__}: {e}"

        # ---- 3b) 套餐 B：把球员像素轨迹投影成球场坐标 ----
        # 只有标定确实适用于这段视频时才投影（标定按机位存，对不上就拒绝），
        # 否则宁可不产出轨迹 —— 错位的俯视战术图比没有更糟。
        # 走自动逐帧标定时，先拿**人工标的篮筐**当独立真值校验一次：
        # 实测这段素材的逐帧标定把篮筐投到 9.2m 外（而它自己的质量分 ratio
        # 反而高达 7.17），坐标整段不可用 —— 不过这道校验就绝不能出战术图。
        if self.sliding and getattr(self, "manual_hoop", None) is not None:
            try:
                from .calibcheck import validate_sliding_with_hoop
                mh = self.manual_hoop
                val = validate_sliding_with_hoop(
                    self.sliding, (mh.cx, mh.cy), t=float(getattr(mh, "t", 0.0)))
                rt.detections_meta["sliding_validation"] = val
                if val.get("checked") and not val.get("ok"):
                    # 校验没过 → 丢掉这套滑动标定，球员坐标一律不产出
                    # （留着它只会得到一张看着专业、实际偏 9 米的战术图）
                    self.sliding = None
            except Exception as e:  # noqa: BLE001
                rt.detections_meta["sliding_validation_error"] = \
                    f"{type(e).__name__}: {e}"

        if rt.detections_meta.get("calibration_valid") or self.sliding:
            rt.detections_meta["player_track_source"] = (
                "sliding_calibration" if self.sliding else "static_calibration")
            # ⚠️ 投影的必须是**脚底**（检测框底边中点），不是框中心。
            # 单应矩阵是把"地面上的点"映到球场坐标；用框中心相当于把球员
            # 往画面深处平移了半个人高 —— 俯视战术图上每个人都会前移 1~2m，
            # 站位、阵型、间距全部跟着偏（近处偏差更大，因为透视放大）。
            # 广播机位下球员框底边≈脚，这是单目定位里最省事也最有效的近似。
            use_sliding = bool(self.sliding and self.sliding.get("anchors"))
            # 逐帧人数上限：同一时刻场内最多 max_players 个人。
            # 按置信度保留最可信的那些 —— 宁可少画一个，也别把观众画进球场。
            dropped_by_cap = 0
            if self.max_players > 0:
                per_frame = {}
                for pid, boxes in (self._player_boxes or {}).items():
                    for row in boxes:
                        conf = row[5] if len(row) > 5 else 1.0
                        per_frame.setdefault(round(row[0], 3), []).append(
                            (conf, pid, row))
                keep = set()
                for _t, lst in per_frame.items():
                    lst.sort(key=lambda x: -x[0])
                    for conf, pid, row in lst[:self.max_players]:
                        keep.add((round(row[0], 3), pid))
                    dropped_by_cap += max(0, len(lst) - self.max_players)
                self._keep_boxes = keep
            else:
                self._keep_boxes = None
            rt.detections_meta["player_cap_dropped"] = dropped_by_cap
            for pid, boxes in (self._player_boxes or {}).items():
                team = rt.players[pid].team if pid in rt.players else "home"
                for row in boxes:
                    if self._keep_boxes is not None and                             (round(row[0], 3), pid) not in self._keep_boxes:
                        continue
                    tt, x1, y1, x2, y2 = row[:5]
                    foot_x = (x1 + x2) / 2.0
                    foot_y = y2
                    # 像素坐标下的球员观测（给「进球是哪一队」用，见 player_obs）
                    rt.player_obs.append(dict(
                        t=round(float(tt), 3), player_id=pid, team=team,
                        foot_x=round(float(foot_x), 1),
                        foot_y=round(float(foot_y), 1),
                        cx=round(float((x1 + x2) / 2.0), 1),
                        cy=round(float((y1 + y2) / 2.0), 1),
                        box=[round(float(v), 1) for v in (x1, y1, x2, y2)],
                        conf=round(float(row[5]) if len(row) > 5 else 1.0, 3)))
                    if use_sliding:
                        # 逐帧 H（锚点间插值）——跟住会动的镜头
                        from .calibcheck import homography_at
                        # 同样：不能叫 H（会覆盖 run() 里的画面高度，见上面那段注释）
                        Hf = homography_at(self.sliding, int(round(tt * fps)),
                                           bool(self.sliding.get("half_court", True)))
                        if Hf is None:
                            continue
                        px, py = apply_homography(Hf, foot_x, foot_y)
                        # sliding 的目标点本身就是折半坐标（half_court=True 时）
                        px, py = fold_to_analysis(px, py, "half")
                    else:
                        # 多机位时按**这一帧所属镜头**取标定
                        _c = self._cal_at(tt)
                        if _c is None or not _c.H:
                            continue
                        px, py = _c.to_court(foot_x, foot_y)
                    # 只保留**真正落在场内**的点。这一刀很关键：广播镜头里
                    # 同时会检出替补席、裁判、观众（实测 30 秒里 83 条轨迹），
                    # 它们站在边线外/中线外/底线后方，投影出来正好贴着边线或
                    # 中圈，会变成战术图上一堆不动的点，把阵型与空间指标全带偏。
                    #
                    # 范围必须取"这份标定实际覆盖的球场"，不能写死 |y|<=14：
                    # 半场标定只覆盖 y∈[-14,0]（或 [0,14]），用对称范围会把
                    # 远底线后方的观众当成场内球员收进来。
                    _cc = self._cal_at(tt) or self.cal
                    if not _cc.in_court(px, py, tol=0.12):
                        continue
                    rt.player_track.append(PlayerSample(
                        t=round(float(tt), 3), player_id=pid, team=team,
                        x=round(px, 3), y=round(py, 3), conf=1.0))
            rt.player_track.sort(key=lambda s: s.t)
            # 3c) 轨迹补接：ByteTrack 一被遮挡就换 ID，30 秒能裂出几十条，
            #     传球网络会因此碎成一地。这里按「时间不重叠 + 位置接得上」
            #     把碎片接回同一个人（经典 tracklet stitching）。
            rt.player_track, stitched = stitch_player_tracks(rt.player_track)
            rt.detections_meta["player_stitch"] = stitched
        rt.detections_meta["player_track"] = {
            "samples": len(rt.player_track),
            "players": len({s.player_id for s in rt.player_track}),
            "calibration_valid": bool(rt.detections_meta.get("calibration_valid")),
        }

        # ---- 3c) 采一次球衣主色 ----
        # **必须放在这里**：`player_obs` 是上面那段投影循环里填的。
        # 放在 `_assign_teams` 之后、这段之前会一个观测都拿不到
        # （实测就这样静默失败过：meta 里连键都没有，报告直接不显示颜色对照）。
        try:
            self._sample_jersey_colors(rt)
        except Exception as e:  # noqa: BLE001
            rt.detections_meta["jersey_color_error"] = f"{type(e).__name__}: {e}"

        # ---- 4) 比分牌（有覆盖层时最硬的一路证据）----
        sb_ok = False
        if self.scoreboard:
            step(0.64, "读取广播比分牌")
            from .scoreboard import (
                ScoreBug, ScoreBugConfig, ScoreboardError, scan_scoreboard,
                score_points_to_attempts,
            )
            cfg_sb = self.sb_cfg or ScoreBugConfig()
            scan = None
            sb_source = ""

            # 3.0 外部事件优先（手动框选 + OCR 的产出）——非标准台标唯一可用的路
            if self.scoreboard_events_path:
                try:
                    _d = json.loads(Path(self.scoreboard_events_path)
                                    .read_text(encoding="utf-8"))
                    _ev = [{"kind": "baseline", "t": 0.0, "team": "", "delta": 0,
                            "home": int((_d.get("start") or {}).get("home", 0)),
                            "away": int((_d.get("start") or {}).get("away", 0)),
                            "period": 1}]
                    _ev += [{"kind": "score", "t": float(e["t"]),
                             "team": e["team"], "delta": int(e["delta"])}
                            for e in _d.get("events", [])]
                    scan = _ManualScoreScan(_ev, _d.get("start"), "manual-ocr")
                    # 覆盖率/基线位移都要从**这份事件文件**里带过来：
                    # 以前这里只造一个替身对象、frames_hit 恒为 1，报告便写成
                    # "读数覆盖 1/1"，而实际是 379/480 —— 反而让人不信（实测踩到）。
                    scan.frames_hit = int(_d.get("ocr_hit") or scan.frames_hit)
                    scan.frames_read = int(_d.get("n_crops") or _d.get("ocr_hit")
                                           or scan.frames_hit)
                    scan.baseline_from = _d.get("baseline_from", "")
                    scan.baseline_at = _d.get("baseline_at") or {}
                    scan.baseline_note = _d.get("baseline_note", "")
                    sb_source = "manual-ocr"
                    rt.detections_meta["scoreboard_manual"] = {
                        "path": str(self.scoreboard_events_path),
                        "n_events": len(_d.get("events", [])),
                        "start": _d.get("start"),
                        "ocr_hit": scan.frames_hit, "n_crops": scan.frames_read,
                        "baseline_at": scan.baseline_at,
                        "baseline_note": scan.baseline_note}
                    print("[info] 用外部记分牌事件：%d 条（起始 %s:%s）"
                          % (len(_d.get("events", [])),
                             (_d.get("start") or {}).get("home"),
                             (_d.get("start") or {}).get("away")))
                except Exception as _e:  # noqa: BLE001
                    rt.detections_meta["scoreboard_manual_error"] = \
                        f"{type(_e).__name__}: {_e}"

            # 4.1 先试 Windows OCR：能处理模板匹配不认识的其他台标样式
            # 注意：外部事件（manual-ocr）已经给了 scan 时必须**跳过检测** ——
            # 否则这里的 `scan = read_scoreboard_ocr(...)` 会把外部结果覆盖成 None
            # （实测踩到：加了 --scoreboard-events 却完全没生效，事件数还是 0）。
            try:
                if sb_source != "manual-ocr":
                    import importlib.util
                    script = Path(__file__).resolve().parents[2] / "scripts" / "read_marked_scoreboard.py"
                    spec = importlib.util.spec_from_file_location("video_marked_sb", script)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    data = mod.read_scoreboard(self.video_path)
                    baseline = data["start"]
                    ev = [{"kind":"baseline", "t":0., "team":"", "delta":0,
                           "home":baseline["home"], "away":baseline["away"], "period":1}]
                    ev += [dict(e, kind="score") for e in data["events"]]
                    scan = _ManualScoreScan(ev, baseline, "ocr")
                    scan.frames_hit = data["ocr_hit"]
                    # 覆盖率要如实记：以前 frames_read 恒等于 frames_hit，
                    # 报告写成"读到 1 帧"却给出 38:38，反而不可信（实测踩到）。
                    scan.frames_read = int(data.get("n_crops") or data["ocr_hit"] or 1)
                    scan.baseline_from = data.get("baseline_from", "")
                    scan.baseline_at = data.get("baseline_at") or {}
                    scan.baseline_note = data.get("baseline_note", "")
                    sb_source = "ocr"
                    rt.detections_meta["scoreboard_bar"] = {
                        "box":data["box"], "method":data["method"],
                        "baseline_from":data["baseline_from"],
                        "baseline_at":data.get("baseline_at") or {},
                        "baseline_note":data.get("baseline_note", "")}
            except (Exception, SystemExit) as e:
                rt.detections_meta["ocr_scoreboard_error"] = f"{type(e).__name__}: {e}"
                rt.detections_meta["scoreboard_action"] = "请手动框选比分牌后重试"

            # 4.2 OCR 不适用就退回原来的模板匹配
            if scan is None:
                try:
                    bug = self._load_bug(ScoreBug, W, H, fps, total)
                    if bug is None:
                        # 没有任何可信的比分牌几何 —— 这段视频大概率没有台标。
                        # 不要再硬扫一遍（那只会花几十秒然后报 0 帧），
                        # 直接判「没有比分牌」，让下面的「球 + 篮筐」路径接手。
                        rt.detections_meta["scoreboard_absent"] = True
                        rt.detections_meta["scoreboard_note"] = (
                            "画面里没有找到广播比分牌覆盖层"
                            "（自动定位到的横条是地板/看台色块，不是台标）")
                    else:
                        scan = scan_scoreboard(
                            self.video_path, cfg_sb, bug=bug,
                            progress=lambda p, m: step(0.64 + 0.16 * p, m))
                        sb_source = "template"
                except Exception as e:
                    # 没有比分牌不是致命错误：退回「球 + 篮筐」那条视觉路径。
                    # 连 traceback 一起记下来 —— 老版本只留一句
                    # "TypeError: int() argument ... not 'list'"，看不出是谁抛的，
                    # 排查时白跑好几轮（实测踩到）。记全了下次一眼能定位。
                    import traceback as _tb
                    rt.detections_meta["scoreboard_error"] = \
                        f"{type(e).__name__}: {e}"
                    rt.detections_meta["scoreboard_error_traceback"] = \
                        _tb.format_exc()[-2000:]
                    scan = None

            if scan is not None:
                sb_meta = scan.to_dict()
                sb_meta["source"] = sb_source or "unknown"
                # 逐帧读数对调试很有用，但没必要每帧都塞进 raw_track.json
                sb_meta["readings"] = sb_meta.get("readings", [])[::5]
                sb_meta["readings_note"] = "每 5 个采样帧保留一条"
                rt.detections_meta["scoreboard"] = sb_meta
                if getattr(scan, "team_names", None):
                    rt.detections_meta["team_names"] = dict(scan.team_names)
                events = [e for e in scan.events if e.get("kind") == "score"]
                sb_ok = bool(events)
                rt.scoreboard_events = [
                    {"t": e["t"], "team": e["team"], "delta": e["delta"]}
                    for e in events]
                # 首帧读到的比分 = 开局带入分（视频常从半场中间开始录）
                for e in scan.events:
                    if e.get("kind") == "baseline":
                        if e["home"] or e["away"]:
                            rt.base_score = {"home": int(e["home"]),
                                             "away": int(e["away"])}
                            rt.base_period = int(e.get("period", 1) or 1)
                        break
                # 只有真的读到过比分才记「最终比分」，否则会在战报里显示一个
                # 误导性的 0:0 —— 那看起来像"读到了 0 比 0"，其实什么都没读到。
                if scan.frames_hit > 0:
                    rt.detections_meta["final_score"] = {
                        "home": scan.final[0], "away": scan.final[1]}
                rt.attempts = [Attempt(**a) for a in score_points_to_attempts(
                    scan, hoop_side=self._hoop_sides(), fps=fps)]

        # 默认 court/visual：场上检测到的进球直接计入本片段得分。
        # scoreboard：比分牌可读但没有得分事件时，视觉命中标为未确认、不计入。
        # auto：有比分牌得分事件时用比分牌事件，否则按场上进球计分。
        if self.score_policy in ("court", "visual"):
            self._visual_counts_for_score = True
        elif self.score_policy == "scoreboard":
            self._visual_counts_for_score = not (rt.base_score and not rt.scoreboard_events)
        else:  # auto
            self._visual_counts_for_score = True

        # ---- 5) 视觉路径：认篮筐 + 认球 + 判断穿筐 ----
        # 只在比分牌那条路拿不到任何得分事件时才启用。
        # 大量真实素材（手机拍的野球场、单机位训练视频）根本没有比分牌覆盖层，
        # 这时候唯一的证据就是「球有没有从篮筐里下去」。
        #
        # ⚠ 顺序很重要：**必须放在球员像素观测（player_obs）填好之后**。
        # 之前把它放在第 3 步之前，导致「向前找」那条判队逻辑拿不到任何
        # 球员观测、静默退回"筐下没人"（实测就这样白跑了一轮）。
        use_legacy = self.shot_engine == "legacy" and (
            self.score_policy in ("court", "visual") or not sb_ok)
        if use_legacy:
            # 场上口径：比分牌仍留在 meta 作参考，不改变旧状态机的三态结果。
            rt.scoreboard_events = []
        if use_legacy or not sb_ok:
            self._dispatch_shot_engine(rt, step)

        # ---- 6) 位置归因 + 打铁候选 ----
        step(0.94, "融合出手")
        if use_legacy:
            # 状态机已含未中/未知，不再用另一套近筐候选补入重复事件或改写位置。
            rt.detections_meta["attempts"] = {
                "total": len(rt.attempts), "from_legacy": len(rt.attempts),
                "from_scoreboard": 0, "from_ball_track": 0, "miss_candidates": 0}
        else:
            self.extract_attempts(rt)
        if progress:
            progress(1.0, "推理完成")
        return rt

    def _dispatch_shot_engine(self, rt, progress=None):
        progress = progress or (lambda *args: None)
        if self.shot_engine == "legacy":
            from .legacy_shots.adapter import to_attempts
            progress(0.82, "整理逐次投篮：命中、未中与未知")
            rt.attempts = to_attempts(self._legacy_events, rt, self.cal,
                value=self.visual_shot_value,
                counts_for_score=getattr(self,"_visual_counts_for_score",True))
            rt.detections_meta["visual"] = {
                "engine": "legacy", "shots": len(rt.attempts),
                "made": sum(a.made is True for a in rt.attempts),
                "missed": sum(a.made is False for a in rt.attempts),
                "unknown": sum(a.made is None for a in rt.attempts),
                "method": "逐帧跟踪球与篮筐，先记录出手，再区分命中、未中和未知"}
        else:
            rt.detections_meta["shot_engine"] = "geometry"
            progress(0.78, "篮下判进球（球从筐里下去）")
            self._hoopsight_attempts(rt, progress=progress)
            if not rt.attempts:
                progress(0.82, "识别篮筐并判断进球")
                self._visual_attempts(rt, progress=progress,
                    counts_for_score=getattr(self,"_visual_counts_for_score",True))

    # ------------------------------------------------------------------
    def _label_says_made(self, t: float, tol: float = 1.2) -> bool:
        """人工标注里，t 时刻附近有没有一条「进球」。

        标注来自 `scripts/label_baskets.py`（每个候选一段 t0~t1 + made/miss）。
        这里按区间重叠判断，容差 `tol` 秒覆盖自动判定与人工候选的时间差。
        """
        lab = self.basket_labels or {}
        for row in (lab.get("rows") or []):
            if str(row.get("label")) != "made":
                continue
            t0 = float(row.get("t0", 0.0))
            t1 = float(row.get("t1", t0))
            if t0 - tol <= t <= t1 + tol:
                return True
        return False

    # ------------------------------------------------------------------
    def _hoopsight_attempts(self, rt: RawTrack, progress=None) -> None:
        """「篮下判进球」：不依赖球检测器，只在篮筐窗口里看球有没有穿下去。

        为什么要有这条（而不是只用 `_visual_attempts`）：
        `_visual_attempts` 依赖「先把球追成一条完整弧线」，而球检测器在固定
        机位远景素材上经常整段失效（实测一段青少年比赛里它认的是黄色球衣），
        于是 0 次出手。但**进球本身是直接在画面上可测的**：
        篮圈窗口里出现「球的尺寸」的运动块，并从筐口连续下落到网下。

        判出来的每一球都带完整证据（入筐/出筐时刻、下落像素、穿筐点），
        并且会存一张逐帧验证图，方便人工复核 —— 自动计数必须能被核对。
        `_visual_attempts` 仍然保留在后面兜底（有球轨迹的素材上它更细）。
        """
        from .hoopsight import HoopSightError, SightConfig, scan_hoopsight

        cfg = self.hoop_sight_cfg or SightConfig()

        # ---- 镜头运动把关 ----
        # 「球从筐上方落到下方」这套判据的前提是**画面里只有球在动**。
        # 镜头一平移，整幅画面都在动 → 运动块到处都是 → 疯狂误报
        # （实测这段 960x544 的转播：候选块 13366 个、判出 41 个"进球"）。
        # 这种情况直接不做运动块判定，交给球检测器那条路（_visual_attempts）。
        _cm = (rt.detections_meta.get("camera_motion") or {})
        _moving = (_cm.get("verdict") == "moving")
        if not _moving:
            # verdict 只有三档，这里再用「帧间位移中位数」兜一道：
            # 明显平移（>8 px/s）就足够让运动块判据失效。
            try:
                _moving = float(_cm.get("median_px") or 0.0) > 8.0
            except (TypeError, ValueError):
                _moving = False
        if _moving:
            rt.detections_meta["hoopsight_skipped"] = (
                "镜头在移动（%s px/s）—— 运动块判进球的前提（只有球在动）不成立，"
                "已跳过；改用球检测器路径判断出手。"
                % _cm.get("median_px"))
            meta_early = {"skipped": True, "reason":
                          rt.detections_meta["hoopsight_skipped"],
                          "camera_motion": _cm}
            rt.detections_meta["hoopsight"] = meta_early
            return

        # 有「人工标点」就直接用它，别跑检测器 —— 见 manual_hoop 的说明
        ht = None
        hoop_hint = self.hoop_hint
        if self.manual_hoop is not None:
            hoop_hint = (float(self.manual_hoop.cx), float(self.manual_hoop.cy))
        have_radius = (self.manual_hoop is not None
                       and float(self.manual_hoop.rx or 0) > 0
                       and float(self.manual_hoop.ry or 0) > 0)
        if self.manual_hoop is not None and not have_radius:
            rt.detections_meta["hoop_source"] = "manual_center+detector_geometry"
        if have_radius:
            from .hoop import Hoop, HoopTrack
            h = self.manual_hoop
            h.method = h.method or "manual"
            ht = HoopTrack(samples=[(0.0, h)], fps=rt.fps,
                           duration=rt.duration, votes=1,
                           frames=max(1, int(rt.duration * (rt.fps or 30))),
                           width=rt.width)
            rt.detections_meta["hoop_source"] = "manual_landmarks"
        try:
            scan = scan_hoopsight(
                self.video_path, cfg,
                hoop_cfg=self.hoop_cfg, weights=self.hoop_weights,
                device=(self.device or "cpu"), hoop_track=ht, hoop_hint=hoop_hint,
                progress=(lambda p, m: progress(0.78 + 0.06 * p, m))
                if progress else None)
        except HoopSightError as e:
            rt.detections_meta["hoopsight_error"] = str(e)
            return
        except Exception as e:  # noqa: BLE001
            rt.detections_meta["hoopsight_error"] = f"{type(e).__name__}: {e}"
            return

        # ---- 候选块密度把关 ----
        # 密度 = 候选块数 / 扫描帧数。固定机位下它极低（实测 0.018 块/帧）；
        # 镜头平移或**多镜头剪辑**时整幅画面都在动，密度会暴涨（实测 2.2 块/帧，
        # 高 120 倍），此时"球从筐上方落到下方"这套判据会把满屏噪声当成进球
        # （实测判出 41 个假进球）。密度超阈值就直接跳过运动块路径，
        # 交给球检测器路径（_visual_attempts）—— 那条路不依赖"只有球在动"。
        _rate = (float(scan.blobs) / max(1.0, float(scan.frames)))
        meta_blob_rate = {"blobs": scan.blobs, "frames": scan.frames,
                          "per_frame": round(_rate, 4),
                          "limit": float(getattr(cfg, "max_blob_rate", 0.5))}
        if _rate > float(getattr(cfg, "max_blob_rate", 0.5)):
            rt.detections_meta["hoopsight_skipped"] = (
                f"候选块密度 {_rate:.2f} 块/帧（阈值 "
                f"{getattr(cfg, 'max_blob_rate', 0.5)}）—— 画面整体在动"
                "（镜头平移 / 多镜头剪辑），运动块判据不成立，已跳过；"
                "改用球检测器路径判断出手。")
            rt.detections_meta["hoopsight"] = {
                "skipped": True, "reason": rt.detections_meta["hoopsight_skipped"],
                "blob_rate": meta_blob_rate, "shots": [],
                "candidate_trace": getattr(scan, "candidate_trace", {})}
            return

        meta = {"hoops": scan.hoops, "blobs": scan.blobs,
                "candidate_trace": getattr(scan, "candidate_trace", {}),
                "frames": scan.frames, "note": scan.note,
                "diagnosis": getattr(scan, "diagnosis", {}) or {},
                "video_path": self.video_path,
                "min_confidence": float(getattr(cfg, "min_confidence", 0.0)),
                "rejected": getattr(scan, "rejected", []) or [],
                "player_obs_n": len(getattr(rt, "player_obs", []) or []),
                "player_track_n": len(getattr(rt, "player_track", []) or []),
                "people": getattr(scan, "people", []) or [],
                "note_people": getattr(scan, "note_people", ""),
                "shots": [s.to_dict() for s in scan.shots],
                "video_path": self.video_path,
                "auto_shots": [],       # 下面填「人工筛选前」的自动判定
                "auto_rejected": []}
        rt.detections_meta["hoopsight"] = meta

        # 人工筛选前的**自动判定**单独留一份：界面上「进球确认」要看到
        # "自动判了哪几球"，用户才能逐条判进/没进；否则过滤之后就没得看了。
        meta["auto_shots"] = [s.to_dict() for s in scan.shots]
        meta["auto_rejected"] = getattr(scan, "rejected", []) or []

        # ---- 模型分数 / 人工反馈 一起决定「留下哪些进球」----
        #
        # 规则（重要，别写反）：
        #   * **模型**给分数，低于阈值的不计入（学习式判据）；
        #   * **人工反馈只做否决**：你判过「没进」的时刻不再出现；
        #     你判过「进了」的时刻直接保留（哪怕模型分低）；
        #   * 没被反馈覆盖的时刻，按模型分数决定 —— 不能因为"反馈里没有它"
        #     就把新判出来的球一起丢掉。
        #
        # 为什么强调：上一版把反馈当**白名单**（只报告判过"进了"的），
        # 用户只标过"没进"，重跑就成了"0 进球 / 0 出手" ——
        # 看起来像识别功能坏了，其实是过滤器把结果全吃了。
        fb_made, fb_miss = [], []
        if self.basket_labels is not None:
            for row in (self.basket_labels.get("rows") or []):
                try:
                    t0 = float(row.get("t0", 0.0))
                    t1 = float(row.get("t1", t0))
                except Exception:
                    continue
                mid = (t0 + t1) / 2.0
                if str(row.get("label")) == "made":
                    fb_made.append(mid)
                elif str(row.get("label")) == "miss":
                    fb_miss.append(mid)

        def _fb_of(t: float, tol: float = 1.2):
            for tt in fb_made:
                if abs(tt - t) <= tol:
                    return "made"
            for tt in fb_miss:
                if abs(tt - t) <= tol:
                    return "miss"
            return None

        # 人工反馈优先，且**不依赖模型**：
        #   miss → 否掉；made → 保留；没有反馈 → 有模型按模型分，没模型按判据自身。
        # 踩过的坑：这段逻辑曾经包在 `if self.basket_model:` 里，而模型默认关闭
        # → 用户"明明判过了却还报这个进球"。
        if fb_made or fb_miss or getattr(self, "basket_model", None):
            try:
                sc = getattr(self, "basket_model", None)
                thr = sc.threshold() if sc is not None else None
                h0 = (scan.hoops or [{}])[0]
                hp = (h0.get("cx"), h0.get("cy"))
                pool = list(scan.shots) + list(getattr(scan, "rejected", []) or [])
                scored, kept = [], []
                for s_ in pool:
                    t = float(s_.t)
                    fb = _fb_of(t)
                    v = None
                    if sc is not None:
                        v = sc.score_candidate(self.video_path, t, hp)
                    if fb == "miss":
                        keep = False
                    elif fb == "made":
                        keep = True
                    elif v is None:
                        keep = True          # 没有模型也没有反馈 → 尊重判据自身
                    else:
                        keep = float(v) >= float(thr)
                    d = s_.to_dict()
                    d["model_score"] = (None if v is None else round(float(v), 4))
                    d["model_threshold"] = thr
                    d["feedback"] = fb
                    d["kept"] = bool(keep)
                    if not keep:
                        d["drop_reason"] = ("你判过：没进" if fb == "miss"
                                            else "模型分 %.3f < 阈值 %.3f"
                                                 % (float(v or 0.0), float(thr or 0.0)))
                    scored.append(d)
                    if keep:
                        kept.append(s_)
                meta["decision"] = {
                    "model": (str(getattr(sc, "dir", "")) if sc is not None else None),
                    "threshold": thr,
                    "feedback_made": len(fb_made), "feedback_miss": len(fb_miss),
                    "evaluated": len(scored), "kept": len(kept),
                    "dropped_by_feedback": sum(1 for d in scored
                                               if d.get("feedback") == "miss"),
                    "dropped_by_model": sum(
                        1 for d in scored if not d["kept"]
                        and d.get("feedback") != "miss"),
                    "all": scored,
                    "note": ("人工反馈优先（判过没进的一律否掉）；"
                             "其余有模型按模型分，没模型按判据自身")}
                scan.shots = kept
                meta["shots"] = [s_.to_dict() for s_ in scan.shots]
            except Exception as e:  # noqa: BLE001
                meta["decision_error"] = f"{type(e).__name__}: {e}"

        # 标定能不能用来判 2/3 分 / 能不能用来出热区与战术图？
        # 先验一道：画面里的篮筐必须被投影到真篮筐附近。
        # **放在 early-return 之前**：即使一球没判出，也要把这个结论记下来，
        # 否则管线看不到"标定不适用"，照样画错的热区与战术图（用户实测踩到）。
        try:
            from .baskets import calibration_sane_for_scoring
            hoop_px = None
            if scan.hoops:
                h0 = scan.hoops[0]
                hoop_px = (h0.get("cx", 0.0), h0.get("cy", 0.0),
                           h0.get("rx", 1.0))
            elif getattr(self, "manual_hoop", None) is not None:
                # 检测不到篮筐（镜头在动、画面不清）时，**退回用用户手工标的篮筐**做验证。
                # 实测踩到：用户把球场标定和篮筐都标好了，却因为这里只认"检测到的篮筐"
                # 而判成"没有可用标定" → 热区/战术图一个都不生成，
                # 用户完全不知道为什么（标定明明存下来了、误差 0.693m）。
                mh = self.manual_hoop
                hoop_px = (float(mh.cx), float(mh.cy), float(mh.rx))
                meta["calibration_hoop_source"] = "manual_marks"
            cal_ok2, cal_why = calibration_sane_for_scoring(
                (self.cal if self.cal and self.cal.H else None), hoop_px)
            meta["calibration_for_value"] = {"ok": cal_ok2, "reason": cal_why}
            if not cal_ok2:
                # 统一 gate：标定不适用时，后续所有"球场坐标"产物都不该出
                rt.detections_meta["calibration_valid"] = False
                rt.detections_meta["calibration_rejected"] = cal_why
        except Exception as e:  # noqa: BLE001
            meta["calibration_check_error"] = f"{type(e).__name__}: {e}"

        if not scan.shots:
            return

        from .baskets import hoopsight_to_attempts

        counts = getattr(self, "_visual_counts_for_score", True)
        cal_ok = bool((meta.get("calibration_for_value") or {}).get("ok", True))
        # 每个进球都留一张逐帧验证图：结论要能被肉眼核对，不许只有数字
        try:
            from .hoopsight import make_person_sheet, make_team_sheet, make_verify_sheet
            out_dir = Path(self.video_path).resolve().parent
            sheet = out_dir / "hoopsight_verify.png"
            p = make_verify_sheet(self.video_path, scan, out_path=str(sheet))
            if p:
                meta["verify_sheet"] = str(p)
            # 再留一张「筐下球员 + 队别/球衣颜色」的图：用来核对这球是哪队进的
            ts = out_dir / "hoopsight_team.png"
            p2 = make_team_sheet(self.video_path, scan,
                                 getattr(rt, "player_obs", None) or [],
                                 jersey_colors=rt.detections_meta.get("jersey_colors"),
                                 out_path=str(ts))
            if p2:
                meta["team_sheet"] = str(p2)
            # 筐下「人形运动块」的证据图：一行一次进球，没检出就明写
            ps = out_dir / "hoopsight_people.png"
            p3 = make_person_sheet(self.video_path, scan,
                                   getattr(scan, "people", None) or [],
                                   out_path=str(ps))
            if p3:
                meta["people_sheet"] = str(p3)
        except Exception:
            pass

        atts, team_att = hoopsight_to_attempts(
            scan, rt, cal=(self.cal if self.cal and self.cal.H else None),
            fps=rt.fps, default_value=self.visual_shot_value,
            counts_for_score=counts, hoop_side=self._hoop_sides(),
            cal_ok=cal_ok, team_override=self.basket_teams,
            jersey_colors=rt.detections_meta.get("jersey_colors"),
            lookback_s=self.basket_lookback_s)
        # 进球序号 → 队别写进 meta，报告里能直接引用
        meta["team_attribution"] = team_att
        meta["team_unknown"] = sorted(
            i + 1 for i, info in (team_att.get("by_shot") or {}).items()
            if not (info or {}).get("team"))
        rt.attempts.extend(atts)
        rt.attempts.sort(key=lambda a: a.t)
        meta["attempts"] = len(atts)
        self._hoopsight_times = [float(a.t) for a in atts]
        meta["score_if_2pt_each"] = {
            "home": sum(int(a.forced_value or 0)
                        for a in atts if a.team == "home"),
            "away": sum(int(a.forced_value or 0)
                        for a in atts if a.team == "away")}

    # ------------------------------------------------------------------
    def _cal_at(self, t: float):
        """取 t 时刻该用哪份标定。

        多机位（multi_cal 有 segments）时按"这一帧属于哪个镜头"取那一份 H；
        单机位就直接用 self.cal —— 调用方不用关心是哪种。
        为什么必须按帧取：两个机位没有共同坐标系，用错一份 H 投出来的球员
        会整片落在球场外（实测把球员投到边线外好几米）。
        """
        mc = getattr(self, "multi_cal", None)
        if mc is None:
            return self.cal
        c = mc.cal_at(t)
        return c if c is not None else self.cal

    def _calibration_matches(self, W: int, H: int) -> bool:
        """标定文件是不是这段视频的。

        标定是**按机位**存的，不是按分辨率。只比分辨率根本拦不住 ——
        实测拿公园球场的标定去算一段 NBA 转播（同样 1280×720）也能"通过"，
        结果出手点被算到三分线外十几米，看着像真的其实全是垃圾。
        所以标定文件现在显式记录它属于哪个视频（`for_video`），对不上就拒绝。
        """
        if not self.cal or not self.cal.H:
            # 多机位：self.cal 只是"主标定"那份，也可能为空 —— 只要有一段对得上就算对得上
            mc = getattr(self, "multi_cal", None)
            if mc is not None:
                try:
                    return bool(mc.matches_video(self.video_path, W, H))
                except Exception:                            # noqa: BLE001
                    return False
            return False
        mc = getattr(self, "multi_cal", None)
        if mc is not None and len(getattr(mc, "segments", []) or []) > 1:
            # 多机位：任意一段与本视频匹配即可（每段都是同一台机器的不同镜头）
            try:
                if mc.matches_video(self.video_path, W, H):
                    return True
            except Exception:                                # noqa: BLE001
                pass
        return bool(self.cal.matches_video(self.video_path, W, H))

    # ------------------------------------------------------------------
    def _visual_attempts(self, rt: RawTrack, progress=None,
                         counts_for_score: bool = True) -> None:
        """「球 + 篮筐」路径：认篮筐、认球轨迹、判断有没有穿筐，产出出手。"""
        from .hoop import HoopConfig, detect_hoop_track, detect_shots
        from .ball import BallConfig, rank_ball_tracks

        cfg = self.hoop_cfg or HoopConfig()
        ball_cfg = getattr(self, "_ball_cfg", None) or BallConfig()
        tracks = getattr(self, "_tracks_px", None) or []
        if not tracks:
            rt.detections_meta["visual_error"] = "没有可用的球轨迹，视觉路径跳过"
            return
        try:
            # 人工标点怎么用，分两种情况（2026-09-27 实测后定的）：
            #
            # ① 标了中心 **且** 至少一侧边缘（能算出 rx）→ 直接采信，跳过检测器。
            #    用户已经在画面上确认过篮筐，固定机位下这是最可靠的来源
            #    （实测 fixedcam 手标之后 3/3 全对）。
            #
            # ② **只标了中心** → 把中心当**初始位置提示**交给检测器，筐位仍然逐帧跟踪。
            #    为什么不能按①那样固定成一个点：那样等于声称"整段素材篮筐不动"。
            #    实测踩过：nathan（手持、镜头在动）只标中心后，整段被当成静止筐，
            #    真进球从 3 个掉到 1 个（4.91s / 15.55s 被误判成"贴筐掠过"）——
            #    因为那一刻真实筐心比标点那一帧偏了 20 多像素。
            #    当提示用则两头都要：检测器逐帧跟随镜头，同时"从用户点的位置开始找"。
            manual = getattr(self, "manual_hoop", None)
            have_radius = bool(manual is not None
                               and float(manual.rx or 0) > 0
                               and float(manual.ry or 0) > 0)
            if manual is not None and have_radius:
                from .hoop import HoopTrack
                manual.method = manual.method or "manual"
                ht = HoopTrack(
                    samples=[(0.0, manual)], fps=rt.fps, duration=rt.duration,
                    votes=1, frames=max(1, int(rt.duration * (rt.fps or 30))),
                    width=rt.width)
                rt.detections_meta["visual_hoop_source"] = "manual_landmarks"
            else:
                hint = getattr(self, "hoop_hint", None)
                if hint is None and manual is not None:
                    hint = (float(manual.cx), float(manual.cy))
                ht = detect_hoop_track(
                    self.video_path, cfg,
                    progress=(lambda p, m: progress(0.82 + 0.06 * p, m))
                    if progress else None,
                    cuts=getattr(self, "_cuts", None),
                    hint=hint,
                    weights=self.hoop_weights, device=(self.device or "cpu"))
                if manual is not None:
                    # 只标了中心：筐位是**逐帧跟出来的**，用户那一点只是起点。
                    # 半径也来自检测器 —— 以前前端会给一个"画面宽 2%"的猜测值，
                    # 那是假精度（实测真实 rx 可能是它的 2 倍），会让真进球被判成贴筐。
                    med = ht.median
                    rt.detections_meta["visual_hoop_source"] = \
                        "manual_center+hint(tracked)"
                    rt.detections_meta["manual_hoop_radius"] = {
                        "rx": round(float(med.rx or 0), 1),
                        "ry": round(float(med.ry or 0), 1),
                        "source": "detector_track",
                        "detector_method": getattr(med, "method", ""),
                        "note": "用户只标了篮筐中心：中心当初始位置提示，筐位逐帧跟踪；"
                                "半径取跟踪结果的中值"}
        except RuntimeError as e:
            rt.detections_meta["visual_error"] = str(e)
            return

        # 篮筐位置确定后，再按「靠近篮筐 / 有穿筐趋势」筛选球轨迹。
        all_tracks = tracks
        tracks = rank_ball_tracks(tracks, ball_cfg, hoop=ht)
        self._tracks_px = tracks
        # 归属投篮者时要用完整的球轨迹：真正的「出手段」可能没被
        # 排进 top-K（例如被拆成上面一小截、下面一大截）。
        self._tracks_all_px = all_tracks
        if rt.detections_meta.get("ball"):
            rt.detections_meta["ball"]["tracks_ranked"] = len(tracks)
        shots = detect_shots(tracks, ht, cfg)

        # 反过来用「篮筐」校验标定：标定正确的话，画面里检测到的篮筐中心投到
        # 球场坐标应该正好落在 (0, ±1.575) 附近。落不到就说明这份标定不是
        # 这段视频的 —— **分辨率一样也完全可能是另一个机位**，只比分辨率根本
        # 拦不住（实测踩过：拿公园球场的标定去算 NBA 转播，出手点被算到
        # 三分线外 15 米）。校验不过就退回「篮筐占位坐标」，绝不硬套。
        if rt.detections_meta.get("calibration_valid"):
            hx, hy = self.cal.to_court(ht.median.cx, ht.median.cy)
            d = min(math.hypot(hx, hy - 1.575), math.hypot(hx, hy + 1.575))
            if d > 3.0:
                rt.detections_meta["calibration_valid"] = False
                rt.detections_meta["calibration_reject"] = (
                    f"标定把画面里的篮筐投到了球场坐标 ({hx:.1f}, {hy:.1f})，"
                    f"离真实篮筐 {d:.1f}m —— 这份标定不属于这个机位，已拒绝使用")

        rt.detections_meta["hoop"] = ht.to_dict()
        rt.detections_meta["visual"] = {
            "shots": len(shots), "made": sum(1 for s in shots if s.made and s.evidence not in {"cross_interpolated", "cross_extrapolated"}),
            "missed": sum(1 for s in shots if s.made is False),
            "unknown": sum(1 for s in shots if s.made is None or s.evidence in {"cross_interpolated", "cross_extrapolated"}),
            "inferred": sum(1 for s in shots if s.evidence in {"cross_interpolated", "cross_extrapolated"}),
            "hoop_votes": ht.votes, "hoop_frames": ht.frames,
            "hoop_drift_px": [round(v, 1) for v in ht.drift()],
            "method": ("比分牌不可用，改用「球轨迹穿筐」判定进球"
                       if rt.detections_meta.get("scoreboard_error")
                       else "比分牌没给出得分事件，改用视觉路径"),
        }

        # 球员归因：出手时刻离球最近的球员轨迹
        pos = getattr(self, "_player_pos", None) or {}
        boxes = getattr(self, "_player_boxes", None) or {}
        all_tracks = getattr(self, "_tracks_all_px", None) or tracks
        for s in shots:
            # 先看谁起跳：投篮者出手时会跳，另一名球员（抢板/传球）很少跳。
            jpid = self._jump_shooter(s, boxes)
            hit = None
            if jpid is not None:
                hit = (jpid, self._nearest_box(boxes.get(jpid), s.t))
            if hit is None:
                hit = self._locate_shooter(s, boxes, all_tracks)
            if hit is not None:
                s.player_box = [hit[0]]
                if hit[1] is not None:
                    s.player_bbox = hit[1]
                continue
            # 兜底：找不到球迹起点时，才退回「出手帧附近离球最近的球员」。
            best_pid, bd = None, 1e18
            for pid, arr in pos.items():
                for (tt, cx, cy) in arr:
                    if abs(tt - s.t) > 0.6:
                        continue
                    d = math.hypot(cx - s.release_x, cy - s.release_y)
                    if d < bd:
                        bd, best_pid = d, pid
            s.player_box = [best_pid] if best_pid else None
            best_bbox = None
            if best_pid and best_pid in boxes:
                bbd = 1e18
                for _row in boxes[best_pid]:
                    tt, x1, y1, x2, y2 = _row[:5]
                    if abs(tt - s.t) > 0.6:
                        continue
                    cx = (x1 + x2) / 2.0
                    d = math.hypot(cx - s.release_x, y2 - s.release_y)
                    if d < bbd:
                        bbd, best_bbox = d, [x1, y1, x2, y2]
            if best_bbox is not None:
                s.player_bbox = best_bbox

        # Canonical jersey/manual assignment shared with player statistics.
        team_of = {pid:p.team for pid,p in rt.players.items() if p.team in ("home","away")}
        side = self._hoop_sides()
        cal_ok = bool(rt.detections_meta.get("calibration_valid"))
        for s in shots:
            pid = s.player_box[0] if s.player_box else ""
            team = team_of.get(pid)
            team_unknown = team is None
            if team is None:
                # 认不出人：按出手点横向位置粗分左右
                # （只影响统计分组，不影响总进球数）
                team = "home" if s.release_x < ht.median.cx else "away"
            hx, hy = (0.0, -1.575) if side.get(team, "left") == "left" \
                else (0.0, 1.575)
            att = Attempt(
                t=round(s.t, 2), team=team, player_id=pid or f"{team[0].upper()}?",
                x=hx, y=hy, period=1, is_free_throw=False,
                release_frame=int(s.t * rt.fps),
                made=s.made, conf=float(s.confidence),
                source="ball_rim", location_source="rim_placeholder",
                location_estimated=True, counts_for_score=counts_for_score and not team_unknown)
            att.attach_shot_evidence(s)
            if att.made is None:
                att.tags.extend(["outcome_unknown", "needs_review"])
            if team_unknown:
                att.tags.extend(["team_unknown", "team_source_heuristic", "needs_review"])
            if cal_ok:
                # 有可信的球场标定：把出手像素投到地面坐标，分值由规则引擎按
                # 「三分线几何」判 —— 这是唯一能真正区分 2 分和 3 分的办法。
                xm, ym = self.cal.to_court(s.release_x, s.release_y)
                if abs(xm) <= 9.0 and abs(ym) <= 16.0:
                    att.x, att.y = xm, ym
                    att.location_source = "ball_track"
            else:
                # 没有可用标定时，用「球员身高 + 篮圈尺寸」做图像空间的
                # 距离估计，尽量按正常篮球规则判 2 分/3 分；估不准就落回
                # visual_shot_value，并明确标记分值来自估计、需要复核。
                val, vsrc = self._estimate_shot_value(s, ht, cal_ok)
                att.forced_value = int(val)
                att.value_assumed = True
                att.value_source = vsrc
            # 最低置信度：实测球+篮筐那条路会在没人投篮的时刻吐出碎片轨迹。
            # **但不能直接删** —— 删掉的后果是「出手 0 次」，用户会以为功能坏了
            # （实测踩到：这段视频的 3 次出手 conf 恰好都等于 0.45，被 `<=` 全部丢掉）。
            # 正确做法是保留 + 标记，让它进"出手次数"，但不让低置信度的"进球"污染比分。
            low = (att.conf is not None and float(att.conf) <= 0.45)
            if low:
                tags = list(getattr(att, "tags", None) or [])
                if "low_confidence" not in tags:
                    tags.append("low_confidence")
                att.tags = tags
                if att.made:
                    # 低置信度的"进了"不计分：宁可少算，不可虚高
                    att.counts_for_score = False
                rt.detections_meta.setdefault("attempts_low_conf", []).append(
                    {"t": round(float(att.t), 2), "conf": float(att.conf),
                     "made": att.made, "kept": True,
                     "note": "已保留在出手里，但标为需复核" +
                             ("；且不计入比分" if att.made else "")})
            rt.attempts.append(att)
        rt.attempts.sort(key=lambda a: a.t)

        # 和「篮下判进球」去重：球+篮筐那条路对同一次进球可能给出一个
        # 时间相近、但 made 未知的候选（实测多出一条 t=101.1s 的重复项，
        # 把「3 次出手」变成「4 次」）。同一时刻附近以篮下判进球为准，
        # 因为它是**直接看到球穿筐**的那一路。
        hs_times = getattr(self, "_hoopsight_times", None) or []
        if hs_times:
            before = len(rt.attempts)
            keep = []
            dropped = []
            for a in rt.attempts:
                near = min((abs(a.t - t) for t in hs_times), default=9e9)
                if a.source != "hoopsight" and near <= 1.5:
                    dropped.append(round(float(a.t), 2))
                    continue
                keep.append(a)
            rt.attempts = keep
            if dropped:
                rt.detections_meta["attempts_dedup"] = {
                    "dropped": dropped,
                    "reason": "与篮下判进球时刻重叠（<=1.5s），以直接看到穿筐的那条为准",
                    "before": before, "after": len(keep)}

    # ------------------------------------------------------------------
    @staticmethod
    def _nearest_box(arr, t, tol=None):
        """取 time=t 附近的那一帧球员框。"""
        if not arr:
            return None
        best = None
        for _row in arr:
            tt, x1, y1, x2, y2 = _row[:5]
            d = abs(tt - t)
            if tol is not None and d > tol:
                continue
            if best is None or d < best[0]:
                best = (d, [x1, y1, x2, y2])
        return best[1] if best else None

    def _jump_shooter(self, shot, boxes, win_back: float = 1.3,
                      win_fwd: float = 0.4, min_frac: float = 0.15,
                      min_px: float = 12.0):
        """用「谁起跳了」来判定投篮者，返回 player_id 或 None。

        投篮者出手时会起跳（球衣框顶部先上移再回落），而抢篮板/传球的球员
        基本不会。实测：单人投篮视频里另一位球员也常在画面里，光靠"球在谁
        附近"很容易张冠李戴；而 3 次出手时刻那位投篮者都在跳，另一个人不跳。
        找不到起跳动作时返回 None，交给球迹兜底。
        """
        best = None
        for pid, arr in boxes.items():
            seq = sorted(arr, key=lambda r: r[0])
            score = 0.0
            for i, row in enumerate(seq):
                t, _x1, y1, _x2, y2 = row[:5]
                if not (shot.t - win_back <= t <= shot.t + win_fwd):
                    continue
                h = max(1.0, float(y2 - y1))
                drop = 0.0
                for j in range(max(0, i - 14), i):
                    t0, _a, y10, _b, _c = seq[j][:5]
                    if 0.2 <= t - t0 <= 0.75:
                        drop = max(drop, y10 - y1)
                if drop >= max(min_px, min_frac * h):
                    score = max(score, drop / h)
            if score > 0 and (best is None or score > best[1]):
                best = (pid, score)
        return best[0] if best else None

    def _locate_shooter(self, shot, boxes, tracks,
                        window_s: float = 3.0,
                        min_speed: float = 120.0,
                        head_tol_px: float = 40.0):
        """把一次出手归到投篮者，返回 (player_id, bbox)；找不到则 None。

        为什么不能拿「出手帧的球位」比球员中心：单目画面里球在空中时，
        像素位置对应的是另一条视线，和球员本人差着几米远；球迹还常被拆成
        几段，release 点往往是抛物线中段，跟谁都不沾边。实测同一位投篮者
        的 3 次出手被分给了 2~3 个球员 ID。

        可靠的信号是「球从谁头顶出手」：投篮时球从投篮者头顶离开。于是在
        窗口内找所有快速球迹里、落在某位球员头顶正上方（YOLO 框顶部附近）
        的点，离谁的头顶最近就算谁出手。
        """
        if not tracks or not boxes:
            return None
        best = {}  # pid -> (dist, t, bbox)
        for tr in tracks:
            if len(tr) < 3:
                continue
            span = tr[-1].t - tr[0].t
            if span <= 0:
                continue
            path = sum(math.hypot(tr[i + 1].x - tr[i].x,
                                  tr[i + 1].y - tr[i].y)
                       for i in range(len(tr) - 1))
            if path / span < min_speed:
                continue
            for c in tr:
                if not (shot.t - window_s <= c.t <= shot.t + 0.25):
                    continue
                for pid, arr in boxes.items():
                    box, bdt = None, 1e18
                    for _row in arr:
                        tt, x1, y1, x2, y2 = _row[:5]
                        if abs(tt - c.t) <= 0.35 and abs(tt - c.t) < bdt:
                            bdt, box = abs(tt - c.t), (x1, y1, x2, y2)
                    if box is None:
                        continue
                    x1, y1, x2, y2 = box
                    if c.y > y1 - 6 or not (x1 - head_tol_px <= c.x
                                            <= x2 + head_tol_px):
                        continue
                    d = math.hypot(c.x - (x1 + x2) / 2.0, c.y - y1)
                    cur = best.get(pid)
                    if cur is None or d < cur[0]:
                        best[pid] = (d, c.t, [x1, y1, x2, y2])
        if not best:
            return None
        pid = min(best, key=lambda k: best[k][0])
        return pid, best[pid][2]

    def _estimate_shot_value(self, s, ht, cal_ok: bool):
        """没有可信标定时，估计这次出手是 2 分还是 3 分。

        思路（全部在图像空间，不需要固定机位）：
          1. 篮圈真实半径 0.225m，用检测到的篮圈像素半径得到篮筐处的 px/m；
          2. 如果 YOLO 找到了投篮球员，用球员框高度（按 1.9m）得到球员处的 px/m；
          3. 用两处尺度的平均值，把「球员脚点 -> 篮筐地面点」的像素距离换成米；
          4. ≥6.75m 判 3 分，≤5.2m 判 2 分，灰区再参考球的最高点。
        结果一律标记 value_assumed / value_source，允许复核页改分。
        """
        # 用户明确指定 3 分时直接强制
        if int(getattr(self, "visual_shot_value", 2)) == 3:
            return 3, "forced"
        try:
            hoop = ht.at(s.t)
        except Exception:
            return int(getattr(self, "visual_shot_value", 2)), "default"
        rim_r = max(3.0, float(hoop.rx))
        scale_hoop = rim_r / 0.225          # 篮筐处 px/m
        bbox = getattr(s, "player_bbox", None)
        if bbox:
            x1, y1, x2, y2 = [float(v) for v in bbox]
            foot_x = (x1 + x2) / 2.0
            foot_y = y2
            player_h = max(8.0, y2 - y1)
            scale_shooter = player_h / 1.9  # 球员处 px/m（按 1.9m 身高）
            hoop_floor_y = float(hoop.cy) + float(hoop.ry) * 2.5
            d_px = math.hypot(foot_x - float(hoop.cx),
                              foot_y - hoop_floor_y)
            scale = max(1.0, (scale_shooter + scale_hoop) / 2.0)
            dist_m = d_px / scale
        else:
            # 没有球员框：用球轨迹释放点粗估，再乘保守系数
            d_px = math.hypot(float(s.release_x) - float(hoop.cx),
                              float(s.release_y) - float(hoop.cy))
            dist_m = d_px / max(1.0, scale_hoop) * 0.55
        if dist_m >= 6.75:
            return 3, "visual_estimate"
        if dist_m <= 5.2:
            return 2, "visual_estimate"
        # 灰区：用球的最高点/弧线辅助
        apex_above = max(0.0, (float(hoop.cy) - float(hoop.ry)) - float(s.apex_y))
        if dist_m >= 6.0 and apex_above >= float(hoop.ry) * 1.5:
            return 3, "visual_estimate"
        return 2, "visual_estimate"

    # ------------------------------------------------------------------
    def _sample_jersey_colors(self, rt: RawTrack, max_frames: int = 40,
                              per_frame: int = 12) -> None:
        """采每个球员的球衣主色（报告/复核用，也是「哪一队」的可读证据）。

        **按帧采，不是按球员采。** 球员观测有好几万条，一人一条去
        `cap.set()` 定位再读就是几千次随机 seek —— 在 1GB 的 MP4 上这能跑
        几分钟甚至像卡死（真踩过：直接把这轮跑挂住）。所以只挑
        `max_frames` 帧、每帧最多 `per_frame` 个框；整段覆盖两类球队足够。

        取样窗口取躯干中部（避开头部与背景），剔掉肤色与过暗/过曝像素，
        再取色相直方图的众数。结果写进 meta 的 `jersey_colors`。
        """
        obs = getattr(rt, "player_obs", []) or []
        if not obs:
            rt.detections_meta["jersey_colors_error"] = "没有球员像素观测"
            return
        import cv2
        import numpy as np

        by_t: dict[float, list] = {}
        for o in obs:
            by_t.setdefault(round(float(o.get("t", 0.0)), 3), []).append(o)
        stamps = sorted(by_t)
        if not stamps:
            return
        step = max(1, len(stamps) // max(1, int(max_frames)))
        chosen = stamps[::step][:max(1, int(max_frames))]

        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            return
        per_pid: dict[str, list] = {}
        frames_used = 0
        try:
            for ts in chosen:
                cap.set(cv2.CAP_PROP_POS_FRAMES,
                        int(round(ts * (rt.fps or 30.0))))
                ok, frame = cap.read()
                if not ok:
                    continue
                frames_used += 1
                # 同一帧里分散取：按横向位置排序后均匀抽，别只采到半边场的人
                lst = sorted(by_t[ts], key=lambda o: float(o.get("foot_x", 0)))
                if len(lst) > per_frame:
                    idxs = np.linspace(0, len(lst) - 1,
                                       int(per_frame)).astype(int)
                    lst = [lst[i] for i in idxs]
                for o in lst:
                    pid = str(o.get("player_id", ""))
                    if not pid or len(per_pid.get(pid, [])) >= 2:
                        continue
                    c = self._jersey_color_of(frame, o.get("box"))
                    if c is not None:
                        per_pid.setdefault(pid, []).append(c)
        finally:
            cap.release()
        if not per_pid:
            rt.detections_meta["jersey_colors_error"] = (
                f"没有采到任何球衣色（用了 {frames_used} 帧）")
            return

        def name_of(h, s, v):
            """把一个 HSV 主色说成人话（报告与复核用）。

            顺序很重要：**先判白**再判色相。球衣是白/浅灰时色相基本是噪声
            （实测白球衣的众数色相落在 113~131 的蓝区），不先按「低饱和 +
            高亮度」摘出来，白队会被叫成"蓝队"。
            """
            if v < 70:
                return "深色/黑"
            if s < 45 and v >= 130:
                return "白色/浅灰"
            if s < 60:
                return "灰色"
            if h < 12 or h >= 165:
                return "红"
            if h < 22:
                return "橙"
            if h < 40:
                return "黄"
            if h < 78:
                return "绿"
            if h < 130:
                return "蓝"
            return "紫/品红"

        per_team: dict[str, list] = {"home": [], "away": []}
        per_team_all: dict[str, list] = {"home": [], "away": []}
        per_player: dict[str, dict] = {}
        for pid, cols in per_pid.items():
            h = float(np.median([c[0] for c in cols]))
            s = float(np.median([c[1] for c in cols]))
            v = float(np.median([c[2] for c in cols]))
            team = rt.players[pid].team if pid in rt.players else ""
            label = name_of(h, s, v)
            # 注意：`Player.jersey` 是**球衣号**（数字），别拿它装颜色 ——
            # 前端与导出都按数字用。颜色单独放 meta。
            # 同时存一份 HSV 的**实际颜色块**，绘图时直接涂出来给人核对
            # （标签可能是错的，色块不会骗人）。
            per_player[pid] = {"team": team, "label": label,
                               "hue": round(h, 1), "sat": round(s, 1),
                               "val": round(v, 1), "n": len(cols),
                               "hsv": [int(round(h)), int(round(s)),
                                       int(round(v))]}
            if team in per_team:
                per_team[team].append((h, s, v, label))
                # 原始逐次采样也留着：颜色判断要尽量多的样本，而不是
                # 每人一个中位数（少数人采样失败会主导结果）
                per_team_all[team].extend(cols)
        summary = {"per_player": per_player, "frames_used": frames_used,
                   "players": len(per_pid)}
        for team, cols in per_team.items():
            if not cols:
                continue
            h = float(np.median([c[0] for c in cols]))
            s = float(np.median([c[1] for c in cols]))
            v = float(np.median([c[2] for c in cols]))
            from collections import Counter
            names = Counter(c[3] for c in cols)
            summary[team] = {
                "n": len(cols), "hue": round(h, 1), "sat": round(s, 1),
                "val": round(v, 1), "label": name_of(h, s, v),
                # 众数标签比中位数更抗噪（少数框会采到球裤/背景）
                "label_mode": (names.most_common(1)[0][0] if names else ""),
                "labels": dict(names),
                "samples": {f"H{int(c[0])}S{int(c[1])}V{int(c[2])}": c[3]
                            for c in cols[:5]}}
        # 明确的「主队/客队各是什么颜色」—— 报告与复核都靠这一句。
        # 用**全队所有采样**的众数，而不是某一个人的颜色：单一球员的框可能
        # 正好采到球裤/背景（实测白球衣的众数色相落在蓝区，会被叫成"蓝队"）。
        # 并且**如实报出这个颜色的可靠度**：如果前两名标签的票数咬得很紧
        # （浅色队常见：白球衣在阴影里是灰、带反光偏蓝），就不要嘴硬给一个
        # 颜色，标 uncertain 让人去看核对图。
        from collections import Counter as _Counter
        legend, legend_conf = {}, {}
        for team in ("home", "away"):
            labels = _Counter(name_of(c[0], c[1], c[2])
                              for c in per_team_all[team])
            if not labels:
                legend[team], legend_conf[team] = "未知", "none"
                continue
            top = labels.most_common(2)
            legend[team] = top[0][0]
            total = sum(labels.values())
            share = top[0][1] / max(1, total)
            close = len(top) > 1 and top[1][1] >= 0.6 * top[0][1]
            legend_conf[team] = ("ok" if (share >= 0.5 and not close)
                                 else "uncertain")
        summary["legend"] = legend
        summary["legend_confidence"] = legend_conf
        summary["legend_counts"] = {
            t: dict(_Counter(name_of(c[0], c[1], c[2])
                             for c in per_team_all[t]))
            for t in ("home", "away")}
        rt.detections_meta["jersey_colors"] = summary

    @staticmethod
    def _jersey_color_of(frame, box):
        """一个球员框的球衣主色 → (H, S, V)；采不到返回 None。"""
        import cv2
        import numpy as np

        if box is None:
            return None
        x1, y1, x2, y2 = [float(v) for v in box]
        bw, bh = x2 - x1, y2 - y1
        if bw < 6 or bh < 12:
            return None
        # 躯干：纵向 25%~60%，横向内缩 25%
        yy0 = int(y1 + bh * 0.25)
        yy1 = int(y1 + bh * 0.60)
        xx0 = int(x1 + bw * 0.25)
        xx1 = int(x2 - bw * 0.25)
        H, W = frame.shape[:2]
        yy0, yy1 = max(0, yy0), min(H, max(0, yy1))
        xx0, xx1 = max(0, xx0), min(W, max(0, xx1))
        if yy1 - yy0 < 4 or xx1 - xx0 < 4:
            return None
        patch = frame[yy0:yy1, xx0:xx1]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        flat = hsv.reshape(-1, 3).astype(np.int32)
        hh, ss, vv = flat[:, 0], flat[:, 1], flat[:, 2]
        # 剔肤色（H<=18 且中等饱和）与过暗/过曝
        keep = ((~(hh <= 18) & (ss >= 25) & (ss <= 110) & (vv >= 70))
                & (vv >= 30) & (vv <= 245))
        sel = flat[keep]
        if len(sel) < 10:
            sel = flat[(vv >= 30) & (vv <= 245)]
        if len(sel) < 10:
            return None
        bins = (sel[:, 0] // 16).astype(np.int32)
        vals, cnts = np.unique(bins, return_counts=True)
        top = vals[int(np.argmax(cnts))]
        grp = sel[bins == top]
        return (float(np.median(grp[:, 0])), float(np.median(grp[:, 1])),
                float(np.median(grp[:, 2])))

    # ------------------------------------------------------------------
    def _load_bug(self, ScoreBug, W: int = 0, H: int = 0,
                  fps: float = 0.0, total: int = 0):
        """拿到这段视频可用的比分牌几何；拿不到返回 None（= 没有台标）。

        三级来源，全部都要过「这段视频里真的能读到白字」这一关：

          1. 本视频自己的缓存 `data/scoreboard_bugs/<指纹>.json`；
          2. 旧版全局 `data/scoreboard_bug.json` —— **只在它确实属于这段视频
             时才敢用**（对不上就忽略），这是为了不破坏已经标好的老工程；
          3. 现场自动定位（`locate_score_bug`），定位成功就按视频指纹存下来。

        为什么这么啰嗦：老版本直接无条件复用全局缓存，一段视频定位错了
        （框落在木地板上），换视频就会继承这个错框，整片 0 帧可读 ——
        现象却是「这段视频没有比分牌」，把真正的病因藏得严严实实。
        复用必须**先验证**，验证不过就重新定位，定位不出就如实说没有。
        """
        import json

        from . import scoreboard_cache as sbcache
        from .scoreboard import (ScoreBugConfig, ScoreboardError,
                                 bug_looks_valid, locate_score_bug)

        cfg = self.sb_cfg or ScoreBugConfig()
        duration = (total / fps) if (fps and total) else 0.0

        def ok(bug) -> bool:
            return bug_looks_valid(self.video_path, bug, cfg)

        # 1) 本视频自己的缓存
        d = sbcache.load_bug(self.video_path, W, H, duration)
        if d:
            try:
                bug = ScoreBug.from_dict(d)
                if ok(bug):
                    self._bug_cache_source = "video_cache"
                    return bug
                self._bug_cache_source = "video_cache_rejected"
            except Exception:
                pass

        # 2) 旧版全局缓存（同机位同视频才复用）
        legacy = sbcache.legacy_path()
        if legacy.exists():
            try:
                d = json.loads(legacy.read_text(encoding="utf-8"))
                bug = ScoreBug.from_dict(d)
                inside = bool(
                    (not W or not H)
                    or (bug.x >= 0 and bug.y >= 0
                        and bug.x + bug.w <= W and bug.y + bug.h <= H))
                if inside and ok(bug):
                    self._bug_cache_source = "legacy_cache"
                    sbcache.save_bug(bug.to_dict(), self.video_path, W, H,
                                     duration)
                    return bug
                self._bug_cache_source = "legacy_cache_rejected"
            except Exception:
                pass

        # 3) 现场定位
        try:
            bug = locate_score_bug(self.video_path, cfg)
        except ScoreboardError:
            self._bug_cache_source = "not_found"
            return None
        self._bug_cache_source = "located"
        try:
            sbcache.save_bug(bug.to_dict(), self.video_path, W, H, duration)
        except Exception:
            pass
        return bug

    # ------------------------------------------------------------------
    def _hoop_sides(self) -> dict:
        return {"home": self.home_hoop,
                "away": "right" if self.home_hoop == "left" else "left"}

    def _assign_teams(self, rt: RawTrack, jersey_sum: dict) -> None:
        """用球衣颜色把球员分成主/客两队。

        **不再只按色相聚类**。老做法只取色相做一维 2 聚类，遇到两种常见情况
        必然失败：
          * 一队白/灰、一队深色 —— 白色的色相是噪声；
          * 取样窗口混进头和手臂 —— 肤色（H≈0~25）把球衣色带偏
            （实测 24 条轨迹的色相散在 15~93，聚不出两簇）。
        现在改成：
          1. 取样时剔除肤色与过暗/过曝像素，并按**众数色**（粗分箱）统计，
             而不是对整块取均值（均值会被窗口边角的背景拖走）；
          2. 对每条轨迹取众数色的 (H, S, V)；
          3. 在**色相 / 饱和度 / 明度**三个候选里，各自做一维 2 聚类，
             谁的**分离度**（簇间距 / 簇内散度）最高就用谁 —— 白队靠明度分、
             深色队靠饱和度分、彩色队靠色相分，自动选；
          4. 把选中的特征与分离度写进 meta，**分不开就如实报低置信度**，
             而不是硬分一个看着像样的结果。

        分离度 < 1.0 表示两簇几乎挨在一起，这时返回的归属只能当参考。
        """
        import numpy as _np

        def mode_color(rec: dict):
            """众数色 -> (H, S, V)。粗分箱的中心。"""
            if not rec or not rec.get("bins"):
                return None
            key, _c = max(rec["bins"].items(), key=lambda kv: kv[1])
            hb, sb, vb = key
            return (hb * 8 + 4.0, sb * 32 + 16.0, vb * 64 + 32.0)

        rows = []
        for pid, rec in jersey_sum.items():
            if not isinstance(rec, dict) or rec.get("n", 0) < 3:
                continue
            mc = mode_color(rec)
            if mc is None:
                continue
            rows.append((pid, mc[0], mc[1], mc[2]))
        if len(rows) < 4:
            return

        def flat_hue(h):
            return h + 180.0 if h < 25.0 else h

        # 三个候选特征（都先归一化到 0~1，好比较分离度）
        cands = {
            "hue": [flat_hue(r[1]) for r in rows],
            "sat": [r[2] for r in rows],
            "val": [r[3] for r in rows],
        }

        def two_means(vals):
            lo, hi = min(vals), max(vals)
            if hi - lo < 1e-6:
                return None
            c1, c2 = lo, hi
            for _ in range(15):
                g1 = [v for v in vals if abs(v - c1) <= abs(v - c2)]
                g2 = [v for v in vals if abs(v - c1) > abs(v - c2)]
                if not g1 or not g2:
                    return None
                c1, c2 = sum(g1) / len(g1), sum(g2) / len(g2)
            g1 = [v for v in vals if abs(v - c1) <= abs(v - c2)]
            g2 = [v for v in vals if abs(v - c1) > abs(v - c2)]
            import statistics as _st
            # ⚠️ 必须约束两簇人数均衡：否则 2-means 会把一个离群点单独切出来，
            # 分离度看着很高（实测 15.4），实际是 55 : 1 的退化解 ——
            # 这种"看起来分得很干净"的错结果最危险。少于 25% 直接判无效。
            n = len(g1) + len(g2)
            if min(len(g1), len(g2)) < max(2, int(0.25 * n)):
                return None
            sd = (_st.pstdev(g1) + _st.pstdev(g2)) / 2.0
            sep = abs(c1 - c2) / (sd + 1e-6)
            return c1, c2, sep

        best = None
        detail = {}
        for name, raw in cands.items():
            lo, hi = min(raw), max(raw)
            span = (hi - lo) or 1.0
            vals = [(v - lo) / span for v in raw]
            r = two_means(vals)
            if r is None:
                detail[name] = 0.0
                continue
            detail[name] = round(r[2], 2)
            if best is None or r[2] > best[1][2]:
                best = (name, r)
        if best is None:
            rt.detections_meta["teams"] = {
                "method": "none",
                "reason": ("球衣颜色分不开（三组特征都没通过均衡/分离检查）："
                           "可能是两队球衣颜色接近、画面里人太小、"
                           "或者本来就是 1v1 没有两队"),
                "candidates": detail}
            return

        feat, (c1, c2, sep) = best
        lo, hi = min(cands[feat]), max(cands[feat])
        span = (hi - lo) or 1.0

        # 谁是主队：彩色队按"折叠后低色相 = 绿系 = 主队"；
        # 白/深色队按"更亮的一队 = 主队"（主场通常穿浅色）。可用 jersey_teams 覆盖。
        invert = False
        if self.jersey_teams:
            invert = str(self.jersey_teams.get("low", "home")).lower() == "away"
        elif feat == "val":
            invert = (c1 < c2)      # 归一化后值大 = 更亮 = 主队

        counts = {"home": 0, "away": 0}
        for (pid, hh, ss, vv) in rows:
            raw = {"hue": flat_hue(hh), "sat": ss, "val": vv}[feat]
            p = (raw - lo) / span
            low = abs(p - c1) <= abs(p - c2)
            is_home = (not low) if invert else low
            rt.players[pid].team = "home" if is_home else "away"
            counts["home" if is_home else "away"] += 1
        rt.detections_meta["teams"] = {
            "method": f"color-2means:{feat}",
            "separability": round(sep, 2),
            "candidates": detail,
            "confidence": "ok" if sep >= 1.0 else "low",
            "players": len(rows), "counts": counts}

    # ------------------------------------------------------------------
    def extract_attempts(self, rt: RawTrack,
                         rim_px: float = 60.0) -> list[Attempt]:
        """把两路证据融成出手列表，并做位置归因。

        输入：rt.attempts 里已经有「比分牌给出的已命中的出手」（如果有），
              rt.ball_track 是球场坐标的球轨迹（可能很稀疏）。
        输出：rt.attempts 补全后的列表。

        规则：
          1. 比分牌给出的已命中出手 —— 分值确定、时刻确定，位置用「得分前
             2 秒内最靠近出手时段的球轨迹点」覆盖；没有球轨迹就退到篮筐占位。
          2. 球轨迹里出现「接近篮筐」但比分牌**没有**对应跳变的片段
             —— 判为打铁候选，标 needs_review 交给人工复核页确认。
             这一路置信度刻意压低，绝不混进比分统计。
        """
        fps = rt.fps or 30.0
        hoop_side = self._hoop_sides()
        rims = []
        for t, side in hoop_side.items():
            x, y = (0.0, -1.575) if side == "left" else (0.0, 1.575)
            try:
                px, py = self.cal.to_pixel(x, y)
            except Exception:
                px = py = -1e9
            rims.append((t, x, y, px, py))

        # 1) 位置归因 —— **只有标定确实适用于这段视频时才做**。
        #    标定文件是按机位存的；换一段分辨率不同的视频还硬套，
        #    算出来的球场坐标会整片错位（实测把街球场的出手算到了三分线外 24 米），
        #    看起来像真的但全是垃圾，比"没有坐标"更糟。
        cal_ok = bool(rt.detections_meta.get("calibration_valid"))
        ball = rt.ball_track if cal_ok else []
        for a in rt.attempts:
            if a.location_source == "manual":
                continue
            best = None
            for b in ball:
                if -2.0 <= b.t - a.t <= 0.6:
                    best = b
            if best is not None:
                a.x, a.y = best.x, best.y
                a.location_source = "ball_track"
                a.location_estimated = True
            # 否则保留比分牌/视觉路径给的篮筐占位坐标

        # 2) 打铁候选：球接近篮筐但比分没动
        #    「接近」要收得比较紧：这只是**候选**（没有任何「球穿筐」证据），
        #    放松了就会把「球从筐边飞过」「球被捡起来」都算成一次出手。
        #    实测 2.6m 这种松口径会在没人投篮的时刻灌进一条假出手。
        scored_ts = sorted(a.t for a in rt.attempts if a.made)
        miss_added = 0
        seen: list[float] = []
        for (team, hx, hy, px, py) in rims:
            if px < -1e8:
                continue
            near = [b for b in ball
                    if math.hypot(b.x - hx, b.y - hy) < 1.6]
            for b in near:
                if any(abs(b.t - s) < 4.0 for s in scored_ts):
                    continue
                if any(abs(b.t - s) < 6.0 for s in seen):
                    continue
                seen.append(b.t)
                rt.attempts.append(Attempt(
                    t=b.t, team=team, player_id=f"{team[0].upper()}?",
                    x=b.x, y=b.y, period=1, is_free_throw=False,
                    release_frame=int(b.t * fps), made=None, conf=0.3,
                    source="ball_track", location_source="ball_track",
                    location_estimated=True, value_assumed=True,
                    value_source="default"))
                miss_added += 1
        rt.attempts.sort(key=lambda a: a.t)
        rt.detections_meta["attempts"] = {
            "total": len(rt.attempts),
            "from_scoreboard": sum(1 for a in rt.attempts
                                   if a.source == "scoreboard"),
            "from_ball_track": sum(1 for a in rt.attempts
                                   if a.source == "ball_track"),
            "miss_candidates": miss_added}
        return rt.attempts
