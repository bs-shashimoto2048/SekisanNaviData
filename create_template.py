import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image
from bs_pkg import design_mod, image_mod
import cad_data_process as cad_mod


def build_target_pages_by_missing_output(pages: list[str], output_dir: str, extension: str) -> tuple[list[str], list[str]]:
    pending_pages = []
    completed_pages = []
    for page in pages:
        output_path = os.path.join(output_dir, f"{page}{extension}")
        if os.path.exists(output_path):
            completed_pages.append(page)
        else:
            pending_pages.append(page)
    return pending_pages, completed_pages


class EstimatePipelineGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("テンプレ元画像作成GUI")
        self.root.geometry("980x680")

        self.log_queue: queue.Queue[str] = queue.Queue()

        self.dir_design_product = tk.StringVar(value=os.environ.get("SEKISAN_DESIGN_PRODUCT_DIR", ""))
        self.dir_product = tk.StringVar(value=os.environ.get("SEKISAN_OUTPUT_DIR", ""))
        self.pages_text = tk.StringVar(value="04,15")
        self.white_threshold = tk.IntVar(value=250)

        self.step_print_pdf = tk.BooleanVar(value=True)
        self.step_pdf_to_jpeg = tk.BooleanVar(value=True)
        self.step_jpeg_to_png = tk.BooleanVar(value=True)
        self.step_frame_detect = tk.BooleanVar(value=True)
        self.step_png_binary = tk.BooleanVar(value=True)

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        row1 = ttk.Frame(frame)
        row1.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row1, text="図面ディレクトリ:").pack(side=tk.LEFT)
        design_dir_entry = ttk.Entry(row1, textvariable=self.dir_design_product)
        design_dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        design_dir_entry.bind("<FocusOut>", self._on_design_dir_changed)
        design_dir_entry.bind("<Return>", self._on_design_dir_changed)
        ttk.Button(row1, text="参照", command=self._browse_design_dir).pack(side=tk.LEFT)

        row2 = ttk.Frame(frame)
        row2.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row2, text="出力ディレクトリ:").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.dir_product).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(row2, text="参照", command=self._browse_output_dir).pack(side=tk.LEFT)

        row3 = ttk.Frame(frame)
        row3.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row3, text="対象ページ(カンマ区切り):").pack(side=tk.LEFT)
        ttk.Entry(row3, textvariable=self.pages_text, width=50).pack(side=tk.LEFT, padx=8)
        ttk.Label(row3, text="白判定閾値:").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Entry(row3, textvariable=self.white_threshold, width=8).pack(side=tk.LEFT)

        step_box = ttk.LabelFrame(frame, text="実行ステップ", padding=8)
        step_box.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(step_box, text="DWG -> PDF", variable=self.step_print_pdf).pack(anchor=tk.W)
        ttk.Checkbutton(step_box, text="PDF -> JPEG", variable=self.step_pdf_to_jpeg).pack(anchor=tk.W)
        ttk.Checkbutton(step_box, text="JPEG -> PNG", variable=self.step_jpeg_to_png).pack(anchor=tk.W)
        ttk.Checkbutton(step_box, text="枠検出(cad_data_process.specified_frame_detection)", variable=self.step_frame_detect).pack(anchor=tk.W)
        ttk.Checkbutton(step_box, text="PNG白背景を透過(binaryフォルダ出力)", variable=self.step_png_binary).pack(anchor=tk.W)

        action_row = ttk.Frame(frame)
        action_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(action_row, text="実行", command=self._start_run).pack(side=tk.LEFT)
        ttk.Button(action_row, text="ログクリア", command=self._clear_log).pack(side=tk.LEFT, padx=8)

        self.log_text = tk.Text(frame, height=24)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _browse_output_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.dir_product.set(path)

    def _browse_design_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.dir_design_product.get().strip() or None)
        if not path:
            return

        self.dir_design_product.set(path)
        self._autofill_pages_from_design_dir()

    def _on_design_dir_changed(self, _event: tk.Event) -> None:
        self._autofill_pages_from_design_dir(log_when_empty=False)

    def _autofill_pages_from_design_dir(self, log_when_empty: bool = True) -> None:
        directory = self.dir_design_product.get().strip()
        if not directory or not os.path.isdir(directory):
            return

        pages = self._collect_target_names(directory)
        if pages:
            self.pages_text.set(",".join(pages))
            self._log(f"対象ページを自動設定: {len(pages)}件")
        elif log_when_empty:
            self._log("対象ページを自動設定できませんでした: 対象ファイルが見つかりません")

    @staticmethod
    def _collect_target_names(directory: str) -> list[str]:
        target_exts = {".dwg", ".pdf", ".png", ".jpeg", ".jpg"}
        try:
            names = {
                item.stem
                for item in Path(directory).iterdir()
                if item.is_file() and item.suffix.lower() in target_exts
            }
        except OSError:
            return []
        return sorted(names)

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _log(self, message: str) -> None:
        self.log_queue.put(message)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    @staticmethod
    def _parse_pages(raw: str) -> list[str]:
        pages = [p.strip() for p in raw.replace("\n", ",").split(",")]
        return [p for p in pages if p]

    def _start_run(self) -> None:
        pages = self._parse_pages(self.pages_text.get())
        if not pages:
            messagebox.showerror("エラー", "対象ページを入力してください。")
            return

        out_dir = self.dir_product.get().strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showerror("エラー", "有効な出力ディレクトリを指定してください。")
            return

        threading.Thread(target=self._run_pipeline, daemon=True).start()

    def _run_pipeline(self) -> None:
        try:
            pages = self._parse_pages(self.pages_text.get())
            design_dir = self.dir_design_product.get().strip()
            out_dir = self.dir_product.get().strip()
            white_threshold = int(self.white_threshold.get())

            if self.step_print_pdf.get():
                self._log("[1/5] DWG -> PDF")
                pending, completed = build_target_pages_by_missing_output(pages, out_dir, ".pdf")
                self._log(f"  既存PDF: {completed}")
                self._log(f"  実行対象: {pending}")
                for chunk in cad_mod.chunk_list(pending):
                    script = design_mod.output_print_script(chunk, design_dir, out_dir)
                    design_mod.print_dwg_for_pdf(script, len(chunk))
                    self._log(f"  印刷実行: {chunk}")
                    cad_mod.countdown(len(chunk) * 4)

            if self.step_pdf_to_jpeg.get():
                self._log("[2/5] PDF -> JPEG")
                pending, completed = build_target_pages_by_missing_output(pages, out_dir, ".jpeg")
                self._log(f"  既存JPEG: {completed}")
                self._log(f"  実行対象: {pending}")
                if pending:
                    image_mod.pdf_to_images(out_dir, pending)

            if self.step_jpeg_to_png.get():
                self._log("[3/5] JPEG -> PNG")
                pending, completed = build_target_pages_by_missing_output(pages, out_dir, ".png")
                self._log(f"  既存PNG: {completed}")
                self._log(f"  実行対象: {pending}")
                if pending:
                    image_mod.process_images_from_jpeg(out_dir, pending)

            if self.step_frame_detect.get():
                self._log("[4/5] 枠検出")
                for page in pages:
                    png_path = os.path.join(out_dir, f"{page}.png")
                    if os.path.exists(png_path):
                        try:
                            cad_mod.specified_frame_detection(png_path)
                            self._log(f"  枠検出: {page}.png")
                        except Exception as page_exc:
                            self._log(f"  枠検出エラー: {page}.png -> {page_exc}")
                            self._log("  次のページへ継続します。")
                    else:
                        self._log(f"  スキップ(未存在): {page}.png")

            if self.step_png_binary.get():
                self._log("[5/5] PNG白背景透過")
                output_dir = os.path.join(out_dir, "binary")
                os.makedirs(output_dir, exist_ok=True)

                for filename in os.listdir(out_dir):
                    if not filename.lower().endswith(".png"):
                        continue

                    input_path = os.path.join(out_dir, filename)
                    output_path = os.path.join(output_dir, filename)
                    img = Image.open(input_path).convert("RGBA")
                    pixels = img.getdata()

                    new_pixels = []
                    for r, g, b, a in pixels:
                        if r >= white_threshold and g >= white_threshold and b >= white_threshold:
                            new_pixels.append((255, 255, 255, 0))
                        else:
                            new_pixels.append((r, g, b, 255))

                    img.putdata(new_pixels)
                    img.save(output_path, "PNG")
                    self._log(f"  出力: {filename}")

            self._log("完了: すべての処理が終了しました。")
        except Exception as exc:
            self._log(f"エラー: {exc}")
            self._log("詳細は実行環境・依存モジュールを確認してください。")


if __name__ == "__main__":
    root = tk.Tk()
    app = EstimatePipelineGui(root)
    root.mainloop()
