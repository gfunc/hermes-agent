from typing import Any, Protocol, TypedDict


class CallContext(TypedDict):
    category: str
    method: str
    args: dict[str, Any]


class BeforeCallResult(TypedDict, total=False):
    timeout_ms: int
    args: dict[str, Any]


class CallInterceptor(Protocol):
    name: str

    def match(self, ctx: CallContext) -> bool:
        ...

    def before_call(self, ctx: CallContext) -> BeforeCallResult | None:
        ...

    def after_call(self, ctx: CallContext, result: Any) -> Any:
        ...
