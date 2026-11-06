#!/bin/bash
# Tito Repricer - installer for macOS. Builds "Tito Repricer.app" in Applications.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Tito-Repricer"
DATA="$HOME/.tito-repricer"
mkdir -p "$DEST" "$DATA"
curl -s -m 2 -X POST http://localhost:8766/api/quit >/dev/null 2>&1 && sleep 1 || true
osascript -e 'tell application "Tito Repricer" to quit' >/dev/null 2>&1 || true
cp "$SRC/repricer.py" "$DEST/"
if ! /usr/bin/python3 -c "import sys" >/dev/null 2>&1; then
  echo "Python 3 is missing - install Command Line Tools first (xcode-select --install), then rerun."
  exit 1
fi
APPS="/Applications"; [ -w "$APPS" ] || APPS="$HOME/Applications"; mkdir -p "$APPS"
APP="$APPS/Tito Repricer.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# icon: build icns from icon-1024.png if present
if [ -f "$SRC/icon-1024.png" ]; then
  IC="$(mktemp -d)/AppIcon.iconset"; mkdir -p "$IC"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s "$SRC/icon-1024.png" --out "$IC/icon_${s}x${s}.png" >/dev/null
    d=$((s*2)); sips -z $d $d "$SRC/icon-1024.png" --out "$IC/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$IC" -o "$APP/Contents/Resources/AppIcon.icns" || true
fi
cat > "$APP/Contents/Resources/start.sh" <<EOF
#!/bin/bash
SRC_DRIVE="$SRC"
DEST="$DEST"
DATA="$DATA"
ver(){ grep -m1 '^VERSION' "\$1" 2>/dev/null | tr -dc '0-9.'; }
if [ -f "\$SRC_DRIVE/repricer.py" ]; then
  DV="\$(ver "\$SRC_DRIVE/repricer.py")"; LV="\$(ver "\$DEST/repricer.py")"
  NEWEST="\$(printf '%s\n%s\n' "\$LV" "\$DV" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
  if [ -n "\$DV" ] && [ "\$DV" != "\$LV" ] && [ "\$NEWEST" = "\$DV" ]; then
    cp -f "\$SRC_DRIVE/repricer.py" "\$DEST/repricer.py"
    curl -s -m 2 -X POST http://localhost:8766/api/quit >/dev/null 2>&1
    sleep 1
  fi
fi
cd "\$DEST"
TR_NO_BROWSER=1 nohup /usr/bin/python3 "\$DEST/repricer.py" >> "\$DATA/server.log" 2>&1 &
exit 0
EOF
chmod +x "$APP/Contents/Resources/start.sh"
NATIVE=0
BUILD="$(mktemp -d)"; cp "$SRC/TitoRepricer.swift" "$BUILD/main.swift"
echo "Building the app window (about 30 seconds)..."
if xcrun swiftc -O -o "$APP/Contents/MacOS/Tito Repricer" "$BUILD/main.swift" -framework Cocoa -framework WebKit > "$DATA/build.log" 2>&1; then
  NATIVE=1
fi
rm -rf "$BUILD"
if [ "$NATIVE" != "1" ]; then
  echo "(Native window could not be built - see $DATA/build.log. Using a Chrome window instead.)"
  cat > "$APP/Contents/MacOS/Tito Repricer" <<EOF
#!/bin/bash
/bin/bash "$APP/Contents/Resources/start.sh"
sleep 1.5
cd "$DEST" && /usr/bin/python3 -c "import repricer as R; R.open_window('http://localhost:8766/')"
EOF
  chmod +x "$APP/Contents/MacOS/Tito Repricer"
fi
UIELEMENT="<false/>"; [ "$NATIVE" = "1" ] || UIELEMENT="<true/>"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Tito Repricer</string>
  <key>CFBundleDisplayName</key><string>Tito Repricer</string>
  <key>CFBundleIdentifier</key><string>com.delcampoxt.titorepricer</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Tito Repricer</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key>$UIELEMENT
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
echo ""
echo "Done. Open 'Tito Repricer' from Applications."
echo "First time: Settings -> 'Import keys from FNSKU Labels' -> Save -> Scan now."
echo "It installs in DRY RUN mode: it proposes but sends nothing until you turn Dry run off."
