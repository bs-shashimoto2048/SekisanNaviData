"""本番推論 API: 設定の検証(待受範囲・許可クライアント・出力ルート・公開先)とモデルの識別・検証。"""
import os
import unittest
from pathlib import Path

from prod_helpers import HOST, ProdBase, WEIGHTS_BYTES, sha  # noqa: F401  (sys.path 設定を含む)
import prod_config
import prod_model


class ConfigTest(ProdBase):
    def err(self, **over):
        with self.assertRaises(prod_config.ConfigError) as cm:
            prod_config.parse_config(self.data(**over))
        return str(cm.exception)

    def test_valid_config(self):
        c = self.cfg()
        self.assertEqual(c.host, HOST)
        self.assertEqual(len(c.allowed_clients), 1)
        self.assertEqual(c.model.id, "test_model_20260101")

    def test_mode_must_be_explicit(self):
        d = self.data()
        del d["mode"]
        with self.assertRaises(prod_config.ConfigError):
            prod_config.parse_config(d)
        self.err(mode="test")

    def test_bind_host_must_be_one_explicit_ipv4(self):
        for bad in ("0.0.0.0", "::", "::1", "localhost", "example.invalid", "224.0.0.1", "169.254.1.1", "255.255.255.255", "", "10.0.0.1/24"):
            with self.subTest(bad=bad):
                self.err(bind={"host": bad, "port": self.port})
        self.assertEqual(self.cfg(bind={"host": "192.0.2.10", "port": self.port}).host, "192.0.2.10")

    def test_port_must_be_fixed(self):
        for bad in (0, 80, 1023, 65536, -1, True, "18080", None):
            with self.subTest(bad=bad):
                self.err(bind={"host": HOST, "port": bad})

    def test_allowed_clients_must_be_explicit_and_narrow(self):
        for bad in ([], None, ["0.0.0.0/0"], ["10.0.0.0/8"], ["10.0.0.0/16"], ["::1"], ["not-an-ip"], ["10.0.0.0/23"]):
            with self.subTest(bad=bad):
                self.err(allowed_clients=bad)
        c = self.cfg(allowed_clients=["10.0.0.0/24", "192.0.2.5"])
        self.assertEqual(len(c.allowed_clients), 2)

    def test_host_bits_in_a_cidr_are_rejected_instead_of_silently_widening(self):
        self.err(allowed_clients=["192.0.2.10/24"])
        self.assertEqual(len(self.cfg(allowed_clients=["192.0.2.0/24", "192.0.2.10/32", "192.0.2.10"]).allowed_clients), 3)

    def test_trailing_newline_or_dot_names_are_rejected(self):
        base = self.data()["model"]
        self.err(model={**base, "id": "abc\n"})
        self.err(model={**base, "weights": "best.pt\n"})
        self.err(model={**base, "weights": ".."})
        self.err(model={**base, "yaml": "."})
        self.err(model={**base, "expected": {**base["expected"], "weights_sha256": ("a" * 64) + "\n"}})

    def test_client_allowed(self):
        c = self.cfg(allowed_clients=["10.0.0.0/24", "192.0.2.5"])
        self.assertTrue(prod_config.client_allowed(c, "10.0.0.77"))
        self.assertTrue(prod_config.client_allowed(c, "192.0.2.5"))
        for bad in ("10.0.1.1", "192.0.2.6", "::1", "", None, "garbage"):
            with self.subTest(bad=bad):
                self.assertFalse(prod_config.client_allowed(c, bad))

    def test_output_root_must_be_absolute_and_below_share_or_drive_root(self):
        for bad in ("relative\\out", "", "\\\\host\\share", "\\\\host\\share\\", "C:\\", "C:/", "\\\\host\\share\\..\\x"):
            with self.subTest(bad=bad):
                self.err(output_root=bad)
        self.assertTrue(self.cfg(output_root="\\\\host\\share\\a\\out").output_root)

    def test_publish_target_must_be_named_server_yaml(self):
        d = self.data()
        d["publish"] = {**d["publish"], "server_yaml": str(self.pub_dir / "other.yaml")}
        with self.assertRaises(prod_config.ConfigError):
            prod_config.parse_config(d)
        d["publish"] = {**d["publish"], "server_yaml": "relative/server.yaml"}
        with self.assertRaises(prod_config.ConfigError):
            prod_config.parse_config(d)

    def test_model_fields_are_strict(self):
        base = self.data()["model"]
        cases = [{**base, "weights": "sub\\best.pt"}, {**base, "yaml": "../model.yaml"}, {**base, "id": "bad id!"},
                 {**base, "dir": "relative"}, {**base, "expected": {**base["expected"], "weights_sha256": "abc"}},
                 {**base, "expected": {**base["expected"], "weights_size": 0}}, {**base, "expected": {}}]
        for m in cases:
            with self.subTest(m=m):
                self.err(model=m)

    def test_errors_never_contain_the_configured_values(self):
        msg = self.err(bind={"host": "0.0.0.0", "port": self.port}) + self.err(output_root="\\\\secret-host\\share") \
            + self.err(allowed_clients=["10.20.30.0/16"])
        for secret in ("secret-host", "10.20.30", str(self.port)):
            self.assertNotIn(secret, msg)

    def test_load_config_errors_are_generic(self):
        with self.assertRaises(prod_config.ConfigError):
            prod_config.load_config(str(self.root / "missing.yaml"))
        bad = self.root / "bad.yaml"
        bad.write_text("a: [unclosed", encoding="utf-8")
        with self.assertRaises(prod_config.ConfigError):
            prod_config.load_config(str(bad))


class ModelTest(ProdBase):
    def ident(self):
        return prod_model.identify("m", str(self.model_dir), "best.pt", "model.yaml")

    def test_identify_and_verify_ok(self):
        i = self.ident()
        prod_model.verify(i, sha(WEIGHTS_BYTES), len(WEIGHTS_BYTES), sha((self.model_dir / "model.yaml").read_bytes()))
        pub = i.public()
        self.assertEqual(pub["weights_sha256_12"], sha(WEIGHTS_BYTES)[:12])
        self.assertNotIn(sha(WEIGHTS_BYTES), str(pub))           # 全文は公開しない

    def test_verify_rejects_each_mismatch_with_fixed_messages(self):
        i = self.ident()
        ys = sha((self.model_dir / "model.yaml").read_bytes())
        for args in ((sha(WEIGHTS_BYTES), len(WEIGHTS_BYTES) + 1, ys), ("0" * 64, len(WEIGHTS_BYTES), ys), (sha(WEIGHTS_BYTES), len(WEIGHTS_BYTES), "0" * 64)):
            with self.subTest(args=args[1:]):
                with self.assertRaises(prod_model.ModelVerificationError) as cm:
                    prod_model.verify(i, *args)
                self.assertNotIn(sha(WEIGHTS_BYTES), str(cm.exception))

    def test_missing_or_directory_files_are_rejected(self):
        (self.model_dir / "best.pt").unlink()
        with self.assertRaises(prod_model.ModelVerificationError):
            self.ident()
        (self.model_dir / "best.pt").mkdir()
        with self.assertRaises(prod_model.ModelVerificationError):
            self.ident()

    def stage(self, **over):
        args = dict(model_id="m", model_dir=str(self.model_dir), weights="best.pt", yaml_name="model.yaml",
                    expected_weights_sha256=sha(WEIGHTS_BYTES), expected_weights_size=len(WEIGHTS_BYTES),
                    expected_yaml_sha256=sha((self.model_dir / "model.yaml").read_bytes()), staging_parent=str(self.root))
        args.update(over)
        return prod_model.stage_and_verify(**args)

    def test_stage_copies_exactly_the_verified_bytes_and_read_only(self):
        ident, staged = self.stage()
        self.addCleanup(prod_model.remove_staging, staged)
        self.assertEqual((Path(staged) / "best.pt").read_bytes(), WEIGHTS_BYTES)
        self.assertEqual(ident.weights_sha256, sha(WEIGHTS_BYTES))
        with self.assertRaises(PermissionError):
            (Path(staged) / "best.pt").write_bytes(b"x")                    # 読み取り専用
        prod_model.remove_staging(staged)
        self.assertFalse(Path(staged).exists())                              # 読み取り専用でも削除できる

    def test_stage_leaves_no_directory_on_any_failure(self):
        before = {p.name for p in self.root.iterdir()}
        for over in ({"expected_weights_sha256": "0" * 64}, {"expected_weights_size": 1}, {"expected_yaml_sha256": "0" * 64},
                     {"weights": "missing.pt"}):
            with self.subTest(over=list(over)):
                with self.assertRaises(prod_model.ModelVerificationError) as cm:
                    self.stage(**over)
                self.assertNotIn(sha(WEIGHTS_BYTES), str(cm.exception))
        self.assertEqual({p.name for p in self.root.iterdir()}, before)

    def test_the_source_is_read_once_so_a_swap_after_staging_does_not_matter(self):
        _, staged = self.stage()
        self.addCleanup(prod_model.remove_staging, staged)
        (self.model_dir / "best.pt").write_bytes(b"malicious-replacement")    # 検証・コピーの後に元を差し替える
        self.assertEqual((Path(staged) / "best.pt").read_bytes(), WEIGHTS_BYTES)   # 読み込むのは検証済みのコピー
        prod_model.check_yaml_matches_weights(staged, "model.yaml", "best.pt")

    def test_symlinked_weights_are_rejected(self):
        real = self.root / "real.pt"
        real.write_bytes(WEIGHTS_BYTES)
        (self.model_dir / "best.pt").unlink()
        try:
            os.symlink(real, self.model_dir / "best.pt")
        except (OSError, NotImplementedError):
            self.skipTest("symlink を作れない環境")
        with self.assertRaises(prod_model.ModelVerificationError):
            self.ident()

    def test_yaml_must_name_the_configured_weights(self):
        prod_model.check_yaml_matches_weights(str(self.model_dir), "model.yaml", "best.pt")
        with self.assertRaises(prod_model.ModelVerificationError):
            prod_model.check_yaml_matches_weights(str(self.model_dir), "model.yaml", "other.pt")
        (self.model_dir / "model.yaml").write_text("model: {}\n", encoding="utf-8")
        with self.assertRaises(prod_model.ModelVerificationError):
            prod_model.check_yaml_matches_weights(str(self.model_dir), "model.yaml", "best.pt")

    def test_change_after_verification_is_detected(self):
        i = self.ident()
        self.assertTrue(prod_model.unchanged_since(i, str(self.model_dir), "best.pt", "model.yaml", rehash=True))
        p = self.model_dir / "best.pt"
        data = bytearray(p.read_bytes())
        data[0] ^= 0xFF                                           # 同じサイズのまま内容だけ変える
        st = p.stat()
        p.write_bytes(bytes(data))
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))          # 更新時刻も元へ戻す → 再ハッシュだけが検知できる
        self.assertTrue(prod_model.unchanged_since(i, str(self.model_dir), "best.pt", "model.yaml"))
        self.assertFalse(prod_model.unchanged_since(i, str(self.model_dir), "best.pt", "model.yaml", rehash=True))
        p.write_bytes(bytes(data) + b"x")
        self.assertFalse(prod_model.unchanged_since(i, str(self.model_dir), "best.pt", "model.yaml"))


if __name__ == "__main__":
    unittest.main()
