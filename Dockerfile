FROM ubuntu:eoan as build

ENV LANG C.UTF-8

# IFDEF PROXY
#! RUN echo 'Acquire::http { Proxy "http://${PROXY}"; };' >> /etc/apt/apt.conf.d/01proxy
# ENDIF

RUN apt-get update && \
    apt-get install --no-install-recommends --yes \
        python3 python3-dev python3-setuptools python3-pip python3-venv \
        build-essential

ENV APP_DIR=/usr/lib/rhasspy-dialogue-hermes

COPY Makefile requirements.txt ${APP_DIR}/
COPY scripts/create-venv.sh ${APP_DIR}/scripts/

# IFDEF PYPI
#! ENV PIP_INDEX_URL=http://${PYPI}/simple/
#! ENV PIP_TRUSTED_HOST=${PYPI_HOST}
# ENDIF

RUN cd ${APP_DIR} && \
    make install

# -----------------------------------------------------------------------------

FROM ubuntu:eoan as run

ENV LANG C.UTF-8

# IFDEF PROXY
#! RUN echo 'Acquire::http { Proxy "http://${PROXY}"; };' >> /etc/apt/apt.conf.d/01proxy
# ENDIF

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        python3 libpython3.7

ENV APP_DIR=/usr/lib/rhasspy-dialogue-hermes

# Copy virtual environment
COPY --from=build ${APP_DIR}/.venv/ ${APP_DIR}/.venv/

# Copy source
COPY bin/rhasspy-dialogue-hermes ${APP_DIR}/bin/
COPY rhasspydialogue_hermes/ ${APP_DIR}/rhasspydialogue_hermes/

ENTRYPOINT ["bash", "/usr/lib/rhasspy-dialogue-hermes/bin/rhasspy-dialogue-hermes"]
