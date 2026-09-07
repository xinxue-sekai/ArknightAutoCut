// bridge-cep.js —— CEP 面板与 PR DOM 的桥（evalScript → jsx/host.jsx）

/* global CSInterface, window */
var CEPBridge = (function () {
    var cs = new CSInterface();

    function evalJson(script, cb) {
        cs.evalScript(script, function (result) {
            try {
                cb(JSON.parse(result));
            } catch (e) {
                cb({ error: "PR 返回无法解析: " + result });
            }
        });
    }

    function getActiveClipInfo(cb) {
        evalJson("ncGetClipInfo()", cb);
    }

    // 倍速段在重建时间轴上的位置（与 UXP 版同一套映射）
    function mapSpeedsToRebuiltTimeline(plan) {
        var fps = plan.fps;
        var keep = plan.keep_ranges || [];
        var out = [];
        var cum = 0, ki = 0;
        var speeds = (plan.speeds || []).slice().sort(function (a, b) { return a.start - b.start; });
        for (var i = 0; i < speeds.length; i++) {
            var sp = speeds[i];
            while (ki < keep.length && keep[ki][1] <= sp.start) {
                cum += (keep[ki][1] - keep[ki][0]) / fps;
                ki++;
            }
            var startSec = cum;
            if (ki < keep.length && sp.start > keep[ki][0]) {
                startSec += (Math.min(sp.start, keep[ki][1]) - keep[ki][0]) / fps;
            }
            out.push({
                label: sp.factor === 2 ? "2x" : (sp.factor + "x"),
                sec: startSec,
                dur: Math.max(0, (sp.end - sp.start) / fps)
            });
        }
        return out;
    }

    // 方案经临时文件传递给 JSX（evalScript 直接传大 JSON 会静默失败，
    // 导致"应用"按钮永远解不开）。slim 化后通常只有几十 KB。
    function applyEditPlan(plan, opts, cb) {
        var nodeRequire = window.cep_node ? window.cep_node.require : require;
        var fs = nodeRequire("fs"), os = nodeRequire("os"), path = nodeRequire("path");
        var markers = (opts && opts.markers === false) ? [] : mapSpeedsToRebuiltTimeline(plan);
        var slim = { fps: plan.fps, video_path: plan.video_path, keep_ranges: plan.keep_ranges };
        var tmp = path.join(os.tmpdir(), "newcut-plan-" + Date.now() + ".json");
        fs.writeFileSync(tmp, JSON.stringify(slim), "utf8");
        var payload = { markers: markers, mode: (opts && opts.inPlace) ? "inplace" : "newseq" };
        var script = 'ncApplyPlanFromFile("' + encodeURIComponent(tmp) + '", "' +
            encodeURIComponent(JSON.stringify(payload)) + '")';
        evalJson(script, function (r) {
            try { fs.unlinkSync(tmp); } catch (e) { }
            cb(r);
        });
    }

    // 跳转 PR 播放头到源素材的某秒。
    // 若当前活动序列是 _newcut（重建后），把源时间映射到重建时间轴。
    function seekPR(sourceSec, plan, clip, cb) {
        var target = sourceSec;
        if (plan && plan.keep_ranges) {
            target = mapSourceToRebuilt(sourceSec, plan);
        }
        var offset = (clip && clip.startSec) || 0;
        var script = 'ncSeekPlayhead(' + (target + offset).toFixed(3) + ')';
        evalJson(script, cb);
    }

    function mapSourceToRebuilt(sourceSec, plan) {
        var fps = plan.fps;
        var keep = plan.keep_ranges || [];
        var cum = 0;
        for (var i = 0; i < keep.length; i++) {
            var s = keep[i][0] / fps, e = keep[i][1] / fps;
            if (sourceSec < s) return cum;
            if (sourceSec <= e) return cum + (sourceSec - s);
            cum += e - s;
        }
        return cum;
    }

    return {
        normPath: function (p) { return String(p || "").replace(/\\/g, "/").toLowerCase(); },
        getActiveClipInfo: getActiveClipInfo,
        applyEditPlan: applyEditPlan,
        seekPR: seekPR,
        mapSpeedsToRebuiltTimeline: mapSpeedsToRebuiltTimeline
    };
})();
