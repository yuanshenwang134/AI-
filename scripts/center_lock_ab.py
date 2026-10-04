# -*- coding: utf-8 -*-
"""中心锁定（legacy_center_lock）的真实素材 A/B/C 对照。

要回答的问题（旧项目 `docs/strict_shot_gate.md` 记的老毛病）：
**只标了篮筐中心、没给半径时，旧引擎会不会把前后场两个筐当成同一个**，
于是"传球被记成出手""球从筐前掠过也算进"。中心锁定就是为这个写的实验开关。

为什么要三臂：`hoop_hint` 本身会改变"第一次选哪个筐"（`legacy_shots/stream.py:78-81`），
所以"有提示 + 关锁"和"无提示 + 关锁"不是同一个对照。公平对照是：

    A 无提示、锁关     —— 现状（也是后面挑提示点的数据来源）
    B 有提示、锁关     —— 只把筐定在某个筐上，不限制后续候选
    C 有提示、锁开     —— 中心锁定的实际效果

判据（全部从产物里读，不靠猜）：
  * 每次出手判罚时"软件认为的筐"在哪（`legacy_shots.events[].frame` → `frames[].rim`），
    以及这些筐心的横向跨度（跨度大 = 在几个筐之间来回跳）；
  * `center_rejected`（被中心锁拒掉的候选数）；
  * 出手条数与命中/未知分布。

用法：
    python scripts/center_lock_ab.py <视频路径> [--api http://127.0.0.1:8000]
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\dsh_folder\aihoop")


def post(api: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        api + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get(api: str, path: str) -> dict:
    with urllib.request.urlopen(api + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def run_arm(api: str, video: Path, tag: str, hint, lock: bool) -> dict:
    body = {"source": "video", "video_path": str(video),
            "allow_no_calibration": True, "shot_engine": "legacy",
            "score_policy": "court", "detect_players": True,
            "make_highlights": False, "legacy_center_lock": bool(lock)}
    if hint:
        body["hoop_hint"] = [float(hint[0]), float(hint[1])]
    print("\n[%s] 提交：hint=%s center_lock=%s" % (tag, hint, lock), flush=True)
    jid = (post(api, "/api/jobs", body) or {}).get("job_id")
    t0 = time.time()
    while True:
        st = get(api, "/api/jobs/" + str(jid))
        if st.get("status") in ("done", "error", "failed", "cancelled"):
            break
        if time.time() - t0 > 3600:
            raise RuntimeError("任务超时：" + str(jid))
        time.sleep(5)
    print("    status=%s 耗时=%.0fs job=%s" % (st.get("status"), time.time() - t0, jid),
          flush=True)
    return {"tag": tag, "job": jid, "status": st.get("status"), "elapsed_s": round(time.time() - t0),
            "lock": bool(lock), "hint": list(hint) if hint else None}


def read_legacy(job: str) -> dict:
    """从 raw_track.json 里取旧引擎的逐帧依据与判定时用的筐。"""
    p = ROOT / "out" / job / "raw_track.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    ls = ((d.get("detections_meta") or {}).get("legacy_shots")) or {}
    frames = {int(f["frame"]): f for f in (ls.get("frames") or [])}
    rows = []
    for ev in (ls.get("events") or []):
        fr = frames.get(int(ev.get("frame") or -1)) or {}
        rim = fr.get("rim") or {}
        box = rim.get("xyxy") or [None] * 4
        cx = round((box[0] + box[2]) / 2, 1) if box[0] is not None else None
        cy = round((box[1] + box[3]) / 2, 1) if box[1] is not None else None
        rows.append({"t": ev.get("t"), "type": ev.get("type"), "frame": ev.get("frame"),
                     "rim_cx": cx, "rim_cy": cy, "crossing_t": ev.get("crossing_t")})
    return {"events": rows, "frames": ls.get("frames") or [],
            "center_rejected": ls.get("center_rejected"),
            "center_lock_width": ls.get("center_lock_width"),
            "center_hint": ls.get("center_hint"),
            "real_ball_frames": ls.get("real_ball_frames"),
            "rim_frames": ls.get("rim_frames")}


def harvest_hint(frames: list[dict]) -> tuple | None:
    """从 A 臂的逐帧筐检测里挑出"主筐"：按筐心 x 一维聚成两簇，取帧数多的那簇中位数。"""
    xs = []
    for f in frames:
        rim = f.get("rim") or {}
        box = rim.get("xyxy")
        if not box:
            continue
        xs.append(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    if len(xs) < 20:
        return None
    xs.sort()
    gap_i, gap = 0, -1.0
    for i in range(1, len(xs)):
        if xs[i][0] - xs[i - 1][0] > gap:
            gap, gap_i = xs[i][0] - xs[i - 1][0], i
    left, right = xs[:gap_i], xs[gap_i:]
    main = left if len(left) >= len(right) else right
    return (round(statistics.median([p[0] for p in main]), 1),
            round(statistics.median([p[1] for p in main]), 1),
            len(left), len(right), round(gap, 1))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    video = Path(sys.argv[1])
    api = "http://127.0.0.1:8000"
    if "--api" in sys.argv:
        api = sys.argv[sys.argv.index("--api") + 1]
    if not video.exists():
        print("找不到视频：", video)
        return 2

    out = {"video": str(video), "arms": []}

    arm_a = run_arm(api, video, "A 无提示+锁关", None, False)
    la = read_legacy(arm_a["job"])
    arm_a["legacy"] = {k: la.get(k) for k in
                       ("center_rejected", "center_lock_width", "center_hint",
                        "real_ball_frames", "rim_frames")}
    arm_a["events"] = la.get("events")
    picked = harvest_hint(la.get("frames") or [])
    print("    A 臂筐候选分布：%s" % (picked,), flush=True)
    out["arms"].append(arm_a)

    if not picked:
        print("!! A 臂没检出足够筐候选，无法挑提示点，A/B/C 对照中止（只留 A 臂结果）")
        (ROOT / "out" / "center_lock_ab.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    hint = (picked[0], picked[1])
    print("    选用主筐中心作为人工提示：%s（左簇 %d 帧 / 右簇 %d 帧，簇间距 %.1fpx）"
          % (hint, picked[2], picked[3], picked[4]), flush=True)
    out["hint_pick"] = {"hint": list(hint), "left_frames": picked[2],
                        "right_frames": picked[3], "gap_px": picked[4]}

    for tag, lock in (("B 有提示+锁关", False), ("C 有提示+锁开", True)):
        arm = run_arm(api, video, tag, hint, lock)
        lg = read_legacy(arm["job"])
        arm["legacy"] = {k: lg.get(k) for k in
                         ("center_rejected", "center_lock_width", "center_hint",
                          "real_ball_frames", "rim_frames")}
        arm["events"] = lg.get("events")
        out["arms"].append(arm)

    (ROOT / "out" / "center_lock_ab.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 100)
    print("对照结果（每次判罚时『软件认为的筐』的横向跨度越大 = 越可能在两个筐之间跳）")
    for arm in out["arms"]:
        rows = arm.get("events") or []
        cxs = [r["rim_cx"] for r in rows if r.get("rim_cx") is not None]
        typ = [r["type"] for r in rows]
        span = (max(cxs) - min(cxs)) if cxs else None
        print("  %-14s job=%-13s 出手=%-2d 进=%-2d 未中=%-2d 未知=%-2d 筐心跨度=%-7s "
              "被中心锁拒=%s"
              % (arm["tag"], arm["job"], len(rows), typ.count("make"),
                 typ.count("miss"), typ.count("uncertain"),
                 ("%.0fpx" % span) if span is not None else "-",
                 (arm.get("legacy") or {}).get("center_rejected")))
        if cxs:
            print("      逐次判罚筐心 x：%s" % [round(c) for c in cxs])
    dest = ROOT / "out" / "center_lock_ab.json"
    print("\n明细：%s" % dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
