"""
Сборка VideoDownloader.exe.

Делает всё сам:
  1. ставит библиотеки (yt-dlp, customtkinter, pyinstaller);
  2. скачивает ffmpeg.exe и deno.exe (если их ещё нет рядом);
  3. собирает один exe со всем внутри;
  4. запускает готовый exe в режиме самопроверки.

Запуск: двойной клик по build.bat или  python build.py
"""

import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

EXE_NAME = "VideoDownloader"

TOOLS = {
    "ffmpeg.exe": [
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    ],
    "deno.exe": [
        "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
    ],
}


class BuildError(Exception):
    pass


def step(n, total, text):
    print(f"\n[{n}/{total}] {text}", flush=True)


def run(cmd):
    print("   > " + " ".join(cmd), flush=True)
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise BuildError(f"Команда завершилась с ошибкой (код {res.returncode}).")


def check_environment():
    if not sys.platform.startswith("win"):
        raise BuildError("Собирать exe нужно на Windows.")
    if struct.calcsize("P") * 8 != 64:
        raise BuildError("Установлен 32-битный Python. Нужен 64-битный: "
                         "скачайте «Windows installer (64-bit)» с python.org.")
    if sys.version_info < (3, 9):
        raise BuildError("Python слишком старый, нужен 3.9 или новее.")
    for f in ("app.py",):
        if not os.path.isfile(f):
            raise BuildError(f"Рядом с build.py нет файла {f}.")
    print(f"   Python {sys.version.split()[0]}, 64 бит — ок")
    print(f"   Папка: {ROOT}")


def install_deps():
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    run([sys.executable, "-m", "pip", "install", "--upgrade",
         "yt-dlp[default]", "customtkinter", "pyinstaller"])


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct // 10 != last:
                    last = pct // 10
                    print(f"      {pct}%  ({done // 1048576} из {total // 1048576} МБ)", flush=True)
    print(f"      скачано {done // 1048576} МБ", flush=True)


def extract(zip_path, name, dest):
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.namelist()
                   if m.replace("\\", "/").lower().rsplit("/", 1)[-1] == name.lower()]
        if not members:
            raise BuildError(f"в архиве нет {name}")
        with z.open(members[0]) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)


def fetch_tool(name, urls):
    if os.path.isfile(name) and os.path.getsize(name) > 1_000_000:
        print(f"   {name} уже есть — использую его")
        return
    errors = []
    for url in urls:
        print(f"   Скачиваю {name}: {url}", flush=True)
        tmpdir = tempfile.mkdtemp(prefix="vd_build_")
        try:
            zpath = os.path.join(tmpdir, "pkg.zip")
            download(url, zpath)
            extract(zpath, name, name + ".part")
            os.replace(name + ".part", name)
            print(f"   {name} готов")
            return
        except Exception as e:
            errors.append(f"{url}\n      -> {e}")
            print(f"   Не получилось: {e}", flush=True)
            if os.path.exists(name + ".part"):
                os.remove(name + ".part")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    raise BuildError(
        f"Не удалось скачать {name}.\n   Попробованные адреса:\n   " + "\n   ".join(errors) +
        f"\n\n   Что сделать: откройте любой из адресов в браузере, скачайте архив,\n"
        f"   достаньте из него {name} и положите в папку:\n   {ROOT}\n"
        f"   Затем запустите сборку ещё раз.")


def build_exe():
    for d in ("build", "dist"):
        shutil.rmtree(d, ignore_errors=True)
    if os.path.exists(EXE_NAME + ".spec"):
        os.remove(EXE_NAME + ".spec")

    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", EXE_NAME,
           "--collect-data", "customtkinter",
           "--add-binary", f"ffmpeg.exe{os.pathsep}.",
           "--add-binary", f"deno.exe{os.pathsep}."]
    if importlib.util.find_spec("yt_dlp_ejs"):
        cmd += ["--collect-all", "yt_dlp_ejs"]
    cmd.append("app.py")
    run(cmd)

    exe = os.path.join("dist", EXE_NAME + ".exe")
    if not os.path.isfile(exe):
        raise BuildError("PyInstaller отработал, но exe не появился.")
    return exe


def self_test(exe):
    report = os.path.join(ROOT, "selftest.txt")
    if os.path.exists(report):
        os.remove(report)
    try:
        res = subprocess.run([exe, "--selftest", report], timeout=300)
    except subprocess.TimeoutExpired:
        raise BuildError("Самопроверка exe зависла (возможно, его держит антивирус).")
    text = open(report, encoding="utf-8").read() if os.path.exists(report) else ""
    for line in text.splitlines():
        print("   " + line)
    if res.returncode != 0 or "RESULT: OK" not in text:
        raise BuildError("exe собран, но самопроверка не прошла (см. строки выше). "
                         "Если отчёта нет совсем — скорее всего, exe заблокировал антивирус.")
    os.remove(report)


def main():
    total = 5
    try:
        step(1, total, "Проверяю окружение")
        check_environment()
        step(2, total, "Устанавливаю библиотеки")
        install_deps()
        step(3, total, "Готовлю ffmpeg и Deno")
        for name, urls in TOOLS.items():
            fetch_tool(name, urls)
        step(4, total, "Собираю exe (пара минут)")
        exe = build_exe()
        step(5, total, "Проверяю готовый exe")
        self_test(exe)
    except BuildError as e:
        print(f"\nОШИБКА: {e}\n", flush=True)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 1

    size = os.path.getsize(exe) // 1048576
    print(f"\nГОТОВО! {os.path.abspath(exe)}  ({size} МБ)")
    print("Этот файл можно копировать на любой компьютер с Windows 10/11 (64 бит).")
    if not os.environ.get("CI"):
        subprocess.run(["explorer", "/select,", os.path.abspath(exe)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
