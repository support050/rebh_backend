#!/usr/bin/env bash
# Render Build Script - Installs Chrome + ChromeDriver for Selenium
#
# Build Command on Render MUST be:
#   bash render-build.sh
#
# Runtime env vars (export below only applies during BUILD — set these in Render → Environment):
#   CHROME_BIN=/opt/render/project/.chrome/chrome-linux64/chrome
#   CHROMEDRIVER_PATH=/opt/render/project/.chrome/chromedriver-linux64/chromedriver

set -ex

CHROME_VERSION="131.0.6778.85"
CHROME_DIR="/opt/render/project/.chrome"
CHROME_BIN="${CHROME_DIR}/chrome-linux64/chrome"
CHROMEDRIVER_PATH="${CHROME_DIR}/chromedriver-linux64/chromedriver"

echo "===== render-build.sh START ====="
echo "🔧 Installing Python dependencies..."
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

echo "🌐 Installing Chrome for Testing ${CHROME_VERSION}..."
mkdir -p "${CHROME_DIR}"
cd "${CHROME_DIR}"

echo "Downloading Chrome..."
wget -q -O chrome-linux.zip \
  "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-linux64.zip"
unzip -qo chrome-linux.zip
rm -f chrome-linux.zip

echo "Downloading ChromeDriver..."
wget -q -O chromedriver-linux.zip \
  "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
unzip -qo chromedriver-linux.zip
rm -f chromedriver-linux.zip

chmod +x "${CHROME_BIN}" "${CHROMEDRIVER_PATH}"

# Build-time only — does NOT carry over to runtime. Set the same values in Render Environment.
export CHROME_BIN
export CHROMEDRIVER_PATH

echo "✅ Chrome installed at: ${CHROME_BIN}"
echo "✅ ChromeDriver installed at: ${CHROMEDRIVER_PATH}"

echo "===== Chrome Version ====="
"${CHROME_BIN}" --version || echo "⚠️ Chrome --version failed (missing libs?)"

echo "===== ChromeDriver Version ====="
"${CHROMEDRIVER_PATH}" --version

echo "===== Chrome directory listing ====="
ls -la "${CHROME_DIR}/chrome-linux64/"
ls -la "${CHROME_DIR}/chromedriver-linux64/"

test -x "${CHROME_BIN}"
test -x "${CHROMEDRIVER_PATH}"

echo "🎉 Build complete!"
echo "⚠️ Reminder: set CHROME_BIN and CHROMEDRIVER_PATH as Render Environment Variables for runtime."
echo "===== render-build.sh END ====="
