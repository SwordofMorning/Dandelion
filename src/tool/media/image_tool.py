##
 # @file src/tool/media/image_tool.py
 # @date 2026/09/30
 # 
 # @brief Read Image Tool (multimodal).
 #
 # @note The image is sent as an Anthropic "image" block (base64). Admission is
 # size based only, per design: the token cost is still estimated for budget
 # accounting, but no down-scaling or tiling is performed locally.
 #
 # @note SVG is not an Anthropic-supported image media type, so it is rejected
 # with a pointer to read_file (text) instead.
 #

import os

from .media_base import MediaToolBase, IMAGE_MEDIA_TYPES, human_size
from .media_estimate import estimate_image_cost, read_image_size

##
 # @brief Read Image Class.
 #
class ReadImageTool(MediaToolBase):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     # @param host Agent/SubAgent providing available_token_budget().
     # @param session_dir_fn Callable returning the current session directory.
     # @param media_limits Per-model media limits.
     # @param file_exts Extension whitelist for this model.
     #
    def __init__(self, workspace_dir=None, host=None, session_dir_fn=None,
                 media_limits=None, file_exts=None):
        super().__init__(workspace_dir=workspace_dir, host=host,
                         session_dir_fn=session_dir_fn, media_limits=media_limits,
                         file_exts=file_exts, kind="image")
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "read_image"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Read an image (schematic screenshots, waveform captures, PCB photos) "
            "and make it visible to the model for THIS turn. Oversized files are "
            "rejected: provide a smaller or cropped copy in that case. Only the "
            "pointer stays in history, so re-read the asset when the content is "
            "needed again. SVG is not supported (use read_file for text)."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the image file to read."
                }
            },
            "required": ["path"]
        }
    # End-def

    ##
     # @brief Supported extensions for this tool.
     #
    def supported_extensions(self):
        return set(IMAGE_MEDIA_TYPES.keys())
    # End-def

    ##
     # @brief MIME type for an extension.
     #
    def media_type_for(self, ext):
        return IMAGE_MEDIA_TYPES.get(ext)
    # End-def

    ##
     # @brief Size limit for images.
     #
    def size_limit_bytes(self):
        return int(self.media_limits.get("max_image_bytes", 15 * 1024 * 1024))
    # End-def

    ##
     # @brief Verify the file magic bytes match the extension.
     #
    def header_matches(self, resolved_path, ext):
        try:
            with open(resolved_path, "rb") as f:
                head = f.read(16)
            # End-with
        except OSError:
            return False
        # End-try

        if ext == ".png":
            return head.startswith(b"\x89PNG\r\n\x1a\n")
        if ext in (".jpg", ".jpeg"):
            return head.startswith(b"\xff\xd8")
        if ext == ".gif":
            return head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
        if ext == ".webp":
            return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
        return False
    # End-def

    ##
     # @brief Execute the read.
     #
     # @param kwargs schema properties: path.
     #
     # @return (success_bool, result_dict or error_string)
     #
    def execute(self, **kwargs):
        raw_path = kwargs.get("path") or kwargs.get("file_path") or ""

        resolved, ext, media_type, size_bytes, err = self.prepare_media(
            raw_path, action_desc=f"READ IMAGE (visual) at '{raw_path}'")
        if err:
            return False, err
        # End-if

        cost_factor = float(self.media_limits.get("image_cost_factor", 1.2))
        cost = estimate_image_cost(resolved, factor=cost_factor)

        budget_err = self.check_budget(cost, raw_path)
        if budget_err:
            return False, budget_err
        # End-if

        size = read_image_size(resolved)
        extra = f"pixels={size[0]}x{size[1]}" if size else "pixels=unknown"

        success, output = self.build_result(
            resolved, ext, media_type, size_bytes, cost, extra, raw_path)
        return success, output
    # End-def
# End-class

##
 # @brief Module level shortcut used by the tool factory.
 #
 # @param kwargs Arguments forwarded to ReadImageTool.
 #
 # @return ReadImageTool instance.
 #
def make_tool(**kwargs):
    return ReadImageTool(**kwargs)
# End-def
