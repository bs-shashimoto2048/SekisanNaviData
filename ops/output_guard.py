# 運用画面(8765)の「製番を指定して実行」の出力先の整合確認。
# 本番では、製番フォルダは <出力ルート>/<data.storage>/output/<製番>(pipeline/settings.py・app.py)に作られ、本番推論 API(inference_server/prod_server.py)は、
# その親フォルダ(<data.output>/<data.storage>/output)を output_root として、直下の製番フォルダだけを受け付ける(それ以外の work_dir は 400)。
# 出力先欄(または運用設定の出力先 ops.output_root)が、設定の data.output と違う場所を指していると、製番フォルダ・印刷・CAD 変換の副作用が先に起き、
# 最後の API 呼び出しだけが拒否される。そこで、実行を始める前に、実効の出力先(出力ルートから app と同じ規則で算出)が、設定の data.output から算出した本番の出力先と
# 一致することを確認し、違えば固定の理由で拒否する(別の場所へ黙ってフォールバックしない)。
# - 比較は、app が実際に使う規則そのもの(pipeline.settings.with_output_root → output_dir)で算出する。大文字小文字・区切り文字・末尾の区切りは区別しない(Windows)。
# - 出力先が指定されていない(空)ときは、app は data.output を使うため、確認の必要がなく、設定ファイルも読まない(従来どおり)。
# - 理由は固定文。パス・例外メッセージは含めない。
# - 運用画面(ops_console)の手動実行だけに接続する。定期実行・暫定運用(interim)・開発用の出力先や設定は変えない。
import os
from typing import Callable, Optional

from ops.settings_store import SettingsError

SOURCE_FIELD = "field"            # 画面の「出力先」欄で指定された
SOURCE_SETTING = "ops_setting"    # 欄は空で、運用設定(1.)の出力先が使われる

REASON_FIELD = ("指定された出力先は、本番の出力先(設定 data.output の下の storage/output)と異なるため、実行しませんでした。"
                "本番推論 API は、本番の出力先の直下の製番フォルダだけを受け付けます。製番フォルダの作成・印刷・API の呼び出しは行っていません。"
                "出力先欄を空にするか、設定の data.output と同じ値を指定してください。")
REASON_SETTING = ("運用設定の出力先が、設定 data.output と異なるため、実行しませんでした。"
                  "本番推論 API は、本番の出力先の直下の製番フォルダだけを受け付けます。製番フォルダの作成・印刷・API の呼び出しは行っていません。"
                  "運用設定の出力先を空にするか、設定の data.output と同じ値にしてから実行してください。")
REASON_UNREADABLE = "出力先を確認できないため、実行しませんでした(設定ファイルを読み込めません)。製番フォルダの作成・印刷・API の呼び出しは行っていません。"


def _norm(path: str) -> str:
    p = str(path).strip()
    if os.name == "nt":
        return os.path.normcase(os.path.normpath(p.replace("/", "\\"))).rstrip("\\")     # UNC の先頭の `\\` は normpath が保つ
    return os.path.normpath(p).rstrip("/")


def _default_load(config_path):
    from pipeline import settings as settings_mod       # 遅延 import(bs_pkg が必要。ops の他の機能の読込に影響させない)
    return settings_mod, settings_mod.load_settings(config_path)


class OutputGuard:
    """RunManager.start_manual から、実行を始める前に呼ぶ。`guard(root, source)`。違えば SettingsError(固定理由)。"""

    def __init__(self, config_path, load: Optional[Callable] = None):
        self.config_path = config_path
        self._load = load or _default_load

    def production_output_dir(self) -> str:
        """設定(data.output / data.storage)から算出した、本番の出力先(= 製番フォルダの親。API の output_root)。"""
        _, cfg = self._load(self.config_path)
        return cfg.output_dir

    def effective_output_dir(self, root: str) -> str:
        """出力ルート root を app.main(output_root=root) に渡したときの、製番フォルダの親(app と同じ規則)。"""
        mod, cfg = self._load(self.config_path)
        return mod.with_output_root(cfg, root).output_dir

    def __call__(self, root: str, source: str) -> None:
        if not (root or "").strip():
            return                                        # 指定なし: app は設定の data.output を使う(確認不要。設定も読まない)
        try:
            mod, cfg = self._load(self.config_path)
            base, effective = cfg.output_dir, mod.with_output_root(cfg, root).output_dir
        except Exception:  # noqa: BLE001 - 理由は固定文(例外メッセージ・パスを出さない)
            raise SettingsError(REASON_UNREADABLE) from None
        if _norm(effective) != _norm(base):
            raise SettingsError(REASON_FIELD if source == SOURCE_FIELD else REASON_SETTING)
