"""数据模型 —— 事件流 / 战术流的契约。

这是「算法层」与「后端/前端」之间唯一的强契约层。
只要你产出的 events.jsonl / tactics.json 符合这里的 schema，前后端不用改。

坐标系（全工程唯一约定，务必不要颠倒）：
  * 单位：米
  * **x = 横向**，范围 [-7.5, 7.5]（FIBA 球场宽 15m），x = 0 是球场中轴
  * **y = 纵向**：**符号表示在哪半场，|y| 是「离本方底线的距离」**
        y > 0 → 右半场（攻右篮筐）；y < 0 → 左半场（攻左篮筐）
        |y| = 0 是底线，|y| = 14 是中圈 —— 所以 |y| 越大越靠近**中场**，
        **不是**越靠近底线
  * 篮筐在 (0, ±1.575)：左篮筐 (0, -1.575)，右篮筐 (0, +1.575)。
        1.575m 正是 FIBA 规定的「篮筐圆心距底线」—— 只有把 |y| 定义成
        「离底线的距离」，这个常数才对得上。
  * **没有"球场中心原点"**：这是一套**折半坐标** —— 两个半场叠在一起，
    两条底线都是 |y| = 0。好处是热区图 / 分区统计 / 俯视战术图都只需要
    画一套半场，左右两侧的出手天然对齐，两队也能直接叠着比较。
  * 全场标定（`frame="full"`）用的是未折半坐标（两条底线在 y=±14、中线在 0），
    必须先经 court.fold_to_analysis() 折半再进下游 —— 这一步已经封装在
    Calibration.to_court() 里，业务代码不该直接拿 cal.H 自己算。

  三分线：以篮筐为圆心、半径 6.75m 的圆弧，加上距边线 0.90m 的底角直线。
  底角直线条件因此是 |x| >= 7.5 - 0.90 = 6.60（横向！）。

  ⚠️ 这套约定修过一次真实的 bug：老版本的半场标定点把底线放在 |y|=14、
     中线放在 |y|=0，与计分引擎 / 热区网格 / 前端 court.js 完全相反。
     后果是篮下上篮被算成「离篮筐 12.4m」判成三分，俯视战术图整体偏移 14m，
     而且**不报任何错**。回归测试：
     tests/test_plan_b.py::test_calibration_layup_is_two_points
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------
# FIBA 2014+ 标准尺寸（米）
# --------------------------------------------------------------------------
COURT_LENGTH = 28.0
COURT_WIDTH = 15.0
HOOP_CENTER_X = 1.575          # 篮筐圆心距底线（纵向偏移量的绝对值）
# 篮筐位于中线上，纵向偏移 ±1.575
HOOP_LEFT = (0.0, -HOOP_CENTER_X)
HOOP_RIGHT = (0.0, HOOP_CENTER_X)
THREE_PT_RADIUS = 6.75         # 三分线弧半径
THREE_PT_CORNER_INSET = 0.90   # 底角三分直线距边线
THREE_PT_CORNER_X = 6.60       # = COURT_WIDTH/2 - 0.90，底角直线所在的 |x| 阈值
RESTRICTED_AREA_RADIUS = 1.25  # 合理冲撞区
KEY_WIDTH = 4.90               # 罚球区宽
KEY_LENGTH = 5.80              # 罚球线距底线
FREE_THROW_CIRCLE_RADIUS = 1.80
BACKBOARD_FROM_BASELINE = 1.20


class EventType(str, Enum):
    SHOT = "shot"              # 一次出手（含结果）
    REBOUND = "rebound"
    ASSIST = "assist"
    STEAL = "steal"
    BLOCK = "block"
    TURNOVER = "turnover"
    FOUL = "foul"
    SUBSTITUTION = "substitution"
    PERIOD_START = "period_start"
    PERIOD_END = "period_end"


class ShotResult(str, Enum):
    MADE = "made"
    MISSED = "missed"
    UNKNOWN = "unknown"


class ShotValue(int, Enum):
    FREE_THROW = 1
    TWO = 2
    THREE = 3


class OutcomeSource(str, Enum):
    """进球结论来自哪一路证据 —— 答辩时这是「事件融合」的抓手。"""
    BALL_THROUGH_RIM = "ball_through_rim"      # 球轨迹穿过篮筐平面
    SCOREBOARD_OCR = "scoreboard_ocr"          # 记分牌数字跳变
    NET_MOTION = "net_motion"                  # 网/篮筐震动
    MANUAL = "manual"                          # 人工在复核页修正
    SYNTHETIC = "synthetic"                    # 合成数据（demo 用）
    # 画面里的**广播比分牌**直接读出来的比分跳变。
    # 它和 SCOREBOARD_OCR（场边 LED 记分牌）不是一回事：广播台标由导播系统
    # 直接渲染，数字零噪声、不受遮挡，是自动推理里最硬的一路证据。
    BROADCAST_SCOREBOARD = "broadcast_scoreboard"


@dataclass
class Shot:
    """一次出手的完整记录。"""
    t: float                              # 出手时刻（秒）
    team: str                             # "home" / "away"
    player_id: str
    x: float = 0.0                        # 出手点球场坐标（米）
    y: float = 0.0
    value: int = 2                        # 1 / 2 / 3
    made: bool = False
    counts_for_score: bool = True         # False=只是视觉命中，不计入比分（如回放/比分牌未确认）
    result: str = ShotResult.MISSED.value
    period: int = 1
    clock: float = 0.0                    # 该节剩余时间（秒）
    contest: float = 0.0                  # 防守干扰强度 0~1（可选）
    outcome_source: str = OutcomeSource.SYNTHETIC.value
    confidence: float = 1.0
    release_frame: int = 0
    rim_frame: Optional[int] = None
    clip_start: float = 0.0
    clip_end: float = 0.0
    tags: list[str] = field(default_factory=list)
<<<<<<< HEAD
    evidence: str = ""
    crossing_t: Optional[float] = None
    review_t: Optional[float] = None  # suggested review instant; not a measured crossing
    decision_t: Optional[float] = None
    release_source: str = ""
    suggested_made: Optional[bool] = None
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

    @property
    def points(self) -> int:
        """这次出手本身的得分（不管是否计入总分）。"""
        return self.value if self.made else 0

    @property
    def score_points(self) -> int:
        """计入总分的分数；视觉未确认命中会是 0。"""
        return self.value if (self.made and self.counts_for_score) else 0

    @property
<<<<<<< HEAD
    def location_known(self) -> bool:
        """出手位置是不是**真实测量**出来的。

        旧引擎没有球场标定时给的是占位坐标（`(0, -1.575)`，贴着被进攻篮筐），
        `rules.build_shots` 会给这类出手打上 `location_unknown` 标签。
        凡是拿 `x/y` 去算距离、分区、热区的输出，都必须先问这个属性 ——
        否则导出的 CSV、战报 JSON、热区都会变成"所有出手都在禁区 0 米"的假数据。
        """
        return "location_unknown" not in self.tags

    @property
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    def distance(self) -> float:
        """出手点到最近篮筐的距离（米）。坐标系见模块开头。"""
        d_left = ((self.x - HOOP_LEFT[0]) ** 2 + (self.y - HOOP_LEFT[1]) ** 2) ** 0.5
        d_right = ((self.x - HOOP_RIGHT[0]) ** 2 + (self.y - HOOP_RIGHT[1]) ** 2) ** 0.5
        return min(d_left, d_right)

    def to_dict(self) -> dict:
        d = asdict(self)
<<<<<<< HEAD
        if self.result == ShotResult.UNKNOWN.value:
            d["made"] = None
        d["points"] = self.points
        d["score_points"] = self.score_points
        d["distance"] = None if not self.location_known else round(self.distance, 3)
=======
        d["points"] = self.points
        d["score_points"] = self.score_points
        d["distance"] = round(self.distance, 3)
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        return d


@dataclass
class Event:
    """事件流里的一行。shots 以外的统计事件用它承载。"""
    t: float
    type: str
    team: str = ""
    player_id: str = ""
    period: int = 1
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"t": self.t, "type": self.type, "team": self.team,
             "player_id": self.player_id, "period": self.period}
        d.update(self.extra)
        return d


@dataclass
class Player:
    player_id: str
    name: str
    team: str
    jersey: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def write_jsonl(path: str, records: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_dict() if hasattr(r, "to_dict") else r,
                               ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
