#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
医学生人才信息库 · Schema 结构校验器（本地静态校验，零依赖）

为什么需要它：本机没有 PostgreSQL，DDL 无法实跑。
在拿到真实 PG 之前，至少要做静态一致性校验，抓住最容易犯且最贵的错误：
  1. BEGIN/COMMIT 是否配对（漏 COMMIT 会让整个迁移静默回滚）
  2. 每个 SQL 文件是否设置 search_path（否则对象会建到 public）
  3. 外键 REFERENCES 的目标表是否已定义、且定义在前（前向引用在 PG 中直接报错）
  4. 每张表是否有主键或唯一约束（没有主键的表无法做增量同步与去重）
  5. 重复定义的对象名（同一对象建两次会报错）
  6. 域（DOMAIN）是否先定义后使用
  7. 每张表的列定义括号是否配平

它不能替代在真库上执行，但能在没有数据库的环境里挡住绝大部分低级错误。
用法：python code/validate_schema.py [项目根]
退出码：0 = 通过；1 = 有错误
"""
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SQL_DIR = os.path.join("schema", "sql")

CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)", re.I)
CREATE_VIEW = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+([A-Za-z_][\w.]*)", re.I)
CREATE_DOMAIN = re.compile(r"CREATE\s+DOMAIN\s+([A-Za-z_][\w.]*)", re.I)
CREATE_FUNC = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([A-Za-z_][\w.]*)", re.I)
CREATE_EXT = re.compile(r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
REFERENCES = re.compile(r"REFERENCES\s+([A-Za-z_][\w.]*)", re.I)

# PostgreSQL 保留字（reserved 类别）——作为表名/列名不加引号会直接语法报错。
# 教训：`CREATE TABLE constraint (...)` 曾在真库执行时才暴露，静态校验必须覆盖。
RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric",
    "both", "case", "cast", "check", "collate", "column", "constraint", "create",
    "current_catalog", "current_date", "current_role", "current_time",
    "current_timestamp", "current_user", "default", "deferrable", "desc", "distinct",
    "do", "else", "end", "except", "false", "fetch", "for", "foreign", "from",
    "grant", "group", "having", "in", "initially", "intersect", "into", "lateral",
    "leading", "limit", "localtime", "localtimestamp", "not", "null", "offset", "on",
    "only", "or", "order", "placing", "primary", "references", "returning", "select",
    "session_user", "some", "symmetric", "table", "then", "to", "trailing", "true",
    "union", "unique", "user", "using", "variadic", "when", "where", "window", "with",
}
COLUMN_DEF = re.compile(r"^([A-Za-z_]\w*)\s+\S")


def mask_dollar_quotes(text):
    """把 $$ ... $$ 函数体替换为等长占位，避免其中的分号被当作语句分隔符。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("$$", i):
            j = text.find("$$", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                break
            out.append(" " * (j + 2 - i))
            i = j + 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def split_statements(text):
    masked = mask_dollar_quotes(text)
    stmts = []
    start = 0
    for i, ch in enumerate(masked):
        if ch == ";":
            stmts.append((text[start:i], start))
            start = i + 1
    tail = text[start:]
    if tail.strip():
        stmts.append((tail, start))
    return stmts


def balanced_parens(body):
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def strip_sql_comments(stmt: str) -> str:
    """去掉 `--` 行注释（保留行数，便于行号仍然对得上）。

    为什么必须做（实测撞出来的）：022 的注释里写了一句
        `-- 可重放：CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE FUNCTION。`
    而 CREATE_TABLE 正则里的 `(?:IF\\s+NOT\\s+EXISTS\\s+)?` 在 `EXISTS ` 后面遇到 `/`
    （不是合法标识符开头）时会**回溯**，于是把 `IF` 当成表名，报出
    "表 IF 括号不配平"。也就是说：**校验器在读注释里的示例代码，并把它当成了真代码。**
    这不是"忍一下"的小问题 —— 一个会对文档文字误报的校验器，会训练人忽略它的输出。

    只处理 `--`（本项目的迁移注释一律单独成行），不做通用的引号内识别：
    真正需要精确解析的场景应该交给 PostgreSQL 自己（`--check` 与实跑才是权威）。
    """
    out = []
    for ln in stmt.split("\n"):
        i = ln.find("--")
        out.append(ln[:i] if i >= 0 else ln)
    return "\n".join(out)


def outer_body(stmt, start=0):
    """取出 CREATE TABLE 名之后的括号主体。
    start 必须传 CREATE TABLE 匹配的结束位置——否则会误取注释中的括号。"""
    p = stmt.find("(", start)
    if p == -1:
        return None
    depth = 0
    for i in range(p, len(stmt)):
        if stmt[i] == "(":
            depth += 1
        elif stmt[i] == ")":
            depth -= 1
            if depth == 0:
                return stmt[p + 1:i]
    return None


def split_top_commas(body):
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def main(root):
    os.chdir(root)
    if not os.path.isdir(SQL_DIR):
        print("找不到目录 %s" % SQL_DIR)
        return 1

    errors, warnings = [], []
    files = sorted(f for f in os.listdir(SQL_DIR) if f.lower().endswith(".sql"))
    if not files:
        print("没有 SQL 文件")
        return 1

    defined = {}        # 对象名 -> 文件
    replaced = {}       # 被 OR REPLACE 覆盖过的对象 -> [(文件, 行)]
    defined_domains = {}
    order = []          # 按文件、语句顺序记录定义
    referenced = []     # (target, file, lineno)

    for fname in files:
        path = os.path.join(SQL_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()

        n_begin = len(re.findall(r"^\s*BEGIN\s*;", text, re.I | re.M))
        n_commit = len(re.findall(r"^\s*COMMIT\s*;", text, re.I | re.M))
        if n_begin != n_commit:
            errors.append("%s: BEGIN=%d 与 COMMIT=%d 不配对" % (fname, n_begin, n_commit))
        if "search_path" not in text:
            warnings.append("%s: 未设置 search_path，对象可能建到 public" % fname)

        for stmt, offset in split_statements(text):
            line = text[:offset].count("\n") + 1
            # 先剥注释再解析：否则会把注释里的示例 SQL 当真代码（见 strip_sql_comments）
            stripped = strip_sql_comments(stmt).strip()
            if not stripped:
                continue

            m = CREATE_TABLE.search(stripped)
            if m:
                name = m.group(1)
                order.append(name)
                if name in defined:
                    errors.append("%s:%d: 表 %s 重复定义（已在 %s）"
                                  % (fname, line, name, defined[name]))
                defined[name] = fname
                if name.lower() in RESERVED:
                    errors.append("%s:%d: 表名 %s 是 PostgreSQL 保留字，必须改名或加引号"
                                  % (fname, line, name))
                body = outer_body(stripped, m.end())
                if body is None or not balanced_parens(body):
                    errors.append("%s:%d: 表 %s 括号不配平" % (fname, line, name))
                    continue
                cols = split_top_commas(body)
                has_key = any(re.search(r"\b(PRIMARY\s+KEY|UNIQUE)\b", c, re.I) for c in cols)
                if not has_key:
                    warnings.append("%s:%d: 表 %s 无主键或唯一约束" % (fname, line, name))
                for c in cols:
                    if re.match(r"(PRIMARY|UNIQUE|FOREIGN|CHECK|CONSTRAINT|EXCLUDE)\b", c, re.I):
                        continue
                    cm = COLUMN_DEF.match(c)
                    if cm and cm.group(1).lower() in RESERVED:
                        errors.append("%s:%d: 表 %s 的列名 %s 是保留字"
                                      % (fname, line, name, cm.group(1)))
                    for r in REFERENCES.finditer(c):
                        referenced.append((r.group(1), fname, line))
                continue

            for rx, label in ((CREATE_VIEW, "view"), (CREATE_FUNC, "function")):
                mm = rx.search(stripped)
                if mm:
                    key = "%s:%s" % (label, mm.group(1))
                    # 区分两种写法（这一条是实测撞出来的）：
                    #   · `CREATE FUNCTION foo`       重复 → **错误**：apply 时会报 already exists
                    #   · `CREATE OR REPLACE FUNCTION foo` 重复 → **正常**：
                    #     这正是"不改已应用的迁移、用新迁移替换函数"的正当机制。
                    #     第一版把两者一律判错，于是修一个函数的 bug 就无路可走
                    #     （改老迁移被台账拒绝、改名又会污染函数空间）。
                    replace = re.search(r"CREATE\s+OR\s+REPLACE", stripped, re.I) is not None
                    # 第三种正当写法（051 实测撞出来的）：**先 DROP 再 CREATE**。
                    # 当"改了输出结构"时 `OR REPLACE` 做不到：
                    #   · 视图改列类型 → cannot change data type of view column ...
                    #   · 函数改返回类型 → cannot change return type of existing function
                    # 所以 039/042/051 都用 "DROP IF EXISTS + CREATE"。
                    # 校验器第一版只认 OR REPLACE，于是**把唯一可行的写法判成错误** ——
                    # 那会让"修 bug"无路可走（改老迁移被台账拒绝、改名污染名称空间）。
                    # 判据：**同一文件里这条语句之前**出现过 `DROP ... IF EXISTS <name>`。
                    # 注意要扫 `text[:offset]`（本语句之前的全部内容），而不是 `stripped` ——
                    # DROP 是**上一条语句**，第一版只在当前语句里找，于是仍然误报。
                    dropped = re.search(
                        r"DROP\s+(?:VIEW|FUNCTION|TABLE|MATERIALIZED\s+VIEW)\s+IF\s+EXISTS\s+"
                        + re.escape(mm.group(1)) + r"\b", text[:offset], re.I) is not None
                    if key in defined and not replace and not dropped:
                        errors.append("%s:%d: %s 重复定义（未用 OR REPLACE，也没有先 DROP IF EXISTS，"
                                      "apply 时会报 already exists；上一次定义在 %s）"
                                      % (fname, line, key, defined[key]))
                    elif key in defined and replace:
                        replaced.setdefault(key, []).append((fname, line))
                    defined[key] = fname

            mm = CREATE_DOMAIN.search(stripped)
            if mm:
                defined_domains[mm.group(1)] = (fname, line)
                continue

            mm = CREATE_EXT.search(stripped)
            if mm:
                continue

            mm = re.match(r"ALTER\s+TABLE\s+([A-Za-z_][\w.]*)", stripped, re.I)
            if mm:
                for r in REFERENCES.finditer(stripped):
                    referenced.append((r.group(1), fname, line))

    # 外键目标检查（含前向引用）
    def norm(n):
        return n.split(".")[-1]

    # 按语句顺序重放：用 defined 的顺序列表近似（同文件内按出现顺序）
    order_index = {}
    for i, name in enumerate(order):
        order_index.setdefault(norm(name), i)

    for target, fname, line in referenced:
        t = norm(target)
        if t not in order_index:
            errors.append("%s:%d: 外键指向未定义的表 %s" % (fname, line, target))

    # 域使用检查
    for fname in files:
        path = os.path.join(SQL_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            for i, ln in enumerate(fh, 1):
                for m in re.finditer(r"\bmt\.(\w+)\b", ln):
                    dom = m.group(1)
                    if dom in defined_domains and defined_domains[dom][0] == fname:
                        if defined_domains[dom][1] > i:
                            errors.append("%s:%d: 域 mt.%s 在定义前被使用"
                                          % (fname, i, dom))

    print("=" * 68)
    print("SQL 文件：%d 个（%s）" % (len(files), ", ".join(files)))
    print("对象：表 %d，视图/函数 %d，域 %d"
          % (len(order), len([k for k in defined if ':' in k]), len(defined_domains)))
    print("外键引用：%d 处" % len(referenced))
    print("错误：%d   警告：%d" % (len(errors), len(warnings)))
    for e in errors:
        print("  [ERROR] " + e)
    for w in warnings:
        print("  [WARN ] " + w)
    if replaced:
        # 函数被 OR REPLACE 覆盖**不是错误**（那正是"不改已应用的迁移、用新迁移替换函数"的机制），
        # 但它是"这个函数改过几版"的事实，值得打印出来供人核对最终生效的是哪一版。
        print("被 OR REPLACE 覆盖过的函数 %d 个（正常，非错误）：" % len(replaced))
        for key, hist in sorted(replaced.items()):
            print("  [INFO] %s：最新定义在 %s" % (key, defined.get(key, "?")))
    print("=" * 68)
    if not errors:
        print("提示：静态校验通过 ≠ 可在真库执行。T01 必须在 PostgreSQL 16 上实跑一次。")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
