#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, re, json, pathlib, hashlib, datetime, subprocess, unicodedata, shutil

# === НАСТРОЙКИ ===
VAULT = "/Users/alakey/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian"  # путь к твоему Obsidian
REPO  = os.path.abspath(os.path.dirname(__file__))
OUT_POSTS  = os.path.join(REPO, "content", "posts")
OUT_IMAGES = os.path.join(REPO, "static", "images")
STATE = os.path.expanduser("~/.obsidian_to_hugo_state.json")

# где искать вложения в Obsidian
ATTACH_DIR_CANDIDATES = ["Attachments", "assets", "images", "Cache"]

# какие расширения считаем картинками
IMAGE_EXTS = {".png",".jpg",".jpeg",".gif",".webp",".avif",".svg",".heic",".heif"}

# ПАРАМЕТРЫ ОБРАБОТКИ ИЗОБРАЖЕНИЙ
MAX_WIDTH = 1600             # px (масштабируем по большей стороне)
JPEG_QUALITY = 80            # 1..100 (sips: formatOptions)
RECOMPRESS_JPEG = True       # перепаковывать даже уже JPEG
CONVERT_HEIC_TO_JPEG = True  # heic/heif -> jpeg

# === РЕГЕКСПЫ ===
PUBLIC_TAG_PATTERN = re.compile(r'(^|\s)#public(\s|$)', re.IGNORECASE)
WIKILINK_IMG_PATTERN = re.compile(r'!\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')  # ![[path/name.ext|alt]]

os.makedirs(OUT_POSTS, exist_ok=True)
os.makedirs(OUT_IMAGES, exist_ok=True)

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------

def is_public(text: str) -> bool:
    return PUBLIC_TAG_PATTERN.search(text) is not None

def strip_frontmatter(text: str):
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            head = parts[0].lstrip("-\n")
            body = parts[1].lstrip("\n")
            meta = {}
            for line in head.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"\'')
            return body, meta
    return text, {}

def first_h1(text: str):
    m = re.search(r'^\s*#\s+(.+)$', text, flags=re.M)
    return m.group(1).strip() if m else None

def file_date_iso(p: str) -> str:
    ts = os.path.getmtime(p)
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).lower().strip()
    s = re.sub(r"\s+", "-", s)
    trans = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh","з":"z","и":"i","й":"j","к":"k",
        "л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c",
        "ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"
    }
    s = "".join(trans.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80] if s else "post"

def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_state(state):
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def normalize_body_remove_public(text: str) -> str:
    body, _ = strip_frontmatter(text)
    body = PUBLIC_TAG_PATTERN.sub(" ", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return body

def write_hugo_post(dst_path: str, title: str, date_str: str, body: str):
    fm = f"---\ntitle: \"{title}\"\ndate: {date_str}\ndraft: false\n---\n\n"
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(fm + body)

def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, check=False, capture_output=True, text=True)

# ---------- ПОИСК И КОПИРОВАНИЕ ИЗОБРАЖЕНИЙ ----------

def _candidate_paths_for(note_path: str, ref: str):
    paths = []
    ref = ref.replace("\\", "/")
    base_dir = os.path.dirname(note_path)
    paths.append(os.path.normpath(os.path.join(base_dir, ref)))  # относительный путь от заметки
    for d in ATTACH_DIR_CANDIDATES:                               # стандартные папки
        paths.append(os.path.join(VAULT, d, os.path.basename(ref)))
    paths.append(os.path.join(base_dir, os.path.basename(ref)))    # просто basename рядом
    return paths

def find_asset(note_path: str, ref: str) -> str | None:
    has_ext = any(ref.lower().endswith(ext) for ext in IMAGE_EXTS)
    candidates = _candidate_paths_for(note_path, ref)
    if not has_ext:
        candidates = [p + ext for p in candidates for ext in IMAGE_EXTS]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # последний шанс: глобальный поиск по Vault
    base = os.path.basename(ref)
    if not has_ext:
        names = {base + ext for ext in IMAGE_EXTS}
        for path in pathlib.Path(VAULT).rglob("*"):
            if path.is_file() and path.name in names:
                return str(path)
    else:
        for path in pathlib.Path(VAULT).rglob(base):
            if path.is_file():
                return str(path)
    return None

def sips_process(src: str, dst_jpg: str):
    """
    Обрабатывает изображение через macOS `sips`:
    - при необходимости конвертирует в JPEG
    - ограничивает ширину/высоту (MAX_WIDTH)
    - задаёт качество JPEG (JPEG_QUALITY)
    """
    # создаём временный файл-назначение в папке OUT_IMAGES
    tmp = dst_jpg + ".tmp.jpg"
    # сначала копия исходника (sips иногда пишет "in-place")
    shutil.copy2(src, tmp)
    # ресайз по большей стороне
    subprocess.run(["sips", "-Z", str(MAX_WIDTH), tmp], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # перевод в JPEG + качество
    subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(JPEG_QUALITY), tmp, "--out", dst_jpg],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass

def copy_to_static_images(src_path: str) -> str:
    """
    Копирует файл в static/images с обработкой:
    - HEIC/HEIF всегда -> JPEG
    - JPEG (и если RECOMPRESS_JPEG=True): перепаковываем с качеством и ресайзом
    - остальное: просто копируем как есть
    Возвращает имя файла в целевой папке.
    """
    name = os.path.basename(src_path)
    stem, ext = os.path.splitext(name)
    ext_low = ext.lower()

    # целевые имена
    if CONVERT_HEIC_TO_JPEG and ext_low in {".heic", ".heif"}:
        name = f"{stem}.jpg"
        dst = os.path.join(OUT_IMAGES, name)
        sips_process(src_path, dst)
        return name

    if RECOMPRESS_JPEG and ext_low in {".jpg", ".jpeg"}:
        dst = os.path.join(OUT_IMAGES, f"{stem}.jpg")
        sips_process(src_path, dst)
        return f"{stem}.jpg"

    # остальное — просто копируем (PNG, GIF, WEBP, SVG, AVIF и т.д.)
    dst = os.path.join(OUT_IMAGES, name)
    # если есть коллизия по имени с другим содержимым — добавим 8 символов хэша
    if os.path.exists(dst):
        if hashlib.sha256(open(dst, "rb").read()).hexdigest() != hashlib.sha256(open(src_path, "rb").read()).hexdigest():
            name = f"{stem}-{hashlib.sha256(open(src_path,'rb').read()).hexdigest()[:8]}{ext_low}"
            dst = os.path.join(OUT_IMAGES, name)
    shutil.copy2(src_path, dst)
    return name

def replace_wikilink_images(md_text: str, note_path: str) -> str:
    """Заменяет ![[...]] на стандартный Markdown и копирует файлы в static/images/."""
    def _repl(m):
        ref = m.group(1).strip()
        alt = (m.group(2) or "").strip()
        src = find_asset(note_path, ref)
        if not src:
            return f"![NOTFOUND {ref}]()"
        fname = copy_to_static_images(src)
        alt_text = alt if alt else pathlib.Path(ref).stem
        return f"![{alt_text}](/images/{fname})"
    return WIKILINK_IMG_PATTERN.sub(_repl, md_text)

# ---------- ОСНОВНОЙ ЦИКЛ ----------

def main():
    state = load_state()
    changed = False
    seen_notes = set()     # заметки, которые мы обработали как #public на текущем прогоне
    produced_slugs = set() # слаги, которые есть сейчас

    # 1) проходимся по всем md в Vault
    for p in pathlib.Path(VAULT).rglob("*.md"):
        p = str(p)
        try:
            raw = open(p, "r", encoding="utf-8").read()
        except Exception:
            continue

        if is_public(raw):
            seen_notes.add(p)

            body = normalize_body_remove_public(raw)
            body = replace_wikilink_images(body, p)  # подменим ![[...]] и скопируем файлы

            _, meta = strip_frontmatter(raw)
            title = first_h1(body) or meta.get("title") or pathlib.Path(p).stem
            date_str = meta.get("date") or file_date_iso(p)

            st = state.get(p, {})
            if "slug" not in st:
                st["slug"] = slugify(title)

            dst_post = os.path.join(OUT_POSTS, f"{st['slug']}.md")
            content_for_hash = f"{title}\n{date_str}\n{body}"
            h = sha256(content_for_hash)

            if not (st.get("hash") == h and os.path.exists(dst_post)):
                write_hugo_post(dst_post, title, date_str, body)
                st["hash"] = h
                changed = True

            state[p] = st
            produced_slugs.add(st["slug"])

    # 2) УДАЛЕНИЕ ПОСТОВ, если сняли #public или сам файл удалили
    #    Находим в state записи, которых нет в seen_notes → значит тег пропал/файл исчез
    to_delete = []
    for note_path, st in list(state.items()):
        if not os.path.exists(note_path) or note_path not in seen_notes:
            slug = st.get("slug")
            if slug:
                post_path = os.path.join(OUT_POSTS, f"{slug}.md")
                if os.path.exists(post_path):
                    to_delete.append(post_path)
            # чистим из состояния
            state.pop(note_path, None)

    for post_path in to_delete:
        try:
            os.remove(post_path)
            changed = True
        except FileNotFoundError:
            pass

    # 3) git-операции
    if changed:
        git("add", "content/posts")
        git("add", "static/images")
        git("commit", "-m", "auto: sync #public posts (images compressed, deletions applied)")
        git("push")
        print("✅ Изменения опубликованы.")
    else:
        print("ℹ️ Нет изменений.")

    save_state(state)

if __name__ == "__main__":
    main()
