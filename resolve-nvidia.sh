#!/usr/bin/env bash
# Launch DaVinci Resolve with OpenGL rendered on the NVIDIA dGPU.
#
# Why: on a hybrid-graphics machine (e.g. Radeon Vega iGPU + GTX 1050) with
# prime-select set to "on-demand", OpenGL renders on the integrated GPU by default
# while Resolve runs CUDA on the NVIDIA. Textures cannot be shared across two
# GPUs, so OpenGL<->CUDA interop fails:
#
#   DVIP ERROR | Failed to register OpenGL object for CUDA interop: cudaErrorUnknown
#   GPU.SingleBoardMgr ERROR | Out-of-memory exception caught: Packer OpenGL interop failed
#
# ...which presents as a black preview plus bogus GPU-memory warnings. Forcing
# OpenGL onto the NVIDIA puts both on the same device.
set -euo pipefail

export __NV_PRIME_RENDER_OFFLOAD=1
export __GLX_VENDOR_LIBRARY_NAME=nvidia
export __VK_LAYER_NV_optimus=NVIDIA_only

exec /opt/resolve/bin/resolve "$@"
