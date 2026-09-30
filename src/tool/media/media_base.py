##
 # @file src/tool/media/media_base.py
 # @date 2026/09/30
 # 
 # @brief Shared base class for multimodal media tools (PDF and image).
 #
 # @note Responsibilities shared by ReadPdfTool and ReadImageTool:
 # 1. workspace sandbox approval via BaseTool._prepare_path();
 # 2. extension whitelist enforcement from the model profile ("file" list);
 # 3. single-file byte limit enforcement (configurable per model);
 # 4. content-hash asset storage inside the session directory;
 # 5. structured result assembly (media block + history pointer + cost).
 #
 # @note Media results are NOT plain strings: the agent loop dispatches on the
 # dict shape (see "kind" below) and writes only the pointer into history, so a
 # base64 payload never reaches history.log or api.log.
 #

import os
import base64
import hashlib

from ..base_tool import BaseTool

##
 # ========================================
 # @section I. Media limits (per model profile)
 # ========================================
 #

# Defaults mirror the design document; every value can be overridden per model
# in .env/api.cfg, so a wrong guess is a config change rather than a code change.
DEFAULT_MEDIA_LIMITS = {
    "max_image_bytes": 15 * 1024 * 1024,
    "max_pdf_bytes": 15 * 1024 * 1024,
    "max_pdf_pages": 100,
    "max_media_per_request": 8,
    "pdf_tokens_per_page": 258,
    "image_cost_factor": 1.2,
    "pdf_cost_factor": 1.2,
    "media_cost_fallback": 2000,
}

# Supported media extensions mapped to (block kind, media type).
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
PDF_MEDIA_TYPE = "application/pdf"

##
 # ========================================
 # @section II. Asset storage helpers
 # ========================================
 #

##
 # @brief Resolve the asset directory for one session.
 #
 # @param session_dir Current session directory (absolute).
 #
 # @return Absolute path of "<session_dir>/assets".
 #
def asset_dir_for(session_dir):
    return os.path.join(session_dir, "assets")
# End-def

##
 # @brief Store a media file as a content addressed asset (atomic write).
 #
 # @param session_dir Current session directory.
 # @param source_path Existing file to copy from.
 # @param ext Lower-case extension including the dot.
 #
 # @return Absolute path of the stored asset, or None on failure.
 #
 # @note Naming by content hash means re-reading the same file reuses the same
 # asset (stable pointer text, no duplicate copies).
 #
def store_asset(session_dir, source_path, ext):
    try:
        with open(source_path, "rb") as f:
            payload = f.read()
        # End-with

        digest = hashlib.sha256(payload).hexdigest()[:16]
        target_dir = asset_dir_for(session_dir)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, f"{digest}{ext}")

        if not os.path.exists(target):
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(payload)
            # End-with
            os.replace(tmp, target)
        # End-if

        return os.path.abspath(target)
    except OSError:
        return None
    # End-try
# End-def

##
 # @brief Build the pointer text written into history.
 #
 # @param rel_path Path as the model referred to it.
 # @param media_type MIME type of the payload.
 # @param size_bytes File size in bytes.
 # @param cost Estimated token cost.
 # @param extra Extra detail (e.g. "pages=210"), may be empty.
 # @param asset_path Stored asset path.
 #
 # @return Pointer string carrying a parseable cost marker.
 #
def build_pointer(rel_path, media_type, size_bytes, cost, extra, asset_path):
    from .media_estimate import format_cost_marker

    detail = f", {extra}" if extra else ""
    return (
        f"[Multimodal asset: {rel_path} ({media_type}, "
        f"{size_bytes / (1024 * 1024):.2f} MB{detail}, "
        f"{format_cost_marker(cost)}) at {asset_path}]"
    )
# End-def

##
 # @brief Human readable byte size for messages.
 #
 # @param size_bytes Size in bytes.
 #
 # @return String like "1.5 MB".
 #
def human_size(size_bytes):
    return f"{size_bytes / (1024 * 1024):.2f} MB"
# End-def

##
 # ========================================
 # @section III. Base media tool
 # ========================================
 #

##
 # @brief Shared behaviour for the media tools.
 #
 # @note Host hooks used by subclasses:
 # - self.host: agent/subagent exposing available_token_budget();
 # - self.session_dir_fn: callable returning the current session directory;
 # - self.file_exts: extension whitelist for this model (None = permissive).
 #
class MediaToolBase(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Workspace root (sandbox boundary).
     # @param host Agent/SubAgent providing available_token_budget().
     # @param session_dir_fn Callable returning the current session directory.
     # @param media_limits Per-model limits dict (see DEFAULT_MEDIA_LIMITS).
     # @param file_exts Extension whitelist for this model (None = permissive).
     # @param kind "image" or "document".
     #
    def __init__(self, workspace_dir=None, host=None, session_dir_fn=None,
                 media_limits=None, file_exts=None, kind="image"):
        super().__init__(workspace_dir)
        self.host = host
        self.session_dir_fn = session_dir_fn
        self.media_limits = dict(DEFAULT_MEDIA_LIMITS)
        if media_limits:
            for key, value in media_limits.items():
                if value is not None:
                    self.media_limits[key] = value
                # End-if
            # End-for
        # End-if
        self.file_exts = set(file_exts) if file_exts else None
        self.kind = kind
    # End-def

    ##
     # @brief Media block kinds this tool produces.
     #
    def get_kind(self):
        return self.kind
    # End-def

    ##
     # @brief Extensions this tool accepts (subclass responsibility).
     #
    def supported_extensions(self):
        return set()
    # End-def

    ##
     # @brief Supported extensions actually enabled for this model.
     #
     # @note An empty/absent whitelist means "not configured": the tool is then
     # lenient and accepts its own default set (the agent only registers the
     # tool when the model declares media support anyway).
     #
    def allowed_extensions(self):
        defaults = self.supported_extensions()
        if not self.file_exts:
            return defaults
        # End-if
        return {e for e in defaults if e in self.file_exts}
    # End-def

    ##
     # @brief Size limit that applies to this tool.
     #
    def size_limit_bytes(self):
        return int(self.media_limits.get("max_image_bytes", DEFAULT_MEDIA_LIMITS["max_image_bytes"]))
    # End-def

    ##
     # @brief Enabled media extensions per model profile.
     #
     # @param profile Active model profile (dict) or None.
     #
     # @return list of enabled extensions, or None when unconfigured.
     #
    @staticmethod
    def enabled_extensions(profile):
        if not isinstance(profile, dict):
            return None
        # End-if
        raw = profile.get("file")
        if raw is None:
            return None
        # End-if
        if not isinstance(raw, (list, tuple)):
            return []
        # End-if
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    # End-def

    ##
     # @brief Extract per-model media limits from a model profile.
     #
     # @param profile Active model profile (dict) or None.
     #
     # @return dict merged over DEFAULT_MEDIA_LIMITS.
     #
    @staticmethod
    def limits_from_profile(profile):
        limits = dict(DEFAULT_MEDIA_LIMITS)
        if not isinstance(profile, dict):
            return limits
        # End-if

        for key in limits.keys():
            if key not in profile:
                continue
            # End-if
            value = profile.get(key)
            try:
                if key.startswith("max_") and key.endswith("_bytes"):
                    limits[key] = parse_size_bytes(value, int(limits[key]))
                elif key.startswith("max_"):
                    limits[key] = max(int(value), 1)
                else:
                    limits[key] = float(value)
                # End-if
            except (TypeError, ValueError):
                print(f"[-] Warning: invalid media limit '{key}'={value!r}; using default.")
            # End-try
        # End-for

        return limits
    # End-def

    ##
     # @brief Run the shared admission pipeline.
     #
     # @param file_path Raw path argument from the tool call.
     # @param action_desc Human readable action for the sandbox prompt.
     #
     # @return (resolved_path, ext, media_type, size_bytes, error)
     #
    def prepare_media(self, file_path, action_desc):
        if not file_path:
            return None, None, None, 0, "Error: No file path provided."

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace_dir, file_path)
        # End-if
        file_path = os.path.abspath(file_path)

        resolved, err = self._prepare_path(file_path, action_desc=action_desc)
        if err:
            return None, None, None, 0, err
        # End-if

        if not os.path.exists(resolved):
            return None, None, None, 0, f"Error: File not found at '{file_path}'"
        # End-if
        if not os.path.isfile(resolved):
            return None, None, None, 0, f"Error: Path is not a file: '{file_path}'"
        # End-if

        ext = os.path.splitext(resolved)[1].lower()
        allowed = self.allowed_extensions()
        if ext not in allowed:
            return None, None, None, 0, self._unsupported_message(ext, allowed)
        # End-if

        media_type = self.media_type_for(ext)
        if not media_type:
            return None, None, None, 0, self._unsupported_message(ext, allowed)
        # End-if

        if not self.header_matches(resolved, ext):
            return None, None, None, 0, (
                f"Error: '{os.path.basename(resolved)}' does not look like a valid "
                f"{ext} file (magic bytes mismatch)."
            )
        # End-if

        try:
            size_bytes = os.path.getsize(resolved)
        except OSError as e:
            return None, None, None, 0, f"Error: cannot stat '{file_path}': {e}"
        # End-try

        limit = self.size_limit_bytes()
        if size_bytes > limit:
            return None, None, None, 0, self._oversize_message(file_path, size_bytes, limit)
        # End-if

        return resolved, ext, media_type, size_bytes, None
    # End-def

    ##
     # @brief MIME type for an extension (subclass responsibility).
     #
    def media_type_for(self, ext):
        return None
    # End-def

    ##
     # @brief Verify the file magic bytes match the extension.
     #
    def header_matches(self, resolved_path, ext):
        return True
    # End-def

    ##
     # @brief Remaining token budget reported by the host agent.
     #
     # @return int remaining tokens, or None when the host cannot report one.
     #
    def available_budget(self):
        getter = getattr(self.host, "available_token_budget", None)
        if not callable(getter):
            return None
        # End-if
        try:
            return int(getter())
        except Exception:
            return None
        # End-try
    # End-def

    ##
     # @brief Current session directory (None when unavailable).
     #
    def current_session_dir(self):
        if not callable(self.session_dir_fn):
            return None
        # End-if
        try:
            return self.session_dir_fn()
        except Exception:
            return None
        # End-try
    # End-def

    ##
     # @brief Read the payload and assemble the structured tool result.
     #
     # @param resolved_path Resolved file path.
     # @param ext Lower-case extension.
     # @param media_type MIME type.
     # @param size_bytes File size in bytes.
     # @param cost Estimated token cost.
     # @param extra Extra pointer detail (e.g. "pages=210").
     # @param display_path Path shown to the model.
     #
     # @return (success, output_dict or error_string)
     #
    def build_result(self, resolved_path, ext, media_type, size_bytes, cost,
                     extra, display_path):
        session_dir = self.current_session_dir()
        if not session_dir:
            return False, "Error: no active session directory for asset storage."
        # End-if

        asset_path = store_asset(session_dir, resolved_path, ext)
        if not asset_path:
            return False, f"Error: failed to store the media asset for '{display_path}'."
        # End-if

        try:
            with open(resolved_path, "rb") as f:
                encoded = base64.standard_b64encode(f.read()).decode("ascii")
            # End-with
        except OSError as e:
            return False, f"Error: cannot read '{display_path}': {e}"
        # End-try

        pointer = build_pointer(display_path, media_type, size_bytes, cost, extra, asset_path)

        summary = (
            f"Loaded {self.kind} '{display_path}' ({human_size(size_bytes)}). "
            f"Estimated context cost: {cost} tokens. The content is visible in THIS "
            f"turn only; later turns see just the pointer, so re-read the asset if it "
            f"is needed again."
        )

        output = {
            "kind": self.kind,
            "block": {
                "type": self.kind,
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": encoded,
                },
            },
            "pointer": pointer,
            "media_cost": int(cost),
            "asset_path": asset_path,
            "summary": summary,
        }
        return True, output
    # End-def

    ##
     # @brief Remaining media slots reported by the host agent.
     #
     # @return int slots, or None when the host cannot report them.
     #
    def available_slots(self):
        getter = getattr(self.host, "available_media_slots", None)
        if not callable(getter):
            return None
        # End-if
        try:
            return int(getter())
        except Exception:
            return None
        # End-try
    # End-def

    ##
     # @brief Budget admission check (tokens and per-request media slots).
     #
     # @param cost Estimated token cost of the payload.
     # @param display_path Path as the model wrote it.
     #
     # @return None when admitted, otherwise the error string to return.
     #
    def check_budget(self, cost, display_path):
        # 1. Per-request media count: providers cap how many media blocks one
        #    request may carry, and that cap is independent of the token ledger.
        slots = self.available_slots()
        if slots is not None and slots <= 0:
            return (
                f"Error: this request already carries the maximum number of media "
                f"blocks ({self.media_limits.get('max_media_per_request')}); "
                f"'{display_path}' cannot be added. Finish the current turn, or "
                f"delegate the reading to a SubAgent."
            )
        # End-if

        # 2. Token budget.
        budget = self.available_budget()
        if budget is None:
            return None
        # End-if

        if cost > budget:
            return (
                f"Error: reading '{display_path}' would cost about {cost} tokens, "
                f"but only {budget} tokens are available in the current context. "
                f"Delegate the reading to a SubAgent (spawn_subagent with "
                f"toolset=\"filesystem\"), or finish the current context and retry "
                f"later. Do NOT retry immediately."
            )
        # End-if

        return None
    # End-def

    ##
     # @brief Standard "unsupported extension" message.
     #
    def _unsupported_message(self, ext, allowed):
        return (
            f"Error: unsupported media extension '{ext}'. Supported: "
            f"{', '.join(sorted(allowed))}. For text or vector files, use "
            f"read_file instead."
        )
    # End-def

    ##
     # @brief Standard "too large" message.
     #
    def _oversize_message(self, display_path, size_bytes, limit):
        return (
            f"Error: '{display_path}' is too large ({human_size(size_bytes)}, "
            f"limit {human_size(limit)}). Provide a smaller version, a cropped "
            f"region, or a lower-resolution copy. Do NOT retry the same file."
        )
    # End-def
# End-class

##
 # @brief Parse a size value that may be an int or a string like "15MB".
 #
 # @param value Raw config value.
 # @param default Value used when parsing fails.
 #
 # @return int size in bytes.
 #
def parse_size_bytes(value, default):
    if isinstance(value, bool):
        return default
    # End-if
    if isinstance(value, (int, float)):
        return int(value)
    # End-if
    if not isinstance(value, str):
        return default
    # End-if

    text = value.strip().lower()
    units = (("gb", 1024 ** 3), ("mb", 1024 ** 2), ("kb", 1024), ("b", 1))
    for suffix, scale in units:
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)].strip()) * scale)
            except ValueError:
                return default
            # End-try
        # End-if
    # End-for

    try:
        return int(float(text))
    except ValueError:
        return default
    # End-try
# End-def
