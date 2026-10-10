/* Calendar report rendering and navigation. Run: node --test scripts/tests/test_calendar_reports.js */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const project = path.resolve(__dirname, "../..");
const template = fs.readFileSync(path.join(project, "app/templates/calendar/index.html"), "utf8");
const shared = fs.readFileSync(path.join(project, "app/static/js/app.js"), "utf8");
const source = template.match(/<script>([\s\S]*?)<\/script>/)[1];

function element() {
  const classes = new Set();
  return {
    innerHTML: "", textContent: "", value: "", style: {}, listeners: {},
    classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value) },
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
    emit(name) { (this.listeners[name] || []).forEach(callback => callback({ target: this, preventDefault() {} })); },
    getAttribute() { return null; }, closest() { return null; }, reset() {},
  };
}

function browser(events) {
  const elements = new Map([...template.matchAll(/id="([^"]+)"/g)].map(match => [match[1], element()]));
  const listeners = {}, requests = [];
  let nextEvents = events;
  const document = {
    documentElement: { getAttribute: name => name === "data-script-root" ? "/proxy/lingxi" : "" },
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    querySelector: selector => selector.startsWith("#") ? elements.get(selector.slice(1)) : null,
    querySelectorAll: () => [], getElementById: id => elements.get(id),
  };
  const context = vm.createContext({
    window: {}, document, setTimeout,
    fetch: async url => {
      requests.push(url);
      return { json: async () => ({ ok: true, data: { events: nextEvents } }) };
    },
  });
  vm.runInContext(shared, context);
  vm.runInContext(source.replace(/var CAL = \{[\s\S]*?\};/, "var CAL = " + JSON.stringify({
    year: 2026, month: 10, day: "2026-10-10", today: "2026-10-10", events,
  }) + ";"), context);
  return {
    elements, requests,
    select(date) {
      const cell = element();
      cell.getAttribute = name => name === "data-date" ? date : null;
      cell.closest = selector => selector === ".cal-cell" ? cell : null;
      (listeners.click || []).forEach(callback => callback({ target: cell }));
    },
    async month(button, response) {
      nextEvents = response;
      elements.get(button).emit("click");
      for (let index = 0; index < 10; index++) await Promise.resolve();
    },
  };
}

function report(date = "2026-10-10") {
  return {
    id: "daily-report-1", kind: "daily_report", title: "日报 " + date, date, start: date,
    end: date.slice(0, 8) + "11", all_day: true, readonly: true,
    sections: [
      { kind: "morning", label: "早安简报", content: "第一行\n<script>alert(1)</script>", generated_at: date + " 08:00", conversation_url: "/proxy/lingxi/chat/?conversation=12" },
      { kind: "noon", label: "午间简报", content: "午间进度", generated_at: date + " 12:00" },
      { kind: "evening", label: "晚间复盘", content: "晚间总结", generated_at: date + " 21:00" },
    ],
  };
}

test("one daily report shows all generated sections, escaped content and a prefixed conversation link", () => {
  const page = browser([report()]);
  page.select("2026-10-10");
  const html = page.elements.get("day-list").innerHTML;
  assert.equal((html.match(/class="day-list-item day-report"/g) || []).length, 1);
  assert.equal((html.match(/<details class="report-section">/g) || []).length, 3);
  assert.match(html, /早安简报/); assert.match(html, /午间简报/); assert.match(html, /晚间复盘/);
  assert.match(html, /第一行\n&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /href="\/proxy\/lingxi\/chat\/\?conversation=12"/);
  assert.doesNotMatch(html, /<script|data-modal-open|编辑|删除/);
  assert.equal(page.elements.get("event-modal").classList.contains("open"), false);
  page.select("2026-10-11");
  assert.match(page.elements.get("day-list").innerHTML, /当天没有日程或日报/);
});

test("changing months preserves report styling, one-day placement and the API mount prefix", async () => {
  const page = browser([]);
  await page.month("btn-next", [report("2026-11-10")]);
  assert.equal(page.requests[0], "/proxy/lingxi/calendar/api/events?year=2026&month=11");
  const grid = page.elements.get("calendar-grid").innerHTML;
  assert.equal((grid.match(/data-kind="daily_report"/g) || []).length, 1);
  assert.match(grid, /class="cal-event cal-event-report"/);
  page.select("2026-11-10");
  assert.match(page.elements.get("day-list").innerHTML, /日报 2026-11-10/);
  assert.doesNotMatch(page.elements.get("day-list").innerHTML, /data-modal-open/);
});

test("ordinary events retain editing alongside a report, while missing briefing sections stay absent", () => {
  const item = report(); item.sections = item.sections.slice(0, 1);
  const page = browser([item, {
    id: 4, title: "会议", date: "2026-10-10", start: "2026-10-10 10:00", end: "2026-10-10 11:00", all_day: false,
  }]);
  page.select("2026-10-10");
  const html = page.elements.get("day-list").innerHTML;
  assert.equal((html.match(/<details class="report-section">/g) || []).length, 1);
  assert.equal((html.match(/data-modal-open/g) || []).length, 1);
  assert.match(html, /data-id="4"/);
  assert.doesNotMatch(html, /午间简报|晚间复盘/);
});

test("report links cannot navigate to scripts or external hosts", () => {
  for (const url of ["javascript:alert(1)", "//evil.example/chat", "/\\evil.example/chat", "https://evil.example/chat"]) {
    const item = report(); item.sections = [{ label: "早安简报", content: "内容", conversation_url: url }];
    const page = browser([item]); page.select("2026-10-10");
    assert.doesNotMatch(page.elements.get("day-list").innerHTML, /href=/);
  }
});

test("a report without structured sections still offers its complete escaped text", () => {
  const item = report(); item.sections = []; item.description = "旧日报\n<strong>完整内容</strong>";
  const page = browser([item]); page.select("2026-10-10");
  assert.match(page.elements.get("day-list").innerHTML, /旧日报\n&lt;strong&gt;完整内容&lt;\/strong&gt;/);
  assert.doesNotMatch(page.elements.get("day-list").innerHTML, /data-modal-open/);
});
