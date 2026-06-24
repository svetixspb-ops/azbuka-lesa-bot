"""
Обходит сайт alyansles.ru, собирает карту "название товара → URL страницы"
и добавляет колонку url в catalog.db.

Запуск: python crawl_product_urls.py
"""
import sqlite3
import time
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE_URL = "https://www.alyansles.ru"
DB_PATH = Path(__file__).parent / "catalog.db"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NawiBot/1.0)"}
DELAY = 0.3  # seconds between requests


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", errors="replace")


class CategoryParser(HTMLParser):
    """Собирает ссылки на категории товаров из /catalog/"""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            href = d.get("href", "")
            if re.match(r"^/catalog/[a-z0-9_]+/$", href) and href != "/catalog/":
                self.links.append(href)


class ProductLinkParser(HTMLParser):
    """Собирает ссылки на товарные страницы /catalog/items/..."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            href = d.get("href", "")
            if re.match(r"^/catalog/items/[a-z0-9_]+/$", href):
                if href not in self.links:
                    self.links.append(href)


class ProductNameParser(HTMLParser):
    """Извлекает название товара из h1 на странице товара."""
    def __init__(self):
        super().__init__()
        self.in_h1 = False
        self.name = ""

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self.in_h1 = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_h1 = False

    def handle_data(self, data):
        if self.in_h1:
            self.name += data


def get_category_links() -> list[str]:
    html = fetch(BASE_URL + "/catalog/")
    p = CategoryParser()
    p.feed(html)
    seen = set()
    result = []
    for link in p.links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


def get_product_links_from_category(cat_path: str) -> list[str]:
    """Получает все товарные ссылки из категории, включая пагинацию."""
    links = []
    page = 1
    while True:
        if page == 1:
            url = BASE_URL + cat_path
        else:
            # Bitrix pagination: ?PAGEN_1=N
            url = BASE_URL + cat_path + f"?PAGEN_1={page}"
        try:
            html = fetch(url)
        except Exception:
            break
        p = ProductLinkParser()
        p.feed(html)
        new = [l for l in p.links if l not in links]
        if not new:
            break
        links.extend(new)
        # Если меньше 10 товаров — скорее всего последняя страница
        if len(new) < 10:
            break
        page += 1
        time.sleep(DELAY)
    return links


def get_product_name(product_path: str) -> str:
    html = fetch(BASE_URL + product_path)
    p = ProductNameParser()
    p.feed(html)
    return p.name.strip()


def add_url_column():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE products ADD COLUMN url TEXT")
        conn.commit()
        print("Колонка url добавлена")
    except sqlite3.OperationalError:
        print("Колонка url уже существует")
    conn.close()


def update_urls(name_to_url: dict[str, str]) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    updated = 0
    for name, url in name_to_url.items():
        c.execute(
            "UPDATE products SET url=? WHERE name_lower=?",
            (url, name.lower())
        )
        updated += c.rowcount
    conn.commit()
    conn.close()
    return updated


def main():
    add_url_column()

    print("Получаю список категорий…")
    cat_links = get_category_links()
    print(f"Найдено категорий: {len(cat_links)}")

    all_product_paths = []
    for i, cat in enumerate(cat_links, 1):
        print(f"[{i}/{len(cat_links)}] {cat}")
        try:
            paths = get_product_links_from_category(cat)
            all_product_paths.extend(paths)
            print(f"  → {len(paths)} товаров")
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(DELAY)

    # Дедупликация
    all_product_paths = list(dict.fromkeys(all_product_paths))
    print(f"\nВсего товарных страниц: {len(all_product_paths)}")

    name_to_url: dict[str, str] = {}
    for i, path in enumerate(all_product_paths, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(all_product_paths)} обработано…")
        try:
            name = get_product_name(path)
            if name:
                name_to_url[name] = BASE_URL + path
        except Exception as e:
            print(f"  ERROR {path}: {e}")
        time.sleep(DELAY)

    print(f"\nСобрано имён с URL: {len(name_to_url)}")

    updated = update_urls(name_to_url)
    print(f"Обновлено записей в БД: {updated}")

    # Отчёт о покрытии
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    covered = conn.execute("SELECT COUNT(*) FROM products WHERE url IS NOT NULL").fetchone()[0]
    conn.close()
    print(f"Покрытие: {covered}/{total} товаров ({covered*100//total}%)")


if __name__ == "__main__":
    main()
