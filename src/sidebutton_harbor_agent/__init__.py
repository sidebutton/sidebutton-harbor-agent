"""SideButton agent adapter for the Harbor harness.

Public import path (Harbor ``--agent``): ``sidebutton_harbor_agent:SidebuttonAgent``.
"""

from sidebutton_harbor_agent.agent import (
    ADAPTER_VERSION,
    DEFAULT_SIDEBUTTON_CLI_VERSION,
    InContainerInvocation,
    SidebuttonAgent,
)

__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_SIDEBUTTON_CLI_VERSION",
    "InContainerInvocation",
    "SidebuttonAgent",
]

__version__ = ADAPTER_VERSION
