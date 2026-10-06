ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

COPY requirements.txt /requirements.txt
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --only-binary=:all: -r /requirements.txt

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
