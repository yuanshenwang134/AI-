"""多画面标定接口：**端到端能返回结果**（不是 500）。

为什么单独钉这一条：`POST /api/calibrate_multi` 一旦抛异常，FastAPI 返回的是
**纯文本 500 且没有 CORS 头**（前端跑在另一个端口上），浏览器只会给出
`TypeError: Failed to fetch` —— 用户看到的就一句"解算失败：Failed to fetch"，
完全不知道是后端崩了。实测就是这么踩的：`n_used` 在使用之后才赋值，
`elif n_used <= 4` 直接 UnboundLocalError，用户点了 10 个点却怎么都存不下标定。

所以这里用一个**真实存在的小视频**（可由 ffmpeg 合成，不需要网络）跑通
"点 5 个点 -> 预览 -> 保存 -> 读回"整条链路。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aihoop import api                      # noqa: E402

# 点名字必须真的在坐标表里，否则后端会**静默丢弃**（实测踩过），这里先断言一下
assert "hoop_far" in api.COURT_LANDMARKS and "arc_far" in api.COURT_LANDMARKS


def _work_dir() -> Path:
    """在**仓库内**建临时目录：pytest 的 tmp_path 落在 %TEMP%，
    某些受限环境（本机就是）连创建/清理都报 WinError 5，测试直接起不来。"""
    d = ROOT / "_tmp" / "pytest_calib"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_video(path: Path) -> bool:
    """用便携版 ffmpeg 合成一段 1 秒的小视频（没有 ffmpeg 就跳过测试）。"""
    if path.exists():
        return True
    sys.path.insert(0, str(ROOT / "src"))
    from aihoop.highlight import ffmpeg_path
    ff = ffmpeg_path()
    if not ff:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "testsrc=size=640x480:rate=10", "-t", "1",
           "-pix_fmt", "yuv420p", "-y", str(path)]
    subprocess.run(cmd, check=False)
    return path.exists() and path.stat().st_size > 0


def _payload(video: Path) -> dict:
    # 5 个真实场地特征点（归一化坐标），铺得开、不共线
    return {
        "video_path": str(video),
        "compensate": True,
        "confirm": False,
        "frames": [{"t": 0.5, "landmarks": {
            "hoop_far": [0.36, 0.51],
            "corner_far_left": [0.20, 0.72],
            "corner_far_right": [0.65, 0.71],
            "lane_far_left": [0.50, 0.81],
            "arc_far": [0.76, 0.66],
        }}],
    }


def test_calibrate_multi_returns_result_instead_of_500():
    video = _work_dir() / "tiny.mp4"
    if not _make_video(video):
        pytest.skip("本机没有 ffmpeg，跳过（跑 python tools_fetch_ffmpeg.py 可装）")

    body = _payload(video)
    req = api.MultiCalibRequest(**body)
    # 关键：**不许抛异常**。以前这里抛 UnboundLocalError，接口 500，
    # 前端只能显示 "解算失败：Failed to fetch"。
    out = asyncio.run(api.post_calibrate_multi(req))

    assert isinstance(out, dict)
    assert "ok" in out and "rmse_m" in out and "note" in out
    # 5 个点都在同一幅画面上 —— 覆盖率读数必须认出来（不能是 0）
    assert out["n_points"] == 5
    assert max(f["n"] for f in out["per_frame_fit"]) == 5


def test_calibrate_multi_confirm_saves_and_is_readable():
    video = _work_dir() / "tiny2.mp4"
    if not _make_video(video):
        pytest.skip("本机没有 ffmpeg，跳过")

    body = _payload(video)
    body["confirm"] = True
    # 隔离：标定落盘路径重定向到测试目录。
    # 不然会写进真实的 data/calibration_*.json —— 第二次跑 revision 就是 2，
    # 断言莫名失败，还会给用户留下一份测试标定（这个坑实测踩到）。
    out_box = _work_dir() / "calib_out"
    if out_box.exists():                       # 上一次跑留下的 revision 会让断言不稳
        for f in out_box.glob("*.json"):
            f.unlink()
    out_box.mkdir(parents=True, exist_ok=True)
    old = api._calibration_path_for
    api._calibration_path_for = lambda v: out_box / (Path(v).stem + ".json")
    try:
        out = asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))
    finally:
        api._calibration_path_for = old

    assert out["saved"] is True
    assert out["revision"] == 1
    assert out["path"] and Path(out["path"]).exists()
    assert Path(out["path"]).parent == out_box          # 确实没写到真实 data/
    saved = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert saved["H"] and len(saved["H"]) == 3
    assert saved["frame_size"] == [640, 480]
    # 落盘的是**实际参与解算**的那一组点，不是收到的全部
    assert len(saved["src_px"]) == out["n_points"]


def test_calibrate_multi_rejects_when_no_frame_has_four_points():
    video = _work_dir() / "tiny3.mp4"
    if not _make_video(video):
        pytest.skip("本机没有 ffmpeg，跳过")

    body = _payload(video)
    # 把同一幅画面的点拆到两幅画面上：每幅都不够 4 个 -> 拦下并说清楚
    lm = body["frames"][0]["landmarks"]
    names = list(lm)
    body["frames"] = [
        {"t": 0.3, "landmarks": {k: lm[k] for k in names[:3]}},
        {"t": 0.7, "landmarks": {k: lm[k] for k in names[3:]}},
    ]
    out = asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))
    assert out["ok"] is False
    assert "4 个点" in str(out["note"])
