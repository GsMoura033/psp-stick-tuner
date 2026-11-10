#!/usr/bin/env python3
"""PSP Stick Tuner — calibração amigável do joystick ZMK via USB."""

from __future__ import annotations

import re
import sys
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
# VID/PID padrão ZMK (OpenMoko)
ZMK_VID = 0x1D50
ZMK_PID = 0x615E


def list_candidate_ports():
    """Lista portas; marca as que parecem ZMK/PSP Stick."""
    ports = []
    for p in list_ports.comports():
        meta = " ".join(
            filter(
                None,
                [
                    p.description or "",
                    p.manufacturer or "",
                    p.product or "",
                    p.hwid or "",
                ],
            )
        )
        is_zmk = (p.vid == ZMK_VID and p.pid == ZMK_PID) or "1D50" in (p.hwid or "").upper()
        is_psp = "PSP" in meta.upper() or "STICK" in meta.upper()
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


class StickTunerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PSP Stick Tuner")
        self.geometry("580x700")
        self.minsize(520, 640)

        self.ser: serial.Serial | None = None
        self.reader_stop = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.lock = threading.Lock()

        self.live_x = tk.StringVar(value="—")
        self.live_y = tk.StringVar(value="—")
        self.status = tk.StringVar(value="Desconectado — ligue o stick no USB")
        self.port_var = tk.StringVar()

        self.mid_x = tk.IntVar(value=8880)
        self.mid_y = tk.IntVar(value=9400)
        self.deadzone = tk.IntVar(value=200)
        self.divisor = tk.IntVar(value=500)
        self.invert_y = tk.BooleanVar(value=False)

        self._mv_hist = {0: deque(maxlen=40), 1: deque(maxlen=40)}
        self._last_reply = ""
        self._port_map: dict[str, str] = {}

        self._build_ui()
        self.refresh_ports(select_best=True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(root, text="PSP Stick Tuner", font=("Segoe UI", 16, "bold"))
        title.pack(anchor=tk.W)
        ttk.Label(
            root,
            text="Calibre o joystick USB. Quanto mais o stick inclina, mais o cursor anda — "
            "aqui você define o centro, a zona morta e a velocidade máxima.",
            wraplength=540,
        ).pack(anchor=tk.W, pady=(2, 10))

        # Passo 1
        step1 = ttk.LabelFrame(root, text="Passo 1 — Conectar o joystick", padding=10)
        step1.pack(fill=tk.X, **pad)
        ttk.Label(
            step1,
            text="1) Ligue o joystick na placa (VCC no 3,3 V, não no 5 V)\n"
            "2) Conecte a placa no USB\n"
            "3) Clique em “Detectar joystick” (ou escolha a porta e Conectar)",
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        row = ttk.Frame(step1)
        row.pack(fill=tk.X, pady=(8, 0))
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var, state="readonly", width=52)
        self.port_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Atualizar lista", command=lambda: self.refresh_ports(True)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        btns = ttk.Frame(step1)
        btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btns, text="Detectar joystick", command=self.detect_stick).pack(side=tk.LEFT)
        self.btn_connect = ttk.Button(btns, text="Conectar", command=self.toggle_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=8)
        ttk.Label(btns, textvariable=self.status, wraplength=320).pack(side=tk.LEFT)

        # Passo 2
        step2 = ttk.LabelFrame(root, text="Passo 2 — Centro (stick parado)", padding=10)
        step2.pack(fill=tk.X, **pad)
        ttk.Label(
            step2,
            text="Deixe o stick solto no centro. Os números abaixo são a leitura atual. "
            "Clique em “Usar como centro” para zerar o drift.",
            wraplength=520,
        ).pack(anchor=tk.W)
        live = ttk.Frame(step2)
        live.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(live, text="X:").pack(side=tk.LEFT)
        ttk.Label(live, textvariable=self.live_x, font=("Segoe UI", 13, "bold")).pack(
            side=tk.LEFT, padx=(4, 16)
        )
        ttk.Label(live, text="Y:").pack(side=tk.LEFT)
        ttk.Label(live, textvariable=self.live_y, font=("Segoe UI", 13, "bold")).pack(
            side=tk.LEFT, padx=(4, 16)
        )
        ttk.Button(live, text="Usar como centro", command=self.capture_center).pack(side=tk.LEFT)

        # Passo 3
        step3 = ttk.LabelFrame(root, text="Passo 3 — Ajustes finos", padding=10)
        step3.pack(fill=tk.X, **pad)
        step3.columnconfigure(1, weight=1)

        def add_scale(row, label, help_text, var, from_, to):
            ttk.Label(step3, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
            scale = ttk.Scale(
                step3,
                from_=from_,
                to=to,
                orient=tk.HORIZONTAL,
                command=lambda v, variable=var: variable.set(int(float(v))),
            )
            scale.set(var.get())
            scale.grid(row=row, column=1, sticky=tk.EW, padx=8)
            ttk.Label(step3, textvariable=var, width=6).grid(row=row, column=2, sticky=tk.E)
            ttk.Label(step3, text=help_text, foreground="#555").grid(
                row=row + 1, column=1, sticky=tk.W, pady=(0, 4)
            )
            var.trace_add("write", lambda *_a, v=var, s=scale: s.set(v.get()))
            return scale

        add_scale(0, "Centro X", "Ajuste fino se ainda puxar para os lados", self.mid_x, 0, 12000)
        add_scale(2, "Centro Y", "Ajuste fino se ainda puxar para cima/baixo", self.mid_y, 0, 12000)
        add_scale(
            4,
            "Zona morta",
            "Maior = menos drift parado; alto demais deixa o stick “morto” no meio",
            self.deadzone,
            0,
            255,
        )
        add_scale(
            6,
            "Velocidade",
            "Número MAIOR = cursor MAIS LENTO (mesmo no máximo de inclinação)",
            self.divisor,
            50,
            1000,
        )
        ttk.Checkbutton(
            step3,
            text="Inverter cima/baixo",
            variable=self.invert_y,
        ).grid(row=8, column=0, columnspan=2, sticky=tk.W, pady=6)

        # Passo 4
        step4 = ttk.LabelFrame(root, text="Passo 4 — Salvar na placa", padding=10)
        step4.pack(fill=tk.X, **pad)
        ttk.Label(
            step4,
            text="Salvar envia os ajustes e grava na memória da placa "
            "(continua depois de desligar o USB).",
            wraplength=520,
        ).pack(anchor=tk.W)
        actions = ttk.Frame(step4)
        actions.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(actions, text="Ler da placa", command=self.read_from_stick).pack(side=tk.LEFT)
        ttk.Button(actions, text="Salvar", command=self.save_settings).pack(side=tk.LEFT, padx=8)

        log_frame = ttk.LabelFrame(root, text="Log", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self.log = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True)

    def log_line(self, text: str):
        def _append():
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, text + "\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        self.after(0, _append)

    def refresh_ports(self, select_best: bool = False):
        ports = list_candidate_ports()
        labels = [p["label"] for p in ports]
        self._port_map = {p["label"]: p["device"] for p in ports}
        self.port_combo["values"] = labels
        if not labels:
            self.port_var.set("")
            self.status.set("Nenhuma porta serial encontrada. Conecte o stick no USB.")
            return
        if select_best or self.port_var.get() not in self._port_map:
            preferred = next((p for p in ports if p["is_psp"]), None)
            if not preferred:
                preferred = next((p for p in ports if p["is_zmk"]), ports[0])
            self.port_var.set(preferred["label"])

    def toggle_connect(self):
        if self.ser and self.ser.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self, device: str | None = None):
        if device is None:
            label = self.port_var.get()
            if not label or label not in self._port_map:
                messagebox.showwarning("Porta", "Escolha uma porta serial na lista.")
                return False
            device = self._port_map[label]
        try:
            self.ser = serial.Serial(device, BAUD, timeout=0.05)
            time.sleep(0.35)
            self.ser.reset_input_buffer()
        except serial.SerialException as exc:
            messagebox.showerror("Erro", f"Não foi possível abrir {device}:\n{exc}")
            self.ser = None
            return False

        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.btn_connect.configure(text="Desconectar")
        self.status.set(f"Conectado em {device}")
        self.log_line(f"== conectado {device} ==")
        self.after(400, self.read_from_stick)
        return True

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

    def detect_stick(self):
        """Abre portas ZMK e procura resposta a 'stick get'."""
        if self.ser and self.ser.is_open:
            self.disconnect()
        self.refresh_ports(select_best=True)
        ports = list_candidate_ports()
        candidates = [p for p in ports if p["is_zmk"] or p["is_psp"]] or ports
        self.status.set("Procurando joystick…")
        self.update_idletasks()

        for p in candidates:
            self.log_line(f"testando {p['device']}…")
            try:
                ser = serial.Serial(p["device"], BAUD, timeout=0.2)
            except serial.SerialException:
                continue
            try:
                time.sleep(0.25)
                ser.reset_input_buffer()
                ser.write(b"stick get\n")
                ser.flush()
                deadline = time.time() + 1.5
                buf = ""
                found = False
                while time.time() < deadline:
                    chunk = ser.read(256)
                    if chunk:
                        buf += chunk.decode("utf-8", errors="ignore")
                        if "STICK OK" in buf:
                            found = True
                            break
                    time.sleep(0.05)
            finally:
                ser.close()

            if found:
                # select in combo
                for label, dev in self._port_map.items():
                    if dev == p["device"]:
                        self.port_var.set(label)
                        break
                self.log_line(f"joystick encontrado em {p['device']}")
                self.connect(p["device"])
                messagebox.showinfo("Detectado", f"Joystick encontrado na porta {p['device']}.")
                return

        self.status.set("Não achei o joystick")
        messagebox.showwarning(
            "Não encontrado",
            "Não recebi resposta do joystick.\n\n"
            "Confira:\n"
            "• A placa está no USB?\n"
            "• O firmware tem suporte ao tuner (comandos stick)?\n"
            "• Feche outros programas usando a porta COM (PuTTY, etc.).",
        )

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
            messagebox.showwarning("Serial", "Conecte o joystick primeiro (Passo 1).")
            return False
        self._last_reply = ""
        self.log_line(f"> {cmd}")
        try:
            self.ser.write((cmd.strip() + "\n").encode("utf-8"))
            self.ser.flush()
        except serial.SerialException as exc:
            messagebox.showerror("Serial", str(exc))
            return False
        if not wait_ok:
            return True
        deadline = time.time() + 2.5
        while time.time() < deadline:
            if self._last_reply.startswith("STICK OK"):
                return True
            if self._last_reply.startswith("STICK ERR"):
                return False
            time.sleep(0.05)
            self.update_idletasks()
        self.log_line("! timeout — a placa não respondeu ao comando stick")
        return False

    def read_from_stick(self):
        if self.send_cmd("stick get"):
            self.status.set("Valores lidos da placa")

    def capture_center(self):
        with self.lock:
            xs = list(self._mv_hist.get(0, []))
            ys = list(self._mv_hist.get(1, []))
        if len(xs) < 5 or len(ys) < 5:
            messagebox.showinfo(
                "Centro",
                "Aguarde 1–2 segundos conectado com o stick parado "
                "para acumular leituras X/Y.",
            )
            return
        mx = int(sum(xs) / len(xs))
        my = int(sum(ys) / len(ys))
        self.mid_x.set(mx)
        self.mid_y.set(my)
        self.log_line(f"centro capturado mid_x={mx} mid_y={my}")
        self.status.set(f"Centro definido: X={mx} Y={my}")

    def save_settings(self):
        """Aplica na hora e grava na flash (stick save)."""
        ok = True
        ok &= self.send_cmd(f"stick mid {self.mid_x.get()} {self.mid_y.get()}")
        ok &= self.send_cmd(f"stick deadzone {self.deadzone.get()}")
        ok &= self.send_cmd(f"stick divisor {self.divisor.get()}")
        ok &= self.send_cmd(f"stick invert_y {1 if self.invert_y.get() else 0}")
        ok &= self.send_cmd("stick save")
        if ok:
            self.status.set("Salvo na placa")
            messagebox.showinfo(
                "Salvo",
                "Calibração gravada na placa.\n"
                "Pode desconectar o USB — os valores permanecem.",
            )
        else:
            messagebox.showwarning(
                "Falha ao salvar",
                "A placa não confirmou o comando.\n\n"
                "Use o firmware mais recente do projeto zmk-analog-rp2040 "
                "(com shell do tuner) e feche outros apps na mesma porta COM.",
            )

    def on_close(self):
        self.disconnect()
        self.destroy()


def main():
    app = StickTunerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
