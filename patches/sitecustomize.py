"""
Headroom patches: CJK-aware Kompress + x-headroom-base-url upstream routing.

Auto-loaded by Python's site module at startup (via PYTHONPATH).
"""

import builtins
import contextvars
import os
import re
import sys
import time
from pathlib import Path

# Debug: mark that sitecustomize is loaded
Path("/home/administrator/.headroom/sitecustomize_loaded").write_text("v3")

# ── CJK support ──────────────────────────────────────────────────────────

CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]")

_patched_cjk = False


def _patch_kompress(mod):
    """Apply CJK-aware wrapper to KompressCompressor.compress."""
    global _patched_cjk
    if _patched_cjk:
        return
    _patched_cjk = True

    Klass = mod.KompressCompressor
    _original = Klass.compress

    def _patched_compress(self, content, context="", content_type=None, question=None, target_ratio=None, *, allow_download=True):
        words = content.split()
        if len(words) < 10:
            return _original(self, content, context, content_type, question, target_ratio, allow_download=allow_download)
        cjk_map = {}
        has_cjk = False
        modified_words = []
        for i, w in enumerate(words):
            if CJK_PATTERN.search(w):
                has_cjk = True
                placeholder = f"_CJKBLOCK{i}_"
                cjk_map[placeholder] = w
                modified_words.append(placeholder)
            else:
                modified_words.append(w)
        if not has_cjk:
            return _original(self, content, context, content_type, question, target_ratio, allow_download=allow_download)
        modified_content = " ".join(modified_words)
        result = _original(self, modified_content, context, content_type, question, target_ratio, allow_download=allow_download)
        compressed_words = result.compressed.split()
        restored = []
        missing = set(cjk_map.keys())
        for w in compressed_words:
            original = cjk_map.get(w)
            if original is not None:
                restored.append(original)
                missing.discard(w)
            else:
                restored.append(w)
        if missing:
            restored.extend(cjk_map[p] for p in missing)
        restored_count = len(restored)
        original_count = result.original_tokens
        result.compressed = " ".join(restored)
        result.compressed_tokens = restored_count
        if original_count > 0:
            result.compression_ratio = restored_count / original_count
        return result

    Klass.compress = _patched_compress


# ── x-headroom-base-url routing ─────────────────────────────────────────
# Uses contextvars.ContextVar for asyncio task isolation (no shared state).
# Patches build_copilot_upstream_url at the module level.

_headroom_override_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "headroom_override_url", default=None
)

_patched_headroom = False


def _apply_headroom_patch():
    """
    Apply x-headroom-base-url support.

    Strategy: instead of modifying shared class attribute (self.OPENAI_API_URL),
    we use a contextvar that's task-local in asyncio:

    1. Wrap handle_openai_chat → read header → set contextvar
    2. Patch build_copilot_upstream_url → check contextvar → use override URL
    """
    global _patched_headroom
    if _patched_headroom:
        return
    _patched_headroom = True

    Path("/home/administrator/.headroom/headroom_patch_applied").write_text("ok")

    server_mod = sys.modules.get("headroom.proxy.server")
    if server_mod is None:
        return
    HeadroomProxy = getattr(server_mod, "HeadroomProxy", None)
    if HeadroomProxy is None:
        return

    # Step 1: wrap handle_openai_chat to set contextvar from header
    _original_chat = HeadroomProxy.handle_openai_chat

    async def _patched_chat(self, request, *args, **kwargs):
        hv = request.headers.get("x-headroom-base-url")
        if hv and hv.strip() and hv.startswith("https://"):
            token = _headroom_override_url.set(hv.strip())
            try:
                return await _original_chat(self, request, *args, **kwargs)
            finally:
                _headroom_override_url.reset(token)
        return await _original_chat(self, request, *args, **kwargs)

    HeadroomProxy.handle_openai_chat = _patched_chat

    # Step 2: patch build_copilot_upstream_url to check contextvar
    # Must patch in the importing module's namespace (openai.py does from-import)
    openai_mod = sys.modules.get("headroom.proxy.handlers.openai")
    if openai_mod is None:
        return

    _original_build = getattr(openai_mod, "build_copilot_upstream_url", None)
    if _original_build is None:
        return

    def _patched_build(base_url, path):
        """Build upstream URL, deduplicating path segments when base already carries them."""
        override = _headroom_override_url.get()
        if override is None:
            return _original_build(base_url, path)

        clean_base = override.rstrip("/")
        clean_path = path if path.startswith("/") else f"/{path}"

        # Deduplicate: if base already has the path's first segment, skip it
        # e.g. ".../v1" + "/v1/chat/completions" → ".../v1/chat/completions"
        first_seg = clean_path.split("/")[1]  # "v1" from "/v1/chat"
        if clean_base.endswith(f"/{first_seg}"):
            return f"{clean_base}{clean_path[len(first_seg) + 1:]}"

        return f"{clean_base}{clean_path}"

    openai_mod.build_copilot_upstream_url = _patched_build

    # Also patch in copilot_auth module in case of direct reference
    auth_mod = sys.modules.get("headroom.copilot_auth")
    if auth_mod is not None and hasattr(auth_mod, "build_copilot_upstream_url"):
        auth_mod.build_copilot_upstream_url = _patched_build

    # Patch ContentRouter: fix skip_system (never reads from config)
    _patch_content_router_now()


def _patch_content_router_now():
    """Patch ContentRouter.apply() to default compress_system_messages=True."""
    global _patch_content_router
    if _patch_content_router:
        return
    # Try sys.modules first, then direct import, then schedule via import hook
    cr_mod = sys.modules.get("headroom.transforms.content_router")
    if cr_mod is None:
        try:
            from headroom.transforms import content_router as cr_mod
        except ImportError:
            pass
    if cr_mod is None:
        return  # Not yet imported, import hook will catch it later
    _patch_cr(cr_mod)
    _patch_content_router = True


# ── Compression cache: cap _stable_hashes / _first_seen ────────────
# Both grow unbounded per-session; after ~2h they account for ~800MB
# of accumulated entries. Safe to drop: worst case = a few
# extra compressions on re-seen content.
# Use lazy trimming (every 100th call) to avoid O(n) overhead on hot paths.

_patch_compress_cache = False


def _patch_compression_cache(mod):
    """Cap _stable_hashes / _first_seen to prevent OOM."""
    global _patch_compress_cache
    if _patch_compress_cache:
        return
    _patch_compress_cache = True

    Klass = mod.CompressionCache

    _MAX_STABLE = 5000
    _MAX_FIRST = 10000
    _TRIM_INTERVAL = 100

    _orig_mark_stable = Klass.mark_stable
    _orig_mark_stable_msgs = Klass.mark_stable_from_messages
    _orig_update = Klass.update_from_result

    def _trim_stable(self):
        """Pop arbitrary elements when over limit — O(target) worst case."""
        s = self._stable_hashes
        while len(s) > _MAX_STABLE:
            s.pop()
        fs = self._first_seen
        while len(fs) > _MAX_FIRST:
            fs.pop(next(iter(fs)))

    # Inject trim counter as instance attribute on first call
    def _with_trim(self):
        n = getattr(self, '_trim_count', 0) + 1
        self._trim_count = n
        if n % _TRIM_INTERVAL == 0:
            _trim_stable(self)

    def _patched_mark_stable(self, content_hash):
        _orig_mark_stable(self, content_hash)
        _with_trim(self)

    def _patched_mark_stable_msgs(self, messages, up_to):
        _orig_mark_stable_msgs(self, messages, up_to)
        _with_trim(self)

    def _patched_update(self, originals, compressed):
        _orig_update(self, originals, compressed)
        _with_trim(self)

    Klass.mark_stable = _patched_mark_stable
    Klass.mark_stable_from_messages = _patched_mark_stable_msgs
    Klass.update_from_result = _patched_update


# ── ContentRouter: fix skip_system ───────────────────────────────────────
# ContentRouter.apply() reads skip_system ONLY from per-request kwargs:
#   skip_system = kwargs.get("compress_system_messages") is not True
# The AGENT-90 profile sets compress_system_messages=True but the proxy
# handler never passes it as a per-request kwarg, so skip_system is
# always True and system messages are never compressed.
# Patch: if compress_system_messages is not in kwargs, default to True
# (matching the AGENT-90 profile intent).

_patch_content_router = False


def _patch_cr(mod):
    global _patch_content_router
    if _patch_content_router:
        return
    _patch_content_router = True

    # Debug: mark that patch ran
    Path("/home/administrator/.headroom/cr_patch_applied").write_text("ok")

    _original_apply = mod.ContentRouter.apply

    def _patched_apply(self, messages, tokenizer, **kwargs):
        if "compress_system_messages" not in kwargs:
            kwargs["compress_system_messages"] = True
        return _original_apply(self, messages, tokenizer, **kwargs)

    mod.ContentRouter.apply = _patched_apply

    # ── P0b: cap ContentRouter.CompressionCache._results ────────────────
    # 30-min TTL but no max-entries cap — second-largest leak after
    # session cache's _stable_hashes. Cap at 10k, FIFO eviction.
    # NOTE: this class uses threading.Lock (not RLock), so the wrapper
    # must hold the lock for the entire operation — no nested acquisition.
    _MAX_RESULTS = 10000
    _CR_CompressionCache = mod.CompressionCache

    def _patched_put(self, key, compressed, ratio, strategy):
        with self._lock:
            self._results[key] = (compressed, ratio, strategy, time.monotonic())
            if len(self._results) > _MAX_RESULTS:
                # TTL-first: evict expired before dropping oldest
                now = time.monotonic()
                expired = [k for k, v in self._results.items()
                           if (now - v[3]) >= self._ttl_seconds]
                for k in expired:
                    del self._results[k]
                    self._evictions += 1
                # Still over? FIFO: drop oldest-inserted (dict preserves order)
                while len(self._results) > _MAX_RESULTS:
                    self._results.pop(next(iter(self._results)))
                    self._evictions += 1

    _CR_CompressionCache.put = _patched_put


# ── Unified import hook ────────────────────────────────────────────────
# Single hook replacing both separate hooks. Tracks pending patches.

_original_import = builtins.__import__
_pending = {"kompress": True, "headroom": True, "compress_cache": True, "content_router": True}


def _unified_import_hook(name, *args, **kwargs):
    module = _original_import(name, *args, **kwargs)
    if name == "headroom.transforms.kompress_compressor" and _pending.get("kompress"):
        _patch_kompress(module)
        _pending["kompress"] = False
    elif name == "headroom.proxy.server" and _pending.get("headroom"):
        _apply_headroom_patch()
        _pending["headroom"] = False
    elif name == "headroom.cache.compression_cache" and _pending.get("compress_cache"):
        _patch_compression_cache(module)
        _pending["compress_cache"] = False
    elif name == "headroom.transforms.content_router" and _pending.get("content_router"):
        _patch_cr(module)
        _pending["content_router"] = False
    if not any(_pending.values()):
        builtins.__import__ = _original_import  # all done, restore
    return module


builtins.__import__ = _unified_import_hook
