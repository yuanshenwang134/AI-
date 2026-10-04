"""计分规则引擎 —— 套餐 A 的自研核心（答辩时的「增量」之一）。

职责：
  1. 判断出手点的分值 1 / 2 / 3（几何规则 + 可选的出手标签）
  2. 融合多路证据判断球是否命中（球穿筐 / 记分牌跳变 / 网动 / 人工）
  3. 从原始检测序列重建「回合（possession）」与派生统计事件

设计要点（答辩口径）：
  * 分值只看「出手瞬间脚的位置」，不看球最终落点 —— 与 NBA/FIBA 规则一致，
    也避免了球飞行轨迹不完整时的误判。
  * 命中判定用加权投票 + 置信度，低置信度事件进人工复核队列，
    而不是硬判 —— 这是「可解释、可干预」的产品差异点。
  * 所有阈值集中在 RulesConfig，方便现场调参。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from .model import (
    Event, EventType, HOOP_LEFT, HOOP_RIGHT, OutcomeSource, Player,
    Shot, ShotResult, ShotValue, THREE_PT_CORNER_INSET, THREE_PT_CORNER_X,
    THREE_PT_RADIUS, COURT_LENGTH, COURT_WIDTH,
)


# --------------------------------------------------------------------------
# 阈值配置
# --------------------------------------------------------------------------
@dataclass
class RulesConfig:
    # 三分线判定容差（米）。鞋子/脚点估计有误差，留 5cm 容差，
    # 并且「踩线即 2 分」—— 所以容差是往 2 分方向偏，即减去容差。
    three_pt_tolerance: float = 0.05
    # 二次进攻/进攻时限
    shot_clock: float = 24.0
    # 球穿筐判定的时间窗（秒）：球到达篮筐平面附近算命中
    rim_window: float = 0.45
    # 事件融合各路证据权重
    weight_ball: float = 1.0
    weight_scoreboard: float = 0.8
    weight_net: float = 0.5
    # 广播比分牌跳变：权重最高。它由导播系统直接渲染，数字零噪声、
    # 不受遮挡和光照影响，比「球穿筐」那一路还可靠。
    weight_broadcast: float = 1.2
    # 低于该置信度进入人工复核
    review_threshold: float = 0.6
    # 高光片段的默认前后留白（秒）
    clip_pad_before: float = 2.0
    clip_pad_after: float = 1.5

    # 各路证据对「命中」的投票值
    ballot_ball: float = 1.0
    ballot_scoreboard: float = 1.0
    ballot_net: float = 0.7


# --------------------------------------------------------------------------
# 1) 分值判定
# --------------------------------------------------------------------------
def nearest_hoop(x: float, y: float) -> tuple[float, float]:
    """最近的那个篮筐 —— 三分判定只针对被进攻的篮筐。

    坐标系：x 横向、y 纵向，篮筐在 (0, ±1.575)。所以「攻哪一侧」由 y 的符号决定。
    """
    return HOOP_LEFT if math.hypot(x - HOOP_LEFT[0], y - HOOP_LEFT[1]) <= \
        math.hypot(x - HOOP_RIGHT[0], y - HOOP_RIGHT[1]) else HOOP_RIGHT


def is_three_pointer(x: float, y: float, hoop: str = "auto",
                     cfg: Optional[RulesConfig] = None) -> bool:
    """判断出手点 (x, y) 是否在三分线外（FIBA 2014+ 几何）。

    坐标系：**x = 横向（宽度 ±7.5），y = 纵向（长度 ±14）**，篮筐在 (0, ±1.575)。

    三分线由两段组成，两者取「更靠外」的边界：
      * 弧线段：以**被进攻的篮筐**为圆心、半径 6.75m 的圆弧
      * 底角直线段：距边线 0.90m 的直线，即 |x| >= 7.5 - 0.90 = 6.60（横向！）

    三个关键点：
      1. **只对被进攻（更近）的那个篮筐判定**。若对两个篮筐都判，
         在靠近一侧底线处会因为「离对面篮筐很远」而被误判成三分。
      2. 底角判据用的是**横向坐标 x**（|x| >= 6.60），不是 y。
         把这条写成 |y| >= 6.60 是最容易犯的坐标系错误 ——
         那样禁区两翼离篮筐 5~6m 的出手会被误判成三分。
      3. 容差只作用在弧半径上，且**向场内收缩**（踩线算 2 分）；
         底角直线不跟着收缩，否则同样会误判禁区两翼的近距离出手。
    """
    cfg = cfg or RulesConfig()
    r = THREE_PT_RADIUS - cfg.three_pt_tolerance
    x_corner = THREE_PT_CORNER_X

    if hoop == "left":
        target = HOOP_LEFT
    elif hoop == "right":
        target = HOOP_RIGHT
    else:
        target = nearest_hoop(x, y)

    if math.hypot(x - target[0], y - target[1]) >= r:
        return True
    if abs(x) >= x_corner:
        return True
    return False


def three_corner_inset(cfg: Optional[RulesConfig] = None) -> float:
    """底角三分直线距边线的距离（FIBA 规则值 0.90m，不收容差）。"""
    return THREE_PT_CORNER_INSET


def shot_value(x: float, y: float) -> int:
    """回到 1/2 分：2 分。罚球由调用方显式指定。"""
    return ShotValue.THREE.value if is_three_pointer(x, y) else ShotValue.TWO.value


# --------------------------------------------------------------------------
# 2) 融合判定命中
# --------------------------------------------------------------------------
@dataclass
class Evidence:
    """一路证据。"""
    source: str
    made: bool
    t: float
    confidence: float = 1.0
    detail: str = ""


def fuse_outcome(evidences: Iterable[Evidence], cfg: Optional[RulesConfig] = None):
    """加权投票融合多路命中证据。

<<<<<<< HEAD
    返回 (made, confidence, source)。无有效证据或票数完全相抵时 made 为 None。
=======
    返回 (made, confidence, source)。
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    confidence = |加权票差| / 总权重，落在 [0, 1]。
    单路证据时 confidence 就是该路自身的置信度。
    """
    cfg = cfg or RulesConfig()
    wmap = {
        OutcomeSource.BALL_THROUGH_RIM.value: cfg.weight_ball,
        OutcomeSource.SCOREBOARD_OCR.value: cfg.weight_scoreboard,
        OutcomeSource.NET_MOTION.value: cfg.weight_net,
        # 广播比分牌是最硬的一路：导播系统直接渲染，不受遮挡、不受光照影响
        OutcomeSource.BROADCAST_SCOREBOARD.value: cfg.weight_broadcast,
    }
    evs = list(evidences)
    if not evs:
<<<<<<< HEAD
        return None, 0.0, OutcomeSource.SYNTHETIC.value
=======
        return False, 0.0, OutcomeSource.SYNTHETIC.value
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

    total = 0.0
    score = 0.0
    for e in evs:
        w = wmap.get(e.source, 0.5) * max(0.0, min(1.0, e.confidence))
        total += w
        if e.made:
            score += w
        else:
            score -= w

    if total <= 0:
<<<<<<< HEAD
        return None, 0.0, evs[0].source

    if abs(score) < 1e-9:
        return None, 0.0, evs[0].source
=======
        return False, 0.0, evs[0].source

>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    made = score > 0
    if len(evs) == 1:
        conf = evs[0].confidence
    else:
        conf = abs(score) / total

    # 主证据 = 权重最高的那一路
    primary = max(evs, key=lambda e: wmap.get(e.source, 0.5) * e.confidence)
    return made, round(conf, 3), primary.source


# --------------------------------------------------------------------------
# 3) 从原始检测重建出手序列
# --------------------------------------------------------------------------
@dataclass
class BallSample:
    """单帧球位置（球场坐标）。

    ``source`` 用来区分这条采样点是"球在空中飞"还是"球在谁手里"：

      * ``flight``     —— 投篮飞行段（合成数据用），会在篮筐平面上产生穿越
      * ``possession`` —— 持球 / 运球 / 传球段（合成数据用，z 恒为持球高度）
      * ``track``      —— 真视频的球轨迹（默认值）

    为什么需要这个字段：合成数据里持球段的球高度只有 1.2m，但它就停在
    篮筐正下方附近；如果不加区分，「球在手里」的下一个采样点会被
    :func:`detect_rim_events` 当成"球从筐平面掉下去了"，凭空造出一次打铁。
    真视频路径的采样点混在一起（球是会飞的），所以只在源头上区分。
    """
    t: float
    x: float
    y: float
    z: float = 1.0            # 高度（米），单目估计，缺省用经验值
    conf: float = 1.0
    source: str = "track"


@dataclass
class RimHit:
    """球到达篮筐平面的候选帧。"""
    t: float
    through: bool             # 是否从上往下穿过筐平面
    conf: float = 1.0


def detect_rim_events(samples: list[BallSample],
                      rim=(HOOP_RIGHT[0], HOOP_RIGHT[1]),
                      cfg: Optional[RulesConfig] = None) -> list[RimHit]:
    """从球轨迹里找「球进入篮筐圆柱体并向下穿过」的时刻。

    判定：水平距离 < 0.25m 且 高度由 >3.05m 下落到 <3.05m。
    这是纯几何规则，不依赖训练，是套餐 A「免训练进球判定」的兜底路径。
    """
    cfg = cfg or RulesConfig()
    hits: list[RimHit] = []
    RIM_HEIGHT = 3.05
    RIM_RADIUS = 0.25
    for a, b in zip(samples, samples[1:]):
        if b.t - a.t > 1.0:
            continue
        # 只让"飞行段"参与穿筐判定：持球段（合成数据的 possession）球高度
        # 恒定，不能代表球真的从筐上下落，否则会在篮下凭空造出打铁。
        if (getattr(a, "source", "track") == "possession"
                or getattr(b, "source", "track") == "possession"):
            continue
        near_a = math.hypot(a.x - rim[0], a.y - rim[1]) < RIM_RADIUS * 1.6
        near_b = math.hypot(b.x - rim[0], b.y - rim[1]) < RIM_RADIUS * 1.6
        if not (near_a or near_b):
            continue
        if a.z > RIM_HEIGHT > b.z:
            hits.append(RimHit(t=b.t, through=True, conf=min(a.conf, b.conf)))
        elif a.z > RIM_HEIGHT - 0.3 and b.z < RIM_HEIGHT - 0.3 and near_b:
            hits.append(RimHit(t=b.t, through=False, conf=0.3))
    # 去重：同一秒内只保留一个
    merged: list[RimHit] = []
    for h in hits:
        if merged and h.t - merged[-1].t < cfg.rim_window:
            if h.through and not merged[-1].through:
                merged[-1] = h
            continue
        merged.append(h)
    return merged


def build_shots(attempts: list[dict],
                ball_track: Optional[list[BallSample]] = None,
                scoreboard_events: Optional[list[dict]] = None,
                cfg: Optional[RulesConfig] = None) -> list[Shot]:
    """把「出手候选」+ 多路证据拼成 Shot 列表。

    attempts: [{"t":..,"team":..,"player_id":..,"x":..,"y":..,
                "period":..,"clock":..,"is_free_throw":bool,
                "release_frame":int}, ...]
    ball_track: 球轨迹（可为 None，此时只能靠记分牌/合成标签）
    scoreboard_events: [{"t":..,"team":..,"delta":2}, ...]
    """
    cfg = cfg or RulesConfig()
    rim_hits = detect_rim_events(ball_track, cfg=cfg) if ball_track else []
    sb = scoreboard_events or []
    shots: list[Shot] = []

<<<<<<< HEAD
    def match(edges):
        assigned, used = {}, set()
        for _, attempt, evidence in sorted(edges):
            if attempt not in assigned and evidence not in used:
                assigned[attempt] = evidence
                used.add(evidence)
        return assigned
    rim_matches = match([(abs(h.t-float(a['t'])), i, j)
                         for i,a in enumerate(attempts) for j,h in enumerate(rim_hits)
                         if 0 <= h.t-float(a['t']) <= cfg.rim_window*4])
    sb_matches = match([(float(event['t'])-float(a['t']), i, j)
                        for i,a in enumerate(attempts) for j,event in enumerate(sb)
                        if event.get('team') == a.get('team') and a.get('team')
                        and 0.5 <= float(event['t'])-float(a['t']) <= 4.0
                        and int(event.get('delta',0)) == (a.get('forced_value') or
                            (1 if a.get('is_free_throw') else shot_value(a['x'],a['y'])))])
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    for i, a in enumerate(attempts):
        t = float(a["t"])
        evidences: list[Evidence] = []

<<<<<<< HEAD
        # Evidence assignments are computed once, never reused by nearby attempts.
        if i in rim_matches:
            h = rim_hits[rim_matches[i]]
            evidences.append(Evidence(OutcomeSource.BALL_THROUGH_RIM.value, h.through, h.t, h.conf))
        if i in sb_matches:
            event = sb[sb_matches[i]]
            evidences.append(Evidence(OutcomeSource.SCOREBOARD_OCR.value, True, event['t'], 0.9))
        # 证据 3：先验标签（广播比分牌 / 球穿筐 / 合成数据 / 人工复核）
        if a.get("made") is not None:
            src_prior = {
                "scoreboard": OutcomeSource.BROADCAST_SCOREBOARD.value,
                "ball_rim": OutcomeSource.BALL_THROUGH_RIM.value,
                "legacy_ball_rim": OutcomeSource.BALL_THROUGH_RIM.value,
=======
        # 证据 1：球穿筐
        for h in rim_hits:
            if abs(h.t - t) <= cfg.rim_window * 4:
                evidences.append(Evidence(OutcomeSource.BALL_THROUGH_RIM.value,
                                          h.through, h.t, h.conf))
                break
        # 证据 2：记分牌跳变（出手后 0.5~4s 内）
        for s in sb:
            if s.get("team") == a.get("team") and 0.5 <= s["t"] - t <= 4.0:
                exp = 1 if a.get("is_free_throw") else shot_value(a["x"], a["y"])
                if int(s.get("delta", 0)) >= exp:
                    evidences.append(Evidence(OutcomeSource.SCOREBOARD_OCR.value,
                                              True, s["t"], 0.9))
                break
        # 证据 3：先验标签（广播比分牌 / 球穿筐 / 合成数据 / 人工复核）
        if "made" in a:
            src_prior = {
                "scoreboard": OutcomeSource.BROADCAST_SCOREBOARD.value,
                "ball_rim": OutcomeSource.BALL_THROUGH_RIM.value,
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
            }.get(a.get("source", ""), OutcomeSource.SYNTHETIC.value)
            evidences.append(Evidence(src_prior, bool(a["made"]), t,
                                      float(a.get("conf", 1.0))))

        made, conf, src = fuse_outcome(evidences, cfg)
<<<<<<< HEAD
        if a.get("manual") and a.get("made") is not None:
            made, conf, src = bool(a["made"]), 1.0, OutcomeSource.MANUAL.value
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

        # 罚球既可以由 is_free_throw 标出，也可以由 forced_value==1 标出
        # （广播比分牌给出的 +1 跳变就属于后者）
        if a.get("is_free_throw") or a.get("forced_value") == 1:
            value = ShotValue.FREE_THROW.value
        elif a.get("forced_value") in (2, 3):
            # 人工复核强制指定的分值（例如坐标不准但人工看出是三分）
            value = int(a["forced_value"])
        else:
            value = shot_value(a["x"], a["y"])

        # 人工改判的出手标记出来，战报里会体现「人工修正 N 次」
        if a.get("manual"):
            src = OutcomeSource.MANUAL.value

<<<<<<< HEAD
        tags = list(dict.fromkeys(a.get("tags") or []))
=======
        tags = []
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        if conf < cfg.review_threshold:
            tags.append("needs_review")
        if value == 3:
            tags.append("three")
        if src == OutcomeSource.MANUAL.value:
            tags.append("manual")
        if src == OutcomeSource.BROADCAST_SCOREBOARD.value:
            tags.append("scoreboard")
        if a.get("value_assumed"):
            tags.append("value_assumed")
        if a.get("value_source") == "visual_estimate":
            tags.append("value_estimated")
            if "needs_review" not in tags:
                tags.append("needs_review")
        elif a.get("value_source") == "forced":
            tags.append("value_forced")
        # 队别判不出（筐下检不到人）：得分仍然要记账，但**必须标出来** ——
        # 否则「主队 6:0」会被当成结论，其实那 6 分可能全是客队的。
        if a.get("value_source") == "team_unknown" or a.get("team_unknown"):
            tags.append("team_unknown")
            if "needs_review" not in tags:
                tags.append("needs_review")
        # 位置是占位估计（球没追到，只能放在被进攻篮筐附近）——
        # 这类出手的**比分是对的**，但热区图上的点是假的，必须标出来，
        # 否则热区会变成「所有出手都在禁区」这种误导性结论。
        if a.get("location_source") == "rim_placeholder":
            tags.append("location_unknown")
        elif a.get("location_estimated"):
            tags.append("location_estimated")
        if not bool(a.get("counts_for_score", True)):
            tags.append("scoreboard_unconfirmed")

<<<<<<< HEAD
        if made is not None:
            tags = [tag for tag in tags if tag != "outcome_unknown"]
        if src == OutcomeSource.MANUAL.value:
            tags = [tag for tag in tags if tag not in {"needs_review", "low_confidence"}]

        shots.append(Shot(
            t=t, team=a.get("team", ""), player_id=a.get("player_id", ""),
            x=float(a["x"]), y=float(a["y"]), value=value, made=bool(made),
            counts_for_score=bool(a.get("counts_for_score", True)),
            result=(ShotResult.UNKNOWN.value if made is None else
                    ShotResult.MADE.value if made else ShotResult.MISSED.value),
=======
        shots.append(Shot(
            t=t, team=a.get("team", ""), player_id=a.get("player_id", ""),
            x=float(a["x"]), y=float(a["y"]), value=value, made=made,
            counts_for_score=bool(a.get("counts_for_score", True)),
            result=ShotResult.MADE.value if made else ShotResult.MISSED.value,
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
            period=int(a.get("period", 1)), clock=float(a.get("clock", 0.0)),
            contest=float(a.get("contest", 0.0)),
            outcome_source=src, confidence=conf,
            release_frame=int(a.get("release_frame", 0)),
            clip_start=max(0.0, t - cfg.clip_pad_before),
<<<<<<< HEAD
            clip_end=max(t + cfg.clip_pad_after, float(a.get("clip_end") or 0)),
            tags=list(dict.fromkeys(tags)),
            evidence=a.get("evidence", ""),
            crossing_t=a.get("crossing_t"), review_t=a.get("review_t"),
            decision_t=a.get("decision_t"), release_source=a.get("release_source", ""),
            suggested_made=a.get("suggested_made"),
=======
            clip_end=t + cfg.clip_pad_after,
            tags=tags,
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        ))
    return shots


# --------------------------------------------------------------------------
# 4) 派生统计
# --------------------------------------------------------------------------
@dataclass
class TeamStats:
    team: str
    points: int = 0
    fgm: int = 0
    fga: int = 0
    tpm: int = 0
    tpa: int = 0
    ftm: int = 0
    fta: int = 0
<<<<<<< HEAD
    unknown: int = 0
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

    @property
    def fg_pct(self) -> float:
        return round(self.fgm / self.fga, 3) if self.fga else 0.0

    @property
    def tp_pct(self) -> float:
        return round(self.tpm / self.tpa, 3) if self.tpa else 0.0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["fg_pct"] = self.fg_pct
        d["tp_pct"] = self.tp_pct
        return d


def compute_team_stats(shots: list[Shot], team: str) -> TeamStats:
    st = TeamStats(team=team)
    for s in shots:
        if s.team != team:
            continue
<<<<<<< HEAD
        if s.result == ShotResult.UNKNOWN.value:
            st.unknown += 1
            continue
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        st.points += s.score_points
        if s.value == ShotValue.FREE_THROW.value:
            st.fta += 1
            st.ftm += 1 if s.made else 0
        else:
            st.fga += 1
            st.fgm += 1 if s.made else 0
            if s.value == ShotValue.THREE.value:
                st.tpa += 1
                st.tpm += 1 if s.made else 0
    return st


def compute_player_stats(shots: list[Shot],
                         players: Optional[dict[str, Player]] = None,
                         events: Optional[list[Event]] = None) -> list[dict]:
    """球员统计：得分 / 出手 / 命中率 / 三分 / 罚球 + 事件类统计。

    events 里承载 rebound/assist/steal/block/turnover/foul（这些在套餐 A 里
    用「跟踪 + 规则」或人工补录得到，属于可选项）。
    """
    players = players or {}
    rows: dict[str, dict] = {}

    def row(pid: str, team: str) -> dict:
        if pid not in rows:
            p = players.get(pid)
<<<<<<< HEAD
            if p and p.team in ("home", "away") and p.team != team:
                raise ValueError(f"球员 {pid} 队别冲突：球员表 {p.team}，事件 {team}")
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
            rows[pid] = {
                "player_id": pid,
                "name": p.name if p else pid,
                "jersey": p.jersey if p else None,
                "team": p.team if p else team,
                "points": 0, "fgm": 0, "fga": 0, "tpm": 0, "tpa": 0,
                "ftm": 0, "fta": 0,
                "reb": 0, "ast": 0, "stl": 0, "blk": 0, "tov": 0, "pf": 0,
<<<<<<< HEAD
                "shots": [], "unknown": 0,
            }
        if rows[pid]["team"] != team:
            raise ValueError(f"球员 {pid} 在事件中存在冲突队别")
=======
                "shots": [],
            }
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        return rows[pid]

    for s in shots:
        r = row(s.player_id, s.team)
<<<<<<< HEAD
        if s.result == ShotResult.UNKNOWN.value:
            r["unknown"] += 1
            continue
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        r["points"] += s.score_points
        if s.value == ShotValue.FREE_THROW.value:
            r["fta"] += 1
            r["ftm"] += 1 if s.made else 0
        else:
            r["fga"] += 1
            r["fgm"] += 1 if s.made else 0
            if s.value == ShotValue.THREE.value:
                r["tpa"] += 1
                r["tpm"] += 1 if s.made else 0
        r["shots"].append({"t": s.t, "x": s.x, "y": s.y,
                           "made": s.made, "value": s.value})

    for e in (events or []):
        if not e.player_id:
            continue
        r = row(e.player_id, e.team)
        key = {"rebound": "reb", "assist": "ast", "steal": "stl",
               "block": "blk", "turnover": "tov", "foul": "pf"}.get(e.type)
        if key:
            r[key] += 1

    for r in rows.values():
        r["fg_pct"] = round(r["fgm"] / r["fga"], 3) if r["fga"] else 0.0
        r["tp_pct"] = round(r["tpm"] / r["tpa"], 3) if r["tpa"] else 0.0
        r["ft_pct"] = round(r["ftm"] / r["fta"], 3) if r["fta"] else 0.0
        r["efg"] = round((r["fgm"] + 0.5 * r["tpm"]) / r["fga"], 3) if r["fga"] else 0.0
        r["ts"] = round(r["points"] / (2 * (r["fga"] + 0.44 * r["fta"])), 3) \
            if (r["fga"] + 0.44 * r["fta"]) else 0.0
    return sorted(rows.values(), key=lambda r: (-r["points"], -r["fga"]))


# --------------------------------------------------------------------------
# 5) 投篮热区
# --------------------------------------------------------------------------
# 半场分区（距篮筐距离带 + 左右 + 底角），共 15 区
ZONE_DEFS = [
    ("paint", 0.0, 4.0, "禁区"),
    ("short_mid", 4.0, 5.5, "近距离中投"),
    ("long_mid", 5.5, 6.75, "长两分"),
    ("corner3", 6.75, 99.0, "底角三分"),
    ("wing3", 6.75, 99.0, "45°三分"),
    ("top3", 6.75, 99.0, "弧顶三分"),
]


# 距「被进攻篮筐」的距离带 -> 中文分区名
# 说明：FIBA 的禁区（限制区）是罚球线到底线 5.80m，所以 5.8~6.75m 这一带
# 既可能在禁区内侧也可能是禁区外一点点。为了口径简单、观众一眼能懂，
# 这里按「距篮距离」切三档，再按横向位置细分三分区，不做禁区矩形判断。
ZONE_BANDS = [
    (0.0, 4.0, "禁区"),        # 上篮/篮下终结
    (4.0, 5.8, "近距离中投"),   # 罚球线一带（FIBA 罚球线距底线 5.80m）
    (5.8, 99.0, "长两分"),      # 5.8m 到三分线之间
]


def zone_of(x: float, y: float) -> str:
    """把出手点映射到中文分区名 —— 直接给前端热区图和战报用。

    坐标系：x 横向、y 纵向，篮筐在 (0, ±1.575)。
    分区口径：以「被进攻篮筐」为准，先按距离切档，再按横向位置细分三分区。
    """
    hx, hy = nearest_hoop(x, y)
    dist = math.hypot(x - hx, y - hy)

    if is_three_pointer(x, y):
        # 底角三分：贴边线（横向靠外）；弧顶：靠近中线（横向靠中）
        if abs(x) >= THREE_PT_CORNER_X:
            return "底角三分"
        if abs(x) >= 4.0:
            return "45°三分"
        return "弧顶三分"

    for lo, hi, name in ZONE_BANDS:
        if dist < hi:
            return name
    return "长两分"


def shot_chart(shots: list[Shot], team: Optional[str] = None,
               bin_size: float = 1.0) -> dict:
    """返回三份数据：
      points   —— 每个出手点（前端散点图 + tooltip）
      zones    —— 分区聚合（前端堆叠柱/表）
      grid     —— 半场网格热力（前端 heatmap 或 canvas 绘制）
    """
    if team:
        shots = [s for s in shots if s.team == team]

<<<<<<< HEAD
    shots = [s for s in shots if s.result != ShotResult.UNKNOWN.value
             and "location_unknown" not in s.tags]
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    points = [{"x": s.x, "y": s.y, "made": s.made, "value": s.value,
               "t": s.t, "player_id": s.player_id, "zone": zone_of(s.x, s.y),
               "distance": round(s.distance, 2)} for s in shots]

    zones: dict[str, dict] = {}
    for s in shots:
        z = zone_of(s.x, s.y)
        d = zones.setdefault(z, {"zone": z, "att": 0, "made": 0, "points": 0})
        d["att"] += 1
        d["made"] += 1 if s.made else 0
        d["points"] += s.score_points
    for d in zones.values():
        d["pct"] = round(d["made"] / d["att"], 3) if d["att"] else 0.0
        d["pps"] = round(d["points"] / d["att"], 2) if d["att"] else 0.0

    # 网格：把出手折到「右半场」（y > 0 那边），再按 横向×纵向 建格。
    # 坐标系 x 横向(宽 15m)、y 纵向(半场长 14m)，与前端半场绘制一致。
    nx = int(COURT_WIDTH / bin_size)          # 15 格（横向）
    ny = int((COURT_LENGTH / 2) / bin_size)   # 14 格（纵向半场）
    grid = [[{"att": 0, "made": 0} for _ in range(nx)] for _ in range(ny)]
    for s in shots:
        # 镜像到右半场：攻左侧(y<0)时把 y 取反，x 也取反保持手性
        y = s.y if s.y >= 0 else -s.y
        x = s.x if s.y >= 0 else -s.x
        ix = int((x + COURT_WIDTH / 2) / bin_size)
        iy = int(y / bin_size)
        if 0 <= ix < nx and 0 <= iy < ny:
            grid[iy][ix]["att"] += 1
            grid[iy][ix]["made"] += 1 if s.made else 0

    return {"points": points,
            "zones": sorted(zones.values(), key=lambda d: -d["att"]),
            "grid": grid,
            "bin_size": bin_size}


# --------------------------------------------------------------------------
# 6) 事件融合：把细粒度检测合成「事件流」
# --------------------------------------------------------------------------
def possessions(shots: list[Shot], gap: float = 6.0) -> list[dict]:
    """按时间间隔切分回合 —— 用于战报叙事和节奏统计。"""
    s = sorted(shots, key=lambda x: x.t)
    out: list[dict] = []
    for i, sh in enumerate(s):
        if i == 0 or sh.t - s[i - 1].t > gap or sh.team != s[i - 1].team:
            out.append({"start": sh.t, "team": sh.team, "shots": [sh]})
        else:
            out[-1]["shots"].append(sh)
    return out


def score_progression(shots: list[Shot]) -> list[dict]:
    """比分随时间的变化 —— 前端时间轴 / 走势图直接用。"""
    s = sorted(shots, key=lambda x: x.t)
    home = away = 0
    out = [{"t": 0.0, "home": 0, "away": 0}]
    for sh in s:
        if sh.team == "home":
            home += sh.score_points
        elif sh.team == "away":
            away += sh.score_points
        out.append({"t": round(sh.t, 2), "home": home, "away": away,
                    "scorer": sh.player_id, "value": sh.value, "made": sh.made})
    return out


def quarter_scores(shots: list[Shot], periods: int = 4) -> list[dict]:
    out = []
    for p in range(1, periods + 1):
        ps = [s for s in shots if s.period == p]
        out.append({
            "period": p,
            "home": sum(s.score_points for s in ps if s.team == "home"),
            "away": sum(s.score_points for s in ps if s.team == "away"),
        })
    return out
