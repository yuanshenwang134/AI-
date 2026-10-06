"""两个「说了等于没说」的坑：都让用户看到一片空却不知道原因/下一步。

背景（用户实测 2026-10-06 原话：「为什么我现在分析的这个视频偷懒，
战术图，投篮热区啥都没检测出来？」）——查真实产物得到完整因果链：

    篮筐没标定/没识别出 → 判不出出手（0 次）
        ├─→ 投篮热区：空（没有出手可画）
        └─→ 战术图：另受标定吻合度卡住

本文件钉住两件事：
  ① `calibration_is_manual` 必须真的被**写入** —— 它以前只被 pipeline 读、
     从来没人写，于是"手动标定放行出热区/战术图"那条口子**永远不生效**；
  ② 判不出结果时必须给出**可执行的下一步**（标一次篮筐），而不是只说
     "没有检测到可用篮筐"然后一片 0。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.pipeline import _hoop_next_step, _judgement  # noqa: E402


def test_manual_flag_is_written_somewhere():
    """光有读取端不算 —— 必须有写入端，否则那条放行口子是死的。"""
    src = io.open(os.path.join(ROOT, "src", "aihoop", "sources.py"),
                  encoding="utf-8").read()
    assert 'rt.detections_meta["calibration_is_manual"]' in src, \
        "sources.py 必须写入 calibration_is_manual（pipeline 才会放行手动标定）"


def test_manual_detection_covers_web_keypoints():
    """手动标定的方法名要能被认出来（界面存的是 web-keypoints-multi）。"""
    src = io.open(os.path.join(ROOT, "src", "aihoop", "sources.py"),
                  encoding="utf-8").read()
    assert "web-keypoints" in src, "方法名前缀没覆盖 web-keypoints"


def test_next_step_given_when_hoop_missing():
    meta = {"visual": {"reasons": ["视觉路径：没有检测到可用篮筐，无法确认投篮结果"]}}
    t = _hoop_next_step(meta)
    assert t, "篮筐缺失时必须给出下一步"
    assert "标篮筐" in t and "一个点" in t, t


def test_no_next_step_when_hoop_present():
    meta = {"visual": {"reasons": ["别的原因"]}, "hoop": {"cx": 100, "cy": 200}}
    assert _hoop_next_step(meta) == "", "有篮筐时不该给标篮筐的提示"


def test_judgement_carries_next_step():
    meta = {"score_policy": "court",
            "visual": {"reasons": ["视觉路径：没有检测到可用篮筐，无法确认投篮结果"]}}
    g = _judgement(meta, [])
    assert g["state"] in ("cannot_judge", "court")
    assert g.get("next_step"), "判不出结果时 judgement 要带上 next_step"


def test_report_renders_next_step():
    """报告里必须把 next_step 写出来 —— 用户看的就是这份 md。"""
    src = io.open(os.path.join(ROOT, "src", "aihoop", "report.py"),
                  encoding="utf-8").read()
    assert "next_step" in src, "report.py 要渲染 judgement.next_step"
