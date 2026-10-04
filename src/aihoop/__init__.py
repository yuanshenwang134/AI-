"""AI 篮球分析软件 · 套餐 A（稳妥）实现骨架。

一句话：上传篮球比赛视频 -> 自动生成比分/命中率/球员统计/投篮热区/高光集锦/战报。

模块地图：
  model     数据模型与 FIBA 尺寸常量（Shot / Event / Player / 事件流契约）
  rules     计分规则引擎（自研核心：1/2/3 分判定、命中事件融合、统计、热区）
  court     球场标定：4 角点手标 或 关键点模型 -> 单应矩阵 -> 真实坐标
  sources   数据源适配（合成 / JSONL 回放 / YOLOv8+ByteTrack 真视频）
  pipeline  分析管线：把检测结果变成所有产物
  report    战报生成（模板离线版 + 可选 LLM 版）
  export    CSV / JSON 导出
  highlight 高光片段（ffmpeg，缺 ffmpeg 时降级为时间码）
  api       FastAPI 服务
  cli       命令行入口
"""

__version__ = "0.1.0"
