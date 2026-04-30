import pytest

from gateway.platforms.wecom.interceptors import InterceptorChain, MCPRequest


def test_interceptor_chain_runs_in_order():
    calls = []

    def interceptor_1(req):
        calls.append("1")
        return req

    def interceptor_2(req):
        calls.append("2")
        return req

    chain = InterceptorChain([interceptor_1, interceptor_2])
    result = chain.execute(MCPRequest(tool="test", params={}))

    assert calls == ["1", "2"]
    assert result.tool == "test"


def test_biz_error_interceptor_clears_cache_on_850001():
    from gateway.platforms.wecom.interceptors import BizErrorInterceptor

    cache_cleared = []
    interceptor = BizErrorInterceptor(lambda: cache_cleared.append("cleared"))

    req = MCPRequest(tool="doc.create", params={})
    req.response_error = {"errcode": 850001, "errmsg": "category not found"}

    result = interceptor.intercept(req)
    assert "cleared" in cache_cleared
    assert result.retry is True


def test_biz_error_interceptor_ignores_other_codes():
    from gateway.platforms.wecom.interceptors import BizErrorInterceptor

    cache_cleared = []
    interceptor = BizErrorInterceptor(lambda: cache_cleared.append("cleared"))

    req = MCPRequest(tool="doc.create", params={})
    req.response_error = {"errcode": 0, "errmsg": "ok"}

    result = interceptor.intercept(req)
    assert "cleared" not in cache_cleared
    assert result.retry is False
