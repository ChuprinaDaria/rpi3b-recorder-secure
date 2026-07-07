# rpi5-recorder

Two variants of video recording on Raspberry Pi 5.

## Variant A: Web Bluetooth control

Web page: **[https://bluebird-works.github.io/rpi5-recorder/](https://bluebird-works.github.io/rpi5-recorder/)**

On the Pi:
```bash
sudo bash pi/install_ble.sh
```

Open the page in Chrome (Android/desktop). Enter UUIDs, tap Connect, pick RPi5-CAM, use ● / ■ buttons.

## Variant B: Auto-record on power-on

On the Pi:
```bash
sudo bash pi/install_autostart.sh
```

Recording starts on boot. Files rotate in `/home/pi/recordings/` in 5-min mp4 segments.

## Notes

- Pi 5 has no hardware H.264 encoder — encoding runs on CPU (1080p30 @ 10 Mbps is fine).
- Web Bluetooth requires HTTPS — that is why GitHub Pages hosts the client.
- iOS Safari does not support Web Bluetooth. Android Chrome / desktop Chrome / Edge work.
