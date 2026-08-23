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
    var scope = tab.closest("[data-tabs-scope]") || document;
    var key = tab.getAttribute("data-tab");
    scope.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
    tab.classList.add("active");
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
      }).then(function (r) {
        return r.json().catch(function () {
          throw new Error("HTTP " + r.status + (r.status === 401 ? "（登录已失效，请重新登录）" : ""));
        });
      });
    },
    get: function (url) {
      return fetch(url).then(function (r) {
        return r.json().catch(function () {
          throw new Error("HTTP " + r.status + (r.status === 401 ? "（登录已失效，请重新登录）" : ""));
        });
      });
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
            buffer += decoder.decode();  // 冲刷多字节残余
            processBuffer(true);
            handlers.onDone && handlers.onDone();
            return;
          }
          buffer += decoder.decode(r.value, { stream: true });
          processBuffer(false);
          return pump();
        });
      }
      function processBuffer(final) {
        // 兼容 \n\n 与 \r\n\r\n 分隔
        var parts = buffer.split(/\r?\n\r?\n/);
        buffer = parts.pop();
        parts.forEach(function (raw) {
          var evt = null, data = [];
          raw.split(/\r?\n/).forEach(function (line) {
            if (line.startsWith("event:")) evt = line.slice(6).trim();
            else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
          });
          if (!data.length) return;
          var payload = data.join("\n");
          if (evt) handlers.onEvent && handlers.onEvent(evt, payload);
          else handlers.onDelta && handlers.onDelta(payload);
        });
        if (final && buffer) {
          var evt = null, data = [];
          buffer.split(/\r?\n/).forEach(function (line) {
            if (line.startsWith("event:")) evt = line.slice(6).trim();
            else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
          });
          if (data.length) {
            var payload = data.join("\n");
            if (evt) handlers.onEvent && handlers.onEvent(evt, payload);
            else handlers.onDelta && handlers.onDelta(payload);
          }
          buffer = "";
        }
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
    // 无序列表（打类标记，避免与有序列表互相包裹）
    html = html.replace(/^[-*] (.+)$/gm, "<li class=\"md-li-u\">$1</li>");
    // 有序列表（value 保留原始编号）
    html = html.replace(/^(\d+)\. (.+)$/gm, "<li class=\"md-li-o\" value=\"$1\">$2</li>");
    // 分别包裹
    html = html.replace(/(<li class="md-li-u">[\s\S]*?<\/li>)(?!\s*<li class="md-li-u">)/g, "<ul>$1</ul>");
    html = html.replace(/(<li class="md-li-o">[\s\S]*?<\/li>)(?!\s*<li class="md-li-o">)/g, "<ol>$1</ol>");
    html = html.replace(/class="md-li-u"/g, "").replace(/class="md-li-o"/g, "");
    // 段落与换行
    html = html.replace(/\n{2,}/g, "</p><p>");
    html = html.replace(/\n/g, "<br>");
    html = "<p>" + html + "</p>";
    // 回填代码块
    html = html.replace(/\u0000CODE(\d+)\u0000/g, function (m, i) { return codes[+i]; });
    // 图片：Markdown 语法 ![...](...) 仅限 http(s)，与行内/单独成行的图片 URL
    html = html.replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
      '<img class="md-img" src="$2" alt="$1" loading="lazy">');
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

  /* ---------- 主题切换（立即生效 + 登录后写入账号） ---------- */
  var THEME_KEY = "lingxi-theme";
  var THEMES = { light: 1, dark: 1, system: 1 };
  var THEME_ICONS = { light: "bi-sun", dark: "bi-moon", system: "bi-circle-half" };

  function themePref() {
    var p = document.documentElement.getAttribute("data-theme-pref") || "";
    return THEMES[p] ? p : "light";
  }

  function resolveTheme(pref) {
    if (pref === "dark" || pref === "light") return pref;
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch (e) {
      return "light";
    }
  }

  function syncThemeUI(pref) {
    document.querySelectorAll("[data-theme-icon]").forEach(function (icon) {
      icon.className = "bi " + (THEME_ICONS[pref] || THEME_ICONS.system);
    });
    document.querySelectorAll("[data-theme-set]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-theme-set") === pref);
    });
  }

  function applyTheme(pref, persist) {
    if (!THEMES[pref]) pref = "light";
    document.documentElement.setAttribute("data-theme-pref", pref);
    document.documentElement.setAttribute("data-theme", resolveTheme(pref));
    syncThemeUI(pref);
    if (!persist) return;
    try { localStorage.setItem(THEME_KEY, pref); } catch (e) { /* 隐私模式 */ }
    if (document.body && document.body.getAttribute("data-auth") === "1" && window.api) {
      window.api.post("/settings/api/theme", { theme: pref }).then(function (res) {
        if (!res || !res.ok) toast((res && res.error) || "主题未保存到账号", "error");
      }).catch(function () {
        toast("主题未保存到账号", "error");
      });
    }
  }

  window.setTheme = function (pref) { applyTheme(pref, true); };

  document.addEventListener("DOMContentLoaded", function () {
    syncThemeUI(themePref());
    try {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if (themePref() === "system") applyTheme("system", false);
      });
    } catch (e) { /* 旧浏览器 */ }
  });

  document.addEventListener("click", function (e) {
    var setter = e.target.closest("[data-theme-set]");
    if (setter) {
      applyTheme(setter.getAttribute("data-theme-set"), true);
      document.querySelectorAll(".theme-menu").forEach(function (m) {
        m.hidden = true;
        var btn = m.parentElement && m.parentElement.querySelector("[data-theme-toggle]");
        if (btn) btn.setAttribute("aria-expanded", "false");
      });
      return;
    }
    var toggle = e.target.closest("[data-theme-toggle]");
    if (toggle) {
      var wrap = toggle.closest("[data-theme-switch]");
      var menu = wrap && wrap.querySelector(".theme-menu");
      if (!menu) return;
      var open = menu.hidden;
      document.querySelectorAll(".theme-menu").forEach(function (m) { m.hidden = true; });
      document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
        b.setAttribute("aria-expanded", "false");
      });
      menu.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    if (!e.target.closest("[data-theme-switch]")) {
      document.querySelectorAll(".theme-menu").forEach(function (m) { m.hidden = true; });
      document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
        b.setAttribute("aria-expanded", "false");
      });
    }
  });
})();
