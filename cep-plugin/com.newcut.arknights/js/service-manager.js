// service-manager.js —— 内嵌分析引擎的进程管理（CEP Node）
//
// 面板加载时拉起插件自带的嵌入式 Python 引擎（无窗口），
// 面板关闭时将其退出；引擎自身带心跳空闲自退兜底。
// 若端口上已有可用引擎（如用户手动启动的服务），直接复用不重复拉起。

/* global window, require */
var ServiceManager = (function () {
    var nodeRequire = null;
    try {
        nodeRequire = window.cep_node ? window.cep_node.require : require;
    } catch (e) { nodeRequire = null; }
    if (!nodeRequire) return { available: false, start: function (cb) { cb(false, "Node 环境不可用"); } };

    var http = nodeRequire("http");
    var path = nodeRequire("path");
    var fs = nodeRequire("fs");
    var cp = nodeRequire("child_process");

    var PORTS = [8765, 8766, 8767, 8768, 8769];
    var EXPECTED_ENGINE_VERSION = "0.4.1"; // 不一致时关掉旧引擎重拉（防止复用旧代码/旧模板包的残留进程）
    var state = { port: null, child: null, adopted: false, hbTimer: null, ready: false };

    function extDir() {
        var p = window.__adobe_cep__.getSystemPath("extension"); // file:///D:/...
        p = decodeURIComponent(p.replace(/^file:\/\/\//, "").replace(/\//g, "\\"));
        if (/^[A-Za-z]:/.test(p) === false && p.charAt(0) === "\\") p = p.slice(1);
        return p;
    }

    var DIR = extDir();
    var PY_EXE = path.join(DIR, "runtime", "pythonw.exe");
    var ENGINE = path.join(DIR, "engine", "server.py");

    function ping(port, timeoutMs, cb) {
        var req = http.get({ host: "127.0.0.1", port: port, path: "/status", timeout: timeoutMs },
            function (res) {
                var buf = "";
                res.on("data", function (d) { buf += d; });
                res.on("end", function () {
                    try {
                        var j = JSON.parse(buf);
                        cb(j && j.ok, j);
                    } catch (e) { cb(false, null); }
                });
            });
        req.on("timeout", function () { req.destroy(); cb(false, null); });
        req.on("error", function () { cb(false, null); });
    }

    function heartbeat() {
        if (!state.ready) return;
        var req = http.request({ host: "127.0.0.1", port: state.port, path: "/heartbeat",
                                 method: "POST" }, function () { });
        req.on("error", function () { });
        req.end();
    }

    function waitReady(port, deadline, cb) {
        ping(port, 1200, function (ok, info) {
            if (ok) return cb(true, info);
            if (Date.now() > deadline) return cb(false, null);
            setTimeout(function () { waitReady(port, deadline, cb); }, 350);
        });
    }

    // 依次探测端口：已有引擎则复用；否则在第一个空闲端口拉起自带引擎
    function tryPort(idx, cb) {
        if (idx >= PORTS.length) return cb(false, "8765-8769 端口均不可用");
        var port = PORTS[idx];
        ping(port, 800, function (ok, info) {
            if (ok && info && info.service === "newcut-service") {
                if (info.version !== EXPECTED_ENGINE_VERSION) {
                    // 版本不匹配：终止旧引擎，稍后在本端口拉起新版
                    shutdown(port);
                    setTimeout(function () { spawnOn(port, cb); }, 800);
                    return;
                }
                state.port = port; state.adopted = true; state.ready = true;
                return cb(true, null);
            }
            // 端口被其他程序占用且不是我们的引擎 → 换下一个
            if (ok) return tryPort(idx + 1, cb);
            if (!fs.existsSync(PY_EXE) || !fs.existsSync(ENGINE)) {
                return cb(false, "插件不完整：缺少 runtime/engine（请重新复制整个插件文件夹）");
            }
            spawnOn(port, cb);
        });
    }

    function shutdown(port) {
        var req = http.request({ host: "127.0.0.1", port: port, path: "/shutdown",
                                 method: "POST" }, function () { });
        req.on("error", function () { });
        req.end();
    }

    function spawnOn(port, cb) {
        var child = cp.spawn(PY_EXE, [ENGINE, "--port", String(port), "--idle-exit", "600"],
            { cwd: path.dirname(ENGINE), detached: false, stdio: "ignore",
              windowsHide: true });
        child.on("error", function (e) { cb(false, "引擎启动失败: " + e); });
        waitReady(port, Date.now() + 20000, function (ok2) {
            if (!ok2) {
                try { child.kill(); } catch (e) { }
                return cb(false, "引擎未在 20s 内就绪");
            }
            state.port = port; state.child = child; state.adopted = false; state.ready = true;
            cb(true, null);
        });
    }

    function start(cb) {
        state.ready = false;
        tryPort(0, function (ok, err) {
            if (ok) {
                if (state.hbTimer) clearInterval(state.hbTimer);
                state.hbTimer = setInterval(heartbeat, 30000);
            }
            cb(ok, err, state.port, state.adopted);
        });
    }

    function stop() {
        if (state.hbTimer) { clearInterval(state.hbTimer); state.hbTimer = null; }
        if (state.child) {
            try { state.child.kill(); } catch (e) { }
            state.child = null;
        }
        state.ready = false;
    }

    function baseUrl() { return state.port ? "http://127.0.0.1:" + state.port : null; }

    return {
        available: true,
        start: start,
        stop: stop,
        baseUrl: baseUrl,
        isAdopted: function () { return state.adopted; },
        engineInfo: function () { return { py: PY_EXE, engine: ENGINE }; }
    };
})();
