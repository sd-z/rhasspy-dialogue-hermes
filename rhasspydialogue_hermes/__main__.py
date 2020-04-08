"""Hermes MQTT dialogue manager for Rhasspy"""
import argparse
import asyncio
import logging
import typing
from pathlib import Path

import paho.mqtt.client as mqtt
import rhasspyhermes.cli as hermes_cli

from . import DialogueHermesMqtt

_LOGGER = logging.getLogger("rhasspydialogue_hermes")

# -----------------------------------------------------------------------------


def main():
    """Main method."""
    parser = argparse.ArgumentParser(prog="rhasspy-dialogue-hermes")
    parser.add_argument(
        "--wakeword-id",
        action="append",
        help="Wakeword ID(s) to listen for (default=all)",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=30.0,
        help="Seconds before a dialogue session times out (default: 30)",
    )
    parser.add_argument("--sound", nargs=2, action="append", help="Add WAV id/path")

    hermes_cli.add_hermes_args(parser)
    args = parser.parse_args()

    hermes_cli.setup_logging(args)
    _LOGGER.debug(args)

    sound_paths: typing.Dict[str, Path] = {
        sound[0]: Path(sound[1]) for sound in args.sound or []
    }

    # Listen for messages
    client = mqtt.Client()
    hermes = DialogueHermesMqtt(
        client,
        site_ids=args.site_id,
        wakeword_ids=args.wakeword_id,
        session_timeout=args.session_timeout,
        sound_paths=sound_paths,
    )

    _LOGGER.debug("Connecting to %s:%s", args.host, args.port)
    hermes_cli.connect(client, args)
    client.loop_start()

    try:
        # Run event loop
        asyncio.run(hermes.handle_messages_async())
    except KeyboardInterrupt:
        pass
    finally:
        _LOGGER.debug("Shutting down")
        client.loop_stop()


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
