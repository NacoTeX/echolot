ARG BUILD_FROM
FROM $BUILD_FROM

# What Home Assistant's add-on builder knows about this build. Stamped in
# so a firmware manifest can name the Echolot revision that produced it:
# the image has no git checkout and no config.yaml to read it back from,
# and "unknown" in a build record is the answer nobody can act on.
ARG BUILD_VERSION
ARG BUILD_REF
ARG BUILD_ARCH

ENV LANG=C.UTF-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ECHOLOT_VERSION=${BUILD_VERSION} \
    ECHOLOT_BUILD_REVISION=${BUILD_REF} \
    ECHOLOT_BUILD_ARCH=${BUILD_ARCH}

# ESPHome (used to compile per-device firmware in later phases) has a heavy
# native build chain, so we install it into a venv on top of the HA Debian
# base image rather than Alpine, where prebuilt wheels are far more reliable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        build-essential \
        libffi-dev \
        libssl-dev \
        git \
    && python3 -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH"

COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt

COPY app /opt/echolot/app
COPY rootfs /

RUN chmod a+x /etc/s6-overlay/s6-rc.d/echolot/run

WORKDIR /opt/echolot

LABEL \
    io.hass.name="Echolot" \
    io.hass.description="Self-hosted Wi-Fi CSI presence detection hub built on ESPectre" \
    io.hass.type="addon"
