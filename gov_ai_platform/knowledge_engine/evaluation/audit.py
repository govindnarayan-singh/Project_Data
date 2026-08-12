from __future__ import annotations

from knowledge_engine.retrieval.repository import PostgresRetrievalRepository


def database_counts() -> dict[str, int]:
    return PostgresRetrievalRepository().counts()


def main() -> None:
    for key, value in database_counts().items():
        print(f"{key:12s}: {value}")


if __name__ == "__main__":
    main()
