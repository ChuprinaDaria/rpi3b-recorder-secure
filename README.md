# rpi5-recorder

Відео-реєстратор на Raspberry Pi 4 / Pi 5 з CSI-камерою **Camera Module 3 (Sony IMX708)**. Два незалежні режими:

- **A. BLE web control** — старт/стоп зі смартфона через Web Bluetooth
- **B. Auto-record** — запис починається одразу після подачі живлення

Обидва режими пишуть `mp4` в `~/recordings/` з ротацією (макс 50 файлів). Довжина одного файлу задається змінною `SEGMENT_SEC` (сек). Дефолт `0` = **не різати**, писати одним файлом на всю сесію.

Енкодер обирається автоматично під залізо:
- **Pi 4** має апаратний H.264 (`h264_v4l2m2m`) — `rpicam-vid` віддає готовий elementary stream, ffmpeg тільки муксить його в mp4 без перекодування. CPU майже вільний.
- **Pi 5** апаратного енкодера не має — камера віддає сирий YUV, кодує ffmpeg (libx264 ultrafast).

Обидва шляхи дають однаковий результат: 1080p30 без дропів, сегменти рівно по `SEGMENT_SEC`. Фокус за замовчуванням — **фіксована безкінечність**.

Веб-сторінка клієнта: **https://bluebird-works.github.io/rpi5-recorder/**

---

## 1. Що потрібно

| Компонент | Мінімум |
|---|---|
| Raspberry Pi 4 або 5 | 2 GB+, Raspberry Pi OS / Debian 12+ |
| SD-карта | 32 GB+ (краще A2) |
| CSI-камера | Camera Module 3 (IMX708). Перевірка: `rpicam-hello --list-cameras` |
| Живлення | офіційний БЖ (Pi 4 — 5 V / 3 A) |
| Мережа | тільки для першого налаштування (SSH). Далі BLE — офлайн. |
| Смартфон/ноутбук | Android Chrome/Edge або desktop Chrome/Edge/Opera. **iOS Safari та Firefox — не працюють** (немає Web Bluetooth). |

### Що має стояти на самій Pi

Ставиться автоматично інсталяторами, руками нічого доставляти не треба:

| Пакет | Навіщо | Хто ставить |
|---|---|---|
| `rpicam-apps` | `rpicam-vid` — захоплення з CSI-камери + апаратний H.264 | обидва режими |
| `ffmpeg` | муксинг elementary stream у mp4, нарізка на сегменти | обидва режими |
| `bluez` | `bluetoothd` (GATT-сервер) + `btmgmt` | режим A |
| `python3-bluezero` | Python-обгортка BlueZ через D-Bus. Немає в apt Debian 13 → ставиться через `pip3 --break-system-packages` | режим A |
| `python3-dbus`, `python3-gi` | залежності bluezero | режим A |

Плюс інсталятор сам:
- додає користувача в групу `video` (доступ до камери);
- знімає `rfkill` з Bluetooth і піднімає `hci0` (на свіжому образі адаптер часто лежить `DOWN`);
- створює systemd-юніт з автозапуском на буті.

Перевірити все разом — `setup/verify.sh`.

---

## 2. Клонування репозиторію на Pi

Реєстратор автономний — після налаштування живе без інтернету. Код на Pi треба залити **один раз** (плюс зрідка `git pull` коли міняється). GitHub є публічним, тож клон анонімно через HTTPS — без токенів і без ключів:

```bash
cd ~
git clone https://github.com/bluebird-works/rpi5-recorder.git
cd rpi5-recorder
```

Оновлення потім:
```bash
cd ~/rpi5-recorder && git pull
```

SSH-ключ до GitHub на Pi **не потрібен** — з реєстратора нічого туди не пушиться. (SSH між твоїм ноутом і Pi — це інша річ, потрібна щоб взагалі залізти на Pi, див. пункт 6.)

---

## 3. Встановлення

**Обери один режим.** Обидва одночасно ставити не варто — будуть битися за камеру.

### 3.1. Режим A — BLE web control

```bash
cd ~/rpi5-recorder
sudo bash pi/install_ble.sh
```

Що робить скрипт:
- ставить `python3-bluezero`, `ffmpeg`, `rpicam-apps`, `bluez`
- копіює `ble_recorder.py` в `~/rpi5-ble/`
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
- ставить `ffmpeg`, `rpicam-apps`
- копіює `autostart.sh` в `~/rpi5-auto/`
- створює systemd-сервіс `rpi5-auto-recorder.service`
- вмикає автозапуск при бутстапі

Перевірка:
```bash
sudo systemctl status rpi5-auto-recorder
ls -la ~/recordings/
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
- **Знімаємо живлення** → systemd надсилає SIGTERM → скрипт валить `rpicam-vid`, ffmpeg добиває mp4 по EOF → shutdown. Останній файл не зіпсований.

Забрати відео:
```bash
scp pi@<pi-ip>:~/recordings/*.mp4 ./
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

Обидва режими читають однакові змінні оточення — задаються при старті запису, код правити не треба.

| Змінна | Дефолт | Що робить |
|---|---|---|
| `WIDTH` / `HEIGHT` | `1920` / `1080` | роздільність. **Вище 1920 по ширині HW-енкодер не вміє** |
| `FPS` | `30` | кадрів за секунду |
| `BITRATE` | `10000000` | 10 Mbps |
| `AUTOFOCUS_MODE` | `manual` | `manual`, `auto` або `continuous` |
| `LENS_POSITION` | `0` | тільки для `manual`. `0` = безкінечність, `default` = гіперфокал |
| `ENCODER` | `auto` | `auto` / `hardware` / `software`. `auto` визначає наявність `bcm2835-codec` — Pi 4 → апаратний, Pi 5 → софтверний |
| `SEGMENT_SEC` | `0` | `0` = один файл на сесію; `>0` = чанки по N сек |
| `REC_DIR` | `~/recordings` | куди писати |
| `MAX_FILES` | `50` | скільки файлів тримати |
| `FREE_MB_MIN` | `500` | нижче цього вільного місця — примусова ротація (тільки режим B) |

Змінити на живому сервісі — писати чанки по 5 хвилин:
```bash
sudo systemctl edit rpi5-auto-recorder
# додати:
# [Service]
# Environment=SEGMENT_SEC=300
sudo systemctl restart rpi5-auto-recorder
```

Разово, без systemd:
```bash
REC_DIR=/tmp/test SEGMENT_SEC=10 LENS_POSITION=0 bash pi/autostart.sh
```

### Фокус
`AUTOFOCUS_MODE=manual` + `LENS_POSITION=0` — лінза жорстко на безкінечність, автофокус не смикається під час запису. Це дефолт: для реєстратора «різко все, що далі кількох метрів» кращий за автофокус, який перефокусовується на кожну зміну сцени.

Якщо треба різкість на близькій дистанції — `LENS_POSITION` задається як **обернена відстань** (діоптрії): `0.5` ≈ 2 м, `1` ≈ 1 м, `2` ≈ 0.5 м.

### Роздільність вище 1080p
Апаратний енкодер Pi 4 обмежений 1920 по ширині, тому для ширших кадрів треба явно `ENCODER=software`:
```bash
WIDTH=2304 HEIGHT=1296 ENCODER=software bash pi/autostart.sh
```
Заміряно на Pi 4 / `2304x1296`: софтверний libx264 тримає 30 fps, але грів чіп до 76°C проти 62°C на апаратному — без активного охолодження на довгому записі впреться в тротлінг. Повний сенсорний режим `4608x2592` видав 7.9 fps замість 14 — непридатний. На Pi 5 запас більший, але окремо не міряний.

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

**Запис не стартує (BLE підʼєднаний, кнопка натиснута, але файлів нема)** — типово камера не бачиться:
```bash
rpicam-hello --list-cameras        # має показати imx708
groups | grep -o video             # має вивести "video"
```
Якщо `video` немає в групах — `sudo usermod -aG video $USER && sudo reboot`.
Якщо камери нема в списку — перевірити шлейф CSI (контактами до плати) і `dmesg | grep imx708`.

**`ERROR: *** no cameras available ***`** — камеру вже тримає інший процес. Обидва сервіси одночасно не ставити: `systemctl status rpi5-ble-recorder rpi5-auto-recorder`.

**У логах `Timestamps are unset in a packet`** — косметично, ігнорувати. Вилазить один раз на перший пакет raw h264; тривалість і fps у готовому файлі коректні.

**Файли є, але не програються (moov-атом відсутній)** — станеться якщо процес вбити через `SIGKILL` замість `SIGTERM`. Systemd надсилає SIGTERM за замовчуванням, тож проблема тільки при hard-power-off посеред запису. Пом'якшено фрагментованим mp4 (`+frag_keyframe+empty_moov`) — файл лишається програвабельним і без moov.

**Пристрій `RPi5-CAM` не видно у сканері телефона.** Спершу перевір, що реклама реально в ефірі:
```bash
sudo btmgmt advinfo | grep -i "instances list"    # має бути "1 item", а не "0 items"
```
Якщо `0 items` — не піднявся обхід із `pi/ble_advertise.sh`, дивись логи `journalctl -u rpi5-ble-recorder`. У нормі там є рядок `Instance added: 1`.

Чому взагалі обхід: контролер Pi 4 (CYW43455) не підтримує LE Extended Advertising, а BlueZ 5.82 реєструє рекламу тільки через розширений mgmt-шлях і отримує від ядра `Invalid Parameters (0x0d)`. Через D-Bus рекламу підняти неможливо — bluezero отримує `org.bluez.Error.Failed`. Тому інстанс створюється напряму legacy-шляхом через `btmgmt`, а GATT-сервер лишається за `bluetoothd`. Деталі — в шапці `pi/ble_advertise.sh`.

У логах сервісу через це завжди є нешкідливий рядок від bluezero про невдалу реєстрацію реклами — це очікувано, GATT при цьому працює.

**BLE — це радіо на ~10 метрів.** Ні Tailscale, ні SSH тут не допоможуть: щоб підключитись зі смартфона, треба фізично бути поруч із Pi.

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

- USB/UVC-камери — код заточений під CSI через `rpicam-vid`, входу з `/dev/video0` немає.
- Роздільність вище 1920 по ширині на апаратному енкодері.
- WebUSB — iOS не підтримує, Pi не в USB-gadget режимі.
- Wi-Fi hotspot / raspap — свідомо не робимо, ідея проєкту саме в тому, що керування йде через BLE без будь-якого налаштування мережі з боку користувача.
- iOS Safari клієнт — Apple не запровадила Web Bluetooth.
