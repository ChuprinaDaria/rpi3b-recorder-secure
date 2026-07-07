# CLAUDE.md — rpi5-recorder

## Правила комітів
- **НІКОЛИ не додавати `Co-Authored-By: Claude`** ні в комітах, ні в PR.
- Автор коміту — Даша (`Daria Chuprina <daria.chuprina@blue-bird.tech>`).
- Повідомлення коміту — коротко, англійською, present tense (`add`, `fix`, `refactor`), без емодзі.
- Не пушити без явного дозволу.

## Про проєкт
Відео-реєстратор на Raspberry Pi 5. Два незалежні режими запису:

**A. BLE web control** (`pi/ble_recorder.py` + `index.html`)
Керування зі смартфона через Web Bluetooth. Юзер відкриває сторінку з GitHub Pages, тисне «Підключити» → обирає `RPi5-CAM` → ● / ■.

**B. Auto-record** (`pi/autostart.sh`)
Запис стартує при подачі живлення. Зупинка = вимкнення живлення (SIGTERM → graceful close mp4).

Обидва режими пишуть **5-хвилинні mp4-чанки** з ротацією старих файлів (макс 50). Формат імені: `rec_YYYYMMDD_HHMMSS.mp4`.

## Технічні рішення
- **H.264 софтверний.** Pi 5 не має HW-енкодера. 1080p30 @ 10 Mbps ≈ 100% одного ядра, працює стабільно. Вище не піднімати без тестів.
- **FfmpegOutput** (Picamera2) для одразу-в-mp4 муксингу — не raw h264.
- **rpicam-vid** з `--codec libav --libav-format mp4` для sh-варіанту (Bookworm). На Bullseye — `libcamera-vid`.
- **BLE:** bluezero (BlueZ через D-Bus). Один сервіс, дві характеристики: write (cmd) + read (status).
- **Web Bluetooth вимагає HTTPS.** Тому `index.html` — на GitHub Pages, не на Pi.

## Стек
- Python 3.11+ (picamera2, bluezero)
- Bash (rpicam-apps)
- Vanilla JS (Web Bluetooth API, без фреймворків)
- systemd для автозапуску
- GitHub Pages для клієнта

## Дефолтні UUID
```
Service: 12345678-1234-5678-1234-56789abcdef0
Command: 12345678-1234-5678-1234-56789abcdef1  (write, 0x01=start, 0x00=stop)
Status:  12345678-1234-5678-1234-56789abcdef2  (read, 0x01/0x00)
```
Це placeholder-ери. Якщо треба справжні — згенерувати `uuidgen` і оновити в трьох місцях: `ble_recorder.py`, `index.html` (DEFAULTS), CLAUDE.md.

## Що НЕ робити
- Не пропонувати WebUSB — Pi 5 не в USB-gadget режимі, iOS не підтримує.
- Не пропонувати raspap / hostapd — цей проєкт свідомо йде через Web Bluetooth, не hotspot.
- Не додавати превʼю відео на клієнтську сторінку — MJPEG-стрім конфліктує з активним записом.
- Не тримати відео в репо (`.gitignore` вже блокує `*.mp4`).

## Деплой
```bash
git push                                   # оновити код + GitHub Pages
# на Pi:
sudo bash pi/install_ble.sh                # BLE-варіант
sudo bash pi/install_autostart.sh          # auto-record варіант
```

Web-сторінка: https://bluebird-works.github.io/rpi5-recorder/
