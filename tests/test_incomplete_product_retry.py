"""失敗後に残った製番フォルダだけを全件実行で再処理対象へ戻す。外部接続なし。"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import product_flow as pf


PRODUCT = "A1BN0001"


class IncompleteRetryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="incomplete_retry_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / "output"
        self.output.mkdir()
        self.folder = self.output / PRODUCT
        self.logger = mock.Mock(name="logger")
        self.ctx = pf.ProductContext(
            product=PRODUCT, output_dir=str(self.output), dir_product=str(self.folder), logger=self.logger,
            config_data={}, target_initial="A1", box_types=("OS",), converter_path="", converter_version="",
            converter_extension=".dwg", converted_extension=".dxf")

    def results(self):
        return {"completed": [], "failed": {}, "skipped": {}}

    def test_marker_name_is_opaque_and_creation_is_idempotent(self):
        marker = Path(pf.incomplete_marker_path(str(self.output), PRODUCT))
        self.assertEqual(marker.name, ".sekisan-incomplete-" + hashlib.sha256(PRODUCT.encode()).hexdigest() + ".marker")
        self.assertNotIn(PRODUCT, marker.name)
        self.assertFalse(pf.has_incomplete_marker(str(self.output), PRODUCT))
        pf.mark_incomplete(str(self.output), PRODUCT)
        self.assertTrue(pf.has_incomplete_marker(str(self.output), PRODUCT))
        pf.mark_incomplete(str(self.output), PRODUCT)  # 既存マーカーを壊さない
        self.assertEqual(marker.read_bytes(), b"")

    def test_existing_regular_marker_is_treated_as_pending(self):
        marker = Path(pf.incomplete_marker_path(str(self.output), PRODUCT))
        marker.write_bytes(b"partial")
        self.assertTrue(pf.has_incomplete_marker(str(self.output), PRODUCT))

    def test_incomplete_products_are_added_once_in_candidate_order(self):
        pf.mark_incomplete(str(self.output), PRODUCT)
        result = pf.include_incomplete_products(str(self.output), ["A1BN0001", "A1BN0002", "A1BN0003"], ["A1BN0002"])
        self.assertEqual(result, ["A1BN0002", "A1BN0001"])

    def test_unmarked_existing_product_is_not_reincluded(self):
        self.folder.mkdir()
        result = pf.include_incomplete_products(str(self.output), [PRODUCT], [])
        self.assertEqual(result, [])

    def test_existing_incomplete_folder_is_removed_before_retry(self):
        self.folder.mkdir()
        pf.mark_incomplete(str(self.output), PRODUCT)
        self.rmtree = mock.patch.object(pf.shutil, "rmtree", side_effect=lambda path: Path(path).rmdir())
        self.rmtree.start()
        self.addCleanup(self.rmtree.stop)
        with mock.patch.object(pf.host_mod, "get_property_data", return_value=("S001", "OS2K")):
            result = pf.check_product(self.ctx)
        self.assertIsNone(result)
        self.assertFalse(self.folder.exists())
        self.assertTrue(pf.has_incomplete_marker(str(self.output), PRODUCT))

    def test_locked_incomplete_folder_fails_without_being_skipped(self):
        self.folder.mkdir()
        pf.mark_incomplete(str(self.output), PRODUCT)
        with mock.patch.object(pf.shutil, "rmtree", side_effect=PermissionError("private path")):
            with mock.patch.object(pf.host_mod, "get_property_data", return_value=("S001", "OS2K")):
                result = pf.check_product(self.ctx)
        self.assertEqual(result, pf.ProductResult(pf.FAILED, "前回の未完了フォルダを片付けられません(再処理待ち)"))
        self.assertTrue(self.folder.exists())
        self.assertNotIn(str(self.folder), result.reason)

    def test_run_product_keeps_marker_if_failed_cleanup_leaves_folder(self):
        self.folder.mkdir()
        pf.mark_incomplete(str(self.output), PRODUCT)
        with mock.patch.object(pf, "process_product", return_value=pf.ProductResult(pf.FAILED, "Step failure", cleanup=True)):
            with mock.patch.object(pf.shutil, "rmtree"):
                results = self.results()
                pf.run_product(self.ctx, results)
        self.assertIn(PRODUCT, results["failed"])
        self.assertTrue(self.folder.exists())
        self.assertTrue(pf.has_incomplete_marker(str(self.output), PRODUCT))
        self.assertIn(PRODUCT, pf.include_incomplete_products(str(self.output), [PRODUCT], []))

    def test_exception_cleanup_keeps_marker_when_removal_fails(self):
        self.folder.mkdir()
        pf.mark_incomplete(str(self.output), PRODUCT)
        with mock.patch.object(pf, "process_product", side_effect=ValueError("private path")):
            with mock.patch.object(pf.shutil, "rmtree", side_effect=PermissionError("private path")):
                results = self.results()
                pf.run_product(self.ctx, results)
        self.assertEqual(results["failed"][PRODUCT].split(":", 1)[0], "ValueError")
        self.assertTrue(self.folder.exists())
        self.assertTrue(pf.has_incomplete_marker(str(self.output), PRODUCT))

    def test_run_product_clears_marker_after_success(self):
        pf.mark_incomplete(str(self.output), PRODUCT)
        with mock.patch.object(pf, "process_product", return_value=pf.ProductResult(pf.COMPLETED)):
            results = self.results()
            pf.run_product(self.ctx, results)
        self.assertEqual(results["completed"], [PRODUCT])
        self.assertFalse(pf.has_incomplete_marker(str(self.output), PRODUCT))

    def test_run_product_marks_complete_as_failure_if_marker_cannot_clear(self):
        pf.mark_incomplete(str(self.output), PRODUCT)
        with mock.patch.object(pf, "process_product", return_value=pf.ProductResult(pf.COMPLETED)):
            with mock.patch.object(pf, "clear_incomplete_marker", side_effect=OSError("secret path")):
                with mock.patch.object(pf.shutil, "rmtree"):
                    results = self.results()
                    pf.run_product(self.ctx, results)
        self.assertEqual(results["failed"], {PRODUCT: "完了状態を記録できません(未完了)"})
        self.assertEqual(results["completed"], [])
        self.assertNotIn("secret path", str(results))


if __name__ == "__main__":
    unittest.main()
