# Application-owned desktop test environment. Build and run only on the fleet.
FROM python:3.14.8-slim-bookworm@sha256:44e000a07b48bef1517a2b14adc93c17af728ca98858b5e24c6e1b18de1b2ef2
ARG APT_MIRROR
ARG APT_SECURITY_MIRROR
ARG PACKAGE_INDEX
ARG FIREFOX_DOWNLOAD_HOST
COPY fleet-ca.crt /usr/local/share/ca-certificates/fleet-ci.crt
RUN test -n "$APT_MIRROR" && test -n "$APT_SECURITY_MIRROR" && \
    printf 'deb %s bookworm main\ndeb %s bookworm-updates main\ndeb %s bookworm-security main\n' \
      "$APT_MIRROR" "$APT_MIRROR" "$APT_SECURITY_MIRROR" > /etc/apt/sources.list && \
    rm -f /etc/apt/sources.list.d/debian.sources && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libatomic1 libfontconfig1 fonts-dejavu-core libgl1 libegl1 libopengl0 \
      libdbus-1-3 libxkbcommon0 libnss3 libnspr4 libx11-xcb1 libxcb-cursor0 \
      libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
      libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxcomposite1 \
      libxdamage1 libxrandr2 libxshmfence1 libxtst6 libxss1 libasound2 libdrm2 libgbm1 \
      openssl && update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    UV_NATIVE_TLS=true PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY requirements.txt project.toml /opt/ci-inputs/
RUN PIP_INDEX_URL="$PACKAGE_INDEX" python -m pip install --upgrade pip==26.3 && \
    PIP_INDEX_URL="$PACKAGE_INDEX" python -m pip install --require-hashes -r /opt/ci-inputs/requirements.txt && \
    PIP_INDEX_URL="$PACKAGE_INDEX" python -c \
      'import subprocess,sys,tomllib; p=tomllib.load(open("/opt/ci-inputs/project.toml","rb")); subprocess.run([sys.executable,"-m","pip","install","setuptools","wheel","uv",*p["project"]["optional-dependencies"]["test"],*p["project"]["optional-dependencies"]["connected"]],check=True)' && \
    python -m playwright install-deps firefox && \
    PLAYWRIGHT_DOWNLOAD_HOST="$FIREFOX_DOWNLOAD_HOST" \
      PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST="$FIREFOX_DOWNLOAD_HOST" python -m playwright install firefox && \
    chmod -R a+rX /opt/playwright && \
    python -c 'from PySide6 import QtCore,QtGui,QtWidgets,QtNetwork,QtWebEngineCore,QtWebEngineWidgets,QtQuick; print(QtCore.qVersion())' && \
    dpkg-query -W > /opt/ci-inputs/os-packages.txt && \
    rm -rf /var/lib/apt/lists/* /root/.cache && useradd --uid 1000 --create-home fleet
USER 1000:1000
WORKDIR /workspace
ENTRYPOINT []
