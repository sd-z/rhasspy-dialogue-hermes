SHELL := bash

.PHONY: reformat check dist install sdist deploy

all:

# -----------------------------------------------------------------------------
# Python
# -----------------------------------------------------------------------------

reformat:
	scripts/format-code.sh

check:
	scripts/check-code.sh

install:
	scripts/create-venv.sh

dist: sdist

sdist:
	python3 setup.py sdist

test:
	scripts/run-tests.sh

# -----------------------------------------------------------------------------
# Docker
# -----------------------------------------------------------------------------

deploy:
	docker login --username rhasspy --password "$$DOCKER_PASSWORD"
	docker buildx build . --platform linux/amd64,linux/arm/v7,linux/arm64 --tag rhasspy/rhasspy-dialogue-hermes --push
