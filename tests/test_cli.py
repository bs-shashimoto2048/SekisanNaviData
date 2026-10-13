# 起動入口 app._cli の終了コードのテスト(app.main はモック。ホスト・設計サーバー・推論サーバーには何もしない)
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import inference_support  # noqa: E402


class CliTest(unittest.TestCase):
    def run_cli(self, argv, **main_kw):
        err = io.StringIO()
        with mock.patch.object(app, "main", **main_kw) as m, contextlib.redirect_stderr(err):
            code = app._cli(argv)
        return code, m, err.getvalue()

    def test_default_config_is_none_so_app_yaml_is_used(self):
        code, m, _ = self.run_cli([], return_value={"completed": ["A1BN0001"], "failed": {}, "skipped": {}})
        self.assertEqual(code, 0)
        m.assert_called_once_with(config_path=None)

    def test_config_is_passed_through(self):
        code, m, _ = self.run_cli(["--config", "app_test.yaml"], return_value={"completed": [], "failed": {}, "skipped": {}})
        self.assertEqual(code, 0)
        m.assert_called_once_with(config_path="app_test.yaml")

    def test_failed_product_gives_exit_code_1(self):
        code, _, _ = self.run_cli([], return_value={"completed": [], "failed": {"A1BN0001": "InferenceRequestError: x"}, "skipped": {}})
        self.assertEqual(code, 1)

    def test_server_not_ready_gives_exit_code_1_with_reason_on_stderr(self):
        code, _, err = self.run_cli([], side_effect=inference_support.InferenceServerError("準備に失敗しました: テスト"))
        self.assertEqual(code, 1)
        self.assertIn("準備に失敗しました: テスト", err)


if __name__ == "__main__":
    unittest.main()
