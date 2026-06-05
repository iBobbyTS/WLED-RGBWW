from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


DEFAULT_WS_URL = "ws://127.0.0.1:33782"
DEFAULT_NODE_ID = "40165-560387"
DEFAULT_CLIENT_ID = 1
DEFAULT_OPENAPI_SECRET = "9veqiL0G0EUOviwzL1prPc0iGIGUJtbzSaPYQfgfyxM="

CCT_MIN = 1800
CCT_MAX = 20000
INTENSITY_MIN = 0
INTENSITY_MAX = 1000
GM_MIN = 0
GM_MAX = 200
HUE_MIN = 0
HUE_MAX = 360
SAT_MIN = 0
SAT_MAX = 100
RGB_MIN = 0
RGB_MAX = 255


class AmaranOpenAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAPISettings:
    url: str = DEFAULT_WS_URL
    node_id: str = DEFAULT_NODE_ID
    client_id: int = DEFAULT_CLIENT_ID
    secret: str = DEFAULT_OPENAPI_SECRET
    timeout: float = 3.0

    @classmethod
    def from_env(cls) -> "OpenAPISettings":
        return cls(
            url=os.environ.get("AMARAN_WS_URL", DEFAULT_WS_URL),
            node_id=os.environ.get("AMARAN_NODE_ID", DEFAULT_NODE_ID),
            client_id=int(os.environ.get("AMARAN_CLIENT_ID", str(DEFAULT_CLIENT_ID))),
            secret=os.environ.get("AMARAN_OPENAPI_SECRET", DEFAULT_OPENAPI_SECRET),
            timeout=float(os.environ.get("AMARAN_TIMEOUT", "3.0")),
        )


def generate_token(
    secret_key: str = DEFAULT_OPENAPI_SECRET,
    *,
    now: int | None = None,
    nonce: bytes | None = None,
) -> str:
    """Generate an Amaran/Sidus OpenAPI v2 AES-256-GCM request token."""

    key = base64.b64decode(secret_key)
    if len(key) != 32:
        raise ValueError("OpenAPI secret must decode to 32 bytes for AES-256-GCM")

    iv = nonce if nonce is not None else os.urandom(12)
    if len(iv) != 12:
        raise ValueError("OpenAPI token nonce must be 12 bytes")

    timestamp = str(int(time.time()) if now is None else int(now)).encode("ascii")
    encrypted = AESGCM(key).encrypt(iv, timestamp, None)
    ciphertext = encrypted[:-16]
    tag = encrypted[-16:]
    return base64.b64encode(iv + tag + ciphertext).decode("ascii")


def intensity_percent_to_raw(percent: float) -> int:
    value = round(float(percent) * 10)
    return _require_range("intensity", value, INTENSITY_MIN, INTENSITY_MAX)


def gm_offset_to_raw(offset: float) -> int:
    value = round(100 + float(offset) * 100)
    return _require_range("gm", value, GM_MIN, GM_MAX)


def gm_side_to_raw(side: str) -> int:
    normalized = side.strip().lower()
    if normalized in {"g", "green"}:
        return GM_MAX
    if normalized in {"m", "magenta"}:
        return GM_MIN
    if normalized in {"n", "neutral", "0"}:
        return 100
    raise ValueError("gm side must be one of: g, m, neutral")


def _resolve_intensity(
    *,
    intensity: int | None = None,
    intensity_percent: float | None = None,
    required: bool,
) -> int | None:
    if intensity is not None and intensity_percent is not None:
        raise ValueError("use either intensity or intensity_percent, not both")
    if intensity_percent is not None:
        return intensity_percent_to_raw(intensity_percent)
    if intensity is not None:
        return _require_range("intensity", int(intensity), INTENSITY_MIN, INTENSITY_MAX)
    if required:
        raise ValueError("intensity or intensity_percent is required")
    return None


def _resolve_gm(*, gm: int | None = None, gm_offset: float | None = None) -> int | None:
    if gm is not None and gm_offset is not None:
        raise ValueError("use either gm or gm_offset, not both")
    if gm_offset is not None:
        return gm_offset_to_raw(gm_offset)
    if gm is not None:
        return _require_range("gm", int(gm), GM_MIN, GM_MAX)
    return None


def set_cct(
    cct: int,
    *,
    intensity: int | None = None,
    intensity_percent: float | None = None,
    gm: int | None = None,
    gm_offset: float | None = None,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    args: dict[str, int] = {"cct": _require_range("cct", int(cct), CCT_MIN, CCT_MAX)}

    resolved_intensity = _resolve_intensity(
        intensity=intensity,
        intensity_percent=intensity_percent,
        required=False,
    )
    if resolved_intensity is not None:
        args["intensity"] = resolved_intensity

    resolved_gm = _resolve_gm(gm=gm, gm_offset=gm_offset)
    if resolved_gm is not None:
        args["gm"] = resolved_gm

    response = send_openapi_request("set_cct", args=args, settings=settings, include_events=include_events)
    if raise_on_error:
        _raise_for_error(response)
    return response


def get_cct(
    *,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    response = send_openapi_request("get_cct", settings=settings, include_events=include_events)
    if raise_on_error:
        _raise_for_error(response)
    return response


def get_node_config(
    *,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    response = send_openapi_request("get_node_config", settings=settings, include_events=include_events)
    if raise_on_error:
        _raise_for_error(response)
    return response


def set_hsi(
    hue: int,
    sat: int,
    *,
    intensity: int | None = None,
    intensity_percent: float | None = None,
    cct: int | None = None,
    gm: int | None = None,
    gm_offset: float | None = None,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    args = {
        "hue": _require_range("hue", int(hue), HUE_MIN, HUE_MAX),
        "sat": _require_range("sat", int(sat), SAT_MIN, SAT_MAX),
        "intensity": _resolve_intensity(
            intensity=intensity,
            intensity_percent=intensity_percent,
            required=True,
        ),
    }

    if cct is not None:
        args["cct"] = _require_range("cct", int(cct), CCT_MIN, CCT_MAX)
    resolved_gm = _resolve_gm(gm=gm, gm_offset=gm_offset)
    if resolved_gm is not None:
        args["gm"] = resolved_gm

    response = send_openapi_request("set_hsi", args=args, settings=settings, include_events=include_events)
    if raise_on_error:
        _raise_for_error(response)
    return response


def set_hsl(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return set_hsi(*args, **kwargs)


def get_hsi(
    *,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    response = send_openapi_request("get_hsi", settings=settings, include_events=include_events)
    if raise_on_error:
        _raise_for_error(response)
    return response


def get_hsl(**kwargs: Any) -> dict[str, Any]:
    return get_hsi(**kwargs)


def set_rgb(
    r: int,
    g: int,
    b: int,
    *,
    intensity: int | None = None,
    intensity_percent: float | None = None,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    args = {
        "r": _require_range("r", int(r), RGB_MIN, RGB_MAX),
        "g": _require_range("g", int(g), RGB_MIN, RGB_MAX),
        "b": _require_range("b", int(b), RGB_MIN, RGB_MAX),
    }
    resolved_intensity = _resolve_intensity(
        intensity=intensity,
        intensity_percent=intensity_percent,
        required=False,
    )
    if resolved_intensity is not None:
        args["intensity"] = resolved_intensity

    response = send_openapi_request("set_rgb", args=args, settings=settings, include_events=include_events)
    if raise_on_error:
        _raise_for_error(response)
    return response


def get_rgb(
    *,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    response = send_openapi_request("get_rgb", settings=settings, include_events=include_events)
    if raise_on_error:
        _raise_for_error(response)
    return response


def send_openapi_request(
    action: str,
    *,
    args: dict[str, Any] | None = None,
    settings: OpenAPISettings | None = None,
    include_events: bool = False,
) -> dict[str, Any]:
    active_settings = settings or OpenAPISettings.from_env()
    request_id = _new_request_id()
    request = build_openapi_request(action, args=args, settings=active_settings, request_id=request_id)

    events: list[dict[str, Any]] = []
    with MinimalWebSocket(active_settings.url, timeout=active_settings.timeout) as websocket:
        websocket.send_json(request)
        while True:
            message = json.loads(websocket.recv_text())
            if message.get("type") == "event":
                events.append(message)
                continue
            if message.get("request_id") == request_id:
                if include_events and events:
                    message = dict(message)
                    message["_events"] = events
                return message


def build_openapi_request(
    action: str,
    *,
    args: dict[str, Any] | None = None,
    settings: OpenAPISettings | None = None,
    request_id: int | None = None,
) -> dict[str, Any]:
    active_settings = settings or OpenAPISettings.from_env()
    payload: dict[str, Any] = {
        "version": 2,
        "type": "request",
        "client_id": active_settings.client_id,
        "request_id": _new_request_id() if request_id is None else request_id,
        "node_id": active_settings.node_id,
        "action": action,
        "token": generate_token(active_settings.secret),
    }
    if args is not None:
        payload["args"] = args
    return payload


def _raise_for_error(response: dict[str, Any]) -> None:
    if response.get("code") in (None, 0):
        return
    action = response.get("action", "request")
    message = response.get("message", response)
    raise AmaranOpenAPIError(f"{action} failed with code {response.get('code')}: {message}")


def _require_range(name: str, value: int, min_value: int, max_value: int) -> int:
    if not min_value <= value <= max_value:
        raise ValueError(f"{name} must be in [{min_value}, {max_value}], got {value}")
    return value


def _new_request_id() -> int:
    return time.time_ns() % 2_147_483_647


class MinimalWebSocket:
    def __init__(self, url: str, *, timeout: float = 3.0) -> None:
        self.url = url
        self.timeout = timeout
        self._socket: socket.socket | ssl.SSLSocket | None = None

    def __enter__(self) -> "MinimalWebSocket":
        self.connect()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"unsupported WebSocket scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise ValueError(f"missing WebSocket host in URL: {self.url}")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw_socket = socket.create_connection((parsed.hostname, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            wrapped_socket: socket.socket | ssl.SSLSocket = ssl.create_default_context().wrap_socket(
                raw_socket,
                server_hostname=parsed.hostname,
            )
        else:
            wrapped_socket = raw_socket
        wrapped_socket.settimeout(self.timeout)
        self._socket = wrapped_socket

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        host = parsed.hostname
        default_port = 443 if parsed.scheme == "wss" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sendall(request.encode("ascii"))
        header_text = self._read_http_headers()
        self._validate_handshake(header_text, sec_key)

    def send_json(self, value: dict[str, Any]) -> None:
        self.send_text(json.dumps(value, separators=(",", ":")))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        chunks: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:
                raise ConnectionError("WebSocket connection closed by peer")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x0}:
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8")

    def close(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        finally:
            sock.close()
            self._socket = None

    def _validate_handshake(self, header_text: str, sec_key: str) -> None:
        lines = header_text.split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise ConnectionError(f"WebSocket upgrade failed: {lines[0] if lines else header_text!r}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        expected_accept = base64.b64encode(
            hashlib.sha1((sec_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        actual_accept = headers.get("sec-websocket-accept")
        if actual_accept != expected_accept:
            raise ConnectionError("WebSocket upgrade returned an invalid Sec-WebSocket-Accept header")

    def _read_http_headers(self) -> str:
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = self._recv(1)
            if not chunk:
                raise ConnectionError("socket closed while reading WebSocket handshake")
            buffer.extend(chunk)
            if len(buffer) > 65536:
                raise ConnectionError("WebSocket handshake headers are too large")
        return bytes(buffer).decode("iso-8859-1")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        mask_key = os.urandom(4)
        masked_payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        self._sendall(bytes(header) + mask_key + masked_payload)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        mask_key = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < length:
            chunk = self._recv(length - len(buffer))
            if not chunk:
                raise ConnectionError("socket closed while reading WebSocket frame")
            buffer.extend(chunk)
        return bytes(buffer)

    def _recv(self, length: int) -> bytes:
        sock = self._require_socket()
        return sock.recv(length)

    def _sendall(self, payload: bytes) -> None:
        sock = self._require_socket()
        sock.sendall(payload)

    def _require_socket(self) -> socket.socket | ssl.SSLSocket:
        if self._socket is None:
            raise ConnectionError("WebSocket is not connected")
        return self._socket


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=os.environ.get("AMARAN_WS_URL", DEFAULT_WS_URL))
    parser.add_argument("--node-id", default=os.environ.get("AMARAN_NODE_ID", DEFAULT_NODE_ID))
    parser.add_argument("--client-id", type=int, default=int(os.environ.get("AMARAN_CLIENT_ID", DEFAULT_CLIENT_ID)))
    parser.add_argument("--secret", default=os.environ.get("AMARAN_OPENAPI_SECRET", DEFAULT_OPENAPI_SECRET))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("AMARAN_TIMEOUT", "3.0")))
    parser.add_argument("--include-events", action="store_true")


def _settings_from_args(args: argparse.Namespace) -> OpenAPISettings:
    return OpenAPISettings(
        url=args.url,
        node_id=args.node_id,
        client_id=args.client_id,
        secret=args.secret,
        timeout=args.timeout,
    )


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _parse_set_cct_args(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"cct": args.cct}
    if args.intensity is not None:
        kwargs["intensity"] = args.intensity
    if args.intensity_percent is not None:
        kwargs["intensity_percent"] = args.intensity_percent
    if args.gm is not None:
        kwargs["gm"] = args.gm
    if args.gm_offset is not None:
        kwargs["gm_offset"] = args.gm_offset
    if args.gm_side is not None:
        if args.gm is not None or args.gm_offset is not None:
            raise ValueError("use only one of --gm, --gm-offset, or --gm-side")
        kwargs["gm"] = gm_side_to_raw(args.gm_side)
    return kwargs


def _parse_set_hsi_args(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "hue": args.hue,
        "sat": args.sat,
    }
    if args.intensity is not None:
        kwargs["intensity"] = args.intensity
    if args.intensity_percent is not None:
        kwargs["intensity_percent"] = args.intensity_percent
    if args.cct is not None:
        kwargs["cct"] = args.cct
    if args.gm is not None:
        kwargs["gm"] = args.gm
    if args.gm_offset is not None:
        kwargs["gm_offset"] = args.gm_offset
    if args.gm_side is not None:
        if args.gm is not None or args.gm_offset is not None:
            raise ValueError("use only one of --gm, --gm-offset, or --gm-side")
        kwargs["gm"] = gm_side_to_raw(args.gm_side)
    return kwargs


def _parse_set_rgb_args(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "r": args.r,
        "g": args.g,
        "b": args.b,
    }
    if args.intensity is not None:
        kwargs["intensity"] = args.intensity
    if args.intensity_percent is not None:
        kwargs["intensity_percent"] = args.intensity_percent
    return kwargs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Control an Amaran Ray120c through Amaran Desktop OpenAPI v2.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("set-cct", help="Set Ray120c CCT mode values.")
    _add_common_args(set_parser)
    set_parser.add_argument("cct", type=int)
    intensity_group = set_parser.add_mutually_exclusive_group()
    intensity_group.add_argument("--intensity", type=int, help="Raw OpenAPI intensity in [0, 1000].")
    intensity_group.add_argument("--intensity-percent", type=float, help="Intensity percent in [0, 100].")
    gm_group = set_parser.add_mutually_exclusive_group()
    gm_group.add_argument("--gm", type=int, help="Raw G/M value in [0, 200]. 0=M max, 100=neutral, 200=G max.")
    gm_group.add_argument("--gm-offset", type=float, help="G/M offset in [-1.0, 1.0]. Positive is green.")
    gm_group.add_argument("--gm-side", choices=("g", "m", "neutral"), help="Shortcut for max green, max magenta, or neutral.")

    get_parser = subparsers.add_parser("get-cct", help="Read current Ray120c CCT mode values.")
    _add_common_args(get_parser)

    hsi_parser = subparsers.add_parser("set-hsi", aliases=["set-hsl"], help="Set Ray120c HSI/HSL mode values.")
    _add_common_args(hsi_parser)
    hsi_parser.add_argument("hue", type=int)
    hsi_parser.add_argument("sat", type=int)
    hsi_intensity_group = hsi_parser.add_mutually_exclusive_group(required=True)
    hsi_intensity_group.add_argument("--intensity", type=int, help="Raw OpenAPI intensity in [0, 1000].")
    hsi_intensity_group.add_argument("--intensity-percent", type=float, help="Intensity percent in [0, 100].")
    hsi_parser.add_argument("--cct", type=int, help="Optional CCT payload field for devices that return it in HSI mode.")
    hsi_gm_group = hsi_parser.add_mutually_exclusive_group()
    hsi_gm_group.add_argument("--gm", type=int, help="Raw G/M value in [0, 200].")
    hsi_gm_group.add_argument("--gm-offset", type=float, help="G/M offset in [-1.0, 1.0]. Positive is green.")
    hsi_gm_group.add_argument("--gm-side", choices=("g", "m", "neutral"), help="Shortcut for max green, max magenta, or neutral.")

    get_hsi_parser = subparsers.add_parser("get-hsi", aliases=["get-hsl"], help="Read current Ray120c HSI/HSL mode values.")
    _add_common_args(get_hsi_parser)

    rgb_parser = subparsers.add_parser("set-rgb", help="Set Ray120c RGB mode values.")
    _add_common_args(rgb_parser)
    rgb_parser.add_argument("r", type=int)
    rgb_parser.add_argument("g", type=int)
    rgb_parser.add_argument("b", type=int)
    rgb_intensity_group = rgb_parser.add_mutually_exclusive_group()
    rgb_intensity_group.add_argument("--intensity", type=int, help="Raw OpenAPI intensity in [0, 1000].")
    rgb_intensity_group.add_argument("--intensity-percent", type=float, help="Intensity percent in [0, 100].")

    get_rgb_parser = subparsers.add_parser("get-rgb", help="Read current Ray120c RGB mode values.")
    _add_common_args(get_rgb_parser)

    config_parser = subparsers.add_parser("node-config", help="Read Ray120c OpenAPI node configuration.")
    _add_common_args(config_parser)

    args = parser.parse_args(argv)
    settings = _settings_from_args(args)
    try:
        if args.command == "set-cct":
            response = set_cct(
                **_parse_set_cct_args(args),
                settings=settings,
                include_events=args.include_events,
            )
        elif args.command == "get-cct":
            response = get_cct(settings=settings, include_events=args.include_events)
        elif args.command in {"set-hsi", "set-hsl"}:
            response = set_hsi(
                **_parse_set_hsi_args(args),
                settings=settings,
                include_events=args.include_events,
            )
        elif args.command in {"get-hsi", "get-hsl"}:
            response = get_hsi(settings=settings, include_events=args.include_events)
        elif args.command == "set-rgb":
            response = set_rgb(
                **_parse_set_rgb_args(args),
                settings=settings,
                include_events=args.include_events,
            )
        elif args.command == "get-rgb":
            response = get_rgb(settings=settings, include_events=args.include_events)
        elif args.command == "node-config":
            response = get_node_config(settings=settings, include_events=args.include_events)
        else:
            raise AssertionError(args.command)
    except Exception as exc:
        print(f"ray120c: {exc}", file=sys.stderr)
        return 1

    _print_json(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
