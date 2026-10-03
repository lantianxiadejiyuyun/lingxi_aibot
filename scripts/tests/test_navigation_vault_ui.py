"""Protected vault UI and AI metadata boundary; all callback responses are fake."""
import json
import re
import shutil
import subprocess
from unittest.mock import patch

from flask_login import current_user

from scripts.tests.support import IsolatedAppTestCase
from app.ai import registry
from app.ai.tools.navigation_vault_tools import search_navigation_vault
from app.blueprints.navigation_vault import bp
from app.utils.integration_api import ApiError
from app.utils.scoping import current_user_id, set_current_user_id, user_scope


ITEM_ID = "item-550e8400-e29b-41d4-a716-446655440000"
FULL_USERNAME = "dummy-full-private-user@example.invalid"
PASSWORD = "dummy-private-password-never-send-to-model"
METADATA = {"id": ITEM_ID, "title": "测试账号", "site": "https://example.invalid",
            "username_masked": "d***@example.invalid", "password_set": True}
REVEALED = {"id": ITEM_ID, "title": METADATA["title"], "site": METADATA["site"],
            "username": FULL_USERNAME, "password": PASSWORD}


def listing(item=None):
    return {"items": [dict(item or METADATA)], "pagination": {"limit": 20, "offset": 0, "total": 1}}


class NavigationVaultUITests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        if bp.name not in self.app.blueprints:
            self.app.register_blueprint(bp)
        self.user = self.make_user("vault_ui_owner", is_admin=False)
        self.other = self.make_user("vault_ui_other")
        self.client = self.client_for(self.user)
        self.base = "/settings/navigation-vault"

    def csrf(self, client=None):
        response = (client or self.client).get(self.base + "/")
        self.assertEqual(response.status_code, 200)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', response.get_data(as_text=True)).group(1)

    def reveal(self, body, csrf=None, client=None):
        return (client or self.client).post(self.base + "/api/reveal", json=body,
                                            headers={"X-CSRFToken": csrf or self.csrf(client)})

    def test_initial_page_has_no_credentials_and_requires_login(self):
        with patch("app.services.navigation_vault_reader.list_vault_items") as read, \
                patch("app.services.navigation_vault_reader.reveal_vault_item") as reveal:
            response = self.client.get(self.base + "/")
            anonymous = self.app.test_client()
            self.assertEqual(anonymous.get(self.base + "/").status_code, 302)
            self.assertEqual(anonymous.get(self.base + "/api/items").status_code, 302)
            self.assertNotEqual(anonymous.post(self.base + "/api/reveal", json={"id": ITEM_ID}).status_code, 200)
        read.assert_not_called()
        reveal.assert_not_called()
        html = response.get_data(as_text=True)
        self.assertNotIn(PASSWORD, html)
        self.assertNotIn(FULL_USERNAME, html)
        self.assertIn('id="vault-reveal"', html)
        self.assertIn('type="password" readonly autocomplete="new-password"', html)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_account_settings_entry_opens_protected_page_with_admin_prefix(self):
        from app import apply_admin_entry_config

        apply_admin_entry_config(self.app, "lingxi")
        response = self.client.get("/lingxi/settings/?tab=account")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'href="([^"]*/settings/navigation-vault/)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "/lingxi/settings/navigation-vault/")
        with patch("app.services.navigation_vault_reader.reveal_vault_item") as reveal:
            opened = self.client.get(match.group(1))
        self.assertEqual(opened.status_code, 200)
        self.assertIn('id="vault-reveal"', opened.get_data(as_text=True))
        reveal.assert_not_called()

    def test_list_is_current_account_scoped_and_filters_unexpected_secret_fields(self):
        injected = {**METADATA, "username": FULL_USERNAME, "password": PASSWORD, "notes": "private notes"}

        def read(user_id, **kwargs):
            self.assertEqual(user_id, self.user.id)
            self.assertEqual(current_user.id, self.user.id)
            self.assertEqual(current_user_id(), self.user.id)
            self.assertEqual(kwargs, {"q": "测试", "limit": 20, "offset": 0})
            return listing(injected)

        with patch("app.services.navigation_vault_reader.list_vault_items", side_effect=read):
            response = self.client.get(self.base + "/api/items?q=测试&limit=20")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["items"], [METADATA])
        self.assertNotIn(PASSWORD, response.get_data(as_text=True))
        self.assertNotIn(FULL_USERNAME, response.get_data(as_text=True))
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_reveal_is_explicit_csrf_post_with_no_user_id_override(self):
        token = self.csrf()
        with patch("app.services.navigation_vault_reader.reveal_vault_item", return_value=REVEALED) as reveal:
            self.assertEqual(self.client.get(self.base + "/api/reveal").status_code, 405)
            missing_csrf = self.client.post(self.base + "/api/reveal", json={"id": ITEM_ID})
            self.assertEqual(missing_csrf.status_code, 400)
            self.assertEqual(self.reveal({"id": ITEM_ID, "user_id": self.other.id}, token).status_code, 400)
            reveal.assert_not_called()
            response = self.reveal({"id": ITEM_ID}, token)
        reveal.assert_called_once_with(self.user.id, ITEM_ID)
        self.assertEqual(response.get_json()["data"], REVEALED)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_other_logged_account_cannot_select_the_first_accounts_registration(self):
        other_client = self.client_for(self.other)
        token = self.csrf(other_client)

        def reveal(user_id, item_id):
            self.assertEqual(user_id, self.other.id)
            self.assertEqual(current_user_id(), self.other.id)
            raise ApiError("条目不存在", 404, "not_found")

        with patch("app.services.navigation_vault_reader.reveal_vault_item", side_effect=reveal):
            response = self.reveal({"id": ITEM_ID}, token, other_client)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(PASSWORD, response.get_data(as_text=True))

    def test_bad_ids_and_query_owner_overrides_are_rejected_before_callbacks(self):
        token = self.csrf()
        with patch("app.services.navigation_vault_reader.reveal_vault_item") as reveal, \
                patch("app.services.navigation_vault_reader.list_vault_items") as read:
            for identifier in (None, 1, True, "", "x" * 201, "bad\nvalue"):
                self.assertEqual(self.reveal({"id": identifier}, token).status_code, 400)
            self.assertEqual(self.client.get(self.base + "/api/items?user_id=2").status_code, 400)
            self.assertEqual(self.client.get(self.base + "/api/items?q=" + "x" * 201).status_code, 400)
        reveal.assert_not_called()
        read.assert_not_called()

    def test_unregistered_or_upstream_error_has_safe_message_and_no_cache(self):
        for exception, status in ((ApiError("请先在导航站授权保险库读取", 403, "vault_unavailable"), 403),
                                  (RuntimeError(PASSWORD), 500)):
            with patch("app.services.navigation_vault_reader.list_vault_items", side_effect=exception):
                response = self.client.get(self.base + "/api/items")
            self.assertEqual(response.status_code, status)
            self.assertFalse(response.get_json()["ok"])
            self.assertNotIn(PASSWORD, response.get_data(as_text=True))
            self.assertIn("no-store", response.headers["Cache-Control"])

    def test_model_tool_returns_only_masked_metadata_and_protected_prefixed_link(self):
        injected = {**METADATA, "username": FULL_USERNAME, "password": PASSWORD, "notes": "private notes"}
        with self.app.test_request_context("/api/v1/chat", environ_overrides={"SCRIPT_NAME": "/lingxi"}), \
                user_scope(self.user.id), \
                patch("app.services.navigation_vault_reader.list_vault_items", return_value=listing(injected)) as read, \
                patch("app.services.navigation_vault_reader.reveal_vault_item") as reveal:
            result = search_navigation_vault(" 测试 ")
        read.assert_called_once_with(self.user.id, q="测试", limit=20, offset=0)
        reveal.assert_not_called()
        self.assertEqual(result["items"], [METADATA])
        self.assertEqual(result["url"], "/lingxi/settings/navigation-vault/")
        self.assertEqual(result["url_scope"], "lingxi_web")
        self.assertEqual(next(iter(result)), "action")
        self.assertEqual(result["action"], {"type": "open_navigation_vault",
                                          "list_path": "/integrations/navigation-vault/items",
                                          "reveal_path": "/integrations/navigation-vault/reveal"})
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertNotIn(FULL_USERNAME, json.dumps(result))
        self.assertNotIn("private notes", json.dumps(result))
        self.assertIsNone(registry.get_tool("reveal_navigation_vault"))

    def test_model_tool_requires_user_and_does_not_relay_unexpected_errors(self):
        with patch("app.services.navigation_vault_reader.list_vault_items") as read:
            set_current_user_id(-1)
            with self.assertRaises(ValueError):
                search_navigation_vault("test")
            read.assert_not_called()
        with user_scope(self.user.id), patch("app.services.navigation_vault_reader.list_vault_items",
                                             side_effect=RuntimeError(PASSWORD)):
            with self.assertRaises(ValueError) as caught:
                search_navigation_vault("test")
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_frontend_renders_untrusted_text_and_clears_secrets_in_real_javascript(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for the isolated frontend event test")
        html = self.client.get(self.base + "/").get_data(as_text=True)
        script = re.findall(r"<script>([\s\S]*?)</script>", html)[-1]
        program = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor() { this.value = ''; this.type = ''; this.hidden = true; this.disabled = false; this.children = []; this.handlers = {}; this.textContent = ''; this.attrs = {}; }
  set innerHTML(_) { throw new Error('Unsafe HTML sink'); }
  insertAdjacentHTML() { throw new Error('Unsafe HTML sink'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  focus(options) { this.focusOptions = options; }
  scrollIntoView(options) { this.scrollOptions = options; }
}
const nodes = new Map();
const element = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
const documentHandlers = {}, windowHandlers = {}, timers = new Map(), calls = [];
const document = {
  hidden: false, getElementById: element, createElement: () => new Element(),
  querySelector: () => ({content: 'isolated-csrf'}),
  addEventListener: (name, handler) => { documentHandlers[name] = handler; }
};
let timerId = 0, deferred = null, reducedMotion = false;
const response = data => ({ok: true, redirected: false, json: async () => ({ok: true, data})});
const context = {
  document, URLSearchParams, AbortController,
  window: {
    matchMedia: () => ({matches: reducedMotion}),
    setTimeout: (handler, delay) => { assert.equal(delay, 60000); timers.set(++timerId, handler); return timerId; },
    clearTimeout: id => timers.delete(id),
    addEventListener: (name, handler) => { windowHandlers[name] = handler; }
  },
  fetch: async (url, options) => {
    calls.push({url, options});
    assert.equal(options.cache, 'no-store');
    if (options.method === 'POST') {
      assert.equal(options.headers['X-CSRFToken'], 'isolated-csrf');
      assert.deepEqual(JSON.parse(options.body), {id: input.item.id});
      if (deferred) return deferred;
      return response(input.revealed);
    }
    return response({items: [input.item], pagination: {total: 1}});
  },
  history: {pushState() {throw new Error('History secret persistence');}, replaceState() {throw new Error('History secret persistence');}}
};
for (const key of ['localStorage', 'sessionStorage']) Object.defineProperty(context, key, {get() {throw new Error('Storage access forbidden');}});
vm.runInNewContext(input.script, context);
const flush = () => new Promise(setImmediate);
function cleared() {
  assert.equal(element('vault-password').value, '');
  assert.equal(element('vault-username').value, '');
  assert.equal(element('vault-password').type, 'password');
  assert.equal(element('vault-reveal').hidden, true);
}
(async () => {
  await flush();
  assert.equal(calls.length, 1); // Opening the page never reveals a secret.
  cleared();
  const card = element('vault-results').children[0];
  assert.equal(card.children[0].children[0].textContent, input.item.title);
  assert.equal(card.children[0].children[1].textContent, input.item.site);
  const button = card.children[1];
  await button.handlers.click();
  assert.equal(element('vault-password').value, input.revealed.password);
  assert.equal(element('vault-username').value, input.revealed.username);
  assert.equal(element('vault-password').type, 'password');
  assert.equal(element('vault-reveal').focusOptions.preventScroll, true);
  assert.equal(element('vault-reveal').scrollOptions.block, 'start');
  assert.equal(element('vault-reveal').scrollOptions.behavior, 'smooth');
  element('vault-toggle').handlers.click();
  assert.equal(element('vault-password').type, 'text');
  [...timers.values()][0]();
  cleared();
  reducedMotion = true;
  await button.handlers.click();
  assert.equal(element('vault-reveal').scrollOptions.behavior, 'auto');
  document.hidden = true;
  documentHandlers.visibilitychange();
  cleared();
  document.hidden = false;
  await button.handlers.click();
  windowHandlers.pagehide();
  cleared();
  let resolveLate;
  deferred = new Promise(resolve => { resolveLate = resolve; });
  const late = button.handlers.click();
  await flush();
  document.hidden = true;
  documentHandlers.visibilitychange();
  document.hidden = false;
  resolveLate(response(input.revealed));
  await late;
  cleared(); // A request completing after tab-switch must not restore secrets.
  process.stdout.write('frontend vault behavior passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        malicious_metadata = {**METADATA, "title": '<img src=x onerror="alert(1)">',
                              "site": "javascript:alert(1)"}
        result = subprocess.run([node, "-e", program],
                                input=json.dumps({"script": script, "item": malicious_metadata, "revealed": REVEALED}),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("frontend vault behavior passed", result.stdout)
