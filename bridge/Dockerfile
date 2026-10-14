# Apple TV bridge + bundled Siri voice remote, in one image.
#   docker build -t atvbridge bridge/
# Run with --network host (mDNS for phones and HomeKit) and a volume on /data.

# --- Siri voice remote (HomeKit Target Control accessory, Node.js) ---------------------
# https://github.com/marcusadolfsson/appletv-siri-voice (Apache-2.0), pinned to a commit.
ARG SIRI_REF=b4b951552ac4f2a69b7e0467928ba9eaa32012aa

FROM node:22-bookworm-slim AS siri
ARG SIRI_REF
ADD https://github.com/marcusadolfsson/appletv-siri-voice/archive/${SIRI_REF}.tar.gz /tmp/siri.tar.gz
WORKDIR /opt/siri
RUN mkdir /tmp/src && tar -xzf /tmp/siri.tar.gz -C /tmp/src --strip-components=1 \
    && cp /tmp/src/bridge/index.js /tmp/src/bridge/package.json \
          /tmp/src/LICENSE /tmp/src/NOTICE /tmp/src/THIRD_PARTY.md ./ \
    && rm -rf /tmp/src /tmp/siri.tar.gz \
    && npm install --omit=dev --no-audit --no-fund && npm cache clean --force \
    # Licence notices of everything bundled in node_modules travel with the image.
    && mkdir -p licenses && for d in node_modules/*/ node_modules/@*/*/; do \
         [ -d "$d" ] || continue; \
         name=$(echo "$d" | sed 's|node_modules/||; s|/$||; s|/|__|g'); \
         for f in "$d"LICENSE "$d"LICENSE.md "$d"LICENSE.txt "$d"LICENCE "$d"COPYING; do \
           if [ -f "$f" ]; then cp "$f" "licenses/$name.LICENSE"; break; fi; \
         done; \
       done

# --- bridge ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ATVB_DATA=/data

# Node runtime for the Siri voice remote (same Debian release as the node image).
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=siri /usr/local/bin/node /usr/local/bin/node
COPY --from=siri /opt/siri /opt/siri

WORKDIR /app
COPY requirements.txt .
# Some dependencies (e.g. miniaudio) have no prebuilt wheel for every architecture; compile
# those with a toolchain that is removed again in the same layer.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY atvbridge ./atvbridge
# Licence + notices (copied into the build context by CI; requirements.txt keeps the glob valid).
COPY requirements.txt LICENSE* NOTICE* /app/

VOLUME /data
# 8090 dashboard + companion app, 6466/6467 Google TV remote, 47129 HomeKit (Siri remote).
EXPOSE 8090 6466 6467 47129

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/state' % os.environ.get('ATVB_WEB_PORT', '8090'), timeout=4)"

CMD ["python", "-m", "atvbridge"]
