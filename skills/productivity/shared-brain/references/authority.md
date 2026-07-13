# Authority

The mounted socket directory is the only authorization grant. It permits source-scoped shared recall only; it does not grant host, database, GBrain configuration, credentials, source administration, raw-data, private-memory, HTTP, or MCP access.

Do not install or use this skill until the host broker's read-only CLI preflight has passed for every read operation. A preflight failure means no source-scoped recall is authorized.

All three aliases are read-only. Selected-fact capture has no approved adapter and is not an installation option; the broker returns `forbidden` without calling GBrain. An omitted or unknown source is forbidden.
