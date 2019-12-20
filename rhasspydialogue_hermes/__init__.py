"""Hermes MQTT server for Rhasspy Dialogue Mananger"""
import asyncio
import json
import logging
import typing
from collections import deque, Mapping
from uuid import uuid4

import attr
from rhasspyhermes.base import Message
from rhasspyhermes.dialogue import (
    DialogueStartSession,
    DialogueEndSession,
    DialogueSessionStarted,
    DialogueSessionQueued,
    DialogueSessionEnded,
    DialogueContinueSession,
    DialogueIntentNotRecognized,
    DialogueActionType,
    DialogueNotification,
    DialogueAction,
    DialogueSessionTermination,
    DialogueSessionTerminationReason,
)
from rhasspyhermes.tts import TtsSay, TtsSayFinished
from rhasspyhermes.nlu import NluQuery, NluIntent, NluIntentNotRecognized
from rhasspyhermes.asr import AsrStartListening, AsrStopListening, AsrTextCaptured
from rhasspyhermes.wake import HotwordDetected

_LOGGER = logging.getLogger(__name__)

# -----------------------------------------------------------------------------


@attr.s
class SessionInfo:
    """Information for an activte or queued dialogue session."""

    sessionId: str = attr.ib()
    start_session: DialogueStartSession = attr.ib()
    customData: str = attr.ib(default="")
    intentFilter: typing.Optional[typing.List[str]] = attr.ib(default=None)
    sendIntentNotRecognized: bool = attr.ib(default=False)
    continue_session: typing.Optional[DialogueContinueSession] = attr.ib(default=None)


# -----------------------------------------------------------------------------

# TODO: Session timeouts
# TODO: Dialogue configure message
# TODO: Entity injection


class DialogueHermesMqtt:
    """Hermes MQTT server for Rhasspy Dialogue Manager."""

    def __init__(
        self,
        client,
        siteId: str = "default",
        wakewordIds: typing.Optional[typing.List[str]] = None,
        loop=None,
    ):
        self.client = client
        self.siteId = siteId
        self.loop = loop or asyncio.get_event_loop()

        self.session: typing.Optional[SessionInfo] = None
        self.session_queue = deque()

        self.wakeword_topics = {
            HotwordDetected.topic(wakewordId=w): w for w in wakewordIds or []
        }

        # Set when TtsSayFinished comes back
        self.say_finished_event = asyncio.Event()
        self.say_finished_timeout: float = 10

    # -------------------------------------------------------------------------

    async def handle_start(self, start_session: DialogueStartSession):
        """Starts or queues a new dialogue session."""
        try:
            sessionId = f"{self.siteId}-{uuid4()}"
            new_session = SessionInfo(sessionId=sessionId, start_session=start_session)

            await self.start_session(new_session)
        except Exception:
            _LOGGER.exception("handle_start")

    async def start_session(self, new_session: SessionInfo):
        """Start a new session."""
        start_session = new_session.start_session

        if isinstance(start_session.init, Mapping):
            # Convert to object
            if start_session.init["type"] == DialogueActionType.NOTIFICATION:
                start_session.init = DialogueNotification(**start_session.init)
            else:
                start_session.init = DialogueAction(**start_session.init)

        if start_session.init.type == DialogueActionType.NOTIFICATION:
            # Notification session
            notification = start_session.init
            assert isinstance(notification, DialogueNotification)

            if not self.session:
                # Create new session just for TTS
                _LOGGER.debug("Starting new session (id=%s)", new_session.sessionId)
                self.session = new_session

            if notification.text:
                # Forward to TTS
                await self.say_and_wait(notification.text)

            # End notification session immedately
            _LOGGER.debug("Session ended nominally: %s", self.session.sessionId)
            await self.end_session(DialogueSessionTerminationReason.NOMINAL)
        else:
            # Action session
            action = start_session.init
            assert isinstance(action, DialogueAction)
            _LOGGER.debug("Starting new session (id=%s)", new_session.sessionId)

            new_session.customData = start_session.customData
            new_session.intentFilter = action.intentFilter
            new_session.sendIntentNotRecognized = action.sendIntentNotRecognized

            if self.session:
                # Existing session
                if action.canBeEnqueued:
                    # Queue session for later
                    self.session_queue.append(new_session)
                    self.publish(
                        DialogueSessionQueued(
                            sessionId=new_session.sessionId,
                            siteId=self.siteId,
                            customData=new_session.customData,
                        )
                    )
                else:
                    # Drop session
                    _LOGGER.warning("Session was dropped: %s", start_session)
            else:
                # Start new session
                _LOGGER.debug("Starting new session (id=%s)", new_session.sessionId)
                self.session = new_session

                if action.text:
                    # Forward to TTS
                    await self.say_and_wait(action.text)

                # Start ASR listening
                _LOGGER.debug("Listening for session %s", self.session.sessionId)
                self.publish(
                    AsrStartListening(
                        siteId=self.siteId, sessionId=new_session.sessionId
                    )
                )

        self.session = new_session
        self.publish(
            DialogueSessionStarted(
                siteId=self.siteId,
                sessionId=new_session.sessionId,
                customData=new_session.customData,
            )
        )

    async def handle_continue(self, continue_session: DialogueContinueSession):
        """Continue the existing session."""
        try:
            assert self.session, "No session"

            # Update fields
            self.session.customData = (
                continue_session.customData or self.session.customData
            )

            if self.session.intentFilter is not None:
                # Overwrite intent filter
                self.session.intentFilter = continue_session.intentFilter

            self.session.sendIntentNotRecognized = (
                continue_session.sendIntentNotRecognized
            )

            _LOGGER.debug("Continuing session %s", self.session.sessionId)
            if continue_session.text:
                # Forward to TTS
                await self.say_and_wait(continue_session.text)

            # Start ASR listening
            _LOGGER.debug("Listening for session %s", self.session.sessionId)
            self.publish(AsrStartListening())
        except Exception:
            _LOGGER.exception("handle_continue")

    async def handle_end(self, end_session: DialogueEndSession):
        """End the current session."""
        try:
            _LOGGER.debug("Session ended nominally: %s", self.session.sessionId)
            await self.end_session(DialogueSessionTerminationReason.NOMINAL)
        except Exception:
            _LOGGER.exception("handle_end")

    async def end_session(self, reason: DialogueSessionTerminationReason):
        """End current session and start queued session."""
        assert self.session, "No session"

        self.publish(
            DialogueSessionEnded(
                sessionId=self.session.sessionId,
                siteId=self.siteId,
                customData=self.session.customData,
                termination=DialogueSessionTermination(reason=reason),
            )
        )

        self.session = None

        # Check session queue
        if self.session_queue:
            _LOGGER.debug("Handling queued session")
            await self.start_session(self.session_queue.popleft())

    def handle_text_captured(self, text_captured: AsrTextCaptured):
        """Handle ASR text captured for session."""
        try:
            assert self.session, "No session"
            _LOGGER.debug("Received text: %s", text_captured.text)

            # Stop listening
            self.publish(
                AsrStopListening(siteId=self.siteId, sessionId=self.session.sessionId)
            )

            # Perform query
            self.publish(
                NluQuery(
                    input=text_captured.text,
                    intentFilter=self.session.intentFilter,
                    sessionId=self.session.sessionId,
                )
            )
        except Exception:
            _LOGGER.exception("handle_text_captured")

    def handle_recognized(self, recognition: NluIntent):
        """Intent successfully recognized."""
        try:
            assert self.session, "No session"
            _LOGGER.debug("Recognized %s", recognition)
        except Exception:
            _LOGGER.exception("handle_recognized")

    async def handle_not_recognized(self, not_recognized: NluIntentNotRecognized):
        """Failed to recognized intent."""
        try:
            assert self.session, "No session"

            _LOGGER.warning("No intent recognized")
            if self.session.sendIntentNotRecognized:
                # Client will handle
                self.publish(
                    DialogueIntentNotRecognized(
                        sessionId=self.session.sessionId,
                        customData=self.session.customData,
                        siteId=self.siteId,
                        input=not_recognized.input,
                    )
                )
            else:
                # End session
                self.end_session(DialogueSessionTerminationReason.INTENT_NOT_RECOGNIZED)
        except Exception:
            _LOGGER.exception("handle_not_recognized")

    async def handle_wake(self, wakeword_id: str, detected: HotwordDetected):
        """Wake word was detected."""
        try:
            _LOGGER.debug("Hotword detected: %s", wakeword_id)

            sessionId = f"{self.siteId}-{wakeword_id}-{uuid4()}"
            new_session = SessionInfo(
                sessionId=sessionId,
                start_session=DialogueStartSession(
                    siteId=self.siteId,
                    customData=wakeword_id,
                    init=DialogueAction(canBeEnqueued=False),
                ),
            )

            if self.session:
                # Jump the queue
                self.session_queue.appendleft(new_session)

                # Abort previous session
                await self.end_session(DialogueSessionTerminationReason.ABORTED_BY_USER)
            else:
                # Start new session
                await self.start_session(new_session)
        except Exception:
            _LOGGER.exception("handle_wake")

    # -------------------------------------------------------------------------

    def on_connect(self, client, userdata, flags, rc):
        """Connected to MQTT broker."""
        try:
            topics = [
                DialogueStartSession.topic(),
                DialogueContinueSession.topic(),
                DialogueEndSession.topic(),
                TtsSayFinished.topic(),
                NluIntent.topic(intent_name="#"),
                NluIntentNotRecognized.topic(),
                AsrTextCaptured.topic(),
            ] + list(self.wakeword_topics.keys())
            for topic in topics:
                self.client.subscribe(topic)
                _LOGGER.debug("Subscribed to %s", topic)
        except Exception:
            _LOGGER.exception("on_connect")

    def on_message(self, client, userdata, msg):
        """Received message from MQTT broker."""
        try:
            _LOGGER.debug("Received %s byte(s) on %s", len(msg.payload), msg.topic)
            if msg.topic == DialogueStartSession.topic():
                # Start session
                json_payload = json.loads(msg.payload)
                if not self._check_siteId(json_payload):
                    return

                # Run in event loop (for TTS)
                asyncio.run_coroutine_threadsafe(
                    self.handle_start(DialogueStartSession(**json_payload)), self.loop
                )
            elif msg.topic == DialogueContinueSession.topic():
                # Continue session
                json_payload = json.loads(msg.payload)
                if not self._check_siteId(json_payload):
                    return

                # Run in event loop (for TTS)
                asyncio.run_coroutine_threadsafe(
                    self.handle_continue(DialogueContinueSession(**json_payload)),
                    self.loop,
                )
            elif msg.topic == DialogueEndSession.topic():
                # End session
                json_payload = json.loads(msg.payload)
                if not self._check_siteId(json_payload):
                    return

                # Run outside event loop
                self.handle_end(DialogueEndSession(**json_payload))
            elif msg.topic == TtsSayFinished.topic():
                # TTS finished
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                # Signal event loop
                self.loop.call_soon_threadsafe(self.say_finished_event.set)
            elif msg.topic == AsrTextCaptured.topic():
                # Text captured
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                # Run outside event loop
                self.handle_text_captured(AsrTextCaptured(**json_payload))
            elif msg.topic.startswith(NluIntent.topic(intent_name="")):
                # Intent recognized
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                self.handle_recognized(NluIntent(**json_payload))
            elif msg.topic.startswith(NluIntentNotRecognized.topic()):
                # Intent recognized
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                # Run in event loop (for TTS)
                asyncio.run_coroutine_threadsafe(
                    self.handle_not_recognized(NluIntentNotRecognized(**json_payload)),
                    self.loop,
                )
            elif msg.topic in self.wakeword_topics:
                json_payload = json.loads(msg.payload)
                if not self._check_siteId(json_payload):
                    return

                wakeword_id = self.wakeword_topics[msg.topic]
                asyncio.run_coroutine_threadsafe(
                    self.handle_wake(wakeword_id, HotwordDetected(**json_payload)),
                    self.loop,
                )
        except Exception:
            _LOGGER.exception("on_message")

    # -------------------------------------------------------------------------

    def publish(self, message: Message, **topic_args):
        """Publish a Hermes message to MQTT."""
        try:
            _LOGGER.debug("-> %s", message)
            topic = message.topic(**topic_args)
            payload = json.dumps(attr.asdict(message))
            _LOGGER.debug("Publishing %s char(s) to %s", len(payload), topic)
            self.client.publish(topic, payload)
        except Exception:
            _LOGGER.exception("on_message")

    async def say_and_wait(self, text: str):
        """Send text to TTS system and wait for reply."""
        assert self.session, "No session"
        self.say_finished_event.clear()

        # Forward to TTS
        _LOGGER.debug("Say: %s", text)
        self.publish(
            TtsSay(siteId=self.siteId, sessionId=self.session.sessionId, text=text)
        )

        # Wait for finished response (with timeout)
        try:
            await asyncio.wait_for(
                self.say_finished_event.wait(), timeout=self.say_finished_timeout
            )
        except asyncio.TimeoutError:
            _LOGGER.exception("say_and_wait")

    # -------------------------------------------------------------------------

    def _check_siteId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        """True if payload siteId matches this siteId."""
        return json_payload.get("siteId", "default") == self.siteId

    def _check_sessionId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        """True if payload sessionId matches current sessionId."""
        if self.session:
            return json_payload.get("sessionId", "") == self.session.sessionId

        # No current session
        return False
