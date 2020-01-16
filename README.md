# Rhasspy Hermes Dialogue Manager

Implements `hermes/dialogueManager` functionality from [Hermes protocol](https://docs.snips.ai/reference/hermes).

## Running With Docker

```bash
docker run -it rhasspy/rhasspy-dialogue-hermes:<VERSION> <ARGS>
```

## Building From Source

Clone the repository and create the virtual environment:

```bash
git clone https://github.com/rhasspy/rhasspy-dialogue-hermes.git
cd rhasspy-dialoguej-hermes
make venv
```

Run the `bin/rhasspy-dialoguej-hermes` script to access the command-line interface:

```bash
bin/rhasspy-dialoguej-hermes --help
```

## Building the Debian Package

Follow the instructions to build from source, then run:

```bash
source .venv/bin/activate
make debian
```

If successful, you'll find a `.deb` file in the `dist` directory that can be installed with `apt`.

## Building the Docker Image

Follow the instructions to build from source, then run:

```bash
source .venv/bin/activate
make docker
```

This will create a Docker image tagged `rhasspy/rhasspy-dialogue-hermes:<VERSION>` where `VERSION` comes from the file of the same name in the source root directory.

NOTE: If you add things to the Docker image, make sure to whitelist them in `.dockerignore`.

## Command-Line Options

```
usage: rhasspy-dialogue-hermes [-h] [--wakewordId WAKEWORDID] [--host HOST]
                              [--port PORT] [--siteId SITEID] [--debug]

optional arguments:
  -h, --help            show this help message and exit
  --wakewordId WAKEWORDID
                        Wakeword ID(s) to listen for (default=default)
  --host HOST           MQTT host (default: localhost)
  --port PORT           MQTT port (default: 1883)
  --siteId SITEID       Hermes siteId(s) to listen for (default: all)
  --debug               Print DEBUG messages to the console
  ```
