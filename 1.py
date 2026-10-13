# ##########################################################
# (テスト)自動アノテーション補助ツール
# ##########################################################

import cv2
import numpy as np
import os

# --- 設定 ---
target_path = os.environ.get("SEKISAN_TARGET_IMAGE", r"<TARGET_IMAGE.png>")  # 検出画像(環境変数で指定)
template_folder = os.environ.get("SEKISAN_TEMPLATE_DIR", r"<TEMPLATE_DIR>")  # テンプレートフォルダ(環境変数で指定)
threshold = 0.69     # 確信度閾値（0~1）
nms_threshold = 0.8  # 重なり率 (IoU) の閾値
black_match_threshold = 0.69  # ★追加：黒線一致率閾値

debug_folder = "debug_images"
os.makedirs(debug_folder, exist_ok=True)

# --- 元画像の二値化（黒線を255、その他0） ---
target = cv2.imread(target_path, cv2.IMREAD_COLOR)
target_gray = cv2.cvtColor(target, cv2.COLOR_BGR2GRAY)
target_bin = np.zeros_like(target_gray, dtype=np.uint8)
target_bin[target_gray < 128] = 255
cv2.imwrite(os.path.join(debug_folder, "target_bin.png"), target_bin)

templates = [f for f in os.listdir(template_folder) if f.endswith(".png")]

for idx, tpl_name in enumerate(templates):
    tpl_path = os.path.join(template_folder, tpl_name)
    tpl = cv2.imread(tpl_path, cv2.IMREAD_UNCHANGED)

    # --- テンプレート二値化 ---
    tpl_gray = cv2.cvtColor(tpl[:, :, :3], cv2.COLOR_BGR2GRAY)
    tpl_bin = np.zeros_like(tpl_gray, dtype=np.uint8)
    tpl_bin[tpl_gray < 128] = 255
    if tpl.shape[2] == 4:
        tpl_bin[tpl[:, :, 3] == 0] = 0

    black_coords = np.column_stack(np.where(tpl_bin > 0))

    # --- テンプレートマッチング ---
    res = cv2.matchTemplate(target_bin, tpl_bin, cv2.TM_CCORR_NORMED)
    loc = np.where(res >= threshold)

    boxes = []
    match_scores = []
    black_scores = []
    combined_scores = []

    for pt in zip(*loc[::-1]):
        h, w = tpl_bin.shape

        image_coords = black_coords + np.array([pt[1], pt[0]])
        ys, xs = image_coords[:, 0], image_coords[:, 1]

        valid = (
            (ys >= 0) & (ys < target_bin.shape[0]) &
            (xs >= 0) & (xs < target_bin.shape[1])
        )

        ys = ys[valid]
        xs = xs[valid]

        matched = np.count_nonzero(target_bin[ys, xs])
        match_ratio = matched / len(black_coords)

        if match_ratio < black_match_threshold:
            continue

        match_score = res[pt[1], pt[0]]
        combined_score = match_score + match_ratio  # ★重複判定用

        boxes.append([pt[0], pt[1], pt[0] + w, pt[1] + h])
        match_scores.append(match_score)
        black_scores.append(match_ratio)
        combined_scores.append(combined_score)

    # --- NMS ---
    def non_max_suppression(boxes, scores, iou_threshold):
        if not boxes:
            return []
        boxes = np.array(boxes)
        scores = np.array(scores)

        x1, y1, x2, y2 = boxes.T
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = scores.argsort()[::-1]

        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)

            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])

            w = np.maximum(0, xx2 - xx1 + 1)
            h = np.maximum(0, yy2 - yy1 + 1)
            inter = w * h

            iou = inter / (areas[i] + areas[order[1:]] - inter)
            order = order[np.where(iou <= iou_threshold)[0] + 1]

        return keep

    keep_idx = non_max_suppression(boxes, combined_scores, nms_threshold)

    # --- 描画 ---
    for i in keep_idx:
        x1, y1, x2, y2 = boxes[i]
        # バウンディングボックス描写
        cv2.rectangle(target, (x1, y1), (x2, y2), (0, 0, 255), 2)
        # テキスト描写:一致画像インデックス番号,スコア(マッチング/黒線一致率)
        text = f"[{idx}] {match_scores[i]:.2f}/{black_scores[i]:.2f}"
        cv2.putText(
            target,
            text,
            (x1, y1 - 5),             # 文字の開始座標(左上基準)
            cv2.FONT_HERSHEY_SIMPLEX, # OpenCV標準フォント
            0.5,                      # FontSize
            (0, 230, 0),            # FontSize
            2                         # Tickness
        )

cv2.imwrite("result.png", target)
print("完了：result.pngに保存しました。")
