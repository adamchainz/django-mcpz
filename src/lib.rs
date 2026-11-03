//! Checking an LLM's SQL query before django-mcpz runs it: it must be one
//! SELECT, reading only the given tables and columns, and calling only the
//! given functions.
//!
//! The checker is conservative: it accepts the parts of SQL it understands,
//! and rejects everything else. Every AST struct is matched with all its
//! fields, so a new field in a sqlparser upgrade fails to compile until it's
//! reviewed.
//!
//! The query that runs is the checked AST, rendered back to SQL, rather than
//! the original text, so the database runs what was checked, even where its
//! lexer would read the text differently.

use std::collections::{HashMap, HashSet};
use std::ops::Range;

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sqlparser::ast::{
    BinaryOperator, CastKind, CeilFloorKind, Cte, DataType, DateTimeField, Distinct,
    DuplicateTreatment, ExactNumberInfo, Expr, Function, FunctionArg, FunctionArgExpr,
    FunctionArgumentClause, FunctionArgumentList, FunctionArguments, GroupByExpr, Ident, Join,
    JoinConstraint, JoinOperator, LimitClause, ObjectName, ObjectNamePart, Offset, OrderBy,
    OrderByExpr, OrderByKind, Query, Select, SelectFlavor, SelectItem, SetExpr, SetOperator,
    SetQuantifier, Statement, TableAlias, TableFactor, TableWithJoins, TimezoneInfo, UnaryOperator,
    Value, ValueWithSpan, WindowFrame, WindowFrameBound, WindowSpec, WindowType, With,
};
use sqlparser::dialect::{Dialect, PostgreSqlDialect, SQLiteDialect};
use sqlparser::parser::Parser;

create_exception!(_sqlcheck, QueryNotAllowed, PyValueError);

type Check<T = ()> = Result<T, String>;

/// A table, view, CTE, or subquery in a query's FROM, by the name queries
/// refer to it by, with the columns it may read.
#[derive(Clone)]
struct Source {
    name: String,
    columns: Vec<String>,
}

/// The sources of a SELECT, plus those of the queries enclosing it, which
/// correlated subqueries can refer to.
struct Scope<'a> {
    sources: Vec<Source>,
    parent: Option<&'a Scope<'a>>,
}

impl Scope<'_> {
    fn levels(&self) -> impl Iterator<Item = &Scope<'_>> {
        std::iter::successors(Some(self), |scope| scope.parent)
    }
}

/// The CTEs visible to a query, from its own WITH and those of the queries
/// enclosing it.
struct Ctes<'a> {
    names: HashMap<String, Vec<String>>,
    parent: Option<&'a Ctes<'a>>,
}

impl Ctes<'_> {
    fn get(&self, name: &str) -> Option<&Vec<String>> {
        std::iter::successors(Some(self), |ctes| ctes.parent).find_map(|ctes| ctes.names.get(name))
    }
}

struct Checker {
    sqlite: bool,
    tables: HashMap<String, Vec<String>>,
    functions: HashSet<String>,
}

fn not_allowed<T>(what: &str) -> Check<T> {
    Err(format!("{what} isn't allowed."))
}

impl Checker {
    /// An identifier as the database compares it: PostgreSQL folds unquoted
    /// names to lower case, and SQLite ignores case.
    fn name(&self, ident: &Ident) -> String {
        if self.sqlite || ident.quote_style.is_none() {
            ident.value.to_ascii_lowercase()
        } else {
            ident.value.clone()
        }
    }

    fn object_name(&self, name: &ObjectName) -> Check<String> {
        match name.0.as_slice() {
            [ObjectNamePart::Identifier(ident)] => Ok(self.name(ident)),
            _ => Err(format!(
                "{name} isn't allowed: use unqualified names, without a schema."
            )),
        }
    }

    fn query(
        &self,
        query: &Query,
        ctes: Option<&Ctes>,
        outer: Option<&Scope>,
    ) -> Check<Vec<Option<String>>> {
        let Query {
            with,
            body,
            order_by,
            limit_clause,
            fetch,
            locks,
            for_clause,
            settings,
            format_clause,
            pipe_operators,
        } = query;
        if fetch.is_some() {
            return not_allowed("FETCH");
        }
        if !locks.is_empty() {
            return not_allowed("Locking");
        }
        if for_clause.is_some() || settings.is_some() || format_clause.is_some() {
            return not_allowed("That clause");
        }
        if !pipe_operators.is_empty() {
            return not_allowed("A pipe operator");
        }
        let own_ctes;
        let ctes = match with {
            Some(with) => {
                own_ctes = self.with(with, ctes, outer)?;
                Some(&own_ctes)
            }
            None => ctes,
        };
        let outputs = match body.as_ref() {
            SetExpr::Select(select) => self.select(select, ctes, outer, order_by.as_ref())?,
            body => {
                let outputs = self.set_expr(body, ctes, outer)?;
                if let Some(order_by) = order_by {
                    self.order_by_outputs(order_by, &outputs)?;
                }
                outputs
            }
        };
        if let Some(limit_clause) = limit_clause {
            match limit_clause {
                LimitClause::LimitOffset {
                    limit,
                    offset,
                    limit_by,
                } => {
                    if let Some(limit) = limit {
                        self.number(limit)?;
                    }
                    if let Some(Offset { value, rows: _ }) = offset {
                        self.number(value)?;
                    }
                    if !limit_by.is_empty() {
                        return not_allowed("LIMIT BY");
                    }
                }
                LimitClause::OffsetCommaLimit { offset, limit } => {
                    self.number(offset)?;
                    self.number(limit)?;
                }
            }
        }
        Ok(outputs)
    }

    fn with<'a>(
        &self,
        with: &With,
        ctes: Option<&'a Ctes<'a>>,
        outer: Option<&Scope>,
    ) -> Check<Ctes<'a>> {
        let With {
            with_token: _,
            recursive,
            cte_tables,
        } = with;
        if *recursive {
            return not_allowed("WITH RECURSIVE");
        }
        let mut names = HashMap::new();
        for cte in cte_tables {
            let Cte {
                alias,
                query,
                from,
                materialized: _,
                closing_paren_token: _,
            } = cte;
            if from.is_some() {
                return not_allowed("CTE FROM");
            }
            let TableAlias {
                explicit: _,
                name,
                columns,
                at,
            } = alias;
            if at.is_some() {
                return not_allowed("AT in an alias");
            }
            // Each CTE sees those before it.
            let visible = Ctes {
                names: names.clone(),
                parent: ctes,
            };
            let outputs = self.query(query, Some(&visible), outer)?;
            let columns = if columns.is_empty() {
                named_outputs(outputs, &name.value)?
            } else {
                if columns.len() != outputs.len() || columns.iter().any(|c| c.data_type.is_some()) {
                    return Err(format!(
                        "The column list of {name} must name each of its query's columns."
                    ));
                }
                columns.iter().map(|c| self.name(&c.name)).collect()
            };
            if names.insert(self.name(name), columns).is_some() {
                return Err(format!("{name} is defined twice."));
            }
        }
        Ok(Ctes {
            names,
            parent: ctes,
        })
    }

    fn set_expr(
        &self,
        body: &SetExpr,
        ctes: Option<&Ctes>,
        outer: Option<&Scope>,
    ) -> Check<Vec<Option<String>>> {
        match body {
            SetExpr::Select(select) => self.select(select, ctes, outer, None),
            SetExpr::Query(query) => self.query(query, ctes, outer),
            SetExpr::SetOperation {
                left,
                op,
                set_quantifier,
                right,
            } => {
                if !matches!(op, SetOperator::Union | SetOperator::Except | SetOperator::Intersect) {
                    return not_allowed(&op.to_string());
                }
                if !matches!(
                    set_quantifier,
                    SetQuantifier::All | SetQuantifier::Distinct | SetQuantifier::None
                ) {
                    return not_allowed(&set_quantifier.to_string());
                }
                let outputs = self.set_expr(left, ctes, outer)?;
                self.set_expr(right, ctes, outer)?;
                Ok(outputs)
            }
            _ => Err(
                "Only SELECT queries are allowed, optionally combined with UNION, INTERSECT, or EXCEPT."
                    .to_string(),
            ),
        }
    }

    fn select(
        &self,
        select: &Select,
        ctes: Option<&Ctes>,
        outer: Option<&Scope>,
        order_by: Option<&OrderBy>,
    ) -> Check<Vec<Option<String>>> {
        let Select {
            select_token: _,
            optimizer_hints,
            distinct,
            select_modifiers,
            top,
            top_before_distinct: _,
            projection,
            exclude,
            into,
            from,
            lateral_views,
            prewhere,
            selection,
            connect_by,
            group_by,
            cluster_by,
            distribute_by,
            sort_by,
            having,
            named_window,
            qualify,
            window_before_qualify: _,
            value_table_mode,
            flavor,
        } = select;
        if !optimizer_hints.is_empty() || select_modifiers.is_some() {
            return not_allowed("A SELECT modifier");
        }
        if top.is_some() {
            return not_allowed("TOP");
        }
        if exclude.is_some() {
            return not_allowed("EXCLUDE");
        }
        if into.is_some() {
            return not_allowed("SELECT INTO");
        }
        if !lateral_views.is_empty() {
            return not_allowed("LATERAL VIEW");
        }
        if prewhere.is_some() || !connect_by.is_empty() || qualify.is_some() {
            return not_allowed("That clause");
        }
        if !cluster_by.is_empty() || !distribute_by.is_empty() || !sort_by.is_empty() {
            return not_allowed("That clause");
        }
        if !named_window.is_empty() {
            return not_allowed("A named window");
        }
        if value_table_mode.is_some() || !matches!(flavor, SelectFlavor::Standard) {
            return not_allowed("That SELECT form");
        }
        if let Some(Distinct::On(_)) = distinct {
            return not_allowed("DISTINCT ON");
        }

        let mut sources = Vec::new();
        let mut conditions = Vec::new();
        for table in from {
            self.table_with_joins(table, ctes, outer, &mut sources, &mut conditions)?;
        }
        let mut names = HashSet::new();
        for source in &sources {
            if !names.insert(&source.name) {
                return Err(format!(
                    "{} appears twice in FROM. Give each an alias.",
                    source.name
                ));
            }
        }
        let scope = Scope {
            sources,
            parent: outer,
        };

        let mut outputs = Vec::new();
        let mut aliases = Vec::new();
        for item in projection {
            match item {
                SelectItem::UnnamedExpr(expr) => {
                    self.expr(expr, &scope, ctes)?;
                    outputs.push(match expr {
                        Expr::Identifier(ident) => Some(self.name(ident)),
                        Expr::CompoundIdentifier(parts) => {
                            parts.last().map(|ident| self.name(ident))
                        }
                        _ => None,
                    });
                }
                SelectItem::ExprWithAlias { expr, alias } => {
                    self.expr(expr, &scope, ctes)?;
                    outputs.push(Some(self.name(alias)));
                    aliases.push(self.name(alias));
                }
                SelectItem::Wildcard(_) | SelectItem::QualifiedWildcard(..) => {
                    return Err("SELECT * isn't allowed: name the columns to select.".to_string());
                }
                SelectItem::ExprWithAliases { .. } => return not_allowed("Multiple aliases"),
            }
        }
        for (condition, join) in conditions {
            let join_scope = Scope {
                sources: scope.sources[join].to_vec(),
                parent: outer,
            };
            self.expr(condition, &join_scope, ctes)?;
        }
        if let Some(selection) = selection {
            self.expr(selection, &scope, ctes)?;
        }
        match group_by {
            GroupByExpr::Expressions(exprs, modifiers) => {
                if !modifiers.is_empty() {
                    return not_allowed("A GROUP BY modifier");
                }
                for expr in exprs {
                    // Output columns by position, or expressions of the
                    // inputs, not output names, which PostgreSQL resolves
                    // as input columns first.
                    if is_integer(expr) {
                        continue;
                    }
                    let result = self.expr(expr, &scope, ctes);
                    if let (Err(_), Expr::Identifier(ident)) = (&result, expr)
                        && outputs.contains(&Some(self.name(ident)))
                    {
                        return Err(format!(
                            "GROUP BY {ident} isn't allowed, as it names an output column. Repeat its expression, or give its position."
                        ));
                    }
                    result?;
                }
            }
            GroupByExpr::All(_) => return not_allowed("GROUP BY ALL"),
        }
        if let Some(having) = having {
            self.expr(having, &scope, ctes)?;
        }
        if let Some(order_by) = order_by {
            for OrderByExpr {
                expr,
                options: _,
                with_fill,
            } in self.order_by_exprs(order_by)?
            {
                if with_fill.is_some() {
                    return not_allowed("WITH FILL");
                }
                // A bare name of an output column means that column, ahead
                // of any input column: in PostgreSQL, any output column's
                // name, but in SQLite, only an alias given with AS.
                let output = match expr {
                    Expr::Identifier(ident) if self.sqlite => aliases.contains(&self.name(ident)),
                    Expr::Identifier(ident) => outputs.contains(&Some(self.name(ident))),
                    _ => false,
                };
                if !output && !is_integer(expr) {
                    self.expr(expr, &scope, ctes)?;
                }
            }
        }
        Ok(outputs)
    }

    fn order_by_exprs<'a>(&self, order_by: &'a OrderBy) -> Check<&'a [OrderByExpr]> {
        let OrderBy { kind, interpolate } = order_by;
        if interpolate.is_some() {
            return not_allowed("INTERPOLATE");
        }
        match kind {
            OrderByKind::Expressions(exprs) => Ok(exprs),
            OrderByKind::All(_) => not_allowed("ORDER BY ALL"),
        }
    }

    /// ORDER BY after UNION and so on, which can only name output columns.
    fn order_by_outputs(&self, order_by: &OrderBy, outputs: &[Option<String>]) -> Check {
        for OrderByExpr {
            expr,
            options: _,
            with_fill,
        } in self.order_by_exprs(order_by)?
        {
            if with_fill.is_some() {
                return not_allowed("WITH FILL");
            }
            let ok = match expr {
                Expr::Identifier(ident) => outputs.contains(&Some(self.name(ident))),
                expr => is_integer(expr),
            };
            if !ok {
                return Err(format!(
                    "ORDER BY {expr} isn't allowed here: name an output column, or give its position."
                ));
            }
        }
        Ok(())
    }

    fn table_with_joins<'e>(
        &self,
        table: &'e TableWithJoins,
        ctes: Option<&Ctes>,
        outer: Option<&Scope>,
        sources: &mut Vec<Source>,
        conditions: &mut Vec<(&'e Expr, Range<usize>)>,
    ) -> Check {
        let TableWithJoins { relation, joins } = table;
        // A join's ON and USING see only the tables in its own join tree,
        // from here, not those of other FROM items, separated by commas.
        let start = sources.len();
        self.table_factor(relation, ctes, outer, sources, conditions)?;
        for join in joins {
            let Join {
                relation,
                global,
                join_operator,
            } = join;
            if *global {
                return not_allowed("GLOBAL JOIN");
            }
            let left = sources.len();
            self.table_factor(relation, ctes, outer, sources, conditions)?;
            let constraint = match join_operator {
                JoinOperator::Join(c)
                | JoinOperator::Inner(c)
                | JoinOperator::Left(c)
                | JoinOperator::LeftOuter(c)
                | JoinOperator::Right(c)
                | JoinOperator::RightOuter(c)
                | JoinOperator::FullOuter(c)
                | JoinOperator::CrossJoin(c) => c,
                _ => return not_allowed("That kind of join"),
            };
            match constraint {
                JoinConstraint::On(expr) => conditions.push((expr, start..sources.len())),
                JoinConstraint::Using(names) => {
                    for name in names {
                        let column = self.object_name(name)?;
                        let has = |source: &Source| source.columns.contains(&column);
                        // Readable on each side, or the database reports the
                        // name as ambiguous, or missing, if another table
                        // there has it unreadable.
                        if !sources[start..left].iter().any(has) || !sources[left..].iter().any(has)
                        {
                            return Err(format!(
                                "USING ({name}) needs {name} readable on both sides of the join."
                            ));
                        }
                    }
                }
                JoinConstraint::None => {}
                JoinConstraint::Natural => return not_allowed("NATURAL JOIN"),
            }
        }
        Ok(())
    }

    fn table_factor<'e>(
        &self,
        factor: &'e TableFactor,
        ctes: Option<&Ctes>,
        outer: Option<&Scope>,
        sources: &mut Vec<Source>,
        conditions: &mut Vec<(&'e Expr, Range<usize>)>,
    ) -> Check {
        match factor {
            TableFactor::Table {
                name,
                alias,
                args,
                with_hints,
                version,
                with_ordinality,
                partitions,
                json_path,
                sample,
                index_hints,
            } => {
                if args.is_some() {
                    return not_allowed("A table function");
                }
                if !with_hints.is_empty()
                    || version.is_some()
                    || *with_ordinality
                    || !partitions.is_empty()
                    || json_path.is_some()
                    || sample.is_some()
                    || !index_hints.is_empty()
                {
                    return not_allowed("That table modifier");
                }
                let table = self.object_name(name)?;
                // A CTE's name hides a table's, as in the database.
                let columns = match ctes.and_then(|ctes| ctes.get(&table)) {
                    Some(columns) => columns.clone(),
                    None => match self.tables.get(&table) {
                        Some(columns) => columns.clone(),
                        None => {
                            return Err(format!(
                                "There's no table {name} that can be queried. Call the schema tool to list them."
                            ));
                        }
                    },
                };
                let name = match alias {
                    Some(alias) => self.alias(alias)?,
                    None => table,
                };
                sources.push(Source { name, columns });
            }
            TableFactor::Derived {
                lateral,
                subquery,
                alias,
                sample,
            } => {
                if *lateral {
                    return not_allowed("LATERAL");
                }
                if sample.is_some() {
                    return not_allowed("TABLESAMPLE");
                }
                let Some(alias) = alias else {
                    return Err("A subquery in FROM needs an alias.".to_string());
                };
                let name = self.alias(alias)?;
                let outputs = self.query(subquery, ctes, outer)?;
                let columns = named_outputs(outputs, &name)?;
                sources.push(Source { name, columns });
            }
            TableFactor::NestedJoin {
                table_with_joins,
                alias,
            } => {
                if alias.is_some() {
                    return not_allowed("An alias on a parenthesized join");
                }
                self.table_with_joins(table_with_joins, ctes, outer, sources, conditions)?;
            }
            _ => return not_allowed("That FROM item"),
        }
        Ok(())
    }

    fn alias(&self, alias: &TableAlias) -> Check<String> {
        let TableAlias {
            explicit: _,
            name,
            columns,
            at,
        } = alias;
        if !columns.is_empty() {
            return not_allowed("A column list in an alias");
        }
        if at.is_some() {
            return not_allowed("AT in an alias");
        }
        Ok(self.name(name))
    }

    /// Check a column reference resolves to a readable column, as the
    /// database would resolve it.
    ///
    /// The database resolves an unqualified name in the innermost query with
    /// a table that has a column of that name, so the checker does too, but
    /// it doesn't know the columns it can't read. So exactly one source in
    /// the innermost query with any sources must have the column readable:
    /// with none, an unreadable column of the same name could be the one the
    /// database picks, and with more, the name is ambiguous.
    fn column(&self, parts: &[Ident], scope: &Scope) -> Check {
        match parts {
            [column] => {
                let name = self.name(column);
                let Some(level) = scope.levels().find(|level| !level.sources.is_empty()) else {
                    return Err(format!("There's no table for the column {column}."));
                };
                let mut found = level.sources.iter().filter(|s| s.columns.contains(&name));
                match (found.next(), found.next(), level.sources.as_slice()) {
                    (Some(_), None, _) => Ok(()),
                    (None, _, [source]) => Err(format!(
                        "{} has no column {column} that can be queried.",
                        source.name
                    )),
                    (None, _, _) => Err(format!(
                        "No table in FROM has a column {column} that can be queried."
                    )),
                    (Some(_), Some(_), _) => Err(format!(
                        "The column {column} is ambiguous. Qualify it with its table's name or alias."
                    )),
                }
            }
            [table, column] => {
                let table_name = self.name(table);
                let source = scope
                    .levels()
                    .find_map(|level| level.sources.iter().find(|s| s.name == table_name));
                match source {
                    Some(source) => self.source_column(source, column),
                    None => Err(format!(
                        "There's no table or alias {table} in the query, for {table}.{column}."
                    )),
                }
            }
            _ => Err(format!(
                "{} isn't allowed: qualify columns with just a table name or alias.",
                ObjectName::from(parts.to_vec())
            )),
        }
    }

    fn source_column(&self, source: &Source, column: &Ident) -> Check {
        if source.columns.contains(&self.name(column)) {
            Ok(())
        } else {
            Err(format!(
                "{} has no column {column} that can be queried.",
                source.name
            ))
        }
    }

    fn number(&self, expr: &Expr) -> Check {
        match expr {
            Expr::Value(ValueWithSpan {
                value: Value::Number(_, false),
                span: _,
            }) => Ok(()),
            _ => Err(format!("{expr} must be a number.")),
        }
    }

    fn function_name(&self, name: &str) -> Check {
        if self.functions.contains(name) {
            Ok(())
        } else {
            Err(format!("The function {name}() isn't allowed."))
        }
    }

    fn exprs<'a>(
        &self,
        exprs: impl IntoIterator<Item = &'a Expr>,
        scope: &Scope,
        ctes: Option<&Ctes>,
    ) -> Check {
        exprs
            .into_iter()
            .try_for_each(|expr| self.expr(expr, scope, ctes))
    }

    fn expr(&self, expr: &Expr, scope: &Scope, ctes: Option<&Ctes>) -> Check {
        match expr {
            Expr::Identifier(ident) => self.column(std::slice::from_ref(ident), scope),
            Expr::CompoundIdentifier(parts) => self.column(parts, scope),
            Expr::Value(ValueWithSpan { value, span: _ }) => match value {
                Value::Number(_, false)
                | Value::SingleQuotedString(_)
                | Value::Boolean(_)
                | Value::Null => Ok(()),
                _ => Err(format!(
                    "The value {value} isn't allowed: use plain numbers and 'single-quoted' strings."
                )),
            },
            Expr::IsFalse(e)
            | Expr::IsNotFalse(e)
            | Expr::IsTrue(e)
            | Expr::IsNotTrue(e)
            | Expr::IsNull(e)
            | Expr::IsNotNull(e)
            | Expr::Nested(e) => self.expr(e, scope, ctes),
            Expr::IsDistinctFrom(a, b) | Expr::IsNotDistinctFrom(a, b) => {
                self.exprs([a.as_ref(), b], scope, ctes)
            }
            Expr::InList {
                expr,
                list,
                negated: _,
            } => {
                self.expr(expr, scope, ctes)?;
                self.exprs(list, scope, ctes)
            }
            Expr::InSubquery {
                expr,
                subquery,
                negated: _,
            } => {
                self.expr(expr, scope, ctes)?;
                self.query(subquery, ctes, Some(scope)).map(drop)
            }
            Expr::Between {
                expr,
                negated: _,
                low,
                high,
            } => self.exprs([expr.as_ref(), low, high], scope, ctes),
            Expr::BinaryOp { left, op, right } => {
                use BinaryOperator::*;
                if !matches!(
                    op,
                    Plus | Minus
                        | Multiply
                        | Divide
                        | Modulo
                        | StringConcat
                        | Gt
                        | Lt
                        | GtEq
                        | LtEq
                        | Eq
                        | NotEq
                        | And
                        | Or
                        | Arrow
                        | LongArrow
                ) {
                    return not_allowed(&format!("The operator {op}"));
                }
                self.exprs([left.as_ref(), right], scope, ctes)
            }
            Expr::UnaryOp { op, expr } => {
                if !matches!(
                    op,
                    UnaryOperator::Not | UnaryOperator::Minus | UnaryOperator::Plus
                ) {
                    return not_allowed(&format!("The operator {op}"));
                }
                self.expr(expr, scope, ctes)
            }
            Expr::Like {
                negated: _,
                any,
                expr,
                pattern,
                escape_char,
            } => self.like(*any, expr, pattern, escape_char.as_deref(), scope, ctes),
            Expr::ILike {
                negated: _,
                any,
                expr,
                pattern,
                escape_char,
            } => {
                if self.sqlite {
                    return not_allowed("ILIKE");
                }
                self.like(*any, expr, pattern, escape_char.as_deref(), scope, ctes)
            }
            Expr::Case {
                case_token: _,
                end_token: _,
                operand,
                conditions,
                else_result,
            } => {
                self.exprs(operand.as_deref(), scope, ctes)?;
                for when in conditions {
                    self.exprs([&when.condition, &when.result], scope, ctes)?;
                }
                self.exprs(else_result.as_deref(), scope, ctes)
            }
            Expr::Cast {
                kind,
                expr,
                data_type,
                format,
            } => {
                if !matches!(kind, CastKind::Cast | CastKind::DoubleColon) || format.is_some() {
                    return not_allowed("That cast");
                }
                if !cast_type(data_type) {
                    return Err(format!("Casting to {data_type} isn't allowed."));
                }
                self.expr(expr, scope, ctes)
            }
            Expr::Exists {
                subquery,
                negated: _,
            } => self.query(subquery, ctes, Some(scope)).map(drop),
            Expr::Subquery(query) => self.query(query, ctes, Some(scope)).map(drop),
            Expr::Function(function) => self.function(function, scope, ctes),
            Expr::Ceil { expr, field } => self.ceil_floor("ceil", expr, field, scope, ctes),
            Expr::Floor { expr, field } => self.ceil_floor("floor", expr, field, scope, ctes),
            Expr::Substring {
                expr,
                substring_from,
                substring_for,
                special: _,
                shorthand: _,
            } => {
                self.function_name("substring")?;
                self.expr(expr, scope, ctes)?;
                self.exprs(substring_from.as_deref(), scope, ctes)?;
                self.exprs(substring_for.as_deref(), scope, ctes)
            }
            Expr::Trim {
                trim_where: _,
                trim_what,
                expr,
                trim_characters,
            } => {
                self.function_name("trim")?;
                if trim_characters.is_some() {
                    return not_allowed("That TRIM form");
                }
                self.exprs(trim_what.as_deref(), scope, ctes)?;
                self.expr(expr, scope, ctes)
            }
            Expr::Position { expr, r#in } => {
                self.function_name("position")?;
                self.exprs([expr.as_ref(), r#in], scope, ctes)
            }
            Expr::Extract {
                field,
                syntax: _,
                expr,
            } => {
                self.function_name("extract")?;
                if matches!(field, DateTimeField::Custom(_)) {
                    return Err(format!("EXTRACT({field} FROM ...) isn't allowed."));
                }
                self.expr(expr, scope, ctes)
            }
            Expr::Wildcard(_) | Expr::QualifiedWildcard(..) => {
                Err("* isn't allowed: name the columns to select.".to_string())
            }
            _ => Err(format!("The expression {expr} isn't allowed.")),
        }
    }

    fn ceil_floor(
        &self,
        name: &str,
        expr: &Expr,
        field: &CeilFloorKind,
        scope: &Scope,
        ctes: Option<&Ctes>,
    ) -> Check {
        self.function_name(name)?;
        // Plain, not with a unit or scale.
        if !matches!(
            field,
            CeilFloorKind::DateTimeField(DateTimeField::NoDateTime)
        ) {
            return Err(format!("That form of {name}() isn't allowed."));
        }
        self.expr(expr, scope, ctes)
    }

    fn like(
        &self,
        any: bool,
        expr: &Expr,
        pattern: &Expr,
        escape_char: Option<&Expr>,
        scope: &Scope,
        ctes: Option<&Ctes>,
    ) -> Check {
        if any {
            return not_allowed("LIKE ANY");
        }
        self.exprs([expr, pattern], scope, ctes)?;
        if let Some(escape_char) = escape_char {
            self.expr(escape_char, scope, ctes)?;
        }
        Ok(())
    }

    fn function(&self, function: &Function, scope: &Scope, ctes: Option<&Ctes>) -> Check {
        let Function {
            name,
            uses_odbc_syntax,
            parameters,
            args,
            within_group,
            filter,
            null_treatment,
            over,
        } = function;
        let function_name = self.object_name(name)?;
        self.function_name(&function_name)?;
        if *uses_odbc_syntax || !matches!(parameters, FunctionArguments::None) {
            return not_allowed("That function syntax");
        }
        if !within_group.is_empty() {
            return not_allowed("WITHIN GROUP");
        }
        if null_treatment.is_some() {
            return not_allowed("IGNORE NULLS");
        }
        match args {
            FunctionArguments::None => {}
            FunctionArguments::Subquery(_) => {
                return not_allowed("A subquery as a function's arguments");
            }
            FunctionArguments::List(FunctionArgumentList {
                duplicate_treatment,
                args,
                clauses,
            }) => {
                if !matches!(
                    duplicate_treatment,
                    None | Some(DuplicateTreatment::Distinct) | Some(DuplicateTreatment::All)
                ) {
                    return not_allowed("That function syntax");
                }
                for arg in args {
                    match arg {
                        FunctionArg::Unnamed(FunctionArgExpr::Expr(expr)) => {
                            self.expr(expr, scope, ctes)?
                        }
                        // count(*), the only function taking *.
                        FunctionArg::Unnamed(FunctionArgExpr::Wildcard)
                            if function_name == "count" && args.len() == 1 => {}
                        _ => {
                            return Err(format!(
                                "Those arguments to {function_name}() aren't allowed."
                            ));
                        }
                    }
                }
                for clause in clauses {
                    match clause {
                        FunctionArgumentClause::OrderBy(exprs) => {
                            self.order_by_items(exprs, scope, ctes)?
                        }
                        _ => {
                            return Err(format!("That clause in {function_name}() isn't allowed."));
                        }
                    }
                }
            }
        }
        if let Some(filter) = filter {
            self.expr(filter, scope, ctes)?;
        }
        match over {
            None => {}
            Some(WindowType::WindowSpec(WindowSpec {
                window_name,
                partition_by,
                order_by,
                window_frame,
            })) => {
                if window_name.is_some() {
                    return not_allowed("A named window");
                }
                self.exprs(partition_by, scope, ctes)?;
                self.order_by_items(order_by, scope, ctes)?;
                if let Some(WindowFrame {
                    units: _,
                    start_bound,
                    end_bound,
                }) = window_frame
                {
                    for bound in std::iter::once(start_bound).chain(end_bound) {
                        if let WindowFrameBound::Preceding(Some(expr))
                        | WindowFrameBound::Following(Some(expr)) = bound
                        {
                            self.number(expr)?;
                        }
                    }
                }
            }
            Some(WindowType::NamedWindow(_)) => return not_allowed("A named window"),
        }
        Ok(())
    }

    fn order_by_items(&self, items: &[OrderByExpr], scope: &Scope, ctes: Option<&Ctes>) -> Check {
        for OrderByExpr {
            expr,
            options: _,
            with_fill,
        } in items
        {
            if with_fill.is_some() {
                return not_allowed("WITH FILL");
            }
            self.expr(expr, scope, ctes)?;
        }
        Ok(())
    }
}

fn is_integer(expr: &Expr) -> bool {
    matches!(
        expr,
        Expr::Value(ValueWithSpan {
            value: Value::Number(n, false),
            span: _,
        }) if n.chars().all(|c| c.is_ascii_digit())
    )
}

fn cast_type(data_type: &DataType) -> bool {
    matches!(
        data_type,
        DataType::Int(None)
            | DataType::Integer(None)
            | DataType::BigInt(None)
            | DataType::Real
            | DataType::Double(ExactNumberInfo::None)
            | DataType::DoublePrecision
            | DataType::Float(_)
            | DataType::Numeric(_)
            | DataType::Decimal(_)
            | DataType::Varchar(_)
            | DataType::Text
            | DataType::Date
            | DataType::Timestamp(
                None,
                TimezoneInfo::None | TimezoneInfo::WithoutTimeZone | TimezoneInfo::WithTimeZone
            )
            | DataType::Bool
            | DataType::Boolean
    )
}

/// The column names of a subquery or CTE, which must all be named, to be
/// referred to.
fn named_outputs(outputs: Vec<Option<String>>, name: &str) -> Check<Vec<String>> {
    outputs
        .into_iter()
        .map(|output| output.ok_or_else(|| format!("Give each column of {name} a name, with AS.")))
        .collect()
}

fn check(
    sql: &str,
    dialect: &str,
    tables: HashMap<String, Vec<String>>,
    functions: HashSet<String>,
) -> Check<String> {
    let (parser_dialect, sqlite): (Box<dyn Dialect>, bool) = match dialect {
        "postgresql" => (Box::new(PostgreSqlDialect {}), false),
        "sqlite" => (Box::new(SQLiteDialect {}), true),
        _ => return Err(format!("Unknown dialect {dialect:?}.")),
    };
    let checker = Checker {
        sqlite,
        tables: HashMap::new(),
        functions: functions
            .into_iter()
            .map(|f| f.to_ascii_lowercase())
            .collect(),
    };
    // Compare names as the database does.
    let tables = tables
        .into_iter()
        .map(|(table, columns)| {
            let fold = |name: String| {
                if sqlite {
                    name.to_ascii_lowercase()
                } else {
                    name
                }
            };
            (fold(table), columns.into_iter().map(fold).collect())
        })
        .collect();
    let checker = Checker { tables, ..checker };
    let statements = Parser::parse_sql(parser_dialect.as_ref(), sql).map_err(|e| e.to_string())?;
    let [statement] = statements.as_slice() else {
        return Err("Only a single statement is allowed.".to_string());
    };
    let Statement::Query(query) = statement else {
        return Err("Only SELECT queries are allowed.".to_string());
    };
    checker.query(query, None, None)?;
    Ok(statement.to_string())
}

/// Check the query, returning it rendered back to SQL from what was checked,
/// for the database to run, or raising QueryNotAllowed with the reason.
#[pyfunction]
#[pyo3(signature = (sql, *, dialect, tables, functions))]
fn check_query(
    sql: &str,
    dialect: &str,
    tables: HashMap<String, Vec<String>>,
    functions: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let functions = functions
        .try_iter()?
        .map(|name| name?.extract::<String>())
        .collect::<PyResult<HashSet<String>>>()?;
    check(sql, dialect, tables, functions).map_err(QueryNotAllowed::new_err)
}

#[pymodule]
fn _sqlcheck(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(check_query, m)?)?;
    m.add("QueryNotAllowed", m.py().get_type::<QueryNotAllowed>())?;
    Ok(())
}

#[cfg(test)]
mod tests;
