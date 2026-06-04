#!/usr/bin/env python3
"""
将 PNG 转回 MSFS 可用的 KTX2。

重要说明：
    1. 如果 PNG 是由 main.py 从同名 KTX2 解码得到且未改动，本脚本会直接复用
       原始 KTX2，包含全部 mip 和 ASOBO 元数据，结果 100% 一致。
    2. 如果 PNG 已经改动，只有加 --encode-modified 才会按参考 KTX2 的格式重编码。
       BC1/BC3/BC5 都是有损压缩，重新编码不可能保证完全无色差。
"""

from __future__ import annotations

import argparse
import binascii
import ctypes
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from main import KTX2_IDENTIFIER, Ktx2Error, SUPPORTED_FORMATS, decode_texture


DEFAULT_FORMAT_NAME = "BC1_RGBA_UNORM_BLOCK"


ENCODER_SOURCE = r"""
#include <stdint.h>
#include <stddef.h>

static uint16_t pack_rgb565(uint8_t r, uint8_t g, uint8_t b) {
    uint16_t rr = (uint16_t)((r * 31u + 127u) / 255u);
    uint16_t gg = (uint16_t)((g * 63u + 127u) / 255u);
    uint16_t bb = (uint16_t)((b * 31u + 127u) / 255u);
    return (uint16_t)((rr << 11) | (gg << 5) | bb);
}

static uint8_t expand5(uint16_t value) {
    return (uint8_t)((value << 3) | (value >> 2));
}

static uint8_t expand6(uint16_t value) {
    return (uint8_t)((value << 2) | (value >> 4));
}

static void unpack_rgb565(uint16_t color, uint8_t out[4]) {
    out[0] = expand5((color >> 11) & 31u);
    out[1] = expand6((color >> 5) & 63u);
    out[2] = expand5(color & 31u);
    out[3] = 255u;
}

static int color_distance(const uint8_t *a, const uint8_t *b) {
    int dr = (int)a[0] - (int)b[0];
    int dg = (int)a[1] - (int)b[1];
    int db = (int)a[2] - (int)b[2];
    return dr * dr + dg * dg + db * db;
}

static void choose_endpoints(
    const uint8_t colors[16][4],
    const uint8_t opaque[16],
    uint8_t endpoint0[3],
    uint8_t endpoint1[3]
) {
    int count = 0;
    int min_r = 255, min_g = 255, min_b = 255;
    int max_r = 0, max_g = 0, max_b = 0;

    for (int i = 0; i < 16; ++i) {
        if (!opaque[i]) {
            continue;
        }
        int r = colors[i][0];
        int g = colors[i][1];
        int b = colors[i][2];
        if (r < min_r) min_r = r;
        if (g < min_g) min_g = g;
        if (b < min_b) min_b = b;
        if (r > max_r) max_r = r;
        if (g > max_g) max_g = g;
        if (b > max_b) max_b = b;
        ++count;
    }

    if (count == 0) {
        endpoint0[0] = endpoint0[1] = endpoint0[2] = 0;
        endpoint1[0] = endpoint1[1] = endpoint1[2] = 0;
        return;
    }

    int axis_r = max_r - min_r;
    int axis_g = max_g - min_g;
    int axis_b = max_b - min_b;
    int best_min = 2147483647;
    int best_max = -2147483647;
    int min_index = -1;
    int max_index = -1;

    for (int i = 0; i < 16; ++i) {
        if (!opaque[i]) {
            continue;
        }
        int projection =
            (int)colors[i][0] * axis_r +
            (int)colors[i][1] * axis_g +
            (int)colors[i][2] * axis_b;
        if (projection < best_min) {
            best_min = projection;
            min_index = i;
        }
        if (projection > best_max) {
            best_max = projection;
            max_index = i;
        }
    }

    if (min_index < 0 || max_index < 0) {
        endpoint0[0] = (uint8_t)max_r;
        endpoint0[1] = (uint8_t)max_g;
        endpoint0[2] = (uint8_t)max_b;
        endpoint1[0] = (uint8_t)min_r;
        endpoint1[1] = (uint8_t)min_g;
        endpoint1[2] = (uint8_t)min_b;
        return;
    }

    endpoint0[0] = colors[max_index][0];
    endpoint0[1] = colors[max_index][1];
    endpoint0[2] = colors[max_index][2];
    endpoint1[0] = colors[min_index][0];
    endpoint1[1] = colors[min_index][1];
    endpoint1[2] = colors[min_index][2];
}

int downsample_rgba(
    const uint8_t *input,
    uint32_t width,
    uint32_t height,
    uint8_t *output,
    uint32_t out_width,
    uint32_t out_height
) {
    if (!input || !output || width == 0 || height == 0 || out_width == 0 || out_height == 0) {
        return 1;
    }

    for (uint32_t y = 0; y < out_height; ++y) {
        for (uint32_t x = 0; x < out_width; ++x) {
            uint32_t start_x = x * 2u;
            uint32_t start_y = y * 2u;
            uint32_t end_x = start_x + 2u;
            uint32_t end_y = start_y + 2u;
            if (end_x > width) end_x = width;
            if (end_y > height) end_y = height;

            uint32_t sum[4] = {0, 0, 0, 0};
            uint32_t count = 0;
            for (uint32_t sy = start_y; sy < end_y; ++sy) {
                for (uint32_t sx = start_x; sx < end_x; ++sx) {
                    size_t offset = ((size_t)sy * (size_t)width + (size_t)sx) * 4u;
                    sum[0] += input[offset];
                    sum[1] += input[offset + 1];
                    sum[2] += input[offset + 2];
                    sum[3] += input[offset + 3];
                    ++count;
                }
            }

            size_t out_offset = ((size_t)y * (size_t)out_width + (size_t)x) * 4u;
            output[out_offset] = (uint8_t)((sum[0] + count / 2u) / count);
            output[out_offset + 1] = (uint8_t)((sum[1] + count / 2u) / count);
            output[out_offset + 2] = (uint8_t)((sum[2] + count / 2u) / count);
            output[out_offset + 3] = (uint8_t)((sum[3] + count / 2u) / count);
        }
    }

    return 0;
}

int encode_bc1(
    const uint8_t *rgba,
    uint32_t width,
    uint32_t height,
    uint8_t *output,
    size_t output_size
) {
    if (!rgba || !output || width == 0 || height == 0) {
        return 1;
    }

    uint32_t blocks_x = (width + 3u) / 4u;
    uint32_t blocks_y = (height + 3u) / 4u;
    size_t expected_output = (size_t)blocks_x * (size_t)blocks_y * 8u;
    if (output_size < expected_output) {
        return 2;
    }

    size_t out_offset = 0;
    for (uint32_t block_y = 0; block_y < blocks_y; ++block_y) {
        for (uint32_t block_x = 0; block_x < blocks_x; ++block_x) {
            uint8_t colors[16][4];
            uint8_t opaque[16];
            int has_alpha = 0;
            int opaque_count = 0;

            for (uint32_t py = 0; py < 4; ++py) {
                for (uint32_t px = 0; px < 4; ++px) {
                    uint32_t x = block_x * 4u + px;
                    uint32_t y = block_y * 4u + py;
                    int idx = (int)(py * 4u + px);
                    if (x < width && y < height) {
                        size_t pixel_offset = ((size_t)y * (size_t)width + (size_t)x) * 4u;
                        colors[idx][0] = rgba[pixel_offset];
                        colors[idx][1] = rgba[pixel_offset + 1];
                        colors[idx][2] = rgba[pixel_offset + 2];
                        colors[idx][3] = rgba[pixel_offset + 3];
                    } else {
                        colors[idx][0] = colors[idx][1] = colors[idx][2] = 0;
                        colors[idx][3] = 0;
                    }

                    opaque[idx] = colors[idx][3] >= 128u ? 1u : 0u;
                    if (!opaque[idx]) {
                        has_alpha = 1;
                    } else {
                        ++opaque_count;
                    }
                }
            }

            uint8_t endpoint0[3];
            uint8_t endpoint1[3];
            choose_endpoints(colors, opaque, endpoint0, endpoint1);

            uint16_t color0 = pack_rgb565(endpoint0[0], endpoint0[1], endpoint0[2]);
            uint16_t color1 = pack_rgb565(endpoint1[0], endpoint1[1], endpoint1[2]);

            if (has_alpha) {
                if (color0 > color1) {
                    uint16_t tmp = color0;
                    color0 = color1;
                    color1 = tmp;
                }
            } else {
                if (color0 < color1) {
                    uint16_t tmp = color0;
                    color0 = color1;
                    color1 = tmp;
                }
            }

            uint8_t palette[4][4];
            unpack_rgb565(color0, palette[0]);
            unpack_rgb565(color1, palette[1]);

            if (color0 > color1) {
                palette[2][0] = (uint8_t)((2u * palette[0][0] + palette[1][0]) / 3u);
                palette[2][1] = (uint8_t)((2u * palette[0][1] + palette[1][1]) / 3u);
                palette[2][2] = (uint8_t)((2u * palette[0][2] + palette[1][2]) / 3u);
                palette[2][3] = 255u;
                palette[3][0] = (uint8_t)((palette[0][0] + 2u * palette[1][0]) / 3u);
                palette[3][1] = (uint8_t)((palette[0][1] + 2u * palette[1][1]) / 3u);
                palette[3][2] = (uint8_t)((palette[0][2] + 2u * palette[1][2]) / 3u);
                palette[3][3] = 255u;
            } else {
                palette[2][0] = (uint8_t)((palette[0][0] + palette[1][0]) / 2u);
                palette[2][1] = (uint8_t)((palette[0][1] + palette[1][1]) / 2u);
                palette[2][2] = (uint8_t)((palette[0][2] + palette[1][2]) / 2u);
                palette[2][3] = 255u;
                palette[3][0] = palette[3][1] = palette[3][2] = palette[3][3] = 0u;
            }

            uint32_t indices = 0;
            for (int i = 0; i < 16; ++i) {
                uint32_t selected = 0;
                if (has_alpha && !opaque[i]) {
                    selected = 3;
                } else if (opaque_count == 0) {
                    selected = 3;
                } else {
                    int limit = color0 > color1 ? 4 : 3;
                    int best_distance = 2147483647;
                    for (int p = 0; p < limit; ++p) {
                        int distance = color_distance(colors[i], palette[p]);
                        if (distance < best_distance) {
                            best_distance = distance;
                            selected = (uint32_t)p;
                        }
                    }
                }
                indices |= selected << (2u * (uint32_t)i);
            }

            output[out_offset] = (uint8_t)(color0 & 255u);
            output[out_offset + 1] = (uint8_t)(color0 >> 8);
            output[out_offset + 2] = (uint8_t)(color1 & 255u);
            output[out_offset + 3] = (uint8_t)(color1 >> 8);
            output[out_offset + 4] = (uint8_t)(indices & 255u);
            output[out_offset + 5] = (uint8_t)((indices >> 8) & 255u);
            output[out_offset + 6] = (uint8_t)((indices >> 16) & 255u);
            output[out_offset + 7] = (uint8_t)((indices >> 24) & 255u);
            out_offset += 8u;
        }
    }

    return 0;
}

static uint8_t alpha_from_normal_byte(uint8_t value) {
    (void)value;
    return 255u;
}

static void encode_bc3_alpha_block(const uint8_t alphas[16], uint8_t *output) {
    uint8_t min_alpha = 255u;
    uint8_t max_alpha = 0u;
    for (int i = 0; i < 16; ++i) {
        if (alphas[i] < min_alpha) min_alpha = alphas[i];
        if (alphas[i] > max_alpha) max_alpha = alphas[i];
    }

    uint8_t alpha0 = max_alpha;
    uint8_t alpha1 = min_alpha;
    uint8_t palette[8];
    palette[0] = alpha0;
    palette[1] = alpha1;
    if (alpha0 > alpha1) {
        palette[2] = (uint8_t)((6u * alpha0 + alpha1) / 7u);
        palette[3] = (uint8_t)((5u * alpha0 + 2u * alpha1) / 7u);
        palette[4] = (uint8_t)((4u * alpha0 + 3u * alpha1) / 7u);
        palette[5] = (uint8_t)((3u * alpha0 + 4u * alpha1) / 7u);
        palette[6] = (uint8_t)((2u * alpha0 + 5u * alpha1) / 7u);
        palette[7] = (uint8_t)((alpha0 + 6u * alpha1) / 7u);
    } else {
        palette[2] = (uint8_t)((4u * alpha0 + alpha1) / 5u);
        palette[3] = (uint8_t)((3u * alpha0 + 2u * alpha1) / 5u);
        palette[4] = (uint8_t)((2u * alpha0 + 3u * alpha1) / 5u);
        palette[5] = (uint8_t)((alpha0 + 4u * alpha1) / 5u);
        palette[6] = 0u;
        palette[7] = 255u;
    }

    uint64_t indices = 0u;
    for (int i = 0; i < 16; ++i) {
        uint32_t selected = 0u;
        int best_distance = 2147483647;
        for (uint32_t p = 0; p < 8u; ++p) {
            int d = (int)alphas[i] - (int)palette[p];
            int distance = d * d;
            if (distance < best_distance) {
                best_distance = distance;
                selected = p;
            }
        }
        indices |= (uint64_t)selected << (3u * (uint32_t)i);
    }

    output[0] = alpha0;
    output[1] = alpha1;
    output[2] = (uint8_t)(indices & 255u);
    output[3] = (uint8_t)((indices >> 8) & 255u);
    output[4] = (uint8_t)((indices >> 16) & 255u);
    output[5] = (uint8_t)((indices >> 24) & 255u);
    output[6] = (uint8_t)((indices >> 32) & 255u);
    output[7] = (uint8_t)((indices >> 40) & 255u);
}

static void encode_bc1_color_block(
    const uint8_t colors[16][4],
    const uint8_t opaque[16],
    int force_four_color,
    uint8_t *output
) {
    uint8_t endpoint0[3];
    uint8_t endpoint1[3];
    choose_endpoints(colors, opaque, endpoint0, endpoint1);

    uint16_t color0 = pack_rgb565(endpoint0[0], endpoint0[1], endpoint0[2]);
    uint16_t color1 = pack_rgb565(endpoint1[0], endpoint1[1], endpoint1[2]);

    if (force_four_color) {
        if (color0 < color1) {
            uint16_t tmp = color0;
            color0 = color1;
            color1 = tmp;
        }
    } else {
        int has_alpha = 0;
        for (int i = 0; i < 16; ++i) {
            if (!opaque[i]) {
                has_alpha = 1;
                break;
            }
        }
        if (has_alpha) {
            if (color0 > color1) {
                uint16_t tmp = color0;
                color0 = color1;
                color1 = tmp;
            }
        } else {
            if (color0 < color1) {
                uint16_t tmp = color0;
                color0 = color1;
                color1 = tmp;
            }
        }
    }

    uint8_t palette[4][4];
    unpack_rgb565(color0, palette[0]);
    unpack_rgb565(color1, palette[1]);

    if (force_four_color || color0 > color1) {
        palette[2][0] = (uint8_t)((2u * palette[0][0] + palette[1][0]) / 3u);
        palette[2][1] = (uint8_t)((2u * palette[0][1] + palette[1][1]) / 3u);
        palette[2][2] = (uint8_t)((2u * palette[0][2] + palette[1][2]) / 3u);
        palette[2][3] = 255u;
        palette[3][0] = (uint8_t)((palette[0][0] + 2u * palette[1][0]) / 3u);
        palette[3][1] = (uint8_t)((palette[0][1] + 2u * palette[1][1]) / 3u);
        palette[3][2] = (uint8_t)((palette[0][2] + 2u * palette[1][2]) / 3u);
        palette[3][3] = 255u;
    } else {
        palette[2][0] = (uint8_t)((palette[0][0] + palette[1][0]) / 2u);
        palette[2][1] = (uint8_t)((palette[0][1] + palette[1][1]) / 2u);
        palette[2][2] = (uint8_t)((palette[0][2] + palette[1][2]) / 2u);
        palette[2][3] = 255u;
        palette[3][0] = palette[3][1] = palette[3][2] = palette[3][3] = 0u;
    }

    uint32_t indices = 0u;
    int limit = (force_four_color || color0 > color1) ? 4 : 3;
    for (int i = 0; i < 16; ++i) {
        uint32_t selected = 0u;
        int best_distance = 2147483647;
        for (int p = 0; p < limit; ++p) {
            int distance = color_distance(colors[i], palette[p]);
            if (distance < best_distance) {
                best_distance = distance;
                selected = (uint32_t)p;
            }
        }
        indices |= selected << (2u * (uint32_t)i);
    }

    output[0] = (uint8_t)(color0 & 255u);
    output[1] = (uint8_t)(color0 >> 8);
    output[2] = (uint8_t)(color1 & 255u);
    output[3] = (uint8_t)(color1 >> 8);
    output[4] = (uint8_t)(indices & 255u);
    output[5] = (uint8_t)((indices >> 8) & 255u);
    output[6] = (uint8_t)((indices >> 16) & 255u);
    output[7] = (uint8_t)((indices >> 24) & 255u);
}

int encode_bc3(
    const uint8_t *rgba,
    uint32_t width,
    uint32_t height,
    uint8_t *output,
    size_t output_size
) {
    if (!rgba || !output || width == 0 || height == 0) {
        return 1;
    }

    uint32_t blocks_x = (width + 3u) / 4u;
    uint32_t blocks_y = (height + 3u) / 4u;
    size_t expected_output = (size_t)blocks_x * (size_t)blocks_y * 16u;
    if (output_size < expected_output) {
        return 2;
    }

    size_t out_offset = 0;
    for (uint32_t block_y = 0; block_y < blocks_y; ++block_y) {
        for (uint32_t block_x = 0; block_x < blocks_x; ++block_x) {
            uint8_t colors[16][4];
            uint8_t opaque[16];
            uint8_t alphas[16];

            for (uint32_t py = 0; py < 4; ++py) {
                for (uint32_t px = 0; px < 4; ++px) {
                    uint32_t x = block_x * 4u + px;
                    uint32_t y = block_y * 4u + py;
                    int idx = (int)(py * 4u + px);
                    if (x < width && y < height) {
                        size_t pixel_offset = ((size_t)y * (size_t)width + (size_t)x) * 4u;
                        colors[idx][0] = rgba[pixel_offset];
                        colors[idx][1] = rgba[pixel_offset + 1];
                        colors[idx][2] = rgba[pixel_offset + 2];
                        colors[idx][3] = rgba[pixel_offset + 3];
                    } else {
                        colors[idx][0] = colors[idx][1] = colors[idx][2] = 0u;
                        colors[idx][3] = 0u;
                    }
                    opaque[idx] = 1u;
                    alphas[idx] = colors[idx][3];
                }
            }

            encode_bc3_alpha_block(alphas, output + out_offset);
            encode_bc1_color_block(colors, opaque, 1, output + out_offset + 8u);
            out_offset += 16u;
        }
    }

    return 0;
}

static int8_t byte_to_snorm8(uint8_t value) {
    int v = ((int)value * 254 + 127) / 255 - 127;
    if (v < -127) v = -127;
    if (v > 127) v = 127;
    return (int8_t)v;
}

static int snorm_distance(int8_t a, int8_t b) {
    int d = (int)a - (int)b;
    return d * d;
}

static void encode_bc4_snorm_block(const int8_t values[16], uint8_t *output) {
    int8_t min_value = 127;
    int8_t max_value = -127;
    for (int i = 0; i < 16; ++i) {
        if (values[i] < min_value) min_value = values[i];
        if (values[i] > max_value) max_value = values[i];
    }

    int8_t endpoint0 = max_value;
    int8_t endpoint1 = min_value;
    if (endpoint0 <= endpoint1 && endpoint0 < 127) {
        endpoint0 = (int8_t)(endpoint1 + 1);
    }

    float e0 = endpoint0 <= -127 ? -1.0f : (float)endpoint0 / 127.0f;
    float e1 = endpoint1 <= -127 ? -1.0f : (float)endpoint1 / 127.0f;
    float palette_f[8];
    palette_f[0] = e0;
    palette_f[1] = e1;
    palette_f[2] = (6.0f * e0 + e1) / 7.0f;
    palette_f[3] = (5.0f * e0 + 2.0f * e1) / 7.0f;
    palette_f[4] = (4.0f * e0 + 3.0f * e1) / 7.0f;
    palette_f[5] = (3.0f * e0 + 4.0f * e1) / 7.0f;
    palette_f[6] = (2.0f * e0 + 5.0f * e1) / 7.0f;
    palette_f[7] = (e0 + 6.0f * e1) / 7.0f;

    int8_t palette[8];
    for (int i = 0; i < 8; ++i) {
        float v = palette_f[i] * 127.0f;
        int iv = v >= 0.0f ? (int)(v + 0.5f) : (int)(v - 0.5f);
        if (iv < -127) iv = -127;
        if (iv > 127) iv = 127;
        palette[i] = (int8_t)iv;
    }

    uint64_t indices = 0u;
    for (int i = 0; i < 16; ++i) {
        uint32_t selected = 0u;
        int best_distance = 2147483647;
        for (uint32_t p = 0; p < 8u; ++p) {
            int distance = snorm_distance(values[i], palette[p]);
            if (distance < best_distance) {
                best_distance = distance;
                selected = p;
            }
        }
        indices |= (uint64_t)selected << (3u * (uint32_t)i);
    }

    output[0] = (uint8_t)endpoint0;
    output[1] = (uint8_t)endpoint1;
    output[2] = (uint8_t)(indices & 255u);
    output[3] = (uint8_t)((indices >> 8) & 255u);
    output[4] = (uint8_t)((indices >> 16) & 255u);
    output[5] = (uint8_t)((indices >> 24) & 255u);
    output[6] = (uint8_t)((indices >> 32) & 255u);
    output[7] = (uint8_t)((indices >> 40) & 255u);
}

int encode_bc5_snorm(
    const uint8_t *rgba,
    uint32_t width,
    uint32_t height,
    uint8_t *output,
    size_t output_size
) {
    if (!rgba || !output || width == 0 || height == 0) {
        return 1;
    }

    uint32_t blocks_x = (width + 3u) / 4u;
    uint32_t blocks_y = (height + 3u) / 4u;
    size_t expected_output = (size_t)blocks_x * (size_t)blocks_y * 16u;
    if (output_size < expected_output) {
        return 2;
    }

    size_t out_offset = 0;
    for (uint32_t block_y = 0; block_y < blocks_y; ++block_y) {
        for (uint32_t block_x = 0; block_x < blocks_x; ++block_x) {
            int8_t xs[16];
            int8_t ys[16];

            for (uint32_t py = 0; py < 4; ++py) {
                for (uint32_t px = 0; px < 4; ++px) {
                    uint32_t x = block_x * 4u + px;
                    uint32_t y = block_y * 4u + py;
                    int idx = (int)(py * 4u + px);
                    if (x < width && y < height) {
                        size_t pixel_offset = ((size_t)y * (size_t)width + (size_t)x) * 4u;
                        xs[idx] = byte_to_snorm8(rgba[pixel_offset]);
                        ys[idx] = byte_to_snorm8(rgba[pixel_offset + 1]);
                    } else {
                        xs[idx] = 0;
                        ys[idx] = 0;
                    }
                }
            }

            encode_bc4_snorm_block(xs, output + out_offset);
            encode_bc4_snorm_block(ys, output + out_offset + 8u);
            out_offset += 16u;
        }
    }

    return 0;
}
"""


@dataclass
class PngImage:
    width: int
    height: int
    rgba: bytes


@dataclass
class Ktx2Reference:
    path: Path
    vk_format: int
    width: int
    height: int
    level_count: int
    dfd: bytes
    kvd: bytes
    levels: list[bytes]


_ENCODER: ctypes.CDLL | None = None
_ENCODER_TRIED = False


class PngError(Exception):
    """PNG 解析错误。"""


def align8(value: int) -> int:
    return (value + 7) & ~7


def align16(value: int) -> int:
    return (value + 15) & ~15


def read_be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def read_le32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def read_le64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def write_le32(buffer: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", buffer, offset, value)


def write_le64(buffer: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<Q", buffer, offset, value)


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png(path: Path) -> PngImage:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("不是 PNG 文件")

    offset = 8
    width = height = bit_depth = color_type = interlace = None
    idat_parts: list[bytes] = []

    while offset + 12 <= len(data):
        length = read_be32(data, offset)
        chunk_type = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(data):
            raise PngError("PNG chunk 超出文件范围")

        payload = data[payload_start:payload_end]
        expected_crc = read_be32(data, payload_end)
        actual_crc = binascii.crc32(chunk_type)
        actual_crc = binascii.crc32(payload, actual_crc) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise PngError(f"{chunk_type.decode('latin1')} chunk CRC 校验失败")

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif chunk_type == b"IDAT":
            idat_parts.append(payload)
        elif chunk_type == b"IEND":
            break

        offset = crc_end

    if width is None or height is None or bit_depth is None or color_type is None:
        raise PngError("缺少 IHDR")
    if not idat_parts:
        raise PngError("缺少 IDAT")
    if bit_depth != 8:
        raise PngError("当前脚本仅支持 8-bit PNG")
    if interlace != 0:
        raise PngError("当前脚本暂不支持 Adam7 隔行 PNG")

    channels_by_type = {
        0: 1,  # 灰度
        2: 3,  # RGB
        4: 2,  # 灰度 + alpha
        6: 4,  # RGBA
    }
    if color_type not in channels_by_type:
        raise PngError("当前脚本仅支持灰度、RGB、灰度 Alpha、RGBA PNG")

    channels = channels_by_type[color_type]
    stride = width * channels
    raw = zlib.decompress(b"".join(idat_parts))
    expected = (stride + 1) * height
    if len(raw) != expected:
        raise PngError(f"PNG 解压长度异常：需要 {expected}，实际 {len(raw)}")

    rows: list[bytearray] = []
    raw_offset = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[raw_offset]
        raw_offset += 1
        scanline = bytearray(raw[raw_offset : raw_offset + stride])
        raw_offset += stride

        for i in range(stride):
            left = scanline[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0

            if filter_type == 0:
                value = scanline[i]
            elif filter_type == 1:
                value = (scanline[i] + left) & 0xFF
            elif filter_type == 2:
                value = (scanline[i] + up) & 0xFF
            elif filter_type == 3:
                value = (scanline[i] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                value = (scanline[i] + paeth(left, up, up_left)) & 0xFF
            else:
                raise PngError(f"不支持的 PNG filter：{filter_type}")

            scanline[i] = value

        rows.append(scanline)
        previous = scanline

    rgba = bytearray(width * height * 4)
    out = 0
    for row in rows:
        for x in range(width):
            start = x * channels
            if color_type == 0:
                gray = row[start]
                rgba[out : out + 4] = bytes((gray, gray, gray, 255))
            elif color_type == 2:
                rgba[out : out + 4] = bytes(
                    (row[start], row[start + 1], row[start + 2], 255)
                )
            elif color_type == 4:
                gray = row[start]
                rgba[out : out + 4] = bytes((gray, gray, gray, row[start + 1]))
            else:
                rgba[out : out + 4] = row[start : start + 4]
            out += 4

    return PngImage(width, height, bytes(rgba))


def read_manifest(input_path: Path) -> dict[str, object] | None:
    manifest_path = input_path.with_suffix(input_path.suffix + ".ktx2png.json")
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def manifest_matches(
    manifest: dict[str, object] | None,
    input_path: Path,
    image: PngImage,
    reference_path: Path | None,
) -> bool:
    if manifest is None:
        return False

    try:
        width = int(manifest["width"])
        height = int(manifest["height"])
        expected_hash = str(manifest["rgba_sha256"])
        manifest_png = Path(str(manifest["png"]))
        manifest_source = Path(str(manifest["source_ktx2"]))
    except (KeyError, TypeError, ValueError):
        return False

    if width != image.width or height != image.height:
        return False
    if manifest_png.resolve() != input_path.resolve():
        return False
    if reference_path is not None and manifest_source.resolve() != reference_path.resolve():
        return False

    return hashlib.sha256(image.rgba).hexdigest() == expected_hash


def parse_reference_ktx2(path: Path) -> Ktx2Reference:
    data = path.read_bytes()
    if len(data) < 80 or data[:12] != KTX2_IDENTIFIER:
        raise Ktx2Error("参考文件不是有效 KTX2")

    vk_format = read_le32(data, 12)
    width = read_le32(data, 20)
    height = read_le32(data, 24)
    depth = read_le32(data, 28)
    layer_count = read_le32(data, 32)
    face_count = read_le32(data, 36)
    level_count = read_le32(data, 40)
    supercompression = read_le32(data, 44)
    dfd_offset = read_le32(data, 48)
    dfd_length = read_le32(data, 52)
    kvd_offset = read_le32(data, 56)
    kvd_length = read_le32(data, 60)

    if vk_format not in SUPPORTED_FORMATS:
        supported = ", ".join(value["name"] for value in SUPPORTED_FORMATS.values())
        raise Ktx2Error(f"参考 KTX2 格式暂不支持：vkFormat={vk_format}。支持：{supported}")
    if depth not in (0, 1) or layer_count not in (0, 1) or face_count != 1:
        raise Ktx2Error("当前脚本仅支持普通 2D KTX2")
    if supercompression != 0:
        raise Ktx2Error("当前脚本仅支持未 supercompress 的 KTX2")
    if level_count <= 0:
        raise Ktx2Error("参考 KTX2 缺少 mip")

    dfd = data[dfd_offset : dfd_offset + dfd_length]
    kvd = data[kvd_offset : kvd_offset + kvd_length] if kvd_length else b""

    levels: list[bytes] = []
    for level in range(level_count):
        entry_offset = 80 + level * 24
        level_offset = read_le64(data, entry_offset)
        level_length = read_le64(data, entry_offset + 8)
        levels.append(data[level_offset : level_offset + level_length])

    return Ktx2Reference(path, vk_format, width, height, level_count, dfd, kvd, levels)


def c_library_extension() -> str:
    if sys.platform == "darwin":
        return ".dylib"
    if os.name == "nt":
        return ".dll"
    return ".so"


def compile_encoder() -> Path | None:
    if os.name == "nt":
        return None

    compiler = os.environ.get("CC") or "cc"
    cache_key = hashlib.sha256(ENCODER_SOURCE.encode("utf-8")).hexdigest()[:16]
    cache_dir = Path(tempfile.gettempdir()) / "png_to_ktx2_encoder"
    source_path = cache_dir / f"bc1_encoder_{cache_key}.c"
    library_path = cache_dir / f"bc1_encoder_{cache_key}{c_library_extension()}"

    if library_path.exists():
        return library_path

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        source_path.write_text(ENCODER_SOURCE, encoding="utf-8")
        subprocess.run(
            [
                compiler,
                "-O3",
                "-shared",
                "-fPIC",
                str(source_path),
                "-o",
                str(library_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    return library_path


def load_encoder() -> ctypes.CDLL:
    global _ENCODER, _ENCODER_TRIED
    if _ENCODER is not None:
        return _ENCODER
    if _ENCODER_TRIED:
        raise Ktx2Error("C 编码器不可用，无法重新压缩改动过的 PNG")
    _ENCODER_TRIED = True

    library_path = compile_encoder()
    if library_path is None:
        raise Ktx2Error("无法编译 C 编码器，请确认系统可用 cc/clang")

    library = ctypes.CDLL(str(library_path))
    library.downsample_rgba.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    library.downsample_rgba.restype = ctypes.c_int
    library.encode_bc1.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    library.encode_bc1.restype = ctypes.c_int
    library.encode_bc3.argtypes = library.encode_bc1.argtypes
    library.encode_bc3.restype = ctypes.c_int
    library.encode_bc5_snorm.argtypes = library.encode_bc1.argtypes
    library.encode_bc5_snorm.restype = ctypes.c_int
    _ENCODER = library
    return _ENCODER


def downsample_rgba(rgba: bytes, width: int, height: int) -> tuple[int, int, bytes]:
    encoder = load_encoder()
    out_width = max(1, width // 2)
    out_height = max(1, height // 2)
    output = bytearray(out_width * out_height * 4)
    input_buffer = (ctypes.c_ubyte * len(rgba)).from_buffer_copy(rgba)
    output_buffer = (ctypes.c_ubyte * len(output)).from_buffer(output)
    result = encoder.downsample_rgba(
        ctypes.cast(input_buffer, ctypes.c_void_p),
        width,
        height,
        ctypes.cast(output_buffer, ctypes.c_void_p),
        out_width,
        out_height,
    )
    if result != 0:
        raise Ktx2Error(f"生成 mip 失败，错误码：{result}")
    return out_width, out_height, bytes(output)


def block_size_for_decoder(decoder: str) -> int:
    if decoder == "bc1":
        return 8
    if decoder in {"bc3", "bc5_snorm"}:
        return 16
    raise Ktx2Error(f"暂不支持重编码格式：{decoder}")


def encoder_function_for_decoder(decoder: str) -> str:
    if decoder == "bc1":
        return "encode_bc1"
    if decoder == "bc3":
        return "encode_bc3"
    if decoder == "bc5_snorm":
        return "encode_bc5_snorm"
    raise Ktx2Error(f"暂不支持重编码格式：{decoder}")


def encode_level(rgba: bytes, width: int, height: int, decoder: str) -> bytes:
    encoder = load_encoder()
    blocks_x = math.ceil(width / 4)
    blocks_y = math.ceil(height / 4)
    output = bytearray(blocks_x * blocks_y * block_size_for_decoder(decoder))
    input_buffer = (ctypes.c_ubyte * len(rgba)).from_buffer_copy(rgba)
    output_buffer = (ctypes.c_ubyte * len(output)).from_buffer(output)
    function = getattr(encoder, encoder_function_for_decoder(decoder))
    result = function(
        ctypes.cast(input_buffer, ctypes.c_void_p),
        width,
        height,
        ctypes.cast(output_buffer, ctypes.c_void_p),
        len(output),
    )
    if result != 0:
        raise Ktx2Error(f"{decoder} 编码失败，错误码：{result}")
    return bytes(output)


def build_mip_chain(image: PngImage, levels: int | None, decoder: str) -> list[bytes]:
    max_levels = int(math.log2(max(image.width, image.height))) + 1
    level_count = levels if levels is not None else max_levels
    if not 1 <= level_count <= max_levels:
        raise Ktx2Error(f"mip levels 必须在 1 到 {max_levels} 之间")

    width = image.width
    height = image.height
    rgba = image.rgba
    encoded_levels: list[bytes] = []

    for level in range(level_count):
        encoded_levels.append(encode_level(rgba, width, height, decoder))
        if level + 1 < level_count:
            width, height, rgba = downsample_rgba(rgba, width, height)

    return encoded_levels


def write_ktx2_from_reference(
    output_path: Path,
    reference: Ktx2Reference,
    width: int,
    height: int,
    levels: list[bytes],
) -> None:
    level_count = len(levels)
    dfd_offset = 80 + level_count * 24
    kvd_offset = dfd_offset + len(reference.dfd)
    kvd_length = len(reference.kvd)
    data_start = align16(kvd_offset + kvd_length)

    level_offsets = [0] * level_count
    cursor = data_start
    for level in range(level_count - 1, -1, -1):
        cursor = align16(cursor)
        level_offsets[level] = cursor
        cursor += len(levels[level])

    output = bytearray(cursor)
    output[:12] = KTX2_IDENTIFIER
    write_le32(output, 12, reference.vk_format)
    write_le32(output, 16, 1)
    write_le32(output, 20, width)
    write_le32(output, 24, height)
    write_le32(output, 28, 0)
    write_le32(output, 32, 0)
    write_le32(output, 36, 1)
    write_le32(output, 40, level_count)
    write_le32(output, 44, 0)
    write_le32(output, 48, dfd_offset)
    write_le32(output, 52, len(reference.dfd))
    write_le32(output, 56, kvd_offset if kvd_length else 0)
    write_le32(output, 60, kvd_length)
    write_le64(output, 64, 0)
    write_le64(output, 72, 0)

    for level, level_data in enumerate(levels):
        entry_offset = 80 + level * 24
        write_le64(output, entry_offset, level_offsets[level])
        write_le64(output, entry_offset + 8, len(level_data))
        write_le64(output, entry_offset + 16, len(level_data))

    output[dfd_offset : dfd_offset + len(reference.dfd)] = reference.dfd
    if kvd_length:
        output[kvd_offset : kvd_offset + kvd_length] = reference.kvd

    for level, level_data in enumerate(levels):
        offset = level_offsets[level]
        output[offset : offset + len(level_data)] = level_data

    output_path.write_bytes(bytes(output))


def write_ktx2_with_ktx_create(
    output_path: Path,
    width: int,
    height: int,
    levels: list[bytes],
    format_name: str,
) -> None:
    ktx = shutil.which("ktx")
    if not ktx:
        raise Ktx2Error("找不到 ktx 工具；没有参考 KTX2 时需要它来创建容器")

    with tempfile.TemporaryDirectory(prefix="png_to_ktx2_") as temp_dir:
        temp = Path(temp_dir)
        level_paths: list[Path] = []
        for index, level in enumerate(levels):
            level_path = temp / f"level{index}.bc1"
            level_path.write_bytes(level)
            level_paths.append(level_path)

        command = [
            ktx,
            "create",
            "--raw",
            "--format",
            format_name,
            "--width",
            str(width),
            "--height",
            str(height),
            "--levels",
            str(len(levels)),
            *[str(path) for path in level_paths],
            str(output_path),
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise Ktx2Error(f"ktx create 失败：{stderr}")


def find_inputs(paths: list[Path]) -> list[Path]:
    inputs: list[Path] = []
    for path in paths:
        if path.is_dir():
            inputs.extend(sorted(path.glob("*.png")))
            inputs.extend(sorted(path.glob("*.PNG")))
        else:
            inputs.append(path)
    return inputs


def output_path_for(input_path: Path, output_dir: Path | None) -> Path:
    return (output_dir if output_dir else input_path.parent) / f"{input_path.name}.KTX2"


def reference_path_for(input_path: Path, reference: Path | None) -> Path | None:
    if reference is None:
        candidate = input_path.parent / f"{input_path.name}.KTX2"
        return candidate if candidate.exists() else None
    if reference.is_dir():
        candidate = reference / f"{input_path.name}.KTX2"
        return candidate if candidate.exists() else None
    return reference


def ensure_can_write(
    input_path: Path,
    output_path: Path,
    reference_path: Path | None,
    overwrite: bool,
    replace_original: bool,
    modified: bool,
) -> None:
    if output_path.exists() and not overwrite:
        if reference_path and output_path.resolve() == reference_path.resolve() and not modified:
            return
        raise Ktx2Error(f"输出已存在，请使用 --overwrite：{output_path}")

    if (
        modified
        and reference_path
        and output_path.exists()
        and output_path.resolve() == reference_path.resolve()
        and not replace_original
    ):
        raise Ktx2Error(
            f"{input_path} 已改动，拒绝直接覆盖原始 KTX2；"
            "请使用 -o 输出到其他目录，或显式加 --replace-original"
        )


def convert_file(
    input_path: Path,
    output_dir: Path | None,
    reference_arg: Path | None,
    overwrite: bool,
    replace_original: bool,
    encode_modified: bool,
    levels_arg: int | None,
) -> Path:
    image = read_png(input_path)
    output_path = output_path_for(input_path, output_dir)
    reference_path = reference_path_for(input_path, reference_arg)
    reference = parse_reference_ktx2(reference_path) if reference_path else None

    unchanged = False
    manifest = read_manifest(input_path)
    if reference_path is None and manifest is not None:
        source = manifest.get("source_ktx2")
        if isinstance(source, str) and Path(source).exists():
            reference_path = Path(source)
            reference = parse_reference_ktx2(reference_path)

    if reference is not None:
        if image.width != reference.width or image.height != reference.height:
            raise Ktx2Error(
                f"PNG 尺寸 {image.width}x{image.height} 与参考 KTX2 "
                f"{reference.width}x{reference.height} 不一致"
            )
        if manifest_matches(manifest, input_path, image, reference_path):
            unchanged = True
        else:
            format_info = SUPPORTED_FORMATS[reference.vk_format]
            reference_rgba = decode_texture(
                reference.levels[0],
                reference.width,
                reference.height,
                str(format_info["decoder"]),
            )
            unchanged = image.rgba == reference_rgba

    ensure_can_write(
        input_path,
        output_path,
        reference_path,
        overwrite,
        replace_original,
        modified=not unchanged,
    )

    if reference is not None and unchanged:
        if output_path.resolve() == reference.path.resolve():
            print(f"✅ {input_path}: PNG 未改动，原始 KTX2 已经存在")
            return output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(reference.path, output_path)
        print(f"✅ {input_path}: PNG 未改动，已复用原始 KTX2 -> {output_path}")
        return output_path

    if not encode_modified:
        raise Ktx2Error(
            f"{input_path} 与参考 KTX2 不一致，或没有参考 KTX2。"
            "重新编码 block-compressed KTX2 会产生有损压缩；确认要继续请加 --encode-modified"
        )

    if output_path.exists() and overwrite:
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    level_count = levels_arg
    if level_count is None and reference is not None:
        level_count = reference.level_count
    decoder = str(SUPPORTED_FORMATS[reference.vk_format]["decoder"]) if reference else "bc1"
    format_name = (
        str(SUPPORTED_FORMATS[reference.vk_format]["name"]).replace("VK_FORMAT_", "")
        if reference
        else DEFAULT_FORMAT_NAME
    )
    levels = build_mip_chain(image, level_count, decoder)

    if reference is not None:
        write_ktx2_from_reference(output_path, reference, image.width, image.height, levels)
    else:
        write_ktx2_with_ktx_create(output_path, image.width, image.height, levels, format_name)

    print(f"⚠️ {input_path}: 已重新编码为 {format_name} KTX2（有损） -> {output_path}")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "将 PNG 转回 KTX2。未改动 PNG 会复用原始 KTX2；改动 PNG 需加 "
            "--encode-modified 才会按参考 KTX2 格式进行有损重编码。"
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="要转换的 PNG 文件或目录；不填写时处理当前目录下的 .png/.PNG",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="输出目录；不填写时输出为 PNG 文件名后追加 .KTX2",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        help="参考 KTX2 文件或目录；默认查找同名 sidecar：xxx.PNG.KTX2",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已存在的输出文件",
    )
    parser.add_argument(
        "--replace-original",
        action="store_true",
        help="允许在 PNG 已改动时覆盖同名原始 KTX2",
    )
    parser.add_argument(
        "--encode-modified",
        action="store_true",
        help="允许对改动过的 PNG 按参考 KTX2 格式重新编码；这是有损操作",
    )
    parser.add_argument(
        "--levels",
        type=int,
        help="生成的 mip 级数；默认使用参考 KTX2 的级数，没有参考时生成完整 mip 链",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    search_paths = args.inputs if args.inputs else [Path.cwd()]
    inputs = find_inputs(search_paths)

    if not inputs:
        print("未找到 PNG 文件", file=sys.stderr)
        return 1

    failed = 0
    for input_path in inputs:
        try:
            convert_file(
                input_path,
                args.output_dir,
                args.reference,
                args.overwrite,
                args.replace_original,
                args.encode_modified,
                args.levels,
            )
        except (Ktx2Error, PngError, OSError) as exc:
            failed += 1
            print(f"❌ {input_path}: {exc}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
