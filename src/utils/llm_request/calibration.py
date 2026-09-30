##
 # @file src/utils/llm_request/calibration.py
 # @date 2026/09/30
 # 
 # @brief Post-call context calibration helpers.
 #
 # @note The provider reports the real input size of a finished request through
 # the response usage object. That measurement is used to correct the LOCAL
 # heuristic estimate over time.
 #
 # @note Policy (deliberately conservative): the correction ratio can only grow
 # and is capped. Under-estimating is the only failure mode that can push a
 # request past the provider context limit, so a calibration that shrinks the
 # local estimate must never happen. Over-estimating merely compacts earlier.
 #

##
 # @brief Sum every "*tokens" field of a usage object.
 #
 # @param usage Usage object (SDK object or dict) or None.
 #
 # @return int total tokens, 0 when nothing usable was found.
 #
 # @note Input side fields (input_tokens / cache_read_input_tokens /
 # cache_creation_input_tokens) describe the context the model actually saw.
 # output_tokens describes what it generated, so adding it over-counts - which
 # is the safe direction and keeps the rule simple: add every *tokens field.
 #
def usage_total_tokens(usage):
    if usage is None:
        return 0
    # End-if

    total = 0
    found = False

    # Dict style first (OpenAI-compatible paths and any hand-built payloads).
    if isinstance(usage, dict):
        source = usage
    else:
        # SDK object style: only read public attributes.
        source = {}
        for key in dir(usage):
            if key.startswith("_"):
                continue
            # End-if
            if not key.endswith("tokens"):
                continue
            # End-if
            try:
                source[key] = getattr(usage, key)
            except Exception:
                continue
            # End-try
        # End-for
    # End-if

    for key, value in source.items():
        if not str(key).endswith("tokens"):
            continue
        # End-if
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        # End-if
        total += int(value)
        found = True
    # End-for

    return total if found else 0
# End-def

##
 # @brief Current calibration ratio ceiling.
 #
 # @param config Runtime config dict.
 #
 # @return float cap (>= 1.0).
 #
def ratio_cap(config):
    try:
        cap = float((config or {}).get("CALIB_RATIO_CAP", 1.6))
    except (TypeError, ValueError):
        cap = 1.6
    # End-try
    return max(cap, 1.0)
# End-def

##
 # @brief Update the calibration ratio from one measurement.
 #
 # @param current Current ratio (>= 1.0).
 # @param local_est Local estimate for the payload that was just sent.
 # @param measured Measured provider total (usage_total_tokens()).
 # @param cap Upper bound of the ratio.
 #
 # @return (new_ratio, observed_ratio) - new_ratio >= current, never above cap.
 #
 # @note observed is returned unchanged for audit logging, even when it is
 # smaller than the current ratio (a measurement below the local estimate is
 # the expected, healthy case and must not lower the ratio).
 #
def update_ratio(current, local_est, measured, cap):
    try:
        current = float(current)
    except (TypeError, ValueError):
        current = 1.0
    # End-try
    current = max(current, 1.0)

    if not local_est or measured <= 0:
        return current, 0.0
    # End-if

    observed = float(measured) / float(local_est)
    return min(max(current, observed), cap), observed
# End-def
