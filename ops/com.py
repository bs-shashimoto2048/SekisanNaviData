# COM の初期化・終了(別スレッドから ADODB など COM を使う処理の前後で、同じスレッド内で呼ぶ)。
# メインスレッド以外(ops-run の worker・HTTP の要求スレッド)では、CoInitialize をしないと ADODB.Connection の生成が
# 0x800401F0 で失敗し、ホスト照会が「取得できません」として握りつぶされる。呼んだ回数だけ co_uninitialize を呼ぶこと。


def co_initialize() -> None:
    import pythoncom
    pythoncom.CoInitialize()


def co_uninitialize() -> None:
    import pythoncom
    pythoncom.CoUninitialize()
