"""本番推論 API の運用ログ。標準ライブラリのみ。

- 出力先: 標準出力(前景プロセスの端末)と、設定した**ローカルファイル**(`RotatingFileHandler`: サイズ上限・世代数は設定)。
- **漏えい防止**: 各ハンドラーに `RedactingFilter` を付け、ログ行を出す直前に次を伏せる(どのロガー・どのライブラリ(waitress など)からの出力にも効く)。
    - 例外のトレースバック(`exc_info` / `exc_text`)は**丸ごと捨てる**(例外メッセージにパス・アドレスが入り得るため)
    - UNC パス・ドライブ文字のパス・2 階層以上の POSIX パス、IPv4 アドレス(ポート付き含む)、`Bearer <token>`・`Authorization` 値、
      長い 16 進文字列(ハッシュ・トークン)
  本実装のコード自体も、例外メッセージは出さず、型名と呼び出し位置(prod_server.safe_trace)だけを記録する。フィルターは第二の防御で、
  任意の自由文の秘密(例: 例外メッセージ中の固有名)まで消せる保証はない(docs/production_inference_api.md §8)。
- 記録するもの: 起動・停止、モデルの準備状態、公開の結果、固定形式のエラー、/predict の結果(ページ番号・状態・件数・所要時間)。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import time

_UNC = re.compile(r"\\\\[^\s'\"<>|]+")
_DRIVE = re.compile(r"(?<![0-9A-Za-z])[A-Za-z]:[\\/][^\s'\"<>|]*")
_POSIX = re.compile(r"(?<![\w.:/])/(?:[\w.\-]+/)+[\w.\-]*")
_IPV4 = re.compile(r"(?<![0-9A-Za-z])\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?![0-9A-Za-z])")
_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;]+")
_AUTH = re.compile(r"(?i)\b(authorization:?)\s+(?!<redacted>)[^\s,;]+")
_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")     # `_` の直後・直前も対象(\b だと抜ける)
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")                   # 改行・制御文字(ログの偽造・行の分断を防ぐ)
MAX_LINE = 500                                                              # 1 行の長さの上限(外部から来た文字列でログを溢れさせない)


def redact(text: str) -> str:
    text = _CTRL.sub(" ", text)
    text = _SCHEME.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _AUTH.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _UNC.sub("<path>", text)
    text = _DRIVE.sub("<path>", text)
    text = _POSIX.sub("<path>", text)
    text = _IPV4.sub("<addr>", text)
    text = _HEX.sub("<hex>", text)
    return text if len(text) <= MAX_LINE else text[:MAX_LINE] + "...(省略)"


class RedactingFilter(logging.Filter):
    """ログ行を、整形後に伏せ字へ置き換え、例外情報を捨てる。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001  整形に失敗した行は、固定文にする(例外メッセージを出さない)
            msg = "(ログの整形に失敗しました)"
        record.msg, record.args = redact(msg), None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOGGERS = ("prod_inference_server", "waitress", "prod_server", "werkzeug")   # prod_server は Flask の app.logger(アプリ名)
LOG_ERROR_NOTICE = "ログの書き込み・ローテーションに失敗しました(ログファイルの権限・他のプロセスによるロックを確認してください)"
_NOTICE_INTERVAL_SEC = 60.0


class _QuietErrors:
    """ログ出力の失敗(Windows でログを別のプロセスが開いていて名前を変えられない、など)で、パスを含むトレースバックを標準エラーへ出さない。
    固定の 1 行だけを、一定間隔で出す。サーバーは止めない。"""
    _last_notice = None

    def handleError(self, record):  # noqa: N802
        now = time.monotonic()
        if self._last_notice is None or now - self._last_notice >= _NOTICE_INTERVAL_SEC:
            self._last_notice = now
            try:
                print(LOG_ERROR_NOTICE, file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass


class _QuietRotatingFileHandler(_QuietErrors, logging.handlers.RotatingFileHandler):
    pass


class _QuietStreamHandler(_QuietErrors, logging.StreamHandler):
    pass


class LogHandle:
    """configure_logging の戻り値。close() で、自分が付けたハンドラーだけを外して閉じる。"""

    def __init__(self, handlers, loggers, saved):
        self._handlers, self._loggers, self._saved = handlers, loggers, saved

    def close(self):
        for lg in self._loggers:
            for h in self._handlers:
                lg.removeHandler(h)
        for lg, (level, prop) in zip(self._loggers, self._saved):
            lg.setLevel(level)
            lg.propagate = prop
        for h in self._handlers:
            try:
                h.flush()
                h.close()
            except Exception:  # noqa: BLE001
                pass


def configure_logging(file: str, max_bytes: int, backup_count: int, level: str, *, stream=None) -> LogHandle:
    """運用ログを設定する(ファイル + 標準出力)。親フォルダが無ければ作る。ファイルは追記(UTF-8)。"""
    os.makedirs(os.path.dirname(os.path.abspath(file)), exist_ok=True)
    fmt = logging.Formatter(_FORMAT)
    handlers = []
    fh = _QuietRotatingFileHandler(file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    sh = _QuietStreamHandler(stream if stream is not None else sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        h.setLevel(getattr(logging, level))
        handlers.append(h)
    loggers, saved = [], []
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        saved.append((lg.level, lg.propagate))
        lg.setLevel(getattr(logging, level))
        lg.propagate = False                      # root のハンドラー(フィルターなし)へ流さない
        for h in handlers:
            lg.addHandler(h)
        loggers.append(lg)
    return LogHandle(handlers, loggers, saved)
