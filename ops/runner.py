# 実行管理(運用画面の「4. 製番を指定して実行」と、定期実行の入口)。
# 処理そのものは既存の app.main() をそのまま呼ぶ(処理ロジックを別実装しない)。ここは「いつ・どれを・二重起動なしで呼ぶか」と、
# 結果を履歴に残すことだけを受け持つ。失敗した製番を成功扱いにしない(app.main の results をそのまま状態にする)。
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

from ops.history import COMPLETED, FAILED, KIND_MANUAL, KIND_SCHEDULED, RUNNING, History, sanitize_reason
from ops.settings_store import SettingsError, SettingsStore, validate_product_no

LOCK_FILE = "ops_run.local.lock"
SKIPPED = "スキップ"
ALL_PRODUCTS = "(通常処理)"


class Busy(RuntimeError):
    """実行中のため開始できない(二重起動の防止)。"""


def pid_alive(pid: int) -> bool:
    """プロセスが生きているか。Windows では os.kill(pid, 0) が終了処理になるため使わない。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RunLock:
    """プロセスをまたぐ排他(UI と、タスクスケジューラから起動された定期実行の二重起動を防ぐ)。ロックファイルに PID を書く。"""

    def __init__(self, state_dir, alive: Callable[[int], bool] = pid_alive):
        self.path = Path(state_dir) / LOCK_FILE
        self.alive = alive

    def _holder(self) -> Optional[int]:
        try:
            return int(self.path.read_text(encoding="utf-8").strip().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def is_held(self) -> bool:
        if not self.path.exists():
            return False
        pid = self._holder()
        return pid is not None and self.alive(pid)

    def acquire(self, tag: str = "") -> None:
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pid = self._holder()
                if pid is not None and self.alive(pid):
                    raise Busy("実行中です。完了してから開始してください。")
                try:                       # 持ち主のプロセスがもういない(異常終了の残り)ロックは取り除いて取り直す
                    self.path.unlink()
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{os.getpid()} {tag}\n")
            return
        raise Busy("実行中です。完了してから開始してください。")

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class RunManager:
    def __init__(self, state_dir, store: SettingsStore, history: History, main_func: Callable,
                 config_path: Optional[str] = None, alive: Callable[[int], bool] = pid_alive):
        self.store = store
        self.history = history
        self.main_func = main_func            # app.main と同じ呼び出し形: (config_path=, products=, output_root=) -> results
        self.config_path = config_path
        self.lock = RunLock(state_dir, alive)
        self.current: Optional[dict] = None   # 実行中の {id, kind, product}(このプロセスで開始したもの)

    # ---- 状態 ----
    def status(self) -> dict:
        return {"running": self.lock.is_held(), "current": self.current}

    # ---- 実行 ----
    def _call_main(self, products, output_root):
        kwargs = {"config_path": self.config_path}
        if products is not None:
            kwargs["products"] = products
        if output_root:
            kwargs["output_root"] = output_root
        return self.main_func(**kwargs)

    def _execute(self, run_id: str, products, output_root, product_label: str):
        """app.main を呼び、結果を履歴の状態にする。ここで例外を握って失敗として記録する。"""
        try:
            res = self._call_main(products, output_root)
            status, reason = self._summarize(res, product_label)
        except Exception as e:  # noqa: BLE001 - どんな失敗も「完了」にしない
            status, reason = FAILED, f"{type(e).__name__}: {e}"
        self.history.finish(run_id, status, reason)
        return status

    @staticmethod
    def _summarize(res: dict, product_label: str) -> Tuple[str, str]:
        failed, skipped, done = res.get("failed") or {}, res.get("skipped") or {}, res.get("completed") or []
        if failed:
            detail = " / ".join(f"{p}: {r}" for p, r in list(failed.items())[:5])
            return FAILED, f"完了 {len(done)} 件・失敗 {len(failed)} 件・スキップ {len(skipped)} 件 — {detail}"
        if product_label != ALL_PRODUCTS:
            if done:
                return COMPLETED, ""
            return SKIPPED, " / ".join(f"{p}: {r}" for p, r in list(skipped.items())[:5]) or "処理されませんでした"
        return COMPLETED, f"完了 {len(done)} 件・スキップ {len(skipped)} 件"

    def start_manual(self, product, output_root: Optional[str] = None) -> Tuple[str, threading.Thread]:
        """指定した 1 製番を、別スレッドで 1 回処理する。二重起動は Busy。入力が不正なら SettingsError。"""
        product = validate_product_no(product)
        root = (output_root or "").strip() or self.store.load().output_root
        if root:
            from ops.settings_store import validate_output_root
            root = validate_output_root(root)
        self.lock.acquire("manual")
        try:
            run_id = self.history.start(KIND_MANUAL, product)
        except BaseException:
            self.lock.release()
            raise
        self.current = {"id": run_id, "kind": KIND_MANUAL, "product": product}

        def work():
            try:
                self._execute(run_id, [product], root, product)
            finally:
                self.current = None
                self.lock.release()

        t = threading.Thread(target=work, name="ops-run", daemon=True)
        t.start()
        return run_id, t

    def run_scheduled(self) -> str:
        """定期実行(タスクスケジューラから起動)。通常処理をそのまま 1 回実行する。結果の状態を返す。"""
        try:
            self.lock.acquire("scheduled")
        except Busy:
            rid = self.history.start(KIND_SCHEDULED, ALL_PRODUCTS)
            self.history.finish(rid, SKIPPED, "実行中のため開始しませんでした(二重起動の防止)")
            return SKIPPED
        try:
            root = self.store.load().output_root
            run_id = self.history.start(KIND_SCHEDULED, ALL_PRODUCTS)
            self.current = {"id": run_id, "kind": KIND_SCHEDULED, "product": ALL_PRODUCTS}
            return self._execute(run_id, None, root, ALL_PRODUCTS)
        finally:
            self.current = None
            self.lock.release()
