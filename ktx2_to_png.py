#!/usr/bin/env python3
"""
将 MSFS 生成的 KTX2 纹理转换为 PNG。

命名规则：
    A320_FUSE_1_ALBD.PNG.KTX2 -> A320_FUSE_1_ALBD.PNG

说明：
    当前脚本直接解析 KTX2 的第 0 级 mip，并按 BC1/DXT1 标准解码到 RGBA8。
    全程不做缩放、滤镜、伽马校正或色彩空间转换，PNG 写出为无损压缩。
"""

from __future__ import annotations

import argparse
import binascii
import ctypes
import hashlib
import json
import math
import os
import platform
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path


KTX2_IDENTIFIER = b"\xabKTX 20\xbb\r\n\x1a\n"

# MSFS 常见 block-compressed KTX2 格式。
SUPPORTED_FORMATS = {
    131: {"name": "VK_FORMAT_BC1_RGB_UNORM_BLOCK", "decoder": "bc1"},
    132: {"name": "VK_FORMAT_BC1_RGB_SRGB_BLOCK", "decoder": "bc1"},
    133: {"name": "VK_FORMAT_BC1_RGBA_UNORM_BLOCK", "decoder": "bc1"},
    134: {"name": "VK_FORMAT_BC1_RGBA_SRGB_BLOCK", "decoder": "bc1"},
    137: {"name": "VK_FORMAT_BC3_UNORM_BLOCK", "decoder": "bc3"},
    138: {"name": "VK_FORMAT_BC3_SRGB_BLOCK", "decoder": "bc3"},
    142: {"name": "VK_FORMAT_BC5_SNORM_BLOCK", "decoder": "bc5_snorm"},
}

# 旧脚本和反向脚本可能导入这个名字，保留为兼容别名。
SUPPORTED_BC1_FORMATS = {
    key: value["name"] for key, value in SUPPORTED_FORMATS.items()
}


class Ktx2Error(Exception):
    """KTX2 解析或转换错误。"""


C_DECODER_SOURCE = r"""
#include <math.h>
#include <stdint.h>
#include <stdlib.h>

static uint8_t expand5(uint16_t value) {
    return (uint8_t)((value << 3) | (value >> 2));
}

static uint8_t expand6(uint16_t value) {
    return (uint8_t)((value << 2) | (value >> 4));
}

static void rgb565_to_rgba(uint16_t color, uint8_t out[4]) {
    out[0] = expand5((color >> 11) & 31);
    out[1] = expand6((color >> 5) & 63);
    out[2] = expand5(color & 31);
    out[3] = 255;
}

static void fill_bc1_palette(
    uint16_t color0,
    uint16_t color1,
    int four_color,
    uint8_t palette[4][4]
) {
    rgb565_to_rgba(color0, palette[0]);
    rgb565_to_rgba(color1, palette[1]);

    if (four_color || color0 > color1) {
        palette[2][0] = (uint8_t)((2 * palette[0][0] + palette[1][0]) / 3);
        palette[2][1] = (uint8_t)((2 * palette[0][1] + palette[1][1]) / 3);
        palette[2][2] = (uint8_t)((2 * palette[0][2] + palette[1][2]) / 3);
        palette[2][3] = 255;

        palette[3][0] = (uint8_t)((palette[0][0] + 2 * palette[1][0]) / 3);
        palette[3][1] = (uint8_t)((palette[0][1] + 2 * palette[1][1]) / 3);
        palette[3][2] = (uint8_t)((palette[0][2] + 2 * palette[1][2]) / 3);
        palette[3][3] = 255;
    } else {
        palette[2][0] = (uint8_t)((palette[0][0] + palette[1][0]) / 2);
        palette[2][1] = (uint8_t)((palette[0][1] + palette[1][1]) / 2);
        palette[2][2] = (uint8_t)((palette[0][2] + palette[1][2]) / 2);
        palette[2][3] = 255;

        palette[3][0] = 0;
        palette[3][1] = 0;
        palette[3][2] = 0;
        palette[3][3] = 0;
    }
}

int decode_bc1(
    const uint8_t *input,
    size_t input_size,
    uint32_t width,
    uint32_t height,
    uint8_t *output,
    size_t output_size
) {
    if (!input || !output || width == 0 || height == 0) {
        return 1;
    }

    uint32_t blocks_x = (width + 3) / 4;
    uint32_t blocks_y = (height + 3) / 4;
    size_t expected_input = (size_t)blocks_x * (size_t)blocks_y * 8u;
    size_t expected_output = (size_t)width * (size_t)height * 4u;
    if (input_size < expected_input || output_size < expected_output) {
        return 2;
    }

    size_t offset = 0;
    for (uint32_t block_y = 0; block_y < blocks_y; ++block_y) {
        for (uint32_t block_x = 0; block_x < blocks_x; ++block_x) {
            uint16_t color0 = (uint16_t)(input[offset] | (input[offset + 1] << 8));
            uint16_t color1 = (uint16_t)(input[offset + 2] | (input[offset + 3] << 8));
            uint32_t indices =
                (uint32_t)input[offset + 4] |
                ((uint32_t)input[offset + 5] << 8) |
                ((uint32_t)input[offset + 6] << 16) |
                ((uint32_t)input[offset + 7] << 24);
            offset += 8;

            uint8_t palette[4][4];
            fill_bc1_palette(color0, color1, 0, palette);

            for (uint32_t py = 0; py < 4; ++py) {
                uint32_t y = block_y * 4 + py;
                if (y >= height) {
                    continue;
                }
                for (uint32_t px = 0; px < 4; ++px) {
                    uint32_t x = block_x * 4 + px;
                    if (x >= width) {
                        continue;
                    }
                    uint32_t palette_index = (indices >> (2 * (py * 4 + px))) & 3u;
                    size_t pixel_offset = ((size_t)y * (size_t)width + (size_t)x) * 4u;
                    output[pixel_offset] = palette[palette_index][0];
                    output[pixel_offset + 1] = palette[palette_index][1];
                    output[pixel_offset + 2] = palette[palette_index][2];
                    output[pixel_offset + 3] = palette[palette_index][3];
                }
            }
        }
    }

    return 0;
}

int decode_bc3(
    const uint8_t *input,
    size_t input_size,
    uint32_t width,
    uint32_t height,
    uint8_t *output,
    size_t output_size
) {
    if (!input || !output || width == 0 || height == 0) {
        return 1;
    }

    uint32_t blocks_x = (width + 3) / 4;
    uint32_t blocks_y = (height + 3) / 4;
    size_t expected_input = (size_t)blocks_x * (size_t)blocks_y * 16u;
    size_t expected_output = (size_t)width * (size_t)height * 4u;
    if (input_size < expected_input || output_size < expected_output) {
        return 2;
    }

    size_t offset = 0;
    for (uint32_t block_y = 0; block_y < blocks_y; ++block_y) {
        for (uint32_t block_x = 0; block_x < blocks_x; ++block_x) {
            uint8_t alpha0 = input[offset];
            uint8_t alpha1 = input[offset + 1];
            uint64_t alpha_indices =
                (uint64_t)input[offset + 2] |
                ((uint64_t)input[offset + 3] << 8) |
                ((uint64_t)input[offset + 4] << 16) |
                ((uint64_t)input[offset + 5] << 24) |
                ((uint64_t)input[offset + 6] << 32) |
                ((uint64_t)input[offset + 7] << 40);

            uint8_t alpha_palette[8];
            alpha_palette[0] = alpha0;
            alpha_palette[1] = alpha1;
            if (alpha0 > alpha1) {
                alpha_palette[2] = (uint8_t)((6 * alpha0 + alpha1) / 7);
                alpha_palette[3] = (uint8_t)((5 * alpha0 + 2 * alpha1) / 7);
                alpha_palette[4] = (uint8_t)((4 * alpha0 + 3 * alpha1) / 7);
                alpha_palette[5] = (uint8_t)((3 * alpha0 + 4 * alpha1) / 7);
                alpha_palette[6] = (uint8_t)((2 * alpha0 + 5 * alpha1) / 7);
                alpha_palette[7] = (uint8_t)((alpha0 + 6 * alpha1) / 7);
            } else {
                alpha_palette[2] = (uint8_t)((4 * alpha0 + alpha1) / 5);
                alpha_palette[3] = (uint8_t)((3 * alpha0 + 2 * alpha1) / 5);
                alpha_palette[4] = (uint8_t)((2 * alpha0 + 3 * alpha1) / 5);
                alpha_palette[5] = (uint8_t)((alpha0 + 4 * alpha1) / 5);
                alpha_palette[6] = 0;
                alpha_palette[7] = 255;
            }

            uint16_t color0 = (uint16_t)(input[offset + 8] | (input[offset + 9] << 8));
            uint16_t color1 = (uint16_t)(input[offset + 10] | (input[offset + 11] << 8));
            uint32_t color_indices =
                (uint32_t)input[offset + 12] |
                ((uint32_t)input[offset + 13] << 8) |
                ((uint32_t)input[offset + 14] << 16) |
                ((uint32_t)input[offset + 15] << 24);
            offset += 16;

            uint8_t palette[4][4];
            fill_bc1_palette(color0, color1, 1, palette);

            for (uint32_t py = 0; py < 4; ++py) {
                uint32_t y = block_y * 4 + py;
                if (y >= height) {
                    continue;
                }
                for (uint32_t px = 0; px < 4; ++px) {
                    uint32_t x = block_x * 4 + px;
                    if (x >= width) {
                        continue;
                    }
                    uint32_t i = py * 4 + px;
                    uint32_t color_index = (color_indices >> (2 * i)) & 3u;
                    uint32_t alpha_index = (uint32_t)((alpha_indices >> (3 * i)) & 7u);
                    size_t pixel_offset = ((size_t)y * (size_t)width + (size_t)x) * 4u;
                    output[pixel_offset] = palette[color_index][0];
                    output[pixel_offset + 1] = palette[color_index][1];
                    output[pixel_offset + 2] = palette[color_index][2];
                    output[pixel_offset + 3] = alpha_palette[alpha_index];
                }
            }
        }
    }

    return 0;
}

static float snorm8_to_float(uint8_t value) {
    int8_t signed_value = (int8_t)value;
    if (signed_value <= -127) {
        return -1.0f;
    }
    return (float)signed_value / 127.0f;
}

static uint8_t normal_float_to_byte(float value) {
    if (value < -1.0f) {
        value = -1.0f;
    } else if (value > 1.0f) {
        value = 1.0f;
    }
    return (uint8_t)((value * 0.5f + 0.5f) * 255.0f + 0.5f);
}

static void decode_bc4_snorm_block(const uint8_t *block, float values[16]) {
    float endpoint0 = snorm8_to_float(block[0]);
    float endpoint1 = snorm8_to_float(block[1]);
    int8_t signed0 = (int8_t)block[0];
    int8_t signed1 = (int8_t)block[1];

    float palette[8];
    palette[0] = endpoint0;
    palette[1] = endpoint1;
    if (signed0 > signed1) {
        palette[2] = (6.0f * endpoint0 + endpoint1) / 7.0f;
        palette[3] = (5.0f * endpoint0 + 2.0f * endpoint1) / 7.0f;
        palette[4] = (4.0f * endpoint0 + 3.0f * endpoint1) / 7.0f;
        palette[5] = (3.0f * endpoint0 + 4.0f * endpoint1) / 7.0f;
        palette[6] = (2.0f * endpoint0 + 5.0f * endpoint1) / 7.0f;
        palette[7] = (endpoint0 + 6.0f * endpoint1) / 7.0f;
    } else {
        palette[2] = (4.0f * endpoint0 + endpoint1) / 5.0f;
        palette[3] = (3.0f * endpoint0 + 2.0f * endpoint1) / 5.0f;
        palette[4] = (2.0f * endpoint0 + 3.0f * endpoint1) / 5.0f;
        palette[5] = (endpoint0 + 4.0f * endpoint1) / 5.0f;
        palette[6] = -1.0f;
        palette[7] = 1.0f;
    }

    uint64_t indices =
        (uint64_t)block[2] |
        ((uint64_t)block[3] << 8) |
        ((uint64_t)block[4] << 16) |
        ((uint64_t)block[5] << 24) |
        ((uint64_t)block[6] << 32) |
        ((uint64_t)block[7] << 40);

    for (uint32_t i = 0; i < 16; ++i) {
        uint32_t index = (uint32_t)((indices >> (3 * i)) & 7u);
        values[i] = palette[index];
    }
}

int decode_bc5_snorm(
    const uint8_t *input,
    size_t input_size,
    uint32_t width,
    uint32_t height,
    uint8_t *output,
    size_t output_size
) {
    if (!input || !output || width == 0 || height == 0) {
        return 1;
    }

    uint32_t blocks_x = (width + 3) / 4;
    uint32_t blocks_y = (height + 3) / 4;
    size_t expected_input = (size_t)blocks_x * (size_t)blocks_y * 16u;
    size_t expected_output = (size_t)width * (size_t)height * 4u;
    if (input_size < expected_input || output_size < expected_output) {
        return 2;
    }

    size_t offset = 0;
    for (uint32_t block_y = 0; block_y < blocks_y; ++block_y) {
        for (uint32_t block_x = 0; block_x < blocks_x; ++block_x) {
            float xs[16];
            float ys[16];
            decode_bc4_snorm_block(input + offset, xs);
            decode_bc4_snorm_block(input + offset + 8, ys);
            offset += 16;

            for (uint32_t py = 0; py < 4; ++py) {
                uint32_t y = block_y * 4 + py;
                if (y >= height) {
                    continue;
                }
                for (uint32_t px = 0; px < 4; ++px) {
                    uint32_t x = block_x * 4 + px;
                    if (x >= width) {
                        continue;
                    }
                    uint32_t i = py * 4 + px;
                    float nx = xs[i];
                    float ny = ys[i];
                    float nz2 = 1.0f - nx * nx - ny * ny;
                    float nz = nz2 > 0.0f ? sqrtf(nz2) : 0.0f;
                    size_t pixel_offset = ((size_t)y * (size_t)width + (size_t)x) * 4u;
                    output[pixel_offset] = normal_float_to_byte(nx);
                    output[pixel_offset + 1] = normal_float_to_byte(ny);
                    output[pixel_offset + 2] = normal_float_to_byte(nz);
                    output[pixel_offset + 3] = 255;
                }
            }
        }
    }

    return 0;
}
"""

_C_DECODER: ctypes.CDLL | None = None
_C_DECODER_TRIED = False


def c_decoder_extension() -> str:
    if sys.platform == "darwin":
        return ".dylib"
    if os.name == "nt":
        return ".dll"
    return ".so"


def compile_c_decoder() -> Path | None:
    """编译内嵌 C 解码器；失败时返回 None 并使用 Python 兜底。"""
    compiler = os.environ.get("CC") or "cc"
    cache_key = hashlib.sha256(
        (C_DECODER_SOURCE + platform.platform()).encode("utf-8")
    ).hexdigest()[:16]
    cache_dir = Path(tempfile.gettempdir()) / "ktx2_png_decoder"
    source_path = cache_dir / f"bc1_decoder_{cache_key}.c"
    library_path = cache_dir / f"bc1_decoder_{cache_key}{c_decoder_extension()}"

    if library_path.exists():
        return library_path

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        source_path.write_text(C_DECODER_SOURCE, encoding="utf-8")
        if os.name == "nt":
            return None
        command = [
            compiler,
            "-O3",
            "-shared",
            "-fPIC",
            str(source_path),
            "-o",
            str(library_path),
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError):
        return None

    return library_path


def load_c_decoder() -> ctypes.CDLL | None:
    global _C_DECODER, _C_DECODER_TRIED
    if _C_DECODER_TRIED:
        return _C_DECODER
    _C_DECODER_TRIED = True

    library_path = compile_c_decoder()
    if library_path is None:
        return None

    try:
        library = ctypes.CDLL(str(library_path))
        library.decode_bc1.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        library.decode_bc1.restype = ctypes.c_int
        library.decode_bc3.argtypes = library.decode_bc1.argtypes
        library.decode_bc3.restype = ctypes.c_int
        library.decode_bc5_snorm.argtypes = library.decode_bc1.argtypes
        library.decode_bc5_snorm.restype = ctypes.c_int
    except OSError:
        return None

    _C_DECODER = library
    return _C_DECODER


def _read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _read_u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def parse_ktx2(data: bytes) -> dict[str, int]:
    """解析 KTX2 头部和第 0 级 mip 信息。"""
    if len(data) < 80:
        raise Ktx2Error("文件太小，不是有效的 KTX2 文件")
    if data[:12] != KTX2_IDENTIFIER:
        raise Ktx2Error("文件标识不是 KTX2")

    header = {
        "vk_format": _read_u32(data, 12),
        "type_size": _read_u32(data, 16),
        "width": _read_u32(data, 20),
        "height": _read_u32(data, 24),
        "depth": _read_u32(data, 28),
        "layer_count": _read_u32(data, 32),
        "face_count": _read_u32(data, 36),
        "level_count": _read_u32(data, 40),
        "supercompression": _read_u32(data, 44),
    }

    if header["width"] <= 0 or header["height"] <= 0:
        raise Ktx2Error("纹理宽高无效")
    if header["depth"] not in (0, 1):
        raise Ktx2Error("当前脚本仅支持 2D 纹理")
    if header["layer_count"] not in (0, 1):
        raise Ktx2Error("当前脚本仅支持非数组 2D 纹理")
    if header["face_count"] != 1:
        raise Ktx2Error("当前脚本仅支持普通 2D 纹理，不支持 cubemap")
    if header["level_count"] <= 0:
        raise Ktx2Error("KTX2 文件缺少 mip level")
    if header["supercompression"] != 0:
        raise Ktx2Error("当前脚本仅支持未 supercompress 的 KTX2 文件")

    level_index_offset = 80
    level0_offset = _read_u64(data, level_index_offset)
    level0_length = _read_u64(data, level_index_offset + 8)
    level0_uncompressed = _read_u64(data, level_index_offset + 16)

    if level0_offset <= 0 or level0_length <= 0:
        raise Ktx2Error("第 0 级 mip 数据索引无效")
    if level0_offset + level0_length > len(data):
        raise Ktx2Error("第 0 级 mip 数据超出文件范围")
    if level0_uncompressed and level0_uncompressed != level0_length:
        raise Ktx2Error("第 0 级 mip 数据长度与未压缩长度不一致")

    header["level0_offset"] = level0_offset
    header["level0_length"] = level0_length
    return header


def rgb565_to_rgba(color: int) -> tuple[int, int, int, int]:
    """将 RGB565 扩展到 RGBA8888。"""
    r = (color >> 11) & 0x1F
    g = (color >> 5) & 0x3F
    b = color & 0x1F
    return (
        (r << 3) | (r >> 2),
        (g << 2) | (g >> 4),
        (b << 3) | (b >> 2),
        255,
    )


def expected_block_size(decoder: str) -> int:
    if decoder == "bc1":
        return 8
    if decoder in {"bc3", "bc5_snorm"}:
        return 16
    raise Ktx2Error(f"未知解码器：{decoder}")


def decode_with_c(data: bytes, width: int, height: int, function_name: str) -> bytes | None:
    c_decoder = load_c_decoder()
    if c_decoder is None:
        return None

    output = bytearray(width * height * 4)
    input_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    output_buffer = (ctypes.c_ubyte * len(output)).from_buffer(output)
    function = getattr(c_decoder, function_name)
    result = function(
        ctypes.cast(input_buffer, ctypes.c_void_p),
        len(data),
        width,
        height,
        ctypes.cast(output_buffer, ctypes.c_void_p),
        len(output),
    )
    if result != 0:
        raise Ktx2Error(f"C 解码器 {function_name} 失败，错误码：{result}")
    return bytes(output)


def decode_bc1(data: bytes, width: int, height: int) -> bytes:
    """将 BC1/DXT1 block 数据解码为 RGBA8 像素。"""
    blocks_x = math.ceil(width / 4)
    blocks_y = math.ceil(height / 4)
    expected_size = blocks_x * blocks_y * expected_block_size("bc1")
    if len(data) < expected_size:
        raise Ktx2Error(
            f"BC1 数据长度不足：需要 {expected_size} 字节，实际 {len(data)} 字节"
        )

    c_result = decode_with_c(data, width, height, "decode_bc1")
    if c_result is not None:
        return c_result

    pixels = bytearray(width * height * 4)
    offset = 0

    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            color0, color1, indices = struct.unpack_from("<HHI", data, offset)
            offset += 8

            c0 = rgb565_to_rgba(color0)
            c1 = rgb565_to_rgba(color1)
            palette = [c0, c1]

            if color0 > color1:
                palette.append(
                    (
                        (2 * c0[0] + c1[0]) // 3,
                        (2 * c0[1] + c1[1]) // 3,
                        (2 * c0[2] + c1[2]) // 3,
                        255,
                    )
                )
                palette.append(
                    (
                        (c0[0] + 2 * c1[0]) // 3,
                        (c0[1] + 2 * c1[1]) // 3,
                        (c0[2] + 2 * c1[2]) // 3,
                        255,
                    )
                )
            else:
                palette.append(
                    (
                        (c0[0] + c1[0]) // 2,
                        (c0[1] + c1[1]) // 2,
                        (c0[2] + c1[2]) // 2,
                        255,
                    )
                )
                palette.append((0, 0, 0, 0))

            for py in range(4):
                y = block_y * 4 + py
                if y >= height:
                    continue
                for px in range(4):
                    x = block_x * 4 + px
                    if x >= width:
                        continue
                    palette_index = (indices >> (2 * (py * 4 + px))) & 0x03
                    pixel_offset = (y * width + x) * 4
                    pixels[pixel_offset : pixel_offset + 4] = bytes(
                        palette[palette_index]
                    )

    return bytes(pixels)


def decode_bc3(data: bytes, width: int, height: int) -> bytes:
    """将 BC3/DXT5 block 数据解码为 RGBA8 像素。"""
    blocks_x = math.ceil(width / 4)
    blocks_y = math.ceil(height / 4)
    expected_size = blocks_x * blocks_y * expected_block_size("bc3")
    if len(data) < expected_size:
        raise Ktx2Error(
            f"BC3 数据长度不足：需要 {expected_size} 字节，实际 {len(data)} 字节"
        )

    c_result = decode_with_c(data, width, height, "decode_bc3")
    if c_result is not None:
        return c_result

    raise Ktx2Error("BC3 解码需要可用的 C 编译器/动态库")


def decode_bc5_snorm(data: bytes, width: int, height: int) -> bytes:
    """将 BC5_SNORM 解码为可编辑的 RGBA normal-map PNG。"""
    blocks_x = math.ceil(width / 4)
    blocks_y = math.ceil(height / 4)
    expected_size = blocks_x * blocks_y * expected_block_size("bc5_snorm")
    if len(data) < expected_size:
        raise Ktx2Error(
            f"BC5 数据长度不足：需要 {expected_size} 字节，实际 {len(data)} 字节"
        )

    c_result = decode_with_c(data, width, height, "decode_bc5_snorm")
    if c_result is not None:
        return c_result

    raise Ktx2Error("BC5_SNORM 解码需要可用的 C 编译器/动态库")


def decode_texture(data: bytes, width: int, height: int, decoder: str) -> bytes:
    if decoder == "bc1":
        return decode_bc1(data, width, height)
    if decoder == "bc3":
        return decode_bc3(data, width, height)
    if decoder == "bc5_snorm":
        return decode_bc5_snorm(data, width, height)
    raise Ktx2Error(f"未知解码器：{decoder}")


def png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(chunk_type)
    crc = binascii.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def write_png(path: Path, width: int, height: int, rgba: bytes, compression: int) -> None:
    """写出 RGBA8 PNG，不添加会改变颜色解释的 gAMA/sRGB chunk。"""
    if len(rgba) != width * height * 4:
        raise Ktx2Error("RGBA 像素数据长度与图片尺寸不匹配")
    if not 0 <= compression <= 9:
        raise Ktx2Error("PNG 压缩级别必须在 0 到 9 之间")

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        # 每行 filter type 0，避免任何预测滤波带来的实现差异。
        raw.append(0)
        start = y * stride
        raw.extend(rgba[start : start + stride])

    png = bytearray()
    png.extend(b"\x89PNG\r\n\x1a\n")
    png.extend(
        png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
        )
    )
    png.extend(png_chunk(b"IDAT", zlib.compress(bytes(raw), level=compression)))
    png.extend(png_chunk(b"IEND", b""))

    path.write_bytes(bytes(png))


def write_manifest(png_path: Path, ktx2_path: Path, rgba: bytes, info: dict[str, int]) -> None:
    """记录转换来源，供反向脚本快速判断 PNG 是否未改动。"""
    manifest_path = png_path.with_suffix(png_path.suffix + ".ktx2png.json")
    manifest = {
        "source_ktx2": str(ktx2_path.resolve()),
        "png": str(png_path.resolve()),
        "width": info["width"],
        "height": info["height"],
        "vk_format": info["vk_format"],
        "rgba_sha256": hashlib.sha256(rgba).hexdigest(),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def output_name_for(input_path: Path, output_dir: Path | None) -> Path:
    if input_path.suffix.lower() != ".ktx2":
        raise Ktx2Error("输入文件后缀必须是 .ktx2")
    filename = input_path.name[: -len(input_path.suffix)]
    return (output_dir if output_dir else input_path.parent) / filename


def convert_file(
    input_path: Path,
    output_dir: Path | None,
    overwrite: bool,
    compression: int,
) -> Path:
    output_path = output_name_for(input_path, output_dir)
    if output_path.exists() and not overwrite:
        raise Ktx2Error(f"输出文件已存在，请使用 --overwrite 覆盖：{output_path}")

    data = input_path.read_bytes()
    info = parse_ktx2(data)
    vk_format = info["vk_format"]
    format_info = SUPPORTED_FORMATS.get(vk_format)
    if format_info is None:
        supported = ", ".join(value["name"] for value in SUPPORTED_FORMATS.values())
        raise Ktx2Error(
            f"暂不支持 vkFormat={vk_format}。当前支持：{supported}"
        )

    start = info["level0_offset"]
    end = start + info["level0_length"]
    level0 = data[start:end]
    rgba = decode_texture(
        level0,
        info["width"],
        info["height"],
        str(format_info["decoder"]),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_png(output_path, info["width"], info["height"], rgba, compression)
    write_manifest(output_path, input_path, rgba, info)
    return output_path


def find_inputs(paths: list[Path]) -> list[Path]:
    inputs: list[Path] = []
    for path in paths:
        if path.is_dir():
            inputs.extend(sorted(path.glob("*.ktx2")))
            inputs.extend(sorted(path.glob("*.KTX2")))
        else:
            inputs.append(path)
    return inputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将 MSFS KTX2 文件转换为 PNG，输出文件名会去掉最后的 .ktx2 后缀。"
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="要转换的 .ktx2 文件或目录；不填写时转换当前目录下的 .ktx2/.KTX2",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="输出目录；不填写时输出到输入文件所在目录",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已存在的输出 PNG",
    )
    parser.add_argument(
        "--compression",
        type=int,
        default=1,
        choices=range(10),
        metavar="0-9",
        help="PNG 无损压缩级别，0 最快但最大，9 最小但最慢；默认 1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    search_paths = args.inputs if args.inputs else [Path.cwd()]
    inputs = find_inputs(search_paths)

    if not inputs:
        print("未找到 .ktx2/.KTX2 文件", file=sys.stderr)
        return 1

    failed = 0
    for input_path in inputs:
        try:
            output_path = convert_file(
                input_path,
                args.output_dir,
                args.overwrite,
                args.compression,
            )
        except Ktx2Error as exc:
            failed += 1
            print(f"❌ {input_path}: {exc}", file=sys.stderr)
            continue
        print(f"✅ {input_path} -> {output_path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
