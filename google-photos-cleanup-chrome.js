/* macOS JavaScript for Automation, executed by the built-in osascript.
 * Chrome stays in its normal profile. No browser framework or debugging port.
 */
ObjC.import("Foundation");

function run(argv) {
    "use strict";
    var root = argv[0], docker = argv[1], args = argv.slice(2);
    var check = false, dryRun = false, limit = Infinity, account = null;
    for (var i = 0; i < args.length; i++) {
        if (args[i] === "--check") check = true;
        else if (args[i] === "--dry-run") dryRun = true;
        else if (args[i] === "--account") account = args[++i];
        else if (args[i] === "--limit") {
            limit = Number(args[++i]);
            if (!Number.isInteger(limit) || limit < 1) throw Error("--limit must be positive");
        } else throw Error("Unknown option: " + args[i]);
    }
    if (!account || !/^[^@\s]+@[^@\s]+$/.test(account))
        throw Error("Supply --account EMAIL for the Google Photos account to verify");

    function helper(command, extra) {
        var task = $.NSTask.alloc.init;
        task.executableURL = $.NSURL.fileURLWithPath(docker);
        task.arguments = ["compose", "-f", root + "/docker-compose.google-photos-cleanup.yml",
            "run", "--no-deps", "--rm", "-T", "--interactive=false", "google-photos-cleanup", command].concat(extra || []);
        var pipe = $.NSPipe.pipe;
        // A terminal stdin can keep Compose waiting after the helper has exited.
        task.standardInput = $.NSFileHandle.fileHandleWithNullDevice;
        task.standardOutput = pipe;
        task.standardError = $.NSFileHandle.fileHandleWithStandardError;
        task.launch;
        var data = pipe.fileHandleForReading.readDataToEndOfFile;
        task.waitUntilExit;
        var output = ObjC.unwrap($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding));
        if (task.terminationStatus !== 0) throw Error("Checkpoint helper failed");
        return JSON.parse(output);
    }
    function record(item, status, detail) {
        return helper("result", ["--key", item.key, "--status", status, "--detail", detail]);
    }
    var chrome = Application("Google Chrome");
    if (!chrome.running()) chrome.launch();
    var matches = [];
    var normalWindows = chrome.windows().filter(function (w) { return w.mode() === "normal"; });
    normalWindows.forEach(function (w) {
        if (w.mode() !== "normal") return;
        w.tabs().forEach(function (t) {
            if (/^https:\/\/photos\.google\.com\//.test(t.url())) matches.push(t);
        });
    });
    var openedTab = false;
    if (matches.length === 0) {
        console.log("Opening Google Photos in normal Chrome...");
        var window = normalWindows[0];
        if (!window) {
            window = chrome.Window({mode: "normal"});
            chrome.windows.push(window);
        }
        var newTab = chrome.Tab({url: "https://photos.google.com/"});
        window.tabs.push(newTab);
        var windowTabs = window.tabs();
        matches.push(windowTabs[windowTabs.length - 1]);
        chrome.activate();
        openedTab = true;
    }
    if (matches.length !== 1)
        throw Error("Found " + matches.length + " Google Photos tabs in normal Chrome; open exactly one https://photos.google.com/ tab before starting");
    var tab = matches[0];
    if (openedTab) {
        waitFor(function () { return !tab.loading() && tab.url() !== "about:blank"; },
            30000, "Google Photos did not finish loading; check Chrome and restart");
        if (!/^https:\/\/photos\.google\.com\//.test(tab.url()))
            throw Error("Sign into Google Photos manually in the opened Chrome tab, then restart");
    }
    var originalURL = tab.url(), tabID = String(tab.id());
    var prefix = (originalURL.match(/^https:\/\/photos\.google\.com(\/u\/\d+)?\//) || [])[0];
    if (!prefix) throw Error("Unrecognized Google Photos URL");

    function evaluate(fn, payload) {
        if (String(tab.id()) !== tabID) throw Error("Chrome tab identity changed; stopping");
        if (!/^https:\/\/photos\.google\.com\//.test(tab.url()))
            throw Error("Chrome left Google Photos; sign in manually and restart");
        var source = "(" + fn.toString() + ")(" + JSON.stringify(payload || {}) + ")";
        return JSON.parse(tab.execute({javascript: "JSON.stringify(" + source + ")"}));
    }
    function navigate(url) {
        if (String(tab.id()) !== tabID) throw Error("Chrome tab identity changed; stopping");
        tab.url = url;
    }
    function waitFor(fn, timeout, message) {
        var until = Date.now() + timeout;
        while (Date.now() < until) {
            var result = fn();
            if (result) return result;
            delay(0.5);
        }
        throw Error(message);
    }
    function verifyAccount() {
        var ok = evaluate(function (p) {
            return Array.from(document.querySelectorAll("[aria-label]")).some(function (e) {
                var label = e.getAttribute("aria-label") || "";
                return /Google[ -](Account|Konto)/i.test(label) && label.indexOf("(" + p.account + ")") !== -1;
            });
        }, {account: account});
        if (!ok) throw Error("Expected Google account not visible; verify --account and use English or German Photos");
    }
    // This harmless call also checks Chrome's Allow JavaScript from Apple Events setting.
    evaluate(function () { return true; });
    verifyAccount();
    if (check) return "Chrome automation and expected account verified. No catalog or Photos changes made.";

    var verifiedURL = null, verifiedFilename = null;
    var labels = {
        "Info": ["Info", "Open info", "Info öffnen", "Information", "Informationen"],
        "Move to trash": ["Move to trash", "In den Papierkorb", "In den Papierkorb verschieben", "In Papierkorb verschieben"]
    };
    function button(label, scope, click) {
        return evaluate(function (p) {
            var visible = function (e) { return !!(e.getClientRects().length) && getComputedStyle(e).visibility !== "hidden"; };
            var containers = p.scope === "dialog"
                ? Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible) : [document];
            if (containers.length !== 1) return false;
            var hits = Array.from(containers[0].querySelectorAll('button,[role="button"]')).filter(function (e) {
                return visible(e) && !e.disabled && e.getAttribute("aria-disabled") !== "true" &&
                    p.labels.indexOf((e.getAttribute("aria-label") || e.innerText || "").trim()) !== -1;
            });
            if (hits.length !== 1) {
                if (p.click) throw Error("Expected one " + p.label + " control; found " + hits.length);
                return false;
            }
            if (p.click && p.label === "Trash retention notice") {
                var notice = containers[0].innerText.replace(/\s+/g, " ");
                if (!/Elemente im Papierkorb können jetzt nur noch 30 Tage lang wiederhergestellt werden/.test(notice))
                    throw Error("Unrecognized Trash notice");
            }
            if (p.click && (p.label === "Move to trash" || p.label === "Trash retention notice")) {
                if (location.href !== p.url) throw Error("Verified item URL changed");
                if (p.scope !== "dialog") {
                    var names = Array.from(document.querySelectorAll("body *")).filter(function (e) {
                        return visible(e) && e.children.length === 0 && e.textContent.trim() === p.filename;
                    });
                    var video = Array.from(document.querySelectorAll("video")).some(visible) ||
                        Array.from(document.querySelectorAll("iframe")).some(function (e) {
                            return visible(e) && /^https:\/\/youtube\.googleapis\.com\/embed\//.test(e.src) &&
                                /Google (Photos|Fotos).*Video/i.test(e.title);
                        });
                    if (names.length !== 1 || !video)
                        throw Error("Verified filename or video changed");
                }
            }
            if (p.click) hits[0].click();
            return true;
        }, {label: label, labels: labels[label] || [label], scope: scope, click: !!click, url: verifiedURL, filename: verifiedFilename});
    }
    function trashSuccess() {
        return evaluate(function () {
            return Array.from(document.querySelectorAll('[role="status"],[role="alert"],[aria-live]')).some(function (e) {
                return e.getClientRects().length && /Moved to trash|In (den )?Papierkorb verschoben/i.test(e.innerText);
            });
        });
    }
    function detailsMatch(item) {
        return evaluate(function (p) {
            var visible = function (e) {
                return e.getClientRects().length && getComputedStyle(e).visibility !== "hidden";
            };
            var names = Array.from(document.querySelectorAll("body *")).filter(function (e) {
                return visible(e) && e.children.length === 0 && e.textContent.trim() === p.filename;
            });
            var video = Array.from(document.querySelectorAll("video")).some(visible) ||
                Array.from(document.querySelectorAll("iframe")).some(function (e) {
                    return visible(e) && /^https:\/\/youtube\.googleapis\.com\/embed\//.test(e.src) &&
                        /Google (Photos|Fotos).*Video/i.test(e.title);
                });
            return names.length === 1 && video;
        }, {filename: item.filename});
    }
    function inspect(item) {
        verifyAccount();
        console.log("Searching Google Photos (up to 30 seconds)...");
        // Start outside search so an earlier query's results cannot be reused.
        navigate(prefix);
        waitFor(function () {
            return !tab.loading() && tab.url() === prefix && evaluate(function (p) {
                var inputs = Array.from(document.querySelectorAll('input[role="combobox"],input[role="searchbox"]'))
                    .filter(function (e) { return e.getClientRects().length && !e.disabled; });
                if (inputs.length !== 1) return false;
                var input = inputs[0];
                input.focus();
                input.value = '"' + p.filename + '"';
                input.dispatchEvent(new Event("input", {bubbles: true}));
                input.dispatchEvent(new KeyboardEvent("keydown", {
                    key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true
                }));
                return true;
            }, {filename: item.filename});
        }, 15000, "Unique Google Photos search field not available");
        var emptySince = null, rewrittenSince = null;
        var searchState = waitFor(function () {
            var state = !tab.loading() && evaluate(function (p) {
                var inputs = Array.from(document.querySelectorAll('input[role="combobox"],input[role="searchbox"]'));
                var query = inputs.some(function (e) { return e.value === '"' + p.filename + '"'; });
                if (!/\/search\//.test(location.pathname) || document.readyState !== "complete" ||
                    Array.from(document.querySelectorAll('[role="progressbar"]')).some(function (e) {
                        return e.getClientRects().length && !e.closest('[aria-hidden="true"]') &&
                            getComputedStyle(e).visibility !== "hidden";
                    })) return false;
                if (!query) return inputs.some(function (e) { return e.value; }) ? "rewritten" : false;
                if (Array.from(document.querySelectorAll('a[href*="/photo/"]')).some(function (e) {
                    return e.getClientRects().length && !e.closest('[aria-hidden="true"]') &&
                        getComputedStyle(e).visibility !== "hidden";
                })) return "results";
                var empty = Array.from(document.querySelectorAll("body *")).some(function (e) {
                    return e.children.length === 0 && e.getClientRects().length &&
                        getComputedStyle(e).visibility !== "hidden" &&
                        /^(Keine Ergebnisse|No results|No results found)$/.test(e.textContent.trim());
                });
                return empty ? "empty" : false;
            }, {filename: item.filename});
            if (state === "rewritten") {
                if (rewrittenSince === null) rewrittenSince = Date.now();
                if (Date.now() - rewrittenSince >= 2000)
                    throw Error("Google Photos rewrote the filename query for " + item.filename +
                        "; exact search could not be verified. No Trash action taken; candidate remains retryable.");
                return false;
            }
            rewrittenSince = null;
            if (state === "empty") {
                if (emptySince === null) emptySince = Date.now();
                return Date.now() - emptySince >= 2000 ? "empty" : false;
            }
            emptySince = null;
            return state;
        }, 30000, "Search results not recognized or no visible results; no deletion attempted");
        verifyAccount();
        if (searchState === "empty")
            return {status: "kept_not_found", detail: "Google Photos explicitly reported no results for the exact filename query"};

        // Accumulate virtualized links while scrolling; fail closed on multiple
        // results or an unrecognized/unstable scroll container.
        console.log("Checking search result uniqueness...");
        var seen = {}, stable = 0, previous = "", rounds = 0;
        while (stable < 4 && rounds++ < 120) {
            var scan = evaluate(function () {
                var links = Array.from(document.querySelectorAll('a[href*="/photo/"]')).filter(function (a) {
                    return a.getClientRects().length && !a.closest('[aria-hidden="true"]') &&
                        getComputedStyle(a).visibility !== "hidden";
                });
                var hrefs = links.map(function (a) { return a.href; });
                var first = links[0], scroller = first;
                while (scroller && !(/auto|scroll/.test(getComputedStyle(scroller).overflowY)))
                    scroller = scroller.parentElement;
                scroller = scroller || document.scrollingElement;
                var bottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
                var ids = hrefs.map(function (url) { return (url.match(/\/photo\/([^/?#]+)/) || [])[1]; });
                var signature = [scroller.scrollTop, scroller.scrollHeight, ids.join("|")].join(":");
                scroller.scrollTop += Math.max(200, scroller.clientHeight * 0.8);
                return {hrefs: hrefs, bottom: bottom, signature: signature,
                    busy: Array.from(document.querySelectorAll('[role="progressbar"]')).some(function (e) {
                        return e.getClientRects().length && !e.closest('[aria-hidden="true"]') &&
                            getComputedStyle(e).visibility !== "hidden";
                    })};
            });
            scan.hrefs.forEach(function (url) {
                if (!/^https:\/\/photos\.google\.com\//.test(url)) throw Error("Unexpected result origin");
                var asset = url.match(/\/photo\/([^/?#]+)/);
                if (!asset) throw Error("Unrecognized photo result URL");
                seen[asset[1]] = url;
            });
            if (Object.keys(seen).length > 1) return {status: "kept_ambiguous", detail: "Multiple search results; no deletion attempted"};
            stable = scan.bottom && !scan.busy && scan.signature === previous ? stable + 1 : 0;
            previous = scan.signature;
            delay(1);
        }
        var urls = Object.keys(seen).map(function (key) { return seen[key]; });
        if (stable < 4 || urls.length !== 1) throw Error("Could not establish a single fully loaded search result");
        navigate(urls[0]);
        waitFor(function () { return !tab.loading() && (detailsMatch(item) || button("Info", null, false)); },
            20000, "Item information control not recognized");
        if (!detailsMatch(item) && !button("Info", null, true)) throw Error("Item information control changed");
        waitFor(function () { return detailsMatch(item); },
            15000, "Exact filename and video could not both be verified");
        delay(0.5);
        if (!detailsMatch(item)) throw Error("Video details changed after opening Info");
        verifiedURL = tab.url();
        verifiedFilename = item.filename;
        if (verifiedURL !== urls[0]) throw Error("Item URL changed during verification");
        if (!button("Move to trash", null, false)) throw Error("Unique Move to trash button not recognized");
        return {status: "verified", detail: "Unique result, exact filename and video verified"};
    }
    for (var count = 0; count < limit; count++) {
        console.log("Loading next cleanup candidate...");
        var item = helper("next");
        if (item.complete) return "Cleanup complete.";
        if (item.status === "needs_review")
            throw Error("Prior Trash action needs reconciliation: " + item.key + ". See report.md");
        var intent = false;
        try {
            console.log("Inspecting: " + item.filename);
            var result = inspect(item);
            if (result.status !== "verified") {
                if (dryRun) return "Dry run: " + result.detail + ". Candidate remains in progress.";
                record(item, result.status, result.detail);
                console.log(result.status + ": " + item.filename);
                continue;
            }
            if (dryRun) return "Dry run verified " + item.filename + ". No Trash action taken; candidate remains in progress.";
            // Persist before any click, so interruption never causes an automatic retry.
            helper("intent", ["--key", item.key, "--detail", "Trash action starting; reconcile before retry if interrupted"]);
            intent = true;
            var fresh = evaluate(function () {
                return !/Moved to trash|In (den )?Papierkorb verschoben/i.test(document.body.innerText);
            });
            if (!fresh) throw Error("Stale success message present");
            waitFor(function () { return button("Move to trash", null, true); },
                5000, "Trash control changed");
            labels["Trash retention notice"] = ["OK, weiter"];
            waitFor(function () {
                if (trashSuccess() || button("Move to trash", "dialog", false)) return true;
                if (button("Trash retention notice", "dialog", false))
                    button("Trash retention notice", "dialog", true);
                return false;
            },
                10000, "Trash confirmation dialog not recognized");
            if (!trashSuccess() && !button("Move to trash", "dialog", true)) throw Error("Trash confirmation changed");
            waitFor(trashSuccess, 15000, "Trash action could not be confirmed");
            record(item, "deleted", "Chrome explicitly confirmed moved to Trash");
            console.log("deleted: " + item.filename);
        } catch (error) {
            record(item, intent ? "needs_review" : "error",
                intent ? "Trash outcome uncertain; reconcile in Chrome before retry" :
                    "Chrome UI verification failed before Trash action");
            throw error;
        }
    }
    return "Reached --limit; checkpoints saved.";
}
