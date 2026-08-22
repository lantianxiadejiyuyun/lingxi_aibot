"""RAG 语义检索测试：未配置降级 / monkeypatch 向量验证召回与排序 / 索引挂钩。"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

BASE = "http://127.0.0.1:5000"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


# ============ 进程内验证（无需真实嵌入服务）============
from run import app
from app.models.embedding import Embedding
from app.services import note_service, rag_service
from app.ai import registry

with app.app_context():
    # 预清理上次残留
    for nn in note_service.list_notes():
        if nn.title.startswith("RAG测试"):
            note_service.soft_delete_note(nn)
    # 1. 未配置时降级
    res = registry.execute_tool("semantic_search", {"query": "测试"})
    check("未配置降级提示", isinstance(res, str) and ("降级" in res or "未配置" in res or "没有找到" in res),
          str(res)[:80])

    # 2. monkeypatch 嵌入：验证索引与检索
    rag_service.is_configured = lambda: True
    rag_service.embed = lambda text: ([1.0, 0.0] if "猫" in str(text) else [0.0, 1.0])

    n_cat = note_service.create_note("RAG测试-猫", content="我家的猫喜欢在窗台睡觉")
    n_dog = note_service.create_note("RAG测试-狗", content="邻居家的狗每天早上很吵")
    emb_cat = Embedding.query.filter_by(source_type="note", source_id=n_cat.id).first()
    check("create_note 挂钩建索引", emb_cat is not None)

    results = rag_service.semantic_search("猫", k=2)
    check("语义检索召回", any(r["source_id"] == n_cat.id for r in results), str(results)[:100])
    check("top1 为最相关", results and results[0]["source_id"] == n_cat.id, str(results)[:100])
    check("返回文本片段", results and "猫" in results[0]["text"], str(results)[:100])

    tool_res = registry.execute_tool("semantic_search", {"query": "猫", "k": 2})
    check("工具语义检索", isinstance(tool_res, str) and "source_type" in tool_res
          and "RAG测试-猫" in tool_res, str(tool_res)[:100])

    # 3. 更新挂钩
    note_service.update_note(n_cat, content="我家的狗也很可爱（改）")
    emb_cat2 = Embedding.query.filter_by(source_type="note", source_id=n_cat.id).first()
    check("update_note 重新索引", emb_cat2 is not None)

    # 4. sources 过滤
    only_page = rag_service.semantic_search("猫", sources=["webpage"], k=2)
    check("sources 过滤", all(r["source_type"] == "webpage" for r in only_page))

    # 5. 删除挂钩清索引
    note_service.soft_delete_note(n_cat)
    note_service.soft_delete_note(n_dog)
    gone = Embedding.query.filter_by(source_type="note", source_id=n_cat.id).first()
    check("soft_delete 清索引", gone is None)

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
