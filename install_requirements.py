# ##########################################################
# pip install でエラーをパスするためのスクリプト
# ##########################################################
import subprocess
import chardet

file_path = "requirements.txt"

# エンコーディングを自動検出
with open(file_path, "rb") as f:
    raw_data = f.read()
encoding = chardet.detect(raw_data)["encoding"] or "utf-8"

print(f"Detected encoding: {encoding}")

# 正しいエンコーディングで再読み込み
with open(file_path, "r", encoding=encoding) as f:
    packages = [line.strip() for line in f if line.strip() and not line.startswith("#")]

# 各パッケージを順にインストール
for pkg in packages:
    print(f"Installing {pkg} ...")
    result = subprocess.run(
        ["pip", "install", pkg],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if result.returncode == 0:
        print(f"Successfully: Installed: {pkg}")
    else:
        print(f"Failed: Skipped: {pkg}\n{result.stderr.splitlines()[-1]}")
