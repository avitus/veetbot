"""Read-only effective PostgreSQL schema identity for semantic admission."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_core.adapters.persistence.revision import EXPECTED_REVISION


async def capture_recall_schema(engine: AsyncEngine) -> dict[str, Any]:
    """Capture definitions, without memory rows, OIDs or changing sequence counters."""
    result: dict[str, Any] = {"version": "recall-schema-inputs@1"}
    async with engine.begin() as connection:
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
        revisions = list(
            (
                await connection.execute(
                    text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
                )
            ).scalars()
        )
        if revisions != [EXPECTED_REVISION]:
            raise ValueError("Recall schema revision does not match the evaluated build")
        result["revision"] = revisions[0]
        result["postgres_version"] = await connection.scalar(text("SHOW server_version"))
        for name, query in _SCHEMA_QUERIES.items():
            rows = await connection.execute(text(query))
            result[name] = [dict(row) for row in rows.mappings()]
    if not any(row["name"] == "vector" for row in result["extensions"]):
        raise ValueError("Recall schema requires the actual vector extension")
    return result


_SCHEMA_QUERIES = {
    "extensions": """
        SELECT extname AS name, extversion AS version FROM pg_extension ORDER BY extname
    """,
    "relations": """
        SELECT c.relname AS name, c.relkind::text AS kind, c.relpersistence::text AS persistence,
               c.relrowsecurity AS rls, c.relforcerowsecurity AS forced_rls,
               c.reloptions AS options, c.relreplident::text AS replica_identity,
               CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid, true) END AS view
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' ORDER BY c.relname
    """,
    "columns": """
        SELECT c.relname AS relation, a.attname AS name, a.attnum AS position,
               format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS required,
               a.attidentity::text AS identity, a.attgenerated::text AS generated,
               pg_get_expr(d.adbin, d.adrelid, true) AS default,
               col.collname AS collation
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        LEFT JOIN pg_collation col ON col.oid = a.attcollation
        WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
    """,
    "constraints": """
        SELECT cl.relname AS relation, c.conname AS name, c.convalidated AS validated,
               pg_get_constraintdef(c.oid, true) AS definition
        FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
        LEFT JOIN pg_class cl ON cl.oid = c.conrelid
        WHERE n.nspname = 'public' ORDER BY cl.relname, c.conname
    """,
    "indexes": """
        SELECT c.relname AS relation, i.relname AS name, x.indisvalid AS valid,
               x.indisready AS ready, pg_get_indexdef(i.oid) AS definition
        FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' ORDER BY c.relname, i.relname
    """,
    "policies": """
        SELECT tablename AS relation, policyname AS name, permissive, roles::text[], cmd,
               qual, with_check FROM pg_policies
        WHERE schemaname = 'public' ORDER BY tablename, policyname
    """,
    "triggers": """
        SELECT c.relname AS relation, t.tgname AS name, t.tgenabled::text AS enabled,
               pg_get_triggerdef(t.oid, true) AS definition
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND NOT t.tgisinternal ORDER BY c.relname, t.tgname
    """,
    "functions": """
        SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
        ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
    """,
}
