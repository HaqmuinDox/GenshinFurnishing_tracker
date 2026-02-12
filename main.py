import tkinter as tk
from tkinter import ttk, messagebox
import requests
import sqlite3
import re
import time
import threading

# --- CONFIGURATION ---
API_URL = "https://genshin-impact.fandom.com/api.php"
DB_FILE = 'db.db'


# --- BACKEND LOGIC ---

class GenshinFurnishingManager:
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_file)

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''CREATE TABLE IF NOT EXISTS FurnishingSets (
                            id INTEGER PRIMARY KEY,
                            link TEXT UNIQUE,
                            name TEXT,
                            obtained INTEGER DEFAULT 0 CHECK (obtained IN (0, 1))
                        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS Furnishings (
                            id INTEGER PRIMARY KEY,
                            link TEXT UNIQUE,
                            name TEXT,
                            recipeType TEXT,
                            ingredient1id INTEGER, quantity1 INTEGER,
                            ingredient2id INTEGER, quantity2 INTEGER,
                            ingredient3id INTEGER, quantity3 INTEGER,
                            obtained INTEGER DEFAULT 0 CHECK (obtained IN (0, 1))
                        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS Ingredients (
                            id INTEGER PRIMARY KEY,
                            link TEXT UNIQUE,
                            name TEXT,
                            inventory INTEGER DEFAULT 0
                        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS FurnishingSet_Furnishing (
                            id INTEGER PRIMARY KEY,
                            furnishingSetID INTEGER,
                            furnishingID INTEGER,
                            quantity INTEGER,
                            FOREIGN KEY(furnishingSetID) REFERENCES FurnishingSets(id),
                            FOREIGN KEY(furnishingID) REFERENCES Furnishings(id)
                        )''')
        conn.commit()
        conn.close()

    def drop_tables(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS FurnishingSet_Furnishing")
        cursor.execute("DROP TABLE IF EXISTS FurnishingSets")
        cursor.execute("DROP TABLE IF EXISTS Furnishings")
        cursor.execute("DROP TABLE IF EXISTS Ingredients")
        conn.commit()
        conn.close()
        self.init_db()

    def toggle_set_obtained(self, set_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        # Toggle 1 <-> 0
        cursor.execute("UPDATE FurnishingSets SET obtained = NOT obtained WHERE id = ?", (set_id,))
        conn.commit()
        conn.close()
        self.recalculate_ingredients()

    def recalculate_ingredients(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        # Reset inventory
        cursor.execute("UPDATE Ingredients SET inventory = 0")

        # 1. Get furnishings needed by UNOBTAINED sets
        cursor.execute('''
            SELECT fsf.furnishingID, MAX(fsf.quantity)
            FROM FurnishingSet_Furnishing fsf
            JOIN FurnishingSets fs ON fsf.furnishingSetID = fs.id
            WHERE fs.obtained = 0
            GROUP BY fsf.furnishingID
        ''')

        requirements = cursor.fetchall()

        for furn_id, req_qty in requirements:
            # Get recipe
            cursor.execute('''
                SELECT ingredient1id, quantity1, 
                       ingredient2id, quantity2, 
                       ingredient3id, quantity3 
                FROM Furnishings WHERE id = ?
            ''', (furn_id,))

            row = cursor.fetchone()
            if not row:
                continue

            ingredients = [(row[0], row[1]), (row[2], row[3]), (row[4], row[5])]

            for ing_id, ing_qty in ingredients:
                if ing_id and ing_qty:
                    total = req_qty * ing_qty
                    cursor.execute("UPDATE Ingredients SET inventory = inventory + ? WHERE id = ?", (total, ing_id))

        conn.commit()
        conn.close()

    # --- API SCRAPING FUNCTIONS ---

    def get_page_wikitext(self, title):
        params = {
            "action": "query", "prop": "revisions", "titles": title, "rvprop": "content",
            "rvslots": "main", "formatversion": "2", "format": "json", "redirects": 1
        }
        for attempt in range(3):
            try:
                response = requests.get(API_URL, params=params, timeout=10)
                data = response.json()
                if 'query' in data and 'pages' in data['query']:
                    page = data['query']['pages'][0]
                    if 'missing' in page: return None
                    if 'revisions' in page: return page['revisions'][0]['slots']['main']['content']
                return None
            except Exception:
                time.sleep(1)
        return None

    def extract_template_block(self, text, template_name):
        start_regex = re.compile(r'\{\{\s*' + re.escape(template_name) + r'[\s|]', re.IGNORECASE)
        match = start_regex.search(text)
        if not match: return None
        start_pos = match.start()
        depth = 0
        i = start_pos
        while i < len(text):
            if text.startswith('{{', i):
                depth += 1
                i += 2
            elif text.startswith('}}', i):
                depth -= 1
                i += 2
                if depth == 0: return text[start_pos:i]
            else:
                i += 1
        return None

    def perform_full_refresh(self, status_callback):
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            # 1. Fetch Sets
            status_callback("Fetching Gift Sets list...")
            params = {"action": "query", "list": "categorymembers", "cmtitle": "Category:Gift_Sets", "cmlimit": "max",
                      "cmnamespace": 0, "format": "json"}
            response = requests.get(API_URL, params=params)
            if response.status_code == 200:
                data = response.json()
                gift_sets = data.get("query", {}).get("categorymembers", [])
                for page in gift_sets:
                    name = page["title"]
                    link = f"/wiki/{name.replace(' ', '_')}"
                    cursor.execute('INSERT OR IGNORE INTO FurnishingSets (link, name) VALUES (?, ?)', (link, name))
                conn.commit()

            # 2. Process Sets
            status_callback("Processing Furnishing Sets (finding items)...")
            cursor.execute('SELECT id, name FROM FurnishingSets')
            sets = cursor.fetchall()
            ignored_keys = ['type', 'yield', 'sort', 'description', 'reference']

            for i, (set_id, set_name) in enumerate(sets):
                status_callback(f"Analyzing Set {i + 1}/{len(sets)}: {set_name}")
                wikitext = self.get_page_wikitext(set_name)
                if not wikitext: continue

                recipe_block = self.extract_template_block(wikitext, "Recipe")
                if recipe_block:
                    inner_content = recipe_block[8:-2]
                    parts = inner_content.split('|')
                    candidates = {}
                    for part in parts:
                        if '=' not in part: continue
                        key_raw, val_raw = part.split('=', 1)
                        key = key_raw.strip()
                        val = val_raw.strip()
                        if key.lower() in ignored_keys: continue
                        try:
                            qty_clean = re.split(r'\D', val)[0]
                            qty = int(qty_clean) if qty_clean else 1
                            candidates[key] = qty
                        except ValueError:
                            continue

                    for furn_name, quantity in candidates.items():
                        link = f"/wiki/{furn_name.replace(' ', '_')}"
                        cursor.execute('INSERT OR IGNORE INTO Furnishings (link, name) VALUES (?, ?)',
                                       (link, furn_name))
                        cursor.execute('SELECT id FROM Furnishings WHERE name = ?', (furn_name,))
                        res = cursor.fetchone()
                        if res:
                            furn_id = res[0]
                            cursor.execute(
                                'INSERT OR IGNORE INTO FurnishingSet_Furnishing (furnishingSetID, furnishingID, quantity) VALUES (?, ?, ?)',
                                (set_id, furn_id, quantity))
                    conn.commit()
                time.sleep(0.2)

            # 3. Process Recipes
            status_callback("Processing Furnishing Recipes (finding ingredients)...")
            cursor.execute('SELECT id, name FROM Furnishings')
            furnishings = cursor.fetchall()
            allowed_types = ["Creation", "Combine", "Forging", "Cooking", "Processing"]
            ignored_keys_rec = ['type', 'time', 'yield', 'sort', 'description', 'reference', 'stars', 'source']

            for i, (furn_id, furn_name) in enumerate(furnishings):
                if i % 10 == 0: status_callback(f"Analyzing Item {i + 1}/{len(furnishings)}")

                wikitext = self.get_page_wikitext(furn_name)
                if not wikitext: continue

                recipe_block = self.extract_template_block(wikitext, "Recipe")
                if recipe_block:
                    inner_content = recipe_block[8:-2]
                    parts = inner_content.split('|')
                    found_ingredients = []
                    recipe_type = "Unknown"
                    for part in parts:
                        if '=' not in part: continue
                        k, v = part.split('=', 1)
                        k = k.strip()
                        v = v.strip()
                        if k.lower() == 'type': recipe_type = v; continue
                        if k.lower() in ignored_keys_rec: continue
                        try:
                            q = int(re.split(r'\D', v)[0]) if re.split(r'\D', v)[0] else 1
                            found_ingredients.append((k, q))
                        except:
                            continue

                    cursor.execute("UPDATE Furnishings SET recipeType = ? WHERE id = ?", (recipe_type, furn_id))
                    if recipe_type in allowed_types and found_ingredients:
                        for idx in range(min(len(found_ingredients), 3)):
                            ing_name, ing_qty = found_ingredients[idx]
                            ing_link = f"/wiki/{ing_name.replace(' ', '_')}"
                            cursor.execute('INSERT OR IGNORE INTO Ingredients (link, name) VALUES (?, ?)',
                                           (ing_link, ing_name))
                            cursor.execute('SELECT id FROM Ingredients WHERE name = ?', (ing_name,))
                            ing_res = cursor.fetchone()
                            if ing_res:
                                query = f"UPDATE Furnishings SET ingredient{idx + 1}id = ?, quantity{idx + 1} = ? WHERE id = ?"
                                cursor.execute(query, (ing_res[0], ing_qty, furn_id))
                    conn.commit()
                time.sleep(0.2)

            status_callback("Recalculating totals...")
            conn.close()
            self.recalculate_ingredients()
            status_callback("Done")

        except Exception as e:
            print(e)
            status_callback(f"Error: {e}")
            conn.close()


# --- FRONTEND (GUI) ---
#just testing the commit
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Genshin Furnishing Calculator")
        self.root.geometry("800x600")

        self.manager = GenshinFurnishingManager(DB_FILE)

        # Menu
        menubar = tk.Menu(root)
        db_menu = tk.Menu(menubar, tearoff=0)
        db_menu.add_command(label="Full Refresh (Drop & Scrape)", command=self.confirm_full_refresh)
        db_menu.add_separator()
        db_menu.add_command(label="Exit", command=root.quit)
        menubar.add_cascade(label="Database", menu=db_menu)
        root.config(menu=menubar)

        # Tabs
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=10)

        self.setup_ingredients_tab()
        self.setup_sets_tab()

        # Status Bar
        self.status_var = tk.StringVar()
        self.status_var.set("Ready")
        self.status_bar = tk.Label(root, textvariable=self.status_var, bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # Initial Load
        self.refresh_ui()

    def setup_ingredients_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Ingredients Required")

        columns = ("Name", "Total Required")
        self.ing_tree = ttk.Treeview(frame, columns=columns, show='headings')
        self.ing_tree.heading("Name", text="Ingredient Name",
                              command=lambda: self.sort_column(self.ing_tree, "Name", False))
        self.ing_tree.heading("Total Required", text="Total Required",
                              command=lambda: self.sort_column(self.ing_tree, "Total Required", False))

        self.ing_tree.column("Name", width=400)
        self.ing_tree.column("Total Required", width=100, anchor='center')

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.ing_tree.yview)
        self.ing_tree.configure(yscroll=scrollbar.set)

        self.ing_tree.pack(side=tk.LEFT, fill='both', expand=True)
        scrollbar.pack(side=tk.RIGHT, fill='y')

    def setup_sets_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Furnishing Sets")

        columns = ("ID", "Name", "Obtained")
        self.sets_tree = ttk.Treeview(frame, columns=columns, show='headings')
        self.sets_tree.heading("ID", text="ID")
        self.sets_tree.heading("Name", text="Set Name", command=lambda: self.sort_column(self.sets_tree, "Name", False))
        self.sets_tree.heading("Obtained", text="Obtained?",
                               command=lambda: self.sort_column(self.sets_tree, "Obtained", False))

        self.sets_tree.column("ID", width=50, stretch=False)
        self.sets_tree.column("Name", width=500)
        self.sets_tree.column("Obtained", width=100, anchor='center')

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.sets_tree.yview)
        self.sets_tree.configure(yscroll=scrollbar.set)

        self.sets_tree.pack(side=tk.LEFT, fill='both', expand=True)
        scrollbar.pack(side=tk.RIGHT, fill='y')

        # Bind Double Click
        self.sets_tree.bind("<Double-1>", self.on_set_double_click)

    def refresh_ui(self):
        # Refresh Ingredients
        for row in self.ing_tree.get_children():
            self.ing_tree.delete(row)

        conn = self.manager.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT name, inventory FROM Ingredients WHERE inventory > 0 ORDER BY inventory DESC")
        for name, qty in cursor.fetchall():
            self.ing_tree.insert("", tk.END, values=(name, qty))

        # Refresh Sets
        for row in self.sets_tree.get_children():
            self.sets_tree.delete(row)

        cursor.execute("SELECT id, name, obtained FROM FurnishingSets ORDER BY name")
        for sid, name, obt in cursor.fetchall():
            status = "YES" if obt == 1 else "NO"
            tag = "obtained" if obt == 1 else "needed"
            self.sets_tree.insert("", tk.END, values=(sid, name, status), tags=(tag,))

        conn.close()

        self.sets_tree.tag_configure("obtained", foreground="green")
        self.sets_tree.tag_configure("needed", foreground="black")

    def on_set_double_click(self, event):
        selection = self.sets_tree.selection()
        if not selection: return
        item = selection[0]
        values = self.sets_tree.item(item, "values")
        set_id = values[0]
        self.manager.toggle_set_obtained(set_id)
        self.refresh_ui()

    def confirm_full_refresh(self):
        if messagebox.askyesno("Confirm Refresh",
                               "This will delete all data and scrape the Wiki again.\nIt may take several minutes.\n\nProceed?"):
            self.run_full_refresh()

    def run_full_refresh(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Scraping...")
        dialog.geometry("400x100")
        lbl = tk.Label(dialog, text="Starting...", padx=20, pady=20)
        lbl.pack()

        progress = ttk.Progressbar(dialog, mode='indeterminate')
        progress.pack(fill=tk.X, padx=20)
        progress.start()

        def worker():
            self.manager.drop_tables()

            def update_status(msg):
                self.root.after(0, lambda: self.status_var.set(msg))
                self.root.after(0, lambda: lbl.config(text=msg))

            self.manager.perform_full_refresh(update_status)
            self.root.after(0, progress.stop)
            self.root.after(0, dialog.destroy)
            self.root.after(0, self.refresh_ui)
            self.root.after(0, lambda: messagebox.showinfo("Done", "Database refreshed successfully!"))

        threading.Thread(target=worker, daemon=True).start()

    def sort_column(self, tree, col, reverse):
        l = [(tree.set(k, col), k) for k in tree.get_children('')]
        try:
            l.sort(key=lambda t: int(t[0]), reverse=reverse)
        except ValueError:
            l.sort(key=lambda t: t[0], reverse=reverse)
        for index, (val, k) in enumerate(l):
            tree.move(k, '', index)
        tree.heading(col, command=lambda: self.sort_column(tree, col, not reverse))


# --- MAIN ENTRY POINT ---
if __name__ == "__main__":
    print("Initializing Application...")
    root = tk.Tk()
    app = App(root)
    print("Entering Main Loop...")
    root.mainloop()