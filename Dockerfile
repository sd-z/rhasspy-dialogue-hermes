FROM ubuntu:eoan as build

ENV LANG C.UTF-8
ENV APPDIR=/usr/lib/rhasspy-dialogue-hermes

RUN apt-get update && \
    apt-get install --no-install-recommends --yes \
        python3 python3-dev python3-setuptools python3-pip python3-venv \
        build-essential

COPY Makefile requirements.txt ${APP_DIR}/
RUN cd ${APP_DIR} && \
    make install

# Strip binaries and shared libraries
RUN (find ${APP_DIR} -type f \( -name '*.so*' -or -executable \) -print0 | xargs -0 strip --strip-unneeded -- 2>/dev/null) || true

# -----------------------------------------------------------------------------

FROM ubuntu:eoan as run

ENV LANG C.UTF-8

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        python3 libpython3.7

ENV APP_DIR=/usr/lib/rhasspy-dialogue-hermes

# Copy virtual environment
COPY --from=build ${APP_DIR}/.venv/ ${APP_DIR}/.venv/

# Copy source
COPY bin/rhasspy-dialogue-hermes ${APP_DIR}/bin/
COPY rhasspydialogue_hermes/ ${APP_DIR}/rhasspydialogue_hermes/

ENTRYPOINT ["bash", "/usr/bin/rhasspy-dialogue-hermes/bin/rhasspy-dialogue-hermes"]
