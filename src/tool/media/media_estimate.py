##
 # @file src/tool/media/media_estimate.py
 # @date 2026/09/30
 # 
 # @brief Media cost estimation helpers (images and PDFs).
 #
 # @note The estimates are deliberately CONSERVATIVE: every formula rounds up
 # and applies a safety factor, so the local budget never under-counts what the
 # provider will charge. Under-counting is the only failure mode that can push
 # a request past the provider context limit; over-counting merely compacts a
 # little earlier.
 #
 # @note No third-party dependency is used:
 # - image dimensions come from file headers (PNG IHDR / JPEG SOFn / GIF LSD /
 #   WEBP VP8* chunks);
 # - PDF page count comes from a raw "/Type /Page" scan.
 #
 # @note The provider charges vision input by decoded pixels (Gemini tiles:
 # 258 tokens per 768x768 tile, or 258 for images <= 384px on both sides), and
 # by page for documents. The base64 payload length never participates in the
 # token accounting.
 #

import os

##
 # ========================================
 # @section I. Default constants
 # ========================================
 #

# Gemini vision: one tile of 768x768 pixels costs 258 tokens.
TOKENS_PER_IMAGE_TILE = 258
# Images whose both sides are <= 384px are counted as a single tile.
IMAGE_SMALL_SIDE_PX = 384
# Tile edge in pixels.
IMAGE_TILE_PX = 768

# Conservative per-page cost for a PDF page (treated as an image by the model).
TOKENS_PER_PDF_PAGE = 258
# Fallback page size used when the page-object scan is inconclusive.
PDF_BYTES_PER_PAGE_FALLBACK = 4096

# Safety factors (multiplied into the raw estimate, then rounded up).
IMAGE_COST_FACTOR = 1.2
PDF_COST_FACTOR = 1.2

# Fixed cost used when a media block carries neither a known cost nor a size.
MEDIA_COST_FALLBACK = 2000

# Pointer cost marker written into history, e.g. "cost=1860 tokens".
COST_MARKER_TEMPLATE = "cost={cost} tokens"

# Matches a whole media pointer, e.g. "[Multimodal asset: a.png (...)]".
# Imported by src/core/agent.py so the accounting has a single definition.
ASSET_POINTER_RE = r"\[Multimodal asset:[^\]]*\]"
_ASSET_POINTER_RE = __import__("re").compile(ASSET_POINTER_RE)

##
 # ========================================
 # @section II. Pointer cost marker
 # ========================================
 #

##
 # @brief Build the human readable, machine parseable cost marker.
 #
 # @param cost Estimated cost in tokens.
 #
 # @return Marker string like "cost=1860 tokens".
 #
def format_cost_marker(cost):
    return COST_MARKER_TEMPLATE.format(cost=int(cost))
# End-def

##
 # @brief Parse a cost marker out of a pointer string.
 #
 # @param text Text that may contain a "cost=<n> tokens" marker.
 #
 # @return int cost, or 0 when no marker is present.
 #
def parse_cost_marker(text):
    if not text:
        return 0

    marker = "cost="
    start = text.find(marker)
    if start < 0:
        return 0
    # End-if

    start += len(marker)
    end = text.find(" token", start)
    if end < 0:
        return 0
    # End-if

    try:
        return int(text[start:end].strip())
    except (TypeError, ValueError):
        return 0
    # End-try
# End-def

##
 # ========================================
 # @section III. Image size parsing (header only)
 # ========================================
 #

##
 # @brief Parse PNG dimensions from the IHDR chunk.
 #
 # @param data File header bytes (>= 24 bytes expected).
 #
 # @return (width, height) or None.
 #
def _parse_png_size(data):
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    # End-if
    if data[12:16] != b"IHDR":
        return None
    # End-if
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
# End-def

##
 # @brief Parse GIF dimensions from the logical screen descriptor.
 #
 # @param data File header bytes.
 #
 # @return (width, height) or None.
 #
def _parse_gif_size(data):
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    # End-if
    return (int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
# End-def

##
 # @brief Parse JPEG dimensions by scanning the SOFn frame header.
 #
 # @param data Whole file bytes (or the leading part of them).
 #
 # @return (width, height) or None.
 #
def _parse_jpeg_size(data):
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    # End-if

    # Iterate JPEG segments until a Start-Of-Frame marker is found.
    idx = 2
    total = len(data)
    while idx + 9 < total:
        if data[idx] != 0xFF:
            idx += 1
            continue
        # End-if

        marker = data[idx + 1]
        # SOF0..SOF15 except DHT (0xC4), JPG (0xC8) and DAC (0xCC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[idx + 5:idx + 7], "big")
            width = int.from_bytes(data[idx + 7:idx + 9], "big")
            return (width, height)
        # End-if

        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            idx += 2
            continue
        # End-if

        seg_len = int.from_bytes(data[idx + 2:idx + 4], "big")
        if seg_len < 2:
            return None
        # End-if
        idx += 2 + seg_len
    # End-while

    return None
# End-def

##
 # @brief Parse WEBP dimensions (VP8 lossy, VP8L lossless, VP8X extended).
 #
 # @param data Whole file bytes (or the leading part of them).
 #
 # @return (width, height) or None.
 #
def _parse_webp_size(data):
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    # End-if

    chunk = data[12:16]

    # VP8X: 24-bit (value - 1) canvas size.
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return (width, height)
    # End-if

    # VP8L: 14-bit (value + 1) dimensions packed after the 0x2F signature.
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height)
    # End-if

    # VP8 (lossy): dimensions in the frame header.
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height)
    # End-if

    return None
# End-def

##
 # @brief Read an image's pixel size without external libraries.
 #
 # @param file_path Path of the image file.
 #
 # @return (width, height) or None when the format is unsupported or malformed.
 #
def read_image_size(file_path):
    try:
        with open(file_path, "rb") as f:
            head = f.read(65536)
        # End-with
    except OSError:
        return None
    # End-try

    for parser in (_parse_png_size, _parse_gif_size, _parse_jpeg_size, _parse_webp_size):
        size = parser(head)
        if size:
            return size
        # End-if
    # End-for

    return None
# End-def

##
 # ========================================
 # @section IV. PDF page probing
 # ========================================
 #

##
 # @brief Probe a PDF page count by scanning page objects.
 #
 # @param file_path Path of the PDF file.
 #
 # @return int page count (>= 0; 0 means inconclusive).
 #
 # @note This is a heuristic: compressed object streams may hide page objects,
 # so callers must treat 0 as "unknown" and fall back to a size based guess.
 #
def read_pdf_pages(file_path):
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        # End-with
    except OSError:
        return 0
    # End-try

    if not data.startswith(b"%PDF"):
        return 0
    # End-if

    count = data.count(b"/Type /Page") + data.count(b"/Type/Page")
    # "/Pages" containers are matched by the same pattern, so subtract them.
    count -= data.count(b"/Type /Pages") + data.count(b"/Type/Pages")
    return max(count, 0)
# End-def

##
 # ========================================
 # @section V. Cost formulas
 # ========================================
 #

##
 # @brief Conservative image cost in tokens.
 #
 # @param file_path Path of the image file.
 # @param factor Safety factor (defaults to IMAGE_COST_FACTOR).
 #
 # @return int estimated tokens (never below MEDIA_COST_FALLBACK on failure).
 #
def estimate_image_cost(file_path, factor=IMAGE_COST_FACTOR):
    size = read_image_size(file_path)
    if not size:
        return int(MEDIA_COST_FALLBACK)
    # End-if

    width, height = size
    if width <= IMAGE_SMALL_SIDE_PX and height <= IMAGE_SMALL_SIDE_PX:
        tiles = 1
    else:
        tiles = _ceil_div(width, IMAGE_TILE_PX) * _ceil_div(height, IMAGE_TILE_PX)
    # End-if

    return max(int(tiles * TOKENS_PER_IMAGE_TILE * factor + 0.999), 1)
# End-def

##
 # @brief Conservative PDF cost in tokens.
 #
 # @param file_path Path of the PDF file.
 # @param factor Safety factor (defaults to PDF_COST_FACTOR).
 #
 # @return (cost_tokens, pages, page_source) where page_source is "scan" or
 # "fallback".
 #
def estimate_pdf_cost(file_path, factor=PDF_COST_FACTOR):
    pages = read_pdf_pages(file_path)
    source = "scan"
    if pages <= 0:
        try:
            size_bytes = os.path.getsize(file_path)
        except OSError:
            size_bytes = 0
        # End-try
        pages = max(int(size_bytes / PDF_BYTES_PER_PAGE_FALLBACK), 1)
        source = "fallback"
    # End-if

    cost = max(int(pages * TOKENS_PER_PDF_PAGE * factor + 0.999), 1)
    return cost, pages, source
# End-def

##
 # @brief Integer ceiling division.
 #
 # @param a Dividend.
 # @param b Divisor (must be > 0).
 #
 # @return int ceil(a / b).
 #
def _ceil_div(a, b):
    return (a + b - 1) // b
# End-def

##
 # ========================================
 # @section VI. Message list helpers
 # ========================================
 #

##
 # @brief Test whether a content block is a hydrated media block.
 #
 # @param block Candidate block (dict).
 #
 # @return True when the block carries base64 media data.
 #
def is_media_block(block):
    if not isinstance(block, dict):
        return False
    # End-if
    if block.get("type") not in ("image", "document"):
        return False
    # End-if
    source = block.get("source")
    return isinstance(source, dict) and source.get("type") == "base64"
# End-def

##
 # @brief Collect cost markers and hydrated media blocks of one payload.
 #
 # @param blocks Content payload: a block list, a single block, or a string.
 #
 # @return (marker_count, block_count)
 #
 # @note A payload may carry BOTH a pointer and the hydrated block for the same
 # media (the main agent dispatches them together), so the caller decides which
 # one to count; the marker is authoritative whenever it is present.
 #
def _count_payload(blocks):
    marker_count = 0
    block_count = 0

    def walk(value):
        nonlocal marker_count, block_count

        if isinstance(value, str):
            marker_count += len(_ASSET_POINTER_RE.findall(value))
            return
        # End-if

        if isinstance(value, dict):
            if value.get("type") == "text":
                walk(value.get("text", ""))
                return
            # End-if
            if is_media_block(value):
                block_count += 1
                return
            # End-if
            if value.get("type") == "tool_result":
                walk(value.get("content", ""))
                return
            # End-if
            for key, item in value.items():
                if key == "data" and isinstance(item, str):
                    continue
                # End-if
                walk(item)
            # End-for
            return
        # End-if

        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            # End-for
        # End-if
    # End-def walk

    walk(blocks)
    return marker_count, block_count
# End-def

##
 # @brief Count media payloads referenced by a message list.
 #
 # @param messages Message list (main agent history or SubAgent messages).
 #
 # @return int count of media units.
 #
 # @note Both storage shapes are supported because the two loops persist
 # differently: the main agent keeps pointer text ("[Multimodal asset: ...]")
 # in history, while a SubAgent's private list embeds the hydrated block.
 # A payload carrying both forms (pointer + block for the same media) is counted
 # ONCE, using the marker as the authoritative source.
 #
def count_media_in_messages(messages):
    count = 0

    for msg in messages or []:
        content = msg.get("content", "") if isinstance(msg, dict) else msg
        marker_count, block_count = _count_payload(content)
        count += marker_count if marker_count else block_count
    # End-for

    return count
# End-def

##
 # @brief Return a COPY of `messages` whose last message also carries the media
 # blocks, as SIBLING parts of the tool_result (never nested inside it).
 #
 # @param messages Message list (main agent history or SubAgent messages).
 # @param blocks Media blocks to attach (usually 1-2 per read).
 #
 # @return New message list; the input list and its messages are not mutated.
 #
 # @note Placement matters on the Anthropic-to-Gemini gateways: an inline
 # image/document part NESTED inside a tool_result.content list is dropped
 # (the model then answers from the text pointer alone and may hallucinate),
 # while the SAME part placed as a sibling of the tool_result in the same user
 # message is delivered correctly. Measured on Sub2API -> Antigravity ->
 # Gemini: nested = input_tokens 468 and wrong reading; sibling = 1542 tokens
 # and the correct reading.
 #
def attach_media_blocks(messages, blocks):
    if not blocks:
        return messages
    # End-if

    out = list(messages or [])

    # Preferred shape: append the parts to the message that owns the tool_result.
    if out and isinstance(out[-1], dict):
        last = dict(out[-1])
        content = last.get("content")
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") == "tool_result" for item in content
        ):
            last["content"] = list(content) + list(blocks)
            out[-1] = last
            return out
        # End-if
    # End-if

    # Fallback: a dedicated user message (merging providers normalise this into
    # the previous user message anyway).
    out.append({"role": "user", "content": list(blocks)})
    return out
# End-def

##
 # ========================================
 # @section VII. Generic text estimation for SubAgents
 # ========================================
 #

##
 # @brief Rough token estimate over a message list (SubAgent budget helper).
 #
 # @param messages Message list (same shape as the main agent history).
 #
 # @return float estimated tokens, using the same conservative heuristic as the
 # main agent (ASCII / 4, non-ASCII / 1.5) plus pointer cost markers.
 #
def estimate_messages_tokens(messages):
    ascii_chars = 0
    non_ascii_chars = 0
    media_cost = 0

    def scan(value):
        nonlocal ascii_chars, non_ascii_chars, media_cost

        if isinstance(value, str):
            for ch in value:
                if ord(ch) < 128:
                    ascii_chars += 1
                else:
                    non_ascii_chars += 1
                # End-if
            # End-for
            marker = parse_cost_marker(value)
            if marker:
                media_cost += marker
            # End-if
            return
        # End-if

        if isinstance(value, dict):
            btype = value.get("type")
            if btype in ("image", "document"):
                # Never tokenize base64 payloads; the pointer marker (counted
                # as text above) carries the cost, so nothing is added here.
                return
            # End-if
            for v in value.values():
                scan(v)
            # End-for
            return
        # End-if

        if isinstance(value, (list, tuple)):
            for item in value:
                scan(item)
            # End-for
        # End-if
    # End-def scan

    for msg in messages or []:
        if isinstance(msg, dict):
            scan(msg.get("content", ""))
        else:
            scan(msg)
        # End-if
    # End-for

    return ascii_chars / 4.0 + non_ascii_chars / 1.5 + media_cost
# End-def
