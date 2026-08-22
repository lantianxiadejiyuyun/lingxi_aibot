"""联网搜索测试：各提供方解析（mock）/ fetch_page 正文提取 / 工具路径 / 设置页 tab。"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import test_common

BASE = "http://127.0.0.1:5000"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


# ============ HTTP：设置页 ============
s = requests.Session()
token = test_common.login(s, BASE)
check("登录成功", bool(token))
r = s.get(BASE + "/settings")
check("设置页含联网搜索 tab", 'data-tab="search"' in r.text and "联网搜索" in r.text)

# ============ 进程内（mock 提供方）============
from run import app
from app.services import web_search_service
from app.ai import registry


class FakeResp:
    def __init__(self, text="", json_data=None, status_code=200, url=""):
        self.text = text
        self._json = json_data
        self.status_code = status_code
        self.url = url

    def json(self):
        return self._json

    def close(self):
        pass


DDG_HTML = """
<html><body>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage1&amp;rut=x">示例标题一</a>
<a class="result__snippet">这是第一条结果的摘要内容。</a>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fp2&amp;rut=y">示例标题二</a>
<a class="result__snippet">第二条摘要。</a>
</body></html>
"""

BING_HTML = """
<html><body>
<li class="b_algo"><h2><a href="https://bing.example.com/1">必应标题一</a></h2><div class="b_caption"><p>必应摘要一</p></div></li>
<li class="b_algo"><h2><a href="https://bing.example.com/2">必应标题二</a></h2><div class="b_caption"><p>必应摘要二</p></div></li>
</body></html>
"""

PAGE_HTML = ("<html><head><style>body{color:red}</style></head><body>"
             "<script>alert(1)</script><h1>标题</h1><p>正文内容测试。</p></body></html>")

with app.app_context():
    # Bing 解析（默认提供方）
    web_search_service._cfg = lambda: {"provider": "bing", "searxng_base_url": "",
                                       "serper_api_key": "", "tavily_api_key": ""}
    web_search_service.requests.get = lambda *a, **k: FakeResp(text=BING_HTML)
    res = web_search_service.search("测试", limit=2)
    check("Bing 解析标题/URL", res and res[0]["title"] == "必应标题一"
          and res[0]["url"] == "https://bing.example.com/1", str(res)[:120])
    check("Bing 摘要", res and "必应摘要一" in res[0]["snippet"], str(res)[:120])

    # DuckDuckGo 解析
    web_search_service._cfg = lambda: {"provider": "duckduckgo", "searxng_base_url": "",
                                       "serper_api_key": "", "tavily_api_key": ""}
    web_search_service.requests.get = lambda *a, **k: FakeResp(text=DDG_HTML)
    res = web_search_service.search("测试", limit=2)
    check("DuckDuckGo 解析标题/URL", res and res[0]["title"] == "示例标题一"
          and res[0]["url"] == "https://example.com/page1", str(res)[:120])
    check("DuckDuckGo 摘要", res and "第一条结果" in res[0]["snippet"], str(res)[:120])

    # Serper 解析
    web_search_service._cfg = lambda: {"provider": "serper", "searxng_base_url": "",
                                       "serper_api_key": "sk", "tavily_api_key": ""}
    web_search_service.requests.post = lambda *a, **k: FakeResp(json_data={
        "organic": [{"title": "谷歌标题", "link": "https://g.example.com/1",
                     "snippet": "谷歌摘要"}]})
    res = web_search_service.search("测试", limit=3)
    check("Serper 解析", res and res[0]["url"] == "https://g.example.com/1"
          and res[0]["title"] == "谷歌标题", str(res)[:120])

    # Tavily 解析
    web_search_service._cfg = lambda: {"provider": "tavily", "searxng_base_url": "",
                                       "serper_api_key": "", "tavily_api_key": "tv"}
    web_search_service.requests.post = lambda *a, **k: FakeResp(json_data={
        "results": [{"title": "塔维标题", "url": "https://t.example.com/1",
                     "content": "塔维内容"}]})
    res = web_search_service.search("测试", limit=3)
    check("Tavily 解析", res and res[0]["url"] == "https://t.example.com/1", str(res)[:120])

    # SearXNG 解析
    web_search_service._cfg = lambda: {"provider": "searxng", "searxng_base_url": "https://sx.example.com",
                                       "serper_api_key": "", "tavily_api_key": ""}
    web_search_service.requests.get = lambda *a, **k: FakeResp(json_data={
        "results": [{"title": "自托管标题", "url": "https://sx.example.com/1", "content": "自托管内容"}]})
    res = web_search_service.search("测试", limit=3)
    check("SearXNG 解析", res and res[0]["url"] == "https://sx.example.com/1", str(res)[:120])

    # 未知提供方
    web_search_service._cfg = lambda: {"provider": "nope", "searxng_base_url": "",
                                       "serper_api_key": "", "tavily_api_key": ""}
    try:
        web_search_service.search("测试")
        check("未知提供方报错", False)
    except web_search_service.SearchError:
        check("未知提供方报错", True)

    # fetch_page 正文提取
    web_search_service.requests.get = lambda *a, **k: FakeResp(text=PAGE_HTML)
    text = web_search_service.fetch_page("https://example.com/x")
    check("fetch_page 去标签", "正文内容测试" in text and "script" not in text.lower()
          and "<h1>" not in text, text[:80])

    # 工具路径（DuckDuckGo）
    web_search_service._cfg = lambda: {"provider": "duckduckgo", "searxng_base_url": "",
                                       "serper_api_key": "", "tavily_api_key": ""}
    web_search_service.requests.get = lambda *a, **k: FakeResp(text=DDG_HTML)
    tool_res = registry.execute_tool("web_search", {"query": "测试", "limit": 2})
    check("web_search 工具", isinstance(tool_res, str) and "example.com" in tool_res
          and "示例标题一" in tool_res, str(tool_res)[:100])
    web_search_service.requests.get = lambda *a, **k: FakeResp(text=PAGE_HTML)
    page_res = registry.execute_tool("fetch_page", {"url": "https://example.com/x"})
    check("fetch_page 工具", isinstance(page_res, str) and "正文内容测试" in page_res,
          str(page_res)[:80])
    # 非法 url
    bad = registry.execute_tool("fetch_page", {"url": "ftp://x"})
    check("非法 url 报错", isinstance(bad, str) and "http" in bad, str(bad)[:80])

    # --- SSRF 防护 ---
    for bad in ("http://127.0.0.1:5000/login", "http://192.168.1.1/",
                "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/x",
                "http://localhost/", "http://[::1]/"):
        try:
            web_search_service.fetch_page(bad)
            check(f"SSRF 拦截 {bad}", False)
        except web_search_service.SearchError:
            check(f"SSRF 拦截 {bad}", True)
    # 公网地址放行（mock 返回页面）
    web_search_service.requests.get = lambda *a, **k: FakeResp(text=PAGE_HTML)
    ok_url = web_search_service.fetch_page("https://8.8.8.8/anything")
    check("公网地址放行", "正文内容测试" in ok_url, str(ok_url)[:50])
    # 重定向 hook：逐跳校验
    from app.utils.urlsafety import requests_public_hook
    fr = FakeResp(text="x", url="http://127.0.0.1:5000/")
    try:
        requests_public_hook(fr)
        check("重定向 hook 拦截内网", False)
    except ValueError:
        check("重定向 hook 拦截内网", True)
    fr2 = FakeResp(text="x", url="https://8.8.8.8/")
    requests_public_hook(fr2)
    check("重定向 hook 放行公网", True)

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
