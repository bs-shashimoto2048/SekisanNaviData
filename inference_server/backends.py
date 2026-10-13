"""推論バックエンド。

- FakeBackend: テスト用(モデル不要)。決められた検出結果を返す。外部通信・GPUなし。
- SahiBackend: 本番相当(YOLOv8 + SAHI スライス推論)。重い依存関係(sahi/ultralytics/torch)は生成時に初めて import する。
  ※このリポジトリの自動テストでは実行していない(依存関係とモデルが検証 venv に無いため)。

どちらも predict(image_path, tmp_dir) -> list[Detection] を実装し、可視化画像を tmp_dir/prediction_visual.png に書く。
出力先ルートへの書き込みはサーバー側(server.py)が一時ファイルから置き換える形で行う。
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import time
from dataclasses import dataclass
from pathlib import Path

VISUAL_NAME = "prediction_visual.png"


@dataclass
class Detection:
    name: str
    score: float
    minx: float
    miny: float
    maxx: float
    maxy: float


class BackendError(RuntimeError):
    pass


def _png_size(path: str):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise BackendError("FakeBackend は PNG 画像のみ対応です。")
    return struct.unpack(">II", head[16:24])


class FakeBackend:
    """script(JSON)で検出結果を与えるテスト用バックエンド。座標は画像サイズに対する比率(0〜1)なので解像度に依存しない。

    {"delay_sec": 0, "fail_pages": ["22"], "pages": {"21": [{"name": "panel", "score": 0.9,
                                                              "cx": 0.2, "cy": 0.5, "w": 0.01, "h": 0.01}]}}
    """

    name = "fake"

    def __init__(self, script_path: str | None = None):
        self.script = {}
        if script_path:
            self.script = json.loads(Path(script_path).read_text(encoding="utf-8"))
        if self.script.get("load_delay_sec"):  # モデル読み込みが遅い状況の再現(ready になるまで時間がかかる)
            time.sleep(float(self.script["load_delay_sec"]))

    def predict(self, image_path: str, tmp_dir: str):
        page = Path(image_path).stem
        if self.script.get("delay_sec"):
            time.sleep(float(self.script["delay_sec"]))
        if page in set(map(str, self.script.get("fail_pages", []))):
            raise BackendError(f"テスト用の推論失敗(page={page})")
        width, height = _png_size(image_path)
        dets = []
        for d in self.script.get("pages", {}).get(page, []):
            cx, cy, w, h = d["cx"] * width, d["cy"] * height, d["w"] * width, d["h"] * height
            dets.append(Detection(d["name"], float(d.get("score", 0.9)), cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
        shutil.copyfile(image_path, Path(tmp_dir) / VISUAL_NAME)  # 可視化画像の代わりに入力画像をコピー
        return dets


class SahiBackend:
    """YOLOv8 + SAHI(参照コピー server.py の load_detection_model / predict と同じ呼び出し)。"""

    name = "sahi"

    def __init__(self, model_dir: str, model_yaml: str = "model.yaml"):
        import yaml  # 本体の依存(requirements.txt)

        cfg = yaml.safe_load((Path(model_dir) / model_yaml).read_text(encoding="utf-8"))
        m, s = cfg["model"], cfg["sahi"]
        from sahi import AutoDetectionModel  # requirements-real.txt が必要

        self._slice = dict(slice_height=int(s["slice_higth"]), slice_width=int(s["slice_width"]),
                           overlap_height_ratio=float(s["over_lap_higth"]), overlap_width_ratio=float(s["over_lap_width"]))
        self._model = AutoDetectionModel.from_pretrained(
            model_type=m["use_model_type"], model_path=str(Path(model_dir) / m["my_model"]),
            confidence_threshold=float(m["threshold"]), device=m["use_device"])

    def predict(self, image_path: str, tmp_dir: str):
        from sahi.predict import get_sliced_prediction

        result = get_sliced_prediction(image_path, self._model, verbose=0, **self._slice)
        dets = [Detection(o.category.name, float(o.score.value), float(o.bbox.minx), float(o.bbox.miny),
                          float(math.ceil(o.bbox.maxx)), float(math.ceil(o.bbox.maxy)))
                for o in result.object_prediction_list]
        result.export_visuals(export_dir=tmp_dir)  # tmp_dir/prediction_visual.png
        return dets
