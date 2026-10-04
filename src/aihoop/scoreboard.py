"""记分牌识别 —— 直接从画面里的广播比分牌（score bug）读出比分。

**为什么必须补这一层（这是「3 分钟视频跑出 0:0」的根因）：**

  COCO 预训练的 YOLOv8 里根本没有「篮球」这个类（只有 sports ball，
  而 sports ball 在 720p 远景比赛画面里几乎不触发 —— 实测整段 3 分钟视频
  只命中 2 个点）。球轨迹一断，「出手 -> 命中」整条链路就没有输入，
  于是 attempts 为空 -> 比分为 0:0。

  而**广播比分牌是视频里最干净、最权威的一路证据**：它由导播系统直接渲染，
  数字零噪声、不受遮挡，读出来就是真值。所以自动路径改成三路证据融合：

    1. 比分牌 OCR   -> 比分 / 节次 / 每次得分的球队与分值   ← 本模块
    2. 球轨迹 + 篮筐 -> 出手时刻、出手位置、打铁           ← ball.py
    3. 人工复核     -> 低置信度兜底                         ← 已有

  分工是明确的：比分牌负责**计分与命中判定**（它只告诉你「谁得了多少分」），
  球轨迹负责**位置与出手归因**（它告诉你「从哪投的」）。

**识别链路（免训练、免 OCR 库，只用 OpenCV + numpy）：**

  1. 定位比分牌：抽若干帧各自找「饱和渐变色横条」，取中位数框（抗单帧误检）。
  2. 找比分行：比分牌内白色文字的行投影，取像素量最大的那一条。
  3. 标定字段：「画面里哪些列在变化」—— 会变的就是比分数字。
     对整段视频做逐列时间方差，变化列分成左右两簇 = 主队/客队比分字段，
     中间那段就是节次字段。**这一步不需要认识任何数字**。
  4. 读比分：把「字段窗口」整块当图案做模板匹配。

**为什么不逐字识别数字：**
  一开始我走的是「切字符 -> 归一化 -> 和数字模板比」的路。踩了两个坑：
    * 两位数（"10"/"13"）的两个数字在 JPEG 下会粘成一个连通域；
    * 归一化到统一尺寸会把宽度信息抹掉，这个台标字体里「1」(9px 宽) 和
      「7」(16px 宽) 拉完几乎一样，还原生像素尺寸后又被背景干扰。
  逐字识别要同时解决分割、归一化、字体三件事，很脆。
  而固定机位 + 固定台标下，**同一个比分值在每一帧的像素几乎完全相同** ——
  直接把整个字段窗口当图案聚类，一类就是一个比分值，匹配距离接近 0。
  代价只是「每个频道要一次性人工标注一遍图案」（几十秒），
  换来的是几乎不会出错的读数。这笔账非常划算。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence


class ScoreboardError(RuntimeError):
    """比分牌识别不可用（缺依赖 / 视频里找不到比分牌 / 没标定模板）。"""


def _require_cv():
    """把 cv2/numpy 做成软依赖 —— 合成 demo 那条路不需要它们。"""
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover - 取决于运行环境
        raise ScoreboardError(
            "比分牌识别需要 opencv-python 与 numpy。"
            "安装：pip install -r requirements-full.txt") from e
    return cv2, np


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class ScoreBugConfig:
    """比分牌识别阈值 —— 全部集中在这里，现场好调。"""

    # ---- 定位比分牌 ----
    search_bottom: float = 0.68      # 只在画面下方 (1-search_bottom) 找
    min_val: int = 110               # 比分牌底色是饱和渐变色
    min_aspect: float = 3.2          # 比分牌是横条
    max_aspect: float = 11.0
    min_height: int = 28
    max_height: int = 130
    locate_frames: int = 41          # 抽多少帧投票
    locate_tolerance: float = 0.25   # 与中位数框偏差超过该比例的当离群点丢掉

    # ---- 定位比分牌：放宽兜底 ----
    # 有些转播台标是**又长又扁的一条**：实测这段 960×544 素材里台标是
    # 570×24（长宽比 23.8、高只有 24px），严格阈值（max_aspect=11、
    # min_height=28）会把它**整条挡掉**，于是报「画面里没有广播比分牌」——
    # 而画面里明明写着 KPH 27 : AHS 35。严格阈值一个候选都找不到时，
    # 再跑一遍放宽几何 + **按白字占比排序**的兜底：
    # 白字才是台标的硬特征，几何（横条、长宽比）只是先验。
    relaxed_min_aspect: float = 2.5
    relaxed_max_aspect: float = 34.0
    relaxed_min_height: int = 12
    relaxed_max_height: int = 170
    relaxed_min_frac: float = 0.5    # 兜底候选要在这么多比例的帧里都出现
    text_score_floor: float = 0.02   # 候选框的白字占比下限（低于它不当台标）
    # 「像一排白字」的结构判据（见 bug_glyph_score）：块数、字高、是不是单行。
    # 只按白字占比排序时，"顶部看台"会以 0.118 赢过真台标 0.115（实测），
    # 加上结构判据后真台标 0.11 vs 看台 0.03，才真正分得开。
    glyph_min_components: int = 10
    glyph_min_height: int = 8
    glyph_row_frac: float = 0.55
    glyph_row_pad: float = 6.0
    glyph_score_floor: float = 0.04

    # ---- 定位：叠加层稳定性（首选，见 locate_score_bug_static）----
    static_frames: int = 24          # 抽多少帧量"逐像素时间极差"
    static_range_thresh: int = 14    # 灰度高差小于它 = 这个像素全片几乎没变
    static_min_width: int = 80
    static_min_height: int = 10
    static_max_height: int = 200     # 太高说明整幅画面都不动（镜头静止），不适用

    # ---- 切比分行 ----
    row_min_frac: float = 0.40       # 比分行不早于比分牌高度的 40%
    # 「白度」阈值：白度 = V×(255-S)/255。白字接近 255，彩色渐变底只有几十
    white_score_thresh: int = 150
    row_pad: int = 2                 # 比分行上下各留一点，别把字的边缘切掉

    # ---- 标定字段 ----
    calib_frames: int = 200          # 标定字段时抽多少帧解析词结构
    word_gap_ratio: float = 0.80     # 字间距 > 0.8×字宽 认为换词
    field_pad: int = 4               # 字段窗口左右各外扩

    # ---- 图案匹配 ----
    max_match_dist: float = 0.10     # 窗口图案的最大可接受 RMS 距离
    min_match_margin: float = 0.012  # 最佳与次佳的差距，太小视为歧义

    # ---- 定位守卫 ----
    # 定位出来的框里「白字像素」占比的下限。低于它就是地板/看台色块，
    # 不是台标 —— 见 bug_text_score() 的注释（这是实测踩过的坑）。
    min_bug_text_frac: float = 0.004

    # ---- 时序 ----
    stride: int = 3                  # 每隔几帧读一次（30fps -> 10Hz 足够）
    confirm_reads: int = 2           # 新比分连续读到几次才采信
    min_confidence: float = 0.5

    template_path: Optional[str] = None


# --------------------------------------------------------------------------
# 比分牌几何
# --------------------------------------------------------------------------
@dataclass
class ScoreBug:
    """比分牌在画面里的位置（像素）。"""

    x: int
    y: int
    w: int
    h: int
    row_top: int = 0                 # 比分行（绝对 y）
    row_bottom: int = 0
    # 三个字段的中心 x（绝对坐标）—— 由 calibrate_fields() 用「变化列」定出来
    home_cx: float = 0.0
    away_cx: float = 0.0
    period_cx: float = 0.0
    field_half_w: float = 24.0
    ok_frames: int = 0               # 标定时成功采样的帧数（诊断用）

    def to_dict(self) -> dict:
        return dict(x=self.x, y=self.y, w=self.w, h=self.h,
                    row_top=self.row_top, row_bottom=self.row_bottom,
                    home_cx=self.home_cx, away_cx=self.away_cx,
                    period_cx=self.period_cx, field_half_w=self.field_half_w,
                    ok_frames=self.ok_frames)

    @staticmethod
    def from_dict(d: dict) -> "ScoreBug":
        keys = ("x", "y", "w", "h", "row_top", "row_bottom", "home_cx",
                "away_cx", "period_cx", "field_half_w", "ok_frames")
        return ScoreBug(**{k: d[k] for k in keys if k in d})


# --------------------------------------------------------------------------
# 定位
# --------------------------------------------------------------------------
def _banner_candidates(frame, cfg: ScoreBugConfig, relaxed: bool = False,
                       band: str = "bottom") -> list[tuple[int, int, int, int]]:
    """在单帧里找所有「像比分牌」的横条候选。

    ``band``：比分牌在画面的哪一带。**这一条以前是写死的**：老代码把画面上方
    68% 整片清零，只在下三分之一找 —— 那是中文转播的习惯位置。实测这段美式
    校园转播的台标（DoubleACS KPH 27 / AHS 35）在**画面顶部**，
    于是被整片裁掉，日志报「画面里没有广播比分牌」，而画面里明明写着比分。
    现在两条带都扫。

    ``relaxed=True`` 走放松几何：色相不限（实测这条底色是深蓝 H≈120，
    默认色相 5~100 只看暖色）、饱和度/亮度下限下调、长宽比与高度范围放宽到
    能容纳「又长又扁的一条」（实测 570×24，长宽比 23.8、高 24px）。
    噪声由调用方用 :func:`bug_text_score`（白字占比）排掉 —— 白字才是台标的硬特征。
    """
    cv2, np = _require_cv()
    H, W = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    if relaxed:
        lo_h, hi_h = 0, 179
        min_s = 40
        min_v = max(25, int(cfg.min_val * 0.45))
        min_asp, max_asp = cfg.relaxed_min_aspect, cfg.relaxed_max_aspect
        min_h, max_h = cfg.relaxed_min_height, cfg.relaxed_max_height
        solid_min = 0.60
    else:
        lo_h, hi_h = 5, 100
        min_s = 60
        min_v = cfg.min_val
        min_asp, max_asp = cfg.min_aspect, cfg.max_aspect
        min_h, max_h = cfg.min_height, cfg.max_height
        solid_min = 0.70
    m = cv2.inRange(hsv, (lo_h, min_s, min_v), (hi_h, 255, 255))
    if band == "bottom":
        m[: int(H * cfg.search_bottom), :] = 0
    elif band == "top":
        m[int(H * (1.0 - cfg.search_bottom)):, :] = 0
    # 横向闭运算：把渐变色段连成一条，纵向少连以免和球场粘连
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 31), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if not (min_h <= h <= max_h) or w < 60:
            continue
        if not (min_asp <= w / float(h) <= max_asp):
            continue
        # 实心度：比分牌是实心矩形，球场色块不会这么规整
        if cv2.contourArea(c) / float(w * h) < solid_min:
            continue
        out.append((x, y, w, h))
    return out


def _score_bug_candidates(frame, cfg: ScoreBugConfig
                          ) -> list[tuple[float, tuple[int, int, int, int]]]:
    """一帧里所有候选框 + 它们的「像一排白字」分数，按分数从高到低。

    先在两条带（下/上）里用严格几何找；找不到就放宽几何再找。
    **两种几何都用 `bug_glyph_score` 统一排序** —— 实测木地板横条与看台横条
    都能通过几何筛选（solidity 0.99、长宽比 5.5），但它们不是"一排白字"，
    结构判据一票就能排掉。
    """
    seen: dict[tuple[int, int, int, int], float] = {}
    for band in ("bottom", "top"):
        for relaxed in (False, True):
            for b in _banner_candidates(frame, cfg, relaxed=relaxed, band=band):
                if b in seen:
                    continue
                bug = ScoreBug(x=b[0], y=b[1], w=b[2], h=b[3])
                if bug_text_score(frame, bug, cfg) < cfg.text_score_floor:
                    continue
                s = bug_glyph_score(frame, bug, cfg)
                if s >= cfg.glyph_score_floor:
                    seen[b] = s
    return sorted(((s, b) for b, s in seen.items()), key=lambda x: -x[0])


def bug_glyph_score(frame, bug: "ScoreBug",
                    cfg: Optional[ScoreBugConfig] = None) -> float:
    """台标的「像不像**一排白字**」打分（0~1）。

    :func:`bug_text_score` 只看白像素**占比**，在真实素材上不够用 ——
    实测（960×544 校园转播，台标在画面顶部）：
      真台标      白字 0.114~0.118 | 连通块 25~26 | 块高中位 12~13 | 白块行跨度 5
      看台（误检） 白字 0.045~0.065 | 连通块 4~8   | 块高中位 4~15  | 白块行跨度 12~27
      木地板      白字 0.039~0.046 | 连通块 40~46 | 块高中位 4     | 白块行跨度 52~132
    只比白字占比时"真台标 0.115"和"看台 0.118"只差千分之三 —— 分不开，
    而按**结构**一眼可分：台标里的白像素是**字形笔画**（块多、块高≈字高、
    全挤在同一条文字行上）；看台是成片亮斑，木地板是细白线。

    返回 ``白字占比 × 块数因子 × 字高因子 × 单行因子``；不是"一排白字"就是 0。
    """
    cfg = cfg or ScoreBugConfig()
    cv2, np = _require_cv()
    if frame is None or bug is None or bug.w < 8 or bug.h < 8:
        return 0.0
    H, W = frame.shape[:2]
    x0, y0 = max(0, int(bug.x)), max(0, int(bug.y))
    x1, y1 = min(W, int(bug.x + bug.w)), min(H, int(bug.y + bug.h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    ws = _white_score(hsv)
    white = float((ws >= cfg.white_score_thresh).mean())
    if white <= 0.0:
        return 0.0
    mask = (ws >= cfg.white_score_thresh).astype(np.uint8)
    n_lab, _lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    comps = [stats[i] for i in range(1, n_lab) if stats[i][4] >= 4]
    if not comps:
        return 0.0
    heights = sorted(int(c[3]) for c in comps)
    h_med = heights[len(heights) // 2]
    ys = [float(cent[i][1]) for i in range(1, n_lab) if stats[i][4] >= 4]
    span = (max(ys) - min(ys)) if ys else 0.0
    n_fac = min(1.0, len(comps) / float(cfg.glyph_min_components))
    h_fac = min(1.0, h_med / float(cfg.glyph_min_height))
    row_fac = 1.0 if span <= max(cfg.glyph_row_pad, cfg.glyph_row_frac * (y1 - y0)) else 0.0
    return white * n_fac * h_fac * row_fac


def bug_text_score(frame, bug: "ScoreBug",
                   cfg: Optional[ScoreBugConfig] = None) -> float:
    """一块区域「像不像比分牌文字」的打分（0~1）。

    为什么必须有这个判据：`locate_score_bug()` 找的是「饱和色的横条」，
    在篮球馆的**木地板**上几乎必然误检 —— 实测一段室内比赛视频自动定位出来的
    比分牌框是 (406, 634, 398, 75)，正好落在球场地板上。这个框一旦被写进
    `data/scoreboard_bug.json`，换一段视频就会继续用它：整片扫下来
    `frames_hit = 0`，于是自动计分整条路径悄悄失效，只留下一句
    「比分牌：读到 0/1440 帧」——**看起来像视频没有比分牌，其实是定位错了**。

    真正的台标/记分牌一定有两个特征：
      * 底色是**饱和**的（底色饱和度低说明这是一块普通赛场画面）；
      * 里面有**白字**（白度 = V×(255-S)/255 的高分像素）。

    返回「像白字的像素」占比；没有白字就是 0.0。
    """
    cfg = cfg or ScoreBugConfig()
    cv2, np = _require_cv()
    if frame is None or bug is None or bug.w < 8 or bug.h < 8:
        return 0.0
    H, W = frame.shape[:2]
    x0, y0 = max(0, int(bug.x)), max(0, int(bug.y))
    x1, y1 = min(W, int(bug.x + bug.w)), min(H, int(bug.y + bug.h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    ws = _white_score(hsv)
    white = float((ws >= cfg.white_score_thresh).mean())
    if white <= 0.0:
        return 0.0
    base_sat = float(np.median(hsv[:, :, 1]))
    if base_sat < 45.0:
        # 底色不饱和 —— 不是台标，是普通画面（地板/观众席/球衣）
        return 0.0
    return white


def bug_looks_valid(video_path: str, bug: "ScoreBug",
                    cfg: Optional[ScoreBugConfig] = None, samples: int = 5,
                    min_frac: float = 0.34, min_score: float = 0.004) -> bool:
    """缓存/复用的比分牌几何还成立吗？（跨视频复用前的守卫）

    单帧可能是球员从台标前走过，所以抽若干帧看**占比**：至少 `min_frac`
    比例的帧里能读到白字才算成立。判不过就当这份几何失效，重新定位。
    """
    cfg = cfg or ScoreBugConfig()
    cv2, _np = _require_cv()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        return False
    n = max(1, int(samples))
    step = max(1, total // (n + 1))
    hits = 0
    got = 0
    try:
        for k in range(n):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(min(total - 1, (k + 1) * step)))
            ok, frame = cap.read()
            if not ok:
                continue
            got += 1
            if bug_text_score(frame, bug, cfg) >= min_score:
                hits += 1
    finally:
        cap.release()
    if got == 0:
        return False
    return (hits / float(got)) >= float(min_frac)


def _median_box(boxes: Sequence[tuple[int, int, int, int]],
                cfg: ScoreBugConfig) -> Optional[tuple[int, int, int, int]]:
    """对候选框取中位数并剔除离群点 —— 单帧误检不会带偏结果。"""
    if not boxes:
        return None

    def med(vals):
        s = sorted(vals)
        return s[len(s) // 2]

    med0 = (med([b[0] for b in boxes]), med([b[1] for b in boxes]),
            med([b[2] for b in boxes]), med([b[3] for b in boxes]))
    keep = [b for b in boxes
            if abs(b[0] - med0[0]) <= med0[2] * cfg.locate_tolerance
            and abs(b[2] - med0[2]) <= med0[2] * cfg.locate_tolerance * 2
            and abs(b[3] - med0[3]) <= med0[3] * cfg.locate_tolerance * 2]
    keep = keep or boxes
    return (med([b[0] for b in keep]), med([b[1] for b in keep]),
            med([b[2] for b in keep]), med([b[3] for b in keep]))


def _white_score(hsv):
    """「白度」图：白字接近 255，比分牌的彩色渐变底只有几十。

    比单纯用 V 阈值稳得多 —— 比分牌底色本身也很亮（绿->黄->橙渐变），
    只看亮度会把底色整片吃掉。乘上 (255-S) 就把彩色底压下去了。
    """
    _cv2, np = _require_cv()
    v = hsv[:, :, 2].astype(np.int32)
    s = hsv[:, :, 1].astype(np.int32)
    return ((v * (255 - s)) // 255).astype(np.uint8)


def _pick_score_row(profile, bug: ScoreBug, cfg: ScoreBugConfig) -> tuple[int, int]:
    """从白色文字行投影里挑出「比分行」（标题行在上，比分行在下）。"""
    if profile is None:
        return bug.y + int(bug.h * cfg.row_min_frac), bug.y + bug.h
    _cv2, np = _require_cv()
    p = np.asarray(profile, dtype=np.int64)
    thresh = max(3, int(p.max() * 0.25)) if p.max() > 0 else 1
    on = p >= thresh
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= 3:
                runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(on)))
    runs = [r for r in runs if r[0] >= bug.h * cfg.row_min_frac] or runs
    if not runs:
        return bug.y + int(bug.h * cfg.row_min_frac), bug.y + bug.h
    # 取「像素量最大」的那条，而不是最后一条 —— 比分牌底部常有球场白线穿过，
    # 它会在最后留一条又短又假的文字行，按位置取就会踩坑。
    top, bottom = max(runs, key=lambda r: int(p[r[0]:r[1]].sum()))
    return bug.y + top - 1, bug.y + bottom + 1


def _set_score_row(cap, idxs, bug: ScoreBug, cfg: ScoreBugConfig) -> None:
    """定出比分牌内部"比分行"的上下边界（白字行投影里取像素量最大的那条）。"""
    cv2, np = _require_cv()
    rows_acc = None
    for i in idxs[:9]:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok:
            continue
        roi = frame[bug.y:bug.y + bug.h, bug.x:bug.x + bug.w]
        ws = _white_score(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV))
        prof = (ws >= cfg.white_score_thresh).sum(axis=1).astype(np.int64)
        rows_acc = prof if rows_acc is None else rows_acc + prof
    bug.row_top, bug.row_bottom = _pick_score_row(rows_acc, bug, cfg)


def static_overlay_candidates(frames: list, cfg: Optional[ScoreBugConfig] = None
                              ) -> list[tuple[float, tuple[int, int, int, int]]]:
    """从若干帧里找「全片几乎不变」的横条候选（= 导播叠加的图形）。

    抽出来单独一层是为了**能用帧数组直接测**（不依赖视频编解码器）：
    有损编码会在叠加层边缘留下几十级灰度的噪声，测试里用无损/内存帧才能
    稳定复现算法本身的行为。

    返回 ``[(字形分, (x,y,w,h)), ...]``，按分从高到低。
    """
    cfg = cfg or ScoreBugConfig()
    cv2, np = _require_cv()
    if len(frames) < 4:
        return []
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    st = np.stack(grays).astype(np.int16)
    rng = (st.max(axis=0) - st.min(axis=0)).astype(np.uint8)
    stable = (rng < cfg.static_range_thresh).astype(np.uint8)
    stable = cv2.morphologyEx(stable, cv2.MORPH_CLOSE,
                              np.ones((5, 25), np.uint8))
    n_lab, _lab, stats, _cent = cv2.connectedComponentsWithStats(stable, 8)
    mid = frames[len(frames) // 2]
    out: list[tuple[float, tuple[int, int, int, int]]] = []
    for i in range(1, n_lab):
        x, y, w, h, _area = (int(v) for v in stats[i])
        if w < cfg.static_min_width or not (cfg.static_min_height <= h
                                            <= cfg.static_max_height):
            continue
        bug = ScoreBug(x=x, y=y, w=w, h=h)
        s = bug_glyph_score(mid, bug, cfg)
        if s >= cfg.glyph_score_floor:
            out.append((s, (x, y, w, h)))
    return sorted(out, key=lambda t: -t[0])


def locate_score_bug_static(video_path: str, cfg: Optional[ScoreBugConfig] = None,
                            progress: Optional[Callable[[float, str], None]] = None
                            ) -> Optional[ScoreBug]:
    """按「**叠加层不随时间变化**」定位比分牌 —— 与底色/长宽比/位置都无关。

    为什么需要它（实测踩到）：这段 960×544 校园转播的台标是**深蓝长条**
    （570×24，长宽比 23.8、高 24px），而且底色与它背后的**蓝色墙面同色** ——
    按"饱和横条 + 长宽比"找时，台标会跟整面墙粘成一个巨大轮廓被丢掉；
    按白字占比排序时，看台上那块区域（0.118）还会略微赢过真台标（0.115）。
    按颜色/形状调阈值只是在**赌素材**。

    换个角度就干净了：比分牌是**导播叠加的图形**，全片里**逐像素几乎不变**
    （只有数字会跳），而球场上的人、看台、灯光都在动。所以：
    抽 N 帧算逐像素**时间极差** → 取"几乎不变"的像素 → 横向前景连通 →
    候选块按 :func:`bug_glyph_score`（像不像一排白字）排序。
    实测这段素材：全画面只剩下**一个**稳定连通块，就是台标
    `(202,56,562,27)`（人工真值 198,56,570,24），字形分 0.103。

    返回 None 表示这一招不适用（例如整段镜头完全静止 → 整幅画面都"稳定"，
    这时连通块会大到被尺寸过滤掉），调用方应回退到颜色/几何那条路。
    """
    cfg = cfg or ScoreBugConfig()
    cv2, np = _require_cv()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 2:
        cap.release()
        return None
    n = max(6, int(cfg.static_frames))
    idxs = np.linspace(0, total - 2, n).astype(int)
    frames: list = []
    for k, i in enumerate(idxs):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok:
            continue
        frames.append(frame)
        if progress and k % 4 == 0:
            progress(0.02 + 0.04 * k / max(1, n), "按叠加层稳定性定位比分牌")
    if len(frames) < 6:
        cap.release()
        return None
    cands = static_overlay_candidates(frames, cfg)
    if not cands:
        cap.release()
        return None
    x, y, w, h = cands[0][1]
    bug = ScoreBug(x=x, y=y, w=w, h=h)
    _set_score_row(cap, idxs, bug, cfg)
    cap.release()
    return bug


def locate_score_bug_box(video_path: str, cfg: Optional[ScoreBugConfig] = None,
                         progress: Optional[Callable[[float, str], None]] = None
                         ) -> ScoreBug:
    """只定位比分牌的**横条框**（含比分行），不做字段图案标定。

    为什么单独拆一层：字段标定（`calibrate_fields`）要求台标是它认识的那种
    词结构，样式对不上会抛错；而「框选区域 + OCR」这条路**只需要框**。
    实测这段素材（深蓝长条台标）就是这种：框能定准、白字很清楚，
    但图案模板那套词结构对不上。拆开后 OCR 路不会被字段标定挡住。
    """
    cfg = cfg or ScoreBugConfig()
    cv2, np = _require_cv()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ScoreboardError(f"打不开视频：{video_path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        raise ScoreboardError(f"视频没有可读帧：{video_path}")

    idxs = np.linspace(0, max(0, total - 2), cfg.locate_frames).astype(int)

    # ---- ① 首选：按「叠加层不随时间变化」定位（与底色/长宽比/位置无关）----
    # 实测这条深蓝长条台标只有这一招认得出来：它跟背后的蓝墙同色、长宽比 23.8，
    # 按颜色+几何找必然失败；而"全片唯一不动的区域"就是它。
    try:
        cap.release()
        sb = locate_score_bug_static(video_path, cfg, progress=progress)
    except Exception:  # noqa: BLE001  这一招不适用就回退，不该报错
        sb = None
    if sb is not None:
        return sb

    cap = cv2.VideoCapture(video_path)

    # ---- ② 回退：颜色 + 几何（下带严格 → 上带严格 → 放宽几何 + 字形分）----
    def gather(band: str, relaxed: bool):
        out: list[tuple[float, tuple[int, int, int, int]]] = []
        for k, i in enumerate(idxs):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, frame = cap.read()
            if not ok:
                continue
            if relaxed:
                for s, b in _score_bug_candidates(frame, cfg):
                    if band == "top" and b[1] > frame.shape[0] * 0.35:
                        continue
                    if band == "bottom" and b[1] < frame.shape[0] * 0.5:
                        continue
                    out.append((s, b))
            else:
                for b in _banner_candidates(frame, cfg, relaxed=False, band=band):
                    bug_ = ScoreBug(x=b[0], y=b[1], w=b[2], h=b[3])
                    s = bug_text_score(frame, bug_, cfg)
                    if s >= cfg.min_bug_text_frac:
                        out.append((s, b))
            if progress and k % 5 == 0:
                progress(0.05 + 0.05 * k / max(1, len(idxs)), "定位比分牌")
        return out

    boxes: list[tuple[int, int, int, int]] = []
    for band, relaxed in (("bottom", False), ("top", False), ("bottom", True),
                          ("top", True)):
        got = gather(band, relaxed)
        if got:
            # 严格几何：沿用原来的"所有候选取中位数"（实测 basketball_match 的
            # 底部台标就是靠它稳定命中的）；放宽几何：只取每帧分数最高的一条。
            if relaxed:
                best_per_frame: dict[int, tuple[float, tuple]] = {}
                for s, b in got:
                    key = int(b[1] // 8)
                    if key not in best_per_frame or s > best_per_frame[key][0]:
                        best_per_frame[key] = (s, b)
                boxes = [b for _s, b in best_per_frame.values()]
            else:
                boxes = [b for _s, b in got]
            break

    box = _median_box(boxes, cfg) if boxes else None
    if box is None:
        cap.release()
        raise ScoreboardError(
            "画面里没有找到广播比分牌。两种可能：\n"
            "  ① 这段视频确实没有比分牌覆盖层（那就只能用 --attempts 人工标注）\n"
            "  ② 比分牌样式和默认阈值不符，请调 ScoreBugConfig 的 "
            "min_aspect/max_aspect/min_val，或改用「手动框选 + OCR」那条路")
    bug = ScoreBug(x=box[0], y=box[1], w=box[2], h=box[3])

    # 比分行：比分牌内部白色文字的行投影，取靠下面那条
    _set_score_row(cap, idxs, bug, cfg)

    # 定位出来的框必须**真的像个台标**：底色饱和 + 里面有白字。
    # 只按「饱和色横条」找的话，木地板/黄色看台一定会中招（实测踩过：
    # 定位框 = (406,634,398,75)，正好是球场地板），那种框写进缓存后
    # 换视频继续用，整片读不到一帧，故障表现为「这段视频没有比分牌」，
    # 极难排查。宁可在源头就判死。
    check_frames = []
    for i in idxs[:5]:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if ok:
            check_frames.append(frame)
    if check_frames:
        cover = sum(1 for f in check_frames
                    if bug_text_score(f, bug, cfg) >= cfg.min_bug_text_frac) \
            / float(len(check_frames))
        if cover < 0.34:
            cap.release()
            raise ScoreboardError(
                "画面里没有找到广播比分牌（定位到的横条是地板/看台色块，"
                f"不是台标：白字覆盖率 {cover:.0%}）。\n"
                "  ① 这段视频确实没有比分牌覆盖层 —— 用「球 + 篮筐」路径，"
                "或 --attempts 人工标注出手时刻；\n"
                "  ② 记分牌在画面里但样式特殊 —— 调 ScoreBugConfig 的 "
                "min_aspect/max_aspect/min_val，或改用「手动框选 + OCR」。")
    cap.release()
    return bug


def locate_score_bug(video_path: str, cfg: Optional[ScoreBugConfig] = None,
                     progress: Optional[Callable[[float, str], None]] = None
                     ) -> ScoreBug:
    """自动定位比分牌（横条框 + 字段图案标定）。

    字段标定这一步只服务「模板匹配读数字」那条路；样式对不上时会抛
    :class:`ScoreboardError` —— 那种情况请走 :func:`locate_score_bug_box`
    加 OCR（见 `ocr_scoreboard.read_scoreboard_ocr`）。
    """
    bug = locate_score_bug_box(video_path, cfg, progress=progress)
    calibrate_fields(video_path, bug, cfg or ScoreBugConfig(),
                     progress=progress)
    return bug


# --------------------------------------------------------------------------
# 标定字段：靠比分行的「词结构」
# --------------------------------------------------------------------------
def _row_glyphs(frame, bug: ScoreBug, cfg: ScoreBugConfig) -> list[tuple[int, int, int, int]]:
    """比分行里的白色字符框（相对比分牌左边缘）。

    阈值用 Otsu 自适应而不是固定值 —— 现场摄像机的自动曝光会让整个台标
    一起忽明忽暗，固定阈值一会儿把底色吃进来、一会儿把笔画漏掉。
    """
    cv2, np = _require_cv()
    top = max(bug.y, bug.row_top - cfg.row_pad)
    bottom = min(bug.y + bug.h, bug.row_bottom + cfg.row_pad)
    roi = frame[top:bottom, bug.x:bug.x + bug.w]
    ws = _white_score(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV))
    otsu = cv2.threshold(ws, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0]
    thr = max(int(otsu), cfg.white_score_thresh)
    b = (ws >= thr).astype(np.uint8)
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(b, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]),
                         int(stats[i, 3]), int(stats[i, 4]))
        if a < 4 or h < 6 or w < 2 or w > bug.w * 0.45:
            continue
        out.append((x, y, w, h))
    return sorted(out)


def _group_words(glyphs, gap_thr: float) -> list[list[tuple[int, int, int, int]]]:
    if not glyphs:
        return []
    words = [[glyphs[0]]]
    for g in glyphs[1:]:
        prev = words[-1][-1]
        if g[0] - (prev[0] + prev[2]) > gap_thr:
            words.append([g])
        else:
            words[-1].append(g)
    return words


def _word_span(word) -> int:
    return word[-1][0] + word[-1][2] - word[0][0]


def _parse_row_words(frame, bug: ScoreBug, cfg: ScoreBugConfig):
    """把比分行解析成 (主队得分词, 节次词, 客队词)，失败返回 None。

    依据：比分牌上最宽的两个词是队名（各 6 个汉字，约 78px），
    夹在它们中间的三段依次是「主队得分 / 节次 / 客队得分」。
    这样就不依赖比分牌的绝对宽度，换分辨率/换台标都能用。
    """
    glyphs = _row_glyphs(frame, bug, cfg)
    if len(glyphs) < 5:
        return None
    widths = [g[2] for g in glyphs]
    med_w = sorted(widths)[len(widths) // 2]
    words = _group_words(glyphs, max(3.0, med_w * cfg.word_gap_ratio))
    if len(words) < 5:
        return None
    widest = sorted(range(len(words)), key=lambda i: -_word_span(words[i]))[:2]
    a, b = sorted(widest)
    mid = words[a + 1:b]
    if len(mid) != 3:
        return None
    return mid[0], mid[1], mid[2]


def calibrate_fields(video_path: str, bug: ScoreBug, cfg: ScoreBugConfig,
                     cap=None, progress: Optional[Callable[[float, str], None]] = None
                     ) -> ScoreBug:
    """用「词结构」定出主队/客队/节次三个字段的中心。

    多帧各自解析一次、取中位数：单帧可能因为某个汉字掉笔画导致分词错位，
    但中位数会把它们洗掉。比分牌的版式在整段视频里是固定的，所以很稳。
    """
    cv2, np = _require_cv()
    own = cap is None
    if own:
        cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    idxs = np.linspace(0, max(0, total - 2), max(20, cfg.calib_frames // 4)).astype(int)
    hs, ps, aws = [], [], []
    for k, i in enumerate(idxs):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok:
            continue
        r = _parse_row_words(frame, bug, cfg)
        if r is None:
            continue
        home_w, period_w, away_w = r
        hs.append(bug.x + home_w[0][0] + _word_span(home_w) / 2.0)
        ps.append(bug.x + period_w[0][0] + _word_span(period_w) / 2.0)
        aws.append(bug.x + away_w[0][0] + _word_span(away_w) / 2.0)
        if progress and k % 10 == 0:
            progress(0.10, "标定比分字段")
    if own:
        cap.release()
    bug.ok_frames = len(hs)
    if len(hs) >= 4:
        bug.home_cx = float(np.median(hs))
        bug.period_cx = float(np.median(ps))
        bug.away_cx = float(np.median(aws))
        span = max(12.0, (bug.away_cx - bug.home_cx) / 2.0)
        bug.field_half_w = float(min(30.0, max(18.0, span * 0.75 + 8.0)))
    return bug


# --------------------------------------------------------------------------
# 字段窗口提取
# --------------------------------------------------------------------------
def field_window(frame, bug: ScoreBug, cx: float, cfg: ScoreBugConfig):
    """取某个字段的固定窗口（软灰度白度图，0~1）。

    窗口大小只跟比分牌几何有关，与「这一帧读出来是几位数」无关 ——
    所以同一个比分值在不同帧取到的窗口几乎逐像素相同，模板匹配极其干净。
    """
    cv2, np = _require_cv()
    top = max(bug.y, bug.row_top - cfg.row_pad)
    bottom = min(bug.y + bug.h, bug.row_bottom + cfg.row_pad)
    half = int(round(bug.field_half_w + cfg.field_pad))
    x0 = int(round(cx)) - half
    x1 = int(round(cx)) + half
    x0 = max(bug.x, x0)
    x1 = min(bug.x + bug.w, x1)
    if x1 - x0 < 4 or bottom - top < 4:
        return None
    roi = frame[top:bottom, x0:x1]
    if roi is None or roi.size == 0 or len(roi.shape) < 2:
        return None
    ws = _white_score(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV))
    return ws.astype(np.float32) / 255.0


# --------------------------------------------------------------------------
# 字段图案模板库
# --------------------------------------------------------------------------
@dataclass
class FieldPatterns:
    """每个字段一套「窗口图案 -> 比分值」的模板。

    这是本模块的核心产物，由 scripts/build_scoreboard_templates.py 生成：
    把整段视频里该字段出现过的所有窗口图案聚类，每一类人工给一个数值标签。
    """

    home_reps: object = None
    home_vals: list[int] = field(default_factory=list)
    away_reps: object = None
    away_vals: list[int] = field(default_factory=list)
    period_reps: object = None
    period_vals: list[int] = field(default_factory=list)
    note: str = ""

    def to_npz(self, path: str) -> None:
        _cv2, np = _require_cv()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        d = {}
        for name in ("home", "away", "period"):
            reps = getattr(self, f"{name}_reps")
            vals = getattr(self, f"{name}_vals")
            if reps is not None and len(vals):
                d[f"{name}_reps"] = np.asarray(reps, dtype=np.float32)
                d[f"{name}_vals"] = np.array(vals, dtype=np.int32)
        np.savez_compressed(path, **d)

    @staticmethod
    def load(path: str) -> "FieldPatterns":
        _cv2, np = _require_cv()
        d = np.load(path, allow_pickle=False)
        fp = FieldPatterns()
        for name in ("home", "away", "period"):
            if f"{name}_reps" in d:
                setattr(fp, f"{name}_reps", d[f"{name}_reps"])
                setattr(fp, f"{name}_vals",
                        [int(v) for v in d[f"{name}_vals"]])
        return fp

    def has(self, name: str) -> bool:
        reps = getattr(self, f"{name}_reps", None)
        return reps is not None and len(getattr(self, f"{name}_vals", [])) > 0

    def match(self, name: str, window, cfg: ScoreBugConfig
              ) -> tuple[Optional[int], float, float]:
        """返回 (数值, 置信度, 最佳距离)。认不出时数值为 None。"""
        _cv2, np = _require_cv()
        reps = getattr(self, f"{name}_reps", None)
        vals = getattr(self, f"{name}_vals", [])
        if window is None or reps is None or not vals:
            return None, 0.0, 9.9
        R = np.asarray(reps, dtype=np.float32)
        if R.shape[1:] != window.shape:
            return None, 0.0, 9.9
        d = np.sqrt(((R - window[None, :, :]) ** 2).reshape(len(vals), -1).mean(axis=1))
        order = np.argsort(d)
        best = float(d[order[0]])
        second = float(d[order[1]]) if len(order) > 1 else 0.5
        if best > cfg.max_match_dist:
            return None, 0.0, best
        margin = second - best
        if margin < cfg.min_match_margin:
            return None, 0.0, best
        conf = max(0.0, min(1.0, (1.0 - best / max(1e-6, cfg.max_match_dist))
                            * min(1.0, margin / 0.05)))
        return int(vals[int(order[0])]), round(conf, 3), best


DEFAULT_TEMPLATE_CANDIDATES = ("data/scoreboard_templates.npz",)


def load_patterns(cfg: Optional[ScoreBugConfig] = None) -> Optional[FieldPatterns]:
    cfg = cfg or ScoreBugConfig()
    cands = [cfg.template_path] if cfg.template_path else []
    cands += list(DEFAULT_TEMPLATE_CANDIDATES)
    root = Path(__file__).resolve().parents[2]
    for c in cands:
        if not c:
            continue
        p = Path(c)
        if not p.is_absolute():
            p = root / c
        if p.exists():
            try:
                fp = FieldPatterns.load(str(p))
                if fp.has("home") or fp.has("away"):
                    return fp
            except Exception:
                continue
    return None


# --------------------------------------------------------------------------
# 读一帧
# --------------------------------------------------------------------------
@dataclass
class ScoreReading:
    t: float
    home: int
    away: int
    period: int = 1
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return dict(t=round(self.t, 3), home=self.home, away=self.away,
                    period=self.period, confidence=self.confidence)


def read_frame(frame, bug: ScoreBug, pat: FieldPatterns,
               cfg: Optional[ScoreBugConfig] = None) -> Optional[ScoreReading]:
    """读一帧的比分。

    任一字段读不到（认不出 / 匹配到「画面无比分牌」的 -1 模板）就返回 None ——
    宁可这一段空着、沿用上一个比分，也不能瞎认一个数出来。
    """
    cfg = cfg or ScoreBugConfig()
    if bug.home_cx <= 0 or bug.away_cx <= 0:
        return None
    hw = field_window(frame, bug, bug.home_cx, cfg)
    aw = field_window(frame, bug, bug.away_cx, cfg)
    home, hc, _ = pat.match("home", hw, cfg)
    away, ac, _ = pat.match("away", aw, cfg)
    if home is None or away is None or home < 0 or away < 0:
        return None
    period = 1
    if pat.has("period"):
        pw = field_window(frame, bug, bug.period_cx, cfg)
        pv, _pc, _pd = pat.match("period", pw, cfg)
        if pv is not None and pv > 0:
            period = int(pv)
    return ScoreReading(t=0.0, home=int(home), away=int(away), period=period,
                        confidence=round(min(hc, ac), 3))


# --------------------------------------------------------------------------
# 扫全片
# --------------------------------------------------------------------------
@dataclass
class ScoreboardScan:
    """扫全片的结果。"""

    bug: ScoreBug
    readings: list[ScoreReading] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)   # 每次比分变化
    fps: float = 30.0
    duration: float = 0.0
    frames_read: int = 0
    frames_hit: int = 0
    team_names: dict = field(default_factory=dict)

    @property
    def final(self) -> tuple[int, int]:
        if not self.readings:
            return 0, 0
        r = self.readings[-1]
        return r.home, r.away

    def to_dict(self) -> dict:
        return {"bug": self.bug.to_dict(), "fps": self.fps,
                "duration": self.duration, "frames_read": self.frames_read,
                "frames_hit": self.frames_hit,
                "team_names": self.team_names,
                "readings": [r.to_dict() for r in self.readings],
                "events": self.events}


def scan_scoreboard(video_path: str, cfg: Optional[ScoreBugConfig] = None,
                    patterns: Optional[FieldPatterns] = None,
                    bug: Optional[ScoreBug] = None,
                    progress: Optional[Callable[[float, str], None]] = None
                    ) -> ScoreboardScan:
    """逐帧（按 stride 抽帧）读比分，再用「比分只增不减」把误读洗掉。"""
    cfg = cfg or ScoreBugConfig()
    cv2, np = _require_cv()
    pat = patterns or load_patterns(cfg)
    if pat is None:
        raise ScoreboardError(
            "缺少比分牌图案模板。先生成一次：\n"
            "  python scripts/build_scoreboard_templates.py "
            "--video <视频> --out data/scoreboard_templates.npz")

    if bug is None:
        bug = locate_score_bug(video_path, cfg, progress=progress)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ScoreboardError(f"打不开视频：{video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if fps else 0.0

    scan = ScoreboardScan(bug=bug, fps=fps, duration=duration)
    idx = 0
    last_report = 0.0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % cfg.stride:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        scan.frames_read += 1
        r = read_frame(frame, bug, pat, cfg)
        if r is not None and r.confidence >= cfg.min_confidence:
            r.t = idx / fps
            scan.readings.append(r)
            scan.frames_hit += 1
        if progress and total and idx / total - last_report > 0.02:
            last_report = idx / total
            progress(0.15 + 0.30 * idx / total, "读取比分牌")
        idx += 1
    cap.release()

    scan.events = _debounce(scan.readings, cfg)
    return scan


def _debounce(readings: list[ScoreReading],
              cfg: ScoreBugConfig) -> list[dict]:
    """时序去抖：比分只能不减；新高必须连续 confirm_reads 次成立才采信。

    返回每次「得分事件」：{t, team, delta, home, away, period, confidence}。
    """
    events: list[dict] = []
    if not readings:
        return events

    home = away = None
    period = 1
    pending: Optional[tuple[int, int]] = None
    pending_n = 0
    pending_first: Optional[ScoreReading] = None

    for r in readings:
        if home is None:
            home, away, period = r.home, r.away, r.period
            events.append({"t": round(r.t, 3), "team": "", "delta": 0,
                           "home": home, "away": away, "period": period,
                           "confidence": r.confidence, "kind": "baseline"})
            continue

        # 比分倒退 -> 几乎一定是误读，直接丢
        if r.home < home or r.away < away:
            continue

        if (r.home, r.away) == (home, away):
            period = r.period or period
            pending, pending_n, pending_first = None, 0, None
            continue

        # 出现新高：连续 confirm_reads 次一致才认
        if pending == (r.home, r.away):
            pending_n += 1
        else:
            pending, pending_n, pending_first = (r.home, r.away), 1, r
        if pending_n < cfg.confirm_reads:
            continue

        dh, da = r.home - home, r.away - away
        src = pending_first or r
        if dh > 0:
            events.append({"t": round(src.t, 3), "team": "home", "delta": dh,
                           "home": r.home, "away": r.away, "period": r.period,
                           "confidence": src.confidence, "kind": "score"})
        if da > 0:
            events.append({"t": round(src.t, 3), "team": "away", "delta": da,
                           "home": r.home, "away": r.away, "period": r.period,
                           "confidence": src.confidence, "kind": "score"})
        home, away = r.home, r.away
        period = r.period or period
        pending, pending_n, pending_first = None, 0, None

    events.sort(key=lambda e: (e["t"], e.get("kind") != "baseline"))
    return events


# --------------------------------------------------------------------------
# 把比分事件翻成「得分出手」
# --------------------------------------------------------------------------
def score_points_to_attempts(scan: ScoreboardScan,
                             hoop_side: Optional[dict] = None,
                             fps: float = 30.0) -> list[dict]:
    """把比分跳变翻译成出手记录（已命中的那些）。

    delta=1 罚球 / 2 两分 / 3 三分 —— 这是**规则级确定的事实**，
    不依赖任何球轨迹或图像识别，所以置信度给高分。
    出手位置先给「被进攻的篮筐」占位坐标，真正的位置由 ball.py 覆盖。
    """
    out: list[dict] = []
    for e in scan.events:
        if e.get("kind") != "score":
            continue
        delta = int(e["delta"])
        if delta not in (1, 2, 3):
            # 一次跳变 >3 分：多半是中间漏读了，按 3 分记并标记，交给复核
            delta = 3
        team = e["team"]
        side = (hoop_side or {}).get(team, "left")
        hx, hy = (0.0, -1.575) if side == "left" else (0.0, 1.575)
        out.append({
            "t": float(e["t"]), "team": team,
            "player_id": f"{team[0].upper()}?",
            "x": hx, "y": hy, "period": int(e.get("period", 1)),
            "is_free_throw": delta == 1,
            "made": True, "conf": float(e.get("confidence", 0.9)),
            "release_frame": int(float(e["t"]) * fps),
            "forced_value": delta,
            "location_source": "rim_placeholder",
            "source": "scoreboard",
        })
    return out


def read_scoreboard_events(video_path: str,
                           cfg: Optional[ScoreBugConfig] = None,
                           progress: Optional[Callable[[float, str], None]] = None
                           ) -> tuple[list[dict], dict]:
    """给 sources.VideoSource 用的便捷入口：返回 (attempts, meta)。"""
    cfg = cfg or ScoreBugConfig()
    scan = scan_scoreboard(video_path, cfg, progress=progress)
    attempts = score_points_to_attempts(scan, fps=scan.fps)
    return attempts, {"scoreboard": scan.to_dict()}
