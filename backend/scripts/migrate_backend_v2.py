"""Run explicit V2 migrations, then grant only revision-read access to runtime."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

from sqlalchemy import create_engine, text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-role", default="zhida_runtime")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", args.runtime_role):
        parser.error("runtime role must be a plain PostgreSQL role identifier")
    url = os.getenv("ZHIDA_V2_DATABASE_URL", "")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if not url.startswith("postgresql+psycopg://"):
        parser.error("set ZHIDA_V2_DATABASE_URL to the dedicated migration database")
    backend = Path(__file__).resolve().parents[1]
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            exists = connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"),
                                        {"role": args.runtime_role}).scalar()
            if not exists:
                parser.error("runtime role is missing; initialize separate migration/runtime roles first")
        subprocess.run([sys.executable, "-m", "alembic", "-c", str(backend / "alembic-v2.ini"),
                        "upgrade", "head"], cwd=backend, check=True)
        quoted_role = engine.dialect.identifier_preparer.quote(args.runtime_role)
        with engine.begin() as connection:
            connection.execute(text("GRANT SELECT ON TABLE public.alembic_version TO " + quoted_role))
    finally:
        engine.dispose()
    print("V2 migrations complete; runtime role can read the migration revision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
