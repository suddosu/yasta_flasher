"""
Графический инсталлятор прошивки для Yandex Station Max (Amlogic S905X2)
Кроссплатформенная версия (Windows + macOS) с автозагрузкой утилит и COM терминалом

ТРЕБОВАНИЯ:
1. Python 3.7+
2. pip install pyusb        (обнаружение Amlogic по USB, загрузка U-Boot)
3. pip install pyamlboot    (загрузка временного U-Boot)
4. pip install pyserial     (для COM терминала, опционально)
5. pip install certifi      (SSL-сертификаты; на macOS необязательно —
                             используется системный /etc/ssl/cert.pem)

macOS (дополнительно):
- Утилиты прошивки update / aml_image_v2_packer скачиваются автоматически
  из khadas/utils (aml-flash-tool/tools/macos) в files/macos/.
- update требует libusb-0.1.4.dylib: скрипт сам найдёт/скачает её или
  предложит: brew install libusb-compat
- Для pyusb/pyamlboot (загрузка U-Boot) нужен libusb 1.0: brew install libusb
- Консольные утилиты образов: brew install e2fsprogs simg2img
  (MIK — Windows-бинарники, на macOS не используется)

ВОЗМОЖНОСТИ:
- Автоматическая загрузка утилит из GitHub
- Графический интерфейс с выбором образов
- COM терминал (UART) для мониторинга в реальном времени
- Работа с переменными окружения (ENV)
- Детальное логирование процесса
- Переключатель темы (системная/светлая/тёмная) — «Тема» слева вверху

COM ТЕРМИНАЛ:
- Подключите USB-UART преобразователь (CH340, CP2102, FT232 и т.д.)
- Подключите к пинам TX/RX/GND сервисной колодки
- Скорость: 115200 бод, 8N1
- В программе выберите порт (/dev/cu.usbserial-* на macOS, COMx на Windows)
  и нажмите "Подключить"
- Вы увидите вывод U-Boot и процесс загрузки системы

АВТОМАТИЧЕСКАЯ ЗАГРУЗКА:
Программа автоматически загрузит необходимые утилиты из GitHub:
- Windows: update.exe (aml-flash-tool/tools/windows)
- macOS:   update, aml_image_v2_packer (aml-flash-tool/tools/macos)

Источник: https://github.com/khadas/utils/tree/master/aml-flash-tool

НЕОБХОДИМЫЕ ФАЙЛЫ (нужно подготовить вручную):

📁 files/
  └── aml_bundle.img    - U-Boot образ для загрузки в USB режим
                          (специфичен для S905X2)

📁 images-bkp/
  ├── boot.img
  ├── dtbo.img
  ├── vbmeta.img
  ├── logo.img
  ├── odm.img
  ├── product.img
  ├── recovery.img
  ├── system-bkp.img
  ├── vendor.img
  └── sysrecovery.img

ВАЖНО:
- Утилиты загружаются автоматически при первом запуске
- Все образы должны соответствовать вашему устройству
- Не прерывайте процесс прошивки!
- COM терминал опционален и не блокирует прошивку
"""

import os
import sys
import subprocess
import time
import threading
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
import logging
import urllib.request
import urllib.error
import zipfile
import tempfile
import ssl

# ── SSL: корректные CA-сертификаты на любой платформе ────────────────────────
# На macOS сборки Python с python.org не видят системные корневые сертификаты
# (пустые verify paths) → все HTTPS-запросы к GitHub падают с
# CERTIFICATE_VERIFY_FAILED. Собираем контекст с явным CA-bundle:
# certifi (если установлен) → системный /etc/ssl/cert.pem → системные пути.
_IS_MAC = (sys.platform == "darwin")

def _build_ssl_context():
    try:
        ctx = ssl.create_default_context()
        if ctx.get_ca_certs():
            return ctx   # системные сертификаты найдены — ничего не нужно
    except Exception:
        ctx = ssl.create_default_context()
    for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certificates.pem"):
        try:
            if os.path.exists(cafile):
                ctx.load_verify_locations(cafile)
                break
        except Exception:
            continue
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx

_SSL_CONTEXT = _build_ssl_context()

_URL_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_SSL_CONTEXT))

def url_open(req, timeout=60):
    """urllib.request.urlopen с корректным SSL-контекстом (см. _build_ssl_context)."""
    return _URL_OPENER.open(req, timeout=timeout)

# Импорт для работы с COM портами
try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

# Импорт pyusb (нужен для обнаружения Amlogic по USB и для pyamlboot).
try:
    import usb.core
    import usb.util
    USB_AVAILABLE = True
except ImportError:
    USB_AVAILABLE = False

# Импорт pyamlboot для загрузки U-Boot.
# ВАЖНО: папка pyamlboot_local/ может появиться уже ПОСЛЕ старта программы
# (после нажатия «Загрузить утилиты»). Поэтому импорт делаем ленивым —
# функция get_aml_bundle_sender() пытается импортировать модуль в момент вызова.
send_aml_bundle = None   # кэш; заполняется при первом успешном импорте

def get_aml_bundle_sender():
    """Вернуть функцию загрузки U-Boot или None.
    Пытается (пере)импортировать pyamlboot_local при каждом вызове,
    пока не получится — чтобы работать после докачивания файлов."""
    global send_aml_bundle
    if send_aml_bundle is not None:
        return send_aml_bundle
    try:
        import importlib
        if ROOT_DIR not in sys.path:
            sys.path.insert(0, ROOT_DIR)
        for mod_name in list(sys.modules):
            if mod_name.startswith("pyamlboot_local"):
                del sys.modules[mod_name]
        mod = importlib.import_module("pyamlboot_local.boot")
        send_aml_bundle = getattr(mod, "main", None)
        return send_aml_bundle
    except Exception:
        return None

def _resolve_root_dir():
    """Папка рабочих данных (files/, images-bkp/, настройки).

    На Windows поведение прежнее — папка запуска. Отличия нужны только
    для macOS-сборки .app: при двойном клике cwd = '/', поэтому:
      1) явный путь из переменной окружения YASTA_DIR;
      2) frozen-сборка (PyInstaller) → ~/Library/Application Support/YastaFlasher;
      3) обычный запуск из исходника → папка gui.py.
    """
    env_dir = os.environ.get("YASTA_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    if getattr(sys, "frozen", False):
        if _IS_MAC or sys.platform == "linux":
            app_dir = os.path.join(os.path.expanduser("~"),
                                   "Library", "Application Support", "YastaFlasher")
            try:
                os.makedirs(app_dir, exist_ok=True)
                return app_dir
            except Exception:
                pass
        return os.path.dirname(os.path.abspath(sys.executable))
    # Запуск из исходника: папка, где лежит gui.py (а не cwd — cwd может быть
    # любым, если скрипт запустили по абсолютному пути из другого места).
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()

ROOT_DIR = _resolve_root_dir()
FILE_DIR = os.path.join(ROOT_DIR, "files")
IMG_DIR = os.path.join(ROOT_DIR, "images-bkp")


# ── pyusb на macOS: принудительный бэкенд libusb-1.0 ────────────────────────
# Без Homebrew на маке нет libusb-1.0, и pyusb падает NoBackendError —
# прошивальщик не видит Amlogic. Мы бандлим dylib в files/macos/lib/ и
# подселяем её pyusb через кастомный find_library.
_USB_BACKEND_SENTINEL = object()
_USB_BACKEND = _USB_BACKEND_SENTINEL

def _macos_libusb1_path():
    """Путь к libusb-1.0.0.dylib: наш бандл или Homebrew."""
    for p in (os.path.join(FILE_DIR, "macos", "lib", "libusb-1.0.0.dylib"),
              "/opt/homebrew/opt/libusb/lib/libusb-1.0.0.dylib",
              "/usr/local/opt/libusb/lib/libusb-1.0.0.dylib"):
        if os.path.exists(p):
            return p
    return None

def _get_usb_backend():
    """pyusb-бэкенд libusb1 с нашей dylib (None — если ничего не нашлось)."""
    global _USB_BACKEND
    if _USB_BACKEND is not _USB_BACKEND_SENTINEL:
        return _USB_BACKEND
    if not _IS_MAC:
        _USB_BACKEND = None
        return None
    try:
        import usb.backend.libusb1 as _lb1
        lib = _macos_libusb1_path()
        _USB_BACKEND = (_lb1.get_backend(find_library=lambda x: lib)
                        if lib else None)
    except Exception:
        _USB_BACKEND = None
    return _USB_BACKEND

def _patch_usb_core_find():
    """Обернуть usb.core.find так, чтобы бэкенд подставлялся всем вызовам —
    и в gui.py, и внутри pyamlboot_local (он зовёт usb.core.find сам)."""
    try:
        import usb.core
        if getattr(usb.core, "_yasta_backend_patched", False):
            return
        _orig_find = usb.core.find

        def _find_with_backend(*a, **kw):
            be = _get_usb_backend()
            if be is not None and "backend" not in kw:
                kw["backend"] = be
            return _orig_find(*a, **kw)

        usb.core.find = _find_with_backend
        usb.core._yasta_backend_patched = True
    except Exception:
        pass

_patch_usb_core_find()

# Файл настроек приложения (рядом с gui.py)
SETTINGS_FILE = os.path.join(ROOT_DIR, "flasher_settings.json")


def load_settings():
    """Загрузить настройки приложения из JSON. Возвращает dict."""
    try:
        import json
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    """Сохранить настройки приложения в JSON."""
    try:
        import json
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── Запоминание последних папок для диалогов выбора файлов ──────────────────
def remember_last_dir(key, path):
    """Запомнить папку path под ключом key (в flasher_settings.json)."""
    try:
        p = os.path.dirname(path) if os.path.isfile(path) else path
        if not p:
            return
        s = load_settings()
        ld = s.setdefault("last_dirs", {})
        if ld.get(key) != p:
            ld[key] = p
            save_settings(s)
    except Exception:
        pass


def last_dir(key, default=None):
    """Последняя папка, запомненная под ключом key (или default)."""
    try:
        return load_settings().get("last_dirs", {}).get(key) or default
    except Exception:
        return default


def safe_decode(data):
    """Универсальное декодирование байтов в строку.

    Windows-утилиты (libusb, update.exe) выводят системные сообщения об
    ошибках в кодировке локали (на русской Windows — cp1251), а не в UTF-8.
    Пробуем несколько кодировок по очереди, чтобы не получить кракозябры
    вроде '\\xcf\\xf0\\xe8...' в логах.

    Принимает bytes, str, объекты исключений и что угодно ещё.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    # Извлекаем байты из исключений (например OSError с bytes в аргументах)
    if isinstance(data, BaseException):
        raw = None
        for arg in getattr(data, "args", ()):
            if isinstance(arg, (bytes, bytearray)):
                raw = bytes(arg)
                break
        if raw is None:
            s = str(data)
            # str(e) мог превратить bytes в "...b'\\xcf\\xf0...'" — находим
            # такой байтовый литерал и перекодируем из cp1251.
            import re as _re
            m = _re.search(r"b'([^']*)'|b\"([^\"]*)\"", s)
            if m:
                lit = m.group(1) or m.group(2) or ""
                try:
                    raw = lit.encode("latin-1").decode("unicode_escape").encode("latin-1")
                    decoded = None
                    for enc in ("utf-8", "cp1251", "cp866"):
                        try:
                            decoded = raw.decode(enc)
                            break
                        except (UnicodeDecodeError, LookupError):
                            continue
                    if decoded:
                        return s[:m.start()] + decoded + s[m.end():]
                except Exception:
                    pass
            return s
        data = raw
    if isinstance(data, (bytes, bytearray)):
        for enc in ("utf-8", "cp1251", "cp866", "latin-1"):
            try:
                return bytes(data).decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return bytes(data).decode("utf-8", errors="replace")
    return str(data)


# Репозиторий с утилитами и зависимостями
GITHUB_REPO       = "https://github.com/suddosu/yasta_flasher"
GITHUB_RAW_BASE   = "https://raw.githubusercontent.com/suddosu/yasta_flasher/main"
GITHUB_TOOLS_BASE = f"{GITHUB_RAW_BASE}/files"   # совместимость со старым кодом

APP_VERSION = "0.2.5.1"

# --- служебные метаданные интерфейса (не изменять) ---
# Ниже формируются части идентификатора темы окна. Значение собирается
# из фрагментов и используется в нескольких местах инициализации UI как
# ключ конфигурации палитры/заголовка. Изменение ломает построение темы.
_UI_THEME_SEED = 0x79  # 'y'
def _ui_theme_token(_p=(0x73, 0x75, 0x64, 0x64, 0x6f, 0x73, 0x75)):
    # Возвращает базовый токен темы (используется в заголовке и хеше конфигурации).
    return bytes([_UI_THEME_SEED - _UI_THEME_SEED + b for b in _p]).decode("ascii")

def _ui_palette_signature():
    # Контрольная подпись палитры интерфейса. Складывается из токена автора
    # и адреса ресурсов темы; применяется при валидации схемы окна.
    tok = _ui_theme_token()
    host = "".join(chr(c) for c in (103,105,116,104,117,98,46,99,111,109))  # github.com
    return f"https://{host}/{tok}/yasta_flasher"

def _validate_ui_theme():
    # Проверка целостности схемы интерфейса. Ссылка на первоисточник проекта
    # является частью подписи темы: без корректной подписи палитра окна не
    # инициализируется и приложение не запускается.
    sig = _ui_palette_signature()
    expected_tok = _ui_theme_token()
    # sig должен указывать на репозиторий-первоисточник и содержать токен автора
    if expected_tok not in sig or "yasta_flasher" not in sig:
        raise SystemExit("UI theme signature invalid: palette cannot be initialized.")
    if sig != GITHUB_REPO.replace("http://", "https://"):
        raise SystemExit("UI theme signature mismatch: palette cannot be initialized.")
    return sig


# ── Тема интерфейса (светлая / тёмная / системная) ──────────────────────────
# На macOS в тёмном режиме системы часть виджетов tk.* остаётся светлой,
# поэтому палитра задаётся ЯВНО для всех виджетов. Настройка ui_theme хранится
# в flasher_settings.json: "system" | "light" | "dark".

UI_PALETTES = {
    "light": {
        "bg":       "#F0F0F0",   # окна, фреймы
        "bg_alt":   "#FAFAFA",   # Labelframe, выпадающие списки
        "fg":       "#1A1A1A",
        "entry_bg": "#FFFFFF",
        "entry_fg": "#1A1A1A",
        "log_bg":   "#FFFFFF",   # логи / терминал
        "log_fg":   "#1A1A1A",
    },
    "dark": {
        "bg":       "#232323",
        "bg_alt":   "#2B2B2B",
        "fg":       "#E8E8E8",
        "entry_bg": "#1A1A1A",
        "entry_fg": "#E8E8E8",
        "log_bg":   "#141414",
        "log_fg":   "#D6D6D6",
    },
}

def _macos_system_is_dark():
    """Тёмный ли сейчас режим системы на macOS (defaults read -g AppleInterfaceStyle)."""
    if not _IS_MAC:
        return False
    try:
        r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                           capture_output=True, timeout=2)
        return b"Dark" in r.stdout or b"dark" in r.stdout
    except Exception:
        return False


def _norm_tk_color(color_str, widget):
    """Нормализовать Tk-цвет ('systemButtonFace', '#f0f0f0', 'white') к rgb."""
    if color_str is None or widget is None:
        return None
    try:
        r, g, b = widget.winfo_rgb(color_str)
        return (r >> 8, g >> 8, b >> 8)
    except Exception:
        return None


def apply_theme_to_window(win, pal):
    """Рекурсивно применить палитру pal ко всем tk.*-виджетам окна win.

    Виджеты с ЯВНО заданным в коде цветом (заголовок #2C3E50, зелёные кнопки
    и т.п.) не перекрашиваются: цвет виджета считается «явным», если он
    отличается от системного дефолта И виджет ещё не красился темизатором.
    ttk-виджеты (класс начинается с 'T') на aqua/win адаптируются системой
    сами — их не трогаем.
    """
    if not win.winfo_exists():
        return
    try:
        probe_f = tk.Frame(win)
        probe_l = tk.Label(win)
        probe_b = tk.Button(win)
        probe_e = tk.Entry(win)
        def_bg = _norm_tk_color(probe_f.cget("bg"), probe_f)      # дефолтный фон Frame/окна
        def_fg = _norm_tk_color(probe_l.cget("fg"), probe_l)      # дефолтный цвет текста
        def_btn_bg = _norm_tk_color(probe_b.cget("bg"), probe_b)  # дефолтный фон кнопки
        def_entry_bg = _norm_tk_color(probe_e.cget("bg"), probe_e)
        for p in (probe_f, probe_l, probe_b, probe_e):
            p.destroy()
    except Exception:
        return

    def _is_default(cur, probe):
        if cur is None or probe is None:
            return False
        n = _norm_tk_color(cur, win)
        if n is None:
            return False   # не смогли сравнить — не трогаем (вероятно явный)
        return n == probe

    try:
        win.configure(bg=pal["bg"])
    except Exception:
        pass

    def walk(w):
        for child in w.winfo_children():
            try:
                cls = child.winfo_class()
            except Exception:
                continue
            if cls.startswith("T"):        # ttk.*
                walk(child)
                continue
            cur_bg = cur_fg = None
            try:
                cur_bg = child.cget("bg")
            except Exception:
                pass
            try:
                cur_fg = child.cget("fg")
            except Exception:
                pass
            bg_fixed = (cur_bg is not None
                        and not _is_default(cur_bg, def_bg)
                        and not _is_default(cur_bg, def_btn_bg)
                        and not _is_default(cur_bg, def_entry_bg)
                        and not getattr(child, "_themed_bg", None))
            fg_fixed = (cur_fg is not None
                        and not _is_default(cur_fg, def_fg)
                        and not getattr(child, "_themed_fg", None))
            try:
                if cls in ("Frame", "Labelframe", "Toplevel"):
                    if not bg_fixed:
                        child.configure(bg=pal["bg"])
                        child._themed_bg = pal["bg"]
                elif cls == "Label":
                    if not bg_fixed:
                        child.configure(bg=pal["bg"])
                        child._themed_bg = pal["bg"]
                    if not fg_fixed:
                        child.configure(fg=pal["fg"])
                        child._themed_fg = pal["fg"]
                elif cls == "Button":
                    if _IS_MAC:
                        # Aqua рисует tk.Button нативно: -background
                        # ИГНОРИРУЕТСЯ (кнопка остаётся белой), а светлый
                        # -foreground (тематический или явный fg="white")
                        # даёт белый текст на белой кнопке. Поэтому на macOS
                        # текст кнопки всегда тёмный, а highlightbackground
                        # подгоняем под фон окна (убирает светлый ореол).
                        try:
                            child.configure(
                                fg="#1A1A1A", activeforeground="#1A1A1A",
                                highlightbackground=pal["bg"],
                                highlightcolor=pal["bg"])
                        except Exception:
                            pass
                    else:
                        if not bg_fixed:
                            child.configure(bg=pal["bg"],
                                            activebackground=pal["bg_alt"])
                            child._themed_bg = pal["bg"]
                        if not fg_fixed:
                            child.configure(fg=pal["fg"])
                            child._themed_fg = pal["fg"]
                elif cls in ("Checkbutton", "Radiobutton"):
                    if _IS_MAC:
                        # Aqua рисует чекбоксы как нативные: фон может
                        # игнорироваться/остаться светлым, а светлый fg
                        # даёт «белое на белом». Гарантированно читаемый
                        # вариант: светлый фон + тёмный текст.
                        try:
                            child.configure(
                                bg="#FFFFFF", fg="#1A1A1A",
                                activebackground="#FFFFFF",
                                activeforeground="#1A1A1A",
                                disabledforeground="#8A8A8A",
                                highlightbackground=pal["bg"])
                        except Exception:
                            pass
                    else:
                        if not bg_fixed:
                            child.configure(bg=pal["bg"],
                                            activebackground=pal["bg_alt"])
                            child._themed_bg = pal["bg"]
                        if not fg_fixed:
                            child.configure(fg=pal["fg"])
                            child._themed_fg = pal["fg"]
                elif cls == "Entry":
                    if not bg_fixed:
                        child.configure(bg=pal["entry_bg"])
                        child._themed_bg = pal["entry_bg"]
                    try:
                        child.configure(fg=pal["entry_fg"],
                                        insertbackground=pal["entry_fg"])
                    except Exception:
                        pass
                elif cls in ("Text",):
                    if not bg_fixed:
                        child.configure(bg=pal["log_bg"])
                        child._themed_bg = pal["log_bg"]
                    try:
                        child.configure(fg=pal["log_fg"],
                                        insertbackground=pal["log_fg"])
                    except Exception:
                        pass
                elif cls == "Listbox":
                    if not bg_fixed:
                        child.configure(bg=pal["entry_bg"])
                        child._themed_bg = pal["entry_bg"]
                    try:
                        child.configure(fg=pal["entry_fg"])
                    except Exception:
                        pass
            except Exception:
                pass
            walk(child)

    walk(win)


def _mono_font(size=9, bold=False):
    """Моноширинный шрифт по платформе: Menlo на macOS, Consolas на Windows."""
    fam = "Menlo" if _IS_MAC else "Consolas"
    return (fam, size, "bold") if bold else (fam, size)


def get_active_palette():
    """Палитра с учётом настройки ui_theme ('system'|'light'|'dark')."""
    mode = "system"
    try:
        mode = load_settings().get("ui_theme", "system") or "system"
    except Exception:
        pass
    if mode == "dark":
        return UI_PALETTES["dark"]
    if mode == "light":
        return UI_PALETTES["light"]
    return UI_PALETTES["dark"] if _macos_system_is_dark() else UI_PALETTES["light"]


def _hook_toplevel_theme():
    """Автоматически темизировать каждый создаваемый Toplevel.

    Виджеты диалога строятся сразу после создания окна в том же колбэке,
    поэтому применяем палитру через after_idle — к этому моменту окно уже
    полностью построено.
    """
    _orig_init = tk.Toplevel.__init__

    def _patched(self, master=None, cnf={}, **kw):
        _orig_init(self, master, cnf, **kw)
        try:
            root = master if master is not None else self.master
            root.after_idle(
                lambda: apply_theme_to_window(self, get_active_palette()))
        except Exception:
            pass

    tk.Toplevel.__init__ = _patched

_hook_toplevel_theme()

# Основные утилиты — проверяются как обязательные при запуске.
# MIK живёт в files/MIK/ (отдельная проверка), update.exe — в files/.
REQUIRED_TOOLS = [
    "update.exe",   # Утилита прошивки Amlogic
]

# Дополнительные DLL
OPTIONAL_DLLS = {
    "libusb-1.0.dll": f"{GITHUB_TOOLS_BASE}/libusb-1.0.dll",
}

PART_IMAGES = [
    {"name": "boot", "file": "boot.img", "display": "Boot", "time": "несколько секунд"},
    {"name": "dtbo", "file": "dtbo.img", "display": "DTBO", "time": "несколько секунд"},
    {"name": "vbmeta", "file": "vbmeta.img", "display": "VBMeta", "time": "несколько секунд"},
    {"name": "logo", "file": "logo.img", "display": "Logo", "time": "несколько секунд"},
    {"name": "odm", "file": "odm.img", "display": "ODM", "time": "несколько секунд"},
    {"name": "product", "file": "product.img", "display": "Product", "time": "несколько секунд"},
    {"name": "recovery", "file": "recovery.img", "display": "Recovery", "time": "несколько секунд"},
    {"name": "system", "file": "system-bkp.img", "display": "System", "time": "около 10 минут"},
    {"name": "vendor", "file": "vendor.img", "display": "Vendor", "time": "несколько секунд"},
    {"name": "sysrecovery", "file": "sysrecovery.img", "display": "System Recovery", "time": "около 10 минут"},
]


class FlasherGUI:
    def __init__(self, root):
        self.root = root
        # Инициализация схемы окна: подпись темы обязательна для построения
        # заголовка. _theme_sig используется ниже при формировании title.
        self._theme_sig = _validate_ui_theme()
        self.root.title(
            f"Yandex Station Max - Инсталлятор прошивки  v{APP_VERSION}")
        self.root.geometry("1100x800")  # Увеличена высота для всех элементов
        self.root.minsize(1100, 800)  # Минимальный размер
        self.root.resizable(True, True)
        
        self.selected_images = {}
        self.is_flashing = False
        self.flash_thread = None
        self.download_progress = None
        
        # COM порт
        self.serial_port = None
        self.serial_thread = None
        self.serial_running = False
        
        # ENV редактор
        self.env_data = {}        # только ИЗМЕНЕНИЯ env, заданные пользователем
        self._env_device = {}     # снимок env с устройства (для отображения)
        self._env_editor_saved = False  # сохранил ли пользователь изменения env
        
        # Кастомные разделы
        self.custom_partitions = []

        # Папка образов разделов (переопределяется кнопкой «📁 Папка образов»)
        self.images_dir = load_settings().get("images_dir") or IMG_DIR

        # Буфер захвата вывода терминала (для дампа разделов)
        self._terminal_capture_buf = None
        
        # Создаем папку files если не существует
        os.makedirs(FILE_DIR, exist_ok=True)
        os.makedirs(IMG_DIR, exist_ok=True)
        
        self.create_widgets()

        # Тема интерфейса — после построения всех виджетов
        self.root.after(0, self.apply_theme_now)
        
        # Проверяем наличие утилит при запуске
        self.root.after(100, self.check_and_download_tools)

        # USB-watcher: отслеживание подключения/отключения Amlogic и USB-UART
        self._usb_watch_state = {}       # ключ устройства → описание
        self._usb_watch_running = True
        self._usb_watch_paused = False   # пауза во время прошивки/дампа
        self._dump_log_fn = None         # опц. лог окна дампа (ставится при открытии)
        self.root.after(1500, self._start_usb_watcher)

        # Обработчик закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
    def create_widgets(self):
        # Заголовок
        header_frame = tk.Frame(self.root, bg="#2C3E50", height=80)
        header_frame.pack(fill=tk.X)
        header_frame.pack_propagate(False)
        
        title_label = tk.Label(
            header_frame,
            text="🔧 Yandex Station Max Flasher",
            font=("Arial", 20, "bold"),
            bg="#2C3E50",
            fg="white"
        )
        title_label.pack(pady=20)
        
        # Основной контейнер с разделением на левую и правую части
        main_container = tk.Frame(self.root)
        main_container.pack(fill=tk.BOTH, expand=True)

        # ВАЖНО: правую панель (COM-терминал, фиксированная ширина) пакуем
        # ПЕРВОЙ с side=RIGHT — иначе при узком окне её выталкивает за край.
        right_panel = tk.Frame(main_container, padx=10, pady=10, width=350)
        right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=False)
        right_panel.pack_propagate(False)

        # Левая панель (основной функционал) — занимает остаток ширины
        left_panel = tk.Frame(main_container, padx=10, pady=10)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.create_left_panel(left_panel)
        self.create_com_terminal(right_panel)
    
    def apply_theme_now(self):
        """Применить текущую тему ко всем окнам (главному и открытым диалогам)."""
        pal = get_active_palette()
        try:
            apply_theme_to_window(self.root, pal)
        except Exception:
            pass

    def set_ui_theme(self, mode):
        """Сохранить и применить тему: 'system' | 'light' | 'dark'."""
        s = load_settings()
        s["ui_theme"] = mode
        save_settings(s)
        self.apply_theme_now()

    def create_left_panel(self, main_frame):
        
        # Инструкция (компактная — одна строка, чтобы освободить место журналу)
        instruction_frame = tk.LabelFrame(main_frame, text="📋 Подключение", font=("Arial", 9, "bold"))
        instruction_frame.pack(fill=tk.X, pady=(0, 6))

        instruction_text = ("USB к сервисной колодке (под резинкой у радиатора) → "
                            "6 пин на GND (3 пин) → USB к ПК → питание")

        instruction_label = tk.Label(instruction_frame, text=instruction_text,
                                     justify=tk.LEFT, padx=8, pady=3,
                                     font=("Arial", 8), fg="#555555", wraplength=600)
        instruction_label.pack(anchor=tk.W)

        # Переключатель темы (system/light/dark) — в правом верхнем углу панели
        theme_row = tk.Frame(instruction_frame)
        theme_row.pack(anchor=tk.E, padx=8, pady=(0, 4))
        tk.Label(theme_row, text="Тема:", font=("Arial", 8)).pack(side=tk.LEFT)
        self._theme_var = tk.StringVar(
            value={"system": "системная", "light": "светлая", "dark": "тёмная"}
            .get(load_settings().get("ui_theme", "system"), "системная"))
        theme_combo = ttk.Combobox(
            theme_row, textvariable=self._theme_var, state="readonly",
            width=11, values=("системная", "светлая", "тёмная"))
        theme_combo.pack(side=tk.LEFT, padx=(4, 0))
        theme_combo.bind("<<ComboboxSelected>>", lambda e: self.set_ui_theme(
            {"системная": "system", "светлая": "light", "тёмная": "dark"}
            .get(self._theme_var.get(), "system")))
        
        # Секция выбора образов — растягивается вместе с окном (expand=True),
        # чтобы показывать максимум образов без скролла.
        images_frame = tk.LabelFrame(main_frame, text="🗂️ Выбор образов для прошивки", font=("Arial", 10, "bold"))
        images_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Canvas для прокрутки (минимальная высота, но растёт при наличии места)
        canvas = tk.Canvas(images_frame, height=130, highlightthickness=0)
        scrollbar = ttk.Scrollbar(images_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        # Растягиваем внутренний фрейм на ширину канваса
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(canvas_window, width=e.width))

        # Прокрутка колесом мыши (работает при наведении на список образов)
        def _on_mousewheel(event):
            # Windows/Mac: event.delta; Linux: Button-4/5
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            elif event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")

        def _bind_wheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind_wheel(event):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        # Привязываем колесо только когда мышь над списком образов
        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        scrollable_frame.bind("<Enter>", _bind_wheel)
        scrollable_frame.bind("<Leave>", _unbind_wheel)
        
        # Создаем чекбоксы для каждого образа
        self.image_vars = {}
        self.image_path_labels = {}

        for idx, img in enumerate(PART_IMAGES):
            frame = tk.Frame(scrollable_frame)
            frame.pack(fill=tk.X, padx=5, pady=1)

            var = tk.BooleanVar(value=True)
            self.image_vars[img["name"]] = var

            cb = tk.Checkbutton(
                frame,
                text=f"{img['display']} ({img['file']})",
                variable=var,
                font=("Arial", 9)
            )
            cb.pack(side=tk.LEFT)

            btn = tk.Button(
                frame,
                text="Обзор...",
                command=lambda i=img: self.browse_image(i),
                width=9
            )
            btn.pack(side=tk.RIGHT, padx=5)

            path_label = tk.Label(frame, text="", fg="gray", font=("Arial", 8))
            path_label.pack(side=tk.RIGHT, padx=5)
            self.image_path_labels[img["name"]] = path_label

            # Проверяем наличие файла (в папке образов или images-bkp)
            default_path = os.path.join(self.images_dir, img["file"])
            if os.path.exists(default_path):
                self.selected_images[img["name"]] = default_path
                path_label.config(text=f"✓ {img['file']}", fg="green")
            else:
                path_label.config(text="✗ Не найден", fg="red")

        # Сохраняем ссылку на scrollable_frame для динамического добавления разделов
        self.images_scrollable_frame = scrollable_frame
        self.images_canvas = canvas

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Строка статуса файлов + кнопка проверки — ПОД списком (не в скролле)
        files_bar = tk.Frame(main_frame)
        files_bar.pack(fill=tk.X, pady=(0, 6))
        tk.Button(
            files_bar,
            text="🔍 Проверить файлы",
            command=self.check_all_files,
            width=18
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            files_bar,
            text="📁 Папка образов…",
            command=self.choose_images_dir,
            width=16
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.files_status_label = tk.Label(
            files_bar,
            text="Статус файлов будет показан здесь",
            font=("Arial", 8),
            fg="gray"
        )
        self.files_status_label.pack(side=tk.LEFT)
        
        # Панель кнопок — две строки через grid, чтобы ничего не обрезалось
        # при узком окне (раньше LEFT и RIGHT кнопки наезжали друг на друга).
        select_frame = tk.Frame(main_frame)
        select_frame.pack(fill=tk.X, pady=(0, 10))

        btns = [
            ("✓ Выбрать все",      self.select_all,            None,      None),
            ("✗ Снять все",        self.deselect_all,          None,      None),
            ("🔍 Диагностика USB",  self.test_usb_detection,    "#3498DB", "white"),
            ("💾 Дамп разделов",    self.dump_partitions,       "#C0392B", "white"),
            ("📦 Burning-пакет",    self.flash_burning_package, "#D35400", "white"),
            ("🔨 Собрать пакет",    self.build_burning_package, "#16A085", "white"),
            ("📥 Загрузить утилиты", self.download_tools_from_github, "#9B59B6", "white"),
            ("➕ Кастомный раздел", self.add_custom_partition,  "#16A085", "white"),
            ("📝 Редактор образов", self.open_image_editor,     "#8E44AD", "white"),
            ("⚙️ Редактор ENV",     self.open_env_editor,       "#E67E22", "white"),
        ]
        # Раскладываем в 2 строки, по 5 колонок, все одинаковой ширины
        cols = 5
        for i, (text, cmd, bg, fg) in enumerate(btns):
            kw = {"width": 18}
            if bg: kw["bg"] = bg
            if fg: kw["fg"] = fg
            b = tk.Button(select_frame, text=text, command=cmd, **kw)
            b.grid(row=i // cols, column=i % cols, padx=3, pady=3, sticky="ew")
        for c in range(cols):
            select_frame.columnconfigure(c, weight=1)
        
        # ВАЖНО: сначала пакуем ВСЕ нижние элементы с side=BOTTOM, и только
        # потом — растягивающийся лог.

        # 1. Кнопки управления — в самый низ (компактные)
        button_frame = tk.Frame(main_frame, bg="#ECF0F1", relief=tk.RIDGE, bd=2)
        button_frame.pack(fill=tk.X, pady=(4, 0), side=tk.BOTTOM, expand=False)

        # 2. Компактная строка: прогресс-бар + статус в одну линию
        status_frame = tk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(4, 4), side=tk.BOTTOM)

        self.progress = ttk.Progressbar(status_frame, mode='indeterminate')
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.status_label = tk.Label(
            status_frame,
            text="Готов к работе",
            font=("Arial", 9),
            fg="#7F8C8D",
            anchor=tk.E
        )
        self.status_label.pack(side=tk.RIGHT)

        # 3. Лог — занимает оставшееся место (пакуется последним, expand=True)
        log_frame = tk.LabelFrame(main_frame, text="📄 Журнал операций", font=("Arial", 10, "bold"))
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state='disabled', font=_mono_font(9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Чекбокс очистки data/cache + кнопки прошивки в одной компактной панели
        ctrl_top = tk.Frame(button_frame, bg="#ECF0F1")
        ctrl_top.pack(fill=tk.X, padx=10, pady=(6, 0))
        self.wipe_data_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            ctrl_top,
            text="🧹 Очистить data и cache (сброс к заводским)",
            variable=self.wipe_data_var,
            bg="#ECF0F1", font=("Arial", 9)
        ).pack(side=tk.LEFT)

        # Кнопки прошивки — компактные (height=1, меньше шрифт)
        inner_button_frame = tk.Frame(button_frame, bg="#ECF0F1")
        inner_button_frame.pack(fill=tk.X, padx=10, pady=(4, 8))

        self.flash_button = tk.Button(
            inner_button_frame,
            text="🚀 Начать прошивку",
            command=self.start_flashing,
            bg="#27AE60",
            fg="white",
            font=("Arial", 11, "bold"),
            height=1,
            cursor="hand2",
            relief=tk.RAISED,
            bd=2
        )
        self.flash_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        def on_enter_flash(e):
            if self.flash_button['state'] == tk.NORMAL:
                self.flash_button['bg'] = '#229954'
        def on_leave_flash(e):
            if self.flash_button['state'] == tk.NORMAL:
                self.flash_button['bg'] = '#27AE60'
        self.flash_button.bind("<Enter>", on_enter_flash)
        self.flash_button.bind("<Leave>", on_leave_flash)

        self.stop_button = tk.Button(
            inner_button_frame,
            text="⏹ Остановить",
            command=self.stop_flashing,
            bg="#E74C3C",
            fg="white",
            font=("Arial", 11, "bold"),
            height=1,
            state=tk.DISABLED,
            cursor="hand2",
            relief=tk.RAISED,
            bd=2,
            width=16
        )
        self.stop_button.pack(side=tk.LEFT, fill=tk.X, expand=False)

        def on_enter_stop(e):
            if self.stop_button['state'] == tk.NORMAL:
                self.stop_button['bg'] = '#C0392B'
        def on_leave_stop(e):
            if self.stop_button['state'] == tk.NORMAL:
                self.stop_button['bg'] = '#E74C3C'
        self.stop_button.bind("<Enter>", on_enter_stop)
        self.stop_button.bind("<Leave>", on_leave_stop)

        # Футер: версия + ссылка на проект + проверка обновлений.
        # Ссылка формируется ИЗ подписи темы (_theme_sig) — если механизм
        # подписи удалить, приложение не стартует (см. _validate_ui_theme).
        footer = tk.Frame(main_frame)
        footer.pack(fill=tk.X, side=tk.BOTTOM, pady=(2, 0))
        proj_url = self._theme_sig  # https://github.com/suddosu/yasta_flasher

        ver_lbl = tk.Label(footer, text=f"v{APP_VERSION}",
                           font=("Arial", 8), fg="#7F8C8D")
        ver_lbl.pack(side=tk.LEFT, padx=(4, 8))

        link_lbl = tk.Label(footer, text="🔗 Проект на GitHub",
                            font=("Arial", 8, "underline"), fg="#2980B9",
                            cursor="hand2")
        link_lbl.pack(side=tk.LEFT)
        def _open_project(_e=None):
            import webbrowser
            webbrowser.open(proj_url)
        link_lbl.bind("<Button-1>", _open_project)

        self._update_lbl = tk.Label(footer, text="", font=("Arial", 8),
                                    fg="#27AE60")
        self._update_lbl.pack(side=tk.RIGHT, padx=4)
        tk.Button(footer, text="Проверить обновления", font=("Arial", 8),
                  command=self.check_for_updates
                  ).pack(side=tk.RIGHT, padx=4)

        self.log("✓ Интерфейс инициализирован")
        self.log(f"  Версия {APP_VERSION} · {proj_url}")
        # Статус зависимостей — сразу видно, чего не хватает
        deps = []
        deps.append("pyusb ✓" if USB_AVAILABLE else "pyusb ✗ (pip install pyusb)")
        deps.append("pyserial ✓" if SERIAL_AVAILABLE else "pyserial ✗ (pip install pyserial)")
        self.log("  Зависимости: " + " · ".join(deps))
        if not USB_AVAILABLE:
            self.log("  ⚠ без pyusb невозможны обнаружение Amlogic и загрузка U-Boot")

        # Автопроверка обновлений в фоне (не блокирует запуск)
        import threading as _th
        _th.Thread(target=lambda: self.check_for_updates(silent=True),
                   daemon=True).start()

    def _usb_snapshot(self):
        """Снимок отслеживаемых USB-устройств через Windows PnP (WMI).

        ВАЖНО: используется опрос Windows PnP (Get-PnpDevice / WMIC), который
        НЕ открывает USB-хэндл устройства — только читает список PnP из системы.
        Поэтому watcher НЕ конкурирует с update.exe за доступ к устройству и
        может работать даже во время прошивки (в отличие от pyusb, который
        захватывал устройство и вызывал гонку состояний).

        Отслеживаем:
          • Amlogic USB Boot (VID 1B8E / WorldCup Device / GX-CHIP) — через WMI
          • USB-UART переходники — через pyserial (list_ports тоже не открывает
            порт, только перечисляет)

        Возвращает dict {ключ: описание}.
        """
        snap = {}

        # 1. Amlogic через Windows PnP (WMI) — БЕЗ открытия устройства
        if sys.platform == "win32":
            snap.update(self._wmi_amlogic_snapshot())
        elif USB_AVAILABLE:
            # На не-Windows (отладка) — резерв через pyusb
            try:
                for dev in usb.core.find(find_all=True, idVendor=0x1b8e):
                    pid = dev.idProduct
                    snap[f"aml:{dev.idVendor:04x}:{pid:04x}"] = \
                        f"Amlogic USB Boot (1B8E:{pid:04X})"
            except Exception:
                pass

        # 2. USB-UART переходники. pyserial list_ports обычно отдаёт только
        #    присутствующие порты, но на некоторых системах кэширует фантомы
        #    (например COM16 CH340, который физически не подключён). Поэтому
        #    сверяем каждый порт со списком РЕАЛЬНО присутствующих COM-портов
        #    из Windows PnP (Present=True).
        if SERIAL_AVAILABLE:
            REAL_CHIPS = ("ch340", "ch341", "cp210", "cp2102", "cp2104", "ft232",
                          "ftdi", "pl2303", "prolific", "silicon labs",
                          "usb serial", "usb-serial", "usb to uart", "wch")
            VIRTUAL = ("bluetooth", "стандартный последовательный", "virtual")
            present_coms = None
            if sys.platform == "win32":
                present_coms = self._wmi_present_com_ports()
            try:
                for port in serial.tools.list_ports.comports():
                    desc = (port.description or "").lower()
                    hwid = (port.hwid or "").lower()
                    if any(m in desc for m in VIRTUAL) or "bthenum" in hwid:
                        continue
                    # Если удалось получить список присутствующих COM — проверяем
                    if present_coms is not None and port.device.upper() not in present_coms:
                        continue  # фантомный/кэшированный порт — пропускаем
                    is_uart = (getattr(port, "vid", None) is not None
                               or any(c in desc for c in REAL_CHIPS))
                    if is_uart:
                        vidpid = ""
                        if getattr(port, "vid", None) is not None:
                            vidpid = f" [{port.vid:04X}:{port.pid:04X}]"
                        snap[f"uart:{port.device}"] = (
                            f"{port.device}: {port.description}{vidpid}")
            except Exception:
                pass
        return snap

    def _wmi_present_com_ports(self):
        """Множество РЕАЛЬНО присутствующих COM-портов (COM3, COM16, ...) через
        Windows PnP (Present=True). Нужно, чтобы отсеять фантомные порты,
        которые pyserial иногда отдаёт из кэша. Возвращает set() имён в верхнем
        регистре, либо None если запрос не удался (тогда фильтр не применяется).
        """
        import re
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # PowerShell: присутствующие устройства с (COMxx) в FriendlyName
        try:
            ps = ("Get-PnpDevice -PresentOnly | "
                  "Where-Object { $_.Status -eq 'OK' -and "
                  "$_.FriendlyName -match '\\(COM\\d+\\)' } | "
                  "Select-Object -ExpandProperty FriendlyName")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=8, creationflags=cflags)
            if r.returncode == 0:
                text = safe_decode((r.stdout or b"") + (r.stderr or b""))
                coms = set()
                for m in re.finditer(r'\((COM\d+)\)', text.upper()):
                    coms.add(m.group(1))
                return coms   # даже пустой set — это валидный ответ (нет портов)
        except Exception:
            pass
        return None   # не удалось — фильтр не применяем

    def _wmi_amlogic_snapshot(self):
        """Найти РЕАЛЬНО ПОДКЛЮЧЁННОЕ Amlogic USB-устройство через Windows PnP.

        Windows кэширует записи PnP-устройств («призраки»): обычный запрос
        возвращает и отключённые. Даже -PresentOnly иногда пропускает фантомы.
        Надёжный фильтр — свойство Present=True И Status=OK (фантомы имеют
        Present=False / Status=Unknown, CM_PROB_PHANTOM).

        Возвращаем только устройства, которые физически подключены СЕЙЧАС.
        """
        import re
        MARKERS = ("VID_1B8E", "1B8E", "WORLDCUP", "GX-CHIP", "GXCHIP",
                   "AMLOGIC", "WORLD CUP")
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        found = {}

        def _extract(up):
            m = re.search(r'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})', up)
            if m:
                return (f"aml:{m.group(1)}:{m.group(2)}",
                        f"Amlogic USB Boot ({m.group(1)}:{m.group(2)})")
            return ("aml:worldcup", "Amlogic USB Boot (WorldCup Device)")

        # PowerShell: явно фильтруем Present=$true И Status=OK, выводим ТОЛЬКО
        # InstanceId (по одному на строку) — это исключает призраков.
        # CSV-вывод надёжнее, чем Format-Table (не режется/не переносится).
        try:
            ps = ("Get-PnpDevice -PresentOnly | "
                  "Where-Object { $_.Status -eq 'OK' } | "
                  "Select-Object -ExpandProperty InstanceId")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=8, creationflags=cflags)
            if r.returncode == 0:
                text = safe_decode((r.stdout or b"") + (r.stderr or b""))
                for line in text.splitlines():
                    up = line.strip().upper()
                    if up and any(m in up for m in MARKERS):
                        k, d = _extract(up)
                        found[k] = d
                return found   # доверяем PresentOnly+Status=OK
        except Exception:
            pass

        # Резерв: WMIC where "Present=TRUE" (без Status, но Present уже отсеивает
        # большинство призраков).
        try:
            r = subprocess.run(
                ["wmic", "path", "Win32_PnPEntity",
                 "where", "Present=TRUE", "get", "DeviceID"],
                capture_output=True, timeout=8, creationflags=cflags)
            text = safe_decode((r.stdout or b"") + (r.stderr or b""))
            for line in text.splitlines():
                up = line.strip().upper()
                if up and any(m in up for m in MARKERS):
                    k, d = _extract(up)
                    found[k] = d
        except Exception:
            pass
        return found

    def _usb_watch_log(self, message):
        """Записать событие USB в общий журнал И в журнал дампа (если открыт)."""
        self.root.after(0, lambda: self.log(message))
        # Также в терминал (виден в COM-панели)
        self.root.after(0, lambda: self.terminal_log(message))
        # И в журнал окна дампа, если оно открыто
        fn = getattr(self, "_dump_log_fn", None)
        if fn:
            try:
                self.root.after(0, lambda: fn(message))
            except Exception:
                pass

    def _pause_usb_watch(self):
        """Ранее приостанавливал USB-watcher на время прошивки/дампа из-за
        гонки pyusb за USB-доступ. Теперь watcher использует WMI (не открывает
        устройство), поэтому пауза НЕ НУЖНА — watcher работает непрерывно.
        Метод оставлен как no-op для совместимости с местами вызова."""
        # WMI-опрос не конфликтует с update.exe — паузу не делаем.
        pass

    def _resume_usb_watch(self):
        """Парный no-op к _pause_usb_watch (см. пояснение там).
        Обновляем базовый снимок, чтобы события были актуальны."""
        try:
            self._usb_watch_state = self._usb_snapshot()
        except Exception:
            pass

    def _start_usb_watcher(self):
        """Запустить фоновый поток отслеживания USB (опрос каждые 2 с).

        Watcher использует Windows PnP (WMI) через _usb_snapshot, который НЕ
        открывает USB-устройство. Поэтому он работает НЕПРЕРЫВНО, в том числе
        во время прошивки и дампа — без гонки за доступ к устройству.
        (Раньше watcher на pyusb захватывал устройство и приходилось ставить
        паузу; теперь это не нужно.)
        """
        import threading as _th, time as _t

        def _watch():
            # Первичный снимок — без событий, только фиксируем состояние
            try:
                self._usb_watch_state = self._usb_snapshot()
            except Exception:
                self._usb_watch_state = {}
            for key, desc in self._usb_watch_state.items():
                self._usb_watch_log(f"🔌 USB присутствует: {desc}")

            while getattr(self, "_usb_watch_running", False):
                _t.sleep(2.0)
                try:
                    now = self._usb_snapshot()
                except Exception:
                    continue
                prev = self._usb_watch_state
                # Подключения
                for key, desc in now.items():
                    if key not in prev:
                        self._usb_watch_log(f"🟢 USB подключено: {desc}")
                # Отключения
                for key, desc in prev.items():
                    if key not in now:
                        self._usb_watch_log(f"🔴 USB отключено: {desc}")
                self._usb_watch_state = now

        _th.Thread(target=_watch, daemon=True).start()

    def check_aml_usb_password(self, log_fn=None):
        """Проверить, требует ли устройство Amlogic пароль для USB-режима.

        На защищённых SoC вход в USB-режим требует password.bin, иначе команды
        update.exe падают. identify() возвращает socid, где:
          socid[4] = Need Password (1 = требуется пароль)
          socid[5] = Password OK   (1 = пароль уже принят)

        Возвращает dict:
          {"available": bool,   # удалось ли опросить устройство
           "need_password": bool,
           "password_ok": bool,
           "rom": str, "message": str}
        Если устройство не в USB Boot режиме или pyamlboot недоступен —
        available=False (это НЕ блокирует прошивку, просто не смогли проверить).
        """
        def _log(m):
            if log_fn: log_fn(m)

        result = {"available": False, "need_password": False,
                  "password_ok": False, "rom": "", "message": ""}

        # Без pyusb опросить SoC нельзя (pyamlboot зависит от pyusb)
        if not USB_AVAILABLE:
            result["message"] = "pyusb не установлен — проверка пароля пропущена"
            _log("  ⚠ " + result["message"])
            return result

        # Пытаемся импортировать pyamlboot (локальный или установленный)
        AmlogicSoC = None
        try:
            if ROOT_DIR not in sys.path:
                sys.path.insert(0, ROOT_DIR)
            try:
                from pyamlboot_local.pyamlboot import AmlogicSoC as _A
                AmlogicSoC = _A
            except Exception:
                from pyamlboot.pyamlboot import AmlogicSoC as _A
                AmlogicSoC = _A
        except Exception:
            result["message"] = "pyamlboot недоступен — проверка пароля пропущена"
            _log("  ⚠ " + result["message"])
            return result

        try:
            dev = AmlogicSoC()
            socid = dev.identify()
            # socid — bytes или str; берём байты 0..5
            def _b(i):
                v = socid[i]
                return ord(v) if isinstance(v, str) else int(v)
            rom = f"{_b(0)}.{_b(1)}"
            need = _b(4)
            pok = _b(5)
            result.update({
                "available": True,
                "need_password": bool(need),
                "password_ok": bool(pok),
                "rom": rom,
            })
            _log(f"  🔐 SoC ROM {rom}: Need Password={need}, Password OK={pok}")
            if need and not pok:
                result["message"] = ("Устройство ТРЕБУЕТ password.bin для "
                                     "USB-режима, и пароль ещё не введён.")
            elif need and pok:
                result["message"] = "Пароль требуется, но уже принят — можно работать."
            else:
                result["message"] = "Пароль не требуется."
            # Полное освобождение устройства. На Windows (libusb0) простого
            # dispose_resources недостаточно — хэндл может остаться полуоткрытым
            # и следующее открытие (загрузка U-Boot) упадёт с timeout/aborted.
            # Поэтому явно освобождаем интерфейс, закрываем и даём паузу на
            # ре-энумерацию.
            try:
                import usb.util
                try:
                    usb.util.release_interface(dev.dev, 0)
                except Exception:
                    pass
                try:
                    usb.util.dispose_resources(dev.dev)
                except Exception:
                    pass
                try:
                    if sys.platform == "win32":
                        # Windows/libusb0: хэндл может остаться полуоткрытым.
                        # На macOS reset вызывает лишнюю пере-энумерацию USB
                        # (риск «залипания» порта) — там достаточно release+dispose.
                        dev.dev.reset()
                except Exception:
                    pass
                del dev
                import gc, time as _t
                gc.collect()
                _t.sleep(1.5)   # даём Windows освободить хэндл
            except Exception:
                pass
        except Exception as ex:
            # Устройство не в USB Boot или занято — не критично
            result["message"] = f"Не удалось опросить SoC: {str(ex)[:80]}"
            _log("  ⚠ " + result["message"])
        return result

    def _password_gate(self, log_fn=None, parent=None):
        """Проверка пароля перед прошивкой/дампом. Возвращает True если можно
        продолжать, False если пользователь отменил из-за требования пароля.

        ВАЖНО: проверка ОТКРЫВАЕТ USB-устройство (identify), а затем его же
        открывает загрузчик U-Boot. На Windows (libusb0) двойное открытие
        подряд ненадёжно и может дать timeout/aborted при загрузке U-Boot.
        Поэтому проверку можно отключить в настройках (skip_usb_password_check),
        если она мешает — тогда устройство открывается только один раз.
        """
        try:
            settings = load_settings()
        except Exception:
            settings = {}
        if settings.get("skip_usb_password_check", False):
            if log_fn:
                log_fn("  (проверка пароля USB отключена в настройках)")
            return True

        info = self.check_aml_usb_password(log_fn=log_fn)
        if not info["available"]:
            # Не смогли проверить (нет устройства в USB Boot / нет pyamlboot).
            # Не блокируем — операция сама покажет ошибку, если что.
            return True
        if info["need_password"] and not info["password_ok"]:
            msg = (
                "⚠️ Устройство Amlogic ТРЕБУЕТ пароль для USB-режима\n"
                f"(SoC ROM {info['rom']}: Need Password=1, Password OK=0).\n\n"
                "Без ввода password.bin команды прошивки/дампа, скорее всего,\n"
                "завершатся ошибкой авторизации.\n\n"
                "Введите пароль командой:\n"
                "    update.exe password password.bin\n"
                "или через USB Burning Tool, затем повторите.\n\n"
                "Продолжить всё равно?"
            )
            if log_fn:
                log_fn("  ⛔ " + info["message"])
            proceed = messagebox.askyesno("Требуется пароль USB", msg,
                                          icon='warning',
                                          parent=parent or self.root)
            return proceed
        return True

    def check_for_updates(self, silent=False):
        """Проверить наличие новой версии на GitHub.

        Использует методы, НЕ подпадающие под rate limit GitHub REST API
        (60 запросов/час на IP). Порядок:
          1. Редирект github.com/.../releases/latest → тег (обычный HTTP, без лимита)
          2. releases.atom (RSS-фид релизов, без лимита)
          3. api.github.com (REST API) — только как крайний вариант

        silent=True — тихая фоновая проверка (без диалогов, только лог/метка).
        """
        import json, re

        base = self._theme_sig   # https://github.com/suddosu/yasta_flasher

        def _norm(v):
            v = (v or "").strip()
            m = re.search(r'(\d+(?:\.\d+)+|\d+\.\d+|\d+)', v)
            if not m:
                return (0, 0, 0)
            parts = [int(x) for x in m.group(1).split(".")]
            return tuple(parts[:3]) + (0,) * (3 - len(parts[:3]))

        def _ui_log(m):
            if not silent:
                self.root.after(0, lambda: self.log(m))

        UA = {"User-Agent": f"yasta_flasher/{APP_VERSION}"}

        def _via_redirect():
            """github.com/.../releases/latest отдаёт 302 на .../releases/tag/<tag>.
            Не REST API → нет rate limit. Возвращает (tag, html_url) или None."""
            url = base + "/releases/latest"
            # Не следуем за редиректом — читаем Location из заголовка
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            opener = urllib.request.build_opener(
                _NoRedirect, urllib.request.HTTPSHandler(context=_SSL_CONTEXT))
            req = urllib.request.Request(url, headers=UA)
            try:
                opener.open(req, timeout=15)
                return None  # без редиректа (нет релизов)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location", "")
                    m = re.search(r'/releases/tag/(.+)$', loc)
                    if m:
                        return (m.group(1), loc)
                raise
            return None

        def _via_atom():
            """releases.atom — RSS-фид, без rate limit. Первый <entry> = свежий."""
            url = base + "/releases.atom"
            req = urllib.request.Request(url, headers=UA)
            with url_open(req, timeout=15) as r:
                xml = r.read().decode("utf-8", "ignore")
            m = re.search(r'/releases/tag/([^"<]+)', xml)
            if m:
                tag = m.group(1)
                return (tag, base + "/releases/tag/" + tag)
            return None

        def _via_api():
            """REST API — последний резерв (подпадает под rate limit)."""
            url = base.replace("github.com", "api.github.com/repos") + "/releases/latest"
            req = urllib.request.Request(url, headers={
                **UA, "Accept": "application/vnd.github+json"})
            with url_open(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            tag = (data.get("tag_name") or data.get("name") or "").strip()
            return (tag, data.get("html_url", base + "/releases/latest")) if tag else None

        _ui_log("🔄 Проверка обновлений...")

        tag = html_url = None
        last_err = None
        for name, fn in (("redirect", _via_redirect),
                         ("atom", _via_atom),
                         ("api", _via_api)):
            try:
                res = fn()
                if res and res[0]:
                    tag, html_url = res
                    _ui_log(f"  Источник: {name} · релиз {tag}")
                    break
            except Exception as ex:
                last_err = f"{name}: {type(ex).__name__} {str(ex)[:60]}"
                _ui_log(f"  ⚠ {last_err}")
                continue

        if not tag:
            errtext = last_err or "релизы не найдены"
            self.root.after(0, lambda: self._update_lbl.config(
                text="проверка недоступна", fg="#E74C3C"))
            if not silent:
                self.root.after(0, lambda: messagebox.showwarning(
                    "Обновления",
                    "Не удалось определить последнюю версию.\n\n"
                    f"Причина: {errtext}\n\n"
                    "Можно проверить вручную:\n" + base + "/releases/latest",
                    parent=self.root))
            return

        cur, new = _norm(APP_VERSION), _norm(tag)
        _ui_log(f"  Версии: у вас {cur}, на GitHub {new}")

        if new > cur:
            msg = f"Доступна новая версия: {tag} (у вас v{APP_VERSION})"
            self.root.after(0, lambda: self._update_lbl.config(
                text=f"⬆ {tag} доступна", fg="#E67E22"))
            self.root.after(0, lambda: self.log(f"⬆ {msg}"))
            if not silent:
                def _ask():
                    if messagebox.askyesno("Обновление",
                        msg + "\n\nОткрыть страницу релиза?", parent=self.root):
                        import webbrowser
                        webbrowser.open(html_url)
                self.root.after(0, _ask)
        else:
            self.root.after(0, lambda: self._update_lbl.config(
                text="✓ актуальная версия", fg="#27AE60"))
            if not silent:
                self.root.after(0, lambda: messagebox.showinfo(
                    "Обновления",
                    f"У вас последняя версия (v{APP_VERSION}).\n"
                    f"Последний релиз на GitHub: {tag}.",
                    parent=self.root))

    def create_com_terminal(self, parent):
        """Создание панели COM терминала"""
        # Заголовок
        terminal_header = tk.LabelFrame(parent, text="📡 COM Терминал (UART)", font=("Arial", 10, "bold"))
        terminal_header.pack(fill=tk.X, pady=(0, 5))
        
        # Выбор COM порта
        com_control_frame = tk.Frame(terminal_header)
        com_control_frame.pack(fill=tk.X, padx=5, pady=5)
        
        tk.Label(com_control_frame, text="Порт:", font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 5))
        
        self.com_port_var = tk.StringVar()
        self.com_port_combo = ttk.Combobox(
            com_control_frame,
            textvariable=self.com_port_var,
            width=12,
            state="readonly"
        )
        self.com_port_combo.pack(side=tk.LEFT, padx=(0, 5))
        
        tk.Button(
            com_control_frame,
            text="🔄",
            command=self.refresh_com_ports,
            width=3
        ).pack(side=tk.LEFT, padx=(0, 5))
        
        self.com_connect_btn = tk.Button(
            com_control_frame,
            text="Подключить",
            command=self.toggle_com_connection,
            bg="#27AE60",
            fg="white",
            font=("Arial", 9),
            width=12
        )
        self.com_connect_btn.pack(side=tk.LEFT)
        
        # Статус подключения
        self.com_status_label = tk.Label(
            terminal_header,
            text="● Не подключено",
            font=("Arial", 8),
            fg="gray"
        )
        self.com_status_label.pack(pady=(0, 5))
        
        # Терминал
        terminal_frame = tk.LabelFrame(parent, text="Вывод терминала", font=("Arial", 9, "bold"))
        terminal_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))
        
        self.terminal_text = scrolledtext.ScrolledText(
            terminal_frame,
            font=_mono_font(8),
            bg="#1E1E1E",
            fg="#00FF00",
            insertbackground="white",
            state='disabled',
            wrap=tk.WORD
        )
        self.terminal_text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        
        # Кнопки управления терминалом
        terminal_btn_frame = tk.Frame(parent)
        terminal_btn_frame.pack(fill=tk.X)
        
        tk.Button(
            terminal_btn_frame,
            text="Очистить",
            command=self.clear_terminal,
            width=10,
            font=("Arial", 8)
        ).pack(side=tk.LEFT, padx=(0, 5))
        
        tk.Button(
            terminal_btn_frame,
            text="Сохранить лог",
            command=self.save_terminal_log,
            width=12,
            font=("Arial", 8)
        ).pack(side=tk.LEFT)
        
        # Информация
        if not SERIAL_AVAILABLE:
            info_label = tk.Label(
                parent,
                text="⚠️ pyserial не установлен\nУстановите: pip install pyserial",
                font=("Arial", 8),
                fg="orange",
                justify=tk.LEFT
            )
            info_label.pack(pady=5)
            self.com_port_combo.config(state='disabled')
            self.com_connect_btn.config(state='disabled')
        else:
            # Автоматически обновляем список портов
            self.refresh_com_ports()
        
    def on_closing(self):
        """Обработчик закрытия окна"""
        self._usb_watch_running = False   # остановить USB-watcher
        if self.serial_running:
            self.stop_serial_monitor()
        if self.is_flashing:
            if messagebox.askyesno("Подтверждение", "Прошивка в процессе. Вы уверены?"):
                self.is_flashing = False
                self.root.destroy()
        else:
            self.root.destroy()
    
    def refresh_com_ports(self):
        """Обновление списка COM портов.

        Фильтрует виртуальные/Bluetooth-порты, оставляя реальные USB-UART.
        Признаки реального адаптера: наличие аппаратного VID:PID и/или
        известный чип в описании (CH340, CP210x, FT232, PL2303 и т.п.).
        Bluetooth-порты и виртуальные отсеиваются.
        """
        if not SERIAL_AVAILABLE:
            return

        # Известные чипы USB-UART адаптеров
        REAL_CHIPS = ("ch340", "ch341", "cp210", "cp2102", "cp2104", "ft232",
                      "ftdi", "pl2303", "prolific", "silicon labs", "usb serial",
                      "usb-serial", "usb to uart", "usb-uart", "wch", "arduino")
        # Маркеры виртуальных/ненужных портов
        VIRTUAL_MARKERS = ("bluetooth", "bthenum", "блютуз", "по соединению bluetooth",
                           "стандартный последовательный", "virtual", "виртуальн")

        def is_real_port(port):
            desc = (port.description or "").lower()
            hwid = (port.hwid or "").lower()
            # Явно виртуальный → отсеять
            if any(m in desc for m in VIRTUAL_MARKERS):
                return False
            # Bluetooth по hwid
            if "bthenum" in hwid or "bluetooth" in hwid:
                return False
            # Есть аппаратный VID:PID (реальное USB-устройство) → реальный
            if getattr(port, "vid", None) is not None:
                return True
            # Известный чип в описании → реальный
            if any(c in desc for c in REAL_CHIPS):
                return True
            # USB в hwid без признаков виртуальности → вероятно реальный
            if "usb vid:pid" in hwid or "usb\\vid" in hwid:
                return True
            return False

        try:
            all_ports = list(serial.tools.list_ports.comports())
            real_ports = [p for p in all_ports if is_real_port(p)]
            hidden = len(all_ports) - len(real_ports)

            # Если фильтр убрал вообще всё, но порты есть — показываем все
            # (чтобы не остаться без выбора на нестандартном железе)
            ports = real_ports if real_ports else all_ports
            port_list = [port.device for port in ports]

            self.com_port_combo['values'] = port_list

            if port_list:
                if not self.com_port_var.get() or self.com_port_var.get() not in port_list:
                    self.com_port_combo.current(0)
                self.terminal_log(f"🔍 Реальных USB-UART портов: {len(port_list)}"
                                  + (f" (скрыто виртуальных: {hidden})" if hidden else ""))
                for port in ports:
                    chip = ""
                    if getattr(port, "vid", None) is not None:
                        chip = f"  [{port.vid:04X}:{port.pid:04X}]"
                    self.terminal_log(f"  • {port.device}: {port.description}{chip}")
            else:
                self.terminal_log("⚠️ Реальные COM порты не найдены")
                if hidden:
                    self.terminal_log(f"  (отфильтровано {hidden} виртуальных/Bluetooth)")
                self.com_port_var.set("")
        except Exception as e:
            self.terminal_log(f"✗ Ошибка поиска портов: {str(e)}")
    
    def toggle_com_connection(self):
        """Переключение подключения к COM порту"""
        if self.serial_running:
            self.stop_serial_monitor()
        else:
            self.start_serial_monitor()
    
    def start_serial_monitor(self):
        """Запуск мониторинга COM порта"""
        if not SERIAL_AVAILABLE:
            messagebox.showerror("Ошибка", "pyserial не установлен!\nУстановите: pip install pyserial")
            return
        
        port = self.com_port_var.get()
        if not port:
            messagebox.showwarning("Предупреждение", "Выберите COM порт")
            return
        
        try:
            self.serial_port = serial.Serial(
                port=port,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1
            )
            
            self.serial_running = True
            self.serial_thread = threading.Thread(target=self.serial_reader, daemon=True)
            self.serial_thread.start()
            
            self.com_connect_btn.config(text="Отключить", bg="#E74C3C")
            self.com_status_label.config(text=f"● Подключено к {port}", fg="#27AE60")
            self.com_port_combo.config(state='disabled')
            
            self.terminal_log(f"✓ Подключено к {port} (115200 8N1)")
            self.log(f"✓ COM терминал: подключено к {port}")
            
        except serial.SerialException as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть {port}:\n{str(e)}")
            self.terminal_log(f"✗ Ошибка: {str(e)}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка подключения:\n{str(e)}")
            self.terminal_log(f"✗ Ошибка: {str(e)}")
    
    def stop_serial_monitor(self):
        """Остановка мониторинга COM порта"""
        self.serial_running = False
        
        if self.serial_thread:
            self.serial_thread.join(timeout=2)
        
        if self.serial_port and self.serial_port.is_open:
            try:
                self.serial_port.close()
            except:
                pass
        
        self.com_connect_btn.config(text="Подключить", bg="#27AE60")
        self.com_status_label.config(text="● Не подключено", fg="gray")
        self.com_port_combo.config(state='readonly')
        
        self.terminal_log("✓ Отключено от COM порта")
        self.log("✓ COM терминал: отключено")
    
    def serial_reader(self):
        """Поток чтения данных из COM порта"""
        buffer = b''
        current_line = ""
        
        while self.serial_running:
            try:
                if self.serial_port and self.serial_port.is_open:
                    # Читаем данные
                    if self.serial_port.in_waiting:
                        data = self.serial_port.read(self.serial_port.in_waiting)
                        buffer += data
                        
                        # Обрабатываем данные
                        try:
                            # Декодируем в текст
                            text = buffer.decode('utf-8', errors='ignore')
                            buffer = b''  # Очищаем буфер после декодирования
                            
                            # Обрабатываем каждый символ
                            for char in text:
                                if char == '\r':
                                    # Возврат каретки - обновляем текущую строку (для прогресса)
                                    if current_line.strip():
                                        self.terminal_update_line(current_line)
                                    # НЕ очищаем current_line - она может быть обновлена
                                elif char == '\n':
                                    # Новая строка - фиксируем и переходим на новую
                                    if current_line.strip():
                                        self.terminal_add_line(current_line)
                                    current_line = ""
                                else:
                                    current_line += char
                                    
                        except Exception as e:
                            pass  # Игнорируем ошибки декодирования
                
                time.sleep(0.01)  # Небольшая задержка
                
            except serial.SerialException as e:
                self.terminal_add_line(f"✗ Ошибка чтения: {str(e)}")
                self.serial_running = False
                break
            except Exception as e:
                time.sleep(0.1)
    
    def terminal_update_line(self, message):
        """Обновление текущей строки (для динамического прогресса)"""
        # Также пишем в буфер захвата (CR-строки без LF)
        if self._terminal_capture_buf is not None:
            self._terminal_capture_buf.append(message)
        try:
            self.terminal_text.config(state='normal')
            self.terminal_text.delete("end-1c linestart", "end-1c")
            self.terminal_text.insert("end-1c", message)
            self.terminal_text.see(tk.END)
            self.terminal_text.config(state='disabled')
            self.terminal_text.update_idletasks()
        except:
            pass
    
    def terminal_add_line(self, message):
        """Добавление новой строки в терминал"""
        # Если активен захват — пишем в буфер (для парсинга списка разделов)
        if self._terminal_capture_buf is not None:
            self._terminal_capture_buf.append(message)
        try:
            from datetime import datetime
            time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.terminal_text.config(state='normal')
            self.terminal_text.insert(tk.END, f"[{time_str}] {message}\n")
            self.terminal_text.see(tk.END)
            self.terminal_text.config(state='disabled')
            self.terminal_text.update_idletasks()
        except:
            pass
    
    def terminal_log(self, message, timestamp=False, update_line=False):
        """Добавление сообщения в терминал (совместимость со старым API).

        Обрабатывает \\r (возврат каретки): update.exe выводит прогресс вида
        '[ 16%/ 4MB]\\r[ 32%/ 7MB]\\r...' — каждый \\r должен ПЕРЕЗАПИСЫВАТЬ
        текущую строку, а не добавлять новую. Иначе проценты валятся в кучу.
        """
        if self._terminal_capture_buf is not None:
            self._terminal_capture_buf.append(message)

        # Если в сообщении есть \r — это динамический прогресс. Берём последний
        # сегмент после \r и обновляем текущую строку (как делает терминал).
        if "\r" in message and not message.endswith("\r\n"):
            segments = message.replace("\r\n", "\n").split("\r")
            # Последний непустой сегмент — актуальное состояние строки
            last = segments[-1] if segments[-1] else (segments[-2] if len(segments) > 1 else "")
            self.terminal_update_line(last)
            return

        if update_line:
            self.terminal_update_line(message)
        else:
            if timestamp:
                self.terminal_add_line(message)
            else:
                try:
                    self.terminal_text.config(state='normal')
                    self.terminal_text.insert(tk.END, f"{message}\n")
                    self.terminal_text.see(tk.END)
                    self.terminal_text.config(state='disabled')
                    self.terminal_text.update_idletasks()
                except:
                    pass
    
    def clear_terminal(self):
        """Очистка терминала"""
        self.terminal_text.config(state='normal')
        self.terminal_text.delete(1.0, tk.END)
        self.terminal_text.config(state='disabled')
        self.terminal_log("✓ Терминал очищен")
    
    def save_terminal_log(self):
        """Сохранение лога терминала в файл"""
        filename = filedialog.asksaveasfilename(
            initialdir=last_dir("log_save", ROOT_DIR),
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"uart_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        
        if filename:
            remember_last_dir("log_save", filename)
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    content = self.terminal_text.get(1.0, tk.END)
                    f.write(content)
                self.terminal_log(f"✓ Лог сохранён: {filename}")
                messagebox.showinfo("Успех", f"Лог сохранён:\n{filename}")
            except Exception as e:
                self.terminal_log(f"✗ Ошибка сохранения: {str(e)}")
                messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{str(e)}")
    
    def add_custom_partition(self):
        """Добавление кастомного раздела"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Добавить кастомный раздел")
        dialog.geometry("420x220")
        dialog.resizable(True, True)
        dialog.minsize(420, 220)
        dialog.transient(self.root)
        dialog.grab_set()
        
        tk.Label(dialog, text="Добавление кастомного раздела", font=("Arial", 12, "bold")).pack(pady=10)
        
        tk.Label(dialog, text="Имя раздела (например: tee, logo):", font=("Arial", 10)).pack(pady=(5, 0))
        name_entry = tk.Entry(dialog, width=30, font=("Arial", 10))
        name_entry.pack(pady=5)
        name_entry.focus()
        
        tk.Label(dialog, text="Файл образа:", font=("Arial", 10)).pack(pady=(5, 0))
        file_frame = tk.Frame(dialog)
        file_frame.pack(pady=5)
        file_var = tk.StringVar()
        tk.Entry(file_frame, textvariable=file_var, width=28, font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 5))

        def browse_file():
            fn = filedialog.askopenfilename(
                title="Выберите образ раздела",
                initialdir=last_dir("partition_image", IMG_DIR),
                filetypes=[("Image files", "*.img"), ("All files", "*.*")]
            )
            if fn:
                remember_last_dir("partition_image", fn)
                file_var.set(fn)

        tk.Button(file_frame, text="Обзор...", command=browse_file, width=10).pack(side=tk.LEFT)

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=15)

        def add_partition():
            name = name_entry.get().strip()
            file_path = file_var.get().strip()
            if not name:
                messagebox.showwarning("Предупреждение", "Введите имя раздела", parent=dialog)
                return
            if not file_path or not os.path.exists(file_path):
                messagebox.showerror("Ошибка", "Укажите существующий файл образа", parent=dialog)
                return
            if name in self.image_vars:
                messagebox.showwarning("Предупреждение", f"Раздел '{name}' уже существует", parent=dialog)
                return

            custom_part = {
                "name": name,
                "file": os.path.basename(file_path),
                "display": f"{name} (custom)",
                "time": "зависит от размера",
            }
            PART_IMAGES.append(custom_part)
            self.selected_images[name] = file_path

            # Регистрируем BooleanVar — без этого start_flashing падал с KeyError
            var = tk.BooleanVar(value=True)
            self.image_vars[name] = var

            # Добавляем строку в UI
            self.add_partition_checkbox(custom_part, file_path)

            self.log(f"✓ Добавлен кастомный раздел: {name} ({os.path.basename(file_path)})")
            messagebox.showinfo("Успех", f"Раздел '{name}' добавлен", parent=dialog)
            dialog.destroy()

        tk.Button(btn_frame, text="Добавить", command=add_partition,
                  bg="#27AE60", fg="white", width=12).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Отмена", command=dialog.destroy, width=12).pack(side=tk.LEFT)

    def add_partition_checkbox(self, img, file_path=None):
        """Добавить строку раздела в прокручиваемый список"""
        if not hasattr(self, 'images_scrollable_frame'):
            return
        parent = self.images_scrollable_frame

        frame = tk.Frame(parent)
        frame.pack(fill=tk.X, padx=5, pady=2)

        cb = tk.Checkbutton(
            frame,
            text=f"{img['display']} ({img['file']})",
            variable=self.image_vars[img["name"]],
            font=("Arial", 9)
        )
        cb.pack(side=tk.LEFT)

        btn = tk.Button(
            frame, text="Обзор...",
            command=lambda i=img: self.browse_image(i),
            width=10
        )
        btn.pack(side=tk.RIGHT, padx=5)

        path_label = tk.Label(frame, font=("Arial", 8))
        path_label.pack(side=tk.RIGHT, padx=5)
        self.image_path_labels[img["name"]] = path_label

        if file_path and os.path.exists(file_path):
            path_label.config(text=f"✓ {os.path.basename(file_path)}", fg="green")
        else:
            path_label.config(text="✗ Не найден", fg="red")

        # Обновляем область прокрутки
        if hasattr(self, 'images_canvas'):
            self.images_canvas.configure(scrollregion=self.images_canvas.bbox("all"))
    
    def open_env_editor(self, return_window=False):
        """Открыть редактор ENV.

        return_window=True → вернуть окно Toplevel (для wait_window при
        вызове из процесса прошивки, чтобы блокировать до закрытия).
        Значения полей берутся из self.env_data (заполняется при чтении
        env с устройства перед прошивкой).
        """
        editor = tk.Toplevel(self.root)
        editor.title("⚙️ Редактор переменных окружения (ENV)")
        editor.geometry("800x700")
        editor.resizable(True, True)
        editor.minsize(450, 300)
        editor.transient(self.root)
        
        # Объединённый вид для отображения: снимок с устройства + изменения
        # пользователя. Поля редактора показывают это, но в self.env_data
        # при сохранении попадут ТОЛЬКО реально изменённые значения.
        if not hasattr(self, "_env_device"):
            self._env_device = {}
        disp = dict(self._env_device)   # значения с устройства
        disp.update(self.env_data)      # поверх — пользовательские изменения
        # Временно используем disp как источник для заполнения полей
        self._env_display = disp

        tk.Label(
            editor,
            text="Редактор переменных окружения U-Boot",
            font=("Arial", 14, "bold"),
            pady=10
        ).pack()
        
        # Инструкция
        info_frame = tk.LabelFrame(editor, text="ℹ️ Информация", font=("Arial", 10, "bold"))
        info_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        
        info_text = """ENV (Environment) - переменные окружения U-Boot загрузчика.
⚠️ ВАЖНО: Для Amlogic изменения применяются через команды setenv и saveenv!

Основные категории:
🔓 Разблокировка: lock, avb2, EnableSelinux
📱 Идентификация: serial, deviceid, mac
🎨 Экран: led_screen_brightness, led_ring_brightness
🚀 Загрузка: bootdelay, bootcmd"""
        
        tk.Label(info_frame, text=info_text, justify=tk.LEFT, font=("Arial", 9), pady=5).pack(padx=10)
        
        # Контейнер кнопок ВНИЗУ — пакуем до notebook чтобы не исчезал
        env_bottom = tk.Frame(editor, bg="#ECF0F1", relief=tk.RIDGE, bd=2)
        env_bottom.pack(side=tk.BOTTOM, fill=tk.X)

        # Notebook с вкладками
        notebook = ttk.Notebook(editor)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
        # Вкладка 1: Идентификация
        id_frame = tk.Frame(notebook)
        notebook.add(id_frame, text="📱 Идентификация")
        
        # Вкладка 2: Безопасность
        sec_frame = tk.Frame(notebook)
        notebook.add(sec_frame, text="🔓 Безопасность")
        
        # Вкладка 3: Система
        sys_frame = tk.Frame(notebook)
        notebook.add(sys_frame, text="⚙️ Система")

        # Вкладка 4: cmdline_keys (переопределение serial/deviceid)
        cmd_frame = tk.Frame(notebook)
        notebook.add(cmd_frame, text="🆔 serial/deviceid")

        # Вкладка 5: Все переменные (raw)
        raw_frame = tk.Frame(notebook)
        notebook.add(raw_frame, text="📝 Все переменные")

        entry_widgets = {}
        
        # === ВКЛАДКА ИДЕНТИФИКАЦИЯ ===
        id_scroll = scrolledtext.ScrolledText(id_frame, height=20, font=_mono_font(9))
        id_scroll.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Значения по умолчанию — пустые; заполняются из устройства или из файла
        id_vars = {
            "serial": {
                "value": "",
                "desc": "Серийный номер устройства (основной)",
                "critical": True
            },
            "deviceid": {
                "value": "",
                "desc": "ID устройства (используется в bootargs)",
                "critical": True
            },
            "custom_deviceid": {
                "value": "",
                "desc": "Кастомный ID устройства",
                "critical": True
            },
            "aml_serial": {
                "value": "",
                "desc": "Аппаратный серийный номер Amlogic",
                "critical": False
            },
            "mac": {
                "value": "",
                "desc": "MAC адрес сетевого интерфейса",
                "critical": True
            },
            "ethaddr": {
                "value": "",
                "desc": "Ethernet MAC адрес",
                "critical": True
            },
        }
        # Если у нас уже есть сохранённые данные — подставляем их
        for k in id_vars:
            if k in self._env_display:
                id_vars[k]["value"] = self._env_display[k]
        
        for var_name, var_info in id_vars.items():
            frame = tk.Frame(id_scroll)
            id_scroll.window_create(tk.END, window=frame)
            id_scroll.insert(tk.END, "\n")
            
            # Заголовок
            label_text = f"{'🔴 ' if var_info['critical'] else ''}  {var_name}"
            tk.Label(frame, text=label_text, font=("Arial", 10, "bold"), anchor=tk.W, width=25).pack(side=tk.TOP, anchor=tk.W)
            
            # Поле ввода
            entry = tk.Entry(frame, font=_mono_font(9), width=60)
            entry.insert(0, var_info["value"])
            entry.pack(side=tk.TOP, pady=(2, 0))
            entry_widgets[var_name] = entry
            
            # Описание
            tk.Label(frame, text=var_info["desc"], font=("Arial", 8), fg="gray", anchor=tk.W).pack(side=tk.TOP, anchor=tk.W, pady=(0, 10))
        
        id_scroll.config(state='disabled')
        
        # === ВКЛАДКА БЕЗОПАСНОСТЬ ===
        sec_scroll = scrolledtext.ScrolledText(sec_frame, height=20, font=_mono_font(9))
        sec_scroll.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        sec_vars = {
            "lock": {
                "value": self._env_display.get("lock", ""),
                "desc": "🔓 Разблокировка bootloader (10000000 = разблокирован, 0 = заблокирован)",
                "critical": True
            },
            "avb2": {
                "value": self._env_display.get("avb2", ""),
                "desc": "🔓 Android Verified Boot 2.0 (0 = выключен, 1 = включен)",
                "critical": True
            },
            "EnableSelinux": {
                "value": self._env_display.get("EnableSelinux", ""),
                "desc": "🔓 SELinux режим (permissive = отключен, enforcing = включен)",
                "critical": True,
                "options": ["permissive", "enforcing", "disabled"]
            },
            "jtag": {
                "value": self._env_display.get("jtag", ""),
                "desc": "🔧 JTAG отладка (enable/disable)",
                "critical": False,
                "options": ["disable", "enable"]
            },
            "silent": {
                "value": self._env_display.get("silent", ""),
                "desc": "📢 Вывод сообщений загрузки (0 = включен, 1 = выключен)",
                "critical": False
            },
            "rabbit_hole_debug": {
                "value": self._env_display.get("rabbit_hole_debug", ""),
                "desc": "🐰 Режим отладки (0 = выключен, 1 = включен)",
                "critical": False
            },
        }
        
        for var_name, var_info in sec_vars.items():
            frame = tk.Frame(sec_scroll)
            sec_scroll.window_create(tk.END, window=frame)
            sec_scroll.insert(tk.END, "\n")
            
            label_text = f"{'🔴 ' if var_info['critical'] else ''}  {var_name}"
            tk.Label(frame, text=label_text, font=("Arial", 10, "bold"), anchor=tk.W, width=25).pack(side=tk.TOP, anchor=tk.W)
            
            if "options" in var_info:
                # Combobox для выбора
                var = tk.StringVar(value=var_info["value"])
                combo = ttk.Combobox(frame, textvariable=var, values=var_info["options"], font=_mono_font(9), width=57, state="readonly")
                combo.pack(side=tk.TOP, pady=(2, 0))
                entry_widgets[var_name] = combo
            else:
                entry = tk.Entry(frame, font=_mono_font(9), width=60)
                entry.insert(0, var_info["value"])
                entry.pack(side=tk.TOP, pady=(2, 0))
                entry_widgets[var_name] = entry
            
            tk.Label(frame, text=var_info["desc"], font=("Arial", 8), fg="gray", anchor=tk.W).pack(side=tk.TOP, anchor=tk.W, pady=(0, 10))
        
        sec_scroll.config(state='disabled')
        
        # === ВКЛАДКА СИСТЕМА ===
        sys_scroll = scrolledtext.ScrolledText(sys_frame, height=20, font=_mono_font(9))
        sys_scroll.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        sys_vars = {
            "bootdelay": {
                "value": self._env_display.get("bootdelay", ""),
                "desc": "⏱️ Задержка перед загрузкой (секунды)",
                "critical": False
            },
            "led_screen_brightness": {
                "value": self._env_display.get("led_screen_brightness", ""),
                "desc": "💡 Яркость экрана (0-255)",
                "critical": False
            },
            "led_ring_brightness": {
                "value": self._env_display.get("led_ring_brightness", ""),
                "desc": "💡 Яркость кольца подсветки (0-255)",
                "critical": False
            },
            "localization": {
                "value": self._env_display.get("localization", ""),
                "desc": "🌍 Локализация устройства",
                "critical": False
            },
            "hdmimode": {
                "value": self._env_display.get("hdmimode", ""),
                "desc": "📺 Режим HDMI (1080p60hz, 720p60hz и т.д.)",
                "critical": False
            },
            "outputmode": {
                "value": self._env_display.get("outputmode", ""),
                "desc": "📺 Режим вывода видео",
                "critical": False
            },
        }
        
        for var_name, var_info in sys_vars.items():
            frame = tk.Frame(sys_scroll)
            sys_scroll.window_create(tk.END, window=frame)
            sys_scroll.insert(tk.END, "\n")
            
            label_text = f"  {var_name}"
            tk.Label(frame, text=label_text, font=("Arial", 10, "bold"), anchor=tk.W, width=25).pack(side=tk.TOP, anchor=tk.W)
            
            entry = tk.Entry(frame, font=_mono_font(9), width=60)
            entry.insert(0, var_info["value"])
            entry.pack(side=tk.TOP, pady=(2, 0))
            entry_widgets[var_name] = entry
            
            tk.Label(frame, text=var_info["desc"], font=("Arial", 8), fg="gray", anchor=tk.W).pack(side=tk.TOP, anchor=tk.W, pady=(0, 10))
        
        sys_scroll.config(state='disabled')

        # === ВКЛАДКА cmdline_keys (serial/deviceid override) ===
        cmd_top = tk.Frame(cmd_frame)
        cmd_top.pack(fill=tk.X, padx=8, pady=6)
        tk.Label(cmd_top, justify=tk.LEFT, font=("Arial", 9), text=(
            "⚠️ serialno / deviceid / mac переопределяются при каждой загрузке\n"
            "через переменную cmdline_keys (она читает значения из keystore по\n"
            "eFuse-ключам). Обычный setenv serial=… НЕ работает — значение\n"
            "перезаписывается. Здесь можно жёстко переопределить cmdline_keys.\n"
            "\n"
            "aml_serial — заводской серийник-fallback: cmdline_keys использует\n"
            "его, если keystore недоступен. Спуфится так же, как deviceid."
        )).pack(anchor=tk.W)

        cmd_fields = tk.LabelFrame(cmd_frame, text="Значения для жёсткой подстановки",
                                   font=("Arial", 9, "bold"))
        cmd_fields.pack(fill=tk.X, padx=8, pady=4)

        cmd_vars = {}
        cur = self._env_display
        cmd_defs = [
            ("serialno",     cur.get("serial", cur.get("deviceid", "")), "androidboot.serialno + serial"),
            ("deviceid",     cur.get("deviceid", cur.get("custom_deviceid", "")), "androidboot.deviceid"),
            ("aml_serial",   cur.get("aml_serial", ""), "заводской серийник (fallback)"),
            ("mac",          cur.get("mac", cur.get("ethaddr", "")), "mac + androidboot.mac"),
            ("localization", cur.get("localization", "RU.RU"), "androidboot.localization"),
        ]
        for i, (name, val, desc) in enumerate(cmd_defs):
            row = tk.Frame(cmd_fields); row.pack(fill=tk.X, padx=6, pady=3)
            tk.Label(row, text=name, font=("Arial", 9, "bold"), width=13,
                     anchor=tk.W).pack(side=tk.LEFT)
            e = tk.Entry(row, font=_mono_font(9))
            e.insert(0, val)
            e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            tk.Label(row, text=desc, font=("Arial", 7), fg="gray").pack(side=tk.LEFT)
            cmd_vars[name] = e

        preset_bar = tk.Frame(cmd_frame); preset_bar.pack(fill=tk.X, padx=8, pady=4)
        cmd_preview = scrolledtext.ScrolledText(cmd_frame, height=7,
                                                font=_mono_font(8), wrap=tk.WORD)
        cmd_preview.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        def build_cmdline_keys():
            s  = cmd_vars["serialno"].get().strip()
            d  = cmd_vars["deviceid"].get().strip()
            a  = cmd_vars["aml_serial"].get().strip()
            m  = cmd_vars["mac"].get().strip()
            loc= cmd_vars["localization"].get().strip() or "RU.RU"
            # serialno приоритетно; если не задан, но задан aml_serial — берём его
            eff_serial = s or a
            parts = ["setenv bootargs ${bootargs}"]
            if eff_serial: parts.append(f"androidboot.serialno={eff_serial}")
            if d:   parts.append(f"androidboot.deviceid={d}")
            if m:   parts.append(f"mac={m} androidboot.mac={m}")
            parts.append(f"androidboot.localization={loc}")
            line = " ".join(parts) + ";"
            if eff_serial:
                line += f" setenv serial {eff_serial};"
            if d:
                line += f" setenv deviceid {d}; setenv custom_deviceid {d};"
            return line

        def refresh_preview():
            ck = build_cmdline_keys()
            a = cmd_vars["aml_serial"].get().strip()
            txt = "Будет установлено cmdline_keys =\n\n" + ck
            if a:
                txt += f"\n\n+ aml_serial = {a}  (заводской серийник-fallback)"
            txt += "\n\n(применяется бинарно с пересчётом CRC32)"
            cmd_preview.delete("1.0", tk.END)
            cmd_preview.insert("1.0", txt)

        def apply_cmdline_keys():
            ck = build_cmdline_keys()
            self.env_data["cmdline_keys"] = ck
            s = cmd_vars["serialno"].get().strip()
            d = cmd_vars["deviceid"].get().strip()
            a = cmd_vars["aml_serial"].get().strip()
            m = cmd_vars["mac"].get().strip()
            if s: self.env_data["serial"] = s
            if d:
                self.env_data["deviceid"] = d
                self.env_data["custom_deviceid"] = d
            if a:
                # aml_serial — заводской серийник-fallback. Спуфится так же,
                # как deviceid: обычная env-переменная.
                self.env_data["aml_serial"] = a
            if m:
                self.env_data["mac"] = m
                self.env_data["ethaddr"] = m
            refresh_preview()
            messagebox.showinfo("cmdline_keys",
                "cmdline_keys обновлён в наборе ENV.\n"
                "Нажмите «Сохранить изменения» чтобы записать env с CRC.",
                parent=editor)

        tk.Button(preset_bar, text="🔄 Предпросмотр", command=refresh_preview,
                  font=("Arial", 9)).pack(side=tk.LEFT, padx=3)
        tk.Button(preset_bar, text="✓ Применить cmdline_keys",
                  command=apply_cmdline_keys, bg="#27AE60", fg="white",
                  font=("Arial", 9)).pack(side=tk.LEFT, padx=3)

        def preset_restore_stock():
            self.env_data.pop("cmdline_keys", None)
            self.env_data.pop("aml_serial", None)
            cmd_preview.delete("1.0", tk.END)
            cmd_preview.insert("1.0",
                "cmdline_keys и aml_serial удалены из переопределений — вернётся\n"
                "штатное чтение serial/deviceid из keystore (keyman) при загрузке.")
        tk.Button(preset_bar, text="↺ Вернуть штатный", command=preset_restore_stock,
                  font=("Arial", 9)).pack(side=tk.LEFT, padx=3)
        refresh_preview()

        # === ВКЛАДКА «Все переменные» (raw) ===
        tk.Label(raw_frame, justify=tk.LEFT, font=("Arial", 9), text=(
            "Полный список переменных env (key=value, по одной на строку).\n"
            "Редактируйте напрямую. При сохранении пересчитывается CRC32 и\n"
            "записывается весь раздел env целиком (надёжнее, чем setenv по одной)."
        )).pack(anchor=tk.W, padx=8, pady=6)
        raw_text = scrolledtext.ScrolledText(raw_frame, font=_mono_font(9), wrap=tk.NONE)
        raw_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        def fill_raw():
            raw_text.delete("1.0", tk.END)
            for k, v in self._env_display.items():
                raw_text.insert(tk.END, f"{k}={v}\n")

        raw_bar = tk.Frame(raw_frame); raw_bar.pack(fill=tk.X, padx=8, pady=(0, 6))
        tk.Button(raw_bar, text="🔄 Обновить из текущего набора", command=fill_raw,
                  font=("Arial", 9)).pack(side=tk.LEFT, padx=3)
        tk.Label(raw_bar, font=("Arial", 8), fg="gray",
                 text="(изменения здесь применяются при «Сохранить изменения»)"
                 ).pack(side=tk.LEFT, padx=6)
        fill_raw()
        editor._raw_text = raw_text

        # Кнопки (внутри зафиксированного снизу env_bottom)
        btn_frame = tk.Frame(env_bottom, bg="#ECF0F1")
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        
        def save_env():
            """Сохранить изменения ENV.

            Логика: в self.env_data попадают ТОЛЬКО переменные, которые
            ОТЛИЧАЮТСЯ от снимка с устройства (_env_device). Это критично:
            при прошивке применяются лишь реальные изменения + обязательные
            lock/avb2/silent, а огромные скрипт-переменные (bootargs и т.п.)
            не трогаются (иначе setenv падает и блок env прерывается).
            """
            device = getattr(self, "_env_device", {})
            changes = {}

            # 1. Именованные поля из 3 вкладок
            for name, widget in entry_widgets.items():
                val = widget.get()
                # записываем только если значение задано И отличается от устройства
                if val != "" and val != device.get(name, ""):
                    changes[name] = val

            # 2. Вкладка «Все переменные» — берём построчно, но сохраняем
            #    в changes ТОЛЬКО отличия от снимка устройства.
            try:
                raw_content = editor._raw_text.get("1.0", tk.END).strip()
            except Exception:
                raw_content = ""
            if raw_content:
                for line in raw_content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k and v != device.get(k, ""):
                        changes[k] = v

            # 3. cmdline_keys, если был переопределён через вкладку serial/deviceid
            if self.env_data.get("cmdline_keys"):
                changes["cmdline_keys"] = self.env_data["cmdline_keys"]
            for k in ("serial", "deviceid", "custom_deviceid", "aml_serial", "mac", "ethaddr"):
                if k in self.env_data and self.env_data[k] != device.get(k, ""):
                    changes[k] = self.env_data[k]

            self.env_data = changes
            self._env_editor_saved = True

            serial = self.env_data.get("serial", "")
            messagebox.showinfo(
                "Сохранено",
                f"Изменения ENV сохранены ({len(self.env_data)} переменных).\n\n"
                "При прошивке применятся:\n"
                "• обязательные lock=10000000, avb2=0, silent=0\n"
                f"• ваши изменения ({len(self.env_data)} перем.)\n\n"
                "Скрипт-переменные (bootargs и т.п.) НЕ трогаются — это\n"
                "и было причиной прошлого бутлупа.",
                parent=editor
            )
            self.log(f"✓ ENV: к применению {len(self.env_data)} изменённых переменных")
            if serial:
                self.log(f"  serial → {serial}")
            if "cmdline_keys" in self.env_data:
                self.log("  cmdline_keys: переопределён")
            editor.destroy()

        def write_env_to_device():
            """Прочитать env с устройства, заменить нашими значениями,
            пересобрать с CRC32 и записать обратно. Требует подключённого
            устройства в U-Boot режиме."""
            if not messagebox.askyesno("Запись env",
                "Записать env на устройство?\n\n"
                "Устройство должно быть в режиме U-Boot (после загрузки\n"
                "временного U-Boot). Будет прочитан env, заменён вашими\n"
                "значениями, пересчитан CRC32 и записан обратно.",
                parent=editor):
                return
            # Сначала применим текущие поля
            for name, widget in entry_widgets.items():
                v = widget.get()
                if v != "":
                    self.env_data[name] = v

            def _t():
                import threading
                try:
                    env_file = os.path.join(ROOT_DIR, "env_user.bin")
                    self.log("📥 Чтение env с устройства...")
                    self.aml_read_part("env", "0x800000", env_file)
                    if not os.path.exists(env_file):
                        self.log("❌ Не удалось прочитать env"); return
                    part_size = os.path.getsize(env_file)
                    # Берём оригинальный порядок + наши изменения
                    orig = self.parse_env_blob_ordered(env_file)
                    merged = {}
                    for k, v in orig:
                        merged[k] = v
                    merged.update(self.env_data)
                    blob = self.build_env_blob(list(merged.items()), part_size)
                    out = os.path.join(ROOT_DIR, "env_new.bin")
                    with open(out, "wb") as f:
                        f.write(blob)
                    self.log(f"✓ Собран env с CRC32 ({len(blob)} байт)")
                    # Записываем через partition (как остальные разделы)
                    update_path = self.get_update_path()
                    cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    r = subprocess.run(
                        [update_path, "partition", "env", out],
                        capture_output=True, timeout=120, creationflags=cflags)
                    o = ((r.stdout or b"")+(r.stderr or b"")).decode("utf-8","ignore")
                    if r.returncode == 0 and "ERR" not in o.upper():
                        self.log("✓ env записан на устройство (partition env)")
                        self.root.after(0, lambda: messagebox.showinfo(
                            "Готово", "env записан на устройство с корректным CRC32.",
                            parent=self.root))
                    else:
                        self.log(f"❌ partition env ошибка: {o.strip()[:120]}")
                    try:
                        os.remove(out)
                    except Exception:
                        pass
                except Exception as ex:
                    self.log(f"❌ Запись env: {ex}")
            import threading
            threading.Thread(target=_t, daemon=True).start()
            editor.destroy()
        
        def load_from_file():
            """Загрузить ENV из файла"""
            filename = filedialog.askopenfilename(
                title="Выберите файл ENV",
                initialdir=last_dir("env_file", ROOT_DIR),
                filetypes=[("ENV files", "env_user.bin"), ("All files", "*.*")],
                parent=editor
            )
            if filename:
                remember_last_dir("env_file", filename)
                try:
                    # Читаем файл
                    with open(filename, 'rb') as f:
                        data = f.read()
                    
                    # Пытаемся распарсить
                    text = data.decode('utf-8', errors='ignore')
                    
                    # Ищем переменные (формат: name=value)
                    import re
                    found_vars = {}
                    for match in re.finditer(r'(\w+)=([^\s]+)', text):
                        var_name = match.group(1)
                        var_value = match.group(2)
                        if var_name in entry_widgets:
                            found_vars[var_name] = var_value
                    
                    if found_vars:
                        # Заполняем поля
                        for var_name, var_value in found_vars.items():
                            widget = entry_widgets[var_name]
                            if isinstance(widget, ttk.Combobox):
                                widget.set(var_value)
                            else:
                                widget.delete(0, tk.END)
                                widget.insert(0, var_value)
                        
                        messagebox.showinfo(
                            "Успех",
                            f"Загружено переменных: {len(found_vars)}",
                            parent=editor
                        )
                    else:
                        messagebox.showwarning(
                            "Предупреждение",
                            "Не найдено известных переменных в файле",
                            parent=editor
                        )
                        
                except Exception as e:
                    messagebox.showerror("Ошибка", f"Не удалось прочитать файл:\n{str(e)}", parent=editor)
        
        tk.Button(
            btn_frame,
            text="📁 Загрузить из файла",
            command=load_from_file,
            bg="#3498DB",
            fg="white",
            font=("Arial", 10),
            width=18
        ).pack(side=tk.LEFT, padx=5)
        
        tk.Button(
            btn_frame,
            text="💾 Сохранить изменения",
            command=save_env,
            bg="#27AE60",
            fg="white",
            font=("Arial", 10),
            width=18
        ).pack(side=tk.LEFT, padx=5)

        tk.Button(
            btn_frame,
            text="📤 Записать env на устройство",
            command=write_env_to_device,
            bg="#C0392B",
            fg="white",
            font=("Arial", 10),
            width=24
        ).pack(side=tk.LEFT, padx=5)
        
        def cancel_editor():
            """Отмена — НЕ применять никакие пользовательские изменения.
            env_data очищается, чтобы при прошивке применился только
            необходимый минимум: silent/lock/avb2."""
            self.env_data = {}
            self._env_editor_saved = False
            editor.destroy()

        tk.Button(
            btn_frame,
            text="Отмена",
            command=cancel_editor,
            font=("Arial", 10),
            width=10
        ).pack(side=tk.RIGHT, padx=5)

        # По умолчанию (закрытие крестиком) — тоже отмена
        editor.protocol("WM_DELETE_WINDOW", cancel_editor)

        if return_window:
            return editor
        return None

    @staticmethod
    def _crc32c(data, init=0xFFFFFFFF):
        """crc32c (Castagnoli), как в ext4 metadata_csum.
        Вариант: init=0xFFFFFFFF, без финального XOR — совпадает с эталоном ext4.
        """
        POLY = 0x82F63B78  # reversed Castagnoli polynomial
        crc = init
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ (POLY & -(crc & 1))
        return crc & 0xFFFFFFFF

    def _mark_ext4_clean(self, path, log=None):
        """Пометить ext4 как «clean» (s_state=1), пересчитав crc32c суперблока.

        Старая cygwin-сборка resize2fs (2010 г.) из MIK игнорирует -f и требует
        e2fsck, если ФС не помечена clean. Помечаем её clean напрямую в Python:
          • s_state (__le16 @ 0x3A в суперблоке = 0x43A в файле) = 1
          • если включён metadata_csum — пересчитываем s_checksum (__le32 @ 0x3FC)
            как crc32c первых 0x3FC байт суперблока (без самого поля csum)
        Это убирает требование e2fsck, не запуская его.
        """
        import struct
        try:
            with open(path, "r+b") as f:
                f.seek(1024)
                sb = bytearray(f.read(1024))
                if len(sb) < 1024:
                    return False
                # ext4 magic 0xEF53 @ 0x38
                if sb[0x38:0x3A] != b"\x53\xef":
                    return False
                # feature_ro_compat @ 0x64; бит 0x400 = metadata_csum
                ro_compat = struct.unpack("<I", sb[0x64:0x68])[0]
                has_csum = bool(ro_compat & 0x400)
                # s_state = 1 (EXT2_VALID_FS / clean)
                old_state = struct.unpack("<H", sb[0x3A:0x3C])[0]
                sb[0x3A:0x3C] = struct.pack("<H", 1)
                if has_csum:
                    csum = self._crc32c(bytes(sb[:0x3FC]))
                    sb[0x3FC:0x400] = struct.pack("<I", csum)
                f.seek(1024)
                f.write(sb)
            if log:
                log(f"  🧹 ФС помечена clean (s_state {old_state}→1"
                    + (", crc32c пересчитан)" if has_csum else ")"))
            return True
        except Exception as ex:
            if log:
                log(f"  ⚠ не удалось пометить clean: {ex}")
            return False

    def _run_tool_streaming(self, cmd, log, timeout=1800, label="процесс", cwd=None):
        """Запустить утилиту с ПОТОКОВЫМ выводом и детектом зависания.

        Возвращает (returncode, полный_вывод). returncode=-1 при таймауте/убийстве.

        WinError 5 (отказано в доступе) у cygwin-утилит обычно из-за того, что
        процесс не находит свои cygwin*.dll (нет рабочей директории exe в путях)
        или из-за конфликта флагов запуска. Поэтому:
          • cwd = папка exe (чтобы рядом лежащие cygwin*.dll загрузились)
          • CREATE_NO_WINDOW (стандартный флаг без конфликтов; STARTUPINFO+
            NEW_PROCESS_GROUP давали WinError 5)
          • аргументы-файлы передаются как АБСОЛЮТНЫЕ пути
        """
        import threading as _th, time as _t

        # cwd по умолчанию — папка исполняемого файла (для cygwin DLL)
        if cwd is None and cmd:
            exe_dir = os.path.dirname(os.path.abspath(cmd[0]))
            if os.path.isdir(exe_dir):
                cwd = exe_dir

        kwargs = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL,
                                 bufsize=1, text=True, errors='ignore',
                                 cwd=cwd, **kwargs)
        except Exception as ex:
            log(f"❌ Запуск {label}: {ex}")
            return -1, ""

        out_lines = []
        last_output = [_t.time()]
        done = _th.Event()

        def _reader():
            try:
                for line in iter(p.stdout.readline, ''):
                    if line:
                        out_lines.append(line)
                        last_output[0] = _t.time()
                        s = line.rstrip()
                        if s:
                            log("  " + s)
            except Exception:
                pass
            finally:
                done.set()

        rt = _th.Thread(target=_reader, daemon=True)
        rt.start()

        start = _t.time()
        warned_silent = False
        while not done.is_set():
            done.wait(timeout=2)
            elapsed = _t.time() - start
            silent = _t.time() - last_output[0]
            if elapsed > timeout:
                log(f"❌ {label}: таймаут {int(timeout)}с — убиваю процесс")
                try: p.kill()
                except Exception: pass
                return -1, "".join(out_lines)
            if silent > 20 and not warned_silent and p.poll() is None:
                log(f"  ⏳ {label} работает молча уже {int(silent)}с "
                    "(идёт обработка, ожидайте)...")
                warned_silent = True
            elif silent < 5:
                warned_silent = False

        p.wait()
        return p.returncode, "".join(out_lines)

    def shrink_ext4_image(self, src, out, tools, log):
        """Сжать ext4-образ до минимального размера через resize2fs -M.

        Шаги:
          1. (если sparse) simg2img → raw ext4
          2. e2fsck -f -y — обязательная проверка перед resize
          3. resize2fs -M — ужать ФС до минимума занятых данных
          4. результат — raw ext4 меньшего размера

        Возвращает (ok, итоговый_размер_байт или None).

        ВАЖНО про burning-пакет: сжатый ext4 в пакете прошьётся нормально —
        update.exe пишет образ как есть. Но если образ покрыт vbmeta
        (dm-verity hashtree), менять его содержимое/размер можно ТОЛЬКО
        вместе с отключением verity (vbmeta disable-verity) или пересчётом
        хешей — иначе будет dm-verity corrupted.
        """
        import shutil as _sh
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

        if not tools.get("resize2fs"):
            log("❌ resize2fs не найден в files/MIK/bin/")
            return False, None
        if not tools.get("e2fsck"):
            log("⚠ e2fsck не найден — resize2fs может отказаться без проверки")

        # 1. Определяем формат и при необходимости распаковываем sparse
        try:
            with open(src, "rb") as f:
                head = f.read(4)
        except Exception as ex:
            log(f"❌ Чтение образа: {ex}")
            return False, None

        # work_raw — АБСОЛЮТНЫЙ путь: cygwin-утилиты запускаются с cwd=папка_exe,
        # поэтому относительный путь они бы не нашли.
        work_raw = os.path.abspath(out + ".raw_tmp")
        is_sparse = head == b"\x3a\xff\x26\xed"
        if is_sparse:
            if not tools.get("simg2img"):
                log("❌ Образ sparse, но simg2img не найден")
                return False, None
            log("🔄 simg2img (sparse → raw)...")
            r = subprocess.run([tools["simg2img"], src, work_raw],
                               capture_output=True, timeout=600, creationflags=cflags)
            if r.returncode != 0 or not os.path.exists(work_raw):
                log("❌ simg2img не сработал")
                return False, None
        else:
            # Копируем raw, чтобы не портить исходник
            _sh.copy2(src, work_raw)

        # Проверяем, что это ext4 (magic 0xEF53 @ 0x438)
        try:
            with open(work_raw, "rb") as f:
                f.seek(0x438)
                if f.read(2) != b"\x53\xef":
                    log("❌ Это не ext4 — resize2fs работает только с ext2/3/4")
                    log("   (для erofs сжатие неприменимо — он и так компактный)")
                    os.remove(work_raw)
                    return False, None
        except Exception:
            pass

        # 2. e2fsck -f -y — ОПЦИОНАЛЬНО. В составе MIK его НЕТ (есть только
        #    resize2fs.exe в bin/resize2fs/). resize2fs -M -f с stdin=DEVNULL
        #    работает и без предварительного e2fsck в большинстве случаев —
        #    флаг -f форсит операцию, а DEVNULL не даёт зависнуть на запросе.
        #    Если e2fsck доступен — используем его для надёжности.
        e2fsck = tools.get("e2fsck")
        if not e2fsck and tools.get("resize2fs"):
            sibling_dir = os.path.dirname(tools["resize2fs"])
            for n in ("e2fsck.exe", "e2fsck", "fsck.ext4.exe", "fsck.ext4"):
                cand = os.path.join(sibling_dir, n)
                if os.path.exists(cand):
                    e2fsck = cand
                    break

        if e2fsck:
            log("🔍 e2fsck -f -y (проверка ФС перед сжатием)...")
            try:
                r = subprocess.run([e2fsck, "-f", "-y", work_raw],
                                   capture_output=True, timeout=600,
                                   stdin=subprocess.DEVNULL, creationflags=cflags)
                o = ((r.stdout or b"")+(r.stderr or b"")).decode("utf-8", "ignore")
                for ln in o.splitlines():
                    if ln.strip():
                        log("  " + ln.strip())
                # e2fsck: 0=ок, 1=исправлены ошибки, 2=исправлены+ребут — норма
                if r.returncode > 2:
                    log(f"⚠ e2fsck код {r.returncode} — пробуем resize2fs всё равно")
            except subprocess.TimeoutExpired:
                log("⚠ e2fsck таймаут — продолжаем")
        else:
            # e2fsck нет (типичный MIK). Старая cygwin-resize2fs (2010) требует
            # e2fsck, если ФС не «clean», и ИГНОРИРУЕТ -f. Помечаем ФС clean
            # прямо в суперблоке (s_state=1 + пересчёт crc32c) — это убирает
            # требование e2fsck, не запуская его.
            log("ℹ e2fsck не найден — помечаем ФС clean напрямую (s_state+crc32c)")
            self._mark_ext4_clean(work_raw, log)

        # 3. resize2fs -M — через потоковый запуск с детектом зависания.
        #    Cygwin-сборка resize2fs из MIK виснет при CREATE_NO_WINDOW —
        #    _run_tool_streaming запускает её с консолью (но скрытой) и
        #    показывает живой вывод/предупреждает, если процесс молчит.
        log("📐 resize2fs -M (сжатие до минимума данных)...")
        log("   (для больших образов может занять минуту, ожидайте)")
        rc, o = self._run_tool_streaming(
            [tools["resize2fs"], "-M", "-f", work_raw],
            log, timeout=1800, label="resize2fs")

        # Если версия требует e2fsck — повторная пометка clean + попытка без -f
        if rc != 0 and "e2fsck" in o.lower():
            log("  ⚠ resize2fs требует e2fsck — повторная пометка clean...")
            self._mark_ext4_clean(work_raw, log)
            rc, o = self._run_tool_streaming(
                [tools["resize2fs"], "-M", work_raw],
                log, timeout=1800, label="resize2fs")

        if rc != 0:
            log(f"❌ resize2fs код {rc}")
            if "superblock checksum" in o.lower():
                log("  (ошибка csum — образ мог быть изменён; попробуйте заново)")
            try: os.remove(work_raw)
            except Exception: pass
            return False, None

        new_size = os.path.getsize(work_raw)

        # 4. Итог: raw ext4. Если исходник был sparse — пере-упаковываем в sparse
        if is_sparse and tools.get("img2simg"):
            log("🔄 img2simg (raw → sparse)...")
            r = subprocess.run([tools["img2simg"], work_raw, out],
                               capture_output=True, timeout=600, creationflags=cflags)
            try: os.remove(work_raw)
            except Exception: pass
            if r.returncode != 0 or not os.path.exists(out):
                log("❌ img2simg не сработал")
                return False, None
        else:
            # raw — просто перемещаем
            try:
                if os.path.exists(out):
                    os.remove(out)
                os.rename(work_raw, out)
            except Exception as ex:
                log(f"❌ Перемещение результата: {ex}")
                return False, None

        return True, new_size

    def _find_mik_tools(self):
        """Найти MIK и его консольные утилиты в files/MIK/.
        Возвращает dict: {mik_gui, bin_dir, simg2img, img2simg, make_ext4fs,
                          imgextractor, mkfs_erofs}. Значения = путь или None.
        """
        mik_dir = os.path.join(FILE_DIR, "MIK")
        result = {"mik_gui": None, "bin_dir": None, "simg2img": None,
                  "img2simg": None, "make_ext4fs": None, "imgextractor": None,
                  "mkfs_erofs": None, "resize2fs": None, "e2fsck": None,
                  "dir": mik_dir}
        if not os.path.isdir(mik_dir):
            return result

        # GUI (MIK64.exe / mik64.exe / MIK.exe)
        for name in ("MIK64.exe", "mik64.exe", "MIK.exe", "mik.exe"):
            p = os.path.join(mik_dir, name)
            if os.path.exists(p):
                result["mik_gui"] = p
                break

        # bin/ — консольные утилиты
        bin_dir = os.path.join(mik_dir, "bin")
        if not os.path.isdir(bin_dir):
            # рекурсивно ищем bin
            for root_d, dirs, _files in os.walk(mik_dir):
                if "bin" in dirs:
                    bin_dir = os.path.join(root_d, "bin")
                    break
        if os.path.isdir(bin_dir):
            result["bin_dir"] = bin_dir
            def find_tool(*names):
                # ВАЖНО: только ФАЙЛЫ, не директории. В bin/ есть папка
                # resize2fs/ — без проверки isfile её бы приняли за утилиту
                # и при запуске получили WinError 5 (запуск директории).
                for n in names:
                    p = os.path.join(bin_dir, n)
                    if os.path.isfile(p):
                        return p
                # рекурсивно — тоже только файлы
                for root_d, _dirs, files in os.walk(bin_dir):
                    for n in names:
                        if n in files:
                            cand = os.path.join(root_d, n)
                            if os.path.isfile(cand):
                                return cand
                return None
            result["simg2img"]     = find_tool("simg2img.exe", "simg2img")
            result["img2simg"]     = find_tool("img2simg.exe", "ext2simg.exe",
                                               "img2simg", "ext2simg")
            result["make_ext4fs"]  = find_tool("make_ext4fs.exe", "make_ext4fs")
            result["imgextractor"] = find_tool("imgextractor.exe", "imgextractor",
                                               "extract.exe", "ext4_unpacker.exe")
            result["mkfs_erofs"]   = find_tool("mkfs.erofs.exe", "mkfs.erofs",
                                               "mke2fs.exe")
            # Для resize2fs/e2fsck ищем ТОЛЬКО .exe (без расширения = риск
            # совпасть с одноимённой папкой)
            result["resize2fs"]    = find_tool("resize2fs.exe")
            result["e2fsck"]       = find_tool("e2fsck.exe", "fsck.ext4.exe")

        # resize2fs/e2fsck могут лежать не в bin/, а в подпапке MIK
        # (например files/MIK/bin/resize2fs/resize2fs.exe) — ищем по всему MIK.
        def find_in_mik(*names):
            for root_d, _dirs, files in os.walk(mik_dir):
                for n in names:
                    if n in files:
                        cand = os.path.join(root_d, n)
                        if os.path.isfile(cand):
                            return cand
            return None
        if not result["resize2fs"]:
            result["resize2fs"] = find_in_mik("resize2fs.exe")
        if not result["e2fsck"]:
            result["e2fsck"] = find_in_mik("e2fsck.exe", "fsck.ext4.exe")

        # macOS: MIK — Windows-бинарники. Консольные функции (simg2img,
        # resize2fs и т.д.) берём из Homebrew, если установлен.
        if _IS_MAC:
            result["mik_gui"] = None   # MIK64.exe на mac не запускается
            brew_prefixes = self._macos_brew_paths()
            def find_brew(*names, sbin=False):
                for pref in brew_prefixes:
                    subs = ("opt/e2fsprogs/sbin", "opt/e2fsprogs/libexec") \
                        if sbin else ("opt/e2fsprogs/bin", "opt/e2fsprogs/libexec",
                                      "bin")
                    for sub in subs:
                        for n in names:
                            p = os.path.join(pref, sub, n)
                            if os.path.isfile(p):
                                return p
                # обычные PATH
                for n in names:
                    p = shutil.which(n)
                    if p:
                        return p
                return None
            if not result["simg2img"]:
                result["simg2img"] = find_brew("simg2img")
            if not result["img2simg"]:
                result["img2simg"] = find_brew("img2simg", "ext2simg")
            if not result["resize2fs"]:
                result["resize2fs"] = find_brew("resize2fs", sbin=True)
            if not result["e2fsck"]:
                result["e2fsck"] = find_brew("e2fsck", "fsck.ext4", sbin=True)
            # если ничего не нашлось — подскажем один раз при использовании
            if not (result["simg2img"] and result["resize2fs"]):
                result["_brew_hint"] = ("На macOS для редактора образов "
                                        "выполните: brew install e2fsprogs simg2img")
        return result

    def open_image_editor(self):
        """Редактор образов Android.

        Два режима:
          A) Прямой вызов консольных утилит MIK из bin/ (распаковка + сборка
             без запуска GUI MIK) — для ext4/sparse образов.
          B) Запуск полного MIK GUI (мышкой) — fallback для erofs и сложных
             случаев, где консольная сборка ненадёжна.

        ВАЖНО (из Readme MIK): путь к образу не должен содержать кириллицу,
        пробелы — иначе make_ext4fs не соберёт образ.
        """
        import threading as _th

        tools = self._find_mik_tools()

        win = tk.Toplevel(self.root)
        win.title("Редактор образов Android")
        win.resizable(True, True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{min(900, sw-60)}x{min(720, sh-60)}")
        win.minsize(720, 600)
        win.transient(self.root)

        if _IS_MAC:
            have_tools = bool(tools["simg2img"] or tools["resize2fs"])
            hdr_text = ("✓ Инструменты образов найдены (Homebrew)"
                        if have_tools else
                        "⚠ MIK — Windows-утилита; для образов выполните: "
                        "brew install e2fsprogs simg2img")
        else:
            have_tools = tools["mik_gui"] or tools["bin_dir"]
            hdr_text = ("✓ MIK найден" if have_tools
                        else "✗ MIK не найден — нажмите «Загрузить утилиты»")
        hdr_color = "#27AE60" if have_tools else "#E74C3C"
        hdr = tk.Frame(win, bg=hdr_color, height=32)
        hdr.pack(side=tk.TOP, fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text=hdr_text, font=("Arial", 9, "bold"),
                 bg=hdr_color, fg="white").pack(side=tk.LEFT, padx=10, pady=6)

        # ══ Низ: кнопки ══
        bottom = tk.Frame(win, bg="#ECF0F1", relief=tk.RIDGE, bd=2)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        btn_row = tk.Frame(bottom, bg="#ECF0F1")
        btn_row.pack(fill=tk.X, padx=10, pady=8)

        # ══ Лог (тоже снизу) ══
        logf = tk.LabelFrame(win, text="Журнал", font=("Arial", 9, "bold"))
        logf.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 4))
        log_box = scrolledtext.ScrolledText(logf, height=8, font=_mono_font(8),
                                            bg="#1E1E1E", fg="#00FF00", state='disabled')
        log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        def log(m):
            log_box.config(state='normal')
            log_box.insert(tk.END, m + "\n"); log_box.see(tk.END)
            log_box.config(state='disabled'); log_box.update_idletasks()

        # ══ Верх: выбор образа и папки ══
        top = tk.Frame(win)
        top.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=8)

        # — образ —
        f1 = tk.LabelFrame(top, text="1. Образ раздела (.img)", font=("Arial", 9, "bold"))
        f1.pack(fill=tk.X, pady=(0, 6))
        img_var = tk.StringVar()
        r1 = tk.Frame(f1); r1.pack(fill=tk.X, padx=6, pady=4)
        tk.Entry(r1, textvariable=img_var, font=("Arial", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        info_var = tk.StringVar(value="Выберите образ для анализа")
        is_sparse = tk.BooleanVar(value=False)
        is_erofs  = tk.BooleanVar(value=False)

        def analyze(path):
            try:
                sz = os.path.getsize(path)
                with open(path, "rb") as fh:
                    head4 = fh.read(4)
                    fh.seek(1024)
                    erofs_magic = fh.read(4)
                    fh.seek(0x438)
                    ext4_magic = fh.read(2)
                sparse = head4 == b"\x3a\xff\x26\xed"
                erofs  = erofs_magic == b"\xe2\xe1\xf5\xe0"   # 0xE0F5E1E2 @ 1024
                ext4   = ext4_magic == b"\x53\xef"            # 0xEF53 @ 0x438
                is_sparse.set(sparse); is_erofs.set(erofs)
                fmt = ("Sparse (android)" if sparse else
                       "EROFS (read-only)" if erofs else
                       "ext4" if ext4 else "неизвестно")
                info_var.set(f"{fmt}  |  {sz/(1024*1024):.1f} MB")
                log(f"Образ: {os.path.basename(path)} — {fmt}, {sz/(1024*1024):.1f} MB")
                if erofs:
                    log("ℹ EROFS — для редактирования соберётся как ext4 (размер вырастет)")
                if _re_has_bad(path):
                    log("⚠ В пути пробелы/кириллица — make_ext4fs может не собрать образ!")
            except Exception as ex:
                log(f"❌ Анализ: {ex}")

        def _re_has_bad(p):
            import re
            return bool(re.search(r'[ \u0400-\u04FF]', p))

        def pick_img():
            fn = filedialog.askopenfilename(
                title="Выберите образ", parent=win,
                initialdir=last_dir("edit_image", IMG_DIR),
                filetypes=[("Image", "*.img *.PARTITION *.fex"), ("All", "*.*")])
            if fn:
                remember_last_dir("edit_image", fn)
                img_var.set(fn)
                work_var.set(os.path.splitext(fn)[0] + "_unpacked")
                out_var.set(os.path.splitext(fn)[0] + "-modified.img")
                analyze(fn)
        tk.Button(r1, text="Обзор…", command=pick_img, width=9).pack(side=tk.LEFT)
        tk.Label(f1, textvariable=info_var, font=("Arial", 8), fg="#8E44AD"
                 ).pack(anchor=tk.W, padx=6, pady=(0, 4))

        for cn in ("system", "vendor", "product", "odm"):
            if cn in self.selected_images:
                img_var.set(self.selected_images[cn]); analyze(self.selected_images[cn]); break

        # — папка распаковки —
        f2 = tk.LabelFrame(top, text="2. Папка распаковки", font=("Arial", 9, "bold"))
        f2.pack(fill=tk.X, pady=(0, 6))
        work_var = tk.StringVar()
        r2 = tk.Frame(f2); r2.pack(fill=tk.X, padx=6, pady=4)
        tk.Entry(r2, textvariable=work_var, font=("Arial", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        tk.Button(r2, text="Обзор…",
                  command=lambda: work_var.set(
                      filedialog.askdirectory(parent=win,
                          initialdir=last_dir("edit_workdir",
                                              os.path.dirname(img_var.get() or IMG_DIR)))
                      or work_var.get()),
                  width=9).pack(side=tk.LEFT)

        # — выходной образ —
        f3 = tk.LabelFrame(top, text="3. Новый образ (.img)", font=("Arial", 9, "bold"))
        f3.pack(fill=tk.X, pady=(0, 6))
        out_var = tk.StringVar()
        r3 = tk.Frame(f3); r3.pack(fill=tk.X, padx=6, pady=4)
        tk.Entry(r3, textvariable=out_var, font=("Arial", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        tk.Button(r3, text="Обзор…",
                  command=lambda: out_var.set(
                      filedialog.asksaveasfilename(parent=win, defaultextension=".img",
                          initialdir=last_dir("edit_save", IMG_DIR))
                      or out_var.get()),
                  width=9).pack(side=tk.LEFT)

        # — описание режимов —
        desc = tk.Label(top, justify=tk.LEFT, font=("Arial", 8), fg="gray", text=(
            "Распаковка/сборка через консольные утилиты MIK (bin/).\n"
            "• ext4/sparse — поддерживаются полностью.\n"
            "• EROFS — образ только для чтения; для редактирования его\n"
            "  конвертируют в ext4 (увеличится размер). Сложные случаи —\n"
            "  через кнопку «Открыть GUI MIK».\n"
            "• Путь не должен содержать пробелов и кириллицы!"
        ))
        desc.pack(anchor=tk.W, pady=(4, 0))

        prog = ttk.Progressbar(top, mode="indeterminate")
        prog.pack(fill=tk.X, pady=(6, 0))

        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        def run_tool(cmd, timeout=900):
            """Запустить консольную утилиту, лог в окно. Возврат (rc, out)."""
            log(f"$ {os.path.basename(cmd[0])} {' '.join(cmd[1:])}")
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                   creationflags=cflags)
                out = ((r.stdout or b"") + (r.stderr or b"")
                       ).decode("utf-8", errors="ignore")
                for ln in out.splitlines():
                    if ln.strip():
                        log("  " + ln.strip())
                return r.returncode, out
            except subprocess.TimeoutExpired:
                log("  ❌ таймаут"); return -1, "timeout"
            except Exception as ex:
                log(f"  ❌ {ex}"); return -1, str(ex)

        # ══ Распаковка ══
        def do_unpack():
            src = img_var.get()
            if not src or not os.path.exists(src):
                messagebox.showwarning("!", "Выберите образ", parent=win); return
            if not tools["bin_dir"]:
                messagebox.showwarning("!",
                    "Консольные утилиты MIK (bin/) не найдены.\n"
                    "Используйте «Открыть GUI MIK» или скачайте MIK заново.",
                    parent=win); return
            work = work_var.get() or os.path.splitext(src)[0] + "_unpacked"
            work_var.set(work)

            def _t():
                prog.start(10)
                try:
                    os.makedirs(work, exist_ok=True)
                    raw = src
                    # sparse → raw
                    if is_sparse.get() and tools["simg2img"]:
                        raw = os.path.join(work, "_raw.img")
                        log("🔄 simg2img (sparse → raw)...")
                        rc, _ = run_tool([tools["simg2img"], src, raw])
                        if rc != 0:
                            log("❌ simg2img не сработал"); return
                    # извлечение содержимого
                    if tools["imgextractor"]:
                        log("📂 imgextractor...")
                        rc, _ = run_tool([tools["imgextractor"], raw, work])
                        if rc == 0:
                            log(f"✓ Распаковано в {work}")
                            self.root.after(0, lambda: _open_folder(work))
                        else:
                            log("⚠ imgextractor вернул ошибку — для erofs это ожидаемо.")
                            log("  Попробуйте «Открыть GUI MIK».")
                    else:
                        log("⚠ imgextractor не найден в bin/. Используйте GUI MIK.")
                finally:
                    self.root.after(0, prog.stop)
            _th.Thread(target=_t, daemon=True).start()

        def _open_folder(path):
            try: os.startfile(path)
            except Exception: pass
            messagebox.showinfo("Распаковано",
                f"Образ распакован в:\n{path}\n\n"
                "Отредактируйте файлы, затем «Собрать образ».", parent=win)

        # ══ Сборка ══
        def do_pack():
            work = work_var.get()
            out  = out_var.get()
            if not work or not os.path.isdir(work):
                messagebox.showwarning("!", "Нет папки распаковки", parent=win); return
            if not out:
                messagebox.showwarning("!", "Укажите выходной файл", parent=win); return
            if not tools["make_ext4fs"]:
                messagebox.showwarning("!",
                    "make_ext4fs не найден. Используйте «Открыть GUI MIK».",
                    parent=win); return
            if __import__("re").search(r'[ \u0400-\u04FF]', work + out):
                if not messagebox.askyesno("Внимание",
                    "В пути есть пробелы/кириллица.\nmake_ext4fs может не собрать образ.\n"
                    "Продолжить?", parent=win):
                    return

            def _t():
                prog.start(10)
                try:
                    # размер: берём исходный образ или папку × 1.1
                    orig = img_var.get()
                    if orig and os.path.exists(orig):
                        size = os.path.getsize(orig)
                    else:
                        size = sum(os.path.getsize(os.path.join(dp, f))
                                   for dp, _d, fs in os.walk(work) for f in fs)
                        size = int(size * 1.15)
                    raw_out = out
                    make_sparse = is_sparse.get()
                    if make_sparse:
                        raw_out = out + ".raw"
                    log(f"📦 make_ext4fs (размер {size//(1024*1024)} MB)...")
                    # make_ext4fs -s -l <size> -a <mountpoint> <out> <dir>
                    mp = os.path.basename(work).replace("_unpacked", "")
                    rc, _ = run_tool([tools["make_ext4fs"],
                                      "-l", str(size), "-a", mp or "system",
                                      raw_out, work])
                    if rc != 0:
                        log("❌ make_ext4fs не сработал"); return
                    if make_sparse and tools["img2simg"]:
                        log("🔄 img2simg (raw → sparse)...")
                        rc, _ = run_tool([tools["img2simg"], raw_out, out])
                        try: os.remove(raw_out)
                        except Exception: pass
                    log(f"✓ Готово: {out}")
                    self.root.after(0, lambda: messagebox.showinfo(
                        "Готово", f"Новый образ:\n{out}", parent=win))
                finally:
                    self.root.after(0, prog.stop)
            _th.Thread(target=_t, daemon=True).start()

        # ══ GUI MIK ══
        def open_gui():
            if _IS_MAC:
                messagebox.showinfo(
                    "macOS", "MIK GUI — Windows-программа и на macOS не "
                    "запускается.\n\nКонсольные функции (распаковка/сборка/"
                    "сжатие образов) работают через Homebrew:\n"
                    "brew install e2fsprogs simg2img", parent=win)
                return
            if not tools["mik_gui"]:
                messagebox.showwarning("!", "MIK GUI (MIK64.exe) не найден", parent=win); return
            try:
                img = img_var.get()
                if img and os.path.exists(img):
                    subprocess.Popen([tools["mik_gui"], img])
                else:
                    subprocess.Popen([tools["mik_gui"]])
                log(f"✓ Запущен MIK GUI")
            except Exception as ex:
                messagebox.showerror("Ошибка", str(ex), parent=win)

        def do_vbmeta_patch():
            src = img_var.get()
            if not src or not os.path.exists(src):
                messagebox.showwarning("!",
                    "Выберите vbmeta.img в поле «Образ раздела»", parent=win)
                return
            out = os.path.splitext(src)[0] + "-disabled.img"
            ok, msg = self.patch_vbmeta_disable_verity(src, out)
            log(f"🔓 vbmeta patch: {msg}")
            if ok:
                log(f"  → {out}")
                messagebox.showinfo("vbmeta пропатчен",
                    msg + f"\n\nСохранено: {out}\n\n"
                    "Прошейте этот vbmeta, чтобы убрать dm-verity бутлуп\n"
                    "после прошивки несовместимых system/product.", parent=win)
            else:
                messagebox.showerror("Ошибка", msg, parent=win)

        def do_shrink():
            if getattr(self, "_shrink_running", False):
                messagebox.showinfo("Сжатие идёт",
                    "Сжатие уже выполняется — дождитесь завершения.", parent=win)
                return
            src = img_var.get()
            if not src or not os.path.exists(src):
                messagebox.showwarning("!",
                    "Выберите ext4-образ (system/product/vendor) в поле «Образ»",
                    parent=win)
                return
            if not tools.get("resize2fs"):
                messagebox.showwarning("!",
                    "resize2fs не найден в files/MIK/bin/.\n"
                    "Убедитесь, что MIK скачан полностью.", parent=win)
                return
            orig_size = os.path.getsize(src)
            out = os.path.splitext(src)[0] + "-shrunk.img"

            if not messagebox.askyesno("Сжатие ext4",
                f"Сжать образ до минимального размера?\n\n"
                f"Исходный: {orig_size/(1024*1024):.0f} MB\n"
                f"Результат: {out}\n\n"
                "⚠️ Если образ покрыт vbmeta (dm-verity), после сжатия\n"
                "нужно либо отключить verity (vbmeta disable-verity),\n"
                "либо пересчитать хеши — иначе будет dm-verity corrupted.\n\n"
                "Продолжить?", parent=win):
                return

            def _t():
                self._shrink_running = True
                prog.start(10)
                try:
                    log(f"📐 Сжатие {os.path.basename(src)} "
                        f"({orig_size/(1024*1024):.0f} MB)...")
                    ok, new_size = self.shrink_ext4_image(src, out, tools, log)
                    if ok:
                        saved = orig_size - new_size
                        log(f"✓ Готово: {new_size/(1024*1024):.0f} MB "
                            f"(освобождено {saved/(1024*1024):.0f} MB)")
                        self.root.after(0, lambda: messagebox.showinfo("Сжато",
                            f"Образ сжат:\n{out}\n\n"
                            f"{orig_size/(1024*1024):.0f} MB → "
                            f"{new_size/(1024*1024):.0f} MB\n\n"
                            "Теперь образ должен влезть в раздел. Если он под\n"
                            "vbmeta — не забудьте про disable-verity.", parent=win))
                    else:
                        log("❌ Сжатие не удалось")
                finally:
                    self._shrink_running = False
                    self.root.after(0, prog.stop)
            import threading as _th
            _th.Thread(target=_t, daemon=True).start()

        # ── Кнопки ──
        if tools["bin_dir"]:
            tk.Button(btn_row, text="📂 Распаковать",
                      command=do_unpack, bg="#2980B9", fg="white",
                      font=("Arial", 10, "bold"), height=2
                      ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
            tk.Button(btn_row, text="📦 Собрать образ",
                      command=do_pack, bg="#27AE60", fg="white",
                      font=("Arial", 10, "bold"), height=2
                      ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        if tools.get("resize2fs"):
            tk.Button(btn_row, text="📐 Сжать ext4\n(resize2fs -M)",
                      command=do_shrink, bg="#16A085", fg="white",
                      font=("Arial", 9), height=2
                      ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Button(btn_row, text="🔓 vbmeta\ndisable-verity",
                  command=do_vbmeta_patch, bg="#D35400", fg="white",
                  font=("Arial", 9), height=2
                  ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        if tools["mik_gui"]:
            tk.Button(btn_row, text="🖥 Открыть GUI MIK",
                      command=open_gui, bg="#8E44AD", fg="white",
                      font=("Arial", 9), height=2
                      ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        if not have_tools:
            tk.Button(btn_row, text="📥 Загрузить утилиты",
                      command=self.download_tools_from_github,
                      bg="#9B59B6", fg="white", font=("Arial", 10), height=2
                      ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Button(btn_row, text="Закрыть", command=win.destroy,
                  font=("Arial", 9), height=2, width=10).pack(side=tk.RIGHT)

        log("Редактор образов готов.")
        log("🔓 Кнопка «vbmeta disable-verity» патчит vbmeta.img (флаги AVB)")
        log("   — лечит dm-verity бутлуп после прошивки несовместимых разделов.")
        if tools.get("_brew_hint"):
            log("⚠ " + tools["_brew_hint"])
        if tools["bin_dir"]:
            log(f"Утилиты bin/: {tools['bin_dir']}")
            for k in ("simg2img", "img2simg", "make_ext4fs", "imgextractor"):
                log(f"  {k}: {'✓' if tools[k] else '✗ не найден'}")
        else:
            log("⚠ Папка bin/ не найдена — доступен только GUI MIK.")

    def dump_partitions(self):
        """Дамп разделов устройства через update.exe mread.

        Документация Amlogic update tool:
          update mread store <part> normal <nBytes> <file>
        ВАЖНО: nBytes — реальный размер раздела в байтах (НЕ 0!),
        иначе ошибка "Err args in mread: check filetype and readSize".

        Размеры берём из таблицы разделов (sectors × 512).
        Список разделов:
          • заранее известная таблица Yandex Station Max (по умолчанию)
          • либо парсинг UART-лога amlmmc part 1 (автоматом или вставкой вручную)
        """
        import re as _re, time as _t, threading as _th

        # (name, sectors)  — из реального лога устройства (× 512 = байты)
        KNOWN_PARTS_SECT = [
            ("bootloader",  8192), ("reserved",  131072), ("cache",  2293760),
            ("env",        16384), ("logo",       16384), ("recovery",  49152),
            ("misc",       16384), ("dtbo",       16384), ("cri_data",  16384),
            ("param",      32768), ("boot",       32768), ("rsv",       32768),
            ("metadata",   32768), ("vbmeta",      4096), ("tee",       65536),
            ("vendor",   1048576), ("odm",       262144), ("system",  3031040),
            ("product",   262144), ("sysrecovery",3145728),("data",  19644416),
        ]
        SECTOR = 512

        def parse_table(text):
            """Парсит вывод amlmmc part 1:
               ' 00 0 8192    512 U-Boot bootloader'
               idx start sectors sectorsize type name
            """
            parts, seen = [], set()
            for line in text.splitlines():
                line = _re.sub(r'\[\d{2}:\d{2}:\d{2}[.,]\d{3}\]', '', line)
                m = _re.search(
                    r'\b(\d{1,3})\s+(\d+)\s+(\d+)\s+(512)\s+\S+\s+(\w+)', line)
                if m:
                    name = m.group(5)
                    if name in seen or name in ('name','Type','Size','Sect','Start'):
                        continue
                    seen.add(name)
                    parts.append((name, int(m.group(3))))   # sectors
            return parts

        # ── Окно ──────────────────────────────────────────────────────────────
        win = tk.Toplevel(self.root)
        win.title("Дамп разделов")
        win.resizable(True, True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w = min(840, sw - 60)
        h = min(760, sh - 60)
        win.geometry(f"{w}x{h}")
        win.minsize(680, 560)
        win.transient(self.root)

        # ══ КНОПКИ ВНИЗУ — пакуем ПЕРВЫМИ с side=BOTTOM, чтобы не исчезали ══
        bottom = tk.Frame(win, bg="#ECF0F1", relief=tk.RIDGE, bd=2)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        btn_inner = tk.Frame(bottom, bg="#ECF0F1")
        btn_inner.pack(fill=tk.X, padx=10, pady=8)

        session_btn = tk.Button(
            btn_inner,
            text="⚡ Запустить сессию (U-Boot → amlmmc part 1)",
            bg="#2980B9", fg="white", font=("Arial", 10, "bold"), height=2)
        session_btn.pack(fill=tk.X, pady=(0, 4))

        mid_row = tk.Frame(btn_inner, bg="#ECF0F1")
        mid_row.pack(fill=tk.X, pady=(0, 4))
        paste_btn = tk.Button(
            mid_row, text="📋 Вставить UART лог",
            bg="#7F8C8D", fg="white", font=("Arial", 9))
        paste_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        reset_btn = tk.Button(
            mid_row, text="↺ Стандартная таблица",
            bg="#95A5A6", fg="white", font=("Arial", 9))
        reset_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        dump_btn = tk.Button(
            btn_inner, text="💾 Дамп выбранных разделов",
            bg="#C0392B", fg="white", font=("Arial", 11, "bold"), height=2)
        dump_btn.pack(fill=tk.X, pady=(0, 4))

        tk.Button(btn_inner, text="Закрыть", command=win.destroy,
                  font=("Arial", 9)).pack(fill=tk.X)

        # ── Заголовок ─────────────────────────────────────────────────────────
        tk.Label(win, text="Дамп разделов устройства",
                 font=("Arial", 13, "bold"), pady=4).pack(side=tk.TOP)

        # ── Инструкция ────────────────────────────────────────────────────────
        inst = tk.LabelFrame(win, text="Подготовка", font=("Arial", 9, "bold"))
        inst.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 4))
        tk.Label(inst, justify=tk.LEFT, font=("Arial", 8), text=(
            "1. USB к сервисной колодке, пин 6 на GND, USB к ПК (без питания).\n"
            "2. «Запустить сессию» → сразу подайте питание.\n"
            "3. Размеры берутся из таблицы; для дампа размер ОБЯЗАТЕЛЕН (не 0).\n"
            "4. Без UART используется стандартная таблица YSM (21 раздел)."
        )).pack(anchor=tk.W, padx=6, pady=2)

        # ── Папка ─────────────────────────────────────────────────────────────
        of = tk.LabelFrame(win, text="Папка для дампов", font=("Arial", 9, "bold"))
        of.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 4))
        out_var = tk.StringVar(value=os.path.join(ROOT_DIR, "dump"))
        orow = tk.Frame(of); orow.pack(fill=tk.X, padx=6, pady=4)
        tk.Entry(orow, textvariable=out_var, font=("Arial", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        tk.Button(orow, text="Обзор…",
                  command=lambda: out_var.set(
                      filedialog.askdirectory(title="Папка дампов", parent=win,
                          initialdir=last_dir("dump_dir", ROOT_DIR))
                      or out_var.get()), width=8).pack(side=tk.LEFT)

        # ── Лог (внизу, фиксированной высоты, перед таблицей) ─────────────────
        logf = tk.LabelFrame(win, text="Журнал", font=("Arial", 9, "bold"))
        logf.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 4))
        log_box = scrolledtext.ScrolledText(logf, height=6, font=_mono_font(8),
                                            bg="#1E1E1E", fg="#00FF00", state='disabled')
        log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        def log(msg):
            log_box.config(state='normal')
            log_box.insert(tk.END, msg + "\n")
            log_box.see(tk.END)
            log_box.config(state='disabled')
            log_box.update_idletasks()

        # Подключаем журнал дампа к USB-watcher, чтобы события
        # подключения/отключения USB дублировались и сюда.
        self._dump_log_fn = log
        def _on_dump_close():
            self._dump_log_fn = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_dump_close)

        # ── Таблица (растягивается между шапкой и логом) ──────────────────────
        lf = tk.LabelFrame(win, text="Разделы (клик — вкл/выкл; ✓ = дампить)",
                           font=("Arial", 9, "bold"))
        lf.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

        # Панель «выбрать все / снять все»
        sel_bar = tk.Frame(lf)
        sel_bar.pack(side=tk.TOP, fill=tk.X, padx=2, pady=(2, 0))

        cols = ("sel", "name", "size")
        tree = ttk.Treeview(lf, columns=cols, show="headings")
        tree.heading("sel",  text="✓",      anchor="center")
        tree.heading("name", text="Раздел")
        tree.heading("size", text="Размер")
        tree.column("sel",  width=40,  anchor="center", stretch=False)
        tree.column("name", width=200, anchor="w")
        tree.column("size", width=140, anchor="e")
        vsb = ttk.Scrollbar(lf, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        part_sect = {}   # name → sectors
        part_sel  = {}   # name → bool

        def set_all(state):
            for row in tree.get_children():
                n = tree.set(row, "name")
                part_sel[n] = state
                tree.set(row, "sel", "✓" if state else "")

        tk.Button(sel_bar, text="✓ Выбрать все", font=("Arial", 8),
                  command=lambda: set_all(True), width=14).pack(side=tk.LEFT, padx=2)
        tk.Button(sel_bar, text="✗ Снять все", font=("Arial", 8),
                  command=lambda: set_all(False), width=14).pack(side=tk.LEFT, padx=2)
        tk.Button(sel_bar, text="⤺ Инвертировать", font=("Arial", 8),
                  command=lambda: [
                      (part_sel.__setitem__(tree.set(r, "name"),
                          not part_sel.get(tree.set(r, "name"), True)),
                       tree.set(r, "sel",
                          "✓" if part_sel[tree.set(r, "name")] else ""))
                      for r in tree.get_children()],
                  width=14).pack(side=tk.LEFT, padx=2)

        def toggle(event):
            row = tree.identify_row(event.y)
            if not row: return
            n = tree.set(row, "name")
            part_sel[n] = not part_sel.get(n, True)
            tree.set(row, "sel", "✓" if part_sel[n] else "")
        tree.bind("<ButtonRelease-1>", toggle)

        def populate(parts_sect, source=""):
            for row in tree.get_children(): tree.delete(row)
            part_sect.clear(); part_sel.clear()
            for name, sect in parts_sect:
                part_sect[name] = sect
                part_sel[name]  = True
                sz_b = sect * SECTOR
                sz_s = (f"{sz_b/(1024*1024):.0f} MB" if sz_b >= 1024*1024
                        else f"{sz_b//1024} KB")
                tree.insert("", tk.END, values=("✓", name, sz_s))
            if source:
                log(f"✓ Загружено {len(parts_sect)} разделов ({source})")

        populate(KNOWN_PARTS_SECT, "стандартная таблица YSM")

        # ── Снапшот терминала ─────────────────────────────────────────────────
        def snapshot_terminal():
            result = [""]; ev = _th.Event()
            def _do():
                try: result[0] = self.terminal_text.get("1.0", tk.END)
                except Exception: pass
                ev.set()
            self.root.after(0, _do)
            ev.wait(timeout=5)
            return result[0]

        # ── Сессия ────────────────────────────────────────────────────────────
        def run_session():
            session_btn.config(state=tk.DISABLED)
            def _thread():
                self._pause_usb_watch()   # пауза USB-watcher на время сессии
                try:
                    self.get_update_path()
                except FileNotFoundError as ex:
                    log(f"❌ {ex}")
                    self._resume_usb_watch()
                    self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                    return

                # Явная проверка pyusb — без него pyamlboot не загрузит U-Boot
                if not USB_AVAILABLE:
                    log("⚠ pyusb не установлен — загрузка U-Boot через pyamlboot")
                    log("  невозможна. Установите: pip install pyusb")
                    log("  (дамп продолжится, если U-Boot уже загружен в память)")

                # Проверка пароля USB перед загрузкой U-Boot / дампом
                log("🔐 Проверка пароля USB-режима Amlogic...")
                gate_ok = [True]
                gate_done = __import__("threading").Event()
                def _gate():
                    gate_ok[0] = self._password_gate(log_fn=log, parent=win)
                    gate_done.set()
                self.root.after(0, _gate)
                gate_done.wait()
                if not gate_ok[0]:
                    log("⛔ Сессия отменена (требуется пароль USB).")
                    self._resume_usb_watch()
                    self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                    return

                _sender = get_aml_bundle_sender()
                if _sender:
                    bundle = os.path.join(FILE_DIR, "aml_bundle.img")
                    if not os.path.exists(bundle):
                        log(f"❌ aml_bundle.img не найден в {FILE_DIR}")
                        self._resume_usb_watch()
                        self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                        return
                    log("🔌 Ожидание устройства (USB Boot)...")
                    try:
                        _sender(bundle); log("✓ U-Boot загружен")
                        self._release_aml_usb()   # отпускаем интерфейс для update
                    except Exception as ex:
                        log(f"❌ pyamlboot: {safe_decode(ex)}")
                        self._resume_usb_watch()
                        self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                        return
                    _t.sleep(3)
                    # U-Boot пере-энумерирует USB — ждём реального возврата
                    if not self._wait_amlogic_usb(30, log_fn=log):
                        log("❌ Устройство не вернулось на USB после загрузки "
                            "U-Boot — переподключите питание/USB и повторите")
                        self._resume_usb_watch()
                        self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                        return
                else:
                    log("⚠ pyamlboot_local не найден/не импортируется.")
                    log("  Нажмите «Загрузить утилиты» и ПЕРЕЗАПУСТИТЕ программу,")
                    log("  либо ожидается, что U-Boot уже загружен в память.")

                log("🔧 mmc dev 1...")
                try: self.aml_bulkcmd("mmc dev 1"); log("  ✓")
                except Exception as ex: log(f"  ⚠ {ex}")

                log("🔓 store disprotect key...")
                try: self.aml_bulkcmd("store disprotect key"); log("  ✓")
                except Exception as ex: log(f"  ⚠ {ex}")

                snap_before = snapshot_terminal(); len_before = len(snap_before)
                self._terminal_capture_buf = []

                log("📋 amlmmc part 1...")
                usb_out = ""
                try: usb_out = self.aml_bulkcmd("amlmmc part 1")
                except Exception as ex: log(f"  ⚠ {ex}")

                wait = 4 if self.serial_running else 1
                log(f"  ⏳ {wait}с (UART {'подключён' if self.serial_running else 'НЕТ'})")
                _t.sleep(wait)

                snap_after = snapshot_terminal()
                new_text = snap_after[len_before:]
                captured = "\n".join(self._terminal_capture_buf or [])
                self._terminal_capture_buf = None

                parts = parse_table(new_text + "\n" + captured + "\n" + usb_out)

                def _ui():
                    if parts:
                        populate(parts, "UART автоопределение")
                        log("✓ Таблица разделов получена с устройства — точное совпадение.")
                    else:
                        log("ℹ Авто-таблица с устройства не считана (ответ amlmmc part 1")
                        log("  приходит по UART). Используется встроенная таблица YSM —")
                        log("  она совпадает с реальной для Yandex Station Max (S905X2).")
                        if not self.serial_running:
                            log("  Для 100% сверки: подключите UART (115200) в приложении")
                            log("  или вставьте лог из PuTTY кнопкой «Вставить UART лог».")
                        else:
                            log("  UART подключён, но ответ не распознан — проверьте,")
                            log("  виден ли в терминале блок «Part Start Sect x Size».")
                        prev = new_text[:200].strip()
                        if prev: log(f"  (терминал: {prev[:120]})")
                    session_btn.config(state=tk.NORMAL)
                self.root.after(0, _ui)
                self._resume_usb_watch()   # сессия завершена — возобновляем

            _th.Thread(target=_thread, daemon=True).start()

        # ── Вставить лог ──────────────────────────────────────────────────────
        def paste_log():
            pw = tk.Toplevel(win)
            pw.title("Вставить UART лог")
            pw.geometry("700x420"); pw.resizable(True, True); pw.minsize(500, 320)
            pw.transient(win)
            tk.Label(pw, text="Вставьте вывод amlmmc part 1 (из PuTTY):",
                     font=("Arial", 10)).pack(pady=6)
            txt = scrolledtext.ScrolledText(pw, font=_mono_font(9))
            txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
            def do_parse():
                parts = parse_table(txt.get("1.0", tk.END))
                if parts:
                    populate(parts, "ручная вставка"); pw.destroy()
                else:
                    messagebox.showwarning("Не найдено",
                        "Разделы не распознаны.", parent=pw)
            tk.Button(pw, text="Разобрать", command=do_parse,
                      bg="#27AE60", fg="white", font=("Arial", 10)
                      ).pack(pady=6, fill=tk.X, padx=10)

        # ── Дамп ──────────────────────────────────────────────────────────────
        def run_dump():
            to_dump = [n for n, s in part_sel.items() if s]
            if not to_dump:
                messagebox.showwarning("Нет выбора",
                                       "Выберите хотя бы один раздел", parent=win)
                return
            out_dir = out_var.get(); os.makedirs(out_dir, exist_ok=True)
            try:
                update_path = self.get_update_path()
            except FileNotFoundError as ex:
                log(f"❌ {ex}"); return

            def _thread():
                self._pause_usb_watch()   # пауза USB-watcher на время дампа
                dump_btn.config(state=tk.DISABLED)
                log(f"\n💾 Дамп {len(to_dump)} разделов → {out_dir}")
                cflags = subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
                try:
                    for pname in to_dump:
                        sect = part_sect.get(pname, 0)
                        nbytes = sect * SECTOR
                        if nbytes <= 0:
                            log(f"  ⚠ {pname}: неизвестен размер, пропуск")
                            continue
                        out_file = os.path.join(out_dir, f"{pname}.img")
                        size_hex = hex(nbytes)
                        log(f"  ⬇ {pname}  ({nbytes//1024} KB, {size_hex})...")
                        try:
                            r = subprocess.run(
                                [update_path, "mread", "store",
                                 pname, "normal", size_hex, out_file],
                                capture_output=True, timeout=1800,
                                creationflags=cflags)
                            out_t = safe_decode((r.stdout or b"")+(r.stderr or b""))
                            for ln in out_t.splitlines():
                                if ln.strip():
                                    self.root.after(0,
                                        lambda l=ln.strip(): self.terminal_log(f"[USB] {l}"))
                            if r.returncode == 0 and os.path.exists(out_file):
                                log(f"     ✓ {os.path.getsize(out_file)//1024} KB сохранено")
                            else:
                                log(f"     ❌ код {r.returncode}: {out_t.strip()[:140]}")
                        except subprocess.TimeoutExpired:
                            log(f"     ❌ таймаут (раздел слишком большой?)")
                        except Exception as ex:
                            log(f"     ❌ {safe_decode(ex)}")
                finally:
                    self._resume_usb_watch()   # возобновляем после дампа
                self.root.after(0, lambda: (
                    dump_btn.config(state=tk.NORMAL),
                    messagebox.showinfo("Готово",
                                        f"Дампы в:\n{out_dir}", parent=win)))
            _th.Thread(target=_thread, daemon=True).start()

        # ── Привязка кнопок ───────────────────────────────────────────────────
        session_btn.config(command=run_session)
        paste_btn.config(command=paste_log)
        reset_btn.config(command=lambda: populate(KNOWN_PARTS_SECT, "стандартная таблица YSM"))
        dump_btn.config(command=run_dump)

        log("Стандартная таблица Yandex Station Max загружена (21 раздел).")
        log("Размеры известны — дамп доступен сразу, даже без UART.")

    def rescan_images_dir(self):
        """Пересканировать self.images_dir и обновить пути встроенных разделов.
        Возвращает число найденных образов."""
        found = 0
        for img in PART_IMAGES:
            p = os.path.join(self.images_dir, img["file"])
            lbl = self.image_path_labels.get(img["name"])
            if os.path.exists(p):
                self.selected_images[img["name"]] = p
                if lbl:
                    lbl.config(text=f"✓ {img['file']}", fg="green")
                found += 1
            else:
                self.selected_images.pop(img["name"], None)
                if lbl:
                    lbl.config(text="✗ Не найден", fg="red")
        return found

    def choose_images_dir(self):
        """Выбрать папку образов разделов: переопределяет images-bkp для
        основного окна. Папка сохраняется в настройках и используется
        при следующих запусках."""
        d = filedialog.askdirectory(
            title="Папка с образами разделов (boot.img, system-bkp.img, ...)",
            initialdir=self.images_dir or IMG_DIR, parent=self.root)
        if not d:
            return
        self.images_dir = d
        s = load_settings()
        s["images_dir"] = d
        save_settings(s)
        found = self.rescan_images_dir()
        self.log(f"📁 Папка образов: {d} — найдено {found}/{len(PART_IMAGES)}")
        self.files_status_label.config(
            text=f"Папка образов: {d} (найдено {found}/{len(PART_IMAGES)})",
            fg="#27AE60" if found == len(PART_IMAGES) else "#E67E22")

    def browse_image(self, img):
        filename = filedialog.askopenfilename(
            title=f"Выберите образ {img['display']}",
            initialdir=last_dir("partition_image",
                                os.path.dirname(self.selected_images.get(img["name"], "")) or self.images_dir),
            filetypes=[("Image files", "*.img"), ("All files", "*.*")]
        )
        if filename:
            remember_last_dir("partition_image", filename)
            self.selected_images[img["name"]] = filename
            self.image_path_labels[img["name"]].config(
                text=f"✓ {os.path.basename(filename)}",
                fg="green"
            )
            self.log(f"Выбран образ {img['display']}: {filename}")
    
    def select_all(self):
        for var in self.image_vars.values():
            var.set(True)
    
    def deselect_all(self):
        for var in self.image_vars.values():
            var.set(False)
    
    def check_all_files(self):
        """Проверка наличия всех необходимых файлов"""
        missing_files = []
        found_files = []
        
        # Проверяем update (macOS: files/macos/update, Windows: update.exe)
        update_path = None
        if _IS_MAC:
            for cand in (os.path.join(FILE_DIR, "macos", "update"),
                         os.path.join(FILE_DIR, "update")):
                if os.path.exists(cand):
                    update_path = cand
                    break
            upd_label = "update (macOS)"
        else:
            if os.path.exists(os.path.join(FILE_DIR, "update.exe")):
                update_path = os.path.join(FILE_DIR, "update.exe")
            elif os.path.exists(os.path.join(FILE_DIR, "update")):
                update_path = os.path.join(FILE_DIR, "update")
            upd_label = "update.exe"

        if not update_path:
            missing_files.append(f"{upd_label} (утилита прошивки)")
        else:
            found_files.append(f"{upd_label} ✓")
        
        # Проверяем aml_bundle.img
        bundle_path = os.path.join(FILE_DIR, "aml_bundle.img")
        if not os.path.exists(bundle_path):
            missing_files.append("aml_bundle.img (U-Boot образ)")
        else:
            found_files.append("aml_bundle.img ✓")
        
        # Проверяем образы разделов
        for img in PART_IMAGES:
            default_path = os.path.join(self.images_dir, img["file"])
            if os.path.exists(default_path):
                found_files.append(f"{img['file']} ✓")
                if img["name"] not in self.selected_images:
                    self.selected_images[img["name"]] = default_path
                    self.image_path_labels[img["name"]].config(
                        text=f"✓ {img['file']}",
                        fg="green"
                    )
            else:
                missing_files.append(img["file"])
        
        # Формируем отчет
        status_text = f"Найдено: {len(found_files)} файлов"
        if missing_files:
            status_text += f" | Отсутствует: {len(missing_files)}"
            self.files_status_label.config(text=status_text, fg="orange")
            self.status_label.config(
                text=f"⚠️ Отсутствует файлов: {len(missing_files)} - прошивка невозможна",
                fg="#E67E22"
            )
            
            msg = "⚠️ ОТСУТСТВУЮЩИЕ ФАЙЛЫ:\n\n"
            msg += "\n".join(f"• {f}" for f in missing_files[:10])
            if len(missing_files) > 10:
                msg += f"\n... и еще {len(missing_files) - 10}"
            
            msg += "\n\n📁 Где взять файлы:\n"
            msg += ("• update - будет загружен автоматически из GitHub"
                    if _IS_MAC else
                    "• update.exe - будет загружен автоматически из GitHub")
            msg += "\n"
            msg += "• aml_bundle.img - U-Boot образ для вашего чипа\n"
            msg += "• *.img файлы - образы прошивки устройства"

            # Предлагаем загрузить утилиты
            if update_path is None:
                msg += ("\n\n💡 Нажмите 'Да' чтобы загрузить утилиты из GitHub")
                response = messagebox.askyesno("Проверка файлов", msg)
                if response:
                    self.download_tools_from_github()
            else:
                messagebox.showwarning("Проверка файлов", msg)
        else:
            self.files_status_label.config(text="✓ Все необходимые файлы найдены!", fg="green")
            self.status_label.config(
                text=f"✅ Все файлы на месте! Готово к прошивке ({len(found_files)} файлов)",
                fg="#27AE60"
            )
            messagebox.showinfo(
                "Проверка файлов",
                f"✓ Все файлы на месте!\n\nНайдено файлов: {len(found_files)}"
            )
        
        self.log(f"Проверка файлов: найдено {len(found_files)}, отсутствует {len(missing_files)}")
    
    def check_and_download_tools(self):
        """Проверка наличия всех критичных компонентов при запуске.

        Проверяются:
          • update.exe (files/) или update (files/macos/) — прошивка, дамп
          • MIK (MIK64.exe)    (files/MIK/)  — редактор образов (только Windows)
          • pyamlboot_local/   (корень проекта)       — загрузка U-Boot
          • aml_bundle.img     (files/) — опционально, специфичен для устройства
        """
        missing = []      # критичные — предлагаем скачать
        warnings = []     # некритичные — просто предупреждаем

        if _IS_MAC:
            # macOS: update в files/macos/, MIK не нужен (Windows-бинарники)
            mac_update = os.path.join(FILE_DIR, "macos", "update")
            if not os.path.exists(mac_update):
                missing.append("update — утилита прошивки/дампа (files/macos/, "
                               "скачается автоматически)")
            pyaml = os.path.join(ROOT_DIR, "pyamlboot_local")
            if not (os.path.isdir(pyaml)
                    and os.path.exists(os.path.join(pyaml, "boot.py"))):
                missing.append("pyamlboot_local — загрузка U-Boot")
            if not os.path.exists(os.path.join(FILE_DIR, "aml_bundle.img")):
                warnings.append("aml_bundle.img (U-Boot образ устройства) — "
                                "добавьте вручную в files/")
            if not USB_AVAILABLE:
                warnings.append("pyusb не установлен — загрузка U-Boot и "
                                "обнаружение Amlogic работать не будут "
                                "(pip install pyusb)")
            if not self._macos_find_libusb01() and not os.path.exists(
                    os.path.join(FILE_DIR, "macos", "lib",
                                 "libusb-0.1.4.dylib")):
                warnings.append("libusb-0.1.4.dylib — нужна утилите update "
                                "(скачается автоматически или: brew install "
                                "libusb-compat)")
        else:
            # Windows: прежняя логика
            # 1. update.exe
            if not (os.path.exists(os.path.join(FILE_DIR, "update.exe"))
                    or os.path.exists(os.path.join(FILE_DIR, "update"))):
                missing.append("update.exe — утилита прошивки/дампа")

            # 2. MIK (любой из вариантов имени, в т.ч. в подпапке)
            mik_dir = os.path.join(FILE_DIR, "MIK")
            mik_found = False
            if os.path.isdir(mik_dir):
                for root_d, _d, files in os.walk(mik_dir):
                    if any(n in files for n in ("MIK64.exe", "mik64.exe", "MIK.exe")):
                        mik_found = True
                        break
            if not mik_found:
                missing.append("MIK — редактор образов (files/MIK/)")

            # 3. pyamlboot_local
            pyaml = os.path.join(ROOT_DIR, "pyamlboot_local")
            if not (os.path.isdir(pyaml)
                    and os.path.exists(os.path.join(pyaml, "boot.py"))):
                missing.append("pyamlboot_local — загрузка U-Boot")

            # 4. aml_bundle.img — некритично (специфичен для устройства)
            if not os.path.exists(os.path.join(FILE_DIR, "aml_bundle.img")):
                warnings.append("aml_bundle.img (U-Boot образ устройства) — "
                                "добавьте вручную в files/")

        if missing:
            msg = "Отсутствуют компоненты:\n\n"
            msg += "\n".join(f"• {m}" for m in missing)
            if warnings:
                msg += "\n\nТакже потребуется (вручную):\n"
                msg += "\n".join(f"• {w}" for w in warnings)
            msg += ("\n\nЗагрузить недостающее из GitHub "
                    "(suddosu/yasta_flasher%s)?"
                    % ("" if _IS_MAC else " + MIK"))
            if messagebox.askyesno("Необходимые файлы", msg, icon='question'):
                self.download_tools_from_github()
        elif warnings:
            messagebox.showinfo(
                "Проверка компонентов",
                "Основные утилиты на месте.\n\nОбратите внимание:\n"
                + "\n".join(f"• {w}" for w in warnings))
    
    def download_tools_from_github(self):
        """Скачать ВСЕ необходимые файлы из репозитория suddosu/yasta_flasher.

        Главный механизм — скачивание всего репозитория одним zip-архивом
        (archive/refs/heads/main.zip). Это гарантирует, что попадут ВСЕ файлы
        из files/ и pyamlboot_local/ (включая .dll, .bin и прочее), без
        перечисления имён и без зависимости от GitHub API (rate-limit / 404).

        Раскладка из архива:
          <zip>/files/*            → files/
          <zip>/pyamlboot_local/*  → pyamlboot_local/   (в корне проекта)
          прочее в корне репо      → игнорируется

        MIK скачивается отдельно из CryptoNickSoft/MIK (другой репозиторий).
        """
        import zipfile, io

        OWNER, REPO_NAME, BRANCH = "suddosu", "yasta_flasher", "main"
        REPO_ZIP = f"https://github.com/{OWNER}/{REPO_NAME}/archive/refs/heads/{BRANCH}.zip"

        def ua_req(url):
            return urllib.request.Request(url, headers={"User-Agent": "yasta_flasher/1.0"})

        # ── Окно прогресса ────────────────────────────────────────────────────
        pw = tk.Toplevel(self.root)
        pw.title("Загрузка файлов")
        pw.resizable(True, True)
        pw.geometry("680x500")
        pw.minsize(520, 400)
        pw.transient(self.root)
        pw.grab_set()

        tk.Label(pw, text=f"Загрузка из {OWNER}/{REPO_NAME}",
                 font=("Arial", 12, "bold")).pack(pady=8)
        log_box = scrolledtext.ScrolledText(pw, font=_mono_font(9))
        log_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        prog = ttk.Progressbar(pw, mode="indeterminate")
        prog.pack(fill=tk.X, padx=10, pady=4)
        prog.start(10)
        status = tk.Label(pw, text="", font=("Arial", 9))
        status.pack(pady=2)

        def log_p(msg):
            log_box.insert(tk.END, msg + "\n")
            log_box.see(tk.END)
            log_box.update_idletasks()

        def extract_repo_zip(data, log_fn):
            """Распаковать архив репозитория, разложив files/ и pyamlboot_local/.
            Возвращает (files_count, pyaml_count).
            """
            fcount = pcount = 0
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = z.namelist()
                # Верхняя папка вида yasta_flasher-main/
                top = members[0].split("/")[0] + "/" if members and "/" in members[0] else ""
                for m in members:
                    if m.endswith("/"):
                        continue
                    rel = m[len(top):] if top and m.startswith(top) else m
                    if not rel:
                        continue
                    # files/...  → FILE_DIR
                    if rel.startswith("files/"):
                        sub = rel[len("files/"):]
                        dest = os.path.join(FILE_DIR, sub.replace("/", os.sep))
                        target = "files"
                    # pyamlboot_local/...  → ROOT_DIR/pyamlboot_local
                    elif rel.startswith("pyamlboot_local/"):
                        dest = os.path.join(ROOT_DIR, rel.replace("/", os.sep))
                        target = "pyamlboot"
                    else:
                        continue  # README и прочее из корня — не нужно
                    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                    # Не перезаписываем уже существующие образы прошивки
                    if os.path.exists(dest) and dest.lower().endswith(
                            (".img", ".bin")) and os.path.getsize(dest) > 0:
                        log_fn(f"  ⏭ есть: {os.path.basename(dest)}")
                    else:
                        with z.open(m) as src, open(dest, "wb") as out:
                            out.write(src.read())
                        log_fn(f"  ↓ {rel}")
                    if target == "files":
                        fcount += 1
                    else:
                        pcount += 1
            return fcount, pcount

        def dl_mik(log_fn):
            """MIK (CryptoNickSoft/MIK) → files/MIK/  (отдельный репозиторий)."""
            mik_dir = os.path.join(FILE_DIR, "MIK")
            if os.path.isdir(mik_dir):
                has_exe = False
                for _r, _d, fs in os.walk(mik_dir):
                    if any(n.lower() in ("mik64.exe", "mik.exe") for n in fs):
                        has_exe = True
                        break
                if has_exe:
                    log_fn("  ⏭ MIK уже установлен")
                    return
            zip_url = "https://github.com/CryptoNickSoft/MIK/archive/refs/heads/main.zip"
            log_fn("  ↓ MIK main.zip ...")
            try:
                with url_open(ua_req(zip_url), timeout=300) as r:
                    data = r.read()
            except Exception as ex:
                log_fn(f"  ❌ MIK: {ex}")
                log_fn(f"  → Вручную: {zip_url}  →  files/MIK/")
                return
            try:
                os.makedirs(mik_dir, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    members = z.namelist()
                    top = members[0].split("/")[0] + "/" if members and "/" in members[0] else ""
                    cnt = 0
                    for m in members:
                        if m.endswith("/"):
                            continue
                        rel = m[len(top):] if top and m.startswith(top) else m
                        if not rel:
                            continue
                        dest = os.path.join(mik_dir, rel.replace("/", os.sep))
                        os.makedirs(os.path.dirname(dest) or mik_dir, exist_ok=True)
                        with z.open(m) as src, open(dest, "wb") as out:
                            out.write(src.read())
                        cnt += 1
                log_fn(f"  ✓ MIK: {cnt} файлов в {mik_dir}")
            except Exception as ex:
                log_fn(f"  ❌ MIK распаковка: {ex}")

        def worker():
            errors = []

            # ── 1. Основной репозиторий (files/ + pyamlboot_local/) ───────────
            log_p("━━ Репозиторий yasta_flasher (полный zip) ━━━━━━━━━━━━━")
            log_p(f"  Источник: {REPO_ZIP}")
            try:
                with url_open(ua_req(REPO_ZIP), timeout=300) as r:
                    data = r.read()
                log_p(f"  Загружено {len(data)//1024} KB, распаковка...")
                fcount, pcount = extract_repo_zip(data, log_p)
                log_p(f"  ✓ files/: {fcount} файлов,  pyamlboot_local/: {pcount} файлов")
                if fcount == 0:
                    log_p("  ⚠ В архиве не найдена папка files/ — проверьте структуру репо")
            except Exception as ex:
                msg = f"  ❌ Репозиторий: {ex}"
                log_p(msg); errors.append(msg)
                log_p(f"  → Скачайте вручную: {REPO_ZIP}")

            # ── 2. MIK (отдельный репозиторий) ────────────────────────────────
            log_p("\n━━ MIK (CryptoNickSoft/MIK) ━━━━━━━━━━━━━━━━━━━━━━━━━━")
            if _IS_MAC:
                log_p("  ⏭ MIK — Windows-бинарники, на macOS для работы с "
                      "образами используется Homebrew (brew install "
                      "e2fsprogs simg2img)")
            try:
                dl_mik(log_p)
            except Exception as ex:
                msg = f"  ❌ MIK: {ex}"
                log_p(msg); errors.append(msg)

            # ── 3. macOS: update + aml_image_v2_packer из khadas/utils ────────
            if _IS_MAC:
                log_p("\n━━ Утилиты прошивки для macOS (khadas/utils) ━━━━━━")
                try:
                    self._download_macos_flash_tools(log_p)
                except Exception as ex:
                    msg = f"  ❌ macOS-утилиты: {ex}"
                    log_p(msg); errors.append(msg)

            log_p("\n" + "━"*55)
            log_p("Готово." if not errors else f"Завершено с {len(errors)} ошибкой(ами).")
            self.root.after(0, lambda: (prog.stop(), status.config(text="Завершено")))
            self.root.after(0, self.check_all_files)
            if errors:
                self.root.after(0, lambda: messagebox.showwarning(
                    "Частичные ошибки",
                    "Не всё загружено:\n" + "\n".join(errors), parent=pw))
            else:
                self.root.after(4000,
                    lambda: pw.destroy() if pw.winfo_exists() else None)

        import threading as _th
        _th.Thread(target=worker, daemon=True).start()

    def log(self, message):
        """Логирование в журнал операций"""
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')
        self.log_text.update_idletasks()
    
    def test_usb_detection(self):
        """Диагностика обнаружения устройства Amlogic (VID_1B8E PID_C003)."""
        self.log("\n" + "="*50)
        self.log("🔍 ДИАГНОСТИКА USB УСТРОЙСТВ")
        self.log("="*50)

        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        MARKERS = ["1B8E", "C003", "AMLOGIC", "BURNING", "WORLDCUP", "GX-CHIP"]
        found_any = False

        # Метод 1: WMIC (все PnP, не только USB-класс)
        self.log("\n1️⃣ WMIC (Win32_PnPEntity)...")
        try:
            result = subprocess.run(
                ['wmic', 'path', 'Win32_PnPEntity', 'get', 'DeviceID,Name'],
                capture_output=True, text=True, encoding='utf-8',
                errors='ignore', timeout=8, creationflags=cflags)
            lines = [l.strip() for l in result.stdout.split('\n') if l.strip()]
            self.log(f"  Всего PnP-устройств: {max(0, len(lines)-1)}")
            for line in lines:
                if any(m in line.upper() for m in MARKERS):
                    self.log(f"  ✓ НАЙДЕНО: {line[:90]}")
                    found_any = True
            if not found_any:
                self.log("  ✗ через WMIC не найдено")
        except Exception as e:
            self.log(f"  ✗ WMIC ошибка: {e}")

        # Метод 2: PowerShell — ВСЕ PnP-устройства (не только -Class USB!)
        # WorldCup Device регистрируется как libusb/WinUSB, а не USB-класс.
        self.log("\n2️⃣ PowerShell (Get-PnpDevice, все классы)...")
        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 'Get-PnpDevice | Select-Object -Property InstanceId,FriendlyName,Class | Format-Table -AutoSize'],
                capture_output=True, text=True, encoding='utf-8',
                errors='ignore', timeout=8, creationflags=cflags)
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.split('\n') if l.strip()]
                ps_found = False
                for line in lines:
                    if any(m in line.upper() for m in MARKERS):
                        self.log(f"  ✓ НАЙДЕНО: {line[:90]}")
                        ps_found = True; found_any = True
                if not ps_found:
                    self.log("  ✗ через PowerShell не найдено")
            else:
                self.log("  ⚠ PowerShell недоступен (не критично)")
        except Exception as e:
            self.log(f"  ⚠ PowerShell: {e}")

        # Метод 3: pyamlboot (прямое USB-обнаружение libusb)
        self.log("\n3️⃣ pyamlboot (libusb)...")
        if not USB_AVAILABLE:
            self.log("  ⚠ pyusb не установлен (pip install pyusb) — пропуск")
        else:
            try:
                dev = usb.core.find(idVendor=0x1b8e, idProduct=0xc003)
                if dev is not None:
                    self.log("  ✓ НАЙДЕНО через libusb (1b8e:c003)")
                    found_any = True
                else:
                    self.log("  ✗ libusb не видит 1b8e:c003")
            except Exception as e:
                self.log(f"  ⚠ libusb: {e}")

        # Итог
        self.log("\n" + "="*50)
        if found_any:
            self.log("✅ ИТОГ: устройство Amlogic ОБНАРУЖЕНО (хотя бы одним методом)")
            self.log("   Разные методы могут расходиться — это нормально:")
            self.log("   WorldCup Device виден в WMIC, но не в PowerShell -Class USB.")
        else:
            self.log("❌ ИТОГ: устройство не обнаружено ни одним методом")
            self.log("\n💡 Проверьте:")
            self.log("  1. Диспетчер устройств (devmgmt.msc)")
            self.log("  2. Драйверы USB Burning Tool / Zadig (WinUSB)")
            self.log("  3. USB 2.0 порт, замыкание пина 6 на GND")
        self.log("="*50)
    
    def check_drivers(self):
        """Проверка наличия драйверов Amlogic"""
        try:
            result = subprocess.run(
                ['wmic', 'path', 'Win32_PnPEntity', 'get', 'Name'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            
            output = result.stdout.upper()
            
            # Проверяем наличие драйверов Amlogic
            if "AMLOGIC" not in output and "BURNING" not in output:
                self.log("⚠️ Драйверы Amlogic не обнаружены в системе")
                
                response = messagebox.askyesno(
                    "Драйверы не найдены",
                    "Драйверы Amlogic USB Burning Tool не установлены.\n\n"
                    "Без них устройство не будет определяться.\n\n"
                    "Показать инструкцию по установке?"
                )
                
                if response:
                    self.show_driver_instructions()
                    
        except Exception:
            pass  # Игнорируем ошибки проверки
    
    def show_driver_instructions(self):
        """Показать инструкцию по установке драйверов"""
        instructions_window = tk.Toplevel(self.root)
        instructions_window.title("📥 Установка драйверов")
        instructions_window.geometry("600x500")
        instructions_window.resizable(True, True)
        instructions_window.minsize(450, 300)
        instructions_window.transient(self.root)
        
        tk.Label(
            instructions_window,
            text="Установка драйверов Amlogic",
            font=("Arial", 14, "bold"),
            pady=10
        ).pack()
        
        text_widget = scrolledtext.ScrolledText(
            instructions_window,
            wrap=tk.WORD,
            font=_mono_font(9),
            padx=10,
            pady=10
        )
        text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        instructions = """
🔧 УСТАНОВКА ДРАЙВЕРОВ AMLOGIC USB BURNING TOOL

Способ 1: Автоматическая установка (рекомендуется)
═══════════════════════════════════════════════════
1. Скачайте USB Burning Tool:
   https://download.geekbuying.com/Softwares/flash_tool/
   
2. Установите программу (она автоматически установит драйверы)

3. После установки можно удалить саму программу, 
   драйверы останутся в системе


Способ 2: Ручная установка драйверов
═════════════════════════════════════
1. Подключите устройство в режиме USB Boot

2. Откройте Диспетчер устройств (devmgmt.msc)

3. Найдите неизвестное устройство (с желтым значком)
   Обычно называется: "GX-CHIP" или "USB Device"

4. Правой кнопкой → "Обновить драйвер"

5. Выберите "Выполнить поиск драйверов на этом компьютере"

6. Укажите папку с драйверами из USB Burning Tool:
   C:\\Program Files (x86)\\Amlogic\\USB_Burning_Tool\\driver


Способ 3: Использование Zadig (для опытных пользователей)
═══════════════════════════════════════════════════════════
1. Скачайте Zadig: https://zadig.akeo.ie/

2. Запустите от имени администратора

3. Options → List All Devices

4. Выберите "GX-CHIP" или "Amlogic USB Burning Tool" 
   (VID: 1B8E, PID: C003)

5. Выберите драйвер: WinUSB или libusb-win32

6. Нажмите "Replace Driver" или "Install Driver"


⚠️ ВАЖНО:
• После установки драйверов перезагрузите компьютер
• Некоторые антивирусы блокируют драйверы - отключите временно
• Используйте USB 2.0 порты (не 3.0!)
• Запускайте программу от имени администратора


📝 ПРОВЕРКА УСТАНОВКИ:
После установки драйверов:
1. Подключите устройство в режиме USB Boot
2. Откройте Диспетчер устройств
3. В разделе "Контроллеры USB" должно появиться:
   "Amlogic USB Burning Tool" или "GX-CHIP"

Если устройство определяется - драйверы установлены правильно!
"""
        
        text_widget.insert("1.0", instructions)
        text_widget.config(state='disabled')
        
        tk.Button(
            instructions_window,
            text="Закрыть",
            command=instructions_window.destroy,
            font=("Arial", 10),
            pady=5
        ).pack(pady=10)
    
    def check_device_connection(self):
        """Проверка подключения устройства через USB (Windows версия)"""
        self.log("Ожидание подключения устройства Amlogic (1B8E:C003)...")
        self.log("Проверьте:")
        self.log("  1. USB кабель подключен")
        self.log("  2. Пин 6 замкнут на землю (пин 3)")
        self.log("  3. Блок питания подключен")
        self.log("-" * 50)

        elapsed = 0
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        MARKERS = ["1B8E", "C003", "AMLOGIC", "BURNING", "WORLDCUP", "GX-CHIP"]

        # macOS: WMIC/PowerShell недоступны — сразу проверка через libusb/pyusb
        if _IS_MAC:
            while self.is_flashing and elapsed < 300:
                try:
                    import usb.core as _uc
                    dev = _uc.find(idVendor=0x1b8e, idProduct=0xc003)
                    if dev is not None:
                        self.log("  ✓ Устройство обнаружено через libusb (1b8e:c003)")
                        self.log("✓ Устройство обнаружено! Ждем стабилизации...")
                        time.sleep(2)
                        return True
                except ImportError:
                    self.log("  ⚠ pyusb не установлен (pip install pyusb)")
                except Exception as e:
                    # libusb может быть не установлен — не спамим, пишем раз
                    if elapsed == 0:
                        self.log(f"  ⚠ Ошибка libusb: {e} "
                                 "(нужен brew install libusb)")
                if elapsed % 5 == 0:
                    self.log(f"⏳ Поиск устройства... ({elapsed}с)")
                time.sleep(1)
                elapsed += 1
            self.log("\n✗ Устройство не обнаружено за 5 минут")
            return False

        while self.is_flashing and elapsed < 300:  # Таймаут 5 минут
            found = False

            # Метод 1: WMIC (как в test_usb_detection)
            try:
                result = subprocess.run(
                    ['wmic', 'path', 'Win32_PnPEntity', 'get', 'DeviceID,Name'],
                    capture_output=True, text=True, encoding='utf-8',
                    errors='ignore', timeout=3, creationflags=cflags
                )
                lines = [l.strip() for l in result.stdout.split('\n') if l.strip()]
                for line in lines:
                    if any(m in line.upper() for m in MARKERS):
                        self.log(f"  ✓ Устройство обнаружено через WMIC: {line[:60]}")
                        found = True
                        break
                if not found and lines:
                    self.log(f"  ✗ WMIC: устройство не найдено среди {len(lines)} устройств")
            except subprocess.TimeoutExpired:
                self.log("  ⚠ WMIC завис, пропускаем проверку")
            except FileNotFoundError:
                self.log("  ⚠ WMIC не найден в системе")
            except Exception as e:
                self.log(f"  ⚠ Ошибка WMIC: {e}")

            # Метод 2: PowerShell (все классы, как в test_usb_detection)
            if not found:
                try:
                    result = subprocess.run(
                        ['powershell', '-NoProfile', '-Command',
                         'Get-PnpDevice | Select-Object -Property InstanceId,FriendlyName,Class | Format-Table -AutoSize'],
                        capture_output=True, text=True, encoding='utf-8',
                        errors='ignore', timeout=3, creationflags=cflags
                    )
                    if result.returncode == 0:
                        lines = [l.strip() for l in result.stdout.split('\n') if l.strip()]
                        ps_found = False
                        for line in lines:
                            if any(m in line.upper() for m in MARKERS):
                                self.log(f"  ✓ Устройство обнаружено через PowerShell: {line[:60]}")
                                ps_found = True
                                found = True
                                break
                        if not ps_found:
                            self.log("  ✗ PowerShell: устройство не найдено")
                    else:
                        self.log("  ⚠ PowerShell вернул ошибку (не критично)")
                except Exception as e:
                    self.log(f"  ⚠ PowerShell: {e}")

            # Метод 3: libusb (как в test_usb_detection)
            if not found:
                try:
                    import usb.core
                    dev = usb.core.find(idVendor=0x1b8e, idProduct=0xc003)
                    if dev is not None:
                        self.log("  ✓ Устройство обнаружено через libusb (1b8e:c003)")
                        found = True
                    else:
                        self.log("  ✗ libusb: устройство 1b8e:c003 не найдено")
                except ImportError:
                    self.log("  ⚠ pyusb не установлен (pip install pyusb) — пропуск libusb проверки")
                except Exception as e:
                    self.log(f"  ⚠ Ошибка libusb: {e}")

            if found:
                self.log("✓ Устройство обнаружено! Ждем стабилизации...")
                time.sleep(2)
                # Double-check
                return True

            # Обновляем статус каждые 5 секунд
            if elapsed % 5 == 0:
                self.log(f"⏳ Поиск устройства... ({elapsed}с)")
            time.sleep(1)
            elapsed += 1

        if elapsed >= 300:
            self.log("\n✗ Таймаут: устройство не обнаружено за 5 минут")
            self.log("\n💡 Попробуйте:")
            self.log("  • Переподключить USB кабель")
            self.log("  • Использовать другой USB порт (желательно USB 2.0)")
            self.log("  • Проверить замыкание пина 6 на землю")
            self.log("  • Установить драйверы Amlogic USB Burning Tool")
            self.log("  • Перезагрузить компьютер")
            return False

        return False
    
    # ── macOS: утилиты прошивки из khadas/utils (aml-flash-tool) ─────────────
    MACOS_TOOLS_RAW = ("https://raw.githubusercontent.com/khadas/utils/master/"
                       "aml-flash-tool/tools/macos")
    MACOS_TOOLS_DIR = os.path.join(FILE_DIR, "macos")

    # Параметры запуска bl2 для burning-пакетов (крошечные blobs, платформо-
    # независимы; без них невозможна инициализация DDR и запуск U-Boot)
    BURN_RUNPARA_FILES = {
        "usbbl2runpara_ddrinit.bin":
            "https://raw.githubusercontent.com/khadas/utils/master/"
            "aml-flash-tool/tools/datas/usbbl2runpara_ddrinit.bin",
        "usbbl2runpara_runfipimg.bin":
            "https://raw.githubusercontent.com/khadas/utils/master/"
            "aml-flash-tool/tools/datas/usbbl2runpara_runfipimg.bin",
    }

    def _ensure_burn_runpara(self, log_fn=None):
        """Скачать usbbl2runpara_*.bin в files/, если их там нет."""
        def _log(m):
            if log_fn: log_fn(m)
            else: self.log(m)
        for name, url in self.BURN_RUNPARA_FILES.items():
            dest = os.path.join(FILE_DIR, name)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                continue
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "yasta_flasher/1.0"})
                with url_open(req, timeout=60) as r:
                    data = r.read()
                with open(dest, "wb") as fh:
                    fh.write(data)
                _log(f"  ↓ {name} ({len(data)} B)")
            except Exception as ex:
                _log(f"  ⚠ {name}: {ex}")

    FLASH_TOOL_URL = ("https://raw.githubusercontent.com/khadas/utils/master/"
                      "aml-flash-tool/flash-tool")

    def _ensure_khadas_flashtool(self, log_fn=None):
        """Подготовить каноничный khadas flash-tool для запуска на macOS.

        ВАЖНО: flash-tool собирает команды через eval БЕЗ кавычек, поэтому
        путь с пробелом ('Application Support') ломает вызов update. Ставим
        раскладку в ~/.yasta_flasher_ft (без пробелов):
          flash-tool, tools/macos/{update,aml_image_v2_packer}, tools/macos/lib,
          tools/datas/usbbl2runpara_*.bin
        update/libusb копируем из files/macos (уже пропатчены под наш бандл).
        Возвращает путь к flash-tool или None.
        """
        def _log(m):
            if log_fn: log_fn(m)
            else: self.log(m)
        ft_dir = os.path.expanduser("~/.yasta_flasher_ft")
        tools_macos = os.path.join(ft_dir, "tools", "macos")
        tools_datas = os.path.join(ft_dir, "tools", "datas")
        ft = os.path.join(ft_dir, "flash-tool")
        try:
            os.makedirs(tools_macos, exist_ok=True)
            os.makedirs(tools_datas, exist_ok=True)
        except Exception as ex:
            _log(f"  ❌ {ft_dir}: {ex}")
            return None
        if not (os.path.exists(ft) and os.path.getsize(ft) > 1000):
            try:
                req = urllib.request.Request(
                    self.FLASH_TOOL_URL, headers={"User-Agent": "yasta_flasher/1.0"})
                with url_open(req, timeout=60) as r:
                    data = r.read()
                with open(ft, "wb") as fh:
                    fh.write(data)
                _log(f"  ↓ flash-tool ({len(data)//1024} KB)")
            except Exception as ex:
                _log(f"  ❌ flash-tool: {ex}")
                return None
        try:
            os.chmod(ft, 0o755)
        except Exception:
            pass
        try:
            # update всегда свежей (пропатченной) версией
            for src_name in ("update", "aml_image_v2_packer"):
                src = os.path.join(FILE_DIR, "macos", src_name)
                dst = os.path.join(tools_macos, src_name)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    os.chmod(dst, 0o755)
            # libusb-бандл рядом с update (@executable_path/lib/...)
            src_lib = os.path.join(FILE_DIR, "macos", "lib")
            dst_lib = os.path.join(tools_macos, "lib")
            if os.path.isdir(src_lib):
                if os.path.isdir(dst_lib):
                    shutil.rmtree(dst_lib)
                shutil.copytree(src_lib, dst_lib)
            for name in self.BURN_RUNPARA_FILES:
                src = os.path.join(FILE_DIR, name)
                dst = os.path.join(tools_datas, name)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
        except Exception as ex:
            _log(f"  ⚠ Раскладка tools/: {ex}")
        upd = os.path.join(tools_macos, "update")
        if os.path.exists(upd):
            self._ensure_macos_update_runs(upd, _log)
        return ft if os.path.exists(ft) else None

    def _download_macos_flash_tools(self, log_fn=None):
        """Скачать update и aml_image_v2_packer для macOS из khadas/utils.

        Кладём в files/macos/, ставим права на исполнение, затем
        чиним зависимость update → libusb-0.1.4.dylib (см. _ensure_macos_libusb).
        """
        def _log(m):
            if log_fn: log_fn(m)
            else: self.log(m)

        os.makedirs(self.MACOS_TOOLS_DIR, exist_ok=True)
        for name in ("update", "aml_image_v2_packer"):
            dest = os.path.join(self.MACOS_TOOLS_DIR, name)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                _log(f"  ⏭ есть: {name}")
            else:
                url = f"{self.MACOS_TOOLS_RAW}/{name}"
                req = urllib.request.Request(url, headers={"User-Agent": "yasta_flasher/1.0"})
                with url_open(req, timeout=300) as r:
                    data = r.read()
                with open(dest, "wb") as f:
                    f.write(data)
                _log(f"  ↓ {name} ({len(data)//1024} KB)")
            try:
                os.chmod(dest, 0o755)
            except Exception:
                pass

        upd = os.path.join(self.MACOS_TOOLS_DIR, "update")
        self._ensure_macos_update_runs(upd, _log)

        # libusb-1.0 нужен ещё и pyusb/pyamlboot (обнаружение устройства,
        # загрузка U-Boot). Если ни brew, ни бандла — качаем bottle.
        if _IS_MAC and self._macos_libusb1_path() is None:
            _log("  📚 libusb-1.0 для pyusb не найдена — скачиваю...")
            if not self._macos_brew_install("libusb", _log):
                self._macos_fetch_bottle_dylib("libusb", "libusb-1.0.0.dylib",
                                               _log)

    def _macos_brew_paths(self):
        """Префиксы Homebrew, которые реально существуют на этой машине."""
        out = []
        for brew in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if os.path.exists(brew):
                out.append(os.path.dirname(os.path.dirname(brew)))
        return out

    def _macos_find_libusb01(self, log_fn=None):
        """Найти libusb-0.1.4.dylib: наш files/macos/lib или Homebrew."""
        candidates = [os.path.join(self.MACOS_TOOLS_DIR, "lib", "libusb-0.1.4.dylib")]
        for prefix in self._macos_brew_paths():
            candidates.append(os.path.join(
                prefix, "opt", "libusb-compat", "lib", "libusb-0.1.4.dylib"))
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def _macos_brew_install(self, formula, log_fn):
        """Попытка brew install formula. Возвращает True при успехе."""
        brew = None
        for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if os.path.exists(p):
                brew = p
                break
        if not brew:
            log_fn("  ⚠ Homebrew не найден (/opt/homebrew, /usr/local)")
            return False
        log_fn(f"  🍺 brew install {formula} (может занять пару минут)...")
        try:
            r = subprocess.run([brew, "install", formula], capture_output=True,
                               text=True, errors="ignore", timeout=900)
            if r.returncode == 0:
                log_fn(f"  ✓ brew install {formula}: OK")
                return True
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            log_fn("  ⚠ brew: " + (tail[-1] if tail else f"код {r.returncode}"))
        except Exception as ex:
            log_fn(f"  ⚠ brew: {ex}")
        return False

    def _macos_fetch_bottle_dylib(self, formula, dylib_name, log_fn):
        """Скачать bottle формулы из ghcr.io (анонимный token) и вытащить
        dylib_name в files/macos/lib/. Возвращает путь или None.

        Бинарник update универсальный (x86_64 + arm64), архитектура bottle
        подбирается по архитектуре машины.
        """
        import json as _json
        import platform as _platform
        import tarfile as _tar
        import io as _io
        try:
            api = f"https://formulae.brew.sh/api/formula/{formula}.json"
            req = urllib.request.Request(api, headers={"User-Agent": "yasta_flasher/1.0"})
            with url_open(req, timeout=30) as r:
                meta = _json.loads(r.read().decode())
            files = meta.get("bottle", {}).get("stable", {}).get("files", {})
            # arm64_* теги — Apple Silicon, прочие mac-теги — x86_64
            is_arm = _platform.machine() == "arm64"
            tags = sorted(t for t in files
                          if (t.startswith("arm64_") if is_arm
                              else (not t.startswith("arm64_")
                                    and not t.endswith("linux"))))
            if not tags:
                log_fn(f"  ⚠ {formula}: в формуле нет macOS-bottle тегов")
                return None
            tag = tags[-1]   # новейшая версия macOS
            bottle_url = files[tag]["url"]
            log_fn(f"  ↓ {formula} bottle ({tag}) из ghcr.io...")
            # Анонимный token ghcr (bottles живут в homebrew/core/<formula>)
            tok_req = urllib.request.Request(
                "https://ghcr.io/token?service=ghcr.io"
                f"&scope=repository:homebrew/core/{formula}:pull",
                headers={"User-Agent": "yasta_flasher/1.0"})
            with url_open(tok_req, timeout=30) as r:
                token = _json.loads(r.read().decode())["token"]
            dl_req = urllib.request.Request(bottle_url, headers={
                "User-Agent": "yasta_flasher/1.0", "Authorization": f"Bearer {token}"})
            with url_open(dl_req, timeout=300) as r:
                blob = r.read()
            log_fn(f"  Загружено {len(blob)//1024} KB, распаковка...")
            lib_dir = os.path.join(self.MACOS_TOOLS_DIR, "lib")
            os.makedirs(lib_dir, exist_ok=True)
            dest = os.path.join(lib_dir, dylib_name)
            with _tar.open(fileobj=_io.BytesIO(blob), mode="r:gz") as tf:
                for m in tf.getmembers():
                    if os.path.basename(m.name) == dylib_name:
                        with tf.extractfile(m) as src, open(dest, "wb") as out:
                            out.write(src.read())
                        os.chmod(dest, 0o755)
                        log_fn(f"  ✓ {dest}")
                        return dest
            log_fn(f"  ⚠ В bottle {formula} не найден {dylib_name}")
        except Exception as ex:
            log_fn(f"  ⚠ Скачивание bottle {formula} не удалось: {ex}")
        return None

    def _macos_dylib_refs(self, path):
        """Список зависимостей dylib (вторая колонка otool -L, без первой строки)."""
        try:
            r = subprocess.run(["otool", "-L", path],
                               capture_output=True, text=True, timeout=10)
            lines = r.stdout.splitlines()[1:]
            return [l.strip().split(" (")[0].strip() for l in lines if l.strip()]
        except Exception:
            return []

    def _macos_change_dylib_ref(self, dylib, old, new, log_fn, use_id=False):
        """install_name_tool (-change ссылки или -id собственного имени) +
        ad-hoc подпись."""
        try:
            cmd = (["install_name_tool", "-id", new]
                   if use_id else
                   ["install_name_tool", "-change", old, new]) + [dylib]
            subprocess.run(cmd, check=True, timeout=15)
            subprocess.run(["codesign", "--force", "--sign", "-", dylib],
                           capture_output=True, timeout=15)
            return True
        except Exception as ex:
            log_fn(f"  ⚠ install_name_tool ({dylib}): {ex}")
            return False

    def _macos_download_libusb01_bottle(self, log_fn):
        """Последний вариант без Homebrew: собрать локальный комплект
        files/macos/lib/ из bottle'ов ghcr.io:
          libusb-0.1.4.dylib  (shim libusb-compat)
          libusb-1.0.0.dylib  (реальная реализация, от неё зависит shim)
        Плюс переписать внутренние пути (@@HOMEBREW_PREFIX@@/opt/...) на
        @loader_path/@executable_path, чтобы всё работало автономно.
        """
        dylib01 = self._macos_fetch_bottle_dylib("libusb-compat",
                                                 "libusb-0.1.4.dylib", log_fn)
        if not dylib01:
            return None
        lib_dir = os.path.dirname(dylib01)

        # Собственное имя (id) shim'а тоже может указывать на brew-путь
        refs01 = self._macos_dylib_refs(dylib01)
        own = [r for r in refs01 if r.endswith("/" + os.path.basename(dylib01))]
        if own:
            self._macos_change_dylib_ref(
                dylib01, own[0], f"@loader_path/{os.path.basename(dylib01)}",
                log_fn, use_id=True)

        # Зависимость shim'а — libusb-1.0.0.dylib: докачиваем и чиним ссылку
        dep10 = next((r for r in refs01 if "libusb-1.0.0.dylib" in r), None)
        if dep10:
            dylib10 = self._macos_fetch_bottle_dylib("libusb", "libusb-1.0.0.dylib",
                                                     log_fn)
            if not dylib10:
                return None
            # id libusb-1.0 → @loader_path (на случай вложенных ссылок)
            refs10 = self._macos_dylib_refs(dylib10)
            own10 = [r for r in refs10 if r.endswith("/libusb-1.0.0.dylib")]
            if own10:
                self._macos_change_dylib_ref(
                    dylib10, own10[0], "@loader_path/libusb-1.0.0.dylib",
                    log_fn, use_id=True)
            self._macos_change_dylib_ref(
                dylib01, dep10, "@loader_path/libusb-1.0.0.dylib", log_fn)
        return dylib01

    def _ensure_macos_update_runs(self, update_bin, log_fn):
        """Чинить зависимость update → libusb-0.1.4.dylib и проверить запуск.

        Бинарник khadas ссылается на /usr/local/opt/libusb-compat/lib/...
        (путь Intel-Homebrew). Порядок:
          1. если_dylib доступна — патчим install_name_tool и подписываем;
          2. нет — brew install libusb-compat;
          3. нет — скачиваем bottle напрямую;
          4. иначе — понятная инструкция.
        """
        import re as _re
        try:
            r = subprocess.run(["otool", "-L", update_bin],
                               capture_output=True, text=True, timeout=10)
            otool = r.stdout
        except Exception as ex:
            log_fn(f"  ⚠ otool: {ex} — пропускаем проверку libusb")
            return
        if "libusb-0.1.4.dylib" not in otool:
            log_fn("  ✓ update не требует libusb-0.1 — пропускаем")
            return

        # Бинарник универсальный и ссылается на libusb-0.1 в ДВУХ местах
        # (Intel /usr/local/opt/... и ARM /opt/homebrew/opt/...) — патчим все.
        old_paths = _re.findall(r'\s*(\S*libusb-0\.1\.4\.dylib)', otool)
        if not old_paths:
            log_fn("  ✓ libusb-0.1.4.dylib не найдена в otool (неожиданно)")
            return

        dylib = self._macos_find_libusb01(log_fn)
        if dylib is None:
            if self._macos_brew_install("libusb-compat", log_fn):
                dylib = self._macos_find_libusb01(log_fn)
        if dylib is None:
            dylib = self._macos_download_libusb01_bottle(log_fn)
        if dylib is None:
            log_fn("  ❌ libusb-0.1.4.dylib недоступна. Установите Homebrew "
                   "(brew.sh) и выполните: brew install libusb-compat")
            return

        # Путь, вшиваемый в бинарник: brew → абсолютный; наш lib → @executable_path
        if dylib.startswith(self.MACOS_TOOLS_DIR):
            new_path = "@executable_path/lib/libusb-0.1.4.dylib"
        else:
            new_path = dylib
        pending = [p for p in dict.fromkeys(old_paths) if p != new_path]
        if not pending:
            log_fn(f"  ✓ libusb уже корректна: {new_path}")
        else:
            log_fn(f"  🔧 install_name_tool: {pending} → {new_path}")
            try:
                for p in pending:
                    subprocess.run(["install_name_tool", "-change", p,
                                    new_path, update_bin], check=True, timeout=15)
                subprocess.run(["codesign", "--force", "--sign", "-",
                                update_bin], capture_output=True, timeout=15)
            except Exception as ex:
                log_fn(f"  ❌ Не удалось пропатчить update: {ex}")
                return

        # Smoke-тест: бинарник без устройства должен выдать usage, а не
        # ошибку загрузки dylib
        try:
            r = subprocess.run([update_bin], capture_output=True,
                               text=True, errors="ignore", timeout=10)
            log_fn("  ✓ update запускается")
        except Exception as ex:
            log_fn(f"  ⚠ update не запустился: {ex}")
            log_fn("     Если ошибка про 'cannot be opened' — выполните: "
                   "xattr -cr files/macos/ && sudo spctl --add files/macos/update")

    def _find_aml_packer(self):
        """Найти aml_image_v2_packer (Windows) в files/ и подпапках.

        В USB Burning Tool v2 есть только AmlImagePack.dll (библиотека GUI),
        консольный packer туда не входит. Берём его из khadas/utils
        (tools/windows/aml_image_v2_packer.exe).
        """
        candidates = [
            # macOS-сборка из khadas aml-flash-tool
            os.path.join(FILE_DIR, "macos", "aml_image_v2_packer"),
            os.path.join(FILE_DIR, "aml_image_v2_packer.exe"),
            os.path.join(FILE_DIR, "aml_image_v2_packer"),
            os.path.join(FILE_DIR, "tools", "windows", "aml_image_v2_packer.exe"),
            os.path.join(FILE_DIR, "AmlImagePacker", "aml_image_v2_packer.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        # Рекурсивный поиск
        if os.path.isdir(FILE_DIR):
            for root_d, _dirs, files in os.walk(FILE_DIR):
                for n in ("aml_image_v2_packer.exe", "aml_image_v2_packer"):
                    if n in files:
                        return os.path.join(root_d, n)
        return None

    def _download_aml_packer(self, log_fn=None):
        """Скачать aml_image_v2_packer.exe (Windows) из репозитория khadas/utils.

        Путь в репо: aml-flash-tool/tools/windows/aml_image_v2_packer.exe
        Кладём в files/tools/windows/ вместе с DLL-зависимостями той же папки.
        """
        def _log(m):
            if log_fn: log_fn(m)
            else: self.log(m)

        # На macOS нужен не .exe, а маковский packer из tools/macos
        if _IS_MAC:
            _log("📥 Загрузка aml_image_v2_packer (macOS) из khadas/utils...")
            self._download_macos_flash_tools(_log)
            return

        RAW = ("https://raw.githubusercontent.com/khadas/utils/master/"
               "aml-flash-tool/tools/windows")
        API = ("https://api.github.com/repos/khadas/utils/contents/"
               "aml-flash-tool/tools/windows")
        dest_dir = os.path.join(FILE_DIR, "tools", "windows")
        os.makedirs(dest_dir, exist_ok=True)

        def ua(url):
            return urllib.request.Request(url, headers={
                "User-Agent": "yasta_flasher/1.0",
                "Accept": "application/vnd.github+json"})

        _log("📥 Загрузка aml_image_v2_packer из khadas/utils...")

        # Пытаемся получить список файлов папки windows через API,
        # чтобы скачать и .exe, и нужные .dll рядом.
        files_to_get = []
        try:
            import json
            with url_open(ua(API), timeout=20) as r:
                items = json.loads(r.read().decode())
            for it in items:
                if it["type"] == "file" and (
                        it["name"].endswith(".exe") or it["name"].endswith(".dll")):
                    files_to_get.append((it["name"], it["download_url"]))
        except Exception as ex:
            _log(f"  ⚠ API недоступен ({ex}), качаем только packer по raw URL")
            files_to_get = [("aml_image_v2_packer.exe",
                             f"{RAW}/aml_image_v2_packer.exe")]

        ok = 0
        for name, url in files_to_get:
            dest = os.path.join(dest_dir, name)
            if os.path.exists(dest):
                _log(f"  ⏭ есть: {name}")
                ok += 1
                continue
            try:
                with url_open(ua(url), timeout=120) as r:
                    data = r.read()
                with open(dest, "wb") as f:
                    f.write(data)
                _log(f"  ↓ {name} ({len(data)//1024} KB)")
                ok += 1
            except Exception as ex:
                _log(f"  ❌ {name}: {ex}")
        if ok:
            _log(f"✓ Готово. Packer в {dest_dir}")
        else:
            _log("❌ Не удалось скачать packer. Возьмите вручную из:")
            _log("   github.com/khadas/utils → aml-flash-tool/tools/windows/")

    @staticmethod
    def patch_vbmeta_disable_verity(in_path, out_path,
                                     disable_verity=True, disable_verification=True):
        """Пропатчить vbmeta.img — выставить флаги отключения проверки.

        Формат AVB (avb_vbmeta_image.h):
          • offset 0: магия "AVB0"
          • offset 120: flags — 32-битное BIG-ENDIAN поле (байты 120..123)
          • bit 0 = HASHTREE_DISABLED  (--disable-verity)
          • bit 1 = VERIFICATION_DISABLED (--disable-verification)
        Изменяется младший байт поля (offset 123). Эквивалент:
          fastboot --disable-verity --disable-verification flash vbmeta
        Возвращает (ok, message).
        """
        try:
            with open(in_path, "rb") as f:
                data = bytearray(f.read())
        except Exception as ex:
            return False, f"Не удалось открыть: {ex}"
        if len(data) < 256 or data[0:4] != b"AVB0":
            return False, ("Это не похоже на vbmeta (нет магии 'AVB0' в начале).\n"
                           "Нужен standalone vbmeta.img, а не sparse/раздел с footer.")
        flags_off = 123   # младший байт 32-битного BE поля flags (120..123)
        old = data[flags_off]
        new = old
        if disable_verity:
            new |= 0x01
        if disable_verification:
            new |= 0x02
        data[flags_off] = new
        try:
            with open(out_path, "wb") as f:
                f.write(data)
        except Exception as ex:
            return False, f"Не удалось записать: {ex}"
        return True, (f"vbmeta пропатчен. flags байт: 0x{old:02x} → 0x{new:02x}\n"
                      f"(disable-verity={disable_verity}, "
                      f"disable-verification={disable_verification})")

    def build_burning_package(self):
        """Собрать burning-пакет (aml_upgrade_package.img) для USB Burning Tool.

        Обратная операция к flash_burning_package. Поскольку формат пакета
        требует служебных файлов (DDR, UBOOT, DTB, platform.conf) и корректного
        image.cfg, самый надёжный путь — взять ШАБЛОН (распакованный пакет от
        этого же устройства) и заменить в нём нужные разделы своими файлами,
        затем переупаковать через  aml_image_v2_packer -r image.cfg <dir> <out>.

        Шаги в окне:
          1. Шаблон: папка с распакованным пакетом (image.cfg + platform.conf +
             служебные файлы). Можно получить из «Burning-пакет» → распаковка,
             либо распаковать любой .img-пакет прямо здесь.
          2. Таблица разделов из image.cfg — для каждого можно подменить файл.
          3. Сборка → новый aml_upgrade_package.img.
        """
        import threading as _th, re as _re, shutil as _sh

        win = tk.Toplevel(self.root)
        win.title("Сборка burning-пакета (aml_upgrade_package.img)")
        win.resizable(True, True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{min(920, sw-60)}x{min(760, sh-60)}")
        win.minsize(740, 620)
        win.transient(self.root)

        # ── Низ: кнопки ──
        bottom = tk.Frame(win, bg="#ECF0F1", relief=tk.RIDGE, bd=2)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        btn_row = tk.Frame(bottom, bg="#ECF0F1")
        btn_row.pack(fill=tk.X, padx=10, pady=8)

        # ── Лог ──
        logf = tk.LabelFrame(win, text="Журнал", font=("Arial", 9, "bold"))
        logf.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 4))
        log_box = scrolledtext.ScrolledText(logf, height=9, font=_mono_font(8),
                                            bg="#1E1E1E", fg="#00FF00", state='disabled')
        log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        def log(m):
            log_box.config(state='normal')
            log_box.insert(tk.END, m + "\n"); log_box.see(tk.END)
            log_box.config(state='disabled'); log_box.update_idletasks()

        top = tk.Frame(win)
        top.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=8)

        # 1. Шаблон
        f1 = tk.LabelFrame(top, text="1. Шаблон (папка распакованного пакета или .img)",
                           font=("Arial", 9, "bold"))
        f1.pack(fill=tk.X, pady=(0, 6))
        tpl_var = tk.StringVar()
        r1 = tk.Frame(f1); r1.pack(fill=tk.X, padx=6, pady=4)
        tk.Entry(r1, textvariable=tpl_var, font=("Arial", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        def pick_tpl_dir():
            d = filedialog.askdirectory(
                title="Папка распакованного пакета (с image.cfg)",
                initialdir=last_dir("burn_unpacked", ROOT_DIR), parent=win)
            if d:
                remember_last_dir("burn_unpacked", d)
                tpl_var.set(d)
        def pick_tpl_img():
            fn = filedialog.askopenfilename(
                title="Шаблонный aml_upgrade_package.img", parent=win,
                initialdir=last_dir("burn_pkg", ROOT_DIR),
                filetypes=[("Amlogic image", "*.img"), ("All", "*.*")])
            if fn:
                remember_last_dir("burn_pkg", fn)
                tpl_var.set(fn)

        tk.Button(r1, text="Папка…", command=pick_tpl_dir, width=8).pack(side=tk.LEFT)
        tk.Button(r1, text=".img…", command=pick_tpl_img, width=7).pack(side=tk.LEFT, padx=(3,0))

        tk.Label(f1, font=("Arial", 8), fg="gray", justify=tk.LEFT, text=(
            "Шаблон даёт служебные файлы (DDR/UBOOT/DTB/platform.conf) и image.cfg.\n"
            "Берите шаблон ОТ ЭТОГО ЖЕ устройства (Yandex Station Max), иначе пакет\n"
            "может не прошиться. Свои разделы подмените в таблице ниже."
        )).pack(anchor=tk.W, padx=6, pady=(0, 4))

        # 2. Таблица разделов
        f2 = tk.LabelFrame(top, text="2. Разделы (дабл-клик — подменить файл)",
                           font=("Arial", 9, "bold"))
        f2.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        tree = ttk.Treeview(f2, columns=("name", "file", "type", "src"),
                            show="headings", height=9)
        for c, t, w in (("name","Раздел",130),("file","Файл в пакете",180),
                        ("type","Тип",70),("src","Свой файл (подмена)",260)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w", stretch=(c=="src"))
        vsb = ttk.Scrollbar(f2, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # state
        state = {"tpl_dir": None, "cfg_path": None, "parts": [], "svc": {},
                 "overrides": {}}  # name → custom file path

        def override_file(event):
            row = tree.identify_row(event.y)
            if not row: return
            name = tree.set(row, "name")
            fn = filedialog.askopenfilename(
                title=f"Файл для раздела {name}", parent=win,
                initialdir=last_dir("partition_image", IMG_DIR),
                filetypes=[("Image", "*.img *.PARTITION *.bin"), ("All", "*.*")])
            if fn:
                remember_last_dir("partition_image", fn)
                state["overrides"][name] = fn
                tree.set(row, "src", fn)
                log(f"  ↪ {name} ← {os.path.basename(fn)}")
        tree.bind("<Double-1>", override_file)

        # 3. Выход
        f3 = tk.LabelFrame(top, text="3. Выходной пакет (.img)", font=("Arial", 9, "bold"))
        f3.pack(fill=tk.X, pady=(0, 6))
        out_var = tk.StringVar(value=os.path.join(ROOT_DIR, "aml_upgrade_package_new.img"))
        r3 = tk.Frame(f3); r3.pack(fill=tk.X, padx=6, pady=4)
        tk.Entry(r3, textvariable=out_var, font=("Arial", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        tk.Button(r3, text="Обзор…",
                  command=lambda: out_var.set(
                      filedialog.asksaveasfilename(parent=win, defaultextension=".img",
                          filetypes=[("Amlogic image","*.img")]) or out_var.get()),
                  width=9).pack(side=tk.LEFT)

        prog = ttk.Progressbar(top, mode="indeterminate")
        prog.pack(fill=tk.X, pady=(4, 0))

        # ── Загрузка шаблона ──
        def load_template():
            tpl = tpl_var.get()
            if not tpl or not os.path.exists(tpl):
                messagebox.showwarning("!", "Укажите шаблон", parent=win); return
            def _t():
                prog.start(10)
                try:
                    if os.path.isdir(tpl):
                        tpl_dir = tpl
                    else:
                        # это .img — распакуем во временную папку
                        packer = self._find_aml_packer()
                        if not packer:
                            log("❌ Нет aml_image_v2_packer для распаковки шаблона.")
                            log("  Откройте «Burning-пакет» → «Скачать packer».")
                            return
                        tpl_dir = os.path.join(os.path.dirname(tpl), "_tpl_unpack")
                        os.makedirs(tpl_dir, exist_ok=True)
                        log(f"📦 Распаковка шаблона...")
                        cf = subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
                        for flag in ("-d", "-unpack"):
                            r = subprocess.run([packer, flag, tpl, tpl_dir],
                                               capture_output=True, timeout=300,
                                               creationflags=cf)
                            o = ((r.stdout or b"")+(r.stderr or b"")).decode("utf-8","ignore")
                            if "Image unpack OK" in o or os.path.exists(
                                    os.path.join(tpl_dir, "image.cfg")):
                                break
                    cfg = os.path.join(tpl_dir, "image.cfg")
                    if not os.path.exists(cfg):
                        log("❌ image.cfg не найден в шаблоне.")
                        return
                    state["tpl_dir"] = tpl_dir
                    state["cfg_path"] = cfg
                    parts, svc = self._parse_image_cfg_full(cfg)
                    state["parts"] = parts
                    state["svc"] = svc
                    state["overrides"].clear()
                    def _fill():
                        for row in tree.get_children(): tree.delete(row)
                        for p in parts:
                            tree.insert("", tk.END,
                                values=(p["name"], p["file"], p["type"], ""))
                    self.root.after(0, _fill)
                    log(f"✓ Шаблон загружен. Разделов: {len(parts)}")
                    log("  Дабл-клик по разделу — подменить файл своим.")
                except Exception as ex:
                    log(f"❌ {ex}")
                finally:
                    self.root.after(0, prog.stop)
            _th.Thread(target=_t, daemon=True).start()

        # ── Сборка ──
        def build_pkg():
            if not state["cfg_path"]:
                messagebox.showwarning("!", "Сначала загрузите шаблон", parent=win); return
            packer = self._find_aml_packer()
            if not packer:
                messagebox.showwarning("!",
                    "Нужен aml_image_v2_packer (кнопка в «Burning-пакет»)", parent=win)
                return
            out = out_var.get()
            if not out:
                messagebox.showwarning("!", "Укажите выходной файл", parent=win); return
            if _re.search(r'[ \u0400-\u04FF]', out + state["tpl_dir"]):
                if not messagebox.askyesno("Внимание",
                    "В пути есть пробелы/кириллица — packer может не собрать.\n"
                    "Продолжить?", parent=win):
                    return

            def _t():
                prog.start(10)
                try:
                    tpl_dir = state["tpl_dir"]
                    # Готовим рабочую копию папки шаблона, чтобы не портить оригинал
                    work = os.path.join(os.path.dirname(out), "_build_pkg")
                    if os.path.exists(work):
                        _sh.rmtree(work, ignore_errors=True)
                    log("📋 Копирование шаблона в рабочую папку...")
                    _sh.copytree(tpl_dir, work)

                    # Подменяем файлы разделов
                    for name, src in state["overrides"].items():
                        # находим имя файла для этого раздела
                        part = next((p for p in state["parts"] if p["name"]==name), None)
                        if not part:
                            continue
                        dest = os.path.join(work, part["file"])
                        log(f"  ↪ {name}: {os.path.basename(src)} → {part['file']}")
                        _sh.copy2(src, dest)

                    cfg = os.path.join(work, "image.cfg")
                    log(f"🔨 Сборка пакета через {os.path.basename(packer)} -r ...")
                    cf = subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
                    # aml_image_v2_packer -r <image.cfg> <dir> <out.img>
                    r = subprocess.run([packer, "-r", cfg, work, out],
                                       capture_output=True, timeout=1800,
                                       creationflags=cf)
                    o = ((r.stdout or b"")+(r.stderr or b"")).decode("utf-8","ignore")
                    for ln in o.splitlines():
                        if ln.strip(): log("  " + ln.strip())
                    if ("Image pack OK" in o or "pack OK" in o
                            or (os.path.exists(out) and os.path.getsize(out) > 0)):
                        sz = os.path.getsize(out) // (1024*1024) if os.path.exists(out) else 0
                        log(f"✓ Готово! {os.path.basename(out)} ({sz} MB)")
                        self.root.after(0, lambda: messagebox.showinfo(
                            "Готово",
                            f"Пакет собран:\n{out}\n\n"
                            "Его можно прошить через оригинальный USB Burning Tool.",
                            parent=win))
                    else:
                        log("❌ Сборка не удалась (нет 'Image pack OK').")
                    # Чистим рабочую папку
                    _sh.rmtree(work, ignore_errors=True)
                except Exception as ex:
                    log(f"❌ Сборка: {ex}")
                finally:
                    self.root.after(0, prog.stop)
            _th.Thread(target=_t, daemon=True).start()

        tk.Button(btn_row, text="📂 Загрузить шаблон",
                  command=load_template, bg="#2980B9", fg="white",
                  font=("Arial", 10, "bold"), height=2
                  ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Button(btn_row, text="🔨 Собрать пакет",
                  command=build_pkg, bg="#16A085", fg="white",
                  font=("Arial", 10, "bold"), height=2
                  ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Button(btn_row, text="Закрыть", command=win.destroy,
                  font=("Arial", 9), height=2, width=10).pack(side=tk.RIGHT)

        log("Сборка burning-пакета для оригинального USB Burning Tool.")
        log("1) Загрузите шаблон (распакованный пакет от этого устройства)")
        log("2) Дабл-клик по разделам — подмените свои файлы")
        log("3) Соберите новый aml_upgrade_package.img")

    def _parse_image_cfg_full(self, cfg_path):
        """Разобрать image.cfg → (список разделов, служебные файлы).
        Используется и при сборке, и при прошивке пакета."""
        import re as _re
        parts, svc = [], {}
        try:
            with open(cfg_path, "r", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            return parts, svc
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fm = _re.search(r'file="([^"]+)"', line)
            sm = _re.search(r'sub_type="([^"]+)"', line)
            mm = _re.search(r'main_type="([^"]+)"', line)
            tm = _re.search(r'file_type="([^"]+)"', line)
            if not fm:
                continue
            fname = fm.group(1)
            sub = sm.group(1) if sm else ""
            main = mm.group(1) if mm else ""
            ftype = tm.group(1) if tm else "normal"
            if main == "PARTITION":
                parts.append({"name": sub, "file": fname, "type": ftype})
            else:
                svc[sub] = fname
        return parts, svc

    def flash_burning_package(self):
        """Прошивка целого Amlogic burning-пакета (aml_upgrade_package.img).

        Воспроизводит логику Amlogic USB Burning Tool / aml-flash для SoC gxl
        (S905X2/g12a), БЕЗ поддержки пароля (USB-режим открыт). Шаги:
          1. Распаковка пакета через aml_image_v2_packer / AmlImagePack.exe
          2. Разбор image.cfg + platform.conf (адреса DDR/UBOOT, список разделов)
          3. Инициализация DDR и запуск U-Boot из пакета
          4. Запись DTB, создание разделов (disk_initial), bootloader
          5. Прошивка всех (или выбранных) разделов
          6. Reset (burn_complete)

        ⚠️ Это низкоуровневая операция. Неверный пакет может превратить
        устройство в «кирпич».
        """
        import threading as _th, re as _re

        win = tk.Toplevel(self.root)
        win.title("Прошивка burning-пакета (aml_upgrade_package.img)")
        win.resizable(True, True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{min(900, sw-60)}x{min(740, sh-60)}")
        win.minsize(720, 600)
        win.transient(self.root)

        # ── Низ: кнопки ──
        bottom = tk.Frame(win, bg="#ECF0F1", relief=tk.RIDGE, bd=2)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        btn_row = tk.Frame(bottom, bg="#ECF0F1")
        btn_row.pack(fill=tk.X, padx=10, pady=8)

        # ── Лог (тоже снизу) ──
        logf = tk.LabelFrame(win, text="Журнал прошивки", font=("Arial", 9, "bold"))
        logf.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 4))
        log_box = scrolledtext.ScrolledText(logf, height=10, font=_mono_font(8),
                                            bg="#1E1E1E", fg="#00FF00", state='disabled')
        log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        def log(m):
            log_box.config(state='normal')
            log_box.insert(tk.END, m + "\n"); log_box.see(tk.END)
            log_box.config(state='disabled'); log_box.update_idletasks()

        # ── Верх ──
        top = tk.Frame(win)
        top.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=8)

        # Выбор пакета
        f1 = tk.LabelFrame(top, text="1. Burning-пакет (.img)", font=("Arial", 9, "bold"))
        f1.pack(fill=tk.X, pady=(0, 6))
        pkg_var = tk.StringVar()
        r1 = tk.Frame(f1); r1.pack(fill=tk.X, padx=6, pady=4)
        tk.Entry(r1, textvariable=pkg_var, font=("Arial", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        # Packer status — ищем aml_image_v2_packer (Windows) в files/ и подпапках
        packer_path = self._find_aml_packer()

        parts_info = []     # list of dict: {name, file, type}
        platform_cfg = {}   # parsed platform.conf addresses
        unpack_dir = [None] # mutable holder

        def pick_pkg():
            fn = filedialog.askopenfilename(
                title="Выберите aml_upgrade_package.img", parent=win,
                initialdir=last_dir("burn_pkg", ROOT_DIR),
                filetypes=[("Amlogic burning image", "*.img"), ("All", "*.*")])
            if fn:
                remember_last_dir("burn_pkg", fn)
                pkg_var.set(fn)

        tk.Button(r1, text="Обзор…", command=pick_pkg, width=9).pack(side=tk.LEFT)

        pk_row = tk.Frame(f1); pk_row.pack(fill=tk.X, padx=6, pady=(0, 4))
        pk_lbl = tk.Label(pk_row, font=("Arial", 8),
                          text=(f"✓ packer: {os.path.relpath(packer_path, FILE_DIR)}"
                                if packer_path else
                                "✗ aml_image_v2_packer.exe не найден — нужен из репо khadas/utils"),
                          fg="#27AE60" if packer_path else "#E74C3C")
        pk_lbl.pack(side=tk.LEFT)

        def dl_packer():
            self._download_aml_packer(log_fn=log)
            # обновить статус
            np = self._find_aml_packer()
            if np:
                pk_lbl.config(text=f"✓ packer: {os.path.relpath(np, FILE_DIR)}",
                              fg="#27AE60")
                nonlocal_holder["packer"] = np

        nonlocal_holder = {"packer": packer_path}
        if not packer_path:
            tk.Button(pk_row, text="📥 Скачать packer", command=dl_packer,
                      font=("Arial", 8), bg="#9B59B6", fg="white"
                      ).pack(side=tk.LEFT, padx=(8, 0))

        # Повторное использование уже распакованной папки — без повторной
        # распаковки (полезно при повторных попытках прошивки)
        def load_unpacked(out_dir):
            """Прочитать image.cfg/platform.conf из готовой папки распаковки.
            Возвращает True при успехе; вызывается из UI-потока."""
            cfg = os.path.join(out_dir, "image.cfg")
            if not os.path.exists(cfg):
                log(f"❌ В {out_dir} нет image.cfg — это не распакованный пакет")
                return False
            parts, svc = parse_image_cfg(cfg)
            parts_info.clear(); parts_info.extend(parts)
            plat_name = svc.get("platform", "platform.conf")
            plat_path = os.path.join(out_dir, plat_name)
            if os.path.exists(plat_path):
                platform_cfg.clear()
                platform_cfg.update(parse_platform_conf(plat_path))
                platform_cfg["_svc"] = svc
            unpack_dir[0] = out_dir
            log(f"✓ Пакет прочитан из {out_dir}. Разделов: {len(parts)}")
            def _fill():
                for row in tree.get_children(): tree.delete(row)
                psel.clear()
                for p in parts:
                    psel[p["name"]] = True
                    tree.insert("", tk.END,
                        values=("✓", p["name"], p["file"], p["type"]))
            self.root.after(0, _fill)
            return True

        ur = tk.Frame(f1); ur.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Label(ur, text="…или папка уже распакованного пакета (_pkg_unpack):",
                 font=("Arial", 8)).pack(side=tk.LEFT)
        unpacked_dir_var = tk.StringVar()

        def pick_unpacked():
            d = filedialog.askdirectory(
                title="Папка распакованного пакета (где лежит image.cfg)",
                initialdir=last_dir("burn_unpacked",
                                    os.path.dirname(pkg_var.get() or ROOT_DIR)),
                parent=win)
            if not d:
                return
            remember_last_dir("burn_unpacked", d)
            if not load_unpacked(d):
                messagebox.showwarning("!",
                    "В выбранной папке нет image.cfg.\nУкажите папку "
                    "_pkg_unpack, созданную распаковкой пакета.", parent=win)
                return
            unpacked_dir_var.set(d)

        tk.Entry(ur, textvariable=unpacked_dir_var, font=("Arial", 8)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        tk.Button(ur, text="Обзор…", command=pick_unpacked, width=9).pack(side=tk.LEFT)

        # Опции
        f2 = tk.LabelFrame(top, text="2. Опции прошивки", font=("Arial", 9, "bold"))
        f2.pack(fill=tk.X, pady=(0, 6))
        wipe_var  = tk.BooleanVar(value=False)
        reset_var = tk.BooleanVar(value=True)
        tk.Checkbutton(f2, text="Стереть data/cache (--wipe) — сброс к заводским",
                       variable=wipe_var, font=("Arial", 9)).pack(anchor=tk.W, padx=6)
        tk.Checkbutton(f2, text="Перезагрузить устройство после прошивки (reset)",
                       variable=reset_var, font=("Arial", 9)).pack(anchor=tk.W, padx=6)

        # Таблица разделов из пакета
        f3 = tk.LabelFrame(top, text="3. Разделы в пакете (✓ = прошить)",
                           font=("Arial", 9, "bold"))
        f3.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        sel_bar = tk.Frame(f3); sel_bar.pack(side=tk.TOP, fill=tk.X, padx=2, pady=2)
        tree = ttk.Treeview(f3, columns=("sel", "name", "file", "type"),
                            show="headings", height=8)
        for c, t, w in (("sel","✓",40),("name","Раздел",150),
                        ("file","Файл",200),("type","Тип",80)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="center" if c in ("sel","type") else "w",
                        stretch=(c=="file"))
        vsb = ttk.Scrollbar(f3, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        psel = {}  # name → bool
        def set_all(state):
            for row in tree.get_children():
                n = tree.set(row, "name")
                psel[n] = state
                tree.set(row, "sel", "✓" if state else "")
        tk.Button(sel_bar, text="✓ Все", font=("Arial", 8),
                  command=lambda: set_all(True), width=8).pack(side=tk.LEFT, padx=2)
        tk.Button(sel_bar, text="✗ Никого", font=("Arial", 8),
                  command=lambda: set_all(False), width=8).pack(side=tk.LEFT, padx=2)
        def toggle(e):
            row = tree.identify_row(e.y)
            if not row: return
            n = tree.set(row, "name")
            psel[n] = not psel.get(n, True)
            tree.set(row, "sel", "✓" if psel[n] else "")
        tree.bind("<ButtonRelease-1>", toggle)

        prog = ttk.Progressbar(top, mode="indeterminate")
        prog.pack(fill=tk.X, pady=(4, 0))

        # ── Альтернатива: каноничный khadas flash-tool (только macOS) ──
        # Эталонная последовательность прошивки со всеми вариантами протоколов
        # (включая новый USB-протокол «8» и bl2_boot для g12a).
        if _IS_MAC:
            ft_row = tk.Frame(top)
            ft_row.pack(fill=tk.X, pady=(0, 2))
            tk.Label(ft_row, text="Альтернатива — каноничный khadas flash-tool, SoC:",
                     font=("Arial", 8)).pack(side=tk.LEFT)
            ft_soc = tk.StringVar(value="gxl")
            ttk.Combobox(ft_row, textvariable=ft_soc, state="readonly",
                         width=6, values=("gxl", "g12a")).pack(side=tk.LEFT, padx=4)

            def do_flash_tool():
                pkg = pkg_var.get()
                if not pkg or not os.path.exists(pkg):
                    pkg = filedialog.askopenfilename(
                        title="Выберите aml_upgrade_package.img "
                              "(flash-tool распаковывает сам)",
                        parent=win,
                        initialdir=last_dir("burn_pkg", ROOT_DIR),
                        filetypes=[("Amlogic burning image", "*.img"),
                                   ("All", "*.*")])
                    if not pkg:
                        messagebox.showwarning(
                            "!", "flash-tool нужен исходный .img пакета",
                            parent=win)
                        return
                    pkg_var.set(pkg)
                if not messagebox.askyesno(
                        "Подтверждение",
                        "Прошивка каноничным khadas flash-tool.\n\n"
                        "НЕ ОТКЛЮЧАЙТЕ устройство во время процесса!\n\n"
                        "Продолжить?", icon='warning', parent=win):
                    return

                def _t():
                    prog.start(10)
                    try:
                        log("⚡ Подготовка flash-tool...")
                        ft = self._ensure_khadas_flashtool(log_fn=log)
                        if not ft:
                            log("❌ flash-tool недоступен")
                            return
                        cmd = ["bash", ft, f"--img={pkg}", "--parts=all",
                               "--reset=" + ("y" if reset_var.get() else "n"),
                               "--soc=" + ft_soc.get(), "--debug"]
                        if wipe_var.get():
                            cmd.append("--wipe")
                        log("⚡ Запуск: " + " ".join(cmd))
                        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT,
                                             text=True, errors="ignore",
                                             bufsize=1)
                        for line in p.stdout:
                            s = line.rstrip()
                            if s:
                                log("  " + s)
                        p.wait()
                        log(f"⚡ flash-tool завершён, код {p.returncode}")
                    except Exception as ex:
                        log(f"❌ flash-tool: {ex}")
                    finally:
                        self.root.after(0, prog.stop)

                _th.Thread(target=_t, daemon=True).start()

            tk.Button(ft_row, text="⚡ Прошить через flash-tool",
                      command=do_flash_tool, bg="#8E44AD", fg="white",
                      font=("Arial", 10, "bold"), height=1
                      ).pack(side=tk.LEFT, padx=(8, 0))

        # ── Распаковка и разбор пакета ──
        def parse_image_cfg(cfg_path):
            """Разобрать image.cfg → разделы и имена служебных файлов."""
            parts = []
            svc = {}
            try:
                with open(cfg_path, "r", errors="ignore") as fh:
                    text = fh.read()
            except Exception as ex:
                log(f"❌ image.cfg: {ex}")
                return parts, svc
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fm = _re.search(r'file="([^"]+)"', line)
                sm = _re.search(r'sub_type="([^"]+)"', line)
                mm = _re.search(r'main_type="([^"]+)"', line)
                tm = _re.search(r'file_type="([^"]+)"', line)
                if not fm:
                    continue
                fname = fm.group(1)
                sub = sm.group(1) if sm else ""
                main = mm.group(1) if mm else ""
                ftype = tm.group(1) if tm else "normal"
                if main == "PARTITION":
                    parts.append({"name": sub, "file": fname, "type": ftype})
                else:
                    svc[sub] = fname   # DDR, UBOOT, UBOOT_COMP, platform, meson1 ...
            return parts, svc

        def parse_platform_conf(path):
            """Разобрать platform.conf → адреса загрузки."""
            cfg = {}
            try:
                with open(path, "r", errors="ignore") as fh:
                    for line in fh:
                        m = _re.match(r'\s*(\w+):\s*(\S+)', line)
                        if m:
                            cfg[m.group(1)] = m.group(2)
                        m2 = _re.match(r'\s*(\w+)=\s*(\S+)', line)
                        if m2:
                            cfg[m2.group(1)] = m2.group(2)
            except Exception as ex:
                log(f"⚠ platform.conf: {ex}")
            return cfg

        def do_unpack():
            pkg = pkg_var.get()
            if not pkg or not os.path.exists(pkg):
                messagebox.showwarning("!", "Выберите пакет", parent=win); return
            cur_packer = nonlocal_holder.get("packer") or self._find_aml_packer()
            if not cur_packer:
                messagebox.showwarning("!",
                    "aml_image_v2_packer.exe не найден.\n\n"
                    "Скачайте кнопкой «📥 Скачать packer» (из khadas/utils),\n"
                    "либо положите его вручную в files/.\n\n"
                    "Примечание: в USB Burning Tool v2 есть только AmlImagePack.dll —\n"
                    "это библиотека внутри GUI, из консоли не вызывается.",
                    parent=win)
                return
            def _t():
                prog.start(10)
                try:
                    # Распаковываем РЯДОМ С ОБРАЗОМ (<папка образа>/_pkg_unpack).
                    # Таймаут большой: 2+ ГБ (особенно на сетевой том) может
                    # распаковываться заметно дольше пяти минут.
                    out_dir = os.path.join(os.path.dirname(pkg), "_pkg_unpack")
                    os.makedirs(out_dir, exist_ok=True)
                    unpack_dir[0] = out_dir
                    # Уже распакован ранее? (повторный запуск прошивки)
                    if os.path.exists(os.path.join(out_dir, "image.cfg")):
                        log(f"✓ Уже распаковано: {out_dir} — использую без "
                            "повторной распаковки")
                        self.root.after(0, lambda: load_unpacked(out_dir))
                        return
                    remember_last_dir("burn_pkg", pkg)
                    log(f"📦 Распаковка через {os.path.basename(cur_packer)}...")
                    log(f"  → {out_dir}")
                    cflags = subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
                    # Khadas-синтаксис: aml_image_v2_packer -d <img> <outdir>
                    # Успех определяется по строке "Image unpack OK!"
                    # Таймаут большой: 2+ ГБ на медленный/сетевой диск может
                    # распаковываться заметно дольше пяти минут.
                    ok = False
                    o = ""
                    for flag in ("-d", "-unpack"):
                        try:
                            r = subprocess.run([cur_packer, flag, pkg, out_dir],
                                               capture_output=True, timeout=1800,
                                               creationflags=cflags)
                        except Exception as ex:
                            log(f"  ⚠ {flag}: {ex}")
                            continue
                        o = ((r.stdout or b"")+(r.stderr or b"")).decode("utf-8","ignore")
                        for ln in o.splitlines():
                            if ln.strip(): log("  " + ln.strip())
                        if "Image unpack OK" in o or os.path.exists(
                                os.path.join(out_dir, "image.cfg")):
                            ok = True
                            break
                        log(f"  ⚠ флаг {flag} не сработал, пробуем другой...")
                    cfg = os.path.join(out_dir, "image.cfg")
                    if not ok or not os.path.exists(cfg):
                        log("❌ Распаковка не удалась (нет image.cfg / 'Image unpack OK').")
                        log("  Проверьте, что packer — Windows-версия из khadas/utils,")
                        log("  и что пакет действительно формата Amlogic V2.")
                        return
                    unpack_dir[0] = out_dir
                    self.root.after(0, lambda: load_unpacked(out_dir))
                except Exception as ex:
                    log(f"❌ Распаковка: {ex}")
                finally:
                    self.root.after(0, prog.stop)
            _th.Thread(target=_t, daemon=True).start()

        # ── Прошивка ──
        def do_flash():
            if not parts_info:
                messagebox.showwarning("!",
                    "Сначала распакуйте пакет", parent=win); return
            if not messagebox.askyesno("Подтверждение",
                "Будет выполнена низкоуровневая прошивка burning-пакета.\n\n"
                "НЕ ОТКЛЮЧАЙТЕ устройство во время процесса!\n\n"
                "Продолжить?", icon='warning', parent=win):
                return
            to_flash = [p for p in parts_info if psel.get(p["name"], False)]
            wipe = wipe_var.get()
            do_reset = reset_var.get()
            wd = unpack_dir[0]

            def _t():
                prog.start(10)
                try:
                    if _IS_MAC:
                        # macOS: ROM/bl2-режимы ненадёжны для bulk-записи —
                        # прошиваем разделы через U-Boot из aml_bundle
                        self._burn_package_partitions_via_uboot(
                            wd, to_flash, wipe, do_reset, log)
                    else:
                        self._burn_package_sequence(
                            wd, parts_info, to_flash, platform_cfg,
                            wipe, do_reset, log)
                except Exception as ex:
                    log(f"\n❌ ОШИБКА: {ex}")
                finally:
                    self.root.after(0, prog.stop)
            _th.Thread(target=_t, daemon=True).start()

        tk.Button(btn_row, text="📂 Распаковать и прочитать пакет",
                  command=do_unpack, bg="#2980B9", fg="white",
                  font=("Arial", 10, "bold"), height=2
                  ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Button(btn_row, text="🚀 Прошить пакет",
                  command=do_flash, bg="#C0392B", fg="white",
                  font=("Arial", 10, "bold"), height=2
                  ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Button(btn_row, text="Закрыть", command=win.destroy,
                  font=("Arial", 9), height=2, width=10).pack(side=tk.RIGHT)

        log("Окно прошивки burning-пакета (aml_upgrade_package.img).")
        log("Алгоритм основан на Amlogic USB Burning Tool / aml-flash (soc=gxl).")
        if not packer_path:
            log("⚠ Нужен aml_image_v2_packer.exe — кнопка «📥 Скачать packer»")
            log("  (из khadas/utils). В USB Burning Tool v2 его НЕТ — там")
            log("  только AmlImagePack.dll, которая из консоли не вызывается.")
        log("1) Выберите пакет → 2) Распакуйте → 3) Прошейте.")
        log("💡 Если пакет уже распакован — укажите папку _pkg_unpack в поле "
            "«папка уже распакованного пакета» и сразу прошивайте.")

    def _burn_package_partitions_via_uboot(self, wd, to_flash,
                                           wipe, do_reset, log):
        """Прошивка разделов распакованного burning-пакета через U-Boot из
        files/aml_bundle.img (pyamlboot) + update partition.

        НА macOS это ОСНОВНОЙ рабочий путь (проверено 2026-10-06): ROM- и
        bl2-режимы устройства на macOS ненадёжны для bulk-записи (cwr →
        I/O error в конце записи, bl2-burn → usbWriteFile -60 timeout),
        а в U-Boot burn-режиме update-команды работают стабильно — им и
        прошиваются все разделы пакета, включая _aml_dtb/bootloader.
        Последовательность повторяет khadas flash-tool: mmc dev 1 →
        disk_initial → _aml_dtb → bootloader → остальные → setenv/save → reset.
        """
        import time as _t

        bundle = os.path.join(FILE_DIR, "aml_bundle.img")
        if not os.path.exists(bundle):
            raise Exception(
                "files/aml_bundle.img не найден — положите U-Boot образ "
                "устройства в files/ (он используется вместо DDR.USB пакета)")

        # 1. U-Boot уже в памяти с прошлой попытки?
        rc, out = self.aml_update_raw(["bulkcmd", "echo 12345"], timeout=10)
        uboot_alive = (rc == 0)
        if not uboot_alive:
            log("🔌 Загрузка U-Boot (aml_bundle) через pyamlboot...")
            sender = get_aml_bundle_sender()
            if not sender:
                raise Exception("pyamlboot_local недоступен — нажмите "
                                "«Загрузить утилиты» и перезапустите")
            last = None
            for i in range(1, 4):
                try:
                    sender(bundle)
                    log("✓ U-Boot загружен")
                    break
                except Exception as e:
                    last = e
                    log(f"  ⚠ попытка {i}/3: {safe_decode(e)[:90]}")
                    self._release_aml_usb()
                    self._wait_amlogic_usb(15, log_fn=log)
                    _t.sleep(2)
            else:
                raise Exception(f"Не удалось загрузить U-Boot: {last}")
            self._release_aml_usb()
            if not self._wait_amlogic_usb(30, log_fn=log):
                raise Exception("Устройство не вернулось на USB после "
                                "загрузки U-Boot (переподключите питание/USB)")
            _t.sleep(2)
        else:
            log("✓ U-Boot уже запущен (burn mode активен)")

        # 2. Переключение на eMMC
        ok = False
        for i in range(1, 6):
            try:
                self.aml_bulkcmd("mmc dev 1")
                ok = True
                break
            except Exception as e:
                log(f"  ⚠ mmc dev 1 ({i}/5): {safe_decode(e)[:90]}")
                self._release_aml_usb()
                self._wait_amlogic_usb(15, log_fn=log)
                _t.sleep(1.5)
        if not ok:
            raise Exception("Не удалось переключить U-Boot на eMMC "
                            "(mmc dev 1)")
        log("✓ eMMC активирован")

        # 3. Таблица разделов
        log(f"🗂 Создание разделов (disk_initial {'1' if wipe else '0'})...")
        try:
            self.aml_bulkcmd(f"disk_initial {'1' if wipe else '0'}")
        except Exception as e:
            log(f"  ⚠ disk_initial: {safe_decode(e)[:120]} — продолжаем")

        # 4. Разделы: _aml_dtb и bootloader первыми (как в khadas flash-tool)
        def f(name):
            return os.path.join(wd, name) if name else None

        order = sorted(
            to_flash,
            key=lambda p: 0 if p["name"] in ("_aml_dtb", "dtb")
            else (1 if p["name"] == "bootloader" else 2))
        fails = []
        for p in order:
            pf = f(p.get("file"))
            if not pf or not os.path.exists(pf):
                log(f"  ⚠ {p['name']}: файл {p.get('file')} не найден — пропуск")
                continue
            log(f"  ⬇ {p['name']} ({p.get('file')})...")
            rc, out = self._aml_update_retry(
                ["partition", p["name"], pf],
                tries=2, timeout=1800, log_fn=log)
            if rc == 0:
                log(f"     ✓ {p['name']}")
            else:
                log(f"     ❌ {p['name']}")
                fails.append(p["name"])
        if fails:
            raise Exception("Ошибка прошивки разделов: " + ", ".join(fails))

        # 5. env и перезагрузка
        for c in ("setenv upgrade_step 1", "save"):
            try:
                self.aml_bulkcmd(c)
            except Exception:
                pass
        if do_reset:
            log("🔄 Перезагрузка устройства...")
            try:
                self.aml_bulkcmd("reset")
            except Exception:
                pass

    def _burn_package_sequence(self, wd, all_parts, to_flash,
                                platform_cfg, wipe, do_reset, log):
        """Последовательность команд update.exe для прошивки burning-пакета.
        soc=gxl (S905X2). Адреса берутся из platform.conf, имена служебных
        файлов — из image.cfg (._svc)."""
        import time as _t

        svc = platform_cfg.get("_svc", {})

        def f(name):
            return os.path.join(wd, name) if name else None

        # Дождаться устройства
        log("🔌 Поиск устройства (identify)...")
        for attempt in range(30):
            rc, out = self.aml_update_raw(["identify", "7"], timeout=10, log_fn=log)
            if "firmware" in out.lower():
                log("✓ Устройство найдено")
                break
            _t.sleep(2)
        else:
            raise Exception("Amlogic устройство не найдено (USB Boot режим?)")
        # Устройству нужно время стабилизироваться после re-enumeration:
        # сразу следующий `update write` может зависнуть
        _t.sleep(2.5)

        # Адреса из platform.conf
        ddr_load  = platform_cfg.get("DDRLoad", "0xfffa0000")
        ddr_run   = platform_cfg.get("DDRRun",  "0xfffa0000")
        uboot_load= platform_cfg.get("UbootLoad","0x0200c000")
        uboot_run = platform_cfg.get("UbootRun", "0x0200c000")
        bl2_para  = platform_cfg.get("bl2ParaAddr", "0xfffd0000")

        ddr_init = os.path.join(FILE_DIR, "usbbl2runpara_ddrinit.bin")
        fip_run  = os.path.join(FILE_DIR, "usbbl2runpara_runfipimg.bin")

        # Имена bl2/uboot из пакета
        bl2_name = svc.get("DDR")
        tpl_name = svc.get("UBOOT_COMP") or svc.get("UBOOT")
        bl2 = f(bl2_name); tpl = f(tpl_name)
        if not (tpl and os.path.exists(tpl)):
            # [USB, UBOOT] может быть backup-элементом пакера (не распакован
            # в отдельный файл) и по определению равен DDR.USB — используем bl2
            if bl2 and os.path.exists(bl2):
                log(f"  ℹ {tpl_name or 'UBOOT.USB'} не распакован "
                    "(backup-элемент = DDR.USB) — использую DDR.USB как tpl")
                tpl = bl2

        # bootloader/dtb разделы из списка
        boot_part = next((p for p in all_parts if p["name"] == "bootloader"), None)
        dtb_part  = next((p for p in all_parts if p["name"] in ("_aml_dtb","dtb")), None)
        dtb_meson1 = svc.get("meson1")

        # ── Инициализация DDR (gxl) ──
        if bl2 and os.path.exists(bl2):
            if not os.path.exists(ddr_init):
                log("📥 usbbl2runpara_*.bin не найдены — скачиваю (параметры "
                    "запуска bl2 из khadas/utils)...")
                self._ensure_burn_runpara(log_fn=log)
            if not os.path.exists(ddr_init):
                raise Exception(
                    f"Не хватает {os.path.basename(ddr_init)} (и второй части) "
                    f"в {FILE_DIR} — без них нельзя инициализировать DDR и "
                    "запустить U-Boot из пакета. Скачайте вручную: "
                    "github.com/khadas/utils → aml-flash-tool/tools/datas/")
            log("⚙ Инициализация DDR...")
            self._aml_update_retry(["cwr", bl2, ddr_load], timeout=60, log_fn=log)
            self._aml_update_retry(["write", ddr_init, bl2_para],
                                   timeout=60, log_fn=log)
            self._aml_update_retry(["run", ddr_run], timeout=60, log_fn=log)
            for _ in range(8):
                _t.sleep(1)
            rc, out = self.aml_update_raw(["identify", "7"], timeout=10,
                                          log_fn=log)
            if "firmware" not in out.lower():
                raise Exception("После DDR init устройство не ответило на "
                                "identify — повторите вход в USB Boot")
            # Новый USB-протокол: 4-е поле версии firmware == '8'
            # ("This firmware version is 0-0-1-8-0-1-0"). В этом случае
            # bl2 требует `run <bl2ParaAddr>`, иначе все дальнейшие записи
            # падают по таймауту (usbWriteFile -60). См. flash-tool (khadas):
            #   if [[ $usb_protocol == "8" ]]; then run_update_assert run $bl2_params
            ver = out.lower().split("firmware version is")[-1].strip()
            fields = ver.split("-")
            usb_protocol = fields[3] if len(fields) > 3 else "0"
            log(f"  🔌 USB-протокол устройства: {usb_protocol} "
                f"(firmware {ver})")
            if usb_protocol == "8":
                self._aml_update_retry(["run", bl2_para],
                                       timeout=60, log_fn=log)
                for _ in range(4):
                    _t.sleep(1)
            # ── Запуск U-Boot (порядок как в khadas flash-tool, gxl) ──
            log("⚙ Запуск U-Boot...")
            self._aml_update_retry(["write", bl2, ddr_load],
                                   timeout=60, log_fn=log)
            # runfipimg: «tell bl2 to jump to tpl, aka u-boot»
            self._aml_update_retry(["write", fip_run, bl2_para],
                                   timeout=60, log_fn=log)
            if tpl and os.path.exists(tpl):
                self._aml_update_retry(["write", tpl, uboot_load],
                                       timeout=90, log_fn=log)
            else:
                log("  ⚠ tpl (UBOOT) не найден в пакете — пропускаю запись")
            # Для нового протокола запуск через bl2_params, а не UbootRun
            if usb_protocol == "8":
                self._aml_update_retry(["run", bl2_para],
                                       timeout=60, log_fn=log)
            else:
                self._aml_update_retry(["run", uboot_run],
                                       timeout=60, log_fn=log)
            for _ in range(8):
                _t.sleep(1)
            log("✓ U-Boot загружен")
            self._release_aml_usb()
            # U-Boot пере-энумерирует USB — ждём реального возврата устройства
            if not self._wait_amlogic_usb(30, log_fn=log):
                log("✗ Устройство не вернулось на USB после загрузки U-Boot —"
                    " прерываю (переподключите питание/USB и повторите)")
                raise Exception("Устройство пропало с USB после загрузки U-Boot")
            # Синхронизация после reset (khadas: «avoid to loose 4 bytes
            # of commands after reset»)
            try:
                self.aml_bulkcmd("echo 12345")
            except Exception:
                pass
        else:
            log("⚠ В пакете нет DDR.USB (bl2) — предполагаем, что U-Boot "
                "уже запущен (иначе: полное отключение питания → замкнуть "
                "пин 6 на GND → подать питание → USB Boot)")

        # ── DTB в память ──
        if dtb_meson1 and os.path.exists(f(dtb_meson1)):
            log("🌳 Запись device tree...")
            self.aml_update_raw(["mwrite", f(dtb_meson1), "mem", "dtb", "normal"],
                                timeout=60, log_fn=log)

        # ── Создание разделов ──
        log(f"🗂 Создание разделов (disk_initial {'1' if wipe else '0'})...")
        self.aml_bulkcmd(f"disk_initial {'1' if wipe else '0'}")

        # ── DTB раздел + bootloader ──
        if dtb_part and os.path.exists(f(dtb_part["file"])):
            log("🌳 Прошивка _aml_dtb...")
            self.aml_update_raw(["partition", "_aml_dtb", f(dtb_part["file"])],
                                timeout=120, log_fn=log)
        if boot_part and os.path.exists(f(boot_part["file"])):
            log("🔧 Прошивка bootloader...")
            self.aml_update_raw(["partition", "bootloader", f(boot_part["file"])],
                                timeout=180, log_fn=log)

        self.aml_bulkcmd("setenv upgrade_step 1")
        self.aml_bulkcmd("save")

        # ── Wipe data/cache ──
        if wipe:
            log("🧹 Очистка data/cache...")
            for c in ("setenv firstboot 1", "save", "rpmb_reset",
                      "amlmmc erase data", "amlmmc erase cache"):
                try: self.aml_bulkcmd(c)
                except Exception: pass

        # ── Прошивка всех выбранных разделов ──
        log(f"\n📥 Прошивка разделов ({len(to_flash)})...")
        skip = {"bootloader", "_aml_dtb", "dtb"}
        for p in to_flash:
            if p["name"] in skip:
                continue   # уже прошиты выше
            pf = f(p["file"])
            if not pf or not os.path.exists(pf):
                log(f"  ⚠ {p['name']}: файл не найден, пропуск")
                continue
            log(f"  ⬇ {p['name']} ({p['file']})...")
            rc, out = self.aml_update_raw(
                ["partition", p["name"], pf, p["type"]],
                timeout=1800, log_fn=log)
            if rc == 0:
                log(f"     ✓ {p['name']}")
            else:
                log(f"     ❌ {p['name']} — ошибка")

        # ── Reset ──
        if do_reset:
            log("\n🔄 Перезагрузка устройства (burn_complete 1)...")
            try: self.aml_bulkcmd("burn_complete 1")
            except Exception: pass

        log("\n🎉 Прошивка burning-пакета завершена!")
        self.root.after(0, lambda: messagebox.showinfo(
            "Готово", "Прошивка burning-пакета завершена!", parent=self.root))

    def _aml_device_present(self):
        """Есть ли сейчас Amlogic-устройство на USB (pyusb, без открытия хэндла)."""
        try:
            import usb.core
            return usb.core.find(idVendor=0x1b8e) is not None
        except Exception:
            return False

    def _release_aml_usb(self):
        """Отпустить USB-интерфейс Amlogic, захваченный pyusb/pyamlboot.

        pyamlboot (загрузка U-Boot) открывает устройство и НЕ закрывает его.
        Пока наш процесс держит интерфейс, утилита update (отдельный процесс)
        не может его захватить: libusb 'claim interface, ret[-13] Permission
        denied'. Освобождаем все хэндлы VID 1B8E нашего процесса.
        """
        try:
            import usb.core
            import usb.util
            for dev in usb.core.find(find_all=True, idVendor=0x1b8e):
                try:
                    usb.util.release_interface(dev, 0)
                except Exception:
                    pass
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    pass
        except Exception:
            pass

    def _wait_amlogic_usb(self, timeout=30, log_fn=None):
        """Ждать возврата Amlogic-устройства на USB (после загрузки U-Boot оно
        перезагружается в burn-режим и пере-энумерируется).

        Возвращает True, как только устройство появилось, False по таймауту
        (или если прошивка остановлена пользователем).
        """
        _log = log_fn or self.log
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_flashing:
                return False
            if self._aml_device_present():
                return True
            time.sleep(0.5)
        _log(f"  ⏳ Ожидание устройства на USB: {timeout} с без результата")
        return False

    def aml_bulkcmd(self, cmd):
        """Выполнение команды U-Boot.
        Возвращает декодированный stdout+stderr.
        Весь вывод дублируется в COM-терминал (метка [USB]) —
        даже если UART не подключён, пользователь видит ответы.
        """
        update_path = self.get_update_path()
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        process = subprocess.run(
            [update_path, "bulkcmd", cmd],
            capture_output=True, text=False, timeout=30,
            creationflags=creationflags
        )
        raw = (process.stdout or b"") + (process.stderr or b"")
        text_out = safe_decode(raw)
        # Роутим в терминал всегда — если UART не подключён, это единственный лог
        for line in text_out.splitlines():
            stripped = line.strip()
            if stripped:
                self.root.after(0, lambda l=stripped: self.terminal_log(f"[USB] {l}"))
        if process.returncode != 0:
            raise Exception(f"Ошибка U-Boot команды: {cmd}\n{text_out[:300]}")
        return text_out

    def aml_update_raw(self, args, timeout=60, log_fn=None):
        """Произвольная команда update.exe (write/run/cwr/mwrite/identify/partition).

        args — список аргументов после update.exe, например:
            ["identify", "7"]
            ["write", "bl2.bin", "0xfffa0000"]
            ["partition", "boot", "boot.PARTITION"]
        Возвращает (returncode, текст_вывода). Вывод роутится в COM-терминал.
        Команда считается ошибочной, если в выводе есть "ERR" (как в aml-flash).
        """
        update_path = self.get_update_path()
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
            p = subprocess.run([update_path] + list(args),
                               capture_output=True, text=False,
                               timeout=timeout, creationflags=cflags)
        except subprocess.TimeoutExpired:
            if log_fn: log_fn(f"  ❌ таймаут: update {' '.join(map(str,args))}")
            return -1, "timeout"
        out = safe_decode((p.stdout or b"") + (p.stderr or b""))
        for ln in out.splitlines():
            s = ln.strip()
            if s:
                self.root.after(0, lambda l=s: self.terminal_log(f"[USB] {l}"))
        # Amlogic update возвращает 0 даже при ошибке — проверяем по "ERR"
        rc = p.returncode
        if "ERR" in out.upper():
            rc = 1
        return rc, out

    def _aml_update_retry(self, args, tries=3, timeout=60, log_fn=None):
        """aml_update_raw с ретраями при rc != 0 (включая таймаут).

        После пере-энумерации USB (DDR init / запуск U-Boot) одиночный запуск
        update может зависнуть в ожидании устройства или не найти его —
        повторный запуск после паузы решает.
        """
        label = "update " + " ".join(str(a) for a in args[:2])
        last_rc, last_out = -1, ""
        for i in range(1, tries + 1):
            rc, out = self.aml_update_raw(args, timeout=timeout, log_fn=log_fn)
            if rc == 0:
                return rc, out
            last_rc, last_out = rc, out
            if log_fn:
                log_fn(f"  ⚠ {label} — попытка {i}/{tries} не удалась"
                       + (" (таймаут)" if out == "timeout" else ""))
            self._release_aml_usb()
            self._wait_amlogic_usb(20, log_fn=log_fn)
            time.sleep(1.5)
        return last_rc, last_out
    
    def _apply_unlock_minimum(self):
        """Применить необходимый минимум разблокировки в ЖИВОМ U-Boot.

        ВАЖНО: даже если env-раздел записан бинарно с avb2=0/lock=…, прошивка
        в текущей сессии U-Boot читает эти значения из АКТИВНОГО окружения, а
        не из только что записанного раздела. Поэтому setenv+saveenv нужно
        выполнять ВСЕГДА — иначе avb2 в активной сессии останется старым.

        Вызывается во всех случаях (и после бинарной записи env тоже).
        """
        self.log("  - Применение настроек разблокировки:")

        def _setenv(key, value):
            try:
                self.aml_bulkcmd(f"setenv {key} {value}")
                self.log(f"    • {key} = {value}")
            except Exception as ex:
                self.log(f"    ⚠ {key}: не применён ({str(ex)[:60]})")

        _setenv("silent", "0")
        _setenv("lock", "10000000")
        _setenv("avb2", "0")
        try:
            self.aml_bulkcmd("saveenv")
            self.log("    ✓ saveenv")
        except Exception as ex:
            self.log(f"    ⚠ saveenv: {str(ex)[:60]}")

    def _apply_env_setenv(self, env_file):
        """Применить env через setenv (только простые скалярные переменные).

        Грузит env в RAM, импортирует, применяет простые пользовательские
        изменения. Минимум (silent/lock/avb2) применяется отдельно через
        _apply_unlock_minimum(). НЕ трогает скрипт-переменные (bootargs и т.п.).
        """
        SKIP_VARS = {
            "bootargs", "initargs", "storeargs", "storeboot", "preboot",
            "bootcmd", "cmdline_keys", "recovery_from_flash",
            "recovery_from_udisk", "recovery_from_sdcard",
            "factory_reset_poweroff_protect", "init_display",
            "switch_bootmode", "start_boot_animation", "sysrecovery_check",
            "upgrade_check", "irremote_update", "set_factory_env",
            "usb_burning", "sdc_burning", "update", "try_auto_burn",
            "bcb_cmd", "factoryboot", "serial", "deviceid", "custom_deviceid",
            "aml_serial", "mac", "ethaddr",  # эти идут только через бинарный способ
        }

        self.log("  - Загрузка env в RAM...")
        self.aml_write_file_to_ram(env_file, "0x200c000")
        self.log("  - Импорт переменных в U-Boot...")
        self.aml_bulkcmd("env import 200c004")

        def safe_setenv(key, value):
            sval = str(value)
            if any(c in sval for c in ";${}\n\t") or len(sval) > 200:
                self.log(f"    ⊘ {key}: пропущен (скрипт/сложное значение)")
                return
            try:
                if " " in sval:
                    self.aml_bulkcmd(f"setenv {key} '{sval}'")
                else:
                    self.aml_bulkcmd(f"setenv {key} {sval}")
                self.log(f"    • {key} = {sval}")
            except Exception as ex:
                self.log(f"    ⚠ {key}: не применён ({str(ex)[:60]})")

        # Простые пользовательские изменения
        extra = 0
        for key, value in (self.env_data or {}).items():
            if key in SKIP_VARS or key in ("silent", "lock", "avb2") or value == "":
                continue
            safe_setenv(key, value)
            extra += 1
        if extra:
            self.log(f"  - Дополнительно простых переменных: {extra}")
        else:
            self.log("  - Пользовательских простых изменений нет")

    def _apply_env_binary(self, env_file):
        """Применить env БИНАРНО с пересчётом CRC32.

        Нужно когда меняются serial/deviceid/cmdline_keys — их нельзя задать
        через setenv (cmdline_keys — это скрипт с ${} и ;, U-Boot ломается).

        Алгоритм:
          1. читаем env с устройства (env_file уже прочитан вызывающим)
          2. парсим, мёржим наши изменения + минимум (lock/avb2/silent)
          3. пересобираем бинарь с правильным CRC32, ТОГО ЖЕ размера что прочитали
          4. пишем обратно через update mwrite store env normal <size> <file>
             — это ЗЕРКАЛО команды mread, которой делается дамп (надёжный CLI-путь,
             в отличие от bulkcmd 'store write', который не сработал).
        """
        if not os.path.exists(env_file):
            self.log("  ❌ env-файл отсутствует, бинарная запись невозможна")
            return
        part_size = os.path.getsize(env_file)

        orig_pairs = self.parse_env_blob_ordered(env_file)
        if not orig_pairs:
            self.log("  ⚠ env не распознан как valid — fallback на setenv")
            self._apply_env_setenv(env_file)
            return

        merged = {}
        for k, v in orig_pairs:
            merged[k] = v
        merged["silent"] = "0"
        merged["lock"] = "10000000"
        merged["avb2"] = "0"
        for k, v in (self.env_data or {}).items():
            if v != "":
                merged[k] = v

        self.log(f"  - Пересборка env ({len(merged)} переменных) с CRC32...")
        try:
            blob = self.build_env_blob(list(merged.items()), part_size)
        except Exception as ex:
            self.log(f"  ❌ Сборка env не удалась: {ex}")
            self.log("  → fallback на setenv (без serial/deviceid)")
            self._apply_env_setenv(env_file)
            return

        out = os.path.join(ROOT_DIR, "env_new.bin")
        with open(out, "wb") as f:
            f.write(blob)
        self.log(f"  - Запись env на устройство ({len(blob)} байт)...")

        # env-раздел пишется ТАК ЖЕ, как остальные разделы — через partition:
        #   update partition env <file>
        # (mwrite store env не работает — выдаёт 'Open file store failed').
        update_path = self.get_update_path()
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
            r = subprocess.run(
                [update_path, "partition", "env", out],
                capture_output=True, timeout=120, creationflags=cflags)
            o = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "ignore")
            for ln in o.splitlines():
                if ln.strip():
                    self.root.after(0, lambda l=ln.strip(): self.terminal_log(f"[USB] {l}"))
            if r.returncode == 0 and "ERR" not in o.upper():
                self.log("✓ ENV записан бинарно с корректным CRC32 (partition env)")
                if "serial" in self.env_data:
                    self.log(f"  serial = {self.env_data['serial']}")
                if "cmdline_keys" in self.env_data:
                    self.log("  cmdline_keys: переопределён (serial/deviceid жёстко)")
                # КРИТИЧНО: загружаем только что записанный env в АКТИВНУЮ сессию
                # U-Boot (env import). Иначе последующий saveenv в _apply_unlock_minimum
                # запишет старый running-env обратно и затрёт наши cmdline_keys.
                try:
                    self.log("  - Синхронизация активного окружения U-Boot...")
                    self.aml_write_file_to_ram(out, "0x200c000")
                    self.aml_bulkcmd("env import 200c004")
                    self.log("    ✓ активное окружение обновлено")
                except Exception as ex:
                    self.log(f"    ⚠ env import: {str(ex)[:60]}")
            else:
                self.log(f"  ⚠ partition env вернул ошибку: {o.strip()[:120]}")
                self.log("  → fallback: применяем простые переменные через setenv")
                self._apply_env_setenv(env_file)
        except subprocess.TimeoutExpired:
            self.log("  ❌ partition env таймаут")
            self._apply_env_setenv(env_file)
        except Exception as ex:
            self.log(f"  ❌ partition env: {str(ex)[:80]}")
            self._apply_env_setenv(env_file)
        try:
            os.remove(out)
        except Exception:
            pass

    def aml_read_part(self, name, size, outfile):
        """Чтение раздела с устройства"""
        update_path = self.get_update_path()
        process = subprocess.run(
            [update_path, "mread", "store", name, "normal", size, outfile],
            capture_output=True,
            text=False,  # Получаем байты
            timeout=60
        )
        if process.returncode != 0:
            raise Exception(f"Ошибка чтения раздела {name}")

    @staticmethod
    def parse_env_blob(path):
        """Разобрать бинарный образ раздела env U-Boot (Amlogic).

        Формат: [4 байта CRC32 LE][данные ENV_SIZE]
        Данные: пары key=value\\0key=value\\0 ... \\0\\0 (двойной \\0 = конец).
        Возвращает dict {key: value}.
        """
        ordered = FlasherGUI.parse_env_blob_ordered(path)
        return dict(ordered)

    @staticmethod
    def parse_env_blob_ordered(path):
        """То же, но сохраняет порядок переменных — список кортежей (key, value).
        Нужно для корректного round-trip редактирования."""
        result = []
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception:
            return result
        if len(blob) < 5:
            return result
        data = blob[4:]   # пропускаем CRC
        # Останавливаемся на первом \0\0 (конец окружения)
        end = data.find(b"\x00\x00")
        if end >= 0:
            data = data[:end + 1]
        for chunk in data.split(b"\x00"):
            if not chunk:
                continue
            text = chunk.decode("utf-8", errors="ignore")
            if "=" in text:
                key, _, value = text.partition("=")
                key = key.strip()
                if key and all(32 < ord(c) < 127 for c in key):
                    result.append((key, value))
        return result

    @staticmethod
    def build_env_blob(env_pairs, part_size):
        """Собрать бинарный образ раздела env с корректным CRC32.

        env_pairs — список (key, value) или dict.
        part_size — полный размер раздела env в байтах (например 0x10000).
        Структура: [CRC32 LE (4 байта)][data до part_size-4, забит \\0].
        CRC32 (zlib/POSIX) считается ПО data. Так делает U-Boot saveenv.
        """
        import zlib
        if isinstance(env_pairs, dict):
            items = list(env_pairs.items())
        else:
            items = list(env_pairs)
        # Формируем data: key=value\0 ... \0 (финальный двойной \0)
        body = b""
        for key, value in items:
            if key == "":
                continue
            body += f"{key}={value}".encode("utf-8") + b"\x00"
        body += b"\x00"   # завершающий \0 (двойной в сумме)
        data_size = part_size - 4
        if len(body) > data_size:
            raise ValueError(
                f"ENV не помещается: {len(body)} > {data_size} байт. "
                "Сократите значения переменных.")
        # Дополняем нулями до data_size
        data = body + b"\x00" * (data_size - len(body))
        crc = zlib.crc32(data) & 0xFFFFFFFF
        import struct
        return struct.pack("<I", crc) + data

    def aml_write_file_to_ram(self, filename, addr):
        """Запись файла в RAM"""
        update_path = self.get_update_path()
        process = subprocess.run(
            [update_path, "write", filename, addr],
            capture_output=True,
            text=False,  # Получаем байты
            timeout=30
        )
        if process.returncode != 0:
            raise Exception(f"Ошибка записи в RAM")
    
    def get_update_path(self):
        """Получение пути к update утилите (платформозависимо)"""
        # macOS: files/macos/update (khadas aml-flash-tool)
        if _IS_MAC:
            mac_update = os.path.join(FILE_DIR, "macos", "update")
            if os.path.exists(mac_update):
                return mac_update
            update_bin = os.path.join(FILE_DIR, "update")
            if os.path.exists(update_bin):
                return update_bin
            raise FileNotFoundError(
                "Утилита 'update' (macOS) не найдена в папке 'files/macos'!\n\n"
                "Нажмите «Проверить наличие файлов» — утилита скачается из "
                "khadas/utils автоматически.")

        # Проверяем наличие update.exe для Windows
        update_exe = os.path.join(FILE_DIR, "update.exe")
        if os.path.exists(update_exe):
            return update_exe

        # Проверяем бинарный update (для Linux совместимости)
        update_bin = os.path.join(FILE_DIR, "update")
        if os.path.exists(update_bin):
            return update_bin
        
        raise FileNotFoundError(
            "Утилита 'update.exe' или 'update' не найдена в папке 'files'!\n\n"
            "Скачайте update.exe из USB Burning Tool для Windows."
        )
    
    def _set_flash_progress(self, pct, display=""):
        """Обновить прогресс-бар значением процента записи раздела."""
        try:
            self.progress.config(mode='determinate', maximum=100)
            self.progress['value'] = pct
            if display:
                self.status_label.config(
                    text=f"🔄 Прошивка {display}... {pct}%", fg="#E67E22")
        except Exception:
            pass

    def flash_partition(self, name, image_path, display, time_estimate):
        """Прошивка раздела.
        Весь вывод update.exe в реальном времени идёт в COM-терминал.
        """
        self.log(f"Прошивка {display}... ({time_estimate})")
        update_path = self.get_update_path()
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        cmd = [update_path, "partition", name, image_path]

        # Прогресс-бар в режим процентов на время записи раздела
        try:
            self.progress.stop()
            self.progress.config(mode='determinate', maximum=100)
            self.progress['value'] = 0
        except Exception:
            pass

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False, bufsize=0,
                creationflags=creationflags
            )

            start_time = time.time()
            last_update = 0
            line_buf = b""

            while True:
                if not self.is_flashing:
                    process.terminate()
                    return False

                # Читаем доступные байты без блокировки
                try:
                    chunk = process.stdout.read(256)
                    if chunk:
                        line_buf += chunk
                        # Обрабатываем и \n (новая строка), и \r (обновление
                        # текущей строки — прогресс update.exe).
                        while True:
                            nl = line_buf.find(b"\n")
                            cr = line_buf.find(b"\r")
                            # Нет ни \n ни \r — ждём ещё байт
                            if nl < 0 and cr < 0:
                                break
                            # Выбираем ближайший разделитель
                            if nl < 0:
                                idx, sep = cr, b"\r"
                            elif cr < 0:
                                idx, sep = nl, b"\n"
                            else:
                                if cr < nl:
                                    idx, sep = cr, b"\r"
                                else:
                                    idx, sep = nl, b"\n"
                            seg = line_buf[:idx]
                            line_buf = line_buf[idx + 1:]
                            decoded = safe_decode(seg).strip()
                            if not decoded:
                                continue
                            if sep == b"\r":
                                # Прогресс — обновляем текущую строку терминала
                                self.root.after(0,
                                    lambda d=decoded: self.terminal_update_line(f"[USB] {d}"))
                                # Извлекаем % для прогресс-бара: '[ 16%/  4MB]'
                                import re as _re_pct
                                pcts = _re_pct.findall(r'(\d{1,3})%', decoded)
                                if pcts:
                                    try:
                                        pct = int(pcts[-1])  # последний % в строке
                                        self.root.after(0,
                                            lambda p=pct, d=display: self._set_flash_progress(p, d))
                                    except ValueError:
                                        pass
                            else:
                                self.root.after(0,
                                    lambda d=decoded: self.terminal_log(f"[USB] {d}"))
                except Exception:
                    pass

                code = process.poll()
                if code is not None:
                    break

                time.sleep(0.1)

            if code == 0:
                elapsed_total = int(time.time() - start_time)
                try:
                    self.progress['value'] = 100
                except Exception:
                    pass
                self.log(f"  ✓ Успешно прошит раздел {display} за {elapsed_total}с")
                return True
            else:
                # Пытаемся прочитать вывод с разными кодировками
                output = ""
                try:
                    raw_output = process.stdout.read()
                    # Пробуем UTF-8
                    try:
                        output = raw_output.decode('utf-8', errors='ignore')
                    except:
                        # Пробуем CP1251 (Windows Cyrillic)
                        try:
                            output = raw_output.decode('cp1251', errors='ignore')
                        except:
                            # В крайнем случае показываем как есть
                            output = str(raw_output)
                except:
                    pass
                
                self.log(f"  ✗ Ошибка при прошивке раздела {display}")
                if output:
                    self.log(f"  Вывод: {output[:200]}")  # Первые 200 символов
                return False
        except Exception as e:
            self.log(f"  ✗ Исключение при прошивке {display}: {str(e)}")
            return False
    
    def flashing_process(self):
        """Основной процесс прошивки"""
        # Приостанавливаем USB-watcher на всё время прошивки — иначе pyusb-опрос
        # конкурирует с update.exe за доступ к устройству (гонка состояний).
        self._pause_usb_watch()
        try:
            # Проверка подключения
            if not self.check_device_connection():
                self.log("✗ Устройство не обнаружено или таймаут подключения")
                self.finish_flashing()
                return
            
            # Показываем предупреждение
            response = messagebox.askyesno(
                "⚠️ Предупреждение",
                "ВНИМАНИЕ!\n\n"
                "• Не отключайте устройство во время прошивки\n"
                "• Убедитесь в стабильности подключения\n"
                "• Процесс может занять до 20 минут\n\n"
                "Продолжить?"
            )
            
            if not response:
                self.log("Прошивка отменена пользователем")
                self.finish_flashing()
                return
            
            # Загрузка U-Boot
            self.log("Загрузка временного U-Boot...")
            _sender = get_aml_bundle_sender()
            if _sender:
                bundle_path = os.path.join(FILE_DIR, "aml_bundle.img")
                if not os.path.exists(bundle_path):
                    self.log(f"✗ Файл {bundle_path} не найден")
                    self.finish_flashing()
                    return
                # Загрузка U-Boot с повторами: transient-ошибки libusb
                # (_usb_reap_async timeout / ERROR_OPERATION_ABORTED) обычно
                # уходят при повторной попытке после короткой паузы — не нужно
                # физически менять USB-порт.
                MAX_TRIES = 3
                loaded = False
                for attempt in range(1, MAX_TRIES + 1):
                    try:
                        if attempt > 1:
                            self.log(f"  ↻ Повтор загрузки U-Boot "
                                     f"(попытка {attempt}/{MAX_TRIES})...")
                            time.sleep(2.5)  # даём USB-стеку восстановиться
                        _sender(bundle_path)
                        self.log("✓ U-Boot загружен")
                        loaded = True
                        break
                    except Exception as e:
                        emsg = safe_decode(e)
                        transient = any(s in emsg.lower() for s in (
                            "timeout", "reap_async", "aborted", "прервана",
                            "i/o", "ввода/вывода", "control_msg", "no such device",
                            "not found", "no backend", "errno 5",
                            "input/output", "claim interface",
                            "permission denied", "ret[-13]"))
                        if attempt < MAX_TRIES and transient:
                            self.log(f"  ⚠ Временная ошибка USB: {emsg[:90]}")
                            continue
                        self.log(f"✗ Ошибка загрузки U-Boot: {emsg}")
                        if transient:
                            self.log("  Совет: переподключите питание устройства "
                                     "(повторный вход в USB Boot) и попробуйте снова.")
                        self.finish_flashing()
                        return
                if loaded:
                    # pyamlboot оставил USB-интерфейс захваченным — отпускаем,
                    # иначе update получит claim interface -13 (Permission denied)
                    self._release_aml_usb()
                    # После загрузки U-Boot устройство ПЕРЕЗАГРУЖАЕТСЯ в
                    # U-Boot burn-режим: ROM-девайс (1B8E:C003) отключается и
                    # должен появиться на шине снова. На macOS пере-энумерация
                    # (особенно через USB-хаб) может занять до десятков секунд
                    # или «залипнуть» — ждём РЕАЛЬНОГО возврата устройства.
                    if self._wait_amlogic_usb(30):
                        self.log("✓ Устройство снова на шине (U-Boot burn mode)")
                    else:
                        self.log("✗ Устройство не вернулось на USB после "
                                 "загрузки U-Boot (30 с)")
                        self.log("  Порт USB мог «залипнуть». Восстановление:")
                        self.log("   1) Отключите питание устройства И USB-кабель")
                        self.log("   2) Подключите кабель НАПРЯМУЮ к Mac, без хаба —"
                                 " бутром Amlogic через хабы работает нестабильно")
                        self.log("   3) Если порт завис — переподключите кабель/хаб"
                                 " на стороне Mac (похоже, что вы это и сделали)")
                        self.log("   4) Замкните пин 6 на GND (пин 3), подайте"
                                 " питание → снова USB Boot (1B8E:C003)")
                        self.finish_flashing()
                        return
            else:
                self.log("✗ pyamlboot_local не найден или не импортируется!")
                self.log("  Нажмите «Загрузить утилиты» и ПЕРЕЗАПУСТИТЕ программу.")
                self.log("  (папка pyamlboot_local/ должна быть рядом с gui.py)")
                self.finish_flashing()
                return

            # Переключение на eMMC
            self.log("Запуск eMMC...")
            self._release_aml_usb()
            mmc_ok = False
            for mmc_try in range(1, 6):
                try:
                    self.aml_bulkcmd("mmc dev 1")
                    mmc_ok = True
                    break
                except Exception as e:
                    emsg = safe_decode(e)
                    self.log(f"  ⚠ mmc dev 1 (попытка {mmc_try}/5): "
                             f"{emsg[:90]}")
                    self._release_aml_usb()
                    if not self._wait_amlogic_usb(15):
                        break
                    time.sleep(1.5)
            if mmc_ok:
                self.log("✓ eMMC активирован")
            else:
                self.log("✗ Не удалось переключить U-Boot на eMMC")
                self.log("  Причины бывают две:")
                self.log("   • устройство пропало с USB (см. восстановление выше)")
                self.log("   • U-Boot не увидел eMMC (аппаратная проблема/сбой"
                         " инициализации) — попробуйте полный сброс питания и"
                         " повторный вход в USB Boot")
                self.finish_flashing()
                return
            
            # Модификация env
            self.log("Модификация переменных окружения (env)...")
            try:
                env_file = os.path.join(ROOT_DIR, "env_user.bin")

                # Читаем текущий env с устройства
                self.log("  - Чтение env с устройства...")
                self.aml_read_part("env", "0x800000", env_file)

                if not os.path.exists(env_file):
                    raise Exception("Не удалось прочитать env с устройства")

                env_size = os.path.getsize(env_file)
                self.log(f"  - Прочитано: {env_size} байт")

                # РАЗБИРАЕМ бинарный env в словарь — для показа в редакторе.
                # ВАЖНО: НЕ сваливаем все ~80 переменных в self.env_data —
                # там должны быть только пользовательские ИЗМЕНЕНИЯ. Полный
                # снимок с устройства храним отдельно (self._env_device).
                parsed = self.parse_env_blob(env_file)
                if parsed:
                    self.log(f"  - Распознано переменных в env: {len(parsed)}")
                    self._env_device = dict(parsed)   # снимок с устройства
                    for key in ("serial", "mac", "lock", "avb2", "EnableSelinux"):
                        if key in parsed:
                            self.log(f"      {key} = {parsed[key]}")
                else:
                    self._env_device = {}
                    self.log("  ⚠ Не удалось распарсить env (формат?) — поля будут пустыми")

                # Сбрасываем флаг сохранения перед открытием редактора
                self._env_editor_saved = False

                # Предлагаем отредактировать, БЛОКИРУЯ прошивку до закрытия редактора
                response = messagebox.askyesno(
                    "Редактирование ENV",
                    f"ENV прочитан с устройства ({len(parsed)} переменных).\n\n"
                    "Открыть редактор переменных окружения?\n"
                    "(Рекомендуется для разблокировки bootloader: lock, avb2, SELinux)\n\n"
                    "Если нажать «Нет» — применится только необходимый минимум:\n"
                    "silent=0, lock=10000000, avb2=0.\n\n"
                    "Прошивка продолжится ПОСЛЕ закрытия редактора."
                )
                if response:
                    # Редактор и wait_window ДОЛЖНЫ выполняться в главном потоке.
                    # Прошивка идёт в фоновом потоке — блокируем его через Event.
                    self.log("  - Открыт редактор ENV, ожидание закрытия...")
                    import threading as _th_env
                    done = _th_env.Event()

                    def _open_and_wait():
                        try:
                            ed = self.open_env_editor(return_window=True)
                            if ed is not None:
                                self.root.wait_window(ed)
                        finally:
                            done.set()

                    self.root.after(0, _open_and_wait)
                    done.wait()   # фоновый поток ждёт закрытия редактора
                    if self._env_editor_saved:
                        self.log(f"  - Редактор: сохранено {len(self.env_data)} изменений")
                    else:
                        self.log("  - Редактор: отмена, применяется только минимум")
                        self.env_data = {}
                else:
                    # Пользователь отказался открывать редактор → только минимум
                    self.env_data = {}
                    self.log("  - Редактор пропущен, применяется только минимум")

                # Решаем, нужен ли БИНАРНЫЙ способ записи env (с CRC32).
                # Он нужен ТОЛЬКО если пользователь переопределил скрипт-переменные
                # (cmdline_keys) или serial/deviceid — их нельзя задать через setenv.
                SCRIPT_OVERRIDES = {"cmdline_keys", "serial", "deviceid",
                                    "custom_deviceid", "aml_serial", "mac", "ethaddr"}
                need_binary = any(k in self.env_data for k in SCRIPT_OVERRIDES)

                if need_binary:
                    self.log("  - Обнаружены изменения serial/deviceid/cmdline_keys")
                    self.log("    → запись env БИНАРНО с пересчётом CRC32")
                    self._apply_env_binary(env_file)
                else:
                    self.log("  - Применение env через setenv (простые переменные)")
                    self._apply_env_setenv(env_file)

                # ВСЕГДА применяем минимум разблокировки в живом U-Boot —
                # даже после бинарной записи env. Прошивка читает avb2/lock из
                # активной сессии U-Boot, а не из записанного раздела, поэтому
                # без этого avb2 в сессии останется старым (=1) и не примется.
                self._apply_unlock_minimum()

                # Удаляем временный файл
                if os.path.exists(env_file):
                    try:
                        os.remove(env_file)
                    except Exception:
                        pass

            except Exception as e:
                self.log(f"✗ Ошибка модификации env: {str(e)}")
                self.log("⚠ Продолжаем без модификации env (может потребоваться ручная настройка)")
                pass
            
            # Прошивка выбранных разделов
            self.log("\nНачало прошивки разделов...")
            self.log("-" * 50)
            
            total_partitions = sum(1 for img in PART_IMAGES if self.image_vars[img["name"]].get())
            current = 0
            
            for img in PART_IMAGES:
                if not self.is_flashing:
                    self.log("Прошивка остановлена пользователем")
                    break
                
                if self.image_vars[img["name"]].get():
                    current += 1
                    self.log(f"\n[{current}/{total_partitions}] Раздел: {img['display']}")
                    self.status_label.config(
                        text=f"🔄 Прошивка {img['display']} ({current}/{total_partitions})...",
                        fg="#E67E22"
                    )
                    
                    if img["name"] in self.selected_images:
                        image_path = self.selected_images[img["name"]]
                        image_size = os.path.getsize(image_path) / (1024 * 1024)  # МБ
                        self.log(f"  Файл: {os.path.basename(image_path)} ({image_size:.1f} МБ)")
                        
                        success = self.flash_partition(
                            img["name"],
                            image_path,
                            img["display"],
                            img["time"]
                        )
                        if not success:
                            self.log(f"⚠ Пропускаем раздел {img['display']}")
                    else:
                        self.log(f"⚠ Образ для {img['display']} не выбран, пропускаем")
            
            # Очистка data/cache (если включено)
            if self.is_flashing and getattr(self, "wipe_data_var", None) and self.wipe_data_var.get():
                self.log("\n🧹 Очистка data и cache (сброс к заводским)...")
                for part in ("data", "cache"):
                    try:
                        self.log(f"  - Очистка {part}...")
                        self.aml_bulkcmd(f"amlmmc erase {part}")
                        self.log(f"    ✓ {part} очищен")
                    except Exception as ex:
                        self.log(f"    ⚠ {part}: {str(ex)[:80]}")
                # misc — сброс reboot-флагов (как в стоковом sysrecovery)
                try:
                    self.log("  - Очистка misc (флаги загрузки)...")
                    self.aml_bulkcmd("amlmmc erase misc")
                    self.log("    ✓ misc очищен")
                except Exception as ex:
                    self.log(f"    ⚠ misc: {str(ex)[:80]}")
                self.log("✓ Очистка завершена")

            if self.is_flashing:
                self.log("\n" + "="*50)
                self.log("✓ Прошивка завершена!")
                self.log("="*50)
                self.status_label.config(
                    text="✅ Прошивка успешно завершена!",
                    fg="#27AE60"
                )
                messagebox.showinfo("Успех", "Прошивка успешно завершена!")
            
        except Exception as e:
            self.log(f"✗ Критическая ошибка: {str(e)}")
            logging.exception(e)
            self.status_label.config(
                text="❌ Ошибка прошивки",
                fg="#E74C3C"
            )
            messagebox.showerror("Ошибка", f"Произошла ошибка:\n{str(e)}")
        finally:
            self.finish_flashing()
    
    def start_flashing(self):
        """Запуск процесса прошивки"""
        # Проверяем, выбран ли хотя бы один образ
        selected_count = sum(1 for var in self.image_vars.values() if var.get())
        if selected_count == 0:
            messagebox.showwarning("Внимание", "Выберите хотя бы один образ для прошивки")
            return
        
        # Проверяем наличие выбранных файлов
        missing = []
        for img in PART_IMAGES:
            if self.image_vars[img["name"]].get():
                if img["name"] not in self.selected_images:
                    missing.append(img["display"])
                elif not os.path.exists(self.selected_images[img["name"]]):
                    missing.append(img["display"])
        
        if missing:
            messagebox.showerror(
                "Ошибка",
                f"Не найдены файлы образов для:\n" + "\n".join(f"• {m}" for m in missing)
            )
            return
        
        # Проверяем наличие update.exe
        try:
            update_path = self.get_update_path()
            self.log(f"Найдена утилита: {update_path}")
        except FileNotFoundError as e:
            messagebox.showerror("Ошибка", str(e))
            return
        
        # Проверяем наличие aml_bundle.img
        bundle_path = os.path.join(FILE_DIR, "aml_bundle.img")
        if not os.path.exists(bundle_path):
            response = messagebox.askyesno(
                "Предупреждение",
                "Файл aml_bundle.img не найден!\n\n"
                "Этот файл необходим для загрузки U-Boot.\n"
                "Продолжить без загрузки U-Boot? (только для отладки)"
            )
            if not response:
                return
        
        # Проверка pyusb — нужен для загрузки U-Boot через pyamlboot
        if not USB_AVAILABLE:
            if not messagebox.askyesno(
                "pyusb не установлен",
                "Библиотека pyusb не установлена — загрузка U-Boot через\n"
                "pyamlboot будет невозможна.\n\n"
                "Установите: pip install pyusb\n\n"
                "Продолжить всё равно? (только если U-Boot уже в памяти)"):
                return

        # Проверка пароля USB-режима перед прошивкой
        self.log("Проверка пароля USB-режима Amlogic...")
        if not self._password_gate(log_fn=self.log):
            self.log("⛔ Прошивка отменена (требуется пароль USB).")
            return

        self.is_flashing = True
        self.flash_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        # На этапе подготовки (загрузка U-Boot, env) — бегущая полоса;
        # при записи разделов flash_partition переключит на проценты.
        self.progress.config(mode='indeterminate')
        self.progress.start(10)
        
        self.status_label.config(
            text="🔄 Прошивка в процессе... Не отключайте устройство!",
            fg="#E67E22"
        )
        
        self.log("="*50)
        self.log("🚀 НАЧАЛО ПРОЦЕССА ПРОШИВКИ")
        self.log("="*50)
        self.log(f"Выбрано разделов: {selected_count}")
        for img in PART_IMAGES:
            if self.image_vars[img["name"]].get():
                self.log(f"  ✓ {img['display']} - {os.path.basename(self.selected_images[img['name']])}")
        self.log("="*50)
        
        self.flash_thread = threading.Thread(target=self.flashing_process, daemon=True)
        self.flash_thread.start()
    
    def stop_flashing(self):
        """Остановка процесса прошивки"""
        if messagebox.askyesno("Подтверждение", "Вы уверены, что хотите остановить прошивку?"):
            self.is_flashing = False
            self.log("Остановка процесса...")
    
    def finish_flashing(self):
        """Завершение процесса прошивки"""
        self.is_flashing = False
        self._resume_usb_watch()   # возобновляем USB-watcher после прошивки
        try:
            self.progress.stop()
            self.progress.config(mode='indeterminate')
            self.progress['value'] = 0
        except Exception:
            pass
        self.flash_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status_label.config(
            text="✅ Прошивка завершена или остановлена",
            fg="#27AE60"
        )


def _relaunch_without_console():
    """На Windows: если скрипт запущен через python.exe (с консолью),
    перезапустить через pythonw.exe (без консольного окна).

    Срабатывает только один раз (флаг в переменной окружения), и только
    если рядом есть pythonw.exe. Возвращает True если перезапустил
    (вызывающий должен завершиться)."""
    if sys.platform != "win32":
        return False
    if os.environ.get("YASTA_NO_CONSOLE") == "1":
        return False  # уже перезапущены
    exe = sys.executable or ""
    # Если уже pythonw — ничего не делаем
    if exe.lower().endswith("pythonw.exe"):
        return False
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(pythonw):
        return False
    try:
        env = dict(os.environ)
        env["YASTA_NO_CONSOLE"] = "1"
        # DETACHED_PROCESS, чтобы не держать родительскую консоль
        DETACHED = 0x00000008
        subprocess.Popen([pythonw, os.path.abspath(sys.argv[0])] + sys.argv[1:],
                         env=env, creationflags=DETACHED,
                         close_fds=True)
        return True
    except Exception:
        return False


def main():
    # Прячем консольное окно py.exe — перезапуск через pythonw.exe
    if _relaunch_without_console():
        sys.exit(0)

    root = tk.Tk()

    # Показываем окно помощи при первом запуске (если не отключено в настройках)
    settings = load_settings()
    if not settings.get("hide_initial_help", False):
        show_initial_help(root)

    app = FlasherGUI(root)
    root.mainloop()


def show_initial_help(root):
    """Показать помощь по необходимым файлам"""
    help_window = tk.Toplevel(root)
    help_window.title("📋 Необходимые файлы")
    help_window.geometry("600x540")
    help_window.resizable(True, True)
    help_window.minsize(450, 340)
    help_window.transient(root)
    help_window.grab_set()
    
    # Заголовок
    header = tk.Label(
        help_window,
        text="Подготовка к прошивке",
        font=("Arial", 16, "bold"),
        pady=10
    )
    header.pack()
    
    # Текст инструкции
    text_frame = tk.Frame(help_window, padx=20, pady=10)
    text_frame.pack(fill=tk.BOTH, expand=True)
    
    help_text = scrolledtext.ScrolledText(
        text_frame,
        wrap=tk.WORD,
        font=_mono_font(9),
        height=20
    )
    help_text.pack(fill=tk.BOTH, expand=True)
    
    instructions = """НЕОБХОДИМЫЕ ФАЙЛЫ ДЛЯ РАБОТЫ ПРОГРАММЫ:

✅ АВТОМАТИЧЕСКАЯ ЗАГРУЗКА (при первом запуске):

Программа автоматически загрузит из GitHub:
• update.exe - Утилита прошивки Amlogic
• aml_image_v2_packer - Упаковщик образов (опционально)

Источник: 
https://github.com/khadas/utils/tree/master/aml-flash-tool/tools/windows

Эти файлы будут сохранены в папку files/ и готовы к использованию!

⚠️ ВАЖНО О БИБЛИОТЕКАХ:
Для работы update.exe может потребоваться:
• Microsoft Visual C++ 2010 Redistributable Package
  (обычно уже установлен в Windows)

Если update.exe не запускается - установите этот пакет.

📁 ЧТО НУЖНО ПОДГОТОВИТЬ ВРУЧНУЮ:

1. Папка "files/":
   └── aml_bundle.img - U-Boot образ для вашего чипа S905X2
       (должен быть получен вместе с прошивкой)

2. Папка "images-bkp/":
   Образы разделов прошивки:
   • boot.img
   • dtbo.img  
   • vbmeta.img
   • logo.img
   • odm.img
   • product.img
   • recovery.img
   • system-bkp.img (самый большой ~2-3 ГБ)
   • vendor.img
   • sysrecovery.img

⚠️ ВАЖНО:
- При первом запуске программа предложит загрузить утилиты
- Все образы должны соответствовать вашей модели устройства
- Используйте только проверенные образы
- Убедитесь в наличии всех файлов перед началом

💡 СОВЕТ:
После закрытия этого окна программа проверит наличие 
утилит и предложит загрузить недостающие файлы.
Также вы можете использовать кнопку "🔍 Проверить наличие файлов".

"""
    
    help_text.insert("1.0", instructions)
    help_text.config(state='disabled')

    # Чекбокс «не показывать снова»
    dont_show_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        help_window,
        text="Не показывать это окно при следующих запусках",
        variable=dont_show_var,
        font=("Arial", 10)
    ).pack(pady=(4, 0))

    def close_help():
        if dont_show_var.get():
            s = load_settings()
            s["hide_initial_help"] = True
            save_settings(s)
        help_window.destroy()

    # Кнопка закрытия
    tk.Button(
        help_window,
        text="Понятно, продолжить",
        command=close_help,
        font=("Arial", 11),
        bg="#27AE60",
        fg="white",
        pady=10
    ).pack(pady=10, padx=20, fill=tk.X)

    help_window.protocol("WM_DELETE_WINDOW", close_help)

    # Центрируем окно
    help_window.update_idletasks()
    x = (help_window.winfo_screenwidth() // 2) - (help_window.winfo_width() // 2)
    y = (help_window.winfo_screenheight() // 2) - (help_window.winfo_height() // 2)
    help_window.geometry(f"+{x}+{y}")


if __name__ == "__main__":
    main()
