// main.js —— NewCut 面板 UI 逻辑：服务连接、分析调度、方案展示、应用桥接

/* global globalThis, setInterval, setTimeout */
const SERVICE = "http://127.0.0.1:8765";
const Bridge = globalThis.NewCutBridge;

const state = {
  clip: null,     // bridge.getActiveClipInfo() 结果
  probe: null,    // service /probe 结果（fps/帧数）
  plan: null,     // 分析方案 JSON
  jobId: null,
  analyzing: false,
};

const $ = (id) => document.getElementById(id);

// ---------------- 日志 ----------------

function log(msg, isError) {
  const el = $("log");
  const line = document.createElement("div");
  if (isError) line.className = "err";
  const ts = new Date().toTimeString().slice(0, 8);
  line.textContent = "[" + ts + "] " + msg;
  el.appendChild(line);
  while (el.childElementCount > 80) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

function fmtDur(sec) {
  if (!isFinite(sec) || sec < 0) return "?";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m + ":" + (s < 10 ? "0" : "") + s;
}

// ---------------- 服务状态 ----------------

async function checkService() {
  try {
    const r = await fetch(SERVICE + "/status");
    const j = await r.json();
    $("statusDot").className = "dot ok";
    $("statusText").textContent = "服务已连接 v" + (j.version || "?");
    return true;
  } catch (e) {
    $("statusDot").className = "dot";
    $("statusText").textContent = "服务未连接（运行 service/安装开机自启.bat 一次即可永久生效）";
    return false;
  }
}

// ---------------- 素材 ----------------

async function pickClip() {
  try {
    const info = await Bridge.getActiveClipInfo();
    state.clip = info;
    $("clipInfo").textContent = "序列: " + info.sequenceName +
      "　剪辑: " + (info.clipName || "?") +
      "\n素材: " + (info.mediaPath || "(无法读取路径)") +
      "\n时间轴区间: " + fmtDur(info.startSec) + " ~ " + fmtDur(info.endSec) +
      "（时长 " + fmtDur(info.durationSec) + "）";
    log("已获取剪辑: " + (info.mediaPath || "路径为空"));

    // 用服务端探测源文件的 fps/帧数
    if (info.mediaPath) {
      try {
        const r = await fetch(SERVICE + "/probe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ video_path: info.mediaPath }),
        });
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        state.probe = j;
        log("源文件: " + j.frame_count + " 帧 @ " + j.fps.toFixed(3) + " fps, " +
          j.width + "x" + j.height + ", " + fmtDur(j.duration_sec));
        if (info.startSec > 0.5 || info.endSec > j.duration_sec + 0.5) {
          log("提示: 时间轴上的剪辑存在入出点裁剪，当前版本按完整源文件分析", true);
        }
        $("btnAnalyze").disabled = false;
      } catch (e) {
        log("探测源文件失败: " + e.message, true);
      }
    }
  } catch (e) {
    log("获取剪辑失败: " + (e.message || e), true);
  }
}

// ---------------- 分析 ----------------

function readParams() {
  const num = (id, d) => {
    const v = parseFloat($(id).value);
    return isFinite(v) ? v : d;
  };
  return {
    still_time_thresh: num("pStill", 0.1),
    motion_thresh: num("pMotion", 2.0),
    boundary_thresh: num("pBoundary", 5.0),
    thresholds: {
      pause: num("pThrPause", 0.75),
      speed_1x: num("pThrSpeed", 0.75),
      speed_2x: num("pThrSpeed", 0.75),
    },
  };
}

async function startAnalyze() {
  if (!state.clip || !state.clip.mediaPath) {
    log("请先获取选中剪辑", true);
    return;
  }
  if (state.analyzing) return;
  state.analyzing = true;
  $("btnAnalyze").disabled = true;
  try {
    const body = { video_path: state.clip.mediaPath, params: readParams() };
    const r = await fetch(SERVICE + "/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    state.jobId = j.job_id;
    log("分析已开始 (job " + j.job_id + ")");
    pollJob();
  } catch (e) {
    log("发起分析失败: " + (e.message || e), true);
    state.analyzing = false;
    $("btnAnalyze").disabled = false;
  }
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const r = await fetch(SERVICE + "/job/" + state.jobId);
    const j = await r.json();
    const pct = Math.round((j.pct || 0) * 100);
    $("progressBar").style.width = pct + "%";
    const phaseText = { queued: "排队", load: "加载模板", decode: "解码与帧分类",
                        segments: "段落提取" }[j.phase] || j.phase;
    $("progressText").textContent = phaseText + " " + pct + "% " + (j.detail || "");
    if (j.state === "running") {
      setTimeout(pollJob, 500);
      return;
    }
    if (j.state === "done") {
      state.plan = j.result;
      renderPlan(j.result);
      log("分析完成: 保留 " + j.result.keep_ranges.length + " 段 / 删除 " +
        fmtDur(j.result.duration_sec - j.result.edited_duration_sec) +
        " / 倍速段 " + j.result.speeds.length + " 个");
    } else {
      log("分析失败: " + (j.error || "未知错误"), true);
    }
  } catch (e) {
    log("查询进度失败: " + (e.message || e), true);
  }
  state.analyzing = false;
  state.jobId = null;
  $("btnAnalyze").disabled = false;
}

// ---------------- 结果渲染 ----------------

function renderPlan(plan) {
  const total = plan.frame_count || 1;
  $("planStats").textContent =
    fmtDur(plan.duration_sec) + " → " + fmtDur(plan.edited_duration_sec) +
    "（删 " + plan.delete_ranges.length + " 段 " + plan.deleted_frame_count + " 帧，倍速段 " +
    plan.speeds.length + " 个）";

  const tl = $("timeline");
  tl.innerHTML = "";
  const add = (cls, s, e) => {
    const d = document.createElement("div");
    d.className = "seg " + cls;
    d.style.left = (s / total) * 100 + "%";
    d.style.width = Math.max(0.15, ((e - s) / total) * 100) + "%";
    tl.appendChild(d);
  };
  for (const r of plan.delete_ranges) add("del", r[0], r[1]);
  for (const r of plan.keep_ranges) add("", r[0], r[1]);
  for (const sp of plan.speeds) add("speed", sp.start, sp.end);
}

// ---------------- 应用 ----------------

async function applyPlan() {
  if (!state.plan) {
    log("没有可应用的方案，请先分析", true);
    return;
  }
  $("btnApply").disabled = true;
  log("正在应用到 PR 序列…（期间请勿操作时间轴）");
  try {
    const res = await Bridge.applyEditPlan(state.plan, {
      markers: $("optMarkers").checked,
      selectSpeed: $("optSelect").checked,
    });
    log("完成: 新序列「" + res.sequenceName + "」共 " + res.segments +
      " 段，精剪时长 " + fmtDur(res.editedDurationSec) +
      "（操作模式: " + (res.mode === "single-transaction" ? "单事务" : "逐段事务") + "）");
    if (res.speedSegments > 0) {
      log("倍速段 " + res.speedSegments + " 个已选中" +
        (res.markedCount ? "并打上标记" : "") +
        "：在时间轴按 Ctrl+R 输入速度（如 200% / 1000%）");
    }
  } catch (e) {
    log("应用失败: " + (e.message || e), true);
  }
  $("btnApply").disabled = false;
}

// ---------------- 初始化 ----------------

$("btnPick").addEventListener("click", pickClip);
$("btnAnalyze").addEventListener("click", startAnalyze);
$("btnApply").addEventListener("click", applyPlan);

checkService();
setInterval(checkService, 3000);
log("NewCut 面板已加载。步骤: ①启动本地服务 ②选中素材 ③分析 ④应用到新序列");
