# PSP Stick Tuner

Aplicativo para **calibrar o joystick USB** (firmware ZMK do projeto [zmk-analog-rp2040](https://github.com/GsMoura033/zmk-analog-rp2040)).

Não precisa instalar Python se você baixar o **executável** na página de Releases.

## Para quem só quer usar

1. Flash o firmware do stick (artefato do GitHub Actions em `zmk-analog-rp2040`).
2. Ligue o joystick na placa (**VCC → 3,3 V**, nunca 5 V direto no ADC).
3. Conecte a placa no USB.
4. Baixe `PSP-Stick-Tuner.exe` em [Releases](https://github.com/GsMoura033/psp-stick-tuner/releases).
5. Abra o app → **Detectar joystick** → ajuste → **Salvar**.

### O que cada ajuste faz

| Controle | Função |
|---|---|
| **Usar como centro** | Com o stick parado, define o “zero” para não driftar |
| **Zona morta** | Ignora tremores no meio (maior = mais estável parado) |
| **Velocidade** | Número **maior** = cursor **mais lento** (mesmo no máximo de inclinação) |
| **Inverter cima/baixo** | Troca o sentido do eixo Y |
| **Salvar** | Envia os valores **e grava na memória da placa** |

A inclinação do stick controla a velocidade (é analógico). O app limita o quanto ele anda no máximo.

## Desenvolvedores (rodar do código)

```powershell
cd psp-stick-tuner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

### Gerar o .exe localmente

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name PSP-Stick-Tuner app.py
```

O executável sai em `dist\PSP-Stick-Tuner.exe`.

## Protocolo serial (COM)

Baud 115200. Comandos:

```
stick get
stick mid <x> <y>
stick deadzone <n>
stick divisor <n>
stick invert_y <0|1>
stick save
```

Respostas: `STICK OK ...` ou `STICK ERR ...`.
