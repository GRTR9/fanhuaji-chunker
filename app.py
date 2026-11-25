"""
繁化姬長檔轉換器 — CustomTkinter GUI
執行：python app.py
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
import webbrowser
from tkinter import filedialog, messagebox

import customtkinter as ctk

from zhconv_core import (CONVERTERS, Cancelled, ConvertOptions, convert_file,
                         output_path_for)

try:  # 拖放檔案（選用）：pip install tkinterdnd2
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

FANHUAJI_URL = "https://zhconvert.org"
CONFIG_PATH =os.path.join(os.path.expanduser("~"), ".zhconvert-long.json")
TEXT_EXTS = (".txt", ".json", ".jsonl", ".md", ".srt", ".ass", ".ssa", ".csv",
             ".yml", ".yaml", ".xml", ".html", ".htm", ".ini", ".lrc", ".po")
ENCODINGS = ["auto", "utf-8", "utf-8-sig", "gb18030", "big5", "utf-16"]

BaseTk = ctk.CTk
if HAS_DND:
    class BaseTk(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[no-redef]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            # tkdnd 原生元件與目前的 Tcl/Tk 不相容時（例如 Windows ARM64 版 Python），
            # 停用拖放但程式照常開啟
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_ok = True
            except Exception:  # noqa: BLE001
                self.dnd_ok = False


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.dnd_ok = getattr(self, "dnd_ok", False)
        # tkdnd 不能用時（例如 Windows ARM64 版 Python），改用 Windows 原生拖放
        self.native_drop = False
        if not self.dnd_ok and sys.platform == "win32":
            from win_drop import enable_file_drop
            self.native_drop = enable_file_drop(self, self._add_paths)
        self.title("繁化姬長檔轉換器")
        self.geometry("920x700")
        self.minsize(780, 600)
        self.files: list[str] = []
        self.msgs: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.cfg = self._load_config()

        ctk.set_appearance_mode(self.cfg.get("appearance", "system"))
        ctk.set_default_color_theme("blue")
        family = {"win32": "Microsoft JhengHei UI", "darwin": "PingFang TC"}.get(sys.platform, "Noto Sans CJK TC")
        self.font = ctk.CTkFont(family=family, size=14)
        self.font_b = ctk.CTkFont(family=family, size=20, weight="bold")

        self._build_header()
        self._build_footer()  # 先 pack 在底部，分頁才不會把它擠掉
        self.tabs = ctk.CTkTabview(self, corner_radius=12)
        self.tabs.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        self._build_main_tab(self.tabs.add("轉換"))
        self._build_adv_tab(self.tabs.add("進階設定"))

        if self.dnd_ok:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", lambda e: self._add_paths(self.tk.splitlist(e.data)))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    # ---------------------------------------------------------- 介面 ------
    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=(14, 6))
        ctk.CTkLabel(bar, text="繁化姬長檔轉換器", font=self.font_b).pack(side="left")
        ctk.CTkLabel(bar, text="  自動切段・失敗重試・中斷續傳", font=self.font,
                     text_color=("gray40", "gray65")).pack(side="left")
        self.appearance = ctk.CTkSegmentedButton(
            bar, values=["淺色", "深色", "系統"], command=self._set_appearance, font=self.font)
        self.appearance.set({"light": "淺色", "dark": "深色"}.get(self.cfg.get("appearance"), "系統"))
        self.appearance.pack(side="right")

    def _build_footer(self):
        # 依繁化姬服務條款標示使用其 API，並附上官網連結
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=20, pady=(0, 10))
        small = ctk.CTkFont(family=self.font.cget("family"), size=12)
        link_font = ctk.CTkFont(family=self.font.cget("family"), size=12, underline=True)
        ctk.CTkLabel(bar, text="轉換服務由繁化姬提供：", font=small,
                     text_color=("gray40", "gray65")).pack(side="left")
        link = ctk.CTkLabel(bar, text=FANHUAJI_URL, font=link_font,
                            text_color=("#1a5fb4", "#78aeed"), cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e: webbrowser.open(FANHUAJI_URL))
        ctk.CTkLabel(bar, text="（非官方工具，與繁化姬及其作者無關）", font=small,
                     text_color=("gray40", "gray65")).pack(side="left")

    def _build_main_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_rowconfigure(5, weight=1)

        # 檔案清單
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(4, 6))
        ctk.CTkButton(top, text="＋ 新增檔案", width=110, font=self.font, command=self._pick_files).pack(side="left")
        ctk.CTkButton(top, text="＋ 新增資料夾", width=110, font=self.font, command=self._pick_folder).pack(side="left", padx=8)
        ctk.CTkButton(top, text="清除", width=70, font=self.font, fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=self._clear_files).pack(side="left")
        if self.dnd_ok or self.native_drop:
            hint = "可直接把檔案拖進視窗"
        elif HAS_DND:
            hint = "此環境無法載入拖放元件，請用「新增檔案」"
        else:
            hint = "安裝 tkinterdnd2 即可拖放檔案"
        self.count_lbl = ctk.CTkLabel(top, text=f"尚未加入檔案（{hint}）", font=self.font,
                                      text_color=("gray40", "gray65"))
        self.count_lbl.pack(side="left", padx=12)

        self.file_box = ctk.CTkTextbox(tab, height=120, font=self.font, state="disabled", corner_radius=10)
        self.file_box.grid(row=1, column=0, sticky="nsew")

        # 設定列
        opt = ctk.CTkFrame(tab, corner_radius=10)
        opt.grid(row=2, column=0, sticky="ew", pady=10)
        opt.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(opt, text="轉換器", font=self.font).grid(row=0, column=0, padx=(14, 6), pady=10)
        self.conv_var = ctk.StringVar(value=self.cfg.get("converter_label", "台灣化"))
        ctk.CTkOptionMenu(opt, values=list(CONVERTERS), variable=self.conv_var, font=self.font,
                          dropdown_font=self.font, width=130).grid(row=0, column=1, pady=10)
        ctk.CTkLabel(opt, text="輸出資料夾", font=self.font).grid(row=0, column=2, padx=(18, 6))
        self.out_var = ctk.StringVar(value=self.cfg.get("out_dir", ""))
        ctk.CTkEntry(opt, textvariable=self.out_var, font=self.font,
                     placeholder_text="留空＝與原檔相同").grid(row=0, column=3, sticky="ew")
        ctk.CTkButton(opt, text="選擇", width=60, font=self.font, command=self._pick_out).grid(row=0, column=4, padx=6)
        ctk.CTkLabel(opt, text="檔名後綴", font=self.font).grid(row=0, column=5, padx=(10, 6))
        self.suffix_var = ctk.StringVar(value=self.cfg.get("suffix", "_tw"))
        ctk.CTkEntry(opt, textvariable=self.suffix_var, width=70, font=self.font).grid(row=0, column=6, padx=(0, 14))

        # 執行列
        run = ctk.CTkFrame(tab, fg_color="transparent")
        run.grid(row=3, column=0, sticky="ew")
        run.grid_columnconfigure(1, weight=1)
        self.start_btn = ctk.CTkButton(run, text="開始轉換", height=40, width=140, font=self.font_b,
                                       command=self._start)
        self.start_btn.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        self.file_prog = ctk.CTkProgressBar(run)
        self.file_prog.grid(row=0, column=1, sticky="ew", pady=(4, 2))
        self.all_prog = ctk.CTkProgressBar(run, progress_color=("#2fa572", "#2fa572"))
        self.all_prog.grid(row=1, column=1, sticky="ew", pady=(2, 4))
        self.file_prog.set(0)
        self.all_prog.set(0)
        self.prog_lbl = ctk.CTkLabel(run, text="待命", font=self.font, width=150, anchor="w")
        self.prog_lbl.grid(row=0, column=2, rowspan=2, padx=(12, 0))

        ctk.CTkLabel(tab, text="紀錄", font=self.font, anchor="w").grid(row=4, column=0, sticky="w", pady=(10, 2))
        self.log_box = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Consolas", size=13), corner_radius=10)
        self.log_box.grid(row=5, column=0, sticky="nsew")

    def _build_adv_tab(self, tab):
        tab.grid_columnconfigure((1, 3), weight=1)
        c = self.cfg

        def row_entry(r, col, label, key, default, width=120):
            ctk.CTkLabel(tab, text=label, font=self.font).grid(row=r, column=col, sticky="w", padx=(10, 6), pady=6)
            v = ctk.StringVar(value=str(c.get(key, default)))
            ctk.CTkEntry(tab, textvariable=v, width=width, font=self.font).grid(row=r, column=col + 1, sticky="w", pady=6)
            return v

        self.chunk_var = row_entry(0, 0, "每段字元數", "chunk_size", 20000)
        self.delay_var = row_entry(0, 2, "請求間隔（秒）", "delay", 0.5)
        self.retry_var = row_entry(1, 0, "失敗重試次數", "retries", 4)
        self.key_var = row_entry(1, 2, "API Key（選填）", "api_key", "", width=220)

        ctk.CTkLabel(tab, text="讀取編碼", font=self.font).grid(row=2, column=0, sticky="w", padx=(10, 6), pady=6)
        self.in_enc = ctk.CTkOptionMenu(tab, values=ENCODINGS, font=self.font, width=120)
        self.in_enc.set(c.get("in_encoding", "auto"))
        self.in_enc.grid(row=2, column=1, sticky="w")
        ctk.CTkLabel(tab, text="輸出編碼", font=self.font).grid(row=2, column=2, sticky="w", padx=(10, 6))
        self.out_enc = ctk.CTkOptionMenu(tab, values=["utf-8", "utf-8-sig", "big5", "gb18030", "utf-16"],
                                         font=self.font, width=120)
        self.out_enc.set(c.get("out_encoding", "utf-8"))
        self.out_enc.grid(row=2, column=3, sticky="w")

        self.json_keys = ctk.CTkCheckBox(tab, text="JSON：連「鍵名」一起轉換（預設只轉字串值）", font=self.font)
        if c.get("json_keys"):
            self.json_keys.select()
        self.json_keys.grid(row=3, column=0, columnspan=4, sticky="w", padx=10, pady=6)

        ctk.CTkLabel(tab, text='模組設定（JSON，例：{"Naruto": 1, "GanToZuo": 0}；-1 自動 / 0 停用 / 1 啟用）',
                     font=self.font).grid(row=4, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 2))
        self.modules_var = ctk.StringVar(value=c.get("modules", ""))
        ctk.CTkEntry(tab, textvariable=self.modules_var, font=self.font).grid(
            row=5, column=0, columnspan=4, sticky="ew", padx=10)

        box = ctk.CTkFrame(tab, fg_color="transparent")
        box.grid(row=6, column=0, columnspan=4, sticky="nsew", padx=6, pady=10)
        tab.grid_rowconfigure(6, weight=1)
        box.grid_columnconfigure((0, 1, 2), weight=1)
        box.grid_rowconfigure(1, weight=1)
        self.pre_box = self._labeled_box(box, 0, "轉換前取代（搜尋=取代，一行一組）", c.get("pre", ""))
        self.post_box = self._labeled_box(box, 1, "轉換後取代（搜尋=取代，一行一組）", c.get("post", ""))
        self.protect_box = self._labeled_box(box, 2, "保護詞（一行一個，不被轉換）", c.get("protect", ""))

    def _labeled_box(self, parent, col, label, value):
        ctk.CTkLabel(parent, text=label, font=self.font, anchor="w").grid(row=0, column=col, sticky="w", padx=4)
        tb = ctk.CTkTextbox(parent, font=self.font, corner_radius=10)
        tb.grid(row=1, column=col, sticky="nsew", padx=4)
        tb.insert("1.0", value)
        return tb

    # ---------------------------------------------------------- 檔案 ------
    def _add_paths(self, paths):
        for p in paths:
            if os.path.isdir(p):
                for root, _, names in os.walk(p):
                    for n in sorted(names):
                        if n.lower().endswith(TEXT_EXTS):
                            self._add_one(os.path.join(root, n))
            elif os.path.isfile(p):
                self._add_one(p)
        self._refresh_files()

    def _add_one(self, p):
        p = os.path.abspath(p)
        if p not in self.files:
            self.files.append(p)

    def _pick_files(self):
        self._add_paths(filedialog.askopenfilenames(
            filetypes=[("文字檔", " ".join("*" + e for e in TEXT_EXTS)), ("所有檔案", "*.*")]))

    def _pick_folder(self):
        d = filedialog.askdirectory()
        if d:
            self._add_paths([d])

    def _pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    def _clear_files(self):
        self.files.clear()
        self._refresh_files()

    def _refresh_files(self):
        self.file_box.configure(state="normal")
        self.file_box.delete("1.0", "end")
        for p in self.files:
            size = os.path.getsize(p)
            self.file_box.insert("end", f"{os.path.basename(p)}    ({size / 1024:,.0f} KB)    {os.path.dirname(p)}\n")
        self.file_box.configure(state="disabled")
        self.count_lbl.configure(text=f"共 {len(self.files)} 個檔案" if self.files else "尚未加入檔案")

    # ---------------------------------------------------------- 執行 ------
    def _collect_options(self) -> ConvertOptions:
        mods_txt = self.modules_var.get().strip()
        try:
            modules = json.loads(mods_txt) if mods_txt else {}
            if not isinstance(modules, dict):
                raise ValueError
        except ValueError:
            raise ValueError("模組設定必須是 JSON 物件，例如 {\"Naruto\": 1}")
        chunk = int(self.chunk_var.get())
        if chunk < 500:
            raise ValueError("每段字元數至少 500")
        return ConvertOptions(
            converter=CONVERTERS[self.conv_var.get()],
            chunk_size=chunk,
            delay=float(self.delay_var.get()),
            retries=int(self.retry_var.get()),
            api_key=self.key_var.get().strip(),
            modules=modules,
            user_pre_replace=self.pre_box.get("1.0", "end-1c"),
            user_post_replace=self.post_box.get("1.0", "end-1c"),
            user_protect_replace=self.protect_box.get("1.0", "end-1c"),
            json_convert_keys=bool(self.json_keys.get()),
            output_encoding=self.out_enc.get(),
        )

    def _start(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.start_btn.configure(text="停止中…", state="disabled")
            return
        if not self.files:
            messagebox.showinfo("提示", "請先加入要轉換的檔案")
            return
        try:
            opts = self._collect_options()
        except ValueError as e:
            messagebox.showerror("設定錯誤", str(e))
            return
        self._save_config()
        self.stop_event.clear()
        self.start_btn.configure(text="停止", fg_color="#c0392b", hover_color="#962d22")
        files, out_dir, suffix, enc = list(self.files), self.out_var.get().strip() or None, \
            self.suffix_var.get(), self.in_enc.get()
        self.worker = threading.Thread(target=self._run, args=(files, out_dir, suffix, enc, opts), daemon=True)
        self.worker.start()

    def _run(self, files, out_dir, suffix, enc, opts):
        put = self.msgs.put
        ok = fail = 0
        for idx, src in enumerate(files):
            if self.stop_event.is_set():
                break
            dst = output_path_for(src, out_dir, suffix)
            if os.path.abspath(dst) == os.path.abspath(src):
                put(("log", f"✖ 輸出路徑與原檔相同，跳過：{src}（請設定後綴或輸出資料夾）"))
                fail += 1
                continue
            put(("all", idx, len(files)))
            try:
                convert_file(src, dst, opts, encoding=enc,
                             progress=lambda a, b: put(("file", a, b)),
                             should_stop=self.stop_event.is_set,
                             log=lambda m: put(("log", m)))
                ok += 1
            except Cancelled:
                put(("log", "■ 已停止；已完成的段落存在 .zhcache，下次用相同設定重跑會自動續傳"))
                break
            except Exception as e:  # noqa: BLE001
                fail += 1
                put(("log", f"✖ {os.path.basename(src)} 失敗：{e}"))
                put(("log", traceback.format_exc(limit=2)))
        put(("all", len(files) if not self.stop_event.is_set() else idx, len(files)))
        put(("done", ok, fail))

    def _poll(self):
        try:
            while True:
                m = self.msgs.get_nowait()
                kind = m[0]
                if kind == "log":
                    self.log_box.insert("end", m[1] + "\n")
                    self.log_box.see("end")
                elif kind == "file":
                    self.file_prog.set(m[1] / max(m[2], 1))
                    self.prog_lbl.configure(text=f"本檔 {m[1]}/{m[2]} 段")
                elif kind == "all":
                    self.all_prog.set(m[1] / max(m[2], 1))
                    self.file_prog.set(0)
                elif kind == "done":
                    self.prog_lbl.configure(text=f"完成 {m[1]}・失敗 {m[2]}")
                    self.start_btn.configure(text="開始轉換", state="normal",
                                             fg_color=("#3B8ED0", "#1F6AA5"), hover_color=("#36719F", "#144870"))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---------------------------------------------------------- 設定 ------
    def _set_appearance(self, v):
        mode = {"淺色": "light", "深色": "dark"}.get(v, "system")
        ctk.set_appearance_mode(mode)
        self.cfg["appearance"] = mode

    def _load_config(self) -> dict:
        try:
            return json.load(open(CONFIG_PATH, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        self.cfg.update(
            converter_label=self.conv_var.get(), out_dir=self.out_var.get(), suffix=self.suffix_var.get(),
            chunk_size=self.chunk_var.get(), delay=self.delay_var.get(), retries=self.retry_var.get(),
            api_key=self.key_var.get(), in_encoding=self.in_enc.get(), out_encoding=self.out_enc.get(),
            json_keys=bool(self.json_keys.get()), modules=self.modules_var.get(),
            pre=self.pre_box.get("1.0", "end-1c"), post=self.post_box.get("1.0", "end-1c"),
            protect=self.protect_box.get("1.0", "end-1c"))
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _on_close(self):
        self._save_config()
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
