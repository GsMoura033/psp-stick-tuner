#!/usr/bin/env python3
"""PSP Stick Tuner — calibração ao vivo do joystick ZMK via USB CDC."""

from __future__ import annotations

import re
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit("Instale as dependências: pip install -r requirements.txt")

AIN_RE = re.compile(r"AIN(\d+)\s+raw:\s*(-?\d+)\s+mv:\s*(-?\d+)")
STICK_RE = re.compile(r"STICK\s+(OK|ERR)\b(.*)$")
BAUD = 115200


def list_serial_ports():
    ports = []
    for p in list_ports.comports():
        label = f"{p.device} — {p.description}"
        if p.serial_number:
            label += f" [{p.serial_number}]"
        ports.append((p.device, label, (p.description or "") + " " + (p.manufacturer or "")))
    return ports


class StickTunerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PSP Stick Tuner")
        self.geometry("520x560")
        self.minsize(480, 520)

        self.ser: serial.Serial | None = None
        self.reader_stop = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.lock = threading.Lock()

        self.live_x = tk.StringVar(value="—")
        self.live_y = tk.StringVar(value="—")
        self.status = tk.StringVar(value="Desconectado")
        self.port_var = tk.StringVar()

        self.mid_x = tk.IntVar(value=8880)
        self.mid_y = tk.IntVar(value=9400)
        self.deadzone = tk.IntVar(value=200)
        self.divisor = tk.IntVar(value=500)
        self.invert_y = tk.BooleanVar(value=False)

        self._mv_hist = {0: deque(maxlen=40), 1: deque(maxlen=40)}
        self._last_reply = ""

        self._build_ui()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        conn = ttk.LabelFrame(root, text="Conexão", padding=10)
        conn.pack(fill=tk.X, **pad)
        row = ttk.Frame(conn)
        row.pack(fill=tk.X)
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var, state="readonly", width=48)
        self.port_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Atualizar", command=self.refresh_ports).pack(side=tk.LEFT, padx=(6, 0))
        btns = ttk.Frame(conn)
        btns.pack(fill=tk.X, pady=(8, 0))
        self.btn_connect = ttk.Button(btns, text="Conectar", command=self.toggle_connect)
        self.btn_connect.pack(side=tk.LEFT)
        ttk.Label(btns, textvariable=self.status).pack(side=tk.LEFT, padx=10)

        live = ttk.LabelFrame(root, text="Leitura ao vivo (mV)", padding=10)
        live.pack(fill=tk.X, **pad)
        ttk.Label(live, text="X (AIN0):").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(live, textvariable=self.live_x, font=("Segoe UI", 14, "bold")).grid(
            row=0, column=1, sticky=tk.W, padx=8
        )
        ttk.Label(live, text="Y (AIN1):").grid(row=1, column=0, sticky=tk.W)
        ttk.Label(live, textvariable=self.live_y, font=("Segoe UI", 14, "bold")).grid(
            row=1, column=1, sticky=tk.W, padx=8
        )
        ttk.Button(live, text="Capturar centro", command=self.capture_center).grid(
            row=0, column=2, rowspan=2, padx=12
        )

        cal = ttk.LabelFrame(root, text="Calibração", padding=10)
        cal.pack(fill=tk.X, **pad)

        def add_scale(parent, row, label, var, from_, to, tick=None):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
            scale = ttk.Scale(
                parent,
                from_=from_,
                to=to,
                orient=tk.HORIZONTAL,
                command=lambda v, variable=var: variable.set(int(float(v))),
            )
            scale.set(var.get())
            scale.grid(row=row, column=1, sticky=tk.EW, padx=8)
            val = ttk.Label(parent, textvariable=var, width=6)
            val.grid(row=row, column=2, sticky=tk.E)
            return scale

        cal.columnconfigure(1, weight=1)
        self.scale_mid_x = add_scale(cal, 0, "Centro X", self.mid_x, 0, 12000)
        self.scale_mid_y = add_scale(cal, 1, "Centro Y", self.mid_y, 0, 12000)
        self.scale_dz = add_scale(cal, 2, "Deadzone", self.deadzone, 0, 255)
        self.scale_div = add_scale(cal, 3, "Velocidade (divisor)", self.divisor, 50, 1000)
        ttk.Label(cal, text="(maior = mais lento)").grid(row=4, column=1, sticky=tk.W)
        ttk.Checkbutton(cal, text="Inverter eixo Y", variable=self.invert_y).grid(
            row=5, column=0, columnspan=2, sticky=tk.W, pady=6
        )

        actions = ttk.Frame(root)
        actions.pack(fill=tk.X, **pad)
        ttk.Button(actions, text="Ler do stick", command=self.read_from_stick).pack(side=tk.LEFT)
        ttk.Button(actions, text="Aplicar", command=self.apply_settings).pack(side=tk.LEFT, padx=8)

        log_frame = ttk.LabelFrame(root, text="Log", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self.log = tk.Text(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(self.log, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)

        tip = ttk.Label(
            root,
            text="Dica: use a porta do PSP Stick, não a Sofle. Firmware precisa dos comandos stick.",
            foreground="#555",
        )
        tip.pack(fill=tk.X, padx=10, pady=(0, 4))

        # keep scales in sync when IntVar changes programmatically
        for var, scale in (
            (self.mid_x, self.scale_mid_x),
            (self.mid_y, self.scale_mid_y),
            (self.deadzone, self.scale_dz),
            (self.divisor, self.scale_div),
        ):
            var.trace_add("write", lambda *_a, v=var, s=scale: s.set(v.get()))

    def log_line(self, text: str):
        def _append():
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, text + "\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        self.after(0, _append)

    def refresh_ports(self):
        ports = list_serial_ports()
        labels = [label for _, label, _ in ports]
        self._port_map = {label: dev for dev, label, _ in ports}
        self.port_combo["values"] = labels
        # Prefer PSP Stick
        preferred = next((lbl for _, lbl, meta in ports if "PSP" in meta or "PSP" in lbl), None)
        if preferred:
            self.port_var.set(preferred)
        elif labels and not self.port_var.get():
            self.port_var.set(labels[0])

    def toggle_connect(self):
        if self.ser and self.ser.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        label = self.port_var.get()
        if not label or label not in getattr(self, "_port_map", {}):
            messagebox.showwarning("Porta", "Escolha uma porta serial.")
            return
        dev = self._port_map[label]
        try:
            self.ser = serial.Serial(dev, BAUD, timeout=0.05)
            time.sleep(0.3)
            self.ser.reset_input_buffer()
        except serial.SerialException as exc:
            messagebox.showerror("Erro", f"Não foi possível abrir {dev}:\n{exc}")
            self.ser = None
            return

        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.btn_connect.configure(text="Desconectar")
        self.status.set(f"Conectado em {dev}")
        self.log_line(f"== conectado {dev} ==")
        self.after(500, self.read_from_stick)

    def disconnect(self):
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
                self.after(0, lambda: self.status.set("Porta perdida"))
                break
            except Exception as exc:
                self.log_line(f"reader: {exc}")
                time.sleep(0.05)

    def _handle_line(self, line: str):
        if not line:
            return
        m = AIN_RE.search(line)
        if m:
            idx = int(m.group(1))
            mv = int(m.group(3))
            with self.lock:
                self._mv_hist.setdefault(idx, deque(maxlen=40)).append(mv)
            if idx == 0:
                self.after(0, lambda: self.live_x.set(str(mv)))
            elif idx == 1:
                self.after(0, lambda: self.live_y.set(str(mv)))
            return

        sm = STICK_RE.search(line)
        if sm:
            self._last_reply = line
            self.log_line(line)
            if sm.group(1) == "OK":
                self._parse_stick_ok(sm.group(2))
            return

    def _parse_stick_ok(self, rest: str):
        def grab(name):
            m = re.search(rf"{name}=(-?\d+)", rest)
            return int(m.group(1)) if m else None

        mx, my = grab("mid_x"), grab("mid_y")
        dz, div = grab("deadzone"), grab("divisor")
        inv = grab("invert_y")
        if mx is not None:
            self.mid_x.set(mx)
        if my is not None:
            self.mid_y.set(my)
        if dz is not None:
            self.deadzone.set(dz)
        if div is not None:
            self.divisor.set(div)
        if inv is not None:
            self.invert_y.set(bool(inv))

    def send_cmd(self, cmd: str, wait_ok: bool = True) -> bool:
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Serial", "Conecte primeiro.")
            return False
        self._last_reply = ""
        payload = (cmd.strip() + "\n").encode("utf-8")
        self.log_line(f"> {cmd}")
        try:
            self.ser.write(payload)
            self.ser.flush()
        except serial.SerialException as exc:
            messagebox.showerror("Serial", str(exc))
            return False
        if not wait_ok:
            return True
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self._last_reply.startswith("STICK OK"):
                return True
            if self._last_reply.startswith("STICK ERR"):
                return False
            time.sleep(0.05)
            self.update_idletasks()
        self.log_line("! timeout esperando STICK OK (firmware sem shell?)")
        return False

    def read_from_stick(self):
        self.send_cmd("stick get")

    def capture_center(self):
        with self.lock:
            xs = list(self._mv_hist.get(0, []))
            ys = list(self._mv_hist.get(1, []))
        if len(xs) < 5 or len(ys) < 5:
            messagebox.showinfo(
                "Capturar centro",
                "Aguarde alguns segundos conectado para acumular leituras X/Y.",
            )
            return
        mx = int(sum(xs) / len(xs))
        my = int(sum(ys) / len(ys))
        self.mid_x.set(mx)
        self.mid_y.set(my)
        self.log_line(f"centro capturado mid_x={mx} mid_y={my}")

    def apply_settings(self):
        ok = True
        ok &= self.send_cmd(f"stick mid {self.mid_x.get()} {self.mid_y.get()}")
        ok &= self.send_cmd(f"stick deadzone {self.deadzone.get()}")
        ok &= self.send_cmd(f"stick divisor {self.divisor.get()}")
        ok &= self.send_cmd(f"stick invert_y {1 if self.invert_y.get() else 0}")
        if ok:
            self.status.set("Calibração aplicada (RAM)")
            messagebox.showinfo(
                "Aplicado",
                "Valores enviados ao stick.\n"
                "Valem até reiniciar (ainda não grava na flash).",
            )
        else:
            messagebox.showwarning(
                "Falha",
                "Não recebi STICK OK.\n"
                "Confirme que flashou o firmware com shell do tuner.",
            )

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    StickTunerApp().mainloop()
