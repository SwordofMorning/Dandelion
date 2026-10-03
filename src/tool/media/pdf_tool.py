##
 # @file src/tool/media/pdf_tool.py
 # @date 2026/09/30
 # 
 # @brief Read PDF Tool (multimodal).
 #
 # @note This tool sends the WHOLE PDF as an Anthropic "document" block. Page
 # ranges are deliberately NOT supported: the Anthropic protocol has no page
 # selection field (page numbers only appear in the response citations), and
 # slicing locally would require a PDF library.
 #
 # @note Admission pipeline: workspace sandbox -> extension whitelist -> magic
 # bytes -> byte limit -> page limit -> token budget. Any rejection returns an
 # actionable message, because that text stays in history until compaction and
 # decides whether the model retries or delegates.
 #

import os

from .media_base import MediaToolBase, PDF_MEDIA_TYPE, human_size
from .media_estimate import estimate_pdf_cost, TOKENS_PER_PDF_PAGE

##
 # @brief Read PDF Class.
 #
class ReadPdfTool(MediaToolBase):
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
                         file_exts=file_exts, kind="document")
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "read_pdf"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Read the visual content of a PDF file (datasheets, schematics, "
            "reports) and make it visible to the model for THIS turn. The whole "
            "document is sent; page ranges are not supported. Oversized files are "
            "rejected: in that case delegate the reading to a SubAgent "
            "(spawn_subagent with toolset=\"filesystem\"). Only the pointer stays "
            "in history, so re-read the asset when the content is needed again."
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
                    "description": "Absolute or relative path to the PDF file to read."
                }
            },
            "required": ["path"]
        }
    # End-def

    ##
     # @brief Supported extensions for this tool.
     #
    def supported_extensions(self):
        return {".pdf"}
    # End-def

    ##
     # @brief MIME type for an extension.
     #
    def media_type_for(self, ext):
        return PDF_MEDIA_TYPE if ext == ".pdf" else None
    # End-def

    ##
     # @brief Size limit for PDFs.
     #
    def size_limit_bytes(self):
        return int(self.media_limits.get("max_pdf_bytes", 15 * 1024 * 1024))
    # End-def

    ##
     # @brief Verify the file starts with the PDF signature.
     #
    def header_matches(self, resolved_path, ext):
        try:
            with open(resolved_path, "rb") as f:
                return f.read(5).startswith(b"%PDF")
            # End-with
        except OSError:
            return False
        # End-try
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
            raw_path, action_desc=f"READ PDF (visual) at '{raw_path}'")
        if err:
            return False, err
        # End-if

        # Page probe + conservative cost. The page count is also the page limit gate.
        cost_factor = float(self.media_limits.get("pdf_cost_factor", 1.2))
        cost, pages, page_source = estimate_pdf_cost(resolved, factor=cost_factor)

        max_pages = int(self.media_limits.get("max_pdf_pages", 100))
        if pages > max_pages:
            return False, self._too_many_pages_message(raw_path, pages, max_pages, size_bytes)
        # End-if

        budget_err = self.check_budget(cost, raw_path)
        if budget_err:
            return False, budget_err
        # End-if

        extra = f"pages={pages}" + ("" if page_source == "scan" else ", pages=estimated")
        success, output = self.build_result(
            resolved, ext, media_type, size_bytes, cost, extra, raw_path)
        return success, output
    # End-def

    ##
     # @brief Refusal message for oversized documents.
     #
     # @param display_path Path as the model wrote it.
     # @param pages Detected page count.
     # @param max_pages Allowed page count.
     # @param size_bytes File size.
     #
    def _too_many_pages_message(self, display_path, pages, max_pages, size_bytes):
        return (
            f"Error: '{display_path}' has {pages} pages ({human_size(size_bytes)}), "
            f"above the single-request limit of {max_pages} pages. This tool cannot "
            f"send a page range. Options: (1) delegate with spawn_subagent "
            f"(toolset=\"filesystem\") and ask the SubAgent to read the file and "
            f"extract only the needed parts; (2) ask the user which page matters so a "
            f"smaller extract can be provided. Do NOT call read_pdf on the same file "
            f"again. (For reference the model charges about {TOKENS_PER_PDF_PAGE} "
            f"tokens per page.)"
        )
    # End-def
# End-class

##
 # @brief Module level shortcut used by the tool factory.
 #
 # @param kwargs Arguments forwarded to ReadPdfTool.
 #
 # @return ReadPdfTool instance.
 #
def make_tool(**kwargs):
    return ReadPdfTool(**kwargs)
# End-def
