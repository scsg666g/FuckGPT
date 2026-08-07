"""短信接码 provider 抽象。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import time

from core.network_retry import is_retryable_network_error


@dataclass(frozen=True)
class SmsActivation:
    activation_id: str
    phone_number: str
    provider: str = ""
    service: str = ""
    country: str = ""
    raw: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SmsStatus:
    status: str
    code: str = ""
    last_code: str = ""
    raw: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    @abstractmethod
    def get_number(self, service: str = "", country: str = "", **options) -> SmsActivation:
        """购买并返回一个手机号激活单。"""

    @abstractmethod
    def get_status(self, activation_id: str) -> SmsStatus:
        """查询激活单短信状态。"""

    @abstractmethod
    def set_status(self, activation_id: str, status: int | str) -> str:
        """修改激活单状态并返回平台原始状态文本。"""

    def cancel(self, activation_id: str) -> str:
        return self.set_status(activation_id, 8)

    def mark_sms_sent(self, activation_id: str) -> str:
        return self.set_status(activation_id, 1)

    def request_retry(self, activation_id: str) -> str:
        return self.set_status(activation_id, 3)

    def finish(self, activation_id: str) -> str:
        return self.set_status(activation_id, 6)

    def wait_for_code(
        self,
        activation_id: str,
        *,
        timeout: int | float = 120,
        poll_interval: int | float = 3,
    ) -> str:
        deadline = time.monotonic() + max(float(timeout or 0), 0.1)
        interval = max(float(poll_interval or 0), 0)
        last_status = ""
        network_failures = 0
        while time.monotonic() < deadline:
            try:
                status = self.get_status(activation_id)
                network_failures = 0
            except Exception as exc:
                if not is_retryable_network_error(exc):
                    raise
                network_failures += 1
                last_status = f"瞬时网络/代理异常（连续 {network_failures} 次）: {exc}"
                remaining = max(deadline - time.monotonic(), 0)
                if remaining <= 0:
                    break
                time.sleep(min(max(interval, 1.0), remaining))
                continue
            last_status = status.raw or status.status
            if status.code:
                return status.code
            if status.status == "cancelled":
                raise RuntimeError(f"短信激活已取消: {activation_id}")
            if interval > 0:
                time.sleep(min(interval, max(deadline - time.monotonic(), 0)))
        raise TimeoutError(f"等待短信验证码超时 ({timeout}s)，最后状态: {last_status or 'none'}")
