# Use stable prompt prefixes and capability tool bundles

Model requests use a versioned stable prefix followed by a dynamic context suffix, and expose tools through a small default coding bundle plus explicitly enabled Web, MCP, or domain bundles. This trades per-call tool selection flexibility for deterministic provider-cache reuse while avoiding the persistent token cost of sending every available schema; contract or bundle changes deliberately begin a new cache epoch.
