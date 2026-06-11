def test_mcp_s3_modal_function_registers():
    import mcp_s3_app as m

    assert hasattr(m, "s3_mcp_asgi")
