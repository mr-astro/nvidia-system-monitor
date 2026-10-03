import json
import os
import tempfile
import unittest
from pathlib import Path

from src.utils.config import DEFAULT_CONFIG, Config, sanitize
from src.utils.formatting import fmt, fmt_bytes, fmt_mb, fmt_temp, usage_level
from src.utils.paths import config_dir, state_dir


class Sanitize(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(sanitize({}), DEFAULT_CONFIG)

    def test_valid_values_are_kept(self):
        out = sanitize({
            "refresh_interval_seconds": 2,
            "temperature_unit": "f",
            "logging_enabled": False,
            "panels": {"gpu": False},
            "color_scheme": {"low_usage_color": "#00ff00"},
            "thresholds": {"medium_percent": 50, "high_percent": 90},
        })
        self.assertEqual(out["refresh_interval_seconds"], 2.0)
        self.assertEqual(out["temperature_unit"], "F")
        self.assertFalse(out["logging_enabled"])
        self.assertFalse(out["panels"]["gpu"])
        self.assertTrue(out["panels"]["cpu"])
        self.assertEqual(out["color_scheme"]["low_usage_color"], "#00ff00")
        self.assertEqual(out["thresholds"], {"medium_percent": 50.0, "high_percent": 90.0})

    def test_interval_is_clamped(self):
        self.assertEqual(sanitize({"refresh_interval_seconds": 0.001})["refresh_interval_seconds"], 0.5)
        self.assertEqual(sanitize({"refresh_interval_seconds": 99999})["refresh_interval_seconds"], 60.0)

    def test_invalid_types_fall_back(self):
        out = sanitize({
            "refresh_interval_seconds": "rápido",
            "temperature_unit": "K",
            "logging_enabled": "yes",
            "panels": {"cpu": "no", "gpu": 0},
            "color_scheme": {"low_usage_color": "red; } body { display:none"},
            "thresholds": {"medium_percent": 90, "high_percent": 10},
        })
        self.assertEqual(out, DEFAULT_CONFIG)

    def test_boolean_is_not_a_number(self):
        self.assertEqual(sanitize({"refresh_interval_seconds": True})["refresh_interval_seconds"], 1.0)

    def test_nan_is_rejected(self):
        self.assertEqual(sanitize({"refresh_interval_seconds": float("nan")})["refresh_interval_seconds"], 1.0)

    def test_v2_example_key_is_still_honoured(self):
        self.assertEqual(sanitize({"update_interval_seconds": 3})["refresh_interval_seconds"], 3.0)

    def test_unknown_keys_are_dropped(self):
        self.assertNotIn("evil", sanitize({"evil": 1}))


class ConfigFile(unittest.TestCase):
    def _config(self, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        return Config(path)

    def test_missing_file(self):
        self.assertEqual(self._config(None).values, DEFAULT_CONFIG)

    def test_malformed_json(self):
        self.assertEqual(self._config("{ no es json").values, DEFAULT_CONFIG)

    def test_non_object_json(self):
        self.assertEqual(self._config("[1, 2, 3]").values, DEFAULT_CONFIG)

    def test_valid_file(self):
        cfg = self._config(json.dumps({"temperature_unit": "F"}))
        self.assertEqual(cfg.get("temperature_unit"), "F")

    def test_defaults_are_not_shared_between_instances(self):
        a = self._config(None)
        a.values["panels"]["cpu"] = False
        self.assertTrue(DEFAULT_CONFIG["panels"]["cpu"])

    def test_shipped_example_is_fully_effective(self):
        example = Path(__file__).resolve().parent.parent / "config.json.example"
        data = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(sanitize(data), DEFAULT_CONFIG)
        # every key in the example is a key the code understands
        self.assertLessEqual(set(data), set(DEFAULT_CONFIG))


class Paths(unittest.TestCase):
    def test_xdg_variables_are_honoured_when_absolute(self):
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-conf"
        os.environ["XDG_STATE_HOME"] = "relative/ignored"
        self.assertEqual(config_dir(), Path("/tmp/xdg-conf/nvidia-system-monitor"))
        self.assertEqual(state_dir(), Path.home() / ".local/state/nvidia-system-monitor")


class Formatting(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(fmt(None), "N/D")
        self.assertEqual(fmt(12.345, "%"), "12.3%")
        self.assertEqual(fmt(7, " W"), "7 W")

    def test_fmt_mb_and_bytes_use_binary_units(self):
        self.assertEqual(fmt_mb(None), "N/D")
        self.assertEqual(fmt_mb(512), "512 MiB")
        self.assertEqual(fmt_mb(2048), "2.0 GiB")
        self.assertEqual(fmt_bytes(1024 ** 3), "1.0 GiB")
        self.assertEqual(fmt_bytes(1000204886016), "931.5 GiB")

    def test_fmt_temp(self):
        self.assertEqual(fmt_temp(None), "N/D")
        self.assertEqual(fmt_temp(45.25, "C"), "45.2 °C")
        self.assertEqual(fmt_temp(100.0, "F"), "212.0 °F")

    def test_usage_level(self):
        self.assertIsNone(usage_level(None))
        self.assertEqual(usage_level(10), "low")
        self.assertEqual(usage_level(60), "medium")
        self.assertEqual(usage_level(85), "high")
        self.assertEqual(usage_level(70, medium=80, high=95), "low")


if __name__ == "__main__":
    unittest.main()
