"""本番推論 API のテスト共通部品(一時フォルダ・合成モデル・設定)。実データ・実モデル・外部通信なし。"""
import hashlib
import os
import socket
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import prod_config  # noqa: E402
import prod_model  # noqa: E402
import prod_server  # noqa: E402
from backends import FakeBackend  # noqa: E402

HOST = "127.0.0.1"


def make_png(path, w=200, h=100):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff" * w for _ in range(h))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


WEIGHTS_BYTES = b"synthetic-weights-for-tests" * 100
MODEL_YAML_TEXT = ("model:\n  use_model_type: yolov8\n  my_model: best.pt\n  threshold: 0.81\n  use_device: cuda:0\n"
                   "sahi:\n  slice_higth: 640\n  slice_width: 640\n  over_lap_higth: 0.2\n  over_lap_width: 0.2\n")

SCRIPT = {"pages": {"21": [{"name": "盤", "score": 0.9, "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.2}]}}


class ProdBase(unittest.TestCase):
    """一時フォルダに、出力ルート・製番フォルダ・合成モデル・公開先を用意する。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out = self.root / "share" / "out"
        self.product = self.out / "A1TEST0001"
        self.product.mkdir(parents=True)
        make_png(self.product / "21.png")
        make_png(self.product / "22.png")
        self.model_dir = self.root / "model"
        self.model_dir.mkdir()
        (self.model_dir / "best.pt").write_bytes(WEIGHTS_BYTES)
        (self.model_dir / "model.yaml").write_bytes(MODEL_YAML_TEXT.encode("utf-8"))   # 改行を変換させない(ハッシュを固定する)
        self.pub_dir = self.root / "share" / "api"
        self.pub_dir.mkdir()
        self.server_yaml = self.pub_dir / "server.yaml"
        self.backup_dir = self.root / "backup"
        self.port = free_port()

    def data(self, **over):
        d = {
            "mode": "production",
            "bind": {"host": HOST, "port": self.port},
            "allowed_clients": ["127.0.0.1/32"],
            "output_root": str(self.out),
            "publish": {"server_yaml": str(self.server_yaml), "backup_dir": str(self.backup_dir),
                        "proxy": {"host": "proxy.example", "port": 80}},
            "model": {"id": "test_model_20260101", "dir": str(self.model_dir), "weights": "best.pt", "yaml": "model.yaml",
                      "expected": {"weights_sha256": sha(WEIGHTS_BYTES), "weights_size": len(WEIGHTS_BYTES),
                                   "yaml_sha256": sha(MODEL_YAML_TEXT.encode("utf-8"))}},
            "timeout_probe_sec": 3.0,
            "server": {"threads": 6, "max_pending_predict": 3, "queue_wait_sec": 5, "backlog": 16, "connection_limit": 16,
                       "channel_timeout_sec": 10, "shutdown_grace_sec": 5},
            "logging": {"file": str(self.root / "logs" / "prod_api.log"), "max_bytes": 65536, "backup_count": 3, "level": "INFO"},
        }
        d.update(over)
        return d

    def cfg(self, **over):
        return prod_config.parse_config(self.data(**over))

    def state(self, cfg=None, ready=True, backend=None, token=None):
        cfg = cfg or self.cfg()
        ident, _ = prod_server._check_model_in_place(cfg)
        st = prod_server.ProdState(cfg, "inst" + "0" * 28, ident, token)
        if backend is None:
            backend = FakeBackend(None)
            backend.script = SCRIPT
        st.backend = backend
        st.ready = ready
        return st

    def client(self, state, addr=HOST):
        app = prod_server.create_app(state)
        c = app.test_client()
        c.environ_base["REMOTE_ADDR"] = addr
        return c

    def form(self, page="21", ext="png", work_dir=None):
        return {"work_dir": work_dir if work_dir is not None else str(self.product), "page_nmb": page, "extension": ext}
