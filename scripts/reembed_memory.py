#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆库重嵌入：换 embedding 模型后，把 embeddings.json 里全部文本
用新模型重新向量化（保持 text/time 不变），适配新维度。

用法：python3 scripts/reembed_memory.py [--model qwen3-embedding:4b] [--dry]
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(BASE, "memory", "embeddings.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get(
        "OLLAMA_EMBED_MODEL", "qwen3-embedding:4b"))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, BASE)
    from models.embedding import OllamaEmbedding
    emb = OllamaEmbedding(model=args.model)
    try:
        with open(MEM, encoding="utf-8") as f:
            items = json.load(f)
    except Exception as e:
        print("读取记忆失败:", e)
        return 1
    print(f"记忆 {len(items)} 条，目标模型 {args.model}")
    if args.dry:
        return 0
    ok, fail, dim = 0, 0, None
    for i, it in enumerate(items):
        text = it.get("text") or ""
        if not text:
            continue
        try:
            vec = emb.embed(text)
            it["vector"] = vec
            dim = len(vec)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  [{i}] 失败: {type(e).__name__}: {str(e)[:80]}")
        if (i + 1) % 10 == 0:
            print(f"  进度 {i+1}/{len(items)}（ok={ok} fail={fail}）")
            time.sleep(0.05)
    if ok:
        backup = MEM + time.strftime(".bak-%Y%m%d-%H%M%S")
        with open(MEM, encoding="utf-8") as f:
            old = f.read()
        with open(backup, "w", encoding="utf-8") as f:
            f.write(old)
        tmp = MEM + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MEM)
        print(f"完成：重嵌入 {ok} 条（维度 {dim}），备份 {os.path.basename(backup)}")
    else:
        print("全部失败，未写入")
    return 0 if not fail else 2


if __name__ == "__main__":
    raise SystemExit(main())
