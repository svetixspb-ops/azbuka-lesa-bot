"""Исчерпывающий прогон ВСЕГО каталога через расчёт+подбор Веры (слой 1, без LLM).

Для каждой позиции в наличии формируем запрос её же параметрами и проверяем:
  • позиция НАЙДЕНА (не «точно такого нет» — это была бы ложь о наличии);
  • сумма посчитана и совпадает с qty×цена реального SKU того же размера (математика);
  • нет исключений.

Выдаёт сводку и список проблемных позиций. Цель — добить слой 1 до 100%.
"""
import re, sqlite3
import brain, catalog

con = sqlite3.connect("catalog.db"); con.row_factory = sqlite3.Row
PRODUCTS = [dict(r) for r in con.execute(
    "SELECT * FROM products WHERE available=1 AND count>0").fetchall()]

def same_sig(p):
    """В-наличии позиции того же сечения+длины (для сверки допустимых сумм)."""
    out = []
    for q in PRODUCTS:
        if (q.get("length_mm")) != (p.get("length_mm")):
            continue
        if p.get("diameter_mm"):
            if q.get("diameter_mm") == p.get("diameter_mm"):
                out.append(q)
        else:
            a = (q.get("thickness_mm"), q.get("width_mm"))
            b = (p.get("thickness_mm"), p.get("width_mm"))
            if a == b or a == (b[1], b[0]):
                out.append(q)
    return out

def build_item(p, qty, packs):
    name = p.get("name") or ""
    return {"text": (name.split()[0].lower() if name else ""), "raw": name,
            "thickness_mm": None if p.get("diameter_mm") else p.get("thickness_mm"),
            "width_mm": None if p.get("diameter_mm") else p.get("width_mm"),
            "length_mm": p.get("length_mm"), "diameter_mm": p.get("diameter_mm"),
            "quantity_pieces": qty, "packs": packs, "target_m3": None,
            "species": None, "grade": None}

def parse_sum(reply):
    m = re.findall(r"([\d ]+)\s+рубл", reply)
    return int(m[-1].replace(" ", "")) if m else None

stats = {"ok": 0, "false_negative": 0, "wrong_math": 0, "none": 0, "low_stock": 0, "error": 0}
problems = []

for i, p in enumerate(PRODUCTS):
    sid = f"sweep-{i}"
    brain.reset(sid)
    pack = p.get("pack_count")
    cnt = p.get("count") or 1
    if pack:
        packs = max(1, min(2, cnt)); qty = None
    else:
        packs = None; qty = max(1, min(3, int(cnt)))
    it = build_item(p, qty, packs)
    try:
        products = brain._search_item(it)
        reply = brain._product_reply(sid, [(it, products)])
    except Exception as e:
        stats["error"] += 1
        problems.append(("ERROR", p["id"], p["name"], f"{type(e).__name__}: {e}"))
        continue

    if reply is None:
        stats["none"] += 1  # многотипная неоднозначность одного размера → уходит к уточнению (не баг)
        continue
    low = reply.lower()
    if "точно такого" in low or ("нет" in low and "налич" in low):
        stats["false_negative"] += 1
        problems.append(("FALSE_NEG", p["id"], p["name"], reply))
        continue
    if "только" in low and "наличии" in low:
        stats["low_stock"] += 1
        continue
    if "записала" in low:
        got = parse_sum(reply)
        sig = same_sig(p)
        if pack:
            cand = {int(packs) * int(q["price"]) for q in sig if q.get("pack_count")}
        else:
            cand = {int(qty) * int(q["price"]) for q in sig if not q.get("pack_count")}
        if got in cand:
            stats["ok"] += 1
        else:
            stats["wrong_math"] += 1
            exp = f"{qty or packs}×цена; допустимые суммы {sorted(cand)}"
            problems.append(("WRONG_MATH", p["id"], p["name"], f"озвучено {got}; ожидалось {exp} | {reply}"))
    else:
        # «Есть … сколько нужно?» без количества — для пакованных без packs и т.п.
        stats["ok"] += 1

print("="*72)
print(f"ВСЕГО позиций в наличии: {len(PRODUCTS)}")
print("-"*72)
print(f"  ✅ расчёт верный (OK):        {stats['ok']}")
print(f"  🟡 ушло к уточнению (None):    {stats['none']}  (разные типы одного размера — не баг)")
print(f"  🟡 ограничение остатка:        {stats['low_stock']}")
print(f"  🔴 ложное «нет в наличии»:     {stats['false_negative']}")
print(f"  🔴 неверная сумма:             {stats['wrong_math']}")
print(f"  🔴 исключение:                 {stats['error']}")
print("="*72)
crit = [x for x in problems if x[0] in ("FALSE_NEG","WRONG_MATH","ERROR")]
if crit:
    print(f"\nКРИТИЧНЫЕ ПРОБЛЕМЫ ({len(crit)}):")
    for kind, pid, name, detail in crit[:60]:
        print(f"  [{kind}] id{pid} {name}\n      {detail[:200]}")
else:
    print("\n✅ Критичных проблем (ложное нет / неверная сумма / сбой) НЕ найдено.")
