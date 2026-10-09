/* Offline chat transport and full-page interaction regressions.
 * Run: node --test scripts/tests/test_chat_flow.js */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const project = path.resolve(__dirname, "../..");
const shared = fs.readFileSync(path.join(project, "app/static/js/app.js"), "utf8");
const template = fs.readFileSync(path.join(project, "app/templates/chat/index.html"), "utf8");
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

// Minimal DOM surface used by this page, with real event callbacks and request races.
class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.attributes = {}; this.style = {}; this.value = ""; this.hidden = true;
    this.scrollTop = 0; this.scrollHeight = 1000; this.clientHeight = 300;
    this.className = ""; this._html = "";
    this.classList = { toggle: (name, active) => {
      const names = new Set(this.className.split(/\s+/).filter(Boolean));
      if (active) names.add(name); else names.delete(name);
      this.className = [...names].join(" ");
    } };
  }
  set innerHTML(value) {
    this._html = value; this.children = [];
    // Materialize the child elements the page subsequently looks up.
    for (const match of value.matchAll(/<(div|span|code)\b([^>]*)>/g)) {
      const child = new Element(match[1]);
      for (const attr of match[2].matchAll(/([\w-]+)="([^"]*)"/g)) child.setAttribute(attr[1], attr[2]);
      this.appendChild(child);
    }
  }
  get innerHTML() { return this._html + this.children.map(child => child._html).join(""); }
  set textContent(value) { this._html = String(value); this.children = []; }
  get textContent() { return this._html.replace(/<[^>]*>/g, "") + this.children.map(child => child.textContent).join(""); }
  get firstChild() { return this.children[0] || null; }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === "class") this.className = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  appendChild(child) { return this.insertBefore(child, null); }
  insertBefore(child, before) {
    if (child.tagName === "FRAGMENT") { [...child.children].forEach(item => this.appendChild(item)); return child; }
    child.remove();
    const index = before ? this.children.indexOf(before) : this.children.length;
    this.children.splice(index < 0 ? this.children.length : index, 0, child);
    child.parentElement = this; return child;
  }
  remove() {
    if (!this.parentElement) return;
    const parent = this.parentElement;
    parent.children.splice(parent.children.indexOf(this), 1); this.parentElement = null;
  }
  matches(selector) {
    if (selector.startsWith(".")) return selector.slice(1).split(".").every(name => this.className.split(/\s+/).includes(name));
    const attr = selector.match(/^\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
    if (attr) return attr[2] === undefined ? this.getAttribute(attr[1]) !== null : this.getAttribute(attr[1]) === attr[2];
    return this.tagName.toLowerCase() === selector;
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  emit(type, extra = {}) {
    const event = { target: this, preventDefault() { this.prevented = true; }, stopPropagation() {}, ...extra };
    for (const callback of this.listeners[type] || []) callback(event);
    return event;
  }
  focus() {}
  scrollIntoView() {}
}

function page({ chat = false, response, search = "" } = {}) {
  const elements = new Map([...template.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const document = {
    documentElement: new Element("html"), addEventListener() {},
    querySelector: () => null, querySelectorAll: () => [],
    getElementById: id => elements.get(id), createElement: tag => new Element(tag),
    createDocumentFragment: () => new Element("fragment"),
  };
  const requests = [], streams = [], toasts = [], timers = new Map();
  let timerId = 0, renderCount = 0;
  const context = vm.createContext({
    window: { location: { search } }, document, TextDecoder, URLSearchParams,
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch: () => Promise.resolve(response),
  });
  vm.runInContext(shared, context);
  const window = context.window;
  if (chat) {
    window.toast = (...args) => toasts.push(args);
    window.confirm = () => true;
    window.api = Object.fromEntries(["get", "post"].map(method => [method, (url, body) => {
      const request = { method, url, body, ...deferred() }; requests.push(request); return request.promise;
    }]));
    window.streamSSE = (url, body, handlers) => {
      const stream = { url, body, handlers, ...deferred() }; streams.push(stream); return stream.promise;
    };
    const markdown = window.mdLite;
    window.mdLite = value => { renderCount++; return markdown(value); };
    vm.runInContext(script, context);
  }
  return {
    window, elements, requests, streams, toasts, timers,
    get renderCount() { return renderCount; },
    tick() { const ready = [...timers.values()]; timers.clear(); ready.forEach(callback => callback()); },
    resolve(url, data) { const request = requests.find(item => item.url === url && !item.resolved); assert.ok(request, url); request.resolved = true; request.resolve({ ok: true, data }); },
  };
}

function streamingResponse(chunks, extra = {}) {
  let reads = 0, canceled = 0, released = 0;
  const reader = {
    read: () => { reads++; return Promise.resolve(chunks.length ? { value: chunks.shift(), done: false } : { done: true }); },
    cancel: () => { canceled++; return Promise.resolve(); }, releaseLock: () => { released++; },
  };
  return {
    ok: true, headers: { get: () => "text/event-stream; charset=utf-8" },
    body: { getReader: () => reader }, ...extra,
    get reads() { return reads; }, get canceled() { return canceled; }, get released() { return released; },
  };
}

test("SSE handles UTF-8 and CRLF split at every byte, multiline data, heartbeats, and done without EOF", async () => {
  const wire = ": ping\r\nevent: delta\r\ndata: 你好\r\ndata: 第二行\r\n\r\nevent: delta\r\ndata:\r\ndata:\r\n\r\nevent: done\r\ndata: 完成\r\n\r\n";
  const chunks = [...Buffer.from(wire)].map(byte => Uint8Array.of(byte));
  const response = streamingResponse(chunks);
  const events = []; let done = 0;
  await page({ response }).window.streamSSE("/chat/api/send", {}, {
    onEvent: (...args) => events.push(args), onDone: () => done++,
  });
  assert.deepEqual(events, [["delta", "你好\n第二行"], ["delta", "\n"], ["done", "完成"]]);
  assert.equal(done, 1); assert.equal(response.canceled, 1); assert.equal(response.released, 1);
  assert.ok(response.reads <= Buffer.byteLength(wire));
});

test("SSE accepts CR-only line endings and one final event without a blank separator", async () => {
  const events = [];
  const response = streamingResponse([Buffer.from("data: text\r\revent: done\rdata: final")]);
  await page({ response }).window.streamSSE("/chat/api/send", {}, { onDelta: value => events.push(value), onEvent: (...args) => events.push(args) });
  assert.deepEqual(events, ["text", ["done", "final"]]);
});

test("SSE rejects premature EOF while preserving delivered partial content", async () => {
  const events = []; let done = false;
  const response = streamingResponse([Buffer.from("event: delta\ndata: 部分回答\n\n")]);
  await assert.rejects(page({ response }).window.streamSSE("/chat/api/send", {}, {
    onEvent: (...args) => events.push(args), onDone: () => { done = true; },
  }), /连接提前结束/);
  assert.deepEqual(events, [["delta", "部分回答"]]); assert.equal(done, false); assert.equal(response.released, 1);
});

test("SSE rejects a login page and reports structured HTTP errors", async () => {
  await assert.rejects(page({ response: streamingResponse([], { headers: { get: () => "text/html" } }) }).window.streamSSE("/send", {}), /登录/);
  const response = { ok: false, status: 404, text: () => Promise.resolve('{"error":"会话不存在"}') };
  await assert.rejects(page({ response }).window.streamSSE("/send", {}), /会话不存在/);
});

async function selectedPage() {
  const result = page({ chat: true });
  result.resolve("/chat/api/conversations", [{ id: 1, title: "会话一" }, { id: 2, title: "会话二" }]);
  await flush();
  result.resolve("/chat/api/messages/1", []);
  await flush();
  return result;
}

test("conversation links select an available conversation and fall back when unavailable", async () => {
  for (const [search, expectedId] of [["?conversation=2", 2], ["?conversation=999", 1]]) {
    const result = page({ chat: true, search });
    result.resolve("/chat/api/conversations", [{ id: 1, title: "会话一" }, { id: 2, title: "会话二" }]);
    await flush();
    const histories = result.requests.filter(item => item.url.includes("/messages/"));
    assert.equal(histories.length, 1);
    assert.equal(histories[0].url, `/chat/api/messages/${expectedId}`);
    result.resolve(histories[0].url, [{ role: "user", content: "关联会话历史" }]);
    await flush();
    assert.match(result.elements.get("msg-list").textContent, /关联会话历史/);
  }
});

test("full chat script blocks send until history is loaded and ignores a stale same-conversation response", async () => {
  const result = page({ chat: true });
  result.resolve("/chat/api/conversations", [{ id: 1, title: "会话" }]); await flush();
  const input = result.elements.get("chat-input"); input.value = "草稿";
  result.elements.get("btn-send").emit("click");
  assert.equal(result.streams.length, 0); assert.equal(input.value, "草稿");
  const list = result.elements.get("conv-list");
  list.emit("click", { target: list.firstChild });
  const histories = result.requests.filter(item => item.url === "/chat/api/messages/1");
  histories[1].resolve({ ok: true, data: [{ role: "user", content: "最新历史" }] }); await flush();
  histories[0].resolve({ ok: true, data: [{ role: "user", content: "过期历史" }] }); await flush();
  assert.match(result.elements.get("msg-list").textContent, /最新历史/);
  assert.doesNotMatch(result.elements.get("msg-list").textContent, /过期历史/);
  assert.equal(result.elements.get("btn-send").disabled, false);
});

test("Shift+Enter and IME confirmation do not send; plain Enter sends exactly once", async () => {
  const result = await selectedPage(); const input = result.elements.get("chat-input"); input.value = "第一行\n第二行";
  for (const extra of [{ shiftKey: true }, { isComposing: true }, { keyCode: 229 }]) {
    assert.equal(input.emit("keydown", { key: "Enter", ...extra }).prevented, undefined);
  }
  assert.equal(result.streams.length, 0);
  input.emit("keydown", { key: "Enter" }); input.emit("keydown", { key: "Enter" });
  assert.equal(result.streams.length, 1); assert.equal(result.streams[0].body.message, "第一行\n第二行");
});

test("stream batches many tokens, respects scroll position, and retains partial content on error", async () => {
  const result = await selectedPage(); const input = result.elements.get("chat-input"); input.value = "测试";
  result.elements.get("btn-send").emit("click");
  const stream = result.streams[0]; const messages = result.elements.get("msg-list");
  messages.scrollTop = 10; messages.emit("scroll");
  for (let i = 0; i < 100; i++) stream.handlers.onEvent("delta", "字");
  assert.equal(result.renderCount, 0); assert.equal(result.timers.size, 1);
  result.tick(); assert.equal(result.renderCount, 1); assert.equal(messages.scrollTop, 10);
  stream.handlers.onEvent("tool", '{"name":"查询","result":"完成","ok":true}');
  assert.ok(messages.children.indexOf(messages.querySelector(".tool-row")) < messages.children.indexOf(messages.querySelector(".assistant")));
  stream.handlers.onEvent("error", "上游断开"); stream.handlers.onEvent("done", ""); stream.handlers.onDone();
  assert.match(messages.querySelector(".assistant").textContent, /字{100}/);
  assert.match(messages.querySelector(".chat-reply-error").textContent, /上游断开/);
  assert.equal(result.elements.get("btn-send").disabled, false); assert.equal(input.value, "");
});

test("final authoritative content survives a scheduled render and only gains one speak button", async () => {
  const result = await selectedPage(); result.elements.get("chat-input").value = "测试";
  result.elements.get("btn-send").emit("click"); const stream = result.streams[0];
  stream.handlers.onDelta("草稿"); stream.handlers.onEvent("done", "最终回复"); stream.handlers.onDone(); result.tick();
  const bubble = result.elements.get("msg-list").querySelector(".assistant").querySelector(".msg-bubble");
  assert.match(bubble.textContent, /最终回复/); assert.doesNotMatch(bubble.textContent, /草稿/);
  assert.equal(bubble.querySelectorAll(".btn-speak").length, 1);
});

test("failed pre-acceptance send restores the draft without overwriting newly typed text", async () => {
  for (const newDraft of ["", "下一条草稿"]) {
    const result = await selectedPage(); const input = result.elements.get("chat-input"); input.value = "原问题";
    result.elements.get("btn-send").emit("click"); input.value = newDraft;
    result.streams[0].reject(new Error("会话不存在")); await flush();
    assert.equal(input.value, newDraft || "原问题"); assert.equal(result.elements.get("btn-send").disabled, false);
  }
});

test("failed conversation creation restores text and ignores a late initial list", async () => {
  const result = page({ chat: true }); const input = result.elements.get("chat-input"); input.value = "不要丢掉";
  result.elements.get("btn-send").emit("click");
  result.resolve("/chat/api/conversations", [{ id: 99, title: "迟到" }]); await flush();
  assert.equal(result.requests.filter(item => item.url.includes("/messages/")).length, 0);
  result.requests.find(item => item.url === "/chat/api/new").reject(new Error("offline")); await flush();
  assert.equal(input.value, "不要丢掉"); assert.equal(result.elements.get("btn-send").disabled, false);
});

test("duplicate new-conversation clicks create one request and retain text typed while waiting", async () => {
  const result = await selectedPage(); const button = result.elements.get("btn-new-chat");
  button.emit("click"); button.emit("click");
  assert.equal(result.requests.filter(item => item.url === "/chat/api/new").length, 1);
  result.elements.get("chat-input").value = "等待时的新草稿";
  result.resolve("/chat/api/new", { id: 3 }); await flush();
  assert.equal(result.elements.get("chat-input").value, "等待时的新草稿");
  assert.equal(button.disabled, false);
});
