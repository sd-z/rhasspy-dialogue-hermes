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
    DialogueConfigure,
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
    DialogueSessionStarted,
    DialogueSessionEnded,
    DialogueSessionQueued,
    AsrStartListening,
    AsrStopListening,
    HotwordToggleOff,
    DialogueError,
]

EndSessionType = typing.Union[
    DialogueSessionEnded,
    DialogueSessionStarted,
    DialogueSessionQueued,
    AsrStartListening,
    AsrStopListening,
    HotwordToggleOn,
    DialogueError,
]

SayType = typing.Union[
    TtsSay, AsrToggleOff, HotwordToggleOff, AsrToggleOn, HotwordToggleOn
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

    session_id: str
    site_id: str
    start_session: DialogueStartSession
    custom_data: typing.Optional[str] = None
    intent_filter: typing.Optional[typing.List[str]] = None
    send_intent_not_recognized: bool = False
    continue_session: typing.Optional[DialogueContinueSession] = None
    text_captured: typing.Optional[AsrTextCaptured] = None
    step: int = 0
    send_audio_captured: bool = True
    lang: typing.Optional[str] = None

    # Wake word that activated this session (if any)
    detected: typing.Optional[HotwordDetected] = None
    wakeword_id: str = ""


# -----------------------------------------------------------------------------

# pylint: disable=W0511
# TODO: Entity injection


class DialogueHermesMqtt(HermesClient):
    """Hermes MQTT server for Rhasspy Dialogue Manager."""

    def __init__(
        self,
        client,
        site_ids: typing.Optional[typing.List[str]] = None,
        wakeword_ids: typing.Optional[typing.List[str]] = None,
        sound_paths: typing.Optional[typing.Dict[str, Path]] = None,
        session_timeout: float = 30.0,
    ):
        super().__init__("rhasspydialogue_hermes", client, site_ids=site_ids)

        self.subscribe(
            DialogueStartSession,
            DialogueContinueSession,
            DialogueEndSession,
            DialogueConfigure,
            TtsSayFinished,
            NluIntent,
            NluIntentNotRecognized,
            AsrTextCaptured,
            HotwordDetected,
            AudioPlayFinished,
        )

        self.session: typing.Optional[SessionInfo] = None
        self.session_queue: typing.Deque[SessionInfo] = deque()

        self.wakeword_ids: typing.Set[str] = set(wakeword_ids or [])
        self.sound_paths = sound_paths or {}

        # Session timeout
        self.session_timeout = session_timeout

        # Async events and ids for specific messages
        self.message_events: typing.Dict[
            typing.Type[Message], typing.Dict[typing.Optional[str], asyncio.Event]
        ] = defaultdict(dict)

        self.say_finished_timeout: float = 10

        # Seconds added to sound timeout
        self.sound_timeout_extra: float = 0.25

        # Seconds to wait after ASR/hotword toggle off
        self.toggle_delay: float = 0

        # Intent filter applied to NLU queries by default
        self.default_intent_filter: typing.Optional[typing.List[str]] = None

    # -------------------------------------------------------------------------

    async def handle_start(
        self, start_session: DialogueStartSession
    ) -> typing.AsyncIterable[typing.Union[StartSessionType, EndSessionType, SayType]]:
        """Starts or queues a new dialogue session."""
        try:
            session_id = str(uuid4())
            new_session = SessionInfo(
                session_id=session_id,
                site_id=start_session.site_id,
                start_session=start_session,
            )

            async for start_result in self.start_session(new_session):
                yield start_result
        except Exception as e:
            _LOGGER.exception("handle_start")
            yield DialogueError(
                error=str(e), context=str(start_session), site_id=start_session.site_id
            )

    async def start_session(
        self, new_session: SessionInfo
    ) -> typing.AsyncIterable[typing.Union[StartSessionType, EndSessionType, SayType]]:
        """Start a new session."""
        start_session = new_session.start_session

        if start_session.init.type == DialogueActionType.NOTIFICATION:
            # Notification session
            notification = start_session.init
            assert isinstance(notification, DialogueNotification)

            if not self.session:
                # Create new session just for TTS
                _LOGGER.debug("Starting new session (id=%s)", new_session.session_id)
                self.session = new_session
                yield DialogueSessionStarted(
                    site_id=new_session.site_id,
                    session_id=new_session.session_id,
                    custom_data=new_session.custom_data,
                    lang=new_session.lang,
                )

            if notification.text:
                async for say_result in self.say(
                    notification.text,
                    site_id=new_session.site_id,
                    session_id=new_session.session_id,
                ):
                    yield say_result

            # End notification session immedately
            _LOGGER.debug("Session ended nominally: %s", self.session.session_id)
            async for end_result in self.end_session(
                DialogueSessionTerminationReason.NOMINAL, site_id=new_session.site_id
            ):
                yield end_result
        else:
            # Action session
            action = start_session.init
            assert isinstance(action, DialogueAction)

            new_session.custom_data = start_session.custom_data
            new_session.intent_filter = action.intent_filter
            new_session.send_intent_not_recognized = action.send_intent_not_recognized

            if self.session:
                # Existing session
                if action.can_be_enqueued:
                    # Queue session for later
                    self.session_queue.append(new_session)
                    yield DialogueSessionQueued(
                        session_id=new_session.session_id,
                        site_id=new_session.site_id,
                        custom_data=new_session.custom_data,
                    )
                else:
                    # Drop session
                    _LOGGER.warning("Session was dropped: %s", start_session)
            else:
                # Start new session
                _LOGGER.debug("Starting new session (id=%s)", new_session.session_id)
                self.session = new_session
                yield DialogueSessionStarted(
                    site_id=new_session.site_id,
                    session_id=new_session.session_id,
                    custom_data=new_session.custom_data,
                    lang=new_session.lang,
                )

                # Disable hotword for session
                yield HotwordToggleOff(
                    site_id=new_session.site_id,
                    reason=HotwordToggleReason.DIALOGUE_SESSION,
                )

                if action.text:
                    # Forward to TTS
                    async for say_result in self.say(
                        action.text,
                        site_id=new_session.site_id,
                        session_id=new_session.session_id,
                    ):
                        yield say_result

                # Start ASR listening
                _LOGGER.debug("Listening for session %s", new_session.session_id)
                if (
                    new_session.detected
                    and new_session.detected.send_audio_captured is not None
                ):
                    # Use setting from hotword detection
                    new_session.send_audio_captured = (
                        new_session.detected.send_audio_captured
                    )

                yield AsrStartListening(
                    site_id=new_session.site_id,
                    session_id=new_session.session_id,
                    send_audio_captured=new_session.send_audio_captured,
                    wakeword_id=new_session.wakeword_id,
                    lang=new_session.lang,
                )

            # Set up timeout
            asyncio.create_task(
                self.handle_session_timeout(new_session.session_id, new_session.step)
            )

    async def handle_continue(
        self, continue_session: DialogueContinueSession
    ) -> typing.AsyncIterable[
        typing.Union[AsrStartListening, AsrStopListening, SayType, DialogueError]
    ]:
        """Continue the existing session."""
        if self.session is None:
            _LOGGER.warning("No session. Cannot continue.")
            return

        try:
            if continue_session.custom_data is not None:
                # Overwrite custom data
                self.session.custom_data = continue_session.custom_data

            if continue_session.lang is not None:
                # Overwrite language
                self.session.lang = continue_session.lang

            self.session.intent_filter = continue_session.intent_filter

            self.session.send_intent_not_recognized = (
                continue_session.send_intent_not_recognized
            )

            self.session.step += 1

            _LOGGER.debug(
                "Continuing session %s (step=%s)",
                self.session.session_id,
                self.session.step,
            )

            # Stop listening
            yield AsrStopListening(
                site_id=self.session.site_id, session_id=self.session.session_id
            )

            # Ensure hotword is disabled for session
            yield HotwordToggleOff(
                site_id=self.session.site_id,
                reason=HotwordToggleReason.DIALOGUE_SESSION,
            )

            if continue_session.text:
                # Forward to TTS
                async for tts_result in self.say(
                    continue_session.text,
                    site_id=self.session.site_id,
                    session_id=continue_session.session_id,
                ):
                    yield tts_result

            # Start ASR listening
            _LOGGER.debug("Listening for session %s", self.session.session_id)
            yield AsrStartListening(
                site_id=self.session.site_id,
                session_id=self.session.session_id,
                send_audio_captured=self.session.send_audio_captured,
                lang=self.session.lang,
            )

            # Set up timeout
            asyncio.create_task(
                self.handle_session_timeout(self.session.session_id, self.session.step)
            )

        except Exception as e:
            _LOGGER.exception("handle_continue")
            yield DialogueError(
                error=str(e),
                context=str(continue_session),
                site_id=self.session.site_id,
                session_id=continue_session.session_id,
            )

    async def handle_end(
        self, end_session: DialogueEndSession
    ) -> typing.AsyncIterable[typing.Union[EndSessionType, StartSessionType, SayType]]:
        """End the current session."""
        assert self.session is not None, "No session"
        session = self.session

        try:
            # Say text before ending session
            if end_session.text:
                # Forward to TTS
                async for tts_result in self.say(
                    end_session.text,
                    site_id=session.site_id,
                    session_id=end_session.session_id,
                ):
                    yield tts_result

            # Update fields
            if end_session.custom_data is not None:
                session.custom_data = end_session.custom_data

            _LOGGER.debug("Session ended nominally: %s", session.session_id)
            async for end_result in self.end_session(
                DialogueSessionTerminationReason.NOMINAL, site_id=session.site_id
            ):
                yield end_result
        except Exception as e:
            _LOGGER.exception("handle_end")
            yield DialogueError(
                error=str(e),
                context=str(end_session),
                site_id=session.site_id,
                session_id=end_session.session_id,
            )

            # Enable hotword on error
            yield HotwordToggleOn(
                site_id=session.site_id, reason=HotwordToggleReason.DIALOGUE_SESSION
            )

    async def end_session(
        self, reason: DialogueSessionTerminationReason, site_id: str
    ) -> typing.AsyncIterable[typing.Union[EndSessionType, StartSessionType, SayType]]:
        """End current session and start queued session."""
        assert self.session is not None, "No session"
        session = self.session

        if session.start_session.init.type != DialogueActionType.NOTIFICATION:
            # Stop listening
            yield AsrStopListening(
                site_id=session.site_id, session_id=session.session_id
            )

        yield DialogueSessionEnded(
            site_id=site_id,
            session_id=session.session_id,
            custom_data=session.custom_data,
            termination=DialogueSessionTermination(reason=reason),
        )

        self.session = None

        # Check session queue
        if self.session_queue:
            _LOGGER.debug("Handling queued session")
            async for start_result in self.start_session(self.session_queue.popleft()):
                yield start_result
        else:
            # Enable hotword if no queued sessions
            yield HotwordToggleOn(
                site_id=session.site_id, reason=HotwordToggleReason.DIALOGUE_SESSION
            )

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
                site_id=text_captured.site_id, session_id=self.session.session_id
            )

            # Enable hotword
            yield HotwordToggleOn(
                site_id=text_captured.site_id,
                reason=HotwordToggleReason.DIALOGUE_SESSION,
            )

            # Perform query
            yield NluQuery(
                input=text_captured.text,
                intent_filter=self.session.intent_filter or self.default_intent_filter,
                session_id=self.session.session_id,
                site_id=self.session.site_id,
                wakeword_id=text_captured.wakeword_id or self.session.wakeword_id,
                lang=text_captured.lang or self.session.lang,
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
        typing.Union[
            DialogueIntentNotRecognized, EndSessionType, StartSessionType, SayType
        ]
    ]:
        """Failed to recognized intent."""
        try:
            if self.session is None or (
                self.session.session_id != not_recognized.session_id
            ):
                _LOGGER.warning("No session for %s", not_recognized.session_id)
                return

            _LOGGER.warning(
                "No intent recognized (session_id=%s)", not_recognized.session_id
            )

            if self.session.send_intent_not_recognized:
                # Client will handle
                yield DialogueIntentNotRecognized(
                    session_id=self.session.session_id,
                    custom_data=self.session.custom_data,
                    site_id=not_recognized.site_id,
                    input=not_recognized.input,
                )
            else:
                # End session automatically
                async for end_result in self.end_session(
                    DialogueSessionTerminationReason.INTENT_NOT_RECOGNIZED,
                    site_id=not_recognized.site_id,
                ):
                    yield end_result
        except Exception:
            _LOGGER.exception("handle_not_recognized")

    async def handle_wake(
        self, wakeword_id: str, detected: HotwordDetected
    ) -> typing.AsyncIterable[
        typing.Union[EndSessionType, StartSessionType, SayType, SoundsType]
    ]:
        """Wake word was detected."""
        try:
            session_id = (
                detected.session_id or f"{detected.site_id}-{wakeword_id}-{uuid4()}"
            )
            new_session = SessionInfo(
                session_id=session_id,
                site_id=detected.site_id,
                start_session=DialogueStartSession(
                    site_id=detected.site_id,
                    custom_data=wakeword_id,
                    init=DialogueAction(can_be_enqueued=False),
                ),
                detected=detected,
                wakeword_id=wakeword_id,
                lang=detected.lang,
            )

            # Play wake sound before ASR starts listening
            async for play_wake_result in self.maybe_play_sound(
                "wake", site_id=detected.site_id
            ):
                yield play_wake_result

            if self.session:
                # Jump the queue
                self.session_queue.appendleft(new_session)

                # Abort previous session
                async for end_result in self.end_session(
                    DialogueSessionTerminationReason.ABORTED_BY_USER,
                    site_id=self.session.site_id,
                ):
                    yield end_result
            else:
                # Start new session
                async for start_result in self.start_session(new_session):
                    yield start_result
        except Exception as e:
            _LOGGER.exception("handle_wake")
            yield DialogueError(
                error=str(e), context=str(detected), site_id=detected.site_id
            )

    async def handle_session_timeout(self, session_id: str, step: int):
        """Called when a session has timed out."""
        site_id = self.site_id

        try:
            # Pause execution until timeout
            await asyncio.sleep(self.session_timeout)

            # Check if we're still on the same session and step (i.e., no continues)
            if (
                self.session
                and (self.session.session_id == session_id)
                and (self.session.step == step)
            ):
                _LOGGER.error("Session timed out: %s", session_id)
                site_id = self.session.site_id

                # Abort session
                await self.publish_all(
                    self.end_session(
                        DialogueSessionTerminationReason.TIMEOUT, site_id=site_id
                    )
                )
        except Exception as e:
            _LOGGER.exception("session_timeout")
            self.publish(
                DialogueError(
                    error=str(e),
                    context="session_timeout",
                    site_id=site_id,
                    session_id=session_id,
                )
            )

    def handle_configure(self, configure: DialogueConfigure):
        """Set default intent filter."""
        self.default_intent_filter = [
            intent.intent_id for intent in configure.intents if intent.enable
        ]

        if self.default_intent_filter:
            _LOGGER.debug("Default intent filter set: %s", self.default_intent_filter)
        else:
            self.default_intent_filter = None
            _LOGGER.debug("Removed default intent filter")

    # -------------------------------------------------------------------------

    async def on_message(
        self,
        message: Message,
        site_id: typing.Optional[str] = None,
        session_id: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        if isinstance(message, AsrTextCaptured):
            # ASR transcription received
            if (not message.session_id) or (
                not self.valid_session_id(message.session_id)
            ):
                _LOGGER.warning("Ignoring unknown session %s", message.session_id)
                return

            async for play_recorded_result in self.maybe_play_sound(
                "recorded", site_id=message.site_id
            ):
                yield play_recorded_result

            async for text_result in self.handle_text_captured(message):
                yield text_result

        elif isinstance(message, AudioPlayFinished):
            # Audio output finished
            play_finished_event = self.message_events[AudioPlayFinished].get(message.id)
            if play_finished_event:
                play_finished_event.set()
        elif isinstance(message, DialogueConfigure):
            # Configure intent filter
            self.handle_configure(message)
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
            wakeword_id = HotwordDetected.get_wakeword_id(topic)
            if (not self.wakeword_ids) or (wakeword_id in self.wakeword_ids):
                async for wake_result in self.handle_wake(wakeword_id, message):
                    yield wake_result
            else:
                _LOGGER.warning("Ignoring wake word id=%s", wakeword_id)
        elif isinstance(message, NluIntent):
            # Intent recognized
            await self.handle_recognized(message)
        elif isinstance(message, NluIntentNotRecognized):
            # Intent not recognized
            async for play_error_result in self.maybe_play_sound(
                "error", site_id=message.site_id
            ):
                yield play_error_result

            async for not_recognized_result in self.handle_not_recognized(message):
                yield not_recognized_result
        elif isinstance(message, TtsSayFinished):
            # Text to speech finished
            say_finished_event = self.message_events[TtsSayFinished].pop(
                message.id, None
            )
            if say_finished_event:
                say_finished_event.set()
        else:
            _LOGGER.warning("Unexpected message: %s", message)

    # -------------------------------------------------------------------------

    async def say(
        self,
        text: str,
        site_id="default",
        session_id="",
        request_id: typing.Optional[str] = None,
        block: bool = True,
    ) -> typing.AsyncIterable[
        typing.Union[
            TtsSay, HotwordToggleOn, HotwordToggleOff, AsrToggleOn, AsrToggleOff
        ]
    ]:
        """Send text to TTS system and wait for reply."""
        finished_event = asyncio.Event()
        finished_id = request_id or str(uuid4())
        self.message_events[TtsSayFinished][finished_id] = finished_event

        # Disable ASR/hotword at site
        yield HotwordToggleOff(site_id=site_id, reason=HotwordToggleReason.TTS_SAY)
        yield AsrToggleOff(site_id=site_id, reason=AsrToggleReason.TTS_SAY)

        # Wait for messages to be delivered
        await asyncio.sleep(self.toggle_delay)

        try:
            # Forward to TTS
            _LOGGER.debug("Say: %s", text)
            yield TtsSay(
                id=finished_id, site_id=site_id, session_id=session_id, text=text
            )

            if block:
                # Wait for finished event
                _LOGGER.debug(
                    "Waiting for sayFinished (timeout=%s)", self.say_finished_timeout
                )
                await asyncio.wait_for(
                    finished_event.wait(), timeout=self.say_finished_timeout
                )
        except asyncio.TimeoutError:
            _LOGGER.warning("Did not receive sayFinished before timeout")
        except Exception:
            _LOGGER.exception("say")
        finally:
            # Wait for audio to finish play
            await asyncio.sleep(self.toggle_delay)

            # Re-enable ASR/hotword at site
            yield HotwordToggleOn(site_id=site_id, reason=HotwordToggleReason.TTS_SAY)
            yield AsrToggleOn(site_id=site_id, reason=AsrToggleReason.TTS_SAY)

    # -------------------------------------------------------------------------

    async def maybe_play_sound(
        self,
        sound_name: str,
        site_id: typing.Optional[str] = None,
        request_id: typing.Optional[str] = None,
        block: bool = True,
    ) -> typing.AsyncIterable[SoundsType]:
        """Play WAV sound through audio out if it exists."""
        site_id = site_id or self.site_id
        wav_path = self.sound_paths.get(sound_name)
        if wav_path:
            if not wav_path.is_file():
                _LOGGER.error("WAV does not exist: %s", str(wav_path))
                return

            _LOGGER.debug("Playing WAV %s", str(wav_path))
            wav_bytes = wav_path.read_bytes()

            request_id = request_id or str(uuid4())
            finished_event = asyncio.Event()
            finished_id = request_id
            self.message_events[AudioPlayFinished][finished_id] = finished_event

            # Disable ASR/hotword at site
            yield HotwordToggleOff(
                site_id=site_id, reason=HotwordToggleReason.PLAY_AUDIO
            )
            yield AsrToggleOff(site_id=site_id, reason=AsrToggleReason.PLAY_AUDIO)

            # Wait for messages to be delivered
            await asyncio.sleep(self.toggle_delay)

            try:
                yield (
                    AudioPlayBytes(wav_bytes=wav_bytes),
                    {"site_id": site_id, "request_id": request_id},
                )

                # Wait for finished event or WAV duration
                if block:
                    wav_duration = get_wav_duration(wav_bytes)
                    wav_timeout = wav_duration + self.sound_timeout_extra
                    _LOGGER.debug("Waiting for playFinished (timeout=%s)", wav_timeout)
                    await asyncio.wait_for(finished_event.wait(), timeout=wav_timeout)
            except asyncio.TimeoutError:
                _LOGGER.warning("Did not receive sayFinished before timeout")
            except Exception:
                _LOGGER.exception("maybe_play_sound")
            finally:
                # Wait for audio to finish playing
                await asyncio.sleep(self.toggle_delay)

                # Re-enable ASR/hotword at site
                yield HotwordToggleOn(
                    site_id=site_id, reason=HotwordToggleReason.PLAY_AUDIO
                )
                yield AsrToggleOn(site_id=site_id, reason=AsrToggleReason.PLAY_AUDIO)

    # -------------------------------------------------------------------------

    def valid_session_id(self, session_id: str) -> bool:
        """True if payload session_id matches current session_id."""
        if self.session:
            return session_id == self.session.session_id

        # No current session
        return False
