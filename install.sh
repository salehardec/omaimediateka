#!/usr/bin/env bash
# Установка плагина Omaimediateka в Omarchy Shell.
#
#   ./install.sh            — установить/обновить
#   ./install.sh --uninstall — удалить плагин из каталога пользователя
#
# Каталог назначения берётся из "id" в manifest.json.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
id="$(sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$here/manifest.json" | head -n1)"
[ -n "$id" ] || { echo "manifest.json: не найден id" >&2; exit 1; }

dest="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$id"

if [ "${1:-}" = "--uninstall" ]; then
  if [ -d "$dest" ]; then
    rm -rf "$dest"
    echo "Удалено: $dest"
    echo "Уберите \"$id\" из ~/.config/omarchy/shell.json и выполните: omarchy restart shell"
  else
    echo "Не найден: $dest"
  fi
  exit 0
fi

mkdir -p "$dest"

# Копируем только то, что составляет плагин; служебные файлы репозитория
# (docs, tests, .git) не нужны в каталоге shell.
for f in manifest.json Panel.qml VideoView.qml MediaModel.js README.md LICENSE; do
  [ -e "$here/$f" ] && cp -f "$here/$f" "$dest/"
done
mkdir -p "$dest/bin"
cp -f "$here/bin/oma-mediateka" "$dest/bin/"
cp -f "$here/bin/oma-mediateka-index.py" "$dest/bin/"
chmod +x "$dest/bin/oma-mediateka" "$dest/bin/oma-mediateka-index.py"

echo "Установлено: $dest"
echo
echo "1) Добавьте виджет в центр бара в ~/.config/omarchy/shell.json, секция bar.layout.center:"
echo "     { \"id\": \"$id\" }"
echo "2) Перезапустите шелл: omarchy restart shell"
echo
echo "Системная подготовка (один раз, с sudo):"
echo "     sudo pacman -S gvfs-afc"
echo "     sudo systemctl start usbmuxd   # обычно службу поднимает udev при подключении"
echo "Затем подключите iPhone кабелем и нажмите «Доверять» на телефоне."
