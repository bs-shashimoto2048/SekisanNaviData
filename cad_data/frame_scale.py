# 図枠の検出(画像)・図枠寸法の取得(DXF)・縮尺の書き戻し(product_df)。
# 分割前の cad_data_process.py から関数本体を変更せずに移した(S5)。外部からは `import cad_data_process as cad_mod` で使う。
import os

import cv2          # opencv-python
import ezdxf        # DXF 読込
import numpy as np  # numpy
import pandas as pd

# 図枠サイズ検出&クロップ処理
def specified_frame_detection(img_path):
    """
    画像内の最大矩形を検出し、矩形内を切り抜いて保存する。
    Args:
        img_path (str): 解析対象画像パス
        save_cropped_path (str): 切り抜き保存先
    Returns:
        dict: 矩形情報
            - image_size: (幅, 高さ)
            - frame_size: (幅, 高さ)
            - top_left: (x, y)
            - margin: (left, top, bottom)
            - confidence_lb, confidence_rt: 常に1.0
    """
    if not os.path.exists(img_path):
        raise FileNotFoundError(f"画像が存在しません: {img_path}")

    img = cv2.imread(img_path)
    if img is None:
        raise ValueError(f"画像読み込みに失敗しました: {img_path}")

    img_h, img_w = img.shape[:2]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (3,3)), iterations=1)

    lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=50, minLineLength=3, maxLineGap=5)
    if lines is None:
        raise ValueError("直線が検出されませんでした")

    h_lines, v_lines = [], []

    for x1, y1, x2, y2 in lines[:,0]:
        length = np.hypot(x2-x1, y2-y1)
        if abs(y2 - y1) <= 1 and length >= img_w/2:
            h_lines.append((min(x1,x2), y1, max(x1,x2)))
        elif abs(x2 - x1) <= 1 and length >= img_h/2:
            v_lines.append((x1, min(y1,y2), max(y1,y2)))

    if len(h_lines) < 2 or len(v_lines) < 2:
        raise ValueError("矩形を作れる線が不足しています")

    max_area = 0
    best_rect = None
    for i in range(len(h_lines)):
        for j in range(i+1, len(h_lines)):
            y1, y2 = h_lines[i][1], h_lines[j][1]
            for m in range(len(v_lines)):
                for n in range(m+1, len(v_lines)):
                    x1, x2 = v_lines[m][0], v_lines[n][0]
                    area = abs((x2-x1)*(y2-y1))
                    if area > max_area:
                        max_area = area
                        best_rect = (x1, y1, x2, y2)

    if best_rect is None:
        raise ValueError("矩形が見つかりませんでした")

    xmin, ymin, xmax, ymax = map(int, best_rect)

    # 切抜座標を補正（線の半分だけ内側にずらす）
    line_thickness = 4  # 考慮する図枠自太さ
    add_coordinate = int(line_thickness / 2)
    x_start, x_end = sorted([xmin + line_thickness//2, xmax - line_thickness//2])
    y_start, y_end = sorted([ymin + line_thickness//2, ymax - line_thickness//2])

    # 画像内に収める
    x_start = max(0, x_start + add_coordinate)
    y_start = max(0, y_start + line_thickness)
    x_end   = min(img_w, x_end - line_thickness)
    y_end   = min(img_h, y_end - line_thickness)

    cropped_img = img[y_start:y_end, x_start:x_end]
    # クロップした画像で上書き
    cv2.imwrite(img_path, cropped_img)

    rectangle_data = f"元画像サイズ: ({img_w}, {img_h})\n"
    rectangle_data = rectangle_data + f"クロップサイズ: ({abs(x_start - x_end)}, {abs(y_start - y_end)})\n"

    rectangle_data = {
                "image_size":(img_w,img_h),
                "frame_size":(abs(x_start - x_end),abs(y_start - y_end)),
                "margin":(x_start,y_start)
                }

    print(f"切り抜き画像を保存しました: {img_path}")
    return rectangle_data

# DXFより図枠サイズ取得
def get_frame_size(dxf_file: str) -> tuple[int, int]:
    """
    AUX レイヤ、色赤または BYLAYER の LWPOLYLINE と LINE の最大X,Yを取得し
    幅=最大X、高さ=最大Y として返す
    """
    doc = ezdxf.readfile(dxf_file)
    msp = doc.modelspace()
    all_x = []
    all_y = []

    for e in msp:
        layer_name = e.dxf.layer.strip()
        color_value = e.dxf.color
        linetype_name = e.dxf.linetype.strip()

        if layer_name.upper() == "AUX" and color_value in (0, 1) and linetype_name.upper() == "BYLAYER":

            # LWPOLYLINE の場合
            if e.dxftype() == "LWPOLYLINE":
                for point in e.get_points():
                    x, y = point[:2]
                    all_x.append(float(x))
                    all_y.append(float(y))

            # LINE の場合
            elif e.dxftype() == "LINE":
                start = e.dxf.start
                end = e.dxf.end
                all_x.extend([float(start[0]), float(end[0])])
                all_y.extend([float(start[1]), float(end[1])])

    if not all_x or not all_y:
        print("DEBUG: 対象オブジェクトが見つかりません。")
        return 0, 0

    max_x = max(all_x)
    max_y = max(all_y)

    width = int(max_x)
    height = int(max_y)

    print(f"{max_x = },{max_y = }")
    print(f"{width = },{height = }")

    print(f"DEBUG: min_x={min(all_x)}, max_x={max_x}, min_y={min(all_y)}, max_y={max_y}")
    print(f"DEBUG: Frame Size(width,height) = {width}, {height}")

    return width, height

# 盤情報更新
def update_frame_info(product_df: pd.DataFrame, page, origin_frame_width,
                      origin_frame_height, image_frame_width, image_frame_height,
                      scale_x, scale_y) -> pd.DataFrame:
    """
    指定したページの行に図枠情報と縮尺を更新する (in-place)。

    Parameters
    ----------
    product_df : pd.DataFrame, 盤情報のDataFrame
    page : int or str,更新対象のPAGE番号
    origin_frame_width : int or float,オリジナル図面図枠サイズ X
    origin_frame_height : int or float,オリジナル図面図枠サイズ Y
    image_frame_width : int or float,解析用縮小図面図枠サイズ X
    image_frame_height : int or float,解析用縮小図面図枠サイズ Y
    scale_x : float,図枠縮尺 X
    scale_y : float,図枠縮尺 Y

    Returns
    -------
    pd.DataFrame
        更新済みのDataFrame
    """

    # PAGE が同じ行全てに以下の座標情報を入れる
    mask = product_df["PAGE"] == page
    product_df.loc[mask, "FRAME_ORG_X"] = origin_frame_width
    product_df.loc[mask, "FRAME_ORG_Y"] = origin_frame_height
    product_df.loc[mask, "FRAME_MINI_X"] = image_frame_width
    product_df.loc[mask, "FRAME_MINI_Y"] = image_frame_height
    product_df.loc[mask, "SCALE_X"] = float(scale_x)
    product_df.loc[mask, "SCALE_Y"] = float(scale_y)

    return product_df
