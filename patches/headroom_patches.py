"""Headroom patches: fix ContentRouter skip_system and disable frozen count.
Loaded via .pth file in site-packages.
"""
import builtins
import os
import sys
from pathlib import Path

_DONE = False


def _apply_patches():
    global _DONE
    if _DONE:
        return
    _DONE = True

    # Mark that we ran
    Path(os.path.expanduser("~/.headroom/headroom_patches_applied")).write_text("v4")

    # Patch 1: ContentRouter skip_system fix
    try:
        from headroom.transforms.content_router import ContentRouter
        _original_apply = ContentRouter.apply

        def _patched_apply(self, messages, tokenizer, **kwargs):
            if "compress_system_messages" not in kwargs:
                kwargs["compress_system_messages"] = True
            return _original_apply(self, messages, tokenizer, **kwargs)

        ContentRouter.apply = _patched_apply
        Path(os.path.expanduser("~/.headroom/headroom_cr_applied")).write_text("ok")
    except Exception:
        pass

    # Patch 2: CompressionCache frozen count fix
    try:
        from headroom.cache.compression_cache import CompressionCache
        CompressionCache.compute_frozen_count = lambda self, messages: 0
        Path(os.path.expanduser("~/.headroom/headroom_cache_applied")).write_text("ok")
    except Exception:
        pass


# Try to apply immediately (module loaded during early startup)
_apply_patches()

# Also hook into builtins.__import__ as a fallback
_original_import = builtins.__import__


def _import_hook(name, *args, **kwargs):
    module = _original_import(name, *args, **kwargs)
    if name in ("headroom.transforms.content_router", "headroom.cache.compression_cache"):
        _apply_patches()
    return module


builtins.__import__ = _import_hook
