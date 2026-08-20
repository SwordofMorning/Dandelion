##
 # @file src/tool/system/time_tool.py
 # @date 2026/08/19
 # 
 # @brief Get current system time for the Agent (LLM).
 #
 # @note Returns the current date/time in the system timezone (or an optional
 # override) plus the Unix epoch in seconds. Weekday names are fixed English,
 # independent of the host locale. See DESIGN.md for the locked output format:
 #   "2026-08-19 21:00:00 (Wednesday) (UTC+8)"
 #   "Epoch (Unix seconds): 1787144400"
 #

import datetime
import re

from ..base_tool import BaseTool

# Fixed English weekday names (locale-independent; %A follows the host locale).
_WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday"
]

# Default strftime pattern for the date+time part.
_DEFAULT_FORMAT = "%Y-%m-%d %H:%M:%S"

# UTC offset forms: "UTC+8", "UTC-5", "UTC+8:30", "UTC+05:45".
_UTC_OFFSET_RE = re.compile(r"^UTC([+-])(\d{1,2})(?::?(\d{2}))?$")

# Safety cap for the format string (LLM-provided input).
_MAX_FORMAT_LEN = 64

##
 # @brief Time Tool Class.
 #
 # @note Default timezone = system local (datetime.now().astimezone(), DST
 # handled automatically). Optional overrides: fixed UTC offsets or IANA
 # names via zoneinfo.
 #
class TimeTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     # @param config User config; optional TIMEZONE key pins a default
     # timezone. Absent/empty -> follow the system timezone.
     #
    def __init__(self, workspace_dir=None, config=None):
        super().__init__(workspace_dir)
        self.config = config or {}
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "get_time"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Get the current system time. Returns the current date, time, weekday and timezone "
            "(e.g. '2026-08-19 21:00:00 (Wednesday) (UTC+8)') plus the Unix epoch in seconds. "
            "Use this when you need the current time, date, weekday, or a timestamp. "
            "Optional 'timezone' overrides the zone ('UTC+8', 'UTC-5', 'UTC+8:30', or an IANA "
            "name like 'Asia/Shanghai'); optional 'format' is a strftime pattern for the "
            "date+time part."
        )
    # End-def

    ##
     # @brief Return tool's schema.
     #
     # @note No required fields; the default call (no arguments) returns the
     # system local time.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "Optional timezone override. Forms: 'UTC+8', 'UTC-5', 'UTC+8:30', "
                        "or an IANA name like 'Asia/Shanghai'. Default: the system's local timezone."
                    )
                },
                "format": {
                    "type": "string",
                    "description": (
                        "Optional strftime pattern for the date+time part. "
                        "Default: '%Y-%m-%d %H:%M:%S'."
                    )
                }
            },
            "required": []
        }
    # End-def

    ##
     # @brief Execute the tool.
     #
     # @param kwargs schema properties: timezone (optional), format (optional).
     #
     # @return (success_bool, result_string) result_string is:
     #   "2026-08-19 21:00:00 (Wednesday) (UTC+8)\nEpoch (Unix seconds): 1787144400"
     # on success; "Error: ..." with a hint on failure.
     #
    def execute(self, **kwargs):
        # ----- @par 1. Resolve timezone -----
        tz_override = kwargs.get("timezone") or self.config.get("TIMEZONE") or None
        now, tz_label, err = self._resolve_now(tz_override)
        if err:
            return False, err
        # End-if

        # ----- @par 2. Resolve format -----
        fmt = kwargs.get("format", _DEFAULT_FORMAT)
        if not isinstance(fmt, str) or not fmt:
            return False, "Error: 'format' must be a non-empty string (strftime pattern)."
        # End-if
        if len(fmt) > _MAX_FORMAT_LEN:
            return False, f"Error: 'format' is too long (max {_MAX_FORMAT_LEN} chars)."
        # End-if
        try:
            formatted = now.strftime(fmt)
        except ValueError as e:
            return False, f"Error: invalid strftime pattern '{fmt}': {e}"
        # End-try

        # ----- @par 3. Render locked output -----
        weekday = _WEEKDAYS[now.weekday()]
        line1 = f"{formatted} ({weekday}) ({tz_label})"
        line2 = f"Epoch (Unix seconds): {int(now.timestamp())}"
        return True, line1 + "\n" + line2
    # End-def execute

    ##
     # @brief Resolve the current aware datetime and its timezone label.
     #
     # @param tz_override None (system local), "UTC+N"/"UTC-N" offset, or IANA name.
     #
     # @return (aware_datetime, tz_label, None) on success, or
     # (None, "", error_message) on failure.
     #
    def _resolve_now(self, tz_override):
        # System local timezone (DST handled automatically).
        if not tz_override:
            now = datetime.datetime.now().astimezone()
            return now, self._tz_label(now.utcoffset()), None
        # End-if

        # Fixed UTC offset: "UTC+8", "UTC-5", "UTC+8:30", "UTC+05:45".
        m = _UTC_OFFSET_RE.match(str(tz_override).strip())
        if m:
            sign = 1 if m.group(1) == "+" else -1
            hours = int(m.group(2))
            minutes = int(m.group(3) or 0)
            # Check for UTC+14 and UTC-12
            max_hours = 14 if sign > 0 else 12
            if (hours > max_hours or minutes > 59 or (hours == max_hours and minutes != 0)):
                return None, "", (
                    f"Error: invalid UTC offset '{tz_override}'. "
                    "Valid range is UTC-12:00 to UTC+14:00."
                )
            # End-if
            offset = datetime.timedelta(hours=hours, minutes=minutes) * sign
            now = datetime.datetime.now(datetime.timezone(offset))
            return now, self._tz_label(offset), None
        # End-if

        # IANA name (DST-aware), e.g. "Asia/Shanghai", "America/New_York".
        try:
            from zoneinfo import ZoneInfo
            tzinfo = ZoneInfo(str(tz_override).strip())
        except Exception:
            return None, "", (
                f"Error: unsupported timezone '{tz_override}'. "
                "Use a UTC offset like 'UTC+8', 'UTC-5', 'UTC+8:30', or an IANA name like "
                "'Asia/Shanghai'. Omit 'timezone' to follow the system timezone."
            )
        # End-try
        now = datetime.datetime.now(tzinfo)
        label = f"{tzinfo.key} ({self._tz_label(now.utcoffset())})"
        return now, label, None
    # End-def _resolve_now

    ##
     # @brief Render a timedelta offset as a human label.
     #
     # @param offset datetime.timedelta (or None).
     #
     # @return "UTC+8", "UTC-5", "UTC+8:30", "UTC-3:30", etc.
     #
    @staticmethod
    def _tz_label(offset):
        if offset is None:
            return "UTC+0"
        # End-if
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours = abs(total_minutes) // 60
        minutes = abs(total_minutes) % 60
        if minutes == 0:
            return f"UTC{sign}{hours}"
        return f"UTC{sign}{hours}:{minutes:02d}"
    # End-def
# End-class
