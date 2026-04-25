from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import serial
from obsws_python import ReqClient
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


# Local controller config. Keep the ESP32 serial protocol fixed:
# every command writes exactly one lowercase ASCII byte from VALID_COMMANDS.
SERIAL_PORT = os.getenv("SERIAL_PORT", "COM3")
BAUD_RATE = int(os.getenv("BAUD_RATE", "115200"))
INACTIVITY_TIMEOUT_SECONDS = int(os.getenv("INACTIVITY_TIMEOUT_SECONDS", "60"))
COMMAND_COOLDOWN_SECONDS = int(os.getenv("COMMAND_COOLDOWN_SECONDS", "2"))
STREAM_PUBLIC_BASE_URL = os.getenv("STREAM_PUBLIC_BASE_URL", "http://192.168.1.23:8889")
STREAM_NAME = os.getenv("STREAM_NAME", "pachislot")
OBS_WS_HOST = os.getenv("OBS_WS_HOST", "127.0.0.1")
OBS_WS_PORT = int(os.getenv("OBS_WS_PORT", "4455"))
OBS_WS_PASSWORD = os.getenv("OBS_WS_PASSWORD", "")


VALID_COMMANDS = {
    "a": "lever",
    "s": "left stop",
    "d": "middle stop",
    "f": "right stop",
    "g": "max bet",
}


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pachislot Controller")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)


@dataclass
class Client:
    id: str
    websocket: WebSocket


class SerialBridge:
    def __init__(self, port: str, baud_rate: int) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self._serial: serial.Serial | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def open(self) -> None:
        async with self._lock:
            await self._open_locked()

    async def _open_locked(self) -> None:
        if self._serial and self._serial.is_open:
            return
        try:
            self._serial = serial.Serial(self.port, self.baud_rate, timeout=1, write_timeout=1)
            self.last_error = None
        except serial.SerialException as exc:
            self._serial = None
            self.last_error = str(exc)
            raise

    async def send_command(self, command: str) -> None:
        if command not in VALID_COMMANDS:
            raise ValueError(f"Invalid command: {command!r}")

        async with self._lock:
            try:
                await self._open_locked()
                assert self._serial is not None
                # Protocol guarantee: write exactly one lowercase ASCII byte, no newline.
                self._serial.write(command.encode("ascii"))
                self._serial.flush()
                self.last_error = None
            except (serial.SerialException, OSError) as exc:
                if self._serial:
                    try:
                        self._serial.close()
                    except serial.SerialException:
                        pass
                self._serial = None
                self.last_error = str(exc)
                raise

    async def close(self) -> None:
        async with self._lock:
            if self._serial:
                self._serial.close()
                self._serial = None

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)


class OBSBridge:
    def __init__(self, host: str, port: int, password: str) -> None:
        self.host = host
        self.port = port
        self.password = password
        self._client: ReqClient | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.streaming: bool | None = None

    async def start_stream(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._start_stream_sync)

    async def stop_stream(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._stop_stream_sync)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._disconnect_sync)

    def _get_client_sync(self) -> ReqClient:
        if self._client is None:
            self._client = ReqClient(
                host=self.host,
                port=self.port,
                password=self.password,
                timeout=3,
            )
        return self._client

    def _get_streaming_sync(self) -> bool:
        status = self._get_client_sync().get_stream_status()
        self.streaming = bool(getattr(status, "output_active", False))
        return self.streaming

    def _start_stream_sync(self) -> None:
        try:
            if not self._get_streaming_sync():
                self._get_client_sync().start_stream()
                self.streaming = True
            self.last_error = None
        except Exception as exc:
            self._handle_error_sync(exc)
            raise RuntimeError(str(exc)) from exc

    def _stop_stream_sync(self) -> None:
        try:
            if self._get_streaming_sync():
                self._get_client_sync().stop_stream()
                self.streaming = False
            self.last_error = None
        except Exception as exc:
            self._handle_error_sync(exc)
            raise RuntimeError(str(exc)) from exc

    def _handle_error_sync(self, exc: Exception) -> None:
        self.last_error = str(exc)
        self.streaming = None
        self._disconnect_sync()

    def _disconnect_sync(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None


class ControllerState:
    def __init__(self, serial_bridge: SerialBridge, obs_bridge: OBSBridge) -> None:
        self.serial_bridge = serial_bridge
        self.obs_bridge = obs_bridge
        self.clients: dict[str, Client] = {}
        self.controller_id: str | None = None
        self.last_activity: float | None = None
        self.command_last_sent: dict[str, float] = {command: 0.0 for command in VALID_COMMANDS}
        self._lock = asyncio.Lock()

    async def add_client(self, websocket: WebSocket) -> str:
        client_id = uuid.uuid4().hex
        async with self._lock:
            self.clients[client_id] = Client(id=client_id, websocket=websocket)
        await self.send_to(client_id, {"type": "connected", "client_id": client_id})
        await self.broadcast_state()
        return client_id

    async def remove_client(self, client_id: str) -> None:
        released = False
        async with self._lock:
            self.clients.pop(client_id, None)
            if self.controller_id == client_id:
                self.controller_id = None
                self.last_activity = None
                released = True

        if released:
            await self.broadcast({"type": "control_released", "reason": "disconnect"})
            await self.stop_obs_if_idle()
        await self.broadcast_state()

    async def take_control(self, client_id: str) -> None:
        async with self._lock:
            if self.controller_id is None:
                self.controller_id = client_id
                self.last_activity = time.monotonic()
                granted = True
            elif self.controller_id == client_id:
                self.last_activity = time.monotonic()
                granted = True
            else:
                granted = False

        if granted:
            await self.send_to(client_id, {"type": "control_granted"})
            await self.broadcast_state()
            try:
                await self.obs_bridge.start_stream()
            except Exception as exc:
                await self.broadcast({"type": "obs_error", "message": str(exc)})
        else:
            await self.send_to(client_id, {"type": "control_denied", "reason": "Someone else has control"})
        await self.broadcast_state()

    async def release_control(self, client_id: str, reason: str = "quit") -> None:
        released = False
        async with self._lock:
            if self.controller_id == client_id:
                self.controller_id = None
                self.last_activity = None
                released = True

        if released:
            await self.broadcast({"type": "control_released", "reason": reason})
            await self.stop_obs_if_idle()
        await self.broadcast_state()

    async def stop_obs_if_idle(self) -> None:
        async with self._lock:
            has_controller = self.controller_id is not None
        if has_controller:
            return
        try:
            await self.obs_bridge.stop_stream()
        except Exception as exc:
            await self.broadcast({"type": "obs_error", "message": str(exc)})

    async def heartbeat(self, client_id: str) -> None:
        async with self._lock:
            if self.controller_id == client_id:
                self.last_activity = time.monotonic()

    async def send_command(self, client_id: str, command: str) -> None:
        if command not in VALID_COMMANDS:
            await self.send_to(client_id, {"type": "error", "message": "Invalid command"})
            return

        response: dict[str, Any] | None = None
        async with self._lock:
            if self.controller_id != client_id:
                response = {"type": "control_denied", "reason": "You do not have control"}
            else:
                now = time.monotonic()
                self.last_activity = now
                elapsed = now - self.command_last_sent[command]
                if elapsed < COMMAND_COOLDOWN_SECONDS:
                    remaining = COMMAND_COOLDOWN_SECONDS - elapsed
                    response = {
                        "type": "cooldown_blocked",
                        "command": command,
                        "label": VALID_COMMANDS[command],
                        "remaining_seconds": round(remaining, 2),
                    }

        if response:
            await self.send_to(client_id, response)
            await self.broadcast_state()
            return

        try:
            await self.serial_bridge.send_command(command)
        except Exception as exc:
            await self.broadcast({"type": "serial_error", "message": str(exc)})
            await self.broadcast_state()
            return

        async with self._lock:
            self.command_last_sent[command] = time.monotonic()

        await self.broadcast(
            {
                "type": "command_sent",
                "command": command,
                "label": VALID_COMMANDS[command],
                "sent_by": client_id,
            }
        )
        await self.broadcast_state()

    async def timeout_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            expired_id: str | None = None
            async with self._lock:
                if self.controller_id and self.last_activity:
                    if time.monotonic() - self.last_activity >= INACTIVITY_TIMEOUT_SECONDS:
                        expired_id = self.controller_id
                        self.controller_id = None
                        self.last_activity = None

            if expired_id:
                await self.broadcast({"type": "timeout_expired", "client_id": expired_id})
                await self.broadcast({"type": "control_released", "reason": "timeout"})
                await self.stop_obs_if_idle()
                await self.broadcast_state()

    async def broadcast_state(self) -> None:
        async with self._lock:
            client_ids = list(self.clients)
            controller_id = self.controller_id
            last_activity = self.last_activity
            serial_open = self.serial_bridge.is_open
            serial_error = self.serial_bridge.last_error
            obs_streaming = self.obs_bridge.streaming
            obs_error = self.obs_bridge.last_error

        seconds_remaining = None
        if controller_id and last_activity:
            elapsed = time.monotonic() - last_activity
            seconds_remaining = max(0, round(INACTIVITY_TIMEOUT_SECONDS - elapsed))

        for client_id in client_ids:
            if controller_id is None:
                control_state = "available"
                status_text = "Available"
            elif controller_id == client_id:
                control_state = "you"
                status_text = "You have control"
            else:
                control_state = "other"
                status_text = "Someone else has control"

            await self.send_to(
                client_id,
                {
                    "type": "state",
                    "client_id": client_id,
                    "control_state": control_state,
                    "status_text": status_text,
                    "can_control": control_state == "you",
                    "seconds_until_timeout": seconds_remaining,
                    "valid_commands": VALID_COMMANDS,
                    "stream_url": (
                        f"{STREAM_PUBLIC_BASE_URL.rstrip('/')}/{STREAM_NAME}"
                        "?controls=false&muted=false&autoplay=true&playsInline=true"
                    ),
                    "serial": {
                        "port": SERIAL_PORT,
                        "baud_rate": BAUD_RATE,
                        "open": serial_open,
                        "last_error": serial_error,
                    },
                    "obs": {
                        "host": OBS_WS_HOST,
                        "port": OBS_WS_PORT,
                        "streaming": obs_streaming,
                        "last_error": obs_error,
                    },
                },
            )

    async def broadcast(self, message: dict[str, Any]) -> None:
        async with self._lock:
            client_ids = list(self.clients)
        for client_id in client_ids:
            await self.send_to(client_id, message)

    async def send_to(self, client_id: str, message: dict[str, Any]) -> None:
        async with self._lock:
            client = self.clients.get(client_id)
        if not client:
            return
        try:
            await client.websocket.send_json(message)
        except RuntimeError:
            pass


serial_bridge = SerialBridge(SERIAL_PORT, BAUD_RATE)
obs_bridge = OBSBridge(OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD)
state = ControllerState(serial_bridge, obs_bridge)


@app.on_event("startup")
async def startup() -> None:
    try:
        await serial_bridge.open()
        print(f"Opened {SERIAL_PORT} at {BAUD_RATE} baud")
    except serial.SerialException as exc:
        print(f"Could not open {SERIAL_PORT}: {exc}")
    asyncio.create_task(state.timeout_loop())
    asyncio.create_task(state.stop_obs_if_idle())


@app.on_event("shutdown")
async def shutdown() -> None:
    await serial_bridge.close()
    await obs_bridge.close()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    client_id = await state.add_client(websocket)

    try:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")

            if message_type == "take_control":
                await state.take_control(client_id)
            elif message_type == "release_control":
                await state.release_control(client_id)
            elif message_type == "command":
                command = str(message.get("command", "")).lower()
                await state.send_command(client_id, command)
            elif message_type == "heartbeat":
                await state.heartbeat(client_id)
                await state.broadcast_state()
            else:
                await state.send_to(client_id, {"type": "error", "message": "Unknown message type"})
    except WebSocketDisconnect:
        await state.remove_client(client_id)
    except Exception as exc:
        await state.send_to(client_id, {"type": "error", "message": str(exc)})
        await state.remove_client(client_id)
