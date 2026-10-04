"""球检测模型的重训管线 —— 提升广播远景下的召回率。

为什么需要
------------------------------------------------------------------
现成的权重（ShotTracker best.pt）在广播远景上召回只有 ~26%（450 帧只有
117 帧检测到球），而传球网络、控球归属、出手归属全都依赖球轨迹。
颜色线索更差：每帧 70 个橙色候选（球衣/木地板/皮肤），30 秒 6.2 万候选。

数据集
------------------------------------------------------------------
koppolusameer/basketball-coco-player-detection（HF，公开可下）
  * COCO 格式，类别含 ball / rim / player / referee / number + 若干动作类
  * **就是 NBA 广播画面**（凯尔特人/尼克斯等），与目标素材同域 —— 这是关键，
    拿广场野球场的球模型去跑广播远景同样会崩。
  * test 划分：169 图 / 151 个 ball / 163 个 rim

用法
------------------------------------------------------------------
    # 0) 先装 CUDA 版 torch（Blackwell 显卡需要 cu128）
    #    .venv\\Scripts\\python.exe -m pip install --proxy http://127.0.0.1:7890 \
    #        --index-url https://download.pytorch.org/whl/cu128 --upgrade torch torchvision

    python scripts/train_ball_detector.py download
    python scripts/train_ball_detector.py convert --classes ball,rim
    python scripts/train_ball_detector.py train --model yolo11n.pt --epochs 60
    python scripts/train_ball_detector.py eval --weights runs/.../best.pt

训练完把权重传给管线即可（其它什么都不用改）：
    VideoSource(..., ball_weights="runs/detect/ball/weights/best.pt")
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "koppolusameer/basketball-coco-player-detection"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"
RAW = ROOT / "data" / "ball_ds" / "raw"
OUT = ROOT / "data" / "ball_ds" / "yolo"
SPLITS = ("train", "valid", "test")


def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_dir(split: str) -> list[str]:
    """列出某个 split 下的文件名（用 HF 的树 API）。"""
    import urllib.parse
    url = ("https://huggingface.co/api/datasets/"
           + urllib.parse.quote(REPO) + "/tree/main/" + split)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        items = json.loads(r.read().decode())
    return [it["path"] for it in items if it.get("type") == "file"]


def cmd_download(args) -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    for split in SPLITS:
        d = RAW / split
        d.mkdir(parents=True, exist_ok=True)
        try:
            files = list_dir(split)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {split}: {e}")
            continue
        todo = [f for f in files if not (RAW / f).exists()]
        print(f"[{split}] 共 {len(files)} 个文件，需下载 {len(todo)} 个")

        def one(path: str):
            dst = RAW / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                dst.write_bytes(_get(f"{BASE}/{path}"))
                return True
            except Exception:  # noqa: BLE001
                return False

        ok = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for i, r in enumerate(ex.map(one, todo), 1):
                ok += 1 if r else 0
                if i % 200 == 0:
                    print(f"   {i}/{len(todo)}")
        print(f"[{split}] 下载完成 {ok}/{len(todo)}")
    print(f"[ok] 原始数据集：{RAW}")
    return 0


def cmd_convert(args) -> int:
    """COCO -> YOLO（只保留指定类别，默认 ball,rim —— 我们只关心球和篮筐）。"""
    keep = [c.strip() for c in args.classes.split(",") if c.strip()]
    for split in SPLITS:
        ann = RAW / split / "_annotations.coco.json"
        if not ann.exists():
            print(f"[skip] {ann} 不存在")
            continue
        coco = json.loads(ann.read_text(encoding="utf-8"))
        cats = {c["id"]: c["name"] for c in coco.get("categories", [])}
        keep_ids = {i: n for i, n in cats.items() if n in keep}
        idx = {i: k for k, i in enumerate(sorted(keep_ids))}
        imgs = {im["id"]: im for im in coco.get("images", [])}
        by_img: dict[int, list] = {}
        for a in coco.get("annotations", []):
            if a["category_id"] in keep_ids:
                by_img.setdefault(a["image_id"], []).append(a)
        out_img = OUT / split / "images"
        out_lbl = OUT / split / "labels"
        out_img.mkdir(parents=True, exist_ok=True)
        out_lbl.mkdir(parents=True, exist_ok=True)
        n_img = n_box = 0
        for iid, anns in by_img.items():
            im = imgs.get(iid)
            if not im:
                continue
            src = RAW / split / im["file_name"]
            if not src.exists():
                continue
            stem = Path(im["file_name"]).stem
            (out_img / f"{stem}.jpg").write_bytes(src.read_bytes())
            W, H = float(im["width"]), float(im["height"])
            lines = []
            for a in anns:
                x, y, w, h = a["bbox"]
                cx, cy = (x + w / 2) / W, (y + h / 2) / H
                lines.append(f"{idx[a['category_id']]} {cx:.6f} {cy:.6f} "
                             f"{w / W:.6f} {h / H:.6f}")
                n_box += 1
            (out_lbl / f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8")
            n_img += 1
        print(f"[{split}] {n_img} 图 / {n_box} 框 -> {out_img}")
    yml = OUT / "data.yaml"
    yml.write_text(
        "path: %s\ntrain: train/images\nval: valid/images\ntest: test/images\n"
        "names:\n" % OUT.as_posix()
        + "".join(f"  {i}: {n}\n" for i, n in enumerate(keep)),
        encoding="utf-8")
    print(f"[ok] YOLO 数据集：{yml}")
    return 0


def cmd_train(args) -> int:
    from ultralytics import YOLO
    yml = Path(args.data) if args.data else (OUT / "data.yaml")
    if not yml.exists():
        print(f"[err] 数据集不存在：{yml}")
        return 2
    model = YOLO(args.model)
    model.train(data=str(yml), epochs=args.epochs, imgsz=args.imgsz,
                batch=args.batch, device=args.device, name=args.name,
                project=str(ROOT / "runs" / "detect"),
                # 小目标（球只有 8~20px）是核心难点：放大输入 + 关掉马赛克增强
                mosaic=0.0, scale=0.5, patience=20, exist_ok=True,
                hsv_v=0.5, hsv_s=0.5, fliplr=0.5, plots=False, workers=args.workers)
    print(f"[ok] 训练完成，权重在 runs/detect/{args.name}/weights/best.pt")
    return 0


def cmd_eval(args) -> int:
    """在本工程的真实素材上量**召回**（这才是我们关心的指标）。"""
    import cv2
    from ultralytics import YOLO
    m = YOLO(args.weights)
    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    tot = hit = 0
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % args.stride == 0:
            ok, fr = cap.retrieve()
            if not ok:
                break
            r = m.predict(fr, conf=args.conf, imgsz=args.imgsz,
                          device=args.device, verbose=False)[0]
            tot += 1
            if r.boxes is not None and len(r.boxes):
                names = [str(r.names[int(b.cls[0])]).lower() for b in r.boxes]
                if any("ball" in n for n in names):
                    hit += 1
        idx += 1
    cap.release()
    rec = 100.0 * hit / max(1, tot)
    print(f"采样 {tot} 帧，检出球的帧 {hit}  -> 帧级召回 {rec:.1f}%")
    print("对照基线：ShotTracker best.pt 在同一段视频上是 26%（117/450 帧）")
    print("结论：" + ("✅ 有明显提升，可以接进管线" if rec >= 40 else
                     "⚠️ 提升有限 —— 需要补标注或改用切块推理"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重训篮球检测模型（提升远景召回）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="从 HuggingFace 拉取数据集")
    d.add_argument("--workers", type=int, default=8)
    d.set_defaults(func=cmd_download)

    c = sub.add_parser("convert", help="COCO -> YOLO")
    c.add_argument("--classes", default="ball,rim")
    c.set_defaults(func=cmd_convert)

    t = sub.add_parser("train", help="训练")
    t.add_argument("--model", default="yolo11n.pt")
    t.add_argument("--epochs", type=int, default=60)
    # ⚠️ 尺度匹配：训练图是 576x576 的裁剪块，球中位 17px。
    #    ultralytics 会把输入缩放到 imgsz，所以要看**缩放后的绝对像素大小**：
    #      训练 imgsz=640 -> 576 放大到 640(1.11x) -> 球约 19px
    #      推理 imgsz=1280 -> 1280 宽的整帧按 1.0x -> 球 8~20px
    #    两者对得上。若训练用 960（球被放大到 28px），推理时只有 8~20px，
    #    尺度差 1.5~3 倍，小目标召回会明显掉。
    t.add_argument("--imgsz", type=int, default=640,
                   help="训练输入（要与推理时的球绝对像素大小匹配，见注释）")
    t.add_argument("--batch", type=int, default=16)
    t.add_argument("--device", default="0")
    # Windows 上 8 个 worker 并发写 labels.cache 会被瞬时拒绝
    # （WinError 5），单进程最稳，代价只是数据加载慢一点
    t.add_argument("--workers", type=int, default=0)
    # 用同一套训练管线训别的数据集（比如 scripts/label_rim.py 生成的篮筐集）
    t.add_argument("--data", default="", help="数据集 yaml；不给就用球的数据集")
    t.add_argument("--name", default="ball", help="run 名字")
    t.set_defaults(func=cmd_train)

    e = sub.add_parser("eval", help="在真实素材上量帧级召回")
    e.add_argument("--weights", required=True)
    e.add_argument("--video", default="out/_game/ball_test.mp4")
    e.add_argument("--stride", type=int, default=5)
    e.add_argument("--conf", type=float, default=0.25)
    # 推理用整帧原生分辨率：1280 宽的视频缩放 1.0x -> 球就是 8~20px，
    # 与训练时（640/576*17≈19px）同一量级。
    e.add_argument("--imgsz", type=int, default=1280)
    e.add_argument("--device", default="0")
    e.set_defaults(func=cmd_eval)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())