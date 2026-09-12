"""智能体平台包：SDK / 注册中心 / 内置智能体。"""

from .manifest import AgentManifest
from .registry import registry

__all__ = ["AgentManifest", "registry"]
