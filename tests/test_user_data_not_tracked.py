"""用户数据绝不能进 git —— 标定、标注、比分牌事件、上传的视频都不行。

为什么单独立一条（我踩过两次，第二次是刚发生的）：
  1. `data/calibration_<stem>.json` 是用户**手工在画面上一个个点出来**的标定，
     不被 git 跟踪，所以我在测试里直接往真实路径写文件就把用户的标定覆盖掉了，
     而且**无法恢复**（git 里没有、out/ 里也没有副本）。
  2. 我给标定写盘加了自动备份（`*.json.bak` / `*.bak1`）之后，
     `git add -A` 把 `.bak` **提交进了仓库** ——
     因为 `.gitignore` 里的 `data/calibration_*.json` 要求文件名**以 .json 结尾**，
     匹配不到 `.json.bak`。用户数据就这样进了版本库。

  这两次都是"以为 gitignore 挡住了、其实没有"。所以这里**直接问 git**
  （`git ls-files` / `git check-ignore`），而不是再读一遍 .gitignore 猜。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 仓库**自带**的资源，允许被跟踪
SHIPPED = {
    "data/botsort_reid.yaml",
    "data/court_kp_map.json",
    "data/scoreboard_templates.npz",
    "data/.gitkeep",
}
# 用户数据：这些前缀/后缀组合一旦出现在 git 里就是事故
USER_DATA_HINTS = ("calibration", "marks_", "sb_events_", "basket_feedback",
                   "scoreboard_bug", "jobs.sqlite")
USER_DATA_EXT = (".mp4", ".avi", ".mov", ".mkv", ".bak", ".bak1", ".tmp")


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                       errors="replace", timeout=60)
    return r.stdout if r.returncode == 0 else ""


@pytest.mark.skipif(not os.path.isdir(os.path.join(ROOT, ".git")),
                    reason="不是 git 仓库")
def test_no_user_data_is_tracked():
    tracked = [x.strip().replace("\\", "/")
               for x in _git("ls-files", "data/").splitlines() if x.strip()]
    bad = []
    for f in tracked:
        if f in SHIPPED:
            continue
        low = f.lower()
        if any(h in low for h in USER_DATA_HINTS) or low.endswith(USER_DATA_EXT):
            bad.append(f)
    assert not bad, (
        "这些**用户数据**被 git 跟踪了（标定/标注是用户手工点出来的，不该进版本库）：\n  "
        + "\n  ".join(bad)
        + "\n修法：git rm --cached <路径>，并在 .gitignore 里补上匹配规则。")


@pytest.mark.skipif(not os.path.isdir(os.path.join(ROOT, ".git")),
                    reason="不是 git 仓库")
def test_calibration_backups_are_ignored():
    """自动备份（`*.json.bak` / `.bak1` / `.tmp`）必须被忽略。

    ⚠️ 注意 `.gitignore` 里已有的 `data/calibration_*.json` **匹配不到**
    `data/calibration_x.json.bak`（那个模式要求以 .json 结尾）——
    这正是我误提交的原因，所以要分别钉住。
    """
    for name in ("data/calibration_x.json", "data/calibration_x.json.bak",
                 "data/calibration_x.json.bak1", "data/calibration_x.json.tmp",
                 "data/marks_x.json", "data/sb_events_x.json",
                 "data/uploads/x.mp4"):
        out = _git("check-ignore", "-v", name).strip()
        assert out, "这个路径没有被 .gitignore 忽略：%s" % name
