#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from presets import all_presets, detect_preset, stem_without_png


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
WORK_DIR = ROOT / "work"
OUTPUT_DIR = WORK_DIR / "outputs"
UPLOAD_DIR = WORK_DIR / "uploads"


@dataclass
class ConvertOptions:
    quality: str
    mipmaps: bool
    transfer: str
    metadata: bool
    flags_file: bool
    backend: str


def now_filetime() -> int:
    # Windows FILETIME: 100ns ticks since 1601-01-01.
    return int((time.time() + 11644473600) * 10_000_000)


def safe_filename(name: str) -> str:
    cleaned = Path(name).name.replace("\x00", "")
    allowed = []
    for char in cleaned:
        if char.isalnum() or char in "._- ()[]":
            allowed.append(char)
        else:
            allowed.append("_")
    result = "".join(allowed).strip(" .")
    return result or "texture.png"


def output_name_for(filename: str) -> str:
    path = Path(filename)
    if path.suffix.lower() == ".png":
        return f"{path.name}.KTX2"
    return f"{path.name}.KTX2"


def make_msfs_json(source: Path, flags: tuple[str, ...], has_alpha: bool | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Version": 2,
        "SourceFileDate": now_filetime(),
        "Flags": list(flags),
    }
    if has_alpha:
        payload["HasTransp"] = True
    return payload


def make_flags_line(flags: tuple[str, ...], quality: str) -> str:
    values = ["QUALITYHIGH"] if quality == "high" else []
    if "FL_BITMAP_TANGENT_DXT5N" in flags:
        values.append("TANGENTDXT5N")
    if "FL_BITMAP_NO_GAMMA_CORRECTION" in flags:
        values.append("NOGAMMACORRECTION")
    return "_DEFAULT=" + "+".join(values)


def read_png_has_alpha(path: Path) -> bool:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 33:
        return False
    color_type = data[25]
    return color_type in {4, 6}


def run_basisu(input_path: Path, output_path: Path, options: ConvertOptions) -> tuple[list[str], str]:
    basisu = shutil.which("basisu")
    if not basisu:
        raise RuntimeError("找不到 basisu，请先安装或把 basisu 放进 PATH")

    preset = detect_preset(input_path)
    transfer = preset.transfer if options.transfer == "auto" else options.transfer
    args = preset.basis_args(options.quality, options.mipmaps, transfer)
    command = [
        basisu,
        "-file",
        str(input_path),
        "-output_file",
        str(output_path),
        *args,
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    log = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(log or f"basisu 退出码 {result.returncode}")
    return command, log


def write_metadata(input_path: Path, output_path: Path, options: ConvertOptions) -> dict[str, Path]:
    preset = detect_preset(input_path)
    has_alpha = read_png_has_alpha(input_path)
    written: dict[str, Path] = {}

    if options.metadata:
        payload = make_msfs_json(input_path, preset.flags, has_alpha)
        json_path = Path(f"{output_path}.json")
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        written["json"] = json_path

    if options.flags_file:
        flags_path = output_path.with_suffix(output_path.suffix + ".FLAGS")
        flags_path.write_text(make_flags_line(preset.flags, options.quality) + "\n", encoding="utf-8")
        written["flags"] = flags_path

    return written


def convert_one(upload_path: Path, original_name: str, options: ConvertOptions) -> dict[str, Any]:
    preset = detect_preset(Path(original_name))
    output_name = output_name_for(original_name)
    output_path = OUTPUT_DIR / output_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if options.backend == "sdk":
        raise RuntimeError(
            "SDK Exact 模式需要配置 MSFS 官方 Package Tool/SDK 编译器路径。"
            "当前工具先提供 basisu 稳定模式；要做到官方字节完全一致必须走 SDK 编译器。"
        )

    command, log = run_basisu(upload_path, output_path, options)
    metadata = write_metadata(upload_path, output_path, options)

    return {
        "id": uuid.uuid4().hex,
        "sourceName": original_name,
        "preset": preset.key,
        "presetLabel": preset.label,
        "transfer": preset.transfer if options.transfer == "auto" else options.transfer,
        "quality": options.quality,
        "outputs": {
            "ktx2": f"/outputs/{output_path.name}",
            **{key: f"/outputs/{path.name}" for key, path in metadata.items()},
        },
        "command": command,
        "log": log,
        "status": "ok",
    }


def parse_multipart(headers: dict[str, str], body: bytes) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    content_type = headers.get("content-type", "")
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("请求缺少 multipart boundary")
    boundary = content_type.split(marker, 1)[1].strip().strip('"').encode()
    parts = body.split(b"--" + boundary)
    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []

    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        header_blob, _, payload = part.partition(b"\r\n\r\n")
        if not header_blob:
            continue
        header_lines = header_blob.decode("utf-8", errors="replace").split("\r\n")
        disposition = ""
        for line in header_lines:
            if line.lower().startswith("content-disposition:"):
                disposition = line
                break
        if not disposition:
            continue
        attrs: dict[str, str] = {}
        for item in disposition.split(";")[1:]:
            if "=" in item:
                key, value = item.strip().split("=", 1)
                attrs[key] = value.strip().strip('"')
        name = attrs.get("name", "")
        filename = attrs.get("filename")
        if filename:
            files.append((safe_filename(filename), payload))
        else:
            fields[name] = payload.decode("utf-8", errors="replace")

    return fields, files


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/presets":
            self.send_json(
                {
                    "presets": all_presets(),
                    "basisu": shutil.which("basisu"),
                    "sdkExactAvailable": False,
                }
            )
            return
        if parsed.path.startswith("/outputs/"):
            self.serve_file(OUTPUT_DIR / Path(parsed.path).name)
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            self.serve_file(STATIC_DIR / "index.html")
            return
        static_path = STATIC_DIR / parsed.path.lstrip("/")
        self.serve_file(static_path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/convert":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            fields, files = parse_multipart(
                {key.lower(): value for key, value in self.headers.items()},
                body,
            )
            options = ConvertOptions(
                quality=fields.get("quality", "high"),
                mipmaps=fields.get("mipmaps", "true") == "true",
                transfer=fields.get("transfer", "auto"),
                metadata=fields.get("metadata", "true") == "true",
                flags_file=fields.get("flagsFile", "true") == "true",
                backend=fields.get("backend", "basisu"),
            )
            if options.quality not in {"fast", "high"}:
                raise ValueError("quality 必须是 fast 或 high")
            if options.transfer not in {"auto", "srgb", "linear"}:
                raise ValueError("transfer 必须是 auto、srgb 或 linear")
            if options.backend not in {"basisu", "sdk"}:
                raise ValueError("backend 必须是 basisu 或 sdk")

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            results: list[dict[str, Any]] = []
            for filename, content in files:
                if not filename.lower().endswith(".png"):
                    results.append({"sourceName": filename, "status": "error", "error": "只支持 PNG 输入"})
                    continue
                upload_path = UPLOAD_DIR / f"{uuid.uuid4().hex}_{filename}"
                upload_path.write_bytes(content)
                try:
                    results.append(convert_one(upload_path, filename, options))
                except Exception as exc:
                    results.append({"sourceName": filename, "status": "error", "error": str(exc)})

            self.send_json({"results": results})
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=400)


def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("MSFS_TEXTURE_TOOL_PORT", "8787"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"MSFS Texture Tool running: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
