"""Hermes MQTT server for Rhasspy Dialogue Mananger"""
import asyncio
import logging
import typing
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from rhasspyhermes.asr import (
    AsrStartListening,
    AsrStopListening,
    AsrTextCaptured,
    AsrToggleOff,
    AsrToggleOn,
    AsrToggleReason,
)
from rhasspyhermes.audioserver import AudioPlayBytes, AudioPlayFinished
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient, TopicArgs
from rhasspyhermes.dialogue import (
    DialogueAction,
    DialogueActionType,
    DialogueContinueSession,
    DialogueEndSession,
    DialogueError,
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
from rhasspyhermes.wake import (
    HotwordDetected,
    HotwordToggleOff,
    HotwordToggleOn,
    HotwordToggleReason,
)

from .utils import get_wav_duration

_LOGGER = logging.getLogger("rhasspydialogue_hermes")

# -----------------------------------------------------------------------------

StartSessionType = typing.Union[
    TtsSay,
    DialogueSessionStarted,
    DialogueSessionEnded,
    DialogueSessionQueued,
    AsrStartListening,
    AsrStopListening,
    HotwordToggleOff,
    DialogueError,
]

EndSessionType = typing.Union[
    TtsSay,
    DialogueSessionEnded,
    DialogueSessionStarted,
    DialogueSessionQueued,
    AsrStartListening,
    AsrStopListening,
    HotwordToggleOn,
    DialogueError,
]

SoundsType = typing.Union[
    typing.Tuple[AudioPlayBytes, TopicArgs],
    AsrToggleOff,
    HotwordToggleOff,
    AsrToggleOn,
    HotwordToggleOn,
]

# -----------------------------------------------------------------------------


@dataclass
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
    detected: typing.Optional[HotwordDetected] = None
    wakewordId: str = ""


# -----------------------------------------------------------------------------

# pylint: disable=W0511
# TODO: Dialogue configure message
# TODO: Entity injection


class DialogueHermesMqtt(HermesClient):
    """Hermes MQTT server for Rhasspy Dialogue Manager."""

    def __init__(
        self,
        client,
        siteIds: typing.Optional[typing.List[str]] = None,
        wakewordIds: typing.Optional[typing.List[str]] = None,
        sound_paths: typing.Optional[typing.Dict[str, Path]] = None,
        session_timeout: float = 30.0,
    ):
        super().__init__("rhasspydialogue_hermes", client, siteIds=siteIds)

        self.subscribe(
            DialogueStartSession,
            DialogueContinueSession,
            DialogueEndSession,
            TtsSayFinished,
            NluIntent,
            NluIntentNotRecognized,
            AsrTextCaptured,
            HotwordDetected,
            AudioPlayFinished,
        )

        self.session: typing.Optional[SessionInfo] = None
        self.session_queue: typing.Deque[SessionInfo] = deque()

        self.wakewordIds: typing.Set[str] = set(wakewordIds or [])
        self.sound_paths = sound_paths or {}

        # Session timeout
        self.session_timeout = session_timeout

        # Async events and ids for specific messages
        self.message_events: typing.Dict[
            typing.Type[Message], typing.Dict[str, asyncio.Event]
        ] = defaultdict(dict)

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

            async for start_result in self.start_session(new_session):
                yield start_result
        except Exception as e:
            _LOGGER.exception("handle_start")
            yield DialogueError(
                error=str(e), context=str(start_session), siteId=start_session.siteId
            )

    async def start_session(
        self, new_session: SessionInfo
    ) -> typing.AsyncIterable[typing.Union[StartSessionType, EndSessionType]]:
        """Start a new session."""
        start_session = new_session.start_session

        if start_session.init.type == DialogueActionType.NOTIFICATION:
            # Notification session
            notification = start_session.init
            assert isinstance(notification, DialogueNotification)

            if not self.session:
                # Create new session just for TTS
                _LOGGER.debug("Starting new session (id=%s)", new_session.sessionId)
                self.session = new_session
                yield DialogueSessionStarted(
                    siteId=new_session.siteId,
                    sessionId=new_session.sessionId,
                    customData=new_session.customData,
                )

            if notification.text:
                async for say_result in self.say(
                    notification.text,
                    siteId=new_session.siteId,
                    sessionId=new_session.sessionId,
                ):
                    yield say_result

            # End notification session immedately
            _LOGGER.debug("Session ended nominally: %s", self.session.sessionId)
            async for end_result in self.end_session(
                DialogueSessionTerminationReason.NOMINAL, siteId=new_session.siteId
            ):
                yield end_result
        else:
            # Action session
            action = start_session.init
            assert isinstance(action, DialogueAction)

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
                        siteId=new_session.siteId,
                        customData=new_session.customData,
                    )
                else:
                    # Drop session
                    _LOGGER.warning("Session was dropped: %s", start_session)
            else:
                # Start new session
                _LOGGER.debug("Starting new session (id=%s)", new_session.sessionId)
                self.session = new_session
                yield DialogueSessionStarted(
                    siteId=new_session.siteId,
                    sessionId=new_session.sessionId,
                    customData=new_session.customData,
                )

                if action.text:
                    # Forward to TTS
                    async for say_result in self.say(
                        action.text,
                        siteId=new_session.siteId,
                        sessionId=new_session.sessionId,
                    ):
                        yield say_result

                # Disable hotword
                yield HotwordToggleOff(
                    siteId=new_session.siteId,
                    reason=HotwordToggleReason.DIALOGUE_SESSION,
                )

                # Start ASR listening
                _LOGGER.debug("Listening for session %s", new_session.sessionId)
                sendAudioCaptured = True
                if (
                    new_session.detected
                    and new_session.detected.sendAudioCaptured is not None
                ):
                    # Use setting from hotword detection
                    sendAudioCaptured = new_session.detected.sendAudioCaptured

                yield AsrStartListening(
                    siteId=new_session.siteId,
                    sessionId=new_session.sessionId,
                    sendAudioCaptured=sendAudioCaptured,
                    wakewordId=new_session.wakewordId,
                )

            # Set up timer
            asyncio.create_task(self.handle_session_timeout(new_session.sessionId))

    async def handle_continue(
        self, continue_session: DialogueContinueSession
    ) -> typing.AsyncIterable[
        typing.Union[TtsSay, AsrStartListening, HotwordToggleOff, DialogueError]
    ]:
        """Continue the existing session."""
        if self.session is None:
            _LOGGER.warning("No session. Cannot continue.")
            return

        try:
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
                async for tts_result in self.say(
                    continue_session.text, siteId=self.session.siteId
                ):
                    yield tts_result

            # Disable hotword
            yield HotwordToggleOff(
                siteId=self.session.siteId, reason=HotwordToggleReason.DIALOGUE_SESSION
            )

            # Start ASR listening
            _LOGGER.debug("Listening for session %s", self.session.sessionId)
            yield AsrStartListening(
                siteId=self.session.siteId, sessionId=self.session.sessionId
            )
        except Exception as e:
            _LOGGER.exception("handle_continue")
            yield DialogueError(
                error=str(e),
                context=str(continue_session),
                siteId=self.session.siteId,
                sessionId=continue_session.sessionId,
            )

    async def handle_end(
        self, end_session: DialogueEndSession
    ) -> typing.AsyncIterable[typing.Union[EndSessionType, StartSessionType]]:
        """End the current session."""
        assert self.session is not None, "No session"

        try:
            # Update fields
            self.session.customData = end_session.customData or self.session.customData

            _LOGGER.debug("Session ended nominally: %s", self.session.sessionId)
            async for end_result in self.end_session(
                DialogueSessionTerminationReason.NOMINAL, siteId=self.session.siteId
            ):
                yield end_result

            if end_session.text:
                # Forward to TTS
                async for tts_result in self.say(
                    end_session.text, siteId=self.session.siteId
                ):
                    yield tts_result
        except Exception as e:
            _LOGGER.exception("handle_end")
            yield DialogueError(
                error=str(e),
                context=str(end_session),
                siteId=self.session.siteId,
                sessionId=end_session.sessionId,
            )
        finally:
            # Enable hotword
            yield HotwordToggleOn(
                siteId=self.session.siteId, reason=HotwordToggleReason.DIALOGUE_SESSION
            )

    async def end_session(
        self, reason: DialogueSessionTerminationReason, siteId: str
    ) -> typing.AsyncIterable[typing.Union[EndSessionType, StartSessionType, TtsSay]]:
        """End current session and start queued session."""
        assert self.session is not None, "No session"

        if (
            self.session.start_session.init.type != DialogueActionType.NOTIFICATION
        ) and (not self.session.text_captured):
            # Stop listening
            yield AsrStopListening(
                siteId=self.session.siteId, sessionId=self.session.sessionId
            )

        yield DialogueSessionEnded(
            siteId=siteId,
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

    async def handle_text_captured(
        self, text_captured: AsrTextCaptured
    ) -> typing.AsyncIterable[
        typing.Union[AsrStopListening, HotwordToggleOn, NluQuery]
    ]:
        """Handle ASR text captured for session."""
        try:
            if self.session is None:
                return

            _LOGGER.debug("Received text: %s", text_captured.text)

            # Record result
            self.session.text_captured = text_captured

            # Stop listening
            yield AsrStopListening(
                siteId=text_captured.siteId, sessionId=self.session.sessionId
            )

            # Enable hotword
            yield HotwordToggleOn(
                siteId=text_captured.siteId, reason=HotwordToggleReason.DIALOGUE_SESSION
            )

            # Perform query
            yield NluQuery(
                input=text_captured.text,
                intentFilter=self.session.intentFilter,
                sessionId=self.session.sessionId,
                siteId=self.session.siteId,
                wakewordId=text_captured.wakewordId or self.session.wakewordId,
            )
        except Exception:
            _LOGGER.exception("handle_text_captured")

    async def handle_recognized(self, recognition: NluIntent) -> None:
        """Intent successfully recognized."""
        try:
            if self.session is None:
                return

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
            if self.session is None:
                return

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
        self, wakewordId: str, detected: HotwordDetected
    ) -> typing.AsyncIterable[
        typing.Union[EndSessionType, StartSessionType, SoundsType]
    ]:
        """Wake word was detected."""
        try:
            sessionId = (
                detected.sessionId or f"{detected.siteId}-{wakewordId}-{uuid4()}"
            )
            new_session = SessionInfo(
                sessionId=sessionId,
                siteId=detected.siteId,
                start_session=DialogueStartSession(
                    siteId=detected.siteId,
                    customData=wakewordId,
                    init=DialogueAction(canBeEnqueued=False),
                ),
                detected=detected,
                wakewordId=wakewordId,
            )

            # Play wake sound before ASR starts listening
            async for play_wake_result in self.maybe_play_sound(
                "wake", siteId=detected.siteId, block=True
            ):
                yield play_wake_result

            if self.session:
                # Jump the queue
                self.session_queue.appendleft(new_session)

                # Abort previous session
                async for end_result in self.end_session(
                    DialogueSessionTerminationReason.ABORTED_BY_USER,
                    siteId=self.session.siteId,
                ):
                    yield end_result
            else:
                # Start new session
                async for start_result in self.start_session(new_session):
                    yield start_result
        except Exception as e:
            _LOGGER.exception("handle_wake")
            yield DialogueError(
                error=str(e), context=str(detected), siteId=detected.siteId
            )

    async def handle_session_timeout(self, sessionId: str):
        """Called when a session has timed out."""
        siteId = self.siteId

        try:
            # Pause execution until timeout
            await asyncio.sleep(self.session_timeout)

            # Check if we're still on the same session
            if self.session and self.session.sessionId == sessionId:
                _LOGGER.error("Session timed out: %s", sessionId)
                siteId = self.session.siteId

                # Abort session
                await self.publish_all(
                    self.end_session(
                        DialogueSessionTerminationReason.TIMEOUT, siteId=siteId
                    )
                )
        except Exception as e:
            _LOGGER.exception("session_timeout")
            self.publish(
                DialogueError(
                    error=str(e),
                    context="session_timeout",
                    siteId=siteId,
                    sessionId=sessionId,
                )
            )

    # -------------------------------------------------------------------------

    async def on_message(
        self,
        message: Message,
        siteId: typing.Optional[str] = None,
        sessionId: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        if isinstance(message, AsrTextCaptured):
            # ASR transcription received
            if not self.valid_sessionId(message.sessionId):
                _LOGGER.warning("Ignoring unknown session %s", message.sessionId)
                return

            async for text_result in self.handle_text_captured(message):
                yield text_result

            async for play_recorded_result in self.maybe_play_sound(
                "recorded", siteId=message.siteId
            ):
                yield play_recorded_result
        elif isinstance(message, AudioPlayFinished):
            # Audio output finished
            play_finished_event = self.message_events[AudioPlayFinished].get(message.id)
            if play_finished_event:
                play_finished_event.set()
        elif isinstance(message, DialogueStartSession):
            # Start session
            async for start_result in self.handle_start(message):
                yield start_result
        elif isinstance(message, DialogueContinueSession):
            # Continue session
            async for continue_result in self.handle_continue(message):
                yield continue_result
        elif isinstance(message, DialogueEndSession):
            # End session
            async for end_result in self.handle_end(message):
                yield end_result
        elif isinstance(message, HotwordDetected):
            # Wakeword detected
            assert topic, "Missing topic"
            wakewordId = HotwordDetected.get_wakewordId(topic)
            if (not self.wakewordIds) or (wakewordId in self.wakewordIds):
                async for wake_result in self.handle_wake(wakewordId, message):
                    yield wake_result
            else:
                _LOGGER.warning("Ignoring wake word id=%s", wakewordId)
        elif isinstance(message, NluIntent):
            # Intent recognized
            await self.handle_recognized(message)
        elif isinstance(message, NluIntentNotRecognized):
            # Intent not recognized
            async for not_recognized_result in self.handle_not_recognized(message):
                yield not_recognized_result

            async for play_error_result in self.maybe_play_sound(
                "error", siteId=message.siteId
            ):
                yield play_error_result
        elif isinstance(message, TtsSayFinished):
            # Text to speech finished
            say_finished_event = self.message_events[TtsSayFinished].get(message.id)
            if say_finished_event:
                say_finished_event.set()
        else:
            _LOGGER.warning("Unexpected message: %s", message)

    # -------------------------------------------------------------------------

    async def say(
        self,
        text: str,
        siteId="default",
        sessionId="",
        requestId: typing.Optional[str] = None,
    ) -> typing.AsyncIterable[TtsSay]:
        """Send text to TTS system and wait for reply."""
        finished_event = asyncio.Event()
        finished_id = requestId or str(uuid4())
        self.message_events[TtsSayFinished][finished_id] = finished_event

        # Forward to TTS
        _LOGGER.debug("Say: %s", text)
        yield TtsSay(id=finished_id, siteId=siteId, sessionId=sessionId, text=text)

        # Wait for finished event
        await asyncio.wait_for(finished_event.wait(), self.say_finished_timeout)

        # Clean up
        self.message_events[TtsSayFinished].pop(finished_id, None)

    # -------------------------------------------------------------------------

    async def maybe_play_sound(
        self,
        sound_name: str,
        siteId: typing.Optional[str] = None,
        requestId: typing.Optional[str] = None,
        block: bool = False,
    ) -> typing.AsyncIterable[SoundsType]:
        """Play WAV sound through audio out if it exists."""
        siteId = siteId or self.siteId
        wav_path = self.sound_paths.get(sound_name)
        if wav_path:
            if not wav_path.is_file():
                _LOGGER.error("WAV does not exist: %s", str(wav_path))
                return

            _LOGGER.debug("Playing WAV %s", str(wav_path))
            wav_bytes = wav_path.read_bytes()

            requestId = requestId or str(uuid4())
            finished_event = asyncio.Event()
            finished_id = requestId
            self.message_events[AudioPlayFinished][finished_id] = finished_event

            # Disable ASR/hotword at site
            yield HotwordToggleOff(siteId=siteId, reason=HotwordToggleReason.PLAY_AUDIO)
            yield AsrToggleOff(siteId=siteId, reason=AsrToggleReason.PLAY_AUDIO)

            try:
                yield (
                    AudioPlayBytes(wav_bytes=wav_bytes),
                    {"siteId": siteId, "requestId": requestId},
                )

                # Wait for finished event or WAV duration
                if block:
                    wav_duration = get_wav_duration(wav_bytes)
                    await asyncio.wait_for(finished_event.wait(), timeout=wav_duration)
            except asyncio.TimeoutError:
                pass
            finally:
                # Re-enable ASR/hotword at site
                yield HotwordToggleOn(
                    siteId=siteId, reason=HotwordToggleReason.PLAY_AUDIO
                )
                yield AsrToggleOn(siteId=siteId, reason=AsrToggleReason.PLAY_AUDIO)

    # -------------------------------------------------------------------------

    def valid_sessionId(self, sessionId: str) -> bool:
        """True if payload sessionId matches current sessionId."""
        if self.session:
            return sessionId == self.session.sessionId

        # No current session
        return False
