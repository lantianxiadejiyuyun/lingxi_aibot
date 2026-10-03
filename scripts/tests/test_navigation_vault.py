"""Encrypted navigation callback registration without reading any credentials."""
import base64
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import hashlib
import json
import socket
from unittest.mock import MagicMock, patch

import requests

from scripts.tests.support import IsolatedAppTestCase

from app.blueprints.integration_vault import bp
from app.extensions import csrf, db
from app.models.setting import Setting
from app.services import navigation_vault_service as vault
from app.services import navigation_vault_reader as reader
from app.utils.integration_api import ApiError
from app.utils.integration_api import iso_datetime
from app.utils.scoping import user_scope
from app.utils.timeutil import utcnow


API = "/api/v1/integrations/navigation-vault"
OWNER_TOKEN = "lx_isolated_vault_owner_token"
OTHER_TOKEN = "lx_isolated_vault_other_token"
READ_TOKEN = "isolated_navigation_callback_credential"


class FakeResponseBody(BytesIO):
    def read(self, size=-1, decode_content=False):
        return super().read(size)

    def release_conn(self):
        self.close()


class NavigationVaultFixture(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        if bp.name not in self.app.blueprints:
            self.app.register_blueprint(bp)
        csrf.exempt(bp)
        user = self.make_user()
        user.api_token = OWNER_TOKEN
        other = self.make_user("other_vault_user")
        other.api_token = OTHER_TOKEN
        db.session.commit()
        self.user_id, self.other_id = user.id, other.id
        self.client = self.app.test_client()
        self.now = utcnow()
        clock = patch.object(vault, "utcnow", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.dns = patch("app.utils.urlsafety.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 0)),
        ])
        self.mock_dns = self.dns.start()
        self.addCleanup(self.dns.stop)

    def payload(self, **changes):
        payload = {"read_url": f"https://index.eugenstudio.cn{vault.READ_PATH}",
                   "read_token": READ_TOKEN, "expires_at": iso_datetime(self.now + timedelta(hours=1)),
                   "scope": "all", "navigation_user_id": "navigation-user-12"}
        payload.update(changes)
        return payload

    def call(self, method="get", payload=None, token=OWNER_TOKEN):
        headers = {"Authorization": "Bearer " + token} if token else {}
        return self.client.open(API, method=method.upper(), json=payload, headers=headers)

    def record(self, user_id=None):
        return Setting.query.filter_by(key=vault.SETTING_KEY, user_id=user_id or self.user_id).one().value

    def register(self, **changes):
        response = self.call("put", self.payload(**changes))
        self.assertEqual(response.status_code, 200, response.get_json())
        return response


class NavigationVaultTests(NavigationVaultFixture):
    def test_encrypted_registration_has_no_plaintext_in_database_or_response(self):
        response = self.register(navigation_user_id=42)
        data = response.get_json()["data"]
        self.assertEqual(set(data), {"configured", "navigation_user_id", "expires_at", "scope"})
        self.assertTrue(data["configured"])
        self.assertEqual(data["navigation_user_id"], "42")
        self.assertEqual(data["scope"], "all")
        stored = self.record()
        self.assertNotIn(READ_TOKEN, json.dumps(stored))
        self.assertNotIn("read_token", stored)
        self.assertNotIn(READ_TOKEN, response.get_data(as_text=True))
        self.assertNotIn("read_url", data)
        self.assertEqual(vault.decrypt_registration(self.user_id)["read_token"], READ_TOKEN)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", response.headers)
        self.mock_dns.assert_called_once_with("index.eugenstudio.cn", None, proto=socket.IPPROTO_TCP)

    def test_repeated_registration_uses_a_new_nonce_and_ciphertext(self):
        self.register()
        first = base64.b64decode(self.record()["token_ciphertext"])
        self.register()
        second = base64.b64decode(self.record()["token_ciphertext"])
        self.assertNotEqual(first[:12], second[:12])
        self.assertNotEqual(first, second)
        self.assertEqual(Setting.query.filter_by(key=vault.SETTING_KEY).count(), 1)

    def test_other_account_and_global_settings_are_not_read(self):
        self.register()
        encrypted = deepcopy(self.record())
        db.session.add(Setting(key=vault.SETTING_KEY, user_id=0, value=encrypted))
        db.session.commit()
        response = self.call(token=OTHER_TOKEN)
        self.assertFalse(response.get_json()["data"]["configured"])
        self.assertEqual(response.get_json()["data"]["navigation_user_id"], None)
        self.assertEqual(self.call("delete", token=OTHER_TOKEN).status_code, 200)
        self.assertTrue(self.call().get_json()["data"]["configured"])

    def test_copying_ciphertext_to_another_account_fails_authentication(self):
        self.register()
        db.session.add(Setting(key=vault.SETTING_KEY, user_id=self.other_id, value=deepcopy(self.record())))
        db.session.commit()
        with self.assertRaises(vault.VaultRegistrationError):
            vault.decrypt_registration(self.other_id)
        self.assertFalse(self.call(token=OTHER_TOKEN).get_json()["data"]["configured"])

    def test_ciphertext_and_authenticated_metadata_cannot_be_tampered(self):
        self.register()
        original = deepcopy(self.record())
        changes = [
            {"navigation_user_id": "other-navigation-user"},
            {"read_url": f"https://ojjlab.eugenstudio.cn{vault.READ_PATH}"},
            {"expires_at": iso_datetime(self.now + timedelta(hours=2))},
            {"token_ciphertext": base64.b64encode(b"x" * 64).decode("ascii")},
            {"token_ciphertext": "not-valid-base64"},
            {"version": True},
        ]
        for changed in changes:
            with self.subTest(changed=tuple(changed)):
                row = Setting.query.filter_by(key=vault.SETTING_KEY, user_id=self.user_id).one()
                row.value = {**original, **changed}
                db.session.commit()
                with self.assertRaises(vault.VaultRegistrationError):
                    vault.decrypt_registration(self.user_id)
                response = self.call()
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.get_json()["data"]["configured"])

    def test_expiry_bounds_and_expired_registration_are_enforced(self):
        invalid = [None, "2026-10-03", "2026-10-03T12:00:00",
                   iso_datetime(self.now), iso_datetime(self.now - timedelta(seconds=1)),
                   iso_datetime(self.now + timedelta(hours=24, seconds=1))]
        for expiry in invalid:
            with self.subTest(expiry=expiry):
                self.assertEqual(self.call("put", self.payload(expires_at=expiry)).status_code, 400)
        self.register(expires_at=iso_datetime(self.now + timedelta(hours=24)))
        with patch.object(vault, "utcnow", return_value=self.now + timedelta(hours=24)):
            with self.assertRaises(vault.VaultRegistrationError):
                vault.decrypt_registration(self.user_id)
            self.assertFalse(self.call().get_json()["data"]["configured"])

    def test_unix_milliseconds_round_trip_exactly_and_rfc3339_returns_milliseconds(self):
        expires = (self.now + timedelta(hours=1)).replace(microsecond=123000)
        # A nonzero millisecond suffix detects accidental seconds conversion
        # and avoids relying on a float timestamp to compute the expected value.
        millis = vault._unix_milliseconds(expires)
        self.assertEqual(millis % 1000, 123)
        response = self.register(expires_at=millis)
        self.assertIs(type(response.get_json()["data"]["expires_at"]), int)
        self.assertEqual(response.get_json()["data"]["expires_at"], millis)
        self.assertEqual(self.call().get_json()["data"]["expires_at"], millis)
        self.assertEqual(self.record()["expires_at"], iso_datetime(expires))
        self.assertEqual(vault.decrypt_registration(self.user_id)["expires_at"], iso_datetime(expires))
        response = self.register(expires_at=iso_datetime(expires))
        self.assertEqual(response.get_json()["data"]["expires_at"], millis)

    def test_unix_expiry_rejects_bool_float_seconds_overflow_and_expired_values(self):
        millis = vault._unix_milliseconds(self.now + timedelta(hours=1))
        invalid = [True, False, float(millis), 0, -1, millis // 1000,
                   253402300800000, 10 ** 100,
                   vault._unix_milliseconds(self.now),
                   vault._unix_milliseconds(self.now + timedelta(hours=24)) + 1]
        for expiry in invalid:
            with self.subTest(expiry=expiry):
                self.assertEqual(self.call("put", self.payload(expires_at=expiry)).status_code, 400)
        self.assertEqual(Setting.query.filter_by(key=vault.SETTING_KEY).count(), 0)

    def test_url_rejects_non_allowlisted_targets_and_ambiguous_forms_before_dns(self):
        invalid = [
            f"http://index.eugenstudio.cn{vault.READ_PATH}",
            f"https://example.com{vault.READ_PATH}",
            f"https://127.0.0.1{vault.READ_PATH}",
            f"https://8.8.8.8{vault.READ_PATH}",
            f"https://[::1]{vault.READ_PATH}",
            f"https://index.eugenstudio.cn:443{vault.READ_PATH}",
            f"https://u:p@index.eugenstudio.cn{vault.READ_PATH}",
            f"https://index.eugenstudio.cn.evil.test{vault.READ_PATH}",
            f"https://index.eugenstudio.cn.{vault.READ_PATH}",
            f"https://index.eugenstudio.cn{vault.READ_PATH}?token=secret",
            f"https://index.eugenstudio.cn{vault.READ_PATH}#part",
            f"https://index.eugenstudio.cn{vault.READ_PATH}?",
            f"https://index.eugenstudio.cn{vault.READ_PATH}#",
            f"https://index.eugenstudio.cn{vault.READ_PATH}/../read",
            f"https://index.eugenstudio.cn\\@evil.test{vault.READ_PATH}",
            f"https://index.eu\ngenstudio.cn{vault.READ_PATH}",
            "https://index.eugenstudio.cn/api/other",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertEqual(self.call("put", self.payload(read_url=url)).status_code, 400)
        self.mock_dns.assert_not_called()
        self.assertEqual(Setting.query.filter_by(key=vault.SETTING_KEY).count(), 0)

    def test_private_or_mixed_dns_and_lookup_failure_are_rejected(self):
        for addresses in (["127.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "192.168.100.72"], ["::1"]):
            with self.subTest(addresses=addresses):
                self.mock_dns.return_value = [
                    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))
                    for ip in addresses
                ]
                self.assertEqual(self.call("put", self.payload()).status_code, 400)
        self.mock_dns.side_effect = socket.gaierror("isolated unresolved domain")
        self.assertEqual(self.call("put", self.payload()).status_code, 400)
        self.assertEqual(Setting.query.filter_by(key=vault.SETTING_KEY).count(), 0)

    def test_failed_put_preserves_existing_record_and_strictly_rejects_fields(self):
        self.register()
        original = deepcopy(self.record())
        invalid = [self.payload(extra=True), self.payload(scope="selected"),
                   self.payload(read_token="short"), self.payload(read_token="x" * 4097),
                   self.payload(read_token="bad token containing spaces"),
                   self.payload(navigation_user_id=True), self.payload(navigation_user_id=""),
                   self.payload(navigation_user_id="x" * 129), self.payload(navigation_user_id=-1),
                   {key: value for key, value in self.payload().items() if key != "scope"}]
        for payload in invalid:
            with self.subTest(fields=list(payload)):
                response = self.call("put", payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.record(), original)
                self.assertNotIn(READ_TOKEN, response.get_data(as_text=True))

    def test_get_never_reads_callback_and_delete_is_idempotent(self):
        self.register()
        self.mock_dns.reset_mock()
        self.assertTrue(self.call().get_json()["data"]["configured"])
        self.mock_dns.assert_not_called()
        for _ in range(2):
            response = self.call("delete")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["data"], {"configured": False,
                "navigation_user_id": None, "expires_at": None, "scope": None})
        self.assertEqual(Setting.query.filter_by(key=vault.SETTING_KEY).count(), 0)

    def test_all_routes_require_token_and_allowlist_can_be_narrowed(self):
        for method in ("get", "put", "delete"):
            with self.subTest(method=method):
                self.assertEqual(self.call(method, self.payload(), token=None).status_code, 401)
        self.app.config["NAVIGATION_VAULT_ALLOWED_HOSTS"] = ["ojjlab.eugenstudio.cn"]
        self.assertEqual(self.call("put", self.payload()).status_code, 400)
        self.register(read_url=f"https://ojjlab.eugenstudio.cn{vault.READ_PATH}")

    def test_rotating_server_secret_invalidates_ciphertext(self):
        self.register()
        with patch.dict(self.app.config, {"SECRET_KEY": "replacement-isolated-server-secret"}):
            with self.assertRaises(vault.VaultRegistrationError):
                vault.decrypt_registration(self.user_id)
            self.assertFalse(self.call().get_json()["data"]["configured"])


class NavigationVaultReaderTests(NavigationVaultFixture):
    """All callbacks below are synthetic requests.Response objects, never HTTP."""

    def setUp(self):
        super().setUp()
        self.register()
        self.mock_dns.reset_mock()
        self.http = MagicMock()
        self.http.__enter__.return_value = self.http
        factory = patch.object(reader.requests, "Session", return_value=self.http)
        factory.start()
        self.addCleanup(factory.stop)

    def item(self, **changes):
        item = {"id": "vault-item-1", "title": "示例站点",
                "url": "https://embedded-user:embedded-password@example.com/private-path?token=query-secret#fragment-secret",
                "username": "account-owner@example.com", "password": "synthetic-SECRET-password",
                "notes": "synthetic-PRIVATE-notes", "createdAt": 123, "updatedAt": 456}
        item.update(changes)
        return item

    def response(self, items=None, status=200, raw=None, **changes):
        payload = {"items": [self.item()] if items is None else items,
                   "snapshot_version": 2, "source_version": 3, "updated_at": 456}
        payload.update(changes)
        response = requests.Response()
        response.status_code = status
        response.raw = FakeResponseBody(raw if raw is not None else json.dumps(payload).encode("utf-8"))
        self.http.post.return_value = response
        return response

    def call_reader(self, path="/items", method="get", payload=None, token=OWNER_TOKEN, grant_hash=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        if grant_hash is not None:
            headers["X-Navigation-Vault-Grant"] = grant_hash
        return self.client.open(API + path, method=method.upper(), json=payload, headers=headers)

    def test_grant_hash_correct_or_absent_reads_current_source_for_both_endpoints(self):
        matching = hashlib.sha256(READ_TOKEN.encode("utf-8")).hexdigest()
        for grant_hash in (None, matching, matching.upper()):
            for path, method, payload in (("/items", "get", None),
                                          ("/reveal", "post", {"id": "vault-item-1"})):
                with self.subTest(path=path, header=grant_hash is not None):
                    self.response()
                    response = self.call_reader(path, method, payload, grant_hash=grant_hash)
                    self.assertEqual(response.status_code, 200, response.get_json())
                    self.assertEqual(self.http.post.call_args.kwargs["headers"]["Authorization"],
                                     "Bearer " + READ_TOKEN)

    def test_replaced_source_rejects_previous_grant_hash_before_callback(self):
        previous_hash = hashlib.sha256(READ_TOKEN.encode("utf-8")).hexdigest()
        new_token = "isolated_replacement_navigation_credential"
        self.register(read_url=f"https://ojjlab.eugenstudio.cn{vault.READ_PATH}",
                      read_token=new_token, navigation_user_id="navigation-user-34")
        for path, method, payload in (("/items", "get", None),
                                      ("/reveal", "post", {"id": "vault-item-1"})):
            with self.subTest(path=path):
                response = self.call_reader(path, method, payload, grant_hash=previous_hash)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["code"], "vault_source_changed")
                self.http.post.assert_not_called()
        # Native UI/AI callers omit the optional header and follow the current source.
        for grant_hash in (None, hashlib.sha256(new_token.encode("utf-8")).hexdigest()):
            self.response()
            response = self.call_reader(grant_hash=grant_hash)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.http.post.call_args.args[0], f"https://ojjlab.eugenstudio.cn{vault.READ_PATH}")
            self.assertEqual(self.http.post.call_args.kwargs["headers"]["Authorization"], "Bearer " + new_token)

    def test_malformed_or_wrong_grant_hash_never_calls_callback(self):
        matching = hashlib.sha256(READ_TOKEN.encode("utf-8")).hexdigest()
        for grant_hash in ("", "g" * 64, "0" * 63, "0" * 65, "0" * 64, " " + matching,
                           matching + "," + matching, READ_TOKEN):
            for path, method, payload in (("/items", "get", None),
                                          ("/reveal", "post", {"id": "vault-item-1"})):
                with self.subTest(path=path, value=grant_hash):
                    response = self.call_reader(path, method, payload, grant_hash=grant_hash)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.get_json()["code"], "vault_source_changed")
        self.http.post.assert_not_called()

    def test_list_returns_only_masked_metadata_and_pins_fresh_public_ip(self):
        remote = self.response()
        response = self.call_reader()
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["data"], [{"id": "vault-item-1", "title": "示例站点",
            "site": "https://example.com", "username_masked": "a***m", "password_set": True}])
        text = response.get_data(as_text=True)
        for forbidden in (self.item()["username"], self.item()["password"], self.item()["notes"],
                          "embedded-user", "embedded-password", "private-path", "query-secret", "fragment-secret", READ_TOKEN):
            self.assertNotIn(forbidden, text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(self.http.trust_env)
        kwargs = self.http.post.call_args.kwargs
        self.assertEqual(kwargs["json"], {})
        self.assertEqual(kwargs["timeout"], (5, 15))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertNotIn("verify", kwargs)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + READ_TOKEN)
        adapter = self.http.mount.call_args.args[1]
        self.assertEqual(adapter.poolmanager._pinned_ip, "8.8.8.8")
        self.assertTrue(remote.raw.closed)
        self.mock_dns.assert_called_once_with("index.eugenstudio.cn", None, proto=socket.IPPROTO_TCP)

    def test_list_filters_only_safe_fields_and_paginates_after_filter(self):
        items = [self.item(id=f"item-{i}", title=f"Work {i}", username="") for i in range(4)]
        self.response(items)
        response = self.call_reader("/items?q=work&limit=2&offset=1")
        body = response.get_json()
        self.assertEqual([item["id"] for item in body["data"]], ["item-1", "item-2"])
        self.assertEqual(body["pagination"], {"limit": 2, "offset": 1, "total": 4})
        self.assertEqual(body["data"][0]["username_masked"], "")
        self.response()
        response = self.call_reader("/items?q=synthetic-SECRET-password")
        self.assertEqual(response.get_json()["data"], [])

    def test_single_reveal_posts_only_requested_id_and_never_returns_notes_or_raw_url(self):
        self.response()
        response = self.call_reader("/reveal", "post", {"id": "vault-item-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.http.post.call_args.kwargs["json"], {"ids": ["vault-item-1"]})
        data = response.get_json()["data"]
        self.assertEqual(set(data), {"id", "title", "site", "username", "password"})
        self.assertEqual(data["password"], self.item()["password"])
        self.assertEqual(data["username"], self.item()["username"])
        self.assertEqual(data["site"], "https://example.com")
        self.assertNotIn(self.item()["notes"], response.get_data(as_text=True))
        self.assertNotIn(READ_TOKEN, response.get_data(as_text=True))
        # Only the original encrypted registration is persisted by this feature.
        self.assertEqual(Setting.query.filter_by(key=vault.SETTING_KEY).count(), 1)
        self.assertNotIn(self.item()["password"], json.dumps(self.record()))

    def test_single_reveal_missing_extra_or_mismatched_id_is_rejected(self):
        cases = [([], 404), ([self.item(id="other-id")], 502),
                 ([self.item(), self.item(id="extra-id")], 502)]
        for items, expected in cases:
            with self.subTest(count=len(items), expected=expected):
                self.response(items)
                response = self.call_reader("/reveal", "post", {"id": "vault-item-1"})
                self.assertEqual(response.status_code, expected)
                self.assertNotIn(self.item()["password"], response.get_data(as_text=True))

    def test_other_user_unbound_or_unauthenticated_never_calls_callback(self):
        self.assertEqual(self.call_reader(token=OTHER_TOKEN).status_code, 403)
        self.assertEqual(self.call_reader(token=None).status_code, 401)
        with user_scope(self.other_id):
            with self.assertRaises(ApiError) as caught:
                reader.reveal_vault_item(self.user_id, "vault-item-1")
        self.assertEqual(caught.exception.status, 403)
        self.http.post.assert_not_called()

    def test_expired_or_deleted_local_registration_never_calls_callback(self):
        with patch.object(vault, "utcnow", return_value=self.now + timedelta(hours=2)):
            self.assertEqual(self.call_reader().status_code, 403)
        self.call("delete")
        self.assertEqual(self.call_reader().status_code, 403)
        self.http.post.assert_not_called()

    def test_revoked_stale_decryption_and_redirect_errors_hide_upstream_body(self):
        for status, expected, code in ((401, 403, "vault_unavailable"), (403, 403, "vault_unavailable"),
                (409, 409, "vault_stale"), (503, 503, "vault_decryption_failed"),
                (302, 502, "vault_upstream_error"), (500, 502, "vault_upstream_error")):
            with self.subTest(status=status):
                remote = self.response(status=status, raw=b"synthetic-SECRET-password unexpected error")
                response = self.call_reader()
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.get_json()["code"], code)
                self.assertNotIn("synthetic-SECRET-password", response.get_data(as_text=True))
                self.assertTrue(remote.raw.closed)

    def test_private_mixed_dns_is_rechecked_on_every_callback(self):
        self.mock_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 0)),
                                     (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 0))]
        response = self.call_reader()
        self.assertEqual(response.status_code, 502)
        self.http.post.assert_not_called()

    def test_response_size_schema_utf8_and_duplicate_ids_are_bounded(self):
        cases = [
            {"raw": b"x" * (reader.MAX_RESPONSE_BYTES + 1)},
            {"raw": b'{"items":NaN}'}, {"raw": b"\xff"},
            {"items": [self.item(), self.item()]},
            {"items": [self.item(password=3)]},
            {"items": [self.item(title="\ud800")]},
            {"items": [self.item(id="x" * 201)]},
            {"items": [self.item(notes="x" * 100001)]},
            {"items": [self.item(createdAt=True)]},
            {"source_version": True},
        ]
        for changes in cases:
            with self.subTest(fields=tuple(changes)):
                self.response(**changes)
                response = self.call_reader()
                self.assertEqual(response.status_code, 502)
                self.assertNotIn(self.item()["password"], response.get_data(as_text=True))
        with self.assertRaises(ApiError):
            reader._validate_snapshot({"items": [self.item()] * 10001,
                "snapshot_version": 1, "source_version": 1, "updated_at": 1})

    def test_delete_rotation_or_expiry_during_callback_discards_plaintext(self):
        for action, expected in (("delete", 403), ("rotate", 409), ("expire", 403)):
            with self.subTest(action=action):
                self.register()
                remote = self.response()

                def callback(*args, **kwargs):
                    if action == "delete":
                        vault.unregister_navigation_vault(self.user_id)
                    elif action == "rotate":
                        vault.register_navigation_vault(self.user_id, self.payload())
                    return remote

                self.http.post.side_effect = callback
                if action == "expire":
                    with patch.object(vault, "utcnow", side_effect=[self.now, self.now + timedelta(hours=2)]):
                        response = self.call_reader("/reveal", "post", {"id": "vault-item-1"})
                else:
                    response = self.call_reader("/reveal", "post", {"id": "vault-item-1"})
                self.assertEqual(response.status_code, expected, response.get_json())
                self.assertNotIn(self.item()["password"], response.get_data(as_text=True))
        self.http.post.side_effect = None

    def test_unknown_connection_error_is_sanitized(self):
        self.http.post.side_effect = requests.ConnectionError("Authorization Bearer " + READ_TOKEN)
        response = self.call_reader()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(READ_TOKEN, response.get_data(as_text=True))

    def test_invalid_reveal_or_list_parameters_fail_before_callback(self):
        invalid = [{"id": 1}, {"id": ""}, {"id": "x" * 201}, {"id": "bad\nID"},
                   {"id": "\ud800"}, {"id": "vault-item-1", "user_id": self.other_id}, {}]
        for body in invalid:
            self.assertEqual(self.call_reader("/reveal", "post", body).status_code, 400)
        for query in ("limit=0", "limit=201", "offset=-1", "q=" + "x" * 201):
            self.assertEqual(self.call_reader("/items?" + query).status_code, 400)
        self.http.post.assert_not_called()

    def test_site_origin_never_copies_embedded_secrets(self):
        cases = {"javascript:alert(1)": "", "not-a-url": "",
                 "https://u:p@host.test:443/private?secret=1#token": "https://host.test",
                 "http://host.test:8080/secret": "http://host.test:8080",
                 "https://[2001:4860:4860::8888]/secret": "https://[2001:4860:4860::8888]",
                 "https://host.test\\secret": "", "https://host.test:wrong/secret": ""}
        for value, expected in cases.items():
            self.assertEqual(reader._site_origin(value), expected)
