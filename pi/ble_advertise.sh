#!/usr/bin/env bash
# Обхід бага стека на Pi 4.
#
# Контролер Pi 4 (CYW43455) не підтримує LE Extended Advertising — біт 12
# у LE-фічах = 0. BlueZ 5.82 при цьому реєструє рекламу тільки через
# розширений mgmt-шлях (Add Ext Adv Data, 0x0055), і ядро відповідає
# Invalid Parameters (0x0d). Через D-Bus рекламу підняти неможливо:
# bluezero отримує org.bluez.Error.Failed, ActiveInstances лишається 0.
#
# Старий legacy-шлях (mgmt Add Advertising) на тому ж контролері працює —
# HCI LE Set Advertise Enable повертає Success. Тому інстанс реклами
# створюємо напряму через btmgmt, а GATT-сервер лишається за bluetoothd.
#
# Перевіряти, чи баг ще актуальний: pi/ble_advertise.sh check
set -u

ACTION="${1:-start}"
NAME="${DEVICE_NAME:-RPi5-CAM}"
UUID="${SERVICE_UUID:-12345678-1234-5678-1234-56789abcdef0}"
INSTANCE=1

# btmgmt — це bt_shell: команду з argv він виконує асинхронно, а на EOF у stdin
# виходить негайно, не встигнувши її відправити. Із systemd stdin = /dev/null,
# тобто EOF одразу, і команда тихо не виконується (btmon показує MGMT Open/Close
# без жодної команди). Тому підсовуємо stdin, який лишається відкритим, і
# прибиваємо процес по timeout — сам він не завершується ніколи.
bt() {
  timeout 5 btmgmt "$@" < <(sleep 6) 2>&1 || true
}

clear_instances() {
  bt rm-adv "${INSTANCE}" >/dev/null
}

case "${ACTION}" in
  stop)
    clear_instances
    ;;
  check)
    # біт 12 восьмибайтового LE-features → підтримка ext adv
    hcitool cmd 0x08 0x0003
    echo "якщо байт 2 відповіді (після статусу) має біт 0x10 — ext adv є, обхід більше не потрібен"
    ;;
  start)
    clear_instances
    # scan response: <len> 0x09 <name>, де 0x09 = Complete Local Name
    name_hex=""
    for ((i = 0; i < ${#NAME}; i++)); do
      printf -v h '%02x' "'${NAME:i:1}"
      name_hex+="${h}"
    done
    scan_rsp=$(printf '%02x09%s' $((${#NAME} + 1)) "${name_hex}")
    bt add-adv -u "${UUID}" -s "${scan_rsp}" -c -g "${INSTANCE}" | tail -1
    ;;
  *)
    echo "usage: $0 {start|stop|check}" >&2
    exit 1
    ;;
esac
