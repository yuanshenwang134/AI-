"""多镜头标定：**两台机位各解一份单应矩阵**，再在结果层合并。

为什么必须这样（用户实测的根本需求）
------------------------------------
用户的原话："一个镜头怎么可能看到全场啊，就是两个镜头标点，按照标出来的点
算战术图和投篮热区啊"。

这是对的。一台机位拍不到全场，所以真实工作流是：
    镜头 A → 覆盖半场 A        镜头 B → 覆盖半场 B
两个机位**没有共同坐标系**，把它们的点混在一起解**一个**单应矩阵在数学上
不可能成立（用户反复看到的"解算失败 / 点的位置对不上"就是这么来的）。

正确做法：
    镜头 A 自己解一份 H_A → A 覆盖的那部分投到球场坐标
    镜头 B 自己解一份 H_B → B 覆盖的那部分投到球场坐标
               ↓ 在**结果层**合并（都是球场坐标了，直接叠）
           一张全场战术图 / 热区图

本模块负责：
  * `Segment` / `MultiCal`：把"每个镜头一份标定 + 它覆盖的时间段"存下来；
  * `solve_per_shot()`：按镜头分组、各自解算（**绝不跨镜头合并**）；
  * `plan_segments()`：用切镜时刻把时间轴切开，算出每段的范围。

分析端用 `MultiCal.cal_at(t)` 取该帧所属镜头的标定即可（见 sources.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .court import Calibration


@dataclass
class Segment:
    """一个镜头一段时间的标定。"""
    t_start: float
    t_end: float
    src_px: list = field(default_factory=list)
    dst_m: list = field(default_factory=list)
    H: list = field(default_factory=list)
    frame: str = "full"            # full | half
    names: list = field(default_factory=list)
    rmse_m: float = 0.0
    ratio: float = 0.0             # 吻合度（投影线压在白线上的比例）
    method: str = "web-keypoints-multi"
    n_points: int = 0
    note: str = ""

    def calibration(self, for_video: str = "", frame_size=None) -> Calibration:
        cal = Calibration(name="seg", method=self.method, src_px=self.src_px,
                          dst_m=self.dst_m, H=self.H, reproj_error_m=self.rmse_m,
                          frame=self.frame, for_video=for_video,
                          frame_size=list(frame_size or []))
        try:
            cal.point_names = list(self.names)          # type: ignore[attr-defined]
        except Exception:                               # noqa: BLE001
            pass
        return cal

    def to_dict(self) -> dict:
        return {"t_start": round(float(self.t_start), 3),
                "t_end": round(float(self.t_end), 3),
                "src_px": self.src_px, "dst_m": self.dst_m, "H": self.H,
                "frame": self.frame, "names": list(self.names),
                "rmse_m": round(float(self.rmse_m), 4),
                "ratio": round(float(self.ratio), 3),
                "method": self.method, "n_points": int(self.n_points),
                "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(t_start=float(d.get("t_start") or 0.0),
                   t_end=float(d.get("t_end") or 0.0),
                   src_px=d.get("src_px") or [], dst_m=d.get("dst_m") or [],
                   H=d.get("H") or [], frame=d.get("frame") or "full",
                   names=d.get("names") or [],
                   rmse_m=float(d.get("rmse_m") or 0.0),
                   ratio=float(d.get("ratio") or 0.0),
                   method=d.get("method") or "web-keypoints-multi",
                   n_points=int(d.get("n_points") or 0),
                   note=d.get("note") or "")


class MultiCal:
    """一台机位一段标定，合起来覆盖整段视频。

    没有 `segments` 时它退化成"只有一段"，所以调用方可以无脑用它。
    """

    def __init__(self, segments: list, for_video: str = "",
                 frame_size: Optional[list] = None, note: str = ""):
        self.segments = [s for s in (segments or []) if s and s.H]
        self.segments.sort(key=lambda s: s.t_start)
        self.for_video = for_video or ""
        self.frame_size = list(frame_size or [])
        self.note = note

    # ---------------------------------------------------------------- 查询
    def segment_at(self, t: float) -> Optional[Segment]:
        """取 t 时刻所属的镜头段；落在空隙里时取**最近**的一段。

        边界（正好等于切镜时刻）归**后一段** —— 那一帧已经是新镜头了。
        所以这里倒序找，让后一段优先命中。
        """
        if not self.segments:
            return None
        t = float(t)
        for s in reversed(self.segments):
            if s.t_start <= t <= s.t_end:
                return s
        # 落在两段之间（或视频首尾之外）→ 取时间上最近的一段
        return min(self.segments,
                   key=lambda s: min(abs(t - s.t_start), abs(t - s.t_end)))

    def cal_at(self, t: float, for_video: str = "") -> Optional[Calibration]:
        s = self.segment_at(t)
        if s is None:
            return None
        return s.calibration(for_video=self.for_video or for_video,
                             frame_size=self.frame_size)

    @property
    def primary(self) -> Optional[Segment]:
        """点数最多的那段 —— 用来做展示/叠加（如篮筐投影、画面回投线）。"""
        if not self.segments:
            return None
        return max(self.segments, key=lambda s: (s.n_points, s.ratio))

    def to_court(self, t: float, x: float, y: float):
        cal = self.cal_at(t)
        if cal is None:
            return None
        return cal.to_court(x, y)

    def to_pixel(self, t: float, x: float, y: float):
        cal = self.cal_at(t)
        if cal is None:
            return None
        return cal.to_pixel(x, y)

    def in_court(self, t: float, x: float, y: float, tol: float = 0.0) -> bool:
        cal = self.cal_at(t)
        if cal is None:
            return False
        try:
            return bool(cal.in_court(x, y, tol=tol))
        except TypeError:                    # 兼容不同签名
            return bool(cal.in_court(x, y, tol))

    def matches_video(self, video_path: str, W: int, H: int) -> bool:
        for s in self.segments:
            try:
                if s.calibration(for_video=self.for_video,
                                 frame_size=self.frame_size).matches_video(
                                     video_path, W, H):
                    return True
            except Exception:                # noqa: BLE001
                continue
        return False

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> dict:
        """融进现有的 `calibration_<stem>.json` ——
        顶层仍然写一份"主标定"的字段（老代码/老界面读得懂），
        另外加 `segments` 给新代码用。"""
        p = self.primary
        out: dict = {}
        if p is not None:
            base = p.calibration(for_video=self.for_video,
                                 frame_size=self.frame_size)
            out = dict(getattr(base, "__dict__", {}) or {})
        out["multi_shot"] = True
        out["segments"] = [s.to_dict() for s in self.segments]
        out["for_video"] = self.for_video
        out["frame_size"] = self.frame_size
        if self.note:
            out["multi_note"] = self.note
        if p is not None:
            out["reproj_error_m"] = p.rmse_m
            out["point_names"] = list(p.names)
        return out

    @classmethod
    def from_dict(cls, d: dict) -> Optional["MultiCal"]:
        segs = d.get("segments")
        if not segs:
            return None
        return cls([Segment.from_dict(x) for x in segs],
                   for_video=d.get("for_video") or "",
                   frame_size=d.get("frame_size") or [],
                   note=d.get("multi_note") or "")

    # ---------------------------------------------------------------- 概览
    def summary(self) -> dict:
        return {"multi_shot": True, "n_segments": len(self.segments),
                "segments": [{"t_start": s.t_start, "t_end": s.t_end,
                              "n_points": s.n_points, "rmse_m": s.rmse_m,
                              "ratio": s.ratio, "frame": s.frame}
                             for s in self.segments]}


# ---------------------------------------------------------------------- 切段
def plan_segments(duration: float, cuts: list, marks: list,
                  pad: float = 0.0) -> list:
    """按切镜时刻把 [0, duration] 切成若干段。

    marks 是用户标点的时刻列表；切出来的段会被**收拢到**包含 mark 的那些段，
    没用到的段直接丢掉 —— 用户没标过的镜头，我们没有它的标定，留着没意义。

    返回 [(t_start, t_end, [marks...]), ...]
    """
    d = max(0.0, float(duration or 0.0))
    cs = sorted({round(float(c), 3) for c in (cuts or []) if 0 < float(c) < d})
    bounds = [0.0] + cs + [d]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        inside = [float(m) for m in (marks or []) if a - 1e-6 <= float(m) <= b + 1e-6]
        if inside:
            out.append((max(0.0, a - pad), min(d, b + pad), inside))
    return out


# ------------------------------------------------------------------ 切镜定位
def _same_shot(video: str, a: float, b: float) -> bool:
    """两帧是不是同一个镜头（用 ORB 内点率判，间隔大时走链式估计）。"""
    try:
        from .frame_motion import estimate_motion, _motion_via_chain
        if abs(b - a) <= 0.6:
            return bool(estimate_motion(video, a, b).get("ok"))
        return bool(_motion_via_chain(video, a, b).get("ok"))
    except Exception:                        # noqa: BLE001
        return True                          # 判不了就当同一镜头，别乱切


def find_cut_between(video: str, a: float, b: float,
                     coarse: float = 1.0, fine: float = 0.15,
                     budget: int = 40) -> Optional[float]:
    """在 (a, b) 之间找出**切镜时刻**；找不到就返回 None。

    做法：先按 coarse 均匀采样，找到第一对"相邻样本不同镜头"的区间，
    再在这个区间里二分细化到 fine 精度。比扫全片快得多（用户只标了几帧）。
    """
    a, b = float(a), float(b)
    if b - a < 0.4:
        return None
    calls = 0
    n = max(2, int(round((b - a) / coarse)))
    times = [a + (b - a) * i / n for i in range(n + 1)]
    lo = None
    for t0, t1 in zip(times, times[1:]):
        calls += 1
        if calls > budget:
            break
        if not _same_shot(video, t0, t1):
            lo = (t0, t1)
            break
    if lo is None:
        return None
    x, y = lo
    while (y - x) > fine and calls <= budget:
        mid = (x + y) / 2.0
        calls += 1
        if _same_shot(video, x, mid):
            x = mid
        else:
            y = mid
    return round((x + y) / 2.0, 2)


def split_times_by_shot(video: str, times: list) -> tuple:
    """把用户标点的时刻按镜头分组。

    返回 (groups, cuts)：
      groups = [[t...], [t...]] —— 每个元素是同一镜头里的一组时刻
      cuts   = [切镜时刻...]
    """
    ts = sorted({round(float(t), 3) for t in (times or [])})
    if not ts:
        return [], []
    groups = [[ts[0]]]
    cuts = []
    for t0, t1 in zip(ts, ts[1:]):
        if _same_shot(video, t0, t1):
            groups[-1].append(t1)
            continue
        c = find_cut_between(video, t0, t1)
        cuts.append(c if c is not None else t1)
        groups.append([t1])
    return groups, cuts


# ------------------------------------------------------------------ 分镜解算
def _solve_one(group_frames: list, W: int, H: int):
    """对一个镜头里的点解一份标定。

    group_frames: [{"t":..., "px": [[x,y]...], "dst": [[mx,my]...], "names": [...]}]
    返回 (Segment|None, note)
    """
    import numpy as np

    from .calibcheck import frame_fit_detail
    from .court import find_homography

    src, dst, names = [], [], []
    bgr_ref = None
    for f in group_frames:
        if bgr_ref is None and f.get("bgr") is not None:
            bgr_ref = f["bgr"]
        for p, d, n in zip(f.get("px") or [], f.get("dst") or [],
                           f.get("names") or []):
            src.append(list(p))
            dst.append(list(d))
            names.append(n)
    if len(src) < 4:
        return None, "这一组只有 %d 个点，解不出标定" % len(src)

    via = "dlt"
    try:
        Hm = find_homography(src, dst)
    except Exception:                                   # noqa: BLE001
        import cv2
        try:
            Hr, _mask = cv2.findHomography(
                np.array(src, dtype=np.float64),
                np.array(dst, dtype=np.float64), cv2.RANSAC, 5.0)
            if Hr is None:
                return None, "RANSAC 也没找到一致子集"
            Hm = Hr.tolist()
            via = "ransac"
        except Exception as e:                          # noqa: BLE001
            return None, "解不出单应矩阵（%s: %s）" % (type(e).__name__, e)

    cal = Calibration(name="seg", method="web-keypoints-multi", src_px=src,
                      dst_m=dst, H=Hm, frame="full", frame_size=[W, H])
    rmse = 0.0
    ratio = 0.0
    try:
        cal = cal.fit()
        rmse = float(getattr(cal, "reproj_error_m", 0.0) or 0.0)
    except Exception:                                   # noqa: BLE001
        pass
    if bgr_ref is not None:
        try:
            d = frame_fit_detail(bgr_ref, cal)
            ratio = float(d.get("ratio") or 0.0)
        except Exception:                               # noqa: BLE001
            pass
    seg = Segment(t_start=0.0, t_end=0.0, src_px=src, dst_m=dst, H=list(Hm),
                  frame="full", names=names, rmse_m=rmse, ratio=ratio,
                  method="web-keypoints-multi", n_points=len(src),
                  note="%s 解算，%d 个点" % (via, len(src)))
    return seg, ""


def solve_per_shot(video: str, frames: list, duration: float = 0.0,
                   frame_grab=None) -> dict:
    """**按镜头分别解算**（绝不跨镜头合并）。

    frames: [{"t": 12.3, "px": [[x,y]...], "dst": [[mx,my]...], "names": [...]}]
            px 是**像素**坐标，dst 是球场坐标（米）。
    frame_grab: 可选，callable(t) -> BGR ndarray，用来算吻合度。

    返回 {"ok", "multical", "groups", "cuts", "notes"}
    """
    frames = [f for f in (frames or []) if f.get("px")]
    if not frames:
        return {"ok": False, "multical": None, "groups": [], "cuts": [],
                "notes": ["没有标点数据"]}
    W = int(frames[0].get("W") or 0)
    H = int(frames[0].get("H") or 0)

    times = [float(f["t"]) for f in frames]
    groups, cuts = split_times_by_shot(video, times)

    notes = []
    if len(groups) > 1:
        notes.append("检测到 %d 个镜头（切镜时刻 %s）—— 已**按镜头分别解算**："
                     "每个镜头一份单应矩阵，各自把自己覆盖的部分投到球场坐标后"
                     "再合并。两台机位本来就没有共同坐标系，硬合并成一份必然矛盾。"
                     % (len(groups), "、".join("%.1fs" % c for c in cuts)))
    by_t = {round(float(f["t"]), 3): f for f in frames}
    segs = []
    for gi, g in enumerate(groups):
        gf = []
        for t in g:
            f = by_t.get(round(float(t), 3))
            if not f:
                continue
            f2 = dict(f)
            if frame_grab is not None and f2.get("bgr") is None:
                try:
                    f2["bgr"] = frame_grab(float(t))
                except Exception:                       # noqa: BLE001
                    f2["bgr"] = None
            gf.append(f2)
        seg, why = _solve_one(gf, W, H)
        if seg is None:
            notes.append("第 %d 个镜头（t=%s）：%s"
                         % (gi + 1, "、".join("%.1f" % x for x in g), why))
            continue
        lo = 0.0
        hi = float(duration or 0.0)
        for c in cuts:
            if c <= min(g):
                lo = max(lo, c)
            if c >= max(g):
                hi = min(hi, c) if hi > 0 else c
        if hi <= 0:
            hi = lo + 9999.0
        seg.t_start = round(float(lo), 3)
        seg.t_end = round(float(hi), 3)
        segs.append(seg)
        notes.append("镜头 %d（t=%.1f~%.1fs）：%d 个点，吻合度 %.2f，误差 %.2f m"
                     % (gi + 1, seg.t_start, seg.t_end, seg.n_points,
                        seg.ratio, seg.rmse_m))
    if not segs:
        return {"ok": False, "multical": None, "groups": groups, "cuts": cuts,
                "notes": notes or ["每个镜头都解不出标定"]}
    mc = MultiCal(segs, for_video=video,
                  frame_size=[W, H] if W and H else [],
                  note="；".join(notes))
    return {"ok": True, "multical": mc, "groups": groups, "cuts": cuts,
            "notes": notes}
