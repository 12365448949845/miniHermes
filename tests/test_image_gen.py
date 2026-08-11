import requests

from tools import image_gen


def test_image_generation_tls_error_is_actionable(monkeypatch):
    monkeypatch.setattr(image_gen, "_load_image_config", lambda: {
        "base_url": "",
        "timeout_seconds": 10,
        "auto_open": False,
    })
    monkeypatch.setattr(
        image_gen.requests,
        "get",
        lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(requests.exceptions.SSLError("handshake EOF")),
    )

    result = image_gen.generate_image("portrait")

    assert result.startswith("Error: image generation TLS connection failed.")
    assert "disable TLS verification" in result
