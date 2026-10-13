# safety.py のテスト。本番共有へのファイルシステム照会が起きないことも確認する(realpath をガード)。
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import safety  # noqa: E402

os.environ["SEKISAN_PRODUCTION_HOSTS"] = "prod-host-example"  # 本番ホスト名はリポジトリに書かない。テスト用の架空ホストを本番扱いにする

PROD_FORMS = [
    r"\\prod-host-example\ShareData\exampleroot",
    r"\\prod-host-example\ShareData\exampleroot\a_product\output\A1BN0001",
    "//prod-host-example/sharedata/exampleroot/a_product",
    "//PROD-HOST-EXAMPLE/ShareData/Exampleroot/a_product/server.yaml",
    r"\\prod-host-example\design-share$\D0000",
    r"\\prod-host-example",
    "\\\\?\\UNC\\prod-host-example\\ShareData\\exampleroot",
]


def _real_realpath():
    return os.path.realpath


class NoShareAccess:
    """本番共有ホスト配下の実体解決(ネットワーク照会)が呼ばれたら失敗させる。"""

    def __enter__(self):
        real = os.path.realpath

        def guarded(p, *a, **k):
            if "prod-host-example" in str(p).lower():
                raise AssertionError(f"本番共有への実体解決が呼ばれた: {p}")
            return real(p, *a, **k)

        self._p = mock.patch("os.path.realpath", guarded)
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


class ProductionPathTest(unittest.TestCase):
    def test_all_forms_detected_without_touching_share(self):
        with NoShareAccess():
            for p in PROD_FORMS:
                self.assertTrue(safety.is_production_path(p), p)
                with self.assertRaises(safety.SafetyError):
                    safety.assert_not_production(p)
                with self.assertRaises(safety.SafetyError):
                    safety.validate_output_root(p, allow_unc=True)  # allow_unc でも本番は拒否

    def test_non_production_paths(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(safety.is_production_path(t))
        self.assertFalse(safety.is_production_path(r"\\other-host\share\x"))
        self.assertFalse(safety.is_production_path(r"C:\prod-host-example\x"))  # ホスト名を含むだけのローカルパスは本番ではない

    def test_unc_refused_by_default_even_if_not_production(self):
        with self.assertRaises(safety.SafetyError):
            safety.validate_output_root(r"\\other-host\verify\out")
        self.assertEqual(safety.validate_output_root(r"\\other-host\verify\out", allow_unc=True), Path(r"\\other-host\verify\out"))

    def test_empty_root_refused(self):
        for v in ("", "  ", None):
            with self.assertRaises(safety.SafetyError):
                safety.validate_output_root(v)


class UnderRootTest(unittest.TestCase):
    def test_under_equal_and_outside(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "root"
            (root / "a").mkdir(parents=True)
            (Path(t) / "rootx").mkdir()
            self.assertTrue(safety.is_under(str(root / "a"), str(root)))
            self.assertFalse(safety.is_under(str(root), str(root)))
            self.assertTrue(safety.is_under(str(root), str(root), allow_equal=True))
            self.assertFalse(safety.is_under(str(Path(t) / "rootx"), str(root)))        # 接頭辞が同じだけの別フォルダ
            self.assertFalse(safety.is_under(str(root / ".." / "rootx"), str(root)))    # .. で脱出
            self.assertFalse(safety.is_under(str(Path(t)), str(root)))

    def test_loopback_and_port(self):
        for h in ("127.0.0.1", "localhost", "::1"):
            safety.validate_loopback(h)
        for h in ("0.0.0.0", "192.0.2.1", "", "example.com"):
            with self.assertRaises(safety.SafetyError):
                safety.validate_loopback(h)
        safety.validate_port(18765)
        for p in (0, 80, 1023, 65536, -1, "18765", None):
            with self.assertRaises(safety.SafetyError):
                safety.validate_port(p)


class ValidateRequestTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = os.path.realpath(os.path.join(self.td.name, "root"))
        os.makedirs(os.path.join(self.root, "A1BN0001"))
        Path(self.root, "A1BN0001", "21.png").write_bytes(b"x")
        self.work = os.path.join(self.root, "A1BN0001")

    def test_ok(self):
        d, page, ext, img = safety.validate_request(self.work, "21", "PNG", self.root)
        self.assertEqual((page, ext), ("21", "png"))
        self.assertTrue(img.endswith("21.png"))

    def test_rejects(self):
        bad_pages = ["", "../21", "21/../22", "2 1", "1234567", "٢١", None, 21]
        for p in bad_pages:
            with self.assertRaises(safety.SafetyError, msg=repr(p)):
                safety.validate_request(self.work, p, "png", self.root)
        for e in ("exe", "", "png/../x", "tiff", None):
            with self.assertRaises(safety.SafetyError, msg=repr(e)):
                safety.validate_request(self.work, "21", e, self.root)
        outside = os.path.join(self.td.name, "outside")
        os.makedirs(outside)
        Path(outside, "21.png").write_bytes(b"x")
        for w in ("A1BN0001", "", None, self.root, outside, os.path.join(self.work, ".."), os.path.join(self.work, "..", "..", "outside"),
                  r"\\prod-host-example\ShareData\exampleroot\a_product\output\A1BN0001", "x\x00y"):
            with NoShareAccess():
                with self.assertRaises(safety.SafetyError, msg=repr(w)):
                    safety.validate_request(w, "21", "png", self.root)

    def test_missing_image_and_dir(self):
        with self.assertRaises(safety.SafetyError):
            safety.validate_request(self.work, "99", "png", self.root)
        with self.assertRaises(safety.SafetyError):
            safety.validate_request(os.path.join(self.root, "nodir"), "21", "png", self.root)


class ProductionHostConfigTest(unittest.TestCase):
    """本番ホスト名はリポジトリに書かず、環境変数・ローカルファイルで与える。未設定なら UNC はすべて本番扱い(安全側)。"""

    def setUp(self):
        self._saved = os.environ.get(safety.PRODUCTION_HOSTS_ENV)
        self.addCleanup(self._restore)
        td = tempfile.TemporaryDirectory(prefix="prod_hosts_")
        self.addCleanup(td.cleanup)
        self.file = Path(td.name) / "hosts.txt"
        # ローカルファイル(実際の本番ホスト名が書かれている場合がある)の影響を受けないよう、テスト中は存在しない場所を指す
        p = mock.patch.object(safety, "PRODUCTION_HOSTS_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(safety.PRODUCTION_HOSTS_ENV, None)
        else:
            os.environ[safety.PRODUCTION_HOSTS_ENV] = self._saved

    def test_unset_means_every_unc_is_production(self):
        os.environ.pop(safety.PRODUCTION_HOSTS_ENV, None)
        self.assertEqual(safety.production_hosts(), ())
        self.assertTrue(safety.is_production_path(r"\\any-host\share\x"))
        self.assertTrue(safety.is_production_path("//any-host/share/x"))
        self.assertFalse(safety.is_production_path(os.path.join(tempfile.gettempdir(), "x")))
        with self.assertRaises(safety.SafetyError):
            safety.validate_output_root(r"\\any-host\share\x", allow_unc=True)   # allow_unc でも、本番ホスト未設定なら使えない

    def test_env_hosts(self):
        os.environ[safety.PRODUCTION_HOSTS_ENV] = "Host-A; host-b,  host-c"
        self.assertEqual(safety.production_hosts(), ("host-a", "host-b", "host-c"))
        self.assertTrue(safety.is_production_path(r"\\HOST-B\x\y"))
        self.assertFalse(safety.is_production_path(r"\\other\x\y"))

    def test_local_file_hosts_are_merged_and_comments_ignored(self):
        os.environ[safety.PRODUCTION_HOSTS_ENV] = "host-a"
        self.file.write_text("# 本番ホスト\nhost-b  # コメント\n\n", encoding="utf-8")
        self.assertEqual(safety.production_hosts(), ("host-a", "host-b"))
        self.assertTrue(safety.is_production_path("//host-b/share"))


if __name__ == "__main__":
    unittest.main()
