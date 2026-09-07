#include "json2.jsx"
// host.jsx —— NewCut CEP 面板的 ExtendScript 桥（在 PR 主进程执行）
//
// 职责：读取源剪辑信息；按分析方案在新序列中重建精剪时间轴。
// 重建方式：修剪首段 + [设素材入出点 + overwriteClip 覆写放置]×N，
// 与"刀切+波纹删除"逐帧等价。全部使用文档化 API，对跨版本签名差异做逐级尝试。
//
// 所有函数返回 JSON 字符串（面板侧解析），错误带 error 字段。

function _ncNorm(p) {
    return String(p || "").replace(/\\/g, "/").toLowerCase();
}

function _ncErr(msg) {
    return JSON.stringify({ error: msg });
}

function _ncTimecode(sec, fps) {
    if (!fps || fps <= 0) fps = 30;
    var totalFrames = Math.round(sec * fps);
    var ff = totalFrames % fps;
    var totalSec = Math.floor(totalFrames / fps);
    var ss = totalSec % 60;
    var mm = Math.floor(totalSec / 60) % 60;
    var hh = Math.floor(totalSec / 3600);
    function p2(n) { return (n < 10 ? "0" : "") + n; }
    return p2(hh) + ":" + p2(mm) + ":" + p2(ss) + ":" + p2(ff);
}

// ProjectItem.setInPoint/setOutPoint 在不同 PR 版本接受（秒数|时间码, 媒体类型4=全部），
// 逐级尝试直到成功
function _ncSetProjInOut(pi, inSec, outSec, fps) {
    var inTc = _ncTimecode(inSec, fps), outTc = _ncTimecode(outSec, fps);
    try { pi.setInPoint(inSec, 4); pi.setOutPoint(outSec, 4); return true; } catch (e) { }
    try { pi.setInPoint(inTc, 4); pi.setOutPoint(outTc, 4); return true; } catch (e) { }
    try { pi.setInPoint(inSec); pi.setOutPoint(outSec); return true; } catch (e) { }
    try { pi.setInPoint(inTc); pi.setOutPoint(outTc); return true; } catch (e) { }
    return false;
}

function _ncClearProjInOut(pi) {
    try { pi.clearInOut(); return; } catch (e) { }
    try { pi.setInPoint(0, 4); } catch (e) { }
    try { pi.setOutPoint(1e9, 4); } catch (e) { }
}

// Track.overwriteClip 的第二参数不同版本接受时间码字符串或秒数
function _ncOverwrite(track, pi, sec, fps) {
    try { track.overwriteClip(pi, _ncTimecode(sec, fps)); return true; } catch (e) { }
    try { track.overwriteClip(pi, sec); return true; } catch (e) { }
    return false;
}

// ---------------- 读取源剪辑信息 ----------------

function ncGetClipInfo() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return _ncErr("没有活动序列，请先打开包含录屏素材的序列");
        if (seq.videoTracks.numTracks < 1 || seq.videoTracks[0].clips.numItems < 1) {
            return _ncErr("序列 V1 上没有剪辑（请把录屏素材放到时间轴）");
        }
        var clip = seq.videoTracks[0].clips[0];
        var pi = clip.projectItem;
        var mediaPath = "";
        try { mediaPath = pi.getMediaPath(); } catch (e) { }
        var fps = 0;
        try { fps = 254016000000.0 / parseFloat(seq.timebase); } catch (e) { }
        return JSON.stringify({
            sequenceName: String(seq.name),
            clipName: String(pi ? pi.name : (clip.name || "")),
            mediaPath: String(mediaPath || ""),
            startSec: Number(clip.start.seconds),
            endSec: Number(clip.end.seconds),
            durationSec: Number(clip.end.seconds - clip.start.seconds),
            sequenceFps: fps
        });
    } catch (e) {
        return _ncErr("读取剪辑信息异常: " + e);
    }
}

// ---------------- 播放头跳转 ----------------
// sec: 目标时间（秒，活动序列时间轴）。用于复核暂停段。
function ncSeekPlayhead(sec) {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return _ncErr("没有活动序列");
        var fps = 0;
        try { fps = 254016000000.0 / parseFloat(seq.timebase); } catch (e) { }
        seq.setPlayerPosition(_ncTimecode(Number(sec) || 0, fps));
        return JSON.stringify({ ok: true });
    } catch (e) {
        return _ncErr("跳转失败: " + e);
    }
}

// ---------------- 应用剪辑方案 ----------------
// 方案 JSON 由面板写入临时文件（evalScript 直传大字符串会静默失败，
// 导致应用回调永不触发、按钮卡死），此处读文件解析。
// payload: {markers:[{sec,dur,label}], mode:"newseq"|"inplace"}
function ncApplyPlanFromFile(pathEnc, payloadEnc) {
    try {
        var f = new File(decodeURIComponent(pathEnc));
        f.encoding = "UTF-8";
        if (!f.open("r")) return _ncErr("无法读取方案临时文件: " + f.error);
        var plan = JSON.parse(f.read());
        f.close();
        try { f.remove(); } catch (e2) { }
        var payload = JSON.parse(decodeURIComponent(payloadEnc));
        return _ncApply(plan, payload);
    } catch (e) {
        return _ncErr("应用方案异常: " + e);
    }
}

function _ncApply(plan, payload) {
    var pi = null;
    var inplace = (payload.mode === "inplace");
    try {
        var markers = payload.markers || [];
        var seq = app.project.activeSequence;
        if (!seq) return _ncErr("没有活动序列");
        if (seq.videoTracks.numTracks < 1 || seq.videoTracks[0].clips.numItems < 1) {
            return _ncErr("序列 V1 上没有源剪辑");
        }
        var clip = seq.videoTracks[0].clips[0];
        pi = clip.projectItem;

        var mediaPath = "";
        try { mediaPath = pi.getMediaPath(); } catch (e) { }
        if (plan.video_path && mediaPath &&
            _ncNorm(mediaPath) !== _ncNorm(plan.video_path)) {
            return _ncErr("当前 V1 剪辑（" + mediaPath + "）与分析文件不一致，请先获取选中剪辑后重试");
        }

        var fps = Number(plan.fps);
        var ranges = plan.keep_ranges;
        if (!ranges || !ranges.length) return _ncErr("方案中没有保留区间");

        var vTrack = seq.videoTracks[0];
        var baseSec = 0;
        var full = null;
        var aClip = null;
        var seqName = "";

        if (inplace) {
            // 直接编辑当前序列（V1 内容被精剪结果替换）
            if (/newcut$/i.test(String(seq.name))) {
                return _ncErr("当前已是 NewCut 生成的序列；「应用到原序列」请切换到源素材序列后再用");
            }
            full = clip;
            baseSec = Number(clip.start.seconds);
            // 找 A1 上与视频剪辑同起点的音频剪辑，保持声画同步
            if (seq.audioTracks.numTracks > 0) {
                var aClips = seq.audioTracks[0].clips;
                for (var a = 0; a < aClips.numItems; a++) {
                    if (Math.abs(Number(aClips[a].start.seconds) - baseSec) < 0.05) {
                        aClip = aClips[a];
                        break;
                    }
                }
            }
            seqName = String(seq.name);
        } else {
            // 由源素材新建工作序列（等效复制，原序列不动）
            var newName = String(seq.name) + "_newcut";
            var newSeq = null;
            try { newSeq = app.project.createNewSequenceFromClips(newName, [pi]); } catch (e) {
                try { newSeq = app.project.createNewSequenceFromClips(newName, pi); } catch (e2) {
                    return _ncErr("创建工作序列失败: " + e2);
                }
            }
            if (!newSeq) return _ncErr("创建工作序列失败（PR 未返回序列）");
            try { app.project.activeSequence = newSeq; } catch (e) { }
            seq = newSeq;
            vTrack = newSeq.videoTracks[0];
            if (vTrack.clips.numItems < 1) return _ncErr("新序列上没有自动放置的剪辑");
            full = vTrack.clips[0];
            seqName = newName;
        }

        // 重建：首段直接修剪，其余段设素材入出点后覆写放置
        var t = 0;
        for (var i = 0; i < ranges.length; i++) {
            var fs = Number(ranges[i][0]), fe = Number(ranges[i][1]);
            var inSec = fs / fps, outSec = fe / fps;
            if (i === 0) {
                try {
                    full.inPoint.seconds = inSec;
                    full.outPoint.seconds = outSec;
                    if (aClip) {
                        aClip.inPoint.seconds = inSec;
                        aClip.outPoint.seconds = outSec;
                    }
                } catch (e) {
                    return _ncErr("修剪首段失败: " + e);
                }
            } else {
                if (!_ncSetProjInOut(pi, inSec, outSec, fps)) {
                    return _ncErr("设置素材入出点失败（片段 " + (i + 1) + "）");
                }
                if (!_ncOverwrite(vTrack, pi, baseSec + t, fps)) {
                    return _ncErr("放置片段失败（片段 " + (i + 1) + "）");
                }
            }
            t += (fe - fs) / fps;
        }

        // 清除分析过程中对源素材写入的入出点标记
        _ncClearProjInOut(pi);

        // 倍速段标记
        var marked = 0;
        for (var m = 0; m < markers.length; m++) {
            try {
                var mk = seq.markers.createMarker(baseSec + Number(markers[m].sec));
                mk.name = String(markers[m].label);
                try { mk.comments = "NewCut 建议调速 " + markers[m].label; } catch (e2) { }
                try { mk.end.seconds = baseSec + Number(markers[m].sec) + Number(markers[m].dur); } catch (e3) { }
                marked++;
            } catch (e) { }
        }

        if (!inplace) {
            try { seq.setPlayerPosition(_ncTimecode(0, fps)); } catch (e) { }
        }

        return JSON.stringify({
            sequenceName: seqName,
            inPlace: inplace,
            segments: ranges.length,
            markers: marked,
            editedDurationSec: t,
            audioTrimmed: (inplace ? (aClip ? true : false) : true)
        });
    } catch (e) {
        if (pi) { try { _ncClearProjInOut(pi); } catch (e2) { } }
        return _ncErr("应用方案异常: " + e);
    }
}
