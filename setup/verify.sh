#!/usr/bin/env bash
# Post-install санітарна перевірка. Запускати на Pi одразу після SSH-у.
# Виводить статуси; ненульовий exit-code = щось не так.
set -u

pass=0
fail=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf "  \033[32mOK\033[0m   %s\n" "$label"
    pass=$((pass+1))
  else
    printf "  \033[31mFAIL\033[0m %s\n" "$label"
    fail=$((fail+1))
  fi
}

echo "== Wi-Fi / мережа =="
check "rfkill не блокує Wi-Fi" bash -c '! rfkill list wifi | grep -q "Soft blocked: yes"'
check "regulatory domain != 00 (country виставлено)" bash -c 'iw reg get | grep -q "country [A-Z][A-Z]:" && ! iw reg get | grep -q "country 00:"'
check "wlan0 connected у NetworkManager" bash -c 'nmcli -t -f DEVICE,STATE device | grep -q "^wlan0:connected$"'
check "інтернет доступний (1.1.1.1)" ping -c 2 -W 2 -I wlan0 1.1.1.1

echo
echo "== Камера / ffmpeg =="
check "ffmpeg встановлений" command -v ffmpeg
check "rpicam-vid встановлений" command -v rpicam-vid
check "CSI-камера видима" bash -c 'rpicam-hello --list-cameras 2>&1 | grep -qi imx'
check "апаратний H.264-енкодер доступний" bash -c 'ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_v4l2m2m'
check "юзер у групі video" bash -c 'id -nG | tr " " "\n" | grep -qx video'

echo
echo "== Bluetooth (для BLE-режиму) =="
check "bluetoothd запущений" systemctl is-active --quiet bluetooth
check "hci0 присутній" bash -c 'hciconfig hci0 2>/dev/null | grep -q "hci0"'
check "hci0 піднятий (UP RUNNING)" bash -c 'hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"'
check "bluezero встановлений" python3 -c 'import bluezero'

echo
echo "== Диск =="
free_mb=$(df -Pm /home 2>/dev/null | awk 'NR==2 {print $4}')
if [ "${free_mb:-0}" -gt 2000 ]; then
  printf "  \033[32mOK\033[0m   вільно %s MB на /home\n" "$free_mb"
  pass=$((pass+1))
else
  printf "  \033[31mFAIL\033[0m мало вільного місця на /home: %s MB (треба >2000)\n" "$free_mb"
  fail=$((fail+1))
fi

echo
echo "-----------------------------------"
printf "Разом: \033[32m%d OK\033[0m / \033[31m%d FAIL\033[0m\n" "$pass" "$fail"
[ "$fail" -eq 0 ] && exit 0 || exit 1
