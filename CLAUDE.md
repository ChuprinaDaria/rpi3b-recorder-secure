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

Обидва режими підтримують **розбиття на mp4-чанки** через константу `SEGMENT_SEC` (секунди). Дефолт `0` = не різати, писати одним файлом на всю сесію. Ротація старих файлів (макс 50). Формат імені: `rec_YYYYMMDD_HHMMSS.mp4`.

## Технічні рішення
- **Камера — USB (UVC), не CSI.** Читаємо через V4L2 (`/dev/video0`). Дефолтний `INPUT_FORMAT=mjpeg` — так більшість веб-камер вміщає 1080p30 в USB2.0. Якщо камера видає тільки `yuyv422`, змінити змінну.
- **H.264 софтверний (libx264 preset=ultrafast).** Pi 5 не має HW-енкодера, а UVC-стрім із камери сирий/MJPEG. 1080p30 @ 10 Mbps ≈ 100% одного ядра.
- **ffmpeg segment muxer** (`-f segment -segment_time N -strftime 1`) сам ріже на mp4-чанки без розривів, якщо `SEGMENT_SEC > 0`. Якщо `SEGMENT_SEC = 0`, юзаємо звичайний `-f mp4` в один файл. Не пишемо raw h264, одразу муксимо в mp4.
- **BLE:** bluezero (BlueZ через D-Bus). Один сервіс, дві характеристики: write (cmd) + read (status). Python-код стартує/зупиняє ffmpeg-subprocess.
- **Web Bluetooth вимагає HTTPS.** Тому `index.html` — на GitHub Pages, не на Pi.

## Стек
- Python 3.11+ (bluezero, subprocess → ffmpeg)
- Bash + ffmpeg (v4l2 input, libx264, segment muxer)
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
- Не тягнути назад `picamera2` / `rpicam-vid` — тестова камера USB, CSI відсутня.

## Деплой
```bash
git push                                   # оновити код + GitHub Pages
# на Pi:
sudo bash pi/install_ble.sh                # BLE-варіант
sudo bash pi/install_autostart.sh          # auto-record варіант
```

Web-сторінка: https://bluebird-works.github.io/rpi5-recorder/
