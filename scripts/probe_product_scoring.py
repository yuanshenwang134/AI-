# -*- coding: utf-8 -*-
"""走**产品路径**（POST /api/jobs，默认 detect_players=True）跑一段视频，
把界面会显示的"命中 / 计分 / 得分"逐条打出来。

为什么必须单独有这个脚本：`scripts/eval_timed_truth.py` 之类的评测都直接构造
`VideoSource(..., detect_players=False)`（见该文件第 58 行），那条路上没有球员、
`team` 必然未知，旧引擎适配层会给每条投篮打上 `team_unknown` + `counts_for_score=False`。
所以"命中 5 个、界面得分 0:0"到底是不是产品路径的真实行为，只能用这个脚本问后端。

要点：
  * 任务参数与网页上传完全一致（allow_no_calibration=True、score_policy=court）；
  * 需要后端已在 8000 端口跑着（`python scripts/serve_reload.py 8000`）；
  * 跑完读 `out/<job_id>/game.json` 的 timeline，逐条打印 result /
    counts_for_score / points / team / player / tags，并给出"命中 N 条、计分 M 条"。

用法：python scripts/probe_product_scoring.py <视频文件名> [--engine legacy|geometry]
                                              [--highlights] [--center-lock]
      例：python scripts/probe_product_scoring.py nathan_freethrow.mov --highlights
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"
ROOT = Path(r"D:\dsh_folder\aihoop")
SAMPLES = Path(r"D:\dsh_folder\aihoop_assets\samples")


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "nathan_freethrow.mov"
    engine = "legacy"
    if "--engine" in sys.argv:
        engine = sys.argv[sys.argv.index("--engine") + 1]
    make_hl = "--highlights" in sys.argv
    center_lock = "--center-lock" in sys.argv
    # 只关心球/筐与判定时用 --no-players：跳过球员 YOLO，跑得快很多（代价是没有出手人归属）
    players = "--no-players" not in sys.argv
    hint = None
    if "--hint" in sys.argv:
        hint = [float(x) for x in sys.argv[sys.argv.index("--hint") + 1].split(",")]
    video = Path(name)
    if not video.is_absolute():
        # 先按素材目录找，再按仓库根找（_tmp 里的自切片常用）
        video = (SAMPLES / name) if (SAMPLES / name).exists() else (ROOT / name)
    if not video.exists():
        print("找不到视频：", video)
        return 2
    body = {"source": "video", "video_path": str(video),
            "allow_no_calibration": True, "shot_engine": engine,
            "score_policy": "court", "detect_players": players,
            "legacy_center_lock": center_lock,
            "make_highlights": make_hl}
    if hint:
        body["hoop_hint"] = hint
    print("提交任务：", name, "engine=", engine,
          "highlights=%s" % make_hl, "center_lock=%s" % center_lock,
          "players=%s" % players, "hint=%s" % hint)
    job = post("/api/jobs", body)
    jid = job.get("job_id") or job.get("id")
    print("job_id =", jid, json.dumps(job, ensure_ascii=False)[:200])
    t0 = time.time()
    while True:
        st = get("/api/jobs/" + str(jid))
        if st.get("status") in ("done", "error", "failed", "cancelled"):
            break
        if time.time() - t0 > 1800:
            print("超时")
            return 3
        time.sleep(5)
    print("status =", st.get("status"), "耗时 %.1fs" % (time.time() - t0))
    g = ROOT / "out" / str(jid) / "game.json"
    if not g.exists():
        print("没有 game.json，任务可能失败：", json.dumps(st, ensure_ascii=False)[:400])
        return 4
    game = json.loads(g.read_text(encoding="utf-8"))
    meta = game.get("meta") or {}
    print("shot_engine =", meta.get("shot_engine"),
          " center_lock =", meta.get("legacy_center_lock"))
    print("player_track =", json.dumps(meta.get("player_track"), ensure_ascii=False))
    print("score =", json.dumps(game.get("score"), ensure_ascii=False),
          " clip_score =", json.dumps(game.get("clip_score"), ensure_ascii=False))
    tl = game.get("timeline") or []
    print("timeline n =", len(tl))
    for x in tl:
        print("   t=%-6s %-8s counts=%-5s pts=%-2s 片段=[%s,%s] 判定收尾=%-6s 时间来源=%-11s "
              "cross=%-6s team=%-5s player=%-12s tags=%s"
              % (x.get("t"), x.get("result"), x.get("counts_for_score"), x.get("points"),
                 x.get("clip_start"), x.get("clip_end"), x.get("decision_t"),
                 x.get("release_source") or "-", x.get("crossing_t"),
                 x.get("team"), x.get("player"), ",".join(x.get("tags") or [])))
    n_made = sum(1 for x in tl if x.get("result") == "made")
    n_cnt = sum(1 for x in tl if x.get("result") == "made" and x.get("counts_for_score"))
    print("小结：命中 %d 条，其中计分 %d 条，界面得分 %s"
          % (n_made, n_cnt, json.dumps(game.get("score"), ensure_ascii=False)))
    clips = game.get("highlights") or []
    if clips:
        print("高光片段 %d 个（键：%s）" % (len(clips), ",".join(sorted(clips[0].keys()))))
        for c in clips[:8]:
            print("   #%s t=%s 区间=[%s,%s] made=%s 可播放=%s 文件=%s"
                  % (c.get("index"), c.get("t"), c.get("start"), c.get("end"),
                     c.get("made"), c.get("available"), c.get("path")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
