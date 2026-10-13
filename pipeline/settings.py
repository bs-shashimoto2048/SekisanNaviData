# 設定の読込・検証(S3)。app.main() の冒頭にあった「YAML 読込 → サンプル設定の拒否 → 出力先の決定 →
# 推論サーバー設定の検証」を 1 か所に集約した。YAML のキー・検証の順序・エラーの種類は切り出し前と同じ。
#
# 順序を変えないための注意:
# - 出力先(data.output / data.storage)と inference_server セクションは、読込時にすぐ解決する(従来もここまでが先)。
# - 対象製番・箱体型式・CAD 変換の設定は、**使う時点で**読む(プロパティ)。従来は推論サーバー設定の検証の後で読んでいたため、
#   設定が不足していて、かつ推論サーバー設定が不正なときに先に出るエラー(InferenceServerError)を変えないため。
# - 推論サーバー設定の検証(build_inference_manager)は、出力フォルダ・ログを作る前に呼ぶ(本番共有・範囲外なら何も書かずに失敗)。
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from bs_pkg import config_loader

import inference_support

SAMPLE_CONFIG_KEYS = (("data", "output"), ("api", "dir_venv"), ("dwg_conversion", "converter"))


def default_config_path(base_dir, script_name: str) -> str:
    """設定ファイルの既定。社内の共有パス等を書いたローカル設定 <script_name>.local.yaml(Git管理外)があればそれを、無ければ <script_name>.yaml(サンプル)。"""
    local = Path(base_dir) / f"{script_name}.local.yaml"
    return str(local) if local.is_file() else f"{base_dir}/{script_name}.yaml"


def assert_not_sample_config(config_data: dict, path: str):
    """サンプルのまま(プレースホルダ '<...>' が残っている)の設定では実行しない。"""
    for section, key in SAMPLE_CONFIG_KEYS:
        value = str((config_data.get(section) or {}).get(key, ""))
        if "<" in value:
            raise RuntimeError(
                f"{path} はサンプル設定です({section}.{key} が未設定)。app.local.yaml を作成して実際の値を設定してください"
                f"(手順は docs/setup.md の「ローカル設定」)。")


@dataclass(frozen=True)
class Settings:
    """app.main() が使う設定。config_data は YAML そのまま(Step が参照する api・data_extension もここから読む)。"""
    config_path: str
    config_data: dict
    data_dir: str           # 出力ルート。推論サーバー統合が有効なら絶対パス(サーバーと work_dir を共有するため)
    storage: str
    infer_cfg: dict         # inference_server セクション(無ければ空)
    infer_enabled: bool

    # --- 読込時に決まる出力先 ---
    @property
    def product_dir(self) -> str:
        return os.path.join(self.data_dir, self.storage)

    @property
    def log_dir(self) -> str:
        return os.path.join(self.product_dir, "logs")

    @property
    def output_dir(self) -> str:
        return os.path.join(self.product_dir, "output")

    # --- 使う時点で読む設定(上の注意を参照) ---
    @property
    def target_initial(self) -> str:
        return self.config_data["data"]["initials"]

    @property
    def box_types(self) -> tuple:
        return tuple(self.config_data["data"]["box_types"])

    @property
    def converter_path(self) -> str:
        return self.config_data["dwg_conversion"]["converter"]          # 変換ソフトのパス

    @property
    def converter_version(self) -> str:
        return self.config_data["dwg_conversion"]["cad_version"]        # 出力 CAD バージョン

    @property
    def converter_extension(self) -> str:
        return self.config_data["data_extension"]["convert_bfr"]        # 変換前 CAD 拡張子

    @property
    def converted_extension(self) -> str:
        return self.config_data["data_extension"]["convert_aft"]        # 変換後 CAD 拡張子


def load_settings(config_path=None, default_path: Optional[Callable[[], str]] = None) -> Settings:
    """設定を読み込む。config_path 省略時は default_path() のファイルを読み、サンプルのままなら拒否する(指定時は、検証用 app_test.yaml 等をそのまま読む)。"""
    my_yaml_path: str = str(config_path) if config_path is not None else default_path()
    config_data = config_loader.load_yaml(my_yaml_path)
    if config_path is None:
        assert_not_sample_config(config_data, my_yaml_path)
    data_dir: str = config_data["data"]["output"]
    # 推論サーバー統合(検証用)。app.yaml には無く、省略時(無効)は従来動作のまま。
    infer_cfg: dict = config_data.get("inference_server") or {}
    infer_enabled: bool = bool(infer_cfg.get("enabled", False))
    if infer_enabled:
        data_dir = os.path.abspath(data_dir)  # work_dir をサーバーと共有できる絶対パスにする
    storage: str = config_data["data"]["storage"]
    return Settings(config_path=my_yaml_path, config_data=config_data, data_dir=data_dir, storage=storage,
                    infer_cfg=infer_cfg, infer_enabled=infer_enabled)


def build_inference_manager(cfg: Settings) -> Tuple[Any, Any]:
    """推論サーバー統合が有効なら、設定を検証して管理オブジェクトを作る(起動はしない)。戻り値は (manager, サーバー設定)。無効なら (None, None)。
    本番共有・範囲外の設定、本番共有上の CAD 変換ソフトは InferenceServerError になる。出力フォルダ・ログを作る前に呼ぶこと。"""
    if not cfg.infer_enabled:
        return None, None
    config_data = cfg.config_data
    server_settings = inference_support.InferenceServerSettings.from_config(cfg.infer_cfg)
    api_cfg_path = os.path.abspath(os.path.join(config_data["api"]["dir_venv"], config_data["api"]["yaml_file"]))
    manager = inference_support.InferenceServerManager(
        server_settings, output_root=cfg.data_dir, config_out=api_cfg_path, log_dir=cfg.log_dir)
    manager.validate()
    # 検証モードでは CAD 変換ソフトも本番共有上のものを使わない(ローカルのパスを指定する)
    try:
        inference_support.safety.assert_not_production(
            str(config_data["dwg_conversion"]["converter"]), "dwg_conversion.converter")
    except inference_support.safety.SafetyError as e:
        raise inference_support.InferenceServerError(str(e)) from e
    return manager, server_settings
