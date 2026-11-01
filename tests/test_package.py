"""Verify the installed distribution exposes its typed package."""

from importlib.metadata import distribution
from importlib.resources import files

import crow_trading


def test_installed_package() -> None:
    installed = distribution("crow-trading")
    assert installed.metadata["Name"] == "crow-trading"
    assert crow_trading.__file__ is not None
    assert files(crow_trading).joinpath("py.typed").is_file()
