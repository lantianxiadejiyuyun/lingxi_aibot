/* Offline browser-contract regression tests. Run: node --test scripts/tests/test_admin_entry.js */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const project = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(project, "app/static/js/app.js"), "utf8");

function browser(scriptRoot) {
  const requests = [];
  const document = {
    documentElement: { getAttribute: name => name === "data-script-root" ? scriptRoot : "" },
    addEventListener() {},
    querySelector: () => ({ getAttribute: () => "test-csrf" }),
  };
  const context = vm.createContext({
    window: {}, document, TextDecoder, setTimeout,
    fetch(url, options = {}) {
      requests.push({ url, options });
      const chunks = [Buffer.from("event: delta\ndata: hello"), Buffer.from("\n\nevent: done\ndata: hello\n\n")];
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true, data: [] }),
        body: { getReader: () => ({ read: () => Promise.resolve(chunks.length ?
          { value: chunks.shift(), done: false } : { done: true }) }) },
      });
    },
  });
  vm.runInContext(source, context);
  return { context, window: context.window, requests };
}

for (const root of ["", "/secret", "/proxy/secret", "/chat", "/tasks", "/settings"]) {
  test(`JSON and SSE requests work under ${root || "/"}`, async () => {
    const page = browser(root);
    // These modules all use the shared API helper; cover their actual endpoint families.
    for (const module of ["tasks", "calendar", "notes", "fitness", "travel", "expenses",
      "pages", "images", "skills", "memory", "settings"]) {
      await page.window.api.post(`/${module}/api/action`, { id: 7 });
      const request = page.requests.at(-1);
      assert.equal(request.url, `${root}/${module}/api/action`);
      assert.equal(request.options.headers["X-CSRFToken"], "test-csrf");
      assert.equal(request.options.method, "POST");
      assert.equal(request.options.body, '{"id":7}');
    }
    await page.window.api.get("/chat/api/conversations?limit=20");
    assert.equal(page.requests.at(-1).url, `${root}/chat/api/conversations?limit=20`);
    const events = [];
    let completed = false;
    await page.window.streamSSE("/chat/api/send", { message: "hello" }, {
      onEvent: (event, data) => events.push([event, data]), onDone: () => { completed = true; },
    });
    assert.equal(page.requests.at(-1).url, `${root}/chat/api/send`);
    assert.deepEqual(events, [["delta", "hello"], ["done", "hello"]]);
    assert.equal(completed, true);
  });
}

test("URL resolution preserves query strings, relative and external URLs", () => {
  const { window } = browser("/secret/");
  assert.equal(window.adminUrl("/chat/api/send?x=1#reply"), "/secret/chat/api/send?x=1#reply");
  assert.equal(window.adminUrl("/secretary/api"), "/secret/secretary/api");
  for (const url of ["https://example.com/api", "//example.com/api", "api/send", "?tab=ai", "#reply"]) {
    assert.equal(window.adminUrl(url), url);
  }
});

test("chat speech request uses the same scoped address", async () => {
  const page = browser("/secret");
  const template = fs.readFileSync(path.join(project, "app/templates/chat/index.html"), "utf8");
  const speechFunction = template.slice(template.indexOf("  function speakText(text) {"), template.indexOf("  function attachSpeak(bubble) {"));
  page.context.speakBrowser = () => {};
  page.context.fetch = (url, options) => {
    page.requests.push({ url, options });
    return Promise.resolve({ ok: false }); // Avoid audio playback; test only the HTTP contract.
  };
  vm.runInContext(speechFunction + '\nspeakText("hello");', page.context);
  await Promise.resolve();
  assert.equal(page.requests.at(-1).url, "/secret/chat/api/tts");
  assert.equal(page.requests.at(-1).options.body, '{"text":"hello"}');
});

test("Feishu status polling goes through the scoped GET helper", async () => {
  const page = browser("/secret");
  const template = fs.readFileSync(path.join(project, "app/templates/settings/index.html"), "utf8");
  const poll = template.match(/\(function pollFeishuWs\(\) \{[\s\S]*?\}\)\(\);/)[0];
  page.context.document.getElementById = () => ({ innerHTML: "" });
  page.context.document.querySelector = () => ({ checked: true, getAttribute: () => "test-csrf" });
  vm.runInContext(poll, page.context);
  await Promise.resolve();
  assert.equal(page.requests.at(-1).url, "/secret/settings/api/feishu-ws-status");
  assert.doesNotMatch(template, /fetch\(["']\/settings\//);
});

test("shared helpers load before inline page scripts, and setup stays publicly accessible", () => {
  const base = fs.readFileSync(path.join(project, "app/templates/base.html"), "utf8");
  assert.match(base, /data-script-root="\{\{ request\.script_root \}\}"/);
  const sharedScript = base.match(/<script src="[^\n]+js\/app\.js[^\n]+<\/script>/)[0];
  assert.doesNotMatch(sharedScript, /\bdefer\b/);
  assert.ok(base.indexOf(sharedScript) < base.indexOf("{% block scripts %}"));
  const setup = fs.readFileSync(path.join(project, "app/templates/setup/index.html"), "utf8");
  assert.match(setup, /return fetch\(url,/);
  assert.match(setup, /post\("\/setup\/api\/verify-token"/);
});
