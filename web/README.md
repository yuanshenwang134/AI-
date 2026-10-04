# AI 篮球分析软件 · 前端

纯静态单页应用，**无 npm / 无 Vite / 无构建步骤**：一个 `index.html` + 若干普通 `<script>`，
用 CDN 引入 Vue 3、Element Plus、ECharts，双击即可打开；连不上后端时自动切到
`web/demo/` 下的静态产物，并以「演示数据模式」标注。

---

## 1. 目录结构

```
web/
├── index.html             SPA 外壳：深色顶栏 + 左侧菜单 + hash 路由 + 演示模式提示条
├── styles.css             全局样式（深顶栏 / 浅内容区 / 卡片 / 时间轴 / 复核行 / 球场容器）
├── api.js                 后端 fetch 封装 + WebSocket 进度订阅 + 演示数据降级（window.API）
├── data.js                数值/时间格式化、数据规范化、分区与网格口径、CSV 导出、Markdown 渲染（window.D）
├── court.js               标准 FIBA 半场/全场绘制 + 出手散点/分区热力/网格热力图层（window.Court）
├── components.js          公共组件：echarts-box / court-view / stat-compare / big-scoreboard
├── app.js                 全局状态（window.STORE）+ 数据装载 + 根组件 + Element Plus 中文初始化
├── pages/
│   ├── upload.js          ① 上传与分析配置
│   ├── overview.js        ② 比赛总览
│   ├── stats.js           ③ 球员/球队统计
│   ├── shotchart.js       ④ 投篮热区（重点页面）
│   ├── tactics.js         ⑤ 战术分析（**核心页**：俯视战术图 /
│   │                          传球网络 / 阵型识别 / 间距曲线）
│   ├── highlights.js      ⑥ 高光集锦
│   ├── report.js          ⑦ 导出/报告
│   └── review.js          ⑧ 人工复核（自研加分点）
└── demo/                  演示数据（game.json / players.json / shotchart.json /
                           report.json / report.md / highlights.json /
                           tactics.json / tactics_frames.json）
```

页面组件统一以 `window.PAGES['overview'] = {...}` 的形式注册，`index.html` 里用
`location.hash`（`#/overview`）切换，没有引入 vue-router。

---

## 2. 如何本地打开

### 方式 A：静态托管（推荐，功能最完整）

```bash
cd aihoopanalyst
python -m http.server 8080
# 浏览器打开 http://127.0.0.1:8080/web/
```

用 `http.server` 打开时，浏览器允许读取同目录下的 `web/demo/*.json`，
「演示数据模式」可以完整跑通。

### 方式 B：直接双击 `web/index.html`（`file://`）

页面本身能打开，但 **Chrome / Edge 默认拦截 `file://` 下的 fetch**，
因此 `web/demo/*.json` 读不出来（页面顶部会明确提示原因与解决办法）。
若确实要用 `file://`：

* Firefox：`about:config` 把 `privacy.file_unique_origin` 设为 `false`；
* 或 Chrome 启动参数 `--allow-file-access-from-files`；
* 或直接用方式 A / 方式 C。CDN 资源（Vue/ECharts）仍需要联网。

### 方式 C：连着真实后端一起用

1. 启动后端（默认 `http://127.0.0.1:8000`）；
2. 打开前端（方式 A 或 B 都可以）；
3. 顶栏标签变成 **「后端已连接」**，此时：
   * 新建任务 → 真实跑管线并 WebSocket 实时显示进度；
   * 导出 → 走 `/api/games/{id}/export`；
   * 复核改判 → 走 `POST /api/games/{id}/shots/{index}/correct`，真正写回后端。

后端不在 `127.0.0.1:8000` 时，用查询参数指定：

```
http://127.0.0.1:8080/web/?api=http://192.168.1.20:8000
```

> 跨机访问需要在后端开启 CORS（允许前端所在 origin）。

---

## 3. 各页面 ↔ 后端接口对应关系

| 页面（hash 路由） | 用到的接口 | 说明 |
|---|---|---|
| 上传与分析 `#/upload` | `POST /api/jobs`、`WS /api/jobs/{id}/ws`、`GET /api/jobs/{id}`、`GET /api/jobs`、`POST /api/upload`、`GET /api/health` | 建任务 → WebSocket 推进度（失败自动降级 1.2s 轮询）→ 完成后装载产物；任务列表可直接回看历史；`/api/upload` 支持浏览器直接传视频（`x-filename` 头 + 原始 body），返回的 `path` 直接填进 `video_path`；`/api/health` 显示 ffmpeg / OpenCV / 检测模型是否就绪 |
| 比赛总览 `#/overview` | `GET /api/games/{id}`、`GET /api/games/{id}/video` | 大比分牌 / 分节比分 / 命中率·三分率卡片 / `progression` 走势折线 + 分差 / `timeline` 事件时间轴；点事件跳视频 `currentTime` |
| 球员/球队统计 `#/stats` | `GET /api/games/{id}/players`、`GET /api/games/{id}` | 得分榜、可排序统计表（eFG% / TS%）、点行展开个人出手散点、两人雷达图对比 |
| 投篮热区 `#/shotchart` | `GET /api/games/{id}/shotchart`、`GET /api/games/{id}` | 标准半场 + 出手散点 / 分区热力 / 网格热力；主客队切换、球员筛选、命中率↔出手量、半场镜像↔全场原始、高效区/低效区结论、距离衰减图 |
| 高光集锦 `#/highlights` | `GET /api/games/{id}/highlights`、`GET /api/media/{id}/{path}`、`GET /api/games/{id}/video` | 片段排序（精彩度/得分/三分/时间）、在线播放、逐个下载；无片段文件时给原始视频跳转 + ffmpeg 命令 |
| 导出/报告 `#/report` | `GET /api/games/{id}/report`、`GET /api/games/{id}/export?fmt=…`、`POST /api/games/{id}/report/llm` | Markdown 战报轻量渲染 + 可信度/关键球/高效区/领先球员；下载按钮：`csv_stats` `csv_shots` `json` `report_md` `events`（外加两个产物原始文件 stats.csv / shots.csv）；「LLM 润色（可选）」调 `/report/llm`，无 API Key 时后端返回 400，界面按提示回退模板战报 |
| 人工复核 `#/review` | `GET /api/games/{id}`（`needs_review`）、`POST /api/games/{id}/shots/{index}/correct` | 低置信度出手逐条改判（命中/未中 + 1/2/3 分），提交后本地立即重算比分、球队/球员统计与热区；后端返回的 `summary` / `score` 会显示在改判记录里 |
| 战术分析 `#/tactics` | `GET /api/games/{id}/tactics`、`GET /api/games/{id}/tactics/frames`、`GET /api/games/{id}` | **战术层**：俯视战术图播放器（两层同 viewBox 的 SVG 叠加，带轨迹尾迹 / 进度条 / 倍速）、传球网络（画在半场上，节点=平均站位、边宽=传球次数）、阵型识别时间轴（点段跳转）、间距曲线（5 档指标可切）。数据懒加载：只有打开这一页才拉 `tactics.json` 与 `tactics_frames.json` |

**`correct` 的 `index` 口径（务必对齐）**：后端 `api.correct_shot` 把 `index` 当作
`game["timeline"]` 的下标（按时间排序后的出手序号），并且会累积写入 `corrections.json`、
整条管线重算后返回 `{ok, summary, score, corrections, needs_review_left}`。
前端因此：

* 复核行上的 `序号` 就是 `timeline` 下标；若后端产物里没显式给 `index`，
  前端用 `(t, player_id, value)` 在 `timeline` 中反查得到；
* 后端返回 `score` 时在提示条里回显，用于确认「改判真的写回了统计口径」；
* 后端返回 `needs_review_left` 表示重算后仍低置信度的条数。

> 后端还提供了两个本前端暂未依赖的可选接口：
> `GET /api/games/{id}/review`（复核队列 + 阈值 + 历史改判）与
> `POST /api/games/{id}/report/llm`（已在报告页接入）。

---

## 4. 无后端时的降级处理（演示数据模式）

后端不可用（没启动 / 跨域被拦 / `file://` 限制）时：

1. **自动探测**：`GET /api/jobs` 2.5s 超时探测，失败即进入演示数据模式；
2. **自动加载** `web/demo/` 下的 `game.json`、`players.json`、`shotchart.json`、
   `report.json`、`report.md`、`highlights.json`；
3. **缺件自愈**：缺 `players.json` 时用 `game.timeline` 现算点集，缺 `shotchart.json`
   时前端按后端同一口径补 `zones`（`zone_of`）与 `grid`（1m 网格、镜像到同一半场）；
4. **顶栏 + 提示条**明确标注「演示数据模式」，侧栏比赛卡片标注「来源：演示数据」；
5. **写操作本地化**：
   * 导出 → 浏览器内用同一套统计口径现场生成 CSV / JSON / Markdown（CSV 带 UTF-8 BOM，Excel 不乱码）；
   * 复核改判 → 只做本地重算（页面顶部标注「演示模式：仅本地改判」），并在改判记录里区分「已写回 / 仅本地」；
6. **视频缺失**：高光页与总览页自动隐藏播放器，改为说明 + ffmpeg 切片命令。

`web/demo/` 里的数据**优先由后端 demo 产出**（`game.json / players.json / shotchart.json /
report.md`）。仓库里已附一份结构一致的合成样本，直接打开就有效果；后端跑通后覆盖同名文件即可。

---

## 5. 坐标与口径（与后端严格一致）

**数据坐标（全场唯一约定，不要颠倒）：**

* 单位米，原点在球场中心：**x = 横向（球场宽度）** `x ∈ [-7.5, 7.5]`；
  **y = 纵向（球场长度）** `y ∈ [-14, 14]`；
* x=0 是中线，|x| 越大越贴边线；y=0 是中场，|y| 越大越靠近底线；
* **篮筐在 `(0, ±1.575)`** —— 在 y 轴上（篮筐圆心距底线 1.575m，是**纵向**偏移）；
* 攻哪一侧由 **y 的符号**决定：y<0 攻左篮筐，y>0 攻右篮筐；
* 三分弧半径 **6.75m**（圆心=被进攻的篮筐）；底角三分直线距边线 **0.90m**，
  判据是 **`|x| ≥ 6.60`（横向！）**；
* 距离分档：< 4.0m 禁区；< 5.8m 近距离中投；其余（三分线内）长两分；
* 罚球区 **4.90m × 5.80m**；罚球圈半径 **1.80m**；合理冲撞区半径 **1.25m**；篮板距底线 **1.20m**；
* 六分区中文名与后端 `rules.zone_of` 完全一致：禁区 / 近距离中投 / 长两分 / 底角三分 / 45°三分 / 弧顶三分。

**画布坐标（`court.js` 的 SVG，单位米）：**

* 半场视图：`u ∈ [0,15]`（横向，从左边线量起）、`v ∈ [0,14]`（纵向，v=0 是底线、v=14 是中场线），
  所以**篮筐画在 `(7.5, 1.575)`**；
* `mapXY` 把数据坐标映射到画布：半场视图 `u = |x|`、`v = |y|`（左右半场镜像到同一侧）；
  全场视图 `u = x + 7.5`、`v = y + 14`，保留原始左右分布 —— 这是热区分析的标准做法，也是答辩讲解点。

> ⚠️ **这里曾经有一个真实的轴对调缺陷（已修，请看 `tests/test_plan_a.py::test_axis_frame_consistency`）。**
> 旧版计分引擎把 x 当纵向、y 当横向，而网格与前端把 x 当横向、y 当纵向，
> 导致底角三分被压到网格最后一行、标定角点喂给引擎时上篮被判成三分。
> 现在前后端统一为上面这套约定，并有两道自动校验：
> `web/_validate.js` 用 **0.25m 网格扫描**逐点比对前端 JS 与后端 Python 的分区口径，
> `web/_test.js` 再按 demo 数据逐点比对一次（当前均为 0 处不一致）。
> **如果你改动了分区口径，先跑这两个脚本。**

---


### 俯视战术图的坐标

战术图**只画一张半场**：后端把两侧进攻折叠成「局部进攻坐标」(x, d=|y|)，
被进攻的篮筐永远是 (0, 1.575)，所以左右半场自动叠在一起、两队可以直接比。

前端映射在 `court.js`：`Court.mapTactics(x, y)` → `{u: x + 7.5 + pad, v: |y| + pad}`，
与 `Court.courtSVG({view:'half'})` 的 viewBox **完全一致**，
因此「球场层」和「球员层」两张 SVG 只要容器尺寸相同就是像素级对齐的
（`web/_test.js` 第 [5] 组会断言篮筐落点）。

## 6. 离线演示依赖（`web/vendor/`）

`index.html` 加载依赖的策略是 **本地优先、CDN 兜底**：

* 仓库已提交 `web/vendor/`（Vue / Element Plus / ECharts，约 2.5MB），因此**断网也能打开演示**；
* 若 `web/vendor/` 缺失，自动回退到 unpkg / jsdelivr；
* 页面运行时会把当前模式写在 `window.__DSH_ASSET_MODE`（值为 `local` 或 `cdn`），
  可在浏览器控制台查看确认；
* 重新抓取或换版本：

```bash
python scripts/vendor_web.py            # 下载到 web/vendor/
python scripts/vendor_web.py --check    # 只检查本地是否齐全
```

---

## 7. 开发期自检脚本（可选，不影响运行）

这三个脚本只在开发时使用，不参与前端运行：

```bash
node web/_validate.js     # 模板结构 + computed/methods + 球场几何 + 分区口径 + Markdown 渲染自检
node web/_test.js         # 功能测试：分节和=总分、改判写回口径、镜像、CSV 导出
node web/_gen_demo.js     # （可选）用前端自带的合成器生成一份 demo 数据
```

> 建议：**demo 数据以后端的产物为准**（`python -m aihoop.cli demo` + `scripts/sync_demo.ps1`），
> `_gen_demo.js` 只是前端独立开发时的备用数据源。

最近一次运行结果：`_validate.js` 全部通过（分区口径半场 0.25m 网格扫描 0 处不一致、
Markdown 渲染正常）；
`_test.js` 全部通过（改判后比分同步更新、分节之和仍等于总分、球员统计与三分命中数同步更新、
左右半场镜像一致、本地 CSV 导出可用）。

> 分区口径的判定顺序：后端 `rules.zone_of` 是**先判三分（弧半径 6.75m 或横向 `|x| ≥ 6.60`），
> 再细分三分区（底角 / 45° / 弧顶）；不是三分时按到篮筐距离分档（<4.0 禁区、<5.8 近距离中投、其余长两分）**。
> 前端 `data.js` 的 `zoneOf` 与后端逐字对齐，`_validate.js` 用一份独立参考实现做半场 0.25m 网格交叉校验。
> 改口径时**一定**先跑 `_validate.js` 与 `tests/test_plan_a.py`。

> 离线演示已开箱可用：`web/vendor/` 已随仓库提交（见 §6），`index.html` 会优先加载本地副本，
> 所以**没有外网也能正常打开**。若目录缺失才会回退 CDN。
