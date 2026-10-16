#!/usr/bin/env bash
#
# Собирает CarFinder.app — обычный бандл macOS вокруг того же кода.
#
# Намеренно без py2app: приложение запускает интерпретатор из .venv проекта,
# поэтому правки в carfinder/ и config.yaml подхватываются сразу, без пересборки,
# а сам бандл весит килобайты. Обратная сторона — .app привязан к этой папке
# и не переносится на другой компьютер; для личного инструмента это разумный
# размен.
#
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

APP="${1:-$ROOT/CarFinder.app}"
CONTENTS="$APP/Contents"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "Нет .venv — сначала: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if ! "$ROOT/.venv/bin/python" -c "import rumps" 2>/dev/null; then
  echo "Нет rumps — поставьте: .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

# ── исполняемый файл ──────────────────────────────────────────────────────────
# `arch -<нативная>` обязателен. Python из python.org — universal-бинарь, и при
# запуске через Launch Services macOS выбирает из него x86_64-срез (Rosetta),
# тогда как колёса в .venv собраны под arm64. Без этой строки приложение
# стартует, но любой прогон падает на `incompatible architecture` при импорте
# curl_cffi. uname -m даёт arm64 на Apple Silicon и x86_64 на Intel.
NATIVE_ARCH="$(uname -m)"
#
# Интерпретатор запускается отсоединённым (`&` и сразу `exit`), а не через
# `exec`, и без этого значка в строке меню не будет вовсе.
#
# PyObjC ради доступа к GUI перезапускает процесс через системный
# `Python.app` — то есть через бинарник ЧУЖОГО бандла. При `exec` этим
# процессом оказывается ровно тот, которого Launch Services зарегистрировал
# как CarFinder.app: приложение по учётам системы одно, а исполняемый файл
# принадлежит другому бандлу. В таком состоянии macOS значок создаёт
# (`isVisible` — true), но места в строке меню ему не выделяет: координаты
# остаются нулевыми, и на экране нет ничего. Из терминала тот же скрипт
# работает — там регистрации CarFinder.app просто нет.
#
# Поэтому bash сразу выходит, а Python остаётся жить сам (родителем ему
# становится launchd) и регистрируется в системе самостоятельно — как при
# запуске из терминала. Проверяется это строкой «значок: … на месте»
# в data/app.log.
#
# Повторный двойной клик по бандлу теперь не «активирует» приложение,
# а запускает ещё одну копию, которая натыкается на замок в data/app.lock,
# открывает отчёт и выходит. Ровно то же поведение, что и раньше.
cat > "$CONTENTS/MacOS/CarFinder" <<EOF
#!/bin/bash
cd "$ROOT"
export ARCHPREFERENCE="$NATIVE_ARCH"
arch -"$NATIVE_ARCH" "$ROOT/.venv/bin/python" -m carfinder.app >/dev/null 2>&1 &
exit 0
EOF
chmod +x "$CONTENTS/MacOS/CarFinder"

# ── Info.plist ────────────────────────────────────────────────────────────────
# LSUIElement=1 — приложение живёт только в строке меню, без иконки в Dock
# и без пункта в переключателе окон.
cat > "$CONTENTS/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>CarFinder</string>
  <key>CFBundleDisplayName</key>     <string>Car Finder</string>
  <key>CFBundleIdentifier</key>      <string>pl.local.carfinder</string>
  <key>CFBundleVersion</key>         <string>1.1.0</string>
  <key>CFBundleShortVersionString</key> <string>1.1.0</string>
  <key>CFBundleExecutable</key>      <string>CarFinder</string>
  <key>CFBundleIconFile</key>        <string>AppIcon</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>LSUIElement</key>             <true/>
  <key>LSArchitecturePriority</key>
  <array><string>arm64</string><string>x86_64</string></array>
  <key>LSMinimumSystemVersion</key>  <string>12.0</string>
  <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
EOF

# ── иконка ────────────────────────────────────────────────────────────────────
"$ROOT/.venv/bin/python" "$ROOT/scripts/make_icon.py" "$CONTENTS/Resources/AppIcon.icns" \
  || echo "иконку собрать не удалось — приложение будет с системной по умолчанию"

# ── подпись ───────────────────────────────────────────────────────────────────
# Ad-hoc подписи (без сертификата разработчика) достаточно для запуска на своей
# машине и она снимает статус «code object is not signed at all». Заодно убираем
# карантин — он появляется, если бандл куда-то скопировали или переслали,
# и тогда macOS отказывается открывать приложение по двойному клику.
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep --sign - "$APP" 2>/dev/null \
  && echo "подписано ad-hoc" \
  || echo "подписать не удалось — на запуск с этой машины обычно не влияет"

# Сбрасываем кеш иконок Finder, иначе он показывает старую.
touch "$APP"

echo "Готово: $APP"
echo "Запуск:            open '$APP'"
echo "Положить в Программы: mv '$APP' /Applications/"
