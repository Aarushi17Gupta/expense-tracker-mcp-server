from fastmcp import FastMCP
import json
import os
import sys
import aiosqlite  # Changed: sqlite3 → aiosqlite
import tempfile

#DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expenses.db")
TEMP_DIR = tempfile.gettempdir()
DB_PATH = os.path.join(TEMP_DIR, "expenses.db")

CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")
CREDIT_TYPES = ("salary", "credit")
SAVINGS_TYPES = ("mutual_fund", "fd", "rd")

print(f"Database path: {DB_PATH}", file=sys.stderr)

mcp = FastMCP("ExpenseTracker")

def init_db():  # Keep as sync for initialization
    try:
        # Use synchronous sqlite3 just for initialization
        import sqlite3
        with sqlite3.connect(DB_PATH) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""
                CREATE TABLE IF NOT EXISTS expenses(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT DEFAULT '',
                    note TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS credits(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    credit_type TEXT NOT NULL,
                    note TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS savings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    savings_type TEXT NOT NULL,
                    scheme TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    deleted_at TEXT
                )
            """)
            # Existing databases need the column that marks deleted rows
            for table in ("expenses", "credits"):
                columns = [row[1] for row in c.execute(f"PRAGMA table_info({table})")]
                if "deleted_at" not in columns:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT")
            # Test write access
            c.execute("INSERT OR IGNORE INTO expenses(date, amount, category) VALUES ('2000-01-01', 0, 'test')")
            c.execute("DELETE FROM expenses WHERE category = 'test'")
            print("Database initialized successfully with write access", file=sys.stderr)
    except Exception as e:
        print(f"Database initialization error: {e}", file=sys.stderr)
        raise

# Initialize database synchronously at module load
init_db()

def load_categories():
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

@mcp.tool()
def list_categories():
    '''List valid expense categories and their subcategories. Call this before add_expense.'''
    return load_categories()

def category_error(category, subcategory=""):
    '''Return an error message if the category pair is not in categories.json, else None.'''
    valid = load_categories()
    if category not in valid:
        return f"Unknown category '{category}'. Valid: {sorted(valid)}"
    if subcategory and subcategory not in valid[category]:
        return f"Unknown subcategory '{subcategory}' for '{category}'. Valid: {valid[category]}"
    return None

@mcp.tool()
async def add_expense(date, amount, category, subcategory="", note=""):  # Changed: added async
    '''Add a new expense. Call list_categories first and use its exact category and subcategory keys.'''
    error = category_error(category, subcategory)
    if error:
        return {"status": "error", "message": error}
    try:
        async with aiosqlite.connect(DB_PATH) as c:  # Changed: added async
            cur = await c.execute(  # Changed: added await
                "INSERT INTO expenses(date, amount, category, subcategory, note) VALUES (?,?,?,?,?)",
                (date, amount, category, subcategory, note)
            )
            expense_id = cur.lastrowid
            await c.commit()  # Changed: added await
            return {"status": "success", "id": expense_id, "message": "Expense added successfully"}
    except Exception as e:  # Changed: simplified exception handling
        if "readonly" in str(e).lower():
            return {"status": "error", "message": "Database is in read-only mode. Check file permissions."}
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def edit_expense(expense_id, date=None, amount=None, category=None, subcategory=None, note=None):
    '''Edit an expense by id. Only the fields you pass are changed; pass subcategory="" or note="" to clear them.
    Use list_expenses to find the id and list_categories for valid category and subcategory keys.'''
    fields = {"date": date, "amount": amount, "category": category, "subcategory": subcategory, "note": note}
    changes = {name: value for name, value in fields.items() if value is not None}
    if not changes:
        return {"status": "error", "message": "No fields to update."}
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM expenses WHERE id = ? AND deleted_at IS NULL", (expense_id,))
            row = await cur.fetchone()
            if row is None:
                return {"status": "error", "message": f"No expense found with id {expense_id}."}
            expense = dict(row)

            if "category" in changes or "subcategory" in changes:
                merged = {**expense, **changes}
                error = category_error(merged["category"], merged["subcategory"])
                if error:
                    return {"status": "error", "message": error}

            # Column names come from the fixed `fields` dict above, never from user input
            assignments = ", ".join(f"{name} = ?" for name in changes)
            await c.execute(
                f"UPDATE expenses SET {assignments} WHERE id = ?",
                (*changes.values(), expense_id)
            )
            await c.commit()
            return {"status": "success", "expense": {**expense, **changes}, "message": "Expense updated successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def delete_expense(expense_id):
    '''Delete an expense by id. It is hidden from lists and totals and can be brought back with restore_expenses.
    Use list_expenses to find the id.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM expenses WHERE id = ? AND deleted_at IS NULL", (expense_id,))
            row = await cur.fetchone()
            if row is None:
                return {"status": "error", "message": f"No expense found with id {expense_id}."}
            await c.execute("UPDATE expenses SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (expense_id,))
            await c.commit()
            return {"status": "success", "deleted": dict(row), "message": "Expense deleted successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def delete_expenses_by_category(category=None, subcategory=None, start_date=None, end_date=None, confirm: bool = False):
    '''Delete all expenses matching a category and/or subcategory, optionally within an inclusive date range.
    Call without confirm first: that only previews the matches and deletes nothing. Show the matches to the user,
    then call again with the same filters and confirm=True to delete them. Deleted expenses are hidden from lists
    and totals and can be brought back with restore_expenses.'''
    if not category and not subcategory:
        return {"status": "error", "message": "Provide a category or subcategory."}
    if category:
        error = category_error(category, subcategory or "")
        if error:
            return {"status": "error", "message": error}
    elif not any(subcategory in subs for subs in load_categories().values()):
        return {"status": "error", "message": f"Unknown subcategory '{subcategory}'. Call list_categories for valid names."}

    # Conditions are fixed strings; every value is passed as a bound parameter
    filters = [
        ("category = ?", category),
        ("subcategory = ?", subcategory),
        ("date >= ?", start_date),
        ("date <= ?", end_date),
    ]
    active = [(condition, value) for condition, value in filters if value]
    where = " AND ".join(["deleted_at IS NULL"] + [condition for condition, _ in active])
    params = [value for _, value in active]
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute(f"SELECT * FROM expenses WHERE {where} ORDER BY date DESC, id DESC", params)
            matches = [dict(row) for row in await cur.fetchall()]
            if not matches:
                return {"status": "success", "count": 0, "message": "No matching expenses found."}
            if not confirm:
                return {
                    "status": "preview",
                    "count": len(matches),
                    "expenses": matches,
                    "message": "Nothing deleted. Call again with confirm=True to delete these expenses."
                }
            await c.execute(f"UPDATE expenses SET deleted_at = CURRENT_TIMESTAMP WHERE {where}", params)
            await c.commit()
            return {"status": "success", "count": len(matches), "deleted": matches, "message": "Expenses deleted successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def list_deleted_expenses():
    '''List deleted expenses that can be brought back with restore_expenses.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                """
                SELECT id, date, amount, category, subcategory, note, deleted_at
                FROM expenses
                WHERE deleted_at IS NOT NULL
                ORDER BY deleted_at DESC, id DESC
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error listing deleted expenses: {str(e)}"}

@mcp.tool()
async def restore_expenses(expense_ids: list[int]):
    '''Bring back deleted expenses by id. Use list_deleted_expenses to find the ids.'''
    if not expense_ids:
        return {"status": "error", "message": "Provide at least one expense id."}
    placeholders = ", ".join("?" for _ in expense_ids)
    where = f"id IN ({placeholders}) AND deleted_at IS NOT NULL"
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(f"SELECT id FROM expenses WHERE {where}", expense_ids)
            found = [row[0] for row in await cur.fetchall()]
            if not found:
                return {"status": "error", "message": f"No deleted expenses found with ids {expense_ids}."}
            await c.execute(f"UPDATE expenses SET deleted_at = NULL WHERE {where}", expense_ids)
            await c.commit()
            return {"status": "success", "count": len(found), "restored": found, "message": "Expenses restored successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def add_credit(date, amount, credit_type="credit", note=""):
    '''Record money received. Use credit_type "salary" for salary and "credit" for any other income,
    such as a refund or a transfer in. Use the note for details like the source.'''
    if credit_type not in CREDIT_TYPES:
        return {"status": "error", "message": f"Unknown credit_type '{credit_type}'. Valid: {list(CREDIT_TYPES)}"}
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                "INSERT INTO credits(date, amount, credit_type, note) VALUES (?,?,?,?)",
                (date, amount, credit_type, note)
            )
            credit_id = cur.lastrowid
            await c.commit()
            return {"status": "success", "id": credit_id, "message": "Credit added successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def list_credits(start_date, end_date):
    '''List salary and other credits within an inclusive date range.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                """
                SELECT id, date, amount, credit_type, note
                FROM credits
                WHERE date BETWEEN ? AND ? AND deleted_at IS NULL
                ORDER BY date DESC, id DESC
                """,
                (start_date, end_date)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error listing credits: {str(e)}"}

@mcp.tool()
async def edit_credit(credit_id, date=None, amount=None, credit_type=None, note=None):
    '''Edit a salary or credit by id. Only the fields you pass are changed; pass note="" to clear it.
    credit_type must be "salary" or "credit". Use list_credits to find the id.'''
    fields = {"date": date, "amount": amount, "credit_type": credit_type, "note": note}
    changes = {name: value for name, value in fields.items() if value is not None}
    if not changes:
        return {"status": "error", "message": "No fields to update."}
    if credit_type is not None and credit_type not in CREDIT_TYPES:
        return {"status": "error", "message": f"Unknown credit_type '{credit_type}'. Valid: {list(CREDIT_TYPES)}"}
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM credits WHERE id = ? AND deleted_at IS NULL", (credit_id,))
            row = await cur.fetchone()
            if row is None:
                return {"status": "error", "message": f"No credit found with id {credit_id}."}

            # Column names come from the fixed `fields` dict above, never from user input
            assignments = ", ".join(f"{name} = ?" for name in changes)
            await c.execute(
                f"UPDATE credits SET {assignments} WHERE id = ?",
                (*changes.values(), credit_id)
            )
            await c.commit()
            return {"status": "success", "credit": {**dict(row), **changes}, "message": "Credit updated successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def delete_credit(credit_id):
    '''Delete a salary or credit by id. It is hidden from lists and totals and can be brought back with
    restore_credits. Use list_credits to find the id.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM credits WHERE id = ? AND deleted_at IS NULL", (credit_id,))
            row = await cur.fetchone()
            if row is None:
                return {"status": "error", "message": f"No credit found with id {credit_id}."}
            await c.execute("UPDATE credits SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (credit_id,))
            await c.commit()
            return {"status": "success", "deleted": dict(row), "message": "Credit deleted successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def list_deleted_credits():
    '''List deleted salary and credit entries that can be brought back with restore_credits.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                """
                SELECT id, date, amount, credit_type, note, deleted_at
                FROM credits
                WHERE deleted_at IS NOT NULL
                ORDER BY deleted_at DESC, id DESC
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error listing deleted credits: {str(e)}"}

@mcp.tool()
async def restore_credits(credit_ids: list[int]):
    '''Bring back deleted salary and credit entries by id. Use list_deleted_credits to find the ids.'''
    if not credit_ids:
        return {"status": "error", "message": "Provide at least one credit id."}
    placeholders = ", ".join("?" for _ in credit_ids)
    where = f"id IN ({placeholders}) AND deleted_at IS NOT NULL"
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(f"SELECT id FROM credits WHERE {where}", credit_ids)
            found = [row[0] for row in await cur.fetchall()]
            if not found:
                return {"status": "error", "message": f"No deleted credits found with ids {credit_ids}."}
            await c.execute(f"UPDATE credits SET deleted_at = NULL WHERE {where}", credit_ids)
            await c.commit()
            return {"status": "success", "count": len(found), "restored": found, "message": "Credits restored successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def add_saving(date, amount, savings_type, scheme="", note=""):
    '''Record money put into savings. savings_type must be "mutual_fund", "fd" or "rd".
    Use scheme for the fund or bank name, and note for anything else such as the interest rate or maturity date.'''
    if savings_type not in SAVINGS_TYPES:
        return {"status": "error", "message": f"Unknown savings_type '{savings_type}'. Valid: {list(SAVINGS_TYPES)}"}
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                "INSERT INTO savings(date, amount, savings_type, scheme, note) VALUES (?,?,?,?,?)",
                (date, amount, savings_type, scheme, note)
            )
            saving_id = cur.lastrowid
            await c.commit()
            return {"status": "success", "id": saving_id, "message": "Saving added successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def list_savings(start_date, end_date, savings_type=None):
    '''List savings within an inclusive date range, optionally for one savings_type.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            query = """
                SELECT id, date, amount, savings_type, scheme, note
                FROM savings
                WHERE date BETWEEN ? AND ? AND deleted_at IS NULL
            """
            params = [start_date, end_date]
            if savings_type:
                query += " AND savings_type = ?"
                params.append(savings_type)
            query += " ORDER BY date DESC, id DESC"
            cur = await c.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error listing savings: {str(e)}"}

@mcp.tool()
async def summarize_savings(start_date, end_date):
    '''Show the total amount saved in each savings_type within an inclusive date range.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                """
                SELECT savings_type, SUM(amount) AS total_amount, COUNT(*) AS count
                FROM savings
                WHERE date BETWEEN ? AND ? AND deleted_at IS NULL
                GROUP BY savings_type
                ORDER BY total_amount DESC
                """,
                (start_date, end_date)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error summarizing savings: {str(e)}"}

@mcp.tool()
async def edit_saving(saving_id, date=None, amount=None, savings_type=None, scheme=None, note=None):
    '''Edit a saving by id. Only the fields you pass are changed; pass scheme="" or note="" to clear them.
    savings_type must be "mutual_fund", "fd" or "rd". Use list_savings to find the id.'''
    fields = {"date": date, "amount": amount, "savings_type": savings_type, "scheme": scheme, "note": note}
    changes = {name: value for name, value in fields.items() if value is not None}
    if not changes:
        return {"status": "error", "message": "No fields to update."}
    if savings_type is not None and savings_type not in SAVINGS_TYPES:
        return {"status": "error", "message": f"Unknown savings_type '{savings_type}'. Valid: {list(SAVINGS_TYPES)}"}
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM savings WHERE id = ? AND deleted_at IS NULL", (saving_id,))
            row = await cur.fetchone()
            if row is None:
                return {"status": "error", "message": f"No saving found with id {saving_id}."}

            # Column names come from the fixed `fields` dict above, never from user input
            assignments = ", ".join(f"{name} = ?" for name in changes)
            await c.execute(
                f"UPDATE savings SET {assignments} WHERE id = ?",
                (*changes.values(), saving_id)
            )
            await c.commit()
            return {"status": "success", "saving": {**dict(row), **changes}, "message": "Saving updated successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def delete_saving(saving_id):
    '''Delete a saving by id. It is hidden from lists and totals and can be brought back with restore_savings.
    Use list_savings to find the id.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM savings WHERE id = ? AND deleted_at IS NULL", (saving_id,))
            row = await cur.fetchone()
            if row is None:
                return {"status": "error", "message": f"No saving found with id {saving_id}."}
            await c.execute("UPDATE savings SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (saving_id,))
            await c.commit()
            return {"status": "success", "deleted": dict(row), "message": "Saving deleted successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def list_deleted_savings():
    '''List deleted savings that can be brought back with restore_savings.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                """
                SELECT id, date, amount, savings_type, scheme, note, deleted_at
                FROM savings
                WHERE deleted_at IS NOT NULL
                ORDER BY deleted_at DESC, id DESC
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error listing deleted savings: {str(e)}"}

@mcp.tool()
async def restore_savings(saving_ids: list[int]):
    '''Bring back deleted savings by id. Use list_deleted_savings to find the ids.'''
    if not saving_ids:
        return {"status": "error", "message": "Provide at least one saving id."}
    placeholders = ", ".join("?" for _ in saving_ids)
    where = f"id IN ({placeholders}) AND deleted_at IS NOT NULL"
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(f"SELECT id FROM savings WHERE {where}", saving_ids)
            found = [row[0] for row in await cur.fetchall()]
            if not found:
                return {"status": "error", "message": f"No deleted savings found with ids {saving_ids}."}
            await c.execute(f"UPDATE savings SET deleted_at = NULL WHERE {where}", saving_ids)
            await c.commit()
            return {"status": "success", "count": len(found), "restored": found, "message": "Savings restored successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Database error: {str(e)}"}

@mcp.tool()
async def get_balance(start_date, end_date):
    '''Show total credits, expenses and savings within an inclusive date range, with the balance (credits minus
    expenses) and what is left after savings (balance minus savings).'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE date BETWEEN ? AND ? AND deleted_at IS NULL",
                (start_date, end_date)
            )
            total_credits = (await cur.fetchone())[0]
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN ? AND ? AND deleted_at IS NULL",
                (start_date, end_date)
            )
            total_expenses = (await cur.fetchone())[0]
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM savings WHERE date BETWEEN ? AND ? AND deleted_at IS NULL",
                (start_date, end_date)
            )
            total_savings = (await cur.fetchone())[0]
            balance = total_credits - total_expenses
            return {
                "start_date": start_date,
                "end_date": end_date,
                "total_credits": total_credits,
                "total_expenses": total_expenses,
                "total_savings": total_savings,
                "balance": balance,
                "left_after_savings": balance - total_savings
            }
    except Exception as e:
        return {"status": "error", "message": f"Error calculating balance: {str(e)}"}

@mcp.tool()
async def list_expenses(start_date, end_date):  # Changed: added async
    '''List expense entries within an inclusive date range.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:  # Changed: added async
            cur = await c.execute(  # Changed: added await
                """
                SELECT id, date, amount, category, subcategory, note
                FROM expenses
                WHERE date BETWEEN ? AND ? AND deleted_at IS NULL
                ORDER BY date DESC, id DESC
                """,
                (start_date, end_date)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]  # Changed: added await
    except Exception as e:
        return {"status": "error", "message": f"Error listing expenses: {str(e)}"}

@mcp.tool()
async def summarize(start_date, end_date, category=None):  # Changed: added async
    '''Summarize expenses by category within an inclusive date range.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:  # Changed: added async
            query = """
                SELECT category, SUM(amount) AS total_amount, COUNT(*) as count
                FROM expenses
                WHERE date BETWEEN ? AND ? AND deleted_at IS NULL
            """
            params = [start_date, end_date]

            if category:
                query += " AND category = ?"
                params.append(category)

            query += " GROUP BY category ORDER BY total_amount DESC"

            cur = await c.execute(query, params)  # Changed: added await
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]  # Changed: added await
    except Exception as e:
        return {"status": "error", "message": f"Error summarizing expenses: {str(e)}"}

@mcp.resource("expense:///categories", mime_type="application/json")  # Changed: expense:// → expense:///
def categories():
    try:
        # Provide default categories if file doesn't exist
        default_categories = {
            "categories": [
                "Food & Dining",
                "Transportation",
                "Shopping",
                "Entertainment",
                "Bills & Utilities",
                "Healthcare",
                "Travel",
                "Education",
                "Business",
                "Other"
            ]
        }
        
        try:
            with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            import json
            return json.dumps(default_categories, indent=2)
    except Exception as e:
        return f'{{"error": "Could not load categories: {str(e)}"}}'

# Start the server
if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
    # mcp.run()