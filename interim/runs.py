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
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import yaml

from interim import pathsafe
from interim.taskinfo import default_info_runner
from ops.history import KIND_SCHEDULED, History, sanitize_reason
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


# ---- 仮定期実行の結果表示(画面の「前回の結果」欄。履歴と Task Scheduler の情報だけを使い、製番・パス・ホスト名・ログ原文は出さない) ----
# Task Scheduler の LastTaskResult の意味(固定文言)。0x41301 = 実行中、0x41303 = まだ実行されていない。
TASK_RESULT_RUNNING, TASK_RESULT_NEVER = 267009, 267011
STALE_MARGIN = timedelta(hours=3)                         # 前回の起動から「間隔 + この時間」を過ぎても起動していなければ、予定どおりでない可能性として示す
SOON = timedelta(hours=24)                                # 次回の実行まで、この時間以内なら、API・電源の準備を促す
_SUMMARY = re.compile(r"^完了 (\d+) 件(?:・失敗 (\d+) 件)?・スキップ (\d+) 件(?: — (.*))?$")
_ITEM = re.compile(r"^[^:/]{1,40}: (.*)$")
_STEP2_STAGE = re.compile(r"\[Step\.2\] (印刷\(PDF\)|DXF 変換|JPEG 変換|PNG 前処理)の成果物が不足")
_EXC_TOKEN = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure))\b")
_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")


def format_last_result(code) -> str:
    """終了コードの、固定文言の説明(画面用)。"""
    if code is None:
        return ""
    if code == 0:
        return "正常終了"
    if code == TASK_RESULT_RUNNING:
        return "実行中"
    if code == TASK_RESULT_NEVER:
        return "まだ実行されていません"
    if code == 1:
        return "失敗を含む(製番ごとの失敗は履歴で確認)"
    return "異常終了(起動できなかった・途中で停止した可能性)"


def classify_failure(why: str) -> str:
    """失敗理由(履歴の文字列)を、固定のラベルに分類する。製番・パス・例外メッセージは返さない(返すのは固定文言か例外のクラス名だけ)。"""
    w = why or ""
    m_stage = _STEP2_STAGE.search(w)
    if m_stage:                                             # Step.2 の成果物の検査(印刷 PDF・DXF・JPEG・PNG のどの段階か。件数は履歴の理由で確認)
        return f"Step.2 の成果物が不足({m_stage.group(1)})"
    if "boxspec" in w or "material.csv" in w:
        return "材料情報(*.boxspec)が無い・作れない(設計データ不足の可能性)"
    if "design.csv" in w:
        return "設計情報(design.csv)を作れない"
    if "InferenceRequestError" in w:
        return "推論 API への要求の失敗(InferenceRequestError)"
    if "推論 API を利用できない" in w or "/health" in w:
        return "推論 API を利用できない(事前確認で停止)"
    if "ホスト情報" in w:
        return "ホスト情報を取得できない"
    if "COM" in w:
        return "COM を初期化できない"
    if "SystemExit" in w or "途中で終了" in w:
        return "処理が途中で終了した"
    m = _EXC_TOKEN.match(w)
    if m:
        return f"例外({m.group(1)})"
    return "その他の失敗"


def summarize_run_reason(reason: str, status: str) -> dict:
    """履歴の理由(例: 「完了 20 件・失敗 1 件・スキップ 3 件 — 製番: 理由 / …」)から、件数と失敗の種類だけを取り出す(製番は捨てる)。
    理由は 200 文字で切り詰められ、失敗の列挙は 5 件までなので、種類を判別できない失敗は unclassified として件数だけ示す。"""
    out = {"completed": None, "failed": None, "skipped": None, "failure_kinds": [], "unclassified_failed": 0, "whole_run_failed": False}
    text = (reason or "").strip()
    m = _SUMMARY.match(text)
    if m:
        out["completed"], out["skipped"] = int(m.group(1)), int(m.group(3))
        out["failed"] = int(m.group(2)) if m.group(2) is not None else 0
        counts = {}
        for item in (m.group(4) or "").split(" / "):
            mi = _ITEM.match(item.strip())
            if mi:
                label = classify_failure(mi.group(1))
                counts[label] = counts.get(label, 0) + 1
        out["failure_kinds"] = [{"label": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        out["unclassified_failed"] = max(0, out["failed"] - sum(counts.values()))
    elif status == "失敗":
        out["whole_run_failed"] = True                     # 件数が無い失敗(事前確認・COM・例外・途中終了など。全体で失敗)
        out["failure_kinds"] = [{"label": classify_failure(text), "count": 1}]
    return out


def _fmt_minutes(s: str) -> Optional[str]:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


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
    if not pathsafe.lexically_within(root, isolated) or pathsafe.has_link_in_path(root, repo_root):     # storage が ".." 等でも、最終位置が隔離ルートの内側であること
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

    INFO_CACHE_SECONDS = 30.0                  # 照会に数秒かかる(実機で約 3.6 秒)ため、画面の 15 秒ごとの更新では使い回す

    def __init__(self, python_exe: str, script_path: str, task_name: str = TASK_NAME, info_runner: Optional[Callable[[str], str]] = None, **kw):
        if task_name.strip().lower() == PRODUCTION_TASK_NAME.lower() or not task_name.startswith("SekisanNaviData_Interim"):
            raise ValueError("仮定期実行は、専用のタスク名だけを扱います。")
        super().__init__(python_exe, script_path, task_name=task_name, **kw)
        # テスト用に schtasks の実行(runner)だけが差し替えられたとき、実機の Task Scheduler は読まない(info_runner を明示したときだけ使う)
        self._info_runner = info_runner if info_runner is not None else (default_info_runner if kw.get("runner") is None else (lambda name: ""))
        self._info_cache = (0.0, None)

    def run_info(self) -> Optional[dict]:
        """Task Scheduler の最終起動・次回予定・終了コード・見逃し回数・電源(AC か)。読み取りだけ。取得できないときは None(例外メッセージは出さない)。30 秒間は結果を使い回す。"""
        at, cached = self._info_cache
        if time.monotonic() - at < self.INFO_CACHE_SECONDS and at:
            return cached
        info = None
        try:
            data = json.loads(self._info_runner(self.task_name))
            last, nxt = data.get("last") or "", data.get("next") or ""
            result, missed, ac = data.get("result"), data.get("missed"), data.get("ac")
            if (last and not _ISO.match(last)) or (nxt and not _ISO.match(nxt)):
                raise ValueError("format")
            info = {"last_run": _fmt_minutes(last) if last else None, "next_run": _fmt_minutes(nxt) if nxt else None,
                    "last_result": int(result) if isinstance(result, int) and not isinstance(result, bool) else None,
                    "missed_runs": int(missed) if isinstance(missed, int) and not isinstance(missed, bool) and missed >= 0 else None,
                    "ac_online": ac if isinstance(ac, bool) else None}
        except Exception:  # noqa: BLE001 - 取得できないときは None(メッセージ・パスは出さない)
            info = None
        self._info_cache = (time.monotonic(), info)
        return info

    def invalidate_info(self):
        self._info_cache = (0.0, None)

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
                 info_runner: Optional[Callable[[str], str]] = None, now: Optional[Callable[[], datetime]] = None, **runner_kw):
        self._now = now or datetime.now
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
        sched_kw = {"runner": scheduler_runner} if scheduler_runner else {}
        self.scheduler = InterimScheduler(python_exe or sys.executable, str(self.repo / SCRIPT_NAME), info_runner=info_runner, **sched_kw)

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
        interval = st["interval_days"] or s.schedule_interval_days or DEFAULT_INTERVAL_DAYS
        info = self.scheduler.run_info() if st["registered"] and not st["foreign"] else None
        last = self.last_scheduled_run()
        out = {"task_name": TASK_NAME, "registered": st["registered"], "enabled": st["enabled"], "time": st["time"] or s.schedule_time,
               "interval_days": interval, "foreign": st["foreign"],
               "default_interval_days": DEFAULT_INTERVAL_DAYS, "logon_only": LOGON_ONLY_NOTICE, "scope": "全対象製番(通常処理)",
               "output_dir": self.target()["product_root_rel"], "separate_from_production_task": True,
               # 「前回の結果」欄(読み取りだけ。製番・パス・ホスト名・ログ原文は含めない)
               "info_available": info is not None,
               "last_run": info["last_run"] if info else None, "next_run": info["next_run"] if info else None,
               "last_result": info["last_result"] if info else None,
               "last_result_label": format_last_result(info["last_result"]) if info else "",
               "missed_runs": info["missed_runs"] if info else None, "ac_online": info["ac_online"] if info else None,
               "last_scheduled": last}
        out["attention"] = self._attention(out, interval)
        return out

    def last_scheduled_run(self) -> Optional[dict]:
        """履歴のうち、直近の「定期」の実行(検証用の --dry-run は除く)を、件数・失敗の種類・所要時間に要約する(製番は含めない)。"""
        row = next((r for r in self.history.runs(limit=200) if r.get("kind") == KIND_SCHEDULED), None)
        if row is None:
            return None
        status = row.get("status") or ""
        out = {"status": status, "started": row.get("started"), "finished": row.get("finished"), "duration_minutes": None,
               **summarize_run_reason(safe_reason(row.get("reason", "")), status)}
        try:
            if row.get("started") and row.get("finished"):
                fmt = "%Y-%m-%d %H:%M:%S"
                out["duration_minutes"] = max(0, int((datetime.strptime(row["finished"], fmt) - datetime.strptime(row["started"], fmt)).total_seconds() // 60))
        except ValueError:
            pass
        return out

    def _attention(self, d: dict, interval_days: int) -> list:
        """見落としやすい状態を、固定文言で並べる(画面の「要確認」)。"""
        items = []
        now = self._now()

        def parse(s):
            try:
                return datetime.strptime(s, "%Y-%m-%d %H:%M") if s else None
            except ValueError:
                return None
        if d["foreign"]:
            return items
        if not d["registered"]:
            return ["専用タスクが登録されていません(自動実行されません)。"]
        enabled = d["enabled"] is not False
        if not enabled:
            items.append("専用タスクが無効です(次回は実行されません)。")
        last = d["last_scheduled"]
        if last and last["status"] == "失敗":
            n = last.get("failed")
            items.append("前回の定期実行に失敗が含まれます" + (f"(失敗 {n} 件)。" if n else "(全体で失敗)。") + "失敗の種類は下の欄と履歴で確認してください。")
        code = d["last_result"]
        if code not in (None, 0, TASK_RESULT_RUNNING, TASK_RESULT_NEVER) and not (last and last["status"] == "失敗"):
            items.append(f"タスクの前回の終了コードが 0 ではありません(終了コード {code})。起動できなかった・途中で停止した可能性があります。")
        if d["missed_runs"]:
            items.append(f"見逃した実行が {d['missed_runs']} 回あります(ログオフ・スリープ・バッテリー駆動・電源 OFF などの可能性)。")
        last_run, next_run = parse(d["last_run"]), parse(d["next_run"])
        if enabled and last_run and not self.running() and now - last_run > timedelta(days=interval_days) + STALE_MARGIN:
            items.append(f"予定どおりに起動していない可能性があります(前回の起動から {interval_days} 日以上たっています)。")
        if enabled and next_run and next_run - now <= SOON:
            try:
                api_ok = bool(self._api_ready())
            except Exception:  # noqa: BLE001
                api_ok = False
            if not api_ok:
                items.append(f"次回の実行({d['next_run']})までに、推論 API を「準備完了」にしてください(未起動なら、その回は失敗として履歴に残ります)。")
            if d["ac_online"] is False:
                items.append("いまバッテリー駆動です。次回の実行までに AC 電源へ接続してください(バッテリー駆動では開始されない見込みです)。")
        if enabled and d["info_available"] and not d["next_run"]:
            items.append("次回の実行予定を確認できません(タスクの設定を確認してください)。")
        return items

    def schedule_register(self, hhmm, interval_days) -> dict:
        try:
            t, n = validate_time(hhmm), validate_interval(interval_days)
        except SettingsError:
            raise RunRefused("bad_schedule") from None
        self.target()                                                    # 隔離されていないときは登録しない
        self.scheduler.register(t, n, True)
        self.scheduler.invalidate_info()
        self.store.save({"schedule_enabled": True, "schedule_time": t, "schedule_interval_days": n})
        return self.schedule_state()

    def schedule_unregister(self) -> dict:
        self.scheduler.delete()
        self.scheduler.invalidate_info()
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
