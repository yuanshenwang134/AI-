"""篮球检测与追踪 —— 多线索候选 + 轨迹连接。

**为什么不能只靠 YOLO：**
  COCO 预训练的 YOLOv8 里只有 sports ball 这一个「球」类，而篮球在
  720p 的远景比赛画面里只有 10~20 像素、还经常被球员挡住 —— 实测整段
  3 分钟视频只命中 2 个点。所以这里做的是**多线索融合**：

    线索 1  YOLO sports ball      精度高但召回极低（有就最可信）
    线索 2  橙色色块 + 圆形度      召回高但误检多（球衣、皮肤、广告牌都会中）
    线索 3  运动显著性（帧间差分） 用来给前两路打分，压掉静止的橙色物体

  单帧候选本身都不可信，**可信的是连成一条物理上说得通的轨迹**：
  所以真正的核心是 track linking —— 用「速度外推 + 门限」把候选连成轨迹，
  只保留长度足够、速度合理的那些。这一步能把绝大部分误检滤掉。

**坐标的诚实说明：**
  单目相机 + 单应矩阵只能把「地面上的点」映射到球场坐标。球在空中时，
  它的像素位置对应的是另一条视线，投到地面会偏。所以本模块给出的球场坐标
  是**带系统偏差的估计**（近距离上篮偏差小，远投偏差大），
  一律标记 `z_estimated=True`，报告里也按「估计位置」呈现，不当作测量值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .court import Calibration, apply_homography
from .rules import BallSample


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class BallConfig:
    """候选检测 + 轨迹连接的阈值。"""

    # ---- 线索 1：YOLO ----
    use_yolo: bool = True
    weights: str = "yolov8n.pt"
    yolo_conf: float = 0.20
    imgsz: int = 960
    device: Optional[str] = None

    # ---- 线索 2：橙色色块 ----
    # OpenCV 的色相是**环形**的（0~180），红-橙横跨 0/180 两端：
    #   偏橙的球/壶   -> H ≈ 4~27
    #   偏红的球/红漆篮筐 -> H ≈ 166~180
    # 只取其中一段就会整段漏检。实测一段白天室外视频里球和篮筐的色相都在
    # ~175（红端），用单段 [4,27] 的结果是「一帧里一个橙色像素都没有」，
    # 球完全追不到、篮筐也找不到。所以两段都要。
    # 放宽颜色/面积：实战里球经常偏暗红、逆光、只有 3~8px，
    # 原来的 V/S 下限会把球在篮筐附近的关键几帧整段漏掉。
    hsv_lo: tuple[int, int, int] = (0, 70, 55)
    hsv_hi: tuple[int, int, int] = (35, 255, 255)
    hsv_lo2: tuple[int, int, int] = (150, 55, 50)
    hsv_hi2: tuple[int, int, int] = (180, 255, 255)
    min_area: int = 8
    max_area: int = 3000
    # 圆形度门槛。**不能设高**：篮球有黑色接缝，按颜色抠出来的连通域天然不圆
    # （实测球的圆形度只有 0.23），门槛 0.42 会把球整段丢掉。
    # 形状筛选主要交给「宽高比接近 1」+「面积范围」，圆形度只用来兜底。
    min_circularity: float = 0.10
    min_aspect: float = 0.35
    max_aspect: float = 3.0

    # ---- 线索 3：运动 ----
    motion_min: float = 6.0          # 与前一帧的平均绝对差阈值

    # ---- 轨迹连接 ----
    max_gap_s: float = 0.40          # 允许的最大丢帧间隔
    gate_px: float = 90.0            # 外推位置周围的门限（像素）
    max_speed_px: float = 2600.0     # 像素/秒上限（约 30m/s 投影到近景的量级）
    min_track_len: int = 5           # 少于这么多点的轨迹直接丢掉
    min_track_span_s: float = 0.15
    # 轨迹拼接：球在空中只有一两秒，颜色检测经常被球员挡住/运动模糊打断成几截。
    # 不接起来的话，「球从篮筐上方落到下方」这个判据永远看不到完整的下降段
    # —— 实测一段 28 秒练习视频里 3 次出手全部因为 cross_x=None 判成不中。
    stitch: bool = True
    stitch_gap_s: float = 0.6        # 允许的最大拼接时间间隔（太长会把不同物体接起来）
    stitch_gate_px: float = 80.0     # 外推位置周围的门限
    stitch_min_cos: float = 0.2      # 方向一致性下限（必须大体同向，别把反弹球接上）
    # 同时活跃的轨迹上限。这是**防御性**参数：万一上游给的时间戳不递增
    # （曾经因为漏写 idx += 1 让所有候选的 t 都是 0），活跃轨迹会无限膨胀，
    # 连接过程退化成 O(N²)，6 分钟都跑不完。有上限就只会变慢一点，不会卡死。
    max_active: int = 400

    # ---- 背景建模 / 候选过滤 / 轨迹筛选 ----
    use_bg: bool = True             # 用中值背景差分找运动球候选
    bg_samples: int = 40            # 背景建模采样帧数
    bg_diff_min: float = 2.5        # 背景差分的最小运动量
    min_candidate_motion: float = 2.0  # 候选框内运动量下限（仅在提供 motion 时生效）
    min_avg_speed_px: float = 22.0  # 轨迹平均速度下限（像素/秒）
    min_displacement_px: float = 18.0  # 轨迹首尾位移下限（像素）
    max_direction_turns: int = 8    # 轨迹方向变化次数上限（过滤噪声轨迹）
<<<<<<< HEAD
    # 方向变化**速率**上限（次/秒）。球在整段素材里常常连续被检出（运球 +
    # 出手是同一条轨迹），长度越长反转次数越多；只按次数当闸门会把
    # 「球每帧都检出、置信度 1.0」的真轨迹整条丢掉 —— 实测一段罚球素材
    # 5 个进球因此全部漏判。噪声的特征是**又短又抖**（反转次数/秒高），
    # 所以次数与速率**同时**超标才丢弃。
    max_turn_rate_per_s: float = 6.0
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    # 方向变化计数时，位移幅度小于该像素数的来回抖动不算一次转向。
    # 球到篮筐附近会被检测成一串小抖动点，旧实现会把它记成十几次转向。
    turn_min_amp_px: float = 3.0
    max_step_px: float = 160.0      # 相邻两点最大像素距离（过滤静止物体+大跳变假轨迹）
    keep_top_tracks: int = 12       # 只保留最像球的若干条轨迹给进球判定

    # ---- 掩膜掉的区域（比分牌等固定覆盖层，避免误检） ----
    ignore_boxes: list = field(default_factory=list)

    stride: int = 1


@dataclass
class BallCandidate:
    t: float
    x: float          # 像素
    y: float
    score: float      # 0~1，越大越可信
    source: str       # yolo / color


@dataclass
class BallTrack:
    """追踪结果。"""

    samples: list[BallSample] = field(default_factory=list)   # 球场坐标
    candidates: int = 0
    tracks_total: int = 0
    tracks_kept: int = 0
    frames: int = 0
    note: str = ""

    @property
    def coverage(self) -> float:
        return len(self.samples) / self.frames if self.frames else 0.0

    def to_dict(self) -> dict:
        return {"points": len(self.samples), "candidates": self.candidates,
                "tracks_total": self.tracks_total,
                "tracks_kept": self.tracks_kept, "frames": self.frames,
                "coverage": round(self.coverage, 4), "note": self.note}


# --------------------------------------------------------------------------
# 单帧候选
# --------------------------------------------------------------------------
def _color_candidates(frame, hsv, cfg: BallConfig,
                      motion=None, bg_motion=None) -> list[tuple[float, float, float]]:
    """橙色色块候选，返回 [(cx, cy, radius)]。"""
    cv2, np = _require_cv()
    # 色相环形：橙端 [4,27] + 红端 [166,180] 两段都要（详见 BallConfig 注释）
    mask = cv2.inRange(hsv, cfg.hsv_lo, cfg.hsv_hi)
    mask |= cv2.inRange(hsv, cfg.hsv_lo2, cfg.hsv_hi2)
    # 闭运算把球的黑色接缝填上，否则连通域会被缝切成几块、圆形度和面积都不对
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    for (x0, y0, x1, y1) in cfg.ignore_boxes:
        mask[y0:y1, x0:x1] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if not (cfg.min_area <= a <= cfg.max_area):
            continue
        x, y, w, h = cv2.boundingRect(c)
        if h < 2 or w < 2:
            continue
        if not (cfg.min_aspect <= w / float(h) <= cfg.max_aspect):
            continue
        per = cv2.arcLength(c, True)
        circ = 4.0 * math.pi * a / (per * per) if per > 0 else 0.0
        if circ < cfg.min_circularity:
            continue
        # 运动过滤：静态橙色物体（球衣、广告牌、固定篮筐）不该进入轨迹连接。
        if (motion is not None or bg_motion is not None) and cfg.min_candidate_motion > 0:
            ref = motion if motion is not None else bg_motion
            x0 = max(0, x); y0 = max(0, y)
            x1 = min(ref.shape[1], x + w); y1 = min(ref.shape[0], y + h)
            vals = []
            if motion is not None:
                vals.append(float(motion[y0:y1, x0:x1].mean()))
            if bg_motion is not None:
                vals.append(float(bg_motion[y0:y1, x0:x1].mean()))
            if vals and max(vals) < cfg.min_candidate_motion:
                continue
        out.append((x + w / 2.0, y + h / 2.0, math.sqrt(a / math.pi)))
    return out


def _require_cv():
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "篮球追踪需要 opencv-python 与 numpy。"
            "安装：pip install -r requirements-full.txt") from e
    return cv2, np


# --------------------------------------------------------------------------
# 轨迹连接
# --------------------------------------------------------------------------
def build_background(video_path: str, n: int = 40):
    """用均匀采样的若干帧的中值作为静态背景估计。

    固定机位下，这一步能把「静止的橙色物体」（球衣、广告牌、固定篮筐）
    从运动候选里剔掉，只留下球/球员等真正在动的东西。
    """
    cv2, np = _require_cv()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        return None
    k = max(3, min(int(n), total))
    idxs = np.linspace(0, max(0, total - 1), num=k).astype(int)
    frames = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if ok:
            frames.append(f)
    cap.release()
    if len(frames) < 3:
        return None
    try:
        return np.median(np.stack(frames, 0), axis=0).astype(np.uint8)
    except Exception:
        return None


def _link_tracks(cands: list[BallCandidate], cfg: BallConfig,
                 cuts: Optional[list[float]] = None
                 ) -> list[list[BallCandidate]]:
    """把逐帧候选连成轨迹。

    用「速度外推 + 门限」的贪心连接：
      * 每条活跃轨迹用最近两点的速度外推下一帧的位置；
      * 新候选落在外推位置 gate_px 内才接上，且速度不能超过 max_speed_px；
      * 优先接「预测误差最小」的那个候选（用代价排序，避免近处的杂点抢走球）。
    """
    tracks: list[list[BallCandidate]] = []
    active: list[list[BallCandidate]] = []
    cut_ts = sorted(float(x) for x in (cuts or []))
    last_t = -1e18
    for c in sorted(cands, key=lambda d: d.t):
        # 防御 1：时间戳必须递增。不递增的话「速度外推」无从谈起，
        # 而且每条候选都会新开一条活跃轨迹，把连接过程拖成 O(N²)。
        if c.t < last_t:
            continue
        # 切镜点：上一帧到当前帧之间发生切镜时，先把活跃轨迹结算掉，
        # 避免把切镜前后两个镜头里的候选连成一条轨迹。
        if cut_ts and any(last_t < cut <= c.t for cut in cut_ts):
            tracks.extend(active)
            active = []
        last_t = c.t
        # 防御 2：活跃轨迹数量封顶，超出就把最老的几条结算掉
        if len(active) > cfg.max_active:
            active.sort(key=lambda tr: tr[-1].t)
            cut = len(active) - cfg.max_active
            tracks.extend(active[:cut])
            active = active[cut:]
        best_i, best_cost = -1, 1e18
        for i, tr in enumerate(active):
            last = tr[-1]
            dt = c.t - last.t
            if dt <= 0 or dt > cfg.max_gap_s:
                continue
            if len(tr) >= 2:
                p = tr[-2]
                span = max(1e-3, last.t - p.t)
                vx = (last.x - p.x) / span
                vy = (last.y - p.y) / span
            else:
                vx = vy = 0.0
            px, py = last.x + vx * dt, last.y + vy * dt
            dist = math.hypot(c.x - px, c.y - py)
            if dist > cfg.gate_px:
                continue
            if math.hypot(c.x - last.x, c.y - last.y) / dt > cfg.max_speed_px:
                continue
            cost = dist / cfg.gate_px - 0.15 * c.score
            if cost < best_cost:
                best_cost, best_i = cost, i
        if best_i >= 0:
            active[best_i].append(c)
        else:
            active.append([c])
        # 超过间隔的轨迹退场
        still = []
        for tr in active:
            if c.t - tr[-1].t <= cfg.max_gap_s:
                still.append(tr)
            else:
                tracks.append(tr)
        active = still
    tracks.extend(active)
    return [t for t in tracks
            if len(t) >= cfg.min_track_len
            and (t[-1].t - t[0].t) >= cfg.min_track_span_s]


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def stitch_tracks(tracks: list[list[BallCandidate]],
                  cfg: Optional[BallConfig] = None,
                  cuts: Optional[list[float]] = None) -> list[list[BallCandidate]]:
    """把断开的球轨迹按运动连续性接起来。

    为什么必须做：球在空中只有一两秒，颜色检测会因为「被球员挡住 / 运动模糊 /
    逆光」断成好几截。而进球判据要求**同一条轨迹里同时有上升段和穿越篮筐平面的
    下降段** —— 断成两截就永远判不出来（实测 3 次出手全因 cross_x=None 判成不中）。

    接法：用轨迹末尾两点的速度外推，看下一段轨迹的起点落不落在门限内，
    并且方向不能反过来（避免把打到篮筐弹回来的球接到上升段上）。
    """
    cfg = cfg or BallConfig()
    if not cfg.stitch:
        return tracks
    cut_ts = sorted(float(x) for x in (cuts or []))
    pool = sorted([list(t) for t in tracks if len(t) >= 2],
                  key=lambda t: t[0].t)
    merged: list[list[BallCandidate]] = []
    for tr in pool:
        head, placed = tr[0], False
        for m in merged:
            last = m[-1]
            gap = head.t - last.t
            if gap <= 0 or gap > cfg.stitch_gap_s:
                continue
            if cut_ts and any(last.t < cut <= head.t for cut in cut_ts):
                continue
            if len(m) >= 2:
                p = m[-2]
                span = max(1e-3, last.t - p.t)
                vx, vy = (last.x - p.x) / span, (last.y - p.y) / span
            else:
                vx = vy = 0.0
            px, py = last.x + vx * gap, last.y + vy * gap
            if math.hypot(head.x - px, head.y - py) > cfg.stitch_gate_px:
                continue
            if len(tr) >= 2 and (vx or vy):
                span2 = max(1e-3, tr[1].t - head.t)
                nvx, nvy = (tr[1].x - head.x) / span2, (tr[1].y - head.y) / span2
                n1, n2 = math.hypot(vx, vy), math.hypot(nvx, nvy)
                if n1 > 1e-6 and n2 > 1e-6:
                    cos = (vx * nvx + vy * nvy) / (n1 * n2)
                    if cos < cfg.stitch_min_cos:
                        continue
            m.extend(tr)
            placed = True
            break
        if not placed:
            merged.append(list(tr))
    merged.sort(key=lambda t: -len(t))
    return merged


def _turns(vals: list, min_amp: float = 0.0) -> int:
    """方向变化次数，用来过滤左右横跳的噪声轨迹。

    min_amp > 0 时按之字形统计：累计位移小于 min_amp 的来回抖动不算转向。
    球到篮筐附近时，颜色检测会给出一串几像素的上下抖动点；旧实现把每个抖动
    都记成方向变化，真实的投篮轨迹（上升-穿筐-下落）会被判成变化十几次，
    从而在 rank_ball_tracks 里被直接丢掉。
    """
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



def rank_ball_tracks(tracks: list[list[BallCandidate]],
                     cfg: Optional[BallConfig] = None,
                     hoop=None) -> list[list[BallCandidate]]:
    """从候选轨迹里挑出最像「篮球飞行」的若干条。

    只保留：长度/时间跨度足够、有明显位移、平均速度够、方向变化少的轨迹。
    如果提供 hoop，则额外给「靠近篮筐、并且有从上方穿到下方趋势」的轨迹加权。
    这一步是精确率的关键 —— 以前把几百条含静止橙色物体的轨迹全丢给
    detect_shots()，就会在篮筐附近糊出一堆假命中。
    """
    cfg = cfg or BallConfig()
    get_hoop = hoop.at if hasattr(hoop, "at") else (lambda _t: hoop)
    scored: list[tuple[float, list[BallCandidate]]] = []
    for tr in tracks:
        if len(tr) < cfg.min_track_len:
            continue
        span = tr[-1].t - tr[0].t
        if span < cfg.min_track_span_s:
            continue
        path = sum(math.hypot(tr[i + 1].x - tr[i].x, tr[i + 1].y - tr[i].y)
                   for i in range(len(tr) - 1))
        disp = math.hypot(tr[-1].x - tr[0].x, tr[-1].y - tr[0].y)
        speed = path / max(1e-3, span)
        if disp < cfg.min_displacement_px or speed < cfg.min_avg_speed_px:
            continue
        amp = float(getattr(cfg, "turn_min_amp_px", 0.0) or 0.0)
        turns = (_turns([p.x for p in tr], amp)
                 + _turns([p.y for p in tr], amp))
<<<<<<< HEAD
        if (turns > cfg.max_direction_turns
                and turns / max(1e-3, span)
                > float(getattr(cfg, "max_turn_rate_per_s", 6.0))):
=======
        if turns > cfg.max_direction_turns:
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
            continue
        # 真实球轨迹相邻点不会瞬间跳几百像素；静止物体+大跳变的假轨迹必须去掉。
        max_step = 0.0
        for i in range(len(tr) - 1):
            step = math.hypot(tr[i + 1].x - tr[i].x, tr[i + 1].y - tr[i].y)
            if step > max_step:
                max_step = step
        if max_step > cfg.max_step_px:
            continue
        max_score = max(p.score for p in tr)
        # 长、快、直、置信度高的轨迹优先；对长度做上限，避免又长又乱的
        # 噪声轨迹单靠长度压过真正的短投篮弧线。
        score = (min(len(tr), 40) * min(speed, 800.0)
                 / (1.0 + 1.5 * turns) * (1.0 + 0.3 * max_score))
        if hoop is not None:
            try:
                min_app = min(abs(p.x - get_hoop(p.t).cx) for p in tr)
                mid = tr[len(tr) // 2]
                rx = get_hoop(mid.t).rx or 30.0
                if min_app <= max(25.0, rx * 1.5):
                    score *= 2.5
                else:
                    score *= max(0.15, 1.0 - min_app / max(1.0, rx * 4.0))
                # 明确从篮筐上方落到下方：这是投篮/进球的最强形状信号。
                above = False
                crossed = False
                for p in tr:
                    hp = get_hoop(p.t)
                    if p.y < hp.cy - hp.ry:
                        above = True
                    if above and p.y > hp.cy + hp.ry:
                        crossed = True
                        break
                if crossed:
                    score *= 2.0
            except Exception:
                pass
        scored.append((score, tr))
    scored.sort(key=lambda x: -x[0])
    return [tr for _s, tr in scored[:max(1, cfg.keep_top_tracks)]]


def track_ball(video_path: str, cal: Calibration,
               cfg: Optional[BallConfig] = None,
               yolo_model=None,
               progress: Optional[Callable[[float, str], None]] = None
               ) -> BallTrack:
    """跑一遍视频，产出球场坐标的球轨迹。"""
    cfg = cfg or BallConfig()
    cv2, np = _require_cv()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频：{video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    model = yolo_model
    if model is None and cfg.use_yolo:
        try:
            from ultralytics import YOLO
            model = YOLO(cfg.weights)
        except Exception:
            model = None

    out = BallTrack()
    cands: list[BallCandidate] = []
    prev_gray = None
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % max(1, cfg.stride):
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        t = idx / fps
        out.frames += 1
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        motion = None
        if prev_gray is not None and prev_gray.shape == gray.shape:
            motion = cv2.absdiff(gray, prev_gray)
        prev_gray = gray

        # 线索 1：YOLO
        if model is not None:
            try:
                res = model.predict(frame, conf=cfg.yolo_conf, imgsz=cfg.imgsz,
                                    device=cfg.device, verbose=False)[0]
                for b in res.boxes:
                    name = res.names.get(int(b.cls[0]), "")
                    if name not in ("sports ball", "ball"):
                        continue
                    xy = [float(v) for v in b.xyxy[0]]
                    cands.append(BallCandidate(
                        t=t, x=(xy[0] + xy[2]) / 2, y=(xy[1] + xy[3]) / 2,
                        score=min(1.0, float(b.conf[0]) + 0.3), source="yolo"))
            except Exception:
                pass

        # 线索 2 + 3：橙色色块，用运动量打分
        for (cx, cy, r) in _color_candidates(frame, hsv, cfg, motion):
            sc = 0.35
            if r <= 12:
                sc += 0.15
            if motion is not None:
                yy = int(max(0, min(H - 1, cy)))
                xx = int(max(0, min(W - 1, cx)))
                m = float(motion[max(0, yy - 3):yy + 4,
                                 max(0, xx - 3):xx + 4].mean())
                if m >= cfg.motion_min:
                    sc += 0.35
                else:
                    sc -= 0.15
            if sc > 0.2:
                cands.append(BallCandidate(t=t, x=cx, y=cy, score=sc,
                                           source="color"))
        if progress and total:
            progress(min(0.95, idx / total), "追踪篮球")
        idx += 1
    cap.release()

    out.candidates = len(cands)
    tracks = _link_tracks(cands, cfg)
    out.tracks_total = len(tracks)
    tracks = stitch_tracks(tracks, cfg)
    tracks = rank_ball_tracks(tracks, cfg)
    out.tracks_kept = len(tracks)

    # 取最像球的若干条轨迹的并集作为球场坐标轨迹
    kept: list[BallCandidate] = []
    for tr in tracks[:3]:
        kept.extend(tr)
    kept.sort(key=lambda c: c.t)

    for c in kept:
        xm, ym = cal.to_court(c.x, c.y)
        out.samples.append(BallSample(t=round(c.t, 3), x=round(xm, 3),
                                      y=round(ym, 3), z=1.2, conf=c.score))
    out.note = (f"候选 {out.candidates} 个 / 轨迹 {out.tracks_total} 条；"
                f"保留点数 {len(out.samples)}。"
                "坐标为地面投影估计（球在空中时存在系统偏差），"
                "位置一律按「估计值」使用，不当作测量值。")
    if progress:
        progress(1.0, "篮球追踪完成")
    return out
