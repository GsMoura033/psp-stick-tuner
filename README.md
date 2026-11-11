# PSP Stick Tuner

Calibre o joystick USB (firmware ZMK) sem terminal e sem Python.

Feito por **[Gustavo Moura](https://gsmoura.com.br)**  

Firmware companion: [zmk-analog-rp2040](https://github.com/GsMoura033/zmk-analog-rp2040)

[![Build Windows exe](https://github.com/GsMoura033/psp-stick-tuner/actions/workflows/build-exe.yml/badge.svg)](https://github.com/GsMoura033/psp-stick-tuner/actions/workflows/build-exe.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-lime.svg)](LICENSE)

## Compatibilidade

| O quê | Windows 10/11 | Windows XP | Linux |
|---|---|---|---|
| **Stick como mouse USB** (depois de calibrado) | Sim | Sim | Sim |
| **`PSP-Stick-Tuner.exe`** | Sim | Não | Não (é binário Windows) |
| **Tuner pelo código** (`python app.py`) | Sim | Não | Sim (com Python 3 + pyserial) |

A calibração fica **na flash da placa**, não no PC. Calibre uma vez → use em qualquer máquina.

- **XP:** use o stick como mouse; **não** rode o `.exe` lá. Calibre no Win10/11 e pluggue no XP.
- **Linux:** o `.exe` **não** roda nativamente (Wine pode falhar). No Linux use `python app.py`, ou calibre no Windows e só use o stick.

## Baixar o executável

1. Abra **[Releases](https://github.com/GsMoura033/psp-stick-tuner/releases)**
2. Baixe `PSP-Stick-Tuner.exe`
3. Rode no **Windows 10/11** (64-bit recomendado). Não precisa instalar Python.

Também dá para pegar o artefato do Actions (**PSP-Stick-Tuner-windows**) em qualquer build verde da `main`.

## Uso rápido

1. Flash o firmware do stick ([zmk-analog-rp2040 Actions](https://github.com/GsMoura033/zmk-analog-rp2040/actions))
2. Ligue o módulo: **VCC → 3,3 V** (nunca 5 V no ADC), GND, VRx/VRy nos ADCs
3. USB na placa → abra o tuner (**Win10/11**)
4. **Detectar joystick** (escolhe a COM que responde — não depende de COM fixa)
5. Stick parado → **Usar como centro**
6. Ajuste zona morta / velocidade / inverter eixos → **Salvar**

> **Salvar** grava na **memória da placa**. Depois o stick funciona em qualquer PC/SO como mouse USB.

### Controles

| Controle | Função |
|---|---|
| **Detectar** | Testa as COMs ZMK e conecta na que responde ao `stick` |
| **Usar como centro** | Define o zero com o stick parado |
| **Zona morta** | Maior = menos tremor parado |
| **Velocidade** | **Maior = mais lento** no máximo de inclinação |
| **Inverter X / Y** | Espelha esquerda-direita ou cima-baixo |
| **Salvar** | Aplica e grava na memória da placa |

## Rodar do código

**Windows (PowerShell):**

```powershell
cd psp-stick-tuner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

**Linux:**

```bash
cd psp-stick-tuner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

O usuário do dialout/`uucp` precisa acessar a porta serial (`/dev/ttyACM*` etc.).

### Gerar o `.exe` localmente

```powershell
pip install pyinstaller pillow
pyinstaller --noconfirm --onefile --windowed --name PSP-Stick-Tuner --icon assets/icon.ico app.py
```

Saída: `dist\PSP-Stick-Tuner.exe`

## Protocolo serial

Baud **115200**. Comandos:

```
stick get
stick live
stick mid <x> <y>
stick deadzone <n>
stick divisor <n>
stick invert_x <0|1>
stick invert_y <0|1>
stick save
```

Respostas: `STICK OK ...` / `STICK ERR ...`

## Licença

MIT © 2026 Gustavo Moura — veja [LICENSE](LICENSE).

---

Sites, sistemas e automações: **[gsmoura.com.br](https://gsmoura.com.br)**
