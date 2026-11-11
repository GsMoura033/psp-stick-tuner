# PSP Stick Tuner

Calibre o joystick USB (firmware ZMK) sem terminal e sem Python.

Feito por **[Gustavo Moura](https://gsmoura.com.br)**  

Firmware companion: [zmk-analog-rp2040](https://github.com/GsMoura033/zmk-analog-rp2040)

[![Build Windows exe](https://github.com/GsMoura033/psp-stick-tuner/actions/workflows/build-exe.yml/badge.svg)](https://github.com/GsMoura033/psp-stick-tuner/actions/workflows/build-exe.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-lime.svg)](LICENSE)

## Baixar o executável

1. Abra **[Releases](https://github.com/GsMoura033/psp-stick-tuner/releases)**
2. Baixe `PSP-Stick-Tuner.exe`
3. Rode (Windows). Não precisa instalar Python.

Também dá para pegar o artefato do Actions (**PSP-Stick-Tuner-windows**) em qualquer build verde da `main`.

## Uso rápido

1. Flash o firmware do stick ([zmk-analog-rp2040 Actions](https://github.com/GsMoura033/zmk-analog-rp2040/actions))
2. Ligue o módulo: **VCC → 3,3 V** (nunca 5 V no ADC), GND, VRx/VRy nos ADCs
3. USB na placa → abra o tuner
4. **Detectar joystick** (escolhe a COM que responde — não depende de COM fixa)
5. Stick parado → **Usar como centro**
6. Ajuste zona morta / velocidade / inverter eixos → **Salvar**

> **Salvar** aplica na placa **e** guarda a calibração neste PC.  
> No próximo **Conectar**, o app reaplica sozinho (a flash NVS do RP2040 pode travar — por isso a persistência é no host).

### Controles

| Controle | Função |
|---|---|
| **Detectar** | Testa as COMs ZMK e conecta na que responde ao `stick` |
| **Usar como centro** | Define o zero com o stick parado |
| **Zona morta** | Maior = menos tremor parado |
| **Velocidade** | **Maior = mais lento** no máximo de inclinação |
| **Inverter X / Y** | Espelha esquerda-direita ou cima-baixo |
| **Salvar** | Aplica na placa + JSON local |

## Rodar do código

```powershell
cd psp-stick-tuner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

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
