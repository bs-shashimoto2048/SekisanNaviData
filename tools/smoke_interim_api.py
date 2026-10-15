"""暫定運用 API のスモーク確認(docs/inference_api_interim_operation.md §3.3・§3.5)。

手動で起動済みの推論 API(tools/run_interim_api.ps1。検証専用サーバー)に対して、本番モードの経路(事前確認 → 推論の依頼 → 検出結果ファイル)を
**合成データだけ**で確認する。ホスト DB・設計サーバー・CAD・実図面・本番共有・実タスクには触れない。

    .venv\\Scripts\\python.exe tools\\smoke_interim_api.py [--config app_interim.yaml] [--keep]

安全策(満たさなければ、通信も書き込みも削除もせず終了コード 2 で拒否する):
- 設定は app_interim*.yaml(リポジトリ内)だけ。app.yaml / app.local.yaml は読まない。統合モード(inference_server.enabled)・プロキシ使用は拒否。
- data.output と api.dir_venv は、リポジトリ配下の ./output_interim の内側であること。**表記上の位置**で判定し、そこに至る既存の各階層に
  シンボリックリンク・ジャンクション等の再解析ポイントがあれば拒否する(実体解決後に内側に見えても、外へ出る経路を作らない)。UNC・ネットワークドライブも拒否。
- server.yaml の接続先は 127.0.0.1 / localhost だけ(それ以外は接続前に拒否)。プロキシは使わない。server.yaml がリンクの場合も拒否。
- 書き込むのは ./output_interim/smoke/ 配下の、この実行が新規に作る合成の製番フォルダ(合成 PNG 1 枚)だけ。終了時は、自分が作った印(乱数のトークン)を
  確認できたそのフォルダと、自分が新規に作った空の親フォルダだけを削除する(--keep で残せる)。
- 出力は固定の文言だけ。接続先・パス・認証情報・例外メッセージ・APIの依頼ログは表示しない(例外型と件数だけ)。
終了コード: 0 = すべて OK / 1 = 確認に失敗 / 2 = 安全条件により拒否(何も実行していない)。
"""
import argparse
import contextlib
import io
import os
import re
import shutil
import stat
import struct
import sys
import uuid
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
import yaml  # noqa: E402

ISOLATED_DIRNAME = "output_interim"
SMOKE_DIRNAME = "smoke"
OWNER_MARKER = ".smoke_owner"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")           # IPv6(::1)は、クライアント側の URL 組み立てが対応しないため対象外
FORBIDDEN_HEALTH_KEYS = ("output_root", "pid")        # /health の本文に含まれてはいけない内部情報(docs §5)
PAGE = "1"
EXTENSION = "png"
DEFAULT_TIMEOUT = 120.0                               # スモーク専用の待ち時間[秒]。api.request_timeout(未決定・GPU で実測)とは無関係
DETECTION_LINE = re.compile(r"^Index\$\d+\|Device\$[^|$\r\n]*\|Score\$[-+0-9.eE]+\|Coordinates\$[-+0-9.eE,:]+$")


class SmokeRefused(Exception):
    """安全条件を満たさないため拒否(何も実行しない)。"""


class SmokeFailed(Exception):
    """確認に失敗(理由は固定の文言)。"""


# ---------------------------------------------------------------------------- 安全条件
def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def is_under(path, root) -> bool:
    """実体解決後に、path が root の配下(root 自身は不可)か。"""
    p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([p, r]) == r and p != r
    except ValueError:                                  # 別ドライブ等
        return False


def lexically_within(path, root, allow_equal: bool = True) -> bool:
    """表記上(.. を畳んだ後)、path が root の内側か。実体解決はしない。"""
    p, r = _norm(path), _norm(root)
    try:
        if os.path.commonpath([p, r]) != r:
            return False
    except ValueError:
        return False
    return allow_equal or p != r


def is_link(path) -> bool:
    """シンボリックリンク・ジャンクション等の再解析ポイント(Windows の FILE_ATTRIBUTE_REPARSE_POINT)か。存在しなければ False。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def has_link_in_path(path, base) -> bool:
    """base から path までの、既存の各階層(path 自身を含む)に再解析ポイントがあるか。path は base の内側であること。"""
    rel = os.path.relpath(os.path.normpath(str(path)), os.path.normpath(str(base)))
    cur = os.path.normpath(str(base))
    for part in Path(rel).parts:
        if part in ("", "."):
            continue
        cur = os.path.join(cur, part)
        if is_link(cur):
            return True
    return False


def is_network_drive(path) -> bool:
    """Windows のネットワークドライブ(DRIVE_REMOTE)か。UNC もここで True になる。Windows 以外は False。"""
    if os.name != "nt":
        return False
    import ctypes
    anchor = os.path.splitdrive(os.path.realpath(path))[0]
    if anchor.startswith("\\\\"):
        return True
    return ctypes.windll.kernel32.GetDriveTypeW(anchor + "\\") == 4


def _is_unc_text(text: str) -> bool:
    return str(text).replace("/", "\\").startswith("\\\\")


def validate_config(config_path, repo_root, *, net_drive=is_network_drive):
    """設定ファイルを検証して (設定, 隔離出力ルート, API 設定フォルダ) を返す。違反は SmokeRefused。通信・書き込み・削除はしない。"""
    repo_root = Path(repo_root)
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = repo_root / cfg_path
    cfg_path = cfg_path.resolve()
    if not is_under(cfg_path, repo_root) or not cfg_path.name.startswith("app_interim"):
        raise SmokeRefused("設定は、リポジトリ内の app_interim*.yaml だけ指定できます(app.yaml / app.local.yaml は使いません)")
    try:
        config = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 - メッセージ(パスを含み得る)は出さない
        raise SmokeRefused(f"設定ファイルを読み込めません({type(e).__name__})") from None
    if not isinstance(config, dict):
        raise SmokeRefused("設定ファイルの形式が不正です")
    if (config.get("inference_server") or {}).get("enabled"):
        raise SmokeRefused("推論サーバー統合(inference_server.enabled)の設定は使えません(本番モードの設定だけ)")
    api = config.get("api") or {}
    if api.get("use_proxy") is not False:
        raise SmokeRefused("api.use_proxy: false が必要です(この PC の loopback だけを使います)")
    isolated = os.path.normpath(str(repo_root / ISOLATED_DIRNAME))
    roots = {}
    for label, value in (("data.output", (config.get("data") or {}).get("output")), ("api.dir_venv", api.get("dir_venv"))):
        if not isinstance(value, str) or not value or _is_unc_text(value):
            raise SmokeRefused(f"{label} は、./{ISOLATED_DIRNAME} 配下の相対パスで指定してください(UNC は不可)")
        p = Path(value)
        full = os.path.normpath(str(p if p.is_absolute() else repo_root / p))           # 表記上の位置(実体解決しない)
        if not lexically_within(full, isolated):
            raise SmokeRefused(f"{label} は ./{ISOLATED_DIRNAME} の内側だけ指定できます")
        if has_link_in_path(full, repo_root):
            raise SmokeRefused(f"{label} に至る階層に、シンボリックリンク・ジャンクション等があります(外へ出る経路になり得るため拒否)")
        if net_drive(full):
            raise SmokeRefused(f"{label} がネットワークドライブ・共有を指しています")
        roots[label] = Path(full)
    if not isinstance(api.get("yaml_file"), str) or Path(api["yaml_file"]).name != api["yaml_file"]:
        raise SmokeRefused("api.yaml_file は、ファイル名だけで指定してください")
    if has_link_in_path(os.path.join(isolated, SMOKE_DIRNAME), repo_root) or has_link_in_path(isolated, repo_root):
        raise SmokeRefused("隔離出力先(output_interim・smoke)がシンボリックリンク・ジャンクション等です(外へ出る経路になり得るため拒否)")
    return config, roots["data.output"], roots["api.dir_venv"]


def read_loopback_target(server_yaml: Path, repo_root=None):
    """server.yaml から (ホスト, ポート) を読む。loopback 以外・不正なポート・リンクは SmokeRefused(接続する前に拒否)。"""
    if repo_root is not None and has_link_in_path(server_yaml, repo_root):
        raise SmokeRefused("server.yaml がシンボリックリンク等です(別の場所を指し得るため拒否)")
    try:
        data = yaml.safe_load(Path(server_yaml).read_text(encoding="utf-8")) or {}
        host, port = str(data["host"]["pc_ip"]), int(data["host"]["port"])
    except Exception as e:  # noqa: BLE001
        raise SmokeFailed(f"server.yaml を読み込めません({type(e).__name__})。API が起動済みか確認してください") from None
    if host not in LOOPBACK_HOSTS:
        raise SmokeRefused("server.yaml の接続先が loopback(127.0.0.1 / localhost)ではないため、接続しません(外部ホストへは接続しない)")
    if not (1024 <= port <= 65535):
        raise SmokeRefused("server.yaml のポートが範囲外です")
    return host, port


# ---------------------------------------------------------------------------- 確認の部品
def synthetic_png(size: int = 64) -> bytes:
    """合成の白画像(RGB)。実図面ではない。"""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    row = b"\x00" + b"\xff" * (size * 3)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))


def check_health_body(host: str, port: int, get) -> None:
    """GET /health(loopback・プロキシなし・リダイレクト追跡なし)が 200・ready で、内部情報を含まないこと。"""
    res = get(f"http://{host}:{port}/health", proxies={"http": None, "https": None}, timeout=5.0, allow_redirects=False)
    if res.status_code != 200:
        raise SmokeFailed(f"/health が HTTP 200 ではありません(HTTP {res.status_code})")
    try:
        body = res.json()
    except Exception:  # noqa: BLE001
        raise SmokeFailed("/health の応答が JSON ではありません") from None
    if not isinstance(body, dict) or body.get("ready") is not True:
        raise SmokeFailed("/health が準備完了(ready)ではありません")
    leaked = [k for k in FORBIDDEN_HEALTH_KEYS if k in body]
    if leaked:
        raise SmokeFailed("/health の本文に内部情報が含まれています(" + ", ".join(leaked) + ")")


def default_request(dir_product: str, pages: list, api_config_path: str, timeout: float):
    """推論の依頼(本番モードと同じクライアント層。プロキシなし)。依頼ログ(接続先・パスを含む)と標準出力は捨てる。"""
    from pipeline import inference_client
    with contextlib.redirect_stdout(io.StringIO()):          # image_mod は経過を標準出力に出す(接続先・パスを含む)
        inference_client.request_detections(dir_product, pages, api_config_path, use_proxy=False, timeout=timeout)


def check_detected_file(dir_product: Path, page: str) -> int:
    """{page}_detected.txt が UTF-8 で読め、各行が契約の形式であること。検出件数を返す(0 件=空ファイルは正常)。"""
    f = Path(dir_product) / f"{page}_detected.txt"
    if not f.is_file():
        raise SmokeFailed(f"{page}_detected.txt が作られていません")
    try:
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except UnicodeDecodeError:
        raise SmokeFailed(f"{page}_detected.txt が UTF-8 ではありません") from None
    bad = [i for i, ln in enumerate(lines, 1) if not DETECTION_LINE.match(ln)]
    if bad:
        raise SmokeFailed(f"{page}_detected.txt に、形式が契約と合わない行があります({len(bad)} 行)")
    return len(lines)


class OwnedFolder:
    """この実行が新規に作った合成フォルダ(と、新規に作った親フォルダ)だけを管理する。印(乱数のトークン)を確認できたものだけを削除する。"""

    def __init__(self, smoke_root: Path, repo_root: Path):
        self.smoke_root, self.repo_root = Path(smoke_root), Path(repo_root)
        self.folder = self.smoke_root / f"smoke_{uuid.uuid4().hex[:8]}"
        self.token = uuid.uuid4().hex
        self.created_parents = []        # 新規に作った親フォルダ(浅い順)
        self.created = False

    def create(self) -> Path:
        if has_link_in_path(self.folder.parent, self.repo_root):
            raise SmokeRefused("smoke フォルダに至る階層に、リンク・ジャンクション等があります(拒否)")
        missing = []
        cur = self.folder.parent
        while not cur.exists() and lexically_within(cur, self.repo_root / ISOLATED_DIRNAME):
            missing.append(cur)
            cur = cur.parent
        for d in reversed(missing):
            d.mkdir()                                          # 既存なら失敗(他者のものは使わない)
            self.created_parents.append(d)
        self.folder.mkdir()                                    # exist_ok=False: 既にあれば失敗(自分が作ったものだけを対象にする)
        self.created = True
        (self.folder / OWNER_MARKER).write_text(self.token, encoding="ascii")
        if has_link_in_path(self.folder, self.repo_root):      # 作成直後の再確認
            raise SmokeFailed("合成フォルダの階層にリンク等を検出しました")
        return self.folder

    def _is_mine(self) -> bool:
        try:
            return (self.folder.parent.resolve() == self.smoke_root.resolve()
                    and not has_link_in_path(self.folder, self.repo_root)
                    and (self.folder / OWNER_MARKER).read_text(encoding="ascii") == self.token)
        except OSError:
            return False

    def cleanup(self) -> bool:
        """自分が作ったものだけを削除する。想定外(印が違う・リンクがある)なら何も削除せず False。"""
        if not self.created:
            return True
        if not self._is_mine():
            return False
        for entry in self.folder.rglob("*"):
            if is_link(entry):
                return False                                   # 想定外のリンクがある(内容を辿らない)
        shutil.rmtree(self.folder, ignore_errors=True)
        for d in reversed(self.created_parents):               # 新規に作った空の親だけ(空でなければ残る)
            try:
                d.rmdir()
            except OSError:
                break
        return True


# ---------------------------------------------------------------------------- 本体
def main(argv=None, *, get=requests.get, request=default_request, net_drive=is_network_drive, repo_root: Path = ROOT, out=print) -> int:
    ap = argparse.ArgumentParser(description="暫定運用 API のスモーク確認(合成データ・loopback・隔離出力先だけ)")
    ap.add_argument("--config", default="app_interim.yaml", help="app_interim*.yaml(既定: app_interim.yaml)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="推論の依頼の待ち時間[秒](スモーク専用)")
    ap.add_argument("--keep", action="store_true", help="合成の製番フォルダを削除せずに残す")
    args = ap.parse_args(argv)
    if not (0 < args.timeout < 3600):
        out("REFUSED: --timeout は 0 より大きく 3600 未満で指定してください")
        return 2

    repo_root = Path(repo_root).resolve()
    owned = None
    try:
        # 1. 安全条件(通信・書き込みの前)
        config, out_root, api_dir = validate_config(Path(args.config), repo_root, net_drive=net_drive)
        server_yaml = api_dir / config["api"]["yaml_file"]
        if not server_yaml.is_file():
            raise SmokeFailed("server.yaml が見つかりません。tools\\run_interim_api.ps1 で API を起動してください")
        host, port = read_loopback_target(server_yaml, repo_root)
        out("[1/5] 安全条件: OK(隔離出力先・リンクなし・loopback・プロキシなし)")

        # 2. 製番処理の事前確認と同じ判定(本番モードの経路)
        from pipeline import api_preflight
        reason = api_preflight.unavailable_reason(
            {"api": {"dir_venv": str(api_dir), "yaml_file": config["api"]["yaml_file"], "use_proxy": False,
                     "health_timeout": (config["api"].get("health_timeout", api_preflight.HEALTH_TIMEOUT))}}, get=get)
        if reason is not None:
            raise SmokeFailed(reason)
        out("[2/5] 事前確認(/health): OK(製番処理の事前確認と同じ判定)")

        # 3. /health の本文に内部情報が無いこと(目標契約)
        check_health_body(host, port, get)
        out("[3/5] /health の本文: OK(ready・内部情報なし)")

        # 4. 合成の製番フォルダ(常に ./output_interim/smoke/ 配下)で推論を依頼
        owned = OwnedFolder(repo_root / ISOLATED_DIRNAME / SMOKE_DIRNAME, repo_root)
        folder = owned.create()
        (folder / f"{PAGE}.{EXTENSION}").write_bytes(synthetic_png())
        try:
            request(str(folder), [PAGE], str(server_yaml), args.timeout)
        except Exception as e:  # noqa: BLE001 - 依頼ログ・メッセージ(接続先・パスを含み得る)は出さない
            failed = getattr(e, "failed_pages", None)
            extra = f"、失敗ページ {len(failed)} 件" if failed else ""
            raise SmokeFailed(f"推論の依頼に失敗しました({type(e).__name__}{extra})") from None
        out("[4/5] 推論の依頼(合成画像 1 枚): OK")

        # 5. 検出結果ファイル
        n = check_detected_file(folder, PAGE)
        out(f"[5/5] 検出結果ファイル: OK(検出 {n} 件。0 件は正常)")
        out("RESULT: OK")
        return 0
    except SmokeRefused as e:
        out(f"REFUSED: {e}")
        return 2
    except SmokeFailed as e:
        out(f"NG: {e}")
        out("RESULT: NG")
        return 1
    finally:
        if owned is not None and not args.keep:
            if not owned.cleanup():
                out("NOTE: 合成フォルダを削除しませんでした(想定外の状態を検出)。./output_interim/smoke/ を確認してください")


if __name__ == "__main__":
    sys.exit(main())
