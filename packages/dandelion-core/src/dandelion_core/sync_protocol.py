##
 # @file packages/dandelion-core/src/dandelion_core/sync_protocol.py
 # @date 2026/11/28
 #
 # @brief Session sync wire protocol shared by the CLI client and Remote server.
 # Defines the synced file set, chunk size, access-key digest, and deterministic
 # snapshot digest so both ends always agree on the same bytes.
 #

import hashlib
import json

REQUIRED_FILES = ("history.log", "meta.log")
OPTIONAL_FILES = ("task_state.json",)
ALLOWED_FILES = REQUIRED_FILES + OPTIONAL_FILES
CHUNK_SIZE = 256 * 1024


##
 # @brief Digest an access key into the lowercase MD5 hex sent as Bearer token.
 #
 # @param access_key Raw access key string.
 #
 # @return 32-character lowercase ASCII hex string.
 #
def key_digest(access_key):
    return hashlib.md5(str(access_key).strip().encode("utf-8")).hexdigest()
# End-def


##
 # @brief Compute the deterministic SHA-256 snapshot ID of a sync manifest.
 #
 # @param manifest Mapping with device, project, session_id, and files (or session_id
 #        string when files_meta is passed as the second argument).
 # @param files_meta Optional file metadata mapping for 2-argument callers.
 #
 # @return SHA-256 hex digest of the canonical JSON manifest.
 #
def snapshot_id(manifest, files_meta=None):
    if files_meta is not None:
        payload = {"session_id": manifest, "files": files_meta}
    else:
        payload = manifest
    # End-if
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
# End-def
