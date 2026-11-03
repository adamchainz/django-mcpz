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
    ]
}

fn from() -> impl Strategy<Value = String> {
    prop_oneof![
        4 => from_item(),
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
