from cc_harness.tool_bundles import bundle_digest, parse_tool_bundles, select_tool_specs


def _spec(name: str, **metadata):
    function = {"name": name, "parameters": {"type": "object"}}
    if metadata:
        function["x-cc-harness-capability"] = metadata
    return {"type": "function", "function": function}


def test_default_bundle_keeps_native_core_and_requires_explicit_mcp():
    bundles = parse_tool_bundles(None)
    specs = [_spec("Read"), _spec("mcp__fs__read"), _spec("mcp__web__fetch", bundle="web")]
    selected = select_tool_specs(specs, bundles, native_names={"Read"})
    assert [item["function"]["name"] for item in selected] == ["Read"]


def test_optional_mcp_and_web_bundles_are_stable():
    specs = [_spec("mcp__fs__read"), _spec("mcp__web__fetch", bundle="web")]
    assert len(select_tool_specs(specs, parse_tool_bundles("mcp"))) == 2
    assert [
        item["function"]["name"]
        for item in select_tool_specs(specs, parse_tool_bundles("web"))
    ] == ["mcp__web__fetch"]
    assert bundle_digest(specs, parse_tool_bundles("web")) == bundle_digest(
        specs, parse_tool_bundles("web")
    )
