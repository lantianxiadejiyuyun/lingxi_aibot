/* 灵犀 前端公共工具 */
(function () {
  "use strict";

  /* ---------- Toast ---------- */
  window.toast = function (msg, type) {
    var box = document.getElementById("toast-container");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast " + (type || "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 3200);
  };

  /* ---------- Flash 自动消失 ---------- */
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".flash-area .alert").forEach(function (a) {
      setTimeout(function () { a.remove(); }, 4000);
    });
  });

  /* ---------- 确认表单 ---------- */
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.matches("form[data-confirm]") && !window.confirm(f.getAttribute("data-confirm"))) {
      e.preventDefault();
    }
  });

  /* ---------- 弹窗 ---------- */
  document.addEventListener("click", function (e) {
    var opener = e.target.closest("[data-modal-open]");
    if (opener) {
      var modal = document.querySelector(opener.getAttribute("data-modal-open"));
      if (modal) modal.classList.add("open");
      return;
    }
    if (e.target.closest("[data-modal-close]") || e.target.classList.contains("modal-mask")) {
      e.target.closest(".modal-mask") && e.target.closest(".modal-mask").classList.remove("open");
      var c = e.target.closest("[data-modal-close]");
      if (c) c.closest(".modal-mask").classList.remove("open");
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      document.querySelectorAll(".modal-mask.open").forEach(function (m) { m.classList.remove("open"); });
    }
  });

  /* ---------- Tabs ---------- */
  document.addEventListener("click", function (e) {
    var tab = e.target.closest(".tab");
    if (!tab) return;
    var tabs = tab.parentElement;
    var key = tab.getAttribute("data-tab");
    tabs.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
    tab.classList.add("active");
    var scope = tabs.closest("[data-tabs-scope]") || document;
    scope.querySelectorAll(".tab-panel").forEach(function (p) {
      p.classList.toggle("active", p.getAttribute("data-panel") === key);
    });
  });

  /* ---------- JSON API ---------- */
  window.api = {
    post: function (url, data) {
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data || {}),
      }).then(function (r) { return r.json(); });
    },
    get: function (url) {
      return fetch(url).then(function (r) { return r.json(); });
    },
  };

  /* ---------- SSE 流式（fetch + POST）---------- */
  window.streamSSE = function (url, body, handlers) {
    handlers = handlers || {};
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.text().then(function (t) {
          throw new Error(t || ("HTTP " + resp.status));
        });
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) {
            handlers.onDone && handlers.onDone();
            return;
          }
          buffer += decoder.decode(r.value, { stream: true });
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (raw) {
            var evt = null, data = [];
            raw.split("\n").forEach(function (line) {
              if (line.startsWith("event:")) evt = line.slice(6).trim();
              else if (line.startsWith("data:")) data.push(line.slice(5).trim());
            });
            if (!data.length) return;
            var payload = data.join("\n");
            if (evt) handlers.onEvent && handlers.onEvent(evt, payload);
            else handlers.onDelta && handlers.onDelta(payload);
          });
          return pump();
        });
      }
      return pump();
    });
  };

  /* ---------- Markdown-lite 渲染 ---------- */
  window.escapeHtml = function (s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  window.mdLite = function (src) {
    var text = String(src == null ? "" : src);
    // 提取代码块
    var codes = [];
    text = text.replace(/```([\s\S]*?)```/g, function (m, code) {
      codes.push("<pre><code>" + window.escapeHtml(code.replace(/^\n/, "")) + "</code></pre>");
      return "\u0000CODE" + (codes.length - 1) + "\u0000";
    });
    var html = window.escapeHtml(text);
    // 行内代码
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    // 标题
    html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>")
               .replace(/^## (.+)$/gm, "<h2>$1</h2>")
               .replace(/^# (.+)$/gm, "<h1>$1</h1>");
    // 加粗
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    // 无序列表
    html = html.replace(/^[-*] (.+)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
    // 有序列表
    html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
    // 段落与换行
    html = html.replace(/\n{2,}/g, "</p><p>");
    html = html.replace(/\n/g, "<br>");
    html = "<p>" + html + "</p>";
    // 回填代码块
    html = html.replace(/\u0000CODE(\d+)\u0000/g, function (m, i) { return codes[+i]; });
    // 图片：Markdown 语法 ![...](...) 与 行内/单独成行的图片 URL（.png/.jpg/.webp/.gif）
    html = html.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img class="md-img" src="$2" alt="$1" loading="lazy">');
    html = html.replace(/(^|[\s(（【:：])(https?:\/\/[^\s<>\u4e00-\u9fff。，！？；、）"'，]+\.(?:png|jpe?g|webp|gif)(?:\?[^\s<>\u4e00-\u9fff。，！？；、）"'，]+)?)/g,
      '$1<img class="md-img" src="$2" alt="图片" loading="lazy">');
    // 链接：Markdown [文本](http...) 与 裸 URL（排除中文字符与结束标点，支持中文冒号前缀）
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    html = html.replace(/(^|[\s(（【:：])(https?:\/\/[^\s<>\u4e00-\u9fff。，！？；、）"'，]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    // 清理空段落
    html = html.replace(/<p><\/p>/g, "");
    return html;
  };
})();
