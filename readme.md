> **セットアップ**: 社内共通パッケージ `bs_pkg` はこのリポジトリに含まれません。導入手順は [docs/setup.md](docs/setup.md) を参照してください。

> **処理フロー・出力契約・改修計画**は [docs/data_pipeline_overview.md](docs/data_pipeline_overview.md) と [docs/refactor_plan.md](docs/refactor_plan.md) を参照してください(ブランチ `refactor/data-pipeline` で大規改修中)。

> **図面印刷の仕様**(印刷対象・X始まりの除外・印刷位置)は [docs/print_spec.md](docs/print_spec.md) を参照してください。


# 標準積算自動化

## Sub Title

---

### 解析用データ詳細

EX, 基礎図
解析用データ(EX・基礎図)の列と記入例を下表に示す。(説明図は実図面に由来するため、このリポジトリには含めていない。)

|BAN_MENNO|BAN_NO|PAGE|ZUMEI|BAN_MEISYOU|BAN_TYPE|`KITEN_X`|`KITEN_Y`|`DETECT_AREA_X`|`DETECT_AREA_Y`|
|:--|:--|:--|:--|:--|:--|:--|:--|:--|:--|
|1|21|6|基礎図|非常側高圧受電盤|基礎図|3000|3055|BAN_W|BAN_D|
|2|22|6|基礎図|高圧母線連絡盤No.2|基礎図|3900|3055|BAN_W|BAN_D|
|3|23|6|基礎図|GC系高圧フィーダー盤|基礎図|4800|3055|BAN_W|BAN_D|
|4|24|6|基礎図|非常保安動力配電盤|基礎図|5700|3055|BAN_W|BAN_D|
|5|25|6|基礎図|非常保安電灯配電盤|基礎図|7300|3055|BAN_W|BAN_D|

---

#### bbb

---

##### ccc

---

###### ddd

```python
import os


```

---
