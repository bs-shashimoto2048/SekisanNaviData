# 暫定 API 操作画面の「製番の指定実行」と「全製番の仮定期実行」(標準ライブラリ + PyYAML。既存の ops/ の実行管理・履歴・タスク連携を再利用する)。
#
# 設計の要点:
# - 製番処理そのものは既存の app.main()(→ pipeline.product_flow.run_product)をそのまま呼ぶ。処理ロジックを重複実装しない。
#   失敗時の製番フォルダの後始末(出力ルート配下に限る削除)・処理済み判定・対象判定は、既存の処理に任せる。ここでは削除しない。
# - 出力先は、専用設定 app_interim.yaml の data.output が ./output_interim の内側であることを、**実行のたびに**検証する
#   (表記上の位置で判定し、リンク/ジャンクション/UNC/ネットワークドライブを拒否。共有の出力先には書かない)。app.yaml / app.local.yaml は読まない。
#   app.main へは output_root として、検証済みの**絶対パス**の出力ルートを渡す(外部 API モードではアプリが相対の data.output を絶対パス化せず、
#   相対の work_dir が API に拒否されるため。場所は設定どおりで、表記だけ絶対パスにする)。work_dir は実行前に隔離ルート基準で検証する。
# - 実行管理(二重起動の防止・COM の初期化と終了・履歴・SystemExit の扱い)は ops.runner.RunManager をそのまま使う。状態・履歴・ロックは
#   隔離領域 output_interim/run_state に置く(運用画面 ops_console の ops.local.yaml / ops_history.local.jsonl とは別。いずれも Git 管理外)。
# - 仮定期実行のタスクは、通常の定期タスク(SekisanNaviData_Ops)とは別名(SekisanNaviData_InterimOps)・別の起動入口(interim_console.py run-scheduled)。
#   既存の通常タスクは参照も変更もしない。同名で、この画面のものではないタスクは、上書きも解除もしない。
# - 画面・応答に出すのは、隔離先からの相対パス・状態・固定文言・ファイル名とサイズだけ(絶対パス・設定値・例外メッセージは出さない)。
import os
import re
from pathlib import Path
from typing import Callable, Optional

import yaml

from interim import pathsafe
from ops.history import History, sanitize_reason
from ops.runner import Busy, RunManager
from ops.scheduler import SchedulerError, TaskScheduler
from ops.settings_store import SettingsError, SettingsStore, validate_interval, validate_product_no, validate_time

ISOLATED_DIRNAME = "output_interim"
RUN_STATE_DIRNAME = "run_state"
RUN_CONFIG_NAME = "app_interim.yaml"
TASK_NAME = "SekisanNaviData_InterimOps"
PRODUCTION_TASK_NAME = "SekisanNaviData_Ops"           # 通常の定期タスク。この画面は一切操作しない
SCRIPT_NAME = "interim_console.py"
DEFAULT_INTERVAL_DAYS = 2                               # 合意済みの既定(1 日置き)
LOGON_ONLY_NOTICE = "タスクは、この PC にログオンしている間だけ動きます(ログオフ中・スリープ中は動きません)。"
MAX_LISTED_FILES = 60

MESSAGES = {
    "api_not_ready": "推論 API が準備完了ではありません。先に「API の状態」で起動し、準備完了にしてください。",
    "busy": "製番処理を実行中です。完了してから開始してください。",
    "isolation": "出力先が隔離領域(output_interim)であることを確認できないため、実行しません。",
    "work_dir": "処理対象フォルダ(work_dir)を、隔離領域(output_interim)内の絶対パスとして確認できないため、実行しません。",
    "bad_product": "製番は英数字 4〜20 文字で指定してください。",
    "task_foreign": "同名のタスクが既にあり、この画面が登録したものではないため、上書き・解除しません。",
}


_EXC_DETAIL = re.compile(r"((?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Failure))\s*:\s*.*?(?=\s+/\s+|$)")


def safe_reason(text) -> str:
    """履歴・画面に出す理由。例外のメッセージ(パス・ホスト名・接続先を含み得る)は落とし、例外の種類名だけを残す。固定の文言(処理済み・事前確認の理由など)はそのまま。"""
    return sanitize_reason(_EXC_DETAIL.sub(r"\1", "" if text is None else str(text)))


class SafeHistory(History):
    """この画面の履歴。理由は、書き込み時に safe_reason を通す(例外メッセージを、履歴ファイルにも残さない)。"""

    def finish(self, run_id: str, status: str, reason: str = ""):
        super().finish(run_id, status, safe_reason(reason))


class RunRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class IsolationError(RuntimeError):
    """出力先が隔離領域でない(固定の文言。パスは含めない)。"""


class TaskForeignError(SchedulerError):
    """同名のタスクが、この画面のものではない(上書き・解除しない)。"""


def validate_run_config(config_path, repo_root, *, net_drive=pathsafe.is_network_drive) -> dict:
    """専用設定を検証して {storage, product_root(絶対パス), product_root_rel} を返す。違反は IsolationError(固定の文言)。通信・書き込みはしない。"""
    repo_root = Path(repo_root).resolve()
    isolated = os.path.normpath(str(repo_root / ISOLATED_DIRNAME))
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = repo_root / cfg_path
    try:
        cfg_real = cfg_path.resolve()
        ok_name = cfg_real.name == RUN_CONFIG_NAME and pathsafe.is_under(cfg_real, repo_root)
        config = yaml.safe_load(cfg_real.read_text(encoding="utf-8")) if ok_name else None
    except Exception:  # noqa: BLE001 - メッセージ(パスを含み得る)は残さない
        raise IsolationError(MESSAGES["isolation"]) from None
    if not isinstance(config, dict) or (config.get("inference_server") or {}).get("enabled"):
        raise IsolationError(MESSAGES["isolation"])                     # 設定が読めない・統合モード(アプリが API を起動する別経路)は不可
    data, api = config.get("data") or {}, config.get("api") or {}
    output, storage = data.get("output"), data.get("storage")
    if not (isinstance(output, str) and output and isinstance(storage, str) and storage and Path(storage).name == storage
            and not pathsafe._is_unc_text(output) and api.get("use_proxy") is False):
        raise IsolationError(MESSAGES["isolation"])
    for value in (output, api.get("dir_venv")):
        if not isinstance(value, str) or not value or pathsafe._is_unc_text(value):
            raise IsolationError(MESSAGES["isolation"])
        p = Path(value)
        full = os.path.normpath(str(p if p.is_absolute() else repo_root / p))
        if (not pathsafe.lexically_within(full, isolated) or pathsafe.has_link_in_path(full, repo_root)
                or pathsafe.has_link_in_path(isolated, repo_root) or net_drive(full)):
            raise IsolationError(MESSAGES["isolation"])
    base = Path(output) if Path(output).is_absolute() else repo_root / output
    root = Path(os.path.normpath(str(base / storage / "output")))
    if pathsafe.has_link_in_path(root, repo_root):
        raise IsolationError(MESSAGES["isolation"])
    # output_root: app.main に渡す絶対パスの出力ルート(設定の data.output を、作業ディレクトリではなくリポジトリ基準で解決したもの)。
    # 外部 API モードでは、アプリは data.output を絶対パス化しない。相対のままだと work_dir が相対になり、API に拒否される。
    output_root = Path(os.path.normpath(str(base)))
    return {"storage": storage, "product_root": root, "product_root_rel": root.relative_to(repo_root).as_posix(),
            "output_root": output_root, "isolated_root": Path(isolated)}


def work_dir_for(target: dict, product: str) -> str:
    """app.main が推論 API に渡す work_dir(= 出力ルート/storage/output/製番)を、実行前に組み立てて検証する。
    絶対パスで、API が使う出力ルート(output_interim。API 起動時の --output-root と同じ基準)の配下に実体解決後も収まることを確認する。違反は IsolationError。"""
    output_root, isolated = str(target["output_root"]), str(target["isolated_root"])
    try:
        product = validate_product_no(product)
    except SettingsError:
        raise IsolationError(MESSAGES["work_dir"]) from None
    work_dir = os.path.join(output_root, target["storage"], "output", product)
    if (not os.path.isabs(output_root) or not os.path.isabs(work_dir) or os.path.normpath(work_dir) != work_dir
            or os.path.normpath(os.path.join(str(target["product_root"]), product)) != work_dir       # 検証済みの製番フォルダと同一
            or not pathsafe.lexically_within(work_dir, isolated)
            or not pathsafe.is_under(work_dir, isolated)                                             # 実体解決後(リンク経由)も隔離ルート配下
            or pathsafe.has_link_in_path(work_dir, isolated)):                                       # 製番フォルダ自身がリンクになっていない
        raise IsolationError(MESSAGES["work_dir"])
    return work_dir


class InterimScheduler(TaskScheduler):
    """仮定期実行のタスク(専用名)。既存の TaskScheduler(schtasks の呼び出し)を、専用名・専用の起動入口で使い、
    この画面のものではない同名タスクは上書き・解除しない。通常の定期タスクの名前は受け付けない。"""

    def __init__(self, python_exe: str, script_path: str, task_name: str = TASK_NAME, **kw):
        if task_name.strip().lower() == PRODUCTION_TASK_NAME.lower() or not task_name.startswith("SekisanNaviData_Interim"):
            raise ValueError("仮定期実行は、専用のタスク名だけを扱います。")
        super().__init__(python_exe, script_path, task_name=task_name, **kw)

    def _query_xml(self) -> Optional[str]:
        from ops.scheduler import _decode
        res = self.runner(self.query_args())
        return _decode(res.stdout) if res.returncode == 0 else None

    def _assert_not_foreign(self):
        xml = self._query_xml()
        if xml is not None and SCRIPT_NAME not in xml:
            raise TaskForeignError(MESSAGES["task_foreign"])

    def register(self, hhmm: str, interval_days: int, enabled: bool):
        self._assert_not_foreign()
        super().register(hhmm, interval_days, enabled)

    def delete(self):
        if self._query_xml() is None:
            return                                                      # 登録されていない(何もしない)
        self._assert_not_foreign()
        super().delete()

    def status(self) -> dict:
        st = super().status()
        if st.get("registered"):
            xml = self._query_xml()
            st["foreign"] = bool(xml is not None and SCRIPT_NAME not in xml)
        else:
            st["foreign"] = False
        return st


class InterimRuns:
    def __init__(self, repo, *, main_func: Optional[Callable] = None, scheduler_runner: Optional[Callable] = None,
                 python_exe: Optional[str] = None, api_ready: Callable[[], bool] = lambda: True, net_drive=pathsafe.is_network_drive,
                 **runner_kw):
        self.repo = Path(repo).resolve()
        self.config_path = self.repo / RUN_CONFIG_NAME
        self.state_dir = self.repo / ISOLATED_DIRNAME / RUN_STATE_DIRNAME
        self._main_func = main_func
        self._api_ready = api_ready
        self._net_drive = net_drive
        self._ensure_state_dir()
        self.store = SettingsStore(self.state_dir)
        self.history = SafeHistory(self.state_dir)
        self.runs = RunManager(self.state_dir, self.store, self.history, self._guarded_main, config_path=str(self.config_path), **runner_kw)
        import sys
        sched_kw = {"runner": scheduler_runner} if scheduler_runner else {}
        self.scheduler = InterimScheduler(python_exe or sys.executable, str(self.repo / SCRIPT_NAME), **sched_kw)

    # ---- 隔離 ----
    def _ensure_state_dir(self):
        if pathsafe.has_link_in_path(self.state_dir, self.repo):
            raise IsolationError(MESSAGES["isolation"])
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def target(self) -> dict:
        return validate_run_config(self.config_path, self.repo, net_drive=self._net_drive)

    def _guarded_main(self, **kw):
        """RunManager が呼ぶ入口。出力先の隔離を実行のたびに検証してから、既存の app.main を呼ぶ。
        外部 API モードではアプリが data.output を絶対パス化しないため、検証済みの絶対パスの出力ルートを output_root として渡す
        (場所は設定の data.output と同じ。表記だけ絶対パスにする)。work_dir の検証に失敗したら、app.main(重い処理)へ進まない。"""
        t = self.target()                                                # 違反なら IsolationError(履歴には固定の文言で「失敗」として残る)
        for product in kw.get("products") or []:                         # 指定実行: 製番ごとの work_dir を、API の出力ルート基準で事前に検証
            work_dir_for(t, product)
        if not os.path.isabs(str(t["output_root"])):
            raise IsolationError(MESSAGES["work_dir"])
        self._check_cwd()
        kw["output_root"] = str(t["output_root"])
        return self._resolve_main()(**kw)

    def _check_cwd(self):
        """実の app.main を呼ぶときだけ: アプリは api.dir_venv(相対)を作業ディレクトリ基準で読むため、作業ディレクトリがリポジトリであることを確認する
        (別の場所の server.yaml を読んで別の API へ要求しない)。"""
        if self._main_func is None and os.path.normcase(os.path.realpath(os.getcwd())) != os.path.normcase(str(self.repo)):
            raise IsolationError(MESSAGES["work_dir"])

    def _resolve_main(self) -> Callable:
        if self._main_func is not None:
            return self._main_func
        import app                                                       # 重い依存(bs_pkg など)は、実行するときだけ読み込む
        return app.main

    # ---- 1 製番の指定実行 ----
    def target_for(self, product) -> dict:
        try:
            p = validate_product_no(product)
        except SettingsError:
            raise RunRefused("bad_product") from None
        t = self.target()
        folder = t["product_root"] / p
        return {"product": p, "output_dir": f"{t['product_root_rel']}/{p}", "exists": folder.is_dir(),
                "isolated": True, "note": "既にこの製番の出力がある場合は、処理済みとして「スキップ」になります。"}

    def start(self, product) -> dict:
        info = self.target_for(product)                                  # 製番の形式・隔離の検証
        if not self._api_ready():
            raise RunRefused("api_not_ready")
        try:
            run_id, _ = self.runs.start_manual(info["product"], None)
        except Busy:
            raise RunRefused("busy") from None
        return {"run_id": run_id, **info}

    def running(self) -> bool:
        return self.runs.lock.is_held()

    def files(self, product) -> list:
        """製番フォルダの生成ファイル(相対名とサイズだけ。リンクは辿らない)。"""
        try:
            t = self.target()
            p = validate_product_no(product)
        except (IsolationError, SettingsError):
            return []
        root = t["product_root"] / p
        out = []
        if not root.is_dir() or pathsafe.is_link(root):
            return out
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not pathsafe.is_link(os.path.join(dp, d))]
            for n in sorted(fn):
                full = os.path.join(dp, n)
                if pathsafe.is_link(full):
                    continue
                out.append({"name": Path(full).relative_to(root).as_posix(), "size": os.path.getsize(full)})
                if len(out) >= MAX_LISTED_FILES:
                    return out
        return out

    def state(self, product: Optional[str] = None) -> dict:
        """実行状態(実行中か・直近の履歴・指定製番の生成ファイル)。"""
        runs = self.history.runs(limit=10)
        cur = self.runs.current
        data = {"running": self.running(), "current": ({"kind": cur["kind"], "product": cur["product"]} if cur else None),
                "history": [{"started": r.get("started"), "finished": r.get("finished"), "kind": r.get("kind"), "product": r.get("product"),
                             "status": r.get("status"), "reason": safe_reason(r.get("reason", ""))} for r in runs]}
        if product:
            data["files"] = self.files(product)
        return data

    # ---- 仮定期実行 ----
    def schedule_state(self) -> dict:
        s = self.store.load()
        st = self.scheduler.status()
        return {"task_name": TASK_NAME, "registered": st["registered"], "enabled": st["enabled"], "time": st["time"] or s.schedule_time,
                "interval_days": st["interval_days"] or s.schedule_interval_days or DEFAULT_INTERVAL_DAYS, "foreign": st["foreign"],
                "default_interval_days": DEFAULT_INTERVAL_DAYS, "logon_only": LOGON_ONLY_NOTICE, "scope": "全対象製番(通常処理)",
                "output_dir": self.target()["product_root_rel"], "separate_from_production_task": True}

    def schedule_register(self, hhmm, interval_days) -> dict:
        try:
            t, n = validate_time(hhmm), validate_interval(interval_days)
        except SettingsError:
            raise RunRefused("bad_schedule") from None
        self.target()                                                    # 隔離されていないときは登録しない
        self.scheduler.register(t, n, True)
        self.store.save({"schedule_enabled": True, "schedule_time": t, "schedule_interval_days": n})
        return self.schedule_state()

    def schedule_unregister(self) -> dict:
        self.scheduler.delete()
        cur = self.store.load()
        self.store.save({"schedule_enabled": False, "schedule_time": cur.schedule_time, "schedule_interval_days": cur.schedule_interval_days})
        return self.schedule_state()

    def run_scheduled(self, dry_run: bool = False) -> str:
        """タスクスケジューラからの起動入口(interim_console.py run-scheduled)。全対象製番を通常処理で 1 回実行する。"""
        if dry_run:
            return self.runs.run_scheduled_dry_run()
        return self.runs.run_scheduled()

    def mark_orphans(self):
        """画面サーバーの起動時: 別プロセス(定期実行)が実行中でなければ、終わりが書かれず「実行中」のまま残った履歴を失敗にする。"""
        if not self.running():
            self.history.mark_orphans_failed()
