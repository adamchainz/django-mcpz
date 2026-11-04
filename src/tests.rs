//! Property tests, fuzzing the checker with generated SQL.

use std::collections::{HashMap, HashSet};

use proptest::prelude::*;

use crate::{Check, check};

const DIALECTS: [&str; 2] = ["postgresql", "sqlite"];

/// The readable tables: everything else generated, a table, a column, or a
/// function, has "secret" or another forbidden name in it, so any accepted
/// query that mentions one is a hole.
fn tables() -> HashMap<String, Vec<String>> {
    HashMap::from([
        (
            "widget".to_string(),
            vec!["id".to_string(), "name".to_string(), "price".to_string()],
        ),
        (
            "part".to_string(),
            vec![
                "id".to_string(),
                "name".to_string(),
                "widget_id".to_string(),
            ],
        ),
    ])
}

fn functions() -> HashSet<String> {
    ["count", "sum", "round", "lower", "coalesce"]
        .map(String::from)
        .into()
}

const FORBIDDEN: [&str; 4] = ["secret", "pg_sleep", "set_config", "pg_read_file"];

fn run(sql: &str, dialect: &str) -> Check<String> {
    check(sql, dialect, tables(), functions())
}

/// An accepted query must check again to itself, so what runs is what was
/// checked.
fn assert_stable(sql: &str, dialect: &str) -> Result<(), TestCaseError> {
    if let Ok(checked) = run(sql, dialect) {
        prop_assert_eq!(
            run(&checked, dialect),
            Ok(checked.clone()),
            "from {:?}",
            sql
        );
    }
    Ok(())
}

/// An accepted query must also mention nothing forbidden, for queries whose
/// own names, such as aliases, can't be forbidden ones.
fn assert_sound(sql: &str, dialect: &str) -> Result<(), TestCaseError> {
    assert_stable(sql, dialect)?;
    if let Ok(checked) = run(sql, dialect) {
        for name in FORBIDDEN {
            prop_assert!(
                !checked.to_lowercase().contains(name),
                "accepted {name}: {sql:?} as {checked:?}"
            );
        }
    }
    Ok(())
}

// Generated SQL, from a small grammar over readable and forbidden names.

// Readable names mostly, with forbidden ones rarer, so plenty of generated
// queries are accepted, testing the properties on them.

fn table_name() -> impl Strategy<Value = &'static str> {
    prop_oneof![
        8 => prop::sample::select(vec!["widget", "part"]),
        1 => prop::sample::select(vec!["secret_table", "pg_class"]),
    ]
}

fn alias() -> impl Strategy<Value = &'static str> {
    prop::sample::select(vec!["widget", "part", "w"])
}

fn column_name() -> impl Strategy<Value = &'static str> {
    prop_oneof![
        8 => prop::sample::select(vec!["id", "name", "price", "widget_id"]),
        1 => Just("secret"),
    ]
}

fn function_name() -> impl Strategy<Value = &'static str> {
    prop_oneof![
        8 => prop::sample::select(vec!["count", "sum", "round", "lower", "coalesce"]),
        1 => prop::sample::select(vec!["pg_sleep", "set_config", "pg_read_file"]),
    ]
}

fn literal() -> impl Strategy<Value = String> {
    prop_oneof![
        (0u32..1000).prop_map(|n| n.to_string()),
        // Too short to spell a forbidden name.
        "[a-z ]{0,5}".prop_map(|s| format!("'{s}'")),
        Just("NULL".to_string()),
        Just("true".to_string()),
    ]
}

fn column() -> impl Strategy<Value = String> {
    prop_oneof![
        6 => column_name().prop_map(String::from),
        3 => (alias(), column_name()).prop_map(|(a, c)| format!("{a}.{c}")),
        // Whole-row references.
        1 => alias().prop_map(String::from),
    ]
}

fn expr() -> impl Strategy<Value = String> {
    let leaf = prop_oneof![column(), literal()];
    leaf.prop_recursive(3, 16, 3, |inner| {
        prop_oneof![
            (
                inner.clone(),
                prop::sample::select(vec!["+", "-", "*", "=", "<", "AND", "OR", "||", "->>"]),
                inner.clone()
            )
                .prop_map(|(a, op, b)| format!("({a} {op} {b})")),
            inner.clone().prop_map(|e| format!("NOT {e}")),
            (function_name(), prop::collection::vec(inner.clone(), 0..3))
                .prop_map(|(f, args)| format!("{f}({})", args.join(", "))),
            Just("count(*)".to_string()),
            (inner.clone(), inner.clone(), inner.clone())
                .prop_map(|(a, b, c)| format!("CASE WHEN {a} THEN {b} ELSE {c} END")),
            (table_name(), column(), inner.clone())
                .prop_map(|(t, c, w)| format!("(SELECT {c} FROM {t} WHERE {w})")),
            // Correlated, ordered by a bare name, which may be the output
            // column, from the outer query, or an input column.
            (table_name(), column(), column_name())
                .prop_map(|(t, c, o)| format!("(SELECT {c} FROM {t} ORDER BY {o} LIMIT 1)")),
            (table_name(), inner.clone())
                .prop_map(|(t, w)| format!("EXISTS (SELECT 1 FROM {t} WHERE {w})")),
            (
                inner.clone(),
                prop_oneof![
                    4 => prop::sample::select(vec!["integer", "text"]),
                    1 => Just("regclass"),
                ]
            )
                .prop_map(|(e, t)| format!("CAST({e} AS {t})")),
        ]
    })
}

fn from_item() -> impl Strategy<Value = String> {
    prop_oneof![
        table_name().prop_map(String::from),
        (table_name(), alias()).prop_map(|(t, a)| format!("{t} AS {a}")),
        (table_name(), column(), alias())
            .prop_map(|(t, c, a)| format!("(SELECT {c} AS id FROM {t}) AS {a}")),
        // Ordering inside a subquery, by a bare name, whose resolution
        // differs between the databases.
        (table_name(), column(), column_name(), alias()).prop_map(|(t, c, o, a)| format!(
            "(SELECT {c}, {t}.id FROM {t} ORDER BY {o} LIMIT 1) AS {a}"
        )),
    ]
}

fn from() -> impl Strategy<Value = String> {
    prop_oneof![
        10 => from_item(),
        1 => (from_item(), from_item()).prop_map(|(a, b)| format!("{a}, {b}")),
        1 => (
            from_item(),
            prop::sample::select(vec!["JOIN", "LEFT JOIN", "CROSS JOIN"]),
            from_item(),
            expr()
        )
            .prop_map(|(a, join, b, on)| if join == "CROSS JOIN" {
                format!("{a} {join} {b}")
            } else {
                format!("{a} {join} {b} ON {on}")
            }),
        1 => (from_item(), from_item(), column_name())
            .prop_map(|(a, b, c)| format!("{a} JOIN {b} USING ({c})")),
        // Joins beside other FROM items, and inside each other, whose ON
        // and USING see only their own tables.
        1 => (from_item(), from_item(), from_item(), expr())
            .prop_map(|(a, b, c, on)| format!("{a}, {b} JOIN {c} ON {on}")),
        1 => (from_item(), from_item(), from_item(), expr())
            .prop_map(|(a, b, c, on)| format!("{a} JOIN {b} ON {on} CROSS JOIN {c}")),
        1 => (from_item(), from_item(), from_item(), column_name())
            .prop_map(|(a, b, c, col)| format!("{a}, {b} JOIN {c} USING ({col})")),
        1 => (from_item(), from_item(), from_item(), column_name())
            .prop_map(|(a, b, c, col)| format!("{a} JOIN ({b} JOIN {c} USING ({col})) ON true")),
    ]
}

fn select() -> impl Strategy<Value = String> {
    (
        prop::collection::vec(
            (
                expr(),
                prop::option::of(prop::sample::select(vec!["a", "b", "id"])),
            ),
            1..4,
        ),
        from(),
        prop::option::of(expr()),
        prop::option::of(expr()),
        prop::option::of(prop_oneof![
            column(),
            Just("1".to_string()),
            prop::sample::select(vec!["a", "b"]).prop_map(String::from),
        ]),
    )
        .prop_map(|(items, from, filter, group, order)| {
            let items: Vec<String> = items
                .into_iter()
                .map(|(e, alias)| match alias {
                    Some(alias) => format!("{e} AS {alias}"),
                    None => e,
                })
                .collect();
            let mut sql = format!("SELECT {} FROM {from}", items.join(", "));
            if let Some(filter) = filter {
                sql += &format!(" WHERE {filter}");
            }
            if let Some(group) = group {
                sql += &format!(" GROUP BY {group}");
            }
            if let Some(order) = order {
                sql += &format!(" ORDER BY {order}");
            }
            sql
        })
}

fn query() -> impl Strategy<Value = String> {
    prop_oneof![
        3 => select(),
        1 => (alias(), select(), select()).prop_map(|(name, cte, body)| format!(
            "WITH {name} AS ({cte}) {body}"
        )),
        1 => (select(), select()).prop_map(|(a, b)| format!("{a} UNION ALL {b}")),
    ]
}

/// SQL-ish tokens, for queries that mostly don't parse, but get past the
/// lexer in unusual ways.
fn token_soup() -> impl Strategy<Value = String> {
    let token = prop::sample::select(vec![
        "SELECT", "FROM", "WHERE", "WITH", "AS", "JOIN", "ON", "UNION", "(", ")", ",", ";", "*",
        ".", "'", "\"", "--", "/*", "*/", "$$", "E'", "U&", "::", "widget", "part", "secret", "id",
        "name", "pg_sleep", "count", "1", "'x'", "NULL", "=", "||", "->>",
    ]);
    prop::collection::vec(token, 0..24).prop_map(|tokens| tokens.join(" "))
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2000))]

    #[test]
    fn arbitrary_text_doesnt_panic(sql in any::<String>()) {
        for dialect in DIALECTS {
            let _ = run(&sql, dialect);
        }
    }

    #[test]
    fn token_soup_is_stable(sql in token_soup()) {
        for dialect in DIALECTS {
            assert_stable(&sql, dialect)?;
        }
    }

    #[test]
    fn generated_queries_are_sound(sql in query()) {
        for dialect in DIALECTS {
            assert_sound(&sql, dialect)?;
        }
    }
}

/// A SQLite database with the readable tables, plus unreadable columns named
/// like the other table's readable ones, so a query whose names the checker
/// resolves differently from SQLite reads one.
const ORACLE_SCHEMA: &str = "
    CREATE TABLE widget (id, name, price, widget_id, secret);
    CREATE TABLE part (id, name, widget_id, price, secret);
    CREATE TABLE secret_table (id, name);
";

/// The unreadable columns and tables, and functions, an accepted query
/// reads or calls, as SQLite compiles it, reported by its authorizer.
fn oracle_violations(sql: &str) -> Vec<String> {
    use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
    use std::sync::{Arc, Mutex};

    let mut connection = rusqlite::Connection::open_in_memory().unwrap();
    connection.execute_batch(ORACLE_SCHEMA).unwrap();
    let readable = tables();
    let allowed_functions = functions();
    let violations = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::clone(&violations);
    connection
        .authorizer(Some(move |context: AuthContext<'_>| {
            let violation = match context.action {
                AuthAction::Read {
                    table_name,
                    column_name,
                } => {
                    let ok = match readable.get(table_name) {
                        Some(columns) => {
                            // No column, as for count(*), or SQLite's own
                            // ROWID, read for its own purposes.
                            column_name.is_empty()
                                || column_name.eq_ignore_ascii_case("rowid")
                                || columns.iter().any(|c| c == column_name)
                        }
                        None => false,
                    };
                    (!ok).then(|| format!("read {table_name}.{column_name}"))
                }
                // SQLite implements the -> and ->> operators as functions.
                AuthAction::Function { function_name } => (!allowed_functions
                    .contains(&function_name.to_lowercase())
                    && !matches!(function_name, "->" | "->>"))
                .then(|| format!("called {function_name}()")),
                AuthAction::Select | AuthAction::Recursive => None,
                action => Some(format!("{action:?}")),
            };
            if let Some(violation) = violation {
                seen.lock().unwrap().push(violation);
            }
            Authorization::Allow
        }))
        .unwrap();
    // Compiling runs the authorizer, and SQLite may reject the query, such as
    // for an ambiguous column, which is fine.
    let _ = connection.prepare(sql);
    violations.lock().unwrap().clone()
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2000))]

    #[test]
    fn generated_queries_read_only_readable_columns_in_sqlite(sql in query()) {
        // SQLite runs a parenthesized join as a subquery reading every
        // column of its tables, which the query can't see unless it names
        // them, so skip those queries here, to check others.
        if let Ok(checked) = run(&sql, "sqlite")
            && !checked.contains("JOIN (")
        {
            let violations = oracle_violations(&checked);
            prop_assert!(
                violations.is_empty(),
                "{violations:?}, from {sql:?} as {checked:?}"
            );
        }
    }
}

/// The oracle catches a hole the checker once had: SQLite resolving a bare
/// ORDER BY name as an input column, widget's unreadable widget_id, rather
/// than the output column from part.
#[test]
fn oracle_catches_wrong_resolution() {
    let sql = "SELECT part.id, (SELECT d.id FROM (SELECT part.widget_id, widget.id FROM widget ORDER BY widget_id LIMIT 1) AS d) FROM part";
    assert_eq!(oracle_violations(sql), vec!["read widget.widget_id"]);
    assert!(run(sql, "sqlite").is_err());
}

/// The grammar generates queries the checker accepts, not only ones it
/// rejects, so the properties above test something.
#[test]
fn generated_queries_are_sometimes_accepted() {
    use proptest::strategy::ValueTree;
    use proptest::test_runner::TestRunner;

    let mut runner = TestRunner::deterministic();
    let strategy = query();
    let accepted = (0..2000)
        .filter(|_| {
            let sql = strategy.new_tree(&mut runner).unwrap().current();
            run(&sql, "postgresql").is_ok()
        })
        .count();
    assert!(accepted > 50, "only {accepted} of 2000 accepted");
}

/// Deeply nested SQL, past sqlparser's recursion limit, is rejected rather
/// than overflowing the stack, even in a debug build.
#[test]
fn deep_nesting_is_rejected() {
    let sql = format!("SELECT {}1{}", "(".repeat(10_000), ")".repeat(10_000));
    assert_eq!(
        run(&sql, "postgresql"),
        Err("sql parser error: recursion limit exceeded".to_string())
    );
    let mut sql = "1".to_string();
    for _ in 0..10_000 {
        sql = format!("(SELECT {sql})");
    }
    assert!(run(&format!("SELECT {sql}"), "sqlite").is_err());
}

// Regressions, with tables whose readable columns share names with other
// tables' unreadable ones.

fn run_on(sql: &str, dialect: &str, tables: &[(&str, &[&str])]) -> Check<String> {
    let tables = tables
        .iter()
        .map(|(table, columns)| {
            (
                table.to_string(),
                columns.iter().map(|c| c.to_string()).collect(),
            )
        })
        .collect();
    check(sql, dialect, tables, functions())
}

/// An ON condition sees only the tables of its own join, so an unqualified
/// name in it can't resolve to a readable column of another FROM item,
/// when the database would read an unreadable one of the join's.
#[test]
fn on_sees_only_its_join() {
    // a has an unreadable x, and c a readable one.
    let tables: &[(&str, &[&str])] = &[("a", &["id"]), ("b", &["id", "a_id"]), ("c", &["id", "x"])];
    for dialect in DIALECTS {
        for sql in [
            "SELECT a.id FROM a JOIN b ON a.id = b.a_id AND x = 'k' CROSS JOIN c",
            "SELECT a.id FROM c, a JOIN b ON x = 'k'",
            "SELECT a.id FROM a JOIN b ON (SELECT 1 WHERE x = 'k') = 1, c",
        ] {
            assert_eq!(
                run_on(sql, dialect, tables),
                Err("No table in FROM has a column x that can be queried.".to_string()),
                "{sql}"
            );
        }
        assert!(run_on("SELECT a.id FROM a JOIN c ON x = 'k'", dialect, tables).is_ok());
        // Outer queries are still in scope.
        assert!(
            run_on(
                "SELECT c.id FROM c WHERE EXISTS (SELECT 1 FROM a JOIN b ON b.a_id = c.id)",
                dialect,
                tables
            )
            .is_ok()
        );
    }
}

/// USING checks the join's own left side, not other FROM items.
#[test]
fn using_checks_its_own_left_side() {
    // b has an unreadable x, and a and c readable ones.
    let tables: &[(&str, &[&str])] = &[("a", &["id", "x"]), ("b", &["id"]), ("c", &["id", "x"])];
    for dialect in DIALECTS {
        for sql in [
            "SELECT c.id FROM a, b JOIN c USING (x)",
            "SELECT c.id FROM a JOIN (b JOIN c USING (x)) ON true",
        ] {
            assert_eq!(
                run_on(sql, dialect, tables),
                Err("USING (x) needs x readable on both sides of the join.".to_string()),
                "{sql}"
            );
        }
        assert!(run_on("SELECT c.id FROM b, a JOIN c USING (x)", dialect, tables).is_ok());
    }
}

/// SQLite resolves a bare ORDER BY name as an output column only for an
/// alias given with AS, and otherwise as an input column.
#[test]
fn sqlite_order_by_only_explicit_aliases() {
    // s has an unreadable name, and p a readable one.
    let tables: &[(&str, &[&str])] = &[("p", &["id", "name"]), ("s", &["id"])];
    let sql = "SELECT p.id, (SELECT d.id FROM (SELECT p.name, s.id FROM s ORDER BY name LIMIT 1) AS d) FROM p";
    assert_eq!(
        run_on(sql, "sqlite", tables),
        Err("s has no column name that can be queried.".to_string())
    );
    // PostgreSQL sorts by the output column, p.name.
    assert!(run_on(sql, "postgresql", tables).is_ok());
    assert!(
        run_on(
            "SELECT p.id, (SELECT d.id FROM (SELECT p.name AS name, s.id FROM s ORDER BY name LIMIT 1) AS d) FROM p",
            "sqlite",
            tables
        )
        .is_ok()
    );
}
