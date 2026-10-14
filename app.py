"""
Загрузчик видео — десктоп-приложение на CustomTkinter + yt-dlp.

Запуск:
    pip install -r requirements.txt
    python app.py

Для лучшего качества и MP3 нужен ffmpeg в PATH.
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import threading

# В exe без консоли (--windowed) stdout/stderr равны None, и любая библиотека,
# которая попытается что-то напечатать, упадёт. Подставляем «пустые» потоки.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from tkinter import filedialog, messagebox

import customtkinter as ctk
import yt_dlp

# ---------- Настройки ----------

ACCENT = "#1F7A8C"
ACCENT_HOVER = "#165B69"
DANGER = "#B5442E"
DANGER_HOVER = "#8E3524"

def compatible(height=None):
    """Видео H.264 + звук AAC (m4a): играет в любом плеере на любом компьютере.
    Если такого варианта нет — берём что есть, но звук всё равно стараемся AAC."""
    h = f"[height<={height}]" if height else ""
    return (f"bestvideo*{h}[vcodec^=avc1]+bestaudio[ext=m4a]"
            f"/bestvideo*{h}+bestaudio[ext=m4a]"
            f"/bestvideo*{h}+bestaudio"
            f"/best{h}")


# Форматы с ffmpeg (склейка видео и аудио)
FORMATS_FFMPEG = {
    "До 1080p (играет везде)": compatible(1080),
    "До 720p (играет везде)": compatible(720),
    "До 480p (играет везде)": compatible(480),
    "Максимальное, до 4K (нужен VLC)": "bestvideo*+bestaudio[ext=m4a]/bestvideo*+bestaudio/best",
    "Только звук (MP3)": "bestaudio/best",
}

# Форматы без ffmpeg (только готовые файлы со звуком, качество ниже)
FORMATS_NO_FFMPEG = {
    "Лучшее качество": "best[ext=mp4]/best",
    "До 1080p": "best[height<=1080][ext=mp4]/best[height<=1080]",
    "До 720p": "best[height<=720][ext=mp4]/best[height<=720]",
    "До 480p": "best[height<=480][ext=mp4]/best[height<=480]",
    "Только звук (M4A)": "bestaudio[ext=m4a]/bestaudio",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Cancelled(Exception):
    pass


def add_bundled_tools_to_path():
    """Добавляет в PATH папку внутри exe и папку рядом с exe,
    чтобы yt-dlp нашёл встроенные deno.exe и ffmpeg.exe."""
    folders = []
    if getattr(sys, "frozen", False):
        folders.append(getattr(sys, "_MEIPASS", ""))
        folders.append(os.path.dirname(sys.executable))
    folders.append(os.path.dirname(os.path.abspath(__file__)))
    extra = os.pathsep.join(f for f in folders if f and os.path.isdir(f))
    os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")


add_bundled_tools_to_path()


def find_ffmpeg():
    """Ищет ffmpeg: внутри exe (PyInstaller), рядом с exe/скриптом, затем в PATH."""
    name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(getattr(sys, "_MEIPASS", ""))
        candidates.append(os.path.dirname(sys.executable))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for folder in candidates:
        path = os.path.join(folder, name)
        if folder and os.path.isfile(path):
            return path
    return shutil.which("ffmpeg")


def human_size(n):
    if not n:
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_time(sec):
    if sec is None:
        return "—"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def clean_error(text):
    text = ANSI_RE.sub("", str(text))
    return text.replace("ERROR: ", "").strip()


class QueueLogger:
    """Передаёт предупреждения и ошибки yt-dlp в журнал приложения."""

    def __init__(self, q):
        self.q = q

    def debug(self, msg):
        if msg.startswith("[Merger]") or msg.startswith("[ExtractAudio]"):
            self.q.put(("log", "Обработка: склейка/конвертация файла"))

    def info(self, msg):
        pass

    def warning(self, msg):
        self.q.put(("log", "Предупреждение: " + clean_error(msg)))

    def error(self, msg):
        self.q.put(("log", "Ошибка: " + clean_error(msg)))


# ---------- Приложение ----------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")

        self.title("Загрузчик видео")
        self.geometry("680x600")
        self.minsize(560, 520)

        self.q = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()
        self.last_title = None

        self.ffmpeg_path = find_ffmpeg()
        self.has_ffmpeg = self.ffmpeg_path is not None
        self.formats = FORMATS_FFMPEG if self.has_ffmpeg else FORMATS_NO_FFMPEG

        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.out_dir = default_dir if os.path.isdir(default_dir) else os.path.expanduser("~")

        self._build_ui()
        self.after(100, self._poll_queue)

        if not self.has_ffmpeg:
            self._log("ffmpeg не найден: доступны только готовые форматы (обычно до 720p), "
                      "звук сохраняется в M4A. Установите ffmpeg, чтобы получить лучшее качество и MP3.")

    # ----- Интерфейс -----

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)
        pad = {"padx": 20}

        ctk.CTkLabel(self, text="Загрузчик видео",
                     font=ctk.CTkFont(size=24, weight="bold")).grid(
            row=0, column=0, sticky="w", pady=(20, 2), **pad)
        ctk.CTkLabel(self, text="Вставьте ссылку на видео или плейлист",
                     text_color=("gray40", "gray65")).grid(
            row=1, column=0, sticky="w", pady=(0, 12), **pad)

        # Ссылка
        url_row = ctk.CTkFrame(self, fg_color="transparent")
        url_row.grid(row=2, column=0, sticky="ew", **pad)
        url_row.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(url_row, height=40,
                                      placeholder_text="https://www.youtube.com/watch?v=…")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.url_entry.bind("<Return>", lambda e: self.start_download())
        ctk.CTkButton(url_row, text="Вставить", width=100, height=40,
                      fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"),
                      command=self._paste).grid(row=0, column=1)

        # Параметры
        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=3, column=0, sticky="ew", pady=12, **pad)
        opts.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(opts, text="Качество").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.quality = ctk.CTkOptionMenu(opts, values=list(self.formats.keys()),
                                         fg_color=ACCENT, button_color=ACCENT_HOVER,
                                         button_hover_color=ACCENT_HOVER)
        self.quality.grid(row=0, column=1, sticky="w")

        self.playlist_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Скачать весь плейлист", variable=self.playlist_var,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER).grid(
            row=0, column=2, sticky="e")

        ctk.CTkLabel(opts, text="Папка").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        self.dir_label = ctk.CTkLabel(opts, text=self._short_path(self.out_dir), anchor="w",
                                      text_color=("gray30", "gray75"))
        self.dir_label.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        ctk.CTkButton(opts, text="Изменить", width=100, fg_color="transparent",
                      border_width=1, text_color=("gray10", "gray90"),
                      command=self._choose_dir).grid(row=1, column=2, sticky="e", pady=(10, 0))

        # Прогресс и кнопки
        prog = ctk.CTkFrame(self)
        prog.grid(row=4, column=0, sticky="ew", pady=(0, 12), **pad)
        prog.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(prog, text="Ничего не загружается", anchor="w",
                                        font=ctk.CTkFont(weight="bold"))
        self.title_label.grid(row=0, column=0, columnspan=3, sticky="ew", padx=14, pady=(12, 4))

        self.progress = ctk.CTkProgressBar(prog, progress_color=ACCENT, height=10)
        self.progress.set(0)
        self.progress.grid(row=1, column=0, columnspan=3, sticky="ew", padx=14)

        self.status_label = ctk.CTkLabel(prog, text="", anchor="w",
                                         text_color=("gray35", "gray70"))
        self.status_label.grid(row=2, column=0, sticky="ew", padx=14, pady=(4, 12))

        self.cancel_btn = ctk.CTkButton(prog, text="Отменить", width=100, state="disabled",
                                        fg_color=DANGER, hover_color=DANGER_HOVER,
                                        command=self.cancel_download)
        self.cancel_btn.grid(row=2, column=1, padx=(0, 8), pady=(4, 12))

        self.download_btn = ctk.CTkButton(prog, text="Скачать", width=120,
                                          fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                          command=self.start_download)
        self.download_btn.grid(row=2, column=2, padx=(0, 14), pady=(4, 12))

        # Журнал
        log_frame = ctk.CTkFrame(self, fg_color="transparent")
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(0, 12), **pad)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(log_frame, text="Журнал").grid(row=0, column=0, sticky="w")
        self.log_box = ctk.CTkTextbox(log_frame, state="disabled", wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

        ctk.CTkButton(self, text="Открыть папку", fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"),
                      command=self._open_folder).grid(row=6, column=0, sticky="e",
                                                      pady=(0, 20), **pad)

    # ----- Вспомогательное -----

    @staticmethod
    def _short_path(path, limit=50):
        return path if len(path) <= limit else "…" + path[-(limit - 1):]

    def _log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _paste(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, text)

    def _choose_dir(self):
        path = filedialog.askdirectory(initialdir=self.out_dir)
        if path:
            self.out_dir = path
            self.dir_label.configure(text=self._short_path(path))

    def _open_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(self.out_dir)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.out_dir])
            else:
                subprocess.Popen(["xdg-open", self.out_dir])
        except Exception as e:
            messagebox.showerror("Не удалось открыть папку", str(e))

    def _set_busy(self, busy):
        self.download_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        self.url_entry.configure(state="disabled" if busy else "normal")

    # ----- Загрузка -----

    def start_download(self):
        if self.worker and self.worker.is_alive():
            return
        url = self.url_entry.get().strip()
        if not re.match(r"^https?://", url):
            messagebox.showwarning("Нужна ссылка", "Вставьте ссылку, которая начинается с http:// или https://")
            return

        self.cancel_flag.clear()
        self.last_title = None
        self.progress.set(0)
        self.title_label.configure(text="Получаю информацию о видео…")
        self.status_label.configure(text="")
        self._set_busy(True)
        self._log(f"Старт: {url}")

        self.worker = threading.Thread(
            target=self._run,
            args=(url, self.quality.get(), self.playlist_var.get(), self.out_dir),
            daemon=True,
        )
        self.worker.start()

    def cancel_download(self):
        self.cancel_flag.set()
        self.status_label.configure(text="Отменяю…")

    def _build_options(self, quality, playlist, out_dir):
        if playlist:
            template = os.path.join(out_dir, "%(playlist_title)s",
                                    "%(playlist_index)03d - %(title)s.%(ext)s")
        else:
            template = os.path.join(out_dir, "%(title)s.%(ext)s")

        opts = {
            "format": self.formats[quality],
            "outtmpl": template,
            "noplaylist": not playlist,
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._pp_hook],
            "logger": QueueLogger(self.q),
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "ignoreerrors": playlist,  # в плейлисте пропускаем недоступные видео
            "windowsfilenames": True,
        }

        audio = quality.startswith("Только звук")
        if self.has_ffmpeg:
            opts["ffmpeg_location"] = self.ffmpeg_path
            if audio:
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]
            else:
                opts["merge_output_format"] = "mp4"
        return opts

    def _run(self, url, quality, playlist, out_dir):
        try:
            with yt_dlp.YoutubeDL(self._build_options(quality, playlist, out_dir)) as ydl:
                ydl.download([url])
            if self.cancel_flag.is_set():
                self.q.put(("cancelled",))
            else:
                self.q.put(("done",))
        except Exception as e:
            if self.cancel_flag.is_set():
                self.q.put(("cancelled",))
            else:
                self.q.put(("error", clean_error(e)))

    # Вызывается yt-dlp из рабочего потока, поэтому только кладём сообщения в очередь
    def _hook(self, d):
        if self.cancel_flag.is_set():
            raise Cancelled()

        info = d.get("info_dict") or {}
        title = info.get("title")
        if title and title != self.last_title:
            self.last_title = title
            idx, total_n = info.get("playlist_index"), info.get("n_entries")
            prefix = f"[{idx}/{total_n}] " if idx and total_n else ""
            self.q.put(("title", prefix + title))

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            frac = done / total if total else 0
            speed = d.get("speed")
            text = (f"{frac * 100:.0f}%  ·  {human_size(done)} из {human_size(total)}  ·  "
                    f"{human_size(speed)}/с  ·  осталось {human_time(d.get('eta'))}")
            self.q.put(("progress", frac, text))
        elif d["status"] == "finished":
            self.q.put(("progress", 1.0, "Файл скачан, обрабатываю…"))

    def _pp_hook(self, d):
        if d.get("status") == "finished" and d.get("postprocessor") in ("Merger", "ExtractAudio"):
            path = (d.get("info_dict") or {}).get("filepath")
            if path:
                self.q.put(("log", f"Готово: {os.path.basename(path)}"))

    # ----- Обновление интерфейса из основного потока -----

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "title":
                    self.title_label.configure(text=msg[1])
                    self._log(f"Загружаю: {msg[1]}")
                elif kind == "progress":
                    self.progress.set(msg[1])
                    self.status_label.configure(text=msg[2])
                elif kind == "log":
                    self._log(msg[1])
                elif kind == "done":
                    self._set_busy(False)
                    self.progress.set(1)
                    self.status_label.configure(text="Загрузка завершена")
                    self._log(f"Загрузка завершена. Файлы в папке: {self.out_dir}")
                elif kind == "cancelled":
                    self._set_busy(False)
                    self.progress.set(0)
                    self.status_label.configure(text="Загрузка отменена")
                    self._log("Загрузка отменена. Недокачанные части (.part) можно удалить.")
                elif kind == "error":
                    self._set_busy(False)
                    self.status_label.configure(text="Ошибка — подробности в журнале")
                    self._log("Ошибка: " + msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def self_test(report_path):
    """Проверка собранного exe: находятся ли и запускаются ли ffmpeg и deno.
    Результат пишется в файл (у exe без консоли нет вывода)."""
    lines, ok = [], True
    flags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
    for name, arg in (("ffmpeg", "-version"), ("deno", "--version")):
        path = shutil.which(name)
        if not path:
            ok = False
            lines.append(f"{name}: NOT FOUND")
            continue
        try:
            res = subprocess.run([path, arg], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=120, creationflags=flags)
            first = (res.stdout or res.stderr).strip().splitlines()[0]
            if res.returncode != 0:
                ok = False
            lines.append(f"{name}: {first}")
        except Exception as e:
            ok = False
            lines.append(f"{name}: ERROR {e}")
    lines.append(f"yt-dlp: {yt_dlp.version.__version__}")
    lines.append(f"customtkinter: {ctk.__version__}")
    lines.append("RESULT: OK" if ok else "RESULT: FAIL")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        report = sys.argv[i + 1] if i + 1 < len(sys.argv) else "selftest.txt"
        sys.exit(self_test(report))
    App().mainloop()
