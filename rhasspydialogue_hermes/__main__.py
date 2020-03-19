"""Hermes MQTT dialogue manager for Rhasspy"""
import argparse
import asyncio
import logging

import paho.mqtt.client as mqtt

from . import DialogueHermesMqtt

_LOGGER = logging.getLogger(__name__)


def main():
    """Main method."""
    parser = argparse.ArgumentParser(prog="rhasspy-dialogue-hermes")
    parser.add_argument(
        "--wakewordId",
        action="append",
        default=["default"],
        help="Wakeword ID(s) to listen for (default=default)",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=30.0,
        help="Seconds before a dialogue session times out (default: 30)",
    )
    parser.add_argument(
        "--host", default="localhost", help="MQTT host (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=1883, help="MQTT port (default: 1883)"
    )
    parser.add_argument(
        "--siteId",
        action="append",
        help="Hermes siteId(s) to listen for (default: all)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print DEBUG messages to the console"
    )
    parser.add_argument(
        "--log-format",
        default="[%(levelname)s:%(asctime)s] %(name)s: %(message)s",
        help="Python logger format",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format=args.log_format)
    else:
        logging.basicConfig(level=logging.INFO, format=args.log_format)

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

        def on_disconnect(client, userdata, flags, rc):
            try:
                # Automatically reconnect
                _LOGGER.info("Disconnected. Trying to reconnect...")
                client.reconnect()
            except Exception:
                logging.exception("on_disconnect")

        # Connect
        client.on_connect = hermes.on_connect
        client.on_disconnect = on_disconnect
        client.on_message = hermes.on_message

        _LOGGER.debug("Connecting to %s:%s", args.host, args.port)
        client.connect(args.host, args.port)
        client.loop_start()

        # Run event loop
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _LOGGER.debug("Shutting down")
        client.loop_stop()


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
