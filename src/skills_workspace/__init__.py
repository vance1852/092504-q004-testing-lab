"""技能赛训协作基础服务与软件测试实验平台的服务端包。"""

from .experiments import ExperimentService
from .service import DomainService

__all__ = ["DomainService", "ExperimentService"]
