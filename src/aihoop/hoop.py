"""篮筐自动识别 + 出手/进球判定（像素空间，不依赖球场标定）。

**为什么需要这个模块：**

  `scoreboard.py` 那条路依赖画面里有广播比分牌覆盖层。但大量真实素材
  —— 手机拍的野球场、训练视频、单机位录像 —— **根本没有比分牌**。
  这时候唯一的证据就是球本身：球飞向篮筐、球穿过篮筐。

  这一层做的事就是「用球和篮筐把比分数出来」，全程在**像素空间**完成，
  不需要单应矩阵，也就不受「单目没法测球高度」这个限制的拖累 ——
  判进球不需要知道球离地几米，只需要知道它**从篮筐平面上方落到了下方**。

**为什么不能靠「静止的橙色像素」找篮圈：**

  我先试了「橙色像素在多数帧里出现 = 静止物体 = 篮筐」，结果失败了。
  因为这类视频大多是**手持拍摄**，画面整体在抖，同一个物理点在不同帧
  落在不同像素上，逐像素的时间频率根本攒不起来（实测篮圈区域的橙色
  频率最高只有 0.39）。

  所以改成**逐帧独立检测 + 跨帧取中位数**：单帧里篮圈就是一个橙色横椭圆、
  篮板就是一块高亮白矩形，在夜空背景下都很显眼；检测会抖，但中位数稳。

**判定口径（全部只用图像坐标）：**

  出手      一条轨迹出现「明显上升段」，且上升段之后落点在篮筐水平范围内
  命中      轨迹**向下穿过篮筐所在的那条水平线**，且穿越点的 x 落在篮筐内
  不中      连续下降穿过篮筐高度，交点明确偏出篮圈
  待确认    飞出画面、被遮挡或篮圈边缘证据不足

  穿越点用相邻两帧线性插值算 —— 30fps 下球穿过篮筐往往只有 1~2 帧，
  不插值就会漏掉大量进球。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence


def _require_cv():
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "篮筐识别需要 opencv-python 与 numpy。"
            "安装：pip install -r requirements-full.txt") from e
    return cv2, np


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class HoopConfig:
    """篮筐检测 + 出手判定的阈值。"""

    # ---- 篮板：夜里是一块高亮低饱和的矩形 ----
    board_v_min: int = 185
    board_s_max: int = 75
    board_min_area: int = 120
    search_top: float = 0.60        # 只在上半部分找篮筐
    board_min_aspect: float = 0.35  # 篮板大致是竖着的矩形
    board_max_aspect: float = 2.2

    # ---- 篮圈：红色/橙色横椭圆 ----
    # 同样要覆盖色相环的两端：球馆里红漆篮圈常常落在红端（H≈170+），
    # 只取橙端 [4,26] 会一个篮圈都找不到（实测踩过）。
    orange_lo: tuple = (4, 110, 110)
    orange_hi: tuple = (26, 255, 255)
    orange_lo2: tuple = (166, 90, 80)
    orange_hi2: tuple = (180, 255, 255)
    # 空心度区间：篮筐是环（填充度低），球衣是实心块（填充度高）。
    # 这两个值是区分"篮筐 vs 橙色球衣"的关键闸门。
    rim_min_fill: float = 0.12
    rim_max_fill: float = 0.62
    rim_min_w: int = 14
    rim_max_w: int = 170
    rim_min_aspect: float = 1.15    # 宽度 / 高度
    rim_max_aspect: float = 9.0
    rim_below_board: float = 0.25   # 篮圈中心允许在篮板底边以下多少倍板高
    rim_above_board_top: float = 0.15

    # ---- 检测用的抽样 ----
    detect_frames: int = 150
    detect_stride: int = 6          # 每 6 帧采一次篮筐（5Hz，够跟上摇摄）
    min_rim_votes: int = 5

    # ---- 出手 / 进球判定 ----
    min_track_pts: int = 6
    min_rise_px: float = 30.0       # 上升段至少要抬这么高
    min_rise_speed: float = 60.0    # 上升速度下限（像素/秒）
    rim_x_tol: float = 1.9          # 出手落点允许偏离篮圈中心多少倍半径
    rim_inner: float = 0.85         # 穿越点落在篮圈内圈的判定系数
    # 只对短间隔做穿越插值；轨迹中间断了一大截时线性插值会把穿越点算飞
    cross_max_gap_s: float = 0.15
    # 穿越时的下落速度上限（像素/秒）。物理约束：球在画面里下落不可能比这更快，
    # 超过就说明这是插值插出来的假穿越 —— 用来兜住「轨迹有空洞时插值算飞了」。
    max_cross_speed_px: float = 1200.0
    # 轨迹有检测空洞时，穿越点必须落得更靠筐心才采信（越小越严）。
    hole_rim_tighten: float = 0.8
    # 是否允许「跨越检测空洞」的插值判进。默认**关**（与既有的严格行为一致，
    # 见 tests/test_shot_visibility.py 里"短遮挡不许插值出进球"那条）。
    # 为什么把它做成开关而不是直接放开：实测一段罚球素材，5 个进球里有 1 个
    # 正好卡在这条上 —— 球在篮网后漏检 0.43s，两端点连起来下落 62px、
    # 穿越点离筐心 18px（筐半径 31px），人工真值是"进"。放开能把召回从
    # 4/5 提到 5/5，代价是理论上多一类"靠插值凑出来"的进球。
    # 这是召回率 vs 精确率的取舍，需要人来定，所以留开关、默认不开。
    allow_hole_interpolation: bool = False
    max_track_gap_s: float = 0.6   # 长遮挡后的球不能直接当作同一次飞行
    min_shot_gap_s: float = 0.5     # 两次出手之间的最小间隔（去重；集锦里可能连续有进球）
    # 机位允许的移动幅度（占画面宽度的比例）。
    # 手持/摇摄下篮筐漂移几十像素是正常的；但超过这个比例就说明镜头在
    # 大幅摇摄或切镜头（例如 NBA 转播的跟球镜头），这时「篮筐」在画面里
    # 根本不是一个固定目标，检测会乱跳，「球从篮筐上方落到下方」这套判据
    # 也就不成立了。**这种情况必须直接放弃，而不是硬判** ——
    # 实测一段 8 秒 NBA 转播里篮筐检测漂移了 1198 像素（画面宽度 1280），
    # 系统却报出「4 次出手 3 个三分」，全是垃圾。
    max_drift_frac: float = 0.22

    # ---- 鲁棒篮筐跟踪 / 防离群 ----
    cluster_gate_px: float = 70.0    # 初始聚类时多少像素内算同一个篮筐
    max_step_px: float = 150.0       # 相邻采样之间篮筐允许移动的上限
    min_segment_votes: int = 3       # 一个镜头段至少要有这么多帧支持

    # ---- 进球判定精度 ----
    max_cross_gap_s: float = 0.6      # 穿过篮筐平面上下的最大时间间隔
    min_cross_drop_frac: float = 1.0  # 最小下落距离（篮圈半高 ry 的倍数）
    # **米制下限**：除了"ry 的倍数"，再要求下落距离 ≥ 这么多米。
    # 为什么必须有：`ry` 是篮圈在**视向方向**上的像素尺度，篮圈接近正侧视时
    # 它趋于 0（实测手标薄筐 ry 只有 3.9px，物理上约 4cm），于是"必须下落 1 倍 ry"
    # 这条门槛实际只要求下落 3.9 像素 —— 同一段素材里球一帧就能走 10 像素，
    # 门槛随标注方式漂移，这正是"出口证据差 0.16 像素就判不出来"的根因。
    # 米制下限与标注无关：用篮板高（真实 1.05m，若检测到篮板）或篮圈半径
    # （0.225m）反算"篮筐处 1 米 ≈ 多少像素"，再要求下落 ≥ 0.10m（约球半径 0.12m
    # 的保守版）。它只会**收紧**门槛，不会放宽，因此不会新增误报。
    min_cross_drop_m: float = 0.10
    min_cross_speed_px: float = 25.0  # 穿越时的最小下落速度（像素/秒）
    # 轨迹在筐口**断掉**（球被网/板/人挡住）时，是否允许用**趋势外推**补一次穿越判定。
    # 默认关：与 allow_hole_interpolation 一样属于策略开关，见 tests/test_shot_visibility.py
    # 里"短遮挡不许插值出进球"那条。打开后仍有独立护栏（见 extrap_rim_inner、
    # extrap_max_bracket_s、extrap_min_pts），并且只对"更靠近筐心"的落点放行。
    # 实测价值：一段罚球素材里 5 个进球被判出 4 个，漏的那个正是"球在网后漏检 0.43s"，
    # 打开后 5/5（A/B 见 docs/开源调研_提高投篮识别率_2026-09-24.md）。
    allow_extrapolated_crossing: bool = False
    extrap_rim_inner: float = 0.6      # 外推档的横向门槛（× rx，比正常档 0.85 更严）
                                       # 注意：与球净空门槛取**更严者**（0.467×rx），
                                       # 所以默认配置下真正生效的是净空那条。
    extrap_max_bracket_s: float = 0.6  # 外推档允许的"上→下"总时长上限
    extrap_min_pts: int = 3            # 外推至少要几个点才拟合
    # 篮筐位置本身的不确定度（像素，横向）。None = 自动取 HoopTrack 在**该时刻附近**
    # 的局部不确定度（原始样本相对中值滤波轨迹的残差），手工标点（常数 Hoop）按 0。
    hoop_uncertainty_px: Optional[float] = None
    # 局部不确定度的统计窗口（秒）。
    hoop_uncertainty_window_s: float = 1.5
    # 不确定度达到篮圈半径的这么多倍时，**不再判进也不判不中**，只报"结果未知"。
    hoop_uncertainty_max_frac: float = 0.5
    # ---- 球不是质点：净空判据 ----
    # 球要**干净穿过**篮圈，球心必须落在 (篮圈内半径 - 球半径) 以内，也就是
    #   偏移 ≤ (1 - 球直径/篮圈内径) × rx。
    # 取 FIBA 尺寸（球 0.24m、篮圈内径 0.45m）→ 0.467 × rx，比原来的 rim_inner=0.85
    # 严得多。为什么必须这样：实测 nathan 3.40s 那一球，球心相对**当时**筐心偏移
    # 0.63×rx，球半径 16px / 筐半径 29px ≈ 0.55 —— 球体在**图像平面**上已经和篮圈
    # 重叠（人工复核："贴着筐沿/网外掉下去"，即不中），而 0.85 的门槛把它判成了
    # "穿过筐心"。**措辞注意**：这里是图像平面的保守判据，单目投影下"像面重叠"
    # 既不等于也不排除三维接触，所以结论只能是"不足以支撑干净穿筐"，不能断言
    # "一定碰到了筐"（队友侧在复核页文案上纠正过这一点，我同意）。
    # 实测比值 16/29 = 0.55 与 0.24/0.45 = 0.53 一致，所以用物理常数而不是噪声更大的
    # 逐帧框宽。见 docs/假进球根因_球净空判据_2026-09-27.md（根因）与
    # docs/假进球_nathan3.4s_2026-09-26.md（首次报告，其根因段已被更正）
    ball_diameter_m: float = 0.24
    rim_diameter_m: float = 0.45
    max_track_turns: int = 12         # 轨迹方向变化上限，过滤噪声轨迹
    # 方向变化**速率**上限（次/秒）。为什么还需要它：球在整段素材里常常是
    # 连续被检出的（运球、捡球、出手是同一条轨迹），一条 10 秒的轨迹光运球
    # 就能攒出十几次反转 —— 用"次数"当闸门会把真进球整条丢掉（实测一段
    # 罚球素材 5 个进球全漏，而球每帧都被检出、置信度 1.0）。
    # 噪声轨迹的特征是**又短又抖**（反转次数/秒极高），所以按速率判才对。
    max_track_turn_rate: float = 6.0
    # 把"球在最低点"（y 由增转减）当作切点，把一条长轨迹切成
    # 「上升→顶点→下落」的若干条弧线。一次出手 = 一条弧线。
    arc_split_min_px: float = 6.0     # 低于该幅度的来回抖动不算一次弧线
    # 统计轨迹方向变化时忽略的小抖动幅度（像素）。球在篮筐附近会被检出
    # 一串几像素的上下抖动，不能算成真实的转向，否则真进球会被过滤掉。
    track_turn_min_amp_px: float = 4.0
    # 篮筐轨迹的中值滤波半径（单位：采样点，约每 0.2s 一个点）。
    # 球穿过篮筐时会被误检成篮圈，单点离群值会把判进球的篮筐中心拽偏。
    hoop_smooth_window: int = 2


# --------------------------------------------------------------------------
# 篮筐
# --------------------------------------------------------------------------
@dataclass
class Hoop:
    """篮筐在画面里的位置。中心 + 篮圈的半宽/半高（像素）。"""

    cx: float
    cy: float
    rx: float
    ry: float
    board: Optional[list] = None      # 篮板框 [x, y, w, h]
    votes: int = 0
    confidence: float = 0.0
    method: str = ""
    # 人工标这个篮筐时的**时刻**（秒）。为什么需要它：篮筐像素是"画面里的一个点"，
    # 镜头一动它就换位置 —— 拿它当独立真值去校验球场标定时，必须用**同一时刻**的
    # 单应矩阵，否则等于拿两个不同视角的东西对比（实测这段素材镜头一直在跟球）。
    t: float = 0.0

    def to_dict(self) -> dict:
        return {"cx": round(self.cx, 1), "cy": round(self.cy, 1),
                "rx": round(self.rx, 1), "ry": round(self.ry, 1),
                "board": self.board, "votes": self.votes,
                "confidence": round(self.confidence, 3),
                "method": self.method, "t": round(float(self.t or 0.0), 2)}

    @staticmethod
    def from_dict(d: dict) -> "Hoop":
        return Hoop(cx=d["cx"], cy=d["cy"], rx=d["rx"], ry=d["ry"],
                    board=d.get("board"), votes=d.get("votes", 0),
                    confidence=d.get("confidence", 0.0),
                    method=d.get("method", ""), t=float(d.get("t", 0.0) or 0.0))


def _all_rim_candidates(frame, cfg: HoopConfig):
    """单帧里找（篮板，所有像篮圈的候选）。

    返回 (board, [rim_box, ...])，rim 按得分从高到低排序。
    旧实现只返回得分最高的一个；遇到橙色干扰物时，真篮筐会被挤掉。
    现在把所有候选交给 detect_hoop_track() 做时间一致性筛选。
    """
    cv2, np = _require_cv()
    H, W = frame.shape[:2]
    ylim = int(H * cfg.search_top)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    # ---- 篮板（best-effort，只用于展示/弱校验）----
    board = None
    bm = ((hsv[:, :, 2] > cfg.board_v_min) &
          (hsv[:, :, 1] < cfg.board_s_max)).astype(np.uint8)
    bm[ylim:, :] = 0
    cnts, _ = cv2.findContours(bm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        a = cv2.contourArea(c)
        if a < cfg.board_min_area:
            continue
        x, y, w, h = cv2.boundingRect(c)
        if h < 8 or w < 6:
            continue
        if a / float(w * h) < 0.35:
            continue
        ar = w / float(h)
        if not (cfg.board_min_aspect <= ar <= cfg.board_max_aspect):
            continue
        if best is None or a > best[0]:
            best = (a, (x, y, w, h))
    if best is not None:
        board = list(best[1])

    # ---- 篮圈：橙色 + 又宽又扁 ----
    om = cv2.inRange(hsv, cfg.orange_lo, cfg.orange_hi)
    om |= cv2.inRange(hsv, cfg.orange_lo2, cfg.orange_hi2)
    om[ylim:, :] = 0
    om = cv2.morphologyEx(om, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(om, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    rims = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        a = cv2.contourArea(c)
        if not (cfg.rim_min_w <= w <= cfg.rim_max_w) or h < 3:
            continue
        ar = w / float(h)
        if not (cfg.rim_min_aspect <= ar <= cfg.rim_max_aspect):
            continue
        # ---- 空心度：篮筐是个「环」，中间是空的；球衣是实心块 ----
        # 这是区分"篮筐"和"橙色球衣"最关键的一条，而原来的判据
        # （score = 面积 × 扁度）恰恰只奖励实心大块 —— 球衣天生占优，
        # 所以黄色/橙色球衣总被当成篮筐（实测把人家球衣认成了篮筐）。
        # 用「轮廓面积 / 外接矩形面积」衡量填充度：
        #   实心块 ≈ 0.65~0.85（受形状影响）
        #   环      ≈ 0.15~0.55（中间被挖掉一块）
        fill = a / float(max(1, w * h))
        if fill > cfg.rim_max_fill or fill < cfg.rim_min_fill:
            continue
        # 评分改成"越扁越好 + 空心度越接近环越好"，不再单纯奖励大块
        ringness = 1.0 - abs(fill - 0.4) / 0.4
        score = a * min(ar, 6.0) * (0.3 + 0.7 * max(0.0, ringness))
        if board is not None:
            bx, by, bw, bh = board
            cx = x + w / 2.0
            cy = y + h / 2.0
            # ---- 水平：篮圈要大致在篮板中轴附近 ----
            # 原来是 bw*1.7，宽得离谱：画面里任何橙色块（比如黄色球衣）只要
            # 横向不太偏就会被当成篮筐。收到 0.75*bw + 20px。
            if abs(cx - (bx + bw / 2.0)) > bw * 0.75 + 20:
                continue
            # ---- 垂直：篮圈必须在篮板**下沿**附近 ----
            # 这是原来**完全缺失**的一条约束，也是这次误检的直接原因：
            # 实测检出的"篮筐"(y=247) 落在"篮板"(y=397) 上方 150px，
            # 真实篮圈不可能在篮板上面。允许上下各 0.15 倍板高 + 15px 的余量。
            if not (by - 0.15 * bh - 15 <= cy <= by + bh + 0.5 * bh + 15):
                continue
        rims.append((score, [x, y, w, h]))
    rims.sort(key=lambda t: -t[0])
    return board, [list(b) for _s, b in rims]


def _validate_hoop(h) -> bool:
    """最终结果的自检：篮圈必须在篮板下沿附近、且在篮板中轴附近。

    为什么要在"取完中位数之后"再查一次：投票取中位数时，篮圈位置和篮板位置
    是**各自独立**取中位的 —— 如果逐帧配对本身是错的（比如把黄色球衣当篮圈），
    两个中位数可能来自完全不同的物体，合起来看就是几何上不可能的姿势
    （实测：篮圈在篮板上方 150px）。所以最后必须再验一次。
    """
    b = getattr(h, "board", None)
    if not b:
        return True                      # 没有篮板信息就不否决
    bx, by, bw, bh = b
    if not (bx - 0.75 * bw - 20 <= h.cx <= bx + bw + 0.75 * bw + 20):
        return False
    if not (by - 0.15 * bh - 25 <= h.cy <= by + bh + 0.5 * bh + 25):
        return False
    if h.ry > h.rx * 1.2:                # 篮圈不应该比它自己还高
        return False
    return True


def _frame_candidates(frame, cfg: HoopConfig):
    """向后兼容：返回单帧得分最高的篮圈。"""
    board, rims = _all_rim_candidates(frame, cfg)
    return board, (rims[0] if rims else None)


@dataclass
class HoopTrack:
    """篮筐位置随时间的轨迹（已剔除离群检出）。"""

    samples: list = field(default_factory=list)   # [(t, Hoop)]，按 t 排序
    fps: float = 30.0
    duration: float = 0.0
    votes: int = 0
    frames: int = 0
    width: int = 0
    cut_times: list = field(default_factory=list)  # 切镜时刻（秒）
    # 中值滤波半径（采样点数）。球穿过篮筐时会被误检成篮圈，
    # 单点离群值会把进球判定用的篮筐中心拽偏十几像素，必须先滤掉。
    smooth_window: int = 2
    identity_trace: list = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.votes / self.frames if self.frames else 0.0

    @property
    def median(self) -> Hoop:
        import statistics as st
        if not self.samples:
            raise RuntimeError("没有篮筐样本")
        hs = [h for _t, h in self.samples]
        return Hoop(cx=st.median([h.cx for h in hs]),
                    cy=st.median([h.cy for h in hs]),
                    rx=st.median([h.rx for h in hs]),
                    ry=st.median([h.ry for h in hs]),
                    board=list(hs[0].board) if hs[0].board else None,
                    votes=self.votes, confidence=round(self.coverage, 3),
                    method=hs[0].method)

    def _segment_id(self, t: float) -> int:
        return sum(1 for c in self.cut_times if c <= t)

    def _smooth_samples(self) -> list:
        """中值滤波后的采样序列。

        球穿过篮筐的那一两帧，橙色检测会把「球」误当成「篮圈」，
        让这一步的篮筐中心突然偏十几像素、半宽缩一半。
        判进球时用的就是这个位置，所以必须先做时间中值滤波把单点离群值滤掉。
        """
        cache = getattr(self, "_smooth_cache", None)
        if cache is not None:
            return cache
        import statistics as st
        win_r = max(0, int(getattr(self, "smooth_window", 2) or 0))
        n = len(self.samples)
        if n < 3 or win_r == 0:
            out = list(self.samples)
        else:
            out = []
            for i, (t, h) in enumerate(self.samples):
                sid = self._segment_id(t)
                lo = i
                while (lo - 1 >= 0 and i - (lo - 1) <= win_r
                       and self._segment_id(self.samples[lo - 1][0]) == sid):
                    lo -= 1
                hi = i
                while (hi + 1 < n and (hi + 1) - i <= win_r
                       and self._segment_id(self.samples[hi + 1][0]) == sid):
                    hi += 1
                win = [self.samples[j][1] for j in range(lo, hi + 1)]
                out.append((t, Hoop(
                    cx=st.median([w.cx for w in win]),
                    cy=st.median([w.cy for w in win]),
                    rx=st.median([w.rx for w in win]),
                    ry=st.median([w.ry for w in win]),
                    board=list(h.board) if h.board else None,
                    votes=h.votes, confidence=h.confidence, method=h.method)))
        self._smooth_cache = out
        return out

    def at(self, t: float) -> Hoop:
        """取 t 时刻的篮筐位置（已做时间中值滤波）；优先取同一镜头段内的样本。"""
        samples = self._smooth_samples()
        if not samples:
            raise RuntimeError("没有篮筐样本")
        if self.cut_times:
            sid = self._segment_id(t)
            same = [s for s in samples if self._segment_id(s[0]) == sid]
            if same:
                return min(same, key=lambda s: abs(s[0] - t))[1]
        return min(samples, key=lambda s: abs(s[0] - t))[1]

    def drift(self) -> tuple[float, float]:
        """整段视频里篮筐移动的 max-min（报告/诊断用）。"""
        if len(self.samples) < 2:
            return 0.0, 0.0
        xs = [h.cx for _t, h in self.samples]
        ys = [h.cy for _t, h in self.samples]
        return max(xs) - min(xs), max(ys) - min(ys)

    def robust_drift(self) -> tuple[float, float]:
        """10%~90% 分位数的漂移量：不受一两个离群点影响。"""
        if len(self.samples) < 2:
            return 0.0, 0.0

        def rng(vals):
            vals = sorted(vals)
            if len(vals) < 3:
                return vals[-1] - vals[0]
            lo = vals[max(0, int(0.1 * (len(vals) - 1)))]
            hi = vals[min(len(vals) - 1, int(0.9 * (len(vals) - 1)))]
            return hi - lo

        return (rng([h.cx for _t, h in self.samples]),
                rng([h.cy for _t, h in self.samples]))

    def local_uncertainty(self, t: float, window_s: float = 1.5) -> float:
        """t 时刻附近"筐心到底在哪"的不确定度（像素，横向）。

        **这不是概率置信区间**，只是"原始样本相对中值滤波轨迹的横向残差尺度"的
        覆盖值（10%~90% 区间宽度）—— 用来判断"这一刻的筐位够不够可信到可以下结论"。

        **为什么不用整段漂移量**：手持/摇摄素材里篮筐在画面里本来就一直在动
        （实测 nathan 整段稳健漂移 31px ≈ 一个篮圈半径），但那是**真实的镜头运动**、
        被跟踪得好好的 —— 它不代表"这一刻不知道筐心在哪"。拿它当不确定度会让所有
        判进都被否决（实测踩过：nathan 3 个真进球全变成"未知"，召回 0/5）。
        真正的不确定度来源是**检测抖动/离群** —— 同一段素材在 t=3.40s 附近的残差
        只有 2.5px（0.09×rx）。

        **边界**：窗口内样本不足 3 个时，这里会**退回整段残差**（不是严格局部）；
        采样率很低或镜头切换附近时不保证局部性。这一点由队友侧指出，当前保留该回退
        （宁可有值也不返回 0）。
        """
        raw = list(self.samples or [])
        if len(raw) < 3:
            return 0.0                       # 单点/两点：没有抖动可言（含手标筐）
        try:
            smoothed = self._smooth_samples()
        except Exception:                    # noqa: BLE001  诊断量不该拖垮主流程
            return 0.0
        if len(smoothed) != len(raw):
            return 0.0
        vals = sorted(h.cx - s.cx for (rt, h), (st, s) in zip(raw, smoothed)
                      if abs(rt - t) <= window_s)
        if len(vals) < 3:
            vals = sorted(h.cx - s.cx for (rt, h), (st, s) in zip(raw, smoothed))
        if len(vals) < 3:
            return 0.0
        lo = vals[max(0, int(0.1 * (len(vals) - 1)))]
        hi = vals[min(len(vals) - 1, int(0.9 * (len(vals) - 1)))]
        return max(0.0, hi - lo)

    def to_dict(self) -> dict:
        dx, dy = self.drift()
        rdx, rdy = self.robust_drift()
        return {"fps": self.fps, "duration": round(self.duration, 2),
                "votes": self.votes, "frames": self.frames,
                "coverage": round(self.coverage, 3),
                "median": self.median.to_dict(),
                "drift_px": [round(dx, 1), round(dy, 1)],
                "robust_drift_px": [round(rdx, 1), round(rdy, 1)],
                "cut_times": [round(c, 3) for c in self.cut_times],
                "identity_trace": self.identity_trace,
                "samples_complete": True,
                "samples": [[round(t, 6), h.to_dict()]
                            for t, h in self.samples]}

def _init_hoop_from_samples(samples, cfg) -> Optional[tuple]:
    """用段内前几帧「得分最高」的候选估计初始篮筐位置。

    真篮筐通常是最宽的橙色横椭圆，得分最高；离群橙色物体即使偶尔得分高，
    也很难在前几帧连续出现。这里取前 3 帧最高分候选的中位数。
    """
    tops = []
    for _t, _board, rims in samples[:3]:
        if rims:
            x, y, w, h = rims[0]
            tops.append((x + w / 2.0, y + h / 2.0))
    if not tops:
        return None
    xs = sorted(p[0] for p in tops)
    ys = sorted(p[1] for p in tops)
    return xs[len(xs) // 2], ys[len(ys) // 2]


def _select_rim_candidate(candidates, previous, cfg):
    """Select one observed identity; never replace a lost target with a distant box."""
    valid = [h for h in candidates if all(math.isfinite(v) for v in
             (h.cx, h.cy, h.rx, h.ry, h.confidence)) and h.rx > 0 and h.ry > 0]
    if not valid:
        return None, "no_valid_candidate"
    if previous is None:
        return max(valid, key=lambda h: h.confidence), "initial_lock"
    nearby = [h for h in valid if
              math.hypot(h.cx-previous.cx, h.cy-previous.cy) <= cfg.max_step_px
              and 0.45 * previous.rx <= h.rx <= 2.2 * previous.rx
              and 0.45 * previous.ry <= h.ry <= 2.2 * previous.ry]
    if not nearby:
        return None, "identity_gate_rejected"
    return min(nearby, key=lambda h: (math.hypot(h.cx-previous.cx, h.cy-previous.cy),
                                    -h.confidence)), "identity_matched"


def detect_hoop_track_yolo(video_path: str, weights: str,
                           cfg: Optional[HoopConfig] = None,
                           sample_fps: float = 3.0, conf: float = 0.25,
                           imgsz: int = 1280, device: str = "cpu") -> "HoopTrack":
    """用**训练好的检测器**找篮筐（替代手工颜色+形状启发式）。

    为什么要这条路：手工判据在真实素材上彻底不行（实测帧覆盖率 1~3%，
    还把黄色球衣当成篮筐）。篮筐是"小目标 + 环形 + 颜色和球衣重叠"，
    属于典型的需要学习的目标 —— 和球一样，训练后才靠得住。

    标注成本极低：固定机位下篮筐不动，scripts/label_rim.py 点一次就能
    给整段视频生成上百张标注。训练命令见 docs/实施步骤_战术层.md。
    """
    import cv2
    import numpy as np
    from ultralytics import YOLO
    cfg = cfg or HoopConfig()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频：{video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W, H = int(cap.get(3)), int(cap.get(4))
    step = max(1, int(round(fps / max(0.1, sample_fps))))
    model = YOLO(weights)
    samples = []
    identity_trace = []
    previous = None
    i = 0
    while i < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        if not ok:
            break
        r = model.predict(fr, conf=conf, imgsz=imgsz, device=device,
                          verbose=False)[0]
        candidates = []
        if r.boxes is not None:
            for b in r.boxes:
                if str(r.names[int(b.cls[0])]).lower() != "rim":
                    continue
                x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
                if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
                    continue
                candidates.append(Hoop(cx=(x1+x2)/2, cy=(y1+y2)/2,
                    rx=max(6.0,(x2-x1)/2), ry=max(4.0,(y2-y1)/2),
                    votes=1, confidence=float(b.conf[0])))
        selected, reason = _select_rim_candidate(candidates, previous, cfg)
        identity_trace.append({"t": round(i/fps, 6), "reason": reason,
                               "candidates": [h.to_dict() for h in candidates],
                               "selected": selected.to_dict() if selected else None})
        if selected is not None:
            samples.append((i/fps, selected))
            previous = selected
        i += step
    cap.release()
    if len(samples) < max(3, cfg.min_rim_votes):
        raise RuntimeError(
            f"篮筐检测器在 {len(samples)} 帧上找到 rim，太少"
            f"（需要 >= {max(3, cfg.min_rim_votes)}）。"
            "可能是这个机位没在训练集里 —— 用 scripts/label_rim.py 点几次补数据。")
    tr = HoopTrack(samples=samples, fps=fps, duration=total / fps if fps else 0.0,
                   votes=len(samples), frames=max(1, total // step),
                   width=W, cut_times=[], identity_trace=identity_trace)
    # 与颜色启发式那条路**同一道闸门**。为什么检测器这条路也必须查：
    # 模型每帧只挑"最像篮筐"的那个框，镜头上大幅移动（手持/摇摄）或者
    # 画面里出现别的橙色物体时，它会把不同物体当成同一个篮筐 —— 轨迹照样
    # 拉得出来，但「球从篮筐上方落到下方」这套判据已经不成立了。
    # 实测一段夜间手持素材：检测到的篮筐在 960px 宽的画面里漂了 780px，
    # 系统仍报出「进球」，其实是巧合。**这种情况必须放弃，而不是硬判。**
    dx, dy = tr.robust_drift()
    if W and dx > cfg.max_drift_frac * W:
        raise RuntimeError(
            f"篮筐轨迹不稳定：检测到的篮筐稳健漂移 {dx:.0f} 像素"
            f"（画面宽 {W}，阈值 {cfg.max_drift_frac:.0%}；纵向漂移 {dy:.0f}）。"
            "这类镜头下「球从篮筐上方落到下方」这套判据不成立，"
            "所以放弃视觉判定。把机位固定住再拍，或在画面上手工标一次篮筐"
            "（移动镜头下按逐帧位置判定）。")
    return tr


def detect_hoop_track(video_path: str, cfg: Optional[HoopConfig] = None,
                      progress: Optional[Callable[[float, str], None]] = None,
                      cuts: Optional[list[float]] = None,
                      hint: Optional[tuple] = None,
                      weights: str = "",
                      sample_fps: float = 3.0,
                      device: str = "cpu") -> HoopTrack:
    """按时间采样检测篮筐，得到随时间变化、且已剔除离群的篮筐轨迹。

    做法：
      1. 每个采样帧取出**所有**像篮圈的候选，而不是只取一个；
      2. 按切镜把视频分段；
      3. 每段用前几帧最高分候选初始化篮筐；
      4. 之后每帧只在「离上一帧篮筐不超过 max_step_px」的候选里选最近的，
         离群橙色物体（球员、篮球、广告牌）会被这一步挡掉；
      5. 用清洗后的轨迹判断机位是否真的在大幅移动（稳健漂移）。
    """
    # ---- 优先使用**训练好的篮筐检测器**（给了权重就走这条）----
    # 手工启发式在真实素材上帧覆盖率只有 1~3%，还会把黄色球衣当成篮筐；
    # 训练过的检测器是唯一靠得住的路（和球一样，见 scripts/label_rim.py）。
    if weights:
        try:
            tr = detect_hoop_track_yolo(video_path, weights, cfg,
                                        sample_fps=sample_fps, device=device)
            if progress:
                progress(0.35, "篮筐已定位（训练模型）")
            return tr
        except Exception as e:  # noqa: BLE001
            if hint is None:
                raise
            if progress:
                progress(0.2, f"篮筐模型不可用({str(e)[:40]})，退回手工检测")
    cv2, np = _require_cv()
    cfg = cfg or HoopConfig()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频：{video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)

    if cuts is None:
        from .scene import detect_scene_cuts
        cut_ts = detect_scene_cuts(video_path)
    else:
        cut_ts = sorted(float(c) for c in cuts)

    idxs = np.arange(0, max(1, total - 1), max(1, cfg.detect_stride))
    raw = []
    for k, i in enumerate(idxs):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok:
            continue
        board, rims = _all_rim_candidates(frame, cfg)
        if rims:
            raw.append((i / fps, board, rims))
        if progress and k % 20 == 0:
            progress(0.30 * k / max(1, len(idxs)), "识别篮筐")
    cap.release()

    if not raw:
        raise RuntimeError(
            "画面里没找到篮筐（一帧篮圈候选都没有）。可以：\n"
            "  ① 确认视频里能看到篮筐（篮板 + 篮圈）\n"
            "  ② 调 HoopConfig 的 orange_lo/hi、search_top、rim_min_aspect")

    # 按切镜分段
    groups: dict = {}
    for t, board, rims in raw:
        sid = sum(1 for c in cut_ts if c <= t)
        groups.setdefault(sid, []).append((t, board, rims))

    accepted: list = []
    for sid in sorted(groups):
        samples = groups[sid]
        hint_rx = 0.0
        if hint is not None:
            # 用户指定了篮筐位置：用它做初始位置/宽度，只在附近找候选。
            try:
                hx = float(hint[0]); hy = float(hint[1])
                hint_rx = float(hint[2]) if len(hint) > 2 else 0.0
            except Exception:
                hx = hy = None
            init = (hx, hy) if hx is not None else None
        else:
            init = _init_hoop_from_samples(samples, cfg)
        if init is None:
            continue
        # 初始 rx=0：第一帧不做宽度约束，先让它锁定真篮筐，
        # 之后再用锁定的宽度做一致性筛选。
        prev = Hoop(cx=init[0], cy=init[1], rx=hint_rx, ry=max(3.0, hint_rx * 0.3))
        seg_acc: list = []
        for t, board, rims in samples:
            cand = None
            best_d = 1e18
            best_cost = 1e18
            for box in rims:
                x, y, w, h = box
                # 篮圈宽度在同一镜头里应该大体稳定；突然变成很小/很大的
                # 橙色物体（篮球、球员、广告牌）不接。
                rx = w / 2.0
                if prev.rx > 0 and not (0.45 * prev.rx <= rx <= 2.2 * prev.rx):
                    continue
                d = math.hypot(x + w / 2.0 - prev.cx, y + h / 2.0 - prev.cy)
                if d > cfg.max_step_px:
                    continue
                # 同等距离下优先更宽、更像篮圈的候选（-0.15*w）。
                cost = d - 0.15 * w
                if cost < best_cost:
                    best_cost, best_d, cand = cost, d, box
            if cand is None or best_d > cfg.max_step_px:
                # 本帧没有可信篮筐：跳过，保留上一帧位置作为下一帧的参考。
                continue
            x, y, w, h = cand
            hobj = Hoop(cx=x + w / 2.0, cy=y + h / 2.0,
                        rx=w / 2.0, ry=max(2.0, h / 2.0),
                        board=board, votes=1, confidence=1.0,
                        method="orange-rim+white-board" if board else "orange-rim")
            seg_acc.append((t, hobj))
            prev = hobj
        min_seg = 1 if hint is not None else cfg.min_segment_votes
        if len(seg_acc) >= min_seg:
            accepted.extend(seg_acc)

    if hint is not None and not accepted:
        # 用户指定了篮筐但自动候选太少：退回固定位置，至少让穿筐判定能用。
        hx = float(hint[0]); hy = float(hint[1])
        hr = float(hint[2]) if len(hint) > 2 else 25.0
        accepted = [(0.0, Hoop(cx=hx, cy=hy, rx=hr,
                               ry=max(3.0, hr * 0.3), votes=1,
                               confidence=0.5, method="manual_hint"))]
    if len(accepted) < (1 if hint is not None else cfg.min_rim_votes):
        raise RuntimeError(
            f"画面里找到的篮筐候选太少（有效 {len(accepted)} 帧，需要 "
            f"{cfg.min_rim_votes} 帧以上）。可以调 HoopConfig 的 "
            "min_rim_votes / min_segment_votes / max_step_px")

    accepted.sort(key=lambda s: s[0])
    tr = HoopTrack(samples=accepted, fps=fps,
                   duration=total / fps if fps else 0.0,
                   votes=len(accepted), frames=len(idxs), width=W,
                   cut_times=cut_ts,
                   smooth_window=max(0, int(cfg.hoop_smooth_window)))

    # 清洗后的轨迹再做机位漂移检查，避免一两个离群点把整段视频毙掉。
    # 用户手动指定篮筐时，说明他确认了目标；移动机位下按逐帧位置判定，
    # 不再因为全局漂移而整体放弃。
    dx, dy = tr.robust_drift()
    if hint is None and W and dx > cfg.max_drift_frac * W:
        raise RuntimeError(
            f"镜头在移动：清洗后篮筐稳健漂移仍有 {dx:.0f} 像素"
            f"（画面宽 {W}，阈值 {cfg.max_drift_frac:.0%}）。"
            "这类镜头下「球从篮筐上方落到下方」这套判据不成立，"
            "所以放弃视觉判定。若是固定机位拍摄，可以调大 "
            "HoopConfig.max_drift_frac 或改用手工标注。")
    # ---- 最终几何自检 ----
    # 逐帧配对已经查过几何关系，但"篮圈位置"和"篮板位置"是各自独立取中位数的，
    # 万一逐帧配对本身是错的，两个中位数可能来自不同的物体，合起来就是
    # 几何上不可能的姿势。实测踩过：篮圈被定在篮板上方 150px（黄色球衣误检）。
    # 这时**宁可报"找不到篮筐"**，也不要返回一个错的 —— 错的篮筐会让
    # 后面所有进球判定变成垃圾。
    if not _validate_hoop(tr.median):
        raise RuntimeError(
            "篮筐候选的几何关系不成立（篮圈不在篮板下沿附近）——"
            "多半是把橙色球衣/其他橙色物体误检成了篮筐。已放弃视觉路径判定。")
    if progress:
        progress(0.35, "篮筐已定位")
    return tr


def detect_hoop(video_path: str, cfg: Optional[HoopConfig] = None,
                progress: Optional[Callable[[float, str], None]] = None) -> Hoop:
    """只取一个代表位置（报告/展示用）。判定进球请用 detect_hoop_track。"""
    return detect_hoop_track(video_path, cfg, progress).median


# --------------------------------------------------------------------------
# 出手 / 进球
# --------------------------------------------------------------------------
@dataclass
class ShotEvent:
    """一次由球轨迹推断出来的出手。"""

    t: float                       # 出手时刻（轨迹上升段起点）
    release_x: float               # 出手点（像素）
    release_y: float
    made: Optional[bool]          # None: 出手可见，但结果证据不足
    apex_y: float                  # 轨迹最高点（图像 y 越小越高）
    cross_x: Optional[float] = None   # 向下穿越篮筐水平线时的 x
    approach: float = 0.0          # 轨迹末端离篮筐中心的水平距离
    confidence: float = 0.5
    quality: float = 0.0           # 穿筐质量：下落速度/时间间隔，用于去重
    player_box: Optional[list] = None
    crossing_t: Optional[float] = None  # 落到篮筐平面的时刻，与出手时刻分开
    cluster: int = -1              # 属于第几条轨迹（调试用）
    # 这一球是"怎么判进"的：cross_measured = 逐帧看到穿筐；
    # cross_extrapolated = 筐口有检测空洞、用趋势拟合补出来的（置信度已降档）。
    evidence: str = ""

    def to_dict(self) -> dict:
        return {"t": round(self.t, 2), "release_x": round(self.release_x, 1),
                "release_y": round(self.release_y, 1), "made": self.made,
                "apex_y": round(self.apex_y, 1),
                "cross_x": None if self.cross_x is None else round(self.cross_x, 1),
                "approach": round(self.approach, 1),
                "confidence": round(self.confidence, 3),
                "quality": round(self.quality, 2),
                "player_box": self.player_box, "cluster": self.cluster,
                "crossing_t": self.crossing_t, "evidence": self.evidence}


def _crossing_x(a, b, y_line: float) -> Optional[float]:
    """线段 a->b 与水平线 y=y_line 的交点 x（只在向下穿越时返回）。"""
    if not (a[1] < y_line <= b[1]):
        return None
    dy = b[1] - a[1]
    if abs(dy) < 1e-6:
        return None
    k = (y_line - a[1]) / dy
    return a[0] + (b[0] - a[0]) * k


def _px_per_m(hoop) -> float:
    """篮筐处「1 米 ≈ 多少像素」。优先篮板高（真实 1.05m），否则篮圈半径（0.225m）。

    为什么不用 `ry`：`ry` 是篮圈在**视向方向**上的像素尺度，篮圈接近正侧视时
    它趋于 0（实测手标薄筐只有 3.9px），拿它当尺度，门槛会随标注方式漂移。
    返回 0 表示算不出来（此时调用方应退回到原来的像素倍数判据）。
    """
    board = getattr(hoop, "board", None)
    if board:
        try:
            bh = float(board[3])
        except (TypeError, ValueError, IndexError):
            bh = 0.0
        if bh >= 8.0:                    # 太小的框不是篮板
            return bh / 1.05
    try:
        rx = float(getattr(hoop, "rx", 0.0) or 0.0)
    except (TypeError, ValueError):
        rx = 0.0
    return rx / 0.225 if rx > 1.0 else 0.0


def _fitted_crossing(relative, ts, above_i: int, below_i: int, cfg):
    """用「入筐→出筐」之间的**所有点**拟合一条线，求它与篮筐平面（y=0）的交点。

    比"首尾两点插值"稳：中间有丢帧（球被网挡住）时，两端点连成的直线会被
    空洞带飞，而多点拟合会跟着真实趋势走。返回 (got, crossing_t, speed) 或 None。
    护栏：
      * 至少 `extrap_min_pts` 个点；
      * 斜率必须向下（y 增大）——回升的球不算穿筐；
      * 交点时刻必须落在 bracket 内或其后很短的时间里。
    """
    idx = [i for i in range(above_i, below_i + 1)]
    if len(idx) > 8:                     # 点太多时只用靠近筐口的一段
        idx = idx[-8:]
    pts = [(float(ts[i]), float(relative[i][0]), float(relative[i][1])) for i in idx]
    if len(pts) < max(3, int(getattr(cfg, "extrap_min_pts", 3))):
        return None
    m = len(pts)
    tbar = sum(p[0] for p in pts) / m
    sxx = sum((p[0] - tbar) ** 2 for p in pts)
    if sxx <= 1e-9:
        return None
    ybar = sum(p[2] for p in pts) / m
    xbar = sum(p[1] for p in pts) / m
    by = sum((p[0] - tbar) * (p[2] - ybar) for p in pts) / sxx
    bx = sum((p[0] - tbar) * (p[1] - xbar) for p in pts) / sxx
    if by <= 1e-6:                       # 没在往下走
        return None
    t_star = -ybar / by + tbar
    if not (pts[0][0] - 0.05 <= t_star <= pts[-1][0] + 0.15):
        return None
    return xbar + bx * (t_star - tbar), t_star, by


def _turns(vals, min_amp: float = 0.0) -> int:
    """方向变化次数；min_amp > 0 时忽略小于该幅度的来回抖动。"""
    if len(vals) < 3:
        return 0
    if min_amp <= 0:
        dirs = []
        for i in range(len(vals) - 1):
            if vals[i + 1] > vals[i]:
                dirs.append(1)
            elif vals[i + 1] < vals[i]:
                dirs.append(-1)
        return sum(1 for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1])
    turns = 0
    ext = vals[0]
    direction = 0
    for v in vals[1:]:
        d = v - ext
        if abs(d) < min_amp:
            continue
        nd = 1 if d > 0 else -1
        if direction == 0:
            direction = nd
        elif nd != direction:
            turns += 1
            direction = nd
        ext = v
    return turns



def _arc_cuts(seg, min_amp: float) -> list[int]:
    """找「球在最低点」的下标：y 由增转减的拐点。

    为什么必须切：球在整段素材里往往是**连续**被检出的 —— 运球、捡球、出手
    落在同一条轨迹上，而「一次出手」只是其中一段「上升→顶点→下落」的弧线。
    不切开的话，一条 10 秒的轨迹只算**一次**出手，而且运球造成的十来次反转
    会让它被当成噪声直接丢掉（实测：一段罚球素材球每帧都被检出、置信度 1.0，
    5 个进球却全部漏判）。
    """
    if len(seg) < 3:
        return []
    # Track the actual extremum on every sample. Small steps must accumulate;
    # otherwise a slow reversal moves the cut away from the physical low point.
    cuts: list[int] = []
    ext_i, ext_v = 0, float(seg[0].y)
    direction = 0
    for i in range(1, len(seg)):
        value = float(seg[i].y)
        if direction == 0:
            if abs(value - ext_v) >= min_amp:
                direction = 1 if value > ext_v else -1
                ext_i, ext_v = i, value
        elif direction > 0:
            if value >= ext_v:
                ext_i, ext_v = i, value
            elif ext_v - value >= min_amp:
                cuts.append(ext_i)
                direction, ext_i, ext_v = -1, i, value
        else:
            if value <= ext_v:
                ext_i, ext_v = i, value
            elif value - ext_v >= min_amp:
                direction, ext_i, ext_v = 1, i, value
    return [c for c in cuts if 0 < c < len(seg) - 1]


def _arcs(track_id: int, seg: list, min_amp: float, hoop=None):
    """把一段轨迹按最低点切成若干条弧线；相邻弧线共用那个最低点。"""
    if min_amp <= 0 or len(seg) < 3:
        yield track_id, seg
        return
    start = 0
    # Use the same camera-relative plane as the subsequent shot classifier.
    from types import SimpleNamespace
    relative = [SimpleNamespace(y=p.y - (hoop.at(p.t).cy if isinstance(hoop, HoopTrack)
                                        else hoop.cy if hoop is not None else 0)) for p in seg]
    for c in _arc_cuts(relative, min_amp):
        yield track_id, seg[start:c + 1]
        start = c
    if start < len(seg) - 1:
        yield track_id, seg[start:]


def _shot_segments(tracks, hoop, max_gap, arc_split_min_px: float = 0.0):
    """Break unsupported temporal bridges, retaining the original track ID."""
    cuts = sorted(getattr(hoop, 'cut_times', []) or [])
    for track_id, track in enumerate(tracks):
        segment = []
        for point in track:
            # Predicted/inpainted points may draw a track, but cannot witness an event.
            inferred = str(getattr(point, 'source', '')).lower() in {
                'interp', 'interpolated', 'inpaint', 'inpainted', 'kalman', 'predicted'}
            if inferred or not all(math.isfinite(float(v)) for v in (point.t, point.x, point.y)):
                if segment:
                    yield from _arcs(track_id, segment, arc_split_min_px, hoop)
                segment = []
                continue
            if segment:
                previous = float(segment[-1].t)
                dt = float(point.t) - previous
                if dt <= 0 or dt > max_gap or any(previous < c <= point.t for c in cuts):
                    yield from _arcs(track_id, segment, arc_split_min_px, hoop)
                    segment = []
            segment.append(point)
        if segment:
            yield from _arcs(track_id, segment, arc_split_min_px, hoop)


def detect_shots(tracks: Sequence[Sequence], hoop,
                 cfg: Optional[HoopConfig] = None) -> list[ShotEvent]:
    """从像素空间的球轨迹里找出手，并判断有没有穿筐。

    精度要点（针对之前「把静止橙色物体当球、在篮筐附近糊出一堆假命中」）：
      * 轨迹方向变化不能太多；
      * 判命中时，篮筐上方/下方必须都有明确采样点，且中间时间间隔要短；
      * 穿越点必须在篮圈内，穿越时的下落速度不能太小；
      * 没有上升段的轨迹，只有在「明确穿筐」时才保留。
    """
    cfg = cfg or HoopConfig()
    get_hoop = hoop.at if isinstance(hoop, HoopTrack) else (lambda _t: hoop)
    # 球不是质点：干净穿过要求球心落在 (1 - 球直径/筐内径) × rx 以内。
    # 这条**与标注方式无关**（物理常数），而且比原来的 rim_inner=0.85 严得多，
    # 见 HoopConfig.ball_diameter_m 的注释与 docs/假进球根因_球净空判据_2026-09-27.md。
    clearance = 1.0 - (cfg.ball_diameter_m / max(1e-6, cfg.rim_diameter_m))

    def hoop_unc_at(t: Optional[float]) -> float:
        """t 时刻的筐位不确定度（像素）。不确定度大时不许判进也不许判不中。"""
        if cfg.hoop_uncertainty_px is not None:
            return float(cfg.hoop_uncertainty_px)
        if t is None:
            return 0.0
        fn = getattr(hoop, "local_uncertainty", None)
        if not callable(fn):
            return 0.0                       # 常数 Hoop（手标筐）没有抖动
        try:
            return float(fn(t, cfg.hoop_uncertainty_window_s))
        except Exception:                     # noqa: BLE001  诊断信息不该拖垮主流程
            return 0.0
    out: list[ShotEvent] = []
    for ti, tr in _shot_segments(tracks, hoop, cfg.max_track_gap_s,
                                 getattr(cfg, "arc_split_min_px", 0.0)):
        if len(tr) < cfg.min_track_pts:
            continue
        pts = [(float(c.x), float(c.y), float(c.t)) for c in tr]
        ts = [p[2] for p in pts]
        if ts[-1] - ts[0] < 0.25:
            continue
        # Translation from camera panning must not look like a ball rising/falling.
        relative = [(p[0] - get_hoop(p[2]).cx, p[1] - get_hoop(p[2]).cy, p[2]) for p in pts]
        ys = [p[1] for p in relative]
        # 方向变化太多的轨迹直接丢掉：球飞行最多「升->降」一两次，
        # 不会像噪声轨迹一样来回横跳。
        # 但判据必须按**速率**而不是次数：长轨迹（连续跟住球的运球段）
        # 反转次数天然就多，按次数会把真进球整条丢掉。
        y_turns = _turns(ys, getattr(cfg, "track_turn_min_amp_px", 0.0))
        _span = max(1e-3, ts[-1] - ts[0])
        if (y_turns > cfg.max_track_turns
                and y_turns / _span > getattr(cfg, "max_track_turn_rate", 6.0)):
            continue
        apex_i = int(min(range(len(pts)), key=lambda i: ys[i]))
        apex_y = pts[apex_i][1]
        hp_apex = get_hoop(ts[apex_i])

        made = None
        crossing_t = None
        cross_x = None
        cross_speed = 0.0
        quality = 0.0
        evidence = ""            # "cross_measured" / "cross_extrapolated"

        # ---- 主判据：明确从篮筐上方穿到下方 ----
        # ---- 主判据：明确从篮筐上方穿到下方 ----
        # 先找第一个「明显在篮筐下方」的点，再往前找最后一个「明显在篮筐上方」的点。
        # 不能从整条轨迹的第一个上方点算起：高弧线会在上方停留很久，
        # 时间间隔会超过阈值，导致明明进球却被判不中。
        above_i = None
        below_i = None
        seen_above = False
        for i, p in enumerate(pts):
            hp = get_hoop(p[2])
            if p[1] < hp.cy - hp.ry:
                seen_above = True
                above_i = i          # 持续更新：保留「穿越前最后一个上方点」
            elif seen_above and p[1] > hp.cy + hp.ry:
                below_i = i          # 上方之后的第一个下方点
                break
        if above_i is not None and below_i is not None:
            gap = ts[below_i] - ts[above_i]
            drop = relative[below_i][1] - relative[above_i][1]
            hp_cross = get_hoop(ts[below_i])
            observed_gap = max(ts[j + 1] - ts[j] for j in range(above_i, below_i))
            got = (_crossing_x(relative[above_i], relative[below_i], 0.0)
                   if gap > 0 else None)
            if got is not None:
                # 直接在「最后一个上方点 -> 第一个下方点」之间插值。
                # 这样即使球最后几帧贴筐/被遮，也不会把穿越点算到很远的地方。
                cross_speed = drop / gap
                # 下落距离的**米制下限**：与标注方式无关（见 HoopConfig.min_cross_drop_m）。
                # 算不出像素/米时退回原来的「ry 的倍数」，行为与改动前一致。
                px_per_m = _px_per_m(hp_cross)
                drop_need_px = hp_cross.ry * cfg.min_cross_drop_frac
                if px_per_m > 0 and cfg.min_cross_drop_m > 0:
                    drop_need_px = max(drop_need_px, cfg.min_cross_drop_m * px_per_m)
                # 轨迹中间有**检测空洞**（球被篮网/篮板/人挡住或漏检）时，
                # 插值出的穿越点有可能只是巧合。默认**不采信**（严格）；
                # 打开 cfg.allow_hole_interpolation 后要求插值结果"物理上说得通"
                # 才放行：下落速度落在球该有的区间、落点更靠筐心。
                hole = observed_gap > cfg.cross_max_gap_s
                credible = (
                    0 < gap <= cfg.max_cross_gap_s
                    and drop >= drop_need_px
                    and cfg.min_cross_speed_px <= cross_speed
                    <= cfg.max_cross_speed_px
                    and (not hole or abs(got) <= hp_cross.rx
                         * min(cfg.rim_inner, clearance) * cfg.hole_rim_tighten)
                )
                cross_by_extrapolation = False
                if hole:
                    credible = credible and bool(cfg.allow_hole_interpolation)
                    # 空洞档的独立开关：不改"首尾插值"，而是用 bracket 内所有点
                    # 拟合出交点，并且只对**更靠筐心**的落点放行（默认关）。
                    if (not credible and cfg.allow_extrapolated_crossing
                            and 0 < gap <= min(cfg.extrap_max_bracket_s, cfg.max_cross_gap_s)
                            and drop >= drop_need_px):
                        fit = _fitted_crossing(relative, ts, above_i, below_i, cfg)
                        if fit is not None:
                            got_f, t_f, speed_f = fit
                            if (abs(got_f) <= hp_cross.rx
                                    * min(cfg.extrap_rim_inner, clearance)
                                    and cfg.min_cross_speed_px <= speed_f
                                    <= cfg.max_cross_speed_px):
                                got, crossing_t, cross_speed = got_f, t_f, speed_f
                                credible = True
                                cross_by_extrapolation = True
                if credible:
                    if not cross_by_extrapolation:
                        fraction = -relative[above_i][1] / drop
                        crossing_t = ts[above_i] + fraction * gap
                    cross_x = got + get_hoop(crossing_t).cx
                    # 两种横向门槛取更严的那个：
                    #   ① 球不是质点 —— (1 - 球直径/筐内径) = 0.467（物理净空，见上）
                    #   ② 各档自己的系数（rim_inner / extrap_rim_inner，可以更严）
                    inner = min(cfg.extrap_rim_inner if cross_by_extrapolation
                                else cfg.rim_inner, clearance)
                    # 筐位本身的不确定度：整段漂移量**不能**用（那是镜头运动，见
                    # HoopTrack.local_uncertainty）。只有"这一刻筐心就抖得厉害"时，
                    # 才既不许判进也不许判不中 —— 那是凭误差下结论。
                    unc = hoop_unc_at(crossing_t)
                    unc_frac = unc / max(1.0, hp_cross.rx)
                    if unc_frac >= cfg.hoop_uncertainty_max_frac:
                        evidence = "cross_hoop_uncertain"
                    elif abs(got) <= hp_cross.rx * inner:
                        made = True
                        quality = cross_speed / max(0.03, gap)
                        evidence = ("cross_extrapolated" if cross_by_extrapolation
                                    else "cross_interpolated" if hole
                                    else "cross_measured")
                    elif (not cross_by_extrapolation
                          and abs(got) > hp_cross.rx * 1.15):
                        # 明显从篮圈**外侧**落下去 → 判定不中
                        made = False
                    elif abs(got) <= hp_cross.rx + unc:
                        # 球心的横向偏移已经大到"球体在**图像平面**上与篮圈重叠"，
                        # 但还没到"明显从圈外落下去" → "贴筐掠过（结果未知）"。
                        # 注意措辞：这是**图像平面**的保守判断 —— 单目投影下
                        # "像面上重叠"既不等于也不排除三维接触（球可能从筐前面/后面
                        # 掠过），所以这里**只敢说"可能擦筐、无法判定"，不敢断言碰到**
                        # （这一点由队友侧在复核页文案上纠正，我同意）。
                        # 能确定的只有一件事：这种偏移**不足以支撑"干净穿筐"的结论**，
                        # 所以既不算进也不算不中，交人工。
                        evidence = "cross_rim_contact"
                # Rim-edge overlap is ambiguous; do not force a miss.

        # 不再用「球在筐内被采到就算进」这种弱判据 —— 静止的橙色物体
        # （球衣、手、篮网）经常正好落在筐内，会糊出假命中。
        # 命中必须满足上面的「明确从上方穿到下方」主判据。

        # ---- 再看有没有上升段（用来认定「这是一次出手」）----
        rise = (max(ys[:apex_i + 1]) - ys[apex_i]) if apex_i > 0 else 0.0
        speed = 0.0
        if apex_i >= 2:
            dt = max(1e-3, ts[apex_i] - ts[0])
            speed = (ys[0] - ys[apex_i]) / dt
        saw_rise = (rise >= cfg.min_rise_px
                    and (apex_i < 2 or speed >= cfg.min_rise_speed))

        approach = min(abs(p[0] - get_hoop(p[2]).cx) for p in pts)

        if saw_rise:
            release = pts[0]
            t_rel = release[2]
            if ys[apex_i] > hp_apex.ry * 2.5:
                continue
            if approach > hp_apex.rx * cfg.rim_x_tol:
                continue
            conf = 0.75 if made else 0.45
            quality = max(quality, float(speed))
        else:
            # 没看到上升段：只有明确穿筐才保留（避免把碎片轨迹当进球）。
            if not made:
                continue
            release = pts[0]
            t_rel = ts[0]
            conf = 0.7

        if approach > hp_apex.rx * 1.2 and made:
            conf = max(0.5, conf - 0.1)
        if made and cross_speed < cfg.min_cross_speed_px:
            conf = max(0.5, conf - 0.1)
        # 外推档拿到的"进"要降置信度：证据来自趋势拟合，不是逐帧看到的穿越。
        if made and evidence in {"cross_extrapolated", "cross_interpolated"}:
            conf = max(0.5, conf - 0.1)
        out.append(ShotEvent(t=t_rel, release_x=release[0], release_y=release[1],
                             made=made, apex_y=apex_y, cross_x=cross_x,
                             approach=approach,
                             confidence=max(0.2, round(conf, 3)),
                             quality=round(quality, 2), cluster=ti,
                             crossing_t=crossing_t,
                             evidence=evidence))

    # 去重：同一时刻附近只留一条；优先保留命中的、置信度高的。
    out.sort(key=lambda s: (not s.made, -s.confidence, -s.quality, s.t))
    kept: list[ShotEvent] = []
    for s in out:
        # Fragments can start far apart while observing the very same crossing.
        # Distinct observed crossings take precedence over near release times.
        def same_event(k):
            if s.crossing_t is not None and k.crossing_t is not None:
                return abs(s.crossing_t - k.crossing_t) < min(.2, cfg.min_shot_gap_s)
            return abs(s.t - k.t) < cfg.min_shot_gap_s
        if any(same_event(k) for k in kept):
            continue
        kept.append(s)
    kept.sort(key=lambda s: s.t)
    return kept


def attach_players(shots: Sequence[ShotEvent], detections: Sequence[dict]) -> None:
    """把每次出手归到「出手时刻离球最近的那个球员框」。

    detections: [{"t":.., "boxes":[[x1,y1,x2,y2,track_id], ...]}, ...]
    找不到就留空，绝不硬塞给某个人。
    """
    if not detections:
        return
    for s in shots:
        best = None
        bd = 1e18
        for d in detections:
            if abs(d["t"] - s.t) > 0.35:
                continue
            for b in d["boxes"]:
                cx = (b[0] + b[2]) / 2.0
                cy = (b[1] + b[3]) / 2.0
                dist = math.hypot(cx - s.release_x, cy - s.release_y)
                if dist < bd:
                    bd = dist
                    best = list(b)
        if best is not None:
            s.player_box = best
