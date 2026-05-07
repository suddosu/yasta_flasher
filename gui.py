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
  ├── system.img
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

# Импорт pyamlboot для загрузки U-Boot
try:
    from pyamlboot_local.boot import main as send_aml_bundle
except ImportError:
    send_aml_bundle = None

ROOT_DIR = os.getcwd()
FILE_DIR = os.path.join(ROOT_DIR, "files")
IMG_DIR = os.path.join(ROOT_DIR, "images-bkp")

# URL для загрузки утилит из GitHub (ИСПРАВЛЕННЫЙ)
GITHUB_TOOLS_BASE = "https://raw.githubusercontent.com/althafvly/Amlogic_Kitchen/master/bin/windows"

# Основные утилиты из репозитория
REQUIRED_TOOLS = [
    "update.exe",           # Основная утилита прошивки
]

# Дополнительные DLL (из внешних источников)
OPTIONAL_DLLS = {
    "libusb-1.0.dll": "https://github.com/libusb/libusb/releases/download/v1.0.26/libusb-1.0.26-binaries.7z",
    "msvcp100.dll": "https://www.microsoft.com/en-us/download/details.aspx?id=26999",  # Visual C++ 2010 Redistributable
    "msvcr100.dll": "https://www.microsoft.com/en-us/download/details.aspx?id=26999",  # Visual C++ 2010 Redistributable
}

PART_IMAGES = [
    {"name": "boot", "file": "boot.img", "display": "Boot", "time": "несколько секунд"},
    {"name": "dtbo", "file": "dtbo.img", "display": "DTBO", "time": "несколько секунд"},
    {"name": "vbmeta", "file": "vbmeta.img", "display": "VBMeta", "time": "несколько секунд"},
    {"name": "logo", "file": "logo.img", "display": "Logo", "time": "несколько секунд"},
    {"name": "odm", "file": "odm.img", "display": "ODM", "time": "несколько секунд"},
    {"name": "product", "file": "product.img", "display": "Product", "time": "несколько секунд"},
    {"name": "recovery", "file": "recovery.img", "display": "Recovery", "time": "несколько секунд"},
    {"name": "system", "file": "system.img", "display": "System", "time": "около 10 минут"},
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
        
        # Левая панель (основной функционал)
        left_panel = tk.Frame(main_container, padx=10, pady=10)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Правая панель (COM терминал)
        right_panel = tk.Frame(main_container, padx=10, pady=10, width=350)
        right_panel.pack(side=tk.RIGHT, fill=tk.BOTH)
        right_panel.pack_propagate(False)
        
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
        try:
            self.terminal_text.config(state='normal')
            # Удаляем текущую строку
            self.terminal_text.delete("end-1c linestart", "end-1c")
            # Добавляем обновлённую
            self.terminal_text.insert("end-1c", message)
            self.terminal_text.see(tk.END)
            self.terminal_text.config(state='disabled')
            self.terminal_text.update_idletasks()
        except:
            pass
    
    def terminal_add_line(self, message):
        """Добавление новой строки в терминал"""
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
        # Создаём диалоговое окно
        dialog = tk.Toplevel(self.root)
        dialog.title("Добавить кастомный раздел")
        dialog.geometry("400x200")
        dialog.transient(self.root)
        dialog.grab_set()
        
        tk.Label(dialog, text="Добавление кастомного раздела", font=("Arial", 12, "bold")).pack(pady=10)
        
        # Поле для имени раздела
        tk.Label(dialog, text="Имя раздела:", font=("Arial", 10)).pack(pady=(10, 0))
        name_entry = tk.Entry(dialog, width=30, font=("Arial", 10))
        name_entry.pack(pady=5)
        name_entry.focus()
        
        # Поле для файла образа
        tk.Label(dialog, text="Файл образа:", font=("Arial", 10)).pack(pady=(10, 0))
        
        file_frame = tk.Frame(dialog)
        file_frame.pack(pady=5)
        
        file_var = tk.StringVar()
        file_entry = tk.Entry(file_frame, textvariable=file_var, width=25, font=("Arial", 9))
        file_entry.pack(side=tk.LEFT, padx=(0, 5))
        
        def browse_file():
            filename = filedialog.askopenfilename(
                title="Выберите образ раздела",
                filetypes=[("Image files", "*.img"), ("All files", "*.*")]
            )
            if filename:
                file_var.set(filename)
        
        tk.Button(file_frame, text="Обзор...", command=browse_file, width=10).pack(side=tk.LEFT)
        
        # Кнопки
        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=20)
        
        def add_partition():
            name = name_entry.get().strip()
            file_path = file_var.get().strip()
            
            if not name:
                messagebox.showwarning("Предупреждение", "Введите имя раздела", parent=dialog)
                return
            
            if not file_path:
                messagebox.showwarning("Предупреждение", "Выберите файл образа", parent=dialog)
                return
            
            if not os.path.exists(file_path):
                messagebox.showerror("Ошибка", "Файл не существует", parent=dialog)
                return
            
            # Добавляем в список
            custom_part = {
                "name": name,
                "file": os.path.basename(file_path),
                "display": f"{name} (custom)",
                "time": "зависит от размера"
            }
            
            PART_IMAGES.append(custom_part)
            self.selected_images[name] = file_path
            
            # Добавляем чекбокс в интерфейс
            self.add_partition_checkbox(custom_part)
            
            self.log(f"✓ Добавлен кастомный раздел: {name}")
            messagebox.showinfo("Успех", f"Раздел '{name}' добавлен", parent=dialog)
            dialog.destroy()
        
        tk.Button(btn_frame, text="Добавить", command=add_partition, bg="#27AE60", fg="white", width=12).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Отмена", command=dialog.destroy, width=12).pack(side=tk.LEFT)
    
    def add_partition_checkbox(self, img):
        """Добавить чекбокс для нового раздела (должен быть вызван из основного потока)"""
        # Этот метод нужно вызвать для добавления UI элемента
        # Пока просто логируем - полная реализация требует рефакторинга
        pass
    
    def open_env_editor(self):
        """Открыть редактор ENV"""
        editor = tk.Toplevel(self.root)
        editor.title("⚙️ Редактор переменных окружения (ENV)")
        editor.geometry("800x700")
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
        
        # Notebook с вкладками
        notebook = ttk.Notebook(editor)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
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
        
        id_vars = {
            "serial": {
                "value": "X10AAA5555RM1K",
                "desc": "Серийный номер устройства (основной)",
                "critical": True
            },
            "deviceid": {
                "value": "X10AAA5555RM1K", 
                "desc": "ID устройства (используется в bootargs)",
                "critical": True
            },
            "custom_deviceid": {
                "value": "X10AAA5555RM1K",
                "desc": "Кастомный ID устройства",
                "critical": True
            },
            "aml_serial": {
                "value": "280c400001221a000000000000250",
                "desc": "Аппаратный серийный номер Amlogic",
                "critical": False
            },
            "mac": {
                "value": "3c:0b:4f:ff:ff:ff",
                "desc": "MAC адрес сетевого интерфейса",
                "critical": True
            },
            "ethaddr": {
                "value": "3c:0b:4f:ff:ff:ff",
                "desc": "Ethernet MAC адрес",
                "critical": True
            },
        }
        
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
                "value": "10000000",
                "desc": "🔓 Разблокировка bootloader (10000000 = разблокирован)",
                "critical": True
            },
            "avb2": {
                "value": "0",
                "desc": "🔓 Android Verified Boot 2.0 (0 = выключен, 1 = включен)",
                "critical": True
            },
            "EnableSelinux": {
                "value": "enforcing",
                "desc": "🔓 SELinux режим (permissive = отключен, enforcing = включен)",
                "critical": True,
                "options": ["permissive", "enforcing", "disabled"]
            },
            "jtag": {
                "value": "disable",
                "desc": "🔧 JTAG отладка (enable/disable)",
                "critical": False,
                "options": ["disable", "enable"]
            },
            "silent": {
                "value": "0",
                "desc": "📢 Вывод сообщений загрузки (0 = включен, 1 = выключен)",
                "critical": False
            },
            "rabbit_hole_debug": {
                "value": "0",
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
                "value": "1",
                "desc": "⏱️ Задержка перед загрузкой (секунды)",
                "critical": False
            },
            "led_screen_brightness": {
                "value": "100",
                "desc": "💡 Яркость экрана (0-255)",
                "critical": False
            },
            "led_ring_brightness": {
                "value": "10",
                "desc": "💡 Яркость кольца подсветки (0-255)",
                "critical": False
            },
            "localization": {
                "value": "RU.RU",
                "desc": "🌍 Локализация устройства",
                "critical": False
            },
            "hdmimode": {
                "value": "1080p60hz",
                "desc": "📺 Режим HDMI (1080p60hz, 720p60hz и т.д.)",
                "critical": False
            },
            "outputmode": {
                "value": "576cvbs",
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
        
        # Кнопки
        btn_frame = tk.Frame(editor)
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
        """Проверка и загрузка утилит при запуске"""
        missing_tools = []
        for tool in REQUIRED_TOOLS:
            if not os.path.exists(os.path.join(FILE_DIR, tool)):
                missing_tools.append(tool)
        
        if missing_tools:
            msg = f"Отсутствуют необходимые утилиты:\n\n"
            msg += "\n".join(f"• {tool}" for tool in missing_tools)
            msg += "\n\nЗагрузить их автоматически из GitHub?"
            
            response = messagebox.askyesno(
                "Необходимые файлы",
                msg,
                icon='question'
            )
            
            if response:
                self.download_tools_from_github()
    
    def download_tools_from_github(self):
        """Загрузка утилит из GitHub репозитория Khadas"""
        self.log("="*50)
        self.log("Загрузка утилит из GitHub...")
        self.log("Источник: khadas/utils")
        self.log("="*50)
        
        # Создаем окно прогресса
        progress_window = tk.Toplevel(self.root)
        progress_window.title("Загрузка файлов")
        progress_window.geometry("550x350")
        progress_window.transient(self.root)
        progress_window.grab_set()
        
        tk.Label(
            progress_window,
            text="Загрузка утилит из GitHub",
            font=("Arial", 12, "bold")
        ).pack(pady=10)
        
        progress_text = scrolledtext.ScrolledText(
            progress_window,
            height=12,
            font=("Consolas", 9)
        )
        progress_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        progress_bar = ttk.Progressbar(
            progress_window,
            mode='determinate',
            maximum=len(REQUIRED_TOOLS)
        )
        progress_bar.pack(fill=tk.X, padx=10, pady=5)
        
        status_label = tk.Label(progress_window, text="", font=("Arial", 9))
        status_label.pack(pady=5)
        
        def log_progress(msg):
            progress_text.insert(tk.END, f"{msg}\n")
            progress_text.see(tk.END)
            progress_text.update()
        
        def download_thread():
            success_count = 0
            failed_count = 0
            failed_files = []
            
            log_progress("📥 Загрузка из репозитория Khadas...")
            log_progress(f"URL: {GITHUB_TOOLS_BASE}")
            log_progress("-" * 60)
            
            for idx, tool in enumerate(REQUIRED_TOOLS):
                try:
                    status_label.config(text=f"Загрузка {tool}...")
                    log_progress(f"\n[{idx+1}/{len(REQUIRED_TOOLS)}] {tool}")
                    
                    url = f"{GITHUB_TOOLS_BASE}/{tool}"
                    dest_path = os.path.join(FILE_DIR, tool)
                    
                    # Проверяем, не существует ли файл уже
                    if os.path.exists(dest_path):
                        log_progress(f"  ⚠ Файл уже существует, пропускаем...")
                        success_count += 1
                        progress_bar['value'] = idx + 1
                        progress_window.update()
                        continue
                    
                    log_progress(f"  → Загрузка с {url}")
                    
                    # Загружаем файл с таймаутом
                    req = urllib.request.Request(url)
                    req.add_header('User-Agent', 'Mozilla/5.0')
                    
                    with urllib.request.urlopen(req, timeout=30) as response:
                        data = response.read()
                        with open(dest_path, 'wb') as f:
                            f.write(data)
                    
                    file_size = os.path.getsize(dest_path)
                    size_kb = file_size / 1024
                    log_progress(f"  ✓ Загружено: {size_kb:.1f} KB")
                    self.log(f"✓ Загружен: {tool} ({size_kb:.1f} KB)")
                    success_count += 1
                    
                except urllib.error.HTTPError as e:
                    log_progress(f"  ✗ HTTP ошибка: {e.code} {e.reason}")
                    self.log(f"✗ HTTP ошибка: {tool} - {e.code}")
                    failed_count += 1
                    failed_files.append(tool)
                    
                except urllib.error.URLError as e:
                    log_progress(f"  ✗ Сетевая ошибка: {str(e.reason)}")
                    self.log(f"✗ Сетевая ошибка: {tool}")
                    failed_count += 1
                    failed_files.append(tool)
                    
                except Exception as e:
                    log_progress(f"  ✗ Ошибка: {str(e)}")
                    self.log(f"✗ Ошибка: {tool} - {str(e)}")
                    failed_count += 1
                    failed_files.append(tool)
                
                progress_bar['value'] = idx + 1
                progress_window.update()
            
            log_progress("\n" + "="*60)
            log_progress("ℹ️  ДОПОЛНИТЕЛЬНЫЕ БИБЛИОТЕКИ")
            log_progress("="*60)
            log_progress("\nИсточник утилит:")
            log_progress("https://github.com/althafvly/Amlogic_Kitchen")
            log_progress("\nНекоторые DLL-библиотеки могут потребоваться:")
            log_progress("")
            for dll, url in OPTIONAL_DLLS.items():
                log_progress(f"• {dll}")
                if isinstance(url, str) and url.startswith("http"):
                    log_progress(f"  Скачать: {url}")
            log_progress("\nЕсли update.exe не запустится, установите:")
            log_progress("Microsoft Visual C++ 2010 Redistributable Package")
            log_progress("(обычно уже установлен в Windows)")
            
            log_progress("\n" + "="*60)
            log_progress(f"✓ ЗАВЕРШЕНО")
            log_progress(f"  Успешно: {success_count} файлов")
            log_progress(f"  Ошибок: {failed_count}")
            if failed_files:
                log_progress(f"  Не загружены: {', '.join(failed_files)}")
            log_progress("="*60)
            
            status_label.config(text=f"Завершено: {success_count} успешно, {failed_count} ошибок")
            self.log(f"Загрузка завершена: {success_count} файлов, {failed_count} ошибок")
            
            time.sleep(3)
            progress_window.destroy()
            
            if success_count > 0:
                msg = f"✓ Загружено файлов: {success_count}\n"
                if failed_count > 0:
                    msg += f"⚠ Ошибок: {failed_count}\n\n"
                    msg += "Не удалось загрузить:\n"
                    msg += "\n".join(f"• {f}" for f in failed_files)
                    msg += "\n\nВозможно, файлы удалены из репозитория."
                    msg += "\nПопробуйте загрузить их вручную."
                    messagebox.showwarning("Частичная загрузка", msg)
                else:
                    msg += "\nУтилиты готовы к использованию!"
                    msg += "\n\nℹ️ Если update.exe не запустится, установите:"
                    msg += "\nMicrosoft Visual C++ 2010 Redistributable"
                    messagebox.showinfo("Успех", msg)
                self.check_all_files()
            else:
                messagebox.showerror(
                    "Ошибка загрузки",
                    "Не удалось загрузить файлы.\n\n"
                    "Возможные причины:\n"
                    "• Нет подключения к интернету\n"
                    "• GitHub недоступен\n"
                    "• Файлы изменили расположение в репозитории\n\n"
                    "Решение:\n"
                    "Загрузите update.exe вручную из:\n"
                    "https://github.com/khadas/utils/tree/master/"
                    "aml-flash-tool/tools/windows"
                )
        
        thread = threading.Thread(target=download_thread, daemon=True)
        thread.start()
    
    def log(self, message):
        """Логирование в журнал операций"""
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')
        self.log_text.update_idletasks()
    
    def test_usb_detection(self):
        """Тестовая функция для проверки обнаружения USB"""
        self.log("\n" + "="*50)
        self.log("🔍 ДИАГНОСТИКА USB УСТРОЙСТВ")
        self.log("="*50)
        
        # Метод 1: WMIC
        self.log("\n1️⃣ Проверка через WMIC...")
        try:
            result = subprocess.run(
                ['wmic', 'path', 'Win32_PnPEntity', 'where', 
                 'DeviceID like "%USB%"', 'get', 'DeviceID,Name'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            
            lines = [line.strip() for line in result.stdout.split('\n') if line.strip()]
            self.log(f"  Найдено USB устройств: {len(lines) - 1}")
            
            # Ищем Amlogic
            found_amlogic = False
            for line in lines:
                if any(marker in line.upper() for marker in ["1B8E", "C003", "AMLOGIC", "BURNING"]):
                    self.log(f"  ✓ НАЙДЕНО: {line}")
                    found_amlogic = True
            
            if not found_amlogic:
                self.log("  ✗ Amlogic устройство НЕ найдено")
                self.log("\n  📋 Все USB устройства:")
                for line in lines[:10]:  # Показываем первые 10
                    if line and "DeviceID" not in line:
                        self.log(f"    • {line[:80]}")
                        
        except Exception as e:
            self.log(f"  ✗ Ошибка: {str(e)}")
        
        # Метод 2: PowerShell
        self.log("\n2️⃣ Проверка через PowerShell...")
        try:
            result = subprocess.run(
                ['powershell', '-Command', 
                 'Get-PnpDevice -Class USB | Where-Object {$_.Status -eq "OK"} | Select-Object -Property DeviceID,FriendlyName'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            
            if result.returncode == 0:
                lines = [line.strip() for line in result.stdout.split('\n') if line.strip()]
                self.log(f"  Активных USB устройств: {len(lines)}")
                
                found = False
                for line in lines:
                    if any(marker in line.upper() for marker in ["1B8E", "C003", "AMLOGIC"]):
                        self.log(f"  ✓ НАЙДЕНО: {line}")
                        found = True
                
                if not found:
                    self.log("  ✗ Amlogic устройство НЕ найдено")
            else:
                self.log("  ✗ PowerShell недоступен или ошибка")
                
        except Exception as e:
            self.log(f"  ✗ Ошибка: {str(e)}")
        
        self.log("\n" + "="*50)
        self.log("💡 Если устройство не найдено:")
        self.log("  1. Проверьте Диспетчер устройств (devmgmt.msc)")
        self.log("  2. Установите драйверы USB Burning Tool")
        self.log("  3. Попробуйте другой USB порт (USB 2.0)")
        self.log("  4. Проверьте замыкание пина 6 на GND")
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
            if send_aml_bundle:
                try:
                    # pyamlboot сам умеет находить устройства
                    # Если устройство есть - это будет быстро
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
        """Выполнение команды U-Boot"""
        update_path = self.get_update_path()
        process = subprocess.run(
            [update_path, "bulkcmd", cmd],
            capture_output=True,
            text=False,  # Получаем байты
            timeout=30
        )
        if process.returncode != 0:
            # Пытаемся декодировать вывод
            try:
                error_msg = process.stderr.decode('utf-8', errors='ignore')
            except:
                error_msg = str(process.stderr)
            raise Exception(f"Ошибка U-Boot команды: {cmd}\n{error_msg}")
    
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
        """Прошивка раздела"""
        self.log(f"Прошивка {display}... ({time_estimate})")
        
        update_path = self.get_update_path()
        cmd = [update_path, "partition", name, image_path]
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,  # Получаем байты, не текст
                bufsize=0
            )
            
            start_time = time.time()
            last_update = 0
            
            while True:
                if not self.is_flashing:
                    process.terminate()
                    return False
                
                code = process.poll()
                if code is not None:
                    break
                
                # Показываем прогресс каждые 2 секунды (не каждую секунду!)
                elapsed = int(time.time() - start_time)
                if elapsed - last_update >= 2:  # Обновляем раз в 2 секунды
                    # Обновляем только статус, не логируем
                    self.status_label.config(
                        text=f"🔄 Прошивка {display}... {elapsed}с",
                        fg="#E67E22"
                    )
                    self.root.update_idletasks()
                    last_update = elapsed
                
                time.sleep(0.5)
            
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
            if send_aml_bundle:
                try:
                    bundle_path = os.path.join(FILE_DIR, "aml_bundle.img")
                    if not os.path.exists(bundle_path):
                        raise FileNotFoundError(f"Файл {bundle_path} не найден")
                    send_aml_bundle(bundle_path)
                    self.log("✓ U-Boot загружен")
                    time.sleep(4)
                except Exception as e:
                    self.log(f"✗ Ошибка загрузки U-Boot: {str(e)}")
                    self.finish_flashing()
                    return
            else:
                self.log("✗ pyamlboot не найден! Установите: pip install pyamlboot")
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
                
                # Читаем текущий env
                self.log("  - Чтение env с устройства...")
                self.aml_read_part("env", "0x800000", env_file)
                
                if not os.path.exists(env_file):
                    raise Exception("Не удалось прочитать env с устройства")
                
                env_size = os.path.getsize(env_file)
                self.log(f"  - Прочитано: {env_size} байт")
                
                # Предлагаем отредактировать
                if not self.env_data:  # Если не редактировали ещё
                    response = messagebox.askyesno(
                        "Редактирование ENV",
                        "ENV успешно прочитан с устройства.\n\n"
                        "Хотите отредактировать переменные окружения?\n"
                        "(Рекомендуется для разблокировки bootloader)"
                    )
                    if response:
                        # Приостанавливаем прошивку
                        self.is_flashing = False
                        self.open_env_editor()
                        
                        # Ждём закрытия редактора
                        messagebox.showinfo(
                            "Продолжение",
                            "После редактирования ENV нажмите OK для продолжения прошивки."
                        )
                        self.is_flashing = True
                
                # Загружаем в RAM
                self.log("  - Загрузка env в RAM...")
                self.aml_write_file_to_ram(env_file, "0x200c000")
                
                # Импортируем и модифицируем
                self.log("  - Импорт переменных в U-Boot...")
                self.aml_bulkcmd("env import 200c004")
                
                # Применяем изменения из редактора или стандартные
                if self.env_data:
                    self.log("  - Применение пользовательских настроек ENV:")
                    for key, value in self.env_data.items():
                        self.log(f"    • {key} = {value}")
                        self.aml_bulkcmd(f"setenv {key} {value}")
                else:
                    self.log("  - Применение стандартных настроек:")
                    self.log("    • silent = 0 (включить вывод)")
                    self.aml_bulkcmd("setenv silent 0")
                    
                    self.log("    • lock = 10000000 (разблокировать)")
                    self.aml_bulkcmd("setenv lock 10000000")
                    
                    self.log("    • avb2 = 0 (отключить Android Verified Boot)")
                    self.aml_bulkcmd("setenv avb2 0")
                
                self.log("  - Сохранение изменений...")
                self.aml_bulkcmd("saveenv")
                
                self.log("✓ Переменные окружения успешно модифицированы")
                
                # Удаляем временный файл
                if os.path.exists(env_file):
                    try:
                        os.remove(env_file)
                    except:
                        pass  # Не критично если не удалилось
                    
            except Exception as e:
                self.log(f"✗ Ошибка модификации env: {str(e)}")
                self.log("⚠ Продолжаем без модификации env (может потребоваться ручная настройка)")
                # Не прерываем процесс, продолжаем прошивку
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