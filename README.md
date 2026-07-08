# rpi5-recorder

Відео-реєстратор на Raspberry Pi 5 з USB-камерою (UVC). Два незалежні режими:

- **A. BLE web control** — старт/стоп зі смартфона через Web Bluetooth
- **B. Auto-record** — запис починається одразу після подачі живлення

Обидва режими пишуть `mp4` в `/home/pi/recordings/` з ротацією (макс 50 файлів). Довжина одного файлу задається константою `SEGMENT_SEC` (сек). Дефолт `0` = **не різати**, писати одним файлом на всю сесію.

Веб-сторінка клієнта: **https://bluebird-works.github.io/rpi5-recorder/**

---

## 1. Що потрібно

| Компонент | Мінімум |
|---|---|
| Raspberry Pi 5 | будь-яка модель |
| SD-карта | 32 GB+ (краще A2) |
| USB-веб-камера | UVC, з підтримкою MJPEG 1080p30 (перевіряй через `v4l2-ctl --list-formats`) |
| Живлення | 5 V / 5 A офіційний БЖ |
| Мережа | тільки для першого налаштування (SSH). Далі BLE — офлайн. |
| Смартфон/ноутбук | Android Chrome/Edge або desktop Chrome/Edge/Opera. **iOS Safari та Firefox — не працюють** (немає Web Bluetooth). |

---

## 2. Клонування репозиторію на Pi

Заходиш на Pi по SSH (як налаштувати SSH — див. пункт 6). Далі — два способи склонувати репо. Обирай один.

### 2.1. HTTPS (простіше, підходить якщо у Pi нема ключа GitHub)

```bash
cd ~
git clone https://github.com/bluebird-works/rpi5-recorder.git
cd rpi5-recorder
```

Оновлення потім:
```bash
cd ~/rpi5-recorder && git pull
```

Push з Pi по HTTPS вимагатиме токен (Personal Access Token, не пароль). Для реєстратора зазвичай пушити не треба — тільки читати.

### 2.2. SSH (треба один раз згенерувати ключ)

На Pi:
```bash
ssh-keygen -t ed25519 -C "rpi5-recorder@$(hostname)" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Скопіювати вивід (публічний ключ) → зайти на **https://github.com/settings/keys** → `New SSH key` → назва «rpi5-recorder» → вставити ключ.

Далі перевірити коннект:
```bash
ssh -T git@github.com
# Має відповісти: Hi <username>! You've successfully authenticated...
```

Клонування:
```bash
cd ~
git clone git@github.com:bluebird-works/rpi5-recorder.git
cd rpi5-recorder
```

Оновлення:
```bash
cd ~/rpi5-recorder && git pull
```

Якщо репо вже клоноване через HTTPS і хочеш перемкнути на SSH:
```bash
cd ~/rpi5-recorder
git remote set-url origin git@github.com:bluebird-works/rpi5-recorder.git
git remote -v   # має бути git@github.com:...
```

---

## 3. Встановлення

**Обери один режим.** Обидва одночасно ставити не варто — будуть битися за `/dev/video0`.

### 3.1. Режим A — BLE web control

```bash
cd ~/rpi5-recorder
sudo bash pi/install_ble.sh
```

Що робить скрипт:
- ставить `python3-bluezero`, `ffmpeg`, `bluez`, `v4l-utils`
- копіює `ble_recorder.py` в `/home/pi/rpi5-ble/`
- створює systemd-сервіс `rpi5-ble-recorder.service`
- вмикає автозапуск сервісу при бутстапі

Перевірка:
```bash
sudo systemctl status rpi5-ble-recorder
journalctl -u rpi5-ble-recorder -f
```

Має бути `advertising as 'RPi5-CAM'`.

### 3.2. Режим B — Auto-record

```bash
cd ~/rpi5-recorder
sudo bash pi/install_autostart.sh
```

Що робить скрипт:
- ставить `ffmpeg`, `v4l-utils`
- копіює `autostart.sh` в `/home/pi/rpi5-auto/`
- створює systemd-сервіс `rpi5-auto-recorder.service`
- вмикає автозапуск при бутстапі

Перевірка:
```bash
sudo systemctl status rpi5-auto-recorder
ls -la /home/pi/recordings/
```

Файли повинні зʼявлятись одразу після старту сервісу.

---

## 4. Використання (режим A — BLE)

1. **Живимо Pi.** Ждемо ~15 с — Pi піднімається, стартує сервіс, починає advertising `RPi5-CAM`.
2. **На смартфоні** (Android Chrome/Edge) відкриваємо **https://bluebird-works.github.io/rpi5-recorder/**.
3. Тиснемо **«Підключити»** → системний діалог сканера → обираємо `RPi5-CAM` → **«Pair»**.
4. Через 2-3 секунди — UI показує «готово», активуються кнопки `● Запис` / `■ Стоп`.
5. **`● Запис`** — Pi починає писати. Червона лампочка блимає, статус «запис…».
6. **`■ Стоп`** — Pi коректно закриває mp4 (moov-атом на місці, файл програється).

Скинути UUID до дефолтних або задати свої — розгорни `<details>` «Налаштування BLE» на сторінці, вписуй значення, тисни «Зберегти» (лежить в `localStorage` браузера).

**Радіус BLE:** 5-10 м без перешкод. Через стіну ≈ 3-5 м.

**Reload сторінки = розконект.** Chrome не памʼятає пристрій між сесіями (privacy). Треба знову тиснути «Підключити».

---

## 5. Використання (режим B — Auto-record)

Плагін-плей:
- **Живимо Pi** → через ~15 с починається запис.
- **Знімаємо живлення** → systemd надсилає SIGTERM → ffmpeg закриває mp4 → shutdown. Останній файл не зіпсований.

Забрати відео:
```bash
scp pi@<pi-ip>:/home/pi/recordings/*.mp4 ./
```

---

## 6. Налаштування SSH на Pi (якщо ще не робили)

### 6.1. Ввімкнути SSH (варіант A: під час прошивки SD)

В Raspberry Pi Imager перед записом натисни **⚙ Advanced options**:
- ✅ Enable SSH → Use public-key authentication only (вставити свій `id_ed25519.pub` з ноута)
- ✅ Set username and password (`pi` + сильний пароль)
- ✅ Configure wireless LAN (SSID + пароль домашнього WiFi)

Після першого бута — з ноута:
```bash
ssh pi@<pi-ip>
```

### 6.2. Ввімкнути SSH (варіант B: на вже прошитій системі)

Створити порожній файл `ssh` в `/boot/firmware/` (або підключитись з клавою+моніком і `sudo raspi-config` → `Interface Options` → `SSH` → Enable).

### 6.3. Знайти IP Pi

Якщо Pi в тій же мережі:
```bash
# з ноута
nmap -sn 192.168.1.0/24 | grep -B2 -i raspberry
# або
arp -a | grep -i raspberry
```

Або тимчасово підключити моніторчик і виконати `hostname -I` на самій Pi.

### 6.4. Ключ замість пароля (обовʼязково)

На **ноуті**:
```bash
ssh-copy-id pi@<pi-ip>
# або якщо ключа немає:
ssh-keygen -t ed25519 -C "$USER@$(hostname)"
ssh-copy-id -i ~/.ssh/id_ed25519.pub pi@<pi-ip>
```

Потім вимкнути парольний вхід (на Pi):
```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

---

## 7. Конфігурація

Всі константи — на початку файлів. Значення нижче = дефолти.

### `pi/ble_recorder.py`
```python
REC_DIR = "/home/pi/recordings"
DEVICE_NAME = "RPi5-CAM"                 # ім'я в BLE advertising
VIDEO_DEV = "/dev/video0"
INPUT_FORMAT = "mjpeg"                   # або "yuyv422" якщо камера не вміє MJPEG
WIDTH, HEIGHT, FPS = 1920, 1080, 30
BITRATE = 10_000_000                     # 10 Mbps
SEGMENT_SEC = 0                          # 0 = один файл на сесію; >0 = чанки по N сек
MAX_FILES = 50                           # старі файли ротуються
```

### `pi/autostart.sh`
Ті ж змінні, але задаються через env. Приклад — писати чанки по 5 хвилин:
```bash
sudo systemctl edit rpi5-auto-recorder
# додати:
# [Service]
# Environment=SEGMENT_SEC=300
sudo systemctl restart rpi5-auto-recorder
```

### UUID (сервіс і характеристики BLE)
Дефолтні placeholder-и — у трьох місцях:
- `pi/ble_recorder.py` → `SERVICE_UUID`, `CHAR_UUID`, `STATUS_UUID`
- `index.html` → `DEFAULTS`
- цей README + `CLAUDE.md`

Згенерувати нові — `uuidgen`. Замінити всюди.

---

## 8. Діагностика

**Веб-сторінка каже «Web Bluetooth недоступний»** — браузер не той. Треба Chrome/Edge/Opera, не Firefox/Safari. На iOS — жодна опція не працює.

**Pi не зʼявляється в списку сканера** — перевір з іншого телефону через [nRF Connect](https://play.google.com/store/apps/details?id=no.nordicsemi.android.mcp) чи Pi взагалі рекламується:
```bash
sudo systemctl status rpi5-ble-recorder
sudo hciconfig                    # має бути UP RUNNING
sudo journalctl -u rpi5-ble-recorder -n 50
```

**Запис не стартує (BLE підʼєднаний, кнопка натиснута, але файлів нема)** — типово `/dev/video0` недоступний:
```bash
groups pi | grep -o video          # має вивести "video"
ffmpeg -f v4l2 -list_formats all -i /dev/video0
```
Якщо `video` немає в групах — виконати `sudo usermod -aG video pi && sudo reboot`.

**Файли є, але не програються (moov-атом відсутній)** — це станеться якщо процес вбити через `SIGKILL` замість `SIGTERM`. Systemd надсилає SIGTERM за замовчуванням, тож проблема тільки якщо hard-power-off посеред запису **І** `SEGMENT_SEC=0`. Для протидії — виставити `SEGMENT_SEC=300` (втратиш максимум 5 хв).

**Web Bluetooth debugger від Google** — лінк прямо на сторінці клієнта внизу.

---

## 9. Оновлення коду на Pi

```bash
cd ~/rpi5-recorder
git pull
# режим A:
sudo bash pi/install_ble.sh && sudo systemctl restart rpi5-ble-recorder
# режим B:
sudo bash pi/install_autostart.sh && sudo systemctl restart rpi5-auto-recorder
```

---

## 10. Що НЕ підтримується

- CSI-камери (Raspberry Camera Module) — код заточений під UVC/USB, не використовує `picamera2`.
- WebUSB — iOS не підтримує, Pi 5 не в USB-gadget режимі.
- Wi-Fi hotspot / raspap — свідомо не робимо, ідея проєкту саме в тому, що керування йде через BLE без будь-якого налаштування мережі з боку користувача.
- iOS Safari клієнт — Apple не запровадила Web Bluetooth.
