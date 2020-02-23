FROM python:3.7-alpine

COPY requirements.txt /

RUN grep '^rhasspy-' /requirements.txt | \
    sed -e 's|=.\+|/archive/master.tar.gz|' | \
    sed 's|^|https://github.com/rhasspy/|' \
    > /requirements_rhasspy.txt

RUN pip install --no-cache-dir -r /requirements_rhasspy.txt
RUN pip install --no-cache-dir -r /requirements.txt

COPY rhasspydialogue_hermes/ /rhasspydialogue_hermes/
WORKDIR /

ENTRYPOINT ["python3", "-m", "rhasspydialogue_hermes"]