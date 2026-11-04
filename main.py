from fastmcp import FastMCP
import json
import os
import sys
import aiosqlite  # Changed: sqlite3 → aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expenses.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")
CREDIT_TYPES = ("salary", "credit")

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
            cur = await c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
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
    '''Permanently delete an expense by id. This cannot be undone, so confirm with the user first.
    Use list_expenses to find the id.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
            row = await cur.fetchone()
            if row is None:
                return {"status": "error", "message": f"No expense found with id {expense_id}."}
            await c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
            await c.commit()
            return {"status": "success", "deleted": dict(row), "message": "Expense deleted successfully"}
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
                WHERE date BETWEEN ? AND ?
                ORDER BY date DESC, id DESC
                """,
                (start_date, end_date)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]
    except Exception as e:
        return {"status": "error", "message": f"Error listing credits: {str(e)}"}

@mcp.tool()
async def get_balance(start_date, end_date):
    '''Show total credits, total expenses and the balance (credits minus expenses) within an inclusive date range.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE date BETWEEN ? AND ?",
                (start_date, end_date)
            )
            total_credits = (await cur.fetchone())[0]
            cur = await c.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN ? AND ?",
                (start_date, end_date)
            )
            total_expenses = (await cur.fetchone())[0]
            return {
                "start_date": start_date,
                "end_date": end_date,
                "total_credits": total_credits,
                "total_expenses": total_expenses,
                "balance": total_credits - total_expenses
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
                WHERE date BETWEEN ? AND ?
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
                WHERE date BETWEEN ? AND ?
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