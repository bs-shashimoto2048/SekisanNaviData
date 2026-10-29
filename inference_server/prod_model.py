"""本番推論 API のモデルの識別と検証。標準ライブラリ + PyYAML のみ(重い依存関係は読み込まない)。

- 識別: 重みファイルの SHA-256・サイズ、model.yaml の SHA-256 を、ストリーミングで計算する(52MB 級でも一定メモリ)。
- 検証: 設定に明示した期待値(サイズ・SHA-256)と一致しなければ ModelVerificationError。**一致しないモデルでは起動(ready)しない。**
- model.yaml の model.my_model が、設定の重みファイル名と一致することも確認する(別の重みを読み込ませない)。
- **検証したバイト列そのものを読み込む**: `stage_and_verify` は、元のファイルを 1 回だけ開いて読み、ハッシュを計算しながら私用の一時フォルダ(ステージング)へ
  書き出す。ハッシュは「コピーしたバイト列」について期待値と照合し、バックエンドはそのコピーから読み込む(検証と使用の間に元のファイルを差し替えられても、
  検証済みでないものは読み込まれない)。`model.yaml` の解釈もステージングしたコピーから行う。
- 例外メッセージに、パスとハッシュ全文を含めない(識別には先頭 12 桁だけを使う)。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

PREFIX_LEN = 12
_CHUNK = 1024 * 1024


class ModelVerificationError(RuntimeError):
    """モデルの識別・検証の失敗。メッセージは固定文(値を含めない)。"""


@dataclass(frozen=True)
class ModelIdentity:
    id: str
    weights_size: int
    weights_sha256: str
    yaml_sha256: str
    weights_mtime_ns: int
    yaml_mtime_ns: int

    def public(self) -> dict:
        """/health・server.yaml に出してよい識別情報(ハッシュは先頭 12 桁だけ)。"""
        return {"id": self.id, "weights_sha256_12": self.weights_sha256[:PREFIX_LEN],
                "yaml_sha256_12": self.yaml_sha256[:PREFIX_LEN], "weights_size": self.weights_size}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def identify(model_id: str, model_dir: str, weights: str, yaml_name: str) -> ModelIdentity:
    """モデルフォルダの重みと model.yaml の識別情報を計算する(通常ファイルだけ。リンク・フォルダ・欠落は拒否)。"""
    wp, yp = Path(model_dir) / weights, Path(model_dir) / yaml_name
    for p, what in ((wp, "重みファイル"), (yp, "model.yaml")):
        if p.is_symlink() or not p.is_file():
            raise ModelVerificationError(f"{what}が通常のファイルとして見つかりません。")
    try:
        ws, ys = wp.stat(), yp.stat()
        return ModelIdentity(model_id, ws.st_size, sha256_file(str(wp)), sha256_file(str(yp)), ws.st_mtime_ns, ys.st_mtime_ns)
    except OSError:
        raise ModelVerificationError("モデルファイルを読み込めません。") from None


def verify(identity: ModelIdentity, expected_weights_sha256: str, expected_weights_size: int, expected_yaml_sha256: str) -> None:
    """期待値と一致しなければ ModelVerificationError。どの項目が違うかは項目名だけを返す。"""
    if identity.weights_size != expected_weights_size:
        raise ModelVerificationError("重みファイルのサイズが期待値と一致しません。")
    if identity.weights_sha256 != expected_weights_sha256:
        raise ModelVerificationError("重みファイルの SHA-256 が期待値と一致しません。")
    if identity.yaml_sha256 != expected_yaml_sha256:
        raise ModelVerificationError("model.yaml の SHA-256 が期待値と一致しません。")


def _stage_file(src: Path, dst: Path, expected_sha256: str, expected_size: int | None, what: str) -> str:
    """src を 1 回だけ開いて読み、ハッシュを計算しながら dst へ書く。期待値と一致しなければ ModelVerificationError。戻り値は SHA-256。"""
    if src.is_symlink() or not src.is_file():
        raise ModelVerificationError(f"{what}が通常のファイルとして見つかりません。")
    h = hashlib.sha256()
    total = 0
    try:
        with open(src, "rb") as f, open(dst, "wb") as out:
            if expected_size is not None and os.fstat(f.fileno()).st_size != expected_size:
                raise ModelVerificationError(f"{what}のサイズが期待値と一致しません。")
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                total += len(chunk)
                out.write(chunk)
    except OSError:
        raise ModelVerificationError(f"{what}を読み込めません。") from None
    if expected_size is not None and total != expected_size:
        raise ModelVerificationError(f"{what}のサイズが期待値と一致しません。")
    if h.hexdigest() != expected_sha256:
        raise ModelVerificationError(f"{what}の SHA-256 が期待値と一致しません。")
    return h.hexdigest()


def stage_and_verify(model_id: str, model_dir: str, weights: str, yaml_name: str, expected_weights_sha256: str,
                     expected_weights_size: int, expected_yaml_sha256: str, staging_parent: str | None = None):
    """検証したバイト列そのものをステージングする。(識別情報, ステージングしたフォルダ)を返す。失敗時はフォルダを残さず ModelVerificationError。
    呼び出し側は、読み込みが終わったら `remove_staging` で消すこと。ステージングしたファイルは読み取り専用にする。"""
    staged = tempfile.mkdtemp(prefix="prod_model_", dir=staging_parent)
    try:
        ws = _stage_file(Path(model_dir) / weights, Path(staged) / weights, expected_weights_sha256, expected_weights_size, "重みファイル")
        ys = _stage_file(Path(model_dir) / yaml_name, Path(staged) / yaml_name, expected_yaml_sha256, None, "model.yaml")
        for name in (weights, yaml_name):
            os.chmod(Path(staged) / name, stat.S_IREAD)
        wst, yst = (Path(staged) / weights).stat(), (Path(staged) / yaml_name).stat()
        return ModelIdentity(model_id, wst.st_size, ws, ys, wst.st_mtime_ns, yst.st_mtime_ns), staged
    except BaseException:
        remove_staging(staged)
        raise


def remove_staging(staged: str | None) -> None:
    """ステージングしたフォルダを削除する(読み取り専用の属性は外してから)。存在しなくてもよい。"""
    if not staged:
        return
    def _onerror(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    shutil.rmtree(staged, onerror=_onerror)


def check_yaml_matches_weights(model_dir: str, yaml_name: str, weights: str) -> dict:
    """model.yaml を読み、model.my_model が設定の重み名と一致することを確認して、`device` などの公開してよい要約を返す。"""
    try:
        cfg = yaml.safe_load((Path(model_dir) / yaml_name).read_text(encoding="utf-8"))
        m = cfg["model"]
        cfg["sahi"]
    except Exception:  # noqa: BLE001
        raise ModelVerificationError("model.yaml を読み込めません(model / sahi の節が必要です)。") from None
    if os.path.basename(str(m.get("my_model", ""))) != weights or str(m.get("my_model")) != weights:
        raise ModelVerificationError("model.yaml の重みファイル名が、設定の重みファイル名と一致しません。")
    return {"device": str(m.get("use_device", "")), "model_type": str(m.get("use_model_type", ""))}


def unchanged_since(identity: ModelIdentity, model_dir: str, weights: str, yaml_name: str, *, rehash: bool = False) -> bool:
    """識別した後にファイルが変わっていないか(サイズ・更新時刻。rehash=True ならハッシュも再計算)。"""
    try:
        wp, yp = Path(model_dir) / weights, Path(model_dir) / yaml_name
        ws, ys = wp.stat(), yp.stat()
        if ws.st_size != identity.weights_size or ws.st_mtime_ns != identity.weights_mtime_ns or ys.st_mtime_ns != identity.yaml_mtime_ns:
            return False
        if rehash:
            return sha256_file(str(wp)) == identity.weights_sha256 and sha256_file(str(yp)) == identity.yaml_sha256
        return True
    except OSError:
        return False
