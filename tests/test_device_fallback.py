"""推理设备兜底：请求了 GPU 但机器没有 CUDA 时，必须落到 CPU 而不是抛异常。

用户实测那次失败就是这里：
    ValueError: Invalid CUDA 'device=0' requested ... torch.cuda.device_count(): 0
    sources.py in run -> model.track(..., device=self.device, ...)
根因：JobCreate.device 默认写死 "0"（第一块 GPU），而这台机器装的是
torch 2.14.1+cpu —— 任务 0 秒就失败，用户只看到一大段 CUDA 报错。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.court import Calibration  # noqa: E402
from aihoop.sources import VideoSource  # noqa: E402

CAL = Calibration(name="x", method="m",
                  src_px=[[0, 0], [1, 0], [1, 1], [0, 1]],
                  dst_m=[[-7.5, 0.0], [7.5, 0.0], [7.5, -14.0], [-7.5, -14.0]],
                  H=[[1, 0, 0], [0, 1, 0], [0, 0, 1]])


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available() and torch.cuda.device_count() > 0)
    except Exception:  # noqa: BLE001
        return False


def _vs(device):
    return VideoSource("x.mp4", CAL, device=device)


def test_gpu_request_falls_back_to_cpu_without_cuda():
    if _cuda_available():
        pytest.skip("这台机器有 CUDA，兜底分支不适用")
    for want in ("0", "0,1", "cuda:0", "1"):
        vs = _vs(want)
        note = vs._resolve_device()
        assert vs.device == "cpu", "请求 %r 时应落到 cpu，实际 %r" % (want, vs.device)
        assert "CUDA" in note and "cpu" in note.lower(), \
            "落到 CPU 时必须说明原因（不能静默改变行为）：%r" % note


def test_cpu_and_auto_need_no_note():
    if _cuda_available():
        pytest.skip("有 CUDA 时 \"自动\" 会选 GPU，与这里的前提不同")
    for want in ("cpu", "", None):
        vs = _vs(want)
        note = vs._resolve_device()
        assert vs.device == "cpu", "请求 %r 应留在 cpu" % (want,)
        assert note == "", "显式要 CPU / 留空自动，都不该报警告：%r" % note


def test_non_gpu_device_string_is_passed_through():
    """其它设备串（如 mps）不该被我们改写，交给 ultralytics 自己解释。"""
    vs = _vs("mps")
    note = vs._resolve_device()
    assert vs.device == "mps" and note == ""


def test_job_device_default_is_auto():
    """JobCreate.device 默认必须是"自动"，不能再写死 "0"。"""
    from aihoop.api import JobCreate
    assert JobCreate().device == "", \
        "默认 device 必须是空串（= 自动），写死 \"0\" 会让没有 GPU 的机器直接失败"
