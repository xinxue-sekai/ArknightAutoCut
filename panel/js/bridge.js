// bridge.js —— NewCut 与 Premiere Pro (UXP DOM) 的桥接层
//
// 职责：读取选中剪辑信息；按分析方案在新序列中重建精剪时间轴
//（设素材入出点 + 覆写放置 = 等价"切割+删除"）；打标记、选中倍速段。
//
// 所有 PR DOM 操作基于官方 @adobe/premierepro 26.3 类型定义编写，
// 对个别签名做了防御性探测（不同小版本可能有差异）。
// 注意：executeTransaction / lockedAccess 的回调必须同步执行，
// 所有 await（取对象、建选区）都提前到事务外完成。

/* global require, globalThis */
const ppro = require("premierepro");

// ---------------- 通用小工具 ----------------

function normPath(p) {
  return String(p || "").replace(/\\/g, "/").toLowerCase();
}

function sec(n) {
  return ppro.TickTime.createWithSeconds(Number(n) || 0);
}

async function seqName(seq) {
  try { return await seq.getName(); } catch (e) { return seq.name || "序列"; }
}

async function itemName(item) {
  try { return await item.getName(); } catch (e) { return ""; }
}

// ---------------- 信息读取 ----------------

async function videoTrackItems(seq, trackIndex) {
  const track = await seq.getVideoTrack(trackIndex);
  let items = [];
  try {
    items = track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false) || [];
  } catch (e) {
    items = track.getTrackItems(0, false) || []; // 枚举值兜底
  }
  return items;
}

/**
 * 取当前活动序列中的源剪辑信息。
 * 优先取用户在时间轴上选中的剪辑；没选中则退回 V1 第一个剪辑。
 */
async function getActiveClipInfo() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("没有已打开的 PR 项目");

  const seq = await project.getActiveSequence();
  if (!seq) throw new Error("没有活动序列，请先打开包含录屏素材的序列");

  let items = [];
  try {
    const sel = await seq.getSelection();
    items = (await sel.getTrackItems()) || [];
  } catch (e) { /* selection 不可用时忽略 */ }

  if (!items.length) {
    try { items = await videoTrackItems(seq, 0); } catch (e) { /* 继续走报错 */ }
  }
  if (!items.length) throw new Error("序列 V1 上没有剪辑（请把录屏素材放到时间轴并选中它）");

  const clip = items[0];
  const projItem = await clip.getProjectItem();
  if (!projItem) throw new Error("无法获取剪辑对应的素材（projectItem 为空）");

  let mediaPath = "";
  try { mediaPath = await projItem.getMediaFilePath(); } catch (e) {}
  if (!mediaPath) {
    try { mediaPath = await projItem.getMediaPath(); } catch (e) {}
  }

  const startT = await clip.getStartTime();
  const endT = await clip.getEndTime();
  const durT = await clip.getDuration();

  return {
    mediaPath: mediaPath || "",
    mediaPathNorm: normPath(mediaPath),
    sequenceName: await seqName(seq),
    clipName: await itemName(clip),
    startSec: startT ? startT.seconds : 0,
    endSec: endT ? endT.seconds : 0,
    durationSec: durT ? durT.seconds : 0,
    // 内部复用：把 DOM 句柄一起带出来，apply 时避免二次取选中
    _project: project,
    _sequence: seq,
    _clip: clip,
    _projectItem: projItem,
  };
}

async function getMarkersApi(seq) {
  if (ppro.Markers && ppro.Markers.getMarkers) return await ppro.Markers.getMarkers(seq);
  if (ppro.Marker && ppro.Marker.getMarkers) return await ppro.Marker.getMarkers(seq);
  throw new Error("当前 PR 版本未提供 Markers API");
}

async function buildSelection(items) {
  let sel = null;
  ppro.TrackItemSelection.createEmptySelection((s) => { sel = s; });
  if (!sel) throw new Error("无法创建 TrackItemSelection");
  for (const it of items) sel.addItem(it);
  return sel;
}

// ---------------- 应用剪辑方案 ----------------

/**
 * 把分析方案应用到 PR：
 *   1. 由源素材新建 "<序列名>_newcut" 序列（等效复制，原序列不动）
 *   2. 单事务：移除自动放置的整段剪辑，按 keep_ranges 逐段
 *      设素材入出点 + 覆写放置（= 切割+删除重建，一次事务一步撤销）
 *      单事务失败时自动退化为逐段事务（撤销步数变多，结果相同）
 *   3. 倍速段打标记（2x / 10x…）并选中，方便用户 Ctrl+R 调速
 */
async function applyEditPlan(plan, opts) {
  const options = Object.assign({ markers: true, selectSpeed: true }, opts || {});
  const fps = plan.fps;
  if (!fps || fps <= 0) throw new Error("方案缺少有效 fps");
  if (!plan.keep_ranges || !plan.keep_ranges.length) {
    throw new Error("方案中没有保留区间（所有帧都被判定删除），请调整参数后重新分析");
  }

  const info = await getActiveClipInfo();
  if (plan.video_path && info.mediaPathNorm &&
      normPath(plan.video_path) !== info.mediaPathNorm) {
    throw new Error("当前选中剪辑（" + (info.mediaPath || "未知路径") +
      "）与分析的文件不一致，请重新选中源素材后重试");
  }

  const project = info._project;
  const sourceSeq = info._sequence;
  const projItem = info._projectItem;

  // 1) 新序列（等效复制工作流，原序列不动）
  const newName = info.sequenceName + "_newcut";
  let newSeq = null;
  try {
    let clipItem = projItem;
    try { clipItem = ppro.ClipProjectItem.cast(projItem); } catch (e) { /* 直接传 ProjectItem */ }
    newSeq = await project.createSequenceFromMedia(newName, [clipItem]);
  } catch (e) {
    try { newSeq = await project.createSequenceFromMedia(newName); }
    catch (e2) { throw new Error("创建工作序列失败: " + e2); }
  }
  if (!newSeq) throw new Error("创建工作序列失败（PR 未返回序列）");

  // 2) 重建 —— 事务前完成所有取值与选区构建
  const editor = ppro.SequenceEditor.getEditor(newSeq);
  const placedItems = await videoTrackItems(newSeq, 0);
  if (!placedItems.length) throw new Error("新序列 V1 上没有自动放置的剪辑，无法重建");
  const placedClip = placedItems[0];
  const placedSel = await buildSelection([placedClip]);

  const ranges = plan.keep_ranges;
  let singleTx = true;
  try {
    let ok = false;
    project.lockedAccess(() => {
      ok = project.executeTransaction((ca) => {
        ca.addAction(editor.createRemoveItemsAction(
          placedSel, false, ppro.Constants.MediaType.VIDEO));
        let t = 0;
        for (const r of ranges) {
          ca.addAction(projItem.createSetInOutPointsAction(sec(r[0] / fps), sec(r[1] / fps)));
          ca.addAction(editor.createOverwriteItemAction(projItem, sec(t), 0, 0));
          t += (r[1] - r[0]) / fps;
        }
      }, "NewCut: 应用剪辑方案");
    });
    if (!ok) throw new Error("executeTransaction 返回 false");
  } catch (e) {
    singleTx = false;
    await rebuildPerSegment(project, editor, placedSel, projItem, ranges, fps, e);
  }

  // 3) 倍速段：映射到重建时间轴 → 匹配新序列剪辑 → 打标记 → 选中
  const rebuiltSpeeds = mapSpeedsToRebuiltTimeline(plan);
  let markedCount = 0;
  const speedClips = [];
  if (rebuiltSpeeds.length) {
    const items = await videoTrackItems(newSeq, 0);
    const eps = 1.5 / fps;
    for (const it of items) {
      const st = await it.getStartTime();
      if (!st) continue;
      const hit = rebuiltSpeeds.find((sp) => Math.abs(sp.rebuiltStart - st.seconds) < eps);
      if (hit) speedClips.push({ item: it, speed: hit });
    }

    if (options.markers && speedClips.length) {
      try {
        const markers = await getMarkersApi(newSeq);
        const mType = (ppro.Marker && ppro.Marker.MARKER_TYPE_COMMENT) || undefined;
        let okM = false;
        project.lockedAccess(() => {
          okM = project.executeTransaction((ca) => {
            for (const sc of speedClips) {
              ca.addAction(markers.createAddMarkerAction(
                sc.speed.label, mType,
                sec(sc.speed.rebuiltStart), sec(sc.speed.rebuiltDuration),
                "NewCut 建议调速 " + sc.speed.label));
            }
          }, "NewCut: 添加倍速标记");
        });
        if (okM) markedCount = speedClips.length;
      } catch (e) { /* 标记失败不阻塞主流程 */ }
    }
  }

  if (options.selectSpeed && speedClips.length) {
    try {
      newSeq.setSelection(await buildSelection(speedClips.map((s) => s.item)));
    } catch (e) { /* 选中失败不影响结果 */ }
  }

  try { newSeq.setPlayerPosition(ppro.TickTime.TIME_ZERO); } catch (e) {}

  return {
    sequenceName: newName,
    mode: singleTx ? "single-transaction" : "per-segment-transactions",
    segments: ranges.length,
    speedSegments: speedClips.length,
    markedCount: markedCount,
    editedDurationSec: ranges.reduce((acc, r) => acc + (r[1] - r[0]), 0) / fps,
  };
}

/**
 * 逐段事务重建（单事务失败时的兜底）：
 * 先清空工作序列，再每段一个事务（设入出点 + 覆写）。
 */
async function rebuildPerSegment(project, editor, placedSel, projItem, ranges, fps, reason) {
  project.lockedAccess(() => {
    project.executeTransaction((ca) => {
      ca.addAction(editor.createRemoveItemsAction(
        placedSel, false, ppro.Constants.MediaType.VIDEO));
    }, "NewCut: 清空工作序列");
  });
  let t = 0;
  for (let i = 0; i < ranges.length; i++) {
    const fs = ranges[i][0], fe = ranges[i][1];
    let ok = false;
    project.lockedAccess(() => {
      ok = project.executeTransaction((ca) => {
        ca.addAction(projItem.createSetInOutPointsAction(sec(fs / fps), sec(fe / fps)));
        ca.addAction(editor.createOverwriteItemAction(projItem, sec(t), 0, 0));
      }, "NewCut: 放置片段 " + (i + 1) + "/" + ranges.length);
    });
    if (!ok) throw new Error("逐段重建在片段 " + (i + 1) + " 失败");
    t += (fe - fs) / fps;
  }
}

/**
 * 把"原始帧索引"域的倍速段映射到"重建后时间轴"秒域。
 * 重建段按原生帧率顺排，映射 = 累计保留帧数 / fps。
 */
function mapSpeedsToRebuiltTimeline(plan) {
  const fps = plan.fps;
  const keep = plan.keep_ranges || [];
  const out = [];
  let cum = 0;
  let ki = 0;
  const speeds = (plan.speeds || []).slice().sort((a, b) => a.start - b.start);
  for (const sp of speeds) {
    while (ki < keep.length && keep[ki][1] <= sp.start) {
      cum += (keep[ki][1] - keep[ki][0]) / fps;
      ki++;
    }
    let startSec = cum;
    if (ki < keep.length && sp.start > keep[ki][0]) {
      startSec += (Math.min(sp.start, keep[ki][1]) - keep[ki][0]) / fps;
    }
    out.push({
      label: sp.factor === 2 ? "2x" : (sp.factor + "x"),
      factor: sp.factor,
      rebuiltStart: startSec,
      rebuiltDuration: Math.max(0, (sp.end - sp.start) / fps),
    });
  }
  return out;
}

// 导出（面板脚本直接挂在全局）
globalThis.NewCutBridge = {
  getActiveClipInfo,
  applyEditPlan,
  mapSpeedsToRebuiltTimeline,
};
