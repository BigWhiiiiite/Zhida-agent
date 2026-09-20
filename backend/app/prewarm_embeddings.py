"""Explicitly download the local, public embedding model before serving users.

Run from backend/: python -m app.prewarm_embeddings
No resume or other private user content is sent during this setup step.
"""

from .job_rag import MODEL_CACHE, MODEL_NAME, _embed


def main() -> None:
    from fastembed import TextEmbedding

    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(
        model_name=MODEL_NAME,
        cache_dir=str(MODEL_CACHE),
        local_files_only=False,
    )
    vector = next(model.embed(["公开模型初始化测试"], batch_size=1))
    print(f"已缓存 {MODEL_NAME}：{len(vector)} 维，位置 {MODEL_CACHE}")
    # Ordinary requests use local_files_only=True; verify that path as well.
    print(f"离线读取验证：{len(_embed(['离线检索测试'], MODEL_NAME)[0])} 维")


if __name__ == "__main__":
    main()
