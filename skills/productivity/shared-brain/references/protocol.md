# Protocol

The client sends exactly one UTF-8 JSON request plus LF to `/run/user/1000/gbrain-nano-broker/gbrain-nano.sock`, half-closes, and accepts exactly one LF-terminated JSON response. Request frames are capped at 32 KiB; response frames at 256 KiB; timeout is 10 seconds.

The closed request shape is `version`, `request_id`, `operation`, `source`, and `params`. Read operations are `sources`, `search`, `get`, and `graph`. `capture` is recognized only so it can return the stable `forbidden` response; it has no success result. Unsupported operations such as `think`, `query`, source administration, raw data, file access, and socket selection are rejected locally.

Success responses are closed and bound to the request ID and source. Search count, page reference, graph root, and graph depth are checked against the originating request before any result is printed.
