#!/usr/bin/env python3
"""PSP Stick Tuner — calibração amigável do joystick ZMK via USB."""

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
ZMK_VID = 0x1D50
ZMK_PID = 0x615E


def open_serial(device: str, timeout: float = 0.05) -> serial.Serial:
    """Abre a COM sem pulsar DTR (evita reset do RP2040 no Windows)."""
    ser = serial.Serial()
    ser.port = device
    ser.baudrate = BAUD
    ser.timeout = timeout
    ser.write_timeout = 2
    ser.dsrdtr = False
    ser.rtscts = False
    # Definir antes do open para não resetar a placa
    ser.dtr = False
    ser.rts = False
    ser.open()
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass
    time.sleep(0.15)
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


def probe_stick(device: str, wait_s: float = 3.0) -> bool:
    """Envia stick get e espera STICK OK (com retentativas)."""
    try:
        ser = open_serial(device, timeout=0.2)
    except serial.SerialException:
        return False
    try:
        deadline = time.time() + wait_s
        buf = ""
        next_send = 0.0
        while time.time() < deadline:
            now = time.time()
            if now >= next_send:
                try:
                    ser.write(b"stick get\n")
                    ser.flush()
                except serial.SerialException:
                    return False
                next_send = now + 0.6
            chunk = ser.read(512)
            if chunk:
                buf += chunk.decode("utf-8", errors="ignore")
                if "STICK OK" in buf:
                    return True
                if "STICK ERR" in buf and "no device" not in buf.lower():
                    # firmware respondeu ao protocolo
                    return True
            time.sleep(0.05)
        return False
    finally:
        try:
            ser.close()
        except Exception:
            pass


class StickTunerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PSP Stick Tuner")
        # Cabe na maioria das telas; o conteúdo rola por dentro
        self.geometry("560x640")
        self.minsize(480, 480)

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
        # Barra fixa no topo com Salvar sempre visível
        top = ttk.Frame(self, padding=(12, 10, 12, 6))
        top.pack(fill=tk.X)
        ttk.Label(top, text="PSP Stick Tuner", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="Salvar na placa", command=self.save_settings).pack(side=tk.RIGHT)
        ttk.Button(top, text="Ler da placa", command=self.read_from_stick).pack(side=tk.RIGHT, padx=(0, 8))

        ttk.Label(
            self,
            textvariable=self.status,
            padding=(12, 0),
            foreground="#333",
        ).pack(fill=tk.X)

        # Área com scroll
        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        canvas = tk.Canvas(container, highlightthickness=0)
        scroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        self.scroll_body = ttk.Frame(canvas)

        self.scroll_body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._canvas_window = canvas.create_window((0, 0), window=self.scroll_body, anchor=tk.NW)

        def _on_canvas_configure(event):
            canvas.itemconfigure(self._canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", _wheel)
        self._canvas = canvas

        body = self.scroll_body
        pad = {"padx": 8, "pady": 5}

        ttk.Label(
            body,
            text="Calibre o joystick USB. Inclinação = velocidade. "
            "Aqui você define centro, zona morta e velocidade máxima.",
            wraplength=500,
        ).pack(anchor=tk.W, padx=8, pady=(4, 8))

        # Passo 1
        step1 = ttk.LabelFrame(body, text="1) Conectar", padding=8)
        step1.pack(fill=tk.X, **pad)
        ttk.Label(
            step1,
            text="Joystick na placa com VCC em 3,3 V → USB no PC → Detectar (ou Conectar).",
            wraplength=500,
        ).pack(anchor=tk.W)
        row = ttk.Frame(step1)
        row.pack(fill=tk.X, pady=(6, 0))
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

        # Passo 2
        step2 = ttk.LabelFrame(body, text="2) Centro (stick parado)", padding=8)
        step2.pack(fill=tk.X, **pad)
        live = ttk.Frame(step2)
        live.pack(fill=tk.X)
        ttk.Label(live, text="X:").pack(side=tk.LEFT)
        ttk.Label(live, textvariable=self.live_x, font=("Segoe UI", 12, "bold")).pack(
            side=tk.LEFT, padx=(2, 12)
        )
        ttk.Label(live, text="Y:").pack(side=tk.LEFT)
        ttk.Label(live, textvariable=self.live_y, font=("Segoe UI", 12, "bold")).pack(
            side=tk.LEFT, padx=(2, 12)
        )
        ttk.Button(live, text="Usar como centro", command=self.capture_center).pack(side=tk.LEFT)

        # Passo 3
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
            ttk.Label(step3, text=help_text, foreground="#555", font=("Segoe UI", 8)).grid(
                row=row_i + 1, column=1, sticky=tk.W
            )
            var.trace_add("write", lambda *_a, v=var, s=scale: s.set(v.get()))

        add_scale(0, "Centro X", "Se driftar para os lados", self.mid_x, 0, 12000)
        add_scale(2, "Centro Y", "Se driftar para cima/baixo", self.mid_y, 0, 12000)
        add_scale(4, "Zona morta", "Maior = menos tremor parado", self.deadzone, 0, 255)
        add_scale(
            6,
            "Velocidade",
            "MAIOR = mais lento (vale no máximo de inclinação)",
            self.divisor,
            50,
            1000,
        )
        ttk.Checkbutton(step3, text="Inverter cima/baixo", variable=self.invert_y).grid(
            row=8, column=0, columnspan=2, sticky=tk.W, pady=4
        )

        ttk.Label(
            body,
            text="Use o botão “Salvar na placa” no topo para gravar. "
            "Os valores ficam guardados depois de desligar o USB.",
            wraplength=500,
            foreground="#333",
        ).pack(anchor=tk.W, padx=8, pady=(4, 6))

        log_frame = ttk.LabelFrame(body, text="Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self.log = tk.Text(log_frame, height=6, wrap=tk.WORD, state=tk.DISABLED)
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
            self.status.set("Nenhuma porta serial — conecte o stick no USB")
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

    def connect(self, device: str | None = None) -> bool:
        if device is None:
            label = self.port_var.get()
            if not label or label not in self._port_map:
                messagebox.showwarning("Porta", "Escolha uma porta serial na lista.")
                return False
            device = self._port_map[label]
        try:
            self.ser = open_serial(device)
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
        self.after(500, self.read_from_stick)
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
        if self.ser and self.ser.is_open:
            self.disconnect()
        self.refresh_ports(select_best=True)
        ports = list_candidate_ports()
        candidates = [p for p in ports if p["is_zmk"] or p["is_psp"]] or ports
        self.status.set("Procurando joystick… aguarde alguns segundos")
        self.update_idletasks()
        self.log_line("detectar: iniciando (sem reset DTR)")

        for p in candidates:
            self.log_line(f"testando {p['device']}…")
            self.update_idletasks()
            if probe_stick(p["device"], wait_s=4.0):
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
            "Não recebi resposta do comando stick.\n\n"
            "Checklist:\n"
            "• Flashou o firmware mais recente (com tuner/shell)?\n"
            "• A placa está no USB e aparece como dispositivo serial?\n"
            "• Feche PuTTY ou outro app usando a mesma COM\n"
            "• Se a lista mostra a porta certa, tente Conectar manualmente",
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
            messagebox.showwarning("Serial", "Conecte o joystick primeiro.")
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
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self._last_reply.startswith("STICK OK"):
                return True
            if self._last_reply.startswith("STICK ERR"):
                return False
            time.sleep(0.05)
            self.update_idletasks()
        self.log_line("! timeout — placa não respondeu (firmware com shell?)")
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
                "Conecte, espere 1–2 s com o stick parado, e tente de novo.",
            )
            return
        mx = int(sum(xs) / len(xs))
        my = int(sum(ys) / len(ys))
        self.mid_x.set(mx)
        self.mid_y.set(my)
        self.log_line(f"centro capturado mid_x={mx} mid_y={my}")
        self.status.set(f"Centro: X={mx} Y={my}")

    def save_settings(self):
        applied = True
        applied &= self.send_cmd(f"stick mid {self.mid_x.get()} {self.mid_y.get()}")
        applied &= self.send_cmd(f"stick deadzone {self.deadzone.get()}")
        applied &= self.send_cmd(f"stick divisor {self.divisor.get()}")
        applied &= self.send_cmd(f"stick invert_y {1 if self.invert_y.get() else 0}")
        if not applied:
            messagebox.showwarning(
                "Falha",
                "A placa não aceitou os ajustes.\n\n"
                "Confirme a conexão e o firmware com comandos stick.",
            )
            return

        # Persistência na flash (firmware recente com "stick save")
        saved = self.send_cmd("stick save")
        if saved:
            self.status.set("Salvo na placa (flash)")
            messagebox.showinfo(
                "Salvo",
                "Calibração gravada na memória da placa.\n"
                "Pode desconectar o USB — os valores permanecem.",
            )
            return

        self.status.set("Aplicado agora (ainda não gravado na flash)")
        messagebox.showwarning(
            "Aplicado, mas não gravado",
            "Os ajustes JÁ ESTÃO valendo agora no mouse.\n\n"
            "Porém a placa não gravou na flash (comando stick save).\n"
            "Flash o firmware mais recente do repositório zmk-analog-rp2040 "
            "e clique em Salvar de novo para manter depois de desligar.",
        )

    def on_close(self):
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.disconnect()
        self.destroy()


def main():
    StickTunerApp().mainloop()


if __name__ == "__main__":
    main()
