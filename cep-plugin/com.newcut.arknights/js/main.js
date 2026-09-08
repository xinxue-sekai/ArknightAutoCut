// main.js —— NewCut CEP 面板 UI 逻辑
// 与 UXP 版流程一致；差异：引擎由 ServiceManager 随面板拉起/退出，
// 暂停段列表支持逐段排除、缩略图复核、点击跳转 PR 播放头、方案导入。

/* global ServiceManager, CEPBridge, window, document, setInterval, setTimeout */
var SERVICE_FALLBACK = "http://127.0.0.1:8765";

var state = {
  clip: null, probe: null, plan: null,
  jobId: null, analyzing: false,
  excluded: {},        // pauseId -> true（排除=保留）
  rebuildTimer: null,
  observer: null,
};

var $ = function (id) { return document.getElementById(id); };

function log(msg, isError) {
  var el = $("log");
  var line = document.createElement("div");
  if (isError) line.className = "err";
  var ts = new Date().toTimeString().slice(0, 8);
  line.textContent = "[" + ts + "] " + msg;
  el.appendChild(line);
  while (el.childElementCount > 80) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

function fmtDur(sec) {
  if (!isFinite(sec) || sec < 0) return "?";
  var m = Math.floor(sec / 60);
  var s = Math.round(sec % 60);
  return m + ":" + (s < 10 ? "0" : "") + s;
}

function setServiceStatus(ok, text) {
  $("statusDot").className = "dot" + (ok ? " ok" : "");
  $("statusText").textContent = text;
}

// ---------------- 引擎（面板加载即拉起，关闭即退出） ----------------

function startEngine() {
  if (!ServiceManager.available) {
    setServiceStatus(false, "Node 不可用（CEP 参数缺 --enable-nodejs）");
    return;
  }
  setServiceStatus(false, "正在启动内置引擎…");
  ServiceManager.start(function (ok, err, port, adopted) {
    if (ok) {
      BASE = "http://127.0.0.1:" + port;
      setServiceStatus(true, "引擎已就绪" + (adopted ? "（复用已有实例）" : "") + " :" + port);
      log("内置分析引擎已启动" + (adopted ? "（检测到已有实例，直接复用）" : ""));
    } else {
      setServiceStatus(false, "引擎启动失败");
      log("引擎启动失败: " + err, true);
    }
  });
}

function httpJson(method, path, body, cb, timeoutMs) {
  var xhr = new XMLHttpRequest();
  xhr.open(method, BASE + path, true);
  xhr.timeout = timeoutMs || 30000;
  xhr.setRequestHeader("Content-Type", "application/json");
  xhr.onload = function () {
    try { cb(JSON.parse(xhr.responseText)); }
    catch (e) { cb({ error: "响应解析失败: " + xhr.responseText.slice(0, 200) }); }
  };
  xhr.onerror = function () { cb({ error: "无法连接内置引擎" }); };
  xhr.ontimeout = function () { cb({ error: "请求超时" }); };
  xhr.send(body ? JSON.stringify(body) : null);
}

// 引擎健康检查 + 断线自愈：分析/取素材前确认引擎存活，失联则自动重启
function ensureEngine(cb) {
  httpJson("GET", "/status", null, function (j) {
    if (!j.error) return cb(true);
    setServiceStatus(false, "引擎失联，正在自动重启…");
    log("引擎未响应，尝试自动重启…");
    ServiceManager.start(function (ok, err, port) {
      if (ok) {
        BASE = "http://127.0.0.1:" + port;
        setServiceStatus(true, "引擎已就绪 :" + port);
        log("引擎已自动重启 :" + port);
      } else {
        setServiceStatus(false, "引擎启动失败");
        log("引擎自动重启失败: " + err, true);
      }
      cb(ok);
    });
  }, 3000);
}

// ---------------- 素材 ----------------

function pickClip() {
  ensureEngine(function (ok) {
    if (!ok) { log("引擎不可用，无法获取剪辑", true); return; }
    _pickClip();
  });
}

function _pickClip() {
  CEPBridge.getActiveClipInfo(function (r) {
    if (r.error) { log("获取剪辑失败: " + r.error, true); return; }
    state.clip = r;
    $("clipInfo").textContent = "序列: " + r.sequenceName +
      "　剪辑: " + (r.clipName || "?") +
      "\n素材: " + (r.mediaPath || "(无法读取路径)");
    log("已获取剪辑: " + (r.mediaPath || "路径为空"));

    httpJson("POST", "/probe", { video_path: r.mediaPath }, function (j) {
      if (j.error) { log("探测源文件失败: " + j.error, true); return; }
      state.probe = j;
      log("源文件: " + j.frame_count + " 帧 @ " + j.fps.toFixed(3) + " fps, " +
        j.width + "x" + j.height + ", " + fmtDur(j.duration_sec));
      if (r.startSec > 0.5 || r.endSec > j.duration_sec + 0.5) {
        log("提示: 时间轴上的剪辑有入出点裁剪，当前版本按完整源文件分析", true);
      }
      $("btnAnalyze").disabled = false;
      tryRestoreLastJob();
    });
  });
}

// 恢复上次分析（同一视频且引擎里还有缓存时免重分析）
function tryRestoreLastJob() {
  var saved = null;
  try { saved = JSON.parse(window.localStorage.getItem("newcut_lastjob") || "null"); } catch (e) { }
  if (!saved || !saved.job_id || !state.clip) return;
  if (saved.video_path !== state.clip.mediaPathNorm) return;
  httpJson("GET", "/job/" + saved.job_id, null, function (j) {
    if (j.error || j.state !== "done") return;
    if (!j.result || j.result.video_path !== state.clip.mediaPathNorm) return;
    state.jobId = saved.job_id;
    state.plan = j.result;
    renderPlan(j.result);
    renderPauseList(j.result);
    $("btnApply").disabled = false;
    $("progressBar").style.width = "100%";
    $("progressText").textContent = "已恢复上次分析结果";
    log("已恢复上次分析结果（同一视频），可直接调整或应用");
  }, 8000);
}

// ---------------- 分析 ----------------

function readParams() {
  var num = function (id, d) {
    var v = parseFloat($(id).value);
    return isFinite(v) ? v : d;
  };
  return {
    still_time_thresh: num("pStill", 0.1),
    motion_thresh: num("pMotion", 2.0),
    boundary_thresh: num("pBoundary", 5.0),
    fast_mode: $("optFast").checked,
    decode_backend: $("decodeBackend").value,
    thresholds: {
      pause: num("pThrPause", 0.75),
      speed_1x: num("pThrSpeed", 0.75),
      speed_2x: num("pThrSpeed", 0.75)
    }
  };
}

function startAnalyze() {
  if (!state.clip || !state.clip.mediaPath) { log("请先获取选中剪辑", true); return; }
  if (state.analyzing) return;
  ensureEngine(function (ok) {
    if (!ok) { log("引擎不可用，请检查后重试", true); return; }
    _startAnalyze();
  });
}

function _startAnalyze() {
  state.analyzing = true;
  state.excluded = {};
  $("btnAnalyze").disabled = true;
  $("btnCancel").disabled = false;
  var body = { video_path: state.clip.mediaPath, params: readParams() };
  body.params.merge_gap_sec = $("optMerge").checked ? 0.3 : 0;
  body.params.detect_transitions = $("optTrans").checked;
  httpJson("POST", "/analyze", body, function (j) {
    if (j.error) {
      log("发起分析失败: " + j.error, true);
      state.analyzing = false;
      $("btnAnalyze").disabled = false;
      $("btnCancel").disabled = true;
      return;
    }
    state.jobId = j.job_id;
    log("分析已开始 (job " + j.job_id + ")");
    pollJob();
  });
}

function cancelAnalyze() {
  if (!state.jobId) return;
  httpJson("POST", "/job/" + state.jobId + "/cancel", null, function (j) {
    log("已请求取消…");
  });
}

function pollJob() {
  if (!state.jobId) return;
  httpJson("GET", "/job/" + state.jobId, null, function (j) {
    var pct = Math.round((j.pct || 0) * 100);
    $("progressBar").style.width = pct + "%";
    var phaseText = { queued: "排队", load: "加载模板", decode: "解码与帧分类",
                      segments: "分段", boundary: "边界差分",
                      transitions: "转场检测" }[j.phase] || j.phase || "";
    var eta = j.eta_sec ? "，剩余约 " + fmtDur(j.eta_sec) : "";
    $("progressText").textContent = phaseText + " " + pct + "% " + (j.detail || "") + eta;
    if (j.state === "running") { setTimeout(pollJob, 400); return; }
    if (j.state === "done") {
      state.plan = j.result;
      renderPlan(j.result);
      renderPauseList(j.result);
      log("分析完成: 保留 " + j.result.keep_ranges.length + " 段 / 删除 " +
        fmtDur(j.result.duration_sec - j.result.edited_duration_sec) +
        " / 暂停段 " + j.result.pauses.length + " / 转场 " +
        (j.result.transitions || []).length + "（后端 " + (j.result.backend || "?") + "）");
      var bench = j.result.decode_bench || {};
      var parts = Object.keys(bench).map(function (k) {
        return k + "=" + Math.round(bench[k]) + "fps";
      });
      if (parts.length) log("解码实测选型: " + parts.join(", "));
      if (j.result.backend_warning) log(j.result.backend_warning, true);
      try {
        window.localStorage.setItem("newcut_lastjob", JSON.stringify({
          job_id: state.jobId, video_path: state.clip.mediaPathNorm
        }));
      } catch (e) { }
    } else if (j.state === "cancelled") {
      log("分析已取消");
    } else {
      log("分析失败: " + (j.error || "未知错误"), true);
    }
    state.analyzing = false;
    state.jobId = j.state === "done" ? state.jobId : null;
    $("btnAnalyze").disabled = false;
    $("btnCancel").disabled = true;
    $("btnApply").disabled = j.state !== "done";
    $("btnApplyInPlace").disabled = j.state !== "done";
  });
}

// ---------------- 结果渲染 ----------------

function renderPlan(plan) {
  var total = plan.frame_count || 1;
  $("planStats").textContent =
    fmtDur(plan.duration_sec) + " → " + fmtDur(plan.edited_duration_sec) +
    "（删 " + plan.delete_ranges.length + " 段 " + plan.deleted_frame_count + " 帧，暂停段 " +
    plan.pauses.length + " 个）";
  var tl = $("timeline");
  tl.innerHTML = "";
  var add = function (cls, s, e) {
    var d = document.createElement("div");
    d.className = "seg " + cls;
    d.style.left = (s / total) * 100 + "%";
    d.style.width = Math.max(0.15, ((e - s) / total) * 100) + "%";
    tl.appendChild(d);
  };
  var i;
  for (i = 0; i < (plan.transitions || []).length; i++) add("", plan.transitions[i].start, plan.transitions[i].end);
  for (i = 0; i < plan.delete_ranges.length; i++) add("del", plan.delete_ranges[i][0], plan.delete_ranges[i][1]);
  for (i = 0; i < plan.keep_ranges.length; i++) add("", plan.keep_ranges[i][0], plan.keep_ranges[i][1]);
  for (i = 0; i < plan.speeds.length; i++) add("speed", plan.speeds[i].start, plan.speeds[i].end);
}

function renderPauseList(plan) {
  $("pauseCount").textContent = plan.pauses.length;
  var list = $("pauseList");
  list.innerHTML = "";
  var fps = plan.fps;
  state.observer = new IntersectionObserver(onVisible, { root: list, rootMargin: "40px" });

  plan.pauses.forEach(function (p) {
    var row = document.createElement("div");
    row.className = "pl-item" + (p.excluded ? " off" : "");
    row.dataset.id = p.id;
    row.dataset.start = p.start;

    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !p.excluded;
    cb.addEventListener("change", function () {
      state.excluded[p.id] = cb.checked ? undefined : true;
      if (!cb.checked) row.className = "pl-item off"; else row.className = "pl-item";
      scheduleRebuild();
    });

    var img = document.createElement("img");
    img.dataset.sec = p.start / fps;
    img.title = "点击跳转 PR 播放头";
    img.addEventListener("click", function () {
      CEPBridge.seekPR(p.start / fps, state.plan, state.clip, state.applied,
        function (r) {
          if (r && r.error) log("跳转失败: " + r.error, true);
        });
    });

    var t = document.createElement("span");
    t.className = "t";
    t.textContent = "#" + p.id + "  " + fmtDur(p.start / fps) + " ~ " + fmtDur(p.end / fps);

    var d = document.createElement("span");
    d.className = "d";
    var keptSec = (p.sub_keep_ranges || []).reduce(function (a, r) { return a + (r[1] - r[0]); }, 0) / fps;
    d.textContent = (p.mode === "all" ? "全删" : "留操作 " + keptSec.toFixed(1) + "s") +
      " 边界差" + p.boundary_diff;

    row.appendChild(cb);
    row.appendChild(img);
    row.appendChild(t);
    row.appendChild(d);
    list.appendChild(row);
    state.observer.observe(img);
  });
}

function onVisible(entries) {
  entries.forEach(function (en) {
    if (!en.isIntersecting) return;
    var img = en.target;
    state.observer.unobserve(img);
    var sec = parseFloat(img.dataset.sec);
    httpJson("GET", "/thumb?job_id=" + state.jobId + "&sec=" + sec.toFixed(2),
      null, function (r) {
        if (r.url) img.src = r.url;
      }, 20000);
  });
}

// ---------------- 重算（排除/合并/转场变化时，免重解码） ----------------

function scheduleRebuild() {
  if (!state.jobId) return;
  if (state.rebuildTimer) clearTimeout(state.rebuildTimer);
  state.rebuildTimer = setTimeout(function () {
    var overrides = {
      excluded_pauses: Object.keys(state.excluded).filter(function (k) { return state.excluded[k]; })
        .map(function (k) { return parseInt(k, 10); }),
      merge_gap_sec: $("optMerge").checked ? 0.3 : 0,
      detect_transitions: $("optTrans").checked
    };
    httpJson("POST", "/rebuild", { job_id: state.jobId, overrides: overrides }, function (r) {
      if (r.error) { log("重算失败: " + r.error, true); return; }
      state.plan = r;
      renderPlan(r);
      $("pauseCount").textContent = r.pauses.length;
      log("已重算: 保留 " + r.keep_ranges.length + " 段 / 删除 " +
        fmtDur(r.duration_sec - r.edited_duration_sec));
    });
  }, 350);
}

// ---------------- 方案导入 ----------------

function importPlan() { $("fileImport").click(); }

function onImportFile(ev) {
  var f = ev.target.files && ev.target.files[0];
  if (!f) return;
  var reader = new FileReader();
  reader.onload = function () {
    try {
      var plan = JSON.parse(reader.result);
      if (!plan.keep_ranges || !plan.video_path) throw new Error("不是有效的方案文件");
      state.plan = plan;
      state.jobId = null;
      state.excluded = {};
      renderPlan(plan);
      renderPauseList(plan);
      $("btnApply").disabled = false;
      log("已导入方案: " + plan.video_path + "（来自批量分析文件，暂停段勾选不可用）");
      if (state.clip && CEPBridge.normPath &&
          CEPBridge.normPath(plan.video_path) !== state.clip.mediaPathNorm) {
        log("注意: 方案对应的视频与当前选中剪辑不一致，应用前请先切换序列", true);
      }
    } catch (e) {
      log("导入失败: " + e.message, true);
    }
  };
  reader.readAsText(f, "utf-8");
  ev.target.value = "";
}

// ---------------- 应用 ----------------

function applyPlan(inPlace) {
  if (!state.plan) { log("没有可应用的方案，请先分析", true); return; }
  if (inPlace && !window.confirm(
      "将直接把当前序列「" + (state.clip ? state.clip.sequenceName : "?") +
      "」V1 的内容替换为精剪结果（原内容可逐步 Ctrl+Z 撤销）。\n\n确定继续？")) {
    return;
  }
  var btn = $(inPlace ? "btnApplyInPlace" : "btnApply");
  $("btnApply").disabled = true;
  $("btnApplyInPlace").disabled = true;
  log("正在应用到 PR 序列（" + (inPlace ? "原序列" : "新序列") + "）… 期间请勿操作时间轴");
  var done = false;
  setTimeout(function () {
    if (!done) log("仍在执行…若 PR 出现弹窗或对话框请先处理；大方案可能需要十几秒", true);
  }, 30000);
  CEPBridge.applyEditPlan(state.plan, {
    markers: $("optMarkers").checked,
    inPlace: inPlace
  }, function (res) {
    done = true;
    $("btnApply").disabled = false;
    $("btnApplyInPlace").disabled = false;
    if (res.error) {
      log("应用失败: " + res.error, true);
      return;
    }
    log("完成: " + (res.inPlace ? "原序列「" : "新序列「") + res.sequenceName +
      "」共 " + res.segments + " 段，精剪时长 " + fmtDur(res.editedDurationSec) +
      (res.inPlace ? "（多步操作，Ctrl+Z 可逐步撤销）" : ""));
    if (res.markers > 0) {
      log("倍速段已打 " + res.markers + " 个标记：在时间轴选中标记所在段，" +
        "右键 → 速度/持续时间，输入 200%（1x 段）");
    }
    if (res.inPlace && res.audioTrimmed === false) {
      log("注意: A1 上未找到与视频同起点的音频剪辑，音频未随视频裁剪", true);
    }
  });
}

// ---------------- 初始化 ----------------

$("btnPick").addEventListener("click", pickClip);
$("btnAnalyze").addEventListener("click", startAnalyze);
$("btnCancel").addEventListener("click", cancelAnalyze);
$("btnApply").addEventListener("click", function () { applyPlan(false); });
$("btnApplyInPlace").addEventListener("click", function () { applyPlan(true); });
$("btnImport").addEventListener("click", importPlan);
$("fileImport").addEventListener("change", onImportFile);
$("optMerge").addEventListener("change", scheduleRebuild);
$("optTrans").addEventListener("change", scheduleRebuild);
window.addEventListener("unload", function () { ServiceManager.stop(); });

log("NewCut 面板已加载。正在拉起内置引擎…");
startEngine();
