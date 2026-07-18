import base64


def raw_bytes(data_str: str | None) -> bytes | None:
    if data_str is not None:
        return base64.b64decode(data_str)
    return None
