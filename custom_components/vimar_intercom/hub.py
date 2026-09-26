"""Vimar Intercom Hub — manages SIP + media lifecycle."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from . import call_log as cl
from . import sip_client as sip
from . import media_handler as media
from . import system_messages as sm
from . import voicemail as vm
from .const import (
    APT_PARAMS_TIMEOUT,
    CAMERA_SWITCH_ADDRESS,
    DOOR_COMMAND_CURRENT,
    EVENT_MISSED_CALL,
    EVENT_RING,
    EVENT_VIDEO_MESSAGE,
    PICG_ADDRESS,
    PLANT_STATUS_TIMEOUT,
    REGISTRATION_DOWN_GRACE,
    RING_TIMEOUT,
)
from .phonebook import PhonebookError
from .plant_config import PlantConfig
from .runtime import RuntimeConfig, valid_door_command, valid_sip_token

_LOGGER = logging.getLogger(__name__)

STREAM_HANGUP_DELAY = 30
MAX_CALL_DURATION = 300  # 5 minutes — auto-hangup safety net
# How long an unload waits for the BYE before giving up on it. A reload
# must not be able to block on a socket the peer has stopped answering.
HANGUP_ON_UNLOAD_TIMEOUT = 5
REGISTRATION_WATCHDOG_INTERVAL = 30

# Downloads the phonebook the status reply names and parses it for the
# given apartment group. Supplied by `__init__.py`, which owns the HTTP
# session and the executor; raises on any failure.
PlantFetcher = Callable[[sm.InitStatus, str], Awaitable[PlantConfig]]
# Told about every newly downloaded plant configuration, with the one
# it replaces, so it can be persisted. Returns nothing.
PlantListener = Callable[[PlantConfig | None, PlantConfig], Awaitable[None]]


@dataclass
class ApartmentState:
    """The apartment's settings as the indoor unit last reported them.

    Everything is None until the unit has said: the entities built on
    these are unavailable rather than showing a guess.
    """

    dnd: bool | None = None
    voicemail: bool | None = None
    vm_timeout: int | None = None
    vm_timeout_values: tuple[int, ...] = ()
    # (messages stored, capacity) from the status reply's vm_level.
    vm_level: tuple[int, int] | None = None


@dataclass(frozen=True)
class Ring:
    """The INVITE ringing Home Assistant right now."""

    panel: str
    panel_name: str
    call_ids: tuple[str, ...]
    entry_id: int


class VimarIntercomHub:
    """Orchestrates SIP registration, calls, door control, and media."""

    def __init__(self, cfg: RuntimeConfig) -> None:
        """Store the runtime configuration and initialise the state."""
        self._cfg = cfg
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._ring_callbacks: list[Callable] = []
        self._state_callbacks: list[Callable] = []
        self._stream_viewers = 0
        self._hangup_task: asyncio.Task | None = None
        self._call_timeout_task: asyncio.Task | None = None
        self._keyframe_task: asyncio.Task | None = None
        self._auto_called = False
        self._auto_call_target: str | None = None
        # Fire-and-forget work started from callbacks. Kept so an unload
        # cannot leave an auto-call or a decline running against a SIP
        # layer that has just been reset.
        self._background: set[asyncio.Task] = set()
        self._raise_issue: Callable | None = None
        self._clear_issue: Callable | None = None
        self._hass = None
        self._entry_id = ""
        # Plant configuration sync. See `set_plant_sync`.
        self._plant: PlantConfig | None = None
        self._plant_fetcher: PlantFetcher | None = None
        self._plant_listener: PlantListener | None = None
        self._reload_entry: Callable[[], None] | None = None
        self._reload_pending = False
        self._status_waiter: asyncio.Future | None = None
        self._init_status: sm.InitStatus | None = None
        self._sync_task: asyncio.Task | None = None
        self._sync_again = False
        self._was_registered = False
        # The native-app features. See the "Apartment settings", "Rings
        # and the call log" and "Video messages" sections below.
        self._update_callbacks: list[Callable[[], None]] = []
        self._apt = ApartmentState()
        self._apt_waiters: dict[str, asyncio.Future] = {}
        self._call_log = cl.CallLog()
        self._save_call_log: Callable[[], None] | None = None
        self._ring: Ring | None = None
        self._ring_timeout: asyncio.TimerHandle | None = None
        self._grace_timers: dict[int, asyncio.TimerHandle] = {}
        self._switch_available = False
        self._video_messages: tuple[vm.VideoMessage, ...] | None = None
        self._mailbox_task: asyncio.Task | None = None
        self._mailbox_again = False
        self._new_message_pending = False

    @property
    def config(self) -> RuntimeConfig:
        """Runtime configuration for this hub."""
        return self._cfg

    def set_hass(self, hass, entry_id: str) -> None:
        """Give the hub the bus it fires ring events on."""
        self._hass = hass
        self._entry_id = entry_id

    def _panel_for(self, caller_uri: str) -> tuple[str, str]:
        """Map an incoming caller URI to a configured panel."""
        address = caller_uri.split("@")[0].removeprefix("sip:")
        for panel in self._cfg.panels:
            if panel.address == address:
                return panel.address, panel.name
        return address, address or "unknown"

    @staticmethod
    def _extension_of(uri: str) -> str:
        """The SIP extension in a URI, ignoring scheme, domain and port."""
        return uri.split("@")[0].removeprefix("sip:").strip()

    def _is_echo_of_our_call(self, caller_uri: str) -> bool:
        """True when this INVITE is the PBX calling us back for our call.

        After this client places a call the PBX INVITEs it back, and that
        arrives looking exactly like a doorbell press. The one thing that
        separates them is who is calling: the echo comes from the panel
        we dialled, which `_auto_call_target` (an extension) and
        `call_state["original_target"]` (a URI) both record. Anything
        else ringing while a call is up is a real visitor.

        A call this hub answered rather than placed has neither recorded,
        so nothing is treated as its echo — which is right: we never sent
        an INVITE for the PBX to reflect.
        """
        caller = self._extension_of(caller_uri)
        if not caller:
            return False
        ours = {
            self._extension_of(uri)
            for uri in (self._auto_call_target,
                        sip.call_state.get("original_target"))
            if uri
        }
        return caller in ours

    @property
    def registered(self) -> bool:
        return sip.is_registered()

    @property
    def in_call(self) -> bool:
        return sip.in_call

    def register_ring_callback(self, callback: Callable) -> None:
        self._ring_callbacks.append(callback)

    def unregister_ring_callback(self, callback: Callable) -> None:
        if callback in self._ring_callbacks:
            self._ring_callbacks.remove(callback)

    def register_state_callback(self, callback: Callable) -> None:
        """Register a callback for SIP state changes (registered, in_call)."""
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: Callable) -> None:
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def register_update_callback(self, callback: Callable[[], None]) -> None:
        """Be told when a native-app feature's state changes.

        Do-not-disturb, the answering machine, the ring, the call log,
        the mailbox and the camera switch all change on messages from
        the indoor unit rather than on the SIP state, so their entities
        listen here. One signal for all of them: each entity re-reads
        what it shows, and Home Assistant drops a write that changes
        nothing.
        """
        self._update_callbacks.append(callback)

    def unregister_update_callback(self, callback: Callable[[], None]) -> None:
        if callback in self._update_callbacks:
            self._update_callbacks.remove(callback)

    def _notify_update(self) -> None:
        for cb in list(self._update_callbacks):
            try:
                cb()
            except Exception:
                _LOGGER.exception("Update callback error")

    def _on_sip_state_change(self):
        """Called by sip_client when registered/in_call changes."""
        for cb in self._state_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("State callback error")
        registered = sip.is_registered()
        if registered and not self._was_registered and self._running:
            # Every fresh registration asks the indoor unit for its
            # status: after a restart, after an outage, after the cloud
            # moved us to another server. It costs one MESSAGE, and a
            # download only when the phonebook version has changed.
            self.request_plant_sync()
        self._was_registered = registered

    def set_issue_callbacks(self, raise_issue: Callable, clear_issue: Callable) -> None:
        """Install the callbacks used to raise and clear the repair issue."""
        self._raise_issue = raise_issue
        self._clear_issue = clear_issue

    async def _registration_watchdog(self) -> None:
        """Raise a repair issue when registration stays down too long."""
        down_since: float | None = None
        raised = False
        try:
            while self._running:
                await asyncio.sleep(REGISTRATION_WATCHDOG_INTERVAL)
                if sip.is_registered():
                    if raised and self._clear_issue:
                        self._clear_issue()
                        raised = False
                    down_since = None
                    continue
                if down_since is None:
                    down_since = time.monotonic()
                elif (not raised
                      and time.monotonic() - down_since >= REGISTRATION_DOWN_GRACE
                      and self._raise_issue):
                    self._raise_issue()
                    raised = True
        except asyncio.CancelledError:
            pass

    def _track(self, coro) -> asyncio.Task:
        """Run a coroutine in the background without losing track of it."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _schedule_delayed_hangup(self) -> None:
        """Start the grace period before dropping an unwatched auto-call."""
        if self._hangup_task:
            self._hangup_task.cancel()
        self._hangup_task = asyncio.create_task(self._delayed_hangup())

    def _clear_auto_call(self) -> None:
        """Forget that the live call was one this hub placed itself.

        Every path out of an auto-call comes through here. A stale
        `_auto_called` is not a cosmetic leak: `_handle_broadcast` reads
        it to tell a real doorbell press from the PBX echoing our own
        INVITE, so leaving it set declines every visitor with a 603 and
        fires no ring event until Home Assistant is restarted.
        """
        self._auto_called = False
        self._auto_call_target = None

    async def stream_opened(self, target: str | None = None):
        """Count a viewer, and place the call it needs.

        The count is incremented first and given back on any failure. It
        has to go up before the `_auto_called` guard below, and the whole
        of the rest of this may raise — a cancellation, or `_track`
        failing — so without the rollback the hub would keep counting a
        viewer that never arrived, and never hang up the call it placed
        for it. The caller must not call `stream_closed` for a
        `stream_opened` that raised.
        """
        self._stream_viewers += 1
        try:
            _LOGGER.debug("Stream opened (%d viewers)", self._stream_viewers)

            if self._hangup_task:
                self._hangup_task.cancel()
                self._hangup_task = None

            # `_auto_called` is part of the guard, not just a record: it is
            # set synchronously here, before any await, so a second viewer
            # arriving before the first auto-call task has even run sees a
            # call is already being placed. Without it both viewers call
            # `do_call`, the loser gets "Already in a call" and clears the
            # flag mid-call, and the echo INVITE becomes a phantom ring.
            if sip.in_call or sip.calling or self._auto_called:
                return

            if not sip.is_registered():
                return

            self._auto_called = True
            self._auto_call_target = target
            # Fire in the background — don't block the HTTP response.
            self._track(self._do_auto_call(target))
        except BaseException:
            self._stream_viewers = max(0, self._stream_viewers - 1)
            raise

    async def _do_auto_call(self, target: str | None):
        """Answer or place the call a newly opened stream needs.

        A panel that is ringing us right now already has an INVITE
        pending: answering it is what the user meant. Placing a second,
        outgoing INVITE to the default panel instead — which is what
        this used to do — is a call collision, and it is exactly what
        the README's own "snapshot on ring" automation triggers.
        """
        try:
            if sip.pending_incoming["active"]:
                ok, msg = await self._answer_pending()
            elif target:
                ok, msg = await sip.do_call(target=self._cfg.panel_uri(target))
            else:
                ok, msg = await sip.do_call()
            if not ok:
                _LOGGER.error("Auto-call failed: %s", msg)
                self._clear_auto_call()
            elif self._stream_viewers == 0:
                # The viewer gave up while the INVITE was still in
                # flight — the HTTP view waits 15 s, `do_call` allows
                # 45 s. Without this the call establishes with nobody
                # watching and runs to MAX_CALL_DURATION, holding the
                # account's single registration the whole time.
                self._schedule_delayed_hangup()
        except asyncio.CancelledError:
            self._clear_auto_call()
            raise
        except Exception as e:  # noqa: BLE001 - reported, never fatal
            _LOGGER.error("Auto-call error: %s", e)
            self._clear_auto_call()

    async def stream_closed(self):
        self._stream_viewers = max(0, self._stream_viewers - 1)
        _LOGGER.debug("Stream viewer disconnected (%d remaining)",
                      self._stream_viewers)

        if self._stream_viewers or not self._auto_called:
            return

        if sip.in_call:
            self._schedule_delayed_hangup()
        elif not sip.calling:
            # There is no call to hang up and none on its way: the
            # auto-call never established (the view gave up waiting), or
            # the panel ended it first. Nothing else will ever arrive to
            # clear the flag, so clear it here.
            self._clear_auto_call()

    async def _hangup_and_clear(self, hang_up: bool, reason: str) -> None:
        """End the call this hub placed, then forget it — whatever happens.

        `sip.send` raises whenever the SIP socket has gone, and that is
        exactly the state these timers fire in during an outage. An
        exception escaping here would kill the task before it reached
        `_clear_auto_call`, and a stale `_auto_called` declines every
        later visitor with a 603 — the very wedge that flag's single
        reset point exists to prevent.
        """
        try:
            if hang_up and sip.in_call:
                _LOGGER.info("%s; hanging up", reason)
                await sip.do_hangup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a BYE we cannot send is not fatal
            _LOGGER.debug("Hang-up after %s failed", reason, exc_info=True)
        finally:
            self._clear_auto_call()

    async def _delayed_hangup(self):
        try:
            await asyncio.sleep(STREAM_HANGUP_DELAY)
        except asyncio.CancelledError:
            # A viewer came back inside the grace period. The call stays
            # up and stays ours, so the flag stays set.
            return
        await self._hangup_and_clear(
            self._stream_viewers == 0 and self._auto_called,
            "no viewers left on the call this hub placed")

    def _start_call_timeout(self):
        """Start max call duration timer."""
        self._cancel_call_timeout()
        self._call_timeout_task = asyncio.create_task(self._call_timeout())

    def _cancel_call_timeout(self):
        if self._call_timeout_task:
            self._call_timeout_task.cancel()
            self._call_timeout_task = None

    async def _call_timeout(self):
        try:
            await asyncio.sleep(MAX_CALL_DURATION)
        except asyncio.CancelledError:
            # The call ended on its own; `call_ended` has already cleared
            # the flag.
            return
        await self._hangup_and_clear(
            True, f"the maximum call duration ({MAX_CALL_DURATION}s) was reached")

    def _start_keyframe_loop(self):
        """Send periodic keyframe requests during calls for video recovery."""
        self._cancel_keyframe_loop()
        self._keyframe_task = asyncio.create_task(self._keyframe_loop())

    def _cancel_keyframe_loop(self):
        if self._keyframe_task:
            self._keyframe_task.cancel()
            self._keyframe_task = None

    async def _keyframe_loop(self):
        """Aggressive keyframe bursts at start, then periodic requests."""
        try:
            # Immediate first request — no delay
            if sip.in_call:
                await sip.send_keyframe_request()
            # Rapid burst: 8 requests at 100ms intervals
            for i in range(8):
                await asyncio.sleep(0.1)
                if not sip.in_call:
                    return
                await sip.send_keyframe_request()
            # Then periodic for the rest of the call, whether or not a
            # viewer is attached: the registry caches every IDR that
            # arrives, and that cache is what a still is decoded from,
            # so stopping the requests would freeze the snapshot of a
            # call answered without the camera open.
            while sip.in_call:
                await asyncio.sleep(2)
                await sip.send_keyframe_request()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - one failed INFO is not fatal
            # Without this the task dies on the first send failure with
            # "Task exception was never retrieved" and the call gets no
            # further keyframe requests, so video never recovers.
            _LOGGER.debug("Keyframe request loop stopped early", exc_info=True)

    async def async_start(self):
        if self._running:
            return

        # The SIP and media layers keep their state in module globals,
        # which survive an entry reload. Start from a known-clean slate.
        sip.reset_state()

        sip.configure(self._cfg)
        media.configure(self._cfg)

        sip.init(self._handle_broadcast)
        sip.set_state_callback(self._on_sip_state_change)
        media.init(self._handle_broadcast)

        sip.MY_IP = sip.get_local_ip()
        sip.incoming_requests = asyncio.Queue()
        _LOGGER.info("Local IP: %s", sip.MY_IP)

        await media.setup_transports()
        _LOGGER.info("RTP transports ready")

        # The supervisor makes its own first connection attempt; connecting
        # here too would leak that first socket the moment the supervisor
        # opens its own.
        self._tasks.append(asyncio.create_task(sip.connection_supervisor()))
        self._tasks.append(asyncio.create_task(sip.request_processor()))
        self._tasks.append(asyncio.create_task(self._registration_watchdog()))
        self._running = True

    async def async_stop(self):
        self._running = False

        # Hang up before tearing the tasks down: after this the socket
        # is gone and no BYE can be sent, leaving the panel holding a
        # call it thinks is live and the account's single registration
        # occupied.
        #
        # Bounded, because nothing inside the hang-up is. `sip.send`
        # takes the connection lock and then awaits `writer.drain()`,
        # and on a half-open TCP connection — the socket is not closed,
        # the peer simply stops acknowledging — the reader loop's CRLF
        # keepalive is already blocked in its own drain while holding
        # that lock. The reader task is still alive, since this runs
        # before the cancel loop below, so the wait would never end and
        # the entry would sit in "unloading" forever: an option change
        # mid-call never reloads, and a Home Assistant restart hangs.
        if sip.in_call:
            try:
                await asyncio.wait_for(sip.do_hangup(), HANGUP_ON_UNLOAD_TIMEOUT)
            except Exception:  # noqa: BLE001 - shutdown must not fail here
                _LOGGER.debug("Hang-up on unload failed", exc_info=True)

        for t in list(self._background):
            t.cancel()
        self._background.clear()
        self._sync_task = None
        self._mailbox_task = None
        for waiter in self._apt_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._apt_waiters.clear()
        for timer in self._grace_timers.values():
            timer.cancel()
        self._grace_timers.clear()
        self._cancel_ring_timeout()
        self._ring = None
        self._switch_available = False
        if self._status_waiter and not self._status_waiter.done():
            self._status_waiter.cancel()
        self._status_waiter = None
        self._was_registered = False

        for t in self._tasks:
            t.cancel()
        if self._tasks:
            # Await teardown so no task (notably the registration watchdog,
            # which would otherwise sleep up to REGISTRATION_WATCHDOG_INTERVAL
            # seconds after self._running flips) outlives an entry reload.
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._hangup_task:
            self._hangup_task.cancel()
        self._cancel_call_timeout()
        self._cancel_keyframe_loop()
        await media.stop_media()
        media.close_transports()
        if sip.writer:
            try:
                sip.writer.close()
            except Exception:  # noqa: BLE001 - closing a dead socket may raise
                pass
        self._clear_auto_call()
        self._stream_viewers = 0
        _LOGGER.info("Hub stopped")

    async def async_reconnect(self) -> None:
        """Force the SIP connection to be rebuilt."""
        _LOGGER.info("Manual reconnect requested")
        sip.request_reconnect()

    async def async_call(self, target: str | None = None) -> tuple[bool, str]:
        self._clear_auto_call()
        if target:
            uri = self._cfg.panel_uri(target)
            return await sip.do_call(target=uri)
        return await sip.do_call()

    async def async_answer(self) -> tuple[bool, str]:
        return await self._answer_pending()

    async def _answer_pending(self) -> tuple[bool, str]:
        """Answer the ringing INVITE, and tell the other devices.

        The official app sends `C;<call id>;ANSWERED` to the apartment's
        intercom address when an incoming call connects, which is how
        the other phones of the house learn the visitor was taken. The
        call IDs are read before answering: answering clears them.
        """
        call_ids = tuple(sip.pending_incoming.get("call_ids") or ())
        ok, msg = await sip.do_answer_incoming()
        if ok:
            self._ring_over(self._call_log.answered_here())
            if call_ids:
                self._track(self._notify_answered(call_ids[0]))
        return ok, msg

    async def _notify_answered(self, call_id: str) -> None:
        sga = self.apartment_intercom
        if sga is None:
            _LOGGER.debug("No apartment intercom address in the phonebook; "
                          "not telling the other devices this call was answered")
            return
        try:
            body = sm.call_answered_command(call_id)
        except ValueError:
            _LOGGER.debug("The ring's call ID cannot be sent back; not "
                          "telling the other devices it was answered")
            return
        ok, msg = await sip.do_system_message(
            self._cfg.panel_uri(sga), body,
            extra_headers={sm.PANDA_HEADER: sm.PANDA_BLUE})
        if not ok:
            _LOGGER.debug("The answered notice was not accepted (%s)", msg)

    async def async_decline(self):
        """Refuse the ringing INVITE with 603 Decline.

        603 is a global refusal: the proxy stops the other devices of
        the house ringing too. That is what a Decline button is for; a
        visitor this client merely cannot take (a call already up) gets
        486 from the ring handler instead.
        """
        was_ringing = sip.pending_incoming["active"]
        await sip.do_decline_incoming()
        if was_ringing:
            self._ring_over(self._call_log.declined())

    async def async_hangup(self):
        self._clear_auto_call()
        self._cancel_call_timeout()
        await sip.do_hangup()

    def _door_target(
        self, target: str | None, command: str | None
    ) -> tuple[str, str]:
        """Resolve the door command to one (URI, body) pair, right now.

        Three cases:

        - An explicit target: OPEN_CURRENT to that panel, for plants with
          more than one entrance.
        - During a call: OPEN_CURRENT to the door relay group, which opens
          the relay of whichever panel is calling.
        - Otherwise: the configured door command, OPEN_2F by default, to
          the door relay group — the main entrance.

        The relay group comes from the QR, so the common case needs no
        configuration.

        This is deliberately a single cheap snapshot with no await in it.
        Choosing the branch and then awaiting a challenge/response round
        trip of up to 30 s before the next send used to mean the call
        could end in between — `OPEN_CURRENT` then went to the relay
        group with no current call — or start, sending the configured
        command to the main entrance while the user was talking to a side
        gate. Each attempt now takes its own snapshot immediately before
        it sends.
        """
        if target:
            return self._cfg.panel_uri(target), command or DOOR_COMMAND_CURRENT
        if sip.in_call:
            return self._cfg.door_uri, command or DOOR_COMMAND_CURRENT
        return self._cfg.door_uri, command or self._cfg.door_command

    async def async_door(
        self, target: str | None = None, command: str | None = None
    ) -> tuple[bool, str]:
        """Open a door. See `_door_target` for which one, and with what.

        An actuator from the phonebook comes through here too, as an
        explicit target and command — the SDK's `sysMsgActuatorAction`
        is exactly this MESSAGE, `Panda: command` with the command as
        the body, sent to the actuator's GID. Both are checked again
        before they reach the wire: they were checked when the phonebook
        was parsed, but this is the last point before a SIP URI and a
        MESSAGE body are built from them.
        """
        if target is not None and not valid_sip_token(target):
            return False, "That door is not a valid SIP extension."
        if command is not None and not valid_door_command(command):
            return False, "That door command is not a valid command."
        uri, body = self._door_target(target, command)
        _LOGGER.debug("Door command %s to %s (registered=%s)", body, uri, sip.is_registered())

        ok, msg = await sip.do_system_message(
            uri, body, extra_headers={"Panda": "command"})
        if ok:
            _LOGGER.info("Door %s opened", uri)
            return True, msg

        if not sip.is_registered():
            # Re-registering from here would call `connect()` outside the
            # supervisor, opening a TLS socket whose responses nobody
            # reads — one leaked connection per door press for as long as
            # the outage lasts, and a 15 s wait before failing anyway.
            _LOGGER.warning("Door command to %s failed (%s); not registered, "
                            "asking the supervisor to reconnect", uri, msg)
            sip.request_reconnect()
            return False, ("Not connected to the intercom. A reconnect has "
                           "been requested; try again in a few seconds.")

        if msg == sip.NO_RESPONSE:
            # Nothing came back, which does not mean nothing happened.
            # A MESSAGE that reached the panel and whose 200 OK was lost
            # looks exactly like one that never arrived, and resending it
            # pulses the relay a second time: the door opens, closes and
            # opens again, with the second opening unattended. Only an
            # explicit failure response says the command was not carried
            # out, so only that is retried.
            _LOGGER.warning(
                "Door command to %s got no response; not retrying, because "
                "the panel may have opened the door already", uri)
            return False, ("No reply from the intercom. The door may have "
                           "opened anyway; check before trying again.")

        _LOGGER.warning("Door command to %s failed (%s); retrying once", uri, msg)
        try:
            uri, body = self._door_target(target, command)
            ok, msg = await sip.do_system_message(
                uri, body, extra_headers={"Panda": "command"})
            if ok:
                _LOGGER.info("Door %s opened on retry", uri)
            else:
                _LOGGER.error("Door %s failed on retry: %s", uri, msg)
            return ok, msg
        except Exception as err:  # noqa: BLE001 - surfaced to the caller
            _LOGGER.error("Door retry error: %s", err)
            return False, str(err)

    # ─── Apartment settings ──────────────────────────────────────────
    #
    # Do-not-disturb and the answering machine are switched with a
    # `blue` MESSAGE to the apartment's intercom address (the SDK's
    # "SGA", the phonebook's MAGIC_APT_INTERCOM); the voicemail timeout
    # with a `set` SET_APT_PARAMS to the indoor unit, which confirms it.
    # Their state comes from the status reply and from the unit's own
    # notifications when any device changes them.

    @property
    def apartment(self) -> ApartmentState:
        """The apartment's settings as last reported."""
        return self._apt

    @property
    def apartment_intercom(self) -> str | None:
        """The apartment's intercom address, if the phonebook names one."""
        sga = self._plant.apartment_intercom if self._plant is not None else None
        return sga if sga and valid_sip_token(sga) else None

    def _apply_status(self, status: sm.InitStatus) -> None:
        self._apt.dnd = status.dnd
        self._apt.voicemail = status.voicemail
        self._apt.vm_timeout = status.vm_timeout
        self._apt.vm_timeout_values = status.vm_timeout_values or ()
        self._apt.vm_level = sm.parse_vm_level(status.vm_level)
        self._notify_update()

    async def async_set_dnd(self, on: bool) -> tuple[bool, str]:
        """Turn do-not-disturb on or off for the whole apartment."""
        return await self._set_apartment_switch(sm.dnd_command(on), "dnd", on)

    async def async_set_voicemail(self, on: bool) -> tuple[bool, str]:
        """Turn the answering machine on or off."""
        return await self._set_apartment_switch(
            sm.voicemail_command(on), "voicemail", on)

    async def _set_apartment_switch(
        self, body: str, attr: str, on: bool
    ) -> tuple[bool, str]:
        """Send a switch to the SGA; adopt the new state on its 200 OK.

        Optimistic on the 200, as the official app is: the unit sends
        no reply to these, only the notification other devices get, and
        whether it echoes that back to the sender is not known.
        """
        sga = self.apartment_intercom
        if sga is None:
            return False, ("The plant's phonebook names no apartment intercom "
                           "address, so this setting cannot be changed from "
                           "here.")
        ok, msg = await sip.do_system_message(
            self._cfg.panel_uri(sga), body,
            extra_headers={sm.PANDA_HEADER: sm.PANDA_BLUE})
        if ok:
            setattr(self._apt, attr, on)
            self._notify_update()
        return ok, msg

    async def async_set_vm_timeout(self, seconds: int) -> tuple[bool, str]:
        """Change how long the unit rings before the answering machine.

        Unlike the switches this one is confirmed: the unit answers
        SET_APT_PARAMS_REPLY with the request's MSGID and an error code,
        and only ERR_NONE is a success.
        """
        values = self._apt.vm_timeout_values
        if values and seconds not in values:
            return False, f"The indoor unit does not offer a {seconds} s timeout."
        msg_id = sm.new_message_id()
        try:
            body = sm.set_vm_timeout_command(msg_id, seconds)
        except ValueError as err:
            return False, str(err)
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._apt_waiters[msg_id] = waiter
        try:
            ok, msg = await sip.do_system_message(
                self._cfg.panel_uri(PICG_ADDRESS), body,
                extra_headers={sm.PANDA_HEADER: sm.PANDA_SET})
            if not ok:
                return False, msg
            reply: sm.AptParamsReply = await asyncio.wait_for(
                waiter, APT_PARAMS_TIMEOUT)
        except asyncio.TimeoutError:
            return False, "The indoor unit did not confirm the change."
        finally:
            self._apt_waiters.pop(msg_id, None)
        if not reply.ok:
            return False, ("The indoor unit refused the change "
                           f"({reply.error_code or 'no error code'}).")
        self._apt.vm_timeout = seconds
        self._notify_update()
        return True, "OK"

    def _on_dnd_line(self, line: str) -> None:
        self._apt.dnd = sm.parse_switch(line, sm.DND_PREFIX)
        self._notify_update()

    def _on_voicemail_line(self, line: str) -> None:
        self._apt.voicemail = sm.parse_switch(line, sm.VOICEMAIL_PREFIX)
        self._notify_update()

    def _on_apt_params_reply_line(self, line: str) -> None:
        reply = sm.parse_apt_params_reply(line)
        waiter = self._apt_waiters.get(reply.msg_id) if reply.msg_id else None
        if waiter is not None and not waiter.done():
            waiter.set_result(reply)

    def _on_apt_params_changed_line(self, line: str) -> None:
        change = sm.parse_apt_params_changed(line)
        if change.vm_timeout is not None:
            self._apt.vm_timeout = change.vm_timeout
            self._notify_update()

    # ─── Rings and the call log ──────────────────────────────────────
    #
    # A ring is on from the INVITE until Home Assistant answers or
    # declines it, the panel cancels it, or another device answers it.
    # Every ring goes into the call log (`call_log.py`), which also
    # takes the unit's MISSED_CALL reports.

    @property
    def ringing(self) -> Ring | None:
        """The ring in progress, or None."""
        return self._ring

    @property
    def call_log(self) -> cl.CallLog:
        """The local call log."""
        return self._call_log

    def set_call_log(self, log: cl.CallLog, save: Callable[[], None]) -> None:
        """Adopt the restored call log, and the way to persist it.

        `save` is Home Assistant's business (a delayed Store write), so
        it is handed in and the hub stays testable without it.
        """
        self._call_log = log
        self._save_call_log = save

    def _call_log_changed(self) -> None:
        if self._save_call_log is not None:
            try:
                self._save_call_log()
            except Exception:  # noqa: BLE001 - a failed save loses history only
                _LOGGER.exception("Could not save the call log")
        self._notify_update()

    def _panel_name(self, address: str) -> str:
        return self._panel_for(address)[1]

    @property
    def panel_names(self) -> dict[str, str]:
        """Extension to name, for every panel this installation knows."""
        return {panel.address: panel.name for panel in self._cfg.panels}

    def _start_ring(self, panel: str, name: str) -> None:
        call_ids = tuple(sip.pending_incoming.get("call_ids") or ())
        entry = self._call_log.ring(panel, name, call_ids, time.time())
        self._ring = Ring(panel, name, call_ids, entry.id)
        self._cancel_ring_timeout()
        self._ring_timeout = asyncio.get_running_loop().call_later(
            RING_TIMEOUT, self._on_ring_timeout)
        self._call_log_changed()

    def _cancel_ring_timeout(self) -> None:
        if self._ring_timeout is not None:
            self._ring_timeout.cancel()
            self._ring_timeout = None

    def _on_ring_timeout(self) -> None:
        self._ring_timeout = None
        if self._ring is None:
            return
        _LOGGER.debug("A ring nothing ended ran out after %ss", RING_TIMEOUT)
        self._ring_over(self._call_log.ring_ended(), grace=True)

    def _ring_over(self, entry: cl.CallEntry | None, grace: bool = False) -> None:
        """The ring ended. `entry` is its log entry, as the log returned it.

        With `grace`, the entry was left unanswered and is called missed
        after `call_log.UNANSWERED_GRACE` unless something says it was
        answered in the meantime.
        """
        self._ring = None
        self._cancel_ring_timeout()
        if not sip.in_call:
            # A CALL_INFO that came with an unanswered ring.
            self._switch_available = False
        if grace and entry is not None:
            self._grace_timers[entry.id] = asyncio.get_running_loop().call_later(
                cl.UNANSWERED_GRACE, self._finalize_ring, entry.id)
        self._call_log_changed()

    def _finalize_ring(self, entry_id: int) -> None:
        self._grace_timers.pop(entry_id, None)
        entry = self._call_log.finalize(entry_id)
        if entry is not None:
            self._fire_missed(entry)
            self._call_log_changed()

    def _sync_ring_with_log(self) -> None:
        """Drop the ring when the log says it is no longer ringing."""
        if self._ring is not None and self._call_log.ringing is None:
            self._ring = None
            self._cancel_ring_timeout()

    def _on_ring_ended(self, ended) -> None:
        if not getattr(ended, "ours", False) or self._ring is None:
            return
        if getattr(ended, "answered_elsewhere", False):
            self._ring_over(self._call_log.answered_elsewhere(None))
        else:
            self._ring_over(self._call_log.ring_ended(), grace=True)

    def _fire_missed(self, entry: cl.CallEntry) -> None:
        if self._hass is None:
            return
        self._hass.bus.async_fire(EVENT_MISSED_CALL, {
            "panel": entry.panel,
            "panel_name": entry.name,
            "time": cl.iso_time(entry.time),
            "entry_id": self._entry_id,
        })

    def _on_missed_call_line(self, line: str) -> None:
        missed = sm.parse_missed_call(line)
        panel = (missed.sip_id if missed.sip_id and valid_sip_token(missed.sip_id)
                 else "unknown")
        when = cl.epoch_seconds(missed.timestamp) or time.time()
        entry, new = self._call_log.missed_call(
            panel, self._panel_name(panel), when)
        self._sync_ring_with_log()
        if new:
            self._fire_missed(entry)
        self._call_log_changed()

    def _on_call_answered_line(self, line: str) -> None:
        call_id = sm.parse_call_answered(line)
        if call_id is None:
            return
        was_ringing = self._ring is not None
        entry = self._call_log.answered_elsewhere(call_id)
        if entry is None:
            return
        if was_ringing and self._call_log.ringing is None:
            self._ring_over(entry)
        else:
            self._call_log_changed()

    async def async_clear_missed_calls(self) -> None:
        """Reset the missed-call count."""
        self._call_log.clear_missed()
        self._call_log_changed()

    # ─── Camera switch ───────────────────────────────────────────────

    @property
    def camera_switch_available(self) -> bool:
        """True during a call whose panel said it has other cameras."""
        return self._switch_available and sip.in_call

    def _on_call_info_line(self, line: str) -> None:
        available = sm.parse_call_info(line).switch_available
        if available != self._switch_available:
            self._switch_available = available
            self._notify_update()

    async def async_switch_camera(self, forward: bool) -> tuple[bool, str]:
        """Show the calling panel's next (or previous) camera."""
        if not self.camera_switch_available:
            return False, "The calling panel has no other camera to switch to."
        return await sip.do_system_message(
            self._cfg.panel_uri(CAMERA_SWITCH_ADDRESS),
            sm.switch_source_command(forward),
            extra_headers={sm.PANDA_HEADER: sm.PANDA_BLUE})

    # ─── Video messages ──────────────────────────────────────────────

    @property
    def video_messages(self) -> tuple[vm.VideoMessage, ...] | None:
        """The mailbox, newest first, or None until it has been read."""
        return self._video_messages

    @property
    def mailbox_usage(self) -> tuple[int, int] | None:
        """(messages stored, capacity), or None when the unit never said.

        The capacity only comes with the status reply; the count is the
        mailbox's own once it has been read, since the unit sends no new
        vm_level when a message arrives or is deleted.
        """
        level = self._apt.vm_level
        if level is None:
            return None
        used = (len(self._video_messages) if self._video_messages is not None
                else level[0])
        return used, level[1]

    def request_video_messages(self) -> None:
        """Ask the indoor unit for its mailbox; the reply comes as a MESSAGE.

        One request at a time; one asked for while another is being
        sent is sent once more after it.
        """
        if self._mailbox_task is not None and not self._mailbox_task.done():
            self._mailbox_again = True
            return
        self._mailbox_task = self._track(self._request_mailbox_loop())

    async def _request_mailbox_loop(self) -> None:
        while True:
            self._mailbox_again = False
            try:
                ok, msg = await self._send_to_indoor_unit(sm.VM_GET_DB)
                if not ok:
                    _LOGGER.debug("VM;GET_DB was not accepted (%s)", msg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next notification retries
                _LOGGER.debug("Asking for the mailbox failed", exc_info=True)
            if not self._mailbox_again:
                return

    async def _send_to_indoor_unit(self, body: str) -> tuple[bool, str]:
        return await sip.do_system_message(
            self._cfg.panel_uri(PICG_ADDRESS), body,
            extra_headers={sm.PANDA_HEADER: sm.PANDA_BLUE})

    def _on_mailbox_changed_line(self, line: str) -> None:
        if sm.classify_line(line) == sm.KIND_NEW_VOICEMAIL:
            self._new_message_pending = True
        self.request_video_messages()

    async def _on_mailbox(self, body: str) -> None:
        """Adopt a mailbox the unit sent, and announce what is new in it.

        A message is new when the previous mailbox did not have it. The
        first mailbox after a start has nothing to compare with, so only
        a mailbox that answers a NEW notification announces its newest
        unread message then.
        """
        loop = asyncio.get_running_loop()
        try:
            messages = await loop.run_in_executor(None, _read_mailbox, body)
        except ValueError as err:
            _LOGGER.warning("Could not read the video-message mailbox (%s)", err)
            return
        previous = self._video_messages
        self._video_messages = messages
        if previous is not None:
            known = {(m.id, m.orig_time) for m in previous}
            fresh = [m for m in messages
                     if (m.id, m.orig_time) not in known and not m.read]
        elif self._new_message_pending:
            fresh = [m for m in messages if not m.read][:1]
        else:
            fresh = []
        self._new_message_pending = False
        for message in fresh:
            self._fire_video_message(message)
        self._notify_update()

    def _fire_video_message(self, message: vm.VideoMessage) -> None:
        if self._hass is None:
            return
        self._hass.bus.async_fire(EVENT_VIDEO_MESSAGE, {
            **message.as_attribute(self.panel_names),
            "entry_id": self._entry_id,
        })

    def _find_message(self, message_id: str) -> vm.VideoMessage | None:
        for message in self._video_messages or ():
            if message.id == str(message_id).strip():
                return message
        return None

    def _unknown_message(self, message_id: str) -> tuple[bool, str]:
        if self._video_messages is None:
            return False, "The video messages have not been read from the indoor unit yet."
        return False, f"There is no video message with ID {message_id}."

    async def async_mark_video_message_read(self, message_id: str) -> tuple[bool, str]:
        """Mark one message read, on the unit and here."""
        message = self._find_message(message_id)
        if message is None:
            return self._unknown_message(message_id)
        ok, msg = await self._send_to_indoor_unit(vm.read_command(message))
        if ok:
            self._video_messages = tuple(
                replace(m, read=True) if m == message else m
                for m in self._video_messages or ())
            self._notify_update()
            self.request_video_messages()
        return ok, msg

    async def async_delete_video_message(self, message_id: str) -> tuple[bool, str]:
        """Delete one message, on the unit and here."""
        message = self._find_message(message_id)
        if message is None:
            return self._unknown_message(message_id)
        ok, msg = await self._send_to_indoor_unit(vm.delete_command(message))
        if ok:
            self._video_messages = tuple(
                m for m in self._video_messages or () if m != message)
            self._notify_update()
            self.request_video_messages()
        return ok, msg

    async def async_delete_all_video_messages(self) -> tuple[bool, str]:
        """Empty the mailbox."""
        ok, msg = await self._send_to_indoor_unit(vm.DELETE_ALL)
        if ok:
            self._video_messages = ()
            self._notify_update()
            self.request_video_messages()
        return ok, msg

    async def async_play_video_message(self, message_id: str) -> tuple[bool, str]:
        """Play a message: call the extension that plays it back.

        The recording is the media of an ordinary call, so it shows on
        the camera like any call does, and ends with the unit's BYE.
        Playing an unread message marks it read, as opening it in the
        app does.
        """
        message = self._find_message(message_id)
        if message is None:
            return self._unknown_message(message_id)
        prefix = self._plant.vm_prefix if self._plant is not None else None
        extension = vm.playback_extension(message, prefix)
        if not valid_sip_token(extension):
            return False, "That message has no extension it can be played from."
        ok, msg = await self.async_call(target=extension)
        if ok and not message.read:
            self._track(self.async_mark_video_message_read(message.id))
        return ok, msg

    # ─── Diagnostics ─────────────────────────────────────────────────

    def diagnostics(self) -> dict:
        """What a bug report needs, without a secret or an identity.

        No SIP identity, no address, no token, no call ID: counts,
        flags and the settings the unit reported.
        """
        plant = self._plant
        messages = self._video_messages
        return {
            "registered": self.registered,
            "in_call": self.in_call,
            "ringing": self._ring is not None,
            "camera_switch_available": self.camera_switch_available,
            "plant": None if plant is None else {
                "panels": len(plant.panels),
                "actuators": len(plant.actuators),
                "has_group": plant.group is not None,
                "has_apartment_intercom": self.apartment_intercom is not None,
                "has_vm_prefix": plant.vm_prefix is not None,
            },
            "status_received": self._init_status is not None,
            "apartment": {
                "dnd": self._apt.dnd,
                "voicemail": self._apt.voicemail,
                "vm_timeout": self._apt.vm_timeout,
                "vm_timeout_values": list(self._apt.vm_timeout_values),
                "vm_level": (None if self._apt.vm_level is None
                             else list(self._apt.vm_level)),
            },
            "video_messages": None if messages is None else {
                "total": len(messages),
                "unread": sum(1 for m in messages if not m.read),
            },
            "call_log": {
                "entries": len(self._call_log.entries),
                "missed_count": self._call_log.missed_count,
                "outcomes": [e.outcome for e in self._call_log.entries],
            },
        }

    # ─── Plant configuration ─────────────────────────────────────────

    @property
    def plant(self) -> PlantConfig | None:
        """The plant configuration the entities were built from, if any."""
        return self._plant

    @property
    def init_status(self) -> sm.InitStatus | None:
        """The indoor unit's last status reply, kept in memory only.

        It holds the phonebook token, so it is never persisted; the next
        registration asks for a fresh one anyway.
        """
        return self._init_status

    def set_plant_sync(
        self,
        current: PlantConfig | None,
        fetch: PlantFetcher,
        on_change: PlantListener,
        reload_entry: Callable[[], None],
    ) -> None:
        """Enable the phonebook sync.

        `current` is the configuration the entities are being built from
        (the stored one, or None); a status reply naming the same version
        downloads nothing. `on_change` persists a new configuration, and
        `reload_entry` rebuilds the entities when one changes what they
        are. Both are Home Assistant's business, so they are handed in
        rather than done here, and the hub stays testable without it.
        """
        self._plant = current
        self._plant_fetcher = fetch
        self._plant_listener = on_change
        self._reload_entry = reload_entry

    def request_plant_sync(self) -> None:
        """Ask the indoor unit for its status, and act on the answer.

        One sync at a time. A request that arrives while one is running
        — a NEW_PHONEBOOK notification during the download the last
        registration started — runs it once more when it finishes,
        rather than racing it.
        """
        if self._plant_fetcher is None:
            return
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_again = True
            return
        self._sync_task = self._track(self._plant_sync_loop())

    async def _plant_sync_loop(self) -> None:
        while True:
            self._sync_again = False
            try:
                await self._sync_plant_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed sync keeps the old plant
                _LOGGER.exception("Plant configuration sync failed")
            if not self._sync_again:
                return

    async def _request_init_status(self) -> sm.InitStatus | None:
        """Send GET_INIT_STATUS and wait for the reply, or give up."""
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._status_waiter = waiter
        try:
            ok, msg = await sip.do_system_message(
                self._cfg.panel_uri(PICG_ADDRESS), sm.GET_INIT_STATUS,
                extra_headers={sm.PANDA_HEADER: sm.PANDA_BLUE})
            if not ok:
                _LOGGER.debug("GET_INIT_STATUS was not accepted (%s); "
                              "keeping the current panels", msg)
                return None
            return await asyncio.wait_for(waiter, PLANT_STATUS_TIMEOUT)
        except asyncio.TimeoutError:
            # Plants whose indoor unit is not at 60001 never answer, and
            # neither does one that is offline. Both keep what they have.
            _LOGGER.debug("No GET_INIT_STATUS_REPLY within %ss; keeping the "
                          "current panels", PLANT_STATUS_TIMEOUT)
            return None
        finally:
            if self._status_waiter is waiter:
                self._status_waiter = None

    def _group_for(self, status: sm.InitStatus) -> str:
        """The apartment group the phonebook is read for.

        The status reply's GID when it names one (the SDK adopts it), the
        QR's otherwise.
        """
        if status.gid and valid_sip_token(status.gid):
            return status.gid
        return self._cfg.group_id

    async def _sync_plant_once(self) -> None:
        status = await self._request_init_status()
        if status is None:
            return
        # The unit answers, so it is there to ask for its mailbox too.
        # The official app fetches it after the status as well.
        self.request_video_messages()
        if not status.rubrica_ver or not status.token:
            _LOGGER.debug("The indoor unit announced no phonebook; keeping "
                          "the current panels")
            return
        if (self._plant is not None
                and self._plant.version == status.rubrica_ver.strip().lower()):
            _LOGGER.debug("Phonebook unchanged")
            return
        assert self._plant_fetcher is not None
        try:
            plant = await self._plant_fetcher(status, self._group_for(status))
        except asyncio.CancelledError:
            raise
        except (PhonebookError, ValueError) as err:
            # Both carry messages written to be safe to log.
            self._warn_fetch_failed(str(err))
            return
        except Exception as err:  # noqa: BLE001 - transport errors vary by library
            self._warn_fetch_failed(type(err).__name__)
            return
        await self._apply_plant(plant)

    def _warn_fetch_failed(self, reason: str) -> None:
        _LOGGER.warning(
            "Could not load the plant's phonebook (%s); keeping the %s",
            reason,
            "last known plant configuration" if self._plant is not None
            else "panels from the integration options")

    async def _apply_plant(self, plant: PlantConfig) -> None:
        """Adopt a freshly downloaded configuration.

        Persisted first, so a reload — or a restart — builds from it.
        The entry is reloaded only when the entities change; a phonebook
        edit elsewhere in the building must not drop the registration.
        A reload in the middle of a call would hang up on the visitor,
        so it waits for the call to end.
        """
        previous = self._plant
        self._plant = plant
        _LOGGER.info(
            "Plant configuration loaded from the phonebook: %d panel(s), "
            "%d actuator(s)", len(plant.panels), len(plant.actuators))
        if self._plant_listener is not None:
            await self._plant_listener(previous, plant)
        if plant.entities_equal(previous) or self._reload_entry is None:
            return
        if sip.in_call or sip.calling:
            _LOGGER.info("Plant configuration changed; the entities will be "
                         "rebuilt when the current call ends")
            self._reload_pending = True
            return
        _LOGGER.info("Plant configuration changed; rebuilding the entities")
        self._reload_entry()

    def _handle_message(self, message: sip.InboundMessage) -> None:
        """Dispatch the lines of a MESSAGE from the indoor unit.

        A `grey` MESSAGE with `Koala: mailbox.db` is the video-message
        mailbox this hub asked for. Any other `Panda` family but `blue`
        (text messages) is not read as a system notification, whatever
        its text says. One with no header at all is accepted: whether
        the indoor unit always sets it on its replies has not been
        confirmed.

        Every line of a notification is classified; the kinds this
        integration acts on have a handler below, the rest (nicknames,
        apartment names, text-message receipts) are ignored.
        """
        panda = (message.panda or "").lower()
        if panda == sm.PANDA_GREY and message.koala == sm.KOALA_MAILBOX:
            self._track(self._on_mailbox(message.body))
            return
        if message.panda is not None and panda != sm.PANDA_BLUE:
            _LOGGER.debug("Ignoring a MESSAGE of family %s", message.panda)
            return
        _LOGGER.debug("System MESSAGE from %s (%s)", message.sender,
                      sm.summarize_body(message.body))
        handlers = {
            sm.KIND_INIT_STATUS_REPLY: self._on_init_status_line,
            sm.KIND_NEW_PHONEBOOK: self._on_new_phonebook_line,
            sm.KIND_DND_STATUS: self._on_dnd_line,
            sm.KIND_VOICEMAIL_STATUS: self._on_voicemail_line,
            sm.KIND_SET_APT_PARAMS_REPLY: self._on_apt_params_reply_line,
            sm.KIND_APT_PARAMS_CHANGED: self._on_apt_params_changed_line,
            sm.KIND_NEW_VOICEMAIL: self._on_mailbox_changed_line,
            sm.KIND_VOICEMAIL_CHANGE: self._on_mailbox_changed_line,
            sm.KIND_MISSED_CALL: self._on_missed_call_line,
            sm.KIND_CALL_INFO: self._on_call_info_line,
            sm.KIND_CALL_ANSWERED: self._on_call_answered_line,
        }
        for kind, line in sm.classify_body(message.body):
            handler = handlers.get(kind)
            if handler is not None:
                handler(line)

    def _on_init_status_line(self, line: str) -> None:
        try:
            status = sm.parse_init_status_reply(line)
        except ValueError as err:
            _LOGGER.debug("Unreadable GET_INIT_STATUS_REPLY: %s", err)
            return
        self._init_status = status
        self._apply_status(status)
        waiter = self._status_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(status)

    def _on_new_phonebook_line(self, line: str) -> None:
        try:
            note = sm.parse_new_phonebook(line)
        except ValueError as err:
            _LOGGER.debug("Unreadable NEW_PHONEBOOK: %s", err)
            return
        if self._plant is not None and self._plant.version == note.version.lower():
            return
        _LOGGER.info("The installer changed the plant configuration; "
                     "fetching the new phonebook")
        # A fresh status rather than the cached token: the token may have
        # changed with the phonebook, and after a restart there is none.
        self.request_plant_sync()

    async def _handle_broadcast(self, msg_type, msg):
        """React to a SIP-layer event."""
        if msg_type == "message":
            # Never the body: a status reply carries the phonebook token.
            self._handle_message(msg)
            return
        _LOGGER.debug("[%s] %s", msg_type, msg)

        if msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif msg_type == "ring_ended":
            self._on_ring_ended(msg)
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()
            if self._switch_available:
                self._switch_available = False
                self._notify_update()
            if self._ring is not None and not sip.pending_incoming["active"]:
                # The connection went while it rang (`_abandon_call`):
                # nothing will ever CANCEL that INVITE now.
                self._ring_over(self._call_log.ring_ended(), grace=True)
            # The panel, the cloud or our own BYE ended the call. This is
            # the one place every ending converges, so it is where the
            # auto-call record is torn down.
            self._clear_auto_call()
            if self._reload_pending:
                # A new plant configuration arrived during the call and
                # waited for it to end; see `_apply_plant`.
                self._reload_pending = False
                if self._reload_entry is not None:
                    self._reload_entry()

        if msg_type == "ring":
            caller_uri = sip.pending_incoming.get("caller_uri", "")
            busy = self._auto_called or sip.in_call or sip.calling

            # When we placed the call ourselves the panel INVITEs us back.
            # That is the PBX echoing our own call, not a doorbell press,
            # and it must stay completely silent.
            if busy and self._is_echo_of_our_call(caller_uri):
                _LOGGER.debug(
                    "Suppressing ring: the PBX is echoing the call this hub "
                    "placed (auto_called=%s, in_call=%s, calling=%s)",
                    self._auto_called, sip.in_call, sip.calling)
                self._track(sip.do_decline_incoming())
                return

            if busy:
                # A real visitor, while a call is already up or being set
                # up. One call at a time is a genuine constraint of this
                # design, so the INVITE is still declined — with 486 Busy
                # Here rather than 603, which would also stop the indoor
                # unit ringing. But the doorbell did ring, and
                # `vimar_intercom_ring` is the only signal a notification
                # bridge has: firing nothing made every visitor arriving
                # during a call, or during the up-to-45 s setup window,
                # completely invisible.
                _LOGGER.info(
                    "Declining a doorbell press that arrived during a call; "
                    "the ring event still fires")
                call_ids = tuple(sip.pending_incoming.get("call_ids") or ())
                self._track(sip.do_decline_incoming(busy=True))

            address, name = self._panel_for(caller_uri)
            if busy:
                # Logged, but not ringing here: 486 left the rest of the
                # house ringing, and only the unit's MISSED_CALL or
                # another device's C;<id>;ANSWERED can say how it ended.
                self._call_log.ring(address, name, call_ids, time.time())
                self._call_log.ring_ended()
                self._call_log_changed()
            else:
                self._start_ring(address, name)

            if self._hass is not None:
                self._hass.bus.async_fire(EVENT_RING, {
                    "panel": address,
                    "panel_name": name,
                    "entry_id": self._entry_id,
                })

            for cb in self._ring_callbacks:
                try:
                    cb(address)
                except Exception:
                    _LOGGER.exception("Ring callback error")


def _read_mailbox(body: str) -> tuple[vm.VideoMessage, ...]:
    """Decode and parse a mailbox body; blocking, run in the executor."""
    return vm.parse_mailbox(vm.decode_mailbox(body))
