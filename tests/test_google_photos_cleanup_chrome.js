// Run with node --test tests/test_google_photos_cleanup_chrome.js.
// The fake Chrome page exercises the runner without contacting Google or deleting data.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../google-photos-cleanup-chrome.js"), "utf8");

function fixture(options = {}) {
    let clock = 0, stage = "home", currentURL = "https://photos.google.com/";
    let confirmed = false, clicks = 0, checkpoint = "pending", output = "";
    const events = [];
    const node = (label, click = () => {}) => ({
        innerText: label, textContent: label, children: [], disabled: false,
        getClientRects: () => [1], closest: () => null,
        getAttribute: key => key === "aria-label" ? label : null,
        click,
    });
    const scroller = { scrollTop: 0, scrollHeight: 100, clientHeight: 100 };
    // No scrolling is possible in this single-row fixture.
    Object.defineProperty(scroller, "scrollTop", { get: () => 0, set: () => {} });
    const assetURL = "https://photos.google.com/photo/fixture";
    const result = { ...node("Video"), href: assetURL, parentElement: null };
    const info = node(options.german ? "Info öffnen" : "Info", () => { events.push("info"); stage = "info"; });
    const trashLabel = options.german ? "In den Papierkorb verschieben" : "Move to trash";
    const trash = node(trashLabel, () => { clicks++; events.push("trash"); stage = options.retention ? "notice" : "dialog"; });
    const confirm = node(trashLabel, () => {
        clicks++; events.push("confirm"); confirmed = !options.noSuccess; stage = "done";
    });
    const notice = node("OK, weiter", () => { clicks++; events.push("notice"); stage = "dialog"; });
    const dialog = { ...node("dialog"),
        get innerText() { return stage === "notice" ?
            "Elemente im Papierkorb können jetzt nur noch 30 Tage lang wiederhergestellt werden" : "Move to trash"; },
        querySelectorAll: () => stage === "notice" ? [notice] : [confirm] };
    const document = {
        readyState: "complete", scrollingElement: scroller,
        body: { get innerText() { return confirmed ? "Moved to trash" : ""; } },
        querySelector(selector) {
            if (selector === 'a[href*="/photo/"]') return stage === "search" && !options.empty ? result : null;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === '[role="progressbar"]')
                return options.hiddenProgress ? [{...node("Loading"), closest: () => ({})}] : [];
            if (selector === "iframe")
                return options.iframe ? [{...node("player"), src: "https://youtube.googleapis.com/embed/", title: "YouTube Google Fotos-Videoplayer"}] : [];
            if (selector === 'input[role="combobox"],input[role="searchbox"]')
                return [{value: '"fixture.mp4"'}];
            if (selector === "[aria-label]")
                return [node((options.german ? "Google-Konto" : "Google Account") + ": Person (" + (options.wrongAccount ? "notyou@example.com" : "you@example.com") + ")")];
            if (selector === 'a[href*="/photo/"]')
                return options.empty ? [] : options.multiple ? [result, { ...result, href: assetURL + "2" }] :
                    options.duplicate ? [result, {...result, href: "https://photos.google.com/search/token/top/photo/fixture"}] : [result];
            if (selector === 'button,[role="button"]')
                return stage === "detail" ? [info, trash] :
                    stage === "info" ? [trash] : stage === "dialog" ? [trash, confirm] : [];
            if (selector === '[role="dialog"]') return stage === "dialog" || stage === "notice" ? [dialog] : [];
            if (selector === "body *")
                return stage === "search" && options.empty ? [node("Keine Ergebnisse")] :
                    stage === "info" ? [node(options.wrongFilename ? "different.mp4" : "fixture.mp4")] : [];
            if (selector === "video") return options.notVideo || options.iframe ? [] : [node("video")];
            if (selector === '[role="status"],[role="alert"],[aria-live]')
                return confirmed ? [node(options.german ? "Foto in den Papierkorb verschoben" : "Moved to trash")] : [];
            throw Error("Unhandled fixture selector: " + selector);
        },
    };
    const tab = {
        id: () => "123",
        get url() { return currentURL; },
        set url(url) {
            stage = url.includes("/photo/") ? (options.infoOpen ? "info" : "detail") : "search";
            currentURL = options.opaque && stage === "search" ? "https://photos.google.com/search/opaque-token" : url;
        },
        loading: () => false,
        execute({ javascript }) {
            return vm.runInNewContext(javascript, {
                document, location: { href: currentURL, pathname: new URL(currentURL).pathname },
                getComputedStyle: () => ({ visibility: "visible", overflowY: "visible" }),
            });
        },
    };
    // JXA properties are functions when read and setters when assigned.
    const jxaTab = {
        id: tab.id, loading: tab.loading, execute: tab.execute,
        get url() { return () => currentURL; },
        set url(url) { tab.url = url; },
    };
    let running = !options.closedChrome;
    const tabs = options.noTab || options.closedChrome ? [] : options.multipleTabs ? [jxaTab, jxaTab] : [jxaTab];
    const window = { mode: () => "normal", tabs: () => tabs };
    window.tabs.push = tab => { tabs.push(tab); events.push("open-tab"); };
    const windows = options.closedChrome ? [] : [window];
    const chrome = {
        running: () => running,
        launch: () => { running = true; events.push("launch"); },
        activate: () => events.push("activate"),
        Tab: () => jxaTab,
        Window: () => window,
        windows: () => windows,
    };
    chrome.windows.push = w => { windows.push(w); events.push("open-window"); };
    function helper(args) {
        const commandIndex = args.indexOf("google-photos-cleanup") + 1;
        const command = args[commandIndex];
        events.push(command);
        if (command === "next") {
            if (checkpoint === "pending") checkpoint = "in_progress";
            return { key: "abc", filename: "fixture.mp4", status: checkpoint };
        }
        if (command === "intent") checkpoint = "needs_review";
        if (command === "result") checkpoint = args[args.indexOf("--status") + 1];
        return { key: "abc", status: checkpoint };
    }
    const foundation = {
        NSTask: { alloc: { get init() {
            return {
                terminationStatus: 0,
                get launch() { output = JSON.stringify(helper(this.arguments)); },
                get waitUntilExit() {},
            };
        } } },
        NSURL: { fileURLWithPath: x => x },
        NSPipe: { get pipe() {
            return { fileHandleForReading: { get readDataToEndOfFile() { return output; } } };
        } },
        NSFileHandle: { fileHandleWithStandardError: {} },
        NSString: { alloc: { initWithDataEncoding: x => x } },
        NSUTF8StringEncoding: 4,
    };
    const context = {
        ObjC: { import() {}, unwrap: x => x }, $: foundation,
        Application: () => chrome, delay: seconds => { clock += seconds * 1000; },
        Date: { now: () => clock }, console: { log() {} },
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    return {
        run: extra => context.run(["/repo", "/docker", "--account", "you@example.com", "--limit", "1", ...(extra || [])]),
        get checkpoint() { return checkpoint; },
        get clicks() { return clicks; },
        events,
    };
}

test("automated deletion persists intent before clicking and records confirmed success", () => {
    const f = fixture();
    f.run();
    assert.equal(f.checkpoint, "deleted");
    assert.equal(f.clicks, 2);
    assert.ok(f.events.indexOf("intent") < f.events.indexOf("trash"));
});

for (const option of ["noTab", "closedChrome"]) {
    test(option + " opens Google Photos before checking the account", () => {
        const f = fixture({ [option]: true });
        assert.match(f.run(["--check"]), /account verified/);
        assert.ok(f.events.includes("open-tab"));
        assert.equal(f.clicks, 0);
        assert.equal(f.checkpoint, "pending");
    });
}

test("multiple Photos tabs stop before opening a tab or claiming a candidate", () => {
    const f = fixture({ multipleTabs: true });
    assert.throws(() => f.run(), /Found 2 Google Photos tabs/);
    assert.deepEqual(f.events, []);
});

test("dry run verifies without Trash clicks or completed checkpoint", () => {
    const f = fixture();
    assert.match(f.run(["--dry-run"]), /Dry run verified/);
    assert.equal(f.clicks, 0);
    assert.equal(f.checkpoint, "in_progress");
});

test("wrong account fails before claiming a candidate", () => {
    const f = fixture({ wrongAccount: true });
    assert.throws(() => f.run(), /Expected Google account/);
    assert.equal(f.clicks, 0);
    assert.deepEqual(f.events, []);
});

for (const option of ["wrongFilename", "notVideo"]) {
    test(option + " prevents Trash actions", () => {
        const f = fixture({ [option]: true });
        assert.throws(() => f.run(), /Exact filename and video/);
        assert.equal(f.clicks, 0);
        assert.equal(f.checkpoint, "error");
    });
}

test("multiple results are skipped without deletion", () => {
    const f = fixture({ multiple: true });
    f.run();
    assert.equal(f.clicks, 0);
    assert.equal(f.checkpoint, "kept_ambiguous");
});

test("missing explicit success leaves a review checkpoint", () => {
    const f = fixture({ noSuccess: true });
    assert.throws(() => f.run(), /could not be confirmed/);
    assert.equal(f.checkpoint, "needs_review");
    assert.equal(f.clicks, 2);
});

test("duplicate search links identify one video", () => {
    const f = fixture({ duplicate: true });
    f.run();
    assert.equal(f.checkpoint, "deleted");
});

test("hidden progress indicators do not block loaded results", () => {
    const f = fixture({ hiddenProgress: true });
    f.run();
    assert.equal(f.checkpoint, "deleted");
});

test("German controls and the embedded Photos player are supported", () => {
    const f = fixture({ german: true, iframe: true });
    f.run();
    assert.equal(f.checkpoint, "deleted");
});

test("the observed retention notice is handled before the Trash confirmation", () => {
    const f = fixture({ german: true, retention: true });
    f.run();
    assert.equal(f.checkpoint, "deleted");
    assert.ok(f.events.indexOf("notice") < f.events.indexOf("confirm"));
});

test("an already open Info panel is preserved", () => {
    const f = fixture({ infoOpen: true });
    f.run();
    assert.equal(f.checkpoint, "deleted");
    assert.ok(!f.events.includes("info"));
});

test("canonical opaque search URLs are accepted when the query matches", () => {
    const f = fixture({ opaque: true });
    f.run();
    assert.equal(f.checkpoint, "deleted");
});

test("explicit stable empty results are skipped without Trash actions", () => {
    const f = fixture({ empty: true });
    f.run();
    assert.equal(f.checkpoint, "kept_not_found");
    assert.equal(f.clicks, 0);
});
