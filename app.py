#!/usr/bin/env python3
"""PSP Stick Tuner — calibração leve do joystick ZMK via USB."""

from __future__ import annotations

import atexit
import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

# Brand (gsmoura.com.br)
BRAND_BG = "#080808"
BRAND_PANEL = "#121212"
BRAND_FG = "#f2f2f2"
BRAND_MUTED = "#9a9a9a"
BRAND_ACCENT = "#c8f135"
BRAND_BORDER = "#2a2a2a"
SITE_URL = "https://gsmoura.com.br"
APP_DIR = Path(__file__).resolve().parent
ICON_PATH = APP_DIR / "assets" / "icon.ico"

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit("Instale as dependências: pip install -r requirements.txt")

STICK_RE = re.compile(r"STICK\s+(OK|ERR)\b(.*)$")
AIN_RE = re.compile(r"AIN(\d+)\s+raw:\s*(-?\d+)\s+mv:\s*(-?\d+)")
LIVE_RE = re.compile(r"live_x=(-?\d+)\s+live_y=(-?\d+)")
BAUD = 115200
ZMK_VID = 0x1D50
ZMK_PID = 0x615E
CALIB_PATH = Path.home() / "AppData" / "Local" / "psp-stick-tuner" / "calib.json"
LOCK_PATH = Path.home() / "AppData" / "Local" / "psp-stick-tuner" / "app.lock"


def acquire_single_instance() -> object | None:
    """Impede várias janelas do tuner (travam a COM no Windows)."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fp = open(LOCK_PATH, "a+b")
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt

            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fp.close()
        return None

    def _release():
        try:
            if sys.platform == "win32":
                import msvcrt

                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            fp.close()
        except OSError:
            pass

    atexit.register(_release)
    return fp


def port_is_free(device: str) -> bool:
    try:
        ser = serial.Serial()
        ser.port = device
        ser.baudrate = BAUD
        ser.timeout = 0.05
        ser.dsrdtr = False
        ser.rtscts = False
        ser.dtr = False
        ser.rts = False
        ser.open()
        ser.close()
        return True
    except serial.SerialException:
        return False


def open_serial(device: str, timeout: float = 0.05) -> serial.Serial:
    """Abre a COM sem pulsar DTR (evita reset do RP2040 no Windows)."""
    ser = serial.Serial()
    ser.port = device
    ser.baudrate = BAUD
    ser.timeout = timeout
    ser.write_timeout = 2
    ser.dsrdtr = False
    ser.rtscts = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass
    time.sleep(0.1)
    ser.reset_input_buffer()
    return ser


def list_candidate_ports():
    ports = []
    for p in list_ports.comports():
        meta = " ".join(
            filter(
                None,
                [p.description or "", p.manufacturer or "", p.product or "", p.hwid or ""],
            )
        )
        is_zmk = (p.vid == ZMK_VID and p.pid == ZMK_PID) or "1D50" in (p.hwid or "").upper()
        is_psp = "PSP" in meta.upper()
        if is_psp:
            tag = "★ Joystick PSP Stick"
        elif is_zmk:
            tag = "• Dispositivo ZMK"
        else:
            tag = "Outra porta"
        label = f"{p.device}  {tag}  ({p.description or 'serial'})"
        ports.append(
            {
                "device": p.device,
                "label": label,
                "is_zmk": is_zmk,
                "is_psp": is_psp,
                "score": (2 if is_psp else 0) + (1 if is_zmk else 0),
            }
        )
    ports.sort(key=lambda x: (-x["score"], x["device"]))
    return ports


def probe_stick(device: str, wait_s: float = 3.0) -> dict:
    """Testa uma COM. score: 0=falha, 1=AIN só, 2=responde stick get."""
    result = {"device": device, "ok": False, "score": 0, "ain": 0, "error": ""}
    try:
        ser = open_serial(device, timeout=0.15)
    except serial.SerialException as exc:
        result["error"] = str(exc)
        return result
    try:
        # Escuta tráfego espontâneo (AIN) — a porta certa do ZMK costuma spit logs
        t0 = time.time()
        buf = ""
        while time.time() - t0 < min(0.8, wait_s * 0.25):
            chunk = ser.read(512)
            if chunk:
                txt = chunk.decode("utf-8", errors="ignore")
                buf += txt
                result["ain"] += txt.count("AIN")
        if result["ain"] > 0:
            result["score"] = 1
            result["ok"] = True

        deadline = time.time() + wait_s
        next_send = 0.0
        while time.time() < deadline:
            now = time.time()
            if now >= next_send:
                try:
                    ser.write(b"\n")
                    ser.write(b"stick get\n")
                    ser.flush()
                except serial.SerialException as exc:
                    result["error"] = str(exc)
                    return result
                next_send = now + 0.6
            chunk = ser.read(512)
            if chunk:
                txt = chunk.decode("utf-8", errors="ignore")
                buf += txt
                result["ain"] += txt.count("AIN")
                if "STICK OK" in buf or "STICK ERR" in buf:
                    result["score"] = 2
                    result["ok"] = True
                    return result
            time.sleep(0.04)
        if result["ain"] > 0:
            result["score"] = max(result["score"], 1)
            result["ok"] = True
        return result
    finally:
        try:
            ser.close()
        except Exception:
            pass


def find_best_stick_port(candidates: list[dict] | None = None, wait_s: float = 3.0) -> dict | None:
    """Escolhe a COM pelo que responde — sem preferir número de porta."""
    ports = candidates or list_candidate_ports()
    ranked = [p for p in ports if p.get("is_zmk") or p.get("is_psp")] or list(ports)
    best = None
    for p in ranked:
        probe = probe_stick(p["device"], wait_s=wait_s)
        p = {**p, **probe}
        if best is None or p["score"] > best["score"]:
            best = p
        # stick get é decisivo — pode parar cedo
        if p["score"] >= 2:
            return p
    if best and best.get("score", 0) > 0:
        return best
    return None


class StickTunerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PSP Stick Tuner · Gustavo Moura")
        self.geometry("560x680")
        self.minsize(500, 520)
        self.configure(bg=BRAND_BG)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass

        self.ser: serial.Serial | None = None
        self.reader_stop = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.busy = False

        self.live_x = tk.StringVar(value="—")
        self.live_y = tk.StringVar(value="—")
        self.status = tk.StringVar(value="Desconectado — ligue o stick no USB")
        self.port_var = tk.StringVar()

        self.mid_x = tk.IntVar(value=8880)
        self.mid_y = tk.IntVar(value=9400)
        self.deadzone = tk.IntVar(value=350)
        self.divisor = tk.IntVar(value=500)
        self.invert_x = tk.BooleanVar(value=False)
        self.invert_y = tk.BooleanVar(value=False)

        self._mv_hist = {0: deque(maxlen=20), 1: deque(maxlen=20)}
        self._stick_replies: queue.Queue[str] = queue.Queue()
        self._port_map: dict[str, str] = {}
        self._ui_x = None
        self._ui_y = None
        self._live_job = None

        self._setup_style()
        self._build_ui()
        self._load_local_calib()
        self.refresh_ports(select_best=True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BRAND_BG, foreground=BRAND_FG, fieldbackground=BRAND_PANEL)
        style.configure("TFrame", background=BRAND_BG)
        style.configure("TLabel", background=BRAND_BG, foreground=BRAND_FG, font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background=BRAND_BG, foreground=BRAND_MUTED, font=("Segoe UI", 8))
        style.configure("Title.TLabel", background=BRAND_BG, foreground=BRAND_FG, font=("Segoe UI Semibold", 15))
        style.configure("Accent.TLabel", background=BRAND_BG, foreground=BRAND_ACCENT, font=("Segoe UI", 9, "bold"))
        style.configure("Live.TLabel", background=BRAND_BG, foreground=BRAND_ACCENT, font=("Consolas", 13, "bold"))
        style.configure("TLabelframe", background=BRAND_BG, foreground=BRAND_FG, bordercolor=BRAND_BORDER)
        style.configure("TLabelframe.Label", background=BRAND_BG, foreground=BRAND_ACCENT, font=("Segoe UI", 9, "bold"))
        style.configure("TButton", background=BRAND_PANEL, foreground=BRAND_FG, padding=6, borderwidth=0)
        style.map("TButton", background=[("active", "#1c1c1c"), ("disabled", "#101010")])
        style.configure("Accent.TButton", background=BRAND_ACCENT, foreground="#080808", padding=7)
        style.map("Accent.TButton", background=[("active", "#d8ff4a"), ("disabled", "#5a6a20")])
        style.configure("TCheckbutton", background=BRAND_BG, foreground=BRAND_FG)
        style.map("TCheckbutton", background=[("active", BRAND_BG)])
        style.configure("TCombobox", fieldbackground=BRAND_PANEL, background=BRAND_PANEL, foreground=BRAND_FG)
        style.configure("Horizontal.TScale", background=BRAND_BG, troughcolor=BRAND_PANEL)
        style.configure("Vertical.TScrollbar", background=BRAND_PANEL, troughcolor=BRAND_BG)

    def _load_local_calib(self):
        try:
            data = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if "mid_x" in data:
            self.mid_x.set(int(data["mid_x"]))
        if "mid_y" in data:
            self.mid_y.set(int(data["mid_y"]))
        if "deadzone" in data:
            self.deadzone.set(int(data["deadzone"]))
        if "divisor" in data:
            self.divisor.set(int(data["divisor"]))
        if "invert_x" in data:
            self.invert_x.set(bool(data["invert_x"]))
        if "invert_y" in data:
            self.invert_y.set(bool(data["invert_y"]))

    def _save_local_calib(self) -> bool:
        payload = {
            "mid_x": self.mid_x.get(),
            "mid_y": self.mid_y.get(),
            "deadzone": self.deadzone.get(),
            "divisor": self.divisor.get(),
            "invert_x": 1 if self.invert_x.get() else 0,
            "invert_y": 1 if self.invert_y.get() else 0,
        }
        try:
            CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
            CALIB_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.log_line(f"salvo em {CALIB_PATH}")
            return True
        except OSError as exc:
            self.log_line(f"! não gravou no PC: {exc}")
            return False

    def _apply_current(self) -> bool:
        mx, my = self.mid_x.get(), self.mid_y.get()
        dz, div = self.deadzone.get(), self.divisor.get()
        inv_x = 1 if self.invert_x.get() else 0
        inv_y = 1 if self.invert_y.get() else 0
        results = [
            self.send_cmd(f"stick mid {mx} {my}", expect="mid_x="),
            self.send_cmd(f"stick deadzone {dz}", expect="deadzone="),
            self.send_cmd(f"stick divisor {div}", expect="divisor="),
            self.send_cmd(f"stick invert_x {inv_x}", expect="invert_x="),
            self.send_cmd(f"stick invert_y {inv_y}", expect="invert_y="),
        ]
        return any(results)

    def _build_ui(self):
        top = ttk.Frame(self, padding=(14, 12, 14, 8))
        top.pack(fill=tk.X)
        titles = ttk.Frame(top)
        titles.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(titles, text="PSP Stick Tuner", style="Title.TLabel").pack(anchor=tk.W)
        link = ttk.Label(titles, text="gsmoura.com.br · software sob medida", style="Accent.TLabel", cursor="hand2")
        link.pack(anchor=tk.W, pady=(2, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(SITE_URL))

        self.btn_save = ttk.Button(top, text="Salvar", style="Accent.TButton", command=self.save_settings)
        self.btn_save.pack(side=tk.RIGHT)
        self.btn_read = ttk.Button(top, text="Ler da placa", command=self.read_from_stick)
        self.btn_read.pack(side=tk.RIGHT, padx=(0, 8))

        ttk.Label(self, textvariable=self.status, style="Muted.TLabel", padding=(14, 0)).pack(fill=tk.X)

        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        canvas = tk.Canvas(container, highlightthickness=0, bg=BRAND_BG)
        scroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        self.scroll_body = ttk.Frame(canvas)
        self.scroll_body.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        self._canvas_window = canvas.create_window((0, 0), window=self.scroll_body, anchor=tk.NW)

        def _on_canvas_configure(event):
            canvas.itemconfigure(self._canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        self._canvas = canvas

        body = self.scroll_body
        pad = {"padx": 8, "pady": 5}

        ttk.Label(
            body,
            text="Calibre centro, zona morta e velocidade. Não toque nos fios do stick "
            "(ruído do corpo faz o cursor andar sozinho).",
            style="Muted.TLabel",
            wraplength=500,
        ).pack(anchor=tk.W, padx=8, pady=(4, 8))

        step1 = ttk.LabelFrame(body, text="1) Conectar", padding=8)
        step1.pack(fill=tk.X, **pad)
        row = ttk.Frame(step1)
        row.pack(fill=tk.X)
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var, state="readonly")
        self.port_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Atualizar", command=lambda: self.refresh_ports(True)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        btns = ttk.Frame(step1)
        btns.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btns, text="Detectar joystick", command=self.detect_stick).pack(side=tk.LEFT)
        self.btn_connect = ttk.Button(btns, text="Conectar", command=self.toggle_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=8)

        step2 = ttk.LabelFrame(body, text="2) Centro (stick parado)", padding=8)
        step2.pack(fill=tk.X, **pad)
        live = ttk.Frame(step2)
        live.pack(fill=tk.X)
        ttk.Label(live, text="X").pack(side=tk.LEFT)
        ttk.Label(live, textvariable=self.live_x, style="Live.TLabel").pack(side=tk.LEFT, padx=(4, 14))
        ttk.Label(live, text="Y").pack(side=tk.LEFT)
        ttk.Label(live, textvariable=self.live_y, style="Live.TLabel").pack(side=tk.LEFT, padx=(4, 14))
        ttk.Button(live, text="Usar como centro", command=self.capture_center).pack(side=tk.LEFT)

        step3 = ttk.LabelFrame(body, text="3) Ajustes", padding=8)
        step3.pack(fill=tk.X, **pad)
        step3.columnconfigure(1, weight=1)

        def add_scale(row_i, label, help_text, var, from_, to):
            ttk.Label(step3, text=label).grid(row=row_i, column=0, sticky=tk.W, pady=2)
            scale = ttk.Scale(
                step3,
                from_=from_,
                to=to,
                orient=tk.HORIZONTAL,
                command=lambda v, variable=var: variable.set(int(float(v))),
            )
            scale.set(var.get())
            scale.grid(row=row_i, column=1, sticky=tk.EW, padx=6)
            ttk.Label(step3, textvariable=var, width=5).grid(row=row_i, column=2, sticky=tk.E)
            ttk.Label(step3, text=help_text, style="Muted.TLabel").grid(
                row=row_i + 1, column=1, sticky=tk.W
            )
            var.trace_add("write", lambda *_a, v=var, s=scale: s.set(v.get()))

        add_scale(0, "Centro X", "Se driftar para os lados", self.mid_x, 0, 12000)
        add_scale(2, "Centro Y", "Se driftar para cima/baixo", self.mid_y, 0, 12000)
        add_scale(4, "Zona morta", "Maior = menos tremor / menos ruído ao tocar", self.deadzone, 0, 600)
        add_scale(
            6,
            "Velocidade",
            "MAIOR = mais lento (no máximo de inclinação)",
            self.divisor,
            50,
            1000,
        )
        inv = ttk.Frame(step3)
        inv.grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=6)
        ttk.Checkbutton(inv, text="Inverter esquerda/direita (X)", variable=self.invert_x).pack(
            anchor=tk.W
        )
        ttk.Checkbutton(inv, text="Inverter cima/baixo (Y)", variable=self.invert_y).pack(
            anchor=tk.W, pady=(2, 0)
        )

        ttk.Label(
            body,
            text="Salvar aplica na placa e guarda no PC. Ao Conectar de novo, reaplica sozinho.",
            style="Muted.TLabel",
            wraplength=500,
        ).pack(anchor=tk.W, padx=8, pady=(4, 6))

        log_frame = ttk.LabelFrame(body, text="Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self.log = tk.Text(
            log_frame,
            height=5,
            wrap=tk.WORD,
            state=tk.DISABLED,
            bg=BRAND_PANEL,
            fg=BRAND_FG,
            insertbackground=BRAND_FG,
            relief=tk.FLAT,
            font=("Consolas", 9),
        )
        self.log.pack(fill=tk.BOTH, expand=True)

        foot = ttk.Frame(self, padding=(14, 4, 14, 10))
        foot.pack(fill=tk.X)
        ttk.Label(
            foot,
            text="Feito por Gustavo Moura · GV, MG",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)
        site = ttk.Label(foot, text=SITE_URL.replace("https://", ""), style="Accent.TLabel", cursor="hand2")
        site.pack(side=tk.RIGHT)
        site.bind("<Button-1>", lambda _e: webbrowser.open(SITE_URL))

    def log_line(self, text: str):
        def _append():
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, text + "\n")
            # Mantém log curto
            lines = int(self.log.index("end-1c").split(".")[0])
            if lines > 80:
                self.log.delete("1.0", f"{lines - 60}.0")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        self.after(0, _append)

    def _set_busy(self, busy: bool, status: str | None = None):
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.btn_save.configure(state=state)
        self.btn_read.configure(state=state)
        if status:
            self.status.set(status)

    def refresh_ports(self, select_best: bool = False):
        ports = list_candidate_ports()
        labels = [p["label"] for p in ports]
        self._port_map = {p["label"]: p["device"] for p in ports}
        self.port_combo["values"] = labels
        if not labels:
            self.port_var.set("")
            self.status.set("Nenhuma porta serial — conecte o stick no USB")
            return
        # Só preenche a lista — a porta certa vem do Detectar/Conectar (probe)
        if self.port_var.get() not in self._port_map:
            self.port_var.set(labels[0] if labels else "")

    def toggle_connect(self):
        if self.ser and self.ser.is_open:
            self.disconnect()
        else:
            # Sempre escolhe pela resposta do stick — não pela COM favorita
            self.connect(auto_pick=True)

    def connect(self, device: str | None = None, auto_pick: bool = False) -> bool:
        self.refresh_ports(select_best=False)

        if device is None and auto_pick:
            self.status.set("Procurando porta do stick…")
            self.update_idletasks()
            best = find_best_stick_port(wait_s=2.5)
            if best:
                device = best["device"]
                self.log_line(
                    f"porta escolhida {device} (score={best.get('score')} ain={best.get('ain')})"
                )
            else:
                messagebox.showwarning(
                    "Não encontrado",
                    "Nenhuma COM respondeu ao stick.\n"
                    "Desconecte/reconecte o USB e feche outros apps serial.",
                )
                return False
        elif device is None:
            label = self.port_var.get()
            if not label or label not in self._port_map:
                messagebox.showwarning("Porta", "Escolha uma porta serial ou use Detectar.")
                return False
            device = self._port_map[label]

        try:
            self.ser = open_serial(device)
        except serial.SerialException as exc:
            msg = str(exc)
            if "PermissionError" in msg or "Acesso negado" in msg or "Access is denied" in msg:
                messagebox.showerror(
                    "Porta ocupada",
                    f"Não deu para abrir {device}.\n\n"
                    "Feche outras janelas do tuner / apps serial,\n"
                    "ou use Detectar (ele testa todas as COMs).",
                )
            else:
                messagebox.showerror("Erro", f"Não foi possível abrir {device}:\n{msg}")
            self.ser = None
            return False

        for label, dev in self._port_map.items():
            if dev == device:
                self.port_var.set(label)
                break

        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.btn_connect.configure(text="Desconectar")
        self.status.set(f"Conectado em {device}")
        self.log_line(f"== conectado {device} ==")
        self.after(400, lambda: self._run_bg(self._job_reconnect_apply))
        self._schedule_live_poll()
        return True

    def disconnect(self):
        if self._live_job is not None:
            try:
                self.after_cancel(self._live_job)
            except Exception:
                pass
            self._live_job = None
        self.reader_stop.set()
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=1.0)
        self.reader_thread = None
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.btn_connect.configure(text="Conectar")
        self.status.set("Desconectado")
        self.log_line("== desconectado ==")

    def detect_stick(self):
        if self.busy:
            return
        if self.ser and self.ser.is_open:
            self.disconnect()
        self.refresh_ports(select_best=False)
        self.status.set("Procurando joystick em todas as COMs…")
        self.update_idletasks()

        def work():
            ports = list_candidate_ports()
            for p in ports:
                if p["is_zmk"] or p["is_psp"]:
                    self.log_line(f"testando {p['device']}…")
            best = find_best_stick_port(ports, wait_s=3.0)

            def done():
                if best and best.get("ok"):
                    found = best["device"]
                    for label, dev in self._port_map.items():
                        if dev == found:
                            self.port_var.set(label)
                            break
                    self.log_line(
                        f"joystick em {found} (score={best.get('score')} ain={best.get('ain')})"
                    )
                    self.connect(found)
                    messagebox.showinfo(
                        "Detectado",
                        f"Joystick encontrado em {found}.\n"
                        "A porta muda conforme o USB/PC — o Detectar escolhe pela resposta.",
                    )
                else:
                    self.status.set("Não achei o joystick")
                    messagebox.showwarning(
                        "Não encontrado",
                        "Nenhuma COM respondeu ao stick.\n\n"
                        "Checklist:\n"
                        "• Só uma janela do tuner aberta\n"
                        "• Desconecte e reconecte o USB\n"
                        "• Feche PuTTY / outros apps serial",
                    )

            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _reader_loop(self):
        buf = ""
        while not self.reader_stop.is_set():
            try:
                if not self.ser or not self.ser.is_open:
                    break
                chunk = self.ser.read(256)
                if not chunk:
                    continue
                buf += chunk.decode("utf-8", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._handle_line(line.strip("\r"))
            except serial.SerialException:
                self.after(0, lambda: self.status.set("Porta perdida — reconecte o USB"))
                break
            except Exception as exc:
                self.log_line(f"reader: {exc}")
                time.sleep(0.05)

    def _set_live(self, x: int | None = None, y: int | None = None):
        if x is not None:
            with self.lock:
                self._mv_hist[0].append(x)
            self._ui_x = x
        if y is not None:
            with self.lock:
                self._mv_hist[1].append(y)
            self._ui_y = y
        # Atualiza UI no próximo tick (coalescido)
        if not getattr(self, "_live_ui_pending", False):
            self._live_ui_pending = True
            self.after(0, self._flush_live_ui)

    def _handle_line(self, line: str):
        if not line:
            return
        # Logs AIN (firmware com DBG_RAW rate-limited)
        m = AIN_RE.search(line)
        if m:
            idx = int(m.group(1))
            mv = int(m.group(3))
            if idx == 0:
                self._set_live(x=mv)
            elif idx == 1:
                self._set_live(y=mv)
            return

        sm = STICK_RE.search(line)
        if sm:
            self._stick_replies.put(line)
            rest = sm.group(2)
            lm = LIVE_RE.search(rest)
            if lm:
                self._set_live(x=int(lm.group(1)), y=int(lm.group(2)))
            if sm.group(1) == "OK" and "mid_x=" in rest:
                self.log_line(line)
                self.after(0, lambda r=rest: self._parse_stick_ok(r))
            elif sm.group(1) == "OK" and "saved" in rest:
                self.log_line(line)
            elif sm.group(1) == "ERR":
                self.log_line(line)

    def _flush_live_ui(self):
        self._live_ui_pending = False
        if self._ui_x is not None:
            self.live_x.set(str(self._ui_x))
        if self._ui_y is not None:
            self.live_y.set(str(self._ui_y))

    def _schedule_live_poll(self):
        self._flush_live_ui()
        if not self.ser or not self.ser.is_open:
            return
        # Poll leve; aceita stick live OU AIN espontâneo
        if not self.busy:
            self._write_raw("stick live\n")
        self._live_job = self.after(300, self._schedule_live_poll)

    def _parse_stick_ok(self, rest: str):
        def grab(name):
            m = re.search(rf"{name}=(-?\d+)", rest)
            return int(m.group(1)) if m else None

        mx, my = grab("mid_x"), grab("mid_y")
        dz, div = grab("deadzone"), grab("divisor")
        inv_x, inv_y = grab("invert_x"), grab("invert_y")
        if mx is not None:
            self.mid_x.set(mx)
        if my is not None:
            self.mid_y.set(my)
        if dz is not None:
            self.deadzone.set(dz)
        if div is not None:
            self.divisor.set(div)
        if inv_x is not None:
            self.invert_x.set(bool(inv_x))
        if inv_y is not None:
            self.invert_y.set(bool(inv_y))

    def _drain_stick_replies(self):
        while True:
            try:
                self._stick_replies.get_nowait()
            except queue.Empty:
                break

    def _write_raw(self, data: str):
        if not self.ser or not self.ser.is_open:
            return
        try:
            self.ser.write(data.encode("utf-8"))
            self.ser.flush()
        except serial.SerialException:
            pass

    def send_cmd(self, cmd: str, expect: str | None = None, timeout: float = 5.0) -> bool:
        """Chamar só de thread de background (nunca da UI)."""
        if not self.ser or not self.ser.is_open:
            return False
        self._drain_stick_replies()
        self.log_line(f"> {cmd}")
        try:
            self.ser.write(b"\n")
            time.sleep(0.03)
            self.ser.write((cmd.strip() + "\n").encode("utf-8"))
            self.ser.flush()
        except serial.SerialException as exc:
            self.log_line(f"! serial: {exc}")
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                reply = self._stick_replies.get(timeout=0.1)
            except queue.Empty:
                continue
            sm = STICK_RE.search(reply)
            if not sm:
                continue
            if expect and expect not in reply:
                continue
            if sm.group(1) == "OK":
                return True
            self.log_line(f"! placa: {reply}")
            return False
        self.log_line("! timeout")
        return False

    def _run_bg(self, fn):
        if self.busy:
            return
        self._set_busy(True, "Trabalhando…")

        def work():
            try:
                fn()
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=work, daemon=True).start()

    def _job_read(self):
        ok = self.send_cmd("stick get", expect="mid_x=")
        self.after(
            0,
            lambda: self.status.set("Valores lidos da placa" if ok else "Falha ao ler"),
        )

    def _job_reconnect_apply(self):
        if not CALIB_PATH.exists():
            self.send_cmd("stick get", expect="mid_x=")
            self.after(0, lambda: self.status.set("Conectado"))
            return
        ok = self._apply_current()
        self.after(
            0,
            lambda: self.status.set(
                "Calibração do PC aplicada na placa" if ok else "Conectado (falha ao reaplicar)"
            ),
        )

    def _job_save(self):
        # Não chama stick save na flash — no RP2040 settings_save_one trava a placa.
        applied = self._apply_current()
        if not applied:
            self.after(
                0,
                lambda: messagebox.showwarning(
                    "Falha",
                    "A placa não respondeu aos ajustes.\nReconecte o USB e tente de novo.",
                ),
            )
            return
        local = self._save_local_calib()
        # Não envia stick save: no firmware atual o NVS do RP2040 trava a placa.
        if local:
            self.after(
                0,
                lambda: (
                    self.status.set("Salvo no PC e aplicado"),
                    messagebox.showinfo(
                        "Salvo",
                        "Ajustes aplicados no mouse e guardados neste PC.\n\n"
                        "Depois de desligar o USB: abra o tuner e Conectar — "
                        "ele reaplica sozinho.\n"
                        f"Arquivo: {CALIB_PATH}",
                    ),
                ),
            )
        else:
            self.after(
                0,
                lambda: (
                    self.status.set("Aplicado (não gravou no PC)"),
                    messagebox.showwarning(
                        "Aplicado",
                        "Os ajustes já valem no mouse, mas não deu para gravar o arquivo no PC.",
                    ),
                ),
            )

    def read_from_stick(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Serial", "Conecte o joystick primeiro.")
            return
        self._run_bg(self._job_read)

    def capture_center(self):
        with self.lock:
            xs = list(self._mv_hist.get(0, []))
            ys = list(self._mv_hist.get(1, []))
        if not xs or not ys:
            # Usa último valor da UI se o hist ainda está vazio
            try:
                xs = [int(self.live_x.get())]
                ys = [int(self.live_y.get())]
            except ValueError:
                messagebox.showinfo(
                    "Centro",
                    "Conecte, espere o X/Y aparecer, deixe o stick parado e tente de novo.",
                )
                return
        mx = int(sum(xs) / len(xs))
        my = int(sum(ys) / len(ys))
        self.mid_x.set(mx)
        self.mid_y.set(my)
        self.log_line(f"centro capturado mid_x={mx} mid_y={my}")
        self.status.set(f"Centro: X={mx} Y={my}")

    def save_settings(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Serial", "Conecte o joystick primeiro.")
            return
        self._run_bg(self._job_save)

    def on_close(self):
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.disconnect()
        self.destroy()


def main():
    lock = acquire_single_instance()
    if lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "Já está aberto",
            "O PSP Stick Tuner já está rodando.\n"
            "Feche a outra janela (ou finalize python.exe) e abra de novo.\n\n"
            "Várias cópias travam a COM no Windows.",
        )
        root.destroy()
        return
    StickTunerApp().mainloop()


if __name__ == "__main__":
    main()
