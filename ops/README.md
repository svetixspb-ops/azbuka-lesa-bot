# ops — сторож и расписания Веры (копии того, что стоит на сервере)

Файлы здесь — не «документация», а **единственный бэкап** этих скриптов и юнитов:
на сервере они лежат вне репозитория (`/root/bin`, `/etc/systemd/system`), а значит
при потере машины исчезают вместе с ней (дыра из аудита бэкапов 11.10.2026).

| файл | куда ставится |
|---|---|
| `vera-healthcheck.sh` | `/root/bin/vera-healthcheck.sh` (chmod +x) |
| `systemd/vera-healthcheck.{service,timer}` | `/etc/systemd/system/`, раз в 5 минут |
| `systemd/vera-weekly-review.{service,timer}` | `/etc/systemd/system/`, понедельник 09:00 МСК |

Развернуть на чистой машине:

```bash
install -m 755 ops/vera-healthcheck.sh /root/bin/vera-healthcheck.sh
cp ops/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vera-healthcheck.timer vera-weekly-review.timer
```

Что проверяет сторож и почему именно это — в шапке самого скрипта.
Состояние между прогонами: `/var/lib/navi/vera-health-state.json`, журнал —
`/var/log/vera-healthcheck.log` (сам себя обрезает до 2000 строк).

Алерты сторож отправляет ботом **Нави**, а не `@azbukalesa_bot`: сигнал должен
доходить и в тот момент, когда у Веры сломано ровно всё, включая её `.env`.
