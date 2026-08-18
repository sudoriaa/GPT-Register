# -*- coding: utf-8 -*-
import os
import json
import unittest
from unittest.mock import patch

from config import env_loader
from webui import config_editor


class ConfigDefaultFallbackTests(unittest.TestCase):
    def test_blank_env_value_uses_default_for_all_supported_types(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "BOOL_KEY": "",
                "INT_KEY": "",
                "FLOAT_KEY": "",
                "STR_KEY": "",
                "LIST_KEY": "",
            }, clear=True):
                self.assertTrue(env_loader.env_bool("BOOL_KEY", True))
                self.assertEqual(env_loader.env_int("INT_KEY", 90), 90)
                self.assertEqual(env_loader.env_float("FLOAT_KEY", 1.5), 1.5)
                self.assertEqual(env_loader.env_str("STR_KEY", "default"), "default")
                self.assertEqual(env_loader.env_list("LIST_KEY", ["a"]), ["a"])
        finally:
            env_loader._LOADED = old_loaded

    def test_proxy_pool_blank_env_value_means_empty_list(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PROXY_POOL": ["socks5://127.0.0.1:7897"]}
        try:
            with patch.dict(os.environ, {"PROXY_POOL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"PROXY_POOL": "list_str_multiline"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PROXY_POOL"], [])

    def test_config_editor_formats_empty_list_as_literal_empty_list(self):
        self.assertEqual(config_editor._format_env_value([], "list_str_multiline"), "[]")

    def test_apply_env_overrides_does_not_let_blank_values_mask_defaults(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"FEATURE_ENABLED": True, "BASE_URL": "https://example.test"}
        try:
            with patch.dict(os.environ, {"FEATURE_ENABLED": "", "BASE_URL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"FEATURE_ENABLED": "bool", "BASE_URL": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["FEATURE_ENABLED"])
        self.assertEqual(namespace["BASE_URL"], "https://example.test")

    def test_config_editor_parses_env_str_default_from_source(self):
        source = 'API_KEY: str = env_str("API_KEY", "fallback-key")\n'
        self.assertEqual(
            config_editor._parse_value_from_source(source, "API_KEY", "str"),
            "fallback-key",
        )

    def test_config_editor_blank_env_value_falls_back_to_source_default(self):
        self.assertEqual(
            config_editor._coerce_raw_value("", "wss://connect.browser-use.com", "str"),
            "wss://connect.browser-use.com",
        )
        self.assertTrue(config_editor._coerce_raw_value("", True, "bool"))

    def test_config_editor_masks_secret_values_and_reports_presence(self):
        secret = "VAK-SECRET-SENTINEL"
        with patch.object(env_loader, "load_env"), \
             patch.object(env_loader, "read_env_file", return_value={"VAK_SMS_API_KEY": secret}):
            items = config_editor.get_config()
        field = next(item for item in items if item["key"] == "VAK_SMS_API_KEY")
        self.assertEqual(field["value"], "")
        self.assertTrue(field["configured"])
        self.assertNotIn(secret, json.dumps(items, ensure_ascii=False))

    def test_blank_secret_update_preserves_existing_value_and_explicit_clear_is_supported(self):
        with patch.object(env_loader, "write_env_values") as write, \
             patch.object(env_loader, "load_env"):
            result = config_editor.update_config({"VAK_SMS_API_KEY": ""})
            write.assert_not_called()
        self.assertIn("VAK_SMS_API_KEY", result["ignored"])

        with patch.object(env_loader, "write_env_values", return_value=["VAK_SMS_API_KEY"]) as write, \
             patch.object(env_loader, "load_env"):
            result = config_editor.update_config({"VAK_SMS_API_KEY": "__CLEAR__"})
            write.assert_called_once_with({"VAK_SMS_API_KEY": ""})
        self.assertIn("VAK_SMS_API_KEY", result["updated"])


if __name__ == "__main__":
    unittest.main()
