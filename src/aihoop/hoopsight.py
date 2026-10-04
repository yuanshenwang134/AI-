"""篮下视觉判进球 —— **不读比分牌、不依赖球检测器**的自动计分路径。

为什么还要再写一层（`hoop.detect_shots` + `ball.py` 不是已经有了吗）：

  那一层要求「先把球追成一条完整弧线」。实测在固定机位拍的青少年比赛
  （`data/nybo_3min.mp4`）上根本做不到：球检测器在这段素材上**认的是黄色球衣**
  （每帧 1~5 个框全在球员胸口），真正的球（蓝黄拼色）几乎没被框到过，
  于是 186 条候选轨迹里没有一条能形成「升起 → 下落 → 穿筐」，
  最终输出 0 次出手。

  但是**进球这件事本身在画面上是直接可测的**，而且量起来比追球容易得多：

    篮筐是静止的、位置已知（`hoop.detect_hoop_track` 已能 97% 帧定位）；
    球进筐的那一刻，**篮圈附近必然出现一个「球的尺寸」的圆形运动块，
    并且它的质心在几百毫秒内从篮圈上方/外侧连续移动到篮圈下方**。

  所以本模块把问题缩小到「篮筐周围一小块窗口里的运动块时序」，完全不问
  「整个球场里哪个东西是球」。黄色球衣也在动，但它们不会以球的尺寸
  从筐口穿到网下；反过来，只要一个块真的从筐口穿过去，它是什么颜色、
  检测器认不认得它都不重要。

判据（全部是几何/时序，可解释、可断言）：

  1. **候选块**：窗口内帧间差分 → 形态学 → 连通域
     * 面积 = 篮圈半径推出来的「球的尺寸」范围（球直径≈0.55×篮圈直径）；
     * 接近圆（外接框长宽比接近 1、填充率接近 π/4）。
  2. **入筐**：块质心从「篮圈上沿以上**或**篮圈外侧」进入篮圈水平带。
  3. **穿筐**：随后质心落到篮圈下方（≥ 1.0×ry），且横向偏移 ≤ 0.9×rx。
  4. **下落**：穿越段质心单调向下（允许 1 帧抖动），总下落 ≥ 1.5×ry。
  5. **成段**：整个过程 ≤ 0.8s 且至少有 3 帧证据。
  6. **去抖**：相邻两次进球至少间隔 `min_shot_gap_s`。

返回值刻意把**每一步的量**都带出来（`frames`、`entry_*`、`exit_*`、
`drop_px`、`size_ratio`），这样既能做回归测试，也能在答辩时逐条讲。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .hoop import HoopConfig, detect_hoop_track


class HoopSightError(RuntimeError):
    """篮下判进球不可用（缺依赖 / 没定位到篮筐）。"""


def _require_cv():
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover - 取决于运行环境
        raise HoopSightError(
            "篮下判进球需要 opencv-python 与 numpy。"
            "安装：pip install -r requirements-full.txt") from e
    return cv2, np


@dataclass
class SightConfig:
    """篮下判进球的阈值 —— 全部集中在这里，现场好调。"""

    # ---- 窗口 ----
    win_scale: float = 2.4        # 窗口边长 = max(rx,ry) 的多少倍
    win_min_px: int = 90          # 窗口最小边长（远景篮筐很小，别缩得太狠）
    sub_extra: float = 1.9        # 篮圈下方额外扩多少倍 ry（球穿下去后还要看得见）

    # ---- 候选块（相对篮圈半径推出来的球尺寸）----
    # 注意：球穿筐的那几帧，**网会跟着晃**，帧差块是「球 + 网」的连体，
    # 实测面积从 ~250px 一直到 ~1700px（期望球面积 ~490px）。
    # 所以尺寸窗口必须开得很宽 —— 它的作用只是**排除明显不是球的量级**
    # （观众席的整片晃动、球员手臂划过），精确性交给「穿筐轨迹」去保证。
    ball_d_ratio: float = 0.55    # 球直径 / 篮圈直径（FIBA：0.24/0.45 ≈ 0.53）
    size_lo: float = 0.35         # 允许的尺寸下限（× 期望直径）
    size_hi: float = 3.20         # 上限（穿筐瞬间连网一起动，会大好几倍）
    min_area_px: int = 12
    min_fill: float = 0.25        # 连通域填充率（圆≈0.785，连网后会散）
    max_fill: float = 1.0
    aspect_tol: float = 3.0       # 外接框长宽比上限

    # ---- 运动 ----
    motion_thresh: int = 18       # 帧差阈值
    motion_open: int = 3          # 形态学开运算核
    max_gap_frames: int = 2       # 允许丢几帧

    # ---- 入筐 / 穿筐 ----
    enter_above_frac: float = 0.35   # 入筐点可以在篮圈中心上方/下方多少 ry 内
    enter_side_frac: float = 1.6     # 入筐点允许在篮圈外侧多少 rx 内（斜着进的球）
    exit_pixel_tolerance: float = 0.5  # Pixel-scale uncertainty; capped at 10% of ry, central exits only
    cross_inner: float = 0.90        # 穿筐点横向偏移上限（× rx）
    # 「球心真的进了圈」的横向阈值。**这一条是修误判的关键**：
    # 实测（nybo_3min）把 cross_inner 放到 0.9×rx 时，"球贴着篮圈右外缘斜掠而下"
    # 也会被判成穿筐（用户指出的 59.94s / 127.67s 两个假进球，球心横向偏移
    # 一直在 0.4~1.1×rx，从没真正进过圈内）。
    # 0.62 是实测取的折中：仍然容得下从框内外沿进入的真球，但挡掉擦筐而过的。
    cup_inner: float = 0.62
    # 球心必须**至少连续这么多帧**落在圈内（真进球球心会在圈里停留几帧）
    min_cup_frames: int = 2
    # 判据自身的置信度门槛。**默认给得很低（0.5）是有意的** —— 这个软分不是
    # 概率，别用它当安全阀；真正需要的是"这段素材到底能不能判"。
    # 实测（nybo_3min，人工确认 7 个候选**全部没进**）：本判据把 3 个判成进球，
    # 置信度 0.896 / 0.917 / 0.970 —— 门槛要 0.970 才零误报，而那正是它自称的
    # 高置信度。所以在这类「篮筐贴画面顶边」的机位上它**没有判别能力**，
    # 不是调参能救的。要用它做严格判定时，显式把门槛提到 0.98+ 并配人工复核。
    min_confidence: float = 0.5
    # 候选块密度上限（块/帧）。超过就认为"整幅画面在动"（镜头平移/多镜头剪辑），
    # 运动块判据不成立 → 跳过。实测：固定机位 0.018，多镜头剪辑 2.2。
    max_blob_rate: float = 0.5
    min_drop_ry: float = 1.5         # 总下落至少多少倍 ry
    max_cross_s: float = 0.8         # 入筐到出筐的最大时间
    min_chain: int = 3               # 至少几帧证据
    # 下落段允许的最大「回升」。超过它认为是球弹回筐上方（不是进球）。
    # 取 0.8×ry 而不是更小：实测网的晃动会让选中块的位置跳十几像素，
    # 卡太死会把真进球误杀（真丢过一球）。
    split_rise_frac: float = 0.80

    # ---- 去抖 ----
    min_shot_gap_s: float = 0.8
    stride: int = 1               # 逐帧扫（球穿筐只有几帧）
    max_frame_trace: int = 30000  # 逐帧诊断上限；超过后继续累计原因计数
    max_blob_stats: int = 4000    # 诊断留痕上限（窗口内所有连通域）

    # ---- 候选块挑选 ----
    pick_below_ry: float = 2.6        # 超过篮圈下方这么多倍 ry 就算「太靠下」
    pick_below_penalty: float = 3.0   # 太靠下的块加多少代价
    pick_min_area_low: float = 150.0  # 又靠下又小于这个面积 → 当噪声丢掉

    # ---- 筐下的人（判「这次进球是哪一队」用）----
    # 两条硬约束都重要：
    #   ① 形状：人是高的（高/宽 >= 1.4），「球 + 网」的连体块是方的；
    #   ② 尺寸：高度 >= 110px。远景里一个球员至少这么高，小于它的多半是
    #      快速运动的拖影（实测 42x71 的模糊块就是拖影，不是人）。
    # 达不到就**一个都不报**，宁可说「筐下检不到人」，也不把拖影当人。
    people_win_scale: float = 3.6     # 找人的窗口 = max(rx,ry) 的多少倍
    people_win_min_px: int = 180
    people_min_h_px: int = 110        # 比球、网、拖影都大得多才算人
    people_min_area: int = 2200
    people_min_aspect: float = 1.4    # 高 / 宽 下限（人 >= 1.4）
    people_max_aspect: float = 4.5
    # 填充率：实心人体块接近 1；**文字/字幕的笔画**只有 0.2~0.4。
    # 这一条是被真实翻车逼出来的：给视频打标签时烧进去的文字，在后续帧里
    # 会被背景差分当成前景，长宽比和面积都像人 —— 结果「筐下检出人」检到的
    # 是我自己画上去的字。加上填充率就干净了。
    people_min_fill: float = 0.45
    people_max_dist_rx: float = 5.0   # 离篮筐超过这么多倍 rx 就不算「筐下」
    detect_people: bool = True        # 顺带在篮筐附近找人（判队要用）
    people_use_bg: bool = True        # 用中值背景做前景（固定机位最干净）
    people_bg_samples: int = 40


@dataclass
class SightShot:
    """一次「球从筐里下去」的事件。"""

    t: float                      # 出筐时刻（球落到篮圈下方的瞬间）
    t_enter: float                # 入筐时刻
    made: bool = True
    hoop_cx: float = 0.0
    hoop_cy: float = 0.0
    hoop_rx: float = 0.0
    hoop_ry: float = 0.0
    entry_x: float = 0.0
    entry_y: float = 0.0
    exit_x: float = 0.0
    exit_y: float = 0.0
    drop_px: float = 0.0
    cross_x: float = 0.0
    frames: int = 0
    size_px: float = 0.0
    confidence: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


@dataclass
class SightScan:
    """整段视频的篮下扫描结果。"""

    shots: list[SightShot] = field(default_factory=list)
    rejected: list = field(default_factory=list)   # 低于置信度门槛、未计入的判定
    hoops: list[dict] = field(default_factory=list)
    fps: float = 30.0
    duration: float = 0.0
    frames: int = 0
    blobs: int = 0                # 累计候选块数（诊断用）
    people: list = field(default_factory=list)   # 筐下检出的「人形块」
    blob_stats: list = field(default_factory=list)   # 窗口内所有连通域（诊断）
    windows: list = field(default_factory=list)      # 扫描窗口（诊断）
    note: str = ""
    note_people: str = ""
    # 判据在这段素材上到底可不可信（见 _judge_diagnosis）
    diagnosis: dict = field(default_factory=dict)
    candidate_trace: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# 单个篮筐：窗口内的候选块
# --------------------------------------------------------------------------
def _window(cfg: SightConfig, hoop, W: int = 0, H: int = 0
            ) -> tuple[int, int, int, int]:
    """篮筐周围的扫描窗口（会自动裁到画面内）。

    裁边这一步不是可选的：篮筐常常贴着画面边缘（实测左篮筐在 (72,58)，
    半高 24px 时窗口上边界会算到 y=-11）。不裁的话 ROI 切片是空的，
    `connectedComponentsWithStats` 会直接抛异常 —— 而异常被上层吞掉后
    表现成「一个候选块都没有」，看着像阈值太严，其实窗口根本没取到像素。
    """
    r = max(float(hoop.rx), float(hoop.ry))
    size = max(int(cfg.win_min_px), int(round(r * cfg.win_scale)))
    cx, cy = float(hoop.cx), float(hoop.cy)
    x0 = int(math.floor(cx - size * 0.5))
    y0 = int(math.floor(cy - size * 0.5))
    x1 = int(math.ceil(cx + size * 0.5))
    y1 = int(math.ceil(cy + size * 0.5 + float(hoop.ry) * cfg.sub_extra))
    if W:
        x0, x1 = max(0, x0), min(int(W), x1)
    if H:
        y0, y1 = max(0, y0), min(int(H), y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return (0, 0, 0, 0)
    return x0, y0, x1, y1


def _expect_ball_px(hoop, cfg: SightConfig) -> float:
    """由篮圈尺寸反推「球在画面里应该多大」（像素直径）。"""
    return max(3.0, float(hoop.rx) * 2.0 * cfg.ball_d_ratio)


def _blobs_in_window(cv2, np, prev_gray, gray, win, hoop, cfg: SightConfig,
                     stats_out: Optional[list] = None, trace: Optional[dict] = None):
    """窗口内的「球的尺寸」运动块 → [(cx, cy, area, w, h)]（绝对坐标）。

    stats_out 给了就把「所有」连通域（含被过滤掉的）追加进去，用于调阈值
    与答辩时的证据留痕 —— 光看最终 0 结果没法判断是阈值问题还是画面问题。
    """
    if trace is not None:
        trace.update(stage="empty_window", components=0, accepted=0, rejected={})
    x0, y0, x1, y1 = win
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    a = prev_gray[y0:y1, x0:x1]
    b = gray[y0:y1, x0:x1]
    if a.size == 0 or a.shape != b.shape:
        if trace is not None:
            trace["stage"] = "frame_shape_mismatch"
        return []
    d = cv2.absdiff(b, a)
    m = (d >= cfg.motion_thresh).astype(np.uint8) * 255
    if cfg.motion_open >= 2:
        k = np.ones((cfg.motion_open, cfg.motion_open), np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)

    exp_d = _expect_ball_px(hoop, cfg)
    exp_a = math.pi * (exp_d / 2.0) ** 2
    lo_a = max(cfg.min_area_px, int(exp_a * cfg.size_lo ** 2))
    hi_a = int(exp_a * cfg.size_hi ** 2)

    n, _lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    if trace is not None:
        trace.update(components=n-1, stage="no_motion_components" if n <= 1 else "all_components_filtered",
                     area_range=[lo_a, hi_a])
    out = []
    for i in range(1, n):
        bx, by, bw, bh, area = (int(stats[i, 0]), int(stats[i, 1]),
                                int(stats[i, 2]), int(stats[i, 3]),
                                int(stats[i, 4]))
        fill = area / float(max(1, bw * bh))
        cx = float(cent[i][0]) + x0
        cy = float(cent[i][1]) + y0
        if stats_out is not None:
            stats_out.append(dict(cx=round(cx, 1), cy=round(cy, 1), area=area,
                                  w=bw, h=bh, fill=round(fill, 3),
                                  exp_area=round(exp_a, 1),
                                  lo=lo_a, hi=hi_a))
        reason = None
        if area < lo_a:
            reason = "area_small"
        elif area > hi_a:
            reason = "area_large"
        elif bw < 2 or bh < 2:
            reason = "dimensions_small"
        elif max(bw, bh) / float(max(1, min(bw, bh))) > cfg.aspect_tol:
            reason = "aspect"
        elif fill < cfg.min_fill or fill > cfg.max_fill:
            reason = "fill"
        if reason:
            if trace is not None:
                counts = trace["rejected"]
                counts[reason] = counts.get(reason, 0) + 1
            continue
        out.append((cx, cy, area, bw, bh))
    if trace is not None:
        trace["accepted"] = len(out)
        if out:
            trace["stage"] = "candidates_available"
    return out


# --------------------------------------------------------------------------
# 单帧序列 → 进球事件
# --------------------------------------------------------------------------
def _pick_blob(blobs, hoop, cfg: SightConfig):
    """一帧里可能有多个块；取「最像正在穿筐的那个」（返回 None 表示放弃这一帧）。

    优先规则：横向离篮圈中心近 > 面积接近期望值 > 别跑到窗口最底下。
    最后一条很重要：实测固定机位画面里，**网下沿/投影灯会在 y≈154~158
    持续抖出 20~45px 的小块**，它们又小又正好在篮圈正下方，如果不排除，
    「贴中心 + 面积小」反而会被算成最优 —— 于是拼出一条假的「穿筐」链。
    """
    if not blobs:
        return None
    exp_d = _expect_ball_px(hoop, cfg)
    exp_a = math.pi * (exp_d / 2.0) ** 2
    rx = max(1.0, float(hoop.rx))
    ry = max(1.0, float(hoop.ry))
    cy0 = float(hoop.cy)
    # 允许的最低位置：篮圈中心下方 2.6 倍 ry 之外就当成噪声（网/灯抖）
    y_limit = cy0 + ry * cfg.pick_below_ry

    def cost(b):
        cx, cy, area, _w, _h = b
        dx = abs(cx - float(hoop.cx)) / rx
        da = abs(area - exp_a) / max(1.0, exp_a)
        c = dx * 2.0 + da
        if cy > y_limit:
            c += cfg.pick_below_penalty
        return c

    best = min(blobs, key=cost)
    if best[1] > y_limit and best[2] < cfg.pick_min_area_low:
        # 又靠下又小 → 判定为噪声，这一帧不取候选
        return None
    return best


def _judge_span(chain, hoop, cfg: SightConfig,
                hoop_lookup: Optional[Callable[[float], object]] = None,
                diagnostic: Optional[dict] = None) -> Optional[SightShot]:
    """判「一段已经确认 入口在上、出口在下」的候选块算不算进球。

    chain: [(t, cx, cy, area)]，首点=入筐点（在篮圈带上），末点=出筐点（篮圈下方）。
    调用方（`_shots_from_blob_series`）负责找入筐点/出筐点、并排除弹筐回升；
    这里只回答**几何上站不站得住**：时间、下落量、下落干不干脆、穿筐点正不正。
    """
    if len(chain) < cfg.min_chain:
        if diagnostic is not None:
            diagnostic["reason"] = "insufficient_chain"
        return None
    look = hoop_lookup or (lambda _t: hoop)
    rx, ry = float(hoop.rx), float(hoop.ry)
    cy0 = float(hoop.cy)
    cx0 = float(hoop.cx)

    t_enter, t_exit = chain[0][0], chain[-1][0]
    if (t_exit - t_enter) > cfg.max_cross_s:
        if diagnostic is not None:
            diagnostic["reason"] = "crossing_timeout"
        return None

    # 1) 下落要基本单调：允许抖动，但**大幅回升**（球弹回筐上方）要否掉。
    #    阈值取「明显回升」而不是「任何回升」：实测固定机位里网的晃动会让
    #    选中的块位置跳变几像素到十几像素，卡太死会把真进球误杀。
    big_rise = 0.0
    for k in range(len(chain) - 1):
        dy = chain[k + 1][2] - chain[k][2]
        if dy < 0:
            big_rise = max(big_rise, -dy)
    if big_rise > cfg.split_rise_frac * ry:
        if diagnostic is not None:
            diagnostic["reason"] = "upward_rebound"
        return None

    # 2) 总下落量
    drop = chain[-1][2] - chain[0][2]
    if drop < ry * cfg.min_drop_ry:
        if diagnostic is not None:
            diagnostic["reason"] = "insufficient_drop"
        return None

    # 3) 穿筐点：在入筐点/出筐点之间插值出「y = 篮圈中心」的横坐标
    cross_x = None
    for k in range(len(chain) - 1):
        hp = look(chain[k][0])
        y_c = float(hp.cy)
        y_a, y_b = chain[k][2], chain[k + 1][2]
        if (y_a - y_c) * (y_b - y_c) <= 0 and abs(y_b - y_a) > 1e-6:
            f = (y_c - y_a) / (y_b - y_a)
            cross_x = chain[k][1] + f * (chain[k + 1][1] - chain[k][1])
            break
    if cross_x is None:
        cross_x = chain[-1][1]
    if abs(cross_x - cx0) > rx * cfg.cross_inner:
        if diagnostic is not None:
            diagnostic["reason"] = "crossing_outside_rim"
        return None

    # 3b) **球心必须真的进过圈**：至少 `min_cup_frames` 帧，球心横向落在
    #     `cup_inner × rx` 以内。只靠「穿筐点」是不够的 —— 球贴着篮圈外沿
    #     斜着掠过时，插值出来的穿越点也可能落在圈内阈值里（实测踩过：
    #     59.94s / 127.67s 两个假进球就是这么来的）。
    cup_frames = 0
    for p in chain:
        hp = look(p[0])
        if abs(p[1] - float(hp.cx)) <= float(hp.rx) * cfg.cup_inner:
            cup_frames += 1
    if cup_frames < cfg.min_cup_frames:
        if diagnostic is not None:
            diagnostic["reason"] = "insufficient_cup_frames"
        return None

    size = sum(x[3] for x in chain) / float(len(chain))
    size = 2.0 * math.sqrt(max(1.0, size) / math.pi)

    # 置信度：证据帧数、下落干不干脆、穿筐点正不正
    conf = 0.55
    conf += min(0.20, 0.05 * (len(chain) - cfg.min_chain))
    conf += 0.15 * min(1.0, drop / max(1.0, ry * 3.0))
    conf += 0.10 * (1.0 - min(1.0, abs(cross_x - cx0) / max(1.0, rx)))
    if big_rise > 0:
        conf -= 0.05
    conf = max(0.3, min(0.97, conf))

    return SightShot(
        t=round(float(t_exit), 3), t_enter=round(float(t_enter), 3),
        made=True, hoop_cx=cx0, hoop_cy=cy0, hoop_rx=rx, hoop_ry=ry,
        entry_x=round(chain[0][1], 2), entry_y=round(chain[0][2], 2),
        exit_x=round(chain[-1][1], 2), exit_y=round(chain[-1][2], 2),
        drop_px=round(float(drop), 2), cross_x=round(float(cross_x), 2),
        frames=len(chain), size_px=round(float(size), 2),
        confidence=round(float(conf), 3),
        note="入筐 t=%.2fs → 出筐 t=%.2fs" % (t_enter, t_exit))


def _split_chains(series, cfg: SightConfig, hoop, fps: float,
                  hoop_lookup=None) -> list[list]:
    """把候选块序列切成「一段一段的下落」（诊断/可视化用）。

    注意：判进球**不再**依赖这个切分（那样会把真实下落的抖动切碎、漏掉进球），
    真正的判据在 `_shots_from_blob_series` 的顺序扫描里。这里保留它只是
    为了把轨迹画出来看。
    """
    if not series:
        return []
    look = hoop_lookup or (lambda _t: hoop)
    gap = (cfg.max_gap_frames + 1) / max(1.0, fps)
    out: list[list] = []
    cur = [series[0]]
    for p in series[1:]:
        if p[0] - cur[-1][0] > gap:
            out.append(cur)
            cur = [p]
            continue
        hp = look(p[0])
        rise = (cur[-1][2] - p[2]) / max(1.0, float(hp.ry))
        if rise >= cfg.split_rise_frac:
            out.append(cur)
            cur = [p]
            continue
        cur.append(p)
    out.append(cur)
    return [c for c in out if c]


def _exit_observed(point, hoop, cfg):
    """Measured exit, allowing bounded subpixel uncertainty for central points.

    A disappearing blob is not exit evidence. The complete chain still has to
    pass _judge_span, including cup frames, downward drop and rebound rejection.
    """
    rx, ry = max(1.0, float(hoop.rx)), max(1.0, float(hoop.ry))
    dx = abs(point[1] - float(hoop.cx)) / rx
    below = point[2] - float(hoop.cy)
    if below >= ry:
        return dx <= cfg.cross_inner
    tolerance = min(max(0.0, cfg.exit_pixel_tolerance), ry * .1)
    return below >= ry - tolerance and dx <= min(.6, cfg.cross_inner)


def _shots_from_blob_series(series, hoop, cfg: SightConfig,
                            fps: float = 30.0,
                            hoop_lookup=None, diagnostics=None) -> list[SightShot]:
    """一串候选块里切出所有进球。

    做法是**一次顺序扫描**。为什么不是「先切链再逐段判」：
    切链的规则无论松紧都会出事 —— 切太松，「球砸筐弹起又落回」和真实下落
    糊在一起，回升跨在段外，判据看不见；切太紧，真实下落里那几帧
    「块被网/人搅动导致位置跳变」会把它切碎，反而漏掉进球（实测真丢过一球）。

    所以改成：
      * 先找入筐点（纵向进入篮圈带、横向不离谱、时间上连着一段）；
      * 往后找一个出筐点（落到篮圈下方、横向在圈内），时间不超过 `max_cross_s`；
      * 把「入筐点→出筐点」这一段交给 `_judge_span`，它负责几何判据，
        并且会否掉「中途大幅弹回筐上方」的段。
    """
    out: list[SightShot] = []
    def record(row):
        if diagnostics is not None:
            diagnostics["counts"][row["reason"]] = diagnostics["counts"].get(row["reason"], 0) + 1
            if len(diagnostics["spans"]) < cfg.max_frame_trace:
                diagnostics["spans"].append(row)
            else:
                diagnostics["spans_truncated"] += 1
    if not series:
        return out
    look = hoop_lookup or (lambda _t: hoop)
    gap = (cfg.max_gap_frames + 1) / max(1.0, fps)

    i = 0
    n = len(series)
    while i < n:
        # ---- 找入筐点：纵向进入篮圈带、横向不离谱 ----
        enter_i = None
        for k in range(i, n):
            hp = look(series[k][0])
            gy = (series[k][2] - float(hp.cy)) / max(1.0, float(hp.ry))
            gx = (series[k][1] - float(hp.cx)) / max(1.0, float(hp.rx))
            if gy <= cfg.enter_above_frac and abs(gx) <= cfg.enter_side_frac:
                enter_i = k
                break
        if enter_i is None:
            record({"start":series[i][0], "end":series[-1][0], "reason":"no_entry_in_band"})
            break

        # ---- 往后找第一个出筐点（时间与位置都受约束）----
        exit_i = None
        restart = enter_i + 1
        failure = "no_exit_in_band"
        for j in range(enter_i + 1, n):
            if series[j][0] - series[enter_i][0] > cfg.max_cross_s:
                failure = "exit_timeout"
                break
            if series[j][0] - series[j - 1][0] > gap:
                restart = j
                failure = "candidate_gap"
                break
            hp = look(series[j][0])
            gy = (series[j][2] - float(hp.cy)) / max(1.0, float(hp.ry))
            gx = (series[j][1] - float(hp.cx)) / max(1.0, float(hp.rx))
            if _exit_observed(series[j], hp, cfg):
                exit_i = j
                break
        if exit_i is None:
            record({"start":series[enter_i][0], "end":series[min(j if enter_i+1 < n else enter_i,n-1)][0], "reason":failure})
            i = max(restart, enter_i + 1)
            continue

        detail = {"start":series[enter_i][0], "end":series[exit_i][0], "reason":"geometry_passed"}
        shot = _judge_span(series[enter_i:exit_i + 1], hoop, cfg, look, diagnostic=detail)
        exit_hoop = look(series[exit_i][0])
        detail["exit_tolerance_used"] = (series[exit_i][2] - float(exit_hoop.cy)
                                         < max(1.0, float(exit_hoop.ry)))
        record(detail)
        if shot is not None:
            if detail["exit_tolerance_used"]:
                shot.note += "；出口使用亚像素容差（完整几何证据通过）"
            out.append(shot)
            i = exit_i + 1          # 这一球消费掉，继续找下一球
        else:
            i = enter_i + 1         # 这一段不成立，往后挪一个点重试
    return out


def _visible_hoops(hoop_track, W: int, H: int, cfg: SightConfig,
                   min_frac: float = 0.03):
    """把「篮筐轨迹」按画面位置聚成一个个篮筐。

    全场视频里会同时检测到左右两个篮筐，而且每个篮筐在时间上是**一串样本**
    （`HoopTrack.samples`），不是一个框。这里按位置聚类，返回
    [(代表位置 Hoop, 该篮筐的样本列表)]，只保留在画面内、且出现过足够多帧的。

    远景那个篮筐常常只在少数帧被检出，把它算进来只会喂进不可靠的坐标。

    **特例：人工标点。** 如果 `hoop_track` 是用户标出来的（`method="manual"`，
    只有一条样本），直接返回它 —— 这是「用户已经在画面上点过篮筐」的路径，
    不该再被"覆盖率 / 聚类"这些为自动检测设计的门槛筛掉。
    """
    samples = list(getattr(hoop_track, "samples", []) or [])
    if not samples:
        return []
    if str(getattr(samples[0][1], "method", "")).startswith("manual"):
        h = samples[0][1]
        if 0 <= h.cx <= W and 0 <= h.cy <= H and float(h.rx) > 0:
            return [(h, samples)]
        return []
    total_frames = max(1, int(getattr(hoop_track, "frames", 0) or len(samples)))
    gate = max(60.0, 2.0 * float(getattr(samples[0][1], "rx", 40.0)))

    groups: list[list] = []
    for t, h in samples:
        if not (0 <= h.cx <= W and 0 <= h.cy <= H):
            continue
        placed = False
        for g in groups:
            gx = sum(x.cx for _tt, x in g) / len(g)
            gy = sum(x.cy for _tt, x in g) / len(g)
            if math.hypot(h.cx - gx, h.cy - gy) <= gate:
                g.append((t, h))
                placed = True
                break
        if not placed:
            groups.append([(t, h)])

    out = []
    for g in groups:
        if len(g) / float(total_frames) < min_frac:
            continue
        rep = _median_hoop([h for _t, h in g])
        out.append((rep, sorted(g, key=lambda s: s[0])))
    out.sort(key=lambda kv: -len(kv[1]))
    return out


def _median_hoop(hs):
    """一组 Hoop 样本的中位数代表位置。"""
    import statistics as st
    from .hoop import Hoop
    return Hoop(cx=st.median([h.cx for h in hs]),
                cy=st.median([h.cy for h in hs]),
                rx=st.median([h.rx for h in hs]),
                ry=st.median([h.ry for h in hs]),
                board=None, votes=len(hs), confidence=1.0, method="median")


def _hoop_at(samples, t: float, fallback):
    """取 t 时刻该篮筐的位置（样本里最近的一条），拿不到就用代表位置。"""
    if not samples:
        return fallback
    best = None
    bd = 1e18
    for tt, h in samples:
        d = abs(tt - t)
        if d < bd:
            bd, best = d, h
    return best if (best is not None and bd <= 2.0) else fallback



# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def scan_hoopsight(video_path: str, cfg: Optional[SightConfig] = None,
                   hoop_cfg: Optional[HoopConfig] = None,
                   hoop_track=None, weights: str = "", hoop_hint=None,
                   device: str = "cpu",
                   progress: Optional[Callable[[float, str], None]] = None
                   ) -> SightScan:
    """扫全片：定位篮筐 → 逐帧找篮下运动块 → 判穿筐 → 输出进球事件。"""
    cfg = cfg or SightConfig()
    cv2, np = _require_cv()

    if hoop_track is None:
        hoop_track = detect_hoop_track(video_path, hoop_cfg or HoopConfig(),
                                       weights=weights, device=device, hint=hoop_hint,
                                       progress=progress)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise HoopSightError(f"打不开视频：{video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    hoops = _visible_hoops(hoop_track, W, H, cfg)
    if not hoops:
        cap.release()
        raise HoopSightError("这段视频里没有定位到可用的篮筐")

    wins = {id(h): _window(cfg, h, W, H) for h, _s in hoops}
    series: dict = {id(h): [] for h, _s in hoops}
    blob_stats: list = []
    people: list = []

    # 中值背景：固定机位下最干净的前景来源（球/人 vs 场地）。
    # 拿不到就退回帧差，判据照样工作，只是对慢速物体差一些。
    bg_gray = None
    if cfg.people_use_bg:
        try:
            from .ball import build_background
            bg = build_background(video_path, n=cfg.people_bg_samples)
            if bg is not None:
                bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        except Exception:
            bg_gray = None

    prev_gray = None
    idx = 0
    nblobs = 0
    candidate_trace = {"frames": [], "counts": {}, "truncated": 0, "limit": cfg.max_frame_trace, "spans": [], "spans_truncated": 0}
    while True:
        ok = cap.grab()
        if not ok:
            break
        if cfg.stride > 1 and idx % cfg.stride:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None and prev_gray.shape == gray.shape:
            t = idx / fps
            for hoop_index, (h, samples) in enumerate(hoops):
                cur = _hoop_at(samples, t, h)
                w = wins[id(h)]
                keep_stats = len(blob_stats) < cfg.max_blob_stats
                trace = {"t": round(t, 4), "frame": idx, "hoop_index": hoop_index,
                         "window": list(w), "hoop": [cur.cx, cur.cy, cur.rx, cur.ry]}
                blobs = _blobs_in_window(cv2, np, prev_gray, gray, w, cur, cfg,
                                         stats_out=blob_stats if keep_stats else None, trace=trace)
                nblobs += len(blobs)
                b = _pick_blob(blobs, cur, cfg)
                if b is not None:
                    trace.update(stage="selected", selected=list(b))
                elif blobs:
                    trace["stage"] = "low_small_blob_rejected"
                stage = trace["stage"]
                candidate_trace["counts"][stage] = candidate_trace["counts"].get(stage, 0) + 1
                if len(candidate_trace["frames"]) < cfg.max_frame_trace:
                    candidate_trace["frames"].append(trace)
                else:
                    candidate_trace["truncated"] += 1
                if b is not None:
                    series[id(h)].append((t, b[0], b[1], float(b[2])))
                # 筐下的人：和判进球同一次扫描里做，不额外读一遍视频
                if cfg.detect_people:
                    for p in _person_blobs(cv2, np, frame, bg_gray,
                                           prev_gray, cur, cfg):
                        p.t = round(t, 3)
                        p.frame = idx
                        people.append(p)
        prev_gray = gray
        if progress and total and (idx % 60 == 0):
            progress(0.5 + 0.5 * idx / total, "篮下判进球")
        idx += 1
    cap.release()

    scan = SightScan(fps=fps, duration=total / fps if fps else 0.0,
                     frames=idx, blobs=nblobs)
    from dataclasses import asdict
    candidate_trace["config"] = asdict(cfg)
    candidate_trace["fps"] = fps
    scan.candidate_trace = candidate_trace
    scan.blob_stats = blob_stats
    scan.people = [p.to_dict() for p in people]
    scan.note_people = (f"筐下检出人形块 {len(people)} 个"
                        + ("" if people else "（等于 0：这段素材里筐下确实检不到人）"))
    scan.windows = [dict(hoop=[round(float(h.cx), 1), round(float(h.cy), 1)],
                         win=list(w)) for h, _s in hoops for w in [wins[id(h)]]]
    scan.hoops = [dict(cx=round(float(h.cx), 1), cy=round(float(h.cy), 1),
                       rx=round(float(h.rx), 1), ry=round(float(h.ry), 1),
                       frames=len(s))
                  for h, s in hoops]
    for h, samples in hoops:
        s = series[id(h)]
        if not s:
            continue
        # 判进球时用**该时刻的**篮筐位置（篮筐会随镜头漂移）
        shots = _shots_from_blob_series(
            s, h, cfg, fps=fps,
            hoop_lookup=lambda t, _s=samples, _f=h: _hoop_at(_s, t, _f),
            diagnostics=candidate_trace)
        scan.shots.extend(shots)

    # 置信度门槛：低于门槛的判定**不作为进球输出**（但仍会进 rejected 供诊断）。
    # 这一条是被实测逼出来的：nybo 那段 3 个误报的置信度是 0.896/0.917/0.970，
    # 没有门槛时它们全被当成进球，用户看到的比分是错的。
    raw = list(scan.shots)
    scan.shots = [s for s in raw if float(s.confidence) >= cfg.min_confidence]
    scan.rejected = [s.to_dict() for s in raw
                     if float(s.confidence) < cfg.min_confidence]

    # 去重：同一时刻只留一个（两个篮筐的窗口可能重叠）
    scan.shots.sort(key=lambda x: (-x.confidence, x.t))
    kept: list[SightShot] = []
    for s in scan.shots:
        if any(abs(s.t - k.t) < cfg.min_shot_gap_s for k in kept):
            continue
        kept.append(s)
    kept.sort(key=lambda x: x.t)
    scan.shots = kept
    scan.note = (f"篮筐 {len(hoops)} 个，候选块 {nblobs} 个，"
                 f"判出进球 {len(kept)} 次"
                 + (f"（另有 {len(scan.rejected)} 个低于置信度门槛 "
                    f"{cfg.min_confidence}，未计入）"
                    if scan.rejected else ""))
    # 判据余量诊断：判不了就明说（不许给一个像模像样的错比分）
    if hoops:
        scan.diagnosis = _judge_diagnosis(hoops[0][0], H, cfg)
        if not scan.diagnosis.get("judge_reliable", True):
            scan.note += "；⚠ " + scan.diagnosis.get("reason", "")
    return scan


@dataclass
class PersonBlob:
    """篮筐附近检出的「像个人」的运动块（用于判进球是哪一队）。"""

    t: float
    cx: float
    cy: float
    w: int
    h: int
    area: int
    dist_px: float
    aspect: float
    colour: Optional[tuple] = None      # 上半部主色 (H, S, V)
    colour_name: str = ""
    hoop_cx: float = 0.0
    hoop_cy: float = 0.0
    frame: int = 0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        if self.colour is not None:
            d["colour"] = [round(float(v), 1) for v in self.colour]
        return d


def _colour_name(h: float, s: float, v: float) -> str:
    """把一个 HSV 主色说成人话。**先判白/灰**再判色相 ——
    浅色球衣在阴影里色相是噪声（实测白球衣的众数色相落在蓝区）。"""
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


def _upper_colour(cv2, np, frame, x: int, y: int, w: int, h: int):
    """块的上半部主色：人 → 球衣；球+网/地板 → 别的颜色。"""
    yy1 = int(y + h * 0.45)
    xx0 = int(x + w * 0.15)
    xx1 = int(x + w * 0.85)
    H, W = frame.shape[:2]
    y0, y1 = max(0, y), max(0, min(H, yy1))
    x0, x1 = max(0, xx0), max(0, min(W, xx1))
    if y1 - y0 < 6 or x1 - x0 < 6:
        return None
    patch = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    hh, ss, vv = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    sel = hsv[(vv >= 35) & (vv <= 250)]
    if len(sel) < 20:
        return None
    bins = (sel[:, 0] // 16).astype(np.int32)
    vals, cnts = np.unique(bins, return_counts=True)
    top = vals[int(np.argmax(cnts))]
    grp = sel[bins == top]
    return (float(np.median(grp[:, 0])), float(np.median(grp[:, 1])),
            float(np.median(grp[:, 2])))


def _person_blobs(cv2, np, frame, bg_gray, prev_gray, hoop, cfg: SightConfig):
    """篮筐附近「像人」的运动块。

    判据：**形状 + 尺寸**，不是面积。
      * 高 / 宽 >= `people_min_aspect`（人是竖的）；
      * 高度 >= `people_min_h_px`（远景里一个球员至少在画面里这么高）；
      * 面积 >= `people_min_area`。
    为什么必须用形状：实测「球穿过网」那一瞬间，球和网连成一个 1200~1700px
    的方块（长宽比 ≈ 1），面积和远处一个人差不多 —— 只看面积一定会把它当人。
    """
    r = max(float(hoop.rx), float(hoop.ry))
    size = max(int(cfg.people_win_min_px), int(round(r * cfg.people_win_scale)))
    W = frame.shape[1]
    H = frame.shape[0]
    x0 = max(0, int(float(hoop.cx) - size * 0.5))
    y0 = max(0, int(float(hoop.cy) - size * 0.15))
    x1 = min(W, int(float(hoop.cx) + size * 0.5))
    y1 = min(H, int(float(hoop.cy) + size * 0.85))
    if x1 - x0 < 40 or y1 - y0 < 40:
        return []
    gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    ref = None
    if bg_gray is not None and bg_gray.shape == frame.shape[:2]:
        ref = bg_gray[y0:y1, x0:x1]
    if ref is not None:
        d = cv2.absdiff(gray, ref)
    else:                                    # 没有背景就退回帧差
        d = cv2.absdiff(gray, prev_gray[y0:y1, x0:x1])
    m = (d >= cfg.motion_thresh).astype(np.uint8) * 255
    k = np.ones((5, 5), np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        bx, by, bw, bh, area = (int(stats[i, 0]), int(stats[i, 1]),
                                int(stats[i, 2]), int(stats[i, 3]),
                                int(stats[i, 4]))
        if bh < cfg.people_min_h_px or area < cfg.people_min_area:
            continue
        aspect = bh / float(max(1, bw))
        if aspect < cfg.people_min_aspect or aspect > cfg.people_max_aspect:
            continue
        fill = area / float(max(1, bw * bh))
        if fill < cfg.people_min_fill:
            continue
        cx, cy = float(cent[i][0]) + x0, float(cent[i][1]) + y0
        dist = math.hypot(cx - float(hoop.cx), cy - float(hoop.cy))
        if dist > float(hoop.rx) * cfg.people_max_dist_rx:
            continue
        col = _upper_colour(cv2, np, frame, bx + x0, by + y0, bw, bh)
        out.append(PersonBlob(
            t=0.0, cx=cx, cy=cy, w=bw, h=bh, area=area,
            dist_px=round(dist, 1), aspect=round(aspect, 2), colour=col,
            colour_name=(_colour_name(*col) if col else ""),
            hoop_cx=float(hoop.cx), hoop_cy=float(hoop.cy)))
    return out


def make_team_sheet(video_path: str, scan: SightScan, player_obs,
                    jersey_colors: Optional[dict] = None,
                    out_path: str = "out/hoopsight_team.png",
                    zoom: float = 2.6, pad: int = 30,
                    max_dist_px: float = 340.0) -> str:
    """把每次进球的**筐下球员**画出来 —— 用来核对「这球是哪一队进的」。

    每一行一次进球：横排若干帧（进球前后），把穿筐瞬间离篮筐最近的几名球员
    的框画出来，并标上「队别 + 球衣颜色 + 离筐距离」。这样「哪一队进的」
    就不再是一个孤零零的结论，而是一张能对得上的图。

    颜色标签来自 `VideoSource._sample_jersey_colors()` 的 `legend`，
    所以图上会同时出现「home=黄 / away=白」这类对照，防止标签被理解反。
    """
    cv2, np = _require_cv()
    shots = list(getattr(scan, "shots", []) or [])
    if not shots:
        return ""
    legend = {}
    if isinstance(jersey_colors, dict):
        legend = dict(jersey_colors.get("legend") or {})
    cap = cv2.VideoCapture(video_path)
    fps = scan.fps or 30.0
    rows = []
    for s in shots:
        rx = max(20.0, float(s.hoop_rx))
        x0 = int(max(0, s.hoop_cx - rx * 1.6 - pad))
        x1 = int(s.hoop_cx + rx * 2.4 + pad)
        y0 = int(max(0, s.hoop_cy - rx * 1.3 - pad))
        y1 = int(s.hoop_cy + rx * 2.0 + pad)
        f_hit = int(round(s.t * fps))
        tiles = []
        for f in range(f_hit - 10, f_hit + 6, 1):
            if f < 0:
                continue
            cap.set(cv2.CAP_PROP_POS_FRAMES, f)
            ok, fr = cap.read()
            if not ok:
                continue
            crop = fr[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            big = cv2.resize(crop, None, fx=zoom, fy=zoom,
                             interpolation=cv2.INTER_LANCZOS4)
            cv2.ellipse(big, (int((s.hoop_cx - x0) * zoom),
                              int((s.hoop_cy - y0) * zoom)),
                        (int(s.hoop_rx * zoom), int(s.hoop_ry * zoom)),
                        0, 0, 360, (0, 0, 255), 1)
            # 这一帧里离筐最近的球员（按脚底点）
            tt = f / fps
            best = []
            for o in (player_obs or []):
                if abs(float(o.get("t", -9)) - tt) > 0.12:
                    continue
                d = math.hypot(float(o.get("foot_x", 0)) - s.hoop_cx,
                               float(o.get("foot_y", 0)) - s.hoop_cy)
                if d <= max_dist_px:
                    best.append((d, o))
            best.sort(key=lambda kv: kv[0])
            for d, o in best[:3]:
                bx1, by1, bx2, by2 = [float(v) for v in o.get("box", [0, 0, 0, 0])]
                if bx2 <= x0 or bx1 >= x1 or by2 <= y0 or by1 >= y1:
                    continue
                col = (0, 255, 0) if o.get("team") == "home" else (255, 160, 0)
                p1 = (int((bx1 - x0) * zoom), int((by1 - y0) * zoom))
                p2 = (int((bx2 - x0) * zoom), int((by2 - y0) * zoom))
                cv2.rectangle(big, p1, p2, col, 2)
                jc = {}
                if isinstance(jersey_colors, dict):
                    jc = (jersey_colors.get("per_player") or {}).get(
                        str(o.get("player_id")), {}) or {}
                tag = f"{o.get('team')}/{jc.get('label', '?')} {d:.0f}px"
                cv2.putText(big, tag, (p1[0], max(12, p1[1] - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)
                # 把**采样到的球衣色块**直接涂在框旁边：标签可能写错，
                # 色块不会 —— 一眼就能看出「这个框到底是黄队还是白队」。
                hsv = jc.get("hsv")
                if hsv:
                    sw = np.uint8([[[int(hsv[0]), int(hsv[1]), int(hsv[2])]]])
                    bgr = cv2.cvtColor(sw, cv2.COLOR_HSV2BGR)[0][0]
                    cv2.rectangle(big, (p2[0] + 2, p1[1]),
                                  (p2[0] + 14, p1[1] + 14),
                                  (int(bgr[0]), int(bgr[1]), int(bgr[2])), -1)
            cv2.putText(big, f"{tt:.2f}s", (4, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            if abs(tt - s.t) < 1e-6:
                cv2.putText(big, "EXIT", (4, big.shape[0] - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
            tiles.append(big)
        if not tiles:
            continue
        hmax = max(t.shape[0] for t in tiles)
        tiles = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0], 0, 0,
                                    cv2.BORDER_CONSTANT, value=(0, 0, 0))
                 for t in tiles]
        row = np.hstack(tiles)
        head = np.zeros((34, row.shape[1], 3), np.uint8)
        txt = (f"t={s.t:.2f}s  队别 legend: "
               + "  ".join(f"{k}={v}" for k, v in legend.items())
               + "   绿框=home  橙框=away")
        cv2.putText(head, txt, (6, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1)
        rows.append(np.vstack([head, row]))
    cap.release()
    if not rows:
        return ""
    wmax = max(r.shape[1] for r in rows)
    rows = [cv2.copyMakeBorder(r, 0, 0, 0, wmax - r.shape[1],
                               cv2.BORDER_CONSTANT, value=(0, 0, 0))
            for r in rows]
    sheet = np.vstack(rows)
    import os
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cv2.imwrite(out_path, sheet)
    return out_path


def _judge_diagnosis(hoop, frame_h: int, cfg: SightConfig) -> dict:
    """这段素材上「球从筐上方落下」这个判据还有多少余量？

    为什么要有它：`hoopsight` 的核心判据依赖"球从篮圈**上方**穿到下方"。
    但固定机位里篮筐常常紧贴画面顶边 —— 上方根本没有像素，判据就退化成
    "任何从上方斜掠而过的块都算穿筐"。实测 `nybo_3min` 就是这样：
    7 个候选全是误报（人工真值 0 进球），precision = 0%。
    这里把"上方余量"量出来，**判不了就如实说出来**，而不是给一个像模像样的比分。
    """
    top = float(hoop.cy) - float(hoop.ry)
    margin = top                      # 篮圈上沿到画面顶边还有多少像素
    need = float(hoop.ry) * 1.5       # 判据希望上方至少有这么高
    ok = margin >= need
    d = {"hoop_top_px": round(top, 1), "frame_top_margin_px": round(margin, 1),
         "need_margin_px": round(need, 1), "judge_reliable": bool(ok)}
    if not ok:
        d["reason"] = (
            f"篮筐上沿距画面顶边只有 {margin:.0f}px（判据需要 ≥{need:.0f}px）—— "
            "『球从筐上方落下』无从判断，任何从上方斜掠而过的球都会满足条件。"
            "这段素材的自动判定**不可信**，请用 --marks + 界面上的『进球确认』"
            "人工逐球判定，或用 --no-hoop-sight 关掉自动计分。")
    return d


def make_person_sheet(video_path: str, scan: SightScan, people,
                      out_path: str = "out/hoopsight_people.png",
                      zoom: float = 2.2, pad: int = 40,
                      window_s: float = 2.5) -> str:
    """把每次进球前后「筐下检出的人」画出来 —— 判队这件事的直接证据图。

    每一行一次进球：横排若干帧，画出该时刻检出的「像人」的运动块
    （框 + 上半部主色 + 离筐距离），并标出篮圈位置。**如果一行里一个框都没有**，
    那就说明这段素材在筐下确实检不到人（而不是我们没去检）—— 这句话必须
    写在图上，否则「判不出队」看起来像偷懒。
    """
    cv2, np = _require_cv()
    shots = list(getattr(scan, "shots", []) or [])
    if not shots:
        return ""
    cap = cv2.VideoCapture(video_path)
    fps = scan.fps or 30.0
    rows = []
    for s in shots:
        rx = max(20.0, float(s.hoop_rx))
        x0 = int(max(0, s.hoop_cx - rx * 2.6 - pad))
        x1 = int(s.hoop_cx + rx * 2.6 + pad)
        y0 = int(max(0, s.hoop_cy - rx * 1.2 - pad))
        y1 = int(s.hoop_cy + rx * 2.6 + pad)
        in_win = [p for p in (people or [])
                  if abs(float(p.get("t", 0.0)) - s.t) <= window_s]
        picks = []
        if in_win:
            # 取「离筐最近 / 最近时刻」的若干帧来展示
            best = min(in_win, key=lambda p: (p.get("dist_px", 9e9),
                                              abs(p.get("t", 0) - s.t)))
            near_t = float(best.get("t", s.t))
            picks = sorted({round(float(p["t"]), 3) for p in in_win
                            if abs(float(p["t"]) - near_t) < 1.2})[:8]
        if not picks:
            picks = [round(s.t + d, 3) for d in (-0.8, -0.4, 0.0, 0.4)]
        tiles = []
        for tt in picks:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(tt * fps)))
            ok, fr = cap.read()
            if not ok:
                continue
            crop = fr[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            big = cv2.resize(crop, None, fx=zoom, fy=zoom,
                             interpolation=cv2.INTER_LANCZOS4)
            cv2.ellipse(big, (int((s.hoop_cx - x0) * zoom),
                              int((s.hoop_cy - y0) * zoom)),
                        (int(s.hoop_rx * zoom), int(s.hoop_ry * zoom)),
                        0, 0, 360, (0, 0, 255), 1)
            for p in in_win:
                if abs(float(p.get("t", 0)) - tt) > 0.06:
                    continue
                bx1 = (float(p["cx"]) - p["w"] / 2.0 - x0) * zoom
                by1 = (float(p["cy"]) - p["h"] / 2.0 - y0) * zoom
                bx2 = (float(p["cx"]) + p["w"] / 2.0 - x0) * zoom
                by2 = (float(p["cy"]) + p["h"] / 2.0 - y0) * zoom
                cv2.rectangle(big, (int(bx1), int(by1)), (int(bx2), int(by2)),
                              (0, 255, 0), 2)
                tag = f"{p.get('colour_name', '?')} {p.get('dist_px', 0):.0f}px h{p['h']}"
                cv2.putText(big, tag, (int(bx1), max(12, int(by1) - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
                col = p.get("colour")
                if col:
                    sw = np.uint8([[[int(col[0]), int(col[1]), int(col[2])]]])
                    bgr = cv2.cvtColor(sw, cv2.COLOR_HSV2BGR)[0][0]
                    cv2.rectangle(big, (int(bx2) + 2, int(by1)),
                                  (int(bx2) + 16, int(by1) + 16),
                                  (int(bgr[0]), int(bgr[1]), int(bgr[2])), -1)
            cv2.putText(big, f"{tt:.2f}s", (4, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            tiles.append(big)
        if not tiles:
            continue
        hmax = max(t.shape[0] for t in tiles)
        tiles = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0], 0, 0,
                                    cv2.BORDER_CONSTANT, value=(0, 0, 0))
                 for t in tiles]
        row = np.hstack(tiles)
        head = np.zeros((34, row.shape[1], 3), np.uint8)
        n_in = len(in_win)
        txt = (f"t={s.t:.2f}s  筐下检出的人形块: {n_in}"
               + ("" if n_in else "  ← 这段素材在筐下确实没检出人（不是没检）"))
        cv2.putText(head, txt, (6, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1)
        rows.append(np.vstack([head, row]))
    cap.release()
    if not rows:
        return ""
    wmax = max(r.shape[1] for r in rows)
    rows = [cv2.copyMakeBorder(r, 0, 0, 0, wmax - r.shape[1],
                               cv2.BORDER_CONSTANT, value=(0, 0, 0))
            for r in rows]
    sheet = np.vstack(rows)
    import os
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cv2.imwrite(out_path, sheet)
    return out_path


def make_verify_sheet(video_path: str, scan: SightScan,
                      out_path: str = "out/hoopsight_verify.png",
                      zoom: float = 3.0, pad: int = 40) -> str:
    """把每次进球前后的篮筐放大图拼成接触表 —— 自动结果必须能肉眼复核。

    这是「不许编答案」这条原则的执行方式：结论旁边永远配一张能对得上的图。
    """
    cv2, np = _require_cv()
    if not scan.shots:
        return ""
    cap = cv2.VideoCapture(video_path)
    fps = scan.fps or 30.0
    rows = []
    for s in scan.shots:
        rx = max(20.0, float(s.hoop_rx))
        x0 = int(max(0, s.hoop_cx - rx * 1.4 - pad))
        x1 = int(s.hoop_cx + rx * 1.4 + pad)
        y0 = int(max(0, s.hoop_cy - rx * 1.2 - pad))
        y1 = int(s.hoop_cy + rx * 1.6 + pad)
        f_exit = int(round(s.t * fps))
        tiles = []
        for f in range(f_exit - 8, f_exit + 5):
            if f < 0:
                continue
            cap.set(cv2.CAP_PROP_POS_FRAMES, f)
            ok, fr = cap.read()
            if not ok:
                continue
            crop = fr[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            big = cv2.resize(crop, None, fx=zoom, fy=zoom,
                             interpolation=cv2.INTER_LANCZOS4)
            cv2.putText(big, f"{f/fps:.2f}s", (4, 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            cv2.ellipse(big,
                        (int((s.hoop_cx - x0) * zoom), int((s.hoop_cy - y0) * zoom)),
                        (int(s.hoop_rx * zoom), int(s.hoop_ry * zoom)),
                        0, 0, 360, (0, 0, 255), 1)
            if abs(f / fps - s.t) < 1e-6:
                cv2.putText(big, "EXIT", (4, big.shape[0] - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            tiles.append(big)
        if not tiles:
            continue
        hmax = max(t.shape[0] for t in tiles)
        tiles = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0], 0, 0,
                                    cv2.BORDER_CONSTANT, value=(0, 0, 0))
                 for t in tiles]
        rows.append(np.hstack(tiles))
    cap.release()
    if not rows:
        return ""
    wmax = max(r.shape[1] for r in rows)
    rows = [cv2.copyMakeBorder(r, 0, 0, 0, wmax - r.shape[1],
                               cv2.BORDER_CONSTANT, value=(0, 0, 0))
            for r in rows]
    sheet = np.vstack(rows)
    import os
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cv2.imwrite(out_path, sheet)
    return out_path
