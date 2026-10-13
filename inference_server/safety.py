"""推論サーバーの安全判定(標準ライブラリのみ。サーバーとデータ作成側の両方から読み込む)。

- 本番共有(またはその配下)を出力ルートにすることを拒否する。
- 許可した出力ルートの外へ、読み書きするパスを解決させない。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 本番の共有フォルダを指すホスト名は、リポジトリに書かず、環境変数またはローカルファイルで指定する(接頭辞は小文字・バックスラッシュ区切りで比較する)。
#   - 環境変数 SEKISAN_PRODUCTION_HOSTS(カンマ・セミコロン・空白区切り)
#   - inference_server/production_hosts.local.txt(1行1ホスト。# 以降はコメント。Git 管理外 = .gitignore の *.local.txt)
# このホスト配下は、検証モードでは出力ルート・設定出力先のどちらにも使えない。
# 本番ホストが1つも設定されていないときは、安全側に倒して「UNC はすべて本番扱い」とする(UNC の出力ルートは既定でも拒否される)。
PRODUCTION_HOSTS_ENV = "SEKISAN_PRODUCTION_HOSTS"
PRODUCTION_HOSTS_FILE = Path(__file__).with_name("production_hosts.local.txt")


def production_hosts() -> tuple:
    """設定された本番ホスト名(小文字)。未設定なら空のタプル。"""
    hosts = [h for h in re.split(r"[,;\s]+", os.environ.get(PRODUCTION_HOSTS_ENV, "")) if h]
    try:
        for line in PRODUCTION_HOSTS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                hosts.append(line)
    except OSError:
        pass
    return tuple(dict.fromkeys(h.lower() for h in hosts))

ALLOWED_EXTENSIONS = ("png", "jpeg", "jpg")
_PAGE_RE = re.compile(r"^[0-9]{1,6}$")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class SafetyError(ValueError):
    """安全条件に反する入力。メッセージはそのままログ/HTTP応答に出してよい内容だけにする。"""


def _tidy(p: str) -> str:
    p = p.replace("/", "\\")
    if p.lower().startswith("\\\\?\\unc\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(p).rstrip("\\")


def _lexical(path: str) -> str:
    """ファイルシステムに一切アクセスしない正規化(abspath は文字列処理のみ)。本番共有に対する I/O を避けるために使う。"""
    return _tidy(os.path.abspath(str(path)))


def _normalize(path: str) -> str:
    """比較用に実体(リンク解決後)へ正規化する。ファイルシステムへアクセスするため、本番判定の後でのみ呼ぶこと。"""
    return _tidy(os.path.realpath(os.path.abspath(str(path))))


def _lexical_forms(path: str):
    """I/O なしの比較用文字列を2通り返す(生の文字列と abspath 後)。abspath は `\\\\host` のような形を崩すことがあるため両方で判定する。"""
    raw = str(path)
    return {_tidy(raw), _lexical(raw)}


def is_unc(path: str) -> bool:
    return any(f.startswith("\\\\") for f in _lexical_forms(path))


def _is_prod_text(candidate: str) -> bool:
    hosts = production_hosts()
    if not hosts:  # 本番ホスト未設定: UNC はすべて本番扱い(安全側)
        return candidate.startswith("\\\\")
    for host in hosts:
        prefix = "\\\\" + host
        if candidate == prefix or candidate.startswith(prefix + "\\"):
            return True
    return False


def is_production_path(path: str) -> bool:
    """本番共有ホスト配下なら True。まず I/O なしの字句判定を行い、本番と判定できたら実体解決(ネットワーク照会)をしない。
    UNC 以外(マップドライブ等)は実体解決後も判定する(その解決は I/O を伴うが、本番共有そのものを開くことはしない)。"""
    if any(_is_prod_text(f) for f in _lexical_forms(path)):
        return True
    if is_unc(path):
        return False  # 非本番ホストのUNC: 字句判定で十分(これ以上の I/O はしない)
    return _is_prod_text(_normalize(path))


def assert_not_production(path: str, what: str = "パス") -> None:
    if is_production_path(path):
        raise SafetyError(f"{what}が本番共有(またはその配下)を指しています。検証モードでは使用できません。")


def validate_output_root(root: str, allow_unc: bool = False) -> Path:
    """検証用の出力ルートを検証して絶対パスで返す。本番共有は拒否。UNCは既定で拒否(別名表記を判別できないため)。"""
    if not root or not str(root).strip():
        raise SafetyError("出力ルートが指定されていません。")
    assert_not_production(root, "出力ルート")
    if is_unc(root) and not allow_unc:
        raise SafetyError("UNCパスの出力ルートは既定で拒否します(別名表記で本番共有を指す可能性を排除できないため)。")
    return Path(os.path.abspath(root))


def is_under(path: str, root: str, allow_equal: bool = False) -> bool:
    """path(実体解決後)が root(実体解決後)の配下か。別ドライブ等で比較できない場合は False。"""
    p, r = _normalize(path), _normalize(root)
    try:
        common = os.path.commonpath([p, r])
    except ValueError:
        return False
    if common != r:
        return False
    return allow_equal or p != r


def assert_under(path: str, root: str, what: str = "パス", allow_equal: bool = False) -> None:
    if not is_under(path, root, allow_equal=allow_equal):
        raise SafetyError(f"{what}が許可された出力ルートの外を指しています。")


def validate_loopback(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise SafetyError("検証モードで bind できるのは loopback(127.0.0.1 / localhost / ::1)だけです。")
    return host


def validate_port(port: int) -> int:
    if not isinstance(port, int) or not (1024 <= port <= 65535):
        raise SafetyError("検証用ポートは 1024〜65535 の固定値で指定してください(0 や自動採番は不可)。")
    return port


def validate_work_dir(work_dir, output_root: str) -> str:
    """work_dir を検証して実体パスを返す。絶対パスで、実体解決後に出力ルートの"配下"の既存ディレクトリ(ルート自身は不可)。"""
    if not isinstance(work_dir, str) or not work_dir.strip() or "\x00" in work_dir:
        raise SafetyError("work_dir が不正です。")
    if not os.path.isabs(work_dir):
        raise SafetyError("work_dir は絶対パスで指定してください。")
    assert_not_production(work_dir, "work_dir")
    assert_under(work_dir, output_root, "work_dir")
    real_dir = os.path.realpath(work_dir)
    if not os.path.isdir(real_dir):
        raise SafetyError("work_dir が存在しません。")
    return real_dir


def validate_request(work_dir, page_nmb, extension, output_root: str):
    """/predict のリクエスト値を検証し、(work_dir実体, ページ番号, 拡張子, 画像パス) を返す。

    - work_dir: validate_work_dir の条件。 - page_nmb: 数字1〜6桁のみ。 - extension: png / jpeg / jpg のみ。
    - 画像ファイル自体も実体解決後に出力ルート配下の通常ファイルであること(リンクでの脱出を拒否)。
    """
    if not isinstance(page_nmb, str) or not _PAGE_RE.match(page_nmb):
        raise SafetyError("page_nmb は数字(1〜6桁)のみ指定できます。")
    ext = (extension or "").lower() if isinstance(extension, str) else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise SafetyError(f"extension は {', '.join(ALLOWED_EXTENSIONS)} のみ指定できます。")
    real_dir = validate_work_dir(work_dir, output_root)
    image = os.path.join(real_dir, f"{page_nmb}.{ext}")
    real_image = os.path.realpath(image)
    assert_under(real_image, output_root, "画像ファイル")
    if not os.path.isfile(real_image):
        raise SafetyError("対象の画像ファイルが存在しません。")
    return real_dir, page_nmb, ext, real_image
