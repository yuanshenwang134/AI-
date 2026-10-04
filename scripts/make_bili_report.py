"""把 bili_nybo.mp4 的人工确认结果整理成一份可交付报告。

自动判定在这段素材上做不到（8 条路实测全败，见 README 失败案例），
但用户已经逐球确认了 32 个候选 —— 这份**人工确认的结果是正确且可交付的**，
所以把它整理成报告，而不是让工作白做。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FB = ROOT / "data" / "basket_feedback.jsonl"
OUT = ROOT / "out" / "bili_nybo_report.md"

rows = [json.loads(l) for l in FB.read_text(encoding="utf-8").splitlines()
        if l.strip()]
bili = [r for r in rows if "bili" in str(r.get("video", ""))]
made = sorted((float(r["t"]), r) for r in bili if r["label"] == "made")
miss = sorted(float(r["t"]) for r in bili if r["label"] == "miss")


def mmss(t):
    m, s = divmod(int(round(t)), 60)
    return f"{m:02d}:{s:02d}"


lines = ["# bili_nybo 比赛片段：进球确认报告", "",
         f"- 素材：`data/bili_nybo.mp4`（47 分钟，1920×1080，全景机位）",
         f"- 篮筐：人工标定 `(60, 30)` 半径 `45×13` 像素（`data/marks_bili_nybo.json`）",
         f"- 候选：扫描器共给出 **109** 个候选时刻，其中人工复核 **32** 个",
         f"- 结果：**确认进球 {len(made)} 个 / 未进 {len(miss)} 个**", "",
         "> ⚠️ 说明：这份结果是**人工逐球确认**的，不是自动判定的。",
         "> 自动判定在这段素材上实测不可用（8 种方法全部失败，",
         "> 原因与实测数据见 `README.md` 的「已知的失败案例」一节）。", "",
         "## 确认进球", "",
         "| # | 时间 | 时间戳(秒) |", "|---|---|---|"]
for i, (t, _r) in enumerate(made, 1):
    lines.append(f"| {i} | {mmss(t)} | {t:.2f} |")
lines += ["", f"按每次进球 2 分计：**{len(made)} 球 = {len(made) * 2} 分**",
          "（本片段没有可用的球场标定，无法判定 2/3 分，故按 2 分记）", "",
          "## 确认未进的候选时刻", ""]
lines.append("、".join(f"{mmss(t)}" for t in miss[:40]))
lines += ["", "", "## 复现方式", "", "```powershell",
          "python scripts\\mark_landmarks.py --video data\\bili_nybo.mp4 --at 1400",
          "python scripts\\scan_bili_candidates.py",
          "# 界面「训练标注」页逐球判：进/没进",
          "```"]
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"已写出 {OUT}")
print(f"确认进球 {len(made)} 个，未进 {len(miss)} 个")
