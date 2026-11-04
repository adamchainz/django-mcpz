from collections.abc import Iterable, Mapping, Sequence

class QueryNotAllowed(ValueError): ...

def check_query(
    sql: str,
    *,
    dialect: str,
    tables: Mapping[str, Sequence[str]],
    functions: Iterable[str],
) -> str: ...
