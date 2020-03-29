"""Hermes MQTT dialogue manager for Rhasspy"""
import argparse
import asyncio
import logging

import paho.mqtt.client as mqtt
import rhasspyhermes.cli as hermes_cli

from . import DialogueHermesMqtt

_LOGGER = logging.getLogger("rhasspydialogue_hermes")

# -----------------------------------------------------------------------------


def main():
    """Main method."""
    parser = argparse.ArgumentParser(prog="rhasspy-dialogue-hermes")
    parser.add_argument(
        "--wakewordId",
        action="append",
        help="Wakeword ID(s) to listen for (default=all)",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=30.0,
        help="Seconds before a dialogue session times out (default: 30)",
    )

    hermes_cli.add_hermes_args(parser)
    args = parser.parse_args()

    hermes_cli.setup_logging(args)
    _LOGGER.debug(args)

    try:
        # Listen for messages
        loop = asyncio.get_event_loop()
        client = mqtt.Client()
        hermes = DialogueHermesMqtt(
            client,
            siteIds=args.siteId,
            wakewordIds=args.wakewordId,
            session_timeout=args.session_timeout,
            loop=loop,
        )

        _LOGGER.debug("Connecting to %s:%s", args.host, args.port)
        hermes_cli.connect(client, args)
        client.loop_start()

        # Run event loop
        hermes.loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _LOGGER.debug("Shutting down")
        client.loop_stop()


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
