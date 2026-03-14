from nanobot.config.schema import MCPServerConfig


def test_mcp_server_config_accepts_explicit_transport_type() -> None:
    cfg = MCPServerConfig(type="sse", url="https://example.com/mcp/sse")

    assert cfg.type == "sse"
    assert cfg.url == "https://example.com/mcp/sse"


def test_mcp_server_config_defaults_transport_type_to_none() -> None:
    cfg = MCPServerConfig(url="https://example.com/mcp")

    assert cfg.type is None
