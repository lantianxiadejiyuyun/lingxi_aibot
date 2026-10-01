"""Account model catalogs and defaults must validate before any settings change."""
from app.services.settings_service import get_own_setting, set_setting
from scripts.tests.support import IsolatedAppTestCase


class ChatControlSettingsTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.user = self.make_user()
        self.client = self.client_for(self.user)
        self.fields = {
            "llm_protocol": "openai",
            "llm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "llm_model": "qwen-plus",
            "llm_models": "qwen-plus\nqwen-flash\nqwen-plus",
            "llm_reasoning_effort": "high",
            "llm_api_key": "test-account-key",
        }

    def test_saved_catalog_and_default_are_account_scoped_and_editable(self):
        other = self.make_user("other-account", is_admin=False)
        response = self.client.post("/settings/ai", data=self.fields)
        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=ai", response.location)
        self.assertEqual(get_own_setting("llm_models", user_id=self.user.id), ["qwen-plus", "qwen-flash"])
        self.assertEqual(get_own_setting("llm_reasoning_effort", user_id=self.user.id), "high")
        self.assertIsNone(get_own_setting("llm_models", user_id=other.id))
        page = self.client.get("/settings/?tab=ai")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="llm_models"', page.text)
        self.assertIn("qwen-plus\nqwen-flash", page.text)
        self.assertNotIn("test-account-key", page.text)

    def test_invalid_catalog_or_unsupported_reasoning_cannot_partially_save(self):
        for invalid in ({"llm_models": "bad model"}, {"llm_model": "unknown-model", "llm_reasoning_effort": "high"}):
            with self.subTest(invalid=invalid):
                set_setting("llm_model", "original-model", user_id=self.user.id)
                set_setting("llm_api_key", "original-key", user_id=self.user.id)
                response = self.client.post("/settings/ai", data={**self.fields, **invalid})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(get_own_setting("llm_model", user_id=self.user.id), "original-model")
                self.assertEqual(get_own_setting("llm_api_key", user_id=self.user.id), "original-key")

    def test_legacy_form_keeps_catalog_and_legacy_invalid_values_remain_editable(self):
        set_setting("llm_models", ["qwen-plus", "qwen-flash"], user_id=self.user.id)
        set_setting("llm_reasoning_effort", "medium", user_id=self.user.id)
        old_fields = {key: value for key, value in self.fields.items() if key not in {"llm_models", "llm_reasoning_effort"}}
        response = self.client.post("/settings/ai", data=old_fields)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_own_setting("llm_models", user_id=self.user.id), ["qwen-plus", "qwen-flash"])
        self.assertEqual(get_own_setting("llm_reasoning_effort", user_id=self.user.id), "medium")
        set_setting("llm_models", "old invalid model", user_id=self.user.id)
        page = self.client.get("/settings/?tab=ai")
        self.assertEqual(page.status_code, 200)
        self.assertIn("old invalid model", page.text)
