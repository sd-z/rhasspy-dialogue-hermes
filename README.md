# Rhasspy Hermes Dialogue Manager

[![Continous Integration](https://github.com/rhasspy/rhasspy-dialogue-hermes/workflows/Tests/badge.svg)](https://github.com/rhasspy/rhasspy-dialogue-hermes/actions)
[![GitHub license](https://img.shields.io/github/license/rhasspy/rhasspy-dialogue-hermes.svg)](https://github.com/rhasspy/rhasspy-dialogue-hermes/blob/master/LICENSE)

Implements `hermes/dialogueManager` functionality from [Hermes protocol](https://docs.snips.ai/reference/hermes).

## Installation

```bash
$ git clone https://github.com/rhasspy/rhasspy-dialogue-hermes
$ cd rhasspy-dialogue-hermes
$ ./configure
$ make
$ make install
```

## Running

```bash
$ bin/rhasspy-dialogue-hermes <ARGS>
```

## Command-Line Options

```
usage: rhasspy-dialogue-hermes [-h] [--wakeword-id WAKEWORD_ID]
                               [--session-timeout SESSION_TIMEOUT]
                               [--sound SOUND SOUND] [--host HOST]
                               [--port PORT] [--username USERNAME]
                               [--password PASSWORD] [--tls]
                               [--tls-ca-certs TLS_CA_CERTS]
                               [--tls-certfile TLS_CERTFILE]
                               [--tls-keyfile TLS_KEYFILE]
                               [--tls-cert-reqs {CERT_REQUIRED,CERT_OPTIONAL,CERT_NONE}]
                               [--tls-version TLS_VERSION]
                               [--tls-ciphers TLS_CIPHERS] [--site-id SITE_ID]
                               [--debug] [--log-format LOG_FORMAT]

optional arguments:
  -h, --help            show this help message and exit
  --wakeword-id WAKEWORD_ID
                        Wakeword ID(s) to listen for (default=all)
  --session-timeout SESSION_TIMEOUT
                        Seconds before a dialogue session times out (default:
                        30)
  --sound SOUND SOUND   Add WAV id/path
  --host HOST           MQTT host (default: localhost)
  --port PORT           MQTT port (default: 1883)
  --username USERNAME   MQTT username
  --password PASSWORD   MQTT password
  --tls                 Enable MQTT TLS
  --tls-ca-certs TLS_CA_CERTS
                        MQTT TLS Certificate Authority certificate files
  --tls-certfile TLS_CERTFILE
                        MQTT TLS certificate file (PEM)
  --tls-keyfile TLS_KEYFILE
                        MQTT TLS key file (PEM)
  --tls-cert-reqs {CERT_REQUIRED,CERT_OPTIONAL,CERT_NONE}
                        MQTT TLS certificate requirements (default:
                        CERT_REQUIRED)
  --tls-version TLS_VERSION
                        MQTT TLS version (default: highest)
  --tls-ciphers TLS_CIPHERS
                        MQTT TLS ciphers to use
  --site-id SITE_ID     Hermes site id(s) to listen for (default: all)
  --debug               Print DEBUG messages to the console
  --log-format LOG_FORMAT
                        Python logger format
```
