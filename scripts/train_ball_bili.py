"""训练**这段素材专用的球检测器**（在筐口放大图上）。

为什么必须自己训：
  * 现成权重（runs/detect/ball）在这段素材上篮筐 150px 内**一帧都没检出**；
  * 手工特征（运动块、橙色圆块）全是噪声；
  * 球只有 7~15px 且运动模糊 —— 只有在本域标注数据上训才能救。

输入：scripts/label_ball.py 的产出
  data/ball_ds/frames/*.png   放大 2 倍的筐口裁剪图（800x800）
  data/ball_ds/labels/*.txt   YOLO 单类（0=ball），空文件 = 明确没有球

关键做法：
  1. **按时间切分** train/val，不能随机分 —— 相邻帧几乎一样，随机分会严重泄漏；
  2. 从 yolov8n 预训练权重微调（小数据必须微调）；
  3. 报 mAP50 / mAP50-95 / P / R，并**留一份预测可视化**便于人工核对。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "data" / "ball_ds"
YOLO_DS = ROOT / "data" / "ball_yolo"


def build_yolo_ds(val_frac: float = 0.25):
    state = {}
    for line in (DS / "labeled.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            state[d["file"]] = d
    items = []
    for f, st in state.items():
        img = DS / f
        lab = DS / "labels" / (Path(f).stem + ".txt")
        if not img.exists() or not lab.exists() or st.get("status") == "skip":
            continue
        items.append({"img": img, "lab": lab, "t": float(st.get("t", 0.0)),
                      "status": st.get("status")})
    items.sort(key=lambda x: x["t"])
    n_val = max(1, int(len(items) * val_frac))
    val, tr = items[-n_val:], items[:-n_val]
    if YOLO_DS.exists():
        shutil.rmtree(YOLO_DS)
    for split, group in (("train", tr), ("val", val)):
        (YOLO_DS / split / "images").mkdir(parents=True, exist_ok=True)
        (YOLO_DS / split / "labels").mkdir(parents=True, exist_ok=True)
        for it in group:
            shutil.copy(str(it["img"]),
                        str(YOLO_DS / split / "images" / it["img"].name))
            shutil.copy(str(it["lab"]),
                        str(YOLO_DS / split / "labels" / it["lab"].name))
    (YOLO_DS / "data.yaml").write_text(
        f"path: {YOLO_DS.as_posix()}\ntrain: train/images\nval: val/images\n"
        "names:\n  0: ball\n", encoding="utf-8")
    n_pos_tr = sum(1 for i in tr if i["status"] == "ball")
    n_pos_va = sum(1 for i in val if i["status"] == "ball")
    print(f"数据集：训练 {len(tr)}（有球 {n_pos_tr}）/ 验证 {len(val)}（有球 {n_pos_va}）")
    return len(tr), len(val), n_pos_tr


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="训练这段素材专用的球检测器")
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="yolov8n.pt")
    ap.add_argument("--val-frac", type=float, default=0.25)
    a = ap.parse_args(argv)

    if not (DS / "labeled.jsonl").exists():
        print("[err] 还没有标注。先跑：python scripts\\label_ball.py")
        return 2
    n_tr, n_va, n_pos = build_yolo_ds(a.val_frac)
    if n_pos < 30:
        print(f"[warn] 训练集只有 {n_pos} 张有球 —— 太少，结果不可靠（建议 ≥100）")

    from ultralytics import YOLO
    model = YOLO(a.model)
    model.train(data=str(YOLO_DS / "data.yaml"), epochs=a.epochs,
                imgsz=a.imgsz, batch=a.batch,
                project=str(ROOT / "runs/detect"), name="ball_bili",
                exist_ok=True, verbose=False)
    m = model.val(data=str(YOLO_DS / "data.yaml"), imgsz=a.imgsz,
                  project=str(ROOT / "runs/detect"), name="ball_bili_val",
                  exist_ok=True, verbose=False)
    metrics = {
        "n_train": n_tr, "n_val": n_va, "n_pos_train": n_pos,
        "epochs": a.epochs, "imgsz": a.imgsz,
        "mAP50": float(getattr(m.box, "map50", 0.0)),
        "mAP50_95": float(getattr(m.box, "map", 0.0)),
        "precision": float(getattr(m.box, "mp", 0.0)),
        "recall": float(getattr(m.box, "mr", 0.0)),
    }
    print("\n=== 验证结果 ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    w = ROOT / "runs/detect/ball_bili/weights/best.pt"
    print(f"\n权重 {w}  存在={w.exists()}")
    (ROOT / "out" / "ball_bili_train.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print("指标已存 out/ball_bili_train.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
