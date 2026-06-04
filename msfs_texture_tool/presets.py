from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TexturePreset:
    key: str
    suffixes: tuple[str, ...]
    label: str
    transfer: str
    basis_args_fast: tuple[str, ...]
    basis_args_high: tuple[str, ...]
    flags: tuple[str, ...]
    json_extra: dict[str, object] = field(default_factory=dict)
    flags_file_values: tuple[str, ...] = field(default_factory=tuple)

    def basis_args(self, quality: str, mipmaps: bool, transfer_override: str) -> list[str]:
        args = list(self.basis_args_high if quality == "high" else self.basis_args_fast)
        if mipmaps:
            args.append("-mipmap")
            if transfer_override == "linear":
                args.append("-mip_linear")
            else:
                args.append("-mip_srgb")
        if transfer_override == "linear":
            args.append("-linear")
        return args


PRESETS: tuple[TexturePreset, ...] = (
    TexturePreset(
        key="albedo",
        suffixes=("_ALBD", "_ALBEDO", "_BASECOLOR", "_BASE_COLOR"),
        label="Albedo / Base Color",
        transfer="srgb",
        basis_args_fast=("-uastc", "-uastc_level", "1", "-ktx2_no_zstandard"),
        basis_args_high=("-uastc", "-uastc_level", "3", "-ktx2_zstandard_level", "6"),
        flags=("FL_BITMAP_COMPRESSION", "FL_BITMAP_MIPMAP"),
        flags_file_values=(),
    ),
    TexturePreset(
        key="comp",
        suffixes=("_COMP", "_ORM", "_MRAO", "_METALROUGH"),
        label="Composite / ORM",
        transfer="linear",
        basis_args_fast=("-uastc", "-uastc_level", "1", "-linear", "-ktx2_no_zstandard"),
        basis_args_high=("-uastc", "-uastc_level", "3", "-linear", "-ktx2_zstandard_level", "6"),
        flags=(
            "FL_BITMAP_COMPRESSION",
            "FL_BITMAP_MIPMAP",
            "FL_BITMAP_NO_GAMMA_CORRECTION",
        ),
        flags_file_values=(),
    ),
    TexturePreset(
        key="normal",
        suffixes=("_NORM", "_NORMAL", "_NRM"),
        label="Normal Map",
        transfer="linear",
        basis_args_fast=("-uastc", "-uastc_level", "1", "-normal_map", "-linear", "-ktx2_no_zstandard"),
        basis_args_high=("-uastc", "-uastc_level", "3", "-normal_map", "-linear", "-ktx2_zstandard_level", "6"),
        flags=(
            "FL_BITMAP_COMPRESSION",
            "FL_BITMAP_MIPMAP",
            "FL_BITMAP_TANGENT_DXT5N",
            "FL_BITMAP_NO_GAMMA_CORRECTION",
        ),
        flags_file_values=(),
    ),
    TexturePreset(
        key="emissive",
        suffixes=("_EMIS", "_EMISSIVE", "_LIT"),
        label="Emissive",
        transfer="srgb",
        basis_args_fast=("-uastc", "-uastc_level", "1", "-ktx2_no_zstandard"),
        basis_args_high=("-uastc", "-uastc_level", "3", "-ktx2_zstandard_level", "6"),
        flags=("FL_BITMAP_COMPRESSION", "FL_BITMAP_MIPMAP"),
        flags_file_values=(),
    ),
)

DEFAULT_PRESET = TexturePreset(
    key="generic",
    suffixes=(),
    label="Generic Texture",
    transfer="srgb",
    basis_args_fast=("-uastc", "-uastc_level", "1", "-ktx2_no_zstandard"),
    basis_args_high=("-uastc", "-uastc_level", "3", "-ktx2_zstandard_level", "6"),
    flags=("FL_BITMAP_COMPRESSION", "FL_BITMAP_MIPMAP"),
    flags_file_values=(),
)


def stem_without_png(path: Path) -> str:
    if path.suffix.lower() == ".png":
        return path.stem
    return path.name


def detect_preset(path: Path) -> TexturePreset:
    stem = stem_without_png(path).upper()
    for preset in PRESETS:
        if any(stem.endswith(suffix) for suffix in preset.suffixes):
            return preset
    return DEFAULT_PRESET


def all_presets() -> list[dict[str, object]]:
    return [
        {
            "key": preset.key,
            "label": preset.label,
            "suffixes": preset.suffixes,
            "transfer": preset.transfer,
            "flags": preset.flags,
        }
        for preset in (*PRESETS, DEFAULT_PRESET)
    ]
