"""上线自检：把服务器上"缺什么就会翻车"的东西一次性体检出来。

用法（在服务器上、仓库根目录执行）：
    python scripts/check_server.py
    python scripts/check_server.py --ocr-video <一段视频>   # 额外试跑一次比分牌 OCR 路径

为什么需要它：本项目的失败模式大多不是"报错"，而是**静默降级**——
缺 YOLO_CONFIG_DIR 时报的是 Ultralytics 写 AppData 被拒；缺权重时球检测悄悄退回色块；
Linux 上比分牌 OCR 直接没有；磁盘满了会在写视频那一步才炸。上线前一次问清，比现场排查便宜。
退出码：0 = 关键项全过；1 = 有必须先解决的问题。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK, BAD, WARN = "[ok]  ", "[FAIL]", "[warn]"
problems: list[str] = []
warnings: list[str] = []


def say(tag: str, text: str) -> None:
    print(f"  {tag} {text}")


def check_runtime() -> None:
    print("\n=== 1. 运行时 ===")
    say(OK, f"平台 {platform.platform()}   Python {platform.python_version()}")
    if os.name != "nt":
        say(WARN, "非 Windows：比分牌 OCR 路径不可用（见 docs/部署方案.md 第 1 节），"
                  "只剩模板匹配那条路")
        warnings.append("Linux/容器上没有比分牌 OCR")
    print("  依赖：")
    for mod in ("fastapi", "uvicorn", "pydantic", "httpx", "numpy", "cv2",
                "torch", "ultralytics"):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            tag = OK
        except Exception as e:  # noqa: BLE001
            ver = f"{type(e).__name__}"
            tag = WARN if mod in ("torch", "ultralytics", "cv2") else BAD
            if tag == BAD:
                problems.append(f"缺依赖 {mod}（服务起不来）")
            else:
                warnings.append(f"缺 {mod}（真视频推理不可用）")
        say(tag, f"{mod:12} {ver}")


def check_env() -> None:
    print("\n=== 2. 环境变量 ===")
    pypath = os.environ.get("PYTHONPATH", "")
    if str(ROOT / "src") in pypath or (ROOT / "src").as_posix() in pypath:
        say(OK, "PYTHONPATH 含仓库 src")
    else:
        say(BAD, f"PYTHONPATH 里没有 {ROOT / 'src'} —— 服务会找不到 aihoop 包")
        problems.append("PYTHONPATH 未指向仓库 src")
    for var, why in (("YOLO_CONFIG_DIR", "不设会让 Ultralytics 去写用户目录被拒（本机实测）"),
                     ("TMP", "临时目录；受限环境里必须指向可写目录"),
                     ("TEMP", "同 TMP")):
        val = os.environ.get(var)
        if val:
            say(OK, f"{var} = {val}")
        else:
            say(WARN, f"{var} 未设置 —— {why}")
            warnings.append(f"{var} 未设置")
    cfg = os.environ.get("YOLO_CONFIG_DIR")
    if cfg:
        try:
            Path(cfg).mkdir(parents=True, exist_ok=True)
            (Path(cfg) / ".write_test").write_text("x", encoding="utf-8")
            (Path(cfg) / ".write_test").unlink()
            say(OK, "YOLO_CONFIG_DIR 可写")
        except Exception as e:  # noqa: BLE001
            say(BAD, f"YOLO_CONFIG_DIR 不可写：{e}")
            problems.append("YOLO_CONFIG_DIR 不可写")


def check_dirs_and_disk() -> None:
    print("\n=== 3. 目录与磁盘 ===")
    for rel in ("data", "data/uploads", "out"):
        p = ROOT / rel
        try:
            p.mkdir(parents=True, exist_ok=True)
            t = p / ".write_test"
            t.write_text("x", encoding="utf-8")
            t.unlink()
            say(OK, f"{rel}/ 可写")
        except Exception as e:  # noqa: BLE001
            say(BAD, f"{rel}/ 不可写：{e}")
            problems.append(f"{rel}/ 不可写")
    usage = shutil.disk_usage(str(ROOT))
    free_gb = usage.free / 2**30
    tag = OK if free_gb >= 10 else (WARN if free_gb >= 3 else BAD)
    say(tag, f"磁盘剩余 {free_gb:.1f} GB（建议 ≥10GB：单任务产物 2–350MB，上传原片另计）")
    if free_gb < 3:
        problems.append("磁盘不足 3GB")
    elif free_gb < 10:
        warnings.append("磁盘不足 10GB")


def check_ffmpeg() -> None:
    print("\n=== 4. ffmpeg（高光剪辑用）===")
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            say(OK, f"{name} → {p}")
            return
    local = ROOT / "ffmpeg.exe"
    if local.exists():
        say(OK, f"仓库内 {local.name}")
        return
    say(WARN, "PATH 里没有 ffmpeg —— 高光片段会降级为时间码（其余功能不受影响）")
    warnings.append("缺 ffmpeg")


def check_weights() -> None:
    print("\n=== 5. 模型权重（.gitignore 排除了，必须手工放到服务器）===")
    cands = [ROOT / "runs/detect/ball/weights/best.pt",
             ROOT / "runs/detect/rim/weights/best.pt",
             ROOT / "data/shot_tracker_best.pt"]
    env_dir = os.environ.get("AIHOOP_MODELS")
    if env_dir:
        cands += [Path(env_dir) / n for n in
                  ("rim_ball_v6.pt", "rim_ball.pt", "yolov8s.pt")]
    found = []
    for c in cands:
        if c.is_file():
            import hashlib
            h = hashlib.sha256()
            with open(c, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            found.append(c)
            say(OK, f"{c.relative_to(ROOT) if ROOT in c.parents else c}  "
                    f"{c.stat().st_size/1048576:.1f} MB  sha256={h.hexdigest()[:16]}")
    if not found:
        say(WARN, "一个权重都没找到 —— 球/筐检测会退回色块/模板路径，真视频准确率显著下降")
        warnings.append("没有模型权重")
    else:
        names = {p.name for p in found}
        if "best.pt" not in names:
            say(WARN, "没有放到 runs/detect/{ball,rim}/weights/best.pt —— "
                      "自动挑选权重时会找不到（也可用 AIHOOP_MODELS 指目录）")


def check_gpu() -> None:
    print("\n=== 6. GPU ===")
    try:
        import torch
        if torch.cuda.is_available():
            say(OK, f"CUDA 可用：{torch.cuda.get_device_name(0)}  "
                    f"torch {torch.__version__}")
        else:
            say(WARN, f"没有可用 CUDA（torch {torch.__version__}）—— CPU 能跑但慢十几倍")
            warnings.append("无 GPU")
    except Exception as e:  # noqa: BLE001
        say(WARN, f"torch 不可用：{type(e).__name__}")


def check_ocr(video: str) -> None:
    print("\n=== 7. 比分牌 OCR 路径 ===")
    if os.name != "nt":
        say(WARN, "跳过：该路径仅 Windows 可用")
        return
    ps = shutil.which("powershell") or shutil.which("powershell.exe")
    if not ps:
        say(BAD, "找不到 powershell —— 比分牌 OCR 不可用")
        problems.append("缺少 powershell")
        return
    if not (ROOT / "scripts/ocr_batch.ps1").is_file():
        say(BAD, "scripts/ocr_batch.ps1 不存在")
        problems.append("缺 ocr_batch.ps1")
        return
    import subprocess
    code = ("[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]"
            " | Out-Null; "
            "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages"
            " | ForEach-Object { $_.LanguageTag }")
    r = subprocess.run([ps, "-NoProfile", "-Command", code],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    langs = [x.strip() for x in (r.stdout or "").split("\n") if x.strip()]
    if langs:
        say(OK, f"Windows OCR 可用，识别语言：{', '.join(langs)}")
    else:
        say(BAD, "Windows 上没有可用的 OCR 识别语言 —— 比分牌路径会失败"
                 "（Server SKU 常常如此，见部署方案 §1.1）")
        problems.append("Windows OCR 无可用语言")
    if video:
        say(OK, f"可用 --ocr-video 再试一次真实视频：{video}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocr-video", default="", help="额外试跑比分牌 OCR（给一段视频路径）")
    args = ap.parse_args()

    print("=" * 68)
    print("  aihoop 上线自检")
    print(f"  仓库：{ROOT}")
    print("=" * 68)
    check_runtime()
    check_env()
    check_dirs_and_disk()
    check_ffmpeg()
    check_weights()
    check_gpu()
    check_ocr(args.ocr_video)

    print("\n" + "=" * 68)
    print("  结论")
    print("=" * 68)
    if warnings:
        print(f"  警告 {len(warnings)} 条（不阻塞上线，但会降级）：")
        for w in warnings:
            print("    - " + w)
    if problems:
        print(f"  必须先解决 {len(problems)} 条：")
        for p in problems:
            print("    - " + p)
        return 1
    print("  关键项全部通过 —— 可以按 docs/部署方案.md 第 5 节跑上线自检（demo / 真视频 / 自检套件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
