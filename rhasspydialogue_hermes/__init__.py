"""Hermes MQTT server for Rhasspy Dialogue Mananger"""
import asyncio
import json
import logging
import typing
from collections import deque
from collections.abc import Mapping
from uuid import uuid4

import attr
from rhasspyhermes.asr import AsrStartListening, AsrStopListening, AsrTextCaptured
from rhasspyhermes.base import Message
from rhasspyhermes.dialogue import (
    DialogueAction,
    DialogueActionType,
    DialogueContinueSession,
    DialogueEndSession,
    DialogueIntentNotRecognized,
    DialogueNotification,
    DialogueSessionEnded,
    DialogueSessionQueued,
    DialogueSessionStarted,
    DialogueSessionTermination,
    DialogueSessionTerminationReason,
    DialogueStartSession,
)
from rhasspyhermes.nlu import NluIntent, NluIntentNotRecognized, NluQuery
from rhasspyhermes.tts import TtsSay, TtsSayFinished
from rhasspyhermes.wake import HotwordDetected, HotwordToggleOff, HotwordToggleOn

_LOGGER = logging.getLogger(__name__)

# -----------------------------------------------------------------------------

StartSessionType = typing.Union[
    TtsSay,
    DialogueSessionStarted,
    DialogueSessionEnded,
    DialogueSessionQueued,
    AsrStartListening,
    AsrStopListening,
    HotwordToggleOff,
]

EndSessionType = typing.Union[
    TtsSay,
    DialogueSessionEnded,
    DialogueSessionStarted,
    DialogueSessionQueued,
    AsrStartListening,
    AsrStopListening,
    HotwordToggleOn,
]

# -----------------------------------------------------------------------------


@attr.s(auto_attribs=True, slots=True)
class SessionInfo:
    """Information for an active or queued dialogue session."""

    sessionId: str
    siteId: str
    start_session: DialogueStartSession
    customData: str = ""
    intentFilter: typing.Optional[typing.List[str]] = None
    sendIntentNotRecognized: bool = False
    continue_session: typing.Optional[DialogueContinueSession] = None
    text_captured: typing.Optional[AsrTextCaptured] = None

    # Wake word that activated this session (if any)


# -----------------------------------------------------------------------------

# pylint: disable=W0511
# TODO: Session timeouts
# TODO: Dialogue configure message
# TODO: Entity injection


class DialogueHermesMqtt:
    """Hermes MQTT server for Rhasspy Dialogue Manager."""

    def __init__(
        self,
        client,
        siteIds: typing.Optional[typing.List[str]] = None,
        wakewordIds: typing.Optional[typing.List[str]] = None,
        loop=None,
    ):
        self.client = client
        self.siteIds = siteIds or []
        self.loop = loop or asyncio.get_event_loop()

        self.session: typing.Optional[SessionInfo] = None
        self.session_queue: typing.Deque[SessionInfo] = deque()

        self.wakeword_topics = {
            HotwordDetected.topic(wakewordId=w): w for w in wakewordIds or []
        }

        # Set when TtsSayFinished comes back
        self.say_finished_event = asyncio.Event()
        self.say_finished_id: str = ""
        self.say_finished_timeout: float = 10

    # -------------------------------------------------------------------------

    async def handle_start(
        self, start_session: DialogueStartSession
    ) -> typing.AsyncIterable[typing.Union[StartSessionType, EndSessionType]]:
        """Starts or queues a new dialogue session."""
        try:
            sessionId = str(uuid4())
            new_session = SessionInfo(
                sessionId=sessionId,
                siteId=start_session.siteId,
                start_session=start_session,
            )

            async for start_result in self.start_session(
                new_session, siteId=start_session.siteId
            ):
                yield start_result
        except Exception:
            _LOGGER.exception("handle_start")

    async def start_session(
        self, new_session: SessionInfo, siteId: str = "default"
    ) -> typing.AsyncIterable[typing.Union[StartSessionType, EndSessionType]]:
        """Start a new session."""
        start_session = new_session.start_session

        if isinstance(start_session.init, Mapping):
            # Convert to object
            if start_session.init["type"] == DialogueActionType.NOTIFICATION:
                start_session.init = DialogueNotification(
                    text=start_session.init["text"]
                )
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
                async for tts_result in self.say_and_wait(
                    notification.text, siteId=siteId
                ):
                    yield tts_result

            # End notification session immedately
            _LOGGER.debug("Session ended nominally: %s", self.session.sessionId)
            async for end_result in self.end_session(
                DialogueSessionTerminationReason.NOMINAL, siteId=siteId
            ):
                yield end_result
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
                    yield DialogueSessionQueued(
                        sessionId=new_session.sessionId,
                        siteId=siteId,
                        customData=new_session.customData,
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
                    async for tts_result in self.say_and_wait(
                        action.text, siteId=siteId
                    ):
                        yield tts_result

                # Disable hotword
                yield HotwordToggleOff(siteId=siteId)

                # Start ASR listening
                _LOGGER.debug("Listening for session %s", new_session.sessionId)
                yield AsrStartListening(siteId=siteId, sessionId=new_session.sessionId)

        self.session = new_session
        yield DialogueSessionStarted(
            siteId=siteId,
            sessionId=new_session.sessionId,
            customData=new_session.customData,
        )

    async def handle_continue(
        self, continue_session: DialogueContinueSession
    ) -> typing.AsyncIterable[
        typing.Union[TtsSay, AsrStartListening, HotwordToggleOff]
    ]:
        """Continue the existing session."""
        try:
            assert self.session is not None

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
                async for tts_result in self.say_and_wait(
                    continue_session.text, siteId=self.session.siteId
                ):
                    yield tts_result

            # Disable hotword
            yield HotwordToggleOff(siteId=self.session.siteId)

            # Start ASR listening
            _LOGGER.debug("Listening for session %s", self.session.sessionId)
            yield AsrStartListening(
                siteId=self.session.siteId, sessionId=self.session.sessionId
            )
        except Exception:
            _LOGGER.exception("handle_continue")

    async def handle_end(
        self, end_session: DialogueEndSession
    ) -> typing.AsyncIterable[typing.Union[EndSessionType, StartSessionType]]:
        """End the current session."""
        assert self.session is not None

        try:
            _LOGGER.debug("Session ended nominally: %s", self.session.sessionId)
            async for end_result in self.end_session(
                DialogueSessionTerminationReason.NOMINAL, siteId=self.session.siteId
            ):
                yield end_result
        except Exception:
            _LOGGER.exception("handle_end")
        finally:
            # Enable hotword
            yield HotwordToggleOn(siteId=self.session.siteId)

    async def end_session(
        self, reason: DialogueSessionTerminationReason, siteId: str = "default"
    ) -> typing.AsyncIterable[typing.Union[EndSessionType, StartSessionType]]:
        """End current session and start queued session."""
        assert self.session, "No session"

        if (
            self.session.start_session.init.type != DialogueActionType.NOTIFICATION
        ) and (not self.session.text_captured):
            # Stop listening
            yield AsrStopListening(siteId=siteId, sessionId=self.session.sessionId)

        yield DialogueSessionEnded(
            sessionId=self.session.sessionId,
            customData=self.session.customData,
            termination=DialogueSessionTermination(reason=reason),
        )

        self.session = None

        # Check session queue
        if self.session_queue:
            _LOGGER.debug("Handling queued session")
            async for start_result in self.start_session(self.session_queue.popleft()):
                yield start_result

    def handle_text_captured(
        self, text_captured: AsrTextCaptured
    ) -> typing.Iterable[typing.Union[AsrStopListening, HotwordToggleOn, NluQuery]]:
        """Handle ASR text captured for session."""
        try:
            assert self.session, "No session"
            _LOGGER.debug("Received text: %s", text_captured.text)

            # Record result
            self.session.text_captured = text_captured

            # Stop listening
            yield AsrStopListening(
                siteId=text_captured.siteId, sessionId=self.session.sessionId
            )

            # Enable hotword
            yield HotwordToggleOn(siteId=text_captured.siteId)

            # Perform query
            yield NluQuery(
                input=text_captured.text,
                intentFilter=self.session.intentFilter,
                sessionId=self.session.sessionId,
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

    async def handle_not_recognized(
        self, not_recognized: NluIntentNotRecognized
    ) -> typing.AsyncIterable[
        typing.Union[DialogueIntentNotRecognized, EndSessionType, StartSessionType]
    ]:
        """Failed to recognized intent."""
        try:
            assert self.session, "No session"

            _LOGGER.warning("No intent recognized")
            if self.session.sendIntentNotRecognized:
                # Client will handle
                yield DialogueIntentNotRecognized(
                    sessionId=self.session.sessionId,
                    customData=self.session.customData,
                    siteId=not_recognized.siteId,
                    input=not_recognized.input,
                )

            # End session
            async for end_result in self.end_session(
                DialogueSessionTerminationReason.INTENT_NOT_RECOGNIZED,
                siteId=not_recognized.siteId,
            ):
                yield end_result
        except Exception:
            _LOGGER.exception("handle_not_recognized")

    async def handle_wake(
        self, wakeword_id: str, detected: HotwordDetected
    ) -> typing.AsyncIterable[typing.Union[EndSessionType, StartSessionType]]:
        """Wake word was detected."""
        try:
            _LOGGER.debug("Hotword detected: %s", wakeword_id)

            sessionId = (
                detected.sessionId or f"{detected.siteId}-{wakeword_id}-{uuid4()}"
            )
            new_session = SessionInfo(
                sessionId=sessionId,
                siteId=detected.siteId,
                start_session=DialogueStartSession(
                    siteId=detected.siteId,
                    customData=wakeword_id,
                    init=DialogueAction(canBeEnqueued=False),
                ),
            )

            if self.session:
                # Jump the queue
                self.session_queue.appendleft(new_session)

                # Abort previous session
                async for end_result in self.end_session(
                    DialogueSessionTerminationReason.ABORTED_BY_USER,
                    siteId=detected.siteId,
                ):
                    yield end_result
            else:
                # Start new session
                async for start_result in self.start_session(new_session):
                    yield start_result
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
                NluIntent.topic(intentName="#"),
                NluIntentNotRecognized.topic(),
                AsrTextCaptured.topic(),
            ] + list(self.wakeword_topics)

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
                self.publish_all(
                    self.handle_start(DialogueStartSession.from_dict(json_payload))
                )
            elif msg.topic == DialogueContinueSession.topic():
                # Continue session
                json_payload = json.loads(msg.payload)
                if not self._check_siteId(json_payload):
                    return

                # Run in event loop (for TTS)
                self.publish_all(
                    self.handle_continue(
                        DialogueContinueSession.from_dict(json_payload)
                    )
                )
            elif msg.topic == DialogueEndSession.topic():
                # End session
                json_payload = json.loads(msg.payload)
                if not self._check_siteId(json_payload):
                    return

                # Run in event loop
                self.publish_all(
                    self.handle_end(DialogueEndSession.from_dict(json_payload))
                )
            elif msg.topic == TtsSayFinished.topic():
                # TTS finished
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                finished = TtsSayFinished.from_dict(json_payload)
                if finished.id == self.say_finished_id:
                    _LOGGER.debug("Recevied finished")
                    # Signal event loop
                    self.loop.call_soon_threadsafe(self.say_finished_event.set)
            elif msg.topic == AsrTextCaptured.topic():
                # Text captured
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                # Run outside event loop
                for message in self.handle_text_captured(
                    AsrTextCaptured.from_dict(json_payload)
                ):
                    self.publish(message)
            elif NluIntent.is_topic(msg.topic):
                # Intent recognized
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                # Run outside event loop
                # TODO: Do something here
                self.handle_recognized(NluIntent.from_dict(json_payload))
            elif msg.topic == NluIntentNotRecognized.topic():
                # Intent recognized
                json_payload = json.loads(msg.payload)
                if not self._check_sessionId(json_payload):
                    return

                # Run in event loop (for TTS)
                self.publish_all(
                    self.handle_not_recognized(
                        NluIntentNotRecognized.from_dict(json_payload)
                    )
                )
            elif msg.topic in self.wakeword_topics:
                json_payload = json.loads(msg.payload)
                if not self._check_siteId(json_payload):
                    return

                wakeword_id = self.wakeword_topics[msg.topic]
                self.publish_all(
                    self.handle_wake(
                        wakeword_id, HotwordDetected.from_dict(json_payload)
                    )
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

    def publish_all(self, async_generator: typing.AsyncIterable[Message]):
        """Publish all messages from an async generator"""
        asyncio.run_coroutine_threadsafe(
            self.async_publish_all(async_generator), self.loop
        )

    async def async_publish_all(self, async_generator: typing.AsyncIterable[Message]):
        """Enumerate all messages in an async generator publish them"""
        async for message in async_generator:
            self.publish(message)

    # -------------------------------------------------------------------------

    async def say_and_wait(
        self, text: str, siteId="default", requestId: typing.Optional[str] = None
    ) -> typing.AsyncIterable[TtsSay]:
        """Send text to TTS system and wait for reply."""
        assert self.session, "No session"
        self.say_finished_event.clear()
        self.say_finished_id = requestId or str(uuid4())

        # Forward to TTS
        _LOGGER.debug("Say: %s", text)
        yield TtsSay(
            id=self.say_finished_id,
            siteId=siteId,
            sessionId=self.session.sessionId,
            text=text,
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
        if self.siteIds:
            return json_payload.get("siteId", "default") in self.siteIds

        # All sites
        return True

    def _check_sessionId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        """True if payload sessionId matches current sessionId."""
        if self.session:
            return json_payload.get("sessionId", "") == self.session.sessionId

        # No current session
        return False
