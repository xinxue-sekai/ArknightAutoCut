# NewCut —— 明日方舟录屏 Premiere Pro 自动剪辑插件

**EN** | An open-source Adobe Premiere Pro plugin that auto-edits Arknights gameplay recordings: it detects pauses/speed segments by template matching, then **cuts, deletes dead time and marks speed-up ranges directly on the PR timeline** — no intermediate video export. GPU/CPU decode is auto-selected by real benchmarking. See [English summary](#english-summary) below.

分析引擎为**完全自研**（参考开源工具 [arknight-auto-editing](https://github.com/liemark/arknight-auto-editing) 的功能规格逆向实现，不含其任何代码与资源），算法依据见 [docs/逆向实现规格.md](docs/逆向实现规格.md)。

## 功能

- **一键分析**：识别右上角控制条状态（暂停 ▶ / 运行 ❚❚ / 1x / 2x），掩码 NCC + 区域全帧搜索，等比缩放，适配不同分辨率与窗口布局
- **暂停智能处理**：段内运动分析保留操作帧（原程序核心逻辑）；暂停前后画面无变化则整段删除（跳转/转场）
- **暂停段列表**：逐段勾选排除、缩略图复核、点击缩略图跳转 PR 播放头
- **碎片合并**：<0.3s 保留缝隙自动并入删除（可关）
- **转场删除**：黑屏段与冻结段自动检出（可关）
- **GPU 自适应解码**：cuda / qsv / d3d11va / dxva2 / videotoolbox / vaapi 逐个实测选型，无需指定显卡品牌，无显卡自动走 CPU（见下文"GPU 自适应"）
- **应用到 PR**：新建 `<序列名>_newcut` 序列重建精剪结果，一步撤销；倍速段自动打标记
- **批量分析**：`tools/batch_process.py` 离线处理整个文件夹，面板「导入方案文件」应用
- 分析可取消、显示剩余时间、同一视频秒恢复上次结果

## 仓库结构

```
newcut/
├─ cep-plugin/com.newcut.arknights/   自包含 CEP 插件（面板+引擎源码；runtime/ 由构建脚本生成）
├─ panel/ + service/                  备选：UXP 面板 + 独立分析服务（PR 25.1+ UXP 路线）
├─ service/newcut_service/            自研分析引擎（nc_match / nc_segment / decode / pipeline）
├─ service/tools/                     pack_from_video（视频校准）· build_portable（组装插件）· batch_process · calibrate
├─ service/tests/                     冒烟测试 + 测试夹具（截图来自 arknight-auto-editing，MIT）
└─ docs/                              开发方案 v2 + 逆向实现规格
```

## 安装（Windows，PR 2020~2025）

### 方式 A：直接使用（推荐）

从 Releases 下载打包好的 `com.newcut.arknights.zip`（或自己构建，见下），然后：

1. 解压到 `C:\Users\<你>\AppData\Roaming\Adobe\CEP\extensions\`（没有该目录就新建）
2. 双击插件里的 `！一键安装.bat`（放行未签名面板，当前用户注册表，无需管理员）
3. **完全重启 Premiere Pro** → 「窗口 → 扩展 → NewCut 明日方舟自动剪辑」

### 方式 B：从源码构建

需要 [uv](https://docs.astral.sh/uv/)：

```bash
git clone <本仓库> && cd newcut
uv run python service/tools/build_portable.py
# 可选：一步完成模板校准
uv run python service/tools/build_portable.py --calibrate-video 一局录像.mp4 \
    --paused 120 --running 80 --speed2x 80 \
    --boxes "speed=1578,108,1698,215;playpause=1745,108,1868,215"
```

### 首次使用必须：校准模板包

分析依据是一组"右上角控制条"小图（模板包）。不同分辨率/主题需要各自校准一次：

```bash
uv run python service/tools/pack_from_video.py --video 一局录像.mp4 \
    --paused 120 --running 80 --speed2x 80 --verify \
    --boxes "speed=x0,y0,x1,y1;playpause=x0,y0,x1,y1" \
    --out cep-plugin/com.newcut.arknights/engine/template_pack
```

- 三个时间点：一局里任意「暂停中」「运行中」「2x 运行中」的时刻（秒）
- `--boxes`：两个按钮的像素框（支持 0~1 归一化）；测量方法见脚本头部注释
- `--verify` 会在已知状态帧上自检并打印结果
- 生成后把 `template_pack/` 放进插件 `engine/` 目录（构建时用 `--out` 指到那里即可）

## 使用流程

1. 打开面板（引擎自动启动，圆点变绿）
2. 时间轴选中录屏素材 → 「获取选中剪辑」→「开始分析」（可取消，显示剩余时间）
3. 结果色带 + 暂停段列表复核：取消勾选想保留的段、点缩略图跳转检查
4. 「应用到新序列」（推荐，原序列不动）或「应用到原序列」（直接替换当前序列 V1，Ctrl+Z 逐步撤销）→ 精剪结果直接在时间轴上；倍速段按标记手动 Ctrl+R 调速
5. 批量：`uv run python service/tools/batch_process.py --folder "D:\录屏"` → 面板「导入方案文件」

## GPU 自适应解码

不指定显卡品牌，**实测选型**：分析启动时用各候选后端（cuda/qsv/d3d11va/dxva2/videotoolbox/vaapi/软解）各试解约 96 帧，最快者胜出（硬解需比软解快 10% 以上才优先，失败自动回退）。结论写入日志（如 `解码选型: ffmpeg-cuda (cuda=1079fps, sw=1154fps)`）。同一会话内缓存选型结果。

- **看结果**：分析完成后面板日志会多一行「解码实测选型： sw=…fps, cuda=…fps, …」，列出每个后端的实测速度；完成行的「后端 xxx」是本次实际使用的。
- **手动指定**（高级功能 →「解码后端」下拉）：可强制 CUDA/QSV/DXVA/纯 CPU/OpenCV。强制某后端但硬件不可用或探测不达标时，引擎**显式告警**后回退软解（红色日志「GPU 加速未生效：请求 cuda，实际使用 ffmpeg-sw…」），不会静默降级、也不会中断分析。
- **什么时候 GPU 有用**：1080p/1200p 素材上硬解与软解速度接近（此时分类是瓶颈）；4K 素材、低配 CPU 机器上硬解收益明显。

> 实测参考：RTX 5060 + 1920x1200@60fps 素材，完整模式 26 分钟视频约 5 分钟完成。

## 快速模式的取舍

高级功能 →「快速模式」= 跳帧采样分析（每 帧率/12 帧判断一次，中间帧继承前值）：

| | 完整模式（默认） | 快速模式 |
|---|---|---|
| 速度 | 基准 | 约 1.6 倍 |
| 短暂停检出 | 全部 | **可能整段漏检**（落在采样间隙） |
| 切割边界精度 | ±0 帧 | ±2~3 帧 |
| 冻结段检测 | 有 | 自动关闭 |

实测 26 分钟素材：完整模式 582 暂停段 / 快速模式 147 段。**暂停操作频繁的建议保持默认关闭**；适合整局基本不停、或低配机器走 OpenCV 兜底时快速预览。

## 两种应用方式

| | 应用到新序列（推荐） | 应用到原序列 |
|---|---|---|
| 行为 | 新建「`序列名_newcut`」，原序列完全不动 | 当前序列 V1 的内容被精剪结果替换 |
| 撤销 | **一步 Ctrl+Z** 全撤 | 需逐步 Ctrl+Z（多步操作） |
| 音频 | 新序列由源素材整体生成，声画天然同步 | 自动定位 A1 同起点音频剪辑同步裁剪；找不到时显式警告 |
| 适用 | 保留原版反复对比调整 | 确定不要原版、想少一步操作 |

两种模式都会在倍速段位置打命名标记（2x/10x…）；若当前活动序列已是 `_newcut` 生成的，「应用到原序列」会拒绝执行并提示切换回源素材序列。

## 批量处理与方案文件（高级）

素材多、不想逐个等分析？把"分析"和"剪辑"拆成两步：

**第 1 步：离线批量分析整个文件夹**（不占用 PR，可以睡前挂机跑）

```bash
cd newcut/service
uv run python tools/batch_process.py --folder "D:\OBSStudio"
# 可选：--pattern "*.mp4"  --fast（快速模式）  --template-dir（指定模板包）
```

跑完后每个视频旁会生成同名方案文件：

```
D:\OBSStudio\
├─ 2026-07-09 16-22-50.mp4
├─ 2026-07-09 16-22-50.newcut.json   ← 完整剪辑方案（保留/删除区间、暂停段、倍速建议）
└─ ...
```

**第 2 步：在 PR 里应用**

面板底部「高级功能 → **导入方案文件**」→ 选中对应视频的 `.newcut.json` → 色带和统计立即显示 →「应用到新序列」。逐个素材几秒钟就位。

> 注意：① 导入的方案不支持逐段勾选调整（逐帧数据未缓存），需要调整时对该视频重新分析一次即可；② 方案与视频一一对应，「应用」时会校验，不匹配会被拦截；③ 也可在没装 PR 的机器上批量分析，把 JSON 拷到剪辑机应用。

## 参数说明

| 参数 | 默认 | 含义 |
|------|------|------|
| 静止阈值 | 0.1s | 暂停中静止超过该时长删除 |
| 运动阈值 | 2.0 | 暂停内帧差高于此视为有效操作（保留） |
| 边界阈值 | 5.0 | 暂停前后帧差低于此 → 整段全删 |
| 暂停/倍速阈值 | 0.75 | 模板匹配置信度（误检调高、漏检调低） |
| 碎片合并 | 0.3s | 相邻删除段之间的短保留缝隙并入删除 |
| 快速模式 | 关 | 每 N 帧采样分类，速度 ×1.6，短暂停可能漏检 |

## 已知限制

- 分析对象 = 选中剪辑的完整源文件；单序列单剪辑
- 倍速段不自动调速（UXP/ExtendScript 无 setSpeed API；按标记手动 Ctrl+R）
- 模板按校准分辨率识别，换分辨率/主题需重新校准；窗口化录制的布局差异由全帧搜索吸收
- PR 2026 起 Adobe 逐步禁用 CEP，请改用 `panel/` + `service/` 的 UXP 路线

## 故障排查

- 扩展菜单是灰的：先打开/新建一个项目；确认完全重启过 PR
- 引擎启动失败：看面板日志；确认 `runtime/` 存在（方式 A 的完整包已含）
- 全部帧判为正常（全绿）：模板包与录屏分辨率/布局不符 → 重新校准
- 分析报错：面板日志含完整堆栈，附带 `engine/` 版本号提 issue

<a name="english-summary"></a>
## English summary

NewCut is an open-source CEP plugin for Adobe Premiere Pro (Windows, PR 2020–2025) that auto-edits Arknights recordings. A bundled Python 3.12 runtime (no external dependencies) classifies every frame via masked NCC template matching on the in-game top-right control bar, then the panel rebuilds a fine-cut sequence natively in PR (set in/out points + overwrite placement — equivalent to razor + ripple delete, one undo step). Decode backend is chosen by per-session benchmarking across cuda/qsv/d3d11va/dxva2/videotoolbox/vaapi/software — no GPU vendor assumption. Install: drop `com.newcut.arknights` into `%APPDATA%\Adobe\CEP\extensions`, run `！一键安装.bat`, restart PR. First use requires a one-minute template calibration from one of your own recordings (`service/tools/pack_from_video.py`). MIT licensed; analysis algorithms independently reimplemented, inspired by liemark/arknight-auto-editing.

## 致谢 / Credits

- [liemark/arknight-auto-editing](https://github.com/liemark/arknight-auto-editing)（MIT）—— 算法功能规格的参考来源，测试夹具截图亦取自该项目
- [Adobe-CEP/CEP-Resources](https://github.com/Adobe-CEP/CEP-Resources) —— CSInterface.js
- [douglascrockford/JSON-js](https://github.com/douglascrockford/JSON-js)（Public Domain）—— json2.jsx
- [imageio/imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)（BSD）—— 便携 ffmpeg 二进制

## 许可证

[MIT](LICENSE)。游戏素材相关模板由用户自行校准生成，与本仓库无关；请遵守明日方舟相关用户协议，仅用于个人剪辑。
