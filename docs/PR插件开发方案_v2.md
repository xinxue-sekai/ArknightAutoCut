# NewCut · PR 插件开发方案 v2（直接编辑版）

> 对 `final/PR插件开发方案.md`（v1，导出-替换模式）的修订。
> 触发修订的需求变更：**插件要直接在 PR 时间轴里对素材进行切割、标记、一键删除暂停帧**（与原程序体验一致），而不是"导出精剪 mp4 再导入"。
> 本文基于对官方 `@adobe/premierepro` 26.3.0 类型定义和官方示例仓库的逐条核对编写，API 结论均已核实。

---

## 0. 需求变更带来的方案翻转

| | v1 方案（原方案） | v2 方案（本文） |
|---|---|---|
| 交付物形态 | 后端导出精剪 mp4 → 导入 PR | **直接在 PR 序列中切割/删除/标记** |
| 核心 PR 操作 | `importFiles` + `insertIntoSequence` | **SequenceEditor 事务化编辑**（设入出点+覆写重建=切割删除）+ 标记 + 程序化选中 |
| "导出-替换"地位 | v1 主路线 | 降级为兜底备选 |
| "DOM 编辑"地位 | v2（被推迟，标注高风险） | **v1 主路线**（API 已具备，风险大幅低于原方案预估） |

原方案把 DOM 编辑推到 v2 的理由是"ExtendScript 拆段风险高"。这个前提在 2026 年已不成立：PR 2026 的 UXP API 提供了**带事务与撤销支持的声明式编辑 Action 体系**，比 ExtendScript 时代的裸 DOM 操作稳得多。

## 1. 平台调研结论（2026-09，已核实）

### 1.1 平台现状

- **CEP 正在退场**：Adobe 已确认 CEP 将停用；PR 2026 已出现 CEP 插件无法加载的案例（auto-subs #571）。PR ≤2025 仍是 CEP 唯一选择。
- **UXP 已转正**：PR 2026 (26.x) UXP 正式版；官方示例 manifest 显示 `minVersion: 25.1.0` 起 UXP 面板即可加载 → UXP 覆盖 25.1+ 全部现代版本。
- **Hybrid UXP**（C++ 原生模块）已可用，但**进程 spawn 能力仍未确认**——本方案不依赖它。

### 1.2 PR 2026 UXP 编辑 API 实测级核对（来自官方 .d.ts v26.3.0）

**有的（本方案全部用上）：**

| 能力 | API | 备注 |
|------|-----|------|
| 新建序列（等效复制） | `Project.createSequenceFromMedia(name, [ClipProjectItem])` | 从源素材建工作序列，全片自动落在 V1/A1 |
| 删除剪辑（含波纹） | `SequenceEditor.createRemoveItemsAction(selection, ripple, mediaType)` | 事务内执行 |
| 覆写放置素材 | `SequenceEditor.createOverwriteItemAction(projectItem, time, vIdx, aIdx)` | 用素材当前入出点放置 |
| **设素材入出点** | `ProjectItem.createSetInOutPointsAction(in, out)` | 与覆写配合 = 按任意源区间放置 = **等价切割** |
| 修剪时间轴剪辑 | `TrackItem.createSetInPointAction / createSetOutPointAction`（源相对） | 备用策略 |
| 事务 + 一步撤销 | `project.lockedAccess(() => project.executeTransaction(cb, undoName))` | 整个重建一个撤销步 |
| 程序化选中 | `TrackItemSelection.createEmptySelection/addItem` + `Sequence.setSelection` | 倍速段自动选中 |
| 序列标记 | `Markers.getMarkers(seq)` + `createAddMarkerAction(name, type, start, duration, comments)` | 倍速段/事件标记 |
| 剪辑颜色标签 | `TrackItem.createSetColorLabelAction(index)` | 预留 v2 |
| 媒体路径 | `TrackItem.getProjectItem()` → `ProjectItem.getMediaFilePath()` | 关联分析与素材 |
| 时间 | `TickTime.createWithSeconds()` / `.seconds`（只读属性） | 帧↔秒换算 |

**没有的（影响与对策）：**

| 缺口 | 影响 | 对策 |
|------|------|------|
| 无 razor（刀工具级切割 API，仅有切割吸附事件） | 不能直接"刀切"现有剪辑 | **不入出点+覆写重建**：删除区间=不放置，保留区间=依次放置。效果与"刀切+波纹删除"完全一致，且一次事务天然原子 |
| 无 setSpeed（`getSpeed` 只读） | 1x→2x、0.2x→10x 不能 API 调速 | v1：倍速段**打标记 + 自动选中**，用户 Ctrl+R 输入一次速度；v2 评估 ffmpeg 重编码该区间后替换 |

**关键机制：为什么"重建"等价于"切割+删除"**

原程序输出 = 保留帧区间拼接。PR 重建 = 每个保留区间 `[fs, fe)` 设源素材入出点为 `[fs/fps, fe/fps)`、按累计时长覆写放置到新序列 V1。像素、音频、顺序与"刀切+波纹删"逐帧一致；且所有段在一个事务里，Ctrl+Z 一步全撤。

## 2. 总体架构（v2）

```
┌────────────────── Premiere Pro (26.x, UXP) ──────────────────┐
│  NewCut 面板 (HTML/JS)                                        │
│   ├─ 素材面板: 读取选中剪辑 → getMediaFilePath                 │
│   ├─ 结果视图: 保留/删除/倍速 色带 + 统计                      │
│   └─ 应用按钮 ──► bridge.js (PR DOM 桥)                       │
│        createSequenceFromMedia → executeTransaction:          │
│        清空 → [SetInOutPoints + Overwrite]×N → 标记 → 选中     │
└──────────────┬────────────────────────────────────────────────┘
               │ HTTP JSON (127.0.0.1:8765)
┌──────────────▼────────────────────────────────────────────────┐
│  newcut-service (Python, 自研引擎 nc_match/nc_segment/decode) │
│   /status /probe /analyze /job/{id} /rebuild /thumb /cancel   │
│   掩码NCC+区域全帧搜索分类 / 分段 / 暂停掩码 / 边界差分 /        │
│   转场删除 / 碎片合并 / GPU自适应解码（实测选型）                │
└───────────────────────────────────────────────────────────────┘
```

与 v1 方案的差异：
- 服务层砍掉 `/frame` 帧服务与面板内视频预览——**PR 自己的节目监视器就是预览**（用户需求"直接在预览轴中"），UI 只需展示"保留/删除色带"级别的方案概览。
- 导出模块从未实现——重建不需要导出文件。分析引擎为完全自研（nc_*），不含原项目代码，差异清单见《逆向实现规格.md》§2.3。
- 倍速段处理从"抽帧"改为"标记+原生调速"（见 §1.2 缺口对策）。

## 3. 关键设计决策

| DR# | 决策 | 理由 | 状态 |
|-----|------|------|------|
| DR6 | 目标平台 **UXP（PR 25.1+ / 26.x）** | CEP 正在退场且 26.x 已有加载故障；UXP 编辑 API 完整 | ✅ 已定（默认） |
| DR7 | 切割/删除用 **入出点+覆写重建**，不用刀切 | UXP 无 razor API；重建等价且原子 | ✅ 已定 |
| DR8 | **先复制再编辑**：新建 `<序列名>_newcut` 工作序列 | 用户序列永不被破坏；可反复重跑 | ✅ 默认（可议） |
| DR9 | 倍速段 **标记+选中，用户 Ctrl+R 调速** | UXP 无 setSpeed；原生调速等价于原程序抽帧效果 | ✅ 默认（v2 可做重编码自动替换） |
| DR10 | 分析引擎**自研**（规格驱动重写，不含原项目代码/资源） | 逆向其功能作为规格（见《逆向实现规格.md》）；更正其状态模型缺陷；模板包由校准工具从用户截图生成 | ✅ v0.2 起已实现 |
| DR11 | 面板不做视频预览 | PR 监视器即预览；砍掉帧服务降低复杂度 | ✅ 已定 |
| DR12 | 服务常驻：**登录自启（HKCU Run，pythonw 无窗口，惰性加载零空闲占用）** | UXP 无 spawn，无法由面板拉起；自启后等效"打开 PR 即可用" | ✅ 已实现（service/安装开机自启.bat） |

### 用户待确认项（不影响 M0/M1 开工）

1. 若你还需支持 **PR 2024 及更早**（纯 CEP 环境），需追加一个 CEP 壳（面板 UI 与 Python 服务完全复用，只重写桥接层为 ExtendScript，工作量约 +30%）。
2. 若要求**全自动调速**（不经 Ctrl+R），走"服务端 ffmpeg 对倍速区间 setpts 重编码 → 导入 → 覆写替换"路线，v2 再做。
3. "先复制再编辑"如不合意（希望直接改当前序列），改一行选项即可。

## 4. 数据流与帧↔时间映射

1. 面板从 PR 取选中剪辑 → `getMediaFilePath()` → POST `/probe` 得 fps/总帧数。
2. POST `/analyze`：服务端跑 `analyze_video_with_context → build_segments → build_delete_set`。
   - **注意与原项目导出流程的差异**：重建用保留区间在计算 `build_delete_set` 时**关闭抽帧**（`speedup_1x=False, speedup_02=False`）——抽帧交给 PR 原生速度，否则重建时间轴会卡帧。
3. 返回方案 JSON：`keep_ranges`（左闭右开帧区间）、`delete_ranges`、`pauses`（含 mode/boundary_diff/段内子区间）、`speeds`（含建议倍率）、时长统计。
4. 桥接层映射：原始帧 `f` → 重建时间轴秒 = 累计保留帧数/fps；倍速段在新时间轴上按此映射打标记、匹配剪辑并选中。

## 5. 里程碑（重排）

### M0 · PR 端验证（0.5 天，在用户机器上做）
- [ ] UDT（UXP Developer Tool）加载 `panel/`，面板在 PR 中出现
- [ ] `getActiveClipInfo`：选中剪辑 → 读到 mediaPath
- [ ] `createSequenceFromMedia` 建工作序列成功
- [ ] 单事务重建：SetInOutPoints + Overwrite 混合事务的**顺序执行语义**验证（核心风险点，失败则自动走逐段事务兜底，两者都验证）
- [ ] `createRemoveItemsAction(ripple=false)` 在空序列上的行为
- [ ] Markers / setSelection 可用性
- [ ] 产出《M0 验证记录》，更新 bridge.js 防御分支

### M1 · 最小闭环（已基本完成，见下）
- [x] Python 服务（/status /probe /analyze /job /rebuild /thumb /cancel，自研引擎）
- [x] UXP 面板（状态/取素材/分析/进度/色带/应用）
- [x] 桥接层（重建+标记+选中）
- [ ] M0 通过后的真机联调与修复

### M2 · 体验完整化（v0.3 已交付）
- [x] 暂停事件列表 + 单事件勾选排除（服务端缓存分析上下文，/rebuild 免重解码）
- [x] 碎片合并（相邻删除段 <0.3s 保留缝隙并入删除）
- [x] 转场删除（黑屏/冻结段自动检出）
- [x] 缩略图复核 + 点击跳转 PR 播放头
- [x] GPU 自适应解码：候选后端实测选型（cuda/qsv/d3d11va/dxva2/videotoolbox/vaapi/软解），
      26 分钟 1200p60 素材分析 630s → 296s；强制后端未生效显式告警
- [x] 批量分析 CLI（batch_process）+ 面板导入方案文件
- [ ] 应用后自动把节目监视器定位到第一个删除点做抽查
- [ ] 模板包管理 UI（面板内一键重校准）

### M3 · 深度与分发（部分交付）
- [x] 服务登录自启（CEP 版内嵌引擎随面板启停，心跳空闲自退）
- [x] build_portable.py 从源码组装自包含插件（开源分发用）
- [ ] 全自动调速（ffmpeg setpts 重编码 → importFiles → 覆写替换）
- [ ] 时间轴入出点映射（剪辑非全片时按 inPoint 偏移分析）
- [ ] CEP 壳（如需支持 PR ≤2024，CEP 面板可在加载时自动拉起服务）

## 6. 风险表（更新）

| # | 风险 | 状态/对策 |
|---|------|----------|
| R1 | CEP spawn 不可行 | 已绕开：改走 UXP，服务手动启动（M3 自动化） |
| R2 | ExtendScript API 签名 | 已绕开：改用 UXP API，全部按官方 26.3 d.ts 核对 |
| R3 | timeScale 不可设 | **实锤**：UXP 无 setSpeed → DR9 标记+手动调速，v2 重编码 |
| R4 | 单事务内 SetInOutPoints→Overwrite 的顺序语义未在真机验证 | **M0 头号验证项**；已实现逐段事务兜底，任何一台机器都能跑通 |
| R5 | `createSequenceFromMedia(name)` 无媒体参数时的行为（是否弹设置对话框） | M0 验证；主路径总是带媒体参数，不受影响 |
| R6 | 旧版 UXP（25.1~25.x）与 26.3 API 的签名差异 | 防御性探测已布好；M0 在用户实际版本上回归 |
| R7 | 服务生命周期（残留进程/端口占用） | 端口固定 8765；M3 加单实例锁与健康检查 |

## 7. v1 已交付物（newcut/）

```
newcut/
├─ README.md                    安装与使用
├─ docs/
│  ├─ PR插件开发方案_v2.md       本文档
│  └─ 逆向实现规格.md            原工具功能分析 → 自研实现规格（状态模型更正/算法规格/模板包格式）
├─ service/                     Python 分析服务（自研引擎，依赖 numpy+opencv+imageio-ffmpeg）
│  ├─ pyproject.toml  .python-version(3.12)  安装开机自启.bat  卸载开机自启.bat  启动服务.bat
│  ├─ tools/calibrate.py        模板校准：用户基准截图 → 模板包
│  ├─ tests/smoke_test.py       端到端冒烟（校准→服务→分析→断言）
│  └─ newcut_service/
│     ├─ server.py               HTTP API（标准库实现）
│     ├─ pipeline.py             分析编排 + 方案 JSON
│     ├─ nc_match.py             状态匹配（ROI + NCC 模板包）
│     ├─ nc_segment.py           分段/暂停运动分析/边界差分
│     ├─ nc_states.py            状态常量
│     └─ template_pack/          （用户校准后生成，不随仓库分发）
└─ panel/                       PR UXP 面板
   ├─ manifest.json              manifest v5（host: premierepro ≥25.1）
   ├─ index.html                 UI（状态/素材/分析/结果/应用/参数/日志）
   └─ js/
      ├─ bridge.js               PR DOM 桥（重建/标记/选中，含兜底路径）
      └─ main.js                 UI 逻辑 + 服务客户端
```
