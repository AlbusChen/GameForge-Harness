# GameCraft-Bench transport

This directory contains experiment-side transport only. It does not modify the frozen
GameForge release candidate or the pinned third-party benchmark checkout.

`official-evaluator.Dockerfile` pins the public GameCraft-Bench commit and the benchmark's
Godot 4.6.2 Linux runtime. Both official x86_64 and arm64 release archives are checksum-pinned;
Apple Silicon runs use native arm64 because llvmpipe aborts under QEMU x86 emulation. The
container runs the official build check, Xvfb/xdotool
trace replay, video recording, and frame sampling. It deliberately uses the official stub
judge while producing evidence; scoring is performed separately so a subscription-backed
judge can be disclosed rather than misrepresented as an API-key leaderboard run.
