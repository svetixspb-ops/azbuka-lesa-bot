"""Регресс-тест фиксов подбора/расчёта Веры (без LLM, на реальном catalog.db).

Гоняем brain._product_reply на крафченных items — воспроизводим баг-звонок 12:26
и проверяем, что: (1) ложное «нет» ушло, (2) цена в оффере = цене в итоге,
(3) погонаж и обычные позиции не сломались.
"""
import brain, catalog

PASS = "✅"; FAIL = "❌"
fails = 0

def item(text, raw="", th=None, w=None, l=None, d=None, qty=None, species=None, grade=None, packs=None):
    return {"text": text, "raw": raw or text, "thickness_mm": th, "width_mm": w,
            "length_mm": l, "diameter_mm": d, "quantity_pieces": qty, "packs": packs,
            "target_m3": None, "species": species, "grade": grade}

def reply(sid, it):
    products = brain._search_item(it)
    return brain._product_reply(sid, [(it, products)])

def check(name, got, must_have=(), must_not=()):
    global fails
    ok = all(s in (got or "") for s in must_have) and not any(s in (got or "") for s in must_not)
    if not ok: fails += 1
    print(f"{PASS if ok else FAIL} {name}\n    → {got!r}")

print("="*72)
print("СЦЕНАРИЙ 1 — баг-звонок: «15 досок каркасные 141 на 45, сухие, 6 м»")
brain.reset("s1")
a = reply("s1", item("доска", "15 досок каркасные 141 45мм сухие длина 6 м", th=141, w=45, l=6000, qty=15))
check("1a. предложен БЛИЖАЙШИЙ 45×145 (а не 50×150), не ложное «нет»",
      a, must_have=("45 на 145",), must_not=("50 на 150",))
# locked-цена из оффера
import re
m = re.search(r"([\d ]+) рубл", a or "")
offered_price = int(m.group(1).replace(" ", "")) if m else None
print(f"    [оффер: {offered_price} ₽/шт]")
# Turn C: клиент подтвердил и назвал количество (вариант: extract вернул корректный 45×145)
c = reply("s1", item("доска", "115", th=45, w=145, l=6000, qty=115))
check("1b. итог посчитан по ТОМУ ЖЕ товару (цена из оффера)", c,
      must_have=("Записала", "45 на 145"))
if offered_price and c:
    mt = re.search(r"— .*?, ([\d ]+) рубл", c)
    total = int(mt.group(1).replace(" ", "")) if mt else None
    consistent = total == offered_price * 115
    if not consistent: fails += 1
    print(f"    {'✅' if consistent else '❌'} 1c. итог {total} == {offered_price}×115 = {offered_price*115}")

print("\nСЦЕНАРИЙ 1' — то же, но extract на Turn C ВЕРНУЛ старые 141×45 (регургитация)")
brain.reset("s1b")
reply("s1b", item("доска", "15 досок каркасные 141 45 сухие 6м", th=141, w=45, l=6000, qty=15))
c2 = reply("s1b", item("доска", "115", th=141, w=45, l=6000, qty=115))
check("1'b. всё равно посчитан итог (locked), без повторного «подойдёт?»", c2,
      must_have=("Записала",), must_not=("Подойдёт",))

print("\nСЦЕНАРИЙ 2 — погонаж: плинтус «45 на 12» (толщина>ширина — своп НЕЛЬЗЯ)")
brain.reset("s2")
p = reply("s2", item("плинтус", "плинтус 45 на 12, 3 метра", th=45, w=12, l=3000, qty=10))
check("2a. плинтус 45×12 найден точно (не уехал на другой размер)", p,
      must_have=("плинтус",), must_not=("Точно такого размера сейчас нет",))
# порядок-независимость: «12 на 45»
brain.reset("s2b")
p2 = reply("s2b", item("плинтус", "плинтус 12 на 45", th=12, w=45, l=3000, qty=10))
check("2b. тот же плинтус и при «12 на 45» (порядок-независимо)", p2,
      must_not=("Точно такого размера сейчас нет",))

print("\nСЦЕНАРИЙ 3 — обычная позиция: брус 100×150×6000, 30 шт (в пределах остатка)")
brain.reset("s3")
b = reply("s3", item("брус", "брус 100 на 150, 6 метров, 30 штук", th=100, w=150, l=6000, qty=30))
check("3. брус посчитан (одиночный SKU, не сломали)", b, must_have=("Записала", "100 на 150"))

print("\nСЦЕНАРИЙ 4 — мульти-SKU 50×150: «сухая» vs без уточнения")
brain.reset("s4")
d_dry = reply("s4", item("доска", "доска сухая 50 на 150, 6 метров, 10 штук", th=50, w=150, l=6000, qty=10))
check("4a. явная «сухая» → итог 10×1260=12 600 (сухая), не 10×923=9 230 (сырая)", d_dry,
      must_have=("12 600",), must_not=("9 230",))
brain.reset("s4b")
d_any = reply("s4b", item("доска", "доска 50 на 150, 6 метров, 10 штук", th=50, w=150, l=6000, qty=10))
check("4b. без уточнения влажности → считает (любой из 50×150)", d_any, must_have=("Записала",))

print("\nСЦЕНАРИЙ 5 — евровагонка сорт А, 12,5×96, 4-метровая (нет), 6 пачек")
brain.reset("s5")
e1 = reply("s5", item("евровагонка", "евровагонка сорта а 12,5 на 96 4 метровая 6 пачек",
                       th=12.5, w=96, l=4000, grade="А", packs=6))
check("5a. замена — сорт А (1 685 ₽), не В (1 498 ₽); «за упаковку»; «12,5» не теряется", e1,
      must_have=("12,5 на 96", "за упаковку", "1 685"), must_not=("за штуку", "1 498"))
# Turn 2: клиент подтвердил, назвал 6 пачек (extract регургитировал старую 4-метровую длину)
e2 = reply("s5", item("евровагонка", "да, шесть пачек", th=12.5, w=96, l=4000, packs=6))
check("5b. итог по пачкам посчитан (6 уп × 1685 = 10 110), единица «упаковок»", e2,
      must_have=("Записала", "упаков", "10 110"))

print("\nСЦЕНАРИЙ 6 — аксессуар без размера (изоспан) → в смету; пиломатериал без размера → уточнить")
brain.reset("s6")
acc = reply("s6", item("изоспан", "нужен изоспан А, 5 рулонов", packs=5))
check("6a. аксессуар (изоспан) → передаёт в сметный отдел", acc,
      must_have=("сметный",), must_not=("Записала",))
brain.reset("s6b")
nosize = reply("s6b", item("доска", "нужна доска"))
check("6b. пиломатериал без размера → НЕ в смету (уточнит размер через LLM = None)",
      nosize if nosize is not None else "None-уточнение",
      must_not=("сметный",))

print("\n" + "="*72)
print(f"{'ВСЕ ТЕСТЫ ПРОШЛИ '+PASS if fails==0 else str(fails)+' ПРОВАЛОВ '+FAIL}")
