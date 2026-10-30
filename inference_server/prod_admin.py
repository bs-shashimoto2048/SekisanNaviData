"""本番推論 API の「図面解析 AI モデル」管理画面(API ホスト上でのみ使うローカル画面)。標準ライブラリ + PyYAML。

- 127.0.0.1 だけで待ち受ける(LAN からは接続できない)。接続元・Host・Origin も loopback だけ受け付け、状態を変える操作はカスタムヘッダ X-Admin-Request を要求する。
- モデルは**アップロードも別フォルダへのコピーもしない**。API ホストのローカル画面で「参照…」から選んだ `.pt`(UNC 可)と、組になる `model.yaml` を、**その場所のまま**参照する。
  サイズと SHA-256 は、選んだ実ファイルを読み取って算出し、期待値として設定へ書く(手入力しない)。
- 重みだけでは保存できない: `model.yaml`(SahiBackend が必要とする)が見つからない・読めない・`model.my_model` が重みのファイル名と一致しない場合は、保存を拒否する。
  一致しないときだけ、**元の model.yaml の `my_model` の参照名だけ**を選んだ重みのファイル名に置き換えた「設定用コピー」を、ローカルの Git 管理外領域(`*.local.yaml`)に作って使える
  (利用者が明示的に選ぶ。差分を画面に示し、ほかの推論設定値が変わらないことを検証する。元の model.yaml・重みは変更・複製・改名しない)。
- 保存先は Git 管理外の `prod_server.local.yaml` の `model:` 節だけ。保存前に同じフォルダへバックアップを作り、検証してから原子的に置換する。共有 server.yaml は触らない。
- **書き込み先(設定ファイル・バックアップ・一時ファイル・設定用コピー)は、実体としてローカルディスクの中に限る**(`require_local_dir`)。UNC・ネットワークドライブ・取り外し可能/不明なドライブ・
  SUBST 等で別の場所へ解決されるドライブ・パスの途中にシンボリックリンク/ジャンクション等がある場所は、書き込みの前に固定文で拒否する(パス文字列だけで判定しない)。
  読み取り元(重み・元の model.yaml)は UNC も選べる(読み取りと書き込みは別)。
- 保存しても API の起動・再起動・公開はしない。保存後に、読み取り専用の `check`(prod_server.run_check)を実行できる。
- 応答・画面にはファイル名だけを出す。ハッシュは先頭 12 桁だけ(全文は設定ファイルにだけ書く)。パスを返すのは「参照…」で選んだ値を入力欄へ入れるときだけ(ローカル画面のみ)。アクセスログは出さない。
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import prod_config
import prod_model

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8790
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "prod_server.local.yaml"
HTML_PATH = HERE / "prod_admin.html"
# 本番モデル(運用担当が指定した重みファイル)。入力欄の初期値。model.yaml はこの値からは決めない(推測しない)
DEFAULT_WEIGHTS_PATH = r"\\beans-f1\ShareData\estimatic\a_product\model\estimatic_a_20250826.pt"
# 現行の model.yaml(旧サーバー `estimate\server\model`。モデル構造・閾値・デバイス・スライス条件の元)。入力欄の初期値。共有の重みと同じフォルダには model.yaml が無い
DEFAULT_YAML_PATH = r"C:\Users\shashimoto\project\workspace_vsc\estimate\server\model\model.yaml"
LOCAL_DIR_NAME = "local_config"          # 設定用コピーの置き場(prod_server.local.yaml と同じ場所の下。*.local.yaml だけを置く = Git 管理外)
MAX_BODY = 16 * 1024
MAX_WEIGHTS_BYTES = 4 * 1024 ** 3
MAX_YAML_BYTES = 64 * 1024
PICK_TIMEOUT_SECONDS = 300
WEIGHTS_EXTS = (".pt",)
YAML_EXTS = (".yaml", ".yml")
_LOOPBACK_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class AdminError(RuntimeError):
    """利用者に見せてよい失敗。メッセージは固定文(パス・ハッシュ全文を含めない。ファイル名は可)。"""


class PickerBusy(RuntimeError):
    pass


# ----------------------------------------------------------------------------- 書き込み先の検査(読み取り元とは別。書き込みはローカルディスク内に限る)
STORAGE_MESSAGE = ("保存先(設定ファイル・バックアップ・設定用コピー)は、この PC のローカルディスク内に限ります。"
                   "ネットワーク上の場所・ネットワークドライブ・リンクやジャンクションを経由する場所は使えません。起動時の設定(--config)を確認してください。")
DRIVE_FIXED = 3                          # GetDriveTypeW: 固定ディスク(ネットワーク = 4、取り外し可能 = 2、不明 = 0、RAM ディスク = 6 は不可)
_REPARSE_POINT = 0x400                   # FILE_ATTRIBUTE_REPARSE_POINT(シンボリックリンク・ジャンクション・マウントポイント等)


def default_drive_type(root: str) -> int:
    return ctypes.windll.kernel32.GetDriveTypeW(root)


def _is_reparse_or_link(path: str) -> bool:
    st = os.lstat(path)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def require_local_dir(path, drive_type=default_drive_type) -> str:
    """書き込み先のフォルダが、実体としてローカルディスクの中にあることを確認して返す。証明できなければ AdminError(固定文。パス・例外メッセージを含めない)。
    - UNC(`\\\\`・`//`・`\\\\?\\`)は拒否。ドライブ文字つきの絶対パスだけ。
    - ドライブの種類(GetDriveTypeW)が「固定ディスク」でなければ拒否(ネットワーク・取り外し可能・不明・RAM ディスク)。
    - パスの各階層(存在するもの)が、シンボリックリンク・ジャンクション等の再解析ポイントなら拒否(パス文字列だけで判定しない)。
    - 実体(realpath)が UNC、または別のドライブへ解決される(SUBST・マップ)場合も拒否する(解決先のドライブの種類も確認する)。
    - まだ無い階層は、作成した後でもう一度この関数で確認すること。Windows 以外では確認できないため拒否する。"""
    try:
        text = os.fspath(path)
        if os.name != "nt" or not isinstance(text, str) or text.startswith(("\\\\", "//")):
            raise AdminError(STORAGE_MESSAGE)
        full = os.path.abspath(text)
        drive, rest = os.path.splitdrive(full)
        if not re.fullmatch(r"[A-Za-z]:", drive) or drive_type(drive + "\\") != DRIVE_FIXED:
            raise AdminError(STORAGE_MESSAGE)
        cur = deepest = drive + "\\"
        for part in [x for x in rest.split("\\") if x]:
            cur = os.path.join(cur, part)
            if not os.path.lexists(cur):
                break
            if _is_reparse_or_link(cur):
                raise AdminError(STORAGE_MESSAGE)
            deepest = cur
        real = os.path.realpath(deepest)
        rdrive = os.path.splitdrive(real)[0]
        if real.startswith(("\\\\", "//")) or rdrive.lower() != drive.lower() or drive_type(rdrive + "\\") != DRIVE_FIXED:
            raise AdminError(STORAGE_MESSAGE)
        return full
    except AdminError:
        raise
    except Exception:  # noqa: BLE001 - 確認できない = 証明できない(例外メッセージ・パスは出さない)
        raise AdminError(STORAGE_MESSAGE) from None


# ----------------------------------------------------------------------------- パスの検査
def validate_model_path(text, exts: tuple, what: str) -> str:
    """ローカルドライブ(`C:\\...`)または UNC(`\\\\server\\share\\...`)の絶対パスだけ。拡張子を確認する。存在は確認しない。"""
    t = "" if text is None else str(text).strip()
    if not t:
        raise AdminError(f"{what}のパスを入力するか、「参照…」で選んでください。")
    if len(t) > 400 or any(c in t for c in "\x00\r\n<>\"|*?"):
        raise AdminError(f"{what}のパスに使えない文字が含まれています。")
    t = t.replace("/", "\\")
    unc = re.fullmatch(r"\\\\[^\\]+\\[^\\]+(\\[^\\]+)+", t)
    drive = re.fullmatch(r"[A-Za-z]:\\([^\\]+\\)*[^\\]+", t)
    if not (unc or drive):
        raise AdminError(f"{what}は絶対パス(ドライブ名つき、または \\\\サーバー\\共有名\\…)で指定してください。")
    if ".." in t.split("\\"):
        raise AdminError(f"{what}のパスに「..」は使えません。")
    if not t.lower().endswith(exts):
        raise AdminError(f"{what}の拡張子は {' / '.join(exts)} にしてください。")
    return t


def _filename_ok(name: str) -> bool:
    return bool(prod_config._FILENAME_RE.fullmatch(name)) and name not in (".", "..")


def _safe_name(value) -> str:
    v = os.path.basename(str(value)) if value is not None else ""
    return v if _filename_ok(v) else "(不正な値)"


def _looks_like_torch_checkpoint(path: str) -> bool:
    """PyTorch の保存形式の先頭(zip: `PK\\x03\\x04`、旧形式の pickle: `\\x80`)だけを確認する。内容は解釈しない(pickle を実行しない)。"""
    with open(path, "rb") as f:
        head = f.read(4)
    return head.startswith(b"PK\x03\x04") or head[:1] == b"\x80"


def _regular_file(path: str) -> bool:
    return os.path.isfile(path) and not os.path.islink(path)


# ----------------------------------------------------------------------------- model.yaml の参照名の対応付け(my_model だけ)
_MY_MODEL_RE = re.compile(r"""^(?P<pre>[ \t]+my_model:[ \t]*)(?P<q>["']?)(?P<val>[^\s"'#]+)(?P=q)(?P<post>[ \t]*(#.*)?)$""")


def remap_yaml(text: str, new_name: str) -> dict:
    """model.yaml の `model.my_model` の値だけを new_name に置き換えた本文を作る(ほかの行・コメント・改行コードは 1 バイトも変えない)。
    変更が my_model の 1 行だけであること、ほかのすべての値(`my_model` 以外)が同一であることを検証する。失敗は AdminError。"""
    lines = text.splitlines(keepends=True)
    hits, in_model = [], False
    for i, ln in enumerate(lines):
        body = ln.rstrip("\r\n")
        if re.fullmatch(r"model:[ \t]*(#.*)?", body):
            in_model = True
            continue
        if body and body[0] not in " \t#":
            in_model = False
        if in_model:
            m = _MY_MODEL_RE.match(body)
            if m:
                hits.append((i, m))
    if len(hits) != 1:
        raise AdminError("model.yaml の model.my_model の行を 1 つに特定できないため、対応付けできません。")
    i, m = hits[0]
    eol = lines[i][len(lines[i].rstrip("\r\n")):]
    old_line = lines[i].rstrip("\r\n")
    lines[i] = m["pre"] + m["q"] + new_name + m["q"] + m["post"] + eol
    new_text = "".join(lines)
    try:
        a, b = yaml.safe_load(text), yaml.safe_load(new_text)
        ok = b["model"]["my_model"] == new_name
        a2, b2 = copy.deepcopy(a), copy.deepcopy(b)
        a2["model"].pop("my_model"), b2["model"].pop("my_model")
        ok = ok and a2 == b2 and sum(1 for x, y in zip(text.splitlines(), new_text.splitlines()) if x != y) == 1 \
            and len(text.splitlines()) == len(new_text.splitlines())
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise AdminError("my_model 以外の設定値が変わらないことを確認できなかったため、対応付けを中止しました。")
    flat = [f"{sec}.{k} = {str(v)[:40]}" for sec in ("model", "sahi") for k, v in (a.get(sec) or {}).items() if not (sec == "model" and k == "my_model")]
    return {"text": new_text, "diff": {"before": old_line.strip(), "after": lines[i].rstrip("\r\n").strip()}, "unchanged": flat}


# ----------------------------------------------------------------------------- モデルの検査
def inspect_model(weights_path, yaml_path="", remap: bool = False, local_dir=None) -> tuple[dict, dict | None]:
    """選んだ実ファイルを読み取り専用で検査する。(画面用の結果, 保存用の内部情報)。内部情報は問題がなければだけ返す(それ以外は None)。
    画面用の結果にはパス・ハッシュ全文を含めない。remap=True(利用者が明示)のときは、my_model が重みと一致しない model.yaml について、
    my_model だけを対応付けた設定用コピー(local_dir に保存する予定の内容)を内部情報に入れる(ここでは何も書かない)。"""
    problems: list[str] = []
    out = {"weights": None, "yaml": None, "pair": {"ok": False, "message": "未確認"}, "problems": problems, "ok": False}
    internal: dict = {}
    wpath = None
    try:
        wpath = validate_model_path(weights_path, WEIGHTS_EXTS, "重みファイル")
    except AdminError as e:
        problems.append(str(e))
    if wpath:
        wname = os.path.basename(wpath)
        info = {"name": wname}
        out["weights"] = info
        try:
            if not _filename_ok(wname):
                raise AdminError("重みファイル名に使える文字は、英数字と . _ - だけです(設定の制約)。")
            if os.path.islink(wpath):
                raise AdminError(f"{wname} はリンクです。通常のファイルを選んでください。")
            if not os.path.isfile(wpath):
                raise AdminError(f"{wname} が見つかりません(存在しない、または読み取れません)。")
            size = os.path.getsize(wpath)
            if size <= 0 or size > MAX_WEIGHTS_BYTES:
                raise AdminError(f"{wname} のサイズが不正です(空、または大きすぎます)。")
            if not _looks_like_torch_checkpoint(wpath):
                raise AdminError(f"{wname} は PyTorch のモデルファイルの形式ではありません。")
            sha = prod_model.sha256_file(wpath)
            info.update(size=size, sha256_12=sha[:prod_model.PREFIX_LEN])
            internal.update(weights_dir=os.path.dirname(wpath), weights_name=wname, weights_size=size, weights_sha256=sha)
        except AdminError as e:
            problems.append(str(e))
        except OSError:
            problems.append(f"{wname} を読み込めません(権限・ネットワークを確認してください)。")

        ypath = None
        try:
            ytext = (yaml_path or "").strip()
            if ytext:
                ypath = validate_model_path(ytext, YAML_EXTS, "model.yaml")
                out["yaml_source"] = "選択したファイル"
            else:
                cand = os.path.join(os.path.dirname(wpath), "model.yaml")
                if _regular_file(cand):
                    ypath = cand
                    out["yaml_source"] = "重みと同じフォルダの model.yaml"
                else:
                    raise AdminError("model.yaml が重みと同じフォルダにありません。組になる model.yaml を「参照…」で選んでください"
                                     "(重みだけでは保存できません)。")
        except AdminError as e:
            problems.append(str(e))
        if ypath:
            yname = os.path.basename(ypath)
            yinfo = {"name": yname}
            out["yaml"] = yinfo
            try:
                if not _filename_ok(yname):
                    raise AdminError("model.yaml のファイル名に使える文字は、英数字と . _ - だけです(設定の制約)。")
                if os.path.islink(ypath) or not os.path.isfile(ypath):
                    raise AdminError(f"{yname} が通常のファイルとして見つかりません。")
                if os.path.getsize(ypath) > MAX_YAML_BYTES:
                    raise AdminError(f"{yname} が大きすぎます。")
                ysha = prod_model.sha256_file(ypath)
                yinfo.update(sha256_12=ysha[:prod_model.PREFIX_LEN])
                try:
                    cfg = yaml.safe_load(Path(ypath).read_text(encoding="utf-8"))
                    m = cfg["model"]
                    cfg["sahi"]
                    my_model = m.get("my_model")
                except Exception:  # noqa: BLE001
                    raise AdminError(f"{yname} を読み込めません(model / sahi の節が必要な YAML です)。") from None
                yinfo.update(my_model=_safe_name(my_model), device=str(m.get("use_device", ""))[:20],
                             model_type=str(m.get("use_model_type", ""))[:20])
                internal.update(yaml_dir=os.path.dirname(ypath), yaml_name=yname, yaml_sha256=ysha)
                if str(my_model) != wname:
                    plan = None
                    try:
                        plan = remap_yaml(Path(ypath).read_bytes().decode("utf-8"), wname) if _filename_ok(wname) else None
                    except AdminError as e:
                        out["remap_error"] = str(e)
                    except (OSError, UnicodeDecodeError):
                        plan = None
                    copy_name = f"{os.path.splitext(wname)[0]}.model.local.yaml"
                    if plan is not None:
                        out["remap"] = {"available": True, "selected": bool(remap), "from": _safe_name(my_model), "to": wname,
                                        "source_name": yname, "copy_name": copy_name, "diff": plan["diff"], "unchanged": plan["unchanged"]}
                    if plan is not None and remap and local_dir is not None:
                        data = plan["text"].encode("utf-8")
                        yinfo.update(name=copy_name, generated_from=yname, sha256_12=hashlib.sha256(data).hexdigest()[:prod_model.PREFIX_LEN],
                                     my_model=wname)
                        out["pair"] = {"ok": True, "message": f"元の {yname} の my_model(「{_safe_name(my_model)}」)だけを、選んだ重みの名前(「{wname}」)に置き換えた"
                                       f"設定用コピー({copy_name})を使います。ほかの設定値は変わりません。"}
                        internal.update(yaml_dir=str(local_dir), yaml_name=copy_name, yaml_sha256=hashlib.sha256(data).hexdigest(),
                                        yaml_generated=data, yaml_source_name=yname)
                    else:
                        hint = (" 「my_model だけを対応付けた設定用コピーを作る」を選ぶと、ほかの設定値を変えずに使えます。" if plan is not None
                                else " 重みと同名の my_model を持つ model.yaml を用意して選んでください。")
                        out["pair"] = {"ok": False, "message": f"組になっていません: {yname} の my_model は「{_safe_name(my_model)}」で、"
                                       f"選んだ重み「{wname}」と一致しません。元の model.yaml は編集・コピーしません。" + hint}
                        problems.append(out["pair"]["message"])
                else:
                    out["pair"] = {"ok": True, "message": f"組になっています(my_model = {wname})。"}
            except AdminError as e:
                problems.append(str(e))
            except OSError:
                problems.append(f"{yname} を読み込めません(権限・ネットワークを確認してください)。")
    out["ok"] = not problems and out["pair"]["ok"]
    return out, (internal if out["ok"] else None)


# ----------------------------------------------------------------------------- 設定ファイルの更新
def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_model_block(model_id: str, internal: dict, nl: str) -> str:
    generated = bool(internal.get("yaml_generated"))
    lines = ["model:", f"  id: {model_id}", f"  dir: {_q(internal['weights_dir'])}"]
    if os.path.normcase(os.path.normpath(internal["yaml_dir"])) != os.path.normcase(os.path.normpath(internal["weights_dir"])):
        note = ("model.yaml は、元の my_model の参照名だけを対応付けたローカルの設定用コピー(Git 管理外)" if generated
                else "model.yaml は重みと別のフォルダ(コピーせず、この場所を参照する)")
        lines.append(f"  yaml_dir: {_q(internal['yaml_dir'])}      # {note}")
    lines += [f"  weights: {internal['weights_name']}", f"  yaml: {internal['yaml_name']}",
              "  expected:                             # 管理画面が、選んだ実ファイルから算出して書いた値(手で書き換えない)",
              f"    weights_sha256: \"{internal['weights_sha256']}\"", f"    weights_size: {internal['weights_size']}",
              f"    yaml_sha256: \"{internal['yaml_sha256']}\""]
    return nl.join(lines) + nl


def replace_model_section(text: str, block: str, nl: str) -> str:
    """トップレベルの `model:` 節だけを置き換える(ほかの節・コメントは保つ)。節が無ければ末尾へ追加する。"""
    return replace_section(text, "model", block, nl)


# ----------------------------------------------------------------------------- 性能(server 節)・ログ(logging 節)の設定
# 値の範囲は prod_config(LIMIT_RANGES / LOG_RANGES。設定の検証と同じ表)。画面だけの設定は作らない: ここで書く項目は、すべて prod_server が実際に使う。
PERF_DEFAULTS = {"threads": 2, "max_pending_predict": 1, "queue_wait_sec": 30, "backlog": 4, "connection_limit": 8,
                 "channel_timeout_sec": 30, "shutdown_grace_sec": 60}          # 低負荷寄り。推論の同時実行は 1 件(max_pending_predict = 1)
LOG_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
LOG_DEFAULT_BACKUPS = 5
LOG_DEFAULT_LEVEL = "INFO"
LOG_RELATIVE = os.path.join("logs", "prod_api.log")           # 設定ファイルと同じフォルダ配下
PERF_FIELDS = (
    ("threads", "ワーカースレッド数", "個", "HTTP リクエストを処理するスレッド数。推論は直列(同時に 1 件)なので、増やしても速くなりません。/health などに答えるために使います。"),
    ("max_pending_predict", "同時に受け付ける /predict", "件", "実行中 + 順番待ちの /predict の上限。threads より小さくします。超えると 503。1 なら推論を同時に 1 件だけ受け付けます。"),
    ("queue_wait_sec", "推論の順番待ちの上限", "秒", "超えると 503 を返し、クライアントが再試行できます。実行中の推論は中断されません。"),
    ("backlog", "接続待ち行列の長さ", "件", "OS が受け付けて待たせる接続の数。"),
    ("connection_limit", "同時に開く接続数の上限", "接続", "threads + 2 以上にします(Waitress が待受ソケット等も数えるため。/health の接続を確保します)。"),
    ("channel_timeout_sec", "無通信の接続を閉じるまで", "秒", "リクエストが来ない接続を閉じるまでの時間。"),
    ("shutdown_grace_sec", "停止時に推論の完了を待つ上限", "秒", "停止の要求後、実行中の推論の完了を待つ時間。超えると中断して停止します。"),
)
RETENTION_NOTE = ("ログの保管期間(日数)の設定はありません。サイズ上限と世代数で管理します(最大保存量の目安 = 最大サイズ × (世代数 + 1))。"
                  "期間での削除が必要な場合は、別途の運用(IT との取り決め)になります。")
CLIENT_TIMEOUT_NOTE = {
    "setting": "api.request_timeout", "file": "app.local.yaml(データ作成側の設定。本番 API の設定ではありません)", "recommended_sec": 180,
    "text": ("クライアントの応答待ち(秒)は、本番 API ではなく、データ作成側(製番処理)の設定ファイル app.local.yaml の「api:」節にある request_timeout で指定します。"
             "この画面では編集できません(8765 の運用画面も、出力先と定期実行だけを編集し、この項目は編集できません。値の形式の確認だけをします)。"
             "推奨値は 180 秒です。API 側の順番待ち(最大 30 秒)と、1 ページの推論時間(初回は約 21 秒、以降は約 1 秒。実図面は未測定)の合計を十分に上回り、"
             "API の停止待ち(60 秒)より長くして、通常の遅延でクライアントが先に諦めないようにする意図です。省略すると、応答が無くても待ち続けます。")}


def _strict_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_server_values(values) -> dict:
    """server 節の入力を検証する。{項目: メッセージ}(空なら OK)。範囲は prod_config.LIMIT_RANGES、最終判定は prod_config._limits(同じ検証ロジック)。"""
    errors: dict = {}
    if not isinstance(values, dict):
        return {"_": "性能の設定の形式が正しくありません。"}
    for key in PERF_DEFAULTS:
        v = values.get(key)
        lo, hi = prod_config.LIMIT_RANGES[key]
        if not _strict_int(v):
            errors[key] = f"{lo}〜{hi} の整数で指定してください。"
        elif not (lo <= v <= hi):
            errors[key] = f"{lo}〜{hi} の範囲で指定してください。"
    t, p, c = values.get("threads"), values.get("max_pending_predict"), values.get("connection_limit")
    if "threads" not in errors and "max_pending_predict" not in errors and p >= t:
        errors["max_pending_predict"] = f"threads({t})より小さくしてください(最大 {t - 1}。/health 用のスレッドを残すため)。"
    if "threads" not in errors and "connection_limit" not in errors and c < t + 2:
        errors["connection_limit"] = f"threads + 2 以上にしてください(最小 {t + 2}。/health の接続を確保するため)。"
    if not errors:
        try:
            prod_config._limits({k: values[k] for k in PERF_DEFAULTS})        # 設定の検証そのもの(画面と設定ファイルの検証を一致させる)
        except prod_config.ConfigError as e:
            errors["_"] = str(e)
    return errors


def replace_section(text: str, key: str, block: str, nl: str) -> str:
    """トップレベルの `<key>:` 節だけを置き換える(ほかの節・コメントは保つ)。節が無ければ末尾へ追加する。"""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(re.escape(key) + r":\s*(#.*)?\r?\n?$", ln)), None)
    if start is None:
        sep = "" if (not text or text.endswith(("\n", "\r"))) else nl
        return text + sep + (nl if text else "") + block
    end = start + 1
    while end < len(lines) and (lines[end].strip() == "" or lines[end][0] in " \t"):
        end += 1
    while end > start + 1 and lines[end - 1].strip() == "":       # 節の後ろの空行は節の外(間隔を保つ)
        end -= 1
    return "".join(lines[:start]) + block + "".join(lines[end:])


def build_server_block(values: dict, nl: str) -> str:
    notes = {k: f"{label}({unit})" for k, label, unit, _ in PERF_FIELDS}
    lines = ["server:                                 # Waitress の上限(モデル管理画面で保存。範囲は prod_config と同じ。推論の同時実行は max_pending_predict で制限)"]
    lines += [f"  {k}: {values[k]}                  # {notes[k]}" for k in PERF_DEFAULTS]
    return nl.join(lines) + nl


def build_logging_block(values: dict, nl: str) -> str:
    lines = ["logging:                                # 運用ログ(ローカルのフォルダ)。保管期間の設定は無く、最大サイズ × (世代数 + 1) で管理する",
             f"  file: {_q(values['file'])}",
             f"  max_bytes: {values['max_bytes']}                  # {values['max_bytes'] // (1024 * 1024)} MiB(1 ファイルの上限)",
             f"  backup_count: {values['backup_count']}                  # 残す世代数",
             f"  level: {values['level']}"]
    return nl.join(lines) + nl


class ModelAdmin:
    def __init__(self, config_path=DEFAULT_CONFIG, picker=None, local_dir=None, drive_type=default_drive_type):
        # 絶対パスにそろえる(相対の --config で起動すると、設定用コピーの場所 yaml_dir も相対になり、設定の規則「絶対パス」で保存を拒否されるため。
        # 起動時の作業フォルダ基準で一度だけ解決し、以後は変わらない)。UNC はそのまま(書き込み先の検査 require_local_dir が拒否する)
        self.config_path = Path(os.path.abspath(os.fspath(config_path)))
        self.local_dir = Path(os.path.abspath(os.fspath(local_dir))) if local_dir else self.config_path.parent / LOCAL_DIR_NAME
        self.picker = picker or FilePicker()
        self._drive_type = drive_type
        self._lock = threading.Lock()

    def require_local_storage(self):
        """設定ファイル・バックアップ・一時ファイル(設定ファイルのフォルダ)と、設定用コピーのフォルダが、実体としてローカルディスク内であることを確認する。
        書き込みの前に毎回呼ぶ。証明できなければ AdminError(固定文)。設定ファイル自体がリンクなら拒否する。"""
        require_local_dir(self.config_path.parent, self._drive_type)
        try:
            if os.path.lexists(self.config_path) and _is_reparse_or_link(str(self.config_path)):
                raise AdminError(STORAGE_MESSAGE)
        except AdminError:
            raise
        except Exception:  # noqa: BLE001
            raise AdminError(STORAGE_MESSAGE) from None
        require_local_dir(self.local_dir, self._drive_type)

    # ---- 検査・状態
    def inspect(self, body: dict) -> dict:
        res, _ = inspect_model(body.get("weights_path"), body.get("yaml_path", ""), bool(body.get("remap")), self.local_dir)
        return {"ok": True, "inspection": res}

    def state(self) -> dict:
        cur = {"config_file": self.config_path.name, "exists": False, "model": None, "storage_error": None}
        try:
            self.require_local_storage()
            cur["exists"] = self.config_path.is_file()
        except AdminError as e:                                  # 保存先がローカルと証明できない: 内容は読まず、固定文だけ返す
            cur["storage_error"] = str(e)
        if cur["exists"]:
            try:
                data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                m = data.get("model") or {}
                exp = m.get("expected") or {}
                if m.get("weights"):
                    cur["model"] = {"id": str(m.get("id") or ""), "weights": _safe_name(m.get("weights")), "yaml": _safe_name(m.get("yaml")),
                                    "weights_sha256_12": str(exp.get("weights_sha256") or "")[:prod_model.PREFIX_LEN],
                                    "yaml_sha256_12": str(exp.get("yaml_sha256") or "")[:prod_model.PREFIX_LEN],
                                    "weights_size": exp.get("weights_size") if isinstance(exp.get("weights_size"), int) else None,
                                    "yaml_in_other_folder": bool(m.get("yaml_dir"))}
            except Exception:  # noqa: BLE001
                cur["error"] = "設定ファイルの YAML を読み込めません。"
        return {"ok": True, "default_weights_path": DEFAULT_WEIGHTS_PATH, "default_yaml_path": DEFAULT_YAML_PATH, "current": cur,
                "settings": self.settings_state(storage_ok=cur["storage_error"] is None)}

    # ---- 性能(server 節)・ログ(logging 節)の設定
    def _config_data(self) -> dict:
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) if self.config_path.is_file() else None
        except Exception:  # noqa: BLE001
            data = None
        return data if isinstance(data, dict) else {}

    def log_defaults(self) -> dict:
        return {"file": str(self.config_path.parent / LOG_RELATIVE), "max_bytes": LOG_DEFAULT_MAX_BYTES, "backup_count": LOG_DEFAULT_BACKUPS,
                "level": LOG_DEFAULT_LEVEL}

    def _require_local_log(self, path):
        """ログファイルの置き場所が、実体としてローカルディスク内であること(UNC・ネットワークドライブ・リンク/ジャンクション経由は拒否)。固定文。"""
        require_local_dir(os.path.dirname(os.path.abspath(path)), self._drive_type)
        try:
            if os.path.lexists(path) and _is_reparse_or_link(path):
                raise AdminError(STORAGE_MESSAGE)
        except AdminError:
            raise
        except Exception:  # noqa: BLE001
            raise AdminError(STORAGE_MESSAGE) from None

    def log_storage_error(self):
        """設定済みのログの置き場所がローカルと証明できなければ、固定文(無ければ None)。画面の起動時・状態表示で使う。"""
        f = (self._config_data().get("logging") or {}).get("file") if isinstance(self._config_data().get("logging"), dict) else None
        if not isinstance(f, str) or not f.strip():
            return None
        try:
            self._require_local_log(f.strip())
            return None
        except AdminError as e:
            return str(e)

    def settings_state(self, storage_ok: bool = True) -> dict:
        data = self._config_data() if storage_ok else {}
        srv = data.get("server") if isinstance(data.get("server"), dict) else {}
        lg = data.get("logging") if isinstance(data.get("logging"), dict) else {}
        values, configured = {}, True
        for k, dv in PERF_DEFAULTS.items():
            ok = _strict_int(srv.get(k))
            values[k], configured = (srv[k] if ok else dv), configured and ok
        dlog = self.log_defaults()
        if not storage_ok:
            dlog["file"] = ""                        # 設定ファイルの置き場所がローカルと証明できないときは、そこから作ったパス(UNC 等)を返さない
        lvalues, lconf = {}, True
        for k in ("file", "max_bytes", "backup_count", "level"):
            v = lg.get(k)
            good = (isinstance(v, str) and v.strip() != "") if k in ("file", "level") else _strict_int(v)
            if k == "level":
                good = good and str(v).upper() in prod_config.LOG_LEVELS
            lvalues[k] = (v if good else dlog[k])
            lconf = lconf and good
        lvalues["level"] = str(lvalues["level"]).upper()
        return {
            "server": {"configured": configured, "values": values, "defaults": dict(PERF_DEFAULTS),
                       "fields": [{"key": k, "label": label, "unit": unit, "description": desc,
                                   "min": prod_config.LIMIT_RANGES[k][0], "max": prod_config.LIMIT_RANGES[k][1]} for k, label, unit, desc in PERF_FIELDS]},
            "logging": {"configured": lconf, "values": lvalues, "defaults": dlog, "retention_note": RETENTION_NOTE,
                        "max_bytes_range": list(prod_config.LOG_RANGES["max_bytes"]), "backup_count_range": list(prod_config.LOG_RANGES["backup_count"]),
                        "max_total_mib": round((lvalues["backup_count"] + 1) * lvalues["max_bytes"] / 1048576, 1),
                        "storage_error": self.log_storage_error() if storage_ok else None},
            "client_timeout": CLIENT_TIMEOUT_NOTE,
        }

    def check_logging_values(self, values, data: dict) -> dict:
        errors: dict = {}
        if not isinstance(values, dict):
            return {"_": "ログの設定の形式が正しくありません。"}
        f, mb, bc = values.get("file"), values.get("max_bytes"), values.get("backup_count")
        if not isinstance(f, str) or not f.strip():
            errors["file"] = "ログファイルのパスを入力してください。"
        lo, hi = prod_config.LOG_RANGES["max_bytes"]
        if not _strict_int(mb) or not (lo <= mb <= hi):
            errors["max_bytes"] = f"{lo // 1024} KiB〜{hi // (1024 * 1024)} MiB の範囲で指定してください。"
        lo, hi = prod_config.LOG_RANGES["backup_count"]
        if not _strict_int(bc) or not (lo <= bc <= hi):
            errors["backup_count"] = f"{lo}〜{hi} の整数で指定してください。"
        if "file" not in errors:
            cur = data.get("logging") if isinstance(data.get("logging"), dict) else {}
            level = str(cur.get("level") or LOG_DEFAULT_LEVEL).upper()
            level = level if level in prod_config.LOG_LEVELS else LOG_DEFAULT_LEVEL
            pub = data.get("publish") if isinstance(data.get("publish"), dict) else {}
            model = data.get("model") if isinstance(data.get("model"), dict) else {}
            unset = "Z:\\__prod_admin_unset__"                                    # 未設定の項目は、どの場所とも一致しない値で代用(重なりの検査だけ)
            others = tuple(x for x in (pub.get("backup_dir"), model.get("dir")) if isinstance(x, str) and x.strip())
            try:                                                                  # 設定の検証そのもの(ローカルの絶対パス・.. 不可・出力ルート/server.yaml/退避先/モデルの配下は不可)
                prod_config._log({"file": f.strip(), "max_bytes": mb if _strict_int(mb) else LOG_DEFAULT_MAX_BYTES,
                                  "backup_count": bc if _strict_int(bc) else LOG_DEFAULT_BACKUPS, "level": level},
                                 data.get("output_root") if isinstance(data.get("output_root"), str) and data.get("output_root").strip() else unset,
                                 pub.get("server_yaml") if isinstance(pub.get("server_yaml"), str) and pub.get("server_yaml").strip() else unset, others)
            except prod_config.ConfigError as e:
                errors["file"] = str(e)
            if "file" not in errors:
                try:
                    self._require_local_log(f.strip())                            # 実体の検査(ネットワークドライブ・リンク・ジャンクション)
                except AdminError as e:
                    errors["file"] = str(e)
        return errors

    def validate_settings(self, body: dict) -> dict:
        try:
            self.require_local_storage()
            data = self._config_data()
        except AdminError:
            data = {}
        errors: dict = {}
        if "server" in body:
            errors["server"] = check_server_values(body.get("server"))
        if "logging" in body:
            errors["logging"] = self.check_logging_values(body.get("logging"), data)
        summary = {}
        lg = body.get("logging")
        if isinstance(lg, dict) and _strict_int(lg.get("max_bytes")) and _strict_int(lg.get("backup_count")) and not errors.get("logging", {}).get("max_bytes") \
                and not errors.get("logging", {}).get("backup_count"):
            summary["max_total_mib"] = round((lg["backup_count"] + 1) * lg["max_bytes"] / 1048576, 1)
        return {"ok": True, "valid": not any(errors.values()), "errors": {k: v for k, v in errors.items() if v}, "summary": summary}

    def save_settings(self, body: dict) -> dict:
        sections = [k for k in ("server", "logging") if k in body]
        if not sections:
            raise AdminError("保存する設定(性能・ログ)が指定されていません。")
        self.require_local_storage()                                              # 保存前に、設定ファイルの置き場所がローカルであることを確認する
        path = self.config_path
        if not path.name.endswith(".local.yaml") or path.name.endswith(".sample.yaml"):
            raise AdminError("保存先は Git 管理外の *.local.yaml だけです。")
        with self._lock:
            if not path.is_file():
                raise AdminError("prod_server.local.yaml がありません。sample を写して作ってから保存してください。")
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise AdminError("設定ファイルが UTF-8 ではないため、保存しませんでした。") from None
            if "\ufeff" in text[:1]:
                raise AdminError("設定ファイルに BOM があるため、保存しませんでした。")
            try:
                old = yaml.safe_load(text) or {}
            except Exception:  # noqa: BLE001
                raise AdminError("設定ファイルの YAML を読み込めないため、保存しませんでした。") from None
            if not isinstance(old, dict):
                raise AdminError("設定ファイルの形式が正しくないため、保存しませんでした。")
            errors = {}
            if "server" in sections:
                errors["server"] = check_server_values(body["server"])
            if "logging" in sections:
                errors["logging"] = self.check_logging_values(body["logging"], old)
            errors = {k: v for k, v in errors.items() if v}
            if errors:
                return {"ok": False, "saved": False, "errors": errors, "error": "入力に誤りがあるため保存しませんでした(設定ファイルは変更していません)。"}
            nl = "\r\n" if "\r\n" in text else "\n"
            new_text, expect = text, {}
            if "server" in sections:
                expect["server"] = {k: body["server"][k] for k in PERF_DEFAULTS}
                new_text = replace_section(new_text, "server", build_server_block(expect["server"], nl), nl)
            if "logging" in sections:
                cur = old.get("logging") if isinstance(old.get("logging"), dict) else {}
                level = str(cur.get("level") or LOG_DEFAULT_LEVEL).upper()
                level = level if level in prod_config.LOG_LEVELS else LOG_DEFAULT_LEVEL
                lg = body["logging"]
                expect["logging"] = {"file": os.path.normpath(lg["file"].strip().replace("/", "\\")), "max_bytes": lg["max_bytes"],
                                     "backup_count": lg["backup_count"], "level": level}
                new_text = replace_section(new_text, "logging", build_logging_block(expect["logging"], nl), nl)
            try:                                                                  # 保存前の検証: 読めること・保存する値のとおりであること・ほかの節が変わらないこと
                parsed = yaml.safe_load(new_text)
                for key, val in expect.items():
                    assert parsed[key] == val
                if "server" in expect:
                    prod_config._limits(parsed["server"])
                others_old = {k: v for k, v in old.items() if k not in expect}
                others_new = {k: v for k, v in parsed.items() if k not in expect}
                assert others_old == others_new
            except Exception:  # noqa: BLE001
                raise AdminError("保存する内容の検証に失敗したため、保存しませんでした(設定ファイルは変更していません)。") from None
            self.require_local_storage()                                          # バックアップ・一時ファイルの作成の直前にも確認する
            if "logging" in expect:
                self._require_local_log(expect["logging"]["file"])
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 10**9:09d}"
            backup = path.with_name(f"{path.name[:-len('.local.yaml')]}.bak-{stamp}-{os.getpid()}.local.yaml")
            with open(backup, "xb") as f:                                         # 既存の設定のバックアップ(同じフォルダ。上書きしない)
                f.write(raw)
            self._atomic_write(path, new_text.encode("utf-8"))                    # 原子的に置換
        complete = True
        try:
            prod_config.load_config(str(path))
        except prod_config.ConfigError:
            complete = False
        return {"ok": True, "saved": True, "saved_sections": sections, "backup_file": backup.name, "config_complete": complete,
                "note": "設定だけを保存しました。API の起動・再起動・接続設定の公開は行っていません。"
                        + ("" if complete else "(設定全体は未完了です。check で未設定の項目を確認してください)")}

    # ---- 参照…
    def browse(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in ("weights", "yaml"):
            raise AdminError("選ぶファイルの種類が不正です。")
        start = (body.get("current") or "").strip()
        path = self.picker.pick(kind, start)
        return {"ok": True, "path": path, "cancelled": path is None}

    # ---- 保存
    def save(self, body: dict) -> dict:
        res, internal = inspect_model(body.get("weights_path"), body.get("yaml_path", ""), bool(body.get("remap")), self.local_dir)
        if internal is None:
            return {"ok": False, "saved": False, "error": "検証に通らないため保存しませんでした。", "inspection": res}
        model_id = str(body.get("model_id") or "").strip() or os.path.splitext(internal["weights_name"])[0]
        if not prod_config._ID_RE.fullmatch(model_id):
            raise AdminError("モデル ID は英数字と . _ - だけ(64 文字まで)にしてください。")
        path = self.config_path
        if not path.name.endswith(".local.yaml") or path.name.endswith(".sample.yaml"):
            raise AdminError("保存先は Git 管理外の *.local.yaml だけです。")
        self.require_local_storage()                              # 設定の読み書き・バックアップ・設定用コピーの前に、保存先がローカルであることを確認する
        generated = internal.get("yaml_generated")
        copy_path = None
        if generated is not None:
            copy_path = self.local_dir / internal["yaml_name"]
            if not copy_path.name.endswith(".local.yaml"):
                raise AdminError("設定用コピーは Git 管理外の *.local.yaml だけです。")
        with self._lock:
            if not path.is_file():
                raise AdminError("prod_server.local.yaml がありません。sample を写して作ってから保存してください。")
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise AdminError("設定ファイルが UTF-8 ではないため、保存しませんでした。") from None
            nl = "\r\n" if "\r\n" in text else "\n"
            if "\ufeff" in text[:1]:
                raise AdminError("設定ファイルに BOM があるため、保存しませんでした。")
            new_text = replace_model_section(text, build_model_block(model_id, internal, nl), nl)
            try:                                              # 保存前の検証: YAML として読め、model 節が設定の規則を満たし、内容が算出値と一致する
                parsed = yaml.safe_load(new_text)
                model = prod_config._model(parsed["model"])
                assert (model.weights, model.yaml_name, model.weights_sha256, model.weights_size, model.yaml_sha256) == (
                    internal["weights_name"], internal["yaml_name"], internal["weights_sha256"], internal["weights_size"],
                    internal["yaml_sha256"])
            except Exception:  # noqa: BLE001
                raise AdminError("保存する内容の検証に失敗したため、保存しませんでした(設定ファイルは変更していません)。") from None
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 10**9:09d}"      # 同じ秒の連続保存でも衝突しない
            backup = path.with_name(f"{path.name[:-len('.local.yaml')]}.bak-{stamp}-{os.getpid()}.local.yaml")
            self.require_local_storage()                      # バックアップ・一時ファイルの作成の直前にも確認する
            with open(backup, "xb") as f:                     # 既存の設定のバックアップ(同じフォルダ。上書きしない)
                f.write(raw)
            copy_before = None                                 # 設定用コピーの、保存前の状態(失敗時に戻す)
            try:
                if copy_path is not None:
                    copy_before = copy_path.read_bytes() if copy_path.is_file() else None
                    if copy_before != generated:
                        made_dir = not self.local_dir.exists()
                        self.local_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            self.require_local_storage()      # 作成した後で、もう一度、実体を確認する(未作成の階層も含めて)
                        except AdminError:
                            if made_dir:
                                try:
                                    self.local_dir.rmdir()
                                except OSError:
                                    pass
                            raise
                        if copy_before is not None:           # 既存のコピーが違う内容なら、退避してから置換
                            with open(copy_path.with_name(f"{copy_path.name[:-len('.local.yaml')]}.bak-{stamp}-{os.getpid()}.local.yaml"), "xb") as f:
                                f.write(copy_before)
                        self._atomic_write(copy_path, generated)
                self._atomic_write(path, new_text.encode("utf-8"))      # 原子的に置換
            except BaseException:
                if copy_path is not None:                      # 設定の置換に失敗したら、コピーも保存前の状態へ戻す(既存の設定は壊さない)
                    try:
                        if copy_before is None:
                            if copy_path.exists():
                                copy_path.unlink()
                        elif copy_path.read_bytes() != copy_before:
                            self._atomic_write(copy_path, copy_before)
                    except OSError:
                        pass
                raise
        complete = True
        try:
            prod_config.load_config(str(path))
        except prod_config.ConfigError:
            complete = False
        return {"ok": True, "saved": True, "backup_file": backup.name, "model_id": model_id, "inspection": res,
                "yaml_copy_file": copy_path.name if copy_path is not None else None,
                "config_complete": complete,
                "note": "モデル設定だけを保存しました。API の起動・再起動・接続設定の公開は行っていません。"
                        + ("" if complete else "(設定全体は未完了です。check で未設定の項目を確認してください)")}

    @staticmethod
    def _atomic_write(path: Path, data: bytes):
        fd, tmp = tempfile.mkstemp(prefix=".prod_admin_", suffix=".local.yaml.tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- check(読み取り専用)
    def check(self) -> dict:
        import prod_server
        try:
            cfg = prod_config.load_config(str(self.config_path))
        except prod_config.ConfigError as e:
            return {"ok": True, "check_ok": False, "stage": "config", "error": str(e)}
        result = prod_server.run_check(cfg)       # 待ち受けもファイルの書き込みもしない(read-only。API の起動・公開はしない)
        return {"ok": True, "check_ok": bool(result.get("ok")), "stage": "check", "result": result}


# ----------------------------------------------------------------------------- 参照…(ファイル選択)
_SCRIPT = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$o=New-Object System.Windows.Forms.Form;$o.TopMost=$true;$o.ShowInTaskbar=$false;"
    "$o.StartPosition='CenterScreen';$o.Size=New-Object System.Drawing.Size(0,0);"
    "$d=New-Object System.Windows.Forms.OpenFileDialog;$d.Filter=$env:PA_FILTER;$d.Title=$env:PA_TITLE;"
    "$d.CheckFileExists=$true;$d.Multiselect=$false;$d.ValidateNames=$true;"
    "if($env:PA_INIT){$d.InitialDirectory=$env:PA_INIT};"
    "if($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($d.FileName)}"
)
_KINDS = {"weights": ("モデルファイル (*.pt)|*.pt", "図面解析 AI モデル(.pt)を選んでください", WEIGHTS_EXTS, "重みファイル"),
          "yaml": ("model.yaml (*.yaml;*.yml)|*.yaml;*.yml", "重みと組になる model.yaml を選んでください", YAML_EXTS, "model.yaml")}


def default_runner(args, env):
    return subprocess.run(args, capture_output=True, check=False, timeout=PICK_TIMEOUT_SECONDS, env=env)   # noqa: S603 - 引数は配列


class FilePicker:
    """API ホストの画面にファイル選択ダイアログを出す(同時に 1 つ)。ネットワーク(UNC)の場所も選べる。何も作成・コピーしない。
    テストでは runner を差し替える(実ダイアログ・実プロセスは起動しない)。"""

    def __init__(self, runner=default_runner, powershell: str = "powershell"):
        self._runner, self._powershell, self._lock = runner, powershell, threading.Lock()

    def pick(self, kind: str, current: str = ""):
        flt, title, exts, what = _KINDS[kind]
        if not self._lock.acquire(blocking=False):
            raise PickerBusy("ファイル選択の画面がすでに開いています。")
        try:
            init = ""
            try:
                cur = validate_model_path(current, exts, what)
                init = os.path.dirname(cur)
            except AdminError:
                pass
            env = {**os.environ, "PA_FILTER": flt, "PA_TITLE": title, "PA_INIT": init}
            try:
                res = self._runner([self._powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", _SCRIPT], env)
            except Exception as e:  # noqa: BLE001 - 理由は型名だけ
                raise AdminError(f"ファイル選択の画面を開けませんでした({type(e).__name__})。") from e
            if getattr(res, "returncode", 1) != 0:
                raise AdminError("ファイル選択の画面を開けませんでした。")
            raw = getattr(res, "stdout", b"")
            try:
                chosen = (raw if isinstance(raw, str) else raw.decode("utf-8")).strip()
            except UnicodeDecodeError:
                chosen = ""
            if not chosen:
                return None                                   # キャンセル
            return validate_model_path(chosen, exts, what)
        finally:
            self._lock.release()


# ----------------------------------------------------------------------------- HTTP(loopback 限定)
def peer_is_loopback(addr) -> bool:
    return str(addr) in _LOOPBACK_PEERS


def make_handler(admin: ModelAdmin):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SekisanProdAdmin/1"

        def log_message(self, fmt, *args):      # アクセスログは出さない(パス・ファイル名を残さない)
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, code: int, message: str):
            self._json(code, {"ok": False, "error": message})

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost")

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            m = re.fullmatch(r"http://(127\.0\.0\.1|localhost)(:\d+)?", origin.lower())
            return bool(m)

        def _slurp(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            self._too_big = n > MAX_BODY
            self._raw = b"" if self._too_big or n <= 0 else self.rfile.read(n)
            if self._too_big:
                self.close_connection = True

        def _body(self) -> dict:
            if getattr(self, "_too_big", False):
                raise AdminError("送信内容が大きすぎます。")
            raw = getattr(self, "_raw", b"")
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise AdminError("送信内容の形式が正しくありません。") from None
            if not isinstance(data, dict):
                raise AdminError("送信内容の形式が正しくありません。")
            return data

        def _guard(self, mutating: bool) -> bool:
            if not peer_is_loopback(self.client_address[0]):
                self._error(403, "この PC(API ホスト)からだけ使えます。")
                return False
            if not self._host_ok() or not self._origin_ok():
                self._error(403, "このホスト名・送信元ではアクセスできません。")
                return False
            if mutating and (self.headers.get("X-Admin-Request") != "1"
                             or not (self.headers.get("Content-Type") or "").lower().startswith("application/json")):
                self._error(403, "不正なリクエストです。")
                return False
            return True

        def do_GET(self):
            if not self._guard(False):
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._handle(admin.state)
            else:
                self._error(404, "見つかりません。")

        def do_POST(self):
            self._slurp()
            if not self._guard(True):
                return
            routes = {"/api/model/browse": lambda: admin.browse(self._body()), "/api/model/inspect": lambda: admin.inspect(self._body()),
                      "/api/model/save": lambda: admin.save(self._body()), "/api/check": admin.check,
                      "/api/settings/validate": lambda: admin.validate_settings(self._body()),
                      "/api/settings/save": lambda: admin.save_settings(self._body())}
            fn = routes.get(self.path.split("?", 1)[0])
            if fn is None:
                return self._error(404, "見つかりません。")
            self._handle(fn)

        def _handle(self, fn):
            try:
                self._json(200, fn())
            except AdminError as e:
                self._error(400, str(e))
            except PickerBusy as e:
                self._error(409, str(e))
            except Exception as e:  # noqa: BLE001 - 画面には型名だけ(内部の詳細・パスは出さない)
                self._error(500, f"処理に失敗しました({type(e).__name__})。")

    return Handler


def make_server(admin: ModelAdmin, port: int = DEFAULT_PORT, host: str = LOOPBACK) -> ThreadingHTTPServer:
    if host != LOOPBACK:
        raise ValueError("管理画面は 127.0.0.1 だけで待ち受けます。")
    return ThreadingHTTPServer((host, port), make_handler(admin))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="本番推論 API の図面解析 AI モデル管理画面(このPCだけ)")
    p.add_argument("serve", nargs="?", default="serve")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="保存先(Git 管理外の prod_server.local.yaml)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)
    admin = ModelAdmin(args.config)
    try:
        admin.require_local_storage()                        # 起動設定が不正(保存先がローカルと証明できない)なら、待ち受ける前に固定文で終了する
    except AdminError as e:
        print(f"起動を拒否: {e}", file=sys.stderr)
        return 2
    log_error = admin.log_storage_error()                    # 設定済みのログの置き場所もローカルか検査する(不正でも画面は起動し、直せるようにする。API の起動 serve / check は拒否する)
    if log_error:
        print(f"警告(ログの置き場所): {log_error}", file=sys.stderr)
    server = make_server(admin, port=args.port)
    print(f"モデル管理画面: http://127.0.0.1:{args.port}/  (Ctrl+C で終了。API の起動・公開はしません)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
