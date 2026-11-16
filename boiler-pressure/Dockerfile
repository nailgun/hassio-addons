ARG BUILD_FROM
FROM ${BUILD_FROM}

COPY rootfs /
RUN pip install --no-cache-dir --only-binary=:all: -r /usr/src/requirements.txt
