"""
Графический инсталлятор прошивки для Yandex Station Max (Amlogic S905X2)
Версия для Windows с автозагрузкой утилит и COM терминалом

ТРЕБОВАНИЯ:
1. Python 3.7+
2. pip install pyamlboot
3. pip install pyserial (для COM терминала, опционально)

ВОЗМОЖНОСТИ:
- Автоматическая загрузка утилит из GitHub
- Графический интерфейс с выбором образов
- COM терминал (UART) для мониторинга в реальном времени
- Работа с переменными окружения (ENV)
- Детальное логирование процесса

COM ТЕРМИНАЛ:
- Подключите USB-UART преобразователь (CH340, CP2102, FT232 и т.д.)
- Подключите к пинам TX/RX/GND сервисной колодки
- Скорость: 115200 бод, 8N1
- В программе выберите COM порт и нажмите "Подключить"
- Вы увидите вывод U-Boot и процесс загрузки системы

АВТОМАТИЧЕСКАЯ ЗАГРУЗКА:
Программа автоматически загрузит необходимые утилиты из GitHub:
- update.exe (утилита прошивки Amlogic)
- aml_image_v2_packer (упаковщик образов)

Источник: https://github.com/khadas/utils/tree/master/aml-flash-tool/tools/windows

ДОПОЛНИТЕЛЬНЫЕ БИБЛИОТЕКИ (опционально):
Если update.exe не запустится, может потребоваться:
- Microsoft Visual C++ 2010 Redistributable Package
  (обычно уже установлен в Windows)

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
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
import logging
import urllib.request
import zipfile
import tempfile

# Импорт для работы с COM портами
try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

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

ROOT_DIR = os.getcwd()
FILE_DIR = os.path.join(ROOT_DIR, "files")
IMG_DIR = os.path.join(ROOT_DIR, "images-bkp")

# Репозиторий с утилитами и зависимостями
GITHUB_REPO       = "https://github.com/suddosu/yasta_flasher"
GITHUB_RAW_BASE   = "https://raw.githubusercontent.com/suddosu/yasta_flasher/main"
GITHUB_TOOLS_BASE = f"{GITHUB_RAW_BASE}/files"   # совместимость со старым кодом

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
        self.root.title("Yandex Station Max - Инсталлятор прошивки")
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
        self.env_data = {}
        
        # Кастомные разделы
        self.custom_partitions = []

        # Буфер захвата вывода терминала (для дампа разделов)
        self._terminal_capture_buf = None
        
        # Создаем папку files если не существует
        os.makedirs(FILE_DIR, exist_ok=True)
        os.makedirs(IMG_DIR, exist_ok=True)
        
        self.create_widgets()
        
        # Проверяем наличие утилит при запуске
        self.root.after(100, self.check_and_download_tools)
        
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
    
    def create_left_panel(self, main_frame):
        
        # Инструкция
        instruction_frame = tk.LabelFrame(main_frame, text="📋 Инструкция по подключению", font=("Arial", 10, "bold"))
        instruction_frame.pack(fill=tk.X, pady=(0, 10))
        
        instruction_text = """
1. Подключите USB кабель к сервисной колодке (под резинкой возле радиатора)
2. Замкните 6 пин на землю (3 пин) для активации USB boot
3. Подключите USB к ПК
4. Подключите блок питания к станции
        """
        
        instruction_label = tk.Label(instruction_frame, text=instruction_text, justify=tk.LEFT, padx=10, pady=5)
        instruction_label.pack()
        
        # Секция выбора образов
        images_frame = tk.LabelFrame(main_frame, text="🗂️ Выбор образов для прошивки", font=("Arial", 10, "bold"))
        images_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # Canvas для прокрутки
        canvas = tk.Canvas(images_frame, height=180)  # Уменьшил ещё больше
        scrollbar = ttk.Scrollbar(images_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Создаем чекбоксы для каждого образа
        self.image_vars = {}
        self.image_path_labels = {}
        
        # Кнопка проверки файлов
        check_frame = tk.Frame(scrollable_frame, bg="#ECF0F1")
        check_frame.pack(fill=tk.X, padx=5, pady=5)
        
        tk.Button(
            check_frame,
            text="🔍 Проверить наличие файлов",
            command=self.check_all_files,
            width=25
        ).pack(pady=5)
        
        self.files_status_label = tk.Label(
            check_frame,
            text="Статус файлов будет показан здесь",
            font=("Arial", 8),
            fg="gray"
        )
        self.files_status_label.pack()
        
        tk.Label(scrollable_frame, text="").pack()  # Разделитель
        
        for idx, img in enumerate(PART_IMAGES):
            frame = tk.Frame(scrollable_frame)
            frame.pack(fill=tk.X, padx=5, pady=2)
            
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
                width=10
            )
            btn.pack(side=tk.RIGHT, padx=5)
            
            path_label = tk.Label(frame, text="", fg="gray", font=("Arial", 8))
            path_label.pack(side=tk.RIGHT, padx=5)
            self.image_path_labels[img["name"]] = path_label
            
            # Проверяем наличие файла
            default_path = os.path.join(IMG_DIR, img["file"])
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
        
        # Кнопка "Выбрать все/Снять все"
        select_frame = tk.Frame(main_frame)
        select_frame.pack(fill=tk.X, pady=(0, 10))
        
        tk.Button(select_frame, text="✓ Выбрать все", command=self.select_all, width=15).pack(side=tk.LEFT, padx=5)
        tk.Button(select_frame, text="✗ Снять все", command=self.deselect_all, width=15).pack(side=tk.LEFT)
        tk.Button(
            select_frame, 
            text="🔍 Диагностика USB", 
            command=self.test_usb_detection, 
            width=18,
            bg="#3498DB",
            fg="white"
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            select_frame,
            text="💾 Дамп разделов",
            command=self.dump_partitions,
            width=16,
            bg="#C0392B",
            fg="white"
        ).pack(side=tk.LEFT, padx=5)
        
        # Правая сторона
        tk.Button(
            select_frame,
            text="⚙️ Редактор ENV",
            command=self.open_env_editor,
            width=15,
            bg="#E67E22",
            fg="white"
        ).pack(side=tk.RIGHT, padx=5)
        
        tk.Button(
            select_frame,
            text="📝 Редактор образов",
            command=self.open_image_editor,
            width=17,
            bg="#8E44AD",
            fg="white"
        ).pack(side=tk.RIGHT, padx=5)
        
        tk.Button(
            select_frame,
            text="➕ Кастомный раздел",
            command=self.add_custom_partition,
            width=18,
            bg="#16A085",
            fg="white"
        ).pack(side=tk.RIGHT, padx=5)
        
        tk.Button(
            select_frame, 
            text="📥 Загрузить утилиты", 
            command=self.download_tools_from_github, 
            width=18,
            bg="#9B59B6",
            fg="white"
        ).pack(side=tk.RIGHT, padx=5)
        
        # Лог
        log_frame = tk.LabelFrame(main_frame, text="📄 Журнал операций", font=("Arial", 10, "bold"))
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=5, state='disabled', font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Прогресс бар
        progress_frame = tk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=(5, 5))
        
        tk.Label(progress_frame, text="Прогресс:", font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 5))
        self.progress = ttk.Progressbar(progress_frame, mode='indeterminate')
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # Информационная панель статуса
        status_frame = tk.LabelFrame(main_frame, text="📊 Статус готовности", font=("Arial", 10, "bold"))
        status_frame.pack(fill=tk.X, pady=(5, 5))
        
        self.status_label = tk.Label(
            status_frame,
            text="Проверьте наличие файлов перед началом прошивки",
            font=("Arial", 10),
            fg="#7F8C8D",
            pady=8
        )
        self.status_label.pack()
        
        # ВАЖНО: Кнопки управления - фиксированные внизу, НЕ expand!
        button_frame = tk.Frame(main_frame, bg="#ECF0F1", relief=tk.RIDGE, bd=2)
        button_frame.pack(fill=tk.X, pady=(5, 0), side=tk.BOTTOM, expand=False)  # expand=False!
        
        # Добавляем отступы внутри фрейма
        inner_button_frame = tk.Frame(button_frame, bg="#ECF0F1")
        inner_button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.flash_button = tk.Button(
            inner_button_frame,
            text="🚀 Начать прошивку",
            command=self.start_flashing,
            bg="#27AE60",
            fg="white",
            font=("Arial", 14, "bold"),
            height=2,
            cursor="hand2",
            relief=tk.RAISED,
            bd=3
        )
        self.flash_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        
        # Эффект наведения для кнопки "Начать прошивку"
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
            font=("Arial", 14, "bold"),
            height=2,
            state=tk.DISABLED,
            cursor="hand2",
            relief=tk.RAISED,
            bd=3
        )
        self.stop_button.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # Эффект наведения для кнопки "Остановить"
        def on_enter_stop(e):
            if self.stop_button['state'] == tk.NORMAL:
                self.stop_button['bg'] = '#C0392B'
        
        def on_leave_stop(e):
            if self.stop_button['state'] == tk.NORMAL:
                self.stop_button['bg'] = '#E74C3C'
        
        self.stop_button.bind("<Enter>", on_enter_stop)
        self.stop_button.bind("<Leave>", on_leave_stop)
        
        # Логируем создание кнопок для отладки
        self.log("✓ Интерфейс инициализирован")
        self.log("✓ Кнопки управления созданы")
    
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
            font=("Consolas", 8),
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
        if self.serial_running:
            self.stop_serial_monitor()
        if self.is_flashing:
            if messagebox.askyesno("Подтверждение", "Прошивка в процессе. Вы уверены?"):
                self.is_flashing = False
                self.root.destroy()
        else:
            self.root.destroy()
    
    def refresh_com_ports(self):
        """Обновление списка COM портов"""
        if not SERIAL_AVAILABLE:
            return
        
        try:
            ports = serial.tools.list_ports.comports()
            port_list = [port.device for port in ports]
            
            self.com_port_combo['values'] = port_list
            
            if port_list:
                if not self.com_port_var.get() or self.com_port_var.get() not in port_list:
                    self.com_port_combo.current(0)
                self.terminal_log(f"🔍 Найдено портов: {len(port_list)}")
                for port in ports:
                    self.terminal_log(f"  • {port.device}: {port.description}")
            else:
                self.terminal_log("⚠️ COM порты не найдены")
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
        """Добавление сообщения в терминал (совместимость со старым API)"""
        if self._terminal_capture_buf is not None:
            self._terminal_capture_buf.append(message)
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
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"uart_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        
        if filename:
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
                filetypes=[("Image files", "*.img"), ("All files", "*.*")]
            )
            if fn:
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
        
        entry_widgets = {}
        
        # === ВКЛАДКА ИДЕНТИФИКАЦИЯ ===
        id_scroll = scrolledtext.ScrolledText(id_frame, height=20, font=("Consolas", 9))
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
            if k in self.env_data:
                id_vars[k]["value"] = self.env_data[k]
        
        for var_name, var_info in id_vars.items():
            frame = tk.Frame(id_scroll)
            id_scroll.window_create(tk.END, window=frame)
            id_scroll.insert(tk.END, "\n")
            
            # Заголовок
            label_text = f"{'🔴 ' if var_info['critical'] else ''}  {var_name}"
            tk.Label(frame, text=label_text, font=("Arial", 10, "bold"), anchor=tk.W, width=25).pack(side=tk.TOP, anchor=tk.W)
            
            # Поле ввода
            entry = tk.Entry(frame, font=("Consolas", 9), width=60)
            entry.insert(0, var_info["value"])
            entry.pack(side=tk.TOP, pady=(2, 0))
            entry_widgets[var_name] = entry
            
            # Описание
            tk.Label(frame, text=var_info["desc"], font=("Arial", 8), fg="gray", anchor=tk.W).pack(side=tk.TOP, anchor=tk.W, pady=(0, 10))
        
        id_scroll.config(state='disabled')
        
        # === ВКЛАДКА БЕЗОПАСНОСТЬ ===
        sec_scroll = scrolledtext.ScrolledText(sec_frame, height=20, font=("Consolas", 9))
        sec_scroll.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        sec_vars = {
            "lock": {
                "value": self.env_data.get("lock", ""),
                "desc": "🔓 Разблокировка bootloader (10000000 = разблокирован, 0 = заблокирован)",
                "critical": True
            },
            "avb2": {
                "value": self.env_data.get("avb2", ""),
                "desc": "🔓 Android Verified Boot 2.0 (0 = выключен, 1 = включен)",
                "critical": True
            },
            "EnableSelinux": {
                "value": self.env_data.get("EnableSelinux", ""),
                "desc": "🔓 SELinux режим (permissive = отключен, enforcing = включен)",
                "critical": True,
                "options": ["permissive", "enforcing", "disabled"]
            },
            "jtag": {
                "value": self.env_data.get("jtag", ""),
                "desc": "🔧 JTAG отладка (enable/disable)",
                "critical": False,
                "options": ["disable", "enable"]
            },
            "silent": {
                "value": self.env_data.get("silent", ""),
                "desc": "📢 Вывод сообщений загрузки (0 = включен, 1 = выключен)",
                "critical": False
            },
            "rabbit_hole_debug": {
                "value": self.env_data.get("rabbit_hole_debug", ""),
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
                combo = ttk.Combobox(frame, textvariable=var, values=var_info["options"], font=("Consolas", 9), width=57, state="readonly")
                combo.pack(side=tk.TOP, pady=(2, 0))
                entry_widgets[var_name] = combo
            else:
                entry = tk.Entry(frame, font=("Consolas", 9), width=60)
                entry.insert(0, var_info["value"])
                entry.pack(side=tk.TOP, pady=(2, 0))
                entry_widgets[var_name] = entry
            
            tk.Label(frame, text=var_info["desc"], font=("Arial", 8), fg="gray", anchor=tk.W).pack(side=tk.TOP, anchor=tk.W, pady=(0, 10))
        
        sec_scroll.config(state='disabled')
        
        # === ВКЛАДКА СИСТЕМА ===
        sys_scroll = scrolledtext.ScrolledText(sys_frame, height=20, font=("Consolas", 9))
        sys_scroll.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        sys_vars = {
            "bootdelay": {
                "value": self.env_data.get("bootdelay", ""),
                "desc": "⏱️ Задержка перед загрузкой (секунды)",
                "critical": False
            },
            "led_screen_brightness": {
                "value": self.env_data.get("led_screen_brightness", ""),
                "desc": "💡 Яркость экрана (0-255)",
                "critical": False
            },
            "led_ring_brightness": {
                "value": self.env_data.get("led_ring_brightness", ""),
                "desc": "💡 Яркость кольца подсветки (0-255)",
                "critical": False
            },
            "localization": {
                "value": self.env_data.get("localization", ""),
                "desc": "🌍 Локализация устройства",
                "critical": False
            },
            "hdmimode": {
                "value": self.env_data.get("hdmimode", ""),
                "desc": "📺 Режим HDMI (1080p60hz, 720p60hz и т.д.)",
                "critical": False
            },
            "outputmode": {
                "value": self.env_data.get("outputmode", ""),
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
            
            entry = tk.Entry(frame, font=("Consolas", 9), width=60)
            entry.insert(0, var_info["value"])
            entry.pack(side=tk.TOP, pady=(2, 0))
            entry_widgets[var_name] = entry
            
            tk.Label(frame, text=var_info["desc"], font=("Arial", 8), fg="gray", anchor=tk.W).pack(side=tk.TOP, anchor=tk.W, pady=(0, 10))
        
        sys_scroll.config(state='disabled')
        
        # Кнопки (внутри зафиксированного снизу env_bottom)
        btn_frame = tk.Frame(env_bottom, bg="#ECF0F1")
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        
        def save_env():
            """Сохранить изменения ENV"""
            # Собираем все значения
            self.env_data = {}
            for name, widget in entry_widgets.items():
                if isinstance(widget, ttk.Combobox):
                    self.env_data[name] = widget.get()
                else:
                    self.env_data[name] = widget.get()
            
            # Проверяем серийные номера
            serial = self.env_data.get("serial", "")
            deviceid = self.env_data.get("deviceid", "")
            custom_deviceid = self.env_data.get("custom_deviceid", "")
            
            if serial != deviceid or serial != custom_deviceid:
                response = messagebox.askyesno(
                    "Предупреждение",
                    f"Серийные номера не совпадают:\n\n"
                    f"serial: {serial}\n"
                    f"deviceid: {deviceid}\n"
                    f"custom_deviceid: {custom_deviceid}\n\n"
                    f"Рекомендуется использовать одинаковые значения.\n"
                    f"Продолжить?",
                    parent=editor
                )
                if not response:
                    return
            
            messagebox.showinfo(
                "Сохранено",
                f"ENV переменные сохранены.\n\n"
                f"Они будут применены при прошивке через команды:\n"
                f"• env import (загрузка существующего ENV)\n"
                f"• setenv <var> <value> (установка значений)\n"
                f"• saveenv (сохранение на устройство)\n\n"
                f"Всего переменных: {len(self.env_data)}",
                parent=editor
            )
            
            self.log(f"✓ ENV переменные обновлены: {len(self.env_data)} шт.")
            self.log(f"  Серийный номер: {serial}")
            editor.destroy()
        
        def load_from_file():
            """Загрузить ENV из файла"""
            filename = filedialog.askopenfilename(
                title="Выберите файл ENV",
                filetypes=[("ENV files", "env_user.bin"), ("All files", "*.*")],
                parent=editor
            )
            if filename:
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
            text="Отмена",
            command=editor.destroy,
            font=("Arial", 10),
            width=10
        ).pack(side=tk.RIGHT, padx=5)

        if return_window:
            return editor
        return None

    def _find_mik_tools(self):
        """Найти MIK и его консольные утилиты в files/MIK/.
        Возвращает dict: {mik_gui, bin_dir, simg2img, img2simg, make_ext4fs,
                          imgextractor, mkfs_erofs}. Значения = путь или None.
        """
        mik_dir = os.path.join(FILE_DIR, "MIK")
        result = {"mik_gui": None, "bin_dir": None, "simg2img": None,
                  "img2simg": None, "make_ext4fs": None, "imgextractor": None,
                  "mkfs_erofs": None, "dir": mik_dir}
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
                for n in names:
                    p = os.path.join(bin_dir, n)
                    if os.path.exists(p):
                        return p
                # рекурсивно
                for root_d, _dirs, files in os.walk(bin_dir):
                    for n in names:
                        if n in files:
                            return os.path.join(root_d, n)
                return None
            result["simg2img"]     = find_tool("simg2img.exe", "simg2img")
            result["img2simg"]     = find_tool("img2simg.exe", "ext2simg.exe",
                                               "img2simg", "ext2simg")
            result["make_ext4fs"]  = find_tool("make_ext4fs.exe", "make_ext4fs")
            result["imgextractor"] = find_tool("imgextractor.exe", "imgextractor",
                                               "extract.exe", "ext4_unpacker.exe")
            result["mkfs_erofs"]   = find_tool("mkfs.erofs.exe", "mkfs.erofs",
                                               "mke2fs.exe")
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

        have_tools = tools["mik_gui"] or tools["bin_dir"]
        hdr_color = "#27AE60" if have_tools else "#E74C3C"
        hdr_text  = ("✓ MIK найден" if have_tools
                     else "✗ MIK не найден — нажмите «Загрузить утилиты»")
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
        log_box = scrolledtext.ScrolledText(logf, height=8, font=("Consolas", 8),
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
                filetypes=[("Image", "*.img *.PARTITION *.fex"), ("All", "*.*")])
            if fn:
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
                      filedialog.askdirectory(parent=win) or work_var.get()),
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
                      filedialog.asksaveasfilename(parent=win, defaultextension=".img")
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
                      filedialog.askdirectory(title="Папка дампов", parent=win)
                      or out_var.get()), width=8).pack(side=tk.LEFT)

        # ── Лог (внизу, фиксированной высоты, перед таблицей) ─────────────────
        logf = tk.LabelFrame(win, text="Журнал", font=("Arial", 9, "bold"))
        logf.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 4))
        log_box = scrolledtext.ScrolledText(logf, height=6, font=("Consolas", 8),
                                            bg="#1E1E1E", fg="#00FF00", state='disabled')
        log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        def log(msg):
            log_box.config(state='normal')
            log_box.insert(tk.END, msg + "\n")
            log_box.see(tk.END)
            log_box.config(state='disabled')
            log_box.update_idletasks()

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
                try:
                    self.get_update_path()
                except FileNotFoundError as ex:
                    log(f"❌ {ex}")
                    self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                    return

                _sender = get_aml_bundle_sender()
                if _sender:
                    bundle = os.path.join(FILE_DIR, "aml_bundle.img")
                    if not os.path.exists(bundle):
                        log(f"❌ aml_bundle.img не найден в {FILE_DIR}")
                        self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                        return
                    log("🔌 Ожидание устройства (USB Boot)...")
                    try:
                        _sender(bundle); log("✓ U-Boot загружен")
                    except Exception as ex:
                        log(f"❌ pyamlboot: {ex}")
                        self.root.after(0, lambda: session_btn.config(state=tk.NORMAL))
                        return
                    _t.sleep(3)
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

            _th.Thread(target=_thread, daemon=True).start()

        # ── Вставить лог ──────────────────────────────────────────────────────
        def paste_log():
            pw = tk.Toplevel(win)
            pw.title("Вставить UART лог")
            pw.geometry("700x420"); pw.resizable(True, True); pw.minsize(500, 320)
            pw.transient(win)
            tk.Label(pw, text="Вставьте вывод amlmmc part 1 (из PuTTY):",
                     font=("Arial", 10)).pack(pady=6)
            txt = scrolledtext.ScrolledText(pw, font=("Consolas", 9))
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
                dump_btn.config(state=tk.DISABLED)
                log(f"\n💾 Дамп {len(to_dump)} разделов → {out_dir}")
                cflags = subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
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
                        # update mread store <part> normal <nBytes> <file>
                        r = subprocess.run(
                            [update_path, "mread", "store",
                             pname, "normal", size_hex, out_file],
                            capture_output=True, timeout=1800,
                            creationflags=cflags)
                        out_t = ((r.stdout or b"")+(r.stderr or b"")
                                 ).decode('utf-8', errors='ignore')
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
                        log(f"     ❌ {ex}")
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

    def browse_image(self, img):
        filename = filedialog.askopenfilename(
            title=f"Выберите образ {img['display']}",
            filetypes=[("Image files", "*.img"), ("All files", "*.*")]
        )
        if filename:
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
        
        # Проверяем update.exe
        update_path = None
        if os.path.exists(os.path.join(FILE_DIR, "update.exe")):
            update_path = os.path.join(FILE_DIR, "update.exe")
        elif os.path.exists(os.path.join(FILE_DIR, "update")):
            update_path = os.path.join(FILE_DIR, "update")
        
        if not update_path:
            missing_files.append("update.exe (утилита прошивки)")
        else:
            found_files.append("update.exe ✓")
        
        # Проверяем aml_bundle.img
        bundle_path = os.path.join(FILE_DIR, "aml_bundle.img")
        if not os.path.exists(bundle_path):
            missing_files.append("aml_bundle.img (U-Boot образ)")
        else:
            found_files.append("aml_bundle.img ✓")
        
        # Проверяем образы разделов
        for img in PART_IMAGES:
            default_path = os.path.join(IMG_DIR, img["file"])
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
            msg += "• update.exe - будет загружен автоматически из GitHub\n"
            msg += "• aml_bundle.img - U-Boot образ для вашего чипа\n"
            msg += "• *.img файлы - образы прошивки устройства"
            
            # Предлагаем загрузить утилиты
            if "update.exe" in str(missing_files):
                msg += "\n\n💡 Нажмите 'Да' чтобы загрузить update.exe из GitHub"
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
          • update.exe         (files/)               — прошивка, дамп
          • MIK (MIK64.exe)    (files/MIK/)           — редактор образов
          • pyamlboot_local/   (корень проекта)       — загрузка U-Boot
          • aml_bundle.img     (files/) — опционально, специфичен для устройства
        """
        missing = []      # критичные — предлагаем скачать
        warnings = []     # некритичные — просто предупреждаем

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
            msg += "\n\nЗагрузить недостающее из GitHub (suddosu/yasta_flasher + MIK)?"
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
        log_box = scrolledtext.ScrolledText(pw, font=("Consolas", 9))
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
                with urllib.request.urlopen(ua_req(zip_url), timeout=300) as r:
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
                with urllib.request.urlopen(ua_req(REPO_ZIP), timeout=300) as r:
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
            try:
                dl_mik(log_p)
            except Exception as ex:
                msg = f"  ❌ MIK: {ex}"
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
        try:
            import usb.core
            dev = usb.core.find(idVendor=0x1b8e, idProduct=0xc003)
            if dev is not None:
                self.log("  ✓ НАЙДЕНО через libusb (1b8e:c003)")
                found_any = True
            else:
                self.log("  ✗ libusb не видит 1b8e:c003")
        except ImportError:
            self.log("  ⚠ pyusb не установлен (pip install pyusb) — пропуск")
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
            font=("Consolas", 9),
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
        check_methods = ["wmic", "pnputil", "pyamlboot"]
        
        while self.is_flashing and elapsed < 300:  # Таймаут 5 минут
            # Метод 1: Проверка через WMIC (Windows Management Instrumentation)
            try:
                result = subprocess.run(
                    ['wmic', 'path', 'Win32_PnPEntity', 'where', 
                     'DeviceID like "%USB%"', 'get', 'DeviceID,Name'],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                
                output = result.stdout.upper()
                
                # Ищем устройство Amlogic по VID:PID или названию
                if any(marker in output for marker in [
                    "1B8E", "C003", "AMLOGIC", 
                    "USB BURNING TOOL", "GX-CHIP",
                    "VID_1B8E", "PID_C003"
                ]):
                    self.log(f"✓ Устройство обнаружено через WMIC!")
                    time.sleep(2)  # Двойная проверка
                    
                    # Повторная проверка
                    result2 = subprocess.run(
                        ['wmic', 'path', 'Win32_PnPEntity', 'where', 
                         'DeviceID like "%USB%"', 'get', 'DeviceID'],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    
                    if "1B8E" in result2.stdout.upper() or "C003" in result2.stdout.upper():
                        self.log("✓ Подтверждено: устройство стабильно подключено")
                        return True
                    
            except subprocess.TimeoutExpired:
                self.log(f"⚠ WMIC завис, пропускаем проверку")
            except FileNotFoundError:
                self.log(f"⚠ WMIC не найден в системе")
            except Exception as e:
                self.log(f"⚠ Ошибка WMIC: {str(e)}")
            
            # Метод 2: Проверка через список USB устройств (PowerShell)
            try:
                result = subprocess.run(
                    ['powershell', '-Command', 
                     'Get-PnpDevice -Class USB | Select-Object -Property DeviceID,FriendlyName'],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                
                output = result.stdout.upper()
                if "1B8E" in output or "C003" in output or "AMLOGIC" in output:
                    self.log(f"✓ Устройство обнаружено через PowerShell!")
                    time.sleep(2)
                    return True
                    
            except Exception as e:
                pass  # PowerShell может быть недоступен
            
            # Метод 3: Проверка через pyamlboot (если устройство уже в режиме USB Boot)
            if get_aml_bundle_sender():
                try:
                    # pyamlboot сам умеет находить устройства
                    pass
                except Exception:
                    pass
            
            # Обновляем статус
            if elapsed % 5 == 0:  # Логируем каждые 5 секунд
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
        text_out = raw.decode('utf-8', errors='ignore')
        # Роутим в терминал всегда — если UART не подключён, это единственный лог
        for line in text_out.splitlines():
            stripped = line.strip()
            if stripped:
                self.root.after(0, lambda l=stripped: self.terminal_log(f"[USB] {l}"))
        if process.returncode != 0:
            raise Exception(f"Ошибка U-Boot команды: {cmd}\n{text_out[:300]}")
        return text_out
    
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

        Формат: [4 байта CRC32][данные]
        Данные: пары  key=value\\0key=value\\0 ... \\0\\0 (конец).
        Возвращает dict {key: value}.
        """
        result = {}
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception:
            return result
        if len(blob) < 5:
            return result
        # Пропускаем 4-байтовый CRC заголовок
        data = blob[4:]
        # Делим по нулевым байтам
        for chunk in data.split(b"\x00"):
            if not chunk:
                # Двойной \0 — конец окружения
                continue
            try:
                text = chunk.decode("utf-8", errors="ignore")
            except Exception:
                continue
            if "=" in text:
                key, _, value = text.partition("=")
                key = key.strip()
                # Имена переменных U-Boot — печатные ASCII без пробелов
                if key and all(32 < ord(c) < 127 for c in key):
                    result[key] = value
        return result


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
        """Получение пути к update утилите"""
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
    
    def flash_partition(self, name, image_path, display, time_estimate):
        """Прошивка раздела.
        Весь вывод update.exe в реальном времени идёт в COM-терминал.
        """
        self.log(f"Прошивка {display}... ({time_estimate})")
        update_path = self.get_update_path()
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        cmd = [update_path, "partition", name, image_path]

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
                        # Выводим полные строки в терминал
                        while b"\n" in line_buf:
                            ln, line_buf = line_buf.split(b"\n", 1)
                            decoded = ln.decode('utf-8', errors='ignore').strip()
                            if decoded:
                                self.root.after(0,
                                    lambda d=decoded: self.terminal_log(f"[USB] {d}"))
                except Exception:
                    pass

                code = process.poll()
                if code is not None:
                    break

                elapsed = int(time.time() - start_time)
                if elapsed - last_update >= 2:
                    self.status_label.config(
                        text=f"🔄 Прошивка {display}... {elapsed}с", fg="#E67E22")
                    self.root.update_idletasks()
                    last_update = elapsed

                time.sleep(0.1)
            
            if code == 0:
                elapsed_total = int(time.time() - start_time)
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
                try:
                    bundle_path = os.path.join(FILE_DIR, "aml_bundle.img")
                    if not os.path.exists(bundle_path):
                        raise FileNotFoundError(f"Файл {bundle_path} не найден")
                    _sender(bundle_path)
                    self.log("✓ U-Boot загружен")
                    time.sleep(4)
                except Exception as e:
                    self.log(f"✗ Ошибка загрузки U-Boot: {str(e)}")
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
            try:
                self.aml_bulkcmd("mmc dev 1")
                self.log("✓ eMMC активирован")
            except Exception as e:
                self.log(f"✗ Ошибка переключения на eMMC: {str(e)}")
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

                # РАЗБИРАЕМ бинарный env в словарь — чтобы редактор показал
                # реальные значения с устройства, а не пустые поля.
                parsed = self.parse_env_blob(env_file)
                if parsed:
                    self.log(f"  - Распознано переменных в env: {len(parsed)}")
                    # Заполняем env_data значениями с устройства (если ещё не
                    # редактировали вручную в этой сессии)
                    for k, v in parsed.items():
                        self.env_data.setdefault(k, v)
                    # Лог ключевых значений
                    for key in ("serial", "mac", "lock", "avb2", "EnableSelinux"):
                        if key in parsed:
                            self.log(f"      {key} = {parsed[key]}")
                else:
                    self.log("  ⚠ Не удалось распарсить env (формат?) — поля будут пустыми")

                # Предлагаем отредактировать, БЛОКИРУЯ прошивку до закрытия редактора
                response = messagebox.askyesno(
                    "Редактирование ENV",
                    f"ENV прочитан с устройства ({len(parsed)} переменных).\n\n"
                    "Открыть редактор переменных окружения?\n"
                    "(Рекомендуется для разблокировки bootloader: lock, avb2, SELinux)\n\n"
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
                                ed.protocol("WM_DELETE_WINDOW",
                                            lambda: (ed.destroy()))
                                self.root.wait_window(ed)
                        finally:
                            done.set()

                    self.root.after(0, _open_and_wait)
                    done.wait()   # фоновый поток ждёт закрытия редактора
                    self.log(f"  - Редактор закрыт. Переменных к применению: {len(self.env_data)}")

                # Загружаем оригинальный env в RAM
                self.log("  - Загрузка env в RAM...")
                self.aml_write_file_to_ram(env_file, "0x200c000")

                # Импортируем существующее окружение
                self.log("  - Импорт переменных в U-Boot...")
                self.aml_bulkcmd("env import 200c004")

                # Применяем изменения
                if self.env_data:
                    self.log(f"  - Применение настроек ENV ({len(self.env_data)} перем.):")
                    for key, value in self.env_data.items():
                        if value == "":
                            continue  # пустые не трогаем
                        self.log(f"    • {key} = {value}")
                        self.aml_bulkcmd(f"setenv {key} {value}")
                else:
                    self.log("  - Применение стандартных настроек разблокировки:")
                    self.log("    • silent = 0, lock = 10000000, avb2 = 0")
                    self.aml_bulkcmd("setenv silent 0")
                    self.aml_bulkcmd("setenv lock 10000000")
                    self.aml_bulkcmd("setenv avb2 0")

                self.log("  - Сохранение изменений (saveenv)...")
                self.aml_bulkcmd("saveenv")
                self.log("✓ Переменные окружения успешно модифицированы")

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
        
        self.is_flashing = True
        self.flash_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
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
        self.progress.stop()
        self.flash_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status_label.config(
            text="✅ Прошивка завершена или остановлена",
            fg="#27AE60"
        )


def main():
    root = tk.Tk()
    
    # Показываем окно помощи при первом запуске
    show_initial_help(root)
    
    app = FlasherGUI(root)
    root.mainloop()


def show_initial_help(root):
    """Показать помощь по необходимым файлам"""
    help_window = tk.Toplevel(root)
    help_window.title("📋 Необходимые файлы")
    help_window.geometry("600x500")
    help_window.resizable(True, True)
    help_window.minsize(450, 300)
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
        font=("Consolas", 9),
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
    
    # Кнопка закрытия
    tk.Button(
        help_window,
        text="Понятно, продолжить",
        command=help_window.destroy,
        font=("Arial", 11),
        bg="#27AE60",
        fg="white",
        pady=10
    ).pack(pady=10, padx=20, fill=tk.X)
    
    # Центрируем окно
    help_window.update_idletasks()
    x = (help_window.winfo_screenwidth() // 2) - (help_window.winfo_width() // 2)
    y = (help_window.winfo_screenheight() // 2) - (help_window.winfo_height() // 2)
    help_window.geometry(f"+{x}+{y}")


if __name__ == "__main__":
    main()
