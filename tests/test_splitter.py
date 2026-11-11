from sql_lineage.splitter import split_statements


def test_basic_split_and_line_numbers():
    text = "SELECT 1;\n\nSELECT 2;\n   SELECT 3"
    stmts = split_statements(text)
    assert [s for s, _ in stmts] == ["SELECT 1", "SELECT 2", "SELECT 3"]
    assert [ln for _, ln in stmts] == [1, 3, 4]


def test_semicolon_in_string_not_split():
    text = "SELECT ';' AS s, 'a;b' AS t; SELECT 2;"
    stmts = split_statements(text)
    assert len(stmts) == 2
    assert stmts[0][0].startswith("SELECT ';'")


def test_escaped_quote_in_string():
    text = "SELECT 'it''s; ok' AS x; SELECT 2"
    stmts = split_statements(text)
    assert len(stmts) == 2


def test_comments_with_semicolons():
    text = "-- comment; with semicolon\nSELECT 1;\n/* block; comment */ SELECT 2;"
    stmts = split_statements(text)
    assert len(stmts) == 2
    assert stmts[0][1] == 2  # 起始行指向第一条实际 SQL  token（跳过前导注释）
    assert stmts[1][1] == 3


def test_dollar_quoted_body():
    text = "CREATE MACRO m() AS $$ SELECT 1; SELECT 2 $$; SELECT 3;"
    stmts = split_statements(text)
    assert len(stmts) == 2


def test_double_quoted_identifier_with_semicolon():
    text = 'SELECT "weird;name" FROM t; SELECT 1;'
    stmts = split_statements(text)
    assert len(stmts) == 2


def test_empty_and_comment_only():
    assert split_statements("") == []
    assert split_statements("-- only a comment\n") == []
