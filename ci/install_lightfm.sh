#!/usr/bin/env bash
# Installs lightfm 1.17 for the test runs. PyPI only has its source code (no ready-made wheel), and its setup.py
# writes to __builtins__ in a way current pip build backends break. Snowflake installs it from its Anaconda
# channel, so this only concerns CI / laptops. We download the exact release, fix that one line, and build it.
set -euo pipefail
VERSION=1.17
URL=https://files.pythonhosted.org/packages/1f/96/5ec230f5c27811534af0faaa8525f11c1000ee1c24c8a82c0546d0724aea/lightfm-${VERSION}.tar.gz
SHA256=2b77ada182ccd768a8d7643ab3cfcd8b6e855db09087f7cc7329bd63316697a8   # pinned: a changed file fails the install
WORK=$(mktemp -d)
curl -sSfL "$URL" -o "$WORK/lightfm.tar.gz"
echo "$SHA256  $WORK/lightfm.tar.gz" | sha256sum -c -                 # the file is exactly the PyPI release
tar xzf "$WORK/lightfm.tar.gz" -C "$WORK"
sed -i 's/^__builtins__.__LIGHTFM_SETUP__ = True$/import builtins; builtins.__LIGHTFM_SETUP__ = True/' \
    "$WORK/lightfm-${VERSION}/setup.py"
pip install --no-build-isolation "$WORK/lightfm-${VERSION}"
python -c "import lightfm; print('lightfm', lightfm.__version__)"