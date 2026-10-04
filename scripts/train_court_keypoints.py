"""训练球场关键点模型（YOLO-pose）→ 用来替代"手动点标定"。

数据（Roboflow 导出，YOLOv8 格式）：
    data/calib_ds/
      data.yaml                     类别 + kpt_shape + kpt_names
      train/images/*.jpg  train/labels/*.txt     YOLO-pose 标注
      valid/...  test/...

为什么要自己训：
  现成的 `third_party/court_kp/basketball_court_kp_yolo11n.pt` 在**别的机位**上训的，
  实测在用户素材上 48 个关键点大半落在观众席 —— 换机位就失效。
  用公开数据集训一个基础模型，再用你自己机位的几帧微调，才能在你的视频上稳。

用法：
    # 1) 先看数据集里到底有哪些关键点（决定怎么映射到球场坐标）
    python scripts\\train_court_keypoints.py --ds data/calib_ds --inspect

    # 2) 训练
    python scripts\\train_court_keypoints.py --ds data/calib_ds --epochs 100

    # 3) 用你自己机位的几帧微调（labels 由标注工具产出）
    python scripts\\train_court_keypoints.py --ds data/calib_ds `
        --finetune runs/pose/court_kp/weights/best.pt --epochs 40
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_yaml(ds: Path) -> Path | None:
    for name in ("data.yaml", "dataset.yaml", "data.yml"):
        p = ds / name
        if p.exists():
            return p
    hits = list(ds.rglob("data.yaml"))
    return hits[0] if hits else None


def inspect(ds: Path) -> int:
    y = find_yaml(ds)
    if not y:
        print(f"[err] 在 {ds} 里找不到 data.yaml —— Roboflow 导出时选 YOLOv8 格式才有")
        return 2
    text = y.read_text(encoding="utf-8", errors="replace")
    print(f"=== {y} ===")
    print(text[:1200])
    # 关键点信息
    import re
    m = re.search(r"kpt_shape:\s*\[?\s*(\d+)\s*,\s*(\d+)", text)
    if m:
        print(f"\n关键点数量：{m.group(1)}（每个 {m.group(2)} 个值）")
    names = re.search(r"kpt_names:\s*(\[.*?\])", text, re.S)
    if names:
        print(f"关键点名称：{names.group(1)[:600]}")
    else:
        print("data.yaml 里没有 kpt_names —— 需要从数据集页面看关键点定义，"
              "或按顺序自行映射（见 auto_calibrate.py --map）")
    # 统计标注
    for split in ("train", "valid", "test"):
        imgs = list((ds / split / "images").glob("*")) if (ds / split).exists() else []
        lbls = list((ds / split / "labels").glob("*.txt")) if (ds / split).exists() else []
        if imgs or lbls:
            print(f"  {split}: 图 {len(imgs)}  标注 {len(lbls)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="训练球场关键点模型")
    ap.add_argument("--ds", default="data/calib_ds")
    ap.add_argument("--inspect", action="store_true", help="只看数据集信息，不训练")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="yolo11n-pose.pt")
    ap.add_argument("--finetune", default=None,
                    help="在已有权重上微调（把你自己机位的几帧加进数据集后用）")
    ap.add_argument("--name", default="court_kp")
    a = ap.parse_args(argv)

    ds = Path(a.ds)
    if a.inspect or not ds.exists():
        return inspect(ds)

    y = find_yaml(ds)
    if not y:
        print(f"[err] 找不到 data.yaml（{ds}）")
        return 2
    from ultralytics import YOLO
    base = a.finetune or a.model
    print(f"用 {base} 开始训练（{a.epochs} 轮，imgsz={a.imgsz}）…")
    model = YOLO(base)
    # workers=0：ultralytics 默认用多进程 DataLoader（走命名管道），
    # 在受限沙箱里会 PermissionError: [WinError 5]。单进程加载即可绕开。
    model.train(data=str(y), epochs=a.epochs, imgsz=a.imgsz, batch=a.batch,
                project=str(ROOT / "runs" / "pose"), name=a.name, exist_ok=True,
                verbose=False, workers=0)
    m = model.val(data=str(y), imgsz=a.imgsz, project=str(ROOT / "runs" / "pose"),
                  name=a.name + "_val", exist_ok=True, verbose=False)
    out = ROOT / "runs" / "pose" / a.name / "weights" / "best.pt"
    metrics = {"epochs": a.epochs, "base": base,
               "pose_mAP50": float(getattr(m, "pose", None).map50)
               if getattr(m, "pose", None) is not None else None,
               "box_mAP50": float(getattr(m.box, "map50", 0.0)),
               "weights": str(out), "weights_exists": out.exists()}
    print("\n=== 验证 ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    (ROOT / "out" / "court_kp_train.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print("指标已存 out/court_kp_train.json")
    print("\n下一步：python scripts\\auto_calibrate.py --video <你的视频> "
          f"--weights {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
