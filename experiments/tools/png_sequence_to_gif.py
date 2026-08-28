#!/usr/bin/env python3
"""Encode a PNG frame sequence as a compact, infinitely looping GIF."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("frames_directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--colors", type=int, default=256)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.fps <= 0:
        raise SystemExit("--fps must be positive")
    if not 2 <= args.colors <= 256:
        raise SystemExit("--colors must be between 2 and 256")

    frame_paths = sorted(args.frames_directory.glob("*.png"))
    if not frame_paths:
        raise SystemExit("no PNG frames found")

    rgb_frames = [Image.open(path).convert("RGB") for path in frame_paths]
    sample_width = min(160, rgb_frames[0].width)
    sample_height = round(rgb_frames[0].height * sample_width / rgb_frames[0].width)
    samples = [
        frame.resize((sample_width, sample_height), Image.Resampling.BILINEAR)
        for frame in rgb_frames
    ]
    columns = min(8, len(samples))
    rows = (len(samples) + columns - 1) // columns
    atlas = Image.new("RGB", (columns * sample_width, rows * sample_height))
    for index, sample in enumerate(samples):
        atlas.paste(
            sample,
            ((index % columns) * sample_width, (index // columns) * sample_height),
        )
    palette = atlas.quantize(colors=args.colors, method=Image.Quantize.MEDIANCUT)
    frames = [
        frame.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
        for frame in rgb_frames
    ]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        args.output,
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / args.fps),
        loop=0,
        disposal=2,
        optimize=True,
    )
    print(
        f"encoded_frames={len(frames)} width={frames[0].width} "
        f"height={frames[0].height} fps={args.fps} colors={args.colors} "
        f"bytes={args.output.stat().st_size}"
    )


if __name__ == "__main__":
    main()
