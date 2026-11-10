# PSP Stick Tuner

App de desktop para calibrar o joystick ZMK (PSP Stick) em tempo real pela porta serial USB.

## O que faz

- Conecta no **COMx** do PSP Stick
- Mostra **X/Y mV** ao vivo (logs do firmware)
- Ajusta **centro**, **deadzone**, **velocidade** e **inverter Y**
- Botão **Capturar centro** usa a média dos mV atuais
- **Aplicar** manda comandos `stick ...` para o firmware (vale na hora, em RAM)

> Precisa do firmware com shell (`stick` commands). Builds a partir da calibração runtime no repo `zmk-analog-rp2040`.

## Rodar no Windows

```powershell
cd $env:USERPROFILE\Desktop\psp-stick-tuner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

1. Liga o stick **com o joystick já conectado** (VCC → 3V3)
2. Escolhe a porta do **PSP Stick** (não a Sofle)
3. Conectar → Capturar centro → Aplicar
4. Ajusta velocidade (divisor maior = mais lento) e aplica de novo

## Protocolo

| Comando | Efeito |
|---|---|
| `stick get` | Lê calibração atual |
| `stick mid <x> <y>` | Centro em mV reportados |
| `stick deadzone <n>` | Zona morta (0–255) |
| `stick divisor <n>` | Velocidade (maior = mais lento) |
| `stick invert_y <0\|1>` | Inverte eixo Y |

Respostas começam com `STICK OK` ou `STICK ERR`.
